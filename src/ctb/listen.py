"""The "listen" button under a final reply: a voice note of that one reply, on request.

The button (`ls:<10 hex>`, 13 bytes of callback data) goes on the last part of a final reply in a topic, never on
alerts, cards, decisions or progress messages. The mirror files the reply's shown (Hebrew) text under the same short
id in <state>/listen.json; a tap looks the id up, so the callback data never carries text.

A tap (owner only):
 1. the text is cleaned for the ear (no code, links, paths or Summary label), cut to MAX_CHARS, and put
    through the radio's suitability rules (radio_room.clean); then one recording at a time, PER_HOUR an hour;
 3. the free local voice speaks it (never a paid one: a paid request cannot be held to a hard cap here); ffmpeg turns
    it into OGG/Opus and sendVoice replies to the reply.
A recording already made is sent again by its Telegram file id, in a thread, after the same text checks; the id is
dropped only when Telegram says the file is gone.
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from .config import locked_map, read_map, state_dir
from .i18n import get_lang, t
from .telegram import TelegramError

CALLBACK = re.compile(r"ls:[0-9a-f]{10}")
MAX_CHARS = 1200
PER_HOUR = 20
KEEP = 300  # replies remembered
SAVED_CHARS = 20000
FREE_VOICE = "Carmit"

_busy = threading.Lock()  # one recording at a time

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_MD_LINK = re.compile(r"\[([^\]\n]*)\]\([^)\s]*\)")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_CODE_SPAN = re.compile(r"`[^`\n]*`")
_PATH = re.compile(r"(?<![\w/])(?:~|\.{1,2})?(?:/[\w.\-@+]+)+/?|(?<![\w/])[\w.\-@+]+(?:/[\w.\-@+]+)+/?")
_FILE = re.compile(r"(?<![\w/.])[\w\-]+\.(?:py|js|jsx|ts|tsx|json|md|txt|ya?ml|toml|sh|html?|css|log|lock|cfg|ini|png|jpe?g|pdf|csv)\b",
                   re.I)
_LABEL = re.compile(r"(?im)^[ \t>#*_-]*(?:summary|סיכום)[ \t*_]*:[ \t*_]*")
_BULLET = re.compile(r"(?m)^[ \t]*(?:[-*•]|\d+\.)[ \t]+")
_SENTENCE_END = re.compile(r"[.!?؟…]\s|[.!?]$")


def _paths() -> Tuple[Path, Path]:
    return state_dir() / "listen.json", state_dir() / "listen-rate.json"


def new_token() -> str:
    return secrets.token_hex(5)


def button_row(token: str, lang: Optional[str] = None) -> list:
    return [{"text": t("btn_listen", lang=lang), "callback_data": "ls:" + token}]


def remember(token: str, session: str, text: str) -> None:
    """File the reply's shown text under its button's id."""
    if not token or not (text or "").strip():
        return
    with locked_map(_paths()[0], keep=KEEP) as data:
        data[token] = {"session": session, "text": text[:SAVED_CHARS], "at": int(time.time())}


# --- the words ---------------------------------------------------------------------------------

def clean_text(raw: str) -> Tuple[str, bool]:
    """(the text for the ear, whether it was cut). No code, links, file paths or Summary label; at most
    MAX_CHARS, cut at a sentence end when there is one."""
    text = _FENCE.sub(" ", raw or "")
    text = _CODE_SPAN.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    text = _PATH.sub(" ", text)
    text = _FILE.sub(" ", text)
    text = _LABEL.sub("", text)
    text = _BULLET.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    flat = re.sub(r"\s*\n\s*", " ", text).strip()
    if len(flat) <= MAX_CHARS:
        return flat, False
    head = flat[:MAX_CHARS]
    ends = [m.end() for m in _SENTENCE_END.finditer(head)]
    if ends and ends[-1] >= MAX_CHARS // 2:
        head = head[:ends[-1]]
    else:
        head = head.rsplit(" ", 1)[0]
    return head.strip(), True


def spoken_text(raw: str, cfg, lang: str) -> Tuple[str, str]:
    """(the text to speak, why not). why is "" when there is text; "nothing" (no Hebrew left to say) or "blocked"
    (the radio's suitability rules refuse it, or cannot be read: nothing is said then)."""
    from . import radio, radio_room, radio_script, radio_suitability
    text, cut = clean_text(raw)
    if not text or not radio_script.HEBREW.search(text):
        return "", "nothing"
    try:
        rules = radio_room.suitability(radio.settings(cfg).get("room_suitability_file") or "")
    except radio_room.RoomError:
        return "", "blocked"
    out = radio_room.clean(text, MAX_CHARS * 2, rules)
    if not out or not radio_script.hebrew_enough(out, 0.6):
        return "", "blocked" if not out else "nothing"
    if not radio_suitability.suitable(out):  # the shared checker, or the portable word list; fails closed
        return "", "blocked"
    if cut:
        out = out.rstrip(" ,;:-") + (" " if out.endswith((".", "!", "?")) else ". ") + t("listen_cut", lang=lang)
    return out, ""


# --- the voice ---------------------------------------------------------------------------------

def _ffmpeg() -> Optional[str]:
    import os
    return shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else None)


def engine_ready(cfg) -> bool:
    return bool(_ffmpeg() and shutil.which("say"))


def synthesize(cfg, text: str) -> bytes:
    """OGG/Opus bytes of `text`, or b"" when no voice engine could speak it."""
    from . import radio_voice
    from .creds import for_outside
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        return b""
    text = for_outside(text, getattr(cfg, "redact_outgoing", True))
    pcm = radio_voice.system_voice(text, FREE_VOICE)
    if not pcm:
        return b""
    with tempfile.TemporaryDirectory(prefix="ctb-listen-") as d:
        ogg = Path(d) / "v.ogg"
        cmd = [ffmpeg, "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(radio_voice.RATE), "-ac", "1",
               "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-ac", "1", str(ogg)]
        proc = subprocess.run(cmd, input=pcm, capture_output=True, timeout=120)
        if proc.returncode != 0 or not ogg.exists():
            return b""
        return ogg.read_bytes()


# --- the rate limit ----------------------------------------------------------------------------

def take_slot(now: Optional[float] = None) -> bool:
    """Count one generation in the last hour; False when PER_HOUR are already used."""
    now = time.time() if now is None else now
    with locked_map(_paths()[1], keep=4) as data:
        recent = [a for a in data.get("at", []) if isinstance(a, (int, float)) and now - 3600 < a <= now + 60]
        if len(recent) >= PER_HOUR:
            data["at"] = recent
            return False
        data["at"] = recent + [now]
    return True


# --- the tap -----------------------------------------------------------------------------------

def _toast(bot, q: dict, text: str) -> None:
    try:
        bot.call("answerCallbackQuery", {"callback_query_id": q.get("id") or "", "text": text})
    except TelegramError:
        pass


def _target(m: dict) -> Tuple[object, Optional[int], dict]:
    chat = (m.get("chat") or {}).get("id")
    thread = m.get("message_thread_id")
    reply = {"message_id": m.get("message_id"), "allow_sending_without_reply": True}
    return chat, thread, reply


GONE = ("wrong file identifier", "file not found", "wrong remote file", "file_id")


def _resend(bot, m: dict, file_id: str) -> str:
    """"ok", "gone" (Telegram definitely no longer knows the file) or "error" (anything else: keep the id)."""
    chat, thread, reply = _target(m)
    params = {"chat_id": chat, "voice": file_id, "reply_parameters": reply, "disable_notification": True}
    if thread:
        params["message_thread_id"] = thread
    try:
        bot.call("sendVoice", params)
        return "ok"
    except TelegramError as e:
        return "gone" if any(w in (getattr(e, "description", "") or str(e)).lower() for w in GONE) else "error"
    except OSError:
        return "error"


def _failed(bot, m: dict, lang: str) -> None:
    chat, thread, reply = _target(m)
    fields = {"chat_id": chat, "text": t("listen_failed", lang=lang), "disable_notification": True,
              "reply_parameters": reply}
    if thread:
        fields["message_thread_id"] = thread
    try:
        bot.call("sendMessage", fields)
    except (TelegramError, OSError):
        pass


def _generate(cfg, bot, m: dict, token: str, text: str, lang: str) -> None:
    chat, thread, reply = _target(m)
    try:
        try:
            payload = synthesize(cfg, text)
        except Exception:
            payload = b""
        if not payload:
            _failed(bot, m, lang)
            return
        fields = {"chat_id": str(chat), "disable_notification": "true", "reply_parameters": json.dumps(reply)}
        if thread:
            fields["message_thread_id"] = str(thread)
        result = bot.upload("sendVoice", fields, "voice", "listen.ogg", payload)
        file_id = ((result or {}).get("voice") or {}).get("file_id") if isinstance(result, dict) else None
        if isinstance(file_id, str) and file_id:
            with locked_map(_paths()[0], keep=KEEP) as data:
                if isinstance(data.get(token), dict):
                    data[token]["file_id"] = file_id
    except (TelegramError, OSError, subprocess.SubprocessError):
        pass
    finally:
        _busy.release()


def press(q: dict, cfg, bot, session: Optional[str], mine: bool, background: bool = True) -> str:
    """One tap on a listen button. Always answers the tap once; the recording itself is made in a thread."""
    lang = getattr(cfg, "ui_language", None) or get_lang()
    m = q.get("message") or {}
    data = q.get("data") or ""
    if not mine:
        _toast(bot, q, t("toast_listen_owner_only", lang=lang))
        return "ignored"
    token = data[3:]
    rec = read_map(_paths()[0]).get(token) if CALLBACK.fullmatch(data) else None
    if not isinstance(rec, dict) or not session or rec.get("session") != session or not rec.get("text"):
        _toast(bot, q, t("toast_listen_expired", lang=lang))
        return "ignored"
    text, why = spoken_text(rec["text"], cfg, lang)  # also before a recording is sent again
    if why:
        _toast(bot, q, t("toast_listen_blocked" if why == "blocked" else "toast_listen_nothing", lang=lang))
        return "ignored"
    file_id = rec.get("file_id")
    if isinstance(file_id, str) and file_id:
        _toast(bot, q, t("toast_listen_resent", lang=lang))
        job = lambda: _cached(cfg, bot, m, token, file_id, text, lang)  # noqa: E731
        _run(job, background)
        return "listen-cached"
    if not engine_ready(cfg):
        _toast(bot, q, t("toast_listen_engine", lang=lang))
        return "ignored"
    if not _start():
        _toast(bot, q, t("toast_listen_busy" if _busy.locked() else "toast_listen_limit", lang=lang))
        return "busy" if _busy.locked() else "ignored"
    _toast(bot, q, t("toast_listen_preparing", lang=lang))
    _run(lambda: _generate(cfg, bot, m, token, text, lang), background)
    return "listen"


def _run(job, background: bool) -> None:
    if background:
        threading.Thread(target=job, daemon=True).start()
    else:
        job()


def _start() -> bool:
    """Take the one-at-a-time lock and an hourly slot; the lock is released by _generate."""
    if not _busy.acquire(blocking=False):
        return False
    try:
        if take_slot():
            return True
    except BaseException:
        _busy.release()
        raise
    _busy.release()
    return False


def _cached(cfg, bot, m: dict, token: str, file_id: str, text: str, lang: str) -> None:
    """Send a made recording again. A transient error keeps the id; only "file gone" drops it and makes a new one."""
    outcome = _resend(bot, m, file_id)
    if outcome == "ok":
        return
    if outcome == "error":
        _failed(bot, m, lang)
        return
    with locked_map(_paths()[0], keep=KEEP) as saved:
        if isinstance(saved.get(token), dict):
            saved[token].pop("file_id", None)
    if engine_ready(cfg) and _start():
        _generate(cfg, bot, m, token, text, lang)
    else:
        _failed(bot, m, lang)


def with_button(markup: dict, token: str, lang: Optional[str]) -> dict:
    """A copy of an inline keyboard with the listen row last."""
    out = copy.deepcopy(markup)
    out["inline_keyboard"].append(button_row(token, lang))
    return out
