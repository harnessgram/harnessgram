from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ctb import day_assistant as D


def _round(monkeypatch, now):
    monkeypatch.setattr(D, "briefing_enabled", lambda: True)
    called = []
    result = D.tick(SimpleNamespace(day_assistant={}), clock=lambda: now,
                    snapshot=lambda: called.append(True) or {}, state={},
                    private_radio=lambda _text: True, cockpit_card=lambda _card: True,
                    push=lambda _text: None, can_speak=lambda: True,
                    reserve_speech=lambda _text: True)
    assert result == {"briefing": False, "nudges": 0}
    assert called == []


@pytest.mark.parametrize("now", [
    datetime(2026, 11, 20, 17, tzinfo=timezone.utc),
    datetime(2026, 11, 21, 10, tzinfo=timezone.utc),
])
def test_shabbat_silences_tick(monkeypatch, now):
    monkeypatch.setattr(D.shabbat_times, "current", lambda *_args: object())
    _round(monkeypatch, now)


def test_holiday_and_calendar_errors_fail_closed(monkeypatch):
    now = datetime(2026, 11, 18, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(D.shabbat_times, "current", lambda *_args: None)
    from ctb import briefing
    monkeypatch.setattr(briefing, "rest_day", lambda *_args: True)
    _round(monkeypatch, now)
    monkeypatch.setattr(briefing, "rest_day", lambda *_args: (_ for _ in ()).throw(ValueError()))
    _round(monkeypatch, now)
    monkeypatch.setattr(D.shabbat_times, "current", lambda *_args: (_ for _ in ()).throw(OSError()))
    _round(monkeypatch, now)
