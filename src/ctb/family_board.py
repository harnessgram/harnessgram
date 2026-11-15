"""The family home board ("הלוח המשפחתי שלנו"): the owner's weekly chores, daily routine, shopping list and meal plan.

One private file, `family_board.json` in the bridge state directory (0600, written atomically under a lock shared by the
bot and the panel, every list capped). A weekly chore belongs to a weekday (Sunday = 0 ... Saturday = 6; the Saturday
column is "motzaei Shabbat", the night after Shabbat). A checkmark stores the period it was made in: the day for a daily
task, the week (it starts on Sunday) for a weekly chore. A mark from an earlier day or week simply no longer matches, so
the checks reset by themselves; nothing runs at midnight. Shabbat runs from Friday's candle-lighting to Saturday's
nightfall (shabbat_times.py): inside it the board shows no chores to do and refuses chore and routine checkmarks; the
Saturday column ("motzaei Shabbat") can be ticked after nightfall, in the same week. Nothing here talks to the network.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

from .config import private_dir, state_dir, write_private
from . import shabbat_times
from .schedule import _local

FILE = "family_board.json"
LOCK_FILE = "family_board.lock"
MAX_TEXT = 100
LISTS = ("weekly", "daily", "shop", "meals")
CAPS = {"weekly": 90, "daily": 30, "shop": 80, "meals": 40}
ID_RE = re.compile(r"[0-9a-f]{12}")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f‎‏‪-‮⁦-⁩]")
ALWAYS = "-"  # the mark on a shopping item: it never resets, only "clear checked" removes it


class BoardError(Exception):
    """code: "text" (unusable line or body), "full" (list at its cap), "unknown" (no such item), "shabbat" (a chore
    mark on Shabbat), "unavailable" (disk)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --- the starting lists (Hebrew, all editable) ------------------------------------------------------------------------

DEFAULT_WEEKLY = {
    0: ["לימודים בערב", "כביסות (ילדים/בגדים)", "הכנת סל קניות לשבוע"],
    1: ["ניקיון יסודי לפי אזורים (סלון, מטבח, חדרים, שירותים ואמבטיה, פינת אוכל, אבק וניגוב)"],
    2: ["בישולים לארוחות הקבועות (הכנת מנות עיקריות, חלוקה לקופסאות, מילוי מקפיא, סידור במקרר)"],
    3: ["כביסות (בגדי ילדים, בגדי הורים, כביסה נוספת אם צריך)"],
    4: ["הכנות לשבת (בישולים לשבת, קניות השלמה לשבת, סידור הבית, הכנת תבשילים)"],
    5: ["החלפת מצעים ושטיפה בכל הבית (ניקיון כללי, הכנות אחרונות לשבת)"],
    6: ["כביסה קלה מהשבת, מיון ותלייה, סידור בגדים"],
}
DEFAULT_DAILY = ["ארגון בגדים למחר", "הכנת ארוחת בוקר להורים לעבודה", "החזרת משחקים למקום",
                 "שאיבה/שטיפה קלה בפינת האוכל", "כלים"]
DEFAULT_SHOP = ["ירקות ופירות", "בשר/עוף/דגים", "מוצרי חלב", "מוצרי בסיס", "חטיפים לילדים", "מוצרי ניקיון וצריכה"]


def _seed_id(kind: str, day, text: str) -> str:
    return hashlib.sha256(("%s|%s|%s" % (kind, day, text)).encode("utf-8")).hexdigest()[:12]


def _item(kind: str, text: str, day: Optional[int] = None, done: Optional[str] = None, ident: Optional[str] = None) -> dict:
    return {"id": ident or secrets.token_hex(6), "text": text, "day": day, "done": done}


def defaults() -> Dict[str, List[dict]]:
    """The seed lists. Their ids come from the text, so a board read before the first save names items the same way
    after it."""
    out: Dict[str, List[dict]] = {k: [] for k in LISTS}
    for day, texts in DEFAULT_WEEKLY.items():
        out["weekly"] += [_item("weekly", t, day, None, _seed_id("weekly", day, t)) for t in texts]
    out["daily"] = [_item("daily", t, None, None, _seed_id("daily", "", t)) for t in DEFAULT_DAILY]
    out["shop"] = [_item("shop", t, None, None, _seed_id("shop", "", t)) for t in DEFAULT_SHOP]
    return out


# --- the clock --------------------------------------------------------------------------------------------------------

def sunday_index(day: date) -> int:
    """Sunday = 0 ... Saturday = 6."""
    return (day.weekday() + 1) % 7


def week_start(day: date) -> date:
    return day - timedelta(days=sunday_index(day))


def _instant(now: Optional[datetime]) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def shabbat_span(cfg, now: Optional[datetime] = None):
    """(candle-lighting, nightfall) as UTC instants while Shabbat holds `now`, else None."""
    return shabbat_times.current(cfg, _local(cfg, now).date(), _instant(now))


def is_shabbat(cfg, now: Optional[datetime] = None) -> bool:
    return shabbat_span(cfg, now) is not None


def _period(kind: str, today: date) -> str:
    return week_start(today).isoformat() if kind == "weekly" else today.isoformat() if kind == "daily" else ALWAYS


# --- the private file -------------------------------------------------------------------------------------------------

_THREAD_LOCK = threading.Lock()


@contextmanager
def _locked() -> Iterator[None]:
    root = private_dir(state_dir())
    with _THREAD_LOCK:
        fd = os.open(root / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def _path() -> Path:
    return state_dir() / FILE


def _clean(kind: str, raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    ident, text = raw.get("id"), raw.get("text")
    if not isinstance(ident, str) or not ID_RE.fullmatch(ident) or not isinstance(text, str) or not text.strip():
        return None
    day = raw.get("day")
    if kind == "weekly":
        if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6:
            return None
    elif kind == "meals":
        if day is not None and (isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6):
            return None
    else:
        day = None
    done = raw.get("done")
    if done is not None and (not isinstance(done, str) or len(done) > 10):
        done = None
    return {"id": ident, "text": text.strip()[:MAX_TEXT], "day": day, "done": done}


def _read() -> Dict[str, List[dict]]:
    """The stored lists; the starting lists while there is no readable file."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults()
    lists = data.get("lists") if isinstance(data, dict) else None
    if not isinstance(lists, dict):
        return defaults()
    out: Dict[str, List[dict]] = {}
    for kind in LISTS:
        rows = lists.get(kind)
        seen = set()
        out[kind] = []
        for raw in rows if isinstance(rows, list) else []:
            c = _clean(kind, raw)
            if c and c["id"] not in seen and len(out[kind]) < CAPS[kind]:
                seen.add(c["id"])
                out[kind].append(c)
    return out


def _write(lists: Dict[str, List[dict]]) -> None:
    write_private(_path(), json.dumps({"version": 1, "lists": lists}, ensure_ascii=False, separators=(",", ":")))


def _text(value) -> str:
    if not isinstance(value, str):
        raise BoardError("text")
    text = " ".join(_CONTROL.sub(" ", value).split())
    if not text or len(text) > MAX_TEXT:
        raise BoardError("text")
    return text


def _day(kind: str, value) -> Optional[int]:
    if kind not in ("weekly", "meals"):
        if value is not None:
            raise BoardError("text")
        return None
    if value is None and kind == "meals":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6:
        raise BoardError("text")
    return value


def _find(lists: Dict[str, List[dict]], ident) -> tuple:
    if not isinstance(ident, str) or not ID_RE.fullmatch(ident):
        raise BoardError("text")
    for kind in LISTS:
        for row in lists[kind]:
            if row["id"] == ident:
                return kind, row
    raise BoardError("unknown")


def _change(work: Callable[[Dict[str, List[dict]]], Optional[dict]]) -> Optional[dict]:
    try:
        with _locked():
            lists = _read()
            out = work(lists)
            _write(lists)
            return out
    except OSError:
        raise BoardError("unavailable") from None


# --- changes ----------------------------------------------------------------------------------------------------------

def add(kind, text, day=None) -> dict:
    """Store one item. An identical item (same list, weekday and text) is returned instead of a second copy."""
    if kind not in LISTS:
        raise BoardError("text")
    line, when = _text(text), _day(kind, day)

    def work(lists):
        for row in lists[kind]:
            if row["text"] == line and row["day"] == when:
                return dict(row, list=kind)
        if len(lists[kind]) >= CAPS[kind]:
            raise BoardError("full")
        row = _item(kind, line, when)
        lists[kind].append(row)
        return dict(row, list=kind)
    return _change(work)


_KEEP = object()


def edit(ident, text=None, day=_KEEP) -> dict:
    """Rename an item and/or move it to another weekday (`day` is left alone unless given)."""
    line = _text(text) if text is not None else None

    def work(lists):
        kind, row = _find(lists, ident)
        if line is not None:
            row["text"] = line
        if day is not _KEEP:
            row["day"] = _day(kind, day)
        return dict(row, list=kind)
    return _change(work)


def remove(ident) -> dict:
    def work(lists):
        kind, row = _find(lists, ident)
        lists[kind].remove(row)
        return dict(row, list=kind)
    return _change(work)


def check(cfg, ident, done, now: Optional[datetime] = None) -> dict:
    """Mark an item done (or undo it) for the current day or week. Chores and the daily routine are refused on Shabbat."""
    if not isinstance(done, bool):
        raise BoardError("text")
    today = _local(cfg, now).date()
    shabbat = is_shabbat(cfg, now)

    def work(lists):
        kind, row = _find(lists, ident)
        if shabbat and kind in ("weekly", "daily"):
            raise BoardError("shabbat")
        row["done"] = _period(kind, today) if done else None
        return dict(row, list=kind)
    return _change(work)


def clear_checked() -> int:
    """Remove the checked shopping items; the number removed."""
    def work(lists):
        before = len(lists["shop"])
        lists["shop"] = [r for r in lists["shop"] if not r["done"]]
        return {"n": before - len(lists["shop"])}
    return _change(work)["n"]


# --- what the Cockpit shows -------------------------------------------------------------------------------------------

def _shown(kind: str, row: dict, today: date) -> dict:
    return {"id": row["id"], "text": row["text"], "day": row["day"], "done": row["done"] == _period(kind, today)}


def view(cfg, now: Optional[datetime] = None) -> dict:
    """Today, the whole week (Sunday first), the daily routine, the shopping list and the meal plan. A mark made in an
    earlier day or week shows as not done. On Shabbat `shabbat` is true and no chore or routine task is offered."""
    today = _local(cfg, now).date()
    index = sunday_index(today)
    start = week_start(today)
    lists = _read()
    span = shabbat_span(cfg, now)
    shabbat = span is not None
    days = []
    for d in range(7):
        days.append({"day": d, "date": (start + timedelta(days=d)).isoformat(),
                     "chores": [_shown("weekly", r, today) for r in lists["weekly"] if r["day"] == d]})
    daily = [_shown("daily", r, today) for r in lists["daily"]]
    todays = [] if shabbat else days[index]["chores"]
    until = span[1].astimezone(_local(cfg, now).tzinfo).strftime("%H:%M") if span else None
    return {"today": today.isoformat(), "week_start": start.isoformat(), "day": index, "shabbat": shabbat, "shabbat_until": until,
            "days": days, "daily": daily,
            "shop": [_shown("shop", r, today) for r in lists["shop"]],
            "meals": [_shown("meals", r, today) for r in lists["meals"]],
            "open": {"chores": sum(1 for r in todays if not r["done"]),
                     "daily": 0 if shabbat else sum(1 for r in daily if not r["done"])},
            "caps": dict(CAPS, text=MAX_TEXT)}


# --- the bot's /shop --------------------------------------------------------------------------------------------------

def shop_command(cfg, arg: str, reply: Callable[[str], None]) -> str:
    """`/shop <item>` from the owner: adds one line to the shopping list."""
    from .i18n import t
    lang = getattr(cfg, "ui_language", "") or "he"
    if not arg.strip():
        reply(t("shop_usage", lang=lang))
        return "shop"
    try:
        row = add("shop", arg)
    except BoardError as exc:
        reply(t("shop_full" if exc.code == "full" else "shop_bad" if exc.code == "text" else "shop_error", lang=lang))
        return "shop"
    reply("\n".join([t("shop_added", lang=lang), row["text"]]))
    return "shop"
