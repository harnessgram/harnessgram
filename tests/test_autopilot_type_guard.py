"""Autopilot types only into an empty, ready box of the verified session, and presses Enter only while that same
Claude process still owns the pane. Every tmux pane and process reader here is a fake: no test reaches tmux,
ps, claude or the real session records."""
import subprocess
from types import SimpleNamespace

import pytest

from ctb import autopilot
from ctb import inbox as I
from test_heads_up_notice import Pane

SID = "5a1b2c3d-0000-4000-8000-0000000000cc"
TEXT = "[Autopilot] continue"


class Procs:
    """The process table as the bridge reads it: the session record's pid and the pane's foreground Claude."""

    def __init__(self, monkeypatch, pid=100, start="Wed Nov 18 13:00:00 2026"):
        self.pid, self.start, self.foreground = pid, start, True
        monkeypatch.setattr(I, "session_pid", lambda session: self.pid if session == SID else None)
        monkeypatch.setattr(I, "_pane_claude_targets",
                            lambda pane: [(self.pid, self.start)] if self.foreground and pane == "%1" else [])

    def swap(self, pid=None, start=None):
        self.pid = self.pid if pid is None else pid
        self.start = self.start if start is None else start


class SwappingPane(Pane):
    """A fake pane whose process is replaced right after the literal text was typed, before Enter."""

    def __init__(self, procs, **kwargs):
        super().__init__(tip="", **kwargs)
        self.procs, self.swap_to = procs, None

    def __call__(self, *args):
        out = super().__call__(*args)
        if self.swap_to and args[0] == "send-keys" and "-l" in args:
            self.procs.swap(**self.swap_to)
        return out


@pytest.fixture
def procs(monkeypatch):
    return Procs(monkeypatch)


@pytest.fixture
def pane(monkeypatch, procs):
    p = SwappingPane(procs)
    monkeypatch.setattr(I, "_tmux", p)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    return p


def keys(p):
    return [c for c in p.calls if c[0] == "send-keys"]


def enters(p):
    return [c for c in keys(p) if "Enter" in c]


def poll_log_text():
    path = I.private_dir(I.state_dir()) / "poll.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_happy_path_types_into_the_empty_box_and_sends(pane):
    autopilot.type_into("%1", TEXT, SID)
    assert pane.sent == [TEXT] and pane.box == ""
    assert len(enters(pane)) == 1


@pytest.mark.parametrize("draft", ["my half-written reply ", "x"])
def test_a_non_empty_box_is_refused_with_no_keystrokes(pane, draft):
    pane.box = draft
    with pytest.raises(autopilot.NotTyped):
        autopilot.type_into("%1", TEXT, SID)
    assert keys(pane) == [] and pane.sent == [] and pane.box == draft


def test_a_menu_on_screen_is_refused_with_no_keystrokes(pane, monkeypatch):
    monkeypatch.setattr(I, "input_box", lambda p: ("", False))
    with pytest.raises(autopilot.NotTyped):
        autopilot.type_into("%1", TEXT, SID)
    assert keys(pane) == []


def test_a_pane_without_the_session_process_is_refused_with_no_keystrokes(pane, procs):
    procs.foreground = False  # the pane runs a shell now
    with pytest.raises(autopilot.NotTyped):
        autopilot.type_into("%1", TEXT, SID)
    assert keys(pane) == []


@pytest.mark.parametrize("swap", [{"pid": 200}, {"start": "Wed Nov 18 13:05:00 2026"}, {"pid": 200, "start": "other"}])
def test_a_process_swapped_between_typing_and_enter_gets_no_enter(pane, swap):
    pane.swap_to = swap  # another Claude (or a reused pid) takes the pane while the text sits in the box
    with pytest.raises(OSError, match="changed before submit"):
        autopilot.type_into("%1", TEXT, SID)
    assert enters(pane) == [] and pane.sent == []
    assert pane.box == ""  # only our exact text was typed, and only that was taken back
    assert "no Enter, session process changed" in poll_log_text()


def test_the_session_record_moving_to_another_pid_gets_no_enter(pane, procs, monkeypatch):
    """The record now names a different pid while the old process still leads the pane: not the same process."""
    pane.swap_to = None
    calls = []

    def record(session):
        calls.append(session)
        return procs.pid if len(calls) <= 1 else 999
    monkeypatch.setattr(I, "session_pid", record)
    with pytest.raises(OSError):
        autopilot.type_into("%1", TEXT, SID)
    assert enters(pane) == [] and pane.sent == []


def test_a_draft_that_appears_while_typing_is_not_submitted_or_erased(pane, monkeypatch):
    real = pane.__call__
    state = {"done": False}

    def typed_over(*args):
        out = real(*args)
        if args[0] == "send-keys" and "-l" in args and not state["done"]:
            state["done"] = True
            pane.box = "owner " + pane.box  # the owner started typing at the same moment
        return out
    monkeypatch.setattr(I, "_tmux", typed_over)
    with pytest.raises(OSError, match="changed before submit"):
        autopilot.type_into("%1", TEXT, SID)
    assert enters(pane) == [] and pane.sent == []
    assert pane.box == "owner " + TEXT  # not exactly our text: nothing is removed


# --- pane_session_process / pane_runs_session read the process table, not tmux metadata -----------------------

def test_identity_is_pid_and_start_of_the_foreground_claude(procs):
    assert I.pane_session_process("%1", SID) == (100, procs.start)
    assert I.pane_runs_session("%1", SID)


@pytest.mark.parametrize("pane_id", ["", "%x", "main:0.1", "=main", "1"])
def test_only_an_exact_pane_id_is_accepted(procs, pane_id):
    assert I.pane_session_process(pane_id, SID) is None and not I.pane_runs_session(pane_id, SID)


def test_another_session_or_no_record_is_not_this_session(procs, monkeypatch):
    assert not I.pane_runs_session("%1", "another-session")
    monkeypatch.setattr(I, "session_pid", lambda s: None)
    assert not I.pane_runs_session("%1", SID)
    assert I.pane_runs_session("%1", SID, known_pid=100)  # the pid read before the record vanished
    assert not I.pane_runs_session("%1", SID, known_pid=101)


def test_a_record_pid_that_does_not_lead_the_pane_is_not_the_session(procs):
    procs.swap(pid=300)  # the record's pid and the pane's foreground leader agree...
    assert I.pane_runs_session("%1", SID)
    procs.foreground = False  # ...but a shell leads the pane: no
    assert not I.pane_runs_session("%1", SID)


def test_a_target_whose_start_time_is_unreadable_is_not_an_identity(monkeypatch, procs):
    procs.swap(start="")
    assert I.pane_session_process("%1", SID) is None


def test_unreadable_process_state_is_not_a_match(monkeypatch, procs):
    def broken(pane):
        raise subprocess.TimeoutExpired("ps", 5)
    monkeypatch.setattr(I, "_pane_claude_targets", broken)
    assert I.pane_session_process("%1", SID) is None


# --- _pane_claude_targets itself, against a fake ps ----------------------------------------------------------

def _fake_ps(monkeypatch, rows, comm, begun="Wed Nov 18 13:00:00 2026"):
    monkeypatch.setattr(I, "_tmux", lambda *a: SimpleNamespace(returncode=0, stdout="/dev/ttys042\n", stderr=""))

    def run(argv, **kw):
        if "pid=,pgid=,tpgid=,command=" in argv:
            return SimpleNamespace(stdout="\n".join(rows), returncode=0)
        pid = int(argv[argv.index("-p") + 1])
        field = argv[argv.index("-o") + 1]
        value = comm[pid] if field == "comm=" else begun
        return SimpleNamespace(stdout=value + "\n", returncode=0)
    monkeypatch.setattr(I.subprocess, "run", run)


def test_only_the_foreground_claude_leader_is_a_target(monkeypatch):
    rows = [
        "  500  500  500 -zsh",
        "  600  600  600 /Users/x/.claude/versions/2.1.322 --resume abc",
        "  601  600  600 node /Users/x/mcp-server.js",
    ]
    _fake_ps(monkeypatch, rows, {500: "-zsh", 600: "/Users/x/.claude/versions/2.1.322", 601: "node"})
    assert [pid for pid, _ in I._pane_claude_targets("%1")] == [600]


def test_a_dev_server_in_a_claude_named_folder_is_not_claude(monkeypatch):
    rows = ["  700  700  700 node /Users/x/claude-site/node_modules/.bin/vite"]
    _fake_ps(monkeypatch, rows, {700: "node"})
    assert I._pane_claude_targets("%1") == []


def test_a_background_claude_not_in_the_foreground_group_is_not_a_target(monkeypatch):
    rows = ["  800  800  500 /Users/x/.claude/versions/2.1.322 -p task"]  # tpgid names another group
    _fake_ps(monkeypatch, rows, {800: "/Users/x/.claude/versions/2.1.322"})
    assert I._pane_claude_targets("%1") == []
