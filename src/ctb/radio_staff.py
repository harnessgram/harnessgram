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

Numbers are words, so a Hebrew line never mixes directions.

Structured data only. No free text from a card (its title, its step names) or from a model ever reaches the
picture, the voice or YouTube: the station never even reads it (radio_youtube keeps a card's counts only). For each
listed project staff see and hear:
- its label: only the owner's staff label for it (`radio.youtube.projects`), else a neutral "Project A" by
  position; never a key, a topic name or a session title;
- its live progress cards (the bridge's `ctb card progress`, only those in the project's own group chat): done
  and total steps and a percent each, and the project's percent;
- the day's counts of finished and open items.
Narration is fixed templates filled with those values ("בפרויקט אלפא: שישים אחוז, שלושה מתוך חמישה שלבים").

The owner-written strings (the station title and the staff labels) also pass the private deny-list (plain terms
only), the team members' names and the child-suitability list, compared on a normal form (case folded, spaces and
punctuation removed). Without both lists the station uses its built-in title.
"""
from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Tuple

from . import radio_script
from .radio_script import FRESH_WINDOW, clock_he, count_he

SECTIONS = ("station_id", "done_today", "in_work")   # the fixed list of what staff may ever hear
MAX_PROJECTS = 8
MAX_CARDS = 3             # live progress cards shown per project (the most recently updated)
NEUTRAL_HE = "אבגדהוזח"   # "פרויקט א׳": the label of a listed project the owner gave no staff label
NEUTRAL_EN = "ABCDEFGH"
MAX_TOLD = 300
from .i18n import RADIO_STAFF_TITLE_HE as TITLE_HE
TITLE_EN = "Team radio"
_ALLOWED = {
    # Hebrew letters and points, spaces, plain punctuation (incl. geresh and gershayim)
    "he": re.compile(r"[א-תְ-ׇ ,.:!?'\"׳״–\-]+"),
    "en": re.compile(r"[A-Za-z ,.:!?'\-]+"),
}
_CARD_MARKS = ("✅ ", "🛠 ", "📊 ", "📻 ")  # the card's own fixed marks, put in front of gated lines only


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
            "every_minutes": every, "lang": lang, "filters": {}}


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


# --- the owner-written strings: deny-list, names, suitability -----------------------------------------------

# niqqud and cantillation only (deleted); every other non-letter (punctuation, maqaf, underscores) is removed too
_MARKS = re.compile(r"[֑-ׇֽֿׁׂׅׄ]")
_NON_WORD = re.compile(r"[\W_]+")
_REGEX_META = re.compile(r"[\\.^$*+?{}\[\]|()]")


def flat(text: object) -> str:
    """The compared form: niqqud deleted, case folded, every space and punctuation mark removed. "Secret Project",
    "secret-project" and "S e c r e t project" all become one form."""
    return _NON_WORD.sub("", _MARKS.sub("", str(text or ""))).casefold()


_WORD_BOUNDED = re.compile(r"^\\b([^\\]+)\\b$")
_OPTIONAL_SPACE = re.compile(r" \?")


def literal_term(line: str) -> Optional[str]:
    """A deny-list line as a plain term in the compared form, or None when it is not a plain literal (any regular
    expression syntax) or has nothing left to compare: such a list is refused as a whole (fail closed)."""
    # Two regular-expression forms the shared private list has always used are still plain words in disguise:
    # a word wrapped in \b...\b, and an optional space ("a ?b"). The comparison ignores spaces and marks anyway,
    # so they reduce to the same plain term; any other expression syntax is refused (fail closed).
    line = _WORD_BOUNDED.sub(r"\1", line.strip())
    line = _OPTIONAL_SPACE.sub(" ", line)
    if _REGEX_META.search(line):
        return None
    term = flat(line)
    return term or None


def name_parts(name: object) -> List[str]:
    """A team member's name and each part of it, split on spaces, hyphens and every other mark, in the compared
    form; parts of two letters or more ("Anne-Marie Smith" -> annemariesmith, anne, marie, smith)."""
    parts = [flat(name)] + [flat(p) for p in _NON_WORD.split(str(name or ""))]
    return [p for p in dict.fromkeys(parts) if len(p) >= 2]


def filters_ready(prof: dict) -> bool:
    """Both lists are in place (the plain-term deny-list and the child-suitability rule)."""
    f = prof.get("filters") or {}
    return not f.get("broken") and f.get("deny") is not None and f.get("unsuitable") is not None


def blocked(text: str, prof: dict) -> bool:
    """A deny-list term, any part of a team member's name, or an unsuitable word, on the compared form (so case,
    spacing, punctuation and Hebrew prefix letters cannot hide it; over-blocking is the safe side)."""
    f = prof.get("filters") or {}
    flat_text = flat(text)
    for term in f.get("deny") or ():
        if term and term in flat_text:
            return True
    for name in f.get("names") or ():
        if any(part in flat_text for part in name_parts(name)):
            return True
    rx = f.get("unsuitable")
    if rx is not None:
        folded = " ".join(_NON_WORD.sub(" ", _MARKS.sub("", text)).split()).casefold()
        if any(rx.search(x) for x in (text, folded, flat_text)):
            return True
    return False


def station_title(title: object, prof: dict) -> str:
    """The station's configured name when both lists are in place and it passes the gate and `blocked`, else the
    built-in name. The same title is said, drawn, painted and sent to YouTube."""
    lang = prof.get("lang") if prof.get("lang") in ("he", "en") else "he"
    default = TITLE_HE if lang == "he" else TITLE_EN
    line = gate(title, lang, 40) if isinstance(title, str) else ""
    if not line or line == default or not filters_ready(prof) or blocked(line, prof):
        return default
    return line


# --- numbers and the neutral progress words -------------------------------------------------------------------

def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1000 else 0


def _same_day(a: float, b: float) -> bool:
    try:
        return time.localtime(a)[:3] == time.localtime(b)[:3]
    except (OverflowError, ValueError, OSError):
        return False


_EN = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]
_TENS_EN = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_TEENS_EN = ["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
             "nineteen"]


def _count_en(n: int, word: str) -> str:
    return (f"{_EN[n]} {word}{'' if n == 1 else 's'}" if n < len(_EN) else f"many {word}s")


def number_en(n: int) -> str:
    """0..999 in English words."""
    if n <= 0:
        return "zero"
    if n >= 100:
        hundreds, rest = divmod(min(n, 999), 100)
        return _EN[hundreds] + " hundred" + (" " + number_en(rest) if rest else "")
    if n < 10:
        return _EN[n]
    if n < 20:
        return _TEENS_EN[n - 10]
    t, u = divmod(n, 10)
    return _TENS_EN[t] + ("-" + _EN[u] if u else "")


def percent_words(pct: int, he: bool) -> str:
    pct = max(0, min(100, pct))
    if he:
        return "אחוז אחד" if pct == 1 else f"{radio_script.number_he(pct, 'm')} אחוז"
    return f"{number_en(pct)} percent"


def steps_words(done: int, total: int, he: bool) -> str:
    if he:
        return f"{radio_script.number_he(done, 'm')} מתוך {count_he(total, 'שלב', 'שלבים', 'm')}"
    return f"{number_en(done)} of {number_en(total)} step{'' if total == 1 else 's'}"


def neutral_label(index: int, lang: str) -> str:
    if lang == "he":
        return "פרויקט " + NEUTRAL_HE[index % len(NEUTRAL_HE)] + "׳"
    return "Project " + NEUTRAL_EN[index % len(NEUTRAL_EN)]


def _in(label: str, he: bool) -> str:
    """ "בפרויקט אלפא", and for a neutral label "בפרויקט א׳" (never "בפרויקט פרויקט")."""
    if not he:
        return f"In {label}"
    return "ב" + label if label.startswith("פרויקט ") else f"בפרויקט {label}"


# --- the view: allowlisted facts only --------------------------------------------------------------------------

def _label(name: object, index: int, prof: dict) -> str:
    """Only the owner's staff label for the project, else a neutral one: never a key, topic or session name."""
    lang = prof["lang"]
    label = gate(name, lang, 40) if isinstance(name, str) else ""
    if label and filters_ready(prof) and not blocked(label, prof):
        return label
    return neutral_label(index, lang)


def _small_int(value: object, most: int) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= most else None


def _card_numbers(card: dict) -> Optional[dict]:
    """A card's counts (done, total, percent); None for a card with nothing to count. Only numbers are read: a
    card in the staff snapshot carries no text at all (radio_youtube.StaffStation.staff_cards)."""
    total = _small_int(card.get("total"), 999) or 0
    at = _small_int(card.get("at"), 999) or 0
    pct = _small_int(card.get("percent"), 100)
    if total:
        done = min(at, total)
        # the card's own percent covers its whole plan (a long list is shown cut short): it wins when set
        pct = pct if pct is not None else round(100 * done / total)
    elif pct is not None:
        done = 0
    else:
        return None
    return {"done": min(done, 99), "total": min(total, 99), "pct": pct}


def live_cards(snap: dict, key: str) -> List[dict]:
    """The numbers of the cards shown for one listed project: the MAX_CARDS most recently updated cards that have
    something to count. Every one is drawn (radio_paint.staff) and the spoken line counts exactly these."""
    live = [c for c in snap.get("staff_cards") or [] if isinstance(c, dict) and c.get("project") == key]
    live.sort(key=lambda c: -(radio_script._num(c.get("updated")) or 0.0))
    out = []
    for c in live:
        nums = _card_numbers(c)
        if nums is not None:
            out.append(nums)
    return out[:MAX_CARDS]


def staff_view(snap: dict, prof: dict, last: Optional[dict] = None) -> dict:
    """Pass 1 and 2: the allowlisted facts only, built new (nothing from `snap` is passed through as is)."""
    now = radio_script._now(snap)
    lang = prof["lang"]
    told = set((last or {}).get("done") or [])
    sections = set(prof["sections"])
    labels = prof.get("labels") if isinstance(prof.get("labels"), dict) else prof["projects"]
    projects: List[dict] = []
    for index, key in enumerate(prof["projects"]):
        label = _label(labels.get(key), index, prof)
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
        cards = live_cards(snap, key)
        pct = round(sum(c["pct"] for c in cards) / len(cards)) if cards else 0  # each card's own percent
        complete_counts = all(c["total"] for c in cards)
        projects.append({"label": label, "done": done, "open": open_n, "cards": cards,
                         "steps_done": sum(c["done"] for c in cards) if complete_counts else 0,
                         "steps_total": sum(c["total"] for c in cards) if complete_counts else 0,
                         "pct": pct})
    return {"at": int(now), "lang": lang, "title": station_title(prof["title"], prof), "projects": projects,
            "sections": [s for s in SECTIONS if s in sections], "filters": prof.get("filters") or {}}


# --- what is said: fixed templates filled with numbers ----------------------------------------------------------

def progress_line(p: dict, he: bool) -> str:
    """The project's progress line: label, percent, steps. No free text."""
    if not p.get("cards"):
        return ""
    if p["steps_total"]:
        return f"{_in(p['label'], he)}: {percent_words(p['pct'], he)}, {steps_words(p['steps_done'], p['steps_total'], he)}"
    return f"{_in(p['label'], he)}: {percent_words(p['pct'], he)}"


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
    if he:
        out = [x.replace("בפרויקט פרויקט ", "בפרויקט ") for x in out]
    return out


def has_news(view: dict) -> bool:
    return any(d["fresh"] for p in view["projects"] for d in p["done"])


def build_script(view: dict) -> str:
    """Pass 3 included: the spoken staff update, from fixed templates only. Every sentence goes through `gate` and
    `blocked`; the station id always stays."""
    he = view["lang"] == "he"
    prof = {"lang": view["lang"], "filters": view.get("filters") or {}}
    title = station_title(view.get("title"), prof)
    when = time.localtime(view["at"])
    sentences = ([f"{title}, השעה {clock_he(when.tm_hour, when.tm_min)}. שלום לכל הצוות"] if he else
                 [f"{title}. Hello to the whole team"])
    body: List[str] = []
    for p in view["projects"]:
        line = progress_line(p, he)
        body += ([line] if line else []) + _project_sentences(p, he)
    if not body:
        body.append("אין עדכונים חדשים כרגע. המשך יום טוב" if he else "No new updates right now. Have a good day")
    out: List[str] = []
    for s in sentences + body:
        for part in s.split(". "):
            line = gate(part.rstrip(". ") + ".", view["lang"], 400)
            if line and not blocked(line, prof) and line not in out and \
                    sum(len(x) + 1 for x in out) + len(line) <= radio_script.MAX_SCRIPT:
                out.append(line)
    if not out:  # the clock or the title failed the gate: the plain station id
        out.append(TITLE_HE + "." if he else TITLE_EN + ".")
    return " ".join(out)


def card_sections(view: dict) -> List[dict]:
    """The staff card: progress per listed project, what was finished today and what is in work. Only gated lines
    built from templates and numbers."""
    he = view["lang"] == "he"
    progress_lines, done_lines, work_lines = [], [], []
    for p in view["projects"]:
        if p.get("cards"):
            line = gate(f"{p['label']}: " + percent_words(p["pct"], he) + (
                ", " + steps_words(p["steps_done"], p["steps_total"], he) if p["steps_total"] else ""),
                view["lang"], 120)
            if line:
                progress_lines.append(line)
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
    if progress_lines:
        sections.append({"head": "📊 " + ("התקדמות" if he else "Progress"), "lines": progress_lines[:8]})
    if "done_today" in view["sections"] and done_lines:
        sections.append({"head": "✅ " + ("הושלם היום" if he else "Done today"), "tone": "ok", "lines": done_lines[:8]})
    if "in_work" in view["sections"] and work_lines:
        sections.append({"head": "🛠 " + ("בעבודה עכשיו" if he else "In progress now"), "lines": work_lines[:8]})
    if not sections:
        sections.append({"head": "📻 " + station_title(view.get("title"), {"lang": view["lang"],
                                                                               "filters": view.get("filters") or {}}),
                         "lines": ["שלום לכל הצוות" if he else "Hello to the whole team",
                                   "העדכונים יופיעו כאן" if he else "Updates will appear here"]})
    return sections


def card_ok(sections: List[dict], lang: str, filters: Optional[dict] = None) -> bool:
    """Every head and line of a staff card is a fixed mark plus a gated line that is not `blocked` (the card's own
    last check, on the final form)."""
    prof = {"lang": lang, "filters": filters or {}}
    for s in sections:
        for text in [s.get("head", "")] + list(s.get("lines") or []):
            body = text
            for mark in _CARD_MARKS:
                if body.startswith(mark):
                    body = body[len(mark):]
                    break
            if not gate(body, lang, 120) or blocked(body, prof):
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
    prof = {"lang": view["lang"], "filters": view.get("filters") or {}}
    title = station_title(view.get("title"), prof)
    view = dict(view, title=title)
    sections = card_sections(view)
    if not card_ok(sections, view["lang"], view.get("filters")):
        sections = [{"head": "📻 " + title, "lines": []}]
    return "📻 " + title, sections
