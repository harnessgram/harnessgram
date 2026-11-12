import os
import json
import plistlib
import shlex
from pathlib import Path

from ctb import install as I

REAL_SH = I._sh  # before any fixture swaps in a recording runner


def cmds(settings, event):
    return [h["command"] for e in settings["hooks"][event] for h in e["hooks"]]


def assert_launcher(args, sub):
    assert args[-1] == sub
    launcher = args[:-1]
    assert ((len(launcher) >= 3 and launcher[-2:] == ["-m", "ctb"])
            or (len(launcher) == 1 and Path(launcher[0]).is_absolute()
                and Path(launcher[0]).name == "ctb"))


def test_merge_into_empty_settings():
    new, changes = I.merge_settings({})
    stop_commands = cmds(new, "Stop")
    assert len(stop_commands) == 2  # zip(strict=True) needs Python 3.10; the package supports 3.9
    for command, sub in zip(stop_commands, ("hook-stop", "hook-wake")):
        assert_launcher(shlex.split(command), sub)
    assert_launcher(shlex.split(cmds(new, "UserPromptSubmit")[0]), "hook-prompt")
    post = new["hooks"]["PostToolUse"][0]["hooks"][0]
    assert_launcher(shlex.split(post["command"]), "hook-post")
    assert post["timeout"] == 30
    wake = new["hooks"]["Stop"][1]["hooks"][0]
    assert wake["async"] is True and wake["asyncRewake"] is True
    assert all("added" in c for c in changes)


def test_merge_is_idempotent_and_preserves_other_hooks():
    other = {"hooks": [{"type": "command", "command": "echo mine"}]}
    settings = {"theme": "dark", "hooks": {"Stop": [other], "PreToolUse": [other]}}
    once, _ = I.merge_settings(settings)
    twice, changes = I.merge_settings(once)
    assert twice == once
    assert all("unchanged" in c for c in changes)
    assert once["theme"] == "dark" and once["hooks"]["PreToolUse"][0] == other  # existing hooks stay first, untouched
    ask = once["hooks"]["PreToolUse"][1]
    assert ask["matcher"] == "AskUserQuestion" and len(once["hooks"]["PreToolUse"]) == 2
    assert_launcher(shlex.split(ask["hooks"][0]["command"]), "hook-ask") and ask["hooks"][0]["timeout"] <= 5
    assert once["hooks"]["Stop"][0] == other
    assert settings == {"theme": "dark", "hooks": {"Stop": [other], "PreToolUse": [other]}}  # input not mutated


def test_old_python_path_is_replaced_not_duplicated():
    stale = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/old/python -m ctb hook-stop"}]}]}}
    new, changes = I.merge_settings(stale, wake=False)
    assert len(cmds(new, "Stop")) == 1 and "/old/python" not in cmds(new, "Stop")[0]
    assert any("updated" in c for c in changes)


def test_no_wake_option():
    new, _ = I.merge_settings({}, wake=False)
    assert len(new["hooks"]["Stop"]) == 1


def test_remove_only_ours():
    other = {"hooks": [{"type": "command", "command": "echo mine"}]}
    installed, _ = I.merge_settings({"hooks": {"Stop": [other]}})
    cleaned, changes = I.remove_settings(installed)
    assert cleaned == {"hooks": {"Stop": [other]}}
    assert changes
    assert I.remove_settings({}) == ({}, [])


def test_dry_run_writes_nothing(tmp_path, capsys):
    path = tmp_path / "claude" / "settings.json"
    assert I.install(dry_run=True, settings_file=path) == 0
    out = capsys.readouterr().out
    assert "[dry-run] would" in out and "hook-stop" in out
    assert not path.exists()
    assert not list(tmp_path.glob("**/*.bak-ctb-*"))


def test_install_writes_backup_and_merges(tmp_path, capsys):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model": "x"}))
    assert I.install(service=False, settings_file=path) == 0
    data = json.loads(path.read_text())
    assert data["model"] == "x" and "Stop" in data["hooks"]
    backups = list(tmp_path.glob("settings.json.bak-ctb-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == {"model": "x"}
    assert "backup" in capsys.readouterr().out


def test_invalid_settings_json_is_left_alone(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    try:
        I.install(service=False, settings_file=path)
        assert False, "expected SystemExit"
    except SystemExit:
        pass
    assert path.read_text() == "{not json"


def test_plist_is_valid_and_runs_serve():
    data = plistlib.loads(I.plist_bytes())
    assert data["Label"] == I.LABEL and data["KeepAlive"] is True
    assert data["AbandonProcessGroup"] is True  # a restart must not kill sessions the listener started
    assert_launcher(data["ProgramArguments"], "serve")


def test_uninstall_removes_the_post_hook_too():
    installed, _ = I.merge_settings({})
    assert "PostToolUse" in installed["hooks"]
    assert I.remove_settings(installed)[0] == {}


def test_console_scripts_share_entry_point():
    """`harnessgram` is the command; `lingoline` and `ctb` stay as aliases of the same entry point."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^name = "harnessgram"$', text, re.M)
    block = text.split("[project.scripts]")[1].split("\n[")[0]
    scripts = dict(re.findall(r'^(\w+) = "([^"]+)"$', block, re.M))
    assert scripts == {"harnessgram": "ctb.cli:main", "lingoline": "ctb.cli:main", "ctb": "ctb.cli:main"}
    assert "github.com/harnessgram/harnessgram" in text and "OWNER/" not in text  # the public repository


# --- scheduled jobs next to the listener ------------------------------------------------------------------

import pytest


@pytest.fixture
def svc_env(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "_uninstall_radios", lambda dry_run: True)
    """A private home and a recording runner: no test ever calls launchctl or systemctl."""
    home = tmp_path / "svc-home"
    home.mkdir()
    monkeypatch.setattr(I, "HOME", home)
    calls = []
    monkeypatch.setattr(I, "_sh", lambda *cmd: calls.append(cmd) or 0)
    return home, calls


def mac(monkeypatch):
    monkeypatch.setattr(I.sys, "platform", "darwin")


def linux(monkeypatch):
    monkeypatch.setattr(I.sys, "platform", "linux")


def test_labels_and_arguments():
    names = {s.name: s for s in I.SERVICES}
    assert list(names) == ["listener", "dashboard", "heartbeat", "daily"]
    assert [names[n].label for n in names] == ["com.claude-telegram-bridge." + n for n in names]
    assert names["listener"].label == I.LABEL


def test_dashboard_plist_runs_every_minute():
    data = plistlib.loads(I.plist_bytes("dashboard"))
    assert data["Label"] == "com.claude-telegram-bridge.dashboard"
    assert data["StartInterval"] == 60 and "KeepAlive" not in data
    assert_launcher(data["ProgramArguments"], "dashboard")
    assert data["EnvironmentVariables"]["CTB_CONFIG_DIR"] and data["EnvironmentVariables"]["CTB_STATE_DIR"]


def test_heartbeat_plist_is_kept_alive():
    data = plistlib.loads(I.plist_bytes("heartbeat"))
    assert data["Label"].endswith(".heartbeat") and data["KeepAlive"] is True and data["RunAtLoad"] is True
    assert "StartInterval" not in data and "AbandonProcessGroup" not in data
    assert_launcher(data["ProgramArguments"], "heartbeat")


def test_daily_plist_runs_at_eight():
    data = plistlib.loads(I.plist_bytes("daily"))
    assert data["Label"].endswith(".daily")
    assert data["StartCalendarInterval"] == {"Hour": 8, "Minute": 0}
    assert not data.get("RunAtLoad") and "KeepAlive" not in data
    assert data["ProgramArguments"][-2:] == ["dashboard", "--daily"]


def test_systemd_units_for_the_jobs():
    dash_service, dash_timer = I.unit_text("dashboard"), I.timer_text("dashboard")
    assert "Type=oneshot" in dash_service and " dashboard\n" in dash_service
    assert "OnUnitActiveSec=60" in dash_timer and "WantedBy=timers.target" in dash_timer
    assert "Environment=CTB_CONFIG_DIR=" in dash_service and "Environment=CTB_STATE_DIR=" in dash_service
    heartbeat = I.unit_text("heartbeat")
    assert "Restart=always" in heartbeat and "ExecStart=" in heartbeat and " heartbeat\n" in heartbeat
    assert "WantedBy=default.target" in heartbeat
    assert "OnCalendar=*-*-* 08:00:00" in I.timer_text("daily")
    assert " dashboard --daily\n" in I.unit_text("daily")
    assert "Restart=always" in I.unit_text() and " serve\n" in I.unit_text()  # the listener is unchanged


def test_install_writes_four_launch_agents_and_replaces_running_ones(svc_env, tmp_path, monkeypatch, capsys):
    home, calls = svc_env
    mac(monkeypatch)
    assert I.install(settings_file=tmp_path / "s.json") == 0
    agents = home / "Library" / "LaunchAgents"
    names = sorted(p.name for p in agents.iterdir())
    assert names == sorted(f"com.claude-telegram-bridge.{n}.plist" for n in ("listener", "dashboard", "heartbeat", "daily"))
    assert not list(agents.glob("*.ctb-tmp"))  # atomic: no temp file is left behind
    assert plistlib.loads((agents / "com.claude-telegram-bridge.dashboard.plist").read_bytes())["StartInterval"] == 60
    # each one is booted out first (clean replacement) and then bootstrapped
    for s in I.SERVICES:
        out = calls.index(("launchctl", "bootout", f"gui/{os.getuid()}/{s.label}"))
        assert calls[out + 1][:3] == ("launchctl", "bootstrap", f"gui/{os.getuid()}")
        assert calls[out + 1][3].endswith(f"{s.label}.plist")
    # a second install overwrites in place
    (agents / "com.claude-telegram-bridge.daily.plist").write_bytes(b"stale")
    I.install(settings_file=tmp_path / "s.json")
    assert plistlib.loads((agents / "com.claude-telegram-bridge.daily.plist").read_bytes())["StartCalendarInterval"]


def test_install_no_schedules_and_no_start(svc_env, tmp_path, monkeypatch):
    home, calls = svc_env
    mac(monkeypatch)
    I.install(settings_file=tmp_path / "s.json", schedules=False, start=False)
    assert [p.name for p in (home / "Library" / "LaunchAgents").iterdir()] == ["com.claude-telegram-bridge.listener.plist"]
    assert calls == []


def test_install_dry_run_writes_no_service_files(svc_env, tmp_path, monkeypatch, capsys):
    home, calls = svc_env
    mac(monkeypatch)
    I.install(dry_run=True, settings_file=tmp_path / "s.json")
    out = capsys.readouterr().out
    assert "Dashboard service" in out and "Heartbeat service" in out and "Daily service" in out
    assert not (home / "Library").exists() and calls == []


def test_install_on_linux_writes_units_and_timers(svc_env, tmp_path, monkeypatch, capsys):
    home, calls = svc_env
    linux(monkeypatch)
    I.install(settings_file=tmp_path / "s.json")
    base = home / ".config" / "systemd" / "user"
    assert sorted(p.name for p in base.iterdir()) == sorted([
        "claude-telegram-bridge.service", "claude-telegram-bridge-dashboard.service",
        "claude-telegram-bridge-dashboard.timer", "claude-telegram-bridge-heartbeat.service",
        "claude-telegram-bridge-daily.service", "claude-telegram-bridge-daily.timer"])
    assert "OnCalendar=*-*-* 08:00:00" in (base / "claude-telegram-bridge-daily.timer").read_text()
    out = capsys.readouterr().out
    assert "claude-telegram-bridge-dashboard.timer" in out and "claude-telegram-bridge-heartbeat.service" in out
    # written, then reloaded, enabled and started, then each one checked
    units = ("claude-telegram-bridge.service", "claude-telegram-bridge-dashboard.timer",
             "claude-telegram-bridge-heartbeat.service", "claude-telegram-bridge-daily.timer")
    assert calls[:5] == [("systemctl", "--user", "show-environment"), ("systemctl", "--user", "daemon-reload"),
                         ("systemctl", "--user", "enable") + units,
                         ("systemctl", "--user", "stop", "claude-telegram-bridge.service"),  # stop, read, start
                         ("systemctl", "--user", "restart") + units]  # a re-install replaces running units
    assert calls[5:] == [("systemctl", "--user", "is-active", "--quiet", u) for u in units]
    for step in ("daemon-reload", "enable", "stop", "restart"):
        assert f"  systemctl --user {step}: ok" in out.splitlines()
    assert all(f"  {u}: active" in out.splitlines() for u in units) and "loginctl enable-linger" in out


def _runner(calls, fail):
    def run(*cmd):
        calls.append(cmd)
        return next((code for match, code in fail.items() if match in cmd), 0)
    return run


def test_linux_without_a_systemd_user_session_starts_nothing_and_says_so(svc_env, tmp_path, monkeypatch, capsys):
    home, calls = svc_env
    linux(monkeypatch)
    monkeypatch.setattr(I, "_sh", _runner(calls, {"show-environment": 1}))
    I.install(settings_file=tmp_path / "s.json")
    out = capsys.readouterr().out
    assert calls == [("systemctl", "--user", "show-environment")]  # nothing else was tried
    assert "Not started" in out and "written" in out
    lines = out.splitlines()
    assert ("    " + I._start_command(I.SERVICES)) in lines  # the exact command, on its own line
    assert "restart" in I._start_command(I.SERVICES) and "--now" not in I._start_command(I.SERVICES)
    assert "    harnessgram serve" in lines
    assert len(list((home / ".config" / "systemd" / "user").iterdir())) == 6


def test_linux_without_systemctl_at_all(svc_env, tmp_path, monkeypatch, capsys):
    home, calls = svc_env
    linux(monkeypatch)

    real_run = I.subprocess.run

    def missing(cmd, *a, **k):
        if cmd and cmd[0] == "systemctl":
            raise FileNotFoundError(cmd[0])
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(I, "_sh", REAL_SH)
    monkeypatch.setattr(I.subprocess, "run", missing)
    I.install(settings_file=tmp_path / "s.json")  # the real _sh: a missing systemctl is "not available"
    assert "Not started" in capsys.readouterr().out


def test_linux_unit_that_does_not_come_up_is_named_with_a_restart_fix(svc_env, tmp_path, monkeypatch, capsys):
    home, calls = svc_env
    linux(monkeypatch)
    down = "claude-telegram-bridge-heartbeat.service"
    monkeypatch.setattr(I, "_sh", lambda *cmd: calls.append(cmd) or (3 if cmd[2:] == ("is-active", "--quiet", down)
                                                                     else 0))
    report = I.run_install(settings_file=tmp_path / "s.json")
    out = capsys.readouterr().out.splitlines()
    assert f"  {down}: NOT active" in out and "  claude-telegram-bridge.service: active" in out  # measured, each
    assert f"    systemctl --user status {down}" in out
    assert f"    systemctl --user restart {down}" in out  # replaces it; `enable --now` would not
    assert report.failed == ["heartbeat"] and "heartbeat" not in report.running and report.code == 1
    assert not any("loginctl" in line for line in out)


def test_linux_failed_step_is_reported_on_its_own_and_running_units_are_not_called_replaced(
        svc_env, tmp_path, monkeypatch, capsys):
    home, calls = svc_env
    linux(monkeypatch)
    monkeypatch.setattr(I, "_sh", lambda *cmd: calls.append(cmd) or (1 if cmd[2:3] == ("enable",) else 0))
    report = I.run_install(settings_file=tmp_path / "s.json")
    out = capsys.readouterr().out.splitlines()
    assert "  systemctl --user daemon-reload: ok" in out and "  systemctl --user enable: failed (exit 1)" in out
    assert not any(c[2:3] == ("restart",) for c in calls)  # nothing restarted onto a half-applied state
    # each unit's measured state is shown, and none is counted as replaced
    assert "  claude-telegram-bridge.service: active, but not restarted, so it may still run the previous version" in out
    assert report.running == [] and len(report.failed) == 4 and report.code == 1
    assert ("    " + I._start_command(I.SERVICES)) in out


def test_linux_no_start_writes_only(svc_env, tmp_path, monkeypatch):
    home, calls = svc_env
    linux(monkeypatch)
    I.install(settings_file=tmp_path / "s.json", start=False)
    assert calls == []


def test_uninstall_removes_every_service(svc_env, tmp_path, monkeypatch):
    home, calls = svc_env
    mac(monkeypatch)
    I.install(settings_file=tmp_path / "s.json", start=False)
    calls.clear()
    I.uninstall(settings_file=tmp_path / "s.json")
    assert list((home / "Library" / "LaunchAgents").iterdir()) == []
    assert {c[2] for c in calls} == {f"gui/{os.getuid()}/{s.label}" for s in I.SERVICES}
    assert all(c[:2] == ("launchctl", "bootout") for c in calls)


def test_uninstall_on_linux_disables_timers_and_removes_files(svc_env, tmp_path, monkeypatch):
    home, calls = svc_env
    linux(monkeypatch)
    I.install(settings_file=tmp_path / "s.json")
    calls.clear()
    I.uninstall(settings_file=tmp_path / "s.json")
    assert list((home / ".config" / "systemd" / "user").iterdir()) == []
    assert ("systemctl", "--user", "disable", "--now", "claude-telegram-bridge-daily.timer") in calls
    assert ("systemctl", "--user", "disable", "--now", "claude-telegram-bridge-heartbeat.service") in calls
    assert calls[-1] == ("systemctl", "--user", "daemon-reload")


def test_uninstall_dry_run_keeps_the_files(svc_env, tmp_path, monkeypatch, capsys):
    home, calls = svc_env
    mac(monkeypatch)
    I.install(settings_file=tmp_path / "s.json", start=False)
    I.uninstall(dry_run=True, settings_file=tmp_path / "s.json")
    assert len(list((home / "Library" / "LaunchAgents").iterdir())) == 4 and calls == []


def test_doctor_reports_each_service(svc_env, tmp_path, monkeypatch):
    from ctb import doctor
    home, calls = svc_env
    mac(monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    I.install(settings_file=I.settings_path(), start=False)
    results = doctor.check_hooks()
    jobs = [r for r in results if "file" in r[1]]
    assert [r[0] for r in jobs] == ["ok"] * 4
    assert any("dashboard job file" in r[1] for r in jobs) and any("heartbeat job file" in r[1] for r in jobs)
    (home / "Library" / "LaunchAgents" / "com.claude-telegram-bridge.heartbeat.plist").unlink()
    (home / "Library" / "LaunchAgents" / "com.claude-telegram-bridge.listener.plist").unlink()
    results = doctor.check_hooks()
    missing = [r for r in results if "missing" in r[1]]
    assert len(missing) == 2 and all(r[0] == "warn" and "harnessgram install" in r[1] for r in missing)
    assert any("heartbeat job file" in r[1] for r in missing)


def test_ask_hook_is_additive_beside_rtk_and_codex_hooks():
    rtk = {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}
    guard = {"matcher": "Edit|Write|NotebookEdit", "hooks": [{"type": "command", "command": "python3 /opt/guards/edit-guard.py"}]}
    settings = {"hooks": {"PreToolUse": [rtk, guard]}}
    new, _ = I.merge_settings(settings)
    assert new["hooks"]["PreToolUse"][:2] == [rtk, guard] and len(new["hooks"]["PreToolUse"]) == 3
    removed, _ = I.remove_settings(new)
    assert removed == settings  # uninstall takes only ours
    assert I.is_hook_command(new["hooks"]["PreToolUse"][2]["hooks"][0]["command"], "hook-ask")


def test_doctor_checks_the_ask_hook():
    import inspect
    from ctb import doctor
    assert '"hook-ask"' in inspect.getsource(doctor)
