"""A pin board per session topic: links worth keeping, a checklist and reminders for the owner.

The board rides on the topic's pinned Cockpit message. Until that pin exists it has a standalone,
unpinned message; an older separately pinned board is migrated away after the Cockpit pin has absorbed it.

An agent adds to it from inside its session (`ctb board add|check|remind|done|remove|list`). The message
has up to three sections, each shown only when it has items: reminders (waiting on the owner), the
checklist, the links. Items are numbered across the whole board in that order. The owner ticks a check
item or clears a reminder with a button under the message; the session is told in one line.

Like the replies index in pins.py, creating and every edit go through `flood.optional_call`
(an edit is one budget unit); a skipped or failed call never fails the command, because the items are
saved first and the next change re-renders the whole message. The board message is never deleted by us. The items and the
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
import math
import os
import re
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import flood
from .lazy import lazy_module
from . import lang as L
from .config import Config, home_chat, private_dir, state_dir, write_private
from .i18n import t
from .identity import isolate
from .projects import owner_chats
from .pins import PIN_BACKOFF, SUPERGROUP_PREFIX, link as message_link
from .telegram import TelegramError

fmt = lazy_module(".fmt")

STATE_FILE = "board.json"
_CALL_TIMEOUT = 5   # the board is a nicety: never hold the caller for the default 15 seconds
TITLE_CHARS = 80
DUE_CHARS = 40
URL_CHARS = 1000
LIMITS = {"link": 12, "check": 15, "remind": 8}
KEEP_TICKED = 5     # ticked check items kept; beyond that the oldest ticked ones go first
MAX_BUTTONS = 8
PIN_TEXT_MAX = 3500
SECTIONS = (("remind", "board_remind"), ("check", "board_checks"), ("link", "board_links"))
BIDI_CONTROLS = re.compile("[‎‏‪-‮⁦-⁩]")
BUTTON_DATA = re.compile(r"b([dk]):([0-9a-f]{8}):(\d{1,6})")
BUILD_NUMBER = re.compile(
    r"(?ix)(?:\bbuild\s*|\bupdate\s+to\s+version\s*|\btestflight(?:\s+build)?\s*|"
    r"(?<![\w\u0590-\u05ff])[לבה]?(?:גרסה|בנייה)(?![\w\u0590-\u05ff])\s*)(\d{1,6})(?!\w)"
)
STALE_AFTER = 7 * 86400


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


def clean_ledger_ref(raw) -> str:
    """A non-ambiguous ledger reference: project#id or an exact safe HTTPS URL."""
    ref = clean_text(raw, URL_CHARS, "ledger reference")
    if re.fullmatch(r"[^#\s]{1,200}#[^#\s]{1,200}", ref):
        return ref
    if ref.startswith("https://"):
        try:
            if clean_url(ref) == ref:
                return ref
        except BoardError:
            pass
    raise BoardError("--ledger must be project#id or an exact https link (a bare id is ambiguous)")


def _valid_exact_url(value: str) -> bool:
    try:
        return clean_url(value) == value
    except BoardError:
        return False


# --- state -------------------------------------------------------------------

@contextlib.contextmanager
def _locked():
    base = private_dir(state_dir())
    with open(base / (STATE_FILE + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield base / STATE_FILE


def _blank() -> dict:
    return {"id": 0, "pinned": False, "pin_retry_at": 0.0, "onpin": False, "gone": 0,
            "next": 1, "items": [], "history": []}


def _num(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value == value


def _finite_float(value) -> Optional[float]:
    if not _num(value):
        return None
    try:
        converted = float(value)
    except (OverflowError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _json_int(value: str):
    """Parse ordinary JSON integers while turning unreasonably large ones into invalid values."""
    return int(value) if len(value.lstrip("-")) <= 100 else None


def _item(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    kind, ident, text = raw.get("k"), raw.get("i"), raw.get("s")
    if kind not in LIMITS or not _id(ident) or not isinstance(text, str) or not text:
        return None
    at = _finite_float(raw.get("at"))
    item = {"k": kind, "i": ident, "s": text, "x": raw.get("x") is True and kind == "check",
            "at": at if at is not None else 0.0}
    if kind == "link":
        if not isinstance(raw.get("u"), str) or not raw["u"]:
            return None
        item["u"] = raw["u"]
    if kind == "remind" and isinstance(raw.get("d"), str) and raw["d"]:
        item["d"] = raw["d"]
    restored = _finite_float(raw.get("r"))
    if kind == "remind" and restored is not None and restored > 0:
        item["r"] = restored
    prompted = _finite_float(raw.get("q"))
    if kind == "remind" and prompted is not None and prompted > 0:
        item["q"] = prompted
    if kind == "remind" and isinstance(raw.get("l"), str) and raw["l"]:
        item["l"] = raw["l"][:1000]
    return item


def _load(path) -> Dict[str, dict]:
    """The state per "chat:thread", with anything malformed dropped (a hand-edited or half-written file
    must never break a command)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_int=_json_int)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, dict] = {}
    for key, topic in data.items():
        if not isinstance(topic, dict) or not isinstance(topic.get("items"), list):
            continue
        items = [i for i in map(_item, topic["items"]) if i]
        retry = _finite_float(topic.get("pin_retry_at"))
        top = max([i["i"] for i in items], default=0) + 1
        out[str(key)] = dict(_blank(), id=topic["id"] if _id(topic.get("id")) else 0, items=items,
                             pinned=topic.get("pinned") is True, pin_retry_at=retry if retry is not None else 0.0,
                             onpin=topic.get("onpin") is True,
                             gone=topic["gone"] if _id(topic.get("gone")) else 0,
                             next=max(top, topic["next"] if _id(topic.get("next")) else 1),
                             history=[{"at": timestamp, "i": h["i"], "reason": h["reason"]}
                                      for h in (topic.get("history", [])[-100:]
                                                if isinstance(topic.get("history"), list) else [])
                                      if isinstance(h, dict)
                                      for timestamp in [_finite_float(h.get("at"))]
                                      if timestamp is not None and _id(h.get("i"))
                                      and h.get("reason") in ("expired", "superseded", "linked")])
        if topic.get("dirty") is True:
            out[str(key)]["dirty"] = True
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


def add_remind(topic: dict, text: str, due: str, now: float, ledger: str = "") -> dict:
    if sum(1 for i in topic["items"] if i["k"] == "remind") >= LIMITS["remind"]:
        raise BoardError(f"there are already {LIMITS['remind']} reminders; clear one with `board done <n>`")
    extra = {"d": due} if due else {}
    if ledger:
        extra["l"] = ledger
    return _append(topic, "remind", text, now, **extra)


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
        clear = {"text": t(name, lang=lang, n=number_of(topic, item), text=text),
                 "callback_data": f"bd:{digest}:{item['i']}"}
        if item["k"] == "remind" and item.get("q"):
            rows.append([{"text": "עדיין רלוונטי" if lang == "he" else "Keep",
                          "callback_data": f"bk:{digest}:{item['i']}"},
                         dict(clear, text="לנקות" if lang == "he" else "Clear")])
        else:
            rows.append([clear])
    return rows[:MAX_BUTTONS]


# --- Telegram ----------------------------------------------------------------

def _call(bot, method: str, params: dict, essential: bool):
    if essential:
        return bot.call(method, params, timeout=_CALL_TIMEOUT)
    return flood.optional_call(bot, method, params, timeout=_CALL_TIMEOUT)


def _gone(e: TelegramError) -> bool:
    return not getattr(e, "skipped", False) and e.code == 400 and "not found" in e.description.lower()


def pin_content(cfg: Config, chat: int, thread: int, base_text: str, base_rows: list,
                topic: Optional[dict] = None) -> dict:
    """Compose the Cockpit pin with its board, without performing Telegram I/O."""
    topic = topic or view(chat, thread)
    text, rows = html.escape(base_text, quote=False), list(base_rows)
    status_text = ""
    try:
        from . import topic_status
        from .topics import TopicMap
        session = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg).session_at(chat, thread)
        totals = topic_status.totals_for_session(cfg, session, time.time()) if session else None
        status_text = t("topic_status_pin_counts", lang=cfg.ui_language, **totals) if totals else ""
    except Exception:
        status_text = ""
    if topic["items"]:
        shown = topic
        lines = render(cfg, shown).split("\n", 1)[1]
        if len(text) + len(lines) + 2 > PIN_TEXT_MAX:
            # URLs are by far the largest board values. Preserve the actionable reminders and
            # checklist in the pin before falling back to a bare item count.
            shown = dict(topic, items=[item for item in topic["items"] if item["k"] != "link"])
            lines = render(cfg, shown).split("\n", 1)[1] if shown["items"] else ""
            dropped = sum(item["k"] == "link" for item in topic["items"])
            if dropped:
                links_more = html.escape(t("board_pin_links_more", lang=cfg.ui_language,
                                           n=dropped), quote=False)
                lines = "\n".join(part for part in (lines, links_more) if part)
        if len(text) + len(lines) + 2 <= PIN_TEXT_MAX:
            if lines:
                text += "\n\n" + lines
            rows += keyboard(cfg, chat, thread, shown)
        else:
            text += "\n\n" + html.escape(t("board_pin_more", lang=cfg.ui_language,
                                             n=len(topic["items"])), quote=False)
    if status_text and len(text) + len(status_text) + 2 <= PIN_TEXT_MAX:
        text += "\n\n" + status_text
    return {"text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True},
            "reply_markup": {"inline_keyboard": rows}}


def status_digest(cfg: Config, chat: int, thread: int, now: Optional[float] = None) -> str:
    """Minute-granularity digest used by the existing pin refresher; no Telegram I/O."""
    try:
        from . import topic_status
        from .topics import TopicMap
        session = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg).session_at(chat, thread)
        return topic_status.digest_for_session(cfg, session, time.time() if now is None else now) if session else ""
    except Exception:
        return ""


def _unpin_old(bot, chat: int, topic: dict, now: float) -> bool:
    if not topic["pinned"]:
        return True
    if now < topic["pin_retry_at"]:
        return False
    try:
        flood.optional_call(bot, "unpinChatMessage", {"chat_id": chat, "message_id": topic["id"]},
                            timeout=_CALL_TIMEOUT)
    except TelegramError as e:
        if getattr(e, "skipped", False) or e.code is None or e.code == 429 or e.code >= 500:
            return False
        if e.code == 400 and ("not found" in e.description.lower() or "not pinned" in e.description.lower()):
            topic["pinned"] = False
            return True
        topic["pin_retry_at"] = now + PIN_BACKOFF
        return False
    except Exception:
        return False
    topic["pinned"] = False
    return True


def view(chat: int, thread: int) -> dict:
    return _load(state_dir() / STATE_FILE).get(key_of(chat, thread)) or _blank()


def board_message(chat: int, thread: int) -> int:
    return view(chat, thread)["id"]


def settle(chat: int, thread: int, shown: List[dict]) -> None:
    try:
        with _locked() as path:
            data = _load(path)
            row = data.get(key_of(chat, thread))
            if not row:
                return
            row["onpin"] = bool(shown)
            if row["items"] == shown:
                row.pop("dirty", None)
            else:
                row["dirty"] = True
            write_private(path, json.dumps(data))
    except Exception:
        pass


def old_pins() -> set:
    return {key for key, value in _load(state_dir() / STATE_FILE).items()
            if value["pinned"] and value["id"] and value["onpin"]}


def migrate_old(cfg: Config, bot, chat: int, thread: int, pin_id: int, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    try:
        with _locked() as path:
            data = _load(path)
            topic = data.get(key_of(chat, thread))
            if topic and topic["id"] and topic["onpin"]:
                if not _move_old(cfg, bot, chat, thread, pin_id, topic):
                    return
                if topic["id"] and _unpin_old(bot, chat, topic, now):
                    topic.update(id=0, pin_retry_at=0.0)
                write_private(path, json.dumps(data))
    except Exception:
        pass


def gone_id(chat: int, thread: int) -> int:
    return view(chat, thread)["gone"]


def _edit_cockpit(cfg, bot, chat, thread, topic, pin_id, essential):
    if not (topic["items"] or topic["onpin"]):
        return "ok"
    from . import panel_pin
    base = panel_pin.base_content(cfg, chat, thread)
    if base is None:
        return "fail"
    try:
        _call(bot, "editMessageText", dict(pin_content(cfg, chat, thread, *base, topic),
                                             chat_id=chat, message_id=pin_id), essential)
    except TelegramError as exc:
        if "not modified" in exc.description.lower():
            return "ok"
        return "gone" if _gone(exc) else "fail"
    return "ok"


def _move_old(cfg: Config, bot, chat: int, thread: int, pin_id: int, topic: dict) -> bool:
    """Replace a standalone board with a link to its Cockpit carrier.

    A missing old message is already migrated. Other edit failures leave its id intact so a later
    synchronization can retry without discarding the only board the owner can still see.
    """
    text = t("board_pin_moved", lang=cfg.ui_language, link=message_link(chat, thread, pin_id))
    try:
        flood.optional_call(bot, "editMessageText", {"chat_id": chat, "message_id": topic["id"],
                            "text": text, "parse_mode": "HTML",
                            "link_preview_options": {"is_disabled": True}}, timeout=_CALL_TIMEOUT)
    except TelegramError as exc:
        if "not modified" in exc.description.lower():
            return True
        if _gone(exc):
            topic.update(id=0, pinned=False, pin_retry_at=0.0)
            return True
        return False
    return True


def _sync(cfg: Config, bot, chat: int, thread: int, topic: dict, now: float, essential: bool = False) -> bool:
    """Make the board message show `topic`: edit it, or create and pin it the first time (or when it was
    deleted). True when it shows the items. Never raises; a failure is healed by the next change."""
    try:
        from . import panel_pin
        pin_id = panel_pin.pin_message(chat, thread)
        if pin_id:
            status = _edit_cockpit(cfg, bot, chat, thread, topic, pin_id, essential)
            if status == "gone":
                topic["gone"] = pin_id
            if status == "ok":
                topic["onpin"] = bool(topic["items"])
                if topic["id"] and not _move_old(cfg, bot, chat, thread, pin_id, topic):
                    return False
                if topic["id"] and _unpin_old(bot, chat, topic, now):
                    topic.update(id=0, pin_retry_at=0.0)
            return status == "ok"
        if not (topic["id"] or topic["items"]):
            return True
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
                return True
        if rows:
            params["reply_markup"] = {"inline_keyboard": rows}
        sent = _call(bot, "sendMessage", dict(params, message_thread_id=thread, disable_notification=True), essential)
        mid = sent.get("message_id") if isinstance(sent, dict) else None
        if not _id(mid):
            return False
        topic.update(id=mid, pinned=False)
        return True
    except Exception:
        return False


def apply(cfg: Config, bot, chat: int, thread: int, change: Callable[[dict], object],
          essential: bool = False, now: Optional[float] = None, sync: bool = True) -> Tuple[object, bool]:
    """Run `change` on the topic's state and show the result: (what `change` returned, whether the board
    message is up to date). A BoardError from `change` leaves everything as it was."""
    now = time.time() if now is None else now
    key = key_of(chat, thread)
    with _locked() as path:
        data = _load(path)
        topic = data.setdefault(key, _blank())
        result = change(topic)
        topic["dirty"] = True
        # Owner actions are durable immediately, even while Telegram is slow. State
        # polls read the atomic file without waiting for this optional message edit.
        write_private(path, json.dumps(data))
        if not sync:
            return result, False  # heartbeat edits the Telegram message, off the HTTP deadline
        try:
            synced = _sync(cfg, bot, chat, thread, topic, now, essential)
            if synced:
                topic.pop("dirty", None)
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

def refresh_dirty(cfg: Config, bot, limit: int = 2) -> int:
    """Retry message edits off the HTTP path, without holding the state lock during I/O.

    Only existing messages are edited here; normal agent changes own board creation and
    pinning. A concurrent change keeps its dirty bit for the next heartbeat, so an old
    snapshot can never overwrite newer local state or falsely acknowledge a newer edit.
    """
    data = _load(state_dir() / STATE_FILE)
    refreshed = 0
    from . import panel_pin
    for key, topic in data.items():
        chat, _, thread = key.partition(":")
        if (not topic.get("dirty") or not re.fullmatch(r"-?\d+", chat)
                or not thread.isdigit() or int(chat) not in owner_chats(cfg)):
            continue
        pin_id = panel_pin.pin_message(int(chat), int(thread))
        if not (pin_id or topic["id"]):
            continue
        if limit <= 0:
            break
        limit -= 1
        try:
            if pin_id:
                status = _edit_cockpit(cfg, bot, int(chat), int(thread), topic, pin_id, False)
                if status != "ok":
                    if status == "gone":
                        with _locked() as path:
                            current = _load(path)
                            if key in current:
                                current[key]["gone"] = pin_id
                                write_private(path, json.dumps(current))
                    continue
            else:
                _call(bot, "editMessageText", {"chat_id": int(chat), "message_id": topic["id"],
                      "text": render(cfg, topic), "parse_mode": "HTML", "link_preview_options": {"is_disabled": True},
                      "reply_markup": {"inline_keyboard": keyboard(cfg, int(chat), int(thread), topic)}}, False)
        except TelegramError as exc:
            if "not modified" not in exc.description.lower():
                continue
        except Exception:
            continue
        with _locked() as path:
            current = _load(path)
            row = current.get(key)
            if row and row["id"] == topic["id"] and row["items"] == topic["items"]:
                row.pop("dirty", None)
                if pin_id:
                    row["onpin"] = bool(row["items"])
                write_private(path, json.dumps(current))
                refreshed += 1
            elif row and row["id"] == topic["id"]:
                row["dirty"] = True  # our older edit may have arrived after a concurrent newer edit
                write_private(path, json.dumps(current))
    return refreshed

def _ledger_closed(cfg: Config, refs: set) -> set:
    """Linked ledger references which are done/dropped, without a default real-machine path."""
    from . import owner_requests
    folder = owner_requests.ledger_dir(cfg)
    if folder is None or not refs:
        return set()
    closed = set()
    try:
        paths = sorted(folder.glob("*.json"))[:100]
    except OSError:
        return closed
    for path in paths:
        try:
            raw = owner_requests._read(path)
        except (OSError, ValueError, RecursionError):
            continue
        for item in (raw.get("items", []) if isinstance(raw, dict) else [])[:owner_requests.MAX_ITEMS]:
            if not isinstance(item, dict) or item.get("status") not in ("done", "dropped"):
                continue
            candidates = {f"{path.stem}#{item.get('id', '')}"}
            links = item.get("links")
            if (isinstance(links, list)
                    and all(isinstance(link, str) and len(link) <= URL_CHARS for link in links)):
                candidates.update(link for link in links
                                  if link.startswith("https://") and _valid_exact_url(link))
            closed.update(refs & candidates)
    return closed


def _record(topic: dict, item: dict, reason: str, now: float) -> None:
    topic.setdefault("history", []).append({"at": now, "i": item["i"], "reason": reason})
    topic["history"] = topic["history"][-100:]


def sweep(cfg: Config, bot=None, now: Optional[float] = None) -> dict:
    """Conservatively clear obsolete reminders and flag old undated ones for an owner choice.

    Recheck under the board lock so concurrent clears/restores cannot be overwritten.
    The heartbeat and the command share this same implementation.
    """
    from .reminder_lifecycle import expired_text, is_kept
    now = time.time() if now is None else now
    changed = []
    counts = {"expired": 0, "superseded": 0, "linked": 0, "prompted": 0}
    with _locked() as path:
        data = _load(path)
        builds = {}
        refs = {i["l"] for topic in data.values() for i in topic["items"] if i["k"] == "remind" and i.get("l")}
        closed = _ledger_closed(cfg, refs)
        for key, topic in data.items():
            for item in topic["items"]:
                match = BUILD_NUMBER.search(item["s"]) if item["k"] == "remind" else None
                if match:
                    builds[key] = max(builds.get(key, 0), int(match.group(1)))
        for key, topic in data.items():
            chat, _, thread = key.partition(":")
            if not re.fullmatch(r"-?\d+", chat) or not thread.isdigit() or int(chat) not in owner_chats(cfg):
                continue
            gone = []
            touched = False
            for item in topic["items"]:
                if item["k"] != "remind":
                    continue
                if is_kept(item):
                    continue
                match = BUILD_NUMBER.search(item["s"])
                reason = ("expired" if expired_text(item, now) else
                          "superseded" if match and int(match.group(1)) < builds.get(key, 0) else
                          "linked" if item.get("l") in closed else "")
                if reason:
                    gone.append(item)
                    counts[reason] += 1
                    _record(topic, item, reason, now)
                    touched = True
                elif (not item.get("d") and not item.get("q")
                      and now - max(item["at"], item.get("r", 0)) > STALE_AFTER):
                    item["q"] = now
                    counts["prompted"] += 1
                    touched = True
            if gone:
                topic["items"] = [i for i in topic["items"] if i not in gone]
            if touched:
                topic["dirty"] = True
                changed.append((int(chat), int(thread), topic))
        if changed:
            write_private(path, json.dumps(data))
    return dict(cleared=counts["expired"] + counts["superseded"] + counts["linked"],
                topics=len(changed), **counts)


def daily_sweep(cfg: Config, bot, now: float) -> None:
    """One sweep per day across heartbeat restarts; record counts only."""
    from .mirror import log
    with _locked() as path:
        stamp = path.parent / "board-sweep.json"
        try:
            last = json.loads(stamp.read_text()).get("at", 0)
        except (OSError, ValueError, AttributeError):
            last = 0
        if _num(last) and 0 <= now - last < 86400:
            return
        write_private(stamp, json.dumps({"at": now}))
    try:
        counts = sweep(cfg, bot, now)
    except Exception:
        with _locked() as path:
            write_private(path.parent / "board-sweep.json", json.dumps({"at": 0}))
        raise
    log("board sweep: " + " ".join(f"{key}={counts.get(key, 0)}" for key in
        ("cleared", "expired", "superseded", "linked", "prompted", "topics")))

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
    print(f"HarnessGram: {reason}", file=sys.stderr)
    return 1


def run(cfg: Config, action: str, session: str = "", text: str = "", url: str = "", due: str = "",
        n: int = 0, bot=None, ledger: str = "") -> int:
    """`ctb board <action>`: 0 when it worked (the item is saved even if Telegram is rate limiting and the
    message refreshes with the next change), 1 with a one-line reason when it was refused."""
    try:
        if action == "sweep":
            if bot is None:
                from .telegram import Bot
                bot = Bot.from_config(cfg)
            counts = sweep(cfg, bot)
            print(" ".join(f"{key}={counts[key]}" for key in counts))
            return 0
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
            linked = clean_ledger_ref(ledger) if ledger else ""
            change = lambda topic: add_remind(topic, body, when, now, linked)
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
            and _id(m.get("message_id")) and match.group(2) == key_hash(key_of(chat, thread))):
        _answer_button(bot, q, t("toast_not_available", lang=lang))
        return "ignored"

    from . import panel_pin
    shown = {mid for mid in (board_message(chat, thread), panel_pin.pin_message(chat, thread)) if mid}

    def change(topic: dict) -> dict:
        found = [i for i in topic["items"] if i["i"] == int(match.group(3))]
        if m["message_id"] not in shown or not found or found[0]["k"] == "link" or found[0]["x"]:
            raise BoardError("stale")
        if match.group(1) == "k":
            if found[0]["k"] != "remind" or not found[0].get("q"):
                raise BoardError("stale")
            found[0].pop("q", None)
            found[0]["r"] = time.time()
            return found[0]
        return finish(topic, found[0], time.time())

    try:
        item, _ = apply(cfg, bot, chat, thread, change, essential=True)
    except BoardError:
        _answer_button(bot, q, t("board_toast_stale", lang=lang))
        return "ignored"
    kept = match.group(1) == "k"
    _answer_button(bot, q, ("נשמר" if lang == "he" else "Kept") if kept else
                   t("board_toast_clear" if item["k"] == "remind" else "board_toast_tick", lang=lang))
    append_boundary(session)
    word = "kept reminder" if kept else "cleared reminder" if item["k"] == "remind" else "ticked"
    inbox.put(session, {"text": f"✅ owner {word}: {item['s']}", "at": int(time.time()), "id": None,
                        "source": "owner", "chat": chat, "button": "board"})
    _deliver_button_word(inbox, session)
    return "board"
