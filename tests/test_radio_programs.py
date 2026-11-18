import json
from types import SimpleNamespace

import pytest

from ctb import panel, panel_page, radio_programs
from ctb.config import from_dict
from ctb.radio_filler import Filler
from ctb.radio import Station


def test_program_model_is_bounded_and_tracks_current_block(tmp_path, monkeypatch):
    monkeypatch.setattr(radio_programs, "state_dir", lambda: tmp_path)
    state = radio_programs.start([
        {"kind": "music", "minutes": 10, "sources": []},
        {"kind": "news", "minutes": 5, "sources": ["technology"]},
    ], now=100)
    assert radio_programs.current_block(701, state)["kind"] == "news"
    assert radio_programs.current_block(1001, state)["kind"] == "music"
    stored = json.loads(radio_programs.path().read_text())
    assert stored["active"] is True and stored["started_at"] == 100
    with pytest.raises(ValueError):
        radio_programs.save([{"kind": "music", "minutes": 0, "sources": []}])
    with pytest.raises(ValueError):
        radio_programs.save([{"kind": "music", "minutes": 1, "sources": []}] * 25)


def test_program_scheduler_uses_selected_news_channels(monkeypatch):
    block = {"kind": "news", "minutes": 20, "sources": ["one"]}
    monkeypatch.setattr(radio_programs, "current_block", lambda now=None: block)
    station = object.__new__(Station)
    station.cfg = from_dict({"news_edition_channels": ["one", "two"]})
    calls = []
    station.news_edition_step = lambda now=None, channels=None: calls.append((now, channels)) or True
    assert station.program_step(55) is True
    assert calls == [(55, ["one"])]


def test_unimplemented_program_kinds_are_rejected_by_real_validation():
    for kind in ("talk", "genre_news"):
        with pytest.raises(ValueError):
            radio_programs.validate_blocks([{"kind": kind, "minutes": 5, "sources": []}])
    assert radio_programs.KINDS == ("music", "news")


def test_news_configuration_uses_fallback_music_when_it_cannot_produce_content(monkeypatch):
    monkeypatch.setattr(radio_programs, "current_block",
                        lambda now=None: {"kind": "news", "minutes": 5, "sources": []})
    monkeypatch.setattr("ctb.radio_music.enabled", lambda: True)
    cfg = from_dict({"news_edition_enabled": False, "news_edition_channels": []})
    filler = object.__new__(Filler)
    filler.cfg, filler.clock, filler.quiet = cfg, lambda: 10, lambda cfg, now: False
    filler.closed = SimpleNamespace(is_set=lambda: False)
    assert filler.allowed() is True


def test_deferred_put_on_air_is_not_exposed_or_acknowledged():
    assert "/api/put-on-air" not in panel.ROUTES
    assert "/api/put-on-air" not in panel.ACTIONS
    assert "/api/put-on-air" not in panel_page.PAGE


def test_program_presets_are_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(radio_programs, "state_dir", lambda: tmp_path)
    selected = radio_programs.select_preset("drive")
    selected["blocks"][0]["minutes"] = 1
    assert radio_programs.PRESETS["drive"][0]["minutes"] == 60


def test_panel_program_api_saves_and_starts(monkeypatch):
    replies = []
    handler = SimpleNamespace(_request=lambda: {"action": "start", "preset": "drive",
                                                "blocks": radio_programs.PRESETS["drive"]},
                              _json=lambda code, body: replies.append((code, body)))
    monkeypatch.setattr(radio_programs, "start", lambda blocks, preset: {"blocks": blocks, "preset": preset})
    monkeypatch.setattr(radio_programs, "view", lambda: {"active": True})
    panel.PanelHandler._radio_programs(handler)
    assert replies == [(200, {"ok": True, "program": {"active": True}})]
    assert panel.ACTIONS["/api/radio-programs"] == "_radio_programs"


def test_panel_program_api_replies_to_non_object_body():
    replies = []
    handler = SimpleNamespace(_request=lambda: None, _json=lambda code, body: replies.append((code, body)))
    panel.PanelHandler._radio_programs(handler)
    assert replies == [(400, {"error": "bad request"})]
