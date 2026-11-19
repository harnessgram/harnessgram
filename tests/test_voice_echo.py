"""A recording that goes to the agent without a transcript preview gets one quiet echo of what was heard."""
from conftest import FakeBot
from ctb import addons, inbox as I, voice
from ctb.config import Config

from test_oct30_accounts_delivery import SID, VoiceMedia, cfg, rig, voice_update


def echoes(bot):
    return [c for c in bot.methods("sendMessage") if "fix the bug" in c["text"]]


def test_car_mode_sends_one_quiet_echo_without_buttons(isolated, bot):
    topics, box, thread = rig(bot)
    I.set_car_mode(True)
    voice.set_car_choice("send")  # car mode skips the preview only when set to send directly
    assert I.handle(voice_update(thread), cfg(target_language="he"), bot, topics, box, VoiceMedia(), 0) == "queued"
    assert [i["id"] for i in box.take(SID)] == [8]
    [echo] = echoes(bot)
    assert echo["disable_notification"] is True and "reply_markup" not in echo
    assert echo["reply_parameters"]["message_id"] == 8 and "(הודעה קולית" not in echo["text"]


def test_failed_preview_falls_back_with_an_echo(isolated, bot, monkeypatch):
    topics, box, thread = rig(bot)
    real = bot.call

    def call(method, params=None, *a, **k):
        if method == "sendMessage" and params and "reply_markup" in params:
            raise I.TelegramError("boom")
        return real(method, params, *a, **k)

    monkeypatch.setattr(bot, "call", call)
    assert I.handle(voice_update(thread, 13), cfg(target_language="he"), bot, topics, box, VoiceMedia(), 0) == "voice"
    assert [i["id"] for i in box.take(SID)] == [13]
    [echo] = echoes(bot)
    assert echo["disable_notification"] is True and "reply_markup" not in echo


def test_a_forward_is_never_echoed(isolated, bot):
    topics, box, thread = rig(bot)
    I.set_car_mode(True)
    update = voice_update(thread)
    update["message"]["forward_origin"] = {"type": "user", "sender_user": {"id": 99, "first_name": "X"}}
    I.handle(update, cfg(target_language="he"), bot, topics, box, VoiceMedia(), 0)
    assert echoes(bot) == []


def test_the_switch_turns_the_echo_off(isolated, bot):
    topics, box, thread = rig(bot)
    I.set_car_mode(True)
    voice.set_car_choice("send")
    addons.set_on(Config(), "voice_echo", False)
    assert I.handle(voice_update(thread), cfg(target_language="he"), bot, topics, box, VoiceMedia(), 0) == "queued"
    assert echoes(bot) == []
