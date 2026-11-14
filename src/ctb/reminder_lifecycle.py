"""Conservative reminder dates: explicit calendar dates only, never guessed weekdays/times.

Undated reminders age after 14 days; dated reminders after their deadline plus three
days. Restoring grants 14 days in the active list without rewriting the original date.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

DAY = 86400
UNDATED_AGE = 14 * DAY
OVERDUE_AGE = 3 * DAY
_DATE = r"\d{4}-\d{2}-\d{2}"
_FULL = re.compile(r"(" + _DATE + r")(?:[T ](\d{2}:\d{2}(?::\d{2})?)(Z|[+-]\d{2}:\d{2})?)?")
_DEADLINE = re.compile(r"(?:\b(?:by|until|before|due(?:\s+on)?|deadline)\s*:?\s*|(?:עד|מועד אחרון)\s*:?[ ]*(?:ליום\s+)?)" + _DATE, re.I)
_UNSAFE = re.compile(r"\b(?:every|daily|weekly|monthly|yearly|annually|recurring|not|no|never|unless|pending|cancelled|extended|postponed)\b|don['’]t|בכל|מדי|נדחה|בוטל|לא |אל ", re.I)


def deadline(value: str):
    """Full ISO date/date-time -> epoch; dates expire at the end of the local day.

    Mixed prose and date-only times are deliberately not interpreted here.
    """
    match = _FULL.fullmatch(value.strip()) if isinstance(value, str) else None
    if not match:
        return None
    try:
        if match[2]:
            dt = datetime.fromisoformat(match[1] + "T" + match[2] + (match[3] or "").replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(match[1]) + timedelta(days=1)
        return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def is_old(item: dict, now: float) -> bool:
    if item.get("r", 0) > 0 and 0 <= now - item["r"] <= UNDATED_AGE:
        return False
    due = deadline(item.get("d", ""))
    if due is not None:
        return now - due > OVERDUE_AGE
    at = item.get("at", 0)
    return at > 0 and now - at > UNDATED_AGE


def expired_text(item: dict, now: float) -> bool:
    """Only a single explicit ISO deadline in the text permits automatic removal.

    Age alone, a date mentioned in passing, ambiguous/relative dates, recurrences,
    negations, extensions and text with multiple dates can never authorize a sweep.
    """
    text = item.get("s", "")
    if not isinstance(text, str) or _UNSAFE.search(text) or not _DEADLINE.search(text):
        return False
    matches = list(_FULL.finditer(text))
    if len(matches) != 1:
        return False
    if text[matches[0].end():].strip(" .!;\n\t"):
        return False  # trailing qualifications, unknown timezone or malformed time: preserve it
    # The date must be the deadline's own date, not a nearby historical reference.
    marker = _DEADLINE.search(text)
    if matches[0].start() < marker.start() or matches[0].start() >= marker.end():
        return False
    due = deadline(matches[0][0])
    if due is None or due >= now:
        return False
    if item.get("d"):
        explicit = deadline(item["d"])
        if explicit is None or explicit >= now:
            return False
    # An explicit restoration is the owner's instruction to keep it active.
    return not (item.get("r", 0) > 0 and 0 <= now - item["r"] <= UNDATED_AGE)
