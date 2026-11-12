"""Install and setup claim only what they checked (audit F5): a failed start is a failure, and the closing
message lists what was checked and, plainly, what was not. No test runs launchctl or systemctl."""
import os

import pytest

from ctb import doctor, install as I, setup
from test_setup_doctor import GOOD_TOKEN, SetupBot


@pytest.fixture
def svc(tmp_path, monkeypatch):
    home = tmp_path / "svc-home"
    home.mkdir()
    monkeypatch.setattr(I, "HOME", home)
    calls, fail = [], {}

    def run(*cmd):
        calls.append(cmd)
        return next((code for match, code in fail.items() if any(match in part for part in cmd)), 0)

    monkeypatch.setattr(I, "_sh", run)
    return home, calls, fail


def platform(monkeypatch, name):
    monkeypatch.setattr(I.sys, "platform", name)


def test_macos_everything_loaded_is_checked_and_returns_zero(svc, tmp_path, monkeypatch, capsys):
    home, calls, fail = svc
    platform(monkeypatch, "darwin")
    assert I.install(settings_file=tmp_path / "s.json") == 0
    out = capsys.readouterr().out
    uid = os.getuid()
    for s in I.SERVICES:  # launchd itself is asked after each bootstrap
        assert ("launchctl", "print", f"gui/{uid}/{s.label}") in calls
    checked = out.split("Checked:")[1].split("Not checked:")[0]
    assert "hooks are in" in checked and "loaded by launchd: listener, dashboard, heartbeat, daily" in checked
    assert "that the listener reaches Telegram" in out.split("Not checked:")[1]
    assert "started (runs at every login)" not in out


def test_macos_failed_bootstrap_is_a_failure_with_the_command_on_its_own_line(svc, tmp_path, monkeypatch, capsys):
    home, calls, fail = svc
    platform(monkeypatch, "darwin")
    fail["bootstrap"] = 5
    report = I.run_install(settings_file=tmp_path / "s.json")
    assert report.code == 1 and report.started is False and report.failed == ["listener", "dashboard", "heartbeat",
                                                                              "daily"]
    out = capsys.readouterr().out
    plist = home / "Library" / "LaunchAgents" / f"{I.LABEL}.plist"
    assert f"    launchctl bootstrap gui/{os.getuid()} {plist}" in out.splitlines()
    assert "loaded by launchd" not in out.split("Checked:")[1].split("Not checked:")[0]
    assert "not running: listener, dashboard, heartbeat, daily" in out


def test_macos_bootstrap_ok_but_not_loaded_is_still_a_failure(svc, tmp_path, monkeypatch):
    home, calls, fail = svc
    platform(monkeypatch, "darwin")
    fail["print"] = 113  # bootstrap said 0, but launchd does not know the job
    report = I.run_install(settings_file=tmp_path / "s.json")
    assert report.code == 1 and report.running == [] and len(report.failed) == 4


def test_no_start_and_no_service_are_said_plainly_and_are_not_failures(svc, tmp_path, monkeypatch, capsys):
    home, calls, fail = svc
    platform(monkeypatch, "darwin")
    report = I.run_install(settings_file=tmp_path / "s.json", start=False)
    assert report.code == 0 and report.started is None and calls == []
    assert any("written but not started" in u for u in report.unchecked())
    report = I.run_install(settings_file=tmp_path / "s.json", service=False)
    assert report.code == 0 and any("no background service was installed" in u for u in report.unchecked())


def test_linux_without_systemd_returns_one(svc, tmp_path, monkeypatch):
    home, calls, fail = svc
    platform(monkeypatch, "linux")
    fail["show-environment"] = 1
    report = I.run_install(settings_file=tmp_path / "s.json")
    assert report.code == 1 and report.failed and not report.running


def test_linux_active_units_are_checked(svc, tmp_path, monkeypatch, capsys):
    home, calls, fail = svc
    platform(monkeypatch, "linux")
    fail["claude-telegram-bridge-daily.timer"] = 0
    report = I.run_install(settings_file=tmp_path / "s.json")
    assert report.code == 0 and report.running == ["listener", "dashboard", "heartbeat", "daily"]
    assert "active in systemd: listener" in capsys.readouterr().out


def test_hooks_are_read_back(svc, tmp_path, monkeypatch):
    home, calls, fail = svc
    platform(monkeypatch, "darwin")
    real, reads = I._read_settings, []

    def read(p):
        reads.append(p)
        return real(p) if len(reads) == 1 else {}  # the read-back after writing finds no hooks

    monkeypatch.setattr(I, "_read_settings", read)
    report = I.run_install(settings_file=tmp_path / "s.json")
    assert report.hooks is False and report.code == 1
    assert any("could not be confirmed" in u for u in report.unchecked())


def test_dry_run_claims_nothing_and_returns_zero(svc, tmp_path, monkeypatch, capsys):
    home, calls, fail = svc
    platform(monkeypatch, "darwin")
    assert I.install(dry_run=True, settings_file=tmp_path / "s.json") == 0
    out = capsys.readouterr().out
    assert "Checked:" not in out and "nothing was changed" in out


# --- doctor: a service file that exists is not enough --------------------------------------------------------

def test_doctor_warns_when_a_job_file_exists_but_is_not_loaded(svc, tmp_path, monkeypatch):
    home, calls, fail = svc
    platform(monkeypatch, "darwin")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    I.install(settings_file=I.settings_path(), start=False)
    fail[f"{I.LABEL_PREFIX}.heartbeat"] = 113
    results = [r for r in doctor.check_hooks() if " file " in r[1]]
    bad = [r for r in results if r[0] == "warn"]
    assert len(bad) == 1 and "heartbeat job" in bad[0][1] and "not loaded" in bad[0][1]
    assert "harnessgram install" in bad[0][1]
    assert all(r[1].endswith(", loaded") for r in results if r[0] == "ok")


def test_doctor_cannot_tell_without_systemd_and_says_only_the_file(svc, tmp_path, monkeypatch):
    home, calls, fail = svc
    platform(monkeypatch, "linux")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    I.install(settings_file=I.settings_path(), start=False)
    fail["show-environment"] = 1
    results = [r for r in doctor.check_hooks() if " file " in r[1]]
    assert [r[0] for r in results] == ["ok"] * 4 and not any("active" in r[1] for r in results)


def test_wait_listener_polls_until_ok_or_time_is_up():
    answers = iter([[("FAIL", "not running")], [("warn", "no status yet")], [("ok", "polling")]])
    now = [0.0]
    naps = []

    def sleep(s):
        naps.append(s)
        now[0] += s

    assert doctor.wait_listener(10, check=lambda: next(answers), sleep=sleep, clock=lambda: now[0]) == [("ok", "polling")]
    assert naps == [0.5, 0.5]
    late = doctor.wait_listener(1, check=lambda: [("FAIL", "not running")], sleep=sleep, clock=lambda: now[0])
    assert late == [("FAIL", "not running")]
    once = []
    doctor.wait_listener(0, check=lambda: once.append(1) or [("FAIL", "x")], sleep=sleep, clock=lambda: now[0])
    assert once == [1]


# --- setup's closing message ------------------------------------------------------------------------------

def _setup(tmp_path, monkeypatch, report, listener, **kw):
    lines = []
    monkeypatch.setattr(setup, "install_step", lambda *a: report)
    waited = []
    monkeypatch.setattr(doctor, "wait_listener", lambda timeout, restarted=False, old_run=None: waited.append(timeout) or listener)
    monkeypatch.setattr(doctor, "collect", lambda bot=None, listener=None: list(listener or []) + [("ok", "fine")])
    code = setup.run(bot=SetupBot(), owner_id=4242, language="en", interactive=False, say=lines.append,
                     env={"CTB_BOT_TOKEN": GOOD_TOKEN}, settings_file=tmp_path / "claude-settings.json", **kw)
    out = "\n".join(lines)
    return code, out, waited


def _sections(out):
    head, tail = out.split("\nChecked:")[1].split("Not checked:")
    return head, tail


def test_setup_lists_the_running_listener_only_after_the_health_check_saw_it(tmp_path, monkeypatch):
    report = I.InstallReport(settings=tmp_path / "claude-settings.json", hooks=True, running=["listener"])
    code, out, waited = _setup(tmp_path, monkeypatch, report, [("ok", "listener is running and polling Telegram")])
    checked, unchecked = _sections(out)
    assert code == 0 and waited == [setup.LISTENER_WAIT]
    assert "the health check: listener is running and polling Telegram" in checked
    assert "a test message was sent" in checked and "hooks are in" in checked and "(mode 600)" in checked
    assert "a Claude Code session reaches your chat" in unchecked
    assert "Setup finished." in out and "All set" not in out


def test_setup_with_a_service_that_did_not_start_fails_and_says_so(tmp_path, monkeypatch):
    report = I.InstallReport(settings=tmp_path / "claude-settings.json", hooks=True, failed=["listener"])
    code, out, waited = _setup(tmp_path, monkeypatch, report, [("FAIL", "listener is not running")])
    checked, unchecked = _sections(out)
    assert code == 1 and waited == [0]  # nothing started, so no waiting for it
    assert "[FAIL] listener is not running" in out  # the summary and the exit code rest on the same check
    assert "not running: listener" in unchecked and "listener is not running" in unchecked
    assert "health check" not in checked and "Setup finished" not in out


def test_setup_without_doctor_says_the_listener_was_not_checked(tmp_path, monkeypatch):
    report = I.InstallReport(settings=tmp_path / "claude-settings.json", hooks=True, running=["listener"])
    code, out, waited = _setup(tmp_path, monkeypatch, report, None, run_doctor=False, test_message=False)
    checked, unchecked = _sections(out)
    assert code == 1 and waited == []  # a started listener nobody checked is not a full success
    assert "Setup finished" not in out
    assert "that the listener reaches Telegram: run harnessgram doctor" in unchecked
    assert "the test message was skipped" in unchecked
    assert "background services loaded by launchd" in checked or "active in systemd" in checked


# --- the listener's health belongs to the listener this install started (review of #221) ----------------------

import fcntl
import json
import time
from pathlib import Path

from ctb import config as C


@pytest.fixture
def running_listener():
    """The listener's lock held (as a running listener holds it) and a way to write its status file."""
    base = C.state_dir()
    base.mkdir(parents=True, exist_ok=True)
    fh = open(base / "updates.lock", "a")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def status(at, state="listening", run=None):
        row = {"state": state, "at": at}
        if run:
            row["run"] = run
        (base / "listener.status.json").write_text(json.dumps(row))
    yield status
    fh.close()


def test_only_a_status_from_another_run_counts_after_a_restart(running_listener):
    now = time.time()
    running_listener(now, run="111-old")  # the previous listener: fresh, but its own run id
    [(level, msg)] = doctor.check_listener(restarted=True, old_run="111-old")
    assert level == "warn" and "since it was started" in msg
    assert doctor.check_listener()[0][0] == "ok"  # without a restart any fresh status is fine
    running_listener(now - 60, run="222-new")  # the new one, whatever the clock says (no time comparison)
    assert doctor.check_listener(restarted=True, old_run="111-old")[0][0] == "ok"
    running_listener(now, run=None)  # a status without a run id (an older listener) never vouches for a restart
    assert doctor.check_listener(restarted=True, old_run=None)[0][0] == "warn"


def test_the_listener_writes_a_run_id_of_its_own():
    from ctb import inbox
    source = Path(inbox.__file__).read_text(encoding="utf-8")
    assert 'run_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"' in source
    assert source.count('"run": run_id') == 2  # in the "listening" and in the "backoff" status


def test_health_waits_only_for_a_listener_this_install_started_and_shares_one_result(monkeypatch):
    seen = {}

    def wait(timeout, restarted=False, old_run=None):
        seen["wait"] = (timeout, restarted, old_run)
        return [("ok", "listener is running and polling Telegram")]

    def collect(bot=None, listener=None):
        seen["collect"] = listener
        return list(listener)

    monkeypatch.setattr(doctor, "wait_listener", wait)
    monkeypatch.setattr(doctor, "collect", collect)
    # only a job failed: the listener itself was started, so it is waited for (review: it was not before)
    report = I.InstallReport(settings=None, hooks=True, running=["listener"], failed=["daily"], old_run="9-abc")
    listener, results = setup.health(report, bot=None, say=lambda s: None, wait=7)
    assert seen["wait"] == (7, True, "9-abc") and seen["collect"] is listener and results == listener
    setup.health(I.InstallReport(settings=None, hooks=True, failed=["listener"]), bot=None, say=lambda s: None)
    assert seen["wait"] == (0, False, None)
    setup.health(None, bot=None, say=lambda s: None)
    assert seen["wait"] == (0, False, None)


def _status_file():
    status = C.state_dir() / "listener.status.json"
    status.parent.mkdir(parents=True, exist_ok=True)
    return status


def test_macos_reads_the_old_run_id_after_the_old_listener_stopped(svc, tmp_path, monkeypatch):
    """The old listener may still write while it stops: that last id is read, after bootout, before bootstrap."""
    home, calls, fail = svc
    status = _status_file()
    status.write_text(json.dumps({"state": "listening", "at": 1, "run": "77-old"}))
    real = I._sh

    def run(*cmd):
        if cmd[:2] == ("launchctl", "bootout") and cmd[2].endswith(I.LABEL):
            status.write_text(json.dumps({"state": "listening", "at": 2, "run": "77-old-late"}))  # during the stop
        if cmd[:2] == ("launchctl", "bootstrap") and cmd[3].endswith(f"{I.LABEL}.plist"):
            status.write_text(json.dumps({"state": "listening", "at": 3, "run": "88-new"}))  # the new one starts
        return real(*cmd)

    platform(monkeypatch, "darwin")
    monkeypatch.setattr(I, "_sh", run)
    report = I.run_install(settings_file=tmp_path / "s.json")
    assert report.old_run == "77-old-late"  # neither the id from before the stop nor the new one


def test_linux_stops_the_listener_reads_the_id_then_restarts(svc, tmp_path, monkeypatch):
    home, calls, fail = svc
    status = _status_file()
    status.write_text(json.dumps({"state": "listening", "at": 1, "run": "77-old"}))
    real = I._sh

    def run(*cmd):
        if cmd[2:3] == ("stop",):
            status.write_text(json.dumps({"state": "listening", "at": 2, "run": "77-old-late"}))  # during the stop
        if cmd[2:3] == ("restart",):
            status.write_text(json.dumps({"state": "listening", "at": 3, "run": "88-new"}))
        return real(*cmd)

    platform(monkeypatch, "linux")
    monkeypatch.setattr(I, "_sh", run)
    report = I.run_install(settings_file=tmp_path / "s.json")
    assert report.old_run == "77-old-late" and report.running == ["listener", "dashboard", "heartbeat", "daily"]
    steps = [c[2] for c in calls if c[2] in ("stop", "restart")]
    assert steps == ["stop", "restart"]
