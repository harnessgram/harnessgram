"""Tap-to-add team members: a stranger writing in the team group becomes a request to the owner.

The request goes to the owner's home chat only, never into the group and never to the stranger.
It carries the name, username and numeric id from Telegram's own sender field; the words they wrote
are not kept or shown. Only the owner's press adds or ignores them (inbox.handle_button checks that
before calling `button`). The state is small and bounded: one pending request per person, a fixed
number of pending and ignored people, and a cap on requests per hour.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import time
from typing import Optional

from . import config as C, menus
from .config import Config, home_chat, private_dir, state_dir, write_private
from .i18n import t
from .telegram import TelegramError

MAX_PENDING = 10
MAX_IGNORED = 200
MAX_PER_HOUR = 5
PENDING_TTL = 7 * 86400  # an unanswered request expires, so it cannot hold a slot forever
MAX_LISTED = 50  # a message carries at most 100 buttons
_CONTENT = ("text", "caption", "photo", "document", "voice", "video", "audio", "sticker", "animation",
            "video_note")  # a person wrote something; joins, pins and other service messages do not count
_USERNAME = re.compile(r"[A-Za-z0-9_]{1,32}")
_BUTTON = re.compile(r"mb:([aird]):([1-9][0-9]{0,15})(?::([0-9a-f]{16}))?")


def _locked(fn):
    """Run fn(state) on the request state under the lock and save it. State: pending {id: entry},
    ignored [ids, oldest first], hourly [times of the requests sent in the last hour]."""
    root = private_dir(state_dir())
    with open(root / "member-requests.lock", "a") as lock:
        os.chmod(root / "member-requests.lock", 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "member-requests.json"
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            raw = {}
        raw = raw if isinstance(raw, dict) else {}
        pending, ignored, hourly = raw.get("pending"), raw.get("ignored"), raw.get("hourly")
        state = {"pending": pending if isinstance(pending, dict) else {},
                 "ignored": [x for x in ignored if isinstance(x, int)] if isinstance(ignored, list) else [],
                 "hourly": [x for x in hourly if isinstance(x, (int, float))] if isinstance(hourly, list) else []}
        result = fn(state)
        write_private(path, json.dumps(state))
        return result


_NO_PREVIEW = {"is_disabled": True}  # a link in a name must not unfold into a preview


def _iso(name: str) -> str:
    return "⁨" + name + "⁩"  # a name in another script must not reorder the line around it


def _request_rows(ui_lang: str, uid: int, request_id: str) -> list:
    return [[{"text": t("member_btn_add", lang=ui_lang), "callback_data": f"mb:a:{uid}:{request_id}"},
             {"text": t("member_btn_ignore", lang=ui_lang), "callback_data": f"mb:i:{uid}:{request_id}"}],
            [{"text": t("menu_btn_not_now", lang=ui_lang), "callback_data": f"mb:d:{uid}:{request_id}"}]]


def request(m: dict, cfg: Config, bot, now: Optional[float] = None) -> str:
    """A message in the team group from someone who is not on the list. Returns requested | ignored."""
    user = m.get("from") or {}
    uid = user.get("id")
    if (not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0 or uid > 9999999999999999 or uid == cfg.owner_chat_id
            or not any(k in m for k in _CONTENT)):
        return "ignored"
    name = C.clean_name(f"{user.get('first_name') or ''} {user.get('last_name') or ''}") or str(uid)
    username = str(user.get("username") or "")
    username = "@" + username if _USERNAME.fullmatch(username) else "-"
    now = time.time() if now is None else now
    request_id = secrets.token_hex(8)

    def reserve(s):
        s["hourly"] = [x for x in s["hourly"] if now - x < 3600]
        s["pending"] = {k: v for k, v in s["pending"].items()
                        if isinstance(v, dict) and now - float(v.get("at") or 0) < PENDING_TTL}
        if (uid in s["ignored"] or str(uid) in s["pending"] or len(s["hourly"]) >= MAX_PER_HOUR):
            return False
        if len(s["pending"]) >= MAX_PENDING:  # the oldest unanswered one makes room: a new person is never lost
            del s["pending"][min(s["pending"], key=lambda k: float(s["pending"][k].get("at") or 0))]
        s["hourly"].append(now)
        s["pending"][str(uid)] = {"name": name, "at": now, "request_id": request_id}
        return True

    if not _locked(reserve):
        return "ignored"
    ui_lang = getattr(cfg, "ui_language", "en")
    try:
        bot.call("sendMessage", {
            "chat_id": home_chat(cfg), "text": t("member_request", lang=ui_lang, name=_iso(name),
                                                 username=username, uid=uid),
            "reply_markup": {"inline_keyboard": _request_rows(ui_lang, uid, request_id)},
            "link_preview_options": _NO_PREVIEW})
    except TelegramError:
        def refund(s):  # a failed send uses up neither the person's place nor an hourly slot
            if s["pending"].get(str(uid), {}).get("request_id") == request_id:
                s["pending"].pop(str(uid), None)
            if now in s["hourly"]:
                s["hourly"].remove(now)
        _locked(refund)
        return "ignored"
    return "requested"


def _edit(bot, cfg: Config, q: dict, text: str, rows=None) -> None:
    try:
        bot.call("editMessageText", {"chat_id": home_chat(cfg), "message_id": (q.get("message") or {}).get("message_id"),
                                     "text": text, "reply_markup": {"inline_keyboard": (
                                         rows if rows and any((b.get("callback_data") == menus.CLOSE or str(b.get("callback_data", "")).startswith("mb:d:")) for r in rows for b in r)
                                         else rows + [menus.close_row(getattr(cfg, "ui_language", "en"))] if rows else [])},
                                     "link_preview_options": _NO_PREVIEW})
    except TelegramError:
        pass


def listing(cfg: Config) -> tuple:
    """The text and the remove buttons of the member list."""
    ui_lang = getattr(cfg, "ui_language", "en")
    members = sorted(cfg.team.members.items())[:MAX_LISTED]
    if not members:
        return t("members_none", lang=ui_lang), []
    lines = [t("members_title", lang=ui_lang)] + [f"{_iso(n)} ({u})" for u, n in members]
    rows = [[{"text": t("member_btn_remove", lang=ui_lang, name=n), "callback_data": f"mb:r:{u}"}]
            for u, n in members]
    rows.append(menus.close_row(ui_lang))
    return "\n".join(lines), rows


def show(m: dict, cfg: Config, bot) -> str:
    """/members from the owner (the caller has checked that): the list, each member with a remove button."""
    _refresh(cfg)
    text, rows = listing(cfg)
    params = {"chat_id": home_chat(cfg), "text": text, "disable_notification": True,
              "link_preview_options": _NO_PREVIEW}
    if rows:
        params["reply_markup"] = {"inline_keyboard": rows}
    if m.get("message_thread_id"):
        params["message_thread_id"] = m["message_thread_id"]
    try:
        bot.call("sendMessage", params)
    except TelegramError:
        pass
    return "members"


def _refresh(cfg: Config) -> None:
    """Pick up changes made with the command line; an unreadable config keeps what the listener has."""
    from . import team  # imported late: team imports inbox
    try:
        team.refresh(cfg)
    except C.ConfigError:
        pass


def _answer(bot, cfg: Config, q: dict, key: str) -> None:
    try:
        bot.call("answerCallbackQuery", {"callback_query_id": q.get("id") or "",
                                         "text": t(key, lang=getattr(cfg, "ui_language", "en"))})
    except TelegramError:
        pass


def button(q: dict, cfg: Config, bot) -> str:
    """A press on a request or on the list. The caller has checked that it is the owner's, in the home
    chat; the data must match exactly. Returns added | ignored-person | removed | ignored. Whatever the
    config or the disk does, nothing is raised: a press that keeps failing would be handled on every poll
    and block all later updates, so the owner is told instead."""
    try:
        return _button(q, cfg, bot)
    except Exception:
        _edit(bot, cfg, q, t("member_failed", lang=getattr(cfg, "ui_language", "en")))
        _answer(bot, cfg, q, "toast_not_available")
        return "ignored"


def _button(q: dict, cfg: Config, bot) -> str:
    from . import team
    ui_lang = getattr(cfg, "ui_language", "en")
    match = _BUTTON.fullmatch(q.get("data") or "")
    if not match:
        _answer(bot, cfg, q, "toast_not_available")
        return "ignored"
    action, uid, request_id = match.group(1), int(match.group(2)), match.group(3)
    if action == "r":
        if request_id is not None:
            _answer(bot, cfg, q, "toast_not_available")
            return "ignored"
        _refresh(cfg)
        try:
            if uid in cfg.team.members:
                team.remove_member(uid, cfg)  # memory is refreshed before their queued messages are purged
            else:  # already gone (the command line, or an earlier press): only the queued messages may be left
                team.discard_queued(uid)
        except Exception:
            _refresh(cfg)
            if uid in cfg.team.members:
                raise  # the removal itself failed: nothing changed
            _edit(bot, cfg, q, t("member_purge_failed", lang=ui_lang, uid=uid),
                  [[{"text": t("member_btn_remove", lang=ui_lang, name=str(uid)), "callback_data": f"mb:r:{uid}"}],
                   menus.close_row(ui_lang)])
            _answer(bot, cfg, q, "toast_not_available")
            return "removed"
        _edit(bot, cfg, q, *listing(cfg))
        _answer(bot, cfg, q, "member_toast_removed")
        return "removed"

    def apply(s):
        entry = s["pending"].get(str(uid))
        if not request_id or not isinstance(entry, dict) or entry.get("request_id") != request_id:
            return None
        if action == "a":
            name = C.clean_name(entry.get("name") or "") or str(uid)
            team.add_member(uid, name)
            team.refresh(cfg)
        elif action == "i":
            s["ignored"] = (s["ignored"] + [uid])[-MAX_IGNORED:]
        del s["pending"][str(uid)]
        return entry

    try:
        entry = _locked(apply)
    except (C.ConfigError, OSError):
        _edit(bot, cfg, q, t("member_add_failed", lang=ui_lang, name=str(uid), uid=uid),
              _request_rows(ui_lang, uid, request_id))
        _answer(bot, cfg, q, "toast_not_available")
        return "ignored"
    if entry is None:
        _answer(bot, cfg, q, "member_request_closed")
        return "ignored"
    if action == "d":
        _edit(bot, cfg, q, t("menu_dismissed", lang=ui_lang))
        _answer(bot, cfg, q, "toast_not_available")
        return "dismissed"
    name = C.clean_name(entry.get("name") or "") or str(uid)
    if action == "i":
        _edit(bot, cfg, q, t("member_ignored", lang=ui_lang, name=_iso(name), uid=uid))
        _answer(bot, cfg, q, "member_toast_ignored")
        return "ignored-person"
    _edit(bot, cfg, q, t("member_added", lang=ui_lang, name=_iso(name), uid=uid))
    _answer(bot, cfg, q, "member_toast_added")
    return "added"
