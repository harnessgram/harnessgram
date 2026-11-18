"""Topics derived from validated ledger rows, without commands, network or agent work.

PR numbers in prose are references only. Progress uses explicit links and the
request sweep's cached states; this module never refreshes them or does I/O.
"""
from __future__ import annotations

import re

from .owner_requests import DROPPED_NOTE

DAY = 86400
ORDER = {"owner": 0, "stuck": 1, "working": 2, "decided": 3, "done": 4}
DECISION = re.compile(r"^(?:Decided:|Owner picked\b|Owner approved\b)", re.I)
NEXT = re.compile(r"^Next:\s*\S", re.I)
PR = re.compile(r"(?<![\w#])#([1-9][0-9]*)(?!\w)")
STAGES = (("planning", 10), ("development", 40), ("review", 70),
          ("integration", 85), ("active", 100))


def stage(row, link_states):
    """Conservative progress: all required links/members must reach a stage.

    Integration is reserved, but skipped until the sweep can prove batch
    membership. A closed, unmerged PR or not-planned issue is not completion.
    """
    from .request_sweep import completed, item_urls, shipped
    if row["status"] == "done":
        return 4
    links = item_urls(row)
    if not links:
        return 1 if row.get("note", "").strip() or row.get("steps") else 0
    stages = []
    for url, match in links.items():
        state = link_states.get(url, {})
        if completed(state, match.group(3)):
            stages.append(4)
        elif match.group(3) == "pull" and state.get("state") == "open" and shipped(state):
            stages.append(2)
        else:
            stages.append(1)
    return min(stages)


def first_sentence(text):
    match = re.search(r"[.!?](?=\s|$)|\n", text)
    return text[:match.end()].strip() if match else text


def status(row, now):
    if row["status"] == "done":
        return "done"
    if row["wait"] == "owner":
        return "owner"
    if DECISION.match(row.get("note", "")):
        return "decided"
    return "stuck" if now - row["updated"] > DAY else "working"


def build(rows, now, link_states=None):
    """Group optional slugs within a project; untagged items remain separate.

    Completion and owner waiting take priority over a decision marker. A marker
    on the latest note otherwise means Decided.
    For groups, any unfinished item keeps the topic active, with the most urgent
    unfinished state winning. Ledger order breaks ties for undated string notes.
    """
    groups = {}
    for row in rows:
        if row["status"] == "done" and row.get("note", "").casefold().startswith(DROPPED_NOTE):
            continue
        key = (row["project"], "topic", row["topic"]) if row.get("topic") else (row["project"], "item", row["id"])
        groups.setdefault(key, []).append(row)
    topics = []
    for key, members in groups.items():
        members.sort(key=lambda r: (r.get("created", 0), r["id"]))
        first = members[0]
        active = [r for r in members if r["status"] != "done"]
        state = min((status(r, now) for r in active), key=ORDER.get) if active else "done"
        step = min(stage(r, link_states or {}) for r in (active or members))
        stage_name, percent = STAGES[step]
        notes = []
        for row in members:
            position_time = 0
            for index, note_step in enumerate(row["steps"]):
                # Append order is authoritative within an item, including mixed
                # dated/string notes or a producer with an inconsistent clock.
                position_time = max(position_time, note_step.get("at", row["updated"]))
                notes.append(dict(note_step, item_id=row["id"], _sort=(position_time, row["updated"], row["id"], index)))
        notes.sort(key=lambda n: n["_sort"], reverse=True)
        for note in notes:
            del note["_sort"]
        decision = next((n for n in notes if DECISION.match(n["text"])), {})
        active_ids = {r["id"] for r in active}
        latest = next((n for n in notes if n["item_id"] in active_ids), {})
        topic = {"key": list(key), "project": first["project"], "ids": [r["id"] for r in members],
                 "title": first_sentence(first["title"]), "state": state,
                 "stage": stage_name, "percent": percent, "step": step,
                 "updated": max(r["updated"] for r in members),
                 "decision": decision.get("text", ""), "next": latest.get("text", ""),
                 "next_explicit": bool(NEXT.match(latest.get("text", ""))),
                 "notes": notes,
                 "prs": [{"number": n} for n in sorted({int(m) for note in notes for m in PR.findall(note["text"])})]}
        if first.get("title_he"):
            topic["title_he"] = first_sentence(first["title_he"])
        for field, source in (("decision", decision), ("next", latest)):
            if source.get("text_he"):
                topic[field + "_he"] = source["text_he"]
        topics.append(topic)
    return sorted(topics, key=lambda t: (ORDER[t["state"]], -t["updated"], t["project"].casefold(), tuple(t["key"])))


# --- the state payload ------------------------------------------------------------------------------------
# A poll sends what a card shows: a clipped title, decision and next step, and the PR numbers. The timeline
# opens in the detail sheet and is read then (`timeline`). Closed cards stop being sent after CLOSED_DAYS.
CLOSED_DAYS = 7  # the Requests list's completion window (owner_requests.DONE_DAYS)
TITLE_SHOWN = 160
# Decision/update text is only a compact one-line preview (the owner asked for compact rows); opening the card fetches
# the complete timeline. A finished card (folded under "completed") sends a shorter title and its decision, no next step. Together this keeps a
# ledger with hundreds of cards in both languages inside the state budget.
FIELD_SHOWN = 20
DONE_TITLE_SHOWN = 80


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"


def _pair(card, field, original, translated, limit):
    """Keep useful bounded texts so each page can display its own language."""
    # Empty values and text already visible as the card title buy the browser
    # nothing.  Omitting them matters for large ledgers, especially because a
    # Hebrew copy takes more bytes on the wire than its character count.
    if original and original != card.get("title"):
        card[field] = _clip(original, limit)
    if translated and translated != card.get("title_he"):
        card[field + "_he"] = _clip(translated, limit)


def slim(topics, now):
    out = []
    for t in topics:
        if t["state"] == "done" and now - t["updated"] > CLOSED_DAYS * DAY:
            continue
        card = {"key": t["key"], "project": t["project"], "state": t["state"], "next_explicit": t["next_explicit"],
                "prs": t["prs"], "stage": t["stage"], "percent": t["percent"], "step": t["step"]}
        done = t["state"] == "done"
        # A finished card keeps its decision preview (its card and sheet say what was decided); only "next" is gone.
        fields = (("title", DONE_TITLE_SHOWN), ("decision", FIELD_SHOWN)) if done else (
            ("title", TITLE_SHOWN), ("decision", FIELD_SHOWN), ("next", FIELD_SHOWN))
        for field, limit in fields:
            _pair(card, field, t[field], t.get(field + "_he"), limit)
        out.append(card)
    return out


def timeline(rows, key, now, tr=None):
    """The notes of one topic, newest first, with cached Hebrew when a translator is given; None when unknown."""
    topic = next((t for t in build(rows, now) if t["key"] == list(key)), None)
    if topic is None:
        return None
    notes = []
    for n in topic["notes"]:
        note = {"text": n["text"]}
        if n.get("at"):
            note["at"] = n["at"]
        if n.get("text_he"):
            note["text_he"] = n["text_he"]
        elif tr is not None:
            he = tr.he(n["text"])
            if he:
                note["text_he"] = he
        notes.append(note)
    return notes
