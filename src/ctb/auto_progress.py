"""The bridge's own progress card for a working session whose agent made none (cards.sync_auto).

What it shows comes only from what is safe to read: the subjects and statuses of the session's task list
(Claude Code's TaskCreate / TaskUpdate calls, or its latest TodoWrite list) found in the transcript, how many
background tasks the session runs, and the topic's name as the title. No tool output and no file contents.

The transcript's last FIRST_READ bytes are read once (streamed; only lines that name a task tool are parsed), then
incrementally: each call reads only what was appended since the last one, and a file whose size and
modification time did not change is not opened at all, so a frequent /api/state stays cheap.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
from typing import Callable, Dict, Iterable, List, Optional, Tuple

FIRST_READ = 64 * 1024 * 1024  # at most this much of a transcript's end is scanned the first time it is seen
CHUNK = 1024 * 1024
MAX_TASKS = 200
SUBJECT_CHARS = 200
_CREATED = re.compile(r"Task #?(\w+)")
_BYTE_KEYS = (b'"TaskCreate"', b'"TaskUpdate"', b'"TodoWrite"')

_lock = threading.Lock()
_cache: Dict[str, dict] = {}


class _Tasks:
    """The task list as the transcript builds it up, one event at a time."""

    def __init__(self) -> None:
        self.tasks: Dict[str, dict] = {}  # id -> {"content", "status"}, in creation order
        self.todos: Optional[List[dict]] = None  # the latest TodoWrite list
        self.pending: Dict[str, str] = {}  # TaskCreate tool_use id -> subject, until its result names the id
        self.last = ""  # which of the two was written last: "tasks" or "todos"
        self.count = 0  # TaskCreate calls seen, the id a result that names none is given

    def _create(self, ident: str, subject: str) -> None:
        if len(self.tasks) < MAX_TASKS or ident in self.tasks:
            self.tasks[ident] = {"content": subject, "status": "pending"}
            self.last = "tasks"

    def event(self, ev: dict) -> None:
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            return
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                name = b.get("name")
                if name == "TaskCreate" and isinstance(inp.get("subject"), str):
                    self.count += 1
                    self.pending[str(b.get("id"))] = inp["subject"][:SUBJECT_CHARS]
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
                        task["content"] = inp["subject"][:SUBJECT_CHARS]
                    self.last = "tasks"
                elif name == "TodoWrite" and isinstance(inp.get("todos"), list):
                    self.todos = [{"content": str(t.get("content"))[:SUBJECT_CHARS], "status": t.get("status")}
                                  for t in inp["todos"][:MAX_TASKS] if isinstance(t, dict)]
                    self.last = "todos"
            elif b.get("type") == "tool_result" and str(b.get("tool_use_id")) in self.pending:
                subject = self.pending.pop(str(b.get("tool_use_id")))
                if b.get("is_error"):
                    continue
                body = b.get("content")
                text = body if isinstance(body, str) else json.dumps(body)
                found = _CREATED.search(text or "")
                self._create(found.group(1) if found else str(self.count), subject)

    def items(self) -> List[dict]:
        if self.last == "todos":
            return list(self.todos or [])
        return [dict(t) for t in self.tasks.values()]


def _stamp(path: str) -> Optional[Tuple[int, int, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns


def _wanted(line: bytes, tasks: "_Tasks") -> bool:
    """Cheap byte test before a line is parsed: a task call, or the result of a TaskCreate still waiting."""
    if any(k in line for k in _BYTE_KEYS):
        return True
    return bool(tasks.pending) and b'"tool_result"' in line and any(i.encode() in line for i in tasks.pending)


def task_list(path: Optional[str]) -> List[dict]:
    """[{"content": subject, "status": pending|in_progress|completed}] of the session's task list, from its
    transcript; [] when there is none or the file cannot be read. The first call reads the last FIRST_READ bytes
    once (streamed, only matching lines parsed); a task created before them is simply not shown (its later
    updates find nothing to change). Later calls read only what was appended. A file whose size and mtime did
    not change is not opened. The shared cache lock is held only to look up and to store, never while reading."""
    if not path:
        return []
    stamp = _stamp(path)
    if stamp is None:
        return []
    with _lock:
        entry = _cache.get(path)
        if entry and entry.get("stamp") == stamp:
            return entry["tasks"].items()
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
                    if not _wanted(line, tasks):
                        continue
                    try:
                        ev = json.loads(line.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if isinstance(ev, dict):
                        tasks.event(ev)
    except OSError:
        return []
    entry["offset"] = offset
    entry["stamp"] = stamp if not carry else None  # a partial last line: read again, whole, next time
    with _lock:
        held = _cache.get(path)
        if not held or held.get("ino") != entry["ino"] or held["offset"] <= entry["offset"]:
            _cache[path] = entry  # never replaced by an older scan that finished later
    return tasks.items()


def cached(path: Optional[str], key: str, read: Callable[[str], object], default=None):
    """`read(path)` again only when the transcript's size or mtime changed since the last call with this key."""
    if not path:
        return default
    stamp = _stamp(path)
    if stamp is None:
        return default
    with _lock:
        entry = _cache.get(key + "\0" + path)
        if entry and entry["stamp"] == stamp:
            return entry["value"]
    value = read(path)
    with _lock:
        _cache[key + "\0" + path] = {"stamp": stamp, "value": value}
    return value


def forget(keep: Iterable[str]) -> None:
    """Drop cached transcripts that are no longer asked about."""
    keep = set(keep)
    with _lock:
        for k in [k for k in _cache if k.rpartition("\0")[2] not in keep]:
            del _cache[k]


def sync(rows: Iterable[dict], now: float) -> None:
    """Keep each session's automatic card in line with its state. `rows` holds one dict per live session:
    {"session", "chat", "thread", "title", "path", "state", "bg"}. A session working (or running background
    tasks) gets its card made or updated; an idle or ended one loses it. Never raises."""
    from . import cards
    quiet = []
    for r in rows:
        try:
            active = r["state"] == "working" or (r["state"] == "idle" and r.get("bg", 0) > 0)
            if not active:
                quiet.append(r["session"])
                continue
            cards.sync_auto(r["session"], r["chat"], r["thread"], r["title"], task_list(r.get("path")),
                            bg=r.get("bg", 0), now=now)
        except Exception:
            continue
    try:
        cards.close_auto(quiet, now)
    except Exception:
        pass
