"""The PATH of the background services: built in one place, existing folders only, the essentials always."""
import os
import plistlib
import shutil

import pytest

from ctb import config as C, doctor, install as I, radio


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "svc-home"
    home.mkdir()
    monkeypatch.setattr(I, "HOME", home)
    return home


def test_user_tool_folders_are_included_when_they_exist(home, tmp_path):
    (home / ".local" / "bin").mkdir(parents=True)
    uv_bin, pipx_bin = tmp_path / "uv-bin", tmp_path / "pipx-bin"
    uv_bin.mkdir()
    pipx_bin.mkdir()
    path = I.service_path(env={"UV_TOOL_BIN_DIR": str(uv_bin), "PIPX_BIN_DIR": str(pipx_bin), "PATH": ""}).split(":")
    assert str(home / ".local" / "bin") in path and str(uv_bin) in path and str(pipx_bin) in path
    assert path[-4:] == list(I.ESSENTIAL_PATH)


def test_missing_and_relative_folders_are_left_out_and_nothing_repeats(home, tmp_path):
    shell_bin = tmp_path / "shell-bin"
    shell_bin.mkdir()
    env = {"PATH": os.pathsep.join([str(shell_bin), "relative/bin", ".", str(tmp_path / "gone"), "/usr/bin",
                                    str(shell_bin) + "/"]),
           "UV_TOOL_BIN_DIR": str(tmp_path / "not-there")}
    path = I.service_path(env=env).split(":")
    assert str(shell_bin) in path and path.count(str(shell_bin)) == 1
    assert "relative/bin" not in path and "." not in path
    assert str(tmp_path / "gone") not in path and str(tmp_path / "not-there") not in path
    assert path.count("/usr/bin") == 1 and len(path) == len(set(path))
    assert str(home / ".local" / "bin") not in path  # it does not exist in this home


def test_the_shell_path_keeps_its_order_and_missing_tool_folders_follow_in_a_fixed_order(home, monkeypatch):
    real_isdir = os.path.isdir
    monkeypatch.setattr(I.os.path, "isdir",
                        lambda d: d in ("/opt/homebrew/bin", "/usr/local/bin") or real_isdir(d))
    path = I.service_path(env={"PATH": "/usr/bin:/usr/local/bin"}).split(":")
    assert path[:2] == ["/usr/bin", "/usr/local/bin"]  # the terminal's order first
    assert path.index("/usr/local/bin") < path.index("/opt/homebrew/bin")  # appended, not put in front
    for d in I.ESSENTIAL_PATH:
        assert d in path


def test_the_same_program_wins_in_the_service_as_in_the_terminal(home, tmp_path):
    """Two programs with the same name: the service must start the one the installing terminal finds."""
    first, second = tmp_path / "terminal bin", tmp_path / "uv tools"
    for folder in (first, second):
        folder.mkdir()
        tool = folder / "sample-tool"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
    env = {"PATH": f"{first}:/usr/bin", "UV_TOOL_BIN_DIR": str(second)}
    path = I.service_path(env=env)
    assert shutil.which("sample-tool", path=env["PATH"]) == str(first / "sample-tool")
    assert shutil.which("sample-tool", path=path) == str(first / "sample-tool")
    assert str(second) in path.split(":")  # still there, after the terminal's folders


def test_default_uv_tool_folder_follows_xdg_data_home(home, tmp_path):
    data = tmp_path / "xdg" / "share"
    data.mkdir(parents=True)
    (tmp_path / "xdg" / "bin").mkdir()
    assert str(tmp_path / "xdg" / "bin") in I.service_path(env={"XDG_DATA_HOME": str(data)}).split(":")


def test_every_launch_agent_gets_the_shared_path(home, monkeypatch):
    monkeypatch.setattr(I, "service_path", lambda extra=(), env=None: "/a:/usr/bin:/bin")
    for svc in I.SERVICES:
        assert plistlib.loads(I.plist_bytes(svc))["EnvironmentVariables"]["PATH"] == "/a:/usr/bin:/bin"


def test_radio_uses_the_same_builder_and_keeps_configured_folders_first(home):
    path = radio.job_path(["/opt/cloud/bin"]).split(":")
    assert path[0] == "/opt/cloud/bin" and set(I.ESSENTIAL_PATH) <= set(path) and len(path) == len(set(path))


def _plist(home, path):
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / f"{I.LABEL}.plist").write_bytes(plistlib.dumps({"Label": I.LABEL, "EnvironmentVariables": {"PATH": path}}))


def test_doctor_warns_when_the_service_cannot_find_what_the_terminal_finds(home):
    _plist(home, "/usr/bin:/bin")
    cfg = C.Config(bot_token_command=["secret-tool", "lookup"])
    terminal = {"tmux": "/opt/homebrew/bin/tmux", "secret-tool": "/opt/tools/secret-tool"}

    def which(name, path=None):
        return terminal.get(name) if path is None else None

    [(level, msg)] = doctor.check_service_path(cfg, platform="darwin", which=which)
    assert level == "warn" and "tmux" in msg and "secret-tool" in msg and "harnessgram install" in msg


def test_doctor_is_silent_when_the_service_finds_everything_or_off_macos(home):
    _plist(home, "/opt/homebrew/bin:/usr/bin:/bin")

    def which(name, path=None):
        return "/opt/homebrew/bin/tmux" if name == "tmux" else None

    assert doctor.check_service_path(C.Config(), platform="darwin", which=which) == []
    assert doctor.check_service_path(C.Config(), platform="linux", which=lambda n, path=None: None) == []


@pytest.mark.parametrize("host", ["linux", "darwin"])
def test_doctor_honours_the_platform_it_is_given_on_any_host(home, monkeypatch, host):
    """CI regression: on a Linux host the darwin check read the systemd paths and found no plist."""
    monkeypatch.setattr(I.sys, "platform", host)
    _plist(home, "/usr/bin:/bin")

    def which(name, path=None):
        return "/opt/homebrew/bin/tmux" if name == "tmux" and path is None else None

    [(level, msg)] = doctor.check_service_path(C.Config(), platform="darwin", which=which)
    assert level == "warn" and "tmux" in msg
    assert I.service_files("listener", platform="darwin") == [home / "Library" / "LaunchAgents" / f"{I.LABEL}.plist"]
    assert I.service_files("listener", platform="linux")[0].name == "claude-telegram-bridge.service"
    assert doctor.check_service_path(C.Config(), platform="linux", which=which) == []
