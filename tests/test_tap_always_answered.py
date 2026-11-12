"""Every button tap is answered: a crash says so, a silent handler never leaves the spinner."""
import time

import pytest

from ctb import inbox as I


class Bot:
    def __init__(self):
        self.calls = []

    def call(self, method, params=None, **_):
        self.calls.append((method, params or {}))
        return {}


def answers(bot):
    return [p for m, p in bot.calls if m == "answerCallbackQuery"]


def test_a_crashing_tap_is_answered_with_a_failure_toast(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("redraw failed")
    monkeypatch.setattr(I, "handle_button", boom)
    bot = Bot()
    with pytest.raises(RuntimeError):
        I._guarded_button({"id": "q1"}, object(), bot, None, None)
    assert [a["callback_query_id"] for a in answers(bot)] == ["q1"]
    assert answers(bot)[0]["text"]


def test_a_tap_answered_before_the_crash_is_not_answered_twice(monkeypatch):
    def half(q, cfg, bot, *_a):
        bot.call("answerCallbackQuery", {"callback_query_id": q["id"], "text": "ok"})
        raise RuntimeError("later step failed")
    monkeypatch.setattr(I, "handle_button", half)
    bot = Bot()
    with pytest.raises(RuntimeError):
        I._guarded_button({"id": "q2"}, object(), bot, None, None)
    assert [a["text"] for a in answers(bot)] == ["ok"]


def test_a_silent_handler_gets_its_spinner_closed(monkeypatch):
    monkeypatch.setattr(I, "handle_button", lambda *_a: "ignored")
    monkeypatch.setattr(I, "TAP_GRACE", 0.05)
    bot = Bot()
    assert I._guarded_button({"id": "q3"}, object(), bot, None, None) == "ignored"
    time.sleep(0.3)
    assert answers(bot) == [{"callback_query_id": "q3", "text": ""}]


def test_a_background_answer_inside_the_grace_keeps_its_own_toast(monkeypatch):
    def later(q, cfg, bot, *_a):
        import threading
        threading.Timer(0.02, lambda: bot.call("answerCallbackQuery", {"callback_query_id": q["id"], "text": "done"})).start()
        return "queued"
    monkeypatch.setattr(I, "handle_button", later)
    monkeypatch.setattr(I, "TAP_GRACE", 0.2)
    bot = Bot()
    I._guarded_button({"id": "q4"}, object(), bot, None, None)
    time.sleep(0.5)
    assert [a["text"] for a in answers(bot)] == ["done"]


class Cfg:
    ui_language = "en"
    owner_chat_id = 1
    target_language = "he"


class NoInbox:
    def set_hold(self, *_a):
        return False


def markup_cleared(bot):
    return [p for m, p in bot.calls if m == "editMessageReplyMarkup" and p["reply_markup"] == {"inline_keyboard": []}]


def test_a_late_delivery_tap_takes_its_buttons_away(monkeypatch):
    monkeypatch.setattr(I, "_reply_chat", lambda cfg, m, chat=None: -100)
    bot = Bot()
    q = {"id": "q5", "data": "dl:hold:77"}
    assert I.delivery_button(q, Cfg(), bot, {"message_id": 9}, "s", NoInbox()) == "ignored"
    assert answers(bot)[0]["text"]
    assert markup_cleared(bot) == [{"chat_id": -100, "message_id": 9, "reply_markup": {"inline_keyboard": []}}]


def test_a_late_voice_draft_tap_takes_its_buttons_away(monkeypatch):
    monkeypatch.setattr(I, "_reply_chat", lambda cfg, m, chat=None: -100)
    monkeypatch.setattr(I, "_chat_arg", lambda cfg, m: None)
    monkeypatch.setattr(I.voice, "pop", lambda key: None)
    bot = Bot()
    q = {"id": "q6", "data": "vd:send:55"}
    assert I.voice_button(q, Cfg(), bot, {"message_id": 12}, None) == "ignored"
    assert answers(bot)[0]["text"]
    assert len(markup_cleared(bot)) == 1
