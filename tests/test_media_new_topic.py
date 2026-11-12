"""A recording, picture, video or file written in a General topic starts a task exactly like typed text:
all three offer buttons (new topic / answer here / existing topic) and the project-group General (no
offer) carry the transcript or the saved path, never drop it, and a failure is said out loud."""
import json

import pytest

from ctb import dispatcher, i18n
from ctb import inbox as I
from ctb.config import Config, ProjectConfig, state_dir
from ctb.topics import TopicMap

from test_general_offer import (GROUP, OWNER, TOKEN, callback, general, held, labels, offer_of, tap,  # noqa: F401
                                write)

PROJECT_GROUP = -1009876543210

KINDS = {
    "voice": {"voice": {"file_id": "v1"}},
    "audio": {"audio": {"file_id": "a1"}},
    "video_note": {"video_note": {"file_id": "n1"}},
    "photo": {"photo": [{"file_id": "p0"}, {"file_id": "p1"}]},
    "video": {"video": {"file_id": "m1"}},
    "animation": {"animation": {"file_id": "g1"}},
    "document": {"document": {"file_id": "d1", "file_name": "plan.pdf"}},
}


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(I, "projects", lambda *a, **k: ["/tmp/proj1", "/tmp/proj2"])
    monkeypatch.setattr(I, "_reopen_or_nudge", lambda inbox, session: None)

@pytest.fixture
def no_transcript_step():
    """These tests follow a recording through the offer itself; with transcript approval on, the transcript is
    shown first (tests/test_general_offer.py)."""
    from ctb import voice as _voice
    _voice.set_confirm(False)


@pytest.fixture
def cfg():
    return Config(bot_token=TOKEN, owner_chat_id=OWNER, chat_id=GROUP, ui_language="he")


@pytest.fixture
def topics(bot):
    return TopicMap(state_dir() / "topics.json", bot, GROUP)


@pytest.fixture
def box(tmp_path):
    return I.Inbox(tmp_path / "inbox")


@pytest.fixture
def media(cfg, bot, tmp_path, monkeypatch):
    """The real Media: only the download and the speech-to-text engine are stubbed."""
    m = I.Media(cfg, bot, tmp_path / "files")
    saved = []

    def save(file_id, suffix):
        saved.append(file_id)
        return f"/saved/{file_id}{suffix}"

    monkeypatch.setattr(m, "save", save)
    monkeypatch.setattr(m, "transcribe", lambda path: "fix the login page")
    m.saved = saved
    return m


def approve(bot, cfg, topics, box, media, chat=GROUP, thread=None, mode="send"):
    """Tap a button under the last transcript shown for approval."""
    card = [p for p in bot.methods("sendMessage") if "vd:" in str(p.get("reply_markup"))][-1]
    data = next(b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row
                if b["callback_data"].startswith(f"vd:{mode}:"))
    q = {"id": "q1", "from": {"id": OWNER}, "data": data,
         "message": {"message_id": 900, "chat": {"id": chat, "type": "supergroup"}, "text": "heard\nfix",
                     **({"message_thread_id": thread} if thread else {})}}
    return I.handle_button(q, cfg, bot, topics, box, media)


def no_task_yet():
    return not (state_dir() / "projects-pending.json").exists()


def pending_tasks():
    data = json.loads((state_dir() / "projects-pending.json").read_text())
    return [job["task"] for job in data.values()]


@pytest.mark.usefixtures("no_transcript_step")
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_new_topic_button_starts_a_task_from_every_kind_of_media(kind, cfg, bot, topics, box, media):
    assert write(general(None, **KINDS[kind]), cfg, bot, topics, box, media) == "offer"
    assert tap(bot, cfg, topics, box, callback(bot, 0), media=media) == "picker"
    [task] = pending_tasks()
    assert task.strip()
    if kind in ("voice", "audio", "video_note"):
        assert "fix the login page" in task
    else:
        assert f"/saved/{KINDS[kind][kind][-1]['file_id'] if kind == 'photo' else KINDS[kind][kind]['file_id']}" in task
    name = bot.methods("createForumTopic")[-1]["name"]
    assert name.strip() and "/saved/" not in name and "(The owner" not in name  # the label and path are not a title
    assert held() == {}
    assert bot.methods("editMessageText")[-1]["reply_markup"] == {"inline_keyboard": []}


@pytest.mark.usefixtures("no_transcript_step")
def test_topic_title_is_what_was_said_or_the_caption(cfg, bot, topics, box, media):
    write(general(None, voice={"file_id": "v1"}), cfg, bot, topics, box, media)
    tap(bot, cfg, topics, box, callback(bot, 0), media=media)
    assert bot.methods("createForumTopic")[-1]["name"] == "fix the login page"
    write(general(None, mid=20, photo=[{"file_id": "p1"}], caption="Rebuild the footer"), cfg, bot, topics, box, media)
    tap(bot, cfg, topics, box, callback(bot, 0), media=media)
    assert bot.methods("createForumTopic")[-1]["name"] == "Rebuild the footer"


def test_a_picture_without_words_gets_the_attachment_label_as_its_title(cfg, bot, topics, box, media):
    write(general(None, photo=[{"file_id": "p1"}]), cfg, bot, topics, box, media)
    tap(bot, cfg, topics, box, callback(bot, 0), media=media)
    assert bot.methods("createForumTopic")[-1]["name"] == "קובץ מצורף"


@pytest.mark.usefixtures("no_transcript_step")
@pytest.mark.parametrize("kind", ["voice", "photo", "document"])
def test_answer_here_button_delivers_media_to_the_dispatcher(kind, cfg, bot, topics, box, media):
    dispatcher.set_dispatcher("disp")
    write(general(None, **KINDS[kind]), cfg, bot, topics, box, media)
    assert tap(bot, cfg, topics, box, callback(bot, 1), media=media) == "queued"
    [item] = box.take("disp")
    assert item["text"].startswith("NEW TASK from a new Telegram chat")
    assert ("fix the login page" if kind == "voice" else "/saved/") in item["text"]


def test_existing_topic_button_delivers_media(cfg, bot, topics, box, media):
    topics.ensure("s1", "One")
    write(general(None, photo=[{"file_id": "p1"}], caption="look at this"), cfg, bot, topics, box, media)
    assert tap(bot, cfg, topics, box, callback(bot, 1), media=media) == "offer-topics"
    page = bot.methods("editMessageText")[-1]
    assert tap(bot, cfg, topics, box, page["reply_markup"]["inline_keyboard"][0][0]["callback_data"], media=media) == "queued"
    [item] = box.take("s1")
    assert "/saved/p1.jpg" in item["text"] and "look at this" in item["text"]


def test_a_recording_that_cannot_be_transcribed_is_passed_on_as_a_file(cfg, bot, topics, box, media, monkeypatch):
    monkeypatch.setattr(media, "transcribe", lambda path: "")  # no whisper model, or it heard nothing
    write(general(None, voice={"file_id": "v1"}), cfg, bot, topics, box, media)
    assert tap(bot, cfg, topics, box, callback(bot, 0), media=media) == "picker"
    [task] = pending_tasks()
    assert "/saved/v1.oga" in task and "voice note" in task and "untrusted" in task


def test_a_failing_transcription_engine_still_passes_the_recording(cfg, bot, topics, box, media, monkeypatch):
    def boom(path):
        raise RuntimeError("ffmpeg failed")

    monkeypatch.setattr(media, "transcribe", boom)
    write(general(None, voice={"file_id": "v1"}), cfg, bot, topics, box, media)
    assert tap(bot, cfg, topics, box, callback(bot, 0), media=media) == "picker"
    assert "/saved/v1.oga" in pending_tasks()[0]


@pytest.mark.parametrize("button", [0, 1])
def test_an_unreadable_attachment_is_said_out_loud_and_kept(button, cfg, bot, topics, box, media, monkeypatch):
    dispatcher.set_dispatcher("disp")

    def broken(file_id, suffix):
        raise OSError("download failed")

    monkeypatch.setattr(media, "save", broken)
    write(general(None, photo=[{"file_id": "p1"}]), cfg, bot, topics, box, media)
    assert tap(bot, cfg, topics, box, callback(bot, button), media=media) == "unsupported"
    shown = bot.methods("editMessageText")[-1]
    assert i18n.t("general_offer_unreadable", lang="he") in shown["text"]
    assert len(labels(shown["reply_markup"])) == 4  # the buttons are back (with "not now"): try again once it is fixed
    assert len(held()) == 1  # the task is still held
    assert not bot.methods("createForumTopic")


def test_the_general_of_a_project_group_starts_a_task_from_a_recording(bot, box, media, monkeypatch):
    cfg = Config(bot_token=TOKEN, owner_chat_id=OWNER, chat_id=GROUP, ui_language="he",
                 projects=[ProjectConfig(name="Alpha", chat_id=PROJECT_GROUP)])
    topics = TopicMap(state_dir() / "topics.json", bot, GROUP)
    media.cfg = cfg
    update = general(None, voice={"file_id": "v1"})
    update["message"]["chat"] = {"id": PROJECT_GROUP, "type": "supergroup"}
    assert write(update, cfg, bot, topics, box, media) == "voice"  # the transcript is approved first
    assert no_task_yet() and not bot.methods("createForumTopic")
    assert approve(bot, cfg, topics, box, media, chat=PROJECT_GROUP) == "voice"
    assert bot.methods("createForumTopic")[-1]["chat_id"] == PROJECT_GROUP
    assert bot.methods("createForumTopic")[-1]["name"] == "fix the login page"
    assert pending_tasks() == ["🎤 (הודעה קולית, תומללה) fix the login page"]


def test_a_recording_in_general_with_the_offer_switched_off_is_not_dropped(cfg, bot, topics, box, media):
    from ctb.config import write_private
    write_private(state_dir() / I.GENERAL_OFFER_OFF, "")
    assert write(general(None, voice={"file_id": "v1"}), cfg, bot, topics, box, media) == "voice"
    assert no_task_yet()
    approve(bot, cfg, topics, box, media)
    assert "fix the login page" in pending_tasks()[0]


def test_a_sticker_is_still_ignored(cfg, bot, topics, box, media):
    from ctb.config import write_private
    write_private(state_dir() / I.GENERAL_OFFER_OFF, "")
    assert write(general(None, sticker={"file_id": "s"}), cfg, bot, topics, box, media) == "ignored"


def test_a_recording_in_a_topic_without_a_session_opens_the_project_picker(cfg, bot, topics, box, media):
    update = general(None, voice={"file_id": "v1"}, message_thread_id=555)
    assert write(update, cfg, bot, topics, box, media) == "voice"
    assert no_task_yet()
    approve(bot, cfg, topics, box, media, thread=555)
    assert "fix the login page" in pending_tasks()[0]


def test_unreadable_recording_in_a_topic_without_a_session_still_opens_the_picker(cfg, bot, topics, box, media, monkeypatch):
    monkeypatch.setattr(media, "transcribe", lambda path: "")
    update = general(None, voice={"file_id": "v1"}, message_thread_id=555)
    assert write(update, cfg, bot, topics, box, media) == "voice"
    approve(bot, cfg, topics, box, media, thread=555)
    assert "/saved/v1.oga" in pending_tasks()[0]


# --- review of #137 (finding 3): the owner's own recording never starts a task unapproved ---------------

def test_a_cancelled_transcript_starts_no_task_in_a_project_group_general(bot, box, media):
    cfg = Config(bot_token=TOKEN, owner_chat_id=OWNER, chat_id=GROUP, ui_language="he",
                 projects=[ProjectConfig(name="Alpha", chat_id=PROJECT_GROUP)])
    topics = TopicMap(state_dir() / "topics.json", bot, GROUP)
    media.cfg = cfg
    update = general(None, voice={"file_id": "v1"})
    update["message"]["chat"] = {"id": PROJECT_GROUP, "type": "supergroup"}
    assert write(update, cfg, bot, topics, box, media) == "voice"
    card = [p for p in bot.methods("sendMessage") if "vd:" in str(p.get("reply_markup"))][-1]
    assert card["chat_id"] == PROJECT_GROUP and "fix the login page" in card["text"]
    approve(bot, cfg, topics, box, media, chat=PROJECT_GROUP, mode="cancel")
    assert no_task_yet() and not bot.methods("createForumTopic")


def test_with_approval_off_or_driving_a_recording_starts_its_task_at_once(cfg, bot, topics, box, media, monkeypatch):
    from ctb import voice
    voice.set_confirm(False)
    update = general(None, voice={"file_id": "v1"}, message_thread_id=555)
    assert write(update, cfg, bot, topics, box, media) == "picker"
    voice.set_confirm(True)
    monkeypatch.setattr(I, "car_mode_active", lambda: True)
    assert write(general(None, mid=11, voice={"file_id": "v2"}, message_thread_id=556), cfg, bot, topics, box,
                 media) == "picker"


def test_a_forwarded_recording_is_not_approved_as_the_owners_words(cfg, bot, topics, box, media):
    update = general(None, voice={"file_id": "v1"}, message_thread_id=555, forward_origin={"type": "user"})
    assert write(update, cfg, bot, topics, box, media) == "picker"  # framed as someone else's (finding 1)
    assert not [p for p in bot.methods("sendMessage") if "vd:" in str(p.get("reply_markup"))]
