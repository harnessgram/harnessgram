"""Compact, session-scoped views of request-ledger items."""
from __future__ import annotations

from . import owner_requests
from .creds import redact_channel
from .i18n import t

DONE_AFTER = 86400
TITLE_CHARS = 100
NOTE_CHARS = 140
MAX_CARDS = 20


def state(row):
    if row.get("status") == "done":
        return "done"
    if row.get("wait") == "owner":
        return "owner"
    return "working" if row.get("note") else "received"


def _clip(value, limit):
    text = " ".join(redact_channel(value if isinstance(value, str) else "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def matching(rows, session, now):
    """All non-expired items for a session, represented without ledger text."""
    return [{"status": state(row)} for row in rows
            if row.get("session") == session and
            not (row.get("status") == "done" and
                 not 0 <= now - row.get("updated", 0) <= DONE_AFTER)]


def cards(rows, session, now, lang="he"):
    """Bounded cards for exactly one session; recent completions remain for one day."""
    out = []
    for row in rows:
        if row.get("session") != session:
            continue
        if row.get("status") == "done" and not 0 <= now - row.get("updated", 0) <= DONE_AFTER:
            continue
        status = state(row)
        he = lang == "he"
        item = {"id": row["id"], "project": _clip(row["project"], 40),
                "title": _clip((he and row.get("title_he")) or row["title"], TITLE_CHARS),
                "status": status, "status_label": t(f"topic_status_{status}", lang=lang),
                "note": _clip((he and row.get("note_he")) or row.get("note", ""), NOTE_CHARS),
                "idle": max(0, int(now - row["updated"]))}
        out.append(item)
    return out[:MAX_CARDS]


def counts(items):
    """Non-sensitive state totals suitable for a shared Telegram topic."""
    return {name: sum(item["status"] == name for item in items)
            for name in ("received", "working", "owner", "done")}


def digest(items):
    """A stable edit-cap key that contains and reacts only to the public totals."""
    totals = counts(items)
    return ":".join(str(totals[name]) for name in ("received", "working", "owner", "done"))


def for_session(cfg, session, now, tr=None):
    rows = owner_requests.read(now, owner_requests.ledger_dir(cfg), history=True)
    if tr is not None:
        owner_requests.translate(rows, tr, [])
    else:
        from .translate import cached
        for row in rows:
            row["title_he"] = cached(row["title"], cfg)
            row["note_he"] = cached(row["note"], cfg) if row.get("note") else None
    return cards(rows, session, now, lang=cfg.ui_language)


def totals_for_session(cfg, session, now):
    """Public totals over every matching row; no request-ledger text is returned."""
    rows = owner_requests.read(now, owner_requests.ledger_dir(cfg), history=True)
    return counts(matching(rows, session, now))


def digest_for_session(cfg, session, now):
    """Digest over every matching row, including rows beyond the card display cap."""
    rows = owner_requests.read(now, owner_requests.ledger_dir(cfg), history=True)
    return digest(matching(rows, session, now))
