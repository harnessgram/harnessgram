"""Sessions open before a power cut reopen when the heartbeat starts after the boot."""
import json

from ctb import revive
from ctb.config import state_dir, write_private


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
    assert revive.revive_after_boot(now=1100, boot=1000, resume=resume, sleep_fn=lambda s: None) == ["aaaaaaaa-1"]
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
