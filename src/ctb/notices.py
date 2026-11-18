"""Claude Code's own suggestion notices ("✦ Heads up · ... 1: Learn more  2: Knew this already  0: Dismiss") and
the feedback row that follows a tap ("✦ Dismissed. 1: That was helpful ... 0: Dismiss").

Neither is a permission or a question for the owner, so neither counts as waiting. The bridge relays the
notice once as a quiet topic message: in the owner's language first (translated; the English on its own line
below, never mixed on one line) with buttons for the notice's own options (a tap sends that digit), and it closes the feedback row by itself with 0. Digits are only typed when the input box is empty,
and a digit that landed in the box instead of the notice is taken back out.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .config import home_chat, private_dir, state_dir, write_private
from .creds import redact
from .i18n import get_lang, t
from .telegram import TelegramError
from .topics import place

RELAY_TTL = 86400.0   # the same notice text is not relayed again within a day
MAX_TRIES = 3         # automatic dismissals of one feedback row before giving up on it
MAX_TEXT = 700

_FEEDBACK = re.compile(r"(?i)^\s*✦\s+(?:dismissed|thanks|thank you)\b|\bwas (?:not )?helpful\b")
_OPTION = re.compile(r"(?<!\S)(\d):\s+(\S.*?)(?=\s{2,}|$)")
_MORE = re.compile(r"^\s*↓\s*\d+\s+more\b")


@dataclass
class Notice:
    kind: str                        # "tip" or "feedback"
    text: str                        # what the notice says, without the option row
    options: List[Tuple[int, str]]   # (digit, label) as drawn
    key: str                         # twelve hex characters of the text, the callback's proof


def find(body: str) -> Optional[Notice]:
    """The lowest ✦ notice on the screen, or None."""
    from . import inbox as I
    rows = (body or "").replace("‏", "").splitlines()
    span = I.tip_span(rows)
    if span is None:
        return None
    first, last = span
    options = [(int(n), label.strip()[:40]) for n, label in _OPTION.findall(rows[last])]
    if not any(n == 0 for n, _ in options):
        return None
    head = _OPTION.split(rows[last], maxsplit=1)[0]
    parts = [r for r in rows[first:last] if not _MORE.match(r)] + [head]
    text = " ".join(" ".join(parts).replace("✦", " ").split()).strip(" ·")
    text = redact(text)[:MAX_TEXT]
    kind = "feedback" if _FEEDBACK.search(rows[first]) or any("helpful" in label.lower() for _, label in options) else "tip"
    key = hashlib.sha256((kind + "\n" + text).encode("utf-8")).hexdigest()[:12]
    return Notice(kind, text, options, key)


def only_notice(body: str) -> bool:
    """A ✦ notice is on the screen and nothing else on it is a question."""
    from . import inbox as I
    return find(body) is not None and not I._dialog_open(I.strip_tip(body))


# --- state --------------------------------------------------------------------------------------

def _path():
    return private_dir(state_dir()) / "notices.json"


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    write_private(_path(), json.dumps(data, ensure_ascii=False))


# --- keys ---------------------------------------------------------------------------------------

def press(pane: str, digit: int) -> bool:
    """Type one digit for the notice. Only while the screen shows nothing but the notice and the input box
    exists and is empty (no box at all means a dialog may own the keyboard). If the digit stayed in the box,
    it is removed again. False when nothing was pressed or the digit was not taken."""
    from . import dialogs, inbox as I
    try:
        body = I.pane_text(pane)
    except Exception:
        return False
    if not only_notice(body):
        return False
    box, _ready = I.input_box(pane)
    if box != "":  # None: no input box (a dialog); text: the owner's draft
        return False
    if not dialogs._keys(pane, str(digit)):
        return False
    time.sleep(0.3)
    after, _ = I.input_box(pane)
    if after is not None and after.endswith(str(digit)):
        dialogs._keys(pane, "BSpace")
        return False
    return True


def screen_only_notice(session: str) -> bool:
    """The session's screen shows a ✦ notice and no question."""
    return _pane_notice(session)[1] is not None


def _pane_notice(session: str):
    from . import inbox as I
    pane = I.pane_for_session(session) if session else None
    if not pane:
        return None, None
    try:
        body = I.pane_text(pane)
    except Exception:
        return pane, None
    return pane, (find(body) if only_notice(body) else None)


# --- relay --------------------------------------------------------------------------------------

def _markup(n: Notice, lang: str) -> dict:
    from . import menus
    labels = OPTION_HE if lang == "he" else {}
    row = [{"text": f"{num}. {labels.get(label.lower(), label)}", "callback_data": f"nt:{num}:{n.key}"}
           for num, label in n.options]
    return {"inline_keyboard": [row[i:i + 2] for i in range(0, len(row), 2)] + [menus.unbuttons_row(lang)]}


# Claude Code's own option labels, in Hebrew (the button text only; the digit sent is unchanged).
TRANSLATE_WAIT = 3.0  # seconds a notice waits for a translation that is not cached yet
OPTION_HE = {"learn more": "מידע נוסף", "knew this already": "כבר ידעתי", "dismiss": "סגירה",
             "that was helpful": "זה עזר", "not helpful": "לא עזר"}


def _translated(cfg, text: str, lang: str) -> str:
    """The notice in the owner's language (U+200F-led), or "" when it is already in it or translation fails."""
    try:
        from . import lang as L
        if lang == "en" or not getattr(cfg, "translate", False) or not L.needs_translation(text, lang):
            return ""
        from .translate import cached, translate_text
        out = cached(text, cfg)
        if out is None:
            # Never hold the notice for a slow engine: wait briefly, else send the original now. The
            # translation still finishes in the background and is cached for the next time.
            box: list = []

            def work():
                try:
                    box.append(translate_text(text, cfg))  # the full path writes the cache; fast never does
                except Exception:
                    pass  # the original is sent; a failed engine is not an error for the notice

            worker = threading.Thread(target=work, name="notice-translate", daemon=True)
            worker.start()
            worker.join(TRANSLATE_WAIT)
            out = box[0] if box else ""
        out = " ".join((out or "").split())
    except Exception:
        return ""
    if not out or out == text:
        return ""
    return ("\u200f" + out) if lang == "he" else out


def _lang(cfg) -> str:
    return getattr(cfg, "ui_language", None) or get_lang()


def _relay(cfg, bot, session: str, thread: int, chat, n: Notice) -> Optional[int]:
    import html
    lang = _lang(cfg)
    body = html.escape(n.text)
    local = _translated(cfg, n.text, lang)
    if local:
        body = html.escape(local) + "\n\n" + body  # never one mixed-direction line; the original stays below
    params = {"chat_id": home_chat(cfg) if chat is None else chat, "message_thread_id": thread,
              "text": t("notice_head", lang=lang) + "\n" + body,
              "parse_mode": "HTML", "disable_notification": True, "reply_markup": _markup(n, _lang(cfg))}
    try:
        return bot.call("sendMessage", params).get("message_id")
    except (TelegramError, AttributeError):
        return None


def handle(cfg, bot, session: str, thread: int, chat=None) -> str:
    """One notice on this session's screen: relay a tip once, close a feedback row. Returns what was done:
    "relayed", "dismissed", "retry", "seen" or "" (no notice)."""
    pane, n = _pane_notice(session)
    if n is None:
        return ""
    data = _load()
    rec = data.get(session) if isinstance(data.get(session), dict) else {}
    now = time.time()
    if n.kind == "feedback":
        tries = rec.get("tries", 0) if rec.get("key") == n.key else 0
        if tries >= MAX_TRIES:
            return "seen"
        data[session] = {"key": n.key, "tries": tries + 1, "at": now}
        _save(data)
        return "dismissed" if press(pane, 0) else "retry"
    if rec.get("key") == n.key and now - rec.get("at", 0) < RELAY_TTL:
        return "seen"
    mid = _relay(cfg, bot, session, thread, chat, n)
    if mid is None:
        return "retry"
    data[session] = {"key": n.key, "at": now, "mid": mid}
    _save(data)
    return "relayed"


def scan(cfg, bot, topics) -> int:
    """One pass over the sessions that have a topic. Returns how many notices were relayed."""
    from . import inbox as I
    relayed = 0
    for session, entry in topics.all_places().items():
        thread = entry.get("thread") if isinstance(entry, dict) else None
        if not isinstance(thread, int):
            continue
        if not I.session_state(session).get("state") and not _load().get(session):
            continue
        chat = None if entry.get("home", True) else entry["chat"]
        try:
            relayed += handle(cfg, bot, session, thread, chat) == "relayed"
        except Exception as exc:
            I.poll_log(f"notice failed: {type(exc).__name__}")
    return relayed


def button(q: dict, cfg, bot, m: dict, session: str) -> str:
    """nt:<digit>:<key> from a relayed notice: the digit is typed only while that same notice is still shown."""
    from . import inbox as I
    lang = _lang(cfg)
    _, num, key = (q.get("data") or "").split(":")
    found = place(cfg, session)
    if ((q.get("from") or {}).get("id") != cfg.owner_user_id or (q.get("from") or {}).get("is_bot")
            or not found or found.chat != (m.get("chat") or {}).get("id")
            or found.thread != m.get("message_thread_id")):
        I._answer_button(bot, q, t("toast_not_available", lang=lang))
        return "ignored"
    pane, n = _pane_notice(session)
    if n is None or n.key != key or int(num) not in {d for d, _ in n.options}:
        I._answer_button(bot, q, t("toast_question_closed", lang=lang))
        _drop_keyboard(bot, m)
        return "closed"
    if not press(pane, int(num)):
        I._answer_button(bot, q, t("toast_not_available", lang=lang))
        return "failed"
    I._answer_button(bot, q, t("toast_key_sent", lang=lang, name=num))
    _drop_keyboard(bot, m)
    return "sent"


def _drop_keyboard(bot, m: dict) -> None:
    try:
        bot.call("editMessageReplyMarkup", {"chat_id": (m.get("chat") or {}).get("id"),
                 "message_id": m.get("message_id"), "reply_markup": {"inline_keyboard": []}})
    except (TelegramError, AttributeError):
        pass
