"""When Shabbat begins and ends, from a small stdlib solar calculation (the NOAA sunrise/sunset equations).

Shabbat starts at candle-lighting (sunset minus `candle_minutes`) on Friday and ends at nightfall (sunset plus
`nightfall_minutes`) on Saturday, for a configured place (default: the Jerusalem area). The numbers are an approximation
that is good to a minute or two; they are meant for the home board, not as a ruling. Settings live in the config key
`family_shabbat`: {"latitude", "longitude", "candle_minutes" (18 to 40, default 40), "nightfall_minutes" (20 to 90,
default 42)}; every key is optional. Nothing here uses the network.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple

DEFAULTS = {"latitude": 31.778, "longitude": 35.235, "candle_minutes": 40, "nightfall_minutes": 42}
RANGES = {"latitude": (-65.0, 65.0), "longitude": (-180.0, 180.0), "candle_minutes": (18, 40), "nightfall_minutes": (20, 90)}
ZENITH = 90.833  # the sun's upper edge on the horizon, with refraction


def valid_settings(value) -> bool:
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        return False
    for key, got in value.items():
        if isinstance(got, bool) or not isinstance(got, (int, float)) or not math.isfinite(got):
            return False
        if key.endswith("_minutes") and int(got) != got:
            return False
        low, high = RANGES[key]
        if not low <= got <= high:
            return False
    return True


def settings(cfg=None) -> dict:
    raw = getattr(cfg, "family_shabbat", None)
    out = dict(DEFAULTS)
    if valid_settings(raw):
        out.update(raw)
    return out


def _sun(julian_century: float) -> Tuple[float, float]:
    """(declination in degrees, equation of time in minutes) at a moment given in Julian centuries since 2000."""
    rad, deg = math.radians, math.degrees
    jc = julian_century
    l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    c = (math.sin(rad(m)) * (1.914602 - jc * (0.004817 + 0.000014 * jc)) + math.sin(rad(2 * m)) * (0.019993 - 0.000101 * jc)
         + math.sin(rad(3 * m)) * 0.000289)
    omega = 125.04 - 1934.136 * jc
    apparent = l0 + c - 0.00569 - 0.00478 * math.sin(rad(omega))
    mean_obliq = 23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
    obliq = mean_obliq + 0.00256 * math.cos(rad(omega))
    decl = deg(math.asin(math.sin(rad(obliq)) * math.sin(rad(apparent))))
    y = math.tan(rad(obliq / 2)) ** 2
    eq = 4 * deg(y * math.sin(2 * rad(l0)) - 2 * e * math.sin(rad(m)) + 4 * e * y * math.sin(rad(m)) * math.cos(2 * rad(l0))
                 - 0.5 * y * y * math.sin(4 * rad(l0)) - 1.25 * e * e * math.sin(2 * rad(m)))
    return decl, eq


def sunset(day: date, latitude: float, longitude: float) -> Optional[datetime]:
    """Sunset on the local calendar `day` at the place, as a UTC instant (None where the sun does not set that day).
    The local day is taken as the one whose solar noon falls on `day`, so the result needs no time zone."""
    jd0 = day.toordinal() + 1721424.5          # 0h UT of that date
    minutes = 720 - 4 * longitude              # solar noon, refined below
    for _ in range(3):
        decl, eq = _sun((jd0 + minutes / 1440 - 2451545) / 36525)
        cos_ha = (math.cos(math.radians(ZENITH)) / (math.cos(math.radians(latitude)) * math.cos(math.radians(decl)))
                  - math.tan(math.radians(latitude)) * math.tan(math.radians(decl)))
        if not -1 <= cos_ha <= 1:
            return None
        ha = math.degrees(math.acos(cos_ha))
        minutes = 720 - 4 * (longitude - ha) - eq
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def window(friday: date, cfg=None) -> Optional[Tuple[datetime, datetime]]:
    """(candle-lighting on `friday`, nightfall on the Saturday after it) as UTC instants."""
    s = settings(cfg)
    start, end = (sunset(friday, s["latitude"], s["longitude"]), sunset(friday + timedelta(days=1), s["latitude"], s["longitude"]))
    if start is None or end is None:
        return None
    return start - timedelta(minutes=s["candle_minutes"]), end + timedelta(minutes=s["nightfall_minutes"])


def friday_on_or_before(day: date) -> date:
    return day - timedelta(days=(day.weekday() - 4) % 7)


def current(cfg, local_day: date, now: datetime) -> Optional[Tuple[datetime, datetime]]:
    """The Shabbat window that holds `now`, or None. `local_day` is the date at the owner's place."""
    span = window(friday_on_or_before(local_day), cfg)
    if span and span[0] <= now < span[1]:
        return span
    return None


def upcoming(cfg, local_day: date, now: datetime) -> Optional[Tuple[datetime, datetime]]:
    """The window that holds `now`, else the next one that has not ended."""
    friday = friday_on_or_before(local_day)
    for step in (0, 7):
        span = window(friday + timedelta(days=step), cfg)
        if span and now < span[1]:
            return span
    return None
