"""The owner's personal schedule ("הלו״ז שלי"): a small private list plus optional read-only calendar sources.

Items live in `schedule.json` in the bridge state directory (0600, written atomically, at most MAX_ITEMS; done items
disappear after DONE_KEEP_DAYS). Nothing here talks to the network unless `schedule_sources` or a connected
`google_calendar` is set, and a source's link or token is never logged or returned. `parse` turns a line such as "פגישה מחר בשעה 17:00" or "call mom tomorrow 5pm" into a
title plus an optional date and time; whatever it cannot read with certainty stays in the title and the item is undated.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .config import private_dir, state_dir, write_private

log = logging.getLogger("ctb.schedule")

ITEMS_FILE = "schedule.json"
LOCK_FILE = "schedule.lock"
DEFAULT_ZONE = "Asia/Jerusalem"
MAX_ITEMS = 200            # own items kept; done ones go first
DONE_KEEP_DAYS = 7         # a done item is pruned after this many days
MAX_TEXT = 200             # the longest line taken from the owner
MAX_TITLE = 120
ID_RE = re.compile(r"[0-9a-f]{12}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ScheduleError(Exception):
    """code: "text" (unusable line), "full" (200 open items), "unknown" (no such item), "unavailable" (disk)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --- time zone ------------------------------------------------------------------------------------------------------

def valid_timezone(name) -> bool:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+){0,2}", name):
        return False
    try:
        ZoneInfo(name)
    except Exception:
        return False
    return True


def zone(cfg=None) -> tzinfo:
    """The configured zone (default Asia/Jerusalem); the computer's own zone when the zone database is missing."""
    name = getattr(cfg, "timezone", None) or DEFAULT_ZONE
    for candidate in (name, DEFAULT_ZONE):
        try:
            return ZoneInfo(candidate)
        except Exception:
            continue
    return datetime.now().astimezone().tzinfo or timezone.utc


def _local(cfg, now: Optional[datetime]) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(zone(cfg))


# --- reading a line -------------------------------------------------------------------------------------------------

@dataclass
class Parsed:
    title: str
    date: Optional[str] = None   # YYYY-MM-DD in the owner's zone
    time: Optional[str] = None   # HH:MM


_DAY_OFFSET = {"היום": 0, "today": 0, "מחר": 1, "tomorrow": 1, "מחרתיים": 2}
_DAY = re.compile(r"(?<!\w)[לו]?(?P<w>היום|מחר|מחרתיים|today|tomorrow)(?!\w)", re.I)
_ISO = re.compile(r"(?<![\w-])(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})(?![\w-])")
_PRE = r"(?:(?:בשעה|at)\s*|ב[-\u05be]|@\s*)?"
_AP = r"(?P<ap>[ap]\.?m\.?)"
_NOT_AFTER = r"(?![\w:]|[-\u2013]\d)"
# 17:00, 5:30pm (a colon means a clock time), and 5pm
_CLOCK = re.compile(r"(?<![\w:/.\-])" + _PRE + r"(?P<h>\d{1,2}):(?P<m>\d{2})(?:\s?" + _AP + r")?" + _NOT_AFTER, re.I)
_HOUR_AP = re.compile(r"(?<![\w:/.\-])" + _PRE + r"(?P<h>\d{1,2})\s?" + _AP + _NOT_AFTER, re.I)
# a bare hour needs "בשעה"/"at"/"@" and is read only when it cannot be morning or evening: 13 to 23
_BARE = re.compile(r"(?<![\w:/.\-])(?:(?:בשעה|at)\s*|@\s*)(?P<h>\d{1,2})(?![\w:.]|[-\u2013]\d)", re.I)
# words that point at a day or time this reader does not resolve: a weekday, a numeric or named date, "in N days", "next"
_VAGUE = re.compile(
    r"(?<!\w)(?:[בל]?(?:יום\s+)?(?:ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת)|(?:sun|mon|tues|wednes|thurs|fri|satur)day|"
    r"בעוד|הבא|הבאה|הקרוב|הקרובה|שעבר|שעברה|next|in\s+\d+|ינואר|פברואר|מרץ|אפריל|מאי|יוני|יולי|אוגוסט|ספטמבר|אוקטובר|נובמבר|דצמבר|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2})(?!\w)|(?<![\w:])\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?(?![\w:])", re.I)
_EDGE = " \t,.;:-\u2013\u05be\u200e\u200f"


def _clock_value(h: int, m: int, ap: Optional[str]) -> Optional[str]:
    if m > 59:
        return None
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap.lower().startswith("p") else 0)
    elif h > 23:
        return None
    return "%02d:%02d" % (h, m)


def _times(text: str) -> List[Tuple[int, int, str]]:
    found: List[Tuple[int, int, str]] = []
    for rx in (_CLOCK, _HOUR_AP, _BARE):
        for hit in rx.finditer(text):
            groups = hit.groupdict()
            if rx is _BARE and not 13 <= int(groups["h"]) <= 23:
                found.append((hit.start(), hit.end(), ""))  # seen but not certain: blocks guessing
                continue
            value = _clock_value(int(groups["h"]), int(groups.get("m") or 0), groups.get("ap"))
            found.append((hit.start(), hit.end(), value or ""))
    return found


def parse(text, cfg=None, now: Optional[datetime] = None) -> Parsed:
    """Title plus an optional date and time. Only what is certain is used and removed from the title:
    היום / מחר / מחרתיים / today / tomorrow, an ISO date, 17:00 / 5pm / 5:30 pm, and a bare hour after
    בשעה / at only from 13 to 23. A clock time with no day means today when it is still ahead, otherwise nothing
    is guessed. Two different days, a past or impossible date leave the line undated; a time that is uncertain
    (two times, an impossible hour, a bare 5) stays in the title and the item has a date only."""
    if not isinstance(text, str):
        raise ScheduleError("text")
    original = re.sub(r"[\t\r\n]+", " ", text).strip()
    if not original or len(original) > MAX_TEXT or _CONTROL.search(original):
        raise ScheduleError("text")
    if _VAGUE.search(original):
        return Parsed(original[:MAX_TITLE])
    local = _local(cfg, now)
    today = local.date()
    spans: List[Tuple[int, int]] = []
    day: Optional[date] = None

    days = [(m.start(), m.end(), today + timedelta(days=_DAY_OFFSET[m.group("w").lower()])) for m in _DAY.finditer(original)]
    for m in _ISO.finditer(original):
        try:
            days.append((m.start(), m.end(), date(int(m.group("y")), int(m.group("m")), int(m.group("d")))))
        except ValueError:
            days.append((m.start(), m.end(), None))  # an impossible date blocks guessing
    if len(days) == 1 and days[0][2] is not None and days[0][2] >= today:
        day = days[0][2]
        spans.append((days[0][0], days[0][1]))

    clock: Optional[str] = None
    times = _times(original)
    if len(times) == 1 and times[0][2]:
        clock = times[0][2]
        if day is None:
            ahead = clock > local.strftime("%H:%M")
            if len(days) == 0 and ahead:
                day = today
            else:
                clock = None  # no day was said and today's time has passed (or the day is unclear): do not guess
        if clock:
            spans.append((times[0][0], times[0][1]))

    title = original
    for start, end in sorted(spans, reverse=True):
        title = title[:start] + " " + title[end:]
    title = re.sub(r"\s+", " ", title).strip(_EDGE)
    if not title:
        return Parsed(original[:MAX_TITLE])
    return Parsed(title[:MAX_TITLE], day.isoformat() if day else None, clock if day else None)


# --- the private list -----------------------------------------------------------------------------------------------

_THREAD_LOCK = threading.Lock()


@contextmanager
def _locked() -> Iterator[None]:
    """One writer at a time across the bot and the panel (separate processes)."""
    root = private_dir(state_dir())
    with _THREAD_LOCK:
        fd = os.open(root / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def _path() -> Path:
    return state_dir() / ITEMS_FILE


def _clean(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    ident, title = raw.get("id"), raw.get("title")
    if not isinstance(ident, str) or not ID_RE.fullmatch(ident) or not isinstance(title, str) or not title.strip():
        return None
    when, clock = raw.get("date"), raw.get("time")
    try:
        if when is not None:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
                return None
            date.fromisoformat(when)
        if clock is not None and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
            return None
    except (TypeError, ValueError):
        return None
    created, done_at = raw.get("created"), raw.get("done_at")
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        return None
    return {"id": ident, "title": title[:MAX_TITLE], "date": when, "time": clock if when else None,
            "done": raw.get("done") is True, "created": int(created),
            "done_at": int(done_at) if isinstance(done_at, (int, float)) and not isinstance(done_at, bool) else None}


def _read() -> List[dict]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return []
    rows = data.get("items") if isinstance(data, dict) else None
    items = [c for c in (_clean(r) for r in (rows if isinstance(rows, list) else [])) if c]
    return items[:MAX_ITEMS * 2]


def _prune(items: List[dict], now: float) -> List[dict]:
    keep = now - DONE_KEEP_DAYS * 86400
    return [i for i in items if not (i["done"] and (i["done_at"] or i["created"]) < keep)]


def _write(items: List[dict]) -> None:
    write_private(_path(), json.dumps({"version": 1, "items": items}, ensure_ascii=False, separators=(",", ":")))


def items(now: Optional[float] = None) -> List[dict]:
    """The stored items (done ones older than DONE_KEEP_DAYS left out); nothing is written."""
    return _prune(_read(), now or time.time())


def add(cfg, text, now: Optional[datetime] = None) -> dict:
    """Store one item read from `text`. An identical open item is returned instead of a second copy."""
    parsed = parse(text, cfg, now)
    stamp = int((now or datetime.now(timezone.utc)).timestamp())
    try:
        with _locked():
            rows = _prune(_read(), stamp)
            for row in rows:
                if not row["done"] and (row["title"], row["date"], row["time"]) == (parsed.title, parsed.date, parsed.time):
                    return dict(row)
            while len(rows) >= MAX_ITEMS:
                done = [r for r in rows if r["done"]]
                if not done:
                    raise ScheduleError("full")
                rows.remove(min(done, key=lambda r: r["done_at"] or r["created"]))
            row = {"id": secrets.token_hex(6), "title": parsed.title, "date": parsed.date, "time": parsed.time,
                   "done": False, "created": stamp, "done_at": None}
            rows.append(row)
            _write(rows)
            return dict(row)
    except OSError:
        raise ScheduleError("unavailable") from None


def set_done(ident, done, now: Optional[datetime] = None) -> dict:
    if not isinstance(ident, str) or not ID_RE.fullmatch(ident) or not isinstance(done, bool):
        raise ScheduleError("text")
    stamp = int((now or datetime.now(timezone.utc)).timestamp())
    try:
        with _locked():
            rows = _prune(_read(), stamp)
            for row in rows:
                if row["id"] == ident:
                    row["done"], row["done_at"] = done, stamp if done else None
                    _write(rows)
                    return dict(row)
    except OSError:
        raise ScheduleError("unavailable") from None
    raise ScheduleError("unknown")


def remove(ident) -> None:
    if not isinstance(ident, str) or not ID_RE.fullmatch(ident):
        raise ScheduleError("text")
    try:
        with _locked():
            rows = _read()
            kept = [r for r in rows if r["id"] != ident]
            if len(kept) == len(rows):
                raise ScheduleError("unknown")
            _write(_prune(kept, time.time()))
    except OSError:
        raise ScheduleError("unavailable") from None


# --- read-only calendar sources -------------------------------------------------------------------------------------

@dataclass
class Event:
    title: str
    day: str                   # YYYY-MM-DD in the owner's zone
    time: Optional[str]        # HH:MM, None for an all-day event


class ScheduleSource:
    """A read-only place events come from. `events` returns the events of the days first..last (inclusive) and raises
    on failure; a source never writes anywhere and never reveals its location."""
    kind = ""

    def events(self, cfg, first: date, last: date) -> List[Event]:
        raise NotImplementedError


ICS_MAX_BYTES = 2_000_000
ICS_TTL = 300        # seconds a fetched calendar is reused
ICS_RETRY = 60       # seconds before a failed fetch is tried again
ICS_TIMEOUT = 8
ICS_STALE = 86400    # an old copy is still shown for this long when the source cannot be reached
MAX_EVENTS = 100
_CACHE: Dict[str, Tuple[float, Optional[str], float]] = {}  # fetched at, text, do not retry before
_CACHE_LOCK = threading.Lock()


def valid_source(value) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2000 or _CONTROL.search(value) or value != value.strip():
        return False
    if value.startswith("https://"):
        return len(value) > len("https://x")
    return value.startswith("/") and value.lower().endswith(".ics") and "\x00" not in value


def valid_sources(value) -> bool:
    return isinstance(value, list) and len(value) <= 5 and all(valid_source(v) for v in value)


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise urllib.error.URLError("redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(location: str) -> str:
    if location.startswith("https://"):
        request = urllib.request.Request(location, headers={"User-Agent": "ctb-schedule", "Accept": "text/calendar"})
        with urllib.request.build_opener(_HttpsOnly).open(request, timeout=ICS_TIMEOUT) as reply:
            raw = reply.read(ICS_MAX_BYTES + 1)
    else:
        with open(location, "rb") as handle:
            raw = handle.read(ICS_MAX_BYTES + 1)
    if len(raw) > ICS_MAX_BYTES:
        raise ValueError("too large")
    return raw.decode("utf-8", "replace")


def _ics_text(location: str, fetch: Callable[[str], str], now: float) -> str:
    key = hashlib.sha256(location.encode("utf-8")).hexdigest()[:16]
    with _CACHE_LOCK:
        fetched, text, retry_at = _CACHE.get(key, (0.0, None, 0.0))
    usable = text is not None and now - fetched <= ICS_STALE
    if text is not None and now - fetched < ICS_TTL:
        return text
    if now < retry_at:  # a fetch failed a moment ago: the old copy (if still allowed) or nothing, without another try
        if usable:
            return text
        raise OSError("recent failure")
    try:
        fresh = fetch(location)
    except Exception as exc:
        log.warning("schedule source unavailable (%s)", type(exc).__name__)  # never the location
        with _CACHE_LOCK:
            _CACHE[key] = (fetched, text if usable else None, now + ICS_RETRY)  # the copy keeps its own age
        if usable:
            return text
        raise OSError("unavailable") from None
    with _CACHE_LOCK:
        _CACHE[key] = (now, fresh, 0.0)
    return fresh


def _unfold(text: str) -> List[str]:
    out: List[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _split(line: str) -> Optional[Tuple[str, Dict[str, str], str]]:
    quoted, colon = False, -1
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            colon = i
            break
    if colon < 1:
        return None
    head, value = line[:colon], line[colon + 1:]
    name, *rest = head.split(";")
    params = {}
    for part in rest:
        k, _, v = part.partition("=")
        params[k.upper()] = v.strip('"')
    return name.upper(), params, value


def _text_value(value: str) -> str:
    value = value.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    return re.sub(r"\s+", " ", _CONTROL.sub("", value)).strip()[:MAX_TITLE]


MAX_SPAN_DAYS = 366      # an all-day event longer than this is skipped
MAX_WORK = 20_000        # loop steps one event may spend on its repeat rule; an event that needs more is skipped
MAX_TOTAL_WORK = 400_000  # and all events of one parse together; after that the remaining events are skipped


class _Budget(Exception):
    pass


def _when(value: str, params: Dict[str, str], tz: tzinfo):
    """A date (all day) or an aware datetime in the zone the calendar gave it (UTC, its TZID, or `tz` when floating).
    ValueError when it cannot be read with certainty."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d").date()
    if not re.fullmatch(r"\d{8}T\d{6}Z?", value):
        raise ValueError("format")
    naive = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return naive.replace(tzinfo=timezone.utc)
    if params.get("TZID"):
        try:
            return naive.replace(tzinfo=ZoneInfo(params["TZID"]))
        except Exception:
            raise ValueError("zone") from None
    return naive.replace(tzinfo=tz)


def _day_of(value) -> date:
    return value if type(value) is date else value.date()


_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _rule(text: str, tz: tzinfo):
    """A daily or weekly rule as a dict, or None when it is anything else (then only the first occurrence counts).
    `tz` is the zone of the event's start: the rule is expanded there, on its own wall-clock days."""
    parts = dict(p.partition("=")[::2] for p in text.upper().split(";") if "=" in p)
    if parts.get("FREQ") not in ("DAILY", "WEEKLY") or set(parts) - {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"}:
        return None
    try:
        interval = int(parts.get("INTERVAL", "1"))
        count = int(parts["COUNT"]) if "COUNT" in parts else None
        until = None
        if "UNTIL" in parts:
            u = _when(parts["UNTIL"], {}, tz)
            until = u if type(u) is date else u.astimezone(tz).date()
        byday = [_WEEKDAYS[d] for d in parts["BYDAY"].split(",")] if "BYDAY" in parts else None
    except (ValueError, KeyError):
        return None
    if interval < 1 or (count is not None and count < 1) or (byday is not None and parts["FREQ"] != "WEEKLY"):
        return None
    return {"freq": parts["FREQ"], "interval": interval, "count": count, "until": until, "byday": byday}


def _occurs(rule, start: date, day: date, budget: List[int]) -> bool:
    """Does the rule produce an occurrence that starts on `day` (a day in the event's own zone)? COUNT is counted
    exactly, however far back the start is; the work is charged to `budget`."""
    if day < start or (rule["until"] and day > rule["until"]):
        return False
    interval = rule["interval"]
    if rule["freq"] == "DAILY":
        delta = (day - start).days
        return delta % interval == 0 and (rule["count"] is None or delta // interval < rule["count"])
    byday = sorted(set(rule["byday"] or [start.weekday()]))
    monday = start - timedelta(days=start.weekday())
    week = ((day - timedelta(days=day.weekday())) - monday).days // 7
    if day.weekday() not in byday or week % interval:
        return False
    if rule["count"] is None:
        return True
    seen = 0
    for w in range(0, week + 1, interval):
        budget[0] -= 1
        if budget[0] < 0:
            raise _Budget()
        base = monday + timedelta(days=7 * w)
        seen += sum(1 for wd in byday if start <= base + timedelta(days=wd) <= day)
    return seen <= rule["count"]


def _blocks(text: str) -> List[List[Tuple[str, Dict[str, str], str]]]:
    blocks: List[list] = []
    current: Optional[list] = None
    alarm = 0
    for line in _unfold(text)[:50000]:
        split = _split(line)
        if not split:
            continue
        name, params, value = split
        if name == "BEGIN" and value.upper() == "VEVENT":
            current, alarm = [], 0
        elif name == "END" and value.upper() == "VEVENT":
            if current is not None:
                blocks.append(current)
            current = None
            if len(blocks) >= 5000:
                break
        elif current is not None:
            if name == "BEGIN" and value.upper() == "VALARM":
                alarm += 1
            elif name == "END" and value.upper() == "VALARM":
                alarm = max(0, alarm - 1)
            elif not alarm:
                current.append((name, params, value))
    return blocks


def parse_ics(text: str, cfg, first: date, last: date) -> List[Event]:
    """The events of the days first..last in the owner's zone. A malformed event is skipped; garbage gives [].
    Repeats are expanded on the event's own zone (so a daylight-saving change in that zone keeps the wall-clock
    time) and each occurrence is converted to the owner's zone."""
    tz = zone(cfg)
    total = MAX_TOTAL_WORK
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    parsed = []
    moved_days: Dict[str, set] = {}
    moved_times: Dict[str, set] = {}
    for props in _blocks(text):
        try:
            one: dict = {}
            for name, params, value in props:
                if name == "EXDATE":
                    one.setdefault("exdate", []).extend((v, params) for v in value.split(",") if v)
                else:
                    one.setdefault(name, (value, params))
            uid = one.get("UID", ("",))[0]
            if "RECURRENCE-ID" in one:  # recorded first: a cancelled occurrence still removes the original
                original = _when(*one["RECURRENCE-ID"], tz)
                (moved_days if type(original) is date else moved_times).setdefault(uid, set()).add(original)
            if one.get("STATUS", ("",))[0].upper() == "CANCELLED":
                continue
            start = _when(*one["DTSTART"], tz)
            end = _when(*one["DTEND"], tz) if "DTEND" in one else None
            parsed.append((one, start, end))
        except (KeyError, ValueError):
            continue
    out: List[Event] = []
    for one, start, end in parsed:
        if total <= 0:
            break
        budget = [min(MAX_WORK, total)]
        try:
            out.extend(_expand(one, start, end, tz, days, budget, moved_days, moved_times))
        except _Budget:
            pass
        total -= max(0, min(MAX_WORK, total) - budget[0])
    return out[:MAX_EVENTS * 3]


def _expand(one, start, end, tz, days, budget, moved_days, moved_times) -> List[Event]:
    title = _text_value(one.get("SUMMARY", ("",))[0]) or "\u2014"
    allday = type(start) is date
    uid = one.get("UID", ("",))[0]
    ev_tz = tz if allday else start.tzinfo
    first_day = start if allday else start.replace(tzinfo=None).date()
    span = 1
    if allday and type(end) is date:
        span = max(1, (end - start).days)
    if span > MAX_SPAN_DAYS:
        return []
    skip_days, skip_times = set(), set()
    for value, params in one.get("exdate", []):
        try:
            x = _when(value, params, tz)
        except ValueError:
            continue
        (skip_days if type(x) is date else skip_times).add(x)
    if "RECURRENCE-ID" not in one:
        skip_days |= moved_days.get(uid, set())
        skip_times |= moved_times.get(uid, set())
    rule = _rule(one["RRULE"][0], ev_tz) if "RRULE" in one and "RECURRENCE-ID" not in one else None
    wall = None if allday else start.replace(tzinfo=None).time()

    def hit(origin: date) -> bool:
        budget[0] -= 1
        if budget[0] < 0:
            raise _Budget()
        if origin in skip_days:
            return False
        return _occurs(rule, first_day, origin, budget) if rule else origin == first_day

    found: List[Event] = []
    for d in days:
        if allday:
            if any(hit(d - timedelta(days=offset)) for offset in range(span)):
                found.append(Event(title, d.isoformat(), None))
            continue
        for cand in (d - timedelta(days=1), d, d + timedelta(days=1)):  # the event's day can differ from ours
            if not hit(cand):
                continue
            moment = datetime.combine(cand, wall, tzinfo=ev_tz)
            if moment in skip_times:
                continue
            local = moment.astimezone(tz)
            if local.date() == d:
                found.append(Event(title, d.isoformat(), local.strftime("%H:%M")))
                break
    return found


class IcsSource(ScheduleSource):
    """A private .ics feed (an https:// link) or a local .ics file the owner configured."""
    kind = "ics"

    def __init__(self, location: str, fetch: Optional[Callable[[str], str]] = None):
        self._location = location
        self._fetch = fetch or _fetch

    def events(self, cfg, first: date, last: date, now: Optional[float] = None) -> List[Event]:
        return parse_ics(_ics_text(self._location, self._fetch, now or time.time()), cfg, first, last)


def sources(cfg) -> List[ScheduleSource]:
    """The configured sources (none by default): every ICS link or file, then the owner's Google calendars when the
    `google_calendar` section is set and an account is connected (google_calendar.py)."""
    wanted = getattr(cfg, "schedule_sources", None)
    found: List[ScheduleSource] = [IcsSource(v) for v in wanted] if valid_sources(wanted) else []
    if getattr(cfg, "google_calendar", None):
        from . import google_calendar
        google = google_calendar.source(cfg)
        if google is not None:
            found.append(google)
    return found


def fetch_budget(source_list: List[ScheduleSource]) -> float:
    """Seconds a first fetch of every source may take together."""
    return sum(getattr(s, "budget", ICS_TIMEOUT) for s in source_list)


# --- what the Cockpit card shows ------------------------------------------------------------------------------------

def view(cfg, now: Optional[datetime] = None, source_list: Optional[List[ScheduleSource]] = None) -> dict:
    """Today and Tomorrow for the card: own items and calendar events together, timed first, then undated.
    Own items without a date belong to Today; an open item from an earlier day stays on Today, marked late."""
    local = _local(cfg, now)
    today, tomorrow = local.date(), local.date() + timedelta(days=1)
    t0, t1 = today.isoformat(), tomorrow.isoformat()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows: Dict[str, List[Tuple[Tuple, dict]]] = {"today": [], "tomorrow": []}
    for it in items(local.timestamp()):
        when = it["date"]
        if when == t1:
            bucket = "tomorrow"
        elif when == t0 or when is None or (when < t0 and not it["done"]):
            if when is None and it["done"] and (it["done_at"] or 0) < midnight:
                continue
            bucket = "today"
        else:
            continue
        late = bool(when and when < t0)
        group = 1 if late else 2 if it["time"] else 3
        rows[bucket].append(((group, when if late else "", it["time"] or "", it["created"]),
                             {"id": it["id"], "title": it["title"], "time": it["time"], "done": it["done"],
                              "kind": "own", "late": late, "allday": False}))
    configured = sources(cfg) if source_list is None else source_list
    calendar_ok = True
    for source in configured:
        try:
            found = source.events(cfg, today, tomorrow)
        except Exception as exc:
            log.warning("schedule source failed (%s)", type(exc).__name__)
            calendar_ok = False
            continue
        for event in found[:MAX_EVENTS]:
            bucket = "today" if event.day == t0 else "tomorrow" if event.day == t1 else None
            if bucket:
                rows[bucket].append(((0 if event.time is None else 2, "", event.time or "", 0),
                                     {"id": "", "title": event.title, "time": event.time, "done": False,
                                      "kind": "cal", "late": False, "allday": event.time is None}))
    days = {k: [r for _, r in sorted(v, key=lambda p: p[0])] for k, v in rows.items()}
    return {"today": t0, "tomorrow": t1, "days": days,
            "open": {k: sum(1 for r in v if r["kind"] == "own" and not r["done"]) for k, v in days.items()},
            "calendar": {"configured": bool(configured), "ok": calendar_ok}, "google": _google_status(cfg)}


def _google_status(cfg) -> dict:
    """The "Connect with Google" button's state; no Keychain is asked when the section is empty."""
    if not getattr(cfg, "google_calendar", None):
        return {"configured": False, "connected": False}
    from . import google_calendar
    try:
        return google_calendar.status(cfg)
    except Exception:
        return {"configured": False, "connected": False}


# --- the bot's /todo ------------------------------------------------------------------------------------------------

def telegram_lines(cfg, row: dict, lang: str, now: Optional[datetime] = None) -> List[str]:
    from .i18n import t
    lines = [t("todo_added", lang=lang), row["title"]]
    if row["date"]:
        local = _local(cfg, now).date()
        day = {0: "todo_today", 1: "todo_tomorrow"}.get((date.fromisoformat(row["date"]) - local).days)
        word = t(day, lang=lang) if day else row["date"]
        lines.append(word + (" " + row["time"] if row["time"] else ""))
    return lines


def telegram_command(cfg, arg: str, reply: Callable[[str], None]) -> str:
    """`/todo <text>` from the owner: stores one item and answers with what was understood."""
    from .i18n import t
    lang = getattr(cfg, "ui_language", "") or "he"
    if not arg.strip():
        reply(t("todo_usage", lang=lang))
        return "todo"
    try:
        row = add(cfg, arg)
    except ScheduleError as exc:
        reply(t("todo_full" if exc.code == "full" else "todo_bad" if exc.code == "text" else "todo_error", lang=lang))
        return "todo"
    reply("\n".join(telegram_lines(cfg, row, lang)))
    return "todo"
