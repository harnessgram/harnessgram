"""Moving a session's topic to another group: the /more menu item, the one-time offer when a session's title
later names a project, and the move itself.

Every step is a tap. A move opens a new topic in the target group (a project group, or the home group),
removes the session's placeholder, status and progress cards and dialog buttons from the old topic, switches the session to the new topic, and turns the old
topic into a closed note that links to the new one. Input typed in the old topic is answered with that link
and never delivered (TopicMap.moved_session)."""
from __future__ import annotations

import contextlib
import fcntl
import json
import re
import time
import uuid
from typing import Callable, List, Optional

from . import fmt
from .config import Config, home_chat, private_dir, state_dir, write_private
from .i18n import get_lang, t
from .projects import owner_chats, project_by_chat, thread_key, title_project
from .telegram import TelegramError
from .topics import (PLACEHOLDER, STATE_ICONS, TopicMap, _drop, _sweep, session_name, session_state,
                     session_state_path)

PENDING_FILE = "move-pending.json"      # the offers on screen: topic key + kind -> {session, token, targets}
KINDS = ("menu", "offer")               # the move menu and the one-time late-title offer live side by side
OFFERED_FILE = "move-offered.json"      # sessions that were offered a move or moved, so a late offer comes once
PENDING_KEEP = 50
OFFERED_KEEP = 500
CALLBACK = re.compile(r"mv:([0-9a-f]{16}):([0-9]|x)")


def topic_link(chat: int, thread) -> str:
    return f"https://t.me/c/{str(chat).removeprefix('-100')}/{thread}"


def _lang(cfg: Config) -> str:
    return getattr(cfg, "ui_language", None) or get_lang()


def _name(name: str, lang: str) -> str:
    """A project's name inside a Hebrew line: wrapped in a bidi isolate when it may be (fmt.isolate)."""
    return (fmt.isolate(name) or name) if lang == "he" else name


def _where(cfg: Config, chat: int, lang: str) -> str:
    project = project_by_chat(cfg, chat)
    if chat == home_chat(cfg) or project is None:
        return t("move_where_home", lang=lang)
    return t("move_where_project", lang=lang, name=_name(project.name, lang))


@contextlib.contextmanager
def _file_lock(name: str):
    root = private_dir(state_dir())
    with open(root / (name + ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield root / name


def _read(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember(key: str, kind: str, record: dict) -> str:
    """Keep an offer on screen under its topic and kind and return its one-time token. A menu and an offer
    in the same topic do not replace each other."""
    token = uuid.uuid4().hex[:16]
    slot = f"{key}#{kind}"
    with _file_lock(PENDING_FILE) as path:
        pending = _read(path)
        pending.pop(slot, None)
        pending[slot] = dict(record, token=token, at=time.time())
        write_private(path, json.dumps(dict(list(pending.items())[-PENDING_KEEP:])))
    return token


def _take(key: str, token: str, session: str, valid: Callable[[dict], bool]) -> Optional[dict]:
    """The offer on screen for this topic when the token and session match and `valid(offer)` holds; it is
    used up. The same exact-token rule as the project picker's n: buttons: a tap that does not fit leaves it."""
    with _file_lock(PENDING_FILE) as path:
        pending = _read(path)
        for kind in KINDS:
            job = pending.get(f"{key}#{kind}")
            if not isinstance(job, dict) or job.get("token") != token:
                continue
            if job.get("session") != session or not valid(job):
                return None
            pending.pop(f"{key}#{kind}")
            write_private(path, json.dumps(pending))
            return job
        return None


def _offered(session: str, claim: bool = False) -> bool:
    """Whether this session was already offered a move (or moved). With `claim`, record it now and
    return False the first time, so two processes never both make the offer."""
    with _file_lock(OFFERED_FILE) as path:
        seen = _read(path)
        if session in seen:
            return True
        if claim:
            seen[session] = time.time()
            write_private(path, json.dumps(dict(list(seen.items())[-OFFERED_KEEP:])))
        return False


def targets(cfg: Config, current_chat: int) -> List[int]:
    """The groups a session in `current_chat` can move to: every project group but its own, then the home group."""
    home = home_chat(cfg)
    found = [p.chat_id for p in cfg.projects if p.chat_id != current_chat]
    if current_chat != home:
        found.append(home)
    return found


def _label(cfg: Config, chat: int, lang: str) -> str:
    project = project_by_chat(cfg, chat)
    if chat == home_chat(cfg) or project is None:
        return t("move_btn_home", lang=lang)
    return t("move_btn_project", lang=lang, name=_name(project.name, lang))


def show_menu(cfg: Config, bot, m: dict, session: str) -> bool:
    """Turn the /more menu message `m` into the list of groups the session can move to."""
    lang = _lang(cfg)
    chat = (m.get("chat") or {}).get("id")
    thread = m.get("message_thread_id")
    options = targets(cfg, chat) if cfg.projects else []
    if not options or type(thread) is not int:
        return False
    token = _remember(thread_key(thread, None if chat == home_chat(cfg) else chat), "menu",
                      {"session": session, "targets": options})
    rows = [[{"text": _label(cfg, c, lang), "callback_data": f"mv:{token}:{i}"}] for i, c in enumerate(options)]
    rows.append([{"text": t("btn_back", lang=lang), "callback_data": "more:back"},
                 {"text": t("menu_btn_dismiss", lang=lang), "callback_data": "more:dismiss"}])
    try:
        bot.call("editMessageText", {"chat_id": chat, "message_id": m.get("message_id"),
                                     "text": t("move_menu_text", lang=lang),
                                     "reply_markup": {"inline_keyboard": rows}})
    except TelegramError:
        pass
    return True


def suggest(cfg: Config, bot, topics: TopicMap, session: str, title: str) -> bool:
    """A session's title names a project whose group it is not in: ask once, in its topic, whether to move
    it there. Never moves anything by itself; the owner taps."""
    project = title_project(cfg, title)
    found = topics.place_of(session) if project else None
    if project is None or found is None or found.chat == project.chat_id:
        return False
    if _offered(session, claim=True):
        return False
    lang = _lang(cfg)
    token = _remember(found.key, "offer", {"session": session, "targets": [project.chat_id]})
    name = _name(project.name, lang)
    try:
        bot.call("sendMessage", {
            "chat_id": found.chat, "message_thread_id": found.thread, "disable_notification": True,
            "text": t("move_suggest", lang=lang, name=name),
            "reply_markup": {"inline_keyboard": [[
                {"text": t("move_suggest_yes", lang=lang), "callback_data": f"mv:{token}:0"},
                {"text": t("move_suggest_no", lang=lang), "callback_data": f"mv:{token}:x"}]]}})
    except TelegramError:
        return False
    return True


def button(q: dict, cfg: Config, bot, topics: TopicMap, session: str) -> str:
    """A mv:<token>:<n|x> tap in the session's topic. The caller has checked that the sender is the paired
    owner in one of the owner's chats. The token must be the one the menu or the offer made for this very
    topic and session; it works once."""
    from .inbox import _answer_button
    lang = _lang(cfg)
    m = q.get("message") or {}
    chat_id = (m.get("chat") or {}).get("id")
    thread = m.get("message_thread_id")
    match = CALLBACK.fullmatch(q.get("data") or "")
    allowed = targets(cfg, chat_id)

    def fits(offer: dict) -> bool:
        if match.group(2) == "x":
            return True
        options = offer.get("targets") if isinstance(offer.get("targets"), list) else []
        index = int(match.group(2))
        return index < len(options) and type(options[index]) is int and options[index] in allowed

    job = None
    if match and session and type(thread) is int:
        job = _take(thread_key(thread, None if chat_id == home_chat(cfg) else chat_id), match.group(1), session, fits)
    if job is None:
        _answer_button(bot, q, t("toast_not_available", lang=lang))
        return "ignored"
    edit = {"chat_id": chat_id, "message_id": m.get("message_id")}
    if match.group(2) == "x":
        _answer_button(bot, q, t("toast_cancelled", lang=lang))
        try:
            bot.call("editMessageText", dict(edit, text=t("move_kept", lang=lang), reply_markup={"inline_keyboard": []}))
        except TelegramError:
            pass
        return "move-kept"
    target = job["targets"][int(match.group(2))]
    _answer_button(bot, q, t("toast_moving", lang=lang))
    result = move(cfg, bot, session, target, topics=topics, note=m.get("message_id"))
    if result != "moved":
        text = t("move_busy" if result == "busy" else "move_failed", lang=lang)
        try:
            bot.call("editMessageText", dict(edit, text=text, reply_markup={"inline_keyboard": []}))
        except TelegramError:
            pass
    return "move-" + result


def reply_moved(cfg: Config, bot, topics: TopicMap, m: dict, session: str) -> str:
    """The owner wrote in a topic its session has left: say where it went. Nothing is delivered."""
    lang = _lang(cfg)
    now = topics.place_of(session)
    text = (t("move_old_topic", lang=lang, where=_where(cfg, now.chat, lang), link=topic_link(now.chat, now.thread))
            if now else t("move_gone", lang=lang))
    try:
        bot.call("sendMessage", {"chat_id": (m.get("chat") or {}).get("id"),
                                 "message_thread_id": m.get("message_thread_id"),
                                 "text": text, "disable_notification": True})
    except TelegramError:
        pass
    return "moved"


def _sweep_cards(cfg: Config, bot, session: str, old, progress_id=None) -> None:
    """Delete the session's status card, progress card (`progress_id`, already forgotten by the move) and
    dialog keyboard from its old topic and forget what was kept for them, so the new topic draws its own."""
    from . import dialogs, mirror
    card = mirror.drop_topic_state(old)
    if card:
        _drop(bot, old.chat, card)
    if type(progress_id) is int:
        _drop(bot, old.chat, progress_id)
    rec = dialogs.rec_get(session)
    # only a keyboard that is still in the old topic: a dialog posted in the new one meanwhile stays
    if rec and (rec.get("chat") or home_chat(cfg)) == old.chat and rec.get("thread") == old.thread:
        dialogs._drop_keyboard(bot, cfg, rec)
        dialogs.rec_del(session)


def move(cfg: Config, bot, session: str, to_chat: int, topics: Optional[TopicMap] = None,
         note: Optional[int] = None) -> str:
    """Move a session's topic to the group `to_chat` (a project group, or the home group).

    Returns moved | same | busy | failed | nosession. `note` is a message of the old topic (the menu or the
    offer that was tapped) to turn into the link, instead of posting a new one. Nothing changes when
    the new topic cannot be opened."""
    if to_chat not in owner_chats(cfg):
        return "failed"
    topics = topics or TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg)
    lang = _lang(cfg)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_"
    with open(private_dir(state_dir() / "moving") / (safe + ".topic.lock"), "a") as once:
        try:
            fcntl.flock(once, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "busy"
        old = topics.place_of(session)
        if old is None:
            return "nosession"
        if old.chat == to_chat:
            return "same"
        entry = topics.all_places().get(session) or {}
        name = str(entry.get("name") or session_name(session) or "Claude")
        visible = (STATE_ICONS.get(session_state(session).get("state", ""), "") + " " + name).strip()[:128]
        try:
            thread = bot.call("createForumTopic", {"chat_id": to_chat, "name": visible})["message_thread_id"]
        except (TelegramError, KeyError, TypeError):
            return "failed"
        from . import progress
        path = session_state_path(session)
        private_dir(path.parent)
        pdir = private_dir(state_dir() / "progress")
        pname = progress.safe_name(session)
        # The progress lock first, then the session-state lock: a progress run holds its own lock while it
        # calls set_state. Under both, no hook can act on the session between its old place and its new one.
        with open(pdir / f"{pname}.lock", "a") as plock:
            fcntl.flock(plock, fcntl.LOCK_EX)
            pstate_path = pdir / f"{pname}.msg"
            pstate = _read(pstate_path)
            card_id = pstate.get("id")
            if card_id is not None:  # the card is in the old chat: forget it before the session leaves
                write_private(pstate_path, json.dumps({k: v for k, v in pstate.items()
                                                       if k not in ("id", "cards", "dirty")}, ensure_ascii=False))
            with open(str(path) + ".lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    old = topics.relocate(session, to_chat, thread, name, visible)
                except Exception:
                    old = None
                if old is None:
                    if card_id is not None:
                        write_private(pstate_path, json.dumps(pstate, ensure_ascii=False))
                    try:
                        bot.call("deleteForumTopic", {"chat_id": to_chat, "message_thread_id": thread})
                    except TelegramError:
                        pass
                    return "failed"
                cur = session_state(session)
                if cur:
                    # tracked ids of any other chat are not ours to delete (set_state forgets them)
                    stale = [x for x in [*cur.get("all", []), cur.get("placeholder"), cur.get("status_id")]
                             if type(x) is int and x > 0]
                    if cur.get("chat") not in (None, old.chat):
                        stale = []
                    _sweep(bot, old.chat, stale, None)  # a leftover that cannot be deleted is in the closed old topic
                    for key in ("all", "placeholder", "status_id", "status_text"):
                        cur.pop(key, None)
                    cur["chat"] = to_chat  # the ids tracked from here on are messages of the new chat
                    if cur.get("state") == "busy":
                        try:
                            mid = bot.call("sendMessage", {"chat_id": to_chat, "message_thread_id": thread,
                                                           "text": PLACEHOLDER, "disable_notification": True}).get("message_id")
                            if type(mid) is int and mid > 0:
                                cur["placeholder"], cur["all"] = mid, [mid]
                        except TelegramError:
                            pass
                    write_private(path, json.dumps(cur))
        _offered(session, claim=True)  # a moved session is never offered another move
        try:
            _sweep_cards(cfg, bot, session, old, card_id)
        except Exception:
            pass  # the move is done; a card left behind sits in the closed old topic
        text = t("move_old_topic", lang=lang, where=_where(cfg, to_chat, lang), link=topic_link(to_chat, thread))
        done = False
        if note:
            try:
                bot.call("editMessageText", {"chat_id": old.chat, "message_id": note, "text": text,
                                             "reply_markup": {"inline_keyboard": []}})
                done = True
            except TelegramError:
                pass
        if not done:
            try:
                bot.call("sendMessage", {"chat_id": old.chat, "message_thread_id": old.thread, "text": text,
                                         "disable_notification": True})
            except TelegramError:
                pass
        try:
            bot.call("closeForumTopic", {"chat_id": old.chat, "message_thread_id": old.thread})
        except TelegramError:
            pass
        try:
            bot.call("sendMessage", {"chat_id": to_chat, "message_thread_id": thread, "disable_notification": True,
                                     "text": t("move_arrived", lang=lang)})
        except TelegramError:
            pass
        try:
            from . import mirror
            mirror.refresh_card(cfg, bot, session, topics, create=True, force=True)
        except Exception:
            pass
    return "moved"
