"""Small, non-interactive macOS Keychain boundary.

Secrets are never part of a process argument or an exception.  Failed secret
reads are negatively cached so an unattended login Keychain cannot accumulate
authorization dialogs.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from typing import Callable, Optional, Tuple

READ_TIMEOUT = 5.0
FAILURE_COOLDOWN = 30 * 60.0
NOT_FOUND = 44
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FAILURES: dict[Tuple[str, str], float] = {}
_FAILURES_LOCK = threading.Lock()
_IN_FLIGHT: dict[Tuple[str, str], threading.Lock] = {}


def _item_lock(item: Tuple[str, str]) -> threading.Lock:
    """One read per item at a time, so concurrent callers cannot each raise a dialog."""
    with _FAILURES_LOCK:
        return _IN_FLIGHT.setdefault(item, threading.Lock())


def _quoted(value: str) -> Optional[str]:
    if not isinstance(value, str) or not value or _CONTROL.search(value):
        return None
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Keychain:
    """Generic passwords addressed by ``(service, account)``."""

    def __init__(self, runner: Callable = subprocess.run, clock: Callable = time.monotonic,
                 timeout: float = READ_TIMEOUT, cooldown: float = FAILURE_COOLDOWN):
        self.run, self.clock = runner, clock
        self.timeout, self.cooldown = timeout, cooldown
        self._failed = _FAILURES
        self._lock = _FAILURES_LOCK

    @staticmethod
    def _valid(item: Tuple[str, str]) -> bool:
        return len(item) == 2 and _quoted(item[0]) is not None and _quoted(item[1]) is not None

    def get(self, item: Tuple[str, str]) -> Optional[str]:
        return self.get_status(item)[1]

    def get_status(self, item: Tuple[str, str]) -> Tuple[str, Optional[str]]:
        """Return ``ok``, ``missing``, or ``error`` without exposing diagnostic secret data."""
        if not self._valid(item):
            return "error", None
        with _item_lock(item):
            return self._read(item)

    def _read(self, item: Tuple[str, str]) -> Tuple[str, Optional[str]]:
        now = self.clock()
        with self._lock:
            if self._failed.get(item, float("-inf")) + self.cooldown > now:
                return "error", None
        try:
            result = self.run(["security", "find-generic-password", "-s", item[0], "-a", item[1], "-w"],
                              capture_output=True, text=True, timeout=self.timeout,
                              stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError):
            result = None
        value = ((result.stdout or "").strip() if result is not None and result.returncode == 0 else "")
        missing = result is not None and result.returncode == NOT_FOUND
        with self._lock:
            if value or missing:
                self._failed.pop(item, None)
            else:
                self._failed[item] = now
        if value:
            return "ok", value
        if missing:
            return "missing", None
        return "error", None

    def get_service(self, service: str) -> Optional[str]:
        """Read the sole item for a service (used by applications which do not name its account)."""
        if _quoted(service) is None:
            return None
        item = (service, "")
        with _item_lock(item):
            return self._read_service(service, item)

    def _read_service(self, service: str, item: Tuple[str, str]) -> Optional[str]:
        now = self.clock()
        with self._lock:
            if self._failed.get(item, float("-inf")) + self.cooldown > now:
                return None
        try:
            result = self.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True,
                              text=True, timeout=self.timeout, stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError):
            result = None
        value = ((result.stdout or "").strip() if result is not None and result.returncode == 0 else "")
        missing = result is not None and result.returncode == NOT_FOUND
        with self._lock:
            if value or missing:
                self._failed.pop(item, None)
            else:
                self._failed[item] = now
        return value or None

    def has(self, item: Tuple[str, str]) -> bool:
        if not self._valid(item):
            return False
        try:
            return self.run(["security", "find-generic-password", "-s", item[0], "-a", item[1]],
                            capture_output=True, timeout=self.timeout, stdin=subprocess.DEVNULL,
                            check=False).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def set(self, item: Tuple[str, str], value: str, *, update: bool = True, label: Optional[str] = None) -> bool:
        if not self._valid(item) or not isinstance(value, str) or not value:
            return False
        service, account = map(_quoted, item)
        shown = _quoted(label if label is not None else item[0])
        if shown is None:
            return False
        try:
            encoded = value.encode("utf-8").hex()
        except UnicodeError:
            return False
        command = "add-generic-password " + ("-U " if update else "")
        command += f"-s {service} -a {account} -l {shown} -X {encoded}\n"
        try:
            result = self.run(["security", "-i"], input=command, capture_output=True, text=True,
                              timeout=self.timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode == 0:
            with self._lock:
                self._failed.pop(item, None)
            return True
        return False

    def delete(self, item: Tuple[str, str]) -> None:
        if not self._valid(item):
            return
        try:
            self.run(["security", "delete-generic-password", "-s", item[0], "-a", item[1]],
                     capture_output=True, timeout=self.timeout, stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError):
            pass

