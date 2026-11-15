"""Deterministic, restart-safe voice digests of sanitized radio reply records.

Importance: a nonempty Summary other than 'nothing new'/an acknowledgement,
OR an explicit decision, finished job or error in the source reply. No model is
called by this module. Ambiguous in-flight sends are consumed after a crash:
Telegram sendVoice has no idempotency key, so avoiding repeats takes priority.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import logging
import os
import re
import subprocess
import threading
import time

from .config import Config, home_chat, private_dir, read_map, write_private

LEVELS = ("off", "important", "hourly", "every_3_hours")
INTERVALS = {"important": 3600, "hourly": 3600, "every_3_hours": 10800}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEK = 7 * 24 * 60
MAX_TEXT = 900
NEWS = re.compile(r"(?i)\b(?:decision|decided|finished|completed|failed|failure|error)\b|"
                  r"(?:החלטה|הוחלט|החלטתי|הסתיימ|הושלמ|הושלם|סיימתי|שגיאה|תקלה|נכשל)")
NO_NEWS = re.compile(r"(?i)^(?:nothing new|no (?:news|updates?|changes?)|still working(?: on it)?|"
                     r"work (?:continues|in progress)|אין (?:חדש|עדכון|שינוי)|ללא (?:עדכון|שינוי)|"
                     r"עדיין עובד|העבודה נמשכת)[.! ]*$")
LOG = logging.getLogger(__name__)
_WORKER = threading.Lock()


def important(reply: str) -> bool:
    from . import fmt, radio_talk
    body, summary = fmt.split_summary(reply)
    summary = " ".join((summary or "").split())
    return bool((summary and not NO_NEWS.fullmatch(summary) and not radio_talk.TRIVIAL.fullmatch(summary))
                or NEWS.search(body or ""))


def _endpoint(text):
    if not isinstance(text, str):
        raise ValueError("invalid quiet endpoint")
    m = re.fullmatch(r"(mon|tue|wed|thu|fri|sat|sun) ([0-2]\d):([0-5]\d)", text)
    if m and int(m[2]) < 24:
        return DAYS.index(m[1]) * 1440 + int(m[2]) * 60 + int(m[3])
    value = dt.datetime.fromisoformat(text)
    if value.tzinfo is not None:
        raise ValueError("quiet endpoints must be local")
    return value


def valid_windows(windows) -> bool:
    if not isinstance(windows, list):
        return False
    try:
        for window in windows:
            a, b = _endpoint(window["start"]), _endpoint(window["end"])
            if type(a) is not type(b) or a == b or (isinstance(a, dt.datetime) and b < a):
                return False
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
    return True


def quiet(cfg: Config, now: float) -> bool:
    """System local time, including DST; daily quiet plus configured weekly/holiday windows.
    Invalid windows fail closed, including configs constructed without the loader."""
    local = dt.datetime.fromtimestamp(now)
    if local.hour >= 23 or local.hour < 7:
        return True
    windows = cfg.voice_digest_quiet_windows
    if not valid_windows(windows):
        return True
    point = local.weekday() * 1440 + local.hour * 60 + local.minute
    for window in windows:
        a, b = _endpoint(window["start"]), _endpoint(window["end"])
        if isinstance(a, int):
            if (point - a) % WEEK < (b - a) % WEEK:
                return True
        elif a <= local < b:
            return True
    return False


def folder():
    from . import radio
    return private_dir(radio.folder() / "digest")


def enqueue(cfg: Config, key: str, record: dict) -> None:
    """Fan out the existing spoken record before the live station consumes it."""
    if cfg.voice_digest not in INTERVALS or not cfg.mirror:
        return
    from .radio_privacy import configured_identifiers, scrub
    write_private(private_dir(folder() / "pending") / f"{key}.json",
                  json.dumps(scrub(record, identifiers=configured_identifiers(cfg)), ensure_ascii=False))


def _save(state):
    write_private(folder() / "state.json", json.dumps(state, ensure_ascii=False))


def _finish(state, pending, now):
    # Persist the tombstone before deleting inputs. A crash during cleanup cannot replay them.
    state["last"] = now
    state["done"] = pending["ids"]
    state.pop("pending", None)
    _save(state)
    _cleanup(state)


def _cleanup(state):
    for key in state.get("done", []):
        if isinstance(key, str) and re.fullmatch(r"[\w.-]+", key):
            (folder() / "pending" / key).unlink(missing_ok=True)
    if state.pop("done", None) is not None:
        _save(state)


def audio(cfg: Config, text: str) -> bytes:
    """Use the radio's cached, budgeted TTS and convert its PCM to Telegram OGG/Opus."""
    from . import radio, radio_voice
    with radio._SPEAK_LOCK:
        pcm, _ = radio_voice.speak(text, cfg, radio.settings(cfg),
                                   private_dir(radio.folder() / "tts-cache"),
                                   radio_voice.paid_usage(radio.folder()))
    if not pcm:
        raise RuntimeError("no voice audio")
    result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "s16le", "-ar", str(radio_voice.RATE),
                             "-ac", "1", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1"],
                            input=pcm, capture_output=True, timeout=120)
    if result.returncode or not result.stdout.startswith(b"OggS"):
        raise RuntimeError("voice encoding failed")
    return result.stdout


def tick(cfg: Config, bot, now=None) -> bool:
    """One heartbeat round. A private nonblocking process lock serializes encoding and delivery."""
    live = now is None
    now = time.time() if live else now
    from . import policy
    from .config import config_path, load
    cfg = load() if config_path().exists() else cfg
    if not cfg.mirror or not policy.on() or cfg.voice_digest not in INTERVALS or quiet(cfg, now):
        return False
    path = folder() / "send.lock"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return _tick(cfg, bot, now, live)


def _tick(cfg, bot, now, live=False):
    from . import flood, policy, radio_talk
    from .radio_privacy import configured_identifiers, scrub
    from .radio_suitability import suitable
    from .telegram import FloodSkipped, TelegramError
    state = read_map(folder() / "state.json")
    _cleanup(state)
    pending = state.get("pending")
    if pending and pending.get("phase") == "sending":
        # The process died around sendVoice: the server may already have accepted it.
        _finish(state, pending, now)
        return False
    if not pending:
        records = []
        for path in sorted((folder() / "pending").glob("*.json")):
            record = read_map(path)
            if not record or record.get("trivial", radio_talk.trivial(record.get("text", ""))):
                path.unlink(missing_ok=True)
                continue
            if cfg.voice_digest == "important" and not record.get("important"):
                path.unlink(missing_ok=True)
                continue
            records.append((path.name, scrub(record, identifiers=configured_identifiers(cfg))))
        if not records:
            return False
        first = min(float(r.get("replied_at", now)) for _, r in records)
        if now - state.get("last", first) < INTERVALS[cfg.voice_digest]:
            return False
        batch, ids = [], []
        for key, record in records:
            # Keep overflowing items for the next digest rather than consuming unheard lines.
            if batch and len(" ".join(r["text"] for r in batch + [record])) + len(radio_talk.BULLETIN_INTRO) + 4 > MAX_TEXT:
                break
            batch.append(record)
            ids.append(key)
        text = scrub(radio_talk.bulletin(batch, max_chars=MAX_TEXT))
        if not text:
            return False
        pending = {"ids": ids, "text": text, "attempts": 0, "important": all(r.get("important") for r in batch),
                   "since": first, "level": cfg.voice_digest}
        state["pending"] = pending
        _save(state)
    if cfg.voice_digest == "important" and not pending.get("important"):
        _finish(state, pending, now)
        return False
    if (pending.get("level") != cfg.voice_digest
            and now - state.get("last", pending.get("since", now)) < INTERVALS[cfg.voice_digest]):
        return False
    pending["level"] = cfg.voice_digest
    if pending["attempts"] >= 2:
        _finish(state, pending, now)
        LOG.warning("Voice digest dropped after two failed attempts")
        return False
    pending["attempts"] += 1
    _save(state)
    started = time.monotonic()
    try:
        text = scrub(pending["text"], identifiers=configured_identifiers(cfg))
        if not suitable(text):
            _finish(state, pending, now)
            LOG.warning("Voice digest dropped by child-suitability gate")
            return False
        payload = audio(cfg, text)
        # Re-read the live switch/quiet windows immediately before content leaves the machine.
        from .config import load, config_path
        current = load() if config_path().exists() else cfg
        if (not policy.on() or not current.mirror or current.voice_digest not in INTERVALS
                or (current.voice_digest == "important" and not pending.get("important"))
                or (current.voice_digest != pending["level"]
                    and now - state.get("last", pending.get("since", now)) < INTERVALS[current.voice_digest])
                or quiet(current, now + time.monotonic() - started)):
            pending["attempts"] -= 1
            _save(state)
            return False
        pending["phase"] = "sending"
        _save(state)
        # No thread id routes to the forum's General topic, matching the radio's home destination.
        result = flood.optional_upload(bot, "sendVoice", {"chat_id": str(home_chat(current)), "caption": text},
                                       "voice", "digest.ogg", payload)
        if result is None or result is False:
            raise RuntimeError("voice send not accepted")
    except Exception as exc:
        if isinstance(exc, FloodSkipped):
            # No request was made. Preserve the batch without spending a retry.
            pending["attempts"] -= 1
            pending["phase"] = "retry"
            _save(state)
            return False
        rejected = (isinstance(exc, TelegramError) and exc.api_rejected
                    and isinstance(exc.code, int) and 400 <= exc.code < 500)
        if pending.get("phase") == "sending" and not rejected:
            # A timeout, missing response or server failure may follow acceptance.
            # Consume exactly like a crash in flight; retry only definite rejections.
            _finish(state, pending, now)
            LOG.warning("Voice digest delivery uncertain; consumed without replay")
            return False
        pending["phase"] = "retry"
        if pending["attempts"] >= 2:
            _finish(state, pending, now)
            LOG.warning("Voice digest dropped after two failed attempts")
        else:
            _save(state)
        return False
    state["message"] = result.get("message_id") if isinstance(result, dict) else None
    _finish(state, pending, time.time() if live else now)
    return True


def start(cfg: Config, bot, now=None) -> None:
    """The heartbeat remains responsive while one bounded worker makes/sends a digest."""
    from .config import config_path, load
    current = load() if config_path().exists() else cfg
    if (current.voice_digest not in INTERVALS or not current.mirror
            or not (folder() / "pending").exists() or not _WORKER.acquire(blocking=False)):
        return

    def work():
        try:
            tick(current, bot)
        except Exception:
            LOG.warning("Voice digest round failed")
        finally:
            _WORKER.release()
    try:
        threading.Thread(target=work, name="voice-digest", daemon=True).start()
    except BaseException:
        _WORKER.release()
        raise
