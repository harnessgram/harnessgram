import json

from conftest import GOOD_TOKEN, FakeBot
from ctb import state_emoji
from ctb.config import Config, state_dir, write_private
from ctb.telegram import TelegramError
from ctb.topics import TopicMap, session_state, session_state_path, set_state


def cfg(**kwargs):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, **kwargs)


def working(config=None):
    config = config or cfg()
    bot = FakeBot()
    topics = TopicMap(state_dir() / "topics.json", bot, 7, cfg=config)
    topics.ensure("session", "Task")
    set_state(config, "session", "busy", bot)
    bot.calls.clear()
    return config, bot


def test_activity_mapping():
    assert state_emoji.for_activity("busy") == "🤔"
    assert state_emoji.for_activity("busy", "Read") == "🔎"
    assert state_emoji.for_activity("busy", "Edit") == state_emoji.DEFAULT
    assert state_emoji.for_activity("busy", "Bash", {"command": "pytest -q"}) == "🧪"
    assert state_emoji.for_activity("busy", "AskUserQuestion") == "✋"
    assert state_emoji.for_activity("busy", "Agent") == "🤝"
    assert state_emoji.for_activity("busy", "Bash", {"command": "build and upload"}) == "🚀"
    assert state_emoji.for_activity("done") == "✅"


def test_update_debounces_and_skips_unchanged():
    config, bot = working()
    assert state_emoji.update(config, "session", {"tool_name": "Read"}, bot, now=100)
    assert not state_emoji.update(config, "session", {"tool_name": "Bash"}, bot, now=119)
    assert state_emoji.update(config, "session", {"tool_name": "Bash"}, bot, now=120)
    assert not state_emoji.update(config, "session", {"tool_name": "Bash"}, bot, now=200)
    edits = bot.methods("editMessageText")
    assert [call["text"] for call in edits] == ["🔎", "🧪"]
    assert all("disable_notification" not in call for call in edits)


def test_setting_off_restores_and_keeps_default():
    config, bot = working(cfg(state_emoji=False))
    path = session_state_path("session")
    current = session_state("session")
    current.update(placeholder_emoji="🔎", placeholder_emoji_at=1)
    write_private(path, json.dumps(current))
    assert state_emoji.update(config, "session", {"tool_name": "Read"}, bot, now=100)
    assert bot.methods("editMessageText")[-1]["text"] == state_emoji.DEFAULT
    assert not state_emoji.update(config, "session", {"tool_name": "Agent"}, bot, now=200)


def test_edit_failure_keeps_old_emoji():
    config, bot = working()
    bot.fail["editMessageText"] = TelegramError("temporary")
    assert not state_emoji.update(config, "session", {"tool_name": "Read"}, bot, now=100)
    assert session_state("session")["placeholder_emoji"] == state_emoji.DEFAULT
    assert not state_emoji.update(config, "session", {"tool_name": "Read"}, bot, now=119)
    assert len(bot.methods("editMessageText")) == 1


def test_debounced_or_unchanged_calls_never_read_the_topic_map(monkeypatch):
    config, bot = working()
    assert state_emoji.update(config, "session", {"tool_name": "Read"}, bot, now=100)
    def boom(*_args, **_kwargs):
        raise AssertionError("topic map read on a call that changes nothing")
    monkeypatch.setattr(state_emoji.__dict__.get("TopicMap", TopicMap), "place_of", boom)
    assert not state_emoji.update(config, "session", {"tool_name": "Bash"}, bot, now=110)
    assert not state_emoji.update(config, "session", {"tool_name": "Read"}, bot, now=200)
