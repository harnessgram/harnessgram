"""Real pane layouts must not turn a quotation or a notice into a blocking dialog."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from ctb import autopilot, dialogs, inbox, usage

FIXTURES = Path(__file__).parent / "fixtures"
EXIT = (FIXTURES / "exit-dialog-real.txt").read_text()
NOTICE = (FIXTURES / "heads-up-junk-input.txt").read_text()


def test_scheduled_task_exit_screen_and_pending_probe(monkeypatch):
    assert dialogs.exit_background_screen(EXIT)
    assert dialogs.is_exit_background_dialog(EXIT)
    monkeypatch.setattr(inbox, "pane_raw", lambda *a: EXIT)
    assert inbox._exit_dialog_pending("%1")


def test_a_long_list_of_wrapped_scheduled_tasks_is_still_a_dialog():
    task = "   scheduled    · 17 8,11 * * * · Example schedule\n   task        every session, se…\n"
    screen = EXIT.replace(EXIT[EXIT.index("   scheduled"):EXIT.index("   ❯ 1.")], task * 20 + "\n")
    assert dialogs.exit_background_screen(screen)


@pytest.mark.parametrize("fixture,stopped", [
    ("cli2.1.314/exit_background.txt", ["sleep 600", "sleep 601"]),
    ("exit-dialog-real.txt", []),
    ("exit-dialog-scheduled-mixed.txt", ["sleep 600", "Inspect the example report"]),
])
def test_exit_variants_choose_only_option_one(monkeypatch, fixture, stopped):
    screen = (FIXTURES / fixture).read_text()
    monkeypatch.setattr(inbox, "pane_raw", lambda *a: screen)
    monkeypatch.setattr(usage, "transcript_of", lambda s: None)
    sent = []
    monkeypatch.setattr(inbox, "_tmux", lambda *a: sent.append(a))
    assert dialogs.exit_background_screen(screen)
    assert dialogs.is_exit_background_dialog(screen)
    assert inbox._answer_exit_dialog("%1", "fake-session") == stopped
    assert sent == [("send-keys", "-t", "%1", "1")]


@pytest.mark.parametrize("screen", [
    (FIXTURES / "exit-dialog-scheduled-quoted.txt").read_text(),
    EXIT.replace("1. Exit and stop tasks", "1. Delete scheduled tasks"),
    EXIT.replace("1. Exit and stop tasks", "4. Exit and stop tasks"),
])
def test_exit_quotation_or_different_first_option_gets_no_key(monkeypatch, screen):
    monkeypatch.setattr(inbox, "pane_raw", lambda *a: screen)
    sent = []
    monkeypatch.setattr(inbox, "_tmux", lambda *a: sent.append(a))
    assert not dialogs.exit_background_screen(screen)
    assert inbox._answer_exit_dialog("%1", "fake-session") is None
    assert sent == []


@pytest.mark.parametrize("suffix", ["\n❯ ", "\n────────────\n❯ owner draft\n────────────",
                                    "\n  ⏵⏵ bypass permissions on", "\nThe dialog was only quoted.\nMore text.\nEnd.\nDone."])
def test_scheduled_exit_quotation_above_a_prompt_is_not_live(suffix):
    assert not dialogs.exit_background_screen(EXIT + suffix)


def test_heads_up_junk_is_input_and_not_a_dialog(monkeypatch):
    monkeypatch.setattr(inbox, "_tmux", lambda *a: SimpleNamespace(returncode=0, stdout=NOTICE))
    assert not inbox._dialog_open(NOTICE)
    assert dialogs.parse(NOTICE) is None
    box, ready = inbox.input_box("%1")
    assert ready and box.startswith("0📩 Telegram") and box.count("📩") == NOTICE.count("📩")
    assert autopilot.at_prompt("%1")


def test_heads_up_does_not_hide_a_permission_under_it():
    question = "\nDo you want to proceed?\n❯ 1. Yes\n  2. No\nEnter to confirm · Esc to cancel"
    assert inbox._dialog_open(NOTICE + question)


def test_tip_stripping_never_crosses_a_prompt_to_find_a_dismiss_key():
    body = "✦ Heads up · A notice without keys\n────────────\n❯ 0: Dismiss"
    assert inbox.strip_tip(body) == body


@pytest.mark.parametrize("boundary", ["❯ 0: Dismiss", "1. Yes\n2. No\n0: Dismiss"])
def test_tip_stripping_never_hides_question_or_prompt_rows(boundary):
    body = "✦ Heads up · A notice without keys\n" + boundary
    assert inbox.strip_tip(body) == body
