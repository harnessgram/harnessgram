import fcntl
from pathlib import Path

from ctb import heartbeat, listener_service as service
from ctb.config import Config


class Launchctl:
    def __init__(self, bootstrap_failures=0):
        self.loaded = False
        self.bootstrap_failures = bootstrap_failures
        self.calls = []

    def __call__(self, *cmd):
        self.calls.append(cmd[1:])
        action = cmd[1]
        if action == "bootout":
            self.loaded = False
            return 0, ""
        if action == "bootstrap":
            if self.bootstrap_failures:
                self.bootstrap_failures -= 1
                return 5, ""
            self.loaded = True
            return 0, ""
        return (0, "    pid = 42\n") if self.loaded else (113, "")


def test_heartbeat_listener_check_is_opt_in():
    calls = []
    assert heartbeat.run_once(Config(), now=100, listener_check=lambda: calls.append(True)) == 0
    assert calls == [True]


def test_reload_retries_bootout_bootstrap_until_listener_has_a_pid(tmp_path):
    launchctl = Launchctl(bootstrap_failures=1)
    sleeps = []
    assert service.load_and_verify(tmp_path / "listener.plist", runner=launchctl, sleep=sleeps.append)
    assert [call[0] for call in launchctl.calls].count("bootstrap") == 2
    assert "bootout" in [call[0] for call in launchctl.calls]
    assert sleeps == [0.2]


def test_heartbeat_repairs_once_and_never_while_install_lock_is_held(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service.Path, "home", classmethod(lambda cls: tmp_path))
    plist = tmp_path / "Library" / "LaunchAgents" / f"{service.LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("")
    service._last_repair = 0
    service._outage_logged = False
    launchctl = Launchctl()

    lock_path = service.private_dir(service.state_dir()) / "listener-install.lock"
    with open(lock_path, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not service.heartbeat_check(100, runner=launchctl, sleep=lambda _: None)
        assert not any(call[0] == "bootstrap" for call in launchctl.calls)
    assert service.heartbeat_check(100, runner=launchctl, sleep=lambda _: None)
    assert capsys.readouterr().err.count("attempting to reload") == 1
    assert service.heartbeat_check(101, runner=launchctl, sleep=lambda _: None)
    assert capsys.readouterr().err == ""
