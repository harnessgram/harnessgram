"""Deterministic context signals for the adaptive Cockpit home.

This module deliberately owns no readers.  The panel adapter supplies the
calendar, terminal, family-board and work snapshots so callers and tests can
use a fixed clock and plain data.  The adapters that build those snapshots from the bridge's own state live in
home_inputs.py.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

QUIET_START = 22
QUIET_END = 7
DEFAULT_BRIEFING = "07:30"
DEFAULT_AT_MAC_MINUTES = 15
BRIEFING_WINDOW = timedelta(hours=3)


def _setting(cfg, name, default):
    raw = getattr(cfg, "home_context", None)
    return raw.get(name, default) if isinstance(raw, dict) else default


def _briefing_time(cfg) -> time:
    raw = getattr(cfg, "day_assistant", None)
    value = raw.get("briefing_time", DEFAULT_BRIEFING) if isinstance(raw, dict) else DEFAULT_BRIEFING
    try:
        hour, minute = map(int, value.split(":"))
        return time(hour, minute)
    except (AttributeError, TypeError, ValueError):
        return time(7, 30)


def _stamp(value):
    if isinstance(value, datetime):
        return value.timestamp()
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _family_now(value, now: datetime) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("now"), bool):
            return value["now"]
        value = value.get("routines", value.get("daily", ()))
    if not isinstance(value, (list, tuple)):
        return False
    clock = now.strftime("%H:%M")
    for row in value:
        if not isinstance(row, dict) or row.get("done"):
            continue
        slot = row.get("slot") or row.get("time")
        if slot == "now" or slot == clock:
            return True
        if isinstance(slot, dict) and slot.get("start", "99:99") <= clock < slot.get("end", "00:00"):
            return True
    return False


def context(cfg, now: datetime, *, inputs: dict) -> dict:
    """Return the current home mode from injected snapshots only.

    ``shabbat_times`` is tri-state: ``None`` means the calendar could not be
    established and therefore fails quiet.  An adapter should pass ``False``
    when it successfully established an ordinary day.
    """
    shabbat = inputs.get("shabbat_or_holiday", inputs.get("shabbat_times"))
    shabbat_quiet = shabbat is not False
    quiet_night = now.hour >= QUIET_START or now.hour < QUIET_END
    briefing_at = datetime.combine(now.date(), _briefing_time(cfg), tzinfo=now.tzinfo)
    morning = briefing_at <= now <= briefing_at + BRIEFING_WINDOW
    family = _family_now(inputs.get("family_board", inputs.get("family_routine_now",
                         inputs.get("evening_family"))), now)

    last_activity = _stamp(inputs.get("last_terminal_activity", inputs.get("terminal_activity")))
    minutes = _setting(cfg, "at_mac_minutes", DEFAULT_AT_MAC_MINUTES)
    minutes = minutes if isinstance(minutes, (int, float)) and minutes >= 0 else DEFAULT_AT_MAC_MINUTES
    supplied_at_mac = inputs.get("at_mac")
    at_mac = supplied_at_mac if isinstance(supplied_at_mac, bool) else (
        last_activity is not None and 0 <= now.timestamp() - last_activity <= minutes * 60)

    decisions = inputs.get("decisions", ())
    decisions = decisions if isinstance(decisions, (list, tuple)) else ()
    open_decisions = [d for d in decisions if isinstance(d, dict) and not d.get("ended") and d.get("status") != "done"]
    ages = [max(0, now.timestamp() - stamp) for d in open_decisions
            if (stamp := _stamp(d.get("updated", d.get("at")))) is not None]
    waiting = inputs.get("decisions_waiting")
    if isinstance(waiting, dict):
        decision_count = waiting.get("count", 0)
        oldest_age = waiting.get("oldest_age")
    else:
        decision_count, oldest_age = len(open_decisions), max(ages) if ages else None
    sessions = inputs.get("sessions") or ()
    cards = inputs.get("progress_cards", inputs.get("cards")) or ()
    sessions = sessions if isinstance(sessions, (list, tuple)) else ()
    cards = cards if isinstance(cards, (list, tuple)) else ()
    running = sum(1 for s in sessions if isinstance(s, dict) and s.get("state") in ("working", "running"))
    progress = sum(1 for c in cards if isinstance(c, dict) and c.get("kind", "progress") == "progress"
                   and not c.get("ended") and not c.get("finished") and not c.get("auto_closed"))
    supplied_work = inputs.get("active_work")
    active_work = supplied_work if isinstance(supplied_work, (bool, int)) else running + progress

    signals = {"shabbat_or_holiday": shabbat_quiet, "quiet_night": quiet_night, "morning": morning,
               "evening_family": family, "at_mac": at_mac,
               "decisions_waiting": {"count": decision_count, "oldest_age": oldest_age},
               "active_work": active_work}
    if shabbat_quiet:
        mode, why = "shabbat", "shabbat_or_holiday"
    elif quiet_night:
        mode, why = "night", "quiet_night"
    elif family:
        mode, why = "family", "evening_family"
    elif morning:
        mode, why = "morning", "briefing_window"
    elif active_work and at_mac:
        mode, why = "work", "active_work"
    elif (last_activity is not None or isinstance(supplied_at_mac, bool)) and not at_mac:
        mode, why = "away", "away_from_mac"
    else:
        mode, why = "default", "no_strong_signal"
    return {"mode": mode, "signals": signals, "why": why}
