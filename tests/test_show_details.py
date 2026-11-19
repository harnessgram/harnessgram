"""The Show details button under a live action card: a tap from the owner opens every step of THAT card in
place (the folded "N earlier steps" included), and every other tap still gets an answer. Fakes only: the bot
is FakeBot, the state lives in the test's private directory."""
import fcntl
import json

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import config as C
from ctb import i18n, inbox as I, progress
from ctb.config import state_dir
from ctb.topics import TopicMap

OWNER, HOME = 7, -1001234567890
SID = "5e1c0a77-0000-4000-8000-0000000000bb"
LATER = "5e1c0a77-0000-4000-8000-0000000000cc"


class TextMedia:
    def content_of(self, m):
        return m.get("text") or ""


@pytest.fixture
def rig(monkeypatch):
    bot = FakeBot()
    cfg = C.from_dict({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "chat_id": HOME, "target_language": "he",
                       "ui_language": "he", "compact_actions": True})
    topics = TopicMap(state_dir() / "topics.json", bot, HOME, cfg=cfg)
    thread = topics.ensure(SID, "work")
    monkeypatch.setattr("ctb.hooks._seen", lambda _s: set())
    monkeypatch.setattr("ctb.hooks._mark", lambda _s, _i: None)
    monkeypatch.setattr("ctb.topics.set_state", lambda *a, **k: None)
    return cfg, bot, topics, thread


def live_card(rig, steps=10, session=SID, output=""):
    cfg, bot, _, thread = rig
    rows = [{"kind": "tool", "l": f"⚙️ Step {i}", "d": f"command-{i}", **({"output": output} if output else {})}
            for i in range(steps)]
    pdir = state_dir() / "progress"
    pdir.mkdir(parents=True, exist_ok=True)
    progress._send_ordered(cfg, session, rows, {"cursor": 0, "cards": []}, pdir / f"{session}.msg", thread, bot,
                           lambda: 100.0, True, chat=HOME)
    sent = bot.methods("sendMessage")[-1]
    return sent["message_id"], sent


def tap(rig, mid, data="more:actionexpand", sender=OWNER, chat=HOME, thread=None):
    cfg, bot, topics, own = rig
    bot.calls.clear()
    q = {"id": "q1", "from": {"id": sender, "is_bot": False}, "data": data,
         "message": {"message_id": mid, "chat": {"id": chat, "type": "supergroup"},
                     "message_thread_id": own if thread is None else thread}}
    return I.handle({"callback_query": q}, cfg, bot, topics, I.Inbox(state_dir() / "inbox"), TextMedia(), 0)


def answers(bot):
    return [p["text"] for p in bot.methods("answerCallbackQuery")]


def test_show_details_opens_the_folded_earlier_steps_in_place(rig):
    mid, sent = live_card(rig)
    assert i18n.t("progress_earlier_steps", lang="he", n=7) in sent["text"]
    assert "command-0" not in sent["text"]  # compact: headings only
    [button] = sent["reply_markup"]["inline_keyboard"][0]
    assert button["text"] == i18n.t("progress_expand", lang="he")

    assert tap(rig, mid) == "action-details"
    [edit] = rig[1].methods("editMessageText")
    assert edit["message_id"] == mid and edit["chat_id"] == HOME
    # every step opens, the earlier ones too: the tap visibly changes the card
    assert all(f"command-{i}" in edit["text"] for i in range(10))
    assert i18n.t("progress_earlier_steps", lang="he", n=7) not in edit["text"]
    assert edit["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "more:actioncollapse"
    assert answers(rig[1]) == [""]

    assert tap(rig, mid, "more:actioncollapse") == "action-details"
    [edit] = rig[1].methods("editMessageText")
    assert "command-0" not in edit["text"] and edit["reply_markup"]["inline_keyboard"][0][0]["text"] == \
        i18n.t("progress_expand", lang="he")


def test_the_next_live_edit_keeps_the_card_open(rig):
    mid, _ = live_card(rig)
    tap(rig, mid)
    cfg, bot, _, thread = rig
    bot.calls.clear()
    state_path = state_dir() / "progress" / f"{SID}.msg"
    state = json.loads(state_path.read_text())
    rows = [{"kind": "tool", "l": f"⚙️ Step {i}", "d": f"command-{i}"} for i in range(11)]
    progress._send_ordered(cfg, SID, rows, state, state_path, thread, bot, lambda: 200.0, True, chat=HOME)
    [edit] = bot.methods("editMessageText")
    assert "command-0" in edit["text"] and "command-10" in edit["text"]
    assert edit["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "more:actioncollapse"


def test_an_expansion_too_long_for_one_message_folds_its_oldest_steps_instead_of_failing(rig):
    mid, _ = live_card(rig, steps=24, output="x" * 400)
    assert tap(rig, mid) == "action-details"
    [edit] = rig[1].methods("editMessageText")
    assert len(edit["text"]) <= progress.CARD_TEXT_LIMIT
    assert "command-23" in edit["text"] and "command-0" not in edit["text"]
    assert "Step 0" in edit["text"]  # folded back as a heading, never dropped


def test_a_card_after_the_turn_ended_still_opens(rig):
    mid, _ = live_card(rig)
    progress.run_end_progress(rig[0], SID, root=state_dir() / "progress", bot=rig[1])
    assert tap(rig, mid) == "action-details"
    assert "command-0" in rig[1].methods("editMessageText")[0]["text"]


def test_an_earlier_session_card_in_the_same_topic_still_opens(rig):
    mid, _ = live_card(rig)
    progress.run_end_progress(rig[0], SID, root=state_dir() / "progress", bot=rig[1])
    assert rig[2].follow(SID, LATER)  # /clear: the topic now belongs to the next session
    assert tap(rig, mid) == "action-details"
    assert "command-0" in rig[1].methods("editMessageText")[0]["text"]


def test_a_card_of_another_topic_never_opens_here(rig):
    mid, _ = live_card(rig)
    progress.run_end_progress(rig[0], SID, root=state_dir() / "progress", bot=rig[1])
    rig[2].follow(SID, LATER)
    other = rig[2].ensure("5e1c0a77-0000-4000-8000-0000000000dd", "elsewhere")
    assert tap(rig, mid, thread=other) == "action-details-outdated"
    assert not rig[1].methods("editMessageText")
    assert answers(rig[1]) == [i18n.t("toast_button_outdated", lang="he")]


def test_an_unknown_card_gets_a_clear_answer(rig):
    live_card(rig)
    assert tap(rig, 9999) == "action-details-outdated"
    assert answers(rig[1]) == [i18n.t("toast_button_outdated", lang="he")]


def test_a_stranger_still_gets_nothing_opened(rig):
    mid, _ = live_card(rig)
    assert tap(rig, mid, sender=99) == "ignored"
    assert not rig[1].methods("editMessageText")


def test_a_card_being_sent_right_now_asks_to_try_again(rig, monkeypatch):
    mid, _ = live_card(rig)
    monkeypatch.setattr(progress, "CARD_LOCK_WAIT", 0.0)
    with open(state_dir() / "progress" / f"{SID}.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert tap(rig, mid) == "action-details-busy"
    assert answers(rig[1]) == [i18n.t("toast_card_busy", lang="he")]
    assert not rig[1].methods("editMessageText")
