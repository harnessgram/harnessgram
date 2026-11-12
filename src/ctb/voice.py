"""Voice transcript approval.

A voice note is transcribed locally and the transcript is shown with send / edit / cancel buttons
before it reaches the session, because a mis-heard word can send a session in the wrong direction.
The drafts wait in a private file (0600); the setting can be switched off from the settings menu,
and car mode and the dispatcher skip the approval (nobody can tap buttons while driving).
"""
from __future__ import annotations

import json
import threading
import time
from typing import Optional

from .config import private_dir, state_dir, write_private

EDIT_WINDOW = 900  # seconds the next text of the topic counts as the corrected transcript
KEEP = 50
_lock = threading.Lock()


def _off_path():
    return state_dir() / "voice-confirm-off"


def confirm_on() -> bool:
    return not _off_path().exists()


def set_confirm(on: bool) -> None:
    if on:
        _off_path().unlink(missing_ok=True)
    else:
        write_private(_off_path(), "")


def _path():
    return state_dir() / "voice-drafts.json"


def load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(drafts: dict) -> None:
    private_dir(state_dir())
    write_private(_path(), json.dumps(dict(list(drafts.items())[-KEEP:]), ensure_ascii=False))  # mode 0600


def add(mid: int, draft: dict) -> None:
    with _lock:
        drafts = load()
        drafts[str(mid)] = dict(draft, at=time.time())
        _save(drafts)


def pop(mid) -> Optional[dict]:
    with _lock:
        drafts = load()
        found = drafts.pop(str(mid), None)
        if found is not None:
            _save(drafts)
        return found


def mark_editing(mid) -> Optional[dict]:
    with _lock:
        drafts = load()
        draft = drafts.get(str(mid))
        if draft is not None:
            draft["editing"] = time.time()
            _save(drafts)
        return draft


def _editing(drafts: dict, thread, chat) -> list:
    return [k for k, v in drafts.items() if v.get("editing") and v.get("thread") == thread
            and v.get("chat") == chat and time.time() - v["editing"] < EDIT_WINDOW]


def editing(thread, chat: Optional[int] = None) -> Optional[dict]:
    """The draft whose edit button the owner tapped in this topic (the latest), without consuming it: the next
    text is checked first, and only a correction accepted for delivery takes it (take_editing)."""
    with _lock:
        drafts = load()
        found = [drafts[k] for k in _editing(drafts, thread, chat)]
        return max(found, key=lambda v: v["editing"]) if found else None


def take_editing(thread, chat: Optional[int] = None) -> Optional[dict]:
    """The owner tapped the edit button of a draft in this topic: their next text is the corrected
    version. Consumes the draft and returns it (the latest one when there are several), None when no edit is
    pending. `chat`: the topic's project group (None = the home chat)."""
    with _lock:
        drafts = load()
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
