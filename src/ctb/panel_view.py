"""What the control panel derives for the owner from state the bridge already keeps: which open decisions are
stale, what each session is doing right now, its last reply's summary and the link to its topic.

Nothing here writes, and every reader returns "" or an empty result when its file is missing or odd, so one
broken session never breaks the panel.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, Iterable, Optional

from . import fmt
from .config import state_dir
from .pins import SUPERGROUP_PREFIX

OLD_AFTER = 3 * 24 * 3600  # a waiting item (decision, ask card, reminder) older than this is "old": the page folds it away
STALE_AFTER = OLD_AFTER  # an open decision older than this has most likely lost its moment
TAIL = 256 * 1024  # bytes of a transcript's end read for the last reply
STEP_TAIL = 32 * 1024  # bytes of a live progress list read for the current step
STEP_CHARS = 140
SUMMARY_CHARS = 240


# --- decisions -------------------------------------------------------------------------------------

def _gist(title: str) -> str:
    """A title without ids, versions and numbers: "Release 5284cd49d to X?" and "Release 7a1b2c3 to X?" match."""
    return " ".join(w for w in re.findall(r"\w+", title.lower()) if not re.search(r"\d", w))


def is_old(now: float, then) -> bool:
    """True when `then` (an item's own timestamp) is strictly more than OLD_AFTER before `now`. The age is judged
    here, on the server's clock; a missing, non-numeric or future time counts as fresh."""
    if isinstance(then, bool) or not isinstance(then, (int, float)) or then != then:
        return False
    return now - then > OLD_AFTER


def stale_reasons(records: Iterable[dict], states: Dict[str, str], now: float) -> Dict[str, str]:
    """{decision id: why it no longer waits on the owner} for open decision records (decisions.py):
    "ended" when the asking session has ended (or is gone from the topics), "replaced" when the same session
    asked the same question again later (same words once ids and numbers are dropped), "old" when it is older
    than OLD_AFTER (a decision that is only old stays answerable: the page groups it under "old"). Fresh decisions are not in the result."""
    records = list(records)
    newest: Dict[tuple, float] = {}
    for r in records:
        key = (r["session"], _gist(r["title"]))
        newest[key] = max(newest.get(key, 0.0), r["created"])
    out = {}
    for r in records:
        if states.get(r["session"], "ended") == "ended":
            out[r["id"]] = "ended"
        elif r["created"] < newest[(r["session"], _gist(r["title"]))]:
            out[r["id"]] = "replaced"
        elif is_old(now, r["created"]):
            out[r["id"]] = "old"
    return out


# --- sessions --------------------------------------------------------------------------------------

def topic_url(chat, thread) -> str:
    """The t.me link that opens a topic of a private supergroup; "" for anything else."""
    if type(chat) is not int or type(thread) is not int or not str(chat).startswith(SUPERGROUP_PREFIX):
        return ""
    return f"https://t.me/c/{str(chat)[len(SUPERGROUP_PREFIX):]}/{thread}"


def _tail_lines(path: str, size: int) -> list:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            f.seek(max(0, end - size))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[1:] if end > size else lines  # the first line of a cut read is partial


def current_step(session: str) -> str:
    """The latest step on the session's live activity card (its first line), while a turn runs; "" otherwise.
    The card's list is removed when the turn ends, so an idle session has none."""
    from .progress import safe_name
    for line in reversed(_tail_lines(str(state_dir() / "progress" / f"{safe_name(session)}.jsonl"), STEP_TAIL)):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("kind") == "action" and isinstance(row.get("l"), str):
            first = row["l"].strip().splitlines()[0] if row["l"].strip() else ""
            return first[:STEP_CHARS - 1] + "…" if len(first) > STEP_CHARS else first
    return ""


def _plain(text: str) -> str:
    text = fmt.FENCE.sub(" ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return " ".join(re.sub(r"[*_`#>|]", "", text).split())


def last_summary(path: Optional[str]) -> str:
    """The "Summary:" line of the session's last reply, or the reply's first line when it has none; ""."""
    if not path:
        return ""
    for line in reversed(_tail_lines(path, TAIL)):
        if '"assistant"' not in line or '"text"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type") != "assistant":
            continue
        content = (ev.get("message") or {}).get("content")
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"] \
            if isinstance(content, list) else []
        text = "\n".join(t for t in texts if isinstance(t, str)).strip()
        if not text:
            continue
        body, summary = fmt.split_summary(text)
        if not summary:
            summary = next((s for s in (_plain(x) for x in fmt.FENCE.sub("\n", body).splitlines()) if s), "")
        from .creds import redact
        summary = redact(_plain(summary))
        return summary[:SUMMARY_CHARS - 1] + "…" if len(summary) > SUMMARY_CHARS else summary
    return ""
