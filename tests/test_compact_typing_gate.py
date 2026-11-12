"""A slash line typed by the bridge is never joined to other text (live bug 2026-11-11, session c41c5a4a).

`cli_command` typed `/compact` into the input box with no look at what was in it and outside the lock every
nudge takes. The wake nudge "📩 Telegram" sat in the box, `/compact` was appended, Enter sent the plain prompt
"📩 Telegram/compact": a turn started instead of a compaction, the progress message said "did not see
compaction start", the topic flipped busy and done, and a later retry compacted for real.
Rule under test: one gate (`inbox.type_command`) types every slash line, under the session's nudge lock, into an
EMPTY box (bridge leftovers are taken out first, the owner's own draft is never touched), and a nudge is never
typed while a compaction is claimed or running. Every pane here is a fake: no test reaches tmux.
"""
import threading
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.real_type_command  # the real gate, not conftest's plain typist

from conftest import FakeBot  # noqa: F401  (the `bot` fixture lives in conftest)
from ctb import accounts, autopilot, compact
from ctb import inbox as I
from ctb.config import Config
from ctb.i18n import t
from test_heads_up_notice import Pane

SID = "c41c5a4a-0000-4000-8000-0000000000bb"
NUDGE = I.NUDGE_TEXT


@pytest.fixture
def pane(monkeypatch):
    p = Pane(tip="")
    monkeypatch.setattr(I, "_tmux", p)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "recently_moving", lambda s: False)
    monkeypatch.setattr(I, "pane_runs_session", lambda pane, s: True)
    return p


def typed_keys(p):
    return [c for c in p.calls if c[0] == "send-keys"]


# --- the bug as it happened --------------------------------------------------------------------------------

def test_an_unsent_nudge_in_the_box_is_taken_out_before_the_command_is_typed(pane):
    I.mark_nudge_typed(SID)  # the bridge's record that it typed a nudge here lately
    pane.box = NUDGE  # the nudge was typed, Enter never sent it
    assert I.type_command(SID, "%1", "/compact") == "sent"
    assert pane.sent == ["/compact"]  # never "📩 Telegram/compact"
    assert pane.box == ""


def test_several_unsent_nudges_are_all_taken_out(pane):
    I.mark_nudge_typed(SID)
    pane.box = NUDGE * 4  # four "📩 Telegram" once sat unsent in one box
    assert I.type_command(SID, "%1", "/compact keep the plan") == "sent"
    assert pane.sent == ["/compact keep the plan"]


# --- finding 3: text that only LOOKS like a nudge is the owner's unless the bridge typed one -------------------

def test_a_draft_that_reads_like_the_nudge_is_the_owners_without_a_record_of_typing_one(pane):
    pane.box = NUDGE  # nobody typed a nudge into this session: the owner wrote it
    assert I.type_command(SID, "%1", "/compact") == "draft"
    assert typed_keys(pane) == [] and pane.box == NUDGE


def test_an_old_record_does_not_prove_it(pane):
    I.mark_nudge_typed(SID)
    I._nudge_typed_path(SID).write_text(repr(time.time() - 2000))
    pane.box = NUDGE
    assert I.type_command(SID, "%1", "/compact") == "draft"
    assert typed_keys(pane) == [] and pane.box == NUDGE


def test_the_leftover_is_read_again_right_before_the_backspaces(pane):
    pane.box = "owner added words " + NUDGE  # changed since the caller looked
    assert not I._erase_tail("%1", NUDGE, len(NUDGE), True)
    assert not [c for c in pane.calls if c[0] == "send-keys"] and pane.box.startswith("owner added")
    pane.box = NUDGE
    assert I._erase_tail("%1", NUDGE, len(NUDGE), True) and pane.box == ""


def test_the_owners_own_draft_is_never_typed_after_or_sent_along(pane):
    pane.box = "please fix the login "
    assert I.type_command(SID, "%1", "/compact") == "draft"
    assert typed_keys(pane) == [] and pane.sent == [] and pane.box == "please fix the login "


def test_a_draft_that_starts_with_the_nudge_text_is_still_the_owners(pane):
    pane.box = NUDGE + " and also my own words"
    assert not I.bridge_leftover(pane.box)
    assert I.type_command(SID, "%1", "/compact") == "draft"
    assert pane.sent == [] and pane.box == NUDGE + " and also my own words"


def test_text_that_lands_before_the_command_while_typing_is_not_sent_along(monkeypatch, pane):
    class Racing(Pane):  # the owner starts typing in the very moment the line goes in
        def __call__(self, *args):
            if args[0] == "send-keys" and args[3:4] == ("-l",) and not self.sent and not self.raced:
                self.raced = True
                self.box += "owner words "
            return super().__call__(*args)

    p = Racing(tip="")
    p.raced = False
    monkeypatch.setattr(I, "_tmux", p)
    assert I.type_command(SID, "%1", "/compact") == "draft"
    assert p.sent == [] and p.box == "owner words "  # only the bridge's own text was taken out again


def test_a_line_that_enter_did_not_send_is_taken_back(monkeypatch):
    class Stuck(Pane):  # Enter is swallowed (a notice took the key)
        def __call__(self, *args):
            if args[0] == "send-keys" and "Enter" in args:
                self.calls.append(args)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return super().__call__(*args)

    p = Stuck(tip="")
    monkeypatch.setattr(I, "_tmux", p)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I.time, "monotonic", iter(range(0, 10_000)).__next__)
    assert I.type_command(SID, "%1", "/compact") == "failed"
    assert p.box == "" and p.sent == []  # nothing left for the next line to join


# --- finding 1: nothing is typed unless an empty, ready box was read ---------------------------------------------

def test_an_unreadable_box_gets_nothing_typed(monkeypatch):
    calls = []

    def tmux(*args):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="no input box on this screen\n", stderr="")

    monkeypatch.setattr(I, "_tmux", tmux)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    assert I.type_command(SID, "%1", "/compact") == "busy"
    assert [c for c in calls if c[0] == "send-keys"] == []  # an unsent nudge could be hiding there


def test_a_box_that_is_not_ready_gets_nothing_typed(pane):
    pane.tip = ""
    original = pane.render
    pane.render = lambda: original() + "\n  esc to interrupt"  # a turn is running
    assert I.type_command(SID, "%1", "/compact") == "busy"
    assert typed_keys(pane) == []


# --- finding 2: owner text that arrives after our line is typed -----------------------------------------------

def test_owner_text_added_after_our_line_is_never_sent_with_it(monkeypatch):
    class Appending(Pane):  # the owner keeps typing right behind the line
        def __call__(self, *args):
            res = super().__call__(*args)
            if args[0] == "send-keys" and args[3:4] == ("-l",):
                self.box += " please keep this"
            return res

    p = Appending(tip="")
    monkeypatch.setattr(I, "_tmux", p)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I.time, "monotonic", iter(range(0, 10_000)).__next__)
    assert I.type_command(SID, "%1", "/compact") == "draft"
    assert p.sent == [] and p.box == "/compact please keep this"  # no Enter; the owner's draft is left whole
    assert not [c for c in p.calls if c[-1] in ("Enter", "BSpace")]


# --- the same lock as every nudge --------------------------------------------------------------------------

def test_a_nudge_and_a_slash_line_never_interleave(pane):
    """The nudge holds the session's lock while it types and sends; the slash line waits for it and finds an
    empty box, in this process and (through the file lock) in the others."""
    done = threading.Event()
    result = []

    def slash():
        result.append(I.type_command(SID, "%1", "/compact"))
        done.set()

    with I._nudge_guard(SID):
        worker = threading.Thread(target=slash, daemon=True)
        worker.start()
        assert not done.wait(0.3)  # waits for the lock
        assert typed_keys(pane) == []
        assert I.typed_nudge(SID, "%1")  # the nudge types and sends in full while it holds the lock
        assert pane.sent == [NUDGE] and pane.box == ""
    worker.join(5)
    assert result == ["sent"]
    assert pane.sent == [NUDGE, "/compact"]


def test_the_nudge_is_never_typed_while_a_compaction_request_is_claimed(pane):
    compact.ask(SID, "keep the plan")
    assert compact.claim_ask(SID) is not None  # its /compact is about to be typed
    assert compact.command_pending(SID)
    assert not I.typed_nudge(SID, "%1")
    assert typed_keys(pane) == []
    compact.mark_done(SID)  # the compaction ended: nudges may type again
    assert not compact.command_pending(SID)
    assert I.typed_nudge(SID, "%1")


def test_command_pending_covers_a_running_compaction_and_expires(monkeypatch):
    assert not compact.command_pending(SID)
    compact.note_compacting(SID)
    assert compact.command_pending(SID)
    compact.mark_done(SID)
    assert not compact.command_pending(SID)
    compact.ask(SID)
    took = compact.claim_ask(SID)
    assert compact.command_pending(SID, now=took["at"] + 1)
    assert not compact.command_pending(SID, now=time.time() + compact.TYPING_WINDOW + 5)  # a dead run frees it


def test_the_other_nudge_paths_wait_for_a_pending_compaction(pane, monkeypatch):
    compact.note_compacting(SID)
    box = I.Inbox(compact.directory().parent / "box")
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "limit_active", lambda s: False, raising=False)
    assert not I.sweep_nudge(box, SID, force=True)
    from ctb import recover
    monkeypatch.setattr(I, "main_working", lambda s: False)
    monkeypatch.setattr(I, "_waiter_alive", lambda s: False)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: True)
    assert recover._deliver(SID, queued=True) == "later"
    assert typed_keys(pane) == []


def test_autopilot_does_not_type_into_the_owners_draft_or_stack_on_leftovers(pane, monkeypatch):
    pane.box = "my half-written reply "
    with pytest.raises(OSError):
        autopilot.type_into("%1", "[Autopilot] continue")
    assert pane.sent == [] and pane.box == "my half-written reply "
    pane.box = NUDGE  # not proved to be the bridge's: nothing is typed after it either
    with pytest.raises(OSError):
        autopilot.type_into("%1", "[Autopilot] continue")
    assert pane.sent == [] and pane.box == NUDGE
    pane.box = ""
    autopilot.type_into("%1", "[Autopilot] continue")
    assert pane.sent == ["[Autopilot] continue"]


# --- the callers ---------------------------------------------------------------------------------------------

@pytest.fixture
def cli(pane, bot, monkeypatch):
    started = []
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "pane_text", lambda pane_id, lines=45: "● Done.\n" + pane.render())
    monkeypatch.setattr(I, "_terminal_background", lambda s, a: a() or True)
    monkeypatch.setattr(I, "session_busy", lambda s: False)
    monkeypatch.setattr(I, "_start_compact_progress", lambda *a, **k: started.append(a))
    cfg = Config(bot_token="t", owner_chat_id=7, ui_language="en")
    m = {"message_thread_id": 5, "chat": {"id": 7}}
    return SimpleNamespace(cfg=cfg, bot=bot, m=m, started=started)


def say(cli, text):
    return I.cli_command(cli.cfg, cli.bot, cli.m, SID, text)


def test_compact_from_telegram_with_a_nudge_left_in_the_box_compacts_for_real(cli, pane):
    I.mark_nudge_typed(SID)
    pane.box = NUDGE
    assert say(cli, "/compact keep the plan") == "cli"
    assert pane.sent == ["/compact keep the plan"]
    assert len(cli.started) == 1  # the progress message follows a real command


def test_compact_with_the_owners_draft_in_the_box_types_nothing_and_says_so(cli, pane):
    pane.box = "do not lose this "
    assert say(cli, "/compact") == "cli"
    assert pane.sent == [] and pane.box == "do not lose this "
    assert cli.started == []  # no progress bar, so no false "did not see compaction start"
    assert any(t("cli_draft_in_box") in p["text"] for p in cli.bot.methods("sendMessage"))


def test_the_compact_button_uses_the_same_gate(cli, pane, monkeypatch):
    answers = []
    monkeypatch.setattr(I, "_answer_button", lambda bot, q, text, *a, **k: answers.append(text))
    pane.box = "do not lose this "
    q = {"id": "q", "data": "k:/compact", "message": {"message_id": 3, "chat": {"id": 7}, "message_thread_id": 5}}
    I.keypad_press(q, cli.cfg, cli.bot, q["message"], SID)
    assert pane.sent == [] and cli.started == [] and answers == [t("cli_draft_in_box", lang="en")]
    I.mark_nudge_typed(SID)
    pane.box = NUDGE
    I.keypad_press(q, cli.cfg, cli.bot, q["message"], SID)
    assert pane.sent == ["/compact"] and len(cli.started) == 1


# --- the progress message ------------------------------------------------------------------------------------

def test_the_progress_message_shows_no_token_counter(bot):
    ticks = iter(range(0, 1000, 3))
    screens = iter(["✻ Compacting conversation… (23s · ↓ 664.0k tokens · esc to interrupt)"] * 5 + ["done"])
    ok = compact.run(Config(bot_token="t", owner_chat_id=7, ui_language="en"), bot, 5, SID,
                     lambda: next(screens), sleep=lambda s: None, clock=lambda: float(next(ticks)))
    assert ok
    edits = [p["text"] for p in bot.methods("editMessageText")]
    assert edits and not any("tokens" in e or "664" in e for e in edits)


# --- finding 4: a compaction counts as started only on evidence ----------------------------------------------

def test_a_compaction_that_never_showed_leaves_no_mark_so_the_request_is_retried_and_nudges_flow(bot):
    ticks = iter(range(0, 100_000, 3))
    cfg = Config(bot_token="t", owner_chat_id=7, ui_language="en")
    compact.ask(SID, "keep")
    compact.claim_ask(SID)
    compact.note_compacting(SID)  # the provisional mark the follower starts with
    ok = compact.run(cfg, bot, 5, SID, lambda: "an ordinary screen", sleep=lambda s: None,
                     clock=lambda: float(next(ticks)))
    assert not ok
    assert not compact.in_progress(SID) and not compact.hook_marker(SID).exists()
    compact.release_ask(SID, compact.peek_ask(SID)["at"])  # run_asked gives the claim back ...
    assert compact.claim_ask(SID) is not None  # ... and the request can be tried again
    assert not compact.command_pending(SID, now=time.time() + compact.TYPING_WINDOW + 1)


def test_the_follower_clears_its_provisional_mark_even_when_it_dies(monkeypatch, bot):
    def boom(*a, **k):
        raise RuntimeError("follower died")

    monkeypatch.setattr(compact, "run", boom)
    threads = []

    class Inline:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            try:
                self.target()
            except RuntimeError:
                pass

    monkeypatch.setattr(I.threading, "Thread", Inline)
    I._start_compact_progress(Config(bot_token="t", owner_chat_id=7), bot, 5, SID, "%1")
    assert not compact.in_progress(SID) and threads == []


def test_the_precompact_hook_is_evidence_that_it_started(bot):
    ticks = iter(range(0, 100_000, 3))
    cfg = Config(bot_token="t", owner_chat_id=7, ui_language="en")
    compact.note_compacting(SID)
    compact.note_compacting(SID, hook=True)  # Claude Code's own PreCompact hook fired
    assert compact.run(cfg, bot, 5, SID, lambda: "no spinner drawn", sleep=lambda s: None,
                       clock=lambda: float(next(ticks)))


# --- finding 5: /exit goes through the same gate -------------------------------------------------------------

def test_exit_is_typed_only_into_an_empty_ready_box_and_reports_what_happened(pane, monkeypatch):
    monkeypatch.setattr(I, "forget_replay", lambda s: None)
    pane.box = "an unsent draft "
    assert I._exit_pane(SID, "%1") is False
    assert pane.sent == [] and pane.box == "an unsent draft "  # the draft is not sent along with /exit
    pane.box = ""
    assert I._exit_pane(SID, "%1") is True and pane.sent == ["/exit"]


def test_a_close_that_could_not_type_exit_is_not_reported_as_closed(pane, bot, monkeypatch, tmp_path):
    from test_oct30_inbox import OWNER, cfg as ocfg, setup_inbox
    topics, box, thread = setup_inbox(tmp_path, bot)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "forget_replay", lambda s: None)
    monkeypatch.setattr(I, "_terminal_background", lambda s, a: a() or True)
    pane.box = "an unsent draft "
    q = {"id": "q", "data": "close:yes", "from": {"id": OWNER},
         "message": {"message_id": 10, "chat": {"id": OWNER}, "message_thread_id": thread}}
    I.handle_button(q, ocfg(), bot, topics, box)
    assert pane.sent == []
    assert bot.methods("editMessageText")[-1]["text"] == t("close_failed", lang=ocfg().ui_language)


def test_a_move_does_not_stop_a_session_whose_exit_could_not_be_typed(monkeypatch):
    from test_account_move_truth import Said, StuckPane, cfg as mcfg
    accounts.add("small")
    accounts.record(SID, "main")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    rule = "─" * 60
    stuck = StuckPane(screen=f"● Done.\n{rule}\n❯ unsent owner words\n{rule}\n")
    monkeypatch.setattr(I, "_tmux", stuck)
    said = Said()
    assert I.account_move(mcfg(), FakeBot(), SID, "small", said) is False
    assert "/exit" not in " ".join(c[-1] for c in stuck.calls if c[0] == "send-keys")
    assert accounts.account_for(SID) == "main"  # nothing was recorded as moved
    assert not I.planned_restart_marker(SID).exists()


# --- round 2: the record proves only an UNSENT nudge; auto_compact follows the gate's real result -----------------

def test_a_nudge_that_was_sent_leaves_no_record_so_the_same_text_later_is_the_owners(pane):
    assert I.typed_nudge(SID, "%1")  # verified submitted
    assert pane.sent == [NUDGE] and not I.nudge_recently_typed(SID)
    pane.box = NUDGE  # the owner types the same words themselves
    assert I.type_command(SID, "%1", "/compact") == "draft"
    assert typed_keys(pane)[-1] != ("send-keys", "-t", "%1", "BSpace") and pane.box == NUDGE  # never erased


def test_a_nudge_that_stays_unsent_keeps_the_record_until_it_is_taken_out(monkeypatch):
    class Stuck(Pane):
        def __call__(self, *args):
            if args[0] == "send-keys" and "Enter" in args:
                self.calls.append(args)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return super().__call__(*args)

    p = Stuck(tip="")
    monkeypatch.setattr(I, "_tmux", p)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "recently_moving", lambda s: False)
    monkeypatch.setattr(I, "pane_runs_session", lambda pane, s: True)
    monkeypatch.setattr(I.time, "monotonic", iter(range(0, 10_000)).__next__)
    assert not I.typed_nudge(SID, "%1")  # Enter was swallowed and our own text was taken back
    assert p.box == "" and not I.nudge_recently_typed(SID)


@pytest.fixture
def auto(bot, monkeypatch):
    started = []
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "pane_text", lambda pane_id, lines=45: "● Done.")
    monkeypatch.setattr(I, "session_busy", lambda s: False)
    monkeypatch.setattr(I, "_start_compact_progress", lambda *a, **k: started.append(a))
    topics = SimpleNamespace(place_of=lambda s: SimpleNamespace(thread=5, chat=7))
    cfg = Config(bot_token="t", owner_chat_id=7, ui_language="en")
    return SimpleNamespace(started=started, topics=topics, cfg=cfg, bot=bot)


def run_auto(a, **kw):
    return I.auto_compact(SID, 300_000, cfg=a.cfg, bot=a.bot, topics=a.topics, sleep_fn=lambda s: None, **kw)


def test_auto_compact_through_the_real_gate_says_started_only_after_it_was_sent(pane, auto):
    assert run_auto(auto) is True
    assert pane.sent == ["/compact"] and len(auto.started) == 1
    assert len(auto.bot.methods("sendMessage")) == 1  # the announcement, once, after the line went in


def test_a_box_that_changes_between_the_precheck_and_the_gate_keeps_auto_compact_quiet(monkeypatch, auto):
    class Typing(Pane):  # the owner starts typing right after the pre-check has read an empty box
        reads = 0

        def __call__(self, *args):
            res = super().__call__(*args)
            if args[0] == "capture-pane":
                Typing.reads += 1
                if Typing.reads == 1:
                    self.box = "owner just started typing"
            return res

    p = Typing(tip="")
    monkeypatch.setattr(I, "_tmux", p)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    assert run_auto(auto) is False  # the real gate said "draft": nothing announced, no follower, retryable
    assert p.sent == [] and p.box == "owner just started typing"
    assert auto.started == [] and auto.bot.methods("sendMessage") == []
    assert not compact.in_progress(SID)


def test_auto_compact_stays_quiet_when_enter_was_refused(monkeypatch, auto):
    class Stuck(Pane):
        def __call__(self, *args):
            if args[0] == "send-keys" and "Enter" in args:
                self.calls.append(args)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return super().__call__(*args)

    p = Stuck(tip="")
    monkeypatch.setattr(I, "_tmux", p)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I.time, "monotonic", iter(range(0, 10_000)).__next__)
    assert run_auto(auto) is False
    assert p.box == "" and auto.started == [] and auto.bot.methods("sendMessage") == []
