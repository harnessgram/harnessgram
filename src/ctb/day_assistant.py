"""Proactive, private help for the owner's day.

The scheduler is deliberately made of pure functions and an injected ``clock``.
The heartbeat supplies delivery functions; tests and callers never have to read
the machine's clock or configuration.  Radio delivery is *only* through the
private-radio callback passed to :func:`tick`.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional
import json

from . import shabbat_times
from .config import set_setting, setting_on
from .config import home_chat, private_dir, state_dir, write_private

DEFAULT_BRIEFING_TIME = "07:30"
MAX_WAITING = 3
QUIET_START = 22
QUIET_END = 7
BRIEFING_WINDOW = timedelta(hours=3)
FSI = "\u2068"
PDI = "\u2069"
BRIEFING_ON = "day-assistant-briefing-on"
NUDGES_ON = "day-assistant-nudges-on"
AUTO_PLAN_ON = "day-assistant-auto-plan-on"


def briefing_enabled() -> bool:
    return setting_on(BRIEFING_ON)


def set_briefing_enabled(on: bool) -> None:
    set_setting(BRIEFING_ON, on)


def nudges_enabled() -> bool:
    return setting_on(NUDGES_ON)


def set_nudges_enabled(on: bool) -> None:
    set_setting(NUDGES_ON, on)


def auto_plan_enabled() -> bool:
    return setting_on(AUTO_PLAN_ON)


def set_auto_plan_enabled(on: bool) -> None:
    set_setting(AUTO_PLAN_ON, on)


def settings(cfg) -> dict:
    raw = getattr(cfg, "day_assistant", None)
    raw = raw if isinstance(raw, dict) else {}
    when = raw.get("briefing_time", DEFAULT_BRIEFING_TIME)
    try:
        hour, minute = map(int, when.split(":"))
        assert 0 <= hour < 24 and 0 <= minute < 60
    except (ValueError, AttributeError, AssertionError):
        when = DEFAULT_BRIEFING_TIME
    return {"briefing": briefing_enabled(),
            "nudges": nudges_enabled(),
            "auto_plan": auto_plan_enabled(),
            "briefing_time": when}


def rest_time(cfg, now: datetime) -> bool:
    """True from candle-lighting to havdalah, using the repository zmanim helper."""
    try:
        if shabbat_times.current(cfg, now.date(), now) is not None:
            return True
    except Exception:
        return True
    # Existing quiet-window bridge data is also the source for entered holidays.
    try:
        from .briefing import rest_day
        return rest_day(cfg, now.timestamp())
    except (AttributeError, TypeError, ValueError):
        return True  # fail closed: never speak when the calendar cannot be established


def briefing(snapshot: dict, waiting: Iterable[dict] = (), overnight: Iterable[str] = ()) -> dict:
    """Build the ordered Cockpit/radio briefing from already-held bridge data."""
    sched = [r for r in snapshot.get("schedule", {}).get("today", []) if r.get("title")]
    chores = list(snapshot.get("chores", [])) + list(snapshot.get("routines", []))
    names = [str(r.get("name") or r.get("title") or "").strip() for r in waiting
             if isinstance(r, dict) and (r.get("name") or r.get("title"))][:MAX_WAITING]
    done = [str(x).strip() for x in overnight if str(x).strip()]
    def isolate(value) -> str:
        return FSI + str(value) + PDI

    lines = ["בוקר טוב. הנה היום שלך."]
    if sched:
        lines.append("היום: " + "; ".join(isolate(((r.get("time") + " ") if r.get("time") else "") + r["title"])
                                           for r in sched))
    if chores:
        lines.append("בבית: " + "; ".join(isolate(item) for item in chores))
    if names:
        lines.append("מחכים לך: " + "; ".join(isolate(name) for name in names))
    if done:
        lines.append("בלילה הסוכנים סיימו: " + "; ".join(isolate(item) for item in done))
    return {"title": "היום שלי", "lines": lines, "text": " ".join(lines),
            "schedule": sched, "chores": chores, "waiting": names, "overnight": done}


def due_nudges(snapshot: dict, now: datetime, sent: set[str]) -> list[dict]:
    """Timed rows in the next ten minutes, once per stable item key."""
    if now.hour >= QUIET_START or now.hour < QUIET_END:
        return []
    out = []
    for row in snapshot.get("schedule", {}).get("today", []):
        clock = row.get("time")
        title = row.get("title")
        if not isinstance(clock, str) or not isinstance(title, str) or not title.strip():
            continue
        try:
            hour, minute = map(int, clock.split(":"))
            starts = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (ValueError, AttributeError):
            continue
        key = f"{now.date().isoformat()}|{clock}|{title}"
        if key not in sent and timedelta(0) < starts - now <= timedelta(minutes=10):
            out.append({"key": key, "title": title,
                        "text": "עוד עשר דקות: " + FSI + title + PDI})
    return out


def plan(snapshot: dict, owner_items: Iterable[dict], day_start="08:00", day_end="21:00",
         now: Optional[datetime] = None) -> list[dict]:
    """Put 30-minute owner items outside conservative 30-minute event intervals."""
    def mins(value):
        h, m = map(int, value.split(":")); return h * 60 + m
    busy = []
    for row in snapshot.get("schedule", {}).get("today", []):
        try:
            start = mins(row.get("time"))
        except (AttributeError, TypeError, ValueError):
            continue
        busy.append((start, start + 30))
    busy.sort()
    moment = now or datetime.now().astimezone()
    cursor_now = moment.hour * 60 + moment.minute + (1 if moment.second or moment.microsecond else 0)
    slots, cursor = [], max(mins(day_start), cursor_now)
    for item in owner_items:
        title = str(item.get("name") or item.get("title") or "").strip()
        if not title:
            continue
        while True:
            overlaps = [end for start, end in busy if cursor < end and start < cursor + 30]
            if not overlaps:
                break
            cursor = max(overlaps)
        if cursor + 30 > mins(day_end):
            break
        slots.append({"time": f"{cursor // 60:02d}:{cursor % 60:02d}", "title": title})
        cursor += 30
    return slots


def tick(cfg, *, clock: Callable[[], datetime], snapshot: Callable[[], dict], state: dict,
         waiting: Callable[[], Iterable[dict]] = lambda: (), overnight: Callable[[], Iterable[str]] = lambda: (),
         private_radio: Callable[[str], bool], cockpit_card: Callable[[dict], bool],
         push: Callable[[str], None], can_speak: Callable[[], bool], reserve_speech: Callable[[str], bool]) -> dict:
    """Run one scheduler round. All I/O and time are explicit, injectable boundaries."""
    now, conf = clock(), settings(cfg)
    if rest_time(cfg, now):
        return {"briefing": False, "nudges": 0}
    if not conf["briefing"] and not conf["nudges"]:
        return {"briefing": False, "nudges": 0}
    snap, result = snapshot(), {"briefing": False, "nudges": 0}
    today = now.date().isoformat()
    briefing_hour, briefing_minute = map(int, conf["briefing_time"].split(":"))
    briefing_at = now.replace(hour=briefing_hour, minute=briefing_minute, second=0, microsecond=0)
    briefing_due = (QUIET_END <= now.hour < QUIET_START and
                    briefing_at <= now <= briefing_at + BRIEFING_WINDOW)
    if conf["briefing"] and now.weekday() != 5 and briefing_due and state.get("briefing") != today:
        card = briefing(snap, waiting(), overnight())
        card["date"] = today
        delivered = cockpit_card(card) is not False
        if can_speak() and reserve_speech(card["text"]):
            delivered = private_radio(card["text"]) or delivered
        if delivered:
            state["briefing"] = today
            result["briefing"] = True
    if conf["nudges"]:
        sent = set(state.get("nudges", ()))
        for row in due_nudges(snap, now, sent):
            radio_sent = can_speak() and reserve_speech(row["text"]) and private_radio(row["text"])
            try:
                push(row["text"])
                delivered = True
            except Exception:
                delivered = radio_sent
            if delivered:
                sent.add(row["key"])
                result["nudges"] += 1
        state["nudges"] = sorted(k for k in sent if k.startswith(today + "|"))
    return result


def heartbeat_tick(cfg, bot, stamp: float) -> dict:
    """Production adapter used by heartbeat; all policy remains in the injected core above."""
    from . import radio, schedule, tower_home, waiting as waiting_source
    path = state_dir() / "day-assistant.json"
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        state = stored if isinstance(stored, dict) else {}
    except (OSError, ValueError):
        state = {}
    now = datetime.fromtimestamp(stamp, schedule.zone(cfg))

    def waits():
        rows = []
        for group in waiting_source.waiting_items(cfg, now=stamp):
            for item in group:
                if isinstance(item, dict):
                    rows.append({"name": item.get("text") or item.get("title")})
                elif isinstance(item, tuple) and item and isinstance(item[-1], dict):
                    rows.append({"name": item[-1].get("text")})
        return rows

    def speak(text):
        return radio.queue_private_schedule_summary(cfg, text).get("queued", False)

    def push(text):
        bot.call("sendMessage", {"chat_id": home_chat(cfg), "text": text,
                                 "disable_notification": False})

    out = tick(cfg, clock=lambda: now, snapshot=lambda: tower_home.today_at_home(cfg, now), state=state,
               waiting=waits, private_radio=speak,
               cockpit_card=lambda card: (state.update(card=card) or True), push=push,
               can_speak=lambda: radio.private_audio_on(),
               reserve_speech=lambda _text: radio.private_schedule_budget_available(cfg))
    write_private(private_dir(state_dir()) / path.name, json.dumps(state, ensure_ascii=False))
    return out
