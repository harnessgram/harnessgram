"""Voice transcript approval.

A voice note is transcribed locally and the transcript is shown with send / edit / cancel buttons
before it reaches the session, because a mis-heard word can send a session in the wrong direction.
The drafts wait in a private file (0600); the setting can be switched off from the settings menu.

Only two things skip the preview, and each one says so in the topic:
* the driving window (the "🚗 driving" button): voice notes go straight in for DRIVING_SECONDS, then the
  window closes by itself, so a forgotten switch never hides the preview for days;
* car mode, only when `voice_confirm_driving` is "send" (default "preview": car mode stays on until it is
  switched off, so on its own it no longer removes the preview).
A preview that Telegram refuses for a while (a flood limit, a network error) is retried, and only when it
never shows is the recording handed over without it, with a note.

Delivery is claimed (claim / finish / release) under a lock that holds across processes, so a draft is
delivered once even when two listeners overlap during a restart. A claim names its process, and that process
holds voice-claimers/<pid>.lock for as long as it runs: a claim is taken over only once that lock is free (the
process ended, or the pid now belongs to an unrelated process), never by time while its owner still delivers.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
import time
import uuid
from typing import Optional, Tuple

from .config import private_dir, state_dir, write_private

EDIT_WINDOW = 900  # seconds the next text of the topic counts as the corrected transcript
KEEP = 50
DRIVING_SECONDS = 3 * 3600  # the driving window closes by itself after this long
CAR_CHOICES = ("preview", "send")  # what car mode does to a voice note: still preview it, or send it directly
OUTCOMES = ("sent", "cancelled", "direct", "dropped")
_lock = threading.Lock()
_holder: Optional[tuple] = None  # (path, fd) of this process's claimer lock (voice-claimers/<pid>.lock), held while it runs


def _off_path():
    return state_dir() / "voice-confirm-off"


def confirm_on() -> bool:
    return not _off_path().exists()


def set_confirm(on: bool) -> None:
    if on:
        _off_path().unlink(missing_ok=True)
    else:
        write_private(_off_path(), "")


def _driving_path():
    return state_dir() / "voice-driving.json"


def driving_until(now: Optional[float] = None) -> float:
    """When the driving window closes (epoch seconds), 0 when it is closed. A value further away than one window
    (a clock that jumped back, a hand-edited file) counts as closed."""
    now = time.time() if now is None else now
    try:
        until = float(json.loads(_driving_path().read_text(encoding="utf-8")).get("until") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0
    return until if now < until <= now + DRIVING_SECONDS + 60 else 0.0


def driving_on(now: Optional[float] = None) -> bool:
    return driving_until(now) > 0


def set_driving(on: bool, now: Optional[float] = None) -> None:
    """Open the driving window for DRIVING_SECONDS from now, or close it."""
    if on:
        now = time.time() if now is None else now
        private_dir(state_dir())
        write_private(_driving_path(), json.dumps({"until": now + DRIVING_SECONDS}))
    else:
        _driving_path().unlink(missing_ok=True)


def _car_send_path():
    return state_dir() / "voice-car-send"


def car_choice() -> str:
    """The `voice_confirm_driving` setting: "send" when car mode skips the preview, "preview" (default) when not."""
    return "send" if _car_send_path().exists() else "preview"


def set_car_choice(choice: str) -> None:
    if choice not in CAR_CHOICES:
        raise ValueError("voice_confirm_driving is preview or send")
    if choice == "send":
        private_dir(state_dir())
        write_private(_car_send_path(), "")
    else:
        _car_send_path().unlink(missing_ok=True)


def skip_reason(car_mode: bool, now: Optional[float] = None) -> str:
    """Why a voice note goes in without its preview: "driving", "car", or "" when the preview is shown."""
    if driving_on(now):
        return "driving"
    if car_mode and car_choice() == "send":
        return "car"
    return ""


def _outcomes_path():
    return state_dir() / "voice-outcomes.json"


@contextlib.contextmanager
def _locked():
    """Every read-modify-write of the drafts (and their outcomes) under one lock that holds across processes: an
    fcntl lock on a file beside them, so two listeners overlapping during a restart, or an old process's retry and a
    new process's start-up sweep, can never both claim the same draft. The thread lock covers this process."""
    with _lock:
        private_dir(state_dir())
        fd = os.open(str(state_dir() / "voice-drafts.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def note_outcome(mid, outcome: str) -> None:
    """What happened to a draft (sent, cancelled, delivered directly without its preview, or dropped after failed
    tries), so a late or second tap can say so plainly instead of a vague "already handled"."""
    if outcome not in OUTCOMES:
        return
    with _locked():
        try:
            data = json.loads(_outcomes_path().read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        data.pop(str(mid), None)
        data[str(mid)] = {"outcome": outcome, "at": time.time()}
        try:
            write_private(_outcomes_path(), json.dumps(dict(list(data.items())[-KEEP:])))
        except OSError:
            pass


def outcome(mid) -> str:
    try:
        data = json.loads(_outcomes_path().read_text(encoding="utf-8"))
        found = data.get(str(mid)) if isinstance(data, dict) else None
        return found.get("outcome", "") if isinstance(found, dict) else ""
    except (OSError, ValueError):
        return ""


def _path():
    return state_dir() / "voice-drafts.json"


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load() -> dict:
    with _locked():
        return _load()


def _save(drafts: dict) -> None:
    write_private(_path(), json.dumps(dict(list(drafts.items())[-KEEP:]), ensure_ascii=False))  # atomic, mode 0600


def pid_alive(pid) -> bool:
    """Whether a process with this id runs. Not an int, or not above zero (0 would mean our own group): not alive."""
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _holder_path(pid: int):
    return state_dir() / "voice-claimers" / f"{pid}.lock"


def _hold_claimer_lock() -> None:
    """This process marks itself as a live claimer: it locks voice-claimers/<pid>.lock and keeps it until it exits
    (the kernel frees the lock then, also after a crash)."""
    global _holder
    path = _holder_path(os.getpid())
    if _holder is not None and _holder[0] == path:
        return
    private_dir(path.parent)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    if _holder is not None:  # the state folder moved (tests): the old marker is let go
        os.close(_holder[1])
    _holder = (path, fd)


def _claim_live(claim, now: Optional[float] = None) -> bool:
    """A claim holds while the process that made it runs: its claimer lock is still taken. Free (the process ended,
    or its pid was reused by a process that never claimed) means abandoned, and the draft may be claimed again.
    There is no time limit: a slow delivery is never taken over while its owner is alive."""
    if not isinstance(claim, dict) or type(claim.get("pid")) is not int:
        return False
    try:
        fd = os.open(str(_holder_path(claim["pid"])), os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True  # held: the claiming process still runs
    finally:
        os.close(fd)  # closing also drops the lock taken just now
    return False


def add(mid: int, draft: dict) -> None:
    with _locked():
        drafts = _load()
        drafts[str(mid)] = dict(draft, at=time.time())
        _save(drafts)


def pop(mid) -> Optional[dict]:
    with _locked():
        drafts = _load()
        found = drafts.pop(str(mid), None)
        if found is not None:
            _save(drafts)
        return found


def claim(mid, now: Optional[float] = None) -> Tuple[str, Optional[dict]]:
    """Claim a draft for delivery, atomically across processes: ("ok", draft with its "claim" {pid, at, token}),
    ("busy", None) while a claim by a running claimer process holds it, ("gone", None) when there is no such draft. The claimer
    then calls finish() (handled: the draft goes) or release() (nothing was delivered: it waits again)."""
    now = time.time() if now is None else now
    with _locked():
        drafts = _load()
        draft = drafts.get(str(mid))
        if not isinstance(draft, dict):
            return "gone", None
        if _claim_live(draft.get("claim")):
            return "busy", None
        _hold_claimer_lock()
        draft["claim"] = {"pid": os.getpid(), "at": now, "token": uuid.uuid4().hex}
        _save(drafts)
        return "ok", dict(draft)


def _holds(draft, token: str) -> bool:
    return isinstance(draft, dict) and isinstance(draft.get("claim"), dict) and draft["claim"].get("token") == token


def finish(mid, token: str) -> bool:
    """The claimed draft was handled: it is removed. False when the claim was lost (taken over after its lease)."""
    with _locked():
        drafts = _load()
        if not _holds(drafts.get(str(mid)), token):
            return False
        drafts.pop(str(mid))
        _save(drafts)
        return True


def release(mid, token: str, **changes) -> bool:
    """Nothing was delivered: the claim is dropped and the draft waits again, with `changes` applied (for example
    the count of failed delivery attempts)."""
    with _locked():
        drafts = _load()
        draft = drafts.get(str(mid))
        if not _holds(draft, token):
            return False
        draft.pop("claim", None)
        draft.update(changes)
        _save(drafts)
        return True


def mark_shown(mid) -> bool:
    """The draft's preview reached Telegram. False when the draft is gone (handled meanwhile)."""
    with _locked():
        drafts = _load()
        draft = drafts.get(str(mid))
        if draft is None:
            return False
        if draft.pop("preview", None) is not None:
            _save(drafts)
        return True


def unshown(older_than: float, now: Optional[float] = None) -> list:
    """(key, draft) for drafts whose preview never reached Telegram, waiting at least `older_than` seconds and not
    held by a live claim. A draft whose delivery failed waits here too, with its "attempts" count."""
    now = time.time() if now is None else now
    with _locked():
        return [(k, v) for k, v in _load().items() if isinstance(v, dict) and v.get("preview") == "pending"
                and now - float(v.get("at") or 0) >= older_than and not _claim_live(v.get("claim"))]


def mark_editing(mid) -> Optional[dict]:
    """The edit button: the draft waits for the corrected text. None when it is gone or being delivered."""
    with _locked():
        drafts = _load()
        draft = drafts.get(str(mid))
        if draft is None or _claim_live(draft.get("claim")):
            return None
        draft["editing"] = time.time()
        _save(drafts)
        return draft


def _editing(drafts: dict, thread, chat) -> list:
    return [k for k, v in drafts.items() if v.get("editing") and v.get("thread") == thread
            and v.get("chat") == chat and time.time() - v["editing"] < EDIT_WINDOW
            and not _claim_live(v.get("claim"))]


def editing(thread, chat: Optional[int] = None) -> Optional[dict]:
    """The draft whose edit button the owner tapped in this topic (the latest), without consuming it: the next
    text is checked first, and only a correction accepted for delivery takes it (take_editing)."""
    with _locked():
        drafts = _load()
        found = [drafts[k] for k in _editing(drafts, thread, chat)]
        return max(found, key=lambda v: v["editing"]) if found else None


def take_editing(thread, chat: Optional[int] = None) -> Optional[dict]:
    """The owner tapped the edit button of a draft in this topic: their next text is the corrected
    version. Consumes the draft and returns it (the latest one when there are several), None when no edit is
    pending. `chat`: the topic's project group (None = the home chat)."""
    with _locked():
        drafts = _load()
        hit = _editing(drafts, thread, chat)
        found = [drafts.pop(k) for k in hit]
        if hit:
            _save(drafts)
        return max(found, key=lambda v: v["editing"]) if found else None


def edit_pending(thread, chat: Optional[int] = None) -> bool:
    """take_editing as a yes/no."""
    return take_editing(thread, chat) is not None


def shown(text: str, label: str) -> str:
    """The transcript without the "(voice message, transcribed)" label."""
    return text.replace(label, "", 1).strip()
