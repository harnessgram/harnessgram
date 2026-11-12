"""The shell that runs session and login command lines: the user's POSIX shell, else /bin/sh."""
import os
import stat
from pathlib import Path

import pytest

from ctb import inbox, usershell as U

REAL_PROBE = U.probe  # before conftest swaps in a stand-in for each test


def fake_shell(folder: Path, name: str, executable: bool = True) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755 if executable else 0o644)
    return str(path)


@pytest.fixture
def no_account_shell(monkeypatch):
    monkeypatch.setattr(U, "_account_shell", lambda: None)


def test_shell_from_env_when_it_is_a_real_posix_shell(tmp_path, no_account_shell):
    zsh = fake_shell(tmp_path / "bin", "zsh")
    assert U.user_shell({"SHELL": zsh}) == zsh
    bash = fake_shell(tmp_path / "opt", "bash")
    assert U.user_shell({"SHELL": bash}) == bash


@pytest.mark.parametrize("value", ["", "zsh", "relative/bin/zsh", "/no/such/dir/zsh"])
def test_missing_or_relative_shell_falls_back_to_sh(value, no_account_shell):
    assert U.user_shell({"SHELL": value}) == "/bin/sh"
    assert U.user_shell({}) == "/bin/sh"


def test_not_executable_shell_falls_back(tmp_path, no_account_shell):
    assert U.user_shell({"SHELL": fake_shell(tmp_path, "zsh", executable=False)}) == "/bin/sh"


@pytest.mark.parametrize("name", ["fish", "tcsh", "nu", "python3"])
def test_non_posix_shells_fall_back_because_the_command_lines_are_posix(tmp_path, name, no_account_shell):
    assert U.user_shell({"SHELL": fake_shell(tmp_path, name)}) == "/bin/sh"


def test_account_login_shell_when_shell_is_unset(tmp_path, monkeypatch):
    """A launchd or systemd service may start the bridge without $SHELL."""
    bash = fake_shell(tmp_path, "bash")
    monkeypatch.setattr(U, "_account_shell", lambda: bash)
    assert U.user_shell({}) == bash
    zsh = fake_shell(tmp_path / "env", "zsh")
    assert U.user_shell({"SHELL": zsh}) == zsh  # $SHELL wins


def test_this_machine_keeps_its_zsh_when_zsh_is_the_login_shell():
    """No behaviour change where zsh is the user's shell (the common macOS default)."""
    if not (os.path.exists("/bin/zsh") and os.access("/bin/zsh", os.X_OK)):
        pytest.skip("no /bin/zsh here")
    assert U.user_shell({"SHELL": "/bin/zsh"}) == "/bin/zsh"


def test_fallback_is_always_present():
    assert os.access(U.FALLBACK, os.X_OK)


def test_inbox_has_no_hard_coded_zsh_and_one_builder():
    source = Path(inbox.__file__).read_text(encoding="utf-8")
    assert '"/bin/zsh"' not in source and '"-lic"' not in source and '"-lc"' not in source
    assert source.count("shell_argv(") == 6  # every session start, resume and login uses the one builder


def test_account_login_terminal_uses_the_chosen_shell(tmp_path, monkeypatch):
    sh = fake_shell(tmp_path, "bash")
    monkeypatch.setattr(U, "user_shell", lambda env=None: sh)
    tool = tmp_path / "opt tools" / "claude"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setattr(inbox.accounts, "claude_binary", lambda: str(tool))
    seen = []

    class Out:
        returncode = 0
        stderr = ""
        stdout = "https://example.invalid/oauth/authorize?x=1 Paste code here"

    monkeypatch.setattr(inbox, "_tmux", lambda *a, **k: seen.append(a) or Out())
    monkeypatch.setattr(inbox.time, "sleep", lambda s: None)
    monkeypatch.setattr(inbox.accounts, "add", lambda name: None)
    monkeypatch.setattr(inbox.accounts, "login_shell", lambda name: "true")
    monkeypatch.setattr(inbox.accounts, "login_started", lambda name, tm: None)
    said = []
    inbox.account_add(inbox.Config(), None, "account2", lambda *a, **k: said.append(a))
    [start] = [a for a in seen if a[0] == "new-session"]
    assert start[-3] == sh and start[-2] == "-lc" and start[-1].endswith("; true")
    assert f"PATH=\"$PATH\":'{tool.parent}'; export PATH;" in start[-1]


# --- the built command line really runs (review of #221: a fallback shell must still find the program) --------

import subprocess

SHELLS_HERE = [p for p in ("/bin/bash", "/bin/sh") if os.access(p, os.X_OK)]


def fake_program(folder: Path, name: str = "sample-cli", tag: str = "service", body: str = "") -> Path:
    """Records its arguments, so the test sees that it really ran (a stand-in for the real program)."""
    folder.mkdir(parents=True, exist_ok=True)
    prog = folder / name
    prog.write_text(body or f'#!/bin/sh\nprintf "%s %s" "{tag}" "$*" > "$HOME/ran"\n')
    prog.chmod(0o755)
    return prog


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """A private HOME with its own shell profile, a short PATH, and the REAL probe (conftest's stand-in is
    removed), so the probe and the launched line run in exactly the same shell environment."""
    home = tmp_path / "lab home"
    home.mkdir()
    for name, value in (("HOME", str(home)), ("PATH", "/usr/bin:/bin"), ("TERM", "dumb")):
        monkeypatch.setenv(name, value)
    for name in ("ENV", "BASH_ENV", "ZDOTDIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(U, "probe", REAL_PROBE)

    class Lab:
        def profile(self, text: str) -> None:
            (home / ".profile").write_text(text)  # read by `sh -l` and `bash -l` (no .bash_profile here)

        def run(self, argv):
            return subprocess.run(argv, env={"HOME": str(home), "PATH": "/usr/bin:/bin", "TERM": "dumb"},
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)

        def ran(self) -> str:
            return (home / "ran").read_text() if (home / "ran").exists() else ""

    lab = Lab()
    lab.home = home
    return lab


@pytest.mark.parametrize("shell", SHELLS_HERE)
def test_found_only_through_a_profile_added_path(lab, shell):
    """Like nvm: the profile puts the program's folder (here with a space) on PATH; the service never sees it."""
    fake_program(lab.home / "nvm versions" / "bin")
    lab.profile('PATH="$HOME/nvm versions/bin:$PATH"; export PATH\n')
    argv = U.shell_argv("exec sample-cli --resume s1", program="sample-cli", binary="", shell=shell)
    done = lab.run(argv)
    assert done.returncode == 0, done.stderr
    assert lab.ran() == "service --resume s1"


@pytest.mark.parametrize("shell", SHELLS_HERE)
def test_found_only_as_a_shell_function(lab, shell):
    lab.profile('samplecli() { printf "%s %s" function "$*" > "$HOME/ran"; }\n')
    argv = U.shell_argv("samplecli x", program="samplecli", binary="", shell=shell)
    done = lab.run(argv)
    assert done.returncode == 0, done.stderr
    assert lab.ran() == "function x"


@pytest.mark.parametrize("shell", SHELLS_HERE)
def test_found_through_a_wrapper_on_a_profile_path(lab, shell, tmp_path):
    real = fake_program(tmp_path / "real place", tag="real")
    fake_program(lab.home / "wrap bin", body=f'#!/bin/sh\nexec "{real}" "$@"\n')
    lab.profile('PATH="$HOME/wrap bin:$PATH"; export PATH\n')
    done = lab.run(U.shell_argv("exec sample-cli y", program="sample-cli", binary="", shell=shell))
    assert done.returncode == 0 and lab.ran() == "real y"


@pytest.mark.parametrize("shell", SHELLS_HERE)
def test_absent_is_refused_before_any_window(lab, shell):
    lab.profile("")
    assert REAL_PROBE(shell, "-lic", "", "sample-cli") is False
    with pytest.raises(U.ClaudeNotFound):
        U.shell_argv("exec sample-cli x", program="sample-cli", binary="", shell=shell)


@pytest.mark.parametrize("shell", SHELLS_HERE)
def test_a_slow_profile_does_not_block_the_launch(lab, shell, monkeypatch):
    monkeypatch.setattr(U, "PROBE_TIMEOUT", 0.5)
    lab.profile("sleep 5\n")
    assert REAL_PROBE(shell, "-lic", "", "sample-cli") is None  # could not tell
    argv = U.shell_argv("exec sample-cli x", program="sample-cli", binary="", shell=shell)  # not refused
    assert argv[0] == shell and argv[-1].endswith("exec sample-cli x")


def test_a_shell_that_cannot_run_does_not_block_either(lab, tmp_path):
    assert REAL_PROBE(str(tmp_path / "no-such-shell"), "-lic", "", "sample-cli") is None
    broken = fake_program(tmp_path / "shells", name="bash", body="#!/bin/sh\nexit 2\n")  # fails for another reason
    assert REAL_PROBE(str(broken), "-lic", "", "sample-cli") is None


@pytest.mark.parametrize("shell", SHELLS_HERE)
def test_the_folder_the_service_found_is_added_and_the_profiles_own_copy_wins(lab, shell, tmp_path):
    service = fake_program(tmp_path / "service bin")  # outside the shell's PATH
    done = lab.run(U.shell_argv("exec sample-cli a", program="sample-cli", binary=str(service), shell=shell))
    assert done.returncode == 0 and lab.ran() == "service a"
    fake_program(lab.home / "mine", tag="profile")
    lab.profile('PATH="$HOME/mine:$PATH"; export PATH\n')
    done = lab.run(U.shell_argv("exec sample-cli b", program="sample-cli", binary=str(service), shell=shell))
    assert done.returncode == 0 and lab.ran() == "profile b"


@pytest.mark.parametrize("shell", SHELLS_HERE)
def test_the_in_window_check_stays_as_a_backstop(lab, shell, monkeypatch):
    """The probe could not tell (a slow profile), and the window really lacks it: it says so and exits 127."""
    monkeypatch.setattr(U, "MISSING_PAUSE", 0)
    monkeypatch.setattr(U, "probe", lambda *a, **k: None)
    done = lab.run(U.shell_argv("exec sample-cli x", program="sample-cli", binary="", shell=shell))
    assert done.returncode == 127 and "sample-cli was not found on PATH" in done.stderr and not lab.ran()


def test_a_shell_whose_path_has_spaces_runs_the_line(lab, tmp_path, no_account_shell):
    if "/bin/bash" not in SHELLS_HERE:
        pytest.skip("no /bin/bash here")
    spaced = tmp_path / "my shells" / "bash"
    spaced.parent.mkdir()
    spaced.symlink_to("/bin/bash")
    chosen = U.user_shell({"SHELL": str(spaced)})
    assert chosen == str(spaced)
    prog = fake_program(tmp_path / "tools here")
    done = lab.run(U.shell_argv("exec sample-cli ok", program="sample-cli", binary=str(prog), shell=chosen))
    assert done.returncode == 0 and lab.ran() == "service ok"
