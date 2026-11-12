"""The staff edition of the team radio: what an outside listener (the owner's staff) may hear and see.

The owner's radio reads his private state: decisions and questions waiting on him, quotas, every project. The
staff edition is NOT a copy of it. It is built from scratch by an allowlist over the same snapshot (deny by
default), in three passes:

1. `staff_view`: only the projects the owner listed (`radio.youtube.projects`, {ledger key: staff name}) and only
   the sections allowed for staff (SECTIONS). From each listed project it takes how many items were finished
   today and how many of its team items are open: counts only. No text of the snapshot is ever read (no item
   titles, decisions, reminders, sessions, questions, items waiting on the owner, quotas, agents, keys or ids):
   a plain sentence can be private and no filter can tell. An empty allowlist means the station only
   identifies itself.
2. The radio's fresh-only rules: an item is spoken once, only while it is fresh (radio_script.FRESH_WINDOW) and
   finished today; older items are at most counted on the card.
3. A last gate on every sentence and card line (`gate`): it must consist of letters of the station's language,
   spaces and plain punctuation only (no digits, no Latin letters in Hebrew, no links, paths, @, #, emoji or
   brackets) and must come through `creds.for_outside` unchanged. A line that fails is dropped, never repaired.

Numbers are words, so a Hebrew line never mixes directions. What staff hear and see is limited to the
station's name, the listed projects' staff names (from the owner's settings) and counts in words.
"""
from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Tuple

from . import radio_script
from .radio_script import FRESH_WINDOW, clock_he, count_he

SECTIONS = ("station_id", "done_today", "in_work")   # the fixed list of what staff may ever hear
MAX_PROJECTS = 8
MAX_TOLD = 300
from .i18n import RADIO_STAFF_TITLE_HE as TITLE_HE
TITLE_EN = "Team radio"
_ALLOWED = {
    # Hebrew letters and points, spaces, plain punctuation (incl. geresh and gershayim)
    "he": re.compile(r"[א-תְ-ׇ ,.:!?'\"׳״–\-]+"),
    "en": re.compile(r"[A-Za-z ,.:!?'\-]+"),
}
_CARD_MARKS = ("✅ ", "🛠 ", "📻 ")  # the card's own fixed marks, put in front of gated lines only


def profile(raw: object, lang: str = "he") -> dict:
    """The staff profile from the `radio.youtube` settings object. Anything malformed is dropped (deny)."""
    raw = raw if isinstance(raw, dict) else {}
    lang = "he" if lang == "he" else "en"
    projects: Dict[str, str] = {}
    given = raw.get("projects")
    for key, name in (given.items() if isinstance(given, dict) else []):
        if isinstance(key, str) and key.strip() and isinstance(name, str) and len(projects) < MAX_PROJECTS:
            projects[key.strip()] = name
    sections = raw.get("sections")
    if isinstance(sections, list):
        wanted = [s for s in SECTIONS if s in sections]
    else:
        wanted = list(SECTIONS)
    if "station_id" not in wanted:
        wanted.insert(0, "station_id")  # the station always says who it is
    every = raw.get("every_minutes")
    every = int(every) if isinstance(every, (int, float)) and not isinstance(every, bool) and every >= 5 else 30
    title = gate(raw.get("title"), lang, 40) if isinstance(raw.get("title"), str) else ""
    return {"enabled": raw.get("enabled") is True, "projects": projects, "sections": wanted,
            "title": title or (TITLE_HE if lang == "he" else TITLE_EN),
            "every_minutes": every, "lang": lang}


def gate(text: object, lang: str = "he", limit: int = 120) -> str:
    """The text when it is fit for staff as it is, else "". Never repairs: a line that needs cleaning is out."""
    if not isinstance(text, str):
        return ""
    line = " ".join(text.split())
    if not line or len(line) > limit or not _ALLOWED["he" if lang == "he" else "en"].fullmatch(line):
        return ""
    if lang == "he" and not radio_script.hebrew_enough(line, 0.99):
        return ""
    from .creds import for_outside
    return line if for_outside(line, True) == line else ""


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1000 else 0


def _same_day(a: float, b: float) -> bool:
    try:
        return time.localtime(a)[:3] == time.localtime(b)[:3]
    except (OverflowError, ValueError, OSError):
        return False


def staff_view(snap: dict, prof: dict, last: Optional[dict] = None) -> dict:
    """Pass 1 and 2: the allowlisted facts only, built new (nothing from `snap` is passed through as is)."""
    now = radio_script._now(snap)
    lang = prof["lang"]
    told = set((last or {}).get("done") or [])
    sections = set(prof["sections"])
    projects: List[dict] = []
    for key, name in prof["projects"].items():
        label = gate(name, lang, 40)
        if not label:
            continue  # a staff name the voice could not say cleanly: the project is left out
        done: List[dict] = []
        if "done_today" in sections:
            for item in snap.get("done") or []:
                if not isinstance(item, dict) or item.get("project") != key or not isinstance(item.get("key"), str):
                    continue
                age = radio_script._age(item, now, "updated")
                if age is None or not _same_day(now - age, now):
                    continue
                # the key and the time only: an item's text is never read (counts, not titles)
                done.append({"key": item["key"], "fresh": age <= FRESH_WINDOW and item["key"] not in told})
        open_n = _count((snap.get("team_open_by") or {}).get(key)) if "in_work" in sections and \
            isinstance(snap.get("team_open_by"), dict) else 0
        projects.append({"label": label, "done": done, "open": open_n})
    return {"at": int(now), "lang": lang, "title": prof["title"], "projects": projects,
            "sections": [s for s in SECTIONS if s in sections]}


def _project_sentences(p: dict, he: bool) -> List[str]:
    out = []
    new = [d for d in p["done"] if d["fresh"]]
    if new:
        if he:
            out.append(f"בפרויקט {p['label']} הושלמה משימה חדשה" if len(new) == 1 else
                       f"בפרויקט {p['label']} הושלמו {count_he(len(new), 'משימה', 'משימות', 'f')} חדשות")
        else:
            out.append(f"In {p['label']}, {_count_en(len(new), 'task')} finished")
    if p["open"]:
        if he:
            out.append(f"בפרויקט {p['label']} יש עכשיו {count_he(p['open'], 'משימה', 'משימות', 'f')} בעבודה")
        else:
            out.append(f"In {p['label']}, {_count_en(p['open'], 'task')} in progress now")
    return out


_EN = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]


def _count_en(n: int, word: str) -> str:
    return (f"{_EN[n]} {word}{'' if n == 1 else 's'}" if n < len(_EN) else f"many {word}s")


def has_news(view: dict) -> bool:
    return any(d["fresh"] for p in view["projects"] for d in p["done"])


def build_script(view: dict) -> str:
    """Pass 3 included: the spoken staff update. Every sentence goes through `gate`; the station id always stays."""
    he = view["lang"] == "he"
    when = time.localtime(view["at"])
    sentences = ([f"{view['title']}, השעה {clock_he(when.tm_hour, when.tm_min)}. שלום לכל הצוות"] if he else
                 [f"{view['title']}. Hello to the whole team"])
    body: List[str] = []
    for p in view["projects"]:
        body += _project_sentences(p, he)
    if not body:
        body.append("אין עדכונים חדשים כרגע. המשך יום טוב" if he else "No new updates right now. Have a good day")
    out = []
    for s in sentences + body:
        for part in s.split(". "):
            line = gate(part.rstrip(". ") + ".", view["lang"], 400)
            if line:
                out.append(line)
    if not out:  # the clock or the title failed the gate: the plain station id
        out.append(TITLE_HE + "." if he else TITLE_EN + ".")
    return " ".join(out)


def card_sections(view: dict) -> List[dict]:
    """The staff card: what was finished today and what is in work, per listed project. Only gated lines."""
    he = view["lang"] == "he"
    done_lines, work_lines = [], []
    for p in view["projects"]:
        if p["done"]:
            n = len(p["done"])
            line = gate(f"{p['label']}: " + (count_he(n, 'משימה', 'משימות', 'f') if he else _count_en(n, 'task')),
                        view["lang"], 80)
            if line:
                done_lines.append(line)
        if p["open"]:
            line = gate(f"{p['label']}: " + (count_he(p['open'], 'משימה', 'משימות', 'f') if he
                                              else _count_en(p['open'], 'task')), view["lang"], 80)
            if line:
                work_lines.append(line)
    sections = []
    if "done_today" in view["sections"] and done_lines:
        sections.append({"head": "✅ " + ("הושלם היום" if he else "Done today"), "tone": "ok", "lines": done_lines[:8]})
    if "in_work" in view["sections"] and work_lines:
        sections.append({"head": "🛠 " + ("בעבודה עכשיו" if he else "In progress now"), "lines": work_lines[:8]})
    if not sections:
        sections.append({"head": "📻 " + view["title"],
                         "lines": ["שלום לכל הצוות" if he else "Hello to the whole team",
                                   "העדכונים יופיעו כאן" if he else "Updates will appear here"]})
    return sections


def card_ok(sections: List[dict], lang: str) -> bool:
    """Every head and line of a staff card is a fixed mark plus a gated line (the card's own last check)."""
    for s in sections:
        for text in [s.get("head", "")] + list(s.get("lines") or []):
            body = text
            for mark in _CARD_MARKS:
                if body.startswith(mark):
                    body = body[len(mark):]
                    break
            if not gate(body, lang, 120):
                return False
    return True


def decide(view: dict, last: dict, now: float, every: float, news_gap: float = 180.0) -> Optional[str]:
    """"start" on the first look, "news" when a listed project finished something (not more often than
    news_gap), "periodic" every `every` seconds (a station id, so the stream is never silent for long)."""
    at = last.get("at") if isinstance(last.get("at"), (int, float)) else None
    if at is None:
        return "start"
    if has_news(view) and now - at >= news_gap:
        return "news"
    return "periodic" if now - at >= every else None


def remember(view: dict, now: float, last: Optional[dict] = None) -> dict:
    told = list((last or {}).get("done") or [])
    told += [d["key"] for p in view["projects"] for d in p["done"] if d["fresh"]]
    return {"at": int(now), "done": list(dict.fromkeys(told))[-MAX_TOLD:]}


def render_inputs(view: dict) -> Tuple[str, List[dict]]:
    """(card title, sections) for radio_card.render; the foot is always empty (no quotas, no accounts)."""
    sections = card_sections(view)
    if not card_ok(sections, view["lang"]):
        sections = [{"head": "📻 " + view["title"], "lines": []}]
    return "📻 " + view["title"], sections
