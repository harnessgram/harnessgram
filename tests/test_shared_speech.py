"""Spoken features share the radio voice settings and paid-speech ledger."""

import calendar
import json
import time

import pytest

from conftest import GOOD_TOKEN
from ctb import radio, radio_voice, translate
from ctb.config import Config


def _cfg(**radio_settings):
    # Construct the complete test config: these tests must never read config.json.
    radio_settings.setdefault("voice_route", "cloud")
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, radio=radio_settings)


@pytest.fixture
def speech_dir(tmp_path, monkeypatch):
    directory = tmp_path / "radio"
    monkeypatch.setattr(radio, "folder", lambda: directory)
    return directory


def test_shared_speech_uses_radio_model_and_voice(speech_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(
        radio_voice, "vertex",
        lambda text, cfg, model, voice, *args, **kwargs:
            calls.append((model, voice)) or (b"\x01\x00" * radio_voice.RATE, 1.0),
    )

    pcm = translate.speak(
        "שלום", _cfg(tts_model="gemini-new-flash-tts", voice="Sadaltager", tts_daily_minutes=5)
    )

    assert pcm
    assert calls == [("gemini-new-flash-tts", "Sadaltager")]


def test_shared_speech_is_counted_in_radio_budget(speech_dir, monkeypatch):
    monkeypatch.setattr(
        radio_voice, "vertex", lambda *args, **kwargs: (b"\x01\x00" * radio_voice.RATE, 2.0)
    )

    translate.speak("עדכון חדש", _cfg(tts_daily_minutes=1))

    assert radio_voice.paid_usage(speech_dir).today() == pytest.approx(2.0)


def test_shared_speech_uses_system_voice_when_budget_exhausted(speech_dir, monkeypatch):
    cfg = _cfg(tts_daily_minutes=0)
    monkeypatch.setattr(radio_voice, "vertex", lambda *args, **kwargs: pytest.fail("paid voice called"))
    monkeypatch.setattr(radio_voice, "system_voice", lambda text, voice: b"system pcm")

    assert translate.speak("שלום", cfg) == b"system pcm"


def _cloud_cfg(**radio_settings):
    radio_settings.setdefault("voice_route", "cloud")
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, radio=radio_settings,
                  gemini_vertex_project="example-project", gemini_token_command=["fake-token"])


def test_free_voice_order_and_radio_voice_name(speech_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(radio_voice, "vertex", lambda *a, **k: pytest.fail("paid voice called"))
    monkeypatch.setattr(radio_voice, "cloud_tts",
                        lambda text, cfg, voice, **k: calls.append(voice) or
                        (b"wave" if voice.endswith("Wavenet-B") else b""))
    monkeypatch.setattr(radio_voice, "system_voice", lambda *a: pytest.fail("system voice called"))

    pcm = translate.speak("שלום", _cloud_cfg(tts_daily_minutes=0, voice="Orus"))

    assert pcm == b"wave"
    assert calls == ["he-IL-Chirp3-HD-Orus", "he-IL-Wavenet-B"]


def test_free_voice_falls_back_to_system(speech_dir, monkeypatch):
    monkeypatch.setattr(radio_voice, "cloud_tts", lambda *a, **k: b"")
    monkeypatch.setattr(radio_voice, "system_voice", lambda *a: b"system")
    assert translate.speak("שלום", _cloud_cfg(tts_daily_minutes=0)) == b"system"


def test_cloud_tts_uses_vertex_token_project_and_v1_endpoint(monkeypatch):
    sent = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, *args):
            return json.dumps({"audioContent": "AAE="}).encode()

    monkeypatch.setattr(radio_voice, "access_token", lambda cfg, why=None: "access-token")
    monkeypatch.setattr(radio_voice.urllib.request, "urlopen",
                        lambda req, timeout=0: sent.append(req) or Response())
    monkeypatch.setattr(radio_voice, "to_pcm", lambda raw, args: b"pcm")

    assert radio_voice.cloud_tts("שלום", _cloud_cfg(), "he-IL-Wavenet-B") == b"pcm"
    req = sent[0]
    assert req.full_url == "https://texttospeech.googleapis.com/v1/text:synthesize"
    assert req.headers["Authorization"] == "Bearer access-token"
    assert req.headers["X-goog-user-project"] == "example-project"
    body = json.loads(req.data)
    assert body["voice"] == {"languageCode": "he-IL", "name": "he-IL-Wavenet-B"}


def test_free_character_caps_are_hard(tmp_path, monkeypatch):
    monkeypatch.setattr(radio_voice, "FREE_TTS_CAPS", {"chirp": 5, "wavenet": 8})
    usage = radio_voice.FreeUsage(tmp_path / "free.json")
    assert usage.reserve("chirp", 5)
    assert not usage.reserve("chirp", 1)
    assert usage.reserve("wavenet", 8)
    assert not usage.reserve("wavenet", 1)
    assert json.loads((tmp_path / "free.json").read_text())["months"][time.strftime("%Y-%m", time.gmtime())] == {
        "chirp": 5, "wavenet": 8}


def test_free_character_ledger_rolls_over_at_utc_month(tmp_path, monkeypatch):
    monkeypatch.setattr(radio_voice, "FREE_TTS_CAPS", {"chirp": 5, "wavenet": 8})
    usage = radio_voice.FreeUsage(tmp_path / "free.json")
    jan = calendar.timegm((2027, 1, 31, 23, 59, 59))
    feb = calendar.timegm((2027, 2, 1, 0, 0, 0))
    assert usage.reserve("chirp", 5, jan)
    assert not usage.reserve("chirp", 1, jan)
    assert usage.reserve("chirp", 5, feb)


@pytest.mark.parametrize("counter", ["many", -1, 6])
def test_malformed_free_ledger_fails_closed_without_overwrite(tmp_path, monkeypatch, counter):
    monkeypatch.setattr(radio_voice, "FREE_TTS_CAPS", {"chirp": 5, "wavenet": 8})
    path = tmp_path / "free.json"
    original = json.dumps({"months": {"2027-01": {"chirp": counter}}})
    path.write_text(original)
    usage = radio_voice.FreeUsage(path)
    assert not usage.reserve("chirp", 1)
    assert not usage.reserve("wavenet", 1)
    assert path.read_text() == original
