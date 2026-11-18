"""Small, private home-day boundary used by the voice Tower.

Reads are bounded snapshots.  Writes are represented by expiring prepared actions and
can only be committed by the Cockpit's authenticated confirm route.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from . import board, family_board, schedule
from .config import home_chat, private_dir, state_dir, write_private

MAX_ROWS = 12
MAX_TITLE = 100
MAX_PENDING = 12
TTL = 3600
FILE = "tower-pending.json"


def _digest(row):
    payload = {key: row[key] for key in ("kind", "text", "blocks") if key in row}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _title(value) -> str:
    from .creds import for_outside
    return " ".join(for_outside(str(value), True).split())[:MAX_TITLE]


def today_at_home(cfg, now=None, source_list=None) -> dict:
    """One bounded, credential-free snapshot; callers may inject calendar fixtures."""
    moment = now or datetime.now(timezone.utc)
    diary = schedule.view(cfg, moment, source_list=source_list)
    home = family_board.view(cfg, moment)
    reminders = []
    owner_chats = {home_chat(cfg), *(p.chat_id for p in getattr(cfg, "projects", []))}
    for _chat, _thread, item in board.open_reminders():
        if _chat not in owner_chats:
            continue
        title = _title(item.get("text", ""))
        if title:
            reminders.append(title)
        if len(reminders) >= MAX_ROWS:
            break
    chores = home["days"][home["day"]]["chores"] if not home["shabbat"] else []
    return {
        "schedule": {day: [{"title": _title(row["title"]), "time": row.get("time")}
                           for row in diary["days"][day][:MAX_ROWS]]
                     for day in ("today", "tomorrow")},
        "chores": [_title(row["text"]) for row in chores if not row["done"]][:MAX_ROWS],
        "routines": [_title(row["text"]) for row in home["daily"] if not row["done"]][:MAX_ROWS],
        "shopping": [_title(row["text"]) for row in home["shop"] if not row["done"]][:MAX_ROWS],
        "reminders": reminders,
    }


@contextmanager
def _locked():
    root = private_dir(state_dir())
    fd = os.open(root / "tower-pending.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _path():
    return state_dir() / FILE


def _read(now=None):
    stamp = time.time() if now is None else now
    try:
        rows = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rows = []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("expires"), (int, float))
            and r["expires"] > stamp][-MAX_PENDING:]


def prepare(kind, text, now=None, blocks=None) -> dict:
    if kind not in ("shopping", "reminder", "day_plan"):
        raise ValueError("kind")
    clean = _title(text)
    if not clean or len(str(text)) > MAX_TITLE:
        raise ValueError("text")
    stamp = time.time() if now is None else now
    with _locked():
        rows = _read(stamp)
        row = {"id": secrets.token_hex(8), "kind": kind, "text": clean,
               "created": stamp, "expires": stamp + TTL}
        if kind == "day_plan":
            if not isinstance(blocks, list) or not blocks or len(blocks) > MAX_ROWS:
                raise ValueError("blocks")
            row["blocks"] = [{"time": _title(b.get("time")), "title": _title(b.get("title"))}
                             for b in blocks if isinstance(b, dict) and b.get("time") and b.get("title")]
            if len(row["blocks"]) != len(blocks):
                raise ValueError("blocks")
        row["digest"] = _digest(row)
        rows = (rows + [row])[-MAX_PENDING:]
        write_private(_path(), json.dumps(rows, ensure_ascii=False, separators=(",", ":")))
    return row


def pending(now=None):
    with _locked():
        return _read(now)


def confirm(cfg, ident, digest, now=None) -> dict:
    stamp = time.time() if now is None else now
    with _locked():
        all_rows = _read(stamp)
        matches = [r for r in all_rows if r.get("id") == ident]
        if len(matches) != 1:
            raise KeyError("expired")
        row = matches[0]
        supplied = digest if isinstance(digest, str) and digest.isascii() else ""
        if not secrets.compare_digest(supplied, row.get("digest", "")) or row["digest"] != _digest(row):
            raise KeyError("expired")
        # Commit while the pending lock is held, so two taps cannot write twice.
        if row["kind"] == "shopping":
            result = family_board.add("shop", row["text"])
        elif row["kind"] == "day_plan":
            day = datetime.fromtimestamp(stamp, schedule.zone(cfg)).date()
            result = [schedule.add_structured(cfg, b["title"], day, b["time"],
                                              datetime.fromtimestamp(stamp, timezone.utc)) for b in row["blocks"]]
        else:
            result = schedule.add(cfg, row["text"])
        all_rows.remove(row)
        write_private(_path(), json.dumps(all_rows, ensure_ascii=False, separators=(",", ":")))
    return {"id": row["id"], "kind": row["kind"], "item": result}
