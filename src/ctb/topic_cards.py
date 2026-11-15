"""Topics derived from validated ledger rows, without commands, network or agent work.

PR numbers are references only: the bridge currently has no repository-scoped PR
state cache. Never infer a merge from prose, or conflate equal numbers in projects.
"""
from __future__ import annotations

import re

DAY = 86400
ORDER = {"owner": 0, "stuck": 1, "working": 2, "decided": 3, "done": 4}
DECISION = re.compile(r"^(?:Decided:|Owner picked\b|Owner approved\b)", re.I)
NEXT = re.compile(r"^Next:\s*\S", re.I)
PR = re.compile(r"(?<![\w#])#([1-9][0-9]*)(?!\w)")


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


def build(rows, now):
    """Group optional slugs within a project; untagged items remain separate.

    Completion and owner waiting take priority over a decision marker. A marker
    on the latest note otherwise means Decided.
    For groups, any unfinished item keeps the topic active, with the most urgent
    unfinished state winning. Ledger order breaks ties for undated string notes.
    """
    groups = {}
    for row in rows:
        key = (row["project"], "topic", row["topic"]) if row.get("topic") else (row["project"], "item", row["id"])
        groups.setdefault(key, []).append(row)
    topics = []
    for key, members in groups.items():
        members.sort(key=lambda r: (r.get("created", 0), r["id"]))
        first = members[0]
        active = [r for r in members if r["status"] != "done"]
        state = min((status(r, now) for r in active), key=ORDER.get) if active else "done"
        notes = []
        for row in members:
            position_time = 0
            for index, step in enumerate(row["steps"]):
                # Append order is authoritative within an item, including mixed
                # dated/string notes or a producer with an inconsistent clock.
                position_time = max(position_time, step.get("at", row["updated"]))
                notes.append(dict(step, item_id=row["id"], _sort=(position_time, row["updated"], row["id"], index)))
        notes.sort(key=lambda n: n["_sort"], reverse=True)
        for note in notes:
            del note["_sort"]
        decision = next((n for n in notes if DECISION.match(n["text"])), {})
        active_ids = {r["id"] for r in active}
        latest = next((n for n in notes if n["item_id"] in active_ids), {})
        topic = {"key": list(key), "project": first["project"], "ids": [r["id"] for r in members],
                 "title": first_sentence(first["title"]), "state": state,
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
