"""Read-only owner ledger checklist. No commands, writes, or network calls on a state poll.

The ledger is external to bridge state. Each regular JSON file is read afresh, with a hard
byte bound (including a file growing during the read); bad files/items are simply skipped.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
from pathlib import Path

from .creds import redact_channel
from .work_lines import clean_text, project_name

FILE_BYTES = 1024 * 1024
MAX_ITEMS = 1000
MAX_NOTES = 50
TEXT_CHARS = 1500
DONE_DAYS = 7 * 86400


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


def read(now: float, folder: Path = None, *, history=False) -> list:
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
                         "older": status == "open" and item.get("wait") == "owner" and now - updated >= DONE_DAYS,
                         "urgent": bool(steps and steps[-1]["text"] == "pushed by owner"),
                         "note": steps[-1]["text"] if steps else "", "steps": steps})
            topic = item.get("topic")
            if isinstance(topic, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", topic):
                rows[-1]["topic"] = topic
            if history:
                rows[-1]["created"] = created
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
