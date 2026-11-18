"""Hour plans and phone mutations, using only synthetic sources and isolated state."""
from datetime import date, datetime, timedelta

import pytest

from conftest import GOOD_TOKEN
from ctb import google_calendar, schedule, schedule_hours as H
from ctb.config import Config

DAY = "2026-11-16"
NOW = datetime.fromisoformat(DAY + "T07:00:00+02:00")
TOPICS = [{"key": "alpha", "title": "Project Alpha", "summary": "Review the draft"},
          {"key": "beta", "title": "Project Beta"}, {"key": "study", "title": "לימוד"}]


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(H, "project_topics", lambda *a: TOPICS)
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=111, timezone="Asia/Jerusalem")


class Calendar(schedule.ScheduleSource):
    def __init__(self, rows=()):
        self.rows = rows

    def events(self, cfg, first, last):
        return self.rows


def view(cfg, rows=(), now=NOW):
    return schedule.view(cfg, now=now, source_list=[Calendar(rows)])


def blocks(cfg, rows=(), now=NOW):
    return view(cfg, rows, now)["timeline"]["today"]["blocks"]


def test_hour_topics_preserve_real_calendar_durations_and_private_boundary(cfg):
    anchors = [schedule.Event("Appointment", DAY, "09:30", NOW.replace(hour=11, minute=15)),
               schedule.Event("All day note", DAY, None)]
    got = blocks(cfg, anchors)
    assert [(b["title"], b["start"], b["end"]) for b in got] == [
        ("Project Alpha", 480, 540), ("Appointment", 570, 675), ("Project Beta", 675, 735), ("לימוד", 735, 795)]
    assert all(b["visibility"] == "private_work" for b in got)
    assert got[1]["fixed"] and not got[0]["fixed"]
    assert all(b["title"] != "All day note" for b in got)


def test_today_tomorrow_current_and_done_items(cfg):
    task = schedule.add(cfg, "Personal task", now=NOW)
    got = view(cfg, now=NOW.replace(hour=8, minute=30))
    today = got["timeline"]["today"]["blocks"]
    assert today[0]["current"] and not today[1]["current"]
    assert not any(b["current"] for b in got["timeline"]["tomorrow"]["blocks"])
    assert today[-1]["title"] == "Personal task"
    schedule.set_done(task["id"], True, now=NOW)
    assert all(b["title"] != "Personal task" for b in blocks(cfg))


def test_swap_persists_and_never_changes_google_anchors(cfg):
    anchors = [schedule.Event("Appointment", DAY, "12:00")]
    before = blocks(cfg, anchors)
    H.change(cfg, DAY, before[0]["id"], "swap", now=NOW)
    got = blocks(cfg, anchors)
    assert got[0]["title"] == "Project Beta" and got[1]["title"] == "Project Alpha"
    assert got[-1] == before[-1]
    H.change(cfg, DAY, got[0]["id"], "extend", 15, now=NOW)
    got = blocks(cfg, anchors)
    assert [(b["start"], b["end"]) for b in got[:3]] == [(480, 555), (555, 615), (615, 675)]
    assert got[-1]["start"] == 720


def test_collision_and_bad_edits_are_atomic(cfg):
    anchors = [schedule.Event("Appointment", DAY, "11:00")]
    before = blocks(cfg, anchors)
    for ident, action, minutes in [(before[0]["id"], "extend", 15), (before[-1]["id"], "skip", None),
                                   (before[0]["id"], "extend", True), ([], "skip", None)]:
        with pytest.raises(schedule.ScheduleError):
            H.change(cfg, DAY, ident, action, minutes, now=NOW)
        assert blocks(cfg, anchors) == before


def test_skipping_current_advances_next_topic_at_the_actual_clock(cfg):
    current = NOW.replace(hour=8, minute=20, second=30)
    before = blocks(cfg, now=current)
    H.change(cfg, DAY, before[0]["id"], "skip", now=current)
    got = blocks(cfg, now=current)
    assert got[0]["past"] and got[0]["end"] == 500.5
    assert got[1]["current"] and got[1]["start"] == 500.5
    assert got[1]["end"] - got[1]["start"] == 60


def test_cannot_swap_current_or_edit_past_or_unknown_day(cfg):
    before = blocks(cfg)
    for action, now, day in [("swap", NOW.replace(hour=8, minute=20), DAY),
                             ("skip", NOW.replace(hour=10), DAY), ("skip", NOW, "2020-01-01")]:
        with pytest.raises(schedule.ScheduleError):
            H.change(cfg, day, before[0]["id"], action, now=now)


def test_a_changed_calendar_rebuilds_and_an_unavailable_one_hides_the_plan(cfg):
    before = blocks(cfg)
    H.change(cfg, DAY, before[0]["id"], "swap", now=NOW)
    changed = blocks(cfg, [schedule.Event("New anchor", DAY, "08:00")])
    assert changed[0]["fixed"] and changed[1]["title"] == "Project Alpha"

    class Failed(Calendar):
        def events(self, *a):
            raise OSError("fake offline")

    got = schedule.view(cfg, now=NOW, source_list=[Failed()])
    assert got["timeline"]["today"] == {"available": False, "blocks": []}
    assert blocks(cfg, [schedule.Event("New anchor", DAY, "08:00")]) == changed


def test_google_and_ics_keep_real_end_times_without_writes(cfg):
    ev = google_calendar.map_events([{"summary": "Anchor", "start": {"dateTime": DAY + "T10:00:00+02:00"},
                                     "end": {"dateTime": DAY + "T12:30:00+02:00"}}], cfg, date(2026, 11, 16), date(2026, 11, 17))[0]
    assert ev.end.hour == 12 and ev.end.minute == 30
    events = schedule.parse_ics("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Anchor\nDTSTART:20261116T080000Z\nDTEND:20261116T103000Z\nEND:VEVENT\nEND:VCALENDAR", cfg, date(2026, 11, 16), date(2026, 11, 17))
    assert events[0].end == ev.end


@pytest.mark.parametrize("source", ["google", "ics"])
def test_calendar_view_orders_repeated_hour_events_by_instant(cfg, source):
    day = date(2026, 10, 25)
    if source == "google":
        events = google_calendar.map_events([
            {"summary": "Later", "start": {"dateTime": "2026-10-25T01:15:00+02:00"}},
            {"summary": "Earlier", "start": {"dateTime": "2026-10-25T01:30:00+03:00"}},
        ], cfg, day, day)
    else:
        events = schedule.parse_ics(
            "BEGIN:VEVENT\nSUMMARY:Later\nDTSTART:20261024T231500Z\nEND:VEVENT\n"
            "BEGIN:VEVENT\nSUMMARY:Earlier\nDTSTART:20261024T223000Z\nEND:VEVENT\n", cfg, day, day)
    rows = view(cfg, events, now=datetime.fromisoformat("2026-10-25T00:00:00+03:00"))["days"]["today"]
    assert [(r["title"], r["time"]) for r in rows] == [("Earlier", "01:30"), ("Later", "01:15")]


def test_shabbat_silence_starts_thirty_minutes_before_candles(cfg):
    from ctb import shabbat_times
    candle, end = shabbat_times.window(date(2026, 11, 20), cfg)
    boundary = candle - timedelta(minutes=30)
    assert H.quiet_until(cfg, boundary - timedelta(seconds=1))[0] is False
    assert H.quiet_until(cfg, boundary)[0] is True
    assert H.quiet_until(cfg, end - timedelta(seconds=1))[0] is True
    assert H.quiet_until(cfg, end)[0] is False


def test_overnight_google_and_ics_anchors_cover_both_days(cfg):
    first, last = date(2026, 11, 16), date(2026, 11, 17)
    google = google_calendar.map_events([{"summary": "Overnight", "start": {"dateTime": DAY + "T23:00:00+02:00"},
                                         "end": {"dateTime": "2026-11-17T10:30:00+02:00"}}], cfg, first, last)
    ics = schedule.parse_ics("BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Overnight\nDTSTART:20261116T210000Z\nDTEND:20261117T083000Z\nEND:VEVENT\nEND:VCALENDAR", cfg, first, last)
    for found in (google, ics):
        assert [(e.day, e.time) for e in found] == [(DAY, "23:00"), ("2026-11-17", "00:00")]
        data = view(cfg, found)["timeline"]
        assert data["today"]["blocks"][-1]["end"] == 1440
        got = data["tomorrow"]["blocks"]
        assert got[0]["fixed"] and got[0]["end"] == 630
        assert got[1]["start"] == 630


@pytest.mark.parametrize("first,last,utc_start,local_end,end_hour", [
    ("2026-03-26", "2026-03-27", "20260326T210000Z", "20260327T090000", 9),
    ("2026-10-24", "2026-10-25", "20261024T200000Z", "20261025T080000", 8),
])
@pytest.mark.parametrize("zones", ["mixed", "local", "utc"])
def test_overnight_dst_event_keeps_its_real_end_and_reserves_the_full_anchor(
        cfg, first, last, utc_start, local_end, end_hour, zones):
    dtstart = ("DTSTART:" + utc_start if zones == "utc" else
               "DTSTART;TZID=Asia/Jerusalem:" + first.replace("-", "") + "T230000")
    dtend = ("DTEND;TZID=Asia/Jerusalem:" + local_end if zones == "local" else
             "DTEND:" + last.replace("-", "") + "T060000Z")
    text = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:dst-anchor\nSUMMARY:Overnight\n" + dtstart + "\n" + dtend + "\nEND:VEVENT\nEND:VCALENDAR"
    events = schedule.parse_ics(text, cfg, date.fromisoformat(first), date.fromisoformat(last))
    assert [(e.day, e.time) for e in events] == [(first, "23:00"), (last, "00:00")]
    assert all(e.end.hour == end_hour and e.end.minute == 0 for e in events)
    now = datetime.fromisoformat(last + "T07:00:00").replace(tzinfo=schedule.zone(cfg))
    today = blocks(cfg, events, now=now)
    assert today[0]["fixed"] and today[0]["start"] == 0 and today[0]["end"] == end_hour * 60
    assert today[1]["start"] == end_hour * 60
    assert all(b["fixed"] or b["start"] >= end_hour * 60 for b in today)


def test_timeline_phone_route_uses_only_the_fake_read_source(cfg, monkeypatch):
    from types import SimpleNamespace
    from ctb import panel
    monkeypatch.setattr(schedule, "sources", lambda *a: [Calendar()])
    monkeypatch.setattr(schedule, "_local", lambda cfg, now: NOW)
    before = blocks(cfg)
    handler = object.__new__(panel.PanelHandler)
    handler.server = SimpleNamespace(cfg=cfg)
    handler._schedule_off = lambda: False
    handler._extend = lambda *a: None
    handler._request = lambda: {"action": "extend", "date": DAY, "id": before[0]["id"], "minutes": 15}
    replies = []
    handler._schedule_answer = lambda row: replies.append(view(cfg))
    handler._json = lambda code, data: pytest.fail(str((code, data)))
    handler._schedule()
    assert replies[0]["timeline"]["today"]["blocks"][0]["end"] == 555


@pytest.mark.parametrize("repeat", ["", "RRULE:FREQ=WEEKLY;COUNT=1\n"])
def test_honolulu_overnight_anchor_reserves_its_second_jerusalem_day(cfg, repeat):
    text = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:overnight-hnl\nSUMMARY:Overnight\n"
            "DTSTART;TZID=Pacific/Honolulu:20261114T233000\n"
            "DTEND;TZID=Pacific/Honolulu:20261115T223000\n" + repeat + "END:VEVENT\nEND:VCALENDAR")
    found = schedule.parse_ics(text, cfg, date(2026, 11, 15), date(2026, 11, 16))
    assert [(e.day, e.time) for e in found] == [("2026-11-15", "11:30"), (DAY, "00:00")]
    assert found[-1].end.isoformat() == DAY + "T10:30:00+02:00"
    got = blocks(cfg, found)
    assert got[0]["fixed"] and (got[0]["start"], got[0]["end"]) == (0, 630)
    assert got[1]["title"] == "Project Alpha" and got[1]["start"] == 630


def test_session_only_project_is_a_scheduled_topic(monkeypatch, tmp_path):
    from ctb import sessions_menu, work_map
    from ctb.config import home_chat, state_dir
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=111, timezone="Asia/Jerusalem",
                 work_map={"ledger_dir": str(tmp_path / "empty-ledger")})
    entry = sessions_menu.Entry(session="fake-session", name="Alpha: Review draft", thread=12,
                                age=5, state="working", closed=False, chat=None)
    seen = []

    def collect(topics, now):
        seen.append((topics.path, topics.chat_id, topics.cfg, now))
        return [entry]

    monkeypatch.setattr(sessions_menu, "collect", collect)
    expected = work_map.build_all(cfg, [entry], NOW.timestamp())
    assert expected["alpha"]["lines"][0]["what"] == "Review draft"
    assert H.project_topics(cfg, NOW) == [
        {"key": "project:alpha", "title": "Alpha", "summary": "Review draft"}]
    assert seen == [(state_dir() / "topics.json", home_chat(cfg), cfg, NOW.timestamp())]
    got = blocks(cfg)
    assert got[0]["title"] == "Alpha" and got[0]["start"] == 480
