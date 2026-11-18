"""Cross-process serialization for bridge keystrokes sent to Claude panes."""
from __future__ import annotations

import fcntl
import threading
import time
from contextlib import contextmanager

from .config import private_dir, state_dir

LOCK_TIMEOUT = 5.0
LOCK_POLL = 0.05
_local = threading.local()


@contextmanager
def locked(*, blocking: bool = True, timeout: float | None = None):
    """Hold the bridge-wide terminal-input lock for at most ``timeout`` seconds.

    Nested use in one thread shares the already-held lock.  This makes callers
    which serialize a larger operation safe without attempting a second flock.
    """
    depth = getattr(_local, "depth", 0)
    if depth:
        _local.depth = depth + 1
        try:
            yield
        finally:
            _local.depth -= 1
        return
    timeout = LOCK_TIMEOUT if timeout is None else timeout
    directory = private_dir(state_dir())
    with open(directory / "terminal-input.lock", "a") as handle:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not blocking or time.monotonic() >= deadline:
                    raise OSError("terminal busy") from None
                time.sleep(LOCK_POLL)
        _local.depth = 1
        try:
            yield
        finally:
            _local.depth = 0
            fcntl.flock(handle, fcntl.LOCK_UN)
