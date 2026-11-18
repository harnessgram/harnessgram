from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ctb import briefing, day_assistant as D


def _round(monkeypatch, now, *, current=None, holiday=False):
    called = []
    monkeypatch.setattr(D.shabbat_times, "current", lambda *_: current)
    monkeypatch.setattr(briefing, "rest_day", holiday if callable(holiday) else lambda *_: holiday)
    monkeypatch.setattr(D, "briefing_enabled", lambda: True)
    result = D.tick(SimpleNamespace(day_assistant={}), clock=lambda: now,
                    snapshot=lambda: called.append(True) or {}, state={},
                    private_radio=lambda _: True, cockpit_card=lambda _: True,
                    push=lambda _: None, can_speak=lambda: True,
                    reserve_speech=lambda _: True)
    assert result == {"briefing": False, "nudges": 0}
    assert called == []


def test_friday_after_candle_lighting_is_silent(monkeypatch):
    _round(monkeypatch, datetime(2026, 11, 20, 17, tzinfo=timezone.utc), current={"rest": True})


def test_holiday_window_is_silent(monkeypatch):
    _round(monkeypatch, datetime(2026, 11, 18, 9, tzinfo=timezone.utc), holiday=True)


def test_saturday_morning_is_silent(monkeypatch):
    _round(monkeypatch, datetime(2026, 11, 21, 10, tzinfo=timezone.utc), current={"rest": True})


def test_bad_holiday_data_fails_closed(monkeypatch):
    broken = lambda *_: (_ for _ in ()).throw(ValueError("bad calendar"))
    _round(monkeypatch, datetime(2026, 11, 18, 9, tzinfo=timezone.utc), holiday=broken)


def test_zmanim_error_fails_closed_before_snapshot(monkeypatch):
    monkeypatch.setattr(D.shabbat_times, "current", lambda *_: (_ for _ in ()).throw(RuntimeError("offline")))
    called = []
    result = D.tick(SimpleNamespace(day_assistant={}), clock=lambda: datetime(2026, 11, 18, 9, tzinfo=timezone.utc),
                    snapshot=lambda: called.append(True), state={}, private_radio=lambda _: True,
                    cockpit_card=lambda _: True, push=lambda _: None, can_speak=lambda: True,
                    reserve_speech=lambda _: True)
    assert result == {"briefing": False, "nudges": 0} and called == []
