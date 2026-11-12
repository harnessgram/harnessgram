"""A pin board per session topic: links worth keeping, a checklist and reminders for the owner, in ONE
message that is created and pinned once and then edited in place.

An agent adds to it from inside its session (`ctb board add|check|remind|done|remove|list`). The message
has up to three sections, each shown only when it has items: reminders (waiting on the owner), the
checklist, the links. Items are numbered across the whole board in that order. The owner ticks a check
item or clears a reminder with a button under the message; the session is told in one line.

Like the replies index in pins.py, creating, pinning and every edit go through `flood.optional_call`
(an edit is one budget unit); a skipped or failed call never fails the command, because the items are
saved first and the next change re-renders the whole message. Without the "Pin messages" right the pin is
retried only every PIN_BACKOFF seconds. The board message is never deleted by us. The items and the
message id per topic live in a small private state file guarded by a file lock; the lock is held across
the Telegram calls, so two commands never create two boards. A button press is the owner's own action and
is answered at once, so its edit does not wait for the optional budget.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import html
import json
import os
import re
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import flood, fmt
from . import lang as L
from .config import Config, home_chat, private_dir, state_dir, write_private
from .i18n import t
from .identity import isolate
from .projects import owner_chats
from .pins import PIN_BACKOFF, SUPERGROUP_PREFIX, link as message_link
from .telegram import TelegramError

STATE_FILE = "board.json"
_CALL_TIMEOUT = 5   # the board is a nicety: never hold the caller for the default 15 seconds
TITLE_CHARS = 80
DUE_CHARS = 40
URL_CHARS = 1000
LIMITS = {"link": 12, "check": 15, "remind": 8}
KEEP_TICKED = 5     # ticked check items kept; beyond that the oldest ticked ones go first
MAX_BUTTONS = 8
SECTIONS = (("remind", "board_remind"), ("check", "board_checks"), ("link", "board_links"))
BIDI_CONTROLS = re.compile("[‎‏‪-‮⁦-⁩]")
BUTTON_DATA = re.compile(r"bd:([0-9a-f]{8}):(\d{1,6})")


class BoardError(Exception):
    """A refusal with a one-line reason for the person (or agent) who ran the command."""


def _id(value) -> bool:
    return type(value) is int and value > 0


def key_of(chat: int, thread: int) -> str:
    return f"{chat}:{thread}"


def key_hash(key: str) -> str:
    """The short stand-in for a topic key inside a button (callback data is capped at 64 bytes)."""
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


# --- what may go on the board ------------------------------------------------

def clean_text(raw, limit: int, what: str = "title") -> str:
    """One line of plain text: whitespace collapsed, bidi controls removed (an agent's text must not
    reorder the lines around it), credentials redacted (it is shown on the board and in the control panel),
    at most `limit` characters."""
    from .creds import redact_channel
    text = " ".join(redact_channel(BIDI_CONTROLS.sub("", str(raw or ""))).split())
    if not text:
        raise BoardError(f"the {what} is empty")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def clean_url(raw) -> str:
    """An http(s) or t.me link. A bare t.me/... is read as https. Anything else is refused."""
    url = str(raw or "").strip()
    if re.match(r"(?i)t\.me/", url):
        url = "https://" + url
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        raise BoardError("not a valid link") from None
    if (parts.scheme.lower() not in ("http", "https") or not host or parts.username is not None
            or re.search(r"\s", url) or len(url) > URL_CHARS):
        raise BoardError("only http(s) and t.me links can go on the board")
    from .creds import hide_owner_links, redact_channel
    if hide_owner_links(redact_channel(url)) != url:  # never stored cut up: the button would lead nowhere
        raise BoardError("the link holds something that looks like a credential or a private sign-in link; "
                         "pin it without that part")
    return url


# --- state -------------------------------------------------------------------

@contextlib.contextmanager
def _locked():
    base = private_dir(state_dir())
    with open(base / (STATE_FILE + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield base / STATE_FILE


def _blank() -> dict:
    return {"id": 0, "pinned": False, "pin_retry_at": 0.0, "next": 1, "items": []}


def _num(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value == value


def _item(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    kind, ident, text = raw.get("k"), raw.get("i"), raw.get("s")
    if kind not in LIMITS or not _id(ident) or not isinstance(text, str) or not text:
        return None
    item = {"k": kind, "i": ident, "s": text, "x": raw.get("x") is True and kind == "check",
            "at": float(raw["at"]) if _num(raw.get("at")) else 0.0}
    if kind == "link":
        if not isinstance(raw.get("u"), str) or not raw["u"]:
            return None
        item["u"] = raw["u"]
    if kind == "remind" and isinstance(raw.get("d"), str) and raw["d"]:
        item["d"] = raw["d"]
    return item


def _load(path) -> Dict[str, dict]:
    """The state per "chat:thread", with anything malformed dropped (a hand-edited or half-written file
    must never break a command)."""
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
        retry = topic.get("pin_retry_at")
        top = max([i["i"] for i in items], default=0) + 1
        out[str(key)] = dict(_blank(), id=topic["id"] if _id(topic.get("id")) else 0, items=items,
                             pinned=topic.get("pinned") is True, pin_retry_at=float(retry) if _num(retry) else 0.0,
                             next=max(top, topic["next"] if _id(topic.get("next")) else 1))
    return out


def ordered(topic: dict) -> List[dict]:
    """The items in the order they are shown (and numbered): reminders, checklist, links."""
    return [i for kind, _ in SECTIONS for i in topic["items"] if i["k"] == kind]


def number_of(topic: dict, item: dict) -> int:
    return ordered(topic).index(item) + 1


# --- changing the items (each raises BoardError to refuse) ----------------------

def _append(topic: dict, kind: str, text: str, now: float, **extra) -> dict:
    item = dict(k=kind, i=topic["next"], s=text, x=False, at=now, **extra)
    topic["next"] += 1
    topic["items"].append(item)
    return item


def _trim(topic: dict) -> None:
    """Keep only KEEP_TICKED ticked items, dropping the oldest ticked first."""
    ticked = sorted((i for i in topic["items"] if i["x"]), key=lambda i: (i["at"], i["i"]))
    for gone in ticked[:max(0, len(ticked) - KEEP_TICKED)]:
        topic["items"].remove(gone)


def add_link(topic: dict, title: str, url: str, now: float) -> dict:
    topic["items"] = [i for i in topic["items"] if not (i["k"] == "link" and i["u"] == url)]  # same link: newest wins
    links = [i for i in topic["items"] if i["k"] == "link"]
    for gone in links[:max(0, len(links) + 1 - LIMITS["link"])]:
        topic["items"].remove(gone)  # oldest first
    return _append(topic, "link", title, now, u=url)


def add_check(topic: dict, text: str, now: float) -> dict:
    checks = [i for i in topic["items"] if i["k"] == "check"]
    if len(checks) >= LIMITS["check"]:
        ticked = [i for i in checks if i["x"]]
        if not ticked:
            raise BoardError(f"the checklist is full ({LIMITS['check']} open items); tick one with `board done <n>`")
        topic["items"].remove(ticked[0])
    return _append(topic, "check", text, now)


def add_remind(topic: dict, text: str, due: str, now: float) -> dict:
    if sum(1 for i in topic["items"] if i["k"] == "remind") >= LIMITS["remind"]:
        raise BoardError(f"there are already {LIMITS['remind']} reminders; clear one with `board done <n>`")
    return _append(topic, "remind", text, now, **({"d": due} if due else {}))


def finish(topic: dict, item: dict, now: float) -> dict:
    """Tick a check item (it stays on the board) or clear a reminder (it leaves)."""
    if item["k"] == "link":
        raise BoardError("that is a link; remove it with `board remove <n>`")
    if item["k"] == "check":
        if item["x"]:
            raise BoardError("already ticked")
        item.update(x=True, at=now)
        _trim(topic)
    else:
        topic["items"].remove(item)
    return item


def pick(topic: dict, n: int) -> dict:
    items = ordered(topic)
    if not 1 <= n <= len(items):
        raise BoardError(f"no item {n} on the board" if items else "the board is empty")
    return items[n - 1]


# --- the message -------------------------------------------------------------

def _label(text: str, lang: str) -> str:
    """An item's text as HTML; a Latin text inside a Hebrew line is wrapped in a bidi isolate."""
    shown = html.escape(text, quote=False)
    return isolate(shown, lang) if fmt.LAT.search(text) else shown


def render(cfg: Config, topic: dict) -> str:
    lang = getattr(cfg, "ui_language", "") or "en"
    mark = L.RLM if lang == "he" else ""
    items = ordered(topic)
    lines = [f"<b>{html.escape(t('board_header', lang=lang), quote=False)}</b>"]  # the Hebrew strings carry their own RLM
    if not items:
        lines.append(t("board_empty", lang=lang))
    number = 0
    for kind, heading in SECTIONS:
        section = [i for i in items if i["k"] == kind]
        if section:
            lines.append(f"\n<b>{html.escape(t(heading, lang=lang), quote=False)}</b>")
        for item in section:
            number += 1
            label = _label(item["s"], lang)
            if kind == "link":
                label = f'<a href="{html.escape(item["u"], quote=True)}">{label}</a>'
            elif kind == "check":
                label = f"☑ <s>{label}</s>" if item["x"] else f"☐ {label}"
            else:
                label = f"⏰ {label}" + (f" · {_label(item['d'], lang)}" if item.get("d") else "")
            lines.append(f"{mark}{number}. {label}")
    return "\n".join(lines)


def keyboard(cfg: Config, chat: int, thread: int, topic: dict) -> list:
    """One button per row for the reminders, then the open check items, at most MAX_BUTTONS. Only in a chat
    where the owner's button presses are accepted."""
    if chat not in owner_chats(cfg):
        return []
    lang = getattr(cfg, "ui_language", "") or "en"
    digest = key_hash(key_of(chat, thread))
    rows = []
    for item in ordered(topic):
        if item["k"] == "link" or item["x"]:
            continue
        name = "board_btn_clear" if item["k"] == "remind" else "board_btn_tick"
        text = clean_text(item["s"], 24, "text")
        text = isolate(text, lang) if fmt.LAT.search(text) else text
        rows.append([{"text": t(name, lang=lang, n=number_of(topic, item), text=text),
                      "callback_data": f"bd:{digest}:{item['i']}"}])
    return rows[:MAX_BUTTONS]


# --- Telegram ----------------------------------------------------------------

def _call(bot, method: str, params: dict, essential: bool):
    if essential:
        return bot.call(method, params, timeout=_CALL_TIMEOUT)
    return flood.optional_call(bot, method, params, timeout=_CALL_TIMEOUT)


def _gone(e: TelegramError) -> bool:
    return not getattr(e, "skipped", False) and e.code == 400 and "not found" in e.description.lower()


def _pin(cfg: Config, bot, chat: int, topic: dict, now: float) -> None:
    """Pin the board once. Without the admin right Telegram refuses (400/403): that is logged and not
    retried for PIN_BACKOFF; a gate skip or a network error is simply retried with the next change."""
    if topic["pinned"] or now < topic["pin_retry_at"]:
        return
    try:
        flood.optional_call(bot, "pinChatMessage", {"chat_id": chat, "message_id": topic["id"],
                            "disable_notification": True}, timeout=_CALL_TIMEOUT)
    except TelegramError as e:
        if not getattr(e, "skipped", False) and e.code in (400, 403):
            topic["pin_retry_at"] = now + PIN_BACKOFF
            from .mirror import log
            log(f"cannot pin the board (code {e.code}); grant the bot the admin right 'Pin messages'")
        return
    except Exception:
        return
    topic.update(pinned=True, pin_retry_at=0.0)


def _sync(cfg: Config, bot, chat: int, thread: int, topic: dict, now: float, essential: bool = False) -> bool:
    """Make the board message show `topic`: edit it, or create and pin it the first time (or when it was
    deleted). True when it shows the items. Never raises; a failure is healed by the next change."""
    if not (topic["id"] or topic["items"]):
        return True  # nothing was ever shown and nothing is left to show
    try:
        params = {"chat_id": chat, "text": render(cfg, topic), "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}}
        rows = keyboard(cfg, chat, thread, topic)
        if topic["id"]:
            try:
                _call(bot, "editMessageText", dict(params, message_id=topic["id"],
                                                   reply_markup={"inline_keyboard": rows}), essential)
            except TelegramError as e:
                if "not modified" not in e.description.lower():
                    if not _gone(e):
                        return False
                    topic.update(id=0, pinned=False, pin_retry_at=0.0)  # the board was deleted: make a new one
            if topic["id"]:
                _pin(cfg, bot, chat, topic, now)
                return True
        if rows:
            params["reply_markup"] = {"inline_keyboard": rows}
        sent = _call(bot, "sendMessage", dict(params, message_thread_id=thread, disable_notification=True), essential)
        mid = sent.get("message_id") if isinstance(sent, dict) else None
        if not _id(mid):
            return False
        topic.update(id=mid, pinned=False)
        _pin(cfg, bot, chat, topic, now)
        return True
    except Exception:
        return False


def apply(cfg: Config, bot, chat: int, thread: int, change: Callable[[dict], object],
          essential: bool = False, now: Optional[float] = None) -> Tuple[object, bool]:
    """Run `change` on the topic's state and show the result: (what `change` returned, whether the board
    message is up to date). A BoardError from `change` leaves everything as it was."""
    now = time.time() if now is None else now
    key = key_of(chat, thread)
    with _locked() as path:
        data = _load(path)
        topic = data.setdefault(key, _blank())
        result = change(topic)
        try:
            synced = _sync(cfg, bot, chat, thread, topic, now, essential)
        finally:
            write_private(path, json.dumps(data))
    return result, synced


def snapshot(chat: int, thread: int) -> dict:
    with _locked() as path:
        return _load(path).get(key_of(chat, thread)) or _blank()


def items_of(chat: int, thread: int) -> List[dict]:
    """One topic's board items in the order they are shown, read without the lock (the control panel only
    looks; the file is replaced atomically)."""
    topic = _load(state_dir() / STATE_FILE).get(key_of(chat, thread))
    return ordered(topic) if topic else []


def open_reminders() -> List[Tuple[int, int, dict]]:
    """Every reminder still waiting on the owner, across all topics, oldest first: (chat, thread, item). Read
    without the lock, which is safe because the file is replaced atomically (the control panel only looks)."""
    found = []
    for key, topic in _load(state_dir() / STATE_FILE).items():
        chat, _, thread = key.partition(":")
        if re.fullmatch(r"-?\d+", chat) and thread.isdigit():
            found += [(int(chat), int(thread), i) for i in topic["items"] if i["k"] == "remind"]
    return sorted(found, key=lambda f: (f[2]["at"], f[2]["i"]))


def all_items() -> List[Tuple[int, int, dict]]:
    """Every checklist item (ticked or not) and every reminder, across all topics: (chat, thread, item). Read
    without the lock, like open_reminders (the radio's flight board only looks)."""
    found = []
    for key, topic in _load(state_dir() / STATE_FILE).items():
        chat, _, thread = key.partition(":")
        if re.fullmatch(r"-?\d+", chat) and thread.isdigit():
            found += [(int(chat), int(thread), i) for i in topic["items"] if i["k"] in ("check", "remind")]
    return found


def open_checks() -> List[Tuple[int, int, dict]]:
    """Every checklist item not ticked yet, across all topics, newest first: (chat, thread, item). Read without
    the lock, like open_reminders."""
    found = []
    for key, topic in _load(state_dir() / STATE_FILE).items():
        chat, _, thread = key.partition(":")
        if re.fullmatch(r"-?\d+", chat) and thread.isdigit():
            found += [(int(chat), int(thread), i) for i in topic["items"] if i["k"] == "check" and not i.get("x")]
    return sorted(found, key=lambda f: (-f[2]["at"], f[2]["i"]))


# --- the commands --------------------------------------------------------------

def _where(cfg: Config, session: str) -> Tuple[int, int]:
    from .topics import place
    sid = session or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not sid:
        raise BoardError("no session: run it inside a Claude Code session (or pass --session)")
    found = place(cfg, sid)
    if found is None:
        raise BoardError("this session has no Telegram topic")
    if not str(found.chat).startswith(SUPERGROUP_PREFIX):
        raise BoardError("the board needs a topic in a forum group")
    return found.chat, found.thread


def _fail(reason: str) -> int:
    print(f"Harnessgram: {reason}", file=sys.stderr)
    return 1


def run(cfg: Config, action: str, session: str = "", text: str = "", url: str = "", due: str = "",
        n: int = 0, bot=None) -> int:
    """`ctb board <action>`: 0 when it worked (the item is saved even if Telegram is rate limiting and the
    message refreshes with the next change), 1 with a one-line reason when it was refused."""
    try:
        chat, thread = _where(cfg, session)
        if action == "list":
            topic = snapshot(chat, thread)
            for number, item in enumerate(ordered(topic), 1):
                mark = {"remind": "⏰", "link": "📎"}.get(item["k"]) or ("☑" if item["x"] else "☐")
                tail = item.get("u") or (f"({item['d']})" if item.get("d") else "")
                print(f"{number}. {mark} {item['s']}" + (f" {tail}" if tail else ""))
            if not topic["items"]:
                print("(the board is empty)")
            return 0
        if not cfg.ready:
            raise BoardError("bot_token / owner_chat_id missing in the config")
        if bot is None:
            from .telegram import Bot
            bot = Bot.from_config(cfg)
        now = time.time()
        if action == "add":
            title = clean_text(text, TITLE_CHARS)
            if url:
                target = clean_url(url)
            else:
                from .topics import latest_reply
                reply = latest_reply(chat, thread)
                if reply is None:
                    raise BoardError("no reply in this topic to link yet; pass a url")
                target = message_link(chat, thread, reply)
            change = lambda topic: add_link(topic, title, target, now)
        elif action == "check":
            body = clean_text(text, TITLE_CHARS, "text")
            change = lambda topic: add_check(topic, body, now)
        elif action == "remind":
            body = clean_text(text, TITLE_CHARS, "text")
            when = clean_text(due, DUE_CHARS, "due text") if due else ""
            change = lambda topic: add_remind(topic, body, when, now)
        elif action in ("done", "remove"):
            def change(topic):
                item = pick(topic, n)
                if action == "remove":
                    topic["items"].remove(item)
                    return item
                return finish(topic, item, now)
        else:
            raise BoardError(f"unknown board command {action}")
        item, synced = apply(cfg, bot, chat, thread, change, now=now)
        shown = {"add": "pinned", "check": "added to the checklist", "remind": "reminder added",
                 "done": "done", "remove": "removed"}[action]
        print(f"board: {shown}: {item['s']}" + ("" if synced else " (saved; the board message refreshes with the next change)"))
        return 0
    except BoardError as e:
        return _fail(str(e))
    except Exception as e:
        return _fail(f"the board is not available ({type(e).__name__})")


# --- the owner's taps ----------------------------------------------------------

def button(q: dict, cfg: Config, bot, inbox) -> str:
    """A tap on a board button ("bd:<topic hash>:<item id>"): from the owner, in a chat the owner is obeyed
    in, in the board's own topic, on the board message itself and on an item that is still open. Ticks the
    check item or clears the reminder, redraws the board in the tapped message's chat and tells the
    session in one line."""
    from .inbox import _answer_button, _deliver_button_word
    from .progress import append_boundary
    from .topics import TopicMap
    lang = getattr(cfg, "ui_language", "") or "en"
    m = q.get("message") or {}
    chat, thread = (m.get("chat") or {}).get("id"), m.get("message_thread_id")
    match = BUTTON_DATA.fullmatch(q.get("data") or "")
    sender = q.get("from") or {}
    session = (TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg).session_at(chat, thread)
               if type(chat) is int else None)
    if not (sender.get("id") == cfg.owner_chat_id and not sender.get("is_bot") and chat in owner_chats(cfg)
            and (m.get("chat") or {}).get("type") == "supergroup" and session and match and _id(thread)
            and _id(m.get("message_id")) and match.group(1) == key_hash(key_of(chat, thread))):
        _answer_button(bot, q, t("toast_not_available", lang=lang))
        return "ignored"

    def change(topic: dict) -> dict:
        found = [i for i in topic["items"] if i["i"] == int(match.group(2))]
        if topic["id"] != m["message_id"] or not found or found[0]["k"] == "link" or found[0]["x"]:
            raise BoardError("stale")
        return finish(topic, found[0], time.time())

    try:
        item, _ = apply(cfg, bot, chat, thread, change, essential=True)
    except BoardError:
        _answer_button(bot, q, t("board_toast_stale", lang=lang))
        return "ignored"
    _answer_button(bot, q, t("board_toast_clear" if item["k"] == "remind" else "board_toast_tick", lang=lang))
    append_boundary(session)
    word = "cleared reminder" if item["k"] == "remind" else "ticked"
    inbox.put(session, {"text": f"✅ owner {word}: {item['s']}", "at": int(time.time()), "id": None,
                        "source": "owner", "chat": chat, "button": "board"})
    _deliver_button_word(inbox, session)
    return "board"
