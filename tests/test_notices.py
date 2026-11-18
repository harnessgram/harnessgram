"""Claude Code's "✦" suggestion notices and their feedback rows are not questions for the owner. Every pane
here is a fake: no test reaches tmux and no real config is loaded."""
import json
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import dialogs, mirror, notices
from ctb import inbox as I
from ctb.config import Config, state_dir, write_private
from ctb.topics import TopicMap

OWNER = 7
SID = "5e1c0a77-0000-4000-8000-0000000000bb"
RULE = "─" * 60

TIP = ("✦ Heads up · Claude Code can now keep background tasks running while you chat ...   "
       "1: Learn more   2: Knew this already   0: Dismiss")
FEEDBACK = "✦ Dismissed.   1: That was helpful   2: That was not helpful   0: Dismiss"
PERMISSION = ("╭" + "─" * 40 + "╮\n│ Do you want to proceed?\n│ ❯ 1. Yes\n│   2. No\n╰" + "─" * 40 + "╯\n"
              "  Esc to cancel · Enter to confirm")


def screen(top: str, box: str = "") -> str:
    return "\n".join(["● Done. The tests pass.", "", top, RULE, "❯ " + box if box else "❯ ", RULE,
                      "  ⏵⏵ bypass permissions on (shift+tab to cycle)"])


@pytest.mark.parametrize("row", [TIP, FEEDBACK])
def test_a_notice_never_counts_as_waiting(row):
    body = screen(row)
    assert not I._dialog_open(body)
    assert notices.only_notice(body)
    assert dialogs.parse(I.strip_tip(body)) is None


def test_the_tip_and_the_feedback_row_are_read_with_their_own_options():
    tip = notices.find(screen(TIP))
    assert tip.kind == "tip" and "background tasks" in tip.text
    assert tip.options == [(1, "Learn more"), (2, "Knew this already"), (0, "Dismiss")]
    fb = notices.find(screen(FEEDBACK))
    assert fb.kind == "feedback" and fb.text.startswith("Dismissed.")
    assert [n for n, _ in fb.options] == [1, 2, 0]


def test_a_real_permission_dialog_is_still_waiting():
    for above in ("", TIP + "\n", FEEDBACK + "\n"):
        body = "● Done. The tests pass.\n\n" + above + PERMISSION  # the dialog is the bottom of the screen
        assert I._dialog_open(body)
        assert not notices.only_notice(body)


class Pane:
    def __init__(self, top=TIP, box="", takes_keys=True):
        self.top, self.box, self.takes_keys, self.keys = top, box, takes_keys, []
        self.raw = None  # a whole screen that replaces the drawn one (a dialog at the bottom)

    def __call__(self, *args):
        if args[0] == "capture-pane":
            return SimpleNamespace(returncode=0, stdout=(self.raw or screen(self.top, self.box)) + "\n", stderr="")
        if args[0] == "send-keys":
            for key in args[3:]:
                self.keys.append(key)
                if key == "BSpace":
                    self.box = self.box[:-1]
                elif len(key) == 1:
                    if self.takes_keys and self.top:
                        self.top = ""
                    else:
                        self.box += key
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="en", ui_language="en")


@pytest.fixture
def rig(monkeypatch):
    pane = Pane()
    bot = FakeBot()
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Bridge")
    write_private(state_dir() / "tmux-panes.json", json.dumps({SID: "%1"}))
    monkeypatch.setattr(I, "_tmux", pane)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "session_state", lambda s: {"state": "done"})
    monkeypatch.setattr(notices.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "_terminal_background", lambda s, action: action() or True)
    return SimpleNamespace(pane=pane, bot=bot, topics=topics, thread=thread)


def sent(bot):
    return [p for p in bot.methods("sendMessage")]


def test_the_tip_is_relayed_once_quietly_with_its_options_as_buttons(rig):
    assert notices.scan(cfg(), rig.bot, rig.topics) == 1
    assert notices.scan(cfg(), rig.bot, rig.topics) == 0  # the same notice is not relayed again
    [msg] = sent(rig.bot)
    assert msg["disable_notification"] is True and msg["message_thread_id"] == rig.thread
    assert "background tasks" in msg["text"]
    buttons = [b for row in msg["reply_markup"]["inline_keyboard"] for b in row]
    assert [b["text"] for b in buttons[:3]] == ["1. Learn more", "2. Knew this already", "0. Dismiss"]
    assert buttons[-1]["callback_data"] == "more:unbuttons"  # the way out that only removes the buttons
    assert not rig.pane.keys  # relaying presses nothing


def tap(rig, data, mid=20):
    q = {"id": "1", "data": data, "from": {"id": OWNER, "is_bot": False}}
    m = {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": rig.thread, "message_id": mid}
    return notices.button(q, cfg(), rig.bot, m, SID)


def test_a_tap_sends_that_digit_while_the_notice_is_shown(rig):
    notices.scan(cfg(), rig.bot, rig.topics)
    data = sent(rig.bot)[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert data.startswith("nt:1:")
    assert tap(rig, data) == "sent"
    assert rig.pane.keys == ["1"]


def test_a_tap_after_the_notice_is_gone_sends_nothing(rig):
    notices.scan(cfg(), rig.bot, rig.topics)
    data = sent(rig.bot)[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    rig.pane.top = ""
    assert tap(rig, data) == "closed" and not rig.pane.keys


def test_the_feedback_row_is_closed_with_zero_and_not_relayed(rig):
    rig.pane.top = FEEDBACK
    assert notices.scan(cfg(), rig.bot, rig.topics) == 0
    assert rig.pane.keys == ["0"] and not sent(rig.bot)


def test_a_digit_that_lands_in_the_input_box_is_taken_back(rig):
    rig.pane.top, rig.pane.takes_keys = FEEDBACK, False
    notices.scan(cfg(), rig.bot, rig.topics)
    assert rig.pane.keys == ["0", "BSpace"] and rig.pane.box == ""


def test_the_owners_draft_is_never_typed_into(rig):
    rig.pane.top, rig.pane.box = FEEDBACK, "my draft"
    notices.scan(cfg(), rig.bot, rig.topics)
    assert not rig.pane.keys and rig.pane.box == "my draft"


def test_a_real_dialog_gets_no_notice_handling(rig):
    rig.pane.top = TIP + "\n" + PERMISSION  # the fake draws it above the input box, the notice must still wait
    assert notices.scan(cfg(), rig.bot, rig.topics) == 0
    assert not rig.pane.keys and not sent(rig.bot)


def test_run_notify_neither_alerts_nor_marks_waiting_for_a_notice(rig, monkeypatch):
    alerts, states = [], []
    monkeypatch.setattr(mirror, "_notify_alert", lambda *a: alerts.append(a))
    monkeypatch.setattr(mirror, "_essential_place", lambda cfg, bot, s: SimpleNamespace(thread=rig.thread, away=None))
    monkeypatch.setattr("ctb.topics.set_state", lambda *a, **k: states.append(a))
    monkeypatch.setattr(mirror.time, "sleep", lambda _: None)
    rig.pane.takes_keys = False
    assert mirror.run_notify(cfg(), SID, "permission_prompt", bot=rig.bot) == 0
    assert not alerts and not states and not sent(rig.bot)[1:]


def test_no_digit_is_pressed_when_a_dialog_replaced_the_input_box(rig, monkeypatch):
    # no input box on the screen (input_box gives None) while the notice rows are still drawn
    rig.pane.top = TIP + "\n" + PERMISSION
    monkeypatch.setattr(I, "input_box", lambda pane: (None, False))
    assert not notices.press("%1", 2) and not rig.pane.keys
    monkeypatch.undo()


def test_a_missing_input_box_is_not_an_empty_one(rig, monkeypatch):
    monkeypatch.setattr(I, "input_box", lambda pane: (None, False))
    assert not notices.press("%1", 0) and not rig.pane.keys


def test_a_permission_prompt_that_draws_late_still_alerts(rig, monkeypatch):
    alerts, states = [], []
    monkeypatch.setattr(mirror, "_notify_alert", lambda *a: alerts.append(a))
    monkeypatch.setattr(mirror, "_essential_place", lambda cfg, bot, s: SimpleNamespace(thread=rig.thread, away=None))
    monkeypatch.setattr("ctb.topics.set_state", lambda *a, **k: states.append(a))
    rig.pane.takes_keys = False
    naps = []

    def nap(_):  # the dialog is drawn during the second wait
        naps.append(1)
        if len(naps) == 2:
            rig.pane.raw = "● Done.\n" + TIP + "\n" + PERMISSION

    monkeypatch.setattr(mirror.time, "sleep", nap)
    mirror.run_notify(cfg(), SID, "permission_prompt", bot=rig.bot)
    assert alerts and states and not rig.pane.keys
