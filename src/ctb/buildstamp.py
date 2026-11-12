"""Which Harnessgram code is installed, and which code each long-running service started with.

Read-only by design: nothing here restarts, replaces or stops a process. `harnessgram install` restarts the
services it manages; `harnessgram doctor` names any service that still runs older code.

The stamp of an ordinary install (uv tool, pipx, pip) is the SHA-256 of the distribution's RECORD, the
installer's list of every installed file with its own hash: it changes exactly when the installed code
changes, not on a reinstall of the same code, and it does not depend on file times or inodes (uv links files
from its cache). When that RECORD is expected but missing, or two copies are present (the package is being
replaced), the stamp is unknown (None). A source checkout has no RECORD of its files, so its stamp is taken
from the names, sizes and times of its source files. An unknown stamp never makes a warning.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from .config import private_dir, state_dir, write_private

DIST = "harnessgram"
RUNNING = "running-stamps.json"


def _package() -> Path:
    return Path(__file__).resolve().parent


def _record_stamp(site: Path) -> Optional[str]:
    infos = sorted(site.glob(f"{DIST}-*.dist-info"))
    if len(infos) != 1:
        return None  # missing (being replaced) or two copies: unknown
    try:
        record = (infos[0] / "RECORD").read_bytes()
    except OSError:
        return None
    return "r" + hashlib.sha256(record).hexdigest()[:16] if record else None


_SOURCE_STAMPS: dict = {}  # per process: a source checkout is hashed once


def _source_stamp(package: Path) -> Optional[str]:
    """Names and contents of the source files (not their times: a touch or a restored identical file is the
    same code). Computed once per process."""
    key = str(package)
    if key in _SOURCE_STAMPS:
        return _SOURCE_STAMPS[key]
    digest = hashlib.sha256()
    try:
        for path in sorted(package.rglob("*.py")):
            digest.update(str(path.relative_to(package)).encode() + b"\0" + path.read_bytes() + b"\0")
    except OSError:
        return None
    _SOURCE_STAMPS[key] = "s" + digest.hexdigest()[:16]
    return _SOURCE_STAMPS[key]


def read(package: Optional[Path] = None) -> Optional[str]:
    """The installed code's stamp, or None when it cannot be told right now."""
    package = package or _package()
    site = package.parent
    if site.name in ("site-packages", "dist-packages"):  # an installed copy: its RECORD is expected
        return _record_stamp(site)
    return _source_stamp(package)


# --- what each long-running service started with ------------------------------------------------------------

def _path() -> Path:
    return private_dir(state_dir()) / RUNNING


def _update(change: Callable[[dict], None]) -> None:
    path = _path()
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            rows = rows if isinstance(rows, dict) else {}
        except (OSError, ValueError):
            rows = {}
        change(rows)
        write_private(path, json.dumps(rows))


_READ = object()


MAX_PID = 2 ** 22  # Linux's highest possible pid_max; macOS stays far below


def _valid_pid(pid) -> bool:
    return isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid <= MAX_PID


def _proc_start(pid: int) -> str:
    """The process's start time (`ps -o lstart=`, as inbox._proc_start): with the pid it tells one process from
    a later one handed the same pid. "" when it cannot be read."""
    if not _valid_pid(pid):
        return ""
    try:
        return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def record_running(name: str, stamp=_READ, pid: Optional[int] = None) -> None:
    """Called once when a long-running service starts: the code it runs from now on (None: unknown), with
    its pid and start time."""
    stamp = read() if stamp is _READ else stamp
    pid = os.getpid() if pid is None else pid
    row = {"stamp": stamp, "pid": pid, "start": _proc_start(pid)}
    try:
        _update(lambda rows: rows.__setitem__(name, row))
    except OSError:
        pass


def clear_running(name: str, pid: Optional[int] = None) -> None:
    """Called when it exits cleanly; only its own row (a newer copy may already have written its own)."""
    pid = os.getpid() if pid is None else pid

    def drop(rows: dict) -> None:
        if isinstance(rows.get(name), dict) and rows[name].get("pid") == pid:
            del rows[name]
    try:
        _update(drop)
    except OSError:
        pass


def _alive(pid, start) -> bool:
    """True only when this very process is still there: a valid pid that exists and was started at the
    recorded time. Anything else (a malformed value, a gone process, a pid reused by another one, a start time
    that cannot be read) is not called alive, so it never makes a warning."""
    if not _valid_pid(pid) or not isinstance(start, str) or not start:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass  # it exists, under another user
    except (OSError, OverflowError, ValueError, TypeError):
        return False
    return _proc_start(pid) == start


def stale(current: Optional[str] = None, alive: Callable[[object, object], bool] = _alive) -> List[str]:
    """The services, still running, whose code is not the installed one. Unknown on either side, a malformed
    file or row: not stale. Never raises."""
    try:
        current = read() if current is None else current
        if not current:
            return []
        rows = json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return []
    out: List[str] = []
    for name, row in (rows.items() if isinstance(rows, dict) else []):
        try:
            if not isinstance(row, dict) or not isinstance(row.get("stamp"), str) or not row["stamp"]:
                continue
            if row["stamp"] != current and alive(row.get("pid"), row.get("start")):
                out.append(str(name))
        except Exception:
            continue
    return sorted(out)
