from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ctb.home_context import context

NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
CFG = SimpleNamespace(day_assistant={"briefing_time": "07:30"}, home_context={"at_mac_minutes": 15})


def inputs(**more):
    return {"shabbat_times": False, **more}


@pytest.mark.parametrize(("now", "extra", "mode"), [
    (NOW, {"shabbat_times": True}, "shabbat"),
    (NOW.replace(hour=23), {}, "night"),
    (NOW.replace(hour=18), {"family_routine_now": True}, "family"),
    (NOW.replace(hour=8), {}, "morning"),
    (NOW, {"at_mac": True, "active_work": 1}, "work"),
    (NOW, {"at_mac": False}, "away"),
    (NOW, {}, "default"),
])
def test_every_mode(now, extra, mode):
    assert context(CFG, now, inputs=inputs(**extra))["mode"] == mode


def test_priority_is_fixed():
    result = context(CFG, NOW.replace(hour=23), inputs=inputs(
        shabbat_times=True, family_routine_now=True, at_mac=True, active_work=2))
    assert result["mode"] == "shabbat"


def test_unknown_shabbat_times_fail_quiet():
    result = context(CFG, NOW, inputs={})
    assert result["mode"] == "shabbat"
    assert result["signals"]["shabbat_or_holiday"] is True


def test_signals_are_derived_from_injected_snapshots():
    result = context(CFG, NOW, inputs=inputs(
        last_terminal_activity=NOW.timestamp() - 60,
        decisions=[{"updated": NOW.timestamp() - 300}],
        sessions=[{"state": "running"}], progress_cards=[{"kind": "progress"}]))
    assert result["signals"]["at_mac"] is True
    assert result["signals"]["decisions_waiting"] == {"count": 1, "oldest_age": 300}
    assert result["signals"]["active_work"] == 2
