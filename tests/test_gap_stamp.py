"""The status card names the model, the Claude account (when there are several) and the thinking level."""
import json

from conftest import GOOD_TOKEN
from ctb import accounts, mirror
from ctb.config import Config, state_dir, write_private
from ctb.topics import TopicMap

OWNER = 7


def cfg(**kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, **kw)


def models(session, value):
    write_private(state_dir() / "session-models.json", json.dumps({session: value}))


def test_stamp_lists_model_and_thinking_level_one_per_line():
    models("s", "Opus 5.5|high")
    assert mirror.session_stamp("s", "en") == "🧠 Opus 5.5\n🎚️ Thinking: high"
    assert mirror.session_stamp("s", "he") == "🧠 Opus 5.5\n‏🎚️ חשיבה גבוהה"


def test_stamp_is_empty_for_an_unknown_session():
    assert mirror.session_stamp(None) == "" and mirror.session_stamp("nope", "en") == ""


def test_account_line_only_with_several_accounts():
    models("s", "Opus 5.5")
    assert "👤" not in mirror.session_stamp("s", "en")
    (accounts.profiles_dir() / "work").mkdir(parents=True)
    write_private(state_dir() / "account-emails.json", json.dumps({"work": "dana@example.com"}))
    accounts.record("s", "work")
    assert mirror.session_stamp("s", "en") == "🧠 Opus 5.5\n👤 dana"


def test_status_card_text_carries_the_stamp(bot):
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure("s", "Title")
    models("s", "Sonnet 5.5|low")
    mirror.status_message(cfg(ui_language="en"), thread, "📏 Context 50k", mirror.get_keyboard("en"), bot=bot)
    text = bot.methods("sendMessage")[-1]["text"]
    assert text == "📊 Status\n🧠 Sonnet 5.5\n🎚️ Thinking: low\n📏 Context 50k"
