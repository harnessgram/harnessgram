"""One shared view of Telegram's flood limit for every process that sends (listener, hooks, heartbeat,
dashboard, progress workers).

All session topics live in ONE supergroup, so they share its limit of about 20 messages a minute.
This module keeps two facts in a private state file, guarded by a file lock so separate processes agree:

* the gate: when any call gets a 429, `until = now + retry_after` for that chat;
* the budget: timestamps of the optional sends and edits of the last minute, per chat.

Sends have a priority. Essential ones (the owner's replies, permission and question prompts, delivery
receipts, answers to commands) never look at this file. Optional ones (progress cards, the working
placeholder, status-card refreshes, reactions, typing) go through `optional_call`: while the gate is
closed, or the minute's budget is spent, they are skipped at once without a network call and the
caller treats that as an ordinary failed send that it retries on its next tick.
"""
from __future__ import annotations

import contextlib
import contextvars
import fcntl
import json
import time
from typing import Optional

from .config import private_dir, state_dir, write_private

STATE_FILE = "flood.json"
OPTIONAL_PER_MIN = 12   # optional sends and edits per chat per WINDOW, shared by all processes
WINDOW = 60.0
DEFAULT_PAUSE = 5.0     # a 429 without retry_after
MAX_PAUSE = 300.0       # never trust an absurd retry_after for longer than this

_OPTIONAL = contextvars.ContextVar("ctb_optional_call", default=False)


def optional_active() -> bool:
    """True inside `optional_call`: the HTTP layer then fails a 429 at once instead of waiting."""
    return _OPTIONAL.get()


def _key(chat) -> str:
    return str(chat if chat is not None else 0)


@contextlib.contextmanager
def _locked():
    base = private_dir(state_dir())
    with open(base / (STATE_FILE + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield base / STATE_FILE


def _num(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _load(path) -> dict:
    """The state, with anything malformed dropped (a hand-edited or half-written file must never
    break a send)."""
    out = {"until": {}, "sent": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    if isinstance(data.get("until"), dict):
        for key, value in data["until"].items():
            number = _num(value)
            if number is not None:
                out["until"][str(key)] = number
    if isinstance(data.get("sent"), dict):
        for key, values in data["sent"].items():
            if isinstance(values, list):
                out["sent"][str(key)] = [n for n in map(_num, values) if n is not None]
    return out


def _until(data: dict, key: str, now: float) -> float:
    """When the gate for `key` opens; 0 when open. A stamp further ahead than any real pause comes from
    a clock that jumped back, and counts as open."""
    until = data["until"].get(key, 0.0)
    return until if now < until <= now + MAX_PAUSE else 0.0


def record_429(chat, retry_after=None, now: Optional[float] = None) -> None:
    """Close the gate for `chat` until Telegram's retry_after has passed (only ever extends it)."""
    try:
        pause = float(retry_after) if retry_after is not None else DEFAULT_PAUSE
    except (TypeError, ValueError):
        pause = DEFAULT_PAUSE
    pause = min(max(pause, 1.0), MAX_PAUSE)
    now = time.time() if now is None else now
    try:
        with _locked() as path:
            data = _load(path)
            key = _key(chat)
            data["until"][key] = max(_until(data, key, now), now + pause)
            data["until"] = {k: v for k, v in data["until"].items() if now < v <= now + MAX_PAUSE}
            write_private(path, json.dumps(data))
    except Exception:
        pass  # the gate is a courtesy; never fail a send because it cannot be written


def remaining(chat, now: Optional[float] = None) -> float:
    """Seconds until the gate for `chat` opens (0 when it is open)."""
    now = time.time() if now is None else now
    try:
        with _locked() as path:
            return max(0.0, _until(_load(path), _key(chat), now) - now)
    except Exception:
        return 0.0


def is_open(chat, now: Optional[float] = None) -> bool:
    return remaining(chat, now) <= 0


def allow_optional(chat, units: int = 1, budget: bool = True, now: Optional[float] = None) -> bool:
    """Reserve `units` optional calls for `chat`. False when the gate is closed or, with `budget`,
    when the last minute already holds OPTIONAL_PER_MIN of them (in any process)."""
    now = time.time() if now is None else now
    try:
        with _locked() as path:
            data = _load(path)
            key = _key(chat)
            if _until(data, key, now) > now:
                return False
            if not budget:
                return True
            # entries from the future (a clock that jumped back) would block the chat for hours: ignore them
            recent = [x for x in data["sent"].get(key, []) if -1.0 <= now - x < WINDOW]
            if len(recent) + units > OPTIONAL_PER_MIN:
                data["sent"][key] = recent
                write_private(path, json.dumps(data))
                return False
            data["sent"][key] = recent + [now] * units
            data["sent"] = {k: v for k, v in data["sent"].items() if v}
            write_private(path, json.dumps(data))
            return True
    except Exception:
        return True  # an unreadable state file must not silence the bridge


def optional_call(bot, method: str, params: Optional[dict] = None, budget: bool = True, **kwargs):
    """`bot.call(...)` for non-essential traffic. Raises a TelegramError (code 429, `.skipped`)
    without any network call while the gate is closed or the budget is spent; a real 429 inside is
    not waited out, it just closes the gate for everyone."""
    chat = (params or {}).get("chat_id")
    if not allow_optional(chat, budget=budget):
        from .telegram import FloodSkipped
        raise FloodSkipped("flood gate closed", remaining(chat))
    token = _OPTIONAL.set(True)
    try:
        return bot.call(method, params, **kwargs)
    finally:
        _OPTIONAL.reset(token)


def optional_upload(bot, method: str, fields: dict, file_field: str, filename: str, payload: bytes, **kwargs):
    """`bot.upload(...)` for non-essential traffic: the same gate and budget as `optional_call`."""
    chat = (fields or {}).get("chat_id")
    if not allow_optional(chat):
        from .telegram import FloodSkipped
        raise FloodSkipped("flood gate closed", remaining(chat))
    token = _OPTIONAL.set(True)
    try:
        return bot.upload(method, fields, file_field, filename, payload, **kwargs)
    finally:
        _OPTIONAL.reset(token)
