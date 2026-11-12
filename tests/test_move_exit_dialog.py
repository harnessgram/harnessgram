"""2026-11-02 incident (session 0dec98e6): /exit typed into a session with two background shells made Claude
ask "Background work is running ... 1. Exit and stop tasks". The move typed /exit again into that dialog,
then reported "did not exit", and the dialog reached the owner's Telegram as a question. Now the dialog is
recognised, answered with one Escape (never 1 or 2: both lose or detach work), the move waits and retries,
and the relay does not show the dialog while a bridge move owns it."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import accounts, dialogs, inbox as I, recover
from ctb.config import Config

SID = "sess-exitdlg-1"
FIX = Path(__file__).parent / "fixtures" / "cli2.1.314"
DIALOG = (FIX / "exit_background.txt").read_text()
FOOTER = (FIX / "footer_shells.txt").read_text()          # real capture
AGENT_LIST = (FIX / "footer_agent_list.txt").read_text()  # typed from the incident
AFTER_ESCAPE = (FIX / "exit_background_after_escape.txt").read_text()
IDLE = "❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language="en")


class Said(list):
    def __call__(self, text, **kw):
        self.append(text)


class ExitPane:
    """A Claude that answers Enter with the exit dialog (when `dialog`), and goes back to its prompt on Escape."""

    def __init__(self, dialog=True, idle=IDLE):
        self.calls, self.dialog, self.idle, self.showing, self.typed = [], dialog, idle, False, False

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "has-session":
            return SimpleNamespace(returncode=0, stdout="")
        if args[0] == "display":
            return SimpleNamespace(returncode=0, stdout="2.1.314\n")
        if args[0] == "capture-pane":
            return SimpleNamespace(returncode=0, stdout=DIALOG if self.showing else self.idle)
        if args[0] == "send-keys":
            key = args[-1]
            self.typed = key == "/exit" or (self.typed and key != "Escape")
            if key == "Enter" and self.dialog and self.typed:
                self.showing = True
            if key == "Escape":
                self.showing, self.typed = False, False
        return SimpleNamespace(returncode=0, stdout="")

    def keys(self):
        return [c[-1] for c in self.calls if c[0] == "send-keys"]


@pytest.fixture
def move(monkeypatch):
    accounts.add("small")
    accounts.record(SID, "main")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "_pane_command", lambda pane: "2.1.314")
    log = []
    monkeypatch.setattr(I, "poll_log", log.append)
    killed = []
    monkeypatch.setattr(I, "_stop_hung_claude", lambda *a, **k: killed.append(a) or "2.1.314")
    monkeypatch.setattr(I.os, "kill", lambda *a: killed.append(a))
    monkeypatch.setattr(I.identity, "check", lambda *a, **k: None)
    return SimpleNamespace(log=log, killed=killed)


def run(monkeypatch, pane, **kw):
    monkeypatch.setattr(I, "_tmux", pane)
    said = Said()
    ok = I._account_move(cfg(), FakeBot(), SID, "small", said, **kw)
    return ok, said


LIMIT = {"id": "L1", "kind": "five_hour", "reset": 9_999_999_999}


# --- 1. detection ------------------------------------------------------------------------------------------

def test_the_exit_dialog_is_recognised_and_old_text_is_not():
    assert dialogs.exit_background_screen(DIALOG)
    assert not dialogs.exit_background_screen("I will tell you when Background work is running.\n❯ ")
    old = DIALOG + "\n".join(f"line {i}" for i in range(40))
    assert not dialogs.exit_background_screen(old)  # scrolled far above the bottom: history, not a question


def test_the_shells_footer_is_seen_even_with_a_long_agent_list_under_it(monkeypatch):
    monkeypatch.setattr(I, "pane_raw", lambda pane, lines=45: AGENT_LIST)
    assert I.background_work_visible("%1") is True
    monkeypatch.setattr(I, "pane_raw", lambda pane, lines=45: AGENT_LIST.replace("2 shells", "0 things"))
    assert I.background_work_visible("%1") is False


def test_the_footer_under_an_empty_prompt_box_is_seen(monkeypatch):
    """ROOT CAUSE of 2026-11-02: pane_text cuts everything from an empty prompt box down, so an idle session's
    "2 shells" footer was never read and /exit was typed into the work."""
    monkeypatch.setattr(I, "_tmux", lambda *a: SimpleNamespace(returncode=0, stdout=FOOTER))
    assert "manual mode on" not in I.pane_text("%1", 12)            # the cut that hid the footer
    assert I._pane_work_state("%1") is True and I.background_work_visible("%1") is True


def test_the_footer_with_a_left_arrow_agent_count_alone_is_not_work(monkeypatch):
    monkeypatch.setattr(I, "pane_raw", lambda pane, lines=45: "x\n  ⏵⏵ bypass permissions on · ← 47 agents\n")
    assert I.background_work_visible("%1") is False  # finished agents; live ones are read from the transcripts


def test_the_dialog_on_screen_counts_as_background_work(monkeypatch):
    monkeypatch.setattr(I, "pane_raw", lambda pane, lines=45: DIALOG)
    assert I.background_work_visible("%1") is True and I._pane_work_state("%1") is True


# --- 2. the move ---------------------------------------------------------------------------------------------

def test_a_limit_move_answers_the_dialog_with_one_escape_and_waits(monkeypatch, move):
    pane = ExitPane()
    ok, said = run(monkeypatch, pane, defer=True, limit=LIMIT)
    keys = pane.keys()
    assert ok is False
    assert keys.count("/exit") == 1                         # never typed into the dialog again
    assert "1" not in keys and "2" not in keys              # never the options that stop or detach the work
    assert keys[-1] == "Escape" and keys[-2] == "Enter"     # exactly one Escape after the dialog appeared
    assert any("background work is running, waiting" in l for l in move.log)
    assert not any("did not exit" in l for l in move.log)
    assert said == [I.t("account_move_waits", lang="en")]
    wait = accounts.deferred_move(SID)
    assert wait and wait["target"] == "small" and wait["origin"] == "main"
    assert accounts.account_for(SID) == "main"              # still recorded on the account that runs it
    assert not I.recently_moving(SID)                       # the planned-restart marks were undone
    assert move.killed == []                                # the process was not touched


def test_a_manual_move_gets_the_same_escape_and_a_busy_notice(monkeypatch, move):
    pane = ExitPane()
    ok, said = run(monkeypatch, pane)
    assert ok is False and pane.keys().count("/exit") == 1 and pane.keys()[-1] == "Escape"
    assert said == [I.t("account_move_busy_bg", lang="en")]
    assert accounts.deferred_move(SID) is None              # a manual move is not retried on its own
    assert move.killed == []


def test_the_dialog_is_never_answered_with_a_kill_even_at_a_limit(monkeypatch, move):
    monkeypatch.setattr(I, "_hung_at_limit", lambda *a, **k: pytest.fail("a dialog is not a hung process"))
    pane = ExitPane()
    ok, _ = run(monkeypatch, pane, defer=True, limit=LIMIT)
    assert ok is False and move.killed == []


def test_a_move_without_the_dialog_is_unchanged(monkeypatch, move):
    pane = ExitPane(dialog=False)
    states = iter(["2.1.314", "zsh"])
    monkeypatch.setattr(I, "_pane_command", lambda p: next(states, "zsh"))
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)
    monkeypatch.setattr(I, "_resume_dialog_pending", lambda p: False)
    monkeypatch.setattr(I, "_idle_after_move", lambda *a: None)
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **k: None)
    ok, said = run(monkeypatch, pane)
    assert ok is True and "Escape" not in pane.keys()[3:]


# --- 3. the retry --------------------------------------------------------------------------------------------

def test_the_deferred_move_fires_once_the_footer_shows_no_shells(monkeypatch, move):
    pane = ExitPane()
    run(monkeypatch, pane, defer=True, limit=LIMIT)
    assert accounts.deferred_move(SID)
    later = accounts.deferred_move(SID)["tried"] + accounts.DEFER_RETRY + 1
    monkeypatch.setattr(accounts, "restart_is_safe", lambda s, n=None, min_idle=0.0: True)
    monkeypatch.setattr(accounts, "fallback_plan", lambda s, n=None: {"fresh": ["small"]})
    spawned = []
    assert recover._retry_deferred(None, None, SID, 5, later, spawned.append, {"moved": None}, LIMIT) is True
    assert spawned and spawned[0][:2] == ["account", "auto-move"] and "--target=small" in spawned[0]

    # that auto-move now passes the footer check (no shells) and goes on to type /exit
    pane2 = ExitPane(dialog=False)
    monkeypatch.setattr(I, "_tmux", pane2)
    monkeypatch.setattr(I, "_pane_command", lambda p: "zsh")
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)
    monkeypatch.setattr(I, "_resume_dialog_pending", lambda p: False)
    monkeypatch.setattr(I, "_idle_after_move", lambda *a: None)
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **k: None)
    assert I._account_move(cfg(), FakeBot(), SID, "small", Said(), defer=True, limit=LIMIT) is True
    assert "/exit" in pane2.keys()


def test_while_the_footer_still_shows_shells_the_retry_types_nothing(monkeypatch, move):
    accounts.defer_move(SID, "small", origin="main", limit=LIMIT)
    pane = ExitPane()
    pane.idle = FOOTER
    ok, said = run(monkeypatch, pane, defer=True, limit=LIMIT)
    assert ok is False and pane.keys() == [] and said == []   # waits quietly: the owner was told once


# --- 4. the relay --------------------------------------------------------------------------------------------

def screen_for(monkeypatch, body):
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "pane_text", lambda pane, lines=45: body)


def test_the_relay_does_not_show_the_dialog_while_a_move_owns_it(monkeypatch):
    screen_for(monkeypatch, DIALOG)
    I.mark_planned_restart(SID)
    _pane, _body, d, is_open = dialogs.open_screen(SID)
    assert d is None and is_open is False
    I.clear_planned_restart(SID)


def test_the_same_dialog_is_shown_when_the_owner_typed_exit_themselves(monkeypatch):
    screen_for(monkeypatch, DIALOG)
    _pane, _body, d, is_open = dialogs.open_screen(SID)
    assert is_open is True


# --- review of PR #86: quoted text must never read as the live dialog -------------------------------------------

RULE = "─" * 60
QUOTED = "\n".join([
    "● Root cause: /exit opened Claude's dialog:",
    "  Background work is running",
    "  The following will stop when you exit:",
    "  1. Exit and stop tasks  2. Move to background and exit  3. Stay",
    "  The fix presses Escape once and defers.",
    "", "✻ Worked for 2m", RULE, "❯ ", RULE,
    "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
]) + "\n"
QUOTED_ROWS = "\n".join([
    "● The dialog reads:", "  Background work is running", "  The following will stop when you exit:",
    "  1. Exit and stop tasks", "  2. Move to background and exit", "  3. Stay", "",
    "✻ Worked for 2m", RULE, "❯ ", RULE, "  ⏵⏵ bypass permissions on · 2 shells",
]) + "\n"
QUOTED_BUSY = "\n".join([  # a reply being written: no footer yet, the box is below
    "● Quoting:", "  Background work is running", "  1. Exit and stop tasks", "  2. Move to background and exit",
    "  3. Stay", "", "✻ Thinking…", RULE, "❯ ", RULE,
]) + "\n"
PERMISSION_UNDER_QUOTE = "\n".join([
    "● Root cause: /exit opened Claude's dialog:", "  Background work is running",
    "  1. Exit and stop tasks  2. Move to background and exit  3. Stay", "",
    "Bash command", "  rm -rf build", "Do you want to proceed?", "❯ 1. Yes", "  2. No", "Esc to cancel",
]) + "\n"
QUOTE_ABOVE_FOOTER = "\n".join([
    "● The footer said: ⏵⏵ bypass permissions on · 2 shells · ← 47 agents", "", RULE, "❯ ", RULE,
    "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
]) + "\n"


@pytest.mark.parametrize("screen", [QUOTED, QUOTED_ROWS, QUOTED_BUSY, PERMISSION_UNDER_QUOTE, AFTER_ESCAPE])
def test_quoted_or_finished_dialog_text_is_not_the_live_dialog(monkeypatch, screen):
    assert dialogs.exit_background_screen(screen) is False
    monkeypatch.setattr(I, "_tmux", lambda *a: SimpleNamespace(returncode=0, stdout=screen))
    assert I._exit_dialog_pending("%1") is False


def test_the_real_capture_is_the_live_dialog_and_a_wrapped_copy_of_its_text_is_not():
    assert dialogs.exit_background_screen(DIALOG) is True and dialogs.is_exit_background_dialog(DIALOG) is True
    assert dialogs.exit_background_screen(DIALOG + "❯ \n") is False          # a prompt box under it
    assert dialogs.exit_background_screen(DIALOG + "  ⏸ manual mode on\n") is False


def test_a_quoted_footer_in_the_chat_is_not_background_work(monkeypatch):
    monkeypatch.setattr(I, "pane_raw", lambda pane, lines=45: QUOTE_ABOVE_FOOTER)
    assert I.background_work_visible("%1") is False


def test_a_move_that_only_sees_quoted_text_goes_on_normally(monkeypatch, move):
    """The quoted dialog sits in the chat of an idle session that really exits: no Escape beyond the first."""
    pane = ExitPane(dialog=False, idle=QUOTED)
    states = iter(["2.1.314", "zsh"])
    monkeypatch.setattr(I, "_pane_command", lambda p: next(states, "zsh"))
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)
    monkeypatch.setattr(I, "_resume_dialog_pending", lambda p: False)
    monkeypatch.setattr(I, "_idle_after_move", lambda *a: None)
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **k: None)
    ok, said = run(monkeypatch, pane)
    assert ok is True and pane.keys().count("Escape") == 1
    assert not any("background work is running" in l for l in move.log)


def test_the_relay_still_shows_a_permission_prompt_under_quoted_dialog_text(monkeypatch):
    screen_for(monkeypatch, PERMISSION_UNDER_QUOTE)
    I.mark_planned_restart(SID)
    _pane, _body, d, is_open = dialogs.open_screen(SID)
    I.clear_planned_restart(SID)
    assert d is not None and is_open is True


def test_the_relay_shows_a_quoted_dialog_as_before_while_a_move_runs(monkeypatch):
    screen_for(monkeypatch, QUOTED)
    I.mark_planned_restart(SID)
    a = dialogs.open_screen(SID)[3]
    I.clear_planned_restart(SID)
    assert a == dialogs.open_screen(SID)[3]  # the move changes nothing for text that is not the dialog


def test_a_dialog_with_other_options_is_never_hidden(monkeypatch):
    other = DIALOG.replace("Move to background and exit", "Do something else")
    screen_for(monkeypatch, other)
    I.mark_planned_restart(SID)
    _pane, _body, d, is_open = dialogs.open_screen(SID)
    I.clear_planned_restart(SID)
    assert is_open is True


# --- the window changes under us: Claude finished exiting while the dialog was being read -------------------------

def test_a_window_that_is_at_the_shell_when_the_dialog_is_read_continues_the_move(monkeypatch, move):
    pane = ExitPane()
    seen = []

    def cmd(p):  # claude when the dialog is first read, the shell at the re-check just before Escape
        if not pane.showing:
            return "2.1.314"
        seen.append(1)
        return "2.1.314" if len(seen) == 1 else "zsh"

    monkeypatch.setattr(I, "_pane_command", cmd)
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)
    monkeypatch.setattr(I, "_resume_dialog_pending", lambda p: False)
    monkeypatch.setattr(I, "_idle_after_move", lambda *a: None)
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **k: None)
    ok, said = run(monkeypatch, pane, defer=True, limit=LIMIT)
    keys = pane.keys()
    assert ok is True and keys.count("Escape") == 1            # only the one before /exit
    assert not any("waiting" in l for l in move.log) and accounts.deferred_move(SID) is None


# --- the retry with the real shell-children check ------------------------------------------------------------------

def test_the_retry_waits_for_real_while_a_shell_child_runs_then_fires(monkeypatch, move):
    import json, os
    sessions = Path(os.environ["HOME"]) / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    pid = os.getpid()
    (sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": SID, "status": "idle", "statusUpdatedAt": 0}))
    monkeypatch.setattr(accounts, "live_session_records", lambda: [{"pid": pid, "sessionId": SID, "status": "idle",
                                                                    "statusUpdatedAt": 0}])
    monkeypatch.setattr(accounts, "_subagent_active", lambda s, n: False)
    monkeypatch.setattr(accounts, "fallback_plan", lambda s, n=None: {"fresh": ["small"]})
    run(monkeypatch, ExitPane(), defer=True, limit=LIMIT)
    tried = accounts.deferred_move(SID)["tried"]
    later = tried + accounts.DEFER_RETRY + 1
    procs = {"out": f"{pid + 1} zsh\n", "code": 0}                  # only the process list is stubbed
    monkeypatch.setattr(accounts.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=procs["code"], stdout=procs["out"]))
    spawned = []
    assert recover._retry_deferred(None, None, SID, 5, later, spawned.append, {"moved": None}, LIMIT) is True
    assert spawned == []                                              # a shell child: not safe, nothing spawned
    procs.update(out="", code=1)                                      # pgrep: no children
    assert recover._retry_deferred(None, None, SID, 5, later, spawned.append, {"moved": None}, LIMIT) is True
    assert spawned and spawned[0][:2] == ["account", "auto-move"]
