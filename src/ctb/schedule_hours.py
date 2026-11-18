"""Private, local hour-per-topic plans. Calendar anchors never move; no family sharing.

The daily proposal freezes on first use. Calendar changes rebuild it (discarding
local edits rather than silently moving an appointment). Nothing writes to Google.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from . import schedule
from .config import home_chat, private_dir, state_dir, write_private

FILE = "schedule-hours.json"


def _read():
    try:
        value = json.loads((state_dir() / FILE).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise schedule.ScheduleError("unavailable") from None


def _save(value):
    write_private(private_dir(state_dir()) / FILE, json.dumps(value, ensure_ascii=False))


def _id(kind, value):
    return hashlib.sha256((kind + ":" + value).encode()).hexdigest()[:16]


def _block(ident, title, start, end, kind, summary=""):
    return {"id": ident, "title": title[:120], "summary": summary[:200],
            "start": start, "end": end, "kind": kind, "fixed": kind == "calendar",
            "visibility": "private_work"}  # explicit seam: later sharing needs its own authorization


def propose(day, rows, topics):
    """One-hour flexible topics in 08:00–22:00 gaps; timed calendar/own items are anchors.
    All-day events and unscheduled notes remain in the existing list. Real event
    durations are retained, including appointments crossing midnight.
    """
    anchors = []
    for row in rows:
        if not row.get("time") or row.get("done") or row.get("late"):
            continue
        h, m = map(int, row["time"].split(":"))
        start = h * 60 + m
        end = min(1440, max(start + 1, row.get("end_minute") or start + 60))
        ident = _id("anchor", str(row.get("id") or row["title"]) + row["time"])
        anchors.append(_block(ident, row["title"], start, end, "calendar"))
    anchors.sort(key=lambda b: (b["start"], b["end"], b["id"]))
    flex, cursor = [], 8 * 60
    for topic in topics[:24]:
        while cursor + 60 <= 22 * 60:
            hit = next((b for b in anchors if cursor < b["end"] and cursor + 60 > b["start"]), None)
            if hit:
                cursor = hit["end"]
                continue
            flex.append(_block(_id("topic", topic["key"]), topic["title"], cursor, cursor + 60,
                               "topic", topic.get("summary", "")))
            cursor += 60
            break
    return sorted(anchors + flex, key=lambda b: (b["start"], b["id"]))


def project_topics(cfg, now):
    from . import sessions_menu, work_map
    from .topics import TopicMap
    topics = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg)
    entries = sessions_menu.collect(topics, now=now.timestamp())
    maps = work_map.build_all(cfg, entries, now.timestamp())
    topics = []
    for key, project in sorted(maps.items()):
        lines = [r for r in project["lines"] if r["status"] != "done"]
        if lines:
            topics.append({"key": "project:" + key, "title": project["name"], "summary": lines[0]["what"]})
    return topics


def plans(cfg, view, now=None, topics=None):
    local = schedule._local(cfg, now)
    topics = project_topics(cfg, local) if topics is None else topics
    result = {}
    try:
        with schedule._locked():
            original = _read()
            keep = {view["today"], view["tomorrow"]}
            store = {k: v for k, v in original.items() if k in keep}
            for key in ("today", "tomorrow"):
                day, rows = view[key], view["days"][key]
                anchors = [r for r in rows if r.get("time") and not r.get("done") and not r.get("late")]
                extra = [{"key": "own:" + r["id"], "title": r["title"]} for r in rows
                         if r["kind"] == "own" and not r["done"] and not r.get("time")]
                signature = hashlib.sha256(json.dumps([anchors, [(t["key"], t["title"]) for t in topics + extra]], sort_keys=True).encode()).hexdigest()
                rec = store.get(day)
                if not view["calendar"]["ok"]:
                    # An unavailable calendar cannot safely free a previously occupied hour.
                    result[key] = {"blocks": [], "available": False}
                    continue
                if not isinstance(rec, dict) or rec.get("anchors") != signature:
                    rec = {"anchors": signature, "blocks": propose(day, rows, topics + extra)}
                    store[day] = rec
                blocks = []
                minute = local.hour * 60 + local.minute + local.second / 60
                for block in rec["blocks"]:
                    b = dict(block)
                    b["current"] = day == local.date().isoformat() and b["start"] <= minute < b["end"]
                    b["past"] = day < local.date().isoformat() or (day == local.date().isoformat() and b["end"] <= minute)
                    blocks.append(b)
                result[key] = {"blocks": blocks, "available": True}
            if store != original:
                _save(store)
    except OSError:
        raise schedule.ScheduleError("unavailable") from None
    return result


def change(cfg, day, ident, action, minutes=None, now=None):
    """Atomic phone edits. Never move an anchor or edit history; reject collisions.
    Caller refreshes the view first so changed calendars invalidate stale edits.
    """
    local = schedule._local(cfg, now)
    if (not isinstance(day, str) or day not in {local.date().isoformat(), (local.date() + timedelta(days=1)).isoformat()}
            or not isinstance(ident, str) or action not in {"swap", "extend", "skip"}
            or (action == "extend" and (type(minutes) is not int or minutes not in (15, 30)))):
        raise schedule.ScheduleError("text")
    try:
        with schedule._locked():
            store = _read()
            rec = store.get(day) or {}
            blocks = [dict(b) for b in rec.get("blocks", [])]
            i = next((i for i, b in enumerate(blocks) if b["id"] == ident), None)
            if i is None:
                raise schedule.ScheduleError("unknown")
            b = blocks[i]
            point = local.hour * 60 + local.minute + local.second / 60 if day == local.date().isoformat() else -1
            if b["fixed"] or b["end"] <= point:
                raise schedule.ScheduleError("conflict")
            if action == "swap":
                if b["start"] < point or i + 1 == len(blocks) or blocks[i + 1]["fixed"]:
                    raise schedule.ScheduleError("conflict")
                n = blocks[i + 1]
                b["id"], n["id"] = n["id"], b["id"]
                b["title"], n["title"] = n["title"], b["title"]
                b["summary"], n["summary"] = n["summary"], b["summary"]
            else:
                delta = minutes if action == "extend" else b["start"] - b["end"]
                if action == "extend":
                    b["end"] += delta
                elif b["start"] <= point:
                    # Start the next topic immediately, at a whole-second precision boundary.
                    delta = point - b["end"]
                    b["end"] = point
                else:
                    blocks.pop(i)
                    i -= 1
                for n in blocks[i + 1:]:
                    if n["fixed"]:
                        break
                    n["start"] += delta
                    n["end"] += delta
                ordered = sorted(blocks, key=lambda n: n["start"])
                if any(n["end"] > 24 * 60 or n["start"] < 0 for n in blocks) or any(
                        a["end"] > n["start"] and not (a["fixed"] and n["fixed"])
                        for a, n in zip(ordered, ordered[1:])):
                    raise schedule.ScheduleError("conflict")
            rec["blocks"] = sorted(blocks, key=lambda n: n["start"])
            store[day] = rec
            _save(store)
    except OSError:
        raise schedule.ScheduleError("unavailable") from None


def quiet_until(cfg, now):
    """Shabbat starts thirty minutes BEFORE candle lighting; unavailable times fail closed."""
    from . import shabbat_times
    local = schedule._local(cfg, now)
    span = shabbat_times.upcoming(cfg, local.date(), local)
    if span is None:
        return True, local.timestamp()
    start, end = span
    start -= timedelta(minutes=30)
    return start <= local < end, start.timestamp()
