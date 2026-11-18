"""Shared launchd guard for the listener service."""
from __future__ import annotations

import fcntl
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple

from .config import private_dir, state_dir

LABEL = "com.claude-telegram-bridge.listener"
REPAIR_EVERY = 60.0
_last_repair = 0.0
_outage_logged = False


def run(*cmd: str) -> Tuple[int, str]:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return done.returncode, done.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def running(runner: Optional[Callable[..., Tuple[int, str]]] = None) -> bool:
    """Ask launchd whether the listener is loaded and has a live process."""
    runner = runner or run
    code, output = runner("launchctl", "print", f"gui/{os.getuid()}/{LABEL}")
    return code == 0 and re.search(r"^\s*pid = [1-9]\d*\s*$", output or "", re.M) is not None


@contextmanager
def install_lock(*, blocking: bool) -> Iterator[bool]:
    """Serialize installer reloads and heartbeat repairs across processes."""
    path = private_dir(state_dir()) / "listener-install.lock"
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_and_verify(plist: Path, *, runner: Optional[Callable[..., Tuple[int, str]]] = None,
                    sleep: Callable[[float], None] = time.sleep, attempts: int = 3) -> bool:
    """Bootstrap the listener and verify its pid, retrying launchd's asynchronous replacement race."""
    runner = runner or run
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"
    for attempt in range(attempts):
        code, _ = runner("launchctl", "bootstrap", domain, str(plist))
        if code == 0 and running(runner):
            return True
        if attempt + 1 < attempts:
            runner("launchctl", "bootout", target)
            sleep(0.2 * (attempt + 1))
    return False


def heartbeat_check(now: Optional[float] = None, *, runner: Optional[Callable[..., Tuple[int, str]]] = None,
                    sleep: Callable[[float], None] = time.sleep) -> bool:
    """Repair an unloaded listener, without racing an install or retrying every round."""
    global _last_repair, _outage_logged
    if sys.platform != "darwin":
        return True
    runner = runner or run
    now = time.monotonic() if now is None else now
    if running(runner):
        _outage_logged = False
        return True
    if now - _last_repair < REPAIR_EVERY:
        return False
    with install_lock(blocking=False) as acquired:
        if not acquired:
            return False
        if running(runner):
            _outage_logged = False
            return True
        _last_repair = now
        if not _outage_logged:
            print("listener LaunchAgent is not running; attempting to reload it", file=sys.stderr)
            _outage_logged = True
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        if not plist.exists():
            return False
        return load_and_verify(plist, runner=runner, sleep=sleep)
