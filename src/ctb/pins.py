"""One pinned "replies index" message per session topic, so the owner can jump between replies with
Telegram's pinned bar (pins are per topic in a forum supergroup).

Pinning every reply would leave a "pinned a message" notice in the topic for each one. Instead the topic
gets ONE message, created and pinned once (one notice, ever). Each later reply edits that message in
place (an edit makes no notice): a short list of the last `pin_limit` replies, newest last, each line a
time plus the start of the reply's summary, linked to the reply message.

Every Telegram call here is optional traffic that goes through `flood.optional_call` and is charged to
its budget (an edit is one unit per reply, creating and pinning one each). A skipped or failed call never
fails or delays a reply; the next reply re-renders the whole list, so a skipped edit heals itself. Without
the "Pin messages" right the pin is retried only every PIN_BACKOFF seconds. The index message id and the
list per topic live in a small private state file guarded by a file lock like flood.json.
"""
from __future__ import annotations

import contextlib
import fcntl
import html
import json
import re
import time
from typing import Dict, Optional

from . import flood, fmt
from . import lang as L
from .config import Config, home_chat, private_dir, state_dir, write_private
from .projects import owner_chats
from .telegram import TelegramError

STATE_FILE = "pins.json"
_CALL_TIMEOUT = 5   # the index is a nicety: never hold the sender for the default 15 seconds
LINE_CHARS = 60     # how much of a reply's summary one index line shows
PIN_BACKOFF = 6 * 3600.0  # after Telegram refuses a pin (no admin right), try again only this much later
ISOLATE_OPEN, ISOLATE_CLOSE = "\u2068", "\u2069"
SUPERGROUP_PREFIX = "-100"


def _id(value) -> bool:
    return type(value) is int and value > 0


@contextlib.contextmanager
def _locked():
    base = private_dir(state_dir())
    with open(base / (STATE_FILE + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield base / STATE_FILE


def _blank() -> dict:
    # "button": the index message carries the control-panel button (panel_pin then needs no pin of its own)
    return {"id": 0, "pinned": False, "pin_retry_at": 0.0, "items": [], "button": False}


def _item(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    at, text, mid = raw.get("t"), raw.get("s"), raw.get("m")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or at != at or not isinstance(text, str) or not _id(mid):
        return None
    return {"t": float(at), "s": text, "m": mid}


def _load(path) -> Dict[str, dict]:
    """The state per "chat:thread", with anything malformed dropped (a hand-edited or half-written file
    must never break a reply)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, dict] = {}
    for key, topic in data.items():
        if not isinstance(topic, dict) or not isinstance(topic.get("items"), list):
            continue
        items = [i for i in map(_item, topic["items"]) if i]
        index = topic.get("id")
        retry = topic.get("pin_retry_at")
        out[str(key)] = dict(_blank(), id=index if _id(index) else 0, pinned=topic.get("pinned") is True, items=items,
                             button=topic.get("button") is True,
                             pin_retry_at=float(retry) if isinstance(retry, (int, float)) and retry == retry
                             and not isinstance(retry, bool) else 0.0)
    return out


def carries_button(chat: int, thread) -> bool:
    """The topic's pinned replies index exists, is pinned and carries the control-panel button."""
    try:
        with _locked() as path:
            topic = _load(path).get(f"{chat}:{thread}") or {}
        return bool(topic.get("id") and topic.get("pinned") and topic.get("button"))
    except OSError:
        return False


def enabled(cfg: Config, thread, chat: Optional[int] = None) -> bool:
    """The index exists only in a forum group: a supergroup (the home chat unless `chat` names a project group)
    and a session topic."""
    chat = home_chat(cfg) if chat is None else chat
    return bool(cfg.pin_replies and cfg.pin_limit > 0 and str(chat).startswith(SUPERGROUP_PREFIX) and _id(thread))


def link(chat: int, thread: int, message_id: int) -> str:
    """The t.me link of a message in a topic of a private supergroup (-100123 -> /c/123/<topic>/<id>)."""
    return f"https://t.me/c/{str(chat)[len(SUPERGROUP_PREFIX):]}/{thread}/{message_id}"


def strip_markup(md: str) -> str:
    """Markdown link targets and emphasis/code marks removed, for plain-text copies of a reply."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md or "")
    return re.sub(r"[*_`#>~|]", "", text)


def index_text(md: str) -> str:
    """One plain label line for a reply paragraph: markup removed, English and Hebrew never on one line
    (the first line after `fmt.unmix`), at most LINE_CHARS characters, no bidi isolate left open."""
    text = strip_markup(md)
    lines = [" ".join(line.split()) for line in fmt.unmix(text).split("\n")]
    label = next((line for line in lines if line), "")
    if len(label) > LINE_CHARS:
        label = label[:LINE_CHARS - 1].rstrip() + "…"
    return label + ISOLATE_CLOSE * max(0, label.count(ISOLATE_OPEN) - label.count(ISOLATE_CLOSE))


def render(cfg: Config, thread: int, items: list, chat: Optional[int] = None) -> str:
    chat = home_chat(cfg) if chat is None else chat
    rtl = L.is_rtl(cfg.target_language)
    lines = []
    for item in items:
        mark = L.RLM if rtl and fmt.HEB.search(item["s"]) else ""  # an English-only line keeps its punctuation
        clock = time.strftime("%H:%M", time.localtime(item["t"]))
        label = html.escape(item["s"] or "…", quote=False)
        lines.append(f'{mark}{clock} · <a href="{link(chat, thread, item["m"])}">{label}</a>')
    return "\n".join(lines)


def _gone(e: TelegramError) -> bool:
    return not getattr(e, "skipped", False) and e.code == 400 and "not found" in e.description.lower()


def _store(key: str, **fields) -> None:
    with _locked() as path:
        data = _load(path)
        data.setdefault(key, _blank()).update(fields)
        write_private(path, json.dumps(data))


def _pin(cfg: Config, bot, key: str, message_id: int, retry_at: float, now: float, chat: Optional[int] = None) -> None:
    """Pin the index. Without the admin right Telegram refuses (400/403): that is logged once and not
    retried for PIN_BACKOFF; a gate skip or a network error is simply retried with the next reply."""
    if now < retry_at:
        return
    try:
        flood.optional_call(bot, "pinChatMessage", {"chat_id": home_chat(cfg) if chat is None else chat,
                            "message_id": message_id, "disable_notification": True}, timeout=_CALL_TIMEOUT)
    except TelegramError as e:
        if not getattr(e, "skipped", False) and e.code in (400, 403):
            _store(key, pin_retry_at=now + PIN_BACKOFF)
            from .mirror import log
            log(f"cannot pin the replies index (code {e.code}); grant the bot the admin right 'Pin messages'")
        return
    except Exception:
        return
    _store(key, pinned=True, pin_retry_at=0.0)


def update_index(cfg: Config, bot, thread, message_id, text: str, now: Optional[float] = None,
                 chat: Optional[int] = None) -> bool:
    """Add a reply (`message_id`, summarised by `text`) to the topic's index, editing the index message
    in place, or creating and pinning it the first time (or when it was deleted). True when the index
    message shows the reply. Never raises. `chat`: the project group of the topic (None = the home chat)."""
    if not (enabled(cfg, thread, chat) and _id(message_id)):
        return False
    try:
        chat = home_chat(cfg) if chat is None else chat
        key = f"{chat}:{thread}"
        item = {"t": time.time() if now is None else now, "s": index_text(text), "m": message_id}
        with _locked() as path:
            data = _load(path)
            topic = data.setdefault(key, _blank())
            topic["items"] = (topic["items"] + [item])[-cfg.pin_limit:]
            write_private(path, json.dumps(data))
            index, pinned, items = topic["id"], topic["pinned"], list(topic["items"])
            retry_at = topic["pin_retry_at"]
        params = {"chat_id": chat, "text": render(cfg, thread, items, chat), "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}}
        # The control-panel button rides on this message: Telegram's pinned bar shows the newest pinned message,
        # so a separate, older panel pin was never the one in view (the owner, on the phone).
        from . import panel_pin
        button = panel_pin.button(cfg, chat, thread)
        if button:
            params["reply_markup"] = button
        if index:
            try:
                flood.optional_call(bot, "editMessageText", dict(params, message_id=index),
                                    timeout=_CALL_TIMEOUT)
            except TelegramError as e:
                if "not modified" in e.description.lower():
                    return True
                if not _gone(e):
                    return False  # skipped by the gate or failed: the next reply re-renders the list
                _store(key, id=0, pinned=False, pin_retry_at=0.0, button=False)  # the index was deleted: make a new one
                retry_at = 0.0
            else:
                _store(key, button=bool(button))
                if not pinned:
                    _pin(cfg, bot, key, index, retry_at, item["t"], chat)
                return True
        sent = flood.optional_call(bot, "sendMessage", dict(params, message_thread_id=thread,
                                   disable_notification=True), timeout=_CALL_TIMEOUT)
        index = sent.get("message_id") if isinstance(sent, dict) else None
        if not _id(index):
            return False
        _store(key, id=index, pinned=False, button=bool(button))
        _pin(cfg, bot, key, index, retry_at, item["t"], chat)
        return True
    except Exception:
        return False


def _bot_id(cfg: Config) -> Optional[int]:
    """Our bot's user id: the number before the colon in its token (nothing else of it is read)."""
    try:
        return int(cfg.token().split(":", 1)[0])
    except Exception:
        return None


def drop_pin_notice(m: dict, cfg: Config, bot) -> bool:
    """Best effort: delete the "pinned a message" service message that our own bot's pin caused in the
    home group or a project group (it comes once per topic, when the index is first pinned, if Telegram delivers it to
    the bot at all). A pin by anyone else (the owner) is left alone. True when the message was ours."""
    sender = m.get("from") or {}
    mine = _bot_id(cfg)
    if not (m.get("pinned_message") and sender.get("is_bot") and mine is not None and sender.get("id") == mine
            and (m.get("chat") or {}).get("id") in owner_chats(cfg) and m["chat"]["id"] < 0 and _id(m.get("message_id"))):
        return False
    try:
        flood.optional_call(bot, "deleteMessage", {"chat_id": m["chat"]["id"], "message_id": m["message_id"]},
                            budget=False, timeout=_CALL_TIMEOUT)
    except Exception:
        pass  # skipped or already gone: a leftover notice is harmless
    return True
