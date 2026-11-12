"""The suite must never reach the machine's live tmux server (tmux 3.7 falls back to it when TMUX_TMPDIR is
missing)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_the_fixture_gives_a_private_existing_tmux_dir(tmp_path):
    folder = Path(os.environ["TMUX_TMPDIR"])
    assert folder.is_dir() and str(folder).startswith(str(tmp_path))
    assert "TMUX" not in os.environ and "TMUX_PANE" not in os.environ


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_list_sessions_under_the_fixture_shows_none(private_tmux):
    res = subprocess.run([shutil.which("tmux"), "list-sessions"], capture_output=True, text=True, timeout=10)
    assert res.stdout.strip() == "", "reached a tmux server that is not private: " + res.stdout[:80]


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_a_command_to_a_missing_session_fails_instead_of_reaching_a_live_one(private_tmux):
    res = subprocess.run([shutil.which("tmux"), "send-keys", "-t", "tg-a4ab56b1", "-l", "x"],
                         capture_output=True, text=True, timeout=10)
    assert res.returncode != 0


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_tests_never_see_the_live_tmux_server(tmp_path, private_tmux):
    assert "TMUX" not in os.environ and "TMUX_PANE" not in os.environ
    private = os.environ["TMUX_TMPDIR"]
    assert os.path.isdir(private) and private.startswith(str(tmp_path))
    r = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True, env=os.environ, timeout=10)
    assert r.stdout.strip() == "" and r.returncode != 0  # "no server running": the private directory is empty
