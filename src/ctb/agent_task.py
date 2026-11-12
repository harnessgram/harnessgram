"""Which task and model a background agent was given, found when it starts.

The agents card shows each agent's model and task, so the SubagentStart handler asks here. There is ONE source
and it is exact: Claude Code writes the agent's `.meta.json` with the `toolUseId` of the `Agent`/`Task` call that
spawned it. The task and model are taken from that call in the parent transcript (the meta file only confirms
the link, its other fields are never shown). No verified link means no answer: nothing is ever guessed, and the
finish report fills in the model and name as before.

Safety: the meta path is derived from the parent transcript's own directory
(<dir>/<session>/subagents/agent-<id>.meta.json), never from a path in the hook payload or in the meta file;
nothing on the way may be a symlink; every file must be a regular file; the meta file is capped and the parent
transcript is read through one fixed byte window computed once. The whole lookup runs inside one time budget
and never raises. Everything shown is cleaned with the alerts-channel filter, which also hides short labelled
secrets ("password: x")."""
from __future__ import annotations

import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Callable, Optional

from .agent_owner import AGENT_ID
from .creds import redact_channel

TAIL = 1_500_000  # bytes at the end of the parent transcript that are read (one window)
META_CAP = 64_000  # bytes: a larger meta file is not read
TASK_LIMIT = 60  # characters of a task shown on the card
MODEL_LIMIT = 40
BUDGET = 1.5  # seconds for the whole lookup (reads and parsing included)
POLL = 0.25
CALLS = ("Agent", "Task")
CHECK_EVERY = 64  # rows parsed between two looks at the clock
TOOL_USE_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")


class _Over(Exception):
    """The lookup's time budget is used up."""


def clean_label(text, limit: int) -> str:
    """One line, redacted for sending out (labelled short secrets included), at most `limit` characters."""
    line = " ".join(redact_channel(" ".join(str(text or "").split())).split())
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


def clean_task(text) -> str:
    return clean_label(text, TASK_LIMIT)


def model_label(model, effort="") -> str:
    """`opus` + `high` -> "Opus, high"; a full model id -> "Opus 4.8, high". Empty without a model."""
    model = " ".join(str(model or "").split())
    if not model:
        return ""
    found = re.match(r"claude-([a-z]+)-(\d+)-(\d+)", model)
    pretty = f"{found.group(1).title()} {found.group(2)}.{found.group(3)}" if found else model.title()
    effort = " ".join(str(effort or "").split())
    return clean_label(pretty + (", " + effort if effort else ""), MODEL_LIMIT)


def _read_regular(path: Path, cap: Optional[int] = None, tail: Optional[int] = None) -> Optional[bytes]:
    """The bytes of a regular file opened without following a symlink: the whole file (None when it is larger
    than `cap`), or the last `tail` bytes of the size seen once at open. None when it is anything else."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        size = info.st_size
        if cap is not None and size > cap:
            return None
        start = max(0, size - tail) if tail is not None else 0
        chunks, want = [], size - start
        while want > 0:
            got = os.pread(fd, want, start)
            if not got:
                break
            chunks.append(got)
            start += len(got)
            want -= len(got)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _meta_link(session: str, agent_id: str, main: Path) -> Optional[str]:
    """The toolUseId the agent's meta file names. "" while there is no meta file yet; None when the agent can
    never be tied to this session's own folder (a wrong path, a symlink, not a regular file, too large)."""
    root = main.parent.resolve()
    folder = root / session / "subagents"
    meta = folder / f"agent-{agent_id}.meta.json"
    for step in (root / session, folder, meta):
        if os.path.islink(step):
            return None
    if not os.path.lexists(meta):
        return ""
    if not meta.resolve().is_relative_to(root):
        return None
    raw = _read_regular(meta, cap=META_CAP)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return ""
    link = str(data.get("toolUseId") or "") if isinstance(data, dict) else ""
    return link if TOOL_USE_ID.fullmatch(link) else ""


def _call_by_id(main: Path, tool_id: str, deadline: float, clock: Callable[[], float]) -> Optional[dict]:
    """The Agent/Task tool call `tool_id` in the tail window of the parent transcript (the newest one wins)."""
    raw = _read_regular(main, tail=TAIL)
    if raw is None:
        return None
    found = None
    for n, line in enumerate(raw.decode("utf-8", "replace").splitlines()):
        if n % CHECK_EVERY == 0 and clock() >= deadline:
            raise _Over()
        if tool_id not in line or '"tool_use"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # the first line of the window is usually cut in half
        msg = row.get("message") if isinstance(row, dict) else None
        for block in (msg.get("content") if isinstance(msg, dict) else None) or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") == tool_id \
                    and block.get("name") in CALLS and isinstance(block.get("input"), dict):
                found = block["input"]
    return found


def _find(session: str, agent_id: str, transcript: str, deadline: float, clock: Callable[[], float]):
    """A spec when the link is verified; {} to try again shortly; None when waiting cannot help."""
    main = Path(transcript) if transcript else None
    if main is None or not session or not AGENT_ID.fullmatch(agent_id or "") or main.suffix != ".jsonl" \
            or main.stem != session or os.path.islink(main):
        return None
    link = _meta_link(session, agent_id, main)
    if link is None:
        return None
    if not link:
        return {}
    call = _call_by_id(main, link, deadline, clock)
    if call is None:
        return {}  # not in the parent transcript (yet): unverified, so nothing is shown
    return {"id": link, "task": clean_task(call.get("description")),
            "model": model_label(call.get("model"), call.get("effort"))}


def lookup(session: str, agent_id: str, transcript: str, budget: float = BUDGET,
           sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict:
    """{"id", "task", "model"} of the agent's verified spawning call, else {}. Waits for the meta file and the
    call to appear, but never past `budget` seconds in total. Never raises."""
    try:
        deadline = clock() + max(0.0, budget)
        while True:
            spec = _find(session, agent_id, transcript, deadline, clock)
            if spec is None:
                return {}
            if spec:
                return spec
            left = deadline - clock()
            if left <= 0:
                return {}
            sleep(min(POLL, left))
    except Exception:  # includes _Over and a failing clock or sleep: a lookup must never cost the card its line
        return {}
