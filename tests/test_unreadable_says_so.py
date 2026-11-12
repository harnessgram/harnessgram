"""A message the bridge cannot read gets words, not only a reaction; service notices get none."""
from ctb import inbox as I


class Bot:
    def __init__(self):
        self.calls = []

    def call(self, method, params=None, **_):
        self.calls.append((method, params or {}))
        return {}


class Cfg:
    ui_language = "en"


def said(bot):
    return [p for m, p in bot.calls if m == "sendMessage"]


def test_an_unreadable_recording_is_answered_in_words(monkeypatch):
    monkeypatch.setattr(I, "_reply_chat", lambda cfg, m, chat=None: -100)
    monkeypatch.setattr(I, "react", lambda *a, **k: False)  # reactions are off in this chat
    bot = Bot()
    I._unreadable(bot, Cfg(), {"message_id": 5, "message_thread_id": 7, "voice": {"file_id": "v"}})
    (msg,) = said(bot)
    assert msg["chat_id"] == -100 and msg["message_thread_id"] == 7 and msg["reply_to_message_id"] == 5
    assert msg["text"] == I.t("media_unreadable", lang="en")


def test_a_service_notice_gets_no_words(monkeypatch):
    monkeypatch.setattr(I, "_reply_chat", lambda cfg, m, chat=None: -100)
    monkeypatch.setattr(I, "react", lambda *a, **k: False)
    bot = Bot()
    I._unreadable(bot, Cfg(), {"message_id": 6, "forum_topic_edited": {"name": "x"}})
    assert said(bot) == []
