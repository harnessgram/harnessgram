"""The readers behind the Cockpit home mode (home_context.py keeps the rules and owns no readers).

Two inputs are assembled here from what the bridge already knows, so ``build_state`` stays small and tests can
inject plain data:

* ``rest_state``: whether Shabbat or a holiday holds the moment.  Shabbat is the computed window from
  candle-lighting to nightfall (shabbat_times.py, nothing from the network); a holiday is a dated window the owner
  configured in ``voice_digest_quiet_windows``.  The weekly, weekday-based quiet window and "all of Saturday" are not
  used: they are wider than the real times and would keep the home quiet after Shabbat ended.  Every uncertain
  case returns ``None``, which the mode engine treats as quiet.
* ``at_mac_inputs``: a presence signal built only from the bridge's own activity view (the live sessions' last
  activity) and autopilot's explicit away flag.  It reads no input device and needs no macOS permission.  Session
  activity is a proxy: it also moves while an agent works, so it says "recent work at the Mac", not "hands on keys".
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from . import shabbat_times, voice_digest


def rest_state(cfg, moment: datetime) -> Optional[bool]:
    """True inside Shabbat or a configured holiday window, False on an established ordinary moment, None when the
    calendar cannot be established (no computable times, or an unreadable window list)."""
    try:
        span = shabbat_times.window(shabbat_times.friday_on_or_before(moment.date()), cfg)
        if span is None:
            return None
        if span[0] <= moment < span[1]:
            return True
        windows = getattr(cfg, "voice_digest_quiet_windows", None)
        if not voice_digest.valid_windows(windows):
            return None
        local = moment.replace(tzinfo=None)
        for window in windows:
            start, end = voice_digest._endpoint(window["start"]), voice_digest._endpoint(window["end"])
            if isinstance(start, datetime) and start <= local < end:
                return True
    except Exception:
        return None
    return False


def at_mac_inputs(sessions, now: float, away: bool = False) -> dict:
    """The presence part of the mode engine's inputs: an explicit False while away mode is on, else the newest
    activity of a live session (absent when none is known, so the engine stays on its neutral mode)."""
    if away:
        return {"at_mac": False}
    ages = [s["age"] for s in sessions or ()
            if isinstance(s, dict) and s.get("state") != "ended"
            and isinstance(s.get("age"), (int, float)) and not isinstance(s.get("age"), bool) and s["age"] >= 0]
    return {"last_terminal_activity": now - min(ages)} if ages else {}
