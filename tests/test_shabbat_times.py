"""The small solar calculation behind the family board's Shabbat window."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import GOOD_TOKEN
from ctb import shabbat_times as st
from ctb.config import Config, ConfigError, from_dict
from test_panel import OWNER

JER = ZoneInfo("Asia/Jerusalem")


def cfg(**kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, **kw)


def local_sunset(day):
    return st.sunset(day, st.DEFAULTS["latitude"], st.DEFAULTS["longitude"]).astimezone(JER)


@pytest.mark.parametrize("day,hour,minute", [
    (date(2026, 12, 21), 16, 36),    # the shortest day
    (date(2027, 6, 21), 19, 48)])    # the longest day, daylight saving time
def test_jerusalem_sunset_is_within_a_few_minutes_of_the_published_time(day, hour, minute):
    got = local_sunset(day)
    assert abs((got.hour * 60 + got.minute) - (hour * 60 + minute)) <= 5, got


def test_sunset_moves_later_into_summer_and_earlier_into_winter():
    winter, spring, summer = (local_sunset(date(2026, 12, 21)), local_sunset(date(2027, 3, 21)), local_sunset(date(2027, 6, 21)))
    assert winter.hour * 60 + winter.minute < spring.hour * 60 + spring.minute < summer.hour * 60 + summer.minute


def test_no_sunset_where_the_sun_never_sets():
    assert st.sunset(date(2027, 6, 21), 80.0, 20.0) is None


def test_the_window_is_candle_lighting_before_friday_sunset_to_nightfall_after_saturday_sunset():
    start, end = st.window(date(2026, 12, 18), cfg())
    fri, sat = st.sunset(date(2026, 12, 18), 31.778, 35.235), st.sunset(date(2026, 12, 19), 31.778, 35.235)
    assert fri - start == timedelta(minutes=40) and end - sat == timedelta(minutes=42)
    assert 24 * 60 < (end - start).total_seconds() / 60 < 26 * 60           # a bit over 25 hours


def test_the_boundaries_are_exact_in_winter_and_summer():
    for friday in (date(2026, 12, 18), date(2027, 6, 18)):
        start, end = st.window(friday, cfg())
        sat = friday + timedelta(days=1)
        eps = timedelta(seconds=1)
        assert st.current(cfg(), friday, start - eps) is None and st.current(cfg(), friday, start) is not None
        assert st.current(cfg(), sat, end - eps) is not None and st.current(cfg(), sat, end) is None
        assert st.current(cfg(), friday, start - timedelta(hours=3)) is None
        assert st.current(cfg(), sat + timedelta(days=1), end + timedelta(hours=1)) is None
        # the next window, asked on Sunday, is the following Friday's
        nxt = st.upcoming(cfg(), sat + timedelta(days=1), end + timedelta(hours=1))
        assert nxt and nxt[0] > end + timedelta(days=5)


@pytest.mark.parametrize("value,ok", [
    ({}, True), ({"candle_minutes": 18}, True), ({"candle_minutes": 40, "nightfall_minutes": 90}, True),
    ({"latitude": 32.08, "longitude": 34.78}, True),
    ({"candle_minutes": 17}, False), ({"candle_minutes": 41}, False), ({"nightfall_minutes": 19}, False),
    ({"nightfall_minutes": 91}, False), ({"candle_minutes": 20.5}, False), ({"latitude": 90}, False), ({"longitude": 181}, False),
    ({"latitude": "31"}, False), ({"latitude": True}, False), ({"latitude": float("nan")}, False), ({"zip": 1}, False), ([], False), ("x", False)])
def test_settings_are_validated(value, ok):
    assert st.valid_settings(value) is ok
    if ok:
        assert from_dict({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "family_shabbat": value}).family_shabbat == value
    else:
        with pytest.raises(ConfigError):
            from_dict({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "family_shabbat": value})


def test_bad_settings_fall_back_to_the_defaults_instead_of_breaking_the_board():
    assert st.settings(cfg(family_shabbat={"candle_minutes": 5})) == st.DEFAULTS
    assert st.settings(None) == st.DEFAULTS


def test_the_day_of_the_week_helper():
    assert [st.friday_on_or_before(date(2026, 12, d)) for d in (17, 18, 19, 20)] == [date(2026, 12, 11), date(2026, 12, 18), date(2026, 12, 18), date(2026, 12, 18)]
