"""2026-11-01 account moves: a move is reported only when the process really restarted, a refused /exit
leaves everything as it was, usage is written only to the account the process runs on, a spent pair of
accounts is one group, and a reopened session passes the resume dialog."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import accounts, inbox as I, recover, statusline, usage
from ctb.config import Config, state_dir, write_private
from ctb.topics import TopicMap

SID = "sess-truth-1"
NOW = 2_000_000.0


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language="en")


class Said(list):
    def __call__(self, text, **kw):
        self.append(text)


class StuckPane:
    """tmux stand-in for a Claude that ignores /exit (background tasks running)."""

    def __init__(self, screen="❯ \n"):
        self.calls, self.screen = [], screen

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "has-session":
            return SimpleNamespace(returncode=0, stdout="")
        if args[0] == "display":
            return SimpleNamespace(returncode=0, stdout="claude\n")
        if args[0] == "capture-pane":
            return SimpleNamespace(returncode=0, stdout=self.screen)
        return SimpleNamespace(returncode=0, stdout="")

    def keys(self):
        return [c[-1] for c in self.calls if c[0] == "send-keys"]


@pytest.fixture
def stuck(monkeypatch):
    accounts.add("small")
    accounts.record(SID, "main")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    pane = StuckPane()
    monkeypatch.setattr(I, "_tmux", pane)
    return pane


def marker():
    return state_dir() / "limit-cleared" / SID


# --- 1. no pane: nothing restarted, nothing claimed ------------------------------------------------

def test_a_move_without_a_pane_records_nothing_and_writes_no_marker(monkeypatch):
    accounts.add("small")
    accounts.record(SID, "main")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: None)
    monkeypatch.setattr(usage, "limit_of", lambda s: {"id": "err-1"})
    said = Said()
    assert I.account_move(cfg(), FakeBot(), SID, "small", said) is False
    assert accounts.account_for(SID) == "main" and len(said) == 1
    assert I.auto_move(cfg(), FakeBot(), SID, 5, "small") is False
    assert not marker().exists() and accounts.account_for(SID) == "main"


# --- 2. /exit ignored while background work runs ---------------------------------------------------

def test_a_refused_exit_restores_the_record_and_says_one_thing(stuck):
    said = Said()
    assert I.account_move(cfg(), FakeBot(), SID, "small", said, defer=True) is False
    assert accounts.account_for(SID) == "main"
    assert len(said) == 1 and "background" in said[0]
    assert stuck.keys()[-1] == "Escape"                             # left at its prompt
    assert not I.planned_restart_marker(SID).exists() and not I.recently_moving(SID)
    assert accounts.deferred_move(SID)["target"] == "small"
    again = Said()                                                 # tried again: the owner is not told twice
    assert I.account_move(cfg(), FakeBot(), SID, "small", again, defer=True) is False
    assert again == []


def test_a_refused_exit_writes_no_limit_marker(stuck, monkeypatch):
    monkeypatch.setattr(usage, "limit_of", lambda s: {"id": "err-2"})
    monkeypatch.setattr(I, "_terminal_say", lambda *a, **k: None)
    assert I.auto_move(cfg(), FakeBot(), SID, 5, "small") is False
    assert not marker().exists() and accounts.account_for(SID) == "main"


def test_background_work_on_screen_stops_a_limit_move_before_any_key_is_typed(stuck):
    stuck.screen = "output\n  ⏵⏵ bypass permissions on · 2 background tasks\n"
    said = Said()
    assert I.account_move(cfg(), FakeBot(), SID, "small", said, defer=True) is False
    assert stuck.keys() == [] and accounts.account_for(SID) == "main" and len(said) == 1
    # a move the owner forced ("stop and move") still goes ahead
    I.account_move(cfg(), FakeBot(), SID, "small", Said())
    assert "/exit" in stuck.keys()


def test_a_manual_move_that_is_refused_posts_one_message(stuck):
    said = Said()
    assert I.account_move(cfg(), FakeBot(), SID, "small", said) is False
    assert len(said) == 1 and accounts.deferred_move(SID) is None


def test_the_limit_loop_retries_a_deferred_move_when_quiet_and_not_more_than_once_a_minute(bot, monkeypatch):
    accounts.add("small")
    accounts.record(SID, "main")
    TopicMap(state_dir() / "topics.json", bot, 7).ensure(SID, "T")
    reset = NOW + 3600
    monkeypatch.setattr(usage, "limit_of", lambda s: {"id": "e", "reset": reset, "clock": "1"})
    monkeypatch.setattr(recover, "_just_moved", lambda s: False)
    box = I.Inbox(state_dir() / "inbox")
    accounts.defer_move(SID, "small", NOW - 200)
    busy = accounts.claude_home() / "sessions"  # busy = a live record of the session says so
    busy.mkdir(parents=True)
    (busy / f"{__import__('os').getpid()}.json").write_text(json.dumps(
        {"pid": __import__("os").getpid(), "sessionId": SID, "status": "busy"}))
    spawned = []
    safe = {"v": False}
    monkeypatch.setattr(accounts, "restart_is_safe", lambda s, now=None, min_idle=0.0: safe["v"])
    monkeypatch.setattr(accounts, "fallback_plan", lambda s, now=None: {"fresh": ["small"]})

    def tick(at):
        recover.limit_check(cfg(), bot=bot, now=at, inbox=box, spawn_fn=spawned.append)

    tick(NOW)
    assert spawned == []                                            # still busy: wait
    safe["v"] = True
    tick(NOW + 1)
    assert len(spawned) == 1 and "--target=small" in spawned[0][-1]
    tick(NOW + 20)
    assert len(spawned) == 1                                        # throttled
    tick(NOW + 70)
    assert len(spawned) == 2


# --- 3. usage goes to the account the process runs on ----------------------------------------------

def run_status(env, sid, five, extra=None):
    payload = {"session_id": sid, "model": {"display_name": "M"},
               "rate_limits": {"five_hour": {"used_percentage": five, "resets_at": 1793562000},
                               "seven_day": {"used_percentage": 63, "resets_at": 1793966400}}}
    payload.update(extra or {})
    statusline.run(json.dumps(payload), env)


def usage_of_file(name):
    return json.loads((state_dir() / f"claude-usage-{name}.json").read_text())


def test_a_limited_session_never_writes_its_usage_into_the_target_accounts_file():
    accounts.add("spare")
    accounts.record(SID, "spare")        # meant to be on spare, but its process still runs on main
    run_status({}, SID, 100)                # main: limited
    assert usage_of_file("main")["five_hour"] == 100
    assert not (state_dir() / "claude-usage-spare.json").exists()
    folder = accounts.config_dir("spare")
    run_status({"CLAUDE_CONFIG_DIR": str(folder)}, "other-session", 20)
    assert usage_of_file("spare")["five_hour"] == 20 and usage_of_file("main")["five_hour"] == 100


def test_the_transcript_path_names_the_profile_when_the_environment_does_not():
    accounts.add("spare")
    path = str(accounts.profiles_dir() / "spare" / "projects" / "p" / (SID + ".jsonl"))
    run_status({}, SID, 40, {"transcript_path": path})
    assert usage_of_file("spare")["five_hour"] == 40 and not (state_dir() / "claude-usage-main.json").exists()
    assert usage.claude_account(SID, env={}, use_record=False) == "main"


def test_two_accounts_do_not_end_up_with_identical_numbers():
    accounts.add("spare")
    accounts.record("sess-a", "spare")
    accounts.record("sess-b", "main")
    run_status({}, "sess-a", 100)                                   # on main, recorded as spare
    run_status({"CLAUDE_CONFIG_DIR": str(accounts.config_dir("spare"))}, "sess-b", 5)
    assert usage_of_file("main")["five_hour"] == 100
    assert usage_of_file("spare")["five_hour"] == 5


# --- 3b/4. unknown 5-hour reset, exhausted group ---------------------------------------------------

def write_usage(name, five, five_reset, weekly=10, weekly_reset=NOW + 86400):
    data = {"five_hour": five, "weekly": weekly, "weekly_resets": weekly_reset, "at": "2026-11-01T00:00:00Z"}
    if five_reset is not None:
        data["five_hour_resets"] = five_reset
    write_private(accounts.usage_path(name), json.dumps(data))


def test_a_five_hour_reading_without_a_reset_time_is_unknown_not_zero():
    accounts.add("a")
    accounts.add("b")
    write_usage("a", 90, None)
    write_usage("b", 60, NOW + 3600)
    assert accounts.used("a", NOW) >= 90 and accounts.used("b", NOW) == 60
    accounts.record(SID, "main")
    assert accounts.fallback_plan(SID, NOW)["fresh"][:2] == ["b", "a"]
    write_usage("a", 0, "")                                         # a weekly-only plan keeps counting only its week
    assert accounts.used("a", NOW) == 10


def test_accounts_that_ran_out_together_are_one_group():
    for n in ("b", "c"):
        accounts.add(n)
    accounts.record(SID, "main")
    reset = NOW + 3600
    write_usage("main", 100, reset)
    write_usage("b", 30, reset)                                     # looks free, but shares main's reset
    write_usage("c", 30, NOW + 7200)
    accounts.note_limit("main", NOW)
    accounts.note_limit("b", NOW + 3)
    accounts.note_limit("c", NOW + 3)
    plan = accounts.fallback_plan(SID, NOW + 5)
    assert "b" not in plan["fresh"] and plan["fresh"] == ["c"]
    # limits hit minutes apart are not a group
    accounts.note_limit("b", NOW - 600)
    write_usage("b", 40, reset)
    assert "b" in accounts.fallback_plan(SID, NOW + 5)["fresh"]


# --- 4. the announcement does not race the move ----------------------------------------------------

def test_the_limit_announcement_waits_while_the_session_is_being_moved(bot, monkeypatch):
    TopicMap(state_dir() / "topics.json", bot, 7).ensure(SID, "T")
    monkeypatch.setattr(usage, "limit_of", lambda s: {"id": "e", "reset": NOW + 3600, "clock": "16:30"})
    monkeypatch.setattr(recover.accounts, "auto_enabled", lambda: False)
    box = I.Inbox(state_dir() / "inbox")
    moving = {"v": True}
    monkeypatch.setattr(recover, "_just_moved", lambda s: moving["v"])
    recover.limit_check(cfg(), bot=bot, now=NOW, inbox=box)
    assert bot.methods("sendMessage") == []
    moving["v"] = False                                             # the move failed: the limit is announced
    recover.limit_check(cfg(), bot=bot, now=NOW + 5, inbox=box)
    assert len(bot.methods("sendMessage")) == 1


# --- 5. the resume dialog ---------------------------------------------------------------------------

DIALOG = ("Resuming it will use about 8% of your 5-hour usage limit.\n"
          "{c1} 1. Resume\n{c2} 2. Start a new conversation\n")


class DialogPane:
    def __init__(self, cursor=1):
        self.cursor, self.keys, self.answered = cursor, [], False

    def __call__(self, *args):
        if args[0] == "send-keys":
            self.keys.append(args[-1])
            if args[-1] == "Down":
                self.cursor = 2
            elif args[-1] == "Up":
                self.cursor = 1
            elif args[-1] == "Enter":
                self.answered = self.cursor == 1
        if args[0] == "capture-pane":
            if self.answered:
                return subprocess.CompletedProcess(args, 0, stdout="❯ \n")
            return subprocess.CompletedProcess(args, 0, stdout=DIALOG.format(
                c1="❯" if self.cursor == 1 else " ", c2="❯" if self.cursor == 2 else " "))
        if args[0] == "display":
            return subprocess.CompletedProcess(args, 0, stdout="zsh\n")
        return subprocess.CompletedProcess(args, 0, stdout="")


@pytest.mark.parametrize("cursor,keys", [(1, ["Enter"]), (2, ["Up", "Enter"])])
def test_a_reopened_session_answers_the_resume_dialog_with_resume(monkeypatch, cursor, keys):
    pane = DialogPane(cursor)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "_tmux", pane)
    assert I.resume_session(SID) is True
    assert pane.keys == keys and pane.answered


def test_a_move_answers_the_resume_dialog_too(monkeypatch):
    pane = DialogPane(1)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "_tmux", pane)
    assert I.pass_trust("%1", "/some/path", wait=3) is True
    assert pane.keys == ["Enter"] and pane.answered


# --- review round: strict dialogs, deferral lifecycle, footer-only background detection ---------------

HISTORY = ("1. Resume the deploy once the usage limit resets\n"
           "Bash command: rm -rf build\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n")


class ScreenPane:
    def __init__(self, screen, running="zsh"):
        self.screen, self.running, self.keys = screen, running, []

    def __call__(self, *args):
        if args[0] == "send-keys":
            self.keys.append(args[-1])
        if args[0] == "capture-pane":
            return subprocess.CompletedProcess(args, 0, stdout=self.screen)
        if args[0] == "display":
            return subprocess.CompletedProcess(args, 0, stdout=self.running + "\n")
        return subprocess.CompletedProcess(args, 0, stdout="")


@pytest.mark.parametrize("screen", [
    HISTORY,
    "Resuming it will use about 8% of your 5-hour usage limit.\n" + HISTORY,
    "❯ 2. Start a new conversation\n(quoted above)\nsome output\n❯ ",
    ("Resuming it will use about 8% of your 5-hour usage limit.\n1. Resume\nnoise\n2. Start a new conversation\n"
     + "\n".join(f"line {i}" for i in range(12))),
])
def test_resume_dialog_is_never_answered_from_history_or_a_permission_prompt(monkeypatch, screen):
    pane = ScreenPane(screen)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "_tmux", pane)
    assert not I.resume_dialog_shown(screen)
    assert I.answer_resume_dialog("%1") is False
    assert I.pass_trust("%1", "/x", wait=1) is True
    assert pane.keys == []                                          # no Enter, no Up, no Down


def test_resume_dialog_needs_the_cursor_on_the_resume_row_before_enter(monkeypatch):
    stuck_cursor = DIALOG.format(c1=" ", c2=" ")                    # cursor not visible anywhere
    pane = ScreenPane(stuck_cursor)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "_tmux", pane)
    assert I.answer_resume_dialog("%1") is False and pane.keys == []


def test_the_trust_dialog_in_history_is_not_answered(monkeypatch):
    screen = ("1. Yes, I trust this folder\n2. No, exit\nthe old dialog\n" + "\n".join(f"out {i}" for i in range(25))
              + "\n❯ 1. Yes\n  2. No\n")
    pane = ScreenPane(screen)
    monkeypatch.setattr(I, "_tmux", pane)
    assert I.answer_trust_dialog("%1") is False and pane.keys == []


def test_a_resume_dialog_that_cannot_be_answered_is_not_reported_as_a_trust_problem(monkeypatch):
    screen = DIALOG.format(c1=" ", c2=" ")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "_tmux", ScreenPane(screen))
    assert I.pass_trust("%1", "/x", wait=1) is True


def test_only_the_footer_status_line_counts_as_background_work(monkeypatch):
    def seen(screen):
        monkeypatch.setattr(I, "pane_raw", lambda pane, lines=45: screen)
        return I.background_work_visible("%1")

    assert not seen("I started the background tasks you asked for\n⏵⏵ bypass permissions on")
    assert not seen("run 2 shells of tests · 3 background tasks planned")
    assert seen("reply\n  ⏵⏵ bypass permissions on (shift+tab to cycle) · 2 background tasks")
    assert seen("reply\n  ? for shortcuts · 1 shell")


def test_the_deferral_is_cleared_once_the_old_process_stopped(monkeypatch):
    accounts.add("small")
    accounts.record(SID, "main")
    accounts.defer_move(SID, "small", origin="main")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: False)    # even a trust problem later keeps it cleared
    typed = []
    states = iter(["claude", "zsh"])

    def tmux(*a):
        typed.append(a)
        return SimpleNamespace(returncode=0, stdout="/x\n" if a[0] == "display" else "")

    monkeypatch.setattr(I, "_tmux", tmux)
    monkeypatch.setattr(I, "_pane_command", lambda p: next(states))
    assert I.account_move(cfg(), FakeBot(), SID, "small", Said(), defer=True) is False
    assert accounts.deferred_move(SID) is None


def test_a_successful_manual_move_clears_a_waiting_deferral(monkeypatch):
    accounts.add("small")
    accounts.record(SID, "main")
    accounts.defer_move(SID, "small", origin="main")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)
    states = iter(["zsh"])
    monkeypatch.setattr(I, "_tmux", lambda *a: SimpleNamespace(returncode=0, stdout="/x\n"))
    monkeypatch.setattr(I, "_pane_command", lambda p: next(states))
    assert I.account_move(cfg(), FakeBot(), SID, "small", Said()) is True
    assert accounts.deferred_move(SID) is None


def test_a_deferred_move_is_dropped_when_the_session_left_the_account_that_hit_the_limit(bot, monkeypatch):
    accounts.add("small")
    accounts.add("other")
    TopicMap(state_dir() / "topics.json", bot, 7).ensure(SID, "T")
    accounts.record(SID, "other")                                   # moved by hand meanwhile
    accounts.defer_move(SID, "small", NOW - 200, origin="main", limit={"id": "e", "reset": NOW + 3600})
    monkeypatch.setattr(usage, "limit_of", lambda s: {"id": "e", "reset": NOW + 3600, "clock": "1"})
    monkeypatch.setattr(recover, "_just_moved", lambda s: False)
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a, **k: True)
    monkeypatch.setattr(accounts, "fallback_plan", lambda s, now=None: {"fresh": []})
    spawned = []
    recover.limit_check(cfg(), bot=bot, now=NOW, inbox=I.Inbox(state_dir() / "inbox"), spawn_fn=spawned.append)
    assert spawned == [] and accounts.deferred_move(SID, NOW) is None


def test_a_deferral_ends_with_its_limit_and_a_new_limit_starts_a_fresh_record():
    accounts.defer_move(SID, "small", NOW, origin="main", limit={"id": "e1", "reset": NOW + 100})
    assert accounts.deferred_move(SID, NOW + 50)
    assert accounts.deferred_move(SID, NOW + 101) is None           # the limit reset
    accounts.defer_move(SID, "small", NOW, origin="main", limit={"id": "e1", "reset": NOW + 100})
    assert accounts.defer_move(SID, "small", NOW + 10, limit={"id": "e1"}) is False       # same limit: told once
    assert accounts.defer_move(SID, "small", NOW + 20, origin="main", limit={"id": "e2", "reset": NOW + 900}) is True


def test_the_pending_move_waits_for_a_move_started_from_the_limit_loop(bot, monkeypatch):
    accounts.add("small")
    accounts.record(SID, "main")
    TopicMap(state_dir() / "topics.json", bot, 7).ensure(SID, "T")
    accounts.defer_move(SID, "small", NOW - 200, origin="main")
    err = {"id": "e", "reset": NOW + 3600, "clock": "1"}
    monkeypatch.setattr(usage, "limit_of", lambda s: err)
    monkeypatch.setattr(recover, "_just_moved", lambda s: False)
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a, **k: True)
    monkeypatch.setattr(accounts, "fallback_plan", lambda s, now=None: {"fresh": ["small"]})
    write_private(state_dir() / "limit-state.json", json.dumps(
        {SID: dict(err, updated=NOW, moved="small", moved_at=NOW - 10)}))
    spawned = []
    recover.limit_check(cfg(), bot=bot, now=NOW, inbox=I.Inbox(state_dir() / "inbox"), spawn_fn=spawned.append)
    assert spawned == []                                            # that move has not reported back yet
    monkeypatch.setattr(recover, "_just_moved", lambda s: True)
    write_private(state_dir() / "limit-state.json", json.dumps({SID: dict(err, updated=NOW)}))
    recover.limit_check(cfg(), bot=bot, now=NOW + 1, inbox=I.Inbox(state_dir() / "inbox"), spawn_fn=spawned.append)
    assert spawned == []


def test_grouping_needs_a_reset_still_in_the_future():
    for n in ("b",):
        accounts.add(n)
    accounts.record(SID, "main")
    write_usage("main", 100, NOW - 10)
    write_usage("b", 30, NOW - 10)
    accounts.note_limit("main", NOW)
    accounts.note_limit("b", NOW + 2)
    assert "b" in accounts.fallback_plan(SID, NOW + 5)["fresh"]


def test_the_limit_offer_notes_this_limit_before_it_groups_accounts(bot, monkeypatch):
    import time as _t
    from ctb import mirror
    accounts.add("b")
    accounts.add("c")
    accounts.record(SID, "main")
    TopicMap(state_dir() / "topics.json", bot, 7).ensure(SID, "T")
    reset = _t.time() + 3600
    write_usage("main", 100, reset)
    write_usage("b", 30, reset)                                     # shares main's reset and was noted just now
    write_usage("c", 30, reset + 3600)
    accounts.note_limit("b")
    spawned = []
    assert mirror.limit_offer(cfg(), bot, SID, "You've hit your usage limit", spawn_fn=spawned.append)
    assert spawned and "--target=c" in spawned[0][-1]


# --- leftovers of the second review --------------------------------------------------------------

def test_a_quoted_dialog_above_the_input_gets_no_key(monkeypatch):
    screen = ("  Resuming it will use about 8% of your 5-hour usage limit.\n"
              "    1. Resume\n  ❯ 2. Start a new conversation\n\n❯ \n")
    pane = ScreenPane(screen)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "_tmux", pane)
    assert I.answer_resume_dialog("%1") is False
    assert pane.keys == []                                          # the input prompt below it: not a live dialog


def test_a_move_stuck_at_the_resume_question_is_not_reported_as_moved(monkeypatch):
    accounts.add("small")
    accounts.record(SID, "main")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)     # could not answer it, but not a trust problem
    monkeypatch.setattr(I, "_pane_command", lambda p: "zsh")
    pane = ScreenPane(DIALOG.format(c1=" ", c2=" "))
    monkeypatch.setattr(I, "_tmux", pane)
    said = Said()
    assert I.account_move(cfg(), FakeBot(), SID, "small", said) is False
    assert "resume question" in said[-1] and not any("moved to account" in m for m in said)


def test_auto_move_never_types_continue_into_the_resume_question(monkeypatch):
    accounts.add("small")
    accounts.record(SID, "main")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "account_move", lambda *a, **k: True)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    monkeypatch.setattr(I, "_terminal_say", lambda *a, **k: None)
    pane = ScreenPane(DIALOG.format(c1="❯", c2=" "))
    monkeypatch.setattr(I, "_tmux", pane)
    assert I.auto_move(cfg(), FakeBot(), SID, 5, "small") is False
    assert pane.keys == []


def test_the_deferral_origin_is_the_account_that_ran_the_process_not_the_new_session_default(monkeypatch):
    accounts.add("small")
    accounts.add("dflt")
    accounts.use("dflt")                                            # default for NEW sessions
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "_tmux", StuckPane("x\n  ⏵⏵ bypass permissions on · 2 background tasks\n"))
    monkeypatch.setattr(I, "pane_text", lambda pane, lines=45: "  ⏵⏵ bypass permissions on · 2 background tasks")
    assert I.account_move(cfg(), FakeBot(), "sess-unrecorded", "small", Said(), defer=True) is False
    assert accounts.deferred_move("sess-unrecorded")["origin"] == "main"


def test_a_successful_move_brings_the_keyboard_back_with_the_new_account(bot, monkeypatch):
    accounts.add("small")
    accounts.record(SID, "main")
    TopicMap(state_dir() / "topics.json", bot, 7).ensure(SID, "T")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)
    states = iter(["zsh"])
    monkeypatch.setattr(I, "_tmux", lambda *a: SimpleNamespace(returncode=0, stdout="/x\n"))
    monkeypatch.setattr(I, "_pane_command", lambda p: next(states))
    back = []
    monkeypatch.setattr(I, "keyboard_back", lambda thread, **kw: back.append(thread))
    assert I.account_move(cfg(), FakeBot(), SID, "small", Said()) is True
    assert back == [TopicMap(state_dir() / "topics.json", None, 0).thread_of(SID)] and back[0]


def test_a_hung_exit_at_the_limit_ends_the_process_and_finishes_the_move(stuck, monkeypatch):
    """2026-11-02: /exit ran its hooks but the process never ended; nothing recovered until a Mac restart."""
    ended = []
    monkeypatch.setattr(I, "_stop_hung_claude", lambda pane, **kw: ended.append((pane, kw)) or "zsh")
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)
    monkeypatch.setattr(I, "_resume_dialog_pending", lambda pane: False)
    monkeypatch.setattr(I, "_idle_after_move", lambda *a, **k: None)
    monkeypatch.setattr(I, "_hung_at_limit", lambda *a, **k: True)
    said = Said()
    assert I._account_move(cfg(), None, SID, "spare", said, limit={"kind": "five_hour"}) is True
    assert ended and ended[0][0] == "%1" and ended[0][1]["session"] == SID


def test_the_move_hands_the_sessions_pid_and_start_time_from_before_the_exit(stuck, monkeypatch):
    """Its record can vanish in the exit hooks; the pid read before /exit still names the process."""
    pids = iter([4242, None, None])
    monkeypatch.setattr(I, "session_pid", lambda s: next(pids, None))
    monkeypatch.setattr(I, "_proc_start", lambda pid: "Mon Nov  2 03:00:00 2026")
    monkeypatch.setattr(I, "_hung_at_limit", lambda *a, **k: True)
    seen = {}
    monkeypatch.setattr(I, "_stop_hung_claude", lambda pane, **kw: seen.update(kw) or "claude")
    I._account_move(cfg(), None, SID, "spare", Said(), limit={"kind": "five_hour"})
    assert seen["pid"] == 4242 and seen["started"] == "Mon Nov  2 03:00:00 2026" and seen["session"] == SID


@pytest.mark.parametrize("change", ["other pane", "not claude", "background", "unreadable", "agent", "shell job"])
def test_a_limit_move_never_ends_a_process_it_cannot_prove_is_hung(monkeypatch, change):
    monkeypatch.setattr(I, "pane_runs_session", lambda p, s, **k: change != "other pane")
    monkeypatch.setattr(I, "_pane_work_state", lambda p: {"background": True, "unreadable": None}.get(change, False))
    monkeypatch.setattr(I.accounts, "_subagent_active", lambda s, n: change == "agent")
    monkeypatch.setattr(I, "session_pid", lambda s: 42)
    monkeypatch.setattr(I.accounts, "shell_children", lambda p: change == "shell job")
    assert I._hung_at_limit("%1", SID, "vim" if change == "not claude" else "2.1.314") is False


@pytest.mark.parametrize("pid,shell", [(42, False), (None, None)])
def test_a_limit_move_may_end_a_process_whose_screen_shows_no_recognised_footer(monkeypatch, pid, shell):
    """The positive case: a readable screen that shows no work is not "unknown", or recovery is impossible."""
    monkeypatch.setattr(I, "pane_runs_session", lambda p, s, **k: True)
    monkeypatch.setattr(I, "_tmux", lambda *a: SimpleNamespace(returncode=0, stdout="some error text\n"))
    monkeypatch.setattr(I.accounts, "_subagent_active", lambda s, n: False)
    monkeypatch.setattr(I, "session_pid", lambda s: pid)
    monkeypatch.setattr(I.accounts, "shell_children", lambda p: shell)
    assert I._hung_at_limit("%1", SID, "2.1.314") is True


def test_the_work_footer_reads_work_nothing_or_unknown(monkeypatch):
    def screen(returncode, text):
        monkeypatch.setattr(I, "_tmux", lambda *a: SimpleNamespace(returncode=returncode, stdout=text))
        return I._pane_work_state("%1")

    assert screen(0, "x\n  ⏵⏵ accept edits on · 2 background tasks\n") is True
    assert screen(0, "x\n  ? for shortcuts\n") is False
    assert screen(0, "") is False
    assert screen(1, "") is None  # a failed capture is unknown, which the kill path treats as work
    assert I.background_work_visible("%1") is False  # every other caller keeps "unknown means no work"


def test_an_exact_pane_target_is_captured_with_its_colon(monkeypatch):
    seen = []
    monkeypatch.setattr(I, "_tmux", lambda *a: seen.append(a) or SimpleNamespace(returncode=0, stdout=""))
    I._pane_work_state("=tg-abcd")
    assert seen[0][seen[0].index("-t") + 1] == "=tg-abcd:"


# --- the kill itself, with the process table and tmux faked ----------------------------------------

PS_NOW = "Mon Nov  2 03:00:00 2026"


class Arena:
    """One hung process at pid 4242 in a fake pane; records the signals it receives."""

    def __init__(self, monkeypatch, dies_on=(15, 9), targets=None, records=[], work=False, shell=False,
                 etime="05:00"):
        self.signals, self.alive = [], True
        self.dies_on, self.records, self.reads = dies_on, records, 0
        self.targets = [(4242, PS_NOW)] if targets is None else targets
        self.tmux_calls = []
        monkeypatch.setattr(I.time, "sleep", lambda _: None)
        monkeypatch.setattr(I, "_pane_command", lambda pane: "claude" if self.alive else "zsh")
        monkeypatch.setattr(I, "_pane_claude_targets", lambda pane: list(self.targets) if self.alive else [])
        monkeypatch.setattr(I, "_ps_field", lambda field, pid: etime)
        monkeypatch.setattr(I.accounts, "shell_children", lambda pid: shell)
        monkeypatch.setattr(I, "_live_records_strict", self.read_records)
        monkeypatch.setattr(I, "_pane_work_state", lambda pane: work)
        monkeypatch.setattr(I.os, "kill", self.kill)

    def read_records(self, target):
        self.reads += 1
        return self.records(self.reads) if callable(self.records) else self.records

    def kill(self, pid, sig):
        self.signals.append((pid, sig))
        if sig in self.dies_on:
            self.alive = False


def test_a_hung_process_gets_sigterm_and_nothing_else(monkeypatch):
    arena = Arena(monkeypatch, dies_on=(15,))
    assert I._stop_hung_claude("%1", pid=4242, started=PS_NOW, session=SID) == "zsh"
    assert arena.signals == [(4242, 15)]


def test_a_process_that_ignores_sigterm_gets_sigkill_after_it(monkeypatch):
    arena = Arena(monkeypatch, dies_on=(9,))
    assert I._stop_hung_claude("%1", pid=4242, session=SID) == "zsh"
    assert arena.signals == [(4242, 15), (4242, 9)]


@pytest.mark.parametrize("why,kw,arena_kw", [
    ("not the session's pid", dict(pid=7777), {}),
    ("started at another time", dict(pid=4242, started="Tue"), {}),
    ("two foreground Claude processes", dict(pid=4242), dict(targets=[(4242, PS_NOW), (4243, PS_NOW)])),
    ("no foreground Claude process", dict(pid=4242), dict(targets=[])),
    ("a shell job runs", dict(pid=4242), dict(shell=True)),
    ("shell jobs unknown", dict(pid=4242), dict(shell=None)),
    ("records unreadable", dict(pid=4242), dict(records=None)),
    ("another session's record names it", dict(pid=4242),
     dict(records=[{"pid": 4242, "sessionId": "after-clear"}])),
    ("work in the footer", dict(pid=4242), dict(work=True)),
    ("screen unreadable", dict(pid=4242), dict(work=None)),
    ("session pid unknown and too young", dict(), dict(etime="00:30")),
    ("session pid unknown and a record names it", dict(), dict(records=[{"pid": 4242, "sessionId": "x"}])),
    ("session pid unknown and a record names this very session", dict(),
     dict(records=[{"pid": 4242, "sessionId": SID}])),
])
def test_nothing_is_signalled_without_every_proof(monkeypatch, why, kw, arena_kw):
    arena = Arena(monkeypatch, **arena_kw)
    assert I._stop_hung_claude("%1", session=SID, **kw) == "claude"
    assert not arena.signals, why


def test_this_sessions_own_record_does_not_block_ending_its_hung_process(monkeypatch):
    arena = Arena(monkeypatch, dies_on=(15,), records=[{"pid": 4242, "sessionId": SID},
                                                       {"pid": 9, "sessionId": "other"}])
    I._stop_hung_claude("%1", pid=4242, session=SID)
    assert arena.signals == [(4242, 15)]


def test_a_reopen_needs_two_reads_to_find_no_record(monkeypatch):
    """A record is rewritten as the status changes: the second read, 2s later, can still stop the kill."""
    arena = Arena(monkeypatch, records=lambda n: [] if n == 1 else [{"pid": 4242, "sessionId": "x"}])
    I._stop_hung_claude("%1", min_age=90, session=SID)
    assert arena.reads == 2 and not arena.signals


def test_a_reopen_of_an_old_unrecorded_claude_ends_it(monkeypatch):
    """No record at all: a window at the trust question looks like this too. Nothing is lost by ending it."""
    arena = Arena(monkeypatch, dies_on=(15,))
    I._stop_hung_claude("%1", min_age=90, session=SID)
    assert arena.reads == 2 and arena.signals == [(4242, 15)]


def test_an_unrecognised_footer_does_not_block_ending_a_hung_process(monkeypatch):
    arena = Arena(monkeypatch, dies_on=(15,), work=False)
    I._stop_hung_claude("%1", pid=4242, session=SID)
    assert arena.signals


def test_the_process_is_checked_again_before_the_first_signal(monkeypatch):
    """Pid reuse in the gap since the first look (the 2s between record reads): no SIGTERM follows."""
    arena = Arena(monkeypatch, dies_on=())
    reads = []

    def targets(pane):
        reads.append(1)
        return [(4242, PS_NOW)] if len(reads) <= 1 else [(4242, "Tue Nov  3 09:00:00 2026")]

    monkeypatch.setattr(I, "_pane_claude_targets", targets)
    I._stop_hung_claude("%1", pid=4242, session=SID)
    assert arena.signals == []


def test_a_pid_taken_over_between_sigterm_and_sigkill_is_left_alone(monkeypatch):
    arena = Arena(monkeypatch, dies_on=())
    reads = []

    def targets(pane):
        reads.append(1)
        return [(4242, PS_NOW)] if len(reads) <= 2 else [(4242, "Tue Nov  3 09:00:00 2026")]

    monkeypatch.setattr(I, "_pane_claude_targets", targets)
    I._stop_hung_claude("%1", pid=4242, session=SID)
    assert arena.signals == [(4242, 15)]


@pytest.mark.parametrize("comm,command,expected", [
    ("/home/x/.local/share/claude/versions/2.1.314", "/home/x/.local/share/claude/versions/2.1.314 --resume a", True),
    ("/usr/local/bin/node", "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js", True),
    ("/opt/homebrew/bin/node", "node /opt/homebrew/bin/claude --resume a", True),
    ("/opt/homebrew/bin/node", "node --max-old-space-size=4096 /opt/homebrew/bin/claude", True),
    ("/usr/local/bin/node", "node /home/x/claude-dashboard/server.js", False),
    ("/usr/local/bin/node", "node /home/x/claude-tools/claude-plugin/index.js", False),
    ("/usr/local/bin/node", "node /srv/app.js --claude", False),
    ("claude", "claude rc", True),
    ("/usr/local/bin/node", "node /app/devserver.js", False),
    ("/usr/local/bin/node", "node /app/node_modules/.bin/vite", False),
    ("/usr/bin/python3", "python3 /x/claude_helper.py", False),
    ("/bin/zsh", "-zsh", False),
    ("", "claude", False),
])
def test_only_the_shapes_claude_really_runs_as_count(monkeypatch, comm, command, expected):
    monkeypatch.setattr(I, "_ps_field", lambda field, pid: comm)
    assert I._looks_like_claude(1, command) is expected


def test_session_records_fail_closed_only_where_they_could_be_the_targets(monkeypatch):
    sessions = Path(__import__("os").environ["HOME"]) / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    me = __import__("os").getpid()
    (sessions / f"{me}.json").write_text(json.dumps({"pid": me, "sessionId": "s1"}))
    (sessions / "99999991.json").write_text(json.dumps({"pid": 99999991, "sessionId": "dead"}))
    (sessions / "5.json").write_text("{not json")  # some other pid's file: irrelevant to a target of 4242
    assert [r["sessionId"] for r in I._live_records_strict(4242)] == ["s1"]
    (sessions / "4242.json").write_text("{not json")  # the target's own record: unknown
    assert I._live_records_strict(4242) is None
    assert I._live_records_strict(me + 1) == [{"pid": me, "sessionId": "s1"}]


def test_session_records_missing_means_no_records(monkeypatch):
    assert I._live_records_strict(4242) == []


def test_resume_hands_the_session_to_the_stop(monkeypatch):
    """A Claude that ignored /exit and has no record is ended on reopen (with the session, so a record of
    any other session keeps it)."""
    seen = {}
    monkeypatch.setattr(I, "_pane_command", lambda pane: "2.1.314" if not seen else None)
    monkeypatch.setattr(I, "session_pid", lambda s: None)
    monkeypatch.setattr(I, "_stop_hung_claude", lambda pane, **kw: seen.update(kw, pane=pane) or None)
    monkeypatch.setattr(I, "_tmux", StuckPane())
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    I.resume_session(SID)
    assert seen["pane"] == "=tg-" + SID[:8] and seen["session"] == SID and seen["min_age"] == I.HUNG_MIN_AGE


def test_a_hung_exit_without_a_limit_is_not_killed(stuck, monkeypatch):
    monkeypatch.setattr(I, "_stop_hung_claude", lambda *a, **k: pytest.fail("killed a session not at its limit"))
    assert I._account_move(cfg(), None, SID, "spare", Said()) is False


def test_stop_hung_claude_fails_safe_when_the_process_is_unknown(monkeypatch):
    monkeypatch.setattr(I, "_tmux", StuckPane())  # "claude" for the terminal: not a /dev path
    killed = []
    monkeypatch.setattr(I.os, "kill", lambda *a: killed.append(a))
    assert I._stop_hung_claude("p") == "claude"
    assert not killed


@pytest.mark.parametrize("name,expected", [("2.1.314", True), ("claude", True), ("node", True),
                                           ("zsh", False), ("vim", False), ("", False), (None, False)])
def test_only_claude_counts_as_a_hung_process(name, expected):
    assert I._is_claude_process(name) is expected


@pytest.mark.skipif(not __import__("shutil").which("tmux"), reason="needs tmux")
def test_an_exact_session_target_reads_on_real_tmux(monkeypatch, private_tmux):
    """tmux 3.7 prints nothing for `display -t =name`, which read as "busy" and blocked every reopen."""
    import shutil
    import tempfile
    folder = tempfile.mkdtemp(dir="/tmp")
    monkeypatch.setenv("TMUX_TMPDIR", folder)  # a private server: the live one is never touched
    monkeypatch.delenv("TMUX", raising=False)
    try:
        assert I._tmux("new-session", "-d", "-s", "tg-probe", "sleep 30").returncode == 0
        # Linux tmux reports "tmux" for a moment, until the forked pane execs its command.
        end = __import__("time").monotonic() + 5
        while I._pane_command("=tg-probe") != "sleep" and __import__("time").monotonic() < end:
            __import__("time").sleep(0.05)
        assert I._pane_command("=tg-probe") == "sleep"
        assert I._pane_command("=tg-pro") is None
    finally:
        I._tmux("kill-session", "-t", "=tg-probe")
        shutil.rmtree(folder, ignore_errors=True)


def test_the_limit_check_passes_the_pid_read_before_exit_to_the_pane_check(monkeypatch):
    seen = {}
    monkeypatch.setattr(I, "pane_runs_session", lambda p, s, known_pid=None: seen.update(pid=known_pid) or True)
    monkeypatch.setattr(I, "_pane_work_state", lambda p: False)
    monkeypatch.setattr(I.accounts, "_subagent_active", lambda s, n: False)
    monkeypatch.setattr(I, "session_pid", lambda s: None)  # its record vanished in the exit hooks
    monkeypatch.setattr(I.accounts, "shell_children", lambda p: seen.update(shell_of=p) or False)
    assert I._hung_at_limit("%1", SID, "2.1.314", pid=4242) is True
    assert seen == {"pid": 4242, "shell_of": 4242}


def test_a_plain_window_is_this_sessions_by_the_pid_read_earlier(monkeypatch):
    def tmux(*args):
        return SimpleNamespace(returncode=0, stdout="900 work\n")  # pane process 900, not a bridge window

    def run(cmd, **kw):
        return SimpleNamespace(stdout="900\n" if "4242" in cmd else "1\n", returncode=0)  # parent of 4242 is 900

    monkeypatch.setattr(I, "_tmux", tmux)
    monkeypatch.setattr(I.subprocess, "run", run)
    monkeypatch.setattr(I, "session_pid", lambda s: None)
    assert I.pane_runs_session("%1", SID) is False
    assert I.pane_runs_session("%1", SID, known_pid=4242) is True


def test_a_records_folder_that_cannot_be_listed_is_unknown_not_empty(monkeypatch):
    import os
    folder = Path(os.environ["HOME"]) / ".claude" / "sessions"
    folder.mkdir(parents=True)
    (folder / "5.json").write_text(json.dumps({"pid": 5, "sessionId": "x"}))
    assert accounts._session_record_files_strict() == [folder / "5.json"]
    os.chmod(folder, 0)
    try:
        assert accounts.live_session_records() == []  # the lenient reader: shown as "none"
        assert accounts._session_record_files_strict() is None
        assert I._live_records_strict(4242) is None
        assert accounts.restart_is_safe_or_gone(SID, NOW) is False
    finally:
        os.chmod(folder, 0o700)


def test_without_a_record_a_deferred_move_may_retry_once_agents_are_quiet(monkeypatch):
    assert accounts.restart_is_safe(SID, NOW) is False  # the old rule: no record, never
    assert accounts.restart_is_safe_or_gone(SID, NOW) is True
    monkeypatch.setattr(accounts, "_subagent_active", lambda s, n: True)
    assert accounts.restart_is_safe_or_gone(SID, NOW) is False


def test_a_half_written_record_is_not_taken_for_a_gone_one(monkeypatch):
    import os
    folder = Path(os.environ["HOME"]) / ".claude" / "sessions"
    folder.mkdir(parents=True)
    (folder / "5.json").write_text('{"pid": 5, "sess')
    assert accounts.restart_is_safe_or_gone(SID, NOW) is False


def test_with_a_record_the_ordinary_rule_still_decides(monkeypatch):
    import os
    folder = Path(os.environ["HOME"]) / ".claude" / "sessions"
    folder.mkdir(parents=True)
    (folder / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": SID, "status": "busy", "statusUpdatedAt": 0}))
    assert accounts.restart_is_safe_or_gone(SID, NOW) is False  # busy: not safe


def test_the_retry_needs_a_window_that_is_still_this_sessions(monkeypatch):
    from ctb import recover
    accounts.add("small")
    accounts.record(SID, "main")
    accounts.defer_move(SID, "small", NOW - 7200, origin="main", limit={"id": "L1"})
    monkeypatch.setattr(I, "pane_for_session", lambda s: None)
    spawned = []
    recover._retry_deferred(None, None, SID, 5, NOW, lambda a: spawned.append(a), {"moved": None}, {"id": "L1"})
    assert spawned == []  # no record and no window of its own: nothing to retry
