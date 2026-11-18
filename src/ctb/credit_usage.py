"""Estimated credit spend for explicitly recorded activation intervals.

Activation intervals are recorded by credit_mode before the account move starts.
The prices in session_usage are estimates and can become outdated.
The local spend gate leaves a safety margin for concurrent requests and cached
credentials; it cannot impose a hard billing cap. The true hard stop is an
Anthropic Console workspace spend limit, configured by the owner in their account.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

from . import session_usage

DAILY_CAP_USD = 10.0
MARGIN_USD = 1.50


def estimate(intervals, now, *, zone=None):
    """Return today's estimated cost, or None if a trustworthy estimate is unavailable.

    Each interval is (transcript paths, start timestamp, end timestamp or None).
    Include the main transcript and its subagent transcripts. Intervals are half-open;
    callers must persist them durably before starting any billable process.
    """
    # Resolve the local offset for each timestamp: the offset today can differ
    # from the offset at midnight on a daylight-saving transition day.
    local = lambda ts: (datetime.fromtimestamp(ts, zone) if zone else
                        datetime.fromtimestamp(ts).astimezone())
    today = local(now).date()
    messages, anonymous = {}, []
    seen_rows = set()
    try:
        for paths, start, end in intervals:
            if not paths or start > now or (end is not None and end < start):
                return None
            for path in paths:
                with Path(path).open('rb') as stream:
                    stat = Path(path).stat()
                    for line_number, line in enumerate(stream):
                        row_key = (stat.st_dev, stat.st_ino, line_number)
                        if not line.endswith(b'\n'):
                            return None
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            return None
                        if row.get('type') != 'assistant':
                            continue
                        stamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
                        if stamp.tzinfo is None:
                            return None
                        ts = stamp.timestamp()
                        if ts < start or (end is not None and ts >= end) or ts > now or local(ts).date() != today:
                            continue
                        if row_key in seen_rows:
                            continue
                        seen_rows.add(row_key)
                        msg = row.get('message')
                        if not isinstance(msg, dict) or not isinstance(msg.get('usage'), dict):
                            return None
                        usage = msg['usage']
                        counts = {}
                        for field in session_usage.FIELDS:
                            value = usage.get(field, 0)
                            if type(value) is not int or value < 0:
                                return None
                            counts[field] = value
                        creation = usage.get('cache_creation')
                        if creation is not None:
                            if not isinstance(creation, dict):
                                return None
                            five = creation.get('ephemeral_5m_input_tokens', 0)
                            hour = creation.get('ephemeral_1h_input_tokens', 0)
                            if type(five) is not int or type(hour) is not int or min(five, hour) < 0:
                                return None
                            if 'ephemeral_5m_input_tokens' in creation or 'ephemeral_1h_input_tokens' in creation:
                                counts['cache_creation_input_tokens'] = five + hour
                                counts[session_usage.HOURLY] = hour
                        model = msg.get('model')
                        if not isinstance(model, str) or session_usage.price(model, counts) is None:
                            return None
                        item = [today.isoformat(), model, counts]
                        ident = msg.get('id')
                        if isinstance(ident, str) and ident:
                            previous = messages.get(ident)
                            if previous and previous[1] != model:
                                return None
                            messages[ident] = session_usage._merge_message(previous, item)
                        else:
                            anonymous.append(item)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return None
    daily = session_usage._daily(list(messages.values()) + anonymous)
    return session_usage.aggregate(daily.values())['estimate_usd']


def spend_gate_available():
    """Whether the local measured-spend gate is enabled for credential renewal."""
    return True


def may_start(estimated_usd):
    """Allow a launch only with known spend below the daily cap minus its margin."""
    return (spend_gate_available() and type(estimated_usd) in (int, float)
            and math.isfinite(estimated_usd)
            and 0 <= estimated_usd < DAILY_CAP_USD - MARGIN_USD)
