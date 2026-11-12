"""Replay of the owner's messages that only got the plan-limit reply, after the session moved account.

The bridge hands a queued message to the session (Inbox.take) and forgets it. When that session was
still on an account that had just run out, Claude answered with "You've hit your 5-hour limit ..." and
the bridge moved it to another account; nothing ever delivered the message again (the session sat idle
until it was re-sent by hand).

So every taken owner message is remembered for a while in <state>/replay/<session>.json. The end of a
turn decides its fate:
* the turn ended on the limit text: a message is marked `limited` only when the transcript shows
  nothing from Claude after it but that reply (no tool use, no text): a "yes" that Claude already acted
  on must never come back as a new approval;
* the turn ended with a real answer, with no text, or was interrupted (Escape, Stop buttons skip the
  Stop hook): the messages are forgotten;
* a completed account move after a limit queues the `limited` ones again, exactly once, if the limit
  reply was seen minutes ago (inbox._queue_replay).
A replayed message travels the normal path (inbox queue, nudge, UserPromptSubmit framing), wrapped in
a note that it is a re-send and not a new approval (framing.py).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from datetime import datetime
from typing import List, Optional

from .config import private_dir, state_dir, write_private

KEEP = 5  # remembered messages per session
MAX_AGE = 6 * 3600  # seconds a taken message is remembered
LIMITED_AGE = 600  # seconds after the limit reply that a move still replays the message; older marks are dropped
MAX_REPLAYS = 2  # a message that keeps meeting a limit is not sent around forever


def _name(session: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session or "")[:120] or "_"


def _path(session: str):
    return state_dir() / "replay" / (_name(session) + ".json")


def _load(session: str) -> list:
    try:
        data = json.loads(_path(session).read_text(encoding="utf-8"))
        return [e for e in data if isinstance(e, dict) and isinstance(e.get("item"), dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _update(session: str, change) -> list:
    """Run change(entries) under the session's lock and save the result; returns what change returned."""
    root = private_dir(state_dir() / "replay")
    with open(root / (_name(session) + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        entries = _load(session)
        result = change(entries)
        if entries:
            write_private(_path(session), json.dumps(entries[-KEEP:], ensure_ascii=False))
        else:
            try:
                _path(session).unlink()
            except OSError:
                pass
        return result


def record(session: str, items: List[dict], now: Optional[float] = None) -> None:
    """Remember the owner messages a hook has just taken from the inbox."""
    now = time.time() if now is None else now
    fresh = [{"item": dict(i), "taken_at": now} for i in items
             if i.get("source") in ("owner", "panel") and isinstance(i.get("text"), str) and i["text"].strip()
             and not i.get("dropped") and i.get("button") != "stop"]
    if not session or not fresh:
        return

    def add(entries):
        entries[:] = [e for e in entries if now - float(e.get("taken_at") or 0) < MAX_AGE] + fresh

    _update(session, add)


def acted_after(transcript: str, since: float, until: Optional[float] = None) -> bool:
    """Claude did something after `since`: a tool call or text of its own (the plan-limit and other API
    error rows are not answers). `until` leaves out rows of a later turn. Unreadable means "yes": a
    message is replayed only when the transcript proves it got nothing."""
    from .accounts import QUOTA_HIT
    try:
        with open(transcript, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 3_000_000))
            tail = f.read()
        if size > 3_000_000:
            tail = tail.partition(b"\n")[2]
    except OSError:
        return True
    for raw in tail.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("type") != "assistant" or row.get("isApiErrorMessage") is True:
            continue
        content = (row.get("message") or {}).get("content")
        blocks = content if isinstance(content, list) else []
        real = any(isinstance(b, dict) and (b.get("type") == "tool_use" or (
            b.get("type") == "text" and (b.get("text") or "").strip() and not QUOTA_HIT.search(b.get("text") or "")))
            for b in blocks)
        if not real:
            continue
        try:
            at = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, TypeError):
            return True  # cannot place it in time: assume it came after
        if at > since and (until is None or at <= until + 2):
            return True
    return False


def mark_limited(session: str, transcript: Optional[str] = None, until: Optional[float] = None,
                 now: Optional[float] = None) -> int:
    """The turn ended on the limit text. A message that Claude acted on afterwards is forgotten; one
    that got nothing is marked. Without a transcript nothing is marked (nothing proves it got nothing)."""
    now = time.time() if now is None else now
    if not session or not transcript:
        return 0

    def mark(entries):
        n = 0
        for e in list(entries):
            if e.get("limited_at"):
                continue
            if acted_after(transcript, float(e.get("taken_at") or 0), until):
                entries.remove(e)
            else:
                e["limited_at"] = now
                n += 1
        return n

    return _update(session, mark)


def clear_answered(session: str, before: Optional[float] = None) -> int:
    """The turn ended with a real answer: the messages taken before it were answered. `before` is when
    the turn ended; a message taken after that belongs to the next turn and stays."""
    before = time.time() if before is None else before

    def drop(entries):
        keep = [e for e in entries if float(e.get("taken_at") or 0) > before]
        gone = len(entries) - len(keep)
        entries[:] = keep
        return gone

    return _update(session, drop) if session else 0


def clear_all(session: str) -> int:
    """The turn was interrupted (Escape, a Stop button): what it had been given is cancelled, never
    to be replayed. The Stop hook does not run after Escape, so every such path calls this."""
    return _update(session, lambda entries: (len(entries), entries.clear())[0]) if session else 0


def turn_ended(session: str, text: str, at: Optional[float] = None, transcript: Optional[str] = None) -> None:
    """A turn ended with `text` as its last reply: the limit text marks (see mark_limited), anything
    else, including no text at all, clears."""
    from .accounts import QUOTA_HIT
    text = (text or "").strip()
    try:
        if QUOTA_HIT.search(text):
            mark_limited(session, transcript, at)
        else:
            clear_answered(session, at)
    except OSError:
        pass


def take_limited(session: str, now: Optional[float] = None) -> List[dict]:
    """The messages that got only the limit reply, ready to be queued again. Taken under the lock, so a
    second move (or a second process) gets nothing: each message is replayed once per limit."""
    now = time.time() if now is None else now

    def take(entries):
        out, keep = [], []
        for e in entries:
            limited = e.get("limited_at")
            if not limited:
                keep.append(e)
            elif now - float(limited) < LIMITED_AGE and int(e["item"].get("replays") or 0) < MAX_REPLAYS:
                item = {k: v for k, v in e["item"].items() if k not in ("hold", "typed")}  # a copy is never typed
                item["replays"] = int(item.get("replays") or 0) + 1
                out.append(item)
        entries[:] = keep
        return out

    return _update(session, take) if session else []
