"""2026-11-02: a session at its 5-hour limit had one deliberate sleeping background shell, so every limit move and
every manual move was refused for ever ("background work is running, waiting"). Now a manual move, and a limit move
that has waited BG_WAIT_MAX, choose "1. Exit and stop tasks" in Claude's exit dialog (only once the strict detector
confirms it, never "2"), remember the stopped commands, and tell the session about them in the continue nudge."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import accounts, dialogs, inbox as I, recover, usage
from ctb.config import Config

SID = "sess-longbg-1"
FIX = Path(__file__).parent / "fixtures" / "cli2.1.314"
DIALOG = (FIX / "exit_background.txt").read_text()
IDLE = "❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
LIMIT = {"id": "L1", "kind": "five_hour", "reset": 9_999_999_999}


class Said(list):
    def __call__(self, text, **kw):
        self.append(text)


class ExitPane:
    """Claude answering /exit with the dialog; "1" ends the process (back at the shell), Escape closes the dialog."""

    def __init__(self, digit_confirms=True, slow=0):
        self.calls, self.showing, self.typed, self.exited = [], False, False, False
        self.slow, self.polls = slow, 0  # `slow`: screen reads after "1" that still show the dialog before it exits
        self.digit_confirms, self.cursor_on_1 = digit_confirms, False

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "has-session":
            return SimpleNamespace(returncode=0, stdout="")
        if args[0] == "display":
            return SimpleNamespace(returncode=0, stdout="2.1.314\n")
        if args[0] == "capture-pane":
            if self.slow and self.cursor_on_1 and not self.exited:
                self.polls += 1
                if self.polls > self.slow:
                    self.exited, self.showing = True, False
            return SimpleNamespace(returncode=0, stdout=DIALOG if self.showing else IDLE)
        if args[0] == "send-keys":
            key = args[-1]
            self.typed = key == "/exit" or (self.typed and key not in ("Escape", "1"))
            if key == "Enter" and self.showing:
                self.exited, self.showing = self.cursor_on_1, False
            elif key == "Enter" and self.typed:
                self.showing = True
            if key == "1" and self.showing:
                self.cursor_on_1 = True
                if self.digit_confirms and not self.slow:
                    self.exited, self.showing = True, False
            if key == "Escape":
                self.showing, self.typed = False, False
        return SimpleNamespace(returncode=0, stdout="")

    def keys(self):
        return [c[-1] for c in self.calls if c[0] == "send-keys"]


@pytest.fixture
def move(monkeypatch, tmp_path):
    accounts.add("small")
    accounts.record(SID, "main")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    log = []
    monkeypatch.setattr(I, "poll_log", log.append)
    monkeypatch.setattr(I.identity, "check", lambda *a, **k: None)
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)
    monkeypatch.setattr(I, "_resume_dialog_pending", lambda p: False)
    monkeypatch.setattr(I, "_idle_after_move", lambda *a: None)
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **k: None)
    monkeypatch.setattr(I, "_stop_hung_claude", lambda *a, **k: pytest.fail("a dialog is not a hung process"))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    monkeypatch.setattr(usage, "transcript_of", lambda s: transcript)
    return SimpleNamespace(log=log, transcript=transcript)


def run(monkeypatch, pane, **kw):
    monkeypatch.setattr(I, "_tmux", pane)
    monkeypatch.setattr(I, "_pane_command", lambda p: "zsh" if pane.exited else "2.1.314")
    said = Said()
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language="en")
    return I._account_move(cfg, FakeBot(), SID, "small", said, **kw), said


def bash_launch(tool_id, task, command):
    return [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tool_id, "name": "Bash", "input": {
            "command": command, "run_in_background": True}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content":
            f"Command running in background with ID: {task}"}]}}]


def write_rows(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_the_dialog_rows_are_read():
    assert dialogs.exit_background_tasks(DIALOG) == ["sleep 600", "sleep 601"]
    assert dialogs.exit_background_tasks("shell · sleep 600\n" + IDLE) == []   # not the live dialog


def test_a_manual_move_stops_the_tasks_with_option_one_and_moves(monkeypatch, move):
    pane = ExitPane()
    ok, said = run(monkeypatch, pane)
    keys = pane.keys()
    assert ok is True
    assert "1" in keys and "2" not in keys and keys.index("1") > keys.index("/exit")
    assert not any(k == "Escape" for k in keys[keys.index("1"):])
    assert I.t("account_move_bg_stopped", lang="en") in said
    assert I.bg_stopped(SID) == ["sleep 600", "sleep 601"]
    assert not any("refused" in l or "did not exit" in l for l in move.log)


def test_a_digit_that_only_moves_the_cursor_is_confirmed_with_enter(monkeypatch, move):
    pane = ExitPane(digit_confirms=False)
    ok, _ = run(monkeypatch, pane)
    keys = pane.keys()
    assert ok is True and keys[keys.index("1") + 1] == "Enter" and "2" not in keys


def test_only_the_strict_dialog_is_ever_answered(monkeypatch, move):
    pane = ExitPane()
    monkeypatch.setattr(I, "_tmux", pane)
    assert I._answer_exit_dialog("%1", SID) is None    # the screen is an idle prompt: nothing typed
    assert "1" not in pane.keys()


def test_the_full_command_from_the_transcript_replaces_a_cut_dialog_row(monkeypatch, move):
    long = "./pipeline/wait-and-narrate.sh --until tomorrow-morning --story ep07 --voice flow"
    write_rows(move.transcript, bash_launch("tu1", "bg1", long) + bash_launch("tu2", "bg2", "sleep 601"))
    monkeypatch.setattr(dialogs, "exit_background_tasks", lambda body: ["./pipeline/wait-and-narrate.sh --until tom…", "sleep 601"])
    ok, _ = run(monkeypatch, ExitPane())
    assert ok is True and I.bg_stopped(SID) == [long, "sleep 601"]


def test_a_finished_background_command_is_not_listed(move):
    rows = bash_launch("tu1", "bg1", "sleep 5") + bash_launch("tu2", "bg2", "sleep 9")
    rows.append({"type": "queue-operation", "content": "<task-id>bg1</task-id><status>completed</status>"})
    write_rows(move.transcript, rows)
    assert usage.background_commands(str(move.transcript)) == ["sleep 9"]


def test_the_continue_nudge_names_the_stopped_commands(move):
    assert I.resume_text(SID) == I.RESUME_TEXT
    I._save_bg_stopped(SID, ["./wait.sh --until morning"], True)
    text = I.resume_text(SID)
    assert text.startswith(I.RESUME_TEXT) and "./wait.sh --until morning" in text and "restart them" in text
    I._save_bg_stopped(SID, ["sleep 600"], False)      # a manual move: no usage limit in the sentence
    assert "usage limit" not in I.resume_text(SID) and "sleep 600" in I.resume_text(SID)


def test_a_limit_move_still_waits_before_the_bound_and_goes_after_it(monkeypatch, move):
    pane = ExitPane()
    ok, said = run(monkeypatch, pane, defer=True, limit=LIMIT)
    assert ok is False and "1" not in pane.keys() and pane.keys()[-1] == "Escape"
    assert said == [I.t("account_move_waits", lang="en")]
    wait = accounts.deferred_move(SID)

    # still inside the bound: the retry waits again
    monkeypatch.setattr(I.time, "time", lambda: wait["since"] + I.BG_WAIT_MAX - 5)
    pane2 = ExitPane()
    assert run(monkeypatch, pane2, defer=True, limit=LIMIT)[0] is False and "1" not in pane2.keys()

    # past the bound: the dialog is answered with option 1
    monkeypatch.setattr(I.time, "time", lambda: wait["since"] + I.BG_WAIT_MAX + 5)
    pane3 = ExitPane()
    ok, said = run(monkeypatch, pane3, defer=True, limit=LIMIT)
    assert ok is True and "1" in pane3.keys() and "2" not in pane3.keys()
    assert I.bg_stopped(SID) == ["sleep 600", "sleep 601"]
    assert accounts.deferred_move(SID) is None


def test_a_rebalance_still_never_stops_work(monkeypatch, move):
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a, **k: True)
    pane = ExitPane()
    ok, _ = run(monkeypatch, pane, idle_only=True)
    assert ok is False and "1" not in pane.keys() and pane.keys()[-1] == "Escape"


def test_the_dialog_still_drawn_after_option_one_is_waited_out_not_refused(monkeypatch, move):
    pane = ExitPane(slow=3)
    ok, said = run(monkeypatch, pane)
    assert ok is True and "Escape" not in pane.keys()[pane.keys().index("1"):]
    assert not any("refused" in l or "still shown" in l for l in move.log)
    assert I.t("account_move_busy_bg", lang="en") not in said


def test_the_retry_of_an_overdue_limit_move_skips_the_quiet_check(monkeypatch, move):
    accounts.defer_move(SID, "small", origin="main", limit=LIMIT)
    wait = accounts.deferred_move(SID)
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a, **k: False)   # the long job never lets it be quiet
    monkeypatch.setattr(accounts, "restart_is_safe_or_gone", lambda *a, **k: False)
    monkeypatch.setattr(accounts, "fallback_plan", lambda s, n=None: {"fresh": ["small"]})
    seen = []
    monkeypatch.setattr(recover, "_same_hung_process", lambda sess, w, busy_ok=False: seen.append(busy_ok) or True)
    spawned = []

    def retry(at):
        return recover._retry_deferred(None, None, SID, 5, at, spawned.append, {"moved": None}, LIMIT)
    assert retry(wait["tried"] + accounts.DEFER_RETRY + 1) is True and spawned == []          # young: still waits
    late = wait["since"] + I.BG_WAIT_MAX + 1
    assert retry(late) is True and spawned and spawned[0][:2] == ["account", "auto-move"]
    assert seen == [True]                                # the window's process is still checked (as busy, not hung)


def test_replayed_messages_do_not_hide_the_stopped_commands(monkeypatch, move):
    I._save_bg_stopped(SID, ["./wait.sh"], True)
    typed = []
    monkeypatch.setattr(I, "_prompt_state", lambda pane: "ready")
    monkeypatch.setattr(I, "_write_pending", lambda *a: None)
    monkeypatch.setattr(I, "_nudge_without_waiter", lambda *a, **k: pytest.fail("replay path taken"))
    monkeypatch.setattr("ctb.autopilot.type_into", lambda pane, text: typed.append(text))
    monkeypatch.setattr(usage, "prompt_since", lambda *a, **k: True)
    assert I._send_resume(SID, "%1", {"replayed": 2, "at": 0}) == "taken"
    assert typed and "./wait.sh" in typed[0]
    assert I.bg_stopped(SID) == []                      # used: gone


def test_a_new_move_forgets_a_list_left_by_an_earlier_one(monkeypatch, move):
    I._save_bg_stopped(SID, ["old job"], True)
    pane = ExitPane()
    pane.showing = False
    monkeypatch.setattr(I, "_tmux", pane)
    monkeypatch.setattr(I, "_pane_command", lambda p: "zsh")   # exits at once, no dialog
    I._account_move(Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language="en"), FakeBot(), SID, "small", Said())
    assert I.bg_stopped(SID) == []


def test_a_dialog_listing_ten_tasks_is_recognised_and_parsed():
    big = (FIX / "exit_background_10.txt").read_text()
    assert dialogs.exit_background_screen(big) is True
    assert dialogs.exit_background_tasks(big) == [f"sleep {600 + i}" for i in range(10)]
    assert dialogs.exit_background_screen(big + "  ⏸ manual mode on\n") is False      # still strict
    assert dialogs.exit_background_screen(big.replace("   ❯ 1. Exit", "   ❯ 1. Exit").replace("Background work is running", "I said: Background work is running") ) is False
