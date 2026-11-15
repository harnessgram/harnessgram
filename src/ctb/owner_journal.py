"""The owner's own messages that the bridge really handed to a session: a small private journal per session.

Recall (recall.py) lists the owner's recent messages after a compaction. It reads only this journal, never the
session's transcript: a transcript also holds files, edits, tool output and sub-agent text that anyone can make look
like an owner message. The journal is written by the bridge alone, at the moment Inbox.take hands queued Telegram
messages to the session, and only for the owner's own typed text and transcribed voice notes (never team members,
forwarded content, control-panel taps, button words, edits or re-sent copies).

Each line: {"at": epoch seconds, "key": the queue's message key, "text": redacted words, "voice": bool}.
`<state>/owner-journal/<session>.jsonl`, mode 0600, appended under a lock; kept to MAX_ENTRIES lines and MAX_AGE.
A message put back by the sweeper and handed over again keeps its key and is recorded once.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import List, Optional

from .config import private_dir, state_dir, write_private

MAX_ENTRIES = 50
MAX_AGE = 14 * 86400
TEXT_CHARS = 500
FILE_BYTES = 256 * 1024
_SAFE = re.compile(r"[A-Za-z0-9._-]{1,120}")
_NOT_OWN = ("forwarded", "button", "edit", "replays", "redelivered")


def _paths(session: str):
    if not isinstance(session, str) or not _SAFE.fullmatch(session):
        return None
    root = state_dir() / "owner-journal"
    return root, root / (session + ".jsonl"), root / (session + ".lock")


def own_words(item) -> Optional[dict]:
    """The journal line for one delivered queue item, or None when it is not the owner's own typed or voice text."""
    if not isinstance(item, dict) or item.get("source") in ("team", "panel"):
        return None
    if any(item.get(k) for k in _NOT_OWN):
        return None
    voice = item.get("voice") is True
    if not voice and item.get("typed") is not True:
        return None
    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    from .creds import redact_channel
    from .work_lines import clean_text
    words = " ".join(redact_channel(clean_text(text)).split())[:TEXT_CHARS]
    return {"text": words, "voice": voice, "key": str(item.get("key") or "")[:40]} if words else None


def _load(path: Path) -> List[dict]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return []
    with os.fdopen(fd, "rb") as f:
        if os.fstat(f.fileno()).st_size > FILE_BYTES:
            return []
        raw = f.read(FILE_BYTES + 1)
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and isinstance(row.get("text"), str) and isinstance(row.get("at"), (int, float))
                and not isinstance(row.get("at"), bool)):
            out.append({"at": float(row["at"]), "text": row["text"][:TEXT_CHARS], "voice": row.get("voice") is True,
                        "key": str(row.get("key") or "")[:40]})
    return out


def record(session: str, items, now: Optional[float] = None) -> int:
    """Append the owner's own messages among `items` (just handed to the session); returns how many were added."""
    paths = _paths(session)
    rows = [r for r in (own_words(i) for i in items or []) if r]
    if paths is None or not rows:
        return 0
    root, path, lock = paths
    now = time.time() if now is None else now
    private_dir(root)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        kept = [r for r in _load(path) if 0 <= now - r["at"] <= MAX_AGE]
        seen = {r["key"] for r in kept if r["key"]}
        new = [dict(r, at=now) for r in rows if not r["key"] or r["key"] not in seen]
        if not new:
            return 0
        lines = kept + new
        if len(lines) > MAX_ENTRIES or len(kept) != len(_load(path)):
            write_private(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines[-MAX_ENTRIES:]))
        else:
            out = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(out, "a", encoding="utf-8") as f:
                f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in new))
            os.chmod(path, 0o600)
        return len(new)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read(session: str, since: float = 0.0, now: Optional[float] = None) -> List[dict]:
    """The session's journal lines newer than `since` and inside MAX_AGE, oldest first; [] on any problem."""
    paths = _paths(session)
    if paths is None:
        return []
    now = time.time() if now is None else now
    return [r for r in _load(paths[1]) if r["at"] >= since and 0 <= now - r["at"] <= MAX_AGE]
