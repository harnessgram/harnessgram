"""Sessions open before a power cut reopen when the heartbeat starts after the boot."""
import json
import threading

import pytest

from ctb import revive
from ctb.config import state_dir, write_private


@pytest.fixture(autouse=True)
def fake_revive_server(monkeypatch):
    """Boot recovery now reads the new server too; never ask the real tmux server."""
    monkeypatch.setattr(revive, "server_start", lambda: "77")
    monkeypatch.setattr(revive, "revived", threading.Event())
    monkeypatch.setattr(revive, "reviving", threading.Event())


def _record(t, sessions):
    write_private(state_dir() / revive.RECORD, json.dumps({"t": t, "sessions": sessions}))


def test_reopens_sessions_recorded_before_boot(monkeypatch):
    _record(900, ["aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3"])
    monkeypatch.setattr(revive, "open_windows", lambda: ["tg-bbbbbbbb", "0"])
    opened = []
    done = revive.revive_after_boot(now=1100, boot=1000, resume=lambda s: opened.append(s) or True,
                                    sleep_fn=lambda s: None)
    assert opened == ["aaaaaaaa-1", "cccccccc-3"] and done == opened  # the open one is left alone


def test_nothing_when_record_is_after_boot_or_boot_is_old(monkeypatch):
    monkeypatch.setattr(revive, "open_windows", lambda: [])
    calls = []
    _record(1050, ["aaaaaaaa-1"])
    assert revive.revive_after_boot(now=1100, boot=1000, resume=calls.append, sleep_fn=lambda s: None) == []
    _record(900, ["aaaaaaaa-1"])
    assert revive.revive_after_boot(now=1000 + revive.BOOT_WINDOW + 1, boot=1000, resume=calls.append,
                                    sleep_fn=lambda s: None) == []
    assert calls == []


def test_snapshot_keeps_the_record_until_the_revive_ran(monkeypatch):
    monkeypatch.setattr(revive, "revived", __import__("threading").Event())
    _record(900, ["aaaaaaaa-1"])
    monkeypatch.setattr(revive, "open_windows", lambda: [])
    revive.snapshot(now=2000)
    assert json.loads((state_dir() / revive.RECORD).read_text())["sessions"] == ["aaaaaaaa-1"]
    monkeypatch.setattr(revive, "open_windows", lambda: ["tg-bbbbbbbb"])
    monkeypatch.setattr(revive, "_known", lambda: ["aaaaaaaa-1", "bbbbbbbb-2"])
    monkeypatch.setattr(revive, "server_start", lambda: "77")
    revive.snapshot(now=2000)
    assert json.loads((state_dir() / revive.RECORD).read_text()) == {"t": 2000, "sessions": ["bbbbbbbb-2"],
                                                                      "server": "77"}


def test_failed_reopen_is_tried_again(monkeypatch):
    _record(900, ["aaaaaaaa-1"])
    monkeypatch.setattr(revive, "open_windows", lambda: [])
    tries = []
    def resume(s):
        tries.append(s)
        if len(tries) == 1:
            raise OSError("tmux unavailable")
        return True
    assert revive.revive_after_boot(now=1100, boot=1000, resume=resume, sleep_fn=lambda s: None) == []
    revive.revived.set()
    monkeypatch.setattr(revive.time, "sleep", lambda s: None)
    revive.snapshot(now=1100 + revive.RETRY_EVERY, resume=resume, background=False)
    assert len(tries) == 2


def test_empty_list_recorded_after_the_revive(monkeypatch):
    ev = __import__("threading").Event()
    ev.set()
    monkeypatch.setattr(revive, "revived", ev)
    _record(900, ["aaaaaaaa-1"])
    monkeypatch.setattr(revive, "open_windows", lambda: ["0"])
    monkeypatch.setattr(revive, "_known", lambda: ["aaaaaaaa-1"])
    monkeypatch.setattr(revive, "server_start", lambda: "77")  # the tmux server's start time, without tmux
    revive.snapshot(now=2000)
    assert json.loads((state_dir() / revive.RECORD).read_text())["sessions"] == []


def test_stale_record_does_not_revive(monkeypatch):
    _record(1000 - revive.FRESH - 1, ["aaaaaaaa-1"])
    monkeypatch.setattr(revive, "open_windows", lambda: [])
    calls = []
    assert revive.revive_after_boot(now=1100, boot=1000, resume=calls.append, sleep_fn=lambda s: None) == []


def _crash_setup(monkeypatch, server, windows):
    ev = __import__("threading").Event()
    ev.set()
    monkeypatch.setattr(revive, "revived", ev)
    monkeypatch.setattr(revive, "_known", lambda: ["aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3"])
    monkeypatch.setattr(revive, "server_start", lambda: server)
    monkeypatch.setattr(revive, "open_windows", lambda: windows)


def test_tmux_crash_reopens_the_lost_sessions(monkeypatch):
    """2026-11-01: the tmux server died without a reboot; a message reopened one session and the
    snapshot then forgot the other five."""
    write_private(state_dir() / revive.RECORD, json.dumps(
        {"t": 1000, "sessions": ["aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3"], "server": "11"}))
    _crash_setup(monkeypatch, "22", ["tg-bbbbbbbb"])
    opened = []
    monkeypatch.setattr(revive.time, "sleep", lambda s: None)
    revive.snapshot(now=1100, resume=lambda s: opened.append(s) or True, background=False)
    assert opened == ["aaaaaaaa-1", "cccccccc-3"]
    assert json.loads((state_dir() / revive.RECORD).read_text())["server"] == "22"


def test_server_gone_counts_as_a_crash(monkeypatch):
    write_private(state_dir() / revive.RECORD, json.dumps(
        {"t": 1000, "sessions": ["aaaaaaaa-1", "bbbbbbbb-2"], "server": "11"}))
    _crash_setup(monkeypatch, "", [])
    opened = []
    monkeypatch.setattr(revive.time, "sleep", lambda s: None)
    revive.snapshot(now=1100, resume=lambda s: opened.append(s) or True, background=False)
    assert opened == ["aaaaaaaa-1", "bbbbbbbb-2"]


def test_closing_by_hand_or_a_stale_record_reopens_nothing(monkeypatch):
    def run(record, server, windows, now=1100):
        write_private(state_dir() / revive.RECORD, json.dumps(record))
        _crash_setup(monkeypatch, server, windows)
        opened = []
        revive.snapshot(now=now, resume=opened.append, background=False)
        return opened
    # the last session closed by hand: the server exits, one session lost
    assert run({"t": 1000, "sessions": ["aaaaaaaa-1"], "server": "11"}, "", []) == []
    # same server, sessions closed: closed on purpose
    assert run({"t": 1000, "sessions": ["aaaaaaaa-1", "bbbbbbbb-2"], "server": "11"}, "11", []) == []
    # record from before the heartbeat was off for a long time
    assert run({"t": 1000, "sessions": ["aaaaaaaa-1", "bbbbbbbb-2"], "server": "11"}, "22", [],
               now=1000 + revive.FRESH + 1) == []
    # an old record without a server
    assert run({"t": 1000, "sessions": ["aaaaaaaa-1", "bbbbbbbb-2"]}, "22", []) == []


def test_crash_reopen_runs_once_per_window(monkeypatch):
    """Review of #42: sessions that die right after reopening must not be reopened forever."""
    write_private(state_dir() / revive.RECORD, json.dumps(
        {"t": 1000, "sessions": ["aaaaaaaa-1", "bbbbbbbb-2"], "server": "11", "crash_t": 900}))
    _crash_setup(monkeypatch, "", [])
    opened = []
    revive.snapshot(now=1100, resume=opened.append, background=False)
    assert opened == []


def test_sessions_closed_by_the_owner_stay_closed(monkeypatch):
    write_private(state_dir() / revive.RECORD, json.dumps(
        {"t": 1000, "sessions": ["aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3"], "server": "11"}))
    revive.closed_on_purpose("aaaaaaaa-1", now=1050)
    revive.closed_on_purpose("bbbbbbbb-2", now=1060)
    _crash_setup(monkeypatch, "", [])
    opened = []
    revive.snapshot(now=1100, resume=opened.append, background=False)
    assert opened == []  # only one unexplained loss left: below CRASH_MIN


def test_session_end_by_exit_marks_closed(monkeypatch):
    from ctb import hooks
    monkeypatch.setattr(hooks, "load_config", lambda: type("C", (), {"ready": True})())
    monkeypatch.setattr(hooks, "spawn", lambda args: None)
    monkeypatch.setattr("ctb.inbox.consume_planned_restart", lambda s: False)
    hooks.hook_lifecycle(json.dumps({"session_id": "aaaaaaaa-1", "hook_event_name": "SessionEnd",
                                     "reason": "prompt_input_exit"}))
    hooks.hook_lifecycle(json.dumps({"session_id": "bbbbbbbb-2", "hook_event_name": "SessionEnd",
                                     "reason": "other"}))
    assert revive._closed_recently(__import__("time").time()) == {"aaaaaaaa-1"}


def _read_record():
    return json.loads((state_dir() / revive.RECORD).read_text())


def _recovery_setup(monkeypatch, sessions):
    state = {"server": "new", "windows": [], "now": 1100}
    monkeypatch.setattr(revive, "server_start", lambda: state["server"])
    monkeypatch.setattr(revive, "open_windows", lambda: state["windows"])
    monkeypatch.setattr(revive, "_known", lambda: sessions)
    monkeypatch.setattr(revive.time, "time", lambda: state["now"])
    monkeypatch.setattr(revive.time, "sleep", lambda s: None)
    return state


def test_revive_reboot_incident_preserves_nine_and_attempts_all_on_second_boot(monkeypatch):
    """16.11: six failed boot reopens must survive snapshots and another boot 18 minutes later."""
    sessions = [f"{n:08d}-session" for n in range(9)]
    state = _recovery_setup(monkeypatch, sessions)
    write_private(state_dir() / revive.RECORD, json.dumps(
        {"t": 900, "sessions": sessions, "server": "old", "crash_t": 950}))
    calls = []

    def partial(sid):
        calls.append(sid)
        if sid in sessions[:3]:
            state["windows"].append("tg-" + sid[:8])
            return True
        return False

    assert revive.revive_after_boot(now=1100, boot=1000, resume=partial, sleep_fn=lambda s: None) == sessions[:3]
    assert calls == sessions
    assert _read_record()["sessions"] == sessions
    assert set(_read_record()["retry"]) == set(sessions[3:])
    assert _read_record()["server"] == "new"
    assert _read_record()["crash_t"] == 950  # boot did not stamp a crash or obey its cooldown
    revive.revived.set()  # heartbeat does this only after boot recovery finishes
    calls.clear()
    revive.snapshot(now=1101, resume=partial, background=False)
    assert calls == []  # no false crash reopen after the server change
    assert _read_record()["sessions"] == sessions
    assert _read_record()["crash_t"] == 950

    # Normal heartbeat snapshots keep FRESH meaningful during the 18-minute interval.
    revive.snapshot(now=2170, resume=lambda s: False, background=False)
    assert _read_record()["sessions"] == sessions
    state.update(server="second-boot", windows=[], now=2200)
    calls.clear()

    def complete(sid):
        calls.append(sid)
        state["windows"].append("tg-" + sid[:8])
        return True

    assert revive.revive_after_boot(now=2200, boot=2180, resume=complete, sleep_fn=lambda s: None) == sessions
    assert calls == sessions  # all nine, despite crash_t being less than CRASH_EVERY ago
    assert _read_record()["sessions"] == sessions and "retry" not in _read_record()


def test_revive_boot_failure_retries_are_spaced_bounded_and_retained(monkeypatch):
    sessions = ["aaaaaaaa-1", "bbbbbbbb-2"]
    state = _recovery_setup(monkeypatch, sessions)
    _record(900, sessions)
    calls = []
    fail = lambda s: calls.append(s) or False
    revive.revive_after_boot(now=1100, boot=1000, resume=fail, sleep_fn=lambda s: None)
    revive.revived.set()
    assert calls == sessions
    for attempt in range(1, revive.RETRY_MAX):
        t = 1100 + attempt * revive.RETRY_EVERY
        revive.snapshot(now=t - 1, resume=fail, background=False)
        assert len(calls) == len(sessions) * attempt
        revive.snapshot(now=t, resume=fail, background=False)
        assert len(calls) == len(sessions) * (attempt + 1)
    for t in (2000, 4000, 8000):
        revive.snapshot(now=t, resume=fail, background=False)
    assert calls == sessions * revive.RETRY_MAX
    rec = _read_record()
    assert rec["sessions"] == sessions and "crash_t" not in rec
    assert all(a["attempts"] == revive.RETRY_MAX for a in rec["retry"].values())
    # A new boot is a new recovery, even after this budget was exhausted.
    state["server"] = "third"
    revive.revive_after_boot(now=8100, boot=8050, resume=fail, sleep_fn=lambda s: None)
    assert calls == sessions * (revive.RETRY_MAX + 1)
    assert all(a["attempts"] == 1 for a in _read_record()["retry"].values())


def test_revive_boot_does_not_create_crash_time_without_a_prior_crash(monkeypatch):
    sessions = ["aaaaaaaa-1", "bbbbbbbb-2"]
    _recovery_setup(monkeypatch, sessions)
    write_private(state_dir() / revive.RECORD, json.dumps(
        {"t": 900, "sessions": sessions, "server": "old"}))
    revive.revive_after_boot(now=1100, boot=1000, resume=lambda s: False, sleep_fn=lambda s: None)
    revive.revived.set()
    revive.snapshot(now=1101, resume=lambda s: pytest.fail("false crash revive"), background=False)
    assert _read_record()["sessions"] == sessions
    assert _read_record()["server"] == "new" and "crash_t" not in _read_record()


@pytest.mark.parametrize("server", ["", None])
def test_revive_all_failed_with_no_server_still_rebinds_record(monkeypatch, server):
    sessions = ["aaaaaaaa-1", "bbbbbbbb-2"]
    state = _recovery_setup(monkeypatch, sessions)
    state["server"] = server
    write_private(state_dir() / revive.RECORD, json.dumps(
        {"t": 900, "sessions": sessions, "server": "old"}))
    revive.revive_after_boot(now=1100, boot=1000, resume=lambda s: False, sleep_fn=lambda s: None)
    assert _read_record()["sessions"] == sessions
    assert _read_record()["server"] == ""
    assert set(_read_record()["retry"]) == set(sessions)


def test_revive_records_failed_reopens_even_when_reopen_raises(monkeypatch):
    sessions = ["aaaaaaaa-1", "bbbbbbbb-2"]
    _recovery_setup(monkeypatch, sessions)
    _record(900, sessions)
    def broken(*a, **kw):
        raise RuntimeError("unexpected reopen failure")
    monkeypatch.setattr(revive, "reopen", broken)
    with pytest.raises(RuntimeError):
        revive.revive_after_boot(now=1100, boot=1000, resume=lambda s: False)
    assert _read_record()["server"] == "new" and _read_record()["sessions"] == sessions
    assert not revive.reviving.is_set()


def test_revive_crash_preserves_failed_reopens_and_does_not_reset_retry_budget(monkeypatch):
    sessions = ["aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3"]
    state = _recovery_setup(monkeypatch, sessions)
    write_private(state_dir() / revive.RECORD, json.dumps(
        {"t": 1000, "sessions": sessions, "server": "old"}))
    revive.revived.set()
    def partial(sid):
        if sid == sessions[0]:
            state["windows"].append("tg-" + sid[:8])
            return True
        return False
    revive.snapshot(now=1100, resume=partial, background=False)
    assert _read_record()["sessions"] == sessions
    assert _read_record()["crash_t"] == 1100
    assert set(_read_record()["retry"]) == set(sessions[1:])
    revive.snapshot(now=1220, resume=lambda s: False, background=False)
    assert _read_record()["crash_t"] == 1100
    assert all(a["attempts"] == 2 for a in _read_record()["retry"].values())


def test_revive_intentional_close_drops_pending_before_retry_and_next_boot(monkeypatch):
    sessions = ["aaaaaaaa-1", "bbbbbbbb-2"]
    _recovery_setup(monkeypatch, sessions)
    _record(900, sessions)
    revive.closed_on_purpose(sessions[0], now=1050)
    calls = []
    fail = lambda s: calls.append(s) or False
    revive.revive_after_boot(now=1100, boot=1000, resume=fail, sleep_fn=lambda s: None)
    assert calls == sessions[1:]
    assert _read_record()["sessions"] == sessions[1:]
    revive.closed_on_purpose(sessions[1], now=1150)
    revive.revived.set()
    revive.snapshot(now=1220, resume=lambda s: pytest.fail("closed session reopened"), background=False)
    assert _read_record()["sessions"] == [] and "retry" not in _read_record()
    assert revive.revive_after_boot(now=1310, boot=1300, resume=fail, sleep_fn=lambda s: None) == []
    assert calls == sessions[1:]


def test_revive_pending_seen_open_is_removed_from_retry(monkeypatch):
    sessions = ["aaaaaaaa-1"]
    state = _recovery_setup(monkeypatch, sessions)
    _record(900, sessions)
    revive.revive_after_boot(now=1100, boot=1000, resume=lambda s: False, sleep_fn=lambda s: None)
    state["windows"] = ["tg-aaaaaaaa"]  # opened manually, or became visible after resume returned
    revive.revived.set()
    revive.snapshot(now=1101, resume=lambda s: pytest.fail("already open"), background=False)
    assert "retry" not in _read_record()
    state["windows"] = []  # the owner later closes it without a server crash
    revive.snapshot(now=1102, resume=lambda s: pytest.fail("closed session reopened"), background=False)
    assert _read_record()["sessions"] == []


def test_revive_pending_retry_excludes_inflight_snapshots(monkeypatch):
    sessions = ["aaaaaaaa-1"]
    _recovery_setup(monkeypatch, sessions)
    _record(900, sessions)
    revive.revive_after_boot(now=1100, boot=1000, resume=lambda s: False, sleep_fn=lambda s: None)
    revive.revived.set()
    before = _read_record()
    def resume(sid):
        assert revive.reviving.is_set()
        revive.snapshot(now=1220, resume=lambda s: pytest.fail("double reopen"), background=False)
        assert _read_record() == before
        return False
    revive.snapshot(now=1220, resume=resume, background=False)
    assert _read_record()["retry"][sessions[0]]["attempts"] == 2
    assert not revive.reviving.is_set()


def test_revive_exhausted_pending_does_not_count_as_new_crash_losses(monkeypatch):
    sessions = ["aaaaaaaa-1", "bbbbbbbb-2"]
    _recovery_setup(monkeypatch, sessions)
    write_private(state_dir() / revive.RECORD, json.dumps(
        {"t": 1000, "sessions": sessions, "server": "old",
         "retry": {s: {"attempts": revive.RETRY_MAX, "t": 1000} for s in sessions}}))
    revive.revived.set()
    revive.snapshot(now=1100, resume=lambda s: pytest.fail("exhausted budget restarted"), background=False)
    rec = _read_record()
    assert rec["sessions"] == sessions and "crash_t" not in rec
    assert all(a["attempts"] == revive.RETRY_MAX for a in rec["retry"].values())


def test_revive_close_during_reopen_skips_remaining_session(monkeypatch):
    sessions = ["aaaaaaaa-1", "bbbbbbbb-2"]
    _recovery_setup(monkeypatch, sessions)
    _record(900, sessions)
    calls = []
    def resume(sid):
        calls.append(sid)
        revive.closed_on_purpose(sessions[1], now=1100)
        return False
    revive.revive_after_boot(now=1100, boot=1000, resume=resume, sleep_fn=lambda s: None)
    assert calls == sessions[:1]
    assert _read_record()["sessions"] == sessions[:1]
    assert set(_read_record()["retry"]) == set(sessions[:1])


@pytest.mark.parametrize("initially_open", [True, False])
def test_revive_long_recovery_keeps_intentional_close_after_marker_expires(monkeypatch, initially_open):
    pending = [f"{n:08d}-session" for n in range(36)]
    closed = "zzzzzzzz-closed"
    sessions = pending + [closed]
    state = _recovery_setup(monkeypatch, sessions)
    if initially_open:
        state["windows"] = ["tg-zzzzzzzz"]
    _record(900, sessions)
    calls = []

    def slow_resume(sid):
        calls.append(sid)
        state["now"] += 25
        if len(calls) == 1:
            state["windows"] = []
            revive.closed_on_purpose(closed)
        if sid == pending[-1]:
            return False  # a real failed reopen must still be retried
        state["windows"].append("tg-" + sid[:8])
        return True

    def sleep(seconds):
        state["now"] += seconds

    assert revive.revive_after_boot(boot=1000, resume=slow_resume, sleep_fn=sleep) == pending[:-1]
    assert state["now"] == 1100 + 36 * (25 + 5)
    assert closed not in revive._closed_recently(state["now"])
    assert calls == pending
    rec = _read_record()
    assert rec["sessions"] == pending
    assert rec["retry"] == {pending[-1]: {"attempts": 1, "t": state["now"]}}

    revive.revived.set()
    state["now"] += revive.RETRY_EVERY
    def retry(sid):
        calls.append(sid)
        state["windows"].append("tg-" + sid[:8])
        return True
    revive.snapshot(resume=retry, background=False)
    assert calls == pending + [pending[-1]]
    assert _read_record()["sessions"] == pending and "retry" not in _read_record()


def test_revive_retry_interval_starts_after_recovery_finishes(monkeypatch):
    sessions = ["aaaaaaaa-1"]
    state = _recovery_setup(monkeypatch, sessions)
    _record(900, sessions)
    def slow_failure(sid):
        state["now"] += 50
        return False
    revive.revive_after_boot(boot=1000, resume=slow_failure, sleep_fn=lambda s: None)
    assert _read_record()["retry"][sessions[0]]["t"] == 1150
    revive.revived.set()
    state["now"] = 1150 + revive.RETRY_EVERY - 1
    revive.snapshot(resume=lambda s: pytest.fail("retry too soon"), background=False)
    state["now"] += 1
    revive.snapshot(resume=slow_failure, background=False)
    assert _read_record()["retry"][sessions[0]] == {"attempts": 2, "t": state["now"]}


@pytest.mark.parametrize("pending_due", [False, True])
def test_revive_crash_cooldown_preserves_new_losses_alongside_pending_retry(monkeypatch, pending_due):
    sessions = ["aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3"]
    state = _recovery_setup(monkeypatch, sessions)
    write_private(state_dir() / revive.RECORD, json.dumps(
        {"t": 1000, "sessions": sessions, "server": "old", "crash_t": 900,
         "retry": {sessions[0]: {"attempts": 1, "t": 900 if pending_due else 1050}}}))
    revive.revived.set()
    calls = []
    fail = lambda s: calls.append(s) or False
    revive.snapshot(now=1100, resume=fail, background=False)
    assert calls == (sessions[:1] if pending_due else [])
    assert _read_record()["sessions"] == sessions and _read_record()["crash_t"] == 900
    for sid in sessions[1:]:
        assert _read_record()["retry"][sid]["attempts"] == 0  # not attempted during crash cooldown
    revive.snapshot(now=1300, resume=fail, background=False)
    revive.snapshot(now=2699, resume=fail, background=False)
    assert all(s == sessions[0] for s in calls)
    calls.clear()
    def complete(sid):
        calls.append(sid)
        state["windows"].append("tg-" + sid[:8])
        return True
    revive.snapshot(now=2700, resume=complete, background=False)
    assert calls == sessions[1:]
    assert _read_record()["sessions"] == sessions and _read_record()["crash_t"] == 2700


def test_revive_boot_ignores_deferred_crash_retry_cooldown(monkeypatch):
    sessions = ["aaaaaaaa-1", "bbbbbbbb-2"]
    _recovery_setup(monkeypatch, sessions)
    write_private(state_dir() / revive.RECORD, json.dumps(
        {"t": 1000, "sessions": sessions, "server": "old", "crash_t": 900,
         "retry": {s: {"attempts": 0, "t": 1000, "after": 2700} for s in sessions}}))
    calls = []
    revive.revive_after_boot(now=1200, boot=1150, resume=lambda s: calls.append(s) or False,
                            sleep_fn=lambda s: None)
    assert calls == sessions
    assert all(a == {"attempts": 1, "t": 1200} for a in _read_record()["retry"].values())
