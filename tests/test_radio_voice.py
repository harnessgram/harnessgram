"""Offline-first radio voice routing. No real engine, configuration, or network is used."""
import logging
import sys

import pytest

from ctb import radio, radio_voice as V
from ctb.config import Config


def _engine(tmp_path, body):
    script = tmp_path / "fake_voice.py"
    script.write_text(
        "import sys\n"
        "args=sys.argv[1:]\n"
        "out=args[args.index('-o')+1]\n"
        "text=sys.stdin.read()\n" + body,
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def _settings(**values):
    return radio.settings(Config(radio=values))


def _usage(tmp_path):
    return V.Usage(tmp_path / "usage.json")


def test_default_route_is_local_and_cache_skips_engine(tmp_path, monkeypatch):
    seen = tmp_path / "seen"
    command = _engine(tmp_path, f"open({str(seen)!r},'a').write(text+'\\n')\nopen(out,'wb').write(b'OggS-fake')\n")
    cfg = Config(tts_command=command, tts_voice="small")
    monkeypatch.setattr(V, "to_pcm", lambda data, args: b"pcm" if data.startswith(b"OggS") else b"")
    monkeypatch.setattr(V, "vertex", lambda *a, **k: pytest.fail("cloud voice called"))

    args = ("שלום.", cfg, _settings(), tmp_path / "cache", _usage(tmp_path))
    assert V.speak(*args) == (b"pcm", "local")
    assert V.speak(*args) == (b"pcm", "local")
    assert seen.read_text(encoding="utf-8").splitlines() == ["שלום."]


def test_cloud_requires_opt_in_and_failure_or_cap_falls_back_local(tmp_path, monkeypatch):
    cfg = Config(tts_command=[sys.executable], gemini_vertex_project="example", gemini_token_command=["token"])
    monkeypatch.setattr("ctb.listen.run_engine", lambda cfg, text: b"OggS-local")
    monkeypatch.setattr(V, "to_pcm", lambda data, args: b"local")
    calls = []
    monkeypatch.setattr(V, "vertex", lambda *a, **k: calls.append("vertex") or (b"", 0.0))
    monkeypatch.setattr(V, "cloud_tts", lambda *a, **k: b"")

    assert V.speak("שלום", cfg, _settings(), tmp_path / "a", _usage(tmp_path)) == (b"local", "local")
    assert calls == []
    assert V.speak("שלום", cfg, _settings(voice_route="cloud", tts_daily_minutes=1),
                   tmp_path / "b", V.Usage(tmp_path / "cloud.json")) == (b"local", "local")
    assert calls == ["vertex"]
    calls.clear()
    assert V.speak("שלום", cfg, _settings(voice_route="cloud", tts_daily_minutes=0),
                   tmp_path / "c", V.Usage(tmp_path / "cap.json")) == (b"local", "local")
    assert calls == []


def test_local_failure_uses_system_and_never_logs_engine_output(tmp_path, monkeypatch, caplog):
    command = _engine(tmp_path, "print('private output')\nprint('private error',file=sys.stderr)\nsys.exit(2)\n")
    monkeypatch.setattr(V, "system_voice", lambda text, voice: b"system")
    caplog.set_level(logging.DEBUG)
    assert V.speak("שלום", Config(tts_command=command), _settings(), tmp_path / "cache", _usage(tmp_path)) \
           == (b"system", "say")
    assert "private output" not in caplog.text and "private error" not in caplog.text


def test_long_local_text_is_split_by_sentence(tmp_path, monkeypatch):
    cfg = Config(tts_command=[sys.executable])
    pieces = []
    monkeypatch.setattr("ctb.listen.run_engine", lambda cfg, text: pieces.append(text) or b"OggS-local")
    monkeypatch.setattr(V, "to_pcm", lambda data, args: b"p")
    expected = [letter * 240 + "." for letter in "אבג"]
    text = " ".join(expected)
    pcm, route = V.speak(text, cfg, _settings(), tmp_path / "cache", _usage(tmp_path))
    assert route == "local" and pcm == b"p" * 3
    assert pieces == expected


def test_radio_voice_route_is_validated_without_enabling_cloud():
    assert _settings()["voice_route"] == "local"
    assert _settings(tts_daily_minutes=99)["voice_route"] == "local"
    assert _settings(voice_route="cloud")["voice_route"] == "cloud"
    assert _settings(voice_route="other")["voice_route"] == "local"
