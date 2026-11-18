"""The bridge's own progress card for a working session whose agent made none (cards.sync_auto).

The incremental reader retains only task subjects/statuses and Bash descriptions. No assistant prose,
commands, tool names, tool output, thinking or file contents reach the activity snapshot. Task identity is
derived from TaskCreate call order; tool results are never read. auto_cards owns summaries and scheduling.

The transcript's last FIRST_READ bytes are read once (streamed; only matching bounded records are parsed), then
incrementally: each call reads only what was appended since the last one, and a file whose size and
modification time did not change is not opened at all, so a frequent /api/state stays cheap.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

FIRST_READ = 64 * 1024 * 1024  # at most this much of a transcript's end is scanned the first time it is seen
CHUNK = 1024 * 1024
MAX_TASKS = 200
SUBJECT_CHARS = 200
MAX_ACTIVITIES = 40
MAX_LINE = 64 * 1024
_BYTE_KEYS = (b'"TaskCreate"', b'"TaskUpdate"', b'"TodoWrite"')
_CONTROLS = re.compile(r"[\x00-\x1f\x7f\u200e\u200f\u202a-\u202e\u2066-\u2069]")

_lock = threading.Lock()
_cache: Dict[str, dict] = {}


def _short(value, limit: int = SUBJECT_CHARS) -> str:
    """Only short metadata, scrubbed BEFORE any cut; never commands or result bodies."""
    if not isinstance(value, str) or len(value) > 512:
        return ""
    from .cards import scrub_subject
    return scrub_subject(_CONTROLS.sub("", value))[:limit]


class _Tasks:
    """The task list as the transcript builds it up, one event at a time."""

    def __init__(self) -> None:
        self.tasks: Dict[str, dict] = {}  # id -> {"content", "status"}, in creation order
        self.todos: Optional[List[dict]] = None  # the latest TodoWrite list
        self.last = ""  # which of the two was written last: "tasks" or "todos"
        self.count = 0  # TaskCreate calls seen; Claude assigns their numeric IDs in this order
        self.activities: List[dict] = []
        self.revision = 0

    def activity(self, kind: str, text: str) -> None:
        text = _short(text)
        if text:
            self.activities.append({"kind": kind, "text": text})
            self.activities = self.activities[-MAX_ACTIVITIES:]
            self.revision += 1

    def _create(self, ident: str, subject: str) -> None:
        if len(self.tasks) < MAX_TASKS or ident in self.tasks:
            self.tasks[ident] = {"content": subject, "status": "pending"}
            self.last = "tasks"

    def event(self, ev: dict) -> None:
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        assistant = isinstance(msg, dict) and msg.get("role", ev.get("type")) == "assistant"
        if not isinstance(content, list):
            return
        for b in content:
            if not isinstance(b, dict):
                continue
            if assistant and b.get("type") == "tool_use":
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                name = b.get("name")
                if name == "Bash":
                    self.activity("description", inp.get("description"))
                if name in ("TaskCreate", "TaskUpdate"):
                    self.activity("subject", inp.get("subject"))
                if name == "TaskCreate" and isinstance(inp.get("subject"), str):
                    self.count += 1
                    self._create(str(self.count), _short(inp["subject"]))
                elif name == "TaskUpdate" and inp.get("taskId") is not None:
                    task = self.tasks.get(str(inp["taskId"]))
                    if task is None:
                        continue
                    status = inp.get("status")
                    if status == "deleted":
                        del self.tasks[str(inp["taskId"])]
                    elif status in ("pending", "in_progress", "completed"):
                        task["status"] = status
                    if isinstance(inp.get("subject"), str) and inp["subject"].strip():
                        task["content"] = _short(inp["subject"])
                    self.last = "tasks"
                elif name == "TodoWrite" and isinstance(inp.get("todos"), list):
                    self.todos = [{"content": _short(t.get("content")), "status": t.get("status")}
                                  for t in inp["todos"][:MAX_TASKS] if isinstance(t, dict)]
                    for t in self.todos:
                        self.activity("subject", t["content"])
                    self.last = "todos"

    def items(self) -> List[dict]:
        if self.last == "todos":
            return [dict(t) for t in self.todos or []]
        return [dict(t) for t in self.tasks.values()]


def _stamp(path: str) -> Optional[Tuple[int, int, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns


def _wanted(line: bytes, tasks: "_Tasks") -> bool:
    """Cheap byte test for allowed tool-use inputs; tool results and prose are never parsed."""
    return b'"Bash"' in line or any(k in line for k in _BYTE_KEYS)


def _scan(path: Optional[str]) -> _Tasks:
    """[{"content": subject, "status": pending|in_progress|completed}] of the session's task list, from its
    transcript; [] when there is none or the file cannot be read. The first call reads the last FIRST_READ bytes
    once (streamed, only matching lines parsed); a task created before them is simply not shown (its later
    updates find nothing to change). Later calls read only what was appended. A file whose size and mtime did
    not change is not opened. The shared cache lock is held only to look up and to store, never while reading."""
    if not path:
        return _Tasks()
    stamp = _stamp(path)
    if stamp is None:
        return _Tasks()
    with _lock:
        entry = _cache.get(path)
        if entry and entry.get("stamp") == stamp:
            return entry["tasks"]
        if not entry or entry.get("ino") != stamp[0] or stamp[1] < entry["offset"]:
            start = max(0, stamp[1] - FIRST_READ)
            entry = {"ino": stamp[0], "stamp": None, "offset": start, "tasks": _Tasks(), "cut": start > 0}
        else:
            entry = copy.deepcopy(entry)  # scanned outside the lock: the cached one stays as it is meanwhile
    # The scan runs without the shared lock, so one long first read never holds up the other sessions' reads.
    tasks, carry, offset = entry["tasks"], b"", entry["offset"]
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            left = stamp[1] - offset
            while left > 0:
                chunk = f.read(min(CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
                data = carry + chunk
                lines = data.split(b"\n")
                carry = lines.pop()  # a line not yet ended: kept for the next chunk or the next call
                if entry["cut"] and lines:  # the first line of a read from the middle is partial
                    offset += len(lines.pop(0)) + 1
                    entry["cut"] = False
                for line in lines:
                    offset += len(line) + 1
                    if len(line) > MAX_LINE or not _wanted(line, tasks):
                        continue
                    try:
                        ev = json.loads(line.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if isinstance(ev, dict):
                        tasks.event(ev)
                if len(carry) > MAX_LINE:
                    offset += len(carry)
                    carry = b""
                    entry["cut"] = True  # discard an oversized partial record through its next newline
    except OSError:
        return _Tasks()
    entry["offset"] = offset
    entry["stamp"] = stamp if not carry else None  # a partial last line: read again, whole, next time
    with _lock:
        held = _cache.get(path)
        if not held or held.get("ino") != entry["ino"] or held["offset"] <= entry["offset"]:
            _cache[path] = entry  # never replaced by an older scan that finished later
    return tasks


def task_list(path: Optional[str]) -> List[dict]:
    return _scan(path).items()


def activity_snapshot(path: Optional[str]) -> dict:
    """Bounded safe metadata, sharing the task reader's incremental offset and file stamp."""
    found = _scan(path)
    return {"tasks": found.items(), "activities": copy.deepcopy(found.activities), "revision": found.revision}


def cached(path: Optional[str], key: str, read: Callable[[str], object], default=None,
           now: Optional[float] = None, max_age: Optional[float] = None):
    """`read(path)` again only when the transcript's size or mtime changed since the last call with this key."""
    if not path:
        return default
    stamp = _stamp(path)
    if stamp is None:
        return default
    now = time.monotonic() if now is None else now
    with _lock:
        entry = _cache.get(key + "\0" + path)
        if (entry and entry["stamp"] == stamp
                and (max_age is None or 0 <= now - entry.get("read_at", 0) < max_age)):
            return entry["value"]
    value = read(path)
    with _lock:
        _cache[key + "\0" + path] = {"stamp": stamp, "value": value, "read_at": now}
    return value


def forget(keep: Iterable[str]) -> None:
    """Drop cached transcripts that are no longer asked about."""
    keep = set(keep)
    with _lock:
        for k in [k for k in _cache if k.rpartition("\0")[2] not in keep]:
            del _cache[k]


def sync(rows: Iterable[dict], now: float, cfg=None) -> None:
    """Keep each session's automatic card in line with its state. `rows` holds one dict per live session:
    {"session", "chat", "thread", "title", "path", "state", "bg"}. A session working (or running background
    tasks) gets its card made or updated; an idle or ended one loses it. Never raises."""
    from .auto_cards import watcher
    watcher.sync(rows, now, initial_only=True, cfg=cfg)  # the heartbeat owns translated title updates
