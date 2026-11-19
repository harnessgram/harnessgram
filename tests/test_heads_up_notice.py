"""Claude Code 2.1.322's "✦ Heads up" notice is not a question (2026-11-10, session dd1a0cbf): the bridge refused
auto-compaction and the compact button with "answer it from its card" for a card that was never posted, and the
nudge left "📩 Telegram📩 Telegram📩 Telegram📩 Telegram" unsent in the input box. Every pane here is a fake: no
test reaches tmux."""
import json
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import autopilot, dialogs
from ctb import inbox as I
from ctb.config import Config, state_dir, write_private
from ctb.i18n import t
from ctb.topics import TopicMap

OWNER = 7
SID = "dd1a0cbf-0000-4000-8000-0000000000aa"
RULE = "─" * 60

# The notice as the owner saw it: on one row, with its three options at the end.
ONE_ROW = ("✦ Heads up · Claude Code can now keep background tasks running while you chat ...   "
           "1: Learn more   2: Knew this already   0: Dismiss")
# The same notice drawn over several rows, its text folded to "↓ 2 more".
FOLDED = ("✦ Heads up · Claude Code can now keep background tasks running while you chat\n"
          "  Use /tasks to see them.\n"
          "  ↓ 2 more\n"
          "  1: Learn more   2: Knew this already   0: Dismiss")
PERMISSION = ("╭" + "─" * 40 + "╮\n│ Do you want to proceed?\n│ ❯ 1. Yes\n│   2. No\n╰" + "─" * 40 + "╯\n"
              "  Esc to cancel · Enter to confirm")


def screen(tip: str, box: str = "") -> str:
    return "\n".join(["● Done. The tests pass.", "", tip, RULE, "❯ " + box if box else "❯ ", RULE,
                      "  ⏵⏵ bypass permissions on (shift+tab to cycle)"])


@pytest.mark.parametrize("tip", [ONE_ROW, FOLDED, "‏" + ONE_ROW])
def test_the_heads_up_notice_is_not_a_question(tip):
    body = screen(tip)
    assert I._DIALOG_HINT.search(body)  # its option row alone reads like a menu
    assert not I._dialog_open(body) and not I._dialog_open(body, SID)
    assert dialogs.parse(I.strip_tip(body)) is None
    assert "Heads up" not in I.strip_tip(body) and "↓ 2 more" not in I.strip_tip(body)
    assert "❯" in I.strip_tip(body)  # the input box stays


def test_a_real_question_next_to_the_notice_still_counts():
    assert I._dialog_open(ONE_ROW + "\n" + PERMISSION)
    assert I._dialog_open(FOLDED + "\n" + PERMISSION)


class Pane:
    """A fake Claude Code pane: an input box and an optional tip notice. `tip_has_keys`: the notice takes
    number keys and Enter (Enter then sends nothing); otherwise keys go to the input box."""

    def __init__(self, tip=ONE_ROW, tip_has_keys=False, box=""):
        self.tip, self.tip_has_keys, self.box, self.sent, self.calls = tip, tip_has_keys, box, [], []

    def render(self) -> str:
        return screen(self.tip, self.box) if self.tip else "\n".join(["● Done.", RULE, "❯ " + self.box, RULE])

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "capture-pane":
            return SimpleNamespace(returncode=0, stdout=self.render() + "\n", stderr="")
        if args[0] == "send-keys":
            keys = list(args[args.index(args[2]) + 1:])  # after "-t", pane
            if keys[:2] == ["-l", "--"]:
                self.box += keys[2]
            elif keys[:1] == ["-l"]:
                self.box += keys[1]
            else:
                for key in keys:
                    if key == "0" and self.tip and self.tip_has_keys:
                        self.tip = ""
                    elif key == "Enter":
                        if not (self.tip and self.tip_has_keys) and self.box:
                            self.sent.append(self.box)
                            self.box = ""
                    elif key == "BSpace":
                        self.box = self.box[:-1]
                    elif key == "C-u":
                        self.box = ""
                    elif len(key) == 1:
                        self.box += key
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def pane(monkeypatch):
    p = Pane()
    monkeypatch.setattr(I, "_tmux", p)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_runs_session", lambda pane, session: True)
    monkeypatch.setattr(I, "pane_session_process", lambda *a, **k: (7, "Wed Nov 18 13:00:00 2026"))
    return p


@pytest.mark.parametrize("tip_has_keys", [True, False])
@pytest.mark.parametrize("box", ["", "git commit -m "])
def test_notice_probe_never_sends_keys(monkeypatch, tip_has_keys, box):
    p = Pane(tip_has_keys=tip_has_keys, box=box)
    monkeypatch.setattr(I, "_tmux", p)
    assert I.dismiss_tip("%1")
    assert p.tip == ONE_ROW and p.box == box
    assert not [c for c in p.calls if c[0] == "send-keys"]


def test_a_nudge_presses_nothing_into_the_owners_draft(monkeypatch):
    p = Pane(tip_has_keys=False, box="my draft ")
    monkeypatch.setattr(I, "_tmux", p)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "recently_moving", lambda s: False)
    monkeypatch.setattr(I, "pane_runs_session", lambda pane, s: True)
    assert not I.typed_nudge("s", "%1")
    assert p.box == "my draft " and not [c for c in p.calls if c[0] == "send-keys"]


def test_text_that_was_in_the_box_before_and_is_still_there_was_not_sent(monkeypatch):
    p = Pane(tip="", box="📩 Telegram")  # an earlier unsent marker; this round's keys were dropped
    monkeypatch.setattr(I, "_tmux", p)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I.time, "monotonic", iter(range(0, 10_000)).__next__)
    assert not I.submitted("%1", "📩 Telegram", "📩 Telegram")


def test_an_unreadable_box_is_not_mistaken_for_a_submitted_line(monkeypatch):
    monkeypatch.setattr(I, "input_box", lambda pane: (None, False))
    monkeypatch.setattr(I, "pane_raw", lambda pane: "plain screen, no dialog")
    assert not I.submitted("%1", "continue: work", "", wait=0)


def test_an_unreadable_box_behind_an_open_dialog_counts_as_submitted(monkeypatch):
    monkeypatch.setattr(I, "input_box", lambda pane: (None, False))
    monkeypatch.setattr(I, "pane_raw", lambda pane: "dialog")
    monkeypatch.setattr(I, "_dialog_open", lambda raw: raw == "dialog")
    assert I.submitted("%1", "continue: work", "", wait=0)


def test_no_number_is_pressed_while_a_real_question_is_open(monkeypatch):
    p = Pane(tip=ONE_ROW + "\n" + PERMISSION)
    monkeypatch.setattr(I, "_tmux", p)
    assert not I.dismiss_tip("%1")
    assert not [c for c in p.calls if c[0] == "send-keys"]


def test_the_nudge_is_sent_past_the_notice_and_never_left_behind(pane, monkeypatch):
    monkeypatch.setattr(autopilot, "type_into", autopilot.type_into)
    autopilot.type_into("%1", "📩 Telegram", "session")
    assert pane.sent == ["📩 Telegram"] and pane.box == ""


def test_an_unsent_nudge_is_taken_out_and_the_owners_draft_stays(monkeypatch):
    class Stuck(Pane):  # Enter never sends here (and the notice cannot be closed)
        def __call__(self, *args):
            if args[0] == "send-keys" and "Enter" in args:
                self.calls.append(args)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return super().__call__(*args)

    p = Stuck(tip="", box="my draft ")
    monkeypatch.setattr(I, "_tmux", p)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I.time, "monotonic", iter(range(0, 10_000)).__next__)
    with pytest.raises(OSError):
        autopilot.type_into("%1", "📩 Telegram", "session")
    assert p.box == "my draft "  # only the bridge's own text came out
    for _ in range(4):  # the old failure: one more marker per round
        with pytest.raises(OSError):
            autopilot.type_into("%1", "📩 Telegram", "session")
    assert "📩" not in p.box


def test_an_input_box_with_unsent_text_is_never_reported_empty(monkeypatch):
    p = Pane(tip=ONE_ROW, box="📩 Telegram📩 Telegram")
    monkeypatch.setattr(I, "_tmux", p)
    assert not I._empty_prompt("%1")


# --- the compact command and the refusal message -------------------------------------------------

def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", ui_language="he")


@pytest.fixture
def session_rig(pane, monkeypatch):
    bot = FakeBot()
    thread = TopicMap(state_dir() / "topics.json", bot, OWNER).ensure(SID, "Bridge")
    write_private(state_dir() / "tmux-panes.json", json.dumps({SID: "%1"}))
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "_pane_from_process", lambda s: "%1" if s == SID else None)
    from ctb import idle_lines
    monkeypatch.setattr(idle_lines, "_foreground_claude", lambda p, s: p == "%1" and s == SID)
    monkeypatch.setattr(I, "_terminal_background", lambda s, action: action() or True)
    monkeypatch.setattr(I, "session_busy", lambda s: False)
    started = []
    monkeypatch.setattr(I, "_start_compact_progress", lambda *a: started.append(a[3]))
    m = {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": thread, "message_id": 20}
    return SimpleNamespace(bot=bot, m=m, started=started)


def texts(bot):
    return [p.get("text", "") for p in bot.methods("sendMessage")]


def test_compact_runs_with_the_notice_on_screen(session_rig, pane):
    assert I.cli_command(cfg(), session_rig.bot, session_rig.m, SID, "/compact") == "cli"
    assert pane.sent == ["/compact"] and session_rig.started == [SID]
    assert not any(t("cli_unsafe_key", lang="he") in x or t("cli_approval_pending", lang="he") in x
                   for x in texts(session_rig.bot))


def test_no_answer_from_its_card_without_a_card(session_rig, monkeypatch):
    monkeypatch.setattr(I.dialogs, "send_card", lambda *a, **k: False)
    assert not I._question_or_screen(cfg(), session_rig.bot, session_rig.m, SID, "cli_unsafe_key")
    said = texts(session_rig.bot)
    assert said and t("cli_unsafe_key", lang="he") not in said[-1]
    assert said[-1] == t("cli_key_not_sent_no_card", lang="he")


def test_a_real_question_gets_its_card_and_then_the_pointer(session_rig, monkeypatch):
    order = []
    monkeypatch.setattr(I.dialogs, "send_card", lambda *a, **k: order.append("card") or True)
    monkeypatch.setattr(I, "_terminal_say", lambda bot, c, m, text, **k: order.append(text))
    assert I._question_or_screen(cfg(), session_rig.bot, session_rig.m, SID, "cli_unsafe_key")
    assert order == ["card", t("cli_unsafe_key")]
