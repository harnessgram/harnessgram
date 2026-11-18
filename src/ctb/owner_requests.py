"""Read-only owner ledger checklist. No commands, writes, or network calls on a state poll.

The ledger is external to bridge state. Each regular JSON file is read afresh, with a hard
byte bound (including a file growing during the read); bad files/items are simply skipped.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Optional

from .creds import redact_channel
from .work_lines import clean_text, project_name

FILE_BYTES = 1024 * 1024
MAX_ITEMS = 1000
MAX_NOTES = 50
TEXT_CHARS = 1500
DONE_DAYS = 7 * 86400
DROPPED_NOTE = "dropped by owner"

_DISPLAY_PREFIX = re.compile(
    r"^(?:Owner|Team|אונר|הבעלים|בעלים|צוות)\s+\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\s*"
    r"(?:~?\d{1,2}:\d{2})?\s*(?:\([^)]*\))?\s*:\s*",
    re.IGNORECASE,
)
_DISPLAY_PATH = re.compile(r"`[^`]*(?:~\/|/Users/)[^`]*`|(?:~\/|/Users/)\S+")
_DISPLAY_TICKET = re.compile(r"(?<!\w)#\d+\b")
_LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9._'-]*(?:\s+[A-Za-z][A-Za-z0-9._'-]*)*")


def display_preview(value: str) -> str:
    """Return a phone-safe request preview without changing its persisted text."""
    text = _DISPLAY_PREFIX.sub("", value or "")
    text = _DISPLAY_PATH.sub(" ", text)
    text = _DISPLAY_TICKET.sub(" ", text)

    def latin(match):
        run = match.group(0)
        return "\u2068" + run + "\u2069" if len(run.split()) <= 3 else " "

    cleaned = " ".join(text.split()).strip(" -–—:;,·")
    if not re.search(r"[\u0590-\u05FF]", cleaned):
        return cleaned  # English text stays whole; only Hebrew lines need their Latin runs isolated
    return " ".join(_LATIN_RUN.sub(latin, cleaned).split()).strip(" -–—:;,·") or cleaned


def dormant_seconds(cfg):
    value = (getattr(cfg, "brief", None) or {}).get("request_dormant_days", 7)
    return float(value) * 86400 if type(value) in (int, float) and math.isfinite(value) and 0 < value <= 3650 else DONE_DAYS


def ledger_dir(cfg):
    """Reuse the existing private ledger configuration; no owner machine paths in the package."""
    folder = ((getattr(cfg, "brief", None) or {}).get("ledger_dir")
              or (getattr(cfg, "radio", None) or {}).get("ledger_dir"))
    return Path(os.path.expanduser(folder)) if isinstance(folder, str) and folder.strip() else None


def _time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) and value > 0 else None
    except OverflowError:
        return None


def _text(value) -> str:
    return " ".join(redact_channel(clean_text(value)).split())[:TEXT_CHARS] if isinstance(value, str) else ""


def _read(path):
    # Do not follow symlinks or block on a FIFO masquerading as a ledger.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > FILE_BYTES:
            return None
        raw = stream.read(FILE_BYTES + 1)
    return json.loads(raw) if len(raw) <= FILE_BYTES else None


def read(now: float, folder: Path = None, *, history=False, dormant_after=DONE_DAYS) -> list:
    """Flat rows, grouped by project, owner first, team second, recent completions last.

    Both the installed ledger's string notes and {at,text} notes are accepted. Done's
    `updated` is the ledger's completion time. No cache of the source: the next poll sees adds.
    `history` retains older completions and all notes for topic cards, including supplied note dates.
    """
    if folder is None:
        return []
    try:
        paths = sorted(folder.glob("*.json"))
    except OSError:
        return []
    rows = []
    for path in paths:
        project = project_name(path.stem)
        if not project:
            continue
        try:
            data = _read(path)
        except (OSError, ValueError, RecursionError):
            continue
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            continue
        seen = set()
        for item in items[:MAX_ITEMS]:
            if not isinstance(item, dict):
                continue
            ident, title = item.get("id"), _text(item.get("text"))
            created, updated = _time(item.get("created")), _time(item.get("updated"))
            status = item.get("status")
            if (type(ident) is not int or ident < 1 or ident in seen or not title
                    or status not in ("open", "done") or created is None or updated is None):
                continue
            seen.add(ident)
            if not history and status == "done" and not 0 <= now - updated <= DONE_DAYS:
                continue
            notes = item.get("notes")
            steps = []
            notes = notes if isinstance(notes, list) else []
            for note in notes[-MAX_NOTES:]:  # the same window as always, sliced before any cleaning
                text = _text(note.get("text") if isinstance(note, dict) else note)
                if text:
                    steps.append({"text": text})
                    if history and isinstance(note, dict) and _time(note.get("at")) is not None:
                        steps[-1]["at"] = _time(note["at"])
            rows.append({"project": _text(project), "id": ident, "title": title, "status": status,
                         "wait": "owner" if item.get("wait") == "owner" else "team",
                         "age": max(0, int(now - created)), "updated": updated,
                         "older": status == "open" and item.get("wait") == "owner" and now - updated >= dormant_after,
                         "urgent": bool(steps and steps[-1]["text"] == "pushed by owner"),
                         "note": steps[-1]["text"] if steps else "", "steps": steps})
            topic = item.get("topic")
            if isinstance(topic, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", topic):
                rows[-1]["topic"] = topic
            # request-ledger records the creating hook's CLAUDE_CODE_SESSION_ID.  Keep the
            # opaque value only for joining the item to that session's Telegram topic.
            session = item.get("session")
            if isinstance(session, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session):
                rows[-1]["session"] = session
            if history:
                rows[-1]["created"] = created
                # Keep only explicit, validated completion links. Bare #numbers
                # in prose cannot identify a repository or a durable stage.
                from .request_sweep import item_urls
                links = item_urls(item)
                if links:
                    rows[-1]["links"] = list(links)
    return sorted(rows, key=lambda r: (r["project"].casefold(), r["status"] == "done",
                                      r["wait"] != "owner", -r["updated"], r["id"]))


def latest_notes(folder: Path = None) -> dict:
    """{(project, id): the item's latest note, whole}: for checks on the full text (a "parked:" marker past the
    display cut). Same bounded, no-symlink reader as `read`; never shown, so it is not cleaned for display."""
    out = {}
    if folder is None:
        return out
    try:
        paths = sorted(folder.glob("*.json"))
    except OSError:
        return out
    for path in paths:
        project = _text(project_name(path.stem))
        if not project:
            continue
        try:
            data = _read(path)
        except (OSError, ValueError, RecursionError):
            continue
        items = data.get("items") if isinstance(data, dict) else None
        for item in (items if isinstance(items, list) else [])[:MAX_ITEMS]:
            if not isinstance(item, dict) or type(item.get("id")) is not int:
                continue
            notes = item.get("notes")
            texts = [n.get("text") if isinstance(n, dict) else n for n in (notes if isinstance(notes, list) else [])]
            texts = [t for t in texts if isinstance(t, str) and t.strip()]
            if texts:
                out[(project, item["id"])] = texts[-1][:FILE_BYTES]
    return out


def translate(rows, tr, waiting):
    """Use the reply translator's nonblocking cache/queue and its privacy policy."""
    if tr is None:
        return
    for row in rows:
        title = tr.he_item(row["project"], row["id"], row["updated"], row["title"])
        if title is None:
            waiting.append("request")
        row["title_he"] = title or None
    # Prioritize every checklist title before filling the bounded translator queue
    # with long note histories, so a new request is not starved by another item's steps.
    for row in rows:
        for part in [row] + row["steps"]:
            field = "note" if part is row else "text"
            value = tr.he(part[field])
            if value is None:
                waiting.append("request_note")
            part[field + "_he"] = value or None


# --- the list payload -------------------------------------------------------------------------------------
# The Cockpit list shows a compact row per item. State carries only what that row and its grouping need;
# the full text, note and steps come from `detail` when the owner expands a row.
TITLE_SHOWN = 160
LONG_WAIT = 7 * 86400  # an open item nobody touched for this long folds into "Waiting a long time"


def clip(text, limit: int = TITLE_SHOWN) -> str:
    """At most `limit` characters, ending in an ellipsis when cut."""
    if not isinstance(text, str) or len(text) <= limit:
        return text if isinstance(text, str) else ""
    return text[:limit - 1].rstrip() + "…"


_LINK_SECRET: Optional[bytes] = None


def _link_secret() -> bytes:
    """A random key kept only in this installation's state folder: a link key cannot be traced back to a project
    name without it."""
    global _LINK_SECRET
    if _LINK_SECRET is None:
        from .config import private_dir, state_dir
        path = private_dir(state_dir()) / "request-links.key"
        if not path.exists():
            # Written in full under a private temporary name, then linked into place: a second process sees either
            # no key or the whole key, and an existing key is never replaced.
            tmp = path.with_name(f".request-links.{os.getpid()}.{os.urandom(4).hex()}")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(os.urandom(32).hex())
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    pass
            finally:
                tmp.unlink(missing_ok=True)
        text = path.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", text):
            raise ValueError("request link key is malformed")
        _LINK_SECRET = bytes.fromhex(text)
    return _LINK_SECRET


def item_key(project: str, ident: int) -> str:
    """Opaque stable key used by a Main App deep link, keyed with the installation's own secret."""
    raw = f"{project}\0{ident}".encode("utf-8")
    return hmac.new(_link_secret(), raw, hashlib.sha256).hexdigest()[:16]


def resolve_key(rows, key: str):
    """The (project, id) of the ledger row a Main App deep-link key names, or None. Matching runs here, so the list
    payload never carries a per-row key."""
    if not (isinstance(key, str) and re.fullmatch(r"[0-9a-f]{16}", key)):
        return None
    for r in rows:
        if hmac.compare_digest(item_key(r["project"], r["id"]), key):
            return r["project"], r["id"]
    return None


def item_url(cfg, project: str, ident: int) -> str:
    """The existing panel Main App link, targeted at one request-ledger row."""
    from . import panel_pin
    return panel_pin.app_url(cfg, "r" + item_key(project, ident))


def today(cfg, now: float, rows=None) -> dict:
    """The bounded request-ledger summary consumed by the widget snapshot."""
    from datetime import datetime
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if rows is None:
        rows = read(now, ledger_dir(cfg), history=True, dormant_after=dormant_seconds(cfg))
    try:
        zone = ZoneInfo(getattr(cfg, "timezone", "") or "UTC")
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        zone = ZoneInfo("UTC")
    local_day = datetime.fromtimestamp(now, zone).date()
    def on_day(stamp):
        return datetime.fromtimestamp(stamp, zone).date() == local_day
    done = sum(r["status"] == "done" and on_day(r["updated"]) for r in rows)
    open_today = [r for r in rows if r["status"] == "open" and (r["wait"] == "owner" or on_day(r["created"]))]
    owner_waiting = sorted((r for r in rows if r["status"] == "open" and r["wait"] == "owner"),
                           key=lambda r: (-r["created"], r["project"].casefold(), r["id"]))
    waiting = [{"title": clip(r.get("title_he") or r["title"], 60),
                "age_minutes": max(0, int((now - r["created"]) // 60)),
                "url": item_url(cfg, r["project"], r["id"])} for r in owner_waiting[:3]]
    return {"done_today": done, "total_today": done + len(open_today), "waiting": waiting}


def slim(rows, now: float, keep_session: bool = False) -> list:
    """The per-item fields the list renders: id, project, clipped title, age, wait, plus the flags that group it.

    `idle` is the seconds since anyone touched the item; `long` marks an open item idle for LONG_WAIT.
    Keep both bounded title variants so English pages can display the original.
    Nothing is dropped from the ledger itself: this only trims what a poll sends.
    `keep_session` keeps the creating session's id (the MCP server's ownership check); the page never gets it.
    """
    out = []
    for r in rows:
        idle = max(0, int(now - r["updated"]))
        item = {"project": r["project"], "id": r["id"],
                "title": clip(display_preview(r["title"])), "status": r["status"],
                "wait": r["wait"], "age": r["age"], "updated": int(r["updated"]), "idle": idle,
                "long": r["status"] == "open" and idle >= LONG_WAIT}
        if r.get("title_he"):  # both language variants remain bounded list fields
            item["title_he"] = clip(display_preview(r["title_he"]))
        if r.get("older"):
            item["older"] = True
        if r.get("urgent"):
            item["urgent"] = True
        if keep_session and r.get("session"):
            item["session"] = r["session"]
        out.append(item)
    return out


def detail(folder: Path, project, ident, now: float, tr=None):
    """One item's full text, latest note and steps (with cached Hebrew when a translator is given), or None."""
    if not isinstance(project, str) or type(ident) is not int:
        return None
    row = next((r for r in read(now, folder, history=True) if r["project"] == project and r["id"] == ident), None)
    if row is None:
        return None
    steps = row["steps"][-MAX_NOTES:]
    out = {"project": row["project"], "id": row["id"], "title": row["title"], "note": row["note"],
           "steps": [{"text": s["text"]} for s in steps]}
    if tr is not None:
        for part, field in [(out, "title")] + [(out, "note")] + [(s, "text") for s in out["steps"]]:
            if part[field]:
                value = tr.he(part[field])
                if value:
                    part[field + "_he"] = value
    return out
