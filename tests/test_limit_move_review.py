"""Review regressions: decisions survive waits without touching the live bridge."""
import json
from types import SimpleNamespace

import pytest

from conftest import signed_in
from ctb import accounts, inbox as I, mirror, recover, usage, wake
from ctb.config import state_dir
from test_limit_owner_auto_move import LIMIT, NOW, SID, reading, world  # noqa: F401


@pytest.fixture
def worker(world, monkeypatch, tmp_path):
    cfg, box, bot, spawned, refreshed, send = world
    accounts.add("spare")
    accounts.add("reserve")
    signed_in("spare", "reserve")
    accounts.record(SID, "main")
    monkeypatch.setattr(accounts, "origin_account", lambda s: accounts.account_for(s))
    reading("spare", 10)
    reading("reserve", 0)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "session_pid", lambda s: 123)
    monkeypatch.setattr(I, "_proc_start", lambda p: "start-123")
    monkeypatch.setattr(I, "background_work_visible", lambda p: False)
    monkeypatch.setattr(I, "_terminal_say", lambda *a, **kw: None)
    monkeypatch.setattr(I, "pass_trust", lambda *a, **kw: True)
    monkeypatch.setattr(I, "_resume_dialog_pending", lambda p: False)
    monkeypatch.setattr(I, "_idle_after_move", lambda *a: None)
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **kw: None)
    monkeypatch.setattr(I, "resume_after_move", lambda *a, **kw: "nudged")
    monkeypatch.setattr(I, "_pane_command", lambda p: "zsh")
    monkeypatch.setattr(I, "session_folder", lambda s: str(tmp_path))
    calls = []

    def tmux(*args):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=str(tmp_path))

    monkeypatch.setattr(I, "_tmux", tmux)
    monkeypatch.setattr(I, "type_command", lambda *a: "sent")
    return cfg, box, bot, calls


@pytest.mark.parametrize("wait_at", ["identity", "usage"])
def test_owner_notice_rereads_limit_after_a_wait(world, monkeypatch, wait_at):
    cfg, box, bot, spawned, refreshed, send = world
    limited = {"now": True}
    monkeypatch.setattr(usage, "limit_of", lambda s: LIMIT if limited["now"] else None)
    monkeypatch.setattr(I, "_reopen_or_nudge", lambda *a: None)

    def recovered(*a, **kw):
        limited["now"] = False
        reading("spare", 0)

    if wait_at == "identity":
        reading("spare", 0)
        monkeypatch.setattr(I.identity, "check", recovered)
    else:
        monkeypatch.setattr(accounts, "ensure_usage", recovered)
    assert send() == "queued"
    assert spawned == []
    assert not bot.methods("sendMessage")


def test_live_toggle_on_overrides_listener_startup_value(world):
    cfg, box, bot, spawned, refreshed, send = world
    cfg.auto_account_switch = False
    reading("spare", 0)
    assert send() == "queued"
    assert len(spawned) == 1


def test_owner_message_preserves_pending_manual_target(world):
    cfg, box, bot, spawned, refreshed, send = world
    reading("spare", 30)
    reading("reserve", 0)
    accounts.defer_move(SID, "spare", origin="main", manual=True)
    assert send() == "queued"
    assert not spawned or spawned[0][-1] == "--target=spare"
    assert accounts.deferred_move(SID)["target"] == "spare"


def test_automatic_deferral_cannot_replace_owner_target(world):
    accounts.defer_move(SID, "spare", origin="main", limit=LIMIT, manual=True)
    accounts.defer_move(SID, "reserve", origin="main", limit=LIMIT, manual=False)
    wait = accounts.deferred_move(SID)
    assert wait["manual"] and wait["target"] == "spare"


@pytest.mark.parametrize("boundary", ["sleep", "identity", "exit_pause"])
@pytest.mark.parametrize("change", ["off", "recovered", "account"])
def test_worker_rechecks_authority_and_limit_after_wait(worker, monkeypatch, boundary, change):
    cfg, box, bot, calls = worker
    active = {"auto": True, "limit": LIMIT}
    monkeypatch.setattr(accounts, "auto_enabled", lambda: active["auto"])
    monkeypatch.setattr(usage, "limit_of", lambda s: active["limit"])

    def changed(*a, **kw):
        if change == "off":
            active["auto"] = False
        else:
            active["limit"] = None
            accounts.record(SID, "spare")
        if change == "account":
            active["limit"] = LIMIT  # a lagging transcript must not move the recovered account again
            monkeypatch.setattr(accounts, "origin_account", lambda s: "spare")
            reading("spare", 10)

    if boundary == "sleep":
        monkeypatch.setattr(I.time, "sleep", changed)
    elif boundary == "identity":
        monkeypatch.setattr(I.identity, "check", changed)
    else:
        monkeypatch.setattr(I.time, "sleep", lambda seconds: changed() if seconds == (
            0.5 if boundary == "exit_pause" else 1) else None)
    assert not I.auto_move(cfg, bot, SID, 101, "reserve")
    if boundary in ("sleep", "identity"):
        assert not calls
    else:
        assert not any(c[0] == "new-session" or "--resume" in str(c) for c in calls)


@pytest.mark.parametrize("boundary", ["exit_wait", "exit_retry", "exited"])
@pytest.mark.parametrize("change", ["off", "recovered", "account"])
def test_cancellation_after_exit_still_restarts_and_resumes_work(worker, monkeypatch, boundary, change):
    cfg, box, bot, calls = worker
    active = {"auto": True, "limit": LIMIT, "exits": 0, "changed": False}
    resumed = []
    monkeypatch.setattr(accounts, "auto_enabled", lambda: active["auto"])
    monkeypatch.setattr(usage, "limit_of", lambda s: active["limit"])
    monkeypatch.setattr(I, "resume_after_move", lambda s, **kw: resumed.append(s) or "nudged")
    monkeypatch.setattr(I, "_exit_dialog_pending", lambda p: False)
    box.put(SID, {"text": "held"})

    def changed():
        active["changed"] = True
        if change == "off":
            active["auto"] = False
        else:
            active["limit"] = None if change == "recovered" else LIMIT
            accounts.record(SID, "spare")

    def exit_sent(*a):
        active["exits"] += 1
        return "sent"

    def sleep(seconds):
        if (boundary == "exit_wait" and seconds == 1) or (
                boundary == "exit_retry" and seconds == 0.5 and active["exits"]):
            changed()

    def command(pane):
        if boundary == "exited":
            changed()
        return "claude" if boundary == "exit_retry" and active["exits"] < 2 else "zsh"

    monkeypatch.setattr(I, "type_command", exit_sent)
    monkeypatch.setattr(I.time, "sleep", sleep)
    monkeypatch.setattr(I, "_pane_command", command)
    assert I.auto_move(cfg, bot, SID, 101, "reserve")
    assert active["changed"] and active["exits"] >= 1
    assert accounts.account_for(SID) == "reserve"
    assert sum("--resume" in str(c) for c in calls) == 1
    assert I.planned_restart_marker(SID).exists()
    assert resumed == [SID]
    assert box.pending(SID) == 1
    pending = json.loads((state_dir() / "resume-pending" / SID).read_text())
    assert pending["target"] == "reserve"


@pytest.mark.parametrize("elapsed,new_limit", [
    (61, True), (101, False), (accounts.DEFER_TTL + 1, False),
])
def test_manual_refused_exit_survives_reset_age_and_a_new_limit(worker, monkeypatch, elapsed, new_limit):
    cfg, box, bot, calls = worker
    cfg.auto_account_switch = False
    monkeypatch.setattr(accounts, "auto_enabled", lambda: False)
    monkeypatch.setattr(I, "type_command", lambda *a: "refused")
    original = dict(LIMIT, reset=NOW + 100)
    assert not I.account_move(cfg, bot, SID, "spare", lambda *a: None, limit=original)
    wait = accounts.deferred_move(SID, NOW)
    assert wait["manual"] and wait["reset"] == original["reset"]

    later = NOW + elapsed
    monkeypatch.setattr(I.time, "time", lambda: later)
    monkeypatch.setattr(usage, "limit_of", lambda s: dict(LIMIT, id="new-limit") if new_limit else None)
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a, **kw: True)
    reading("spare", 10, age=-elapsed)
    reading("reserve", 0, age=-elapsed)
    spawned = []
    recover.limit_check(cfg, bot=bot, now=later, inbox=box, spawn_fn=spawned.append)
    assert spawned == [["account", "auto-move", "--session=sess-1", "--thread=101", "--target=spare"]]
    wait = accounts.deferred_move(SID, later)
    assert wait["manual"] and wait["target"] == "spare"
    assert wait["since"] == NOW and wait["limit_id"] == original["id"]
    assert wait["tried"] == later


def test_worker_keeps_manual_target_even_when_auto_is_off(worker, monkeypatch):
    cfg, box, bot, calls = worker
    accounts.defer_move(SID, "spare", origin="main", manual=True)
    monkeypatch.setattr(accounts, "auto_enabled", lambda: False)
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    assert I.auto_move(cfg, bot, SID, 101, "reserve")
    assert accounts.account_for(SID) == "spare"
    assert any("spare" in str(c) and "--resume" in str(c) for c in calls)


def test_closed_limited_session_is_reopened_and_message_remains_deliverable(worker, monkeypatch, tmp_path):
    cfg, box, bot, calls = worker
    reading("reserve", 100)
    monkeypatch.setattr(I, "pane_for_session", lambda s: None)
    monkeypatch.setattr(I, "session_pid", lambda s: None)
    monkeypatch.setattr(accounts, "live_session_records", lambda: [])
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    monkeypatch.setattr(I.status, "transcript_of", lambda s: transcript)
    I._queue_owner(cfg, bot, box, {"message_id": 42, "message_thread_id": 101}, SID, "continue please")
    assert box.pending(SID) == 1
    assert I.auto_move(cfg, bot, SID, 101, "spare")
    assert any(c[0] == "new-session" for c in calls)
    assert accounts.account_for(SID) == "spare"
    assert (state_dir() / "limit-cleared" / SID).read_text() == LIMIT["id"]
    assert wake._forgotten_at(SID) == NOW
    assert [m["text"] for m in box.take(SID)] == ["continue please"]


def test_closed_session_launch_failure_keeps_a_retry_on_the_original_account(worker, monkeypatch, tmp_path):
    cfg, box, bot, calls = worker
    monkeypatch.setattr(I, "pane_for_session", lambda s: None)
    monkeypatch.setattr(I, "session_pid", lambda s: None)
    monkeypatch.setattr(accounts, "live_session_records", lambda: [])
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    monkeypatch.setattr(I.status, "transcript_of", lambda s: transcript)
    monkeypatch.setattr(I, "_tmux", lambda *a: SimpleNamespace(returncode=1))
    box.put(SID, {"text": "held"})
    assert not I.auto_move(cfg, bot, SID, 101, "spare")
    wait = accounts.deferred_move(SID)
    assert wait["target"] == "spare" and wait["origin"] == "main"
    assert accounts.account_for(SID) == "main" and box.pending(SID) == 1
    assert not (state_dir() / "limit-cleared" / SID).exists()
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a: False)
    monkeypatch.setattr(accounts, "restart_is_safe_or_gone", lambda *a: True)
    spawned = []
    assert recover._retry_deferred(cfg, bot, SID, 101, NOW + 61, spawned.append, {}, LIMIT)
    assert spawned and spawned[0][-1] == "--target=spare"


def test_pending_manual_move_keeps_its_target_when_background_work_still_runs(worker, monkeypatch):
    cfg, box, bot, calls = worker
    accounts.defer_move(SID, "spare", origin="main", limit=LIMIT, manual=True)
    monkeypatch.setattr(I, "background_work_visible", lambda p: True)
    assert not I.auto_move(cfg, bot, SID, 101, "reserve")
    wait = accounts.deferred_move(SID)
    assert wait["manual"] and wait["target"] == "spare"
    assert not calls


def test_worker_replans_when_target_loses_headroom_during_wait(worker, monkeypatch):
    cfg, box, bot, calls = worker
    monkeypatch.setattr(I.time, "sleep", lambda _: reading("reserve", 100))
    assert I.auto_move(cfg, bot, SID, 101, "reserve")
    assert accounts.account_for(SID) == "spare"
    pending = json.loads((state_dir() / "resume-pending" / SID).read_text())
    assert pending["target"] == "spare"


def test_a_second_worker_does_not_move_the_recovered_session_again(worker, monkeypatch):
    cfg, box, bot, calls = worker
    marker = state_dir() / "limit-cleared" / SID
    monkeypatch.setattr(usage, "limit_of", lambda s: None if marker.exists() else LIMIT)
    assert I.auto_move(cfg, bot, SID, 101, "spare")
    calls.clear()
    assert not I.auto_move(cfg, bot, SID, 101, "reserve")
    assert accounts.account_for(SID) == "spare" and not calls


def test_source_account_stays_true_until_the_reporting_process_exits(worker, monkeypatch):
    cfg, box, bot, calls = worker

    def exiting(*a):
        assert accounts.account_for(SID) == "main"
        return "sent"

    monkeypatch.setattr(I, "type_command", exiting)
    assert I.auto_move(cfg, bot, SID, 101, "spare")
    assert accounts.account_for(SID) == "spare"


def test_manual_hung_exit_saves_process_identity_and_retries(worker, monkeypatch):
    cfg, box, bot, calls = worker
    alive = {"record": True}
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    monkeypatch.setattr(I, "session_pid", lambda s: 123 if alive["record"] else None)
    monkeypatch.setattr(I, "_pane_command", lambda p: "claude")
    monkeypatch.setattr(I, "_exit_dialog_pending", lambda p: False)

    def exit_sent(*a):
        alive["record"] = False
        return "sent"

    monkeypatch.setattr(I, "type_command", exit_sent)
    assert not I.account_move(cfg, bot, SID, "spare", lambda *a: None)
    wait = accounts.deferred_move(SID)
    assert wait["manual"] and (wait["pid"], wait["started"]) == (123, "start-123")
    monkeypatch.setattr(I, "_live_records_strict", lambda p: [])
    monkeypatch.setattr(I, "_pane_claude_targets", lambda p: [(123, "start-123")])
    monkeypatch.setattr(I, "pane_runs_session", lambda *a, **kw: True)
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a: False)
    monkeypatch.setattr(accounts, "restart_is_safe_or_gone", lambda *a: True)
    spawned = []
    assert recover._retry_deferred(cfg, bot, SID, 101, NOW + 61, spawned.append, {}, None)
    assert spawned and spawned[0][-1] == "--target=spare"


@pytest.mark.parametrize("source", ["main", "spare"])
def test_late_limit_report_uses_reporting_process_not_planned_target(world, monkeypatch, source):
    cfg, box, bot, spawned, refreshed, send = world
    accounts.add("spare")
    accounts.add("reserve")
    accounts.record(SID, "reserve")
    monkeypatch.setattr(accounts, "origin_account", lambda s: accounts.account_for(s))
    if source == "spare":
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(accounts.config_dir("spare")))
    else:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert mirror.limit_offer(cfg, bot, SID, "You've hit your usage limit", spawn_fn=spawned.append)
    hits = json.loads(accounts._limits_path().read_text())
    assert source in hits and "reserve" not in hits
