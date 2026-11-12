"""No test can reach the machine's live tmux server or start a real claude (2026-11-10: a reopen thread left by
test_auto_close ran `tmux new-session ... claude --resume dead-sess-1` on the live server after teardown)."""
from __future__ import annotations

import os
import pwd
import subprocess
import threading

import pytest

from conftest import LiveProcessBlocked, expect_blocked, refusal
from ctb import inbox as I


@pytest.fixture
def tmux_installed(monkeypatch):
    """The guard is what refuses here, not a missing binary (the suite may run with no tmux on PATH)."""
    import shutil
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/local/bin/tmux" if name == "tmux" else real(name, *a, **k))


def test_tmux_is_refused_without_the_opt_in(tmux_installed):
    with expect_blocked() as seen:
        with pytest.raises(LiveProcessBlocked):
            subprocess.run(["tmux", "list-sessions"], capture_output=True)
        with pytest.raises(OSError):  # the code under test sees "tmux unavailable"
            I._tmux("has-session", "-t", "=tg-anything")
    assert [v["why"] for v in seen] == ["tmux without the private_tmux fixture"] * 2


def test_a_real_claude_is_never_started_even_with_the_opt_in(private_tmux):
    with expect_blocked() as seen:
        for cmd in (["claude", "--version"], ["/usr/local/bin/claude", "-p", "x"],
                    ["/bin/zsh", "-lic", "claude --chrome --remote-control --resume s1"],
                    "sh -c 'tmux new-session -d'"):
            with pytest.raises(LiveProcessBlocked):
                subprocess.Popen(cmd, shell=isinstance(cmd, str))
    assert len(seen) == 4


def test_the_opt_in_still_needs_a_private_server(private_tmux, tmp_path, monkeypatch):
    uid = os.getuid()
    monkeypatch.setenv("TMUX_TMPDIR", "/tmp")
    assert refusal(["tmux", "list-sessions"]) == "tmux on a server that is not private"
    assert refusal(["tmux", "-S", f"/tmp/tmux-{uid}/default", "kill-server"]) == "tmux on a server that is not private"
    assert refusal(["tmux", "-L", "default", "ls"]) == "tmux on a server that is not private"
    monkeypatch.delenv("TMUX_TMPDIR")
    assert refusal(["tmux", "ls"]) == "tmux on a server that is not private"  # unset falls back to /tmp
    assert refusal(["tmux", "-S", str(tmp_path / "sock"), "ls"]) == ""
    assert refusal(["tmux", "-S", "/tmp/ct12345/sock", "kill-server"]) == ""  # a private socket under /tmp
    assert refusal(["tmux", "ls"], env={"TMUX_TMPDIR": str(tmp_path)}) == ""


def test_what_a_per_test_restore_goes_back_to_is_the_sandbox(session_sandbox):
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    assert os.environ["HOME"] != real_home
    for var in ("CTB_STATE_DIR", "CTB_CONFIG_DIR", "TMUX_TMPDIR", "HOME"):
        assert os.environ[var].startswith(str(session_sandbox.parent)), var  # this test's own dirs
        assert (session_sandbox / var.lower()).is_dir(), var  # the baseline teardown restores
    with pytest.MonkeyPatch.context() as mp:  # what a test's teardown does: back to the value before it
        mp.delenv("TMUX_TMPDIR")
    assert os.environ["TMUX_TMPDIR"].startswith(str(session_sandbox.parent))
    assert "CLAUDE_CODE_SESSION_ID" not in os.environ and "TMUX" not in os.environ


def test_a_thread_that_reopens_a_session_never_reaches_tmux(tmux_installed):
    """The leak path itself: resume_session from a background thread is refused and recorded."""
    out = {}
    with expect_blocked() as seen:
        t = threading.Thread(target=lambda: out.update(ok=I.resume_session("dead-sess-1")), name="leaked-reopen")
        t.start()
        t.join(10)
    assert out == {"ok": False}
    assert seen and all(v["thread"] == "leaked-reopen" for v in seen)
    assert not any("new-session" in v["cmd"] for v in seen)  # stopped at the first tmux call


def test_review_of_149_tmux_claude_paths_and_subcommand_flags(private_tmux, tmp_path):
    from pathlib import Path
    private = {"TMUX_TMPDIR": str(tmp_path)}
    # A tmux session that would start claude is refused even on a private server.
    assert refusal(["tmux", "new-session", "-d", "-s", "tg-x", "/bin/zsh", "-lic", "claude --resume s1"],
                   env=private) == "a tmux session that starts claude"
    assert refusal(["tmux", "send-keys", "-t", "p", "/usr/local/bin/claude &", "Enter"], env=private) \
        == "a tmux session that starts claude"
    fake = tmp_path / "prog" / "claude" / "claude"  # a fake program a test built: not the real claude
    assert refusal(["tmux", "send-keys", "-t", "p", f"{fake} &", "Enter"], env=private) == ""
    # -L / -S after the command are its own flags (a pane move, typed text), not a socket.
    assert refusal(["tmux", "select-pane", "-L"], env=private) == ""
    assert refusal(["tmux", "send-keys", "-t", "p", "-l", "-S"], env=private) == ""
    assert refusal(["tmux", "-L", "default", "ls"], env=private) == ""  # "default" under a private TMUX_TMPDIR
    # Popen also takes a single path.
    assert refusal(Path("/usr/bin/true")) == ""
    assert refusal(Path("/usr/local/bin/claude")) == "a real claude"


def test_review_of_161_a_path_with_spaces_and_claude_before_punctuation(private_tmux, tmp_path):
    from pathlib import Path
    private = {"TMUX_TMPDIR": str(tmp_path)}
    assert refusal(Path("/Applications/Claude Code/claude")) == "a real claude"  # one path, spaces inside
    assert refusal(Path("/opt/My Tools/bin/true")) == ""
    for line in ("claude;", "claude&", "(claude)", "claude|tee log", '"/usr/local/bin/claude" --resume s1',
                 "'claude'"):
        assert refusal(["tmux", "send-keys", "-t", "p", line, "Enter"], env=private) == "a tmux session that starts claude", line
    fake = tmp_path / "bin" / "claude"
    assert refusal(["tmux", "send-keys", "-t", "p", f'"{fake}";', "Enter"], env=private) == ""
    assert refusal(["tmux", "send-keys", "-t", "p", "claudette; preclaude", "Enter"], env=private) == ""
