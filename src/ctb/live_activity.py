"""Small, private, throttled snapshots of tool activity; never store tool inputs."""
import fcntl
import hashlib
import json
import re
import time
from pathlib import PurePath

from .config import private_dir, read_map, state_dir, write_private
from .lazy import lazy_module

# Imported when first used: a throttled call (most of them) reads one small file and needs neither.
cards = lazy_module(".cards")


def redact_channel(text):
    from .creds import redact_channel as redact
    return redact(text)

INTERVAL = 10


def describe(data):
    tool = data.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return ""
    inp = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    if tool in ("Bash", "Agent", "Task"):
        text = inp.get("description")
        if not isinstance(text, str):
            text = ""
        # A mislabeled description must not copy the shell command into private state.
        command = inp.get("command")
        if isinstance(command, str) and command and command in text:
            text = ""
        # Free text written by the agent: anything the credential filter would touch is not shown at all.
        if text and redact_channel(text) != text:
            text = ""
        text = text or tool
    elif tool in ("Edit", "Write"):
        path = inp.get("file_path")
        name = PurePath(path.replace("\\", "/")).name if isinstance(path, str) else ""
        text = "editing " + name if name and name not in (".", "..") else "editing a file"
    else:
        text = re.sub(r"[_-]+", " ", tool)
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return cards.subject_line(text)


def _path(session):
    return state_dir() / "live-activity" / (hashlib.sha256(session.encode()).hexdigest() + ".json")


def read(session):
    row = read_map(_path(session))
    text, at = row.get("text"), row.get("at")
    if isinstance(text, str) and text and isinstance(at, (int, float)) and not isinstance(at, bool):
        return {"text": cards.subject_line(text), "at": float(at)}
    return {}


def _recent(session, now):
    """Whether a snapshot was written less than INTERVAL ago: what read() reports, without cleaning its text."""
    row = read_map(_path(session))
    text, at = row.get("text"), row.get("at")
    return (isinstance(text, str) and bool(text) and isinstance(at, (int, float)) and not isinstance(at, bool)
            and 0 <= now - float(at) < INTERVAL)


def record(session, data, now=None):
    if not isinstance(session, str) or not session:
        return False
    now = time.time() if now is None else now
    if _recent(session, now):  # too soon for another one: nothing to describe or clean
        return False
    text = describe(data)
    if not text:
        return False
    path = _path(session)
    private_dir(path.parent)
    with open(str(path) + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        if _recent(session, now):
            return False
        write_private(path, json.dumps({"text": text, "at": now}, ensure_ascii=False))
    return True
