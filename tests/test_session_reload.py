import os
import json
from types import SimpleNamespace

import pytest

from ctb import accounts, cli, inbox, session_reload as R
from ctb.config import Config

OLD = "Mon Nov  9 10:00:00 2026"
NEW = "Thu Nov 12 10:00:00 2026"


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    os.utime(settings, (R.time.mktime(R.time.strptime(NEW, "%a %b %d %H:%M:%S %Y")),) * 2)
    records = [{"sessionId": sid, "pid": pid} for sid, pid in [("idle", 1), ("busy", 2), ("current", 3), ("other", 4)]]
    pids = {r["sessionId"]: r["pid"] for r in records}
    starts = {1: OLD, 2: OLD, 3: NEW, 4: OLD, 5: NEW}
    monkeypatch.setattr(accounts, "live_session_records", lambda: records)
    monkeypatch.setattr(R, "TopicMap", lambda *a, **kw: SimpleNamespace(all_places=lambda: {s: {} for s in ("idle", "busy", "current")}))
    monkeypatch.setattr(accounts, "restart_is_safe", lambda sid: sid != "busy")
    monkeypatch.setattr(inbox, "_proc_start", lambda pid: starts.get(pid, ""))
    monkeypatch.setattr(inbox, "pane_for_session", lambda sid: "%" + sid)
    monkeypatch.setattr(inbox, "session_pid", lambda sid: pids.get(sid))
    monkeypatch.setattr(inbox, "pane_runs_session", lambda pane, sid, known_pid=None: pane in ("%" + sid, "%5") and known_pid == pids[sid])
    monkeypatch.setattr(inbox, "_pane_claude_targets", lambda pane: [(pids[pane[1:]], starts[pids[pane[1:]]])])
    monkeypatch.setattr(inbox, "pane_raw", lambda pane: "idle prompt")
    monkeypatch.setattr(inbox, "_dialog_open", lambda *a: False)
    monkeypatch.setattr(inbox, "input_box", lambda pane: ("", True))
    actions = []
    monkeypatch.setattr(inbox, "_tmux", lambda *a: actions.append(a) or SimpleNamespace(returncode=0, stdout="%5\n" if a[0] in ("new-session", "new-window") else ""))
    monkeypatch.setattr(inbox, "type_command", lambda sid, pane, cmd, **kw: actions.append((sid, pane, cmd)) or (cmd == "/exit" and pids.update({sid: None})) or "sent")
    monkeypatch.setattr(inbox, "_pane_command", lambda pane: "zsh")
    monkeypatch.setattr(inbox, "session_folder", lambda sid: str(tmp_path))
    monkeypatch.setattr(accounts, "shell_command", lambda sid, replace=False: "claude")
    monkeypatch.setattr(inbox, "_resume_dialog_pending", lambda pane: False)
    monkeypatch.setattr(inbox, "pass_trust", lambda *a: pids.update(idle=5) or True)
    monkeypatch.setattr(R.time, "sleep", lambda _: None)
    return Config(), actions, pids


def test_reload_only_stale_idle_bridge_sessions(sessions):
    cfg, actions, _ = sessions
    result = R.reload_sessions(cfg)
    assert result["reloaded"] == ["idle"]
    assert result["skipped"] == [{"session": "busy", "reason": "busy"}, {"session": "current", "reason": "current"}]
    assert actions[0] == ("idle", "%idle", "/exit")
    assert all("%busy" not in a and "%current" not in a and "%other" not in a for a in actions)
    assert any("claude --remote-control --resume idle" in " ".join(a) for a in actions if a[0] == "new-session")
    assert not any(a[0] == "send-keys" for a in actions)  # never types into a pane


def test_dry_run_lists_without_touching_panes(sessions):
    cfg, actions, _ = sessions
    result = R.reload_sessions(cfg, dry_run=True)
    assert result["would_reload"] == ["idle"] and result["reloaded"] == []
    assert actions == []


@pytest.mark.parametrize("condition", ["turn", "dialog", "draft", "unknown_box", "background", "pid_reused", "pane_reused", "foreground_replaced"])
def test_never_touches_busy_or_uncertain_pane(sessions, monkeypatch, condition):
    cfg, actions, _ = sessions
    if condition == "turn":
        monkeypatch.setattr(accounts, "restart_is_safe", lambda sid: False)
    elif condition == "dialog":
        monkeypatch.setattr(inbox, "_dialog_open", lambda *a: True)
    elif condition in ("draft", "unknown_box"):
        monkeypatch.setattr(inbox, "input_box", lambda pane: ("owner draft", True) if condition == "draft" else (None, False))
    elif condition == "background":
        monkeypatch.setattr(inbox, "_footer_shows_work", lambda *a: True)
    elif condition == "pid_reused":
        monkeypatch.setattr(inbox, "session_pid", lambda sid: 999)
    elif condition == "pane_reused":
        monkeypatch.setattr(inbox, "pane_runs_session", lambda *a, **kw: False)
    elif condition == "foreground_replaced":
        monkeypatch.setattr(inbox, "_pane_claude_targets", lambda pane: [(999, NEW)])
    assert R.reload_sessions(cfg, session="idle")["reloaded"] == []
    assert actions == []


def test_busy_between_selection_and_restart_never_gets_keys(sessions, monkeypatch):
    cfg, actions, _ = sessions
    calls = []
    monkeypatch.setattr(accounts, "restart_is_safe", lambda sid: calls.append(sid) or len(calls) == 1)
    assert R.reload_sessions(cfg, session="idle")["reloaded"] == []
    assert actions == []


def test_does_not_claim_success_without_new_process(sessions, monkeypatch):
    cfg, _, _ = sessions
    monkeypatch.setattr(inbox, "pass_trust", lambda *a: True)
    assert R.reload_sessions(cfg, session="idle")["skipped"] == [{"session": "idle", "reason": "failed"}]


def test_one_session_and_cli_selection(sessions, monkeypatch, capsys):
    cfg, actions, _ = sessions
    monkeypatch.setattr(cli.C, "load", lambda: cfg)
    assert cli.main(["sessions", "reload", "--session", "idle", "--dry-run"]) == 0
    assert '"would_reload": ["idle"]' in capsys.readouterr().out
    assert actions == []
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["sessions", "reload", "--all", "--session", "idle"])


def test_cli_obeys_mirroring_off(sessions, monkeypatch):
    cfg, actions, _ = sessions
    monkeypatch.setattr("ctb.policy.on", lambda: False)
    monkeypatch.setattr(cli.C, "load", lambda: pytest.fail("off command must not read sessions"))
    assert cli.main(["sessions", "reload", "--all"]) == 1
    assert actions == []


def test_verified_command_can_resolve_managed_account_pane(tmp_path, monkeypatch):
    # Profiles store their process records outside the default Claude config directory.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "default"))
    profile = accounts.profiles_dir() / "managed" / "sessions"
    profile.mkdir(parents=True)
    pid = os.getpid()
    (profile / f"{pid}.json").write_text(json.dumps({"sessionId": "profile-session", "pid": pid, "tmux": "bridge.0.%7"}))
    monkeypatch.setattr(inbox, "_tmux", lambda *a: SimpleNamespace(returncode=0, stdout=str(pid)))
    monkeypatch.setattr(inbox.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="1"))
    assert inbox.session_pid("profile-session") == pid
    assert inbox._pane_from_process("profile-session") == "%7"


def test_pane_that_closes_with_claude_is_reopened(sessions, monkeypatch):
    """Bridge panes run Claude directly: /exit closes the pane, and the session must come back."""
    cfg, actions, pids = sessions

    def typed(*a, **kw):
        actions.append(a)
        pids["idle"] = None  # the old process has ended with its pane
        return "sent"

    def tmux(*a):
        actions.append(a)
        return SimpleNamespace(returncode=0, stdout="%9\n" if a[0] == "new-session" else "")

    monkeypatch.setattr(inbox, "type_command", typed)
    monkeypatch.setattr(inbox, "_tmux", tmux)
    monkeypatch.setattr(inbox, "_pane_command", lambda pane: None if pids["idle"] is None else "2.1.322")
    monkeypatch.setattr(inbox, "pane_runs_session", lambda pane, sid, known_pid=None: pane in ("%" + sid, "%9") and known_pid == pids[sid])
    result = R.reload_sessions(cfg, session="idle")
    assert result["reloaded"] == ["idle"]
    opened = [a for a in actions if a[0] == "new-session"]
    assert len(opened) == 1 and opened[0][3] == "tg-idle"
    assert "claude --remote-control --resume idle" in " ".join(opened[0])
    assert not any(a[0] == "send-keys" for a in actions)


def test_pane_closed_but_process_alive_is_not_reopened(sessions, monkeypatch):
    cfg, actions, _ = sessions
    monkeypatch.setattr(inbox, "type_command", lambda *a, **kw: actions.append(a) or "sent")  # the old process stays
    monkeypatch.setattr(inbox, "_pane_command", lambda pane: None)
    assert R.reload_sessions(cfg, session="idle")["skipped"] == [{"session": "idle", "reason": "failed"}]
    assert not any(a[0] == "new-session" for a in actions)


def test_exit_dialog_is_cancelled_never_answered(sessions, monkeypatch):
    """/exit with background tasks asks to stop them: cancel (Stay), never pick an option."""
    cfg, actions, _ = sessions
    monkeypatch.setattr(inbox, "_pane_command", lambda pane: "2.1.322")
    monkeypatch.setattr(inbox, "_exit_dialog_pending", lambda pane: True)
    assert R.reload_sessions(cfg, session="idle")["skipped"] == [{"session": "idle", "reason": "background"}]
    keys = [a for a in actions if a[0] == "send-keys"]
    assert keys == [("send-keys", "-t", "%idle", "Escape")]


def test_owner_typing_in_the_shell_never_gets_keys(sessions, monkeypatch):
    """A manual pane falls back to its shell, where the owner may be typing: open a new window instead."""
    cfg, actions, _ = sessions
    monkeypatch.setattr(inbox, "_pane_command", lambda pane: "zsh")
    assert R.reload_sessions(cfg, session="idle")["reloaded"] == ["idle"]
    assert not any(a[0] == "send-keys" for a in actions)


def test_existing_tmux_session_gets_a_new_window(sessions, monkeypatch):
    cfg, actions, _ = sessions
    def tmux(*a):
        actions.append(a)
        if a[0] == "new-session":
            return SimpleNamespace(returncode=1, stdout="")  # tg-... still exists: another pane kept it
        return SimpleNamespace(returncode=0, stdout="%5\n" if a[0] == "new-window" else "")
    monkeypatch.setattr(inbox, "_tmux", tmux)
    assert R.reload_sessions(cfg, session="idle")["reloaded"] == ["idle"]
    window = [a for a in actions if a[0] == "new-window"]
    assert len(window) == 1 and window[0][4] == "tg-idle:"


def test_session_restarted_by_someone_else_is_not_opened_twice(sessions, monkeypatch):
    cfg, actions, pids = sessions
    def typed(sid, pane, cmd, **kw):
        actions.append((sid, pane, cmd))
        pids[sid] = 5  # another path already resumed it
        return "sent"
    monkeypatch.setattr(inbox, "type_command", typed)
    monkeypatch.setattr(inbox, "_pane_command", lambda pane: None)
    assert R.reload_sessions(cfg, session="idle")["reloaded"] == ["idle"]
    assert not any(a[0] in ("new-session", "new-window") for a in actions)
