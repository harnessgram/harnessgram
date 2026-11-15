"""Anonymous listening signals. Readers never contact Telegram or hold viewer data."""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import time
from pathlib import Path
from typing import Callable, Optional

from .config import write_private

PLAYER_FRESH = 90
CALL_EVERY = 60
CALL_FRESH = 90


def call_enabled(settings: dict) -> bool:
    return all(settings.get(k) for k in ("telethon_python", "account_api_command", "account_session_command"))


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _fresh(at, now: float, ttl: int) -> bool:
    return type(at) in (int, float) and math.isfinite(at) and 0 <= now - at <= ttl


PRESENCE_LOCK_WAIT = 10.0


@contextlib.contextmanager
def presence_lock(directory: Path, wait: float = PRESENCE_LOCK_WAIT):
    """The lock that makes "record a listener fetch" and "decide the idle stop of a Play-started station" one step
    each (radio_play.py): a fetch is either recorded before the decision looks, or waits until the stop is over.
    Waits at most `wait` seconds; the body runs either way (a signal must never break playback) and gets whether the
    lock was held."""
    handle, held = None, False
    try:
        handle = open(directory / "player-listener.lock", "a")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
    except OSError:
        pass
    try:
        yield held
    finally:
        if handle is not None:
            if held:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()


def player_fetch(directory: Path, now: Optional[float] = None) -> None:
    """Called only after an authenticated media response; store just its time, never the URL/token/IP."""
    try:
        with presence_lock(directory):
            write_private(directory / "player-listener.json", json.dumps({"at": time.time() if now is None else now}))
    except OSError:
        pass  # a failed signal write must not break playback


def poll_call(directory: Path, fetch: Callable[[], dict], now: Optional[float] = None) -> None:
    """Worker-only, at most once per minute. Failures replace earlier positive evidence with unknown."""
    now = time.time() if now is None else now
    old = _read(directory / "call-listener.json")
    if _fresh(old.get("at"), now, CALL_EVERY) and now - old["at"] < CALL_EVERY:
        return
    # Invalidate the previous sample before a possibly slow request. Its timestamp is the request start,
    # so a hung/late response cannot renew a positive signal from minutes ago.
    sample = {"at": now, "participants": None}
    try:
        write_private(directory / "call-listener.json", json.dumps(sample))
        result = fetch()
        call = result.get("call") or {}
        count = call.get("participants") if isinstance(call, dict) else None
        if result.get("ok") is True:
            if result.get("active") is False:
                sample["participants"] = 0
            elif result.get("active") is True and type(count) is int and count >= 0:
                sample["participants"] = count
    except Exception:
        pass  # never serialize a helper error, call identity, credentials or viewer details
    try:
        write_private(directory / "call-listener.json", json.dumps(sample))
    except OSError:
        pass


def snapshot(directory: Path, settings: dict, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    # with the group stream off there is no call to count listeners in
    player_on, call_on = bool(settings.get("radio_player")), call_enabled(settings) and settings.get("group_live", True)
    player = player_on and _fresh(_read(directory / "player-listener.json").get("at"), now, PLAYER_FRESH)
    call = _read(directory / "call-listener.json") if call_on else {}
    count = call.get("participants")
    known = _fresh(call.get("at"), now, CALL_FRESH) and type(count) is int and count >= 0
    sources = (["player"] if player else []) + (["call"] if known and count > 0 else [])
    available = player_on or call_on
    source = "+".join(sources) or ("unavailable" if not available else "unknown" if call_on and not known else "none")
    return {"listening": bool(sources), "listening_source": source, "listening_detection": available}


def voice_settings(settings: dict, listening: dict) -> dict:
    """Choose once per bulletin, keeping the legacy cap only for installations without detection."""
    out = dict(settings, tts_hard_cap=True)
    if listening["listening_detection"]:
        out["tts_daily_minutes"] = max(0, settings.get("tts_listening_minutes", 60)) if listening["listening"] else 0
        out["tts_force_system"] = not listening["listening"]
    return out
