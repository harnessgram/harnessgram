"""Scheduled tasks: every run of one task shares one topic, and a task can be muted.

A Claude desktop scheduled task starts a NEW session for every run. Without this module every run opened a
topic of its own. Wrong silence is the worst outcome here, so whenever anything is unsure, the session is
treated as an ordinary one and mirrored normally.

* Proof. A session is a scheduled run only when the FIRST user row of its transcript, read whole, was written
  by the desktop scheduler (entrypoint "claude-desktop", origin kind "task-notification", subkind
  "scheduled-trigger") AND its text starts with a well-formed `<scheduled-task name="...">` tag. Never from
  a hook's prompt text, a later message, or a transcript that is unreadable or still being written. Nothing
  is cached: the answer comes from the transcript itself every time (one short read at its top).
* One topic per chat and task (scheduled-topics.json, key "<chat>|<task>"). Every run maps to it; older runs
  that still run keep mirroring into it, and typed text in it goes to the newest run (the "session" of the
  entry: latest wins). A run proven scheduled after it got an ordinary topic is remapped to the task topic;
  the earlier topic is left exactly as it is.
* Mute (scheduled-muted.json, its own file, never trimmed). `suppressed(session, kind, text)` is the one
  decision point: only the kinds in SUPPRESSED are held back for a muted task, and a reply or idle ring
  that any of the bridge's failure detectors matches still goes out. Everything else (permission requests,
  questions, limit notices, errors, lifecycle) goes out, and every local state update always runs.
* A scheduled task's topic is never auto-closed by the bridge (sessions_menu.close_after_end).
"""
from __future__ import annotations

import datetime
import json
import os
import re
from typing import Dict, Optional

from .config import locked_map, read_map, state_dir
from .telegram import TelegramError

TOPICS_FILE = "scheduled-topics.json"  # "<chat>|<task>" -> {"chat", "thread", "task", "session", "started"}
MUTED_FILE = "scheduled-muted.json"  # task -> true; owner preferences, never trimmed
KEEP = 500
HEAD_BYTES = 64 * 1024  # the scheduler's first user row sits at the very top of the transcript

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")  # a slug: no spaces, quotes, slashes or markup
_TAG = re.compile(r'\A\s*<scheduled-task\s+name="([^"<>\r\n]{1,64})"(?:\s+[A-Za-z_-]{1,32}="[^"<>\r\n]*")*\s*>')


def valid_name(name) -> bool:
    return isinstance(name, str) and bool(NAME.fullmatch(name))


def parse_task(text) -> str:
    """The task name when `text` starts with a well-formed scheduled-task tag, else ""."""
    if not isinstance(text, str):
        return ""
    m = _TAG.match(text)
    return m.group(1) if m and valid_name(m.group(1)) else ""


def _from_scheduler(row: dict) -> str:
    """The task name when this user row was written by the desktop scheduler, else ""."""
    origin = row.get("origin")
    if (row.get("entrypoint") != "claude-desktop" or row.get("isMeta") or row.get("isSidechain")
            or not isinstance(origin, dict) or origin.get("kind") != "task-notification"
            or origin.get("subkind") != "scheduled-trigger"):
        return ""
    msg = row.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return ""
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")
    return parse_task(content)


def decide(path: Optional[str]) -> Optional[str]:
    """The task name, "" (proven ordinary) or None (undecided: no complete first user row yet, or the
    transcript cannot be read). Reads line by line, at most HEAD_BYTES."""
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            budget = HEAD_BYTES
            while budget > 0:
                raw = f.readline(budget)
                if not raw.endswith(b"\n"):
                    return None if len(raw) < budget else ""  # a row still being written, or a huge head
                budget -= len(raw)
                if not raw.strip():
                    continue
                row = json.loads(raw)
                if not isinstance(row, dict):
                    return None
                if row.get("type") == "user":
                    return _from_scheduler(row)
    except (OSError, TypeError, ValueError):
        return None
    return ""  # a long head without a user row is no scheduled run


def task_of(session: Optional[str], transcript: Optional[str] = None) -> str:
    """The scheduled task this session runs; "" for an ordinary session and while it is undecided."""
    if not isinstance(session, str) or not session:
        return ""
    if not transcript:
        from .status import transcript_of
        transcript = transcript_of(session)
    return decide(transcript) or ""


def _path(name: str):
    return state_dir() / name


# --- the task's topic per chat ---------------------------------------------------------------------------

def _key(chat: int, task: str) -> str:
    return f"{chat}|{task}"


def topic_of(chat: int, task: str) -> Optional[int]:
    entry = read_map(_path(TOPICS_FILE)).get(_key(chat, task))
    thread = entry.get("thread") if isinstance(entry, dict) else None
    return thread if type(thread) is int else None


def started_at(session: str, transcript: Optional[str] = None) -> float:
    """When the run started: the timestamp of its transcript's first row, else the file's creation time;
    0 when neither is known."""
    if not transcript:
        from .status import transcript_of
        transcript = transcript_of(session)
    if not transcript:
        return 0.0
    try:
        with open(transcript, "rb") as f:
            stamp = json.loads(f.readline(HEAD_BYTES)).get("timestamp")
        return datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        st = os.stat(transcript)
        return float(getattr(st, "st_birthtime", st.st_ctime))
    except OSError:
        return 0.0


def remember_topic(task: str, chat: int, thread: int, session: str) -> None:
    """The task's topic in this chat, and its newest run: the one that STARTED last, whatever order the
    runs were detected in (typed text in the topic goes to it)."""
    if not valid_name(task) or type(chat) is not int or type(thread) is not int:
        return
    started = started_at(session)
    with locked_map(_path(TOPICS_FILE), KEEP) as data:
        old = data.pop(_key(chat, task), None)
        entry = {"chat": chat, "thread": thread, "task": task, "session": session, "started": started}
        if (isinstance(old, dict) and old.get("thread") == thread and old.get("session") != session
                and isinstance(old.get("started"), (int, float)) and old["started"] > started):
            entry.update(session=old.get("session"), started=old["started"])  # an older run joined late
        data[_key(chat, task)] = entry


def newest_run(chat: int, thread: int) -> Optional[str]:
    """The newest run of the task whose topic this is; None for any other topic."""
    for entry in read_map(_path(TOPICS_FILE)).values():
        if isinstance(entry, dict) and entry.get("chat") == chat and entry.get("thread") == thread:
            return entry.get("session") if isinstance(entry.get("session"), str) else None
    return None


def forget_topic(chat: int, thread: int) -> None:
    """The topic is gone: the task's next run in that chat opens a new one."""
    def hit(e):
        return isinstance(e, dict) and e.get("chat") == chat and e.get("thread") == thread
    if not any(hit(e) for e in read_map(_path(TOPICS_FILE)).values()):
        return
    with locked_map(_path(TOPICS_FILE), KEEP) as data:
        for key in [k for k, e in data.items() if hit(e)]:
            data.pop(key)


# --- mute --------------------------------------------------------------------------------------------------

def muted_tasks() -> Dict[str, bool]:
    return {k: True for k, v in read_map(_path(MUTED_FILE)).items() if valid_name(k) and v is True}


def is_muted(task: str) -> bool:
    return bool(task) and task in muted_tasks()


def set_muted(task: str, muted: bool) -> bool:
    """False for a name that is not a valid task name."""
    if not valid_name(task):
        return False
    with locked_map(_path(MUTED_FILE), keep=10 ** 6) as data:
        if muted:
            data[task] = True
        else:
            data.pop(task, None)
    return True


def tasks() -> Dict[str, dict]:
    """Every known task: {"muted", "topics": [(chat, thread)]} (for `harnessgram tasks list`)."""
    out: Dict[str, dict] = {name: {"muted": True, "topics": []} for name in muted_tasks()}
    for entry in read_map(_path(TOPICS_FILE)).values():
        if isinstance(entry, dict) and valid_name(entry.get("task")):
            out.setdefault(entry["task"], {"muted": False, "topics": []})["topics"].append(
                (entry.get("chat"), entry.get("thread")))
    return out


# The only kinds a muted task holds back; anything else always goes out.
SUPPRESSED = frozenset({"progress", "reply", "idle", "status", "typing", "rename", "radio"})


def _failure(session: str, text: str) -> bool:
    """A reply or idle ring that reports a failure, by the detectors the bridge already has: the plan-limit
    reply (accounts.QUOTA_HIT), Claude Code's own error rows (mirror.SCREEN_ERRORS, here on the text) and
    the error row on the session's screen (mirror.screen_error)."""
    from .accounts import QUOTA_HIT
    from .mirror import SCREEN_ERRORS, screen_error
    return bool(text and (QUOTA_HIT.search(text) or SCREEN_ERRORS.search(text))) or bool(screen_error(session))


def suppressed(session: Optional[str], kind: str, text: str = "") -> bool:
    """Hold back this send? Only a kind in SUPPRESSED of a muted task's run, and never a reply or idle ring
    that reports a failure. Any doubt or failure: False (it goes out)."""
    if kind not in SUPPRESSED:
        return False
    try:
        if not is_muted(task_of(session)):
            return False
        return not (kind in ("reply", "idle") and _failure(session, text))
    except Exception:
        return False


class SilentBot:
    """Stands in for the bot while a held-back update runs: every Telegram call fails like a refused send,
    so the local bookkeeping around it (state, ids still to delete later) runs unchanged."""

    def call(self, method, params=None, timeout=15):
        raise TelegramError("held back: muted scheduled task")


MAX_CALLBACK = 64  # Telegram's limit for callback_data, in bytes


def menu_rows(session: Optional[str], lang: Optional[str] = None) -> list:
    """The mute (or unmute) row of the More menu in a scheduled task's topic; [] elsewhere. The button
    carries the full session id, so a tap acts only on the run it was sent for."""
    from .i18n import t
    try:
        task = task_of(session) if session else ""
        op = "tunmute" if is_muted(task) else "tmute"
    except Exception:
        task = ""
    data = f"more:{op}:{session}" if task else ""
    if not task or len(data.encode()) > MAX_CALLBACK:
        return []
    return [[{"text": t(f"more_btn_task_{op[1:]}", lang=lang), "callback_data": data}]]
