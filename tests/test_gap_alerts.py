"""The optional alerts channel: registration by the owner only, one-way copies of what needs a human."""
import json

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import alerts, inbox as I, mirror
from ctb.config import Config, state_dir
from ctb.topics import TopicMap

OWNER = 7
CHANNEL = -1001234


def cfg(**kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, **dict({"ui_language": "en"}, **kw))


class AdminBot(FakeBot):
    def __init__(self, admins):
        super().__init__()
        self.admins = admins

    def call(self, method, params=None, timeout=15):
        super().call(method, params, timeout)
        if method == "getChatAdministrators":
            return [{"user": {"id": u}} for u in self.admins]
        if method == "getMe":
            return {"username": "demo_bot"}
        return {}


def promoted(by=OWNER):
    return {"chat": {"id": CHANNEL, "type": "channel", "title": "Alerts"}, "from": {"id": by},
            "new_chat_member": {"status": "administrator"}}


def test_owner_making_the_bot_admin_registers_the_channel():
    bot = AdminBot([OWNER])
    assert alerts.joined(promoted(), cfg(), bot) == "registered"
    assert alerts.channel() == {"id": CHANNEL, "title": "Alerts", "bot": "demo_bot"}
    assert [p["chat_id"] for p in bot.methods("sendMessage")] == [CHANNEL, OWNER, OWNER]  # the last is the settings panel


def test_someone_else_cannot_register_a_channel():
    bot = AdminBot([999])
    assert alerts.joined(promoted(by=999), cfg(), bot) == "ignored"
    assert alerts.channel() is None and bot.methods("sendMessage") == []


def test_removing_the_bot_turns_alerts_off():
    bot = AdminBot([OWNER])
    alerts.joined(promoted(), cfg(), bot)
    gone = dict(promoted(), new_chat_member={"status": "kicked"})
    assert alerts.joined(gone, cfg(), bot) == "removed"
    assert alerts.channel() is None
    assert alerts.joined({"chat": {"id": 5, "type": "private"}}, cfg(), bot) == "ignored"


def test_alert_is_a_noop_without_a_channel(bot):
    assert alerts.alert(cfg(), "x", "s", bot=bot) is False and bot.calls == []


def test_alert_names_the_topic_and_adds_an_open_chat_button(bot):
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "Billing fix")
    alerts.joined(promoted(), cfg(), AdminBot([OWNER]))
    bot.calls.clear()
    assert alerts.alert(cfg(), "waiting", "s", bot=bot, silent=True)
    sent = bot.methods("sendMessage")[0]
    assert sent["chat_id"] == CHANNEL and sent["text"] == "💬 Billing fix\nwaiting"
    assert sent["disable_notification"] is True
    assert sent["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://t.me/demo_bot"


def test_alert_never_raises(bot):
    alerts.joined(promoted(), cfg(), AdminBot([OWNER]))
    bot.fail["sendMessage"] = "boom"
    assert alerts.alert(cfg(), "x", bot=bot) is False


def test_join_updates_are_routed_by_the_listener(tmp_path):
    bot = AdminBot([OWNER])
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    box = I.Inbox(tmp_path / "inbox")
    assert "my_chat_member" in I.ALLOWED_UPDATES and "channel_post" in I.ALLOWED_UPDATES
    r = I.handle({"update_id": 1, "my_chat_member": promoted()}, cfg(), bot, topics, box, None, 0)
    assert r == "alerts-registered" and alerts.channel()["id"] == CHANNEL
    # a later channel post changes nothing once a channel is registered
    post = {"update_id": 2, "channel_post": {"chat": {"id": -5, "type": "channel"}}}
    assert I.handle(post, cfg(), bot, topics, box, None, 0) == "alerts-ignored"


def test_session_closing_and_waiting_copy_to_the_channel(bot, monkeypatch):
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "Title")
    alerts.joined(promoted(), cfg(), AdminBot([OWNER]))
    monkeypatch.setattr(mirror.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "terminal_card", lambda s: None)
    bot.calls.clear()
    mirror.run_lifecycle(cfg(), "s", "session-end", bot=bot)
    mirror.run_notify(cfg(), "s", "permission_prompt", bot=bot)
    mirror.run_notify(cfg(), "s", "permission_prompt", bot=bot)  # throttled: one ring per 10 minutes
    to_channel = [p["text"] for p in bot.methods("sendMessage") if p["chat_id"] == CHANNEL]
    assert to_channel == ["💬 Title\n⚪ The session is closed.", "💬 Title\n🔔 Waiting for your approval"]


def test_quota_out_waiting_for_a_choice_rings_the_channel(bot):
    from ctb import accounts
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "Title")
    alerts.joined(promoted(), cfg(), AdminBot([OWNER]))
    bot.calls.clear()
    (accounts.profiles_dir() / "other").mkdir(parents=True)
    accounts.toggle_auto()  # automatic fallback off: the session waits for a choice
    text = "You've hit your 5-hour limit · resets 12:50pm (Asia/Jerusalem)"
    assert mirror.limit_offer(cfg(), bot, "s", text, spawn_fn=lambda a: None)
    ring = [p["text"] for p in bot.methods("sendMessage") if p["chat_id"] == CHANNEL]
    assert ring == ["💬 Title\n⛽ Out of quota. The session is waiting for you to choose an account."]


# --- only the owner's own action registers a channel ----------------------------------------

def test_an_admin_update_made_by_someone_else_is_ignored_even_if_the_owner_administers_the_channel():
    bot = AdminBot([OWNER, 999])
    assert alerts.joined(promoted(by=999), cfg(), bot) == "ignored"
    no_sender = {k: v for k, v in promoted().items() if k != "from"}
    assert alerts.joined(no_sender, cfg(), bot) == "ignored"
    assert alerts.channel() is None and bot.methods("sendMessage") == []


def post(chat_id=CHANNEL):
    return {"chat": {"id": chat_id, "type": "channel", "title": "Alerts"}}


def test_a_channel_post_only_asks_the_owner_and_saves_nothing():
    bot = AdminBot([OWNER])
    assert alerts.propose(post(), cfg(), bot, now=1000.0) == "proposed"
    assert alerts.channel() is None
    ask = bot.methods("sendMessage")[0]
    assert ask["chat_id"] == OWNER and "Alerts" in ask["text"]
    assert ask["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"alerts:use:{CHANNEL}"
    assert alerts.propose(post(), cfg(), bot, now=1500.0) == "ignored"      # asked once a day
    assert len(bot.methods("sendMessage")) == 1


def test_no_question_when_the_owner_does_not_administer_the_channel_or_one_is_registered():
    assert alerts.propose(post(), cfg(), AdminBot([999]), now=1.0) == "ignored"
    assert alerts.propose({"chat": {"id": 5, "type": "private"}}, cfg(), AdminBot([OWNER])) == "ignored"
    alerts.joined(promoted(), cfg(), AdminBot([OWNER]))
    assert alerts.propose(post(-77), cfg(), AdminBot([OWNER]), now=9e9) == "ignored"


def press(by=OWNER, chat_type="private", chat_id=OWNER, data=f"alerts:use:{CHANNEL}"):
    return {"id": "q", "from": {"id": by}, "data": data,
            "message": {"message_id": 4, "chat": {"id": chat_id, "type": chat_type}}}


class ChatBot(AdminBot):
    def call(self, method, params=None, timeout=15):
        out = super().call(method, params, timeout)
        if method == "getChat":
            return {"id": params["chat_id"], "type": "channel", "title": "Alerts"}
        return out


def test_the_button_saves_the_channel_for_the_owner_only():
    for bad in (press(by=999), press(chat_type="supergroup", chat_id=-5), press(data="alerts:use:abc")):
        assert alerts.confirm(bad, cfg(), ChatBot([OWNER])) == "ignored"
    assert alerts.confirm(press(), cfg(), ChatBot([999])) == "ignored"       # the owner must administer it
    assert alerts.channel() is None
    bot = ChatBot([OWNER])
    assert alerts.confirm(press(), cfg(), bot) == "registered"
    assert alerts.channel()["id"] == CHANNEL
    assert bot.methods("editMessageReplyMarkup")


def test_the_listener_routes_the_post_and_the_button(tmp_path):
    bot = ChatBot([OWNER])
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    box = I.Inbox(tmp_path / "inbox")
    r = I.handle({"update_id": 1, "channel_post": post()}, cfg(), bot, topics, box, None, 0)
    assert r == "alerts-proposed" and alerts.channel() is None
    r = I.handle({"update_id": 2, "callback_query": press()}, cfg(), bot, topics, box, None, 0)
    assert r == "alerts-registered" and alerts.channel()["id"] == CHANNEL
