import fcntl
import time

import pytest

from ctb import autopilot, inbox, terminal_lock


@pytest.fixture
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal_lock, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(terminal_lock, "private_dir", lambda path: path)
    monkeypatch.setattr(terminal_lock, "LOCK_TIMEOUT", 0.05)
    monkeypatch.setattr(terminal_lock, "LOCK_POLL", 0.005)
    return tmp_path


def test_type_into_times_out_when_another_fd_holds_lock(lock_dir, monkeypatch):
    monkeypatch.setattr(inbox, "dismiss_tip", lambda pane: True)
    monkeypatch.setattr(inbox, "pane_runs_session", lambda pane, session: True)
    monkeypatch.setattr(inbox, "input_box", lambda pane: ("", True))
    with open(lock_dir / "terminal-input.lock", "a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        with pytest.raises(OSError, match="terminal busy"):
            autopilot.type_into("%1", "hello", "session")
        assert time.monotonic() - started < 0.5


def test_lock_is_released_when_body_raises(lock_dir):
    with pytest.raises(RuntimeError):
        with terminal_lock.locked():
            raise RuntimeError("boom")
    with terminal_lock.locked(blocking=False):
        pass


def test_lock_reentry_in_same_thread_is_safe(lock_dir):
    with terminal_lock.locked():
        with terminal_lock.locked():
            pass
    assert getattr(terminal_lock._local, "depth", 0) == 0
