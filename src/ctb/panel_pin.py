"""A control-panel button pinned once in every session topic.

Telegram shows a forum group's pinned messages only inside the topic they were pinned in, so the panel
button pinned in General was invisible from the session topics. Each round the
listener posts one small message with a URL button to the bot's Main App (`https://t.me/<bot>?startapp=t<hash>`;
web-app inline buttons only work in private chats) into a few topics that do not have it yet, and pins it.
The start parameter names the topic, so the panel opens on that topic's own view (panel.topic_param). A
message placed by an older version (VERSION) is edited once to the current text and link; after that it is
never edited again, so it costs nothing once placed. Only topics of sessions that are working, idle or
ended less than ACTIVE_AGE ago get it: a new message bumps its topic to the top of the group, so posting
into every old topic made long-dead sessions jump up. An old topic gets it when it is used again. Sends and pins go through
`flood.optional_call`; a skipped or failed call is retried in a later round, a topic Telegram refuses
(closed, deleted, no pin right) is retried after RETRY seconds, and a transient failure (a 5xx or no answer)
after TRANSIENT_RETRY, with nothing marked done: an old button is marked current only once its edit
succeeded or Telegram refused it for good. The Cockpit pin also carries the topic board. A v2 pin folded
into the replies index is restored during migration, and pinning the replies index re-pins the Cockpit
carrier so it remains the topic's visible pin. The message ids per topic live in a small private state file
guarded by a file lock.
"""
from __future__ import annotations

import fcntl
import json
import logging
import threading
import re
import time
from pathlib import Path
from typing import Optional

from . import flood, waiting
from .panel import topic_param
from .config import Config, state_dir, write_private
from .i18n import t
from .telegram import TelegramError

PER_ROUND = 3  # new topics handled per round, so a backfill never spends the whole flood budget
RETRY = 24 * 3600
TRANSIENT_RETRY = 10 * 60  # a Telegram server error or no answer: soon, and nothing is marked done
ACTIVE_AGE = 6 * 3600
VERSION = 3  # 1 = shared link; 2 = per-topic link/folding; 3 = board rides on the Cockpit pin
_menu_lock = threading.Lock()
_menu_state = {}  # token and owner -> last attempted time and last successful label
_username = {}  # bot token -> username, read once per process


def _path():
    return state_dir() / "panel-pins.json"


def _name_path():
    return state_dir() / "panel-bot-name.json"


def _token_mark(cfg: Config) -> str:
    import hashlib
    return hashlib.sha256((cfg.bot_token or "").encode()).hexdigest()[:16]


def _bot_name(cfg: Config, bot) -> str:
    if cfg.bot_token not in _username:
        try:
            name = (bot.call("getMe") or {}).get("username") or ""
        except TelegramError:
            return ""
        if not name:
            return ""
        _username[cfg.bot_token] = name
        try:  # kept for the processes that must not ask Telegram for it (a reply's hook: button())
            write_private(_name_path(), json.dumps({"bot": _token_mark(cfg), "name": name}))
        except OSError:
            pass
    return _username[cfg.bot_token]


def known_name(cfg: Config) -> str:
    """The bot's username as the listener last learned it, without a Telegram call; "" when unknown."""
    if cfg.bot_token in _username:
        return _username[cfg.bot_token]
    try:
        saved = json.loads(_name_path().read_text(encoding="utf-8"))
        name = saved.get("name") if isinstance(saved, dict) and saved.get("bot") == _token_mark(cfg) else ""
    except (OSError, ValueError):
        name = ""
    return name if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_]{3,64}", name) else ""


def app_url(cfg: Config, start_param: str) -> str:
    """A no-I/O deep link to this bot's Main App, or ``""`` until its username is known."""
    name = known_name(cfg)
    if not name or not isinstance(start_param, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", start_param):
        return ""
    return f"https://t.me/{name}?startapp={start_param}"


def _markup(cfg: Config, name: str, chat: int, thread: Optional[int], count=None) -> dict:
    """The URL button that opens the Main App on this topic (a topic without a session opens the overview)."""
    url = f"https://t.me/{name}?startapp=" + (topic_param(chat, thread) if type(thread) is int else "panel")
    count = waiting.waiting_count(cfg) if count is None else count
    key = "panel_pin_button_count" if count else "panel_pin_button"
    return {"inline_keyboard": [[{"text": t(key, lang=cfg.ui_language, count=count), "url": url}]]}


def base_content(cfg: Config, chat: int, thread: int, count=None):
    name = known_name(cfg)
    if not name:
        return None
    return (t("panel_pin_text", lang=cfg.ui_language),
            _markup(cfg, name, chat, thread, count)["inline_keyboard"])


def _content(cfg: Config, name: str, chat: int, thread: int, count=None):
    from . import board
    topic = board.view(chat, thread)
    base = t("panel_pin_text", lang=cfg.ui_language)
    rows = _markup(cfg, name, chat, thread, count)["inline_keyboard"]
    return board.pin_content(cfg, chat, thread, base, rows, topic), topic["items"]


def pin_message(chat: int, thread) -> int:
    rec = _load().get(f"{chat}:{thread}") or {}
    mid = rec.get("id")
    if not (rec.get("pinned") is True and not rec.get("folded") and not rec.get("frozen")
            and type(mid) is int and mid > 0):
        return 0
    from . import board
    return 0 if board.gone_id(chat, thread) == mid else mid


def repin(bot, chat: int, thread) -> None:
    mid = pin_message(chat, thread)
    if mid:
        try:
            flood.optional_call(bot, "pinChatMessage", {"chat_id": chat, "message_id": mid,
                                                        "disable_notification": True})
        except Exception:
            pass


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _active(topics, now: float) -> set:
    """The "chat:thread" keys of the topics whose session is in use now (see ACTIVE_AGE)."""
    from .sessions_menu import collect
    return {f"{topics.chat_id if e.chat is None else e.chat}:{e.thread}" for e in collect(topics, now=now)
            if not e.closed and (e.state != "ended" or (e.age is not None and e.age < ACTIVE_AGE))}


PANEL_JOB = "com.claude-telegram-bridge.panel"


def enabled(cfg: Config) -> bool:
    """The config's panel_button when set; otherwise on only where the panel service is installed (a button to a
    Mini App that was never set up would lead nowhere)."""
    value = getattr(cfg, "panel_button", None)
    if isinstance(value, bool):
        return value
    return (Path.home() / "Library" / "LaunchAgents" / f"{PANEL_JOB}.plist").exists()


def _refresh_menu(cfg, bot, count, now):
    """Update only the paired private chat; failed attempts are throttled too."""
    owner = getattr(cfg, "owner_chat_id", None)
    url = getattr(cfg, "panel_base_url", "")
    if type(owner) is not int or owner <= 0 or not url.startswith("https://"):
        return
    label = t("panel_menu_count" if count else "panel_menu", lang=cfg.ui_language, count=count)
    key = (_token_mark(cfg), owner)
    with _menu_lock:
        last, sent = _menu_state.get(key, (float("-inf"), None))
        if sent == (label, url) or now - last < 60:
            return
        _menu_state[key] = (now, sent)
        try:
            flood.optional_call(bot, "setChatMenuButton", {"chat_id": owner, "menu_button": {
                "type": "web_app", "text": label, "web_app": {"url": url}}}, timeout=5)
        except Exception:
            logging.getLogger(__name__).warning("Could not update Cockpit menu button")
        else:
            _menu_state[key] = (now, (label, url))


def run(cfg: Config, bot, topics, now: Optional[float] = None) -> int:
    """Place the panel button in up to PER_ROUND topics that lack it. Returns how many were placed.
    Never raises; does nothing while the panel is not set up (enabled)."""
    now = time.time() if now is None else now
    if not enabled(cfg):
        return 0
    try:
        count = waiting.waiting_count(cfg)
        _refresh_menu(cfg, bot, count, time.monotonic())
        name = _bot_name(cfg, bot)
        if not name:
            return 0
        lock_path = state_dir() / "panel-pins.json.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state, placed = _load(), 0
            from . import board
            old_boards = board.old_pins()
            active = _active(topics, now)
            # Failed topics retain their retry timestamp, so untouched topics get a turn even
            # when rounds are farther apart than the retry delay.
            def retry_order(entry):
                key = f"{entry.get('chat')}:{entry.get('thread')}"
                retry = (state.get(key) or {}).get("retry_at", 0)
                return retry if isinstance(retry, (int, float)) else 0

            for entry in sorted(topics.all_places().values(), key=retry_order):
                chat, thread = entry.get("chat"), entry.get("thread")
                if type(chat) is not int or type(thread) is not int:
                    continue
                key = f"{chat}:{thread}"
                rec = state.get(key) or {}
                if key not in active and not (rec.get("pinned") and rec.get("id")):
                    continue
                # v2 could be folded into the replies index. v3 restores that message as the
                # sole Cockpit/board carrier; a deleted carrier is posted anew.
                restoring = rec.get("folded") is True
                if restoring:
                    rec = {k: v for k, v in rec.items() if k not in ("folded", "pinned", "retry_at")}
                if rec.get("id") and board.gone_id(chat, thread) == rec["id"]:
                    rec = {}
                status_digest = board.status_digest(cfg, chat, thread, now)
                current = (rec.get("pinned") and rec.get("v") == VERSION
                           and (rec.get("frozen") or (rec.get("count") == count
                                                     and rec.get("status_digest", "") == status_digest)))
                if current and key in old_boards:
                    board.migrate_old(cfg, bot, chat, thread, rec["id"], now)
                if current or now < rec.get("retry_at", 0):
                    continue
                if placed >= PER_ROUND:
                    break
                content, shown = _content(cfg, name, chat, thread, count)
                try:
                    if (rec.get("pinned") or restoring) and rec.get("id"):
                        try:
                            flood.optional_call(bot, "editMessageText",
                                                dict(content, chat_id=chat, message_id=rec["id"]))
                        except TelegramError as e:
                            if "not modified" not in e.description.lower():
                                raise
                        board.settle(chat, thread, shown)
                        rec = dict(rec, v=VERSION, count=count, status_digest=status_digest)
                        rec.pop("retry_at", None)
                        if restoring:
                            flood.optional_call(bot, "pinChatMessage", {
                                "chat_id": chat, "message_id": rec["id"], "disable_notification": True})
                            rec["pinned"] = True
                        placed += 1
                        state[key] = rec
                        write_private(_path(), json.dumps(state))
                        continue
                    if not rec.get("id"):
                        sent = flood.optional_call(bot, "sendMessage", dict(
                            content, chat_id=chat, message_thread_id=thread, disable_notification=True))
                        mid = sent.get("message_id") if isinstance(sent, dict) else None
                        if type(mid) is not int:
                            continue
                        rec = {"id": mid, "v": VERSION, "count": count, "status_digest": status_digest}
                        state[key] = rec
                        write_private(_path(), json.dumps(state))
                    flood.optional_call(bot, "pinChatMessage", {"chat_id": chat, "message_id": rec["id"],
                                                                "disable_notification": True})
                    rec["pinned"] = True
                    board.settle(chat, thread, shown)
                    placed += 1
                except TelegramError as e:
                    if getattr(e, "skipped", False) or e.code == 429:
                        break  # the flood gate is closed: try again next round
                    if e.code is None or e.code >= 500:  # transient (review of #136): an old
                        rec = dict(rec, retry_at=now + TRANSIENT_RETRY)  # button was marked current for good
                        placed += 1
                        state[key] = rec
                        write_private(_path(), json.dumps(state))
                        continue
                    rec = dict(rec, retry_at=now + RETRY)
                    if e.code == 400 and "not found" in e.description.lower():
                        rec = {"retry_at": rec["retry_at"]}  # the message or topic is gone: post a new one later
                    elif rec.get("pinned"):
                        rec["v"], rec["frozen"] = VERSION, True  # an old button that cannot be edited stays as it is (no count)
                    placed += 1
                state[key] = rec
                write_private(_path(), json.dumps(state))
            return placed
    except Exception:
        return 0


PREVIEW_ITEMS = 3  # decisions and reminders listed by name in the preview; the rest are counted


def _clip(text: str, n: int = 70) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def preview(cfg: Config, topics, chat: int, thread: Optional[int], now: Optional[float] = None) -> str:
    """What waits behind the panel button, as a short text:
    in a topic its session's state, the open decisions and reminders by name and the checklist's progress; in
    General the same across every topic, each with its topic's name. Never raises: the bare title on failure."""
    lang = cfg.ui_language
    here = (chat, thread) if type(thread) is int else None
    lines = [t("panel_preview_title" if here else "panel_preview_title_all", lang=lang)]
    try:
        from .panel import waiting_for_owner
        from .sessions_menu import collect
        from . import board
        now = time.time() if now is None else now
        if here and topics is not None:
            for e in collect(topics, now=now):
                if (topics.chat_id if e.chat is None else e.chat, e.thread) == here and not e.closed:
                    lines.append(t(f"panel_preview_{e.state}", lang=lang))
                    break
        asked, reminders = waiting_for_owner(topics, now, here=here) if topics is not None else ([], [])
        if here:
            asked = [d for d in asked if d["here"]]
            reminders = [r for r in reminders if r["here"]]
        named = lambda x, key: _clip(x[key]) + ("" if here or not x.get("topic") else f" · {_clip(x['topic'], 30)}")
        if asked:
            lines.append(t("panel_preview_decisions", lang=lang, n=len(asked)))
            lines += ["• " + named(d, "title") for d in asked[:PREVIEW_ITEMS]]
        if reminders:
            lines.append(t("panel_preview_reminders", lang=lang, n=len(reminders)))
            lines += ["• " + named(r, "text") for r in reminders[:PREVIEW_ITEMS]]
        checks = [i for i in board.items_of(chat, thread) if i["k"] == "check"] if here else []
        if checks:
            lines.append(t("panel_preview_checks", lang=lang, done=sum(1 for i in checks if i["x"]), total=len(checks)))
        if not asked and not reminders:
            lines.append(t("panel_preview_nothing", lang=lang))
    except Exception:
        pass
    return "\n".join(lines)[:3500]


def send_link(cfg: Config, bot, chat: int, thread: Optional[int], topics=None) -> bool:
    """The panel on demand (the keyboard's panel button or /panel; a topic must always be able to show its panel): one message with this topic's panel button. In a topic that has no current pinned
    button yet (an old topic, or one that never had a session) it is also pinned and recorded, so the topic
    keeps it from then on. With `topics`, the message previews what waits there (preview()).
    Never raises; False when nothing could be sent."""
    try:
        name = _bot_name(cfg, bot)
        if not name or type(chat) is not int:
            return False
        text = preview(cfg, topics, chat, thread) if topics is not None else t("panel_pin_text", lang=cfg.ui_language)
        params = {"chat_id": chat, "disable_notification": True,
                  "text": text, "reply_markup": _markup(cfg, name, chat, thread)}
        if type(thread) is int:
            params["message_thread_id"] = thread
        sent = bot.call("sendMessage", params)
        mid = sent.get("message_id") if isinstance(sent, dict) else None
        if type(mid) is not int or type(thread) is not int:
            return True
        key = f"{chat}:{thread}"
        lock_path = state_dir() / "panel-pins.json.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = _load()
            rec = state.get(key) or {}
            if rec.get("pinned") and rec.get("v") == VERSION:
                return True  # the topic already has its pinned button: this message is only a shortcut
            try:
                flood.optional_call(bot, "pinChatMessage", {"chat_id": chat, "message_id": mid,
                                                            "disable_notification": True})
            except TelegramError:
                return True  # no pin right or the gate is closed: the round in run() may still place one
            state[key] = {"id": mid, "v": VERSION, "pinned": True}
            write_private(_path(), json.dumps(state))
        try:
            content, shown = _content(cfg, name, chat, thread)
            flood.optional_call(bot, "editMessageText", dict(content, chat_id=chat, message_id=mid))
            from . import board
            board.settle(chat, thread, shown)
        except TelegramError:
            pass
        return True
    except Exception:
        return False
