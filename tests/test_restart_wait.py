"""A restarted session on a loaded Mac can take over a minute to come up; the topic must not hear "failed" first."""
from ctb import inbox


def _slow(after):
    calls = {"n": 0}

    def pid_of(_session):
        calls["n"] += 1
        return 4242 if calls["n"] > after else None
    return pid_of


def test_a_session_that_starts_after_50_seconds_counts_as_started():
    slept = []
    assert inbox.wait_for_session("s", pid_of=_slow(50), sleep=slept.append)
    assert len(slept) == 50


def test_a_session_that_never_starts_fails_after_the_full_wait():
    slept = []
    assert not inbox.wait_for_session("s", seconds=5, pid_of=lambda _s: None, sleep=slept.append)
    assert len(slept) == 5


def test_the_default_wait_covers_a_slow_start():
    assert inbox.RESTART_WAIT >= 120
