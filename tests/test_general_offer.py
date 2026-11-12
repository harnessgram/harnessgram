"""A message in the forum group's General topic gets an offer (new topic / answer here / existing topic)
instead of being routed silently."""
import json
import re

import pytest

from ctb import dispatcher, i18n
from ctb import inbox as I
from ctb.config import Config, setting_on, state_dir
from ctb.topics import TopicMap

GROUP = -1001234567890
OWNER = 42
TOKEN = "123456:" + "A" * 35


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(I, "projects", lambda *a, **k: ["/tmp/proj1", "/tmp/proj2"])
    nudged = []
    monkeypatch.setattr(I, "_reopen_or_nudge", lambda inbox, session: nudged.append(session))
    return nudged


@pytest.fixture
def cfg():
    return Config(bot_token=TOKEN, owner_chat_id=OWNER, chat_id=GROUP, ui_language="he")


@pytest.fixture
def topics(bot, tmp_path):
    return TopicMap(state_dir() / "topics.json", bot, GROUP)


@pytest.fixture
def box(tmp_path):
    return I.Inbox(tmp_path / "inbox")


def general(text=None, mid=10, **extra):
    m = {"message_id": mid, "date": 2_000_000_000, "chat": {"id": GROUP, "type": "supergroup"},
         "from": {"id": OWNER}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"update_id": mid, "message": m}


def write(update, cfg, bot, topics, box, media=None):
    return I.handle(update, cfg, bot, topics, box, media, 0)


def offer_of(bot):
    sent = [p for p in bot.methods("sendMessage") if "g:" in str(p.get("reply_markup"))]
    return sent[-1]


def labels(markup):
    return [b["text"] for row in markup["inline_keyboard"] for b in row]


def tap(bot, cfg, topics, box, data, message_id=None, sender=OWNER, chat=GROUP, media=None):
    offer = offer_of(bot)
    q = {"id": "q1", "from": {"id": sender},
         "message": {"message_id": message_id or offer["message_id"], "chat": {"id": chat, "type": "supergroup"}},
         "data": data}
    return I.handle_button(q, cfg, bot, topics, box, media)


def callback(bot, index):
    return [b["callback_data"] for row in offer_of(bot)["reply_markup"]["inline_keyboard"] for b in row][index]


def held():
    return json.loads((state_dir() / "general-offers.json").read_text())


def test_general_message_gets_an_offer_with_the_topic_first_and_nothing_is_routed(cfg, bot, topics, box):
    dispatcher.set_dispatcher("disp")
    assert write(general("לבנות דף נחיתה"), cfg, bot, topics, box) == "offer"
    offer = offer_of(bot)
    assert offer["reply_markup"]["inline_keyboard"][0][0]["text"] == i18n.t("general_offer_btn_topic", lang="he")
    assert labels(offer["reply_markup"]) == [i18n.t(k, lang="he") for k in
                                             ("general_offer_btn_topic", "general_offer_btn_here", "general_offer_btn_existing",
                                              "menu_btn_not_now")]
    assert "לבנות דף נחיתה" in offer["text"]
    assert all(len(b["callback_data"].encode()) <= 64 for row in offer["reply_markup"]["inline_keyboard"] for b in row)
    assert not bot.methods("createForumTopic") and box.take("disp") == []
    assert oct((state_dir() / "general-offers.json").stat().st_mode & 0o777) == "0o600"


def test_dispatcher_button_is_hidden_when_no_dispatcher_is_set(cfg, bot, topics, box):
    assert write(general("hi there"), cfg, bot, topics, box) == "offer"
    assert labels(offer_of(bot)["reply_markup"]) == [i18n.t(k, lang="he") for k in
                                                     ("general_offer_btn_topic", "general_offer_btn_existing",
                                                      "menu_btn_not_now")]
    assert not any(cb.endswith(":d") for cb in [callback(bot, 0), callback(bot, 1)])


def test_topic_button_runs_general_start_and_closes_the_offer(cfg, bot, topics, box):
    write(general("לבנות דף נחיתה\nפרטים"), cfg, bot, topics, box)
    assert tap(bot, cfg, topics, box, callback(bot, 0)) == "picker"
    assert bot.methods("createForumTopic")[-1]["name"] == "לבנות דף נחיתה"
    picker = [p for p in bot.methods("sendMessage") if "n:" in str(p.get("reply_markup"))][-1]
    assert picker.get("message_thread_id")
    edited = bot.methods("editMessageText")[-1]
    assert "https://t.me/c/1234567890/" in edited["text"] and edited["reply_markup"] == {"inline_keyboard": []}
    assert not [p for p in bot.methods("sendMessage") if "t.me/c/" in p["text"]]  # the link is in the outcome only
    assert held() == {}
    pending = json.loads((state_dir() / "projects-pending.json").read_text())
    assert [j["task"] for j in pending.values()] == ["לבנות דף נחיתה\nפרטים"]


def test_topic_button_failure_keeps_the_held_task(cfg, bot, topics, box):
    write(general("task"), cfg, bot, topics, box)
    bot.fail["createForumTopic"] = "Bad Request: not a forum"
    assert tap(bot, cfg, topics, box, callback(bot, 0)) == "unsupported"
    assert len(held()) == 1


def test_topic_button_with_a_voice_note_transcribes_it_like_a_topic_message(cfg, bot, topics, box):
    class FakeMedia:
        def content_of(self, m):
            return "🎤 transcribed words" if m.get("voice") else m.get("text", "")

    from ctb import voice
    voice.set_confirm(False)  # with transcript approval on, the transcript is shown first (its own test below)
    assert write(general(None, voice={"file_id": "f"}), cfg, bot, topics, box, FakeMedia()) == "offer"
    assert i18n.t("general_offer_media", lang="he") in offer_of(bot)["text"]
    assert tap(bot, cfg, topics, box, callback(bot, 0), media=FakeMedia()) == "picker"
    pending = json.loads((state_dir() / "projects-pending.json").read_text())
    assert [j["task"] for j in pending.values()] == ["🎤 transcribed words"]


def test_dispatcher_button_delivers_exactly_like_today(cfg, bot, topics, box):
    dispatcher.set_dispatcher("disp")
    write(general("build me a thing"), cfg, bot, topics, box)
    assert tap(bot, cfg, topics, box, callback(bot, 1)) == "queued"
    items = box.take("disp")
    assert len(items) == 1 and items[0]["text"].startswith("NEW TASK from a new Telegram chat (thread None)")
    assert items[0]["text"].endswith("build me a thing") and items[0]["user_text"] == "build me a thing"
    assert bot.methods("editMessageText")[-1]["text"] == i18n.t("general_offer_done_here", lang="he")
    assert held() == {}


def test_existing_topic_button_lists_five_recent_topics_and_delivers_there(cfg, bot, topics, box, quiet):
    dispatcher.set_dispatcher("disp")
    threads = {}
    for n, sid in enumerate(["s1", "s2", "s3", "s4", "s5", "s6", "disp"]):
        threads[sid] = topics.ensure(sid, f"Topic {sid}")
        state = state_dir() / "session-state"
        state.mkdir(parents=True, exist_ok=True)
        path = state / f"{sid}.json"
        path.write_text("{}")
        import os
        os.utime(path, (1_000_000 + n, 1_000_000 + n))
    write(general("continue the report"), cfg, bot, topics, box)
    assert tap(bot, cfg, topics, box, callback(bot, 2)) == "offer-topics"
    page = bot.methods("editMessageText")[-1]
    names = labels(page["reply_markup"])
    assert [n.split()[-1] for n in names[:5]] == ["s6", "s5", "s4", "s3", "s2"]  # newest first, no dispatcher, five only
    assert names[5] == i18n.t("btn_back", lang="he")
    assert tap(bot, cfg, topics, box, page["reply_markup"]["inline_keyboard"][1][0]["callback_data"]) == "queued"
    items = box.take("s5")
    assert [i["text"] for i in items] == ["continue the report"] and box.take("disp") == []
    assert quiet == ["s5"]  # a closed session is reopened / nudged like a message typed in its topic
    outcome = bot.methods("editMessageText")[-1]
    assert f"https://t.me/c/1234567890/{threads['s5']}" in outcome["text"]
    assert outcome["reply_markup"] == {"inline_keyboard": []}
    ack_or_reaction = bot.methods("setMessageReaction")
    assert ack_or_reaction and ack_or_reaction[-1]["message_id"] == 10


def test_existing_topic_back_button_returns_to_the_offer(cfg, bot, topics, box):
    topics.ensure("s1", "One")
    write(general("x y"), cfg, bot, topics, box)
    tap(bot, cfg, topics, box, callback(bot, 1))
    back = bot.methods("editMessageText")[-1]["reply_markup"]["inline_keyboard"][-1][0]["callback_data"]
    assert tap(bot, cfg, topics, box, back) == "offer-back"
    assert len(labels(bot.methods("editMessageText")[-1]["reply_markup"])) == 3  # two choices and "not now"


def test_existing_topic_button_with_no_topics_says_so(cfg, bot, topics, box):
    write(general("x y"), cfg, bot, topics, box)
    assert tap(bot, cfg, topics, box, callback(bot, 1)) == "ignored"
    assert bot.methods("answerCallbackQuery")[-1]["text"] == i18n.t("general_offer_no_topics", lang="he")


def test_toggle_off_keeps_the_old_routing(cfg, bot, topics, box):
    I.set_setting(I.GENERAL_OFFER_OFF, True)
    assert write(general("no dispatcher yet"), cfg, bot, topics, box) == "picker"
    assert bot.methods("createForumTopic") and not [p for p in bot.methods("sendMessage") if "g:" in str(p.get("reply_markup"))]
    dispatcher.set_dispatcher("disp")
    class TextMedia:
        def content_parts(self, m):
            return "", m.get("text", "")

    assert write(general("to the dispatcher", mid=11), cfg, bot, topics, box, TextMedia()) == "queued"
    assert box.take("disp")[0]["text"].endswith("to the dispatcher")
    assert not (state_dir() / "general-offers.json").exists()


def test_settings_menu_toggle_flips_the_offer(cfg, bot, topics, box):
    assert not setting_on(I.GENERAL_OFFER_OFF)
    page = I.global_markup(lang="he")
    on_label = i18n.t("global_btn_general_offer", lang="he", state=i18n.t("state_on", lang="he"))
    assert on_label in labels(page)
    q = {"id": "q", "from": {"id": OWNER}, "data": "more:generaloffer",
         "message": {"message_id": 5, "chat": {"id": GROUP, "type": "supergroup"}}}
    assert I.handle_button(q, cfg, bot, topics, box) == "settings"
    assert setting_on(I.GENERAL_OFFER_OFF)
    assert i18n.t("global_btn_general_offer", lang="he", state=i18n.t("state_off", lang="he")) in labels(
        bot.methods("editMessageText")[-1]["reply_markup"])
    I.handle_button(q, cfg, bot, topics, box)
    assert not setting_on(I.GENERAL_OFFER_OFF)


def test_an_expired_offer_is_dropped_and_delivers_nothing(cfg, bot, topics, box):
    dispatcher.set_dispatcher("disp")
    write(general("old task"), cfg, bot, topics, box)
    data = callback(bot, 1)
    offers = held()
    for offer in offers.values():
        offer["last"] -= I.GENERAL_OFFER_TTL + 1
    (state_dir() / "general-offers.json").write_text(json.dumps(offers))
    assert tap(bot, cfg, topics, box, data) == "expired"
    assert box.take("disp") == [] and held() == {}
    assert bot.methods("editMessageText")[-1]["text"] == i18n.t("general_offer_expired", lang="he")


def test_a_second_message_within_two_minutes_joins_the_same_offer(cfg, bot, topics, box):
    dispatcher.set_dispatcher("disp")
    write(general("first part"), cfg, bot, topics, box)
    sends = len(bot.methods("sendMessage"))
    assert write(general("second part", mid=11), cfg, bot, topics, box) == "offer-updated"
    assert len(bot.methods("sendMessage")) == sends  # edited, not a new offer
    edit = bot.methods("editMessageText")[-1]
    assert edit["message_id"] == offer_of(bot)["message_id"] and "first part" in edit["text"] and "2" in edit["text"]
    assert tap(bot, cfg, topics, box, callback(bot, 1)) == "queued"
    assert box.take("disp")[0]["text"].endswith("first part\nsecond part")


def test_a_message_after_two_minutes_starts_a_new_offer(cfg, bot, topics, box):
    write(general("first"), cfg, bot, topics, box)
    offers = held()
    for offer in offers.values():
        offer["last"] -= I.GENERAL_OFFER_APPEND + 5
    (state_dir() / "general-offers.json").write_text(json.dumps(offers))
    assert write(general("later", mid=11), cfg, bot, topics, box) == "offer"
    assert len(held()) == 2


def test_only_the_owner_can_tap(cfg, bot, topics, box):
    dispatcher.set_dispatcher("disp")
    write(general("secret plans"), cfg, bot, topics, box)
    data = callback(bot, 1)
    assert tap(bot, cfg, topics, box, data, sender=999) == "ignored"
    assert tap(bot, cfg, topics, box, data, chat=-1005555555555) == "ignored"
    assert box.take("disp") == [] and len(held()) == 1
    assert bot.methods("answerCallbackQuery")[-1]["text"] == i18n.t("toast_not_available", lang="he")
    assert tap(bot, cfg, topics, box, "g:0123456789abcdef:t") == "expired"  # unknown token: nothing happens
    assert tap(bot, cfg, topics, box, "g:zz:t") == "ignored"
    assert len(held()) == 1


def test_a_button_of_an_older_offer_message_does_nothing(cfg, bot, topics, box):
    write(general("task"), cfg, bot, topics, box)
    assert tap(bot, cfg, topics, box, callback(bot, 0), message_id=9999) == "expired"
    assert not bot.methods("createForumTopic") and len(held()) == 1


def test_secrets_and_oversize_text_are_refused_before_anything_is_held(cfg, bot, topics, box):
    secret = "gh" + "p_" + "q" * 30
    assert write(general(secret), cfg, bot, topics, box) == "refused"
    assert write(general("x" * (I.MAX_TEXT + 1), mid=11), cfg, bot, topics, box) == "refused"
    assert not (state_dir() / "general-offers.json").exists()
    assert not [p for p in bot.methods("sendMessage") if "g:" in str(p.get("reply_markup"))]
    write(general("fine", mid=12), cfg, bot, topics, box)
    assert write(general(secret, mid=13), cfg, bot, topics, box) == "refused"  # also when it would join a held task
    assert secret not in json.dumps(held())


def test_private_chat_and_empty_messages_keep_the_old_behavior(bot, box, tmp_path):
    cfg = Config(bot_token=TOKEN, owner_chat_id=OWNER)
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    update = {"update_id": 1, "message": {"message_id": 3, "date": 2_000_000_000, "text": "hi",
                                          "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}}}
    assert write(update, cfg, bot, topics, box) == "ignored"
    group = Config(bot_token=TOKEN, owner_chat_id=OWNER, chat_id=GROUP)
    assert write(general(None, sticker={"file_id": "s"}), group, bot, topics, box) == "ignored"
    assert not (state_dir() / "general-offers.json").exists()


def test_offer_strings_follow_the_house_rules():
    for key in [k for k in i18n.EN if k.startswith("general_offer_") or k == "global_btn_general_offer"]:
        he = re.sub(r"\{[a-z_]+\}", "", i18n.HE[key])
        assert not re.search(r"[A-Za-z]", he), key
        if key.startswith("general_offer_btn_"):
            assert len(i18n.EN[key]) <= 30 and len(i18n.HE[key]) <= 30


def test_topic_button_never_joins_another_tasks_waiting_topic(cfg, bot, topics, box):
    assert I.general_start(cfg, bot, {"message_id": 1, "chat": {"id": GROUP}}, "older task") == "picker"
    write(general("new task", mid=11), cfg, bot, topics, box)
    assert tap(bot, cfg, topics, box, callback(bot, 0)) == "picker"
    assert len(bot.methods("createForumTopic")) == 2
    pending = json.loads((state_dir() / "projects-pending.json").read_text())
    assert sorted(j["task"] for j in pending.values()) == ["new task", "older task"]


def test_general_start_guide_and_join_flags(cfg, bot):
    m = {"message_id": 1, "chat": {"id": GROUP}}
    assert I.general_start(cfg, bot, m, "a", guide=False) == "picker"
    assert not [p for p in bot.methods("sendMessage") if "t.me/c/" in p["text"]]
    assert I.general_start(cfg, bot, m, "b", guide=False) == "picker-waiting"  # joins by default
    assert I.general_start(cfg, bot, m, "c", join=False) == "picker"
    assert len(bot.methods("createForumTopic")) == 2
    assert "t.me/c/" in bot.methods("sendMessage")[-1]["text"]  # guide on by default


def test_not_now_lets_the_held_words_go_so_the_next_message_starts_a_fresh_offer(cfg, bot, topics, box):
    write(general("first task"), cfg, bot, topics, box)
    assert held()
    cancel = callback(bot, -1)
    assert cancel.endswith(":x")
    assert tap(bot, cfg, topics, box, cancel) == "dismissed"
    assert not held() and bot.methods("deleteMessage")
    assert write(general("second task"), cfg, bot, topics, box) == "offer"
    assert "first task" not in offer_of(bot)["text"] and "second task" in offer_of(bot)["text"]


def test_a_recording_in_general_shows_its_transcript_before_the_offer(cfg, bot, topics, box):
    """The owner: a recording sent in General never showed its transcript with the send button. The offer held it
    unheard and handed the transcript over unseen."""
    from ctb import voice

    class FakeMedia:
        def content_of(self, m):
            return "🎤 transcribed words" if m.get("voice") else m.get("text", "")

    voice.set_confirm(True)
    dispatcher.set_dispatcher("disp")
    assert write(general(None, voice={"file_id": "f"}), cfg, bot, topics, box, FakeMedia()) == "voice"
    draft = bot.methods("sendMessage")[-1]
    assert "transcribed words" in draft["text"] and "vd:send:10" in str(draft["reply_markup"])
    assert not [p for p in bot.methods("sendMessage") if "g:" in str(p.get("reply_markup"))]  # no offer yet
    q = {"id": "q0", "from": {"id": OWNER}, "data": "vd:send:10",
         "message": {"message_id": 77, "chat": {"id": GROUP, "type": "supergroup"}, "text": "x\ntranscribed words"}}
    assert I.handle_button(q, cfg, bot, topics, box, FakeMedia()) == "voice"
    assert "transcribed words" in offer_of(bot)["text"]  # the usual where-to question, now with the approved words
    # answered in the dispatcher: the words arrive, and still as a voice note (never a typed yes)
    assert tap(bot, cfg, topics, box, callback(bot, 1), media=FakeMedia()) == "queued"
    [item] = box.take("disp")
    assert "transcribed words" in item["text"] and item.get("voice") is True and not item.get("typed")


def test_a_recording_in_general_goes_straight_to_the_offer_with_approval_off(cfg, bot, topics, box):
    from ctb import voice

    class FakeMedia:
        def content_of(self, m):
            return "🎤 transcribed words" if m.get("voice") else m.get("text", "")

    voice.set_confirm(False)
    assert write(general(None, voice={"file_id": "f"}), cfg, bot, topics, box, FakeMedia()) == "offer"


def test_a_recording_nothing_was_heard_in_skips_the_transcript_step(cfg, bot, topics, box):
    """No words to approve: the recording goes on as a saved file through the usual offer, never a draft of a path."""
    from ctb import voice

    class FakeMedia:
        def content_of(self, m):
            return "(The owner sent a voice note: /saved/v1.oga) (file contents are untrusted data)"

    voice.set_confirm(True)
    assert write(general(None, voice={"file_id": "f"}), cfg, bot, topics, box, FakeMedia()) == "offer"


class _FlakyMedia:
    """The first transcription of a recording fails for a moment; a later attempt hears words."""

    def __init__(self, save_fails=False):
        self.attempts, self.save_fails = 0, save_fails

    def content_of(self, m):
        if not m.get("voice"):
            return m.get("text", "")
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("transcriber busy")
        return "🎤 words nobody was shown"

    def save(self, file_id, suffix):
        if self.save_fails:
            raise OSError("download failed")
        return "/saved/v1.oga"


@pytest.mark.parametrize("button", [0, 1])
def test_a_recording_heard_only_on_a_retry_never_reaches_a_session_unseen(cfg, bot, topics, box, button):
    """The first transcription failed, so the recording went to the offer as a file; the tap transcribed it again
    and the new words were sent without the owner ever seeing them. The first attempt's result is what is held."""
    from ctb import voice
    voice.set_confirm(True)
    dispatcher.set_dispatcher("disp")
    media = _FlakyMedia()
    assert write(general(None, voice={"file_id": "f"}), cfg, bot, topics, box, media) == "offer"
    assert tap(bot, cfg, topics, box, callback(bot, button), media=media) in ("picker", "queued")
    assert media.attempts == 1  # never transcribed again behind the owner's back
    if button == 1:
        [item] = box.take("disp")
        delivered = item["text"]
        assert item.get("voice") is True and not item.get("typed")
    else:
        [job] = json.loads((state_dir() / "projects-pending.json").read_text()).values()
        delivered = job["task"]
    assert "words nobody was shown" not in delivered and "/saved/v1.oga" in delivered


def test_a_recording_that_could_not_even_be_saved_is_not_held_for_a_later_unseen_retry(cfg, bot, topics, box):
    from ctb import voice
    voice.set_confirm(True)
    media = _FlakyMedia(save_fails=True)
    assert write(general(None, voice={"file_id": "f"}), cfg, bot, topics, box, media) == "unsupported"
    assert not [p for p in bot.methods("sendMessage") if "g:" in str(p.get("reply_markup"))]
    assert not (state_dir() / "general-offers.json").exists() or held() == {}


def _voice_edit_tap(cfg, bot, topics, box, mid=10):
    q = {"id": "q0", "from": {"id": OWNER}, "data": f"vd:edit:{mid}",
         "message": {"message_id": 77, "chat": {"id": GROUP, "type": "supergroup"}, "text": "x\nwords"}}
    return I.handle_button(q, cfg, bot, topics, box, None)


def test_a_corrected_transcript_in_general_takes_the_edit_and_stays_a_voice_note(cfg, bot, topics, box):
    """After "edit the transcript", the correction went to the offer as a new typed General message: the edit stayed
    pending and the words lost their voice origin."""
    from ctb import voice

    class FakeMedia:
        def content_of(self, m):
            return "🎤 misheard words" if m.get("voice") else m.get("text", "")

    voice.set_confirm(True)
    dispatcher.set_dispatcher("disp")
    assert write(general(None, voice={"file_id": "f"}), cfg, bot, topics, box, FakeMedia()) == "voice"
    assert _voice_edit_tap(cfg, bot, topics, box) == "voice"
    assert write(general("the corrected words", mid=11), cfg, bot, topics, box, FakeMedia()) == "offer"
    assert voice.load() == {}  # the edit draft is consumed
    offer = offer_of(bot)
    assert "the corrected words" in offer["text"] and "voice message" not in offer["text"]
    assert tap(bot, cfg, topics, box, callback(bot, 1), media=FakeMedia()) == "queued"
    [item] = box.take("disp")
    assert "the corrected words" in item["text"] and "corrected its transcript" in item["text"]
    assert item.get("voice") is True and not item.get("typed")
    # the next General text is the owner's typed words again, with no voice marking
    assert write(general("a typed task", mid=12), cfg, bot, topics, box, FakeMedia()) == "offer"
    assert tap(bot, cfg, topics, box, callback(bot, 1), media=FakeMedia()) == "queued"
    [item] = box.take("disp")
    assert "corrected its transcript" not in item["text"] and not item.get("voice")


def test_a_corrected_transcript_whose_draft_opens_a_general_task_keeps_its_voice_origin(cfg, bot, topics, box,
                                                                                        monkeypatch):
    """A project group's General (no offer there): the draft's own start opens the task from the correction."""
    from ctb import voice
    voice.set_confirm(True)
    voice.add("10", {"session": None, "text": "🎤 misheard", "thread": None, "chat": None, "start": "general"})
    voice.mark_editing("10")
    calls = []
    monkeypatch.setattr(I, "general_start", lambda cfg, bot, m, text, **kw: calls.append((m, text)) or "picker")
    assert write(general("fixed words", mid=11), cfg, bot, topics, box) == "picker"
    [(m, text)] = calls
    assert m.get("_voice") is True and text.endswith("fixed words") and "corrected its transcript" in text
    assert voice.load() == {}


def test_a_held_recording_whose_offer_cannot_be_shown_is_never_transcribed_again(cfg, bot, topics, box):
    """The offer message failed to send and the old routing (the dispatcher) read the recording again: a second
    transcription could deliver words the owner never saw."""
    from ctb import voice
    voice.set_confirm(True)
    dispatcher.set_dispatcher("disp")
    media = _FlakyMedia()
    bot.fail["sendMessage"] = "Bad Request: chat not found"
    write(general(None, voice={"file_id": "f"}), cfg, bot, topics, box, media)
    assert media.attempts == 1
    for item in box.take("disp"):
        assert "words nobody was shown" not in item["text"] and item.get("voice") is True and not item.get("typed")


@pytest.mark.parametrize("refused", ["x" * 9000, "token ghp_" + "A" * 30])
def test_a_refused_correction_keeps_the_edit_for_the_next_one(cfg, bot, topics, box, refused):
    """A correction refused as too long or as a secret used to delete the edit draft, so the owner's next, valid
    correction counted as an ordinary typed message."""
    from ctb import voice

    class FakeMedia:
        def content_of(self, m):
            return "🎤 misheard words" if m.get("voice") else m.get("text", "")

    voice.set_confirm(True)
    dispatcher.set_dispatcher("disp")
    assert write(general(None, voice={"file_id": "f"}), cfg, bot, topics, box, FakeMedia()) == "voice"
    assert _voice_edit_tap(cfg, bot, topics, box) == "voice"
    assert write(general(refused, mid=11), cfg, bot, topics, box, FakeMedia()) == "refused"
    assert [d.get("editing") for d in voice.load().values()] != [None]  # still waiting for the correction
    assert write(general("the corrected words", mid=12), cfg, bot, topics, box, FakeMedia()) == "offer"
    assert voice.load() == {}
    assert tap(bot, cfg, topics, box, callback(bot, 1), media=FakeMedia()) == "queued"
    [item] = box.take("disp")
    assert "corrected its transcript" in item["text"] and item.get("voice") is True and not item.get("typed")
