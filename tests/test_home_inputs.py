from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ctb import addons, home_inputs, panel, panel_settings, shabbat_times
from ctb.config import Config
from ctb.home_context import context

ZONE = timezone(timedelta(hours=2))
FRIDAY = datetime(2026, 11, 20, tzinfo=ZONE)
START, END = shabbat_times.window(FRIDAY.date())
WEEKLY = [{"start": "fri 12:00", "end": "sun 00:00"}]
HOLIDAY = {"start": "2026-11-25T10:00:00", "end": "2026-11-26T20:00:00"}


def cfg(windows=WEEKLY, **more):
    return SimpleNamespace(voice_digest_quiet_windows=windows, family_shabbat=more.get("family_shabbat"))


def at(value):
    return value.astimezone(ZONE)


@pytest.mark.parametrize(("moment", "expected"), [
    (at(START - timedelta(minutes=1)), False),   # Friday afternoon, still before candle-lighting
    (at(START), True),
    (at(END - timedelta(minutes=1)), True),
    (at(END), False),                            # Saturday night after nightfall is an ordinary moment
    (datetime(2026, 11, 21, 21, tzinfo=ZONE), False),
    (datetime(2026, 11, 18, 12, tzinfo=ZONE), False),
])
def test_shabbat_is_the_computed_candle_lighting_to_nightfall_window(moment, expected):
    assert home_inputs.rest_state(cfg(), moment) is expected


def test_the_coarse_weekly_window_does_not_widen_shabbat():
    friday_noon = datetime(2026, 11, 20, 12, tzinfo=ZONE)
    assert home_inputs.rest_state(cfg(), friday_noon) is False


def test_a_dated_holiday_window_counts_but_only_inside_it():
    both = cfg(WEEKLY + [HOLIDAY])
    assert home_inputs.rest_state(both, datetime(2026, 11, 25, 11, tzinfo=ZONE)) is True
    assert home_inputs.rest_state(both, datetime(2026, 11, 26, 21, tzinfo=ZONE)) is False


@pytest.mark.parametrize("windows", [None, "fri", [{"start": "bad", "end": "sun 00:00"}]])
def test_unreadable_holiday_windows_fail_quiet(windows):
    assert home_inputs.rest_state(cfg(windows), datetime(2026, 11, 18, 12, tzinfo=ZONE)) is None


def test_no_computable_times_fails_quiet(monkeypatch):
    monkeypatch.setattr(shabbat_times, "window", lambda friday, c=None: None)
    assert home_inputs.rest_state(cfg(), datetime(2026, 11, 18, 12, tzinfo=ZONE)) is None
    monkeypatch.setattr(shabbat_times, "window", lambda *a, **k: 1 / 0)
    assert home_inputs.rest_state(cfg(), datetime(2026, 11, 18, 12, tzinfo=ZONE)) is None


def test_away_mode_is_an_explicit_not_at_mac():
    assert home_inputs.at_mac_inputs([{"age": 5, "state": "working"}], 1000.0, away=True) == {"at_mac": False}


def test_presence_is_the_newest_live_session_activity():
    rows = [{"age": 600, "state": "idle"}, {"age": 30, "state": "idle"}, {"age": 1, "state": "ended"},
            {"age": None, "state": "idle"}, {"age": True, "state": "idle"}, "junk"]
    assert home_inputs.at_mac_inputs(rows, 1000.0) == {"last_terminal_activity": 970.0}
    assert home_inputs.at_mac_inputs([], 1000.0) == {}
    assert home_inputs.at_mac_inputs(None, 1000.0) == {}


@pytest.mark.parametrize(("age", "mode"), [(60, "work"), (3600, "away")])
def test_recent_activity_drives_the_engine_to_work_and_old_activity_to_away(age, mode):
    now = datetime(2026, 11, 18, 12, tzinfo=ZONE)
    sessions = [{"age": age, "state": "working" if age < 600 else "idle"}]
    got = context(SimpleNamespace(), now, inputs={
        "shabbat_times": False, "sessions": sessions, **home_inputs.at_mac_inputs(sessions, now.timestamp())})
    assert got["mode"] == mode


# --- build_state wiring ------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    import subprocess

    def refuse(*args, **kwargs):
        raise AssertionError("a state build must not start a process here")
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


def state_cfg():
    return Config(bot_token="1:x", owner_chat_id=1, timezone="Asia/Jerusalem", target_language="he")


def test_state_has_home_context_only_while_the_switch_is_on(monkeypatch):
    monkeypatch.setattr(panel_settings, "read", lambda: {"ui_v2": "on"})
    cfg_ = state_cfg()
    now = datetime(2026, 11, 18, 12, tzinfo=ZONE).timestamp()
    assert addons.enabled(cfg_, "home_context") is False
    off = panel.build_state(cfg_, light=True, now=now)
    assert "home_context" not in off
    addons.set_on(cfg_, "home_context", True)
    on = panel.build_state(cfg_, light=True, now=now)
    assert set(on["home_context"]) == {"mode", "signals", "why"}
    assert on["home_context"]["signals"]["shabbat_or_holiday"] is False
    addons.set_on(cfg_, "home_context", False)
    assert "home_context" not in panel.build_state(cfg_, light=True, now=now)


def test_state_marks_shabbat_from_the_computed_times_and_ends_it_at_nightfall(monkeypatch):
    monkeypatch.setattr(panel_settings, "read", lambda: {"ui_v2": "on"})
    cfg_ = state_cfg()
    addons.set_on(cfg_, "home_context", True)
    inside = panel.build_state(cfg_, light=True, now=(START + timedelta(minutes=5)).timestamp())
    after = panel.build_state(cfg_, light=True, now=(END + timedelta(minutes=5)).timestamp())
    assert inside["home_context"]["mode"] == "shabbat"
    assert after["home_context"]["signals"]["shabbat_or_holiday"] is False


def test_state_reads_the_away_flag_without_touching_the_real_one(monkeypatch):
    from ctb import autopilot
    monkeypatch.setattr(panel_settings, "read", lambda: {"ui_v2": "on"})
    monkeypatch.setattr(autopilot, "away_active", lambda: True)
    cfg_ = state_cfg()
    addons.set_on(cfg_, "home_context", True)
    got = panel.build_state(cfg_, light=True, now=datetime(2026, 11, 18, 12, tzinfo=ZONE).timestamp())
    assert got["home_context"]["mode"] == "away" and got["home_context"]["signals"]["at_mac"] is False


def test_state_stays_quiet_when_the_quiet_window_list_is_unreadable(monkeypatch):
    monkeypatch.setattr(panel_settings, "read", lambda: {"ui_v2": "on"})
    cfg_ = state_cfg()
    cfg_.voice_digest_quiet_windows = [{"start": "bad", "end": "sun 00:00"}]
    addons.set_on(cfg_, "home_context", True)
    got = panel.build_state(cfg_, light=True, now=datetime(2026, 11, 18, 12, tzinfo=ZONE).timestamp())
    assert got["home_context"]["mode"] == "shabbat" and got["home_context"]["signals"]["shabbat_or_holiday"] is True
