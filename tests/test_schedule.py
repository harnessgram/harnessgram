"""The owner's personal schedule: the store, the line reader, the calendar source, the panel routes and /todo."""
import http.client
import json
import os
import stat
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import GOOD_TOKEN
from ctb import config as config_mod
from ctb import panel, schedule
from ctb.config import Config, ConfigError, from_dict, state_dir
from test_panel import OWNER, signed

FIXTURE = Path(__file__).parent / "fixtures" / "schedule_sample.ics"
# Sunday 15 November 2026, 12:00 in Jerusalem (UTC+2).
NOW = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)


def cfg(**kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, **kw)


@pytest.fixture(autouse=True)
def fresh_cache():
    schedule._CACHE.clear()
    yield
    schedule._CACHE.clear()


# --- the line reader ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,title,day,clock", [
    ("פגישה עם דנה מחר בשעה 17:00", "פגישה עם דנה", "2026-11-16", "17:00"),
    ("פגישה עם דנה מחר ב-17:00", "פגישה עם דנה", "2026-11-16", "17:00"),
    ("call mom tomorrow 5pm", "call mom", "2026-11-16", "17:00"),
    ("Tomorrow at 5:30 pm dentist", "dentist", "2026-11-16", "17:30"),
    ("dentist tomorrow", "dentist", "2026-11-16", None),
    ("לקנות חלב היום", "לקנות חלב", "2026-11-15", None),
    ("למחר לסדר את המחסן", "לסדר את המחסן", "2026-11-16", None),
    ("מחרתיים לשלם חשבון", "לשלם חשבון", "2026-11-17", None),
    ("12am backup tomorrow", "backup", "2026-11-16", "00:00"),
    ("lunch tomorrow 12pm", "lunch", "2026-11-16", "12:00"),
    ("חתונה 2026-11-20 בשעה 19:00", "חתונה", "2026-11-20", "19:00"),
    ("חתונה 2026-11-20", "חתונה", "2026-11-20", None),
    # a time with no day: today when it is still ahead
    ("להתקשר לרופא ב-18:30", "להתקשר לרופא", "2026-11-15", "18:30"),
    ("run at 6pm", "run", "2026-11-15", "18:00"),
    ("ריצה בשעה 17", "ריצה", "2026-11-15", "17:00"),
])
def test_known_words_become_a_date_and_time(text, title, day, clock):
    got = schedule.parse(text, None, NOW)
    assert (got.title, got.date, got.time) == (title, day, clock)


@pytest.mark.parametrize("text", [
    "קניות לשבת",                              # no day or time at all
    "להתקשר ב-09:30",                          # today's 09:30 has passed: no guessing tomorrow
    "call at 8am",                             # same, English
    "מחר ומחרתיים לנקות",                       # two different days
    "tomorrow or today maybe",
    "פגישה ביום שלישי בשעה 14:00",              # weekdays are not guessed
    "לפגוש את דנה ב-5",                         # a bare 5 may be morning or evening
    "2020-01-01 משהו ישן",                      # a past date
    "2026-02-30 משהו",                          # an impossible date
    "לקנות 3 תפוחים ו-5 בננות",                  # numbers are not times
    "גרסה 12.30 יוצאת",
    "ללכת לים ביום שישי",
    "meet Dana next Monday at 5pm",
    "תור לרופא ב-14/12 בשעה 10:00",
    "dentist on March 3 at 5pm",
    "בעוד שבוע לשלם בשעה 18:00",
])
def test_unknown_or_unsure_text_stays_undated(text):
    got = schedule.parse(text, None, NOW)
    assert got.date is None and got.time is None and got.title == text


@pytest.mark.parametrize("text,title,day", [
    ("מחר בשעה 5 משהו", "בשעה 5 משהו", "2026-11-16"),            # the day is certain, the bare hour is not
    ("tomorrow 9:00-10:00 gym", "9:00-10:00 gym", "2026-11-16"),   # a range is not one time
    ("tomorrow 14:00 or 15:00 gym", "14:00 or 15:00 gym", "2026-11-16"),
    ("היום 25:99 בדיקה", "25:99 בדיקה", "2026-11-15"),             # an impossible hour stays text
])
def test_an_unsure_time_stays_in_the_title_and_the_date_is_kept(text, title, day):
    got = schedule.parse(text, None, NOW)
    assert (got.title, got.date, got.time) == (title, day, None)


def test_a_line_that_is_only_a_day_keeps_its_words():
    got = schedule.parse("מחר", None, NOW)
    assert got.title == "מחר" and got.date is None


def test_the_zone_decides_what_today_is():
    late = datetime(2026, 11, 15, 22, 30, tzinfo=timezone.utc)  # 00:30 on the 16th in Jerusalem, still the 15th in UTC
    assert schedule.parse("x היום", cfg(), late).date == "2026-11-16"
    assert schedule.parse("x היום", cfg(timezone="UTC"), late).date == "2026-11-15"
    assert schedule.parse("x היום", cfg(timezone="not/a-zone"), late).date == "2026-11-16"  # falls back to the default


@pytest.mark.parametrize("text", ["", "   ", "x" * 201, "a\x00b", "a\x1bb", 5, None, ["x"]])
def test_unusable_lines_are_refused(text):
    with pytest.raises(schedule.ScheduleError) as err:
        schedule.parse(text, None, NOW)
    assert err.value.code == "text"


def test_the_longest_line_is_accepted_and_the_title_is_capped():
    got = schedule.parse("y" * 200, None, NOW)
    assert len(got.title) == schedule.MAX_TITLE


@pytest.mark.parametrize("name,ok", [("Asia/Jerusalem", True), ("UTC", True), ("America/Argentina/Buenos_Aires", True),
                                     ("Nowhere/Land", False), ("../etc", False), ("", False), (5, False)])
def test_timezone_names(name, ok):
    assert schedule.valid_timezone(name) is ok


def test_config_validates_the_new_keys():
    assert from_dict({"timezone": "UTC"}).timezone == "UTC"
    assert from_dict({}).timezone == "Asia/Jerusalem" and from_dict({}).schedule_sources == []
    with pytest.raises(ConfigError):
        from_dict({"timezone": "Mars/Base"})
    for bad in ("x", ["http://example.invalid/a.ics"], ["relative.ics"], ["/a/b.txt"], [5], ["https://"]):
        with pytest.raises(ConfigError):
            from_dict({"schedule_sources": bad})
    good = ["https://example.invalid/private/feed.ics", "/home/x/cal.ics"]
    assert from_dict({"schedule_sources": good}).schedule_sources == good


# --- the private store ----------------------------------------------------------------------------------------------

def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_add_writes_one_private_file_atomically():
    row = schedule.add(cfg(), "פגישה מחר בשעה 17:00", NOW)
    path = state_dir() / schedule.ITEMS_FILE
    assert mode(path) == 0o600 and mode(state_dir()) == 0o700
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["items"][0]["title"] == "פגישה"
    assert row["date"] == "2026-11-16" and row["time"] == "17:00" and row["done"] is False
    assert not [p for p in state_dir().iterdir() if p.name.startswith(schedule.ITEMS_FILE + ".")]  # no temp file left


def test_a_failed_write_keeps_the_old_file(monkeypatch):
    schedule.add(cfg(), "first", NOW)
    before = (state_dir() / schedule.ITEMS_FILE).read_bytes()

    def boom(src, dst):
        raise OSError("disk")

    monkeypatch.setattr(config_mod.os, "replace", boom)
    with pytest.raises(schedule.ScheduleError) as err:
        schedule.add(cfg(), "second", NOW)
    assert err.value.code == "unavailable"
    assert (state_dir() / schedule.ITEMS_FILE).read_bytes() == before
    assert not [p for p in state_dir().iterdir() if p.name.startswith(schedule.ITEMS_FILE + ".")]


def test_an_identical_open_item_is_not_added_twice():
    a = schedule.add(cfg(), "לשלם חשבון", NOW)
    b = schedule.add(cfg(), "לשלם חשבון", NOW)
    assert a["id"] == b["id"] and len(schedule.items()) == 1
    schedule.set_done(a["id"], True, NOW)
    assert schedule.add(cfg(), "לשלם חשבון", NOW)["id"] != a["id"]


def test_the_list_is_capped_and_a_full_open_list_refuses():
    for n in range(schedule.MAX_ITEMS):
        schedule.add(cfg(), "item %d" % n, NOW)
    with pytest.raises(schedule.ScheduleError) as err:
        schedule.add(cfg(), "one too many", NOW)
    assert err.value.code == "full" and len(schedule.items()) == schedule.MAX_ITEMS


def test_a_full_list_makes_room_with_the_oldest_done_item():
    ids = [schedule.add(cfg(), "item %d" % n, NOW + timedelta(seconds=n))["id"] for n in range(schedule.MAX_ITEMS)]
    schedule.set_done(ids[5], True, NOW + timedelta(hours=1))
    schedule.set_done(ids[9], True, NOW + timedelta(hours=2))
    schedule.add(cfg(), "new", NOW + timedelta(hours=3))
    left = {r["id"] for r in schedule.items()}
    assert ids[5] not in left and ids[9] in left and len(left) == schedule.MAX_ITEMS


def test_done_items_are_pruned_after_a_week():
    keep, old = schedule.add(cfg(), "keep", NOW), schedule.add(cfg(), "old", NOW)
    schedule.set_done(old["id"], True, NOW)
    week = NOW + timedelta(days=schedule.DONE_KEEP_DAYS, minutes=1)
    assert {r["title"] for r in schedule.items(week.timestamp())} == {"keep"}
    schedule.add(cfg(), "later", week)  # a write drops it from the file too
    assert {r["title"] for r in json.loads((state_dir() / schedule.ITEMS_FILE).read_text())["items"]} == {"keep", "later"}
    assert keep["id"]


def test_done_and_undo_and_remove():
    row = schedule.add(cfg(), "x", NOW)
    assert schedule.set_done(row["id"], True, NOW)["done"] is True
    assert schedule.set_done(row["id"], False, NOW)["done_at"] is None
    schedule.remove(row["id"])
    assert schedule.items() == []
    for call in (lambda: schedule.set_done(row["id"], True), lambda: schedule.remove(row["id"])):
        with pytest.raises(schedule.ScheduleError) as err:
            call()
        assert err.value.code == "unknown"
    for ident in ("zz", "../x", "A" * 12, 5, None):
        with pytest.raises(schedule.ScheduleError):
            schedule.set_done(ident, True)


def test_a_damaged_or_hostile_file_reads_as_a_clean_list():
    path = state_dir() / schedule.ITEMS_FILE
    state_dir().mkdir(parents=True, exist_ok=True)
    for text in ("not json", "[]", '{"items": 5}', '{"items": [1, "x", {"id": "../../x", "title": "t", "created": 1}]}'):
        path.write_text(text)
        assert schedule.items() == []
    good = {"id": "a" * 12, "title": "ok", "date": "2026-11-16", "time": "09:00", "done": False, "created": 5}
    bad_date = dict(good, id="b" * 12, date="20261116")
    bad_time = dict(good, id="c" * 12, time="25:00")
    path.write_text(json.dumps({"items": [good, bad_date, bad_time]}))
    assert [r["id"] for r in schedule.items()] == ["a" * 12]


def test_concurrent_adds_keep_every_item():
    errors = []

    def work(n):
        try:
            schedule.add(cfg(), "item %d" % n, NOW)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(schedule.items()) == 12


# --- the card's view ------------------------------------------------------------------------------------------------

def titles(rows):
    return [r["title"] for r in rows]


def test_view_sorts_by_time_then_undated_and_splits_the_days():
    schedule.add(cfg(), "אחרי הצהריים היום 16:00", NOW)
    schedule.add(cfg(), "בוקר מחר 08:00", NOW)
    schedule.add(cfg(), "ללא שעה", NOW)
    schedule.add(cfg(), "late evening ב-21:15", NOW)
    schedule.add(cfg(), "afternoon ב-14:00", NOW)
    got = schedule.view(cfg(), NOW, source_list=[])
    assert got["today"] == "2026-11-15" and got["tomorrow"] == "2026-11-16"
    today = got["days"]["today"]
    assert [r["time"] for r in today] == ["14:00", "16:00", "21:15", None]
    assert titles(got["days"]["tomorrow"]) == ["בוקר"] and got["open"] == {"today": 4, "tomorrow": 1}
    assert got["calendar"] == {"configured": False, "ok": True}


def test_an_open_item_from_an_earlier_day_stays_on_today_as_late_and_done_ones_leave():
    path = state_dir() / schedule.ITEMS_FILE
    state_dir().mkdir(parents=True, exist_ok=True)
    base = {"done": False, "created": int(NOW.timestamp()), "done_at": None, "time": None}
    rows = [dict(base, id="a" * 12, title="missed", date="2026-11-10"),
            dict(base, id="b" * 12, title="finished", date="2026-11-10", done=True, done_at=int(NOW.timestamp()) - 60),
            dict(base, id="c" * 12, title="undated yesterday", date=None, done=True, done_at=int(NOW.timestamp()) - 86400),
            dict(base, id="d" * 12, title="undated today", date=None, done=True, done_at=int(NOW.timestamp()) - 60),
            dict(base, id="e" * 12, title="far", date="2026-12-01")]
    path.write_text(json.dumps({"items": rows}))
    got = schedule.view(cfg(), NOW, source_list=[])["days"]
    assert titles(got["today"]) == ["missed", "undated today"] and got["today"][0]["late"] is True
    assert got["tomorrow"] == []


# --- the calendar ---------------------------------------------------------------------------------------------------

def ics_view(**kw):
    return schedule.view(cfg(), NOW, source_list=[schedule.IcsSource(str(FIXTURE))], **kw)


def test_the_ics_fixture_is_merged_read_only_into_both_days():
    got = ics_view()
    today, tomorrow = got["days"]["today"], got["days"]["tomorrow"]
    assert [(r["title"], r["time"], r["allday"]) for r in today] == [
        ("Family day", None, True), ("Morning walk", "08:00", False), ("Call from UTC", "17:00", False),
        ("Evening class", "18:00", False)]
    # the EXDATE removes the walk on the 16th; Monday's class stays; the long title is cut
    assert [(r["title"][:20], r["time"]) for r in tomorrow] == [("Team sync, weekly pl", "09:00"), ("Evening class", "18:00")]
    assert all(r["kind"] == "cal" and r["id"] == "" and r["done"] is False for r in today + tomorrow)
    assert len(tomorrow[0]["title"]) <= schedule.MAX_TITLE
    assert got["calendar"] == {"configured": True, "ok": True}
    assert got["open"] == {"today": 0, "tomorrow": 0}  # calendar events are never counted as open work


def test_calendar_events_interleave_with_own_items_by_time():
    schedule.add(cfg(), "own noon ב-12:30", NOW)
    schedule.add(cfg(), "own undated", NOW)
    today = ics_view()["days"]["today"]
    assert [r["time"] for r in today] == [None, "08:00", "12:30", "17:00", "18:00", None]
    assert today[-1]["title"] == "own undated" and today[0]["allday"]


def test_the_ics_file_is_never_written():
    before = (FIXTURE.read_bytes(), FIXTURE.stat().st_mtime_ns)
    ics_view()
    assert (FIXTURE.read_bytes(), FIXTURE.stat().st_mtime_ns) == before


@pytest.mark.parametrize("text", ["", "garbage\nnot a calendar", "BEGIN:VEVENT\nDTSTART:zzz\nEND:VEVENT", "BEGIN:VEVENT\nSUMMARY:no start\nEND:VEVENT",
                                  "BEGIN:VEVENT\nDTSTART:20261115T100000\n", "\x00\x01\x02", "BEGIN:VEVENT\n" * 50])
def test_malformed_ics_gives_no_events_and_no_error(text):
    assert schedule.parse_ics(text, None, date(2026, 11, 15), date(2026, 11, 16)) == []


def test_one_broken_event_does_not_hide_the_others():
    text = FIXTURE.read_text()
    got = schedule.parse_ics(text, None, date(2026, 11, 15), date(2026, 11, 15))
    assert "Family day" in [e.title for e in got] and "Broken date" not in [e.title for e in got]
    assert "Unknown zone is skipped" not in [e.title for e in got] and "Cancelled thing" not in [e.title for e in got]


def test_unsupported_repeat_rules_show_only_the_first_occurrence():
    text = ("BEGIN:VEVENT\nUID:m\nDTSTART;VALUE=DATE:20261015\nRRULE:FREQ=MONTHLY;BYMONTHDAY=15\nSUMMARY:Monthly\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:n\nDTSTART;VALUE=DATE:20261115\nRRULE:FREQ=MONTHLY\nSUMMARY:First only\nEND:VEVENT\n")
    got = [e.title for e in schedule.parse_ics(text, None, date(2026, 11, 15), date(2026, 11, 16))]
    assert got == ["First only"]


def test_a_moved_occurrence_replaces_the_original():
    text = ("BEGIN:VEVENT\nUID:r\nDTSTART;TZID=Asia/Jerusalem:20261101T100000\nRRULE:FREQ=DAILY\nSUMMARY:Standup\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:r\nRECURRENCE-ID;TZID=Asia/Jerusalem:20261116T100000\nDTSTART;TZID=Asia/Jerusalem:20261116T150000\n"
            "SUMMARY:Standup moved\nEND:VEVENT\n")
    got = [(e.day, e.time, e.title) for e in schedule.parse_ics(text, None, date(2026, 11, 15), date(2026, 11, 16))]
    assert got == [("2026-11-15", "10:00", "Standup"), ("2026-11-16", "15:00", "Standup moved")]


def test_a_multi_day_all_day_event_covers_each_day():
    text = "BEGIN:VEVENT\nDTSTART;VALUE=DATE:20261114\nDTEND;VALUE=DATE:20261117\nSUMMARY:Trip\nEND:VEVENT\n"
    assert [e.day for e in schedule.parse_ics(text, None, date(2026, 11, 15), date(2026, 11, 16))] == ["2026-11-15", "2026-11-16"]


def test_the_zone_converts_calendar_times():
    text = "BEGIN:VEVENT\nDTSTART:20261115T230000Z\nSUMMARY:Late\nEND:VEVENT\n"
    assert [(e.day, e.time) for e in schedule.parse_ics(text, cfg(), date(2026, 11, 15), date(2026, 11, 16))] == [("2026-11-16", "01:00")]
    assert [(e.day, e.time) for e in schedule.parse_ics(text, cfg(timezone="UTC"), date(2026, 11, 15), date(2026, 11, 16))] == [("2026-11-15", "23:00")]


@pytest.mark.parametrize("start,end", [
    ("2026-10-25T01:30:00+03:00", "2026-10-25T01:15:00+02:00"),
    ("2026-03-27T01:30:00+02:00", "2026-03-27T03:15:00+03:00"),
])
@pytest.mark.parametrize("local_start", [False, True])
def test_ics_events_crossing_dst_keep_their_instants_and_local_display(start, end, local_start):
    beginning, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
    dtstart = ("DTSTART;TZID=Asia/Jerusalem:" + beginning.strftime("%Y%m%dT%H%M%S") if local_start else
               "DTSTART:" + beginning.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    text = ("BEGIN:VEVENT\nSUMMARY:DST\n" + dtstart + "\nDTEND:" +
            finish.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "\nEND:VEVENT\n")
    found = schedule.parse_ics(text, cfg(timezone="Asia/Jerusalem"), beginning.date(), beginning.date())
    assert len(found) == 1
    event = found[0]
    assert (event.day, event.time) == (beginning.date().isoformat(), "01:30")
    assert event.start.isoformat() == start
    assert event.end.isoformat() == end
    assert event.end.timestamp() - event.start.timestamp() == 45 * 60


def test_an_unreadable_source_marks_the_calendar_not_ok_and_keeps_the_own_items(tmp_path):
    schedule.add(cfg(), "mine", NOW)
    got = schedule.view(cfg(), NOW, source_list=[schedule.IcsSource(str(tmp_path / "missing.ics"))])
    assert titles(got["days"]["today"]) == ["mine"] and got["calendar"] == {"configured": True, "ok": False}


def test_a_file_over_two_megabytes_is_refused(tmp_path):
    big = tmp_path / "big.ics"
    big.write_bytes(b"x" * (schedule.ICS_MAX_BYTES + 5))
    with pytest.raises(ValueError):
        schedule._fetch(str(big))


def test_the_link_is_fetched_once_per_window_and_never_logged(caplog):
    secret = "https://calendar.example.invalid/private/TOKEN-123/basic.ics"
    calls = []

    def fetch(location):
        calls.append(location)
        return FIXTURE.read_text()

    source = schedule.IcsSource(secret, fetch=fetch)
    assert source.events(cfg(), date(2026, 11, 15), date(2026, 11, 16), now=1000.0)
    source.events(cfg(), date(2026, 11, 15), date(2026, 11, 16), now=1000.0 + schedule.ICS_TTL - 1)
    assert len(calls) == 1
    source.events(cfg(), date(2026, 11, 15), date(2026, 11, 16), now=1000.0 + schedule.ICS_TTL + 1)
    assert len(calls) == 2

    def failing(location):
        raise OSError("connect to %s failed" % location)

    broken = schedule.IcsSource("https://other.example.invalid/SECRET-456.ics", fetch=failing)
    with caplog.at_level("DEBUG"):
        with pytest.raises(OSError):
            broken.events(cfg(), date(2026, 11, 15), date(2026, 11, 16), now=5000.0)
        with pytest.raises(OSError):
            broken.events(cfg(), date(2026, 11, 15), date(2026, 11, 16), now=5010.0)  # a recent failure is not retried at once
        schedule.view(cfg(), NOW, source_list=[broken])
    assert "SECRET-456" not in caplog.text and "TOKEN-123" not in caplog.text and "example.invalid" not in caplog.text


def test_an_old_copy_is_shown_when_the_feed_goes_away():
    state = {"up": True}

    def fetch(location):
        if state["up"]:
            return FIXTURE.read_text()
        raise OSError("down")

    source = schedule.IcsSource("https://calendar.example.invalid/a.ics", fetch=fetch)
    assert source.events(cfg(), date(2026, 11, 15), date(2026, 11, 15), now=1000.0)
    state["up"] = False
    assert source.events(cfg(), date(2026, 11, 15), date(2026, 11, 15), now=1000.0 + schedule.ICS_TTL + 5)
    with pytest.raises(OSError):
        source.events(cfg(), date(2026, 11, 15), date(2026, 11, 15), now=schedule.ICS_STALE + 10_000)


def test_the_redirect_handler_refuses_a_plain_http_target():
    import urllib.error
    import urllib.request
    handler = schedule._HttpsOnly()
    req = urllib.request.Request("https://calendar.example.invalid/a.ics")
    with pytest.raises(urllib.error.URLError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://evil.example.invalid/a.ics")


def test_no_source_is_built_or_read_by_default(monkeypatch):
    monkeypatch.setattr(schedule, "_fetch", lambda location: pytest.fail("no network or file read unless configured"))
    assert schedule.sources(cfg()) == []
    assert schedule.view(cfg(), NOW)["calendar"] == {"configured": False, "ok": True}
    assert [type(s).__name__ for s in schedule.sources(cfg(schedule_sources=[str(FIXTURE)]))] == ["IcsSource"]


# --- the panel routes -----------------------------------------------------------------------------------------------

@pytest.fixture
def srv():
    server = panel.PanelServer(("127.0.0.1", 0), cfg(schedule_sources=[str(FIXTURE)]))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def post(server, body, path="/api/schedule", init="ok"):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
    headers = {}
    if init is not None:
        headers[panel.INIT_HEADER] = signed() if init == "ok" else init
    payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
    conn.request("POST", path, body=payload, headers=headers)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, json.loads(data)


def test_the_routes_are_declared_with_small_bodies():
    assert panel.ROUTES["/api/schedule"] <= 4096 and panel.ACTIONS["/api/schedule"] == "_schedule"
    assert panel.ROUTES["/api/schedule-view"] <= 1024 and panel.ACTIONS["/api/schedule-view"] == "_schedule_view"


@pytest.mark.parametrize("path,body", [("/api/schedule", {"action": "add", "text": "x"}), ("/api/schedule-view", {})])
def test_the_owner_login_is_required(srv, path, body):
    for init in (None, "", "garbage", signed(tamper=True), signed(user_id=OWNER + 1),
                 signed(auth_date=int(time.time()) - panel.MAX_AGE - 30)):
        code, out = post(srv, body, path=path, init=init)
        assert code == 403 and out == {"error": "forbidden"}, init
    assert schedule.items() == []


def test_changes_need_a_fresh_login_but_the_read_accepts_an_hour_old_one(srv):
    old = signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 60)
    assert post(srv, {"action": "add", "text": "x"}, init=old)[0] == 403
    assert schedule.items() == []
    assert post(srv, {}, path="/api/schedule-view", init=old)[0] == 200


def test_add_done_undo_remove_through_the_server(srv):
    code, out = post(srv, {"action": "add", "text": "פגישה מחר בשעה 17:00"})
    assert code == 200 and out["ok"] and out["item"]["title"] == "פגישה" and out["item"]["time"] == "17:00"
    ident = out["item"]["id"]
    assert "פגישה" in titles(out["view"]["days"]["tomorrow"])
    # the calendar from the configured fixture rides along
    assert any(r["kind"] == "cal" for r in out["view"]["days"]["today"] + out["view"]["days"]["tomorrow"])
    assert post(srv, {"action": "done", "id": ident, "done": True})[1]["ok"]
    assert [r["done"] for r in schedule.items()] == [True]
    assert post(srv, {"action": "done", "id": ident, "done": False})[0] == 200
    code, out = post(srv, {"action": "remove", "id": ident})
    assert code == 200 and "item" not in out and schedule.items() == []
    assert post(srv, {}, path="/api/schedule-view")[1]["view"]["calendar"]["configured"] is True


@pytest.mark.parametrize("body", [
    {}, {"action": "add"}, {"action": "add", "text": ""}, {"action": "add", "text": "   "}, {"action": "add", "text": "x" * 201},
    {"action": "add", "text": 5}, {"action": "add", "text": "x", "extra": 1}, {"action": "nope"}, {"action": 5},
    {"action": "done", "id": "a" * 12}, {"action": "done", "id": "a" * 12, "done": "yes"}, {"action": "done", "id": "../x", "done": True},
    {"action": "remove"}, {"action": "remove", "id": "zz"}, b"not json", b"[1]"])
def test_bad_bodies_are_400_and_change_nothing(srv, body):
    code, out = post(srv, body)
    assert code == 400 and out["error"] in ("bad request", "text") and schedule.items() == []


def test_an_unknown_id_is_404_and_a_view_body_must_be_empty(srv):
    assert post(srv, {"action": "done", "id": "a" * 12, "done": True})[0] == 404
    assert post(srv, {"action": "remove", "id": "a" * 12})[0] == 404
    assert post(srv, {"x": 1}, path="/api/schedule-view")[0] == 400


def test_a_full_list_is_409(srv):
    for n in range(schedule.MAX_ITEMS):
        schedule.add(cfg(), "item %d" % n, NOW)
    code, out = post(srv, {"action": "add", "text": "one too many"})
    assert code == 409 and out == {"error": "full"}


def test_the_state_poll_carries_no_schedule(srv):
    schedule.add(cfg(), "private thing", NOW)
    code, out = post(srv, {}, path="/api/state")
    assert code == 200 and "private thing" not in json.dumps(out) and "schedule" not in out


# --- /todo ----------------------------------------------------------------------------------------------------------

def test_todo_adds_and_answers_with_what_was_understood():
    replies = []
    assert schedule.telegram_command(cfg(ui_language="he"), "לשלם חשבון מחר בשעה 09:00", replies.append) == "todo"
    assert len(replies) == 1
    lines = replies[0].split("\n")
    assert lines[1] == "לשלם חשבון" and "09:00" in lines[2] and "מחר" in lines[2]
    assert [r["title"] for r in schedule.items()] == ["לשלם חשבון"]
    replies.clear()
    schedule.telegram_command(cfg(ui_language="en"), "call mom", replies.append)
    assert replies[0].split("\n") == ["Added to your schedule:", "call mom"]


@pytest.mark.parametrize("arg,key", [("", "usage"), ("   ", "usage"), ("x" * 300, "bad")])
def test_todo_without_a_usable_line_explains_and_stores_nothing(arg, key):
    replies = []
    schedule.telegram_command(cfg(ui_language="en"), arg, replies.append)
    assert "/todo" in replies[0] and schedule.items() == []


def say(rig, text, chat=OWNER, chat_type="private", uid=OWNER):
    from ctb import inbox as I
    m = {"message_id": 9, "date": 2_000_000_000, "text": text, "chat": {"id": chat, "type": chat_type}, "from": {"id": uid}}
    return I.handle({"update_id": 3, "message": m}, rig.cfg, rig.bot, rig.topics, rig.box, None, 1000, rig.team_topics)


def test_todo_command_stores_for_the_owner_and_never_reaches_a_session(tmp_path):
    from test_team import Rig
    rig = Rig(tmp_path)
    assert say(rig, "/todo לקנות חלב מחר בשעה 08:30") == "todo"
    assert [(r["title"], r["time"]) for r in schedule.items()] == [("לקנות חלב", "08:30")]
    assert rig.bot.methods("sendMessage") and "לקנות חלב" in rig.bot.methods("sendMessage")[-1]["text"]
    assert say(rig, "/todo@somebot call mom") == "todo" and len(schedule.items()) == 2
    assert say(rig, "/todo") == "todo" and len(schedule.items()) == 2


def test_todo_command_is_ignored_for_anyone_but_the_owner(tmp_path):
    from test_team import Rig
    rig = Rig(tmp_path)
    assert say(rig, "/todo secret plan", uid=99) != "todo"
    assert say(rig, "/todo secret plan", chat=-777, chat_type="supergroup") != "todo"
    assert schedule.items() == []


def test_todo_refuses_a_line_that_looks_like_a_secret(tmp_path):
    from test_team import Rig
    rig = Rig(tmp_path)
    assert say(rig, "/todo ghp_" + "a" * 36) == "refused"
    assert schedule.items() == []


# --- calendar review fixes ------------------------------------------------------------------------------------------

def day_events(text, day, tz=None):
    return [(e.time, e.title) for e in schedule.parse_ics(text, cfg(**({"timezone": tz} if tz else {})), day, day)]


NY_DAILY = ("BEGIN:VEVENT\nUID:ny\nDTSTART;TZID=America/New_York:20270301T090000\nRRULE:FREQ=DAILY\nSUMMARY:Standup\nEND:VEVENT\n")


def test_repeats_follow_the_source_zone_across_its_daylight_saving_change():
    # New York changes on 14 March 2027, Jerusalem on 26 March: the wall-clock 09:00 stays 09:00 in New York.
    assert day_events(NY_DAILY, date(2027, 3, 10)) == [("16:00", "Standup")]
    assert day_events(NY_DAILY, date(2027, 3, 15)) == [("15:00", "Standup")]
    assert day_events(NY_DAILY, date(2027, 3, 27)) == [("16:00", "Standup")]
    weekly = NY_DAILY.replace("FREQ=DAILY", "FREQ=WEEKLY;BYDAY=MO")
    assert day_events(weekly, date(2027, 3, 15)) == [("15:00", "Standup")]


def test_an_occurrence_that_crosses_midnight_lands_on_the_right_local_day():
    text = "BEGIN:VEVENT\nUID:late\nDTSTART;TZID=America/Los_Angeles:20261101T180000\nRRULE:FREQ=DAILY\nSUMMARY:Late\nEND:VEVENT\n"
    # 18:00 in Los Angeles (UTC-8) is 04:00 the next day in Jerusalem (UTC+2)
    assert day_events(text, date(2026, 11, 16)) == [("04:00", "Late")]
    assert day_events(text, date(2026, 11, 1)) == []


def test_a_huge_all_day_span_is_skipped_quickly():
    import time as clock
    text = ("BEGIN:VEVENT\nUID:h\nDTSTART;VALUE=DATE:19000101\nDTEND;VALUE=DATE:29990101\nSUMMARY:Forever\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:ok\nDTSTART;VALUE=DATE:20261101\nDTEND;VALUE=DATE:20270101\nSUMMARY:Two months\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:edge\nDTSTART;VALUE=DATE:20260101\nDTEND;VALUE=DATE:20270103\nSUMMARY:Too long\nEND:VEVENT\n")
    began = clock.monotonic()
    got = [e.title for e in schedule.parse_ics(text, cfg(), date(2026, 11, 15), date(2026, 11, 16))]
    assert clock.monotonic() - began < 1 and got == ["Two months", "Two months"]


def test_the_work_one_event_and_one_parse_may_spend_is_capped(monkeypatch):
    monkeypatch.setattr(schedule, "MAX_WORK", 50)
    text = ("BEGIN:VEVENT\nUID:w\nDTSTART;TZID=Asia/Jerusalem:20000103T100000\nRRULE:FREQ=WEEKLY;COUNT=9000\nSUMMARY:Costly\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:ok\nDTSTART;TZID=Asia/Jerusalem:20261101T100000\nRRULE:FREQ=DAILY\nSUMMARY:Cheap\nEND:VEVENT\n")
    assert [e.title for e in schedule.parse_ics(text, cfg(), date(2026, 11, 16), date(2026, 11, 16))] == ["Cheap"]


def test_the_whole_parse_stops_when_the_total_budget_is_spent(monkeypatch):
    monkeypatch.setattr(schedule, "MAX_TOTAL_WORK", 5)
    one = "BEGIN:VEVENT\nUID:%d\nDTSTART;TZID=Asia/Jerusalem:20261101T100000\nRRULE:FREQ=DAILY\nSUMMARY:E%d\nEND:VEVENT\n"
    got = schedule.parse_ics("".join(one % (n, n) for n in range(100)), cfg(), date(2026, 11, 16), date(2026, 11, 16))
    assert 0 < len(got) < 100


@pytest.mark.parametrize("rule", ["FREQ=DAILY;COUNT=2", "FREQ=WEEKLY;COUNT=2", "FREQ=WEEKLY;BYDAY=MO,TU;COUNT=3", "FREQ=DAILY;INTERVAL=3;COUNT=5"])
def test_count_is_exact_however_far_back_the_start_is(rule):
    text = "BEGIN:VEVENT\nUID:old\nDTSTART;TZID=Asia/Jerusalem:19000101T100000\nRRULE:%s\nSUMMARY:Old\nEND:VEVENT\n" % rule
    assert schedule.parse_ics(text, cfg(), date(2027, 1, 4), date(2027, 1, 5)) == []


def test_count_still_counts_the_first_occurrences():
    text = "BEGIN:VEVENT\nUID:c\nDTSTART;TZID=Asia/Jerusalem:20261110T100000\nRRULE:FREQ=DAILY;COUNT=3\nSUMMARY:Three\nEND:VEVENT\n"
    shown = [d for d in range(9, 15) if day_events(text, date(2026, 11, d))]
    assert shown == [10, 11, 12]
    weekly = text.replace("FREQ=DAILY", "FREQ=WEEKLY;BYDAY=TU,TH")  # 10 Nov 2026 is a Tuesday
    assert [d for d in range(9, 30) if day_events(weekly, date(2026, 11, d))] == [10, 12, 17]


def test_a_cancelled_occurrence_removes_that_day_from_the_series():
    text = ("BEGIN:VEVENT\nUID:r\nDTSTART;TZID=Asia/Jerusalem:20261101T100000\nRRULE:FREQ=DAILY\nSUMMARY:Standup\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:r\nRECURRENCE-ID;TZID=Asia/Jerusalem:20261116T100000\nDTSTART;TZID=Asia/Jerusalem:20261116T100000\n"
            "STATUS:CANCELLED\nSUMMARY:Standup\nEND:VEVENT\n")
    assert day_events(text, date(2026, 11, 15)) == [("10:00", "Standup")]
    assert day_events(text, date(2026, 11, 16)) == []
    assert day_events(text, date(2026, 11, 17)) == [("10:00", "Standup")]


def test_a_moved_occurrence_matches_by_instant_across_zones():
    text = ("BEGIN:VEVENT\nUID:r\nDTSTART;TZID=America/New_York:20261101T090000\nRRULE:FREQ=DAILY\nSUMMARY:Standup\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:r\nRECURRENCE-ID:20261116T140000Z\nDTSTART:20261116T200000Z\nSUMMARY:Moved\nEND:VEVENT\n")
    assert day_events(text, date(2026, 11, 16)) == [("22:00", "Moved")]


def test_a_failed_fetch_does_not_refresh_the_age_of_the_old_copy():
    state = {"up": True}

    def fetch(location):
        if state["up"]:
            return FIXTURE.read_text()
        raise OSError("down")

    source = schedule.IcsSource("https://calendar.example.invalid/a.ics", fetch=fetch)
    day = (date(2026, 11, 15), date(2026, 11, 15))
    assert source.events(cfg(), *day, now=1000.0)
    state["up"] = False
    step = schedule.ICS_STALE // 4 + 1
    for n in (1, 2, 3):  # a failure every quarter of a day keeps serving the copy...
        assert source.events(cfg(), *day, now=1000.0 + n * step)
    with pytest.raises(OSError):  # ...but only until it is a day old, however many failures came in between
        source.events(cfg(), *day, now=1000.0 + schedule.ICS_STALE + 1)
    assert schedule._CACHE[next(iter(schedule._CACHE))][0] != 1000.0 + 3 * step
