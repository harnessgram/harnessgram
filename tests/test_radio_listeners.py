"""Listener-gated paid speech: anonymous HLS evidence, cached calls and daily budgets."""
import json
import os
import threading
import time

import pytest

from ctb import panel, panel_page, radio, radio_listeners as L, radio_voice as V
from ctb.config import Config
from test_radio_player import call, server, hls  # reuse the networkless authenticated HTTP harness
from test_radio_room import _station


def settings(**overrides):
    overrides.setdefault("voice_route", "cloud")
    return radio.settings(Config(radio=overrides))


def call_settings(**overrides):
    return settings(telethon_python="python", account_api_command=["test-api"],
                    account_session_command=["test-session"], **overrides)


@pytest.mark.parametrize("age,expected", [(0, True), (89, True), (90, True), (91, False), (-1, False)])
def test_player_freshness(tmp_path, age, expected):
    L.player_fetch(tmp_path, now=1000 - age)
    observed = L.snapshot(tmp_path, settings(), now=1000)
    assert observed["listening"] is expected
    assert observed["listening_source"] == ("player" if expected else "none")


@pytest.mark.parametrize("name", ["live.m3u8", "segment-0.ts"])
def test_only_successful_signed_media_fetch_counts(server, hls, name):
    path = radio.folder() / "player-listener.json"
    route = "/radio/" + name
    assert call(server, "GET", route + "?token=forged")[0] == 403
    assert not path.exists()
    token = panel.radio_token(server.bot_token, server.owner_id)
    assert call(server, "GET", "/radio/segment-999.ts?token=" + token)[0] == 404
    assert not path.exists()
    assert call(server, "GET", route + "?token=" + token)[0] == 200
    data = json.loads(path.read_text())
    assert set(data) == {"at"} and abs(data["at"] - time.time()) < 2
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert token not in path.read_text() and server.bot_token not in path.read_text()
    assert L.snapshot(radio.folder(), settings())["listening_source"] == "player"


@pytest.mark.parametrize("response,expected,source", [
    ({"ok": True, "active": True, "call": {"participants": 0}}, False, "none"),
    ({"ok": True, "active": True, "call": {"participants": 1}}, True, "call"),
    ({"ok": True, "active": False}, False, "none"),
    ({"ok": False, "error": "unavailable"}, False, "unknown"),
    ({"ok": True, "active": True, "call": {"participants": True}}, False, "unknown"),
    ({"ok": True, "active": True}, False, "unknown"),
    (None, False, "unknown"),
])
def test_call_counts_and_failures(tmp_path, response, expected, source):
    L.poll_call(tmp_path, lambda: response, now=1000)
    observed = L.snapshot(tmp_path, call_settings(radio_player=False), now=1000)
    assert observed["listening"] is expected and observed["listening_source"] == source
    assert not L.snapshot(tmp_path, call_settings(radio_player=False), now=1091)["listening"]


def test_call_is_rate_limited_and_failure_clears_previous_listener(tmp_path):
    calls = []

    def fetch():
        calls.append(1)
        if len(calls) > 1:
            # Unknown is visible even while the slow call is in flight.
            assert not L.snapshot(tmp_path, call_settings(), now=1060)["listening"]
            raise RuntimeError("test-private-detail")
        return {"ok": True, "active": True, "call": {"participants": 1, "access_hash": "test-private-detail"}}

    for at in (1000, 1001, 1059, 1060):
        L.poll_call(tmp_path, fetch, now=at)
    assert len(calls) == 2
    data = json.loads((tmp_path / "call-listener.json").read_text())
    assert data == {"at": 1060, "participants": None}
    assert "test-private-detail" not in json.dumps(data)
    assert L.snapshot(tmp_path, call_settings(), now=1061)["listening_source"] == "unknown"


def test_both_sources_and_unknown_call_do_not_override_fresh_player(tmp_path):
    L.player_fetch(tmp_path, now=1000)
    L.poll_call(tmp_path, lambda: {"ok": True, "active": True, "call": {"participants": 1}}, now=1000)
    assert L.snapshot(tmp_path, call_settings(), now=1000)["listening_source"] == "player+call"
    L.poll_call(tmp_path, lambda: {"ok": False}, now=1060)
    assert L.snapshot(tmp_path, call_settings(), now=1060)["listening_source"] == "player"


@pytest.mark.parametrize("player,account,listening,cap,source", [
    (True, False, False, 0, "none"), (True, False, True, 60, "player"),
    (False, True, False, 0, "unknown"), (False, False, False, 25, "unavailable"),
    (False, False, True, 25, "unavailable"),
])
def test_cap_selection(tmp_path, player, account, listening, cap, source):
    s = call_settings(radio_player=player) if account else settings(radio_player=player)
    if listening:
        L.player_fetch(tmp_path, now=1000)
    observed = L.snapshot(tmp_path, s, now=1000)
    selected = L.voice_settings(s, observed)
    assert observed["listening_source"] == source
    assert selected["tts_daily_minutes"] == cap
    assert s["tts_daily_minutes"] == 25  # never mutate the loaded owner configuration


def test_unheard_bulletin_bypasses_paid_cache_and_does_not_reserve_usage(tmp_path, monkeypatch):
    s = settings()
    c = Config()
    usage = V.Usage(tmp_path / "usage.json")
    cache = tmp_path / "cache"
    monkeypatch.setattr(V, "vertex", lambda *a, **k: (b"premium", 1.0))
    monkeypatch.setattr(V, "system_voice", lambda *a: b"free")
    L.player_fetch(tmp_path)
    selected = L.voice_settings(s, L.snapshot(tmp_path, s))
    assert V.speak("שלום", c, selected, cache, usage) == (b"premium", "vertex")
    used = usage.today()
    selected = L.voice_settings(s, L.snapshot(tmp_path, s, now=time.time() + 91))
    monkeypatch.setattr(V, "vertex", lambda *a, **k: pytest.fail("unheard paid speech"))
    assert V.speak("שלום", c, selected, cache, usage) == (b"free", "say")
    assert not V.last_cap_out and usage.today() == used


def test_daily_cap_rollover_and_reconnecting_keeps_same_days_usage(tmp_path, monkeypatch):
    first = time.mktime((2026, 1, 5, 12, 0, 0, 0, 0, -1))
    next_day = first + 86400
    s = settings(tts_daily_minutes=0, tts_listening_minutes=1)
    usage = V.Usage(tmp_path / "usage.json")
    usage.add(60, now=first)
    monkeypatch.setattr(V, "vertex", lambda *a, **k: (b"premium", 1.0))
    monkeypatch.setattr(V, "system_voice", lambda *a: b"free")
    for at, route in ((first, "say"), (first + 100, "say"), (next_day, "vertex")):
        L.player_fetch(tmp_path, now=at)
        selected = L.voice_settings(s, L.snapshot(tmp_path, s, now=at))
        assert V.speak("שלום", Config(), selected, tmp_path / "cache", usage, now=at)[1] == route
    assert usage.today(now=first) == 60 and usage.today(now=next_day) == 1


def test_status_and_cockpit_info_read_cached_signals_without_calling_helper(monkeypatch, capsys):
    cfg = Config(radio={"every_minutes": 20})
    L.player_fetch(radio.folder())
    V.Usage(radio.folder() / "tts-usage.json").add(120)
    monkeypatch.setattr(radio, "job_pid", lambda: None)
    monkeypatch.setattr(radio, "call_helper", lambda *a, **k: pytest.fail("status must stay cache-only"))
    assert radio.cli(cfg, "status") == 0
    out = json.loads(capsys.readouterr().out)
    for observed in (out, radio.minutes(cfg), panel.build_state(cfg)["radio"]):
        assert observed["listening"] and observed["listening_source"] == "player"
        assert observed["tts_minutes_today"] == 2 and observed["tts_listening_minutes"] == 60
        assert observed["tts_effective_minutes"] == 60


def test_station_selects_per_bulletin_and_keeps_room_budget(monkeypatch):
    st = _station(Config(radio={"tts_daily_minutes": 0, "tts_listening_minutes": 60}))
    selected = []
    monkeypatch.setattr(V, "speak", lambda text, cfg, s, *a: selected.append(s) or (b"audio", "say"))
    st.speak("שלום")
    L.player_fetch(st.dir)
    st.speak("שלום שוב")
    st.speak_room("חדר", "moderator", lambda: True, False)
    assert [s["tts_daily_minutes"] for s in selected] == [0, 60, 60]
    assert selected[-1]["tts_room_daily_minutes"] == 15
    assert all(s["tts_hard_cap"] for s in selected)
    assert selected[0]["tts_force_system"] and not selected[1]["tts_force_system"]


def test_worker_checks_only_once_per_minute_and_stops_with_station(monkeypatch, tmp_path):
    st = object.__new__(radio.Station)
    st.s, st.dir, st.quit = call_settings(), tmp_path, threading.Event()
    calls, waits = [], []

    def wait(seconds):
        waits.append(seconds)
        st.quit.set()
        return True

    monkeypatch.setattr(st.quit, "wait", wait)
    monkeypatch.setattr(radio, "call_helper", lambda s, action: calls.append(action) or {"ok": False})
    st.listener_loop()
    assert calls == ["status"] and waits == [60]


def test_cockpit_has_localized_listening_and_premium_cap():
    for lang in ("he", "en"):
        strings = panel_page.STRINGS[lang]
        for key in ("radioListening", "radioNotListening", "radioListenerUnknown", "radioDetectionOff",
                    "radioListenerPlayer", "radioListenerCall"):
            assert strings[key]
    assert "r.tts_listening_minutes" in panel_page.PAGE


@pytest.mark.parametrize("lang", ["he", "en"])
def test_telegram_status_displays_listening_cap_and_source(monkeypatch, lang):
    from ctb.i18n import t
    cfg = Config(ui_language=lang, radio={"tts_daily_minutes": 0})
    monkeypatch.setattr(radio, "job_pid", lambda: 123)
    replies = []
    radio.telegram_command(cfg, "status", replies.append)
    assert "60" in replies[-1] and t("radio_listening_none", lang=lang) in replies[-1]
    L.player_fetch(radio.folder())
    radio.telegram_command(cfg, "status", replies.append)
    assert "60" in replies[-1] and t("radio_listening_player", lang=lang) in replies[-1]


def test_cockpit_budget_unavailable_and_active_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(radio, 'folder', lambda: tmp_path)
    (tmp_path / 'tts-usage.json').write_text('{"days": null}')
    cfg = Config(radio={'tts_daily_minutes': 0, 'tts_listening_minutes': 60})
    result = radio.minutes(cfg)
    assert result['tts_minutes_today'] is None
    assert result['tts_listening_minutes'] == 60
    assert result['tts_effective_minutes'] == 0
    for lang in ('he', 'en'):
        for key in ('radioBudgetUnavailable', 'radioConfiguredCap', 'radioActiveCap'):
            assert panel_page.STRINGS[lang][key]
    assert 'r.tts_minutes_today===null' in panel_page.PAGE
    assert 'nf.format(r.tts_effective_minutes)' in panel_page.PAGE
