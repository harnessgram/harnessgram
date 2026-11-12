"""Incremental transcript accounting. Costs are estimates at API list prices; subscriptions are flat fee.

Prices are USD per million tokens (input, output, cache write 5m, cache read).
These defaults may be outdated. User-supplied prices take precedence.
"""
from __future__ import annotations

import json
import os
import re
import threading
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import status
from .config import state_dir, private_dir

_MODEL = re.compile(r"claude-(?:opus|sonnet|haiku|fable)-\d{1,2}(?:-\d{1,2})?(?:-\d{8})?")

PRICES = {
    "claude-fable-5-2": (8, 40, 10, .20),
    "claude-fable-5-1": (10, 50, 12.5, .25),
    "claude-fable-5": (10, 50, 12.5, 1),
    "claude-opus-5-5": (4, 20, 5, .20),
    "claude-opus-5": (5, 25, 6.25, .50),
    "claude-opus-4-8": (5, 25, 6.25, .50),
    "claude-opus-4-7": (5, 25, 6.25, .50),
    "claude-opus-4-6": (5, 25, 6.25, .50),
    "claude-opus-4-5": (5, 25, 6.25, .50),
    "claude-opus-4": (15, 75, 18.75, 1.5),
    "claude-opus-4-1": (15, 75, 18.75, 1.5),
    "claude-sonnet-5-5": (2, 10, 2.5, .10),
    "claude-sonnet-5": (2, 10, 2.5, .20),
    "claude-sonnet-4": (3, 15, 3.75, .30),
    "claude-sonnet-4-5": (3, 15, 3.75, .30),
    "claude-sonnet-4-6": (3, 15, 3.75, .30),
    "claude-haiku-5-5": (.10, .50, .125, .01),
    "claude-haiku-4-5": (1, 5, 1.25, .10),
}
FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
HOURLY = "cache_creation_1h_input_tokens"
COUNT_FIELDS = FIELDS + (HOURLY,)
ZONE = ZoneInfo("Asia/Jerusalem")
TTL = 15
_lock = threading.Lock()
_snapshot = {"sessions": {}, "totals": None}
_started = 0.0
_running = False
_price_key = None


def _matches(model, prefix):
    return model == prefix or model.startswith(prefix if prefix.endswith('-') else prefix + '-')


def price(model, counts, overrides=None):
    rates = None
    matches = [k for k in (overrides or {}) if _matches(model, k)]
    if matches:
        rates = overrides[max(matches, key=len)]
    else:
        rates = PRICES.get(re.sub(r"-\d{8}$", "", model))
    if rates is None:
        return None
    total = sum(counts.get(k, 0) * r for k, r in zip(FIELDS, rates))
    # The 1h rate is 2x input, or 1.6x the configured 5m write rate.
    total += counts.get(HOURLY, 0) * rates[2] * .6
    return total / 1_000_000


def scan_file(path, previous):
    """Read only complete appended lines; retain an incomplete tail for the next pass."""
    stat = path.stat()
    old = previous or {}
    if old.get("inode") != stat.st_ino or stat.st_size < old.get("offset", 0):
        old = {}
    offset = old.get("offset", 0)
    # Re-read older caches to normalize models and include request IDs.
    if old.get("version") != 4:
        old = {}
        offset = 0
    messages = old.get("messages", {})
    anonymous = old.get("anonymous", [])
    with path.open("rb") as f:
        f.seek(offset)
        while True:
            line = f.readline()
            if not line or not line.endswith(b"\n"):
                break
            offset = f.tell()
            try:
                row = json.loads(line)
                msg = row.get("message") or {}
                usage = msg.get("usage") or {}
                model = msg.get("model")
                if row.get("type") != "assistant" or not isinstance(model, str) or not isinstance(usage, dict):
                    continue
                if model not in PRICES and not _MODEL.fullmatch(model):
                    model = "unknown"  # only a known model family name ever reaches the panel
                stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    continue
                day = stamp.astimezone(ZONE).date().isoformat()
                counts = {}
                for k in FIELDS:
                    value = usage.get(k, 0)
                    counts[k] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
                creation = usage.get("cache_creation")
                if isinstance(creation, dict) and any(k in creation for k in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")):
                    def valid(value):
                        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
                    five = valid(creation.get("ephemeral_5m_input_tokens", 0))
                    hour = valid(creation.get("ephemeral_1h_input_tokens", 0))
                    counts["cache_creation_input_tokens"] = five + hour
                    counts[HOURLY] = hour
                item = [day, model, counts]
                message_id = msg.get("id")
                if isinstance(message_id, str) and message_id:
                    # an API message id is unique on its own; a copied (resumed or forked) record keeps it
                    messages[message_id] = _merge_message(messages.get(message_id), item)
                else:
                    anonymous.append(item)
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
    data = _daily(list(messages.values()) + anonymous)
    return {"version": 4, "inode": stat.st_ino, "offset": offset, "data": data,
            "messages": messages, "anonymous": anonymous}


def _merge_message(previous, item):
    if previous is None:
        return item
    # Streamed copies are cumulative; keep the greatest reported counts.
    day, model, counts = item
    old_counts = previous[2]
    return [previous[0], model, {k: max(old_counts.get(k, 0), counts.get(k, 0)) for k in COUNT_FIELDS}]


def _daily(items):
    data = {}
    for day, model, counts in items:
        out = data.setdefault(day, {}).setdefault(model, {k: 0 for k in COUNT_FIELDS})
        for k in COUNT_FIELDS:
            out[k] += counts.get(k, 0)
    return data


def aggregate(days, overrides=None):
    models = {}
    for day in days:
        for model, counts in day.items():
            out = models.setdefault(model, {k: 0 for k in COUNT_FIELDS})
            for k in COUNT_FIELDS:
                out[k] += counts.get(k, 0)
    total = {k: sum(c[k] for c in models.values()) for k in FIELDS}
    grouped = {}
    for model, counts in models.items():
        key = re.sub(r"-\d{8}$", "", model)
        entry = grouped.setdefault(key, {**{k: 0 for k in FIELDS}, "estimate_usd": 0, "unpriced_tokens": 0, "has_unpriced": False, "has_priced": False})
        for field in FIELDS:
            entry[field] += counts[field]
        cost = price(model, counts, overrides)
        if cost is None:
            entry["has_unpriced"] = True
            entry["unpriced_tokens"] += sum(counts[k] for k in FIELDS)
        else:
            entry["has_priced"] = True
            entry["estimate_usd"] += cost
    for entry in grouped.values():
        if not entry["has_priced"]:
            entry["estimate_usd"] = None
    unpriced = sum(entry["unpriced_tokens"] for entry in grouped.values())
    known = [entry["estimate_usd"] for entry in grouped.values() if entry["estimate_usd"] is not None]
    return {"models": grouped, **total, "tokens": sum(total.values()),
            "estimate_usd": sum(known) if known or not grouped else None,
            "unpriced_tokens": unpriced, "unknown_models": any(x["has_unpriced"] for x in grouped.values())}


def collect_usage(sessions, cache, now, overrides=None):
    today = datetime.fromtimestamp(now, ZONE).date()
    week = today - timedelta(days=today.weekday())
    result, week_days, all_messages, all_anonymous, seen_files = {}, {}, {}, [], set()
    for session, name in sessions:
        path = status.transcript_of(session)
        if not path:
            continue
        root = Path(path)
        files = [root] + list((root.parent / session / "subagents").glob("*.jsonl"))
        messages, anonymous = {}, []
        for file in files:
            key = str(file)
            try:
                record = scan_file(file, cache.get(key))
            except OSError:
                continue
            cache[key] = record
            for message_id, item in record["messages"].items():
                messages[message_id] = _merge_message(messages.get(message_id), item)
                all_messages[message_id] = _merge_message(all_messages.get(message_id), item)
            anonymous.extend(record["anonymous"])
            if key not in seen_files:
                all_anonymous.extend(record["anonymous"])
                seen_files.add(key)
        daily = _daily(list(messages.values()) + anonymous)
        today_days = [daily.get(today.isoformat(), {})]
        week_data = [v for d, v in daily.items() if week.isoformat() <= d <= today.isoformat()]
        result[session] = {"today": aggregate(today_days, overrides),
                           "week": aggregate(week_data, overrides),
                           "session": aggregate(daily.values(), overrides)}
        week_days[session] = name
    all_daily = _daily(list(all_messages.values()) + all_anonymous)
    today_total = aggregate([all_daily.get(today.isoformat(), {})], overrides)
    week_total = aggregate([v for d, v in all_daily.items() if week.isoformat() <= d <= today.isoformat()], overrides)
    top = sorted(result, key=lambda s: result[s]["today"]["tokens"], reverse=True)[:3]
    return {"sessions": result, "totals": {"today": today_total, "week": week_total,
            "top": [{"id": s[:8], "name": week_days[s], "today": result[s]["today"]} for s in top if result[s]["today"]["tokens"]]}}


def _refresh(sessions, now, directory, overrides, price_key):
    global _snapshot, _running
    path = directory / "session-usage-cache.json"
    try:
        try:
            cache = json.loads(path.read_text())
            if not isinstance(cache, dict):
                cache = {}
        except (OSError, ValueError):
            cache = {}
        result = collect_usage(sessions, cache, now, overrides)
        private_dir(directory)
        fd, tmp = tempfile.mkstemp(prefix=".session-usage-", dir=directory)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(cache, f, separators=(",", ":"))
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        with _lock:
            if _price_key == price_key:
                _snapshot = result
    except (OSError, ValueError, TypeError):
        pass
    finally:
        with _lock:
            _running = False


def snapshot(sessions, now, overrides=None):
    """Return immediately; one daemon refreshes stale data off the request thread."""
    global _started, _running, _snapshot, _price_key
    with _lock:
        price_key = tuple(sorted((k, tuple(v)) for k, v in (overrides or {}).items()))
        if price_key != _price_key:
            _price_key = price_key
            _snapshot = {"sessions": {}, "totals": None}
            _started = 0
        start = not _running and time.monotonic() - _started >= TTL
        if start:
            _started = time.monotonic()
            _running = True
        current = _snapshot
    if start:  # started outside the lock: _refresh takes it again when it ends
        threading.Thread(target=_refresh, args=(list(sessions), now, state_dir(), dict(overrides or {}), price_key), daemon=True).start()
    return current
