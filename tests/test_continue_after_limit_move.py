"""2026-11-02 15:15 (#71): session 83abc949 was moved to another account after a background agent died of
the 5-hour limit and the main turn ended on the limit text. The move worked, but nothing told the resumed
session to carry on: it sat at an empty prompt until someone nudged it by hand, and poll.log said nothing.

Root cause: the bridge waited for a "❯" in pane_text(), which cuts an empty input box away, so on a screen
whose last lines are the limit text it never saw a prompt and gave up after 60 s without a word.

Guards: readiness is read from the raw screen; a limit move (main turn or background agent, running or ended)
resumes the session exactly once; a rebalance of a really idle session types nothing; the resume question
and menus are never typed into; every move and nudge is in poll.log; a marker lets the periodic check finish
a resume the moving process could not."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from conftest import FakeBot
from ctb import accounts, inbox as I, recover, usage
from ctb.config import state_dir
from test_replay_after_move import (LIMIT, OWNER, SID, assistant, cfg, hit_limit, owner_message, user,  # noqa: F401
                                    world, write_row)

RULE = "─" * 60

# The screen of session 83abc949 after the move (2026-11-02 15:15), as tmux prints it.
FIXTURE_SCREEN = "\n".join([
    '⏺ Agent "writer" failed: Agent terminated early due to an API error: You\'ve hit your 5-hour limit',
    "",
    "⏺ You've hit your 5-hour limit · resets 5:20pm (Asia/Jerusalem)",
    "",
    "✻ Worked for 17m 10s · done 3:15 PM",
    "",
    RULE,
    "❯ ",
    RULE,
    "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
]) + "\n"

DIALOG_SCREEN = "\n".join([
    "This session is 3h old and 150k tokens.",
    "Resuming it will use about 40% of your 5-hour usage limit.",
    "",
    "❯ 1. Resume from summary",
    "  2. Start a new conversation",
]) + "\n"

MENU_SCREEN = "\n".join([
    "Bash command", "  rm -rf build", "Do you want to proceed?", "❯ 1. Yes", "  2. No", "Esc to cancel",
]) + "\n"


class Tmux:
    """tmux stand-in with a settable screen; every typed key is recorded."""

    def __init__(self, screen=FIXTURE_SCREEN):
        self.screen, self.calls = screen, []
        self.nudge = ""

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "send-keys":
            if "-l" in args and args[-1].startswith("📩"):
                self.nudge = args[-1]
                self.screen = self.screen.replace("❯ \n", "❯ " + self.nudge + "\n")
            elif args[-1] in ("Enter", "C-u") and self.nudge:
                self.screen = self.screen.replace("❯ " + self.nudge + "\n", "❯ \n")
                self.nudge = ""
        if args[0] == "display" and args[-1] == "#{pane_current_command}":
            return SimpleNamespace(returncode=0, stdout="zsh\n")
        if args[0] == "capture-pane":
            return SimpleNamespace(returncode=0, stdout=self.screen)
        return SimpleNamespace(returncode=0, stdout="")

    def typed(self):
        return [c[-1] for c in self.calls if c[0] == "send-keys" and "-l" in c]

    def continues(self):
        return [x for x in self.typed() if x.startswith("continue:")]

    def nudges(self):  # what tells a session to carry on (the move itself types /exit and the resume command)
        return [x for x in self.typed() if x.startswith(("continue:", "📩"))]

    def enters(self):
        return [c for c in self.calls if c[0] == "send-keys" and c[-1] == "Enter"]


@pytest.fixture
def screen(world, monkeypatch):
    tm = Tmux()
    monkeypatch.setattr(I, "_tmux", tm)
    monkeypatch.setattr(I, "pane_runs_session", lambda *a, **k: True)  # the fake pane hosts the session
    # the session takes what is typed; no other prompt reached the transcript (an empty needle asks for any)
    monkeypatch.setattr(usage, "prompt_since", lambda session, since, needle="": bool(needle))
    return tm


def poll_log_text():
    try:
        return (state_dir() / "poll.log").read_text()
    except OSError:
        return ""


def end_turn_normally(world):
    write_row(world, assistant("Done, all merged.", "ok"), time.time() + 1)


# --- the root cause ----------------------------------------------------------------------------

def test_the_old_ready_test_never_saw_the_empty_prompt_of_the_fixture_screen(world, screen):
    assert "❯" not in I.pane_text("%1", 12)             # pane_text cuts the empty input box away
    assert I._prompt_state("%1") == "ready"              # the raw screen shows it


def test_the_resume_question_and_menus_are_not_ready(world, screen):
    screen.screen = DIALOG_SCREEN
    assert I._prompt_state("%1") == "dialog"
    screen.screen = MENU_SCREEN
    assert I._prompt_state("%1") == "wait"
    screen.screen = FIXTURE_SCREEN.replace("❯ \n", "❯ half a draft\n")
    assert I._prompt_state("%1") == "wait"               # never send a draft along


# --- a limit move resumes the session -------------------------------------------------------------

def test_a_limit_move_types_one_continue_line_and_logs_it(world, screen):
    hit_limit(world)
    assert I.auto_move(cfg(), world.bot, SID, world.thread, "small") is True
    assert accounts.account_for(SID) == "small"
    assert len(screen.continues()) == 1 and "usage limit" in screen.continues()[0]
    assert "re-launch any background agent" in screen.continues()[0]
    log = poll_log_text()
    assert f"limit move of {SID[:8]} to small: done" in log and f"resume {SID[:8]}: typed the continue nudge" in log
    assert not I.resume_pending(SID)                      # done: nothing left to repeat


def test_a_background_agent_that_died_of_the_limit_resumes_the_session(world, screen, monkeypatch):
    """The main turn ended normally (no limit row), but a limit notice was raised for the session."""
    end_turn_normally(world)
    (state_dir() / "limit-offered").mkdir(parents=True, exist_ok=True)
    (state_dir() / "limit-offered" / SID).touch()
    monkeypatch.setattr(I.threading, "Thread", lambda target, args=(), daemon=None: SimpleNamespace(
        start=lambda: target(*args)))                    # the manual path resumes in a background thread
    I.account_command(cfg(), world.bot, {"message_thread_id": world.thread}, SID, "move small now", lambda *a, **k: None)
    assert accounts.account_for(SID) == "small"
    assert len(screen.continues()) == 1
    assert f"manual move of {SID[:8]} to small: done (a limit had stopped it)" in poll_log_text()


def test_a_manual_move_after_a_limit_with_nothing_replayed_resumes_too(world, screen, monkeypatch):
    hit_limit(world)                                      # no owner message was waiting: nothing to replay
    monkeypatch.setattr(I.threading, "Thread", lambda target, args=(), daemon=None: SimpleNamespace(
        start=lambda: target(*args)))
    I.account_command(cfg(), world.bot, {"message_thread_id": world.thread}, SID, "move small now", lambda *a, **k: None)
    assert len(screen.continues()) == 1


def test_replayed_owner_messages_resume_the_work_without_a_continue_line(world, screen):
    owner_message(world, "please fix the login bug", 701)
    hit_limit(world)
    assert I.auto_move(cfg(), world.bot, SID, world.thread, "small") is True
    assert "📩 Telegram" in screen.typed() and screen.continues() == []


def test_a_rebalance_of_a_session_that_stopped_on_the_limit_resumes_it(world, screen, monkeypatch):
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a, **k: True)
    hit_limit(world)
    assert I.auto_move(cfg(), world.bot, SID, world.thread, "small", idle=True) is True
    assert len(screen.continues()) == 1


def test_a_pure_rebalance_of_an_idle_session_types_nothing(world, screen, monkeypatch):
    end_turn_normally(world)
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    monkeypatch.setattr(usage, "transcript_of", lambda s: None)
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a, **k: True)
    assert I.auto_move(cfg(), world.bot, SID, world.thread, "small", idle=True) is True
    assert accounts.account_for(SID) == "small"
    assert screen.nudges() == [] and not I.resume_pending(SID)
    assert f"idle move of {SID[:8]} to small: done" in poll_log_text()


def test_a_rebalance_whose_last_turn_ended_normally_is_not_nudged_even_with_an_old_limit_mark(world, screen, monkeypatch):
    end_turn_normally(world)
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a, **k: True)
    mark = state_dir() / "limit-offered"
    mark.mkdir(parents=True, exist_ok=True)
    (mark / SID).touch()
    old = time.time() - 3600
    import os
    os.utime(mark / SID, (old, old))
    I.auto_move(cfg(), world.bot, SID, world.thread, "small", idle=True)
    assert screen.nudges() == []


# --- never into a dialog ---------------------------------------------------------------------------

def test_nothing_is_typed_into_the_resume_question(world, screen):
    screen.screen = DIALOG_SCREEN
    hit_limit(world)
    assert I.auto_move(cfg(), world.bot, SID, world.thread, "small") is False
    assert screen.nudges() == []
    assert I.resume_pending(SID)                          # still owed once the owner has answered
    assert I.resume_after_move(SID, wait=0) == "dialog" and screen.nudges() == []
    assert "stopped at the resume question" in poll_log_text()
    screen.screen = FIXTURE_SCREEN                        # the owner pressed Resume: the prompt is back
    assert I.resume_after_move(SID, wait=0) == "nudged"
    assert len(screen.continues()) == 1


def test_nothing_is_typed_into_a_permission_menu(world, screen):
    screen.screen = MENU_SCREEN
    I.mark_resume_pending(SID, 0, "small")
    assert I.resume_after_move(SID, wait=0) == "waiting"
    assert screen.typed() == []


# --- exactly once, and robust ---------------------------------------------------------------------------

def test_the_resume_happens_exactly_once(world, screen):
    I.mark_resume_pending(SID, 0, "small")
    assert I.resume_after_move(SID, wait=0) == "nudged"
    assert I.resume_after_move(SID, wait=0) == "none"
    assert len(screen.continues()) == 1


def test_the_start_screen_is_given_a_second_look_before_typing(world, screen):
    screens = iter(["booting...\n", FIXTURE_SCREEN, FIXTURE_SCREEN, FIXTURE_SCREEN])
    seen = []

    def tmux(*args):
        if args[0] == "capture-pane":
            seen.append(1)
            return SimpleNamespace(returncode=0, stdout=next(screens, FIXTURE_SCREEN))
        return screen(*args)
    I._tmux = tmux
    try:
        I.mark_resume_pending(SID, 0, "small")
        assert I.resume_after_move(SID, wait=30) == "nudged"
    finally:
        I._tmux = screen
    assert len(screen.continues()) == 1


def test_a_line_that_was_typed_but_not_taken_is_not_typed_again_while_it_sits_in_the_box(world, screen, monkeypatch):
    taken = iter([False, False, False, False, True])
    monkeypatch.setattr(usage, "prompt_since", lambda session, since, needle="": next(taken) if needle else False)
    box = FIXTURE_SCREEN.replace("❯ \n", "❯ " + I.RESUME_TEXT[:60] + "\n")

    def tmux(*args):
        if args[0] == "capture-pane" and any(c[:2] == ("send-keys", "-t") for c in screen.calls):
            return SimpleNamespace(returncode=0, stdout=box)
        return screen(*args)
    monkeypatch.setattr(I, "_tmux", tmux)
    I.mark_resume_pending(SID, 0, "small")
    assert I.resume_after_move(SID, wait=0) == "nudged"
    assert len(screen.continues()) == 1 and screen.enters()   # Enter was pressed, the text not repeated


def test_a_line_that_never_arrives_is_retried_then_reported(world, screen, monkeypatch):
    monkeypatch.setattr(usage, "prompt_since", lambda *a, **k: False)
    I.mark_resume_pending(SID, 0, "small")
    assert I.resume_after_move(SID, wait=0) == "waiting"      # try 1 failed, the next look is the next call
    assert I.resume_after_move(SID, wait=0) == "waiting"
    assert I.resume_after_move(SID, wait=0) == "failed"       # try 3: reported, the marker is gone
    assert len(screen.continues()) == 3
    assert not I.resume_pending(SID)
    assert I.resume_after_move(SID, wait=0) == "none"         # a 4th periodic tick types nothing
    assert len(screen.continues()) == 3
    assert "failed, the continue line was not taken after 3 tries" in poll_log_text()


def test_the_try_count_lives_in_the_marker_and_is_written_before_typing(world, screen, monkeypatch):
    seen = []
    from ctb import autopilot
    real = autopilot.type_into

    def type_into(pane, text):
        seen.append(json.loads((state_dir() / "resume-pending" / SID).read_text())["tries"])
        real(pane, text)
    monkeypatch.setattr(autopilot, "type_into", type_into)
    monkeypatch.setattr(usage, "prompt_since", lambda *a, **k: False)
    I.mark_resume_pending(SID, 0, "small")
    I.resume_after_move(SID, wait=0)
    I.resume_after_move(SID, wait=0)
    assert seen == [1, 2]


def test_a_marker_already_at_three_tries_types_nothing(world, screen):
    I.mark_resume_pending(SID, 0, "small")
    path = state_dir() / "resume-pending" / SID
    info = json.loads(path.read_text())
    path.write_text(json.dumps(dict(info, tries=3)))
    assert I.resume_after_move(SID, wait=0) == "failed"
    assert screen.typed() == [] and not I.resume_pending(SID)


def test_no_enter_while_a_menu_is_open_and_the_sent_prompt_is_visible_above(world, screen, monkeypatch):
    """The transcript does not show the prompt yet, a permission menu is open, and the sent continue line is
    still drawn with a "❯" above it: Enter would answer "1. Yes"."""
    monkeypatch.setattr(usage, "prompt_since", lambda *a, **k: False)
    menu = "\n".join(["❯ " + I.RESUME_TEXT, "", "⏺ Bash(rm -rf build)", "Bash command", "  rm -rf build",
                      "Do you want to proceed?", "❯ 1. Yes", "  2. No", "Esc to cancel"]) + "\n"
    state = {"typed": False}

    def tmux(*args):
        if args[0] == "send-keys" and "-l" in args:
            state["typed"] = True
        if args[0] == "capture-pane" and state["typed"]:
            screen.calls.append(args)
            return SimpleNamespace(returncode=0, stdout=menu)
        return screen(*args)
    monkeypatch.setattr(I, "_tmux", tmux)
    I.mark_resume_pending(SID, 0, "small")
    I.resume_after_move(SID, wait=0)
    assert len(screen.continues()) == 1
    assert [c for c in screen.enters() if c[-1] == "Enter"] and len(screen.enters()) == 1  # type_into's own only


def test_the_box_check_wants_the_last_prompt_row(world, screen):
    two = "\n".join(["❯ " + I.RESUME_TEXT, "⏺ done", RULE, "❯ ", RULE]) + "\n"
    screen.screen = two
    assert I._box_holds("%1", I.RESUME_TEXT) is False         # the sent prompt above an empty box
    screen.screen = "\n".join(["⏺ done", RULE, "❯ " + I.RESUME_TEXT, RULE]) + "\n"
    assert I._box_holds("%1", I.RESUME_TEXT) is True


def test_the_replayed_fallback_checks_the_screen_again_before_typing(world, screen, monkeypatch):
    world.box.put(SID, {"text": "hi", "at": 1, "id": 1, "source": "owner", "chat": 7})
    draft = FIXTURE_SCREEN.replace("❯ \n", "❯ my half draft\n")
    real = I._nudge_without_waiter

    def nudge(box, session, settle=0.0):
        screen.screen = draft                                  # the owner began typing, the nudge declined
        real(box, session, settle)
    monkeypatch.setattr(I, "_nudge_without_waiter", nudge)
    I.mark_resume_pending(SID, 1, "small")
    assert I.resume_after_move(SID, wait=0) == "waiting"
    assert screen.typed() == []
    assert json.loads((state_dir() / "resume-pending" / SID).read_text()).get("tries", 0) == 0  # no try used


def test_the_continue_line_is_typed_under_the_nudge_lock_and_stamps_last_nudge(world, screen, monkeypatch):
    held = []
    real = I._nudge_guard

    def guard(session):
        held.append(session)
        return real(session)
    monkeypatch.setattr(I, "_nudge_guard", guard)
    I.mark_resume_pending(SID, 0, "small")
    before = I.last_nudge(SID)
    assert I.resume_after_move(SID, wait=0) == "nudged"
    assert held == [SID] and I.last_nudge(SID) > before


def test_a_second_process_on_the_same_resume_gets_busy(world, screen):
    import fcntl
    I.mark_resume_pending(SID, 0, "small")
    with open(state_dir() / "resume-pending" / (SID + ".lock"), "a") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        assert I.resume_after_move(SID, wait=0) == "busy"
    assert screen.typed() == [] and I.resume_pending(SID)


def test_a_prompt_that_reached_the_transcript_by_other_means_drops_the_marker(world, screen, monkeypatch):
    I.mark_resume_pending(SID, 0, "small")
    monkeypatch.setattr(usage, "prompt_since", lambda session, since, needle="": True)
    assert I.resume_after_move(SID, wait=0) == "none"
    assert screen.typed() == [] and not I.resume_pending(SID)


def test_a_limit_notice_followed_by_a_real_prompt_is_not_a_stopped_session(world, screen, monkeypatch):
    end_turn_normally(world)
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    (state_dir() / "limit-offered").mkdir(parents=True, exist_ok=True)
    (state_dir() / "limit-offered" / SID).touch()
    assert I._limit_stopped_work(SID, None) is True
    monkeypatch.setattr(usage, "prompt_since", lambda session, since, needle="": True)
    assert I._limit_stopped_work(SID, None) is False


def test_the_poll_log_notes_a_waiting_resume_once_not_on_every_tick(world, screen):
    screen.screen = MENU_SCREEN
    I.mark_resume_pending(SID, 0, "small")
    for _ in range(5):
        assert I.resume_after_move(SID, wait=0) == "waiting"
    assert poll_log_text().count("no empty prompt yet") == 1
    screen.screen = DIALOG_SCREEN
    for _ in range(3):
        assert I.resume_after_move(SID, wait=0) == "dialog"
    assert poll_log_text().count("stopped at the resume question") == 1
    assert "continue:" not in poll_log_text() and I.RESUME_TEXT not in poll_log_text()


def test_a_pending_resume_expires(world, screen):
    I.mark_resume_pending(SID, 0, "small")
    path = state_dir() / "resume-pending" / SID
    old = time.time() - I.RESUME_TTL - 5
    import os
    os.utime(path, (old, old))
    data = json.loads(path.read_text())
    data["at"] = old
    path.write_text(json.dumps(data))
    assert I.resume_after_move(SID, wait=0) == "none" and screen.typed() == []


def test_the_periodic_limit_check_finishes_a_resume_the_moving_process_could_not(world, screen):
    I.mark_resume_pending(SID, 0, "small")                    # the moving process died after the move
    recover._finish_resume(SID)
    recover._finish_resume(SID)
    assert len(screen.continues()) == 1


# --- the transcript check ---------------------------------------------------------------------------------

def test_prompt_since_sees_only_real_prompts_after_the_given_time(tmp_path, monkeypatch):
    folder = tmp_path / "projects" / "-x"
    folder.mkdir(parents=True)
    monkeypatch.setattr(usage, "PROJECTS", tmp_path / "projects")
    assert usage.prompt_since("nope", 0) is None
    at = time.time()

    def row(kind, text, offset, **extra):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(at + offset)) + ".000Z"
        return json.dumps(dict({"type": kind, "timestamp": stamp, "message": {"content": text}}, **extra))
    (folder / "s1.jsonl").write_text("\n".join([
        row("user", "continue: old one", -60),
        row("user", "<task-notification>continue: x</task-notification>", 5),
        row("user", "continue: x", 6, isMeta=True),
    ]) + "\n")
    assert usage.prompt_since("s1", at, "continue:") is False
    with open(folder / "s1.jsonl", "a") as f:
        f.write(row("user", [{"type": "text", "text": "continue: the previous account hit its usage limit"}], 7) + "\n")
    assert usage.prompt_since("s1", at, "continue:") is True


def test_prompt_since_skips_rows_without_a_text_block(tmp_path, monkeypatch):
    folder = tmp_path / "projects" / "-x"
    folder.mkdir(parents=True)
    monkeypatch.setattr(usage, "PROJECTS", tmp_path / "projects")
    at = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(at + 5)) + ".000Z"
    result = {"type": "user", "timestamp": stamp,
              "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}
    (folder / "s2.jsonl").write_text(json.dumps(result) + "\n")
    assert usage.prompt_since("s2", at) is False              # an empty needle must not count a tool result


# --- machine rows are not prompts (Opus re-review: "Resume from summary" dropped the owed resume) --------------
# These use the REAL prompt_since on a real transcript file, not the fixture stub.

REAL_PROMPT_SINCE = usage.prompt_since


def _stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"


def _transcript(tmp_path, monkeypatch, rows):
    folder = tmp_path / "projects" / "-x"
    folder.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(usage, "PROJECTS", tmp_path / "projects")
    (folder / (SID + ".jsonl")).write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _marker_time():
    return json.loads((state_dir() / "resume-pending" / SID).read_text())["at"]


SUMMARY_ROW = {"type": "user", "isCompactSummary": True, "isVisibleInTranscriptOnly": True,
               "message": {"role": "user", "content": "This session is being continued from a previous conversation..."}}


def test_a_compact_summary_row_after_the_marker_does_not_drop_the_owed_resume(world, screen, monkeypatch, tmp_path):
    monkeypatch.setattr(usage, "prompt_since", REAL_PROMPT_SINCE)
    I.mark_resume_pending(SID, 0, "small")
    at = _marker_time()
    _transcript(tmp_path, monkeypatch, [
        {"type": "user", "timestamp": _stamp(at - 600), "message": {"content": "fix the bug"}},
        dict(SUMMARY_ROW, timestamp=_stamp(at + 5))])
    I.resume_after_move(SID, wait=0)
    assert len(screen.continues()) == 1


def test_a_hand_run_command_row_after_the_marker_does_not_drop_the_owed_resume(world, screen, monkeypatch, tmp_path):
    monkeypatch.setattr(usage, "prompt_since", REAL_PROMPT_SINCE)
    I.mark_resume_pending(SID, 0, "small")
    at = _marker_time()
    _transcript(tmp_path, monkeypatch, [
        {"type": "user", "timestamp": _stamp(at + 5),
         "message": {"content": "<command-name>/model</command-name>\n<command-args></command-args>"}}])
    I.resume_after_move(SID, wait=0)
    assert len(screen.continues()) == 1


def test_a_real_typed_prompt_after_the_marker_still_drops_it(world, screen, monkeypatch, tmp_path):
    monkeypatch.setattr(usage, "prompt_since", REAL_PROMPT_SINCE)
    I.mark_resume_pending(SID, 0, "small")
    at = _marker_time()
    _transcript(tmp_path, monkeypatch, [
        {"type": "user", "timestamp": _stamp(at + 5), "message": {"content": "please look at the login bug"}}])
    assert I.resume_after_move(SID, wait=0) == "none"
    assert screen.typed() == [] and not I.resume_pending(SID)


def test_only_a_compact_summary_after_the_limit_notice_still_counts_as_stopped(world, screen, monkeypatch, tmp_path):
    monkeypatch.setattr(usage, "prompt_since", REAL_PROMPT_SINCE)
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    mark = state_dir() / "limit-offered"
    mark.mkdir(parents=True, exist_ok=True)
    (mark / SID).touch()
    offered = (mark / SID).stat().st_mtime
    rows = [dict(SUMMARY_ROW, timestamp=_stamp(offered + 5)),
            {"type": "user", "timestamp": _stamp(offered + 6),
             "message": {"content": "<command-name>/compact</command-name>"}}]
    _transcript(tmp_path, monkeypatch, rows)
    assert I._limit_stopped_work(SID, None) is True
    rows.append({"type": "user", "timestamp": _stamp(offered + 7), "message": {"content": "carry on please"}})
    _transcript(tmp_path, monkeypatch, rows)
    assert I._limit_stopped_work(SID, None) is False
