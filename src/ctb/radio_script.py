"""What the radio says: a snapshot of the team's state, change detection, and a short Hebrew (or English) script.

Pure functions over plain data, so they are easy to test. The snapshot comes from the same readers the control
panel uses (decisions, reminders, sessions, quota), the sessions that wait on an open question, an optional
owner's ledger folder (one JSON file per project, items with status/wait/updated), and an optional team-quota
command. The script is a template built from that data; numbers are written as words so the voice reads them
naturally.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

HEBREW = re.compile(r"[֐-׿]")
LATIN = re.compile(r"[A-Za-z]")
MAX_ITEMS = 3           # items read aloud per update
MAX_SCRIPT = 600        # about 45 seconds of Hebrew speech
QUOTA_WARN = 80         # percent from which a quota window is worth saying
DONE_WINDOW = 24 * 3600
FRESH_WINDOW = 6 * 3600  # an item that appeared (or was finished) within this many seconds is news; older ones are backlog
CLOCK_SKEW = 300         # seconds: an item stamped this far in the future is still taken as now; beyond it, bogus
SPARE = 1                # extra fresh candidates prepared beyond the MAX_ITEMS read (MAX_ITEMS + SPARE = the pool size)
ATTENTION_PARTS = ("asking", "decisions", "ledger", "reminders")

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def private_speech(text: str, secrets=()) -> str:
    """Remove email addresses and known account identifiers from anything sent to the voice."""
    text = _EMAIL.sub("חשבון", str(text))
    for value in sorted(set(filter(None, secrets)), key=len, reverse=True):
        text = re.sub(re.escape(value), "חשבון", text, flags=re.I)
    return text.replace("@", "")

# --- numbers in words ------------------------------------------------------------------------

from .i18n import RADIO_UNITS as _UNITS
from .i18n import RADIO_TEENS as _TEENS
from .i18n import RADIO_TENS as _TENS
from .i18n import RADIO_HUNDREDS as _HUNDREDS


def _and(word: str) -> str:
    return "ו" + word


def number_he(n: int, gender: str = "m") -> str:
    """0..999 in Hebrew words (masculine "m" or feminine "f"); larger numbers stay digits."""
    if n < 0 or n > 999:
        return str(n)
    if n == 0:
        return "אפס"
    parts = []
    h, rest = divmod(n, 100)
    if h:
        parts.append(_HUNDREDS[h])
    if rest >= 20:
        t, u = divmod(rest, 10)
        parts.append(_TENS[t])
        if u:
            parts.append(_UNITS[gender][u])
    elif rest >= 10:
        parts.append(_TEENS[gender][rest - 10])
    elif rest:
        parts.append(_UNITS[gender][rest])
    if len(parts) > 1:
        parts[-1] = _and(parts[-1])
    return " ".join(parts)


def count_he(n: int, one: str, many: str, gender: str = "m") -> str:
    """'דבר אחד', 'שני דברים', 'שלושה דברים' / 'החלטה אחת', 'שתי החלטות'."""
    if n == 1:
        return f"{one} {'אחד' if gender == 'm' else 'אחת'}"
    if n == 2:
        return f"{'שני' if gender == 'm' else 'שתי'} {many}"
    return f"{number_he(n, gender)} {many}"


def clock_he(hour: int, minute: int) -> str:
    """14:30 -> 'שתיים וחצי'; minutes rounded to five."""
    minute = int(round(minute / 5.0) * 5)
    if minute == 60:
        hour, minute = hour + 1, 0
    h = hour % 12 or 12
    words = number_he(h, "f")
    if minute == 0:
        return words
    if minute == 15:
        return words + " ורבע"
    if minute == 30:
        return words + " וחצי"
    return words + " " + _and(number_he(minute, "f"))


def hebrew_enough(text: str, ratio: float = 0.6) -> bool:
    """Mostly Hebrew letters: safe to read aloud by a Hebrew voice."""
    he, la = len(HEBREW.findall(text)), len(LATIN.findall(text))
    return he > 0 and he / max(1, he + la) >= ratio


def isolate(text: str) -> str:
    """Wrap Latin/number runs in first-strong isolates, so a right-to-left card keeps '#1436?' in order."""
    return re.sub(r"[A-Za-z0-9#/@][A-Za-z0-9#/@._:%+\-]*", lambda m: "⁨" + m.group(0) + "⁩", text)


_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
_INITIAL = re.compile(r"(?:^|\s)\w\.$")  # "ד. כהן": a one-letter initial, not a sentence end
_CLAUSE_END = re.compile(r"[,;:\u2013\u2014](?=\s)")


def _sentence_ends(text: str, limit: int):
    """Where whole sentences end within the first `limit` characters, judged on the whole text (so "3.5" or a
    cut that lands right after a dot is not taken for a sentence end)."""
    return [m.end() for m in _SENTENCE_END.finditer(text) if m.end() <= limit and not _INITIAL.search(text[:m.end()])]


def short(text: str, limit: int = 90) -> str:
    """At most `limit` characters, cut where the voice can stop: after the last whole sentence that fits, else
    at the last clause break, else at a word (the owner heard lines cut off mid-sentence). "…" marks only a
    cut inside a sentence."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    if limit < 2:
        return ""
    ends = _sentence_ends(text, limit)
    if ends and ends[-1] >= limit * 0.4:
        return text[:ends[-1]].rstrip()
    room = text[:limit - 1]
    breaks = [m.start() for m in _CLAUSE_END.finditer(room)]
    if breaks and breaks[-1] >= limit * 0.5:
        return room[:breaks[-1]].rstrip() + "…"
    cut = room.rsplit(" ", 1)[0] if " " in room else room
    return cut.rstrip(" ,;:") + "…"


def say_short(text: str, limit: int = 110) -> str:
    """A line for the voice, never stopped inside a sentence when a sentence can be kept whole: the whole
    sentences that fit (however short), else the first sentence whole while it is at most twice the limit,
    else `short` (a pause at a clause or word)."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    ends = _sentence_ends(text, limit)
    if ends:
        return text[:ends[-1]].rstrip()
    first = _sentence_ends(text, 2 * limit)
    if first:
        return text[:first[0]].rstrip()
    return short(text, limit)


def whole(text: str, limit: int) -> str:
    """At most `limit` characters (never more), ended at a whole sentence: the last one that fits, however short.
    Text with no sentence end inside the limit is cut by `short` (at a clause or a word, marked with an
    ellipsis), never kept whole past the limit. For caps on a full script or bulletin, where a plain `[:limit]`
    stops the voice in the middle of a word."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    ends = _sentence_ends(text, limit)
    return text[:ends[-1]].rstrip() if ends else short(text, limit)


# What the voice is given. It reads the text verbatim, so a symbol or a Latin token that the Hebrew voice cannot
# say is dropped or put into words first (the card keeps the original text).
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
_PAUSE_SYMBOLS = re.compile(r"\s*[\u2192\u2190\u21d2\u279c\u00b7\u2022\u2500\u2013\u2014]\s*")
_SPOKEN_NAMES = {"claude": "קלוד", "codex": "קודקס", "gemini": "ג'מיני", "opus": "אופוס", "sonnet": "סונט",
                 "haiku": "הייקו", "chatgpt": "צ'אט ג'י פי טי", "gpt": "ג'י פי טי", "telegram": "טלגרם",
                 "github": "גיטהאב", "youtube": "יוטיוב", "whatsapp": "וואטסאפ"}
_SPOKEN_LETTERS = {"PR": "פי אר", "CI": "סי איי", "API": "איי פי איי", "AI": "איי איי", "UI": "יו איי",
                   "AGY": "איי ג'י וואי", "OK": "אוקיי"}
_EXT = r"(?:py|js|ts|tsx|jsx|md|json|txt|sh|yml|yaml|toml|swift|html|css|log|pcm|wav|mp3|flv|png|jpg)"
_UNSAYABLE = re.compile(
    r"(?:(?<!\S)--?[A-Za-z][\w-]*"                                       # a command-line flag
    r"|(?<!\S)(?:~|\.{0,2})/[\w.~-]+(?:/[\w.~-]+)*/?"                      # a path from / ~ ./ or ../
    r"|[A-Za-z]:\\\S+"                                                   # a Windows path
    r"|\b(?:[\w.-]+/)*[\w-]+\." + _EXT + r"\b"                              # a file name, with its folders
    r"|\b(?=[\w./-]*[A-Za-z])[\w.-]+(?:/[\w.-]+){2,}"                      # three or more segments with letters
    r"|\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b"                         # snake_case identifier
    r"|\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b)")             # a hash
_MONTHS = ("ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר")
_FRACTIONS = {"1/2": "חצי", "1/3": "שליש", "1/4": "רבע", "2/3": "שני שלישים", "3/4": "שלושה רבעים"}
_DATE = re.compile(r"(?<![\w/.])(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})(?![\w/])")
_ISO_DATE = re.compile(r"(?<![\w-])(\d{4})-(\d{2})-(\d{2})(?![\w-])")
_RATIO = re.compile(r"(?<![\w/.])(\d{1,4})/(\d{1,4})(?![\w/])")


def _date_words(day: int, month: int, year: str) -> Optional[str]:
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return None
    return f"{day} ב{_MONTHS[month - 1]} {year}"


def strip_invisible(text: str) -> str:
    """Remove zero-width, bidi and other invisible format characters (they can hide inside a label or a token)."""
    return _INVISIBLE.sub("", str(text))


def _say_numbers(text: str) -> str:
    """A full date (with a year, as d/m/y or y-m-d) in words, day first: "12/11/2026" -> "12 בנובמבר 2026". A bare a/b
    has no year, so it is never a date ("24/7" and "10/10" stay as they are): a < b is a fraction ("3/5" ->
    "3 מתוך 5", with 1/2, 1/3, 1/4, 2/3 and 3/4 as words); anything else stays as digits."""
    text = _ISO_DATE.sub(lambda m: _date_words(int(m.group(3)), int(m.group(2)), m.group(1)) or m.group(0), text)
    text = _DATE.sub(lambda m: _date_words(int(m.group(1)), int(m.group(2)), m.group(3)) or m.group(0), text)

    def ratio(m: re.Match) -> str:
        if m.group(0) in _FRACTIONS:
            return _FRACTIONS[m.group(0)]
        return f"{m.group(1)} מתוך {m.group(2)}" if int(m.group(1)) < int(m.group(2)) else m.group(0)
    return _RATIO.sub(ratio, text)


def for_voice(text: str) -> str:
    """The text as the Hebrew voice should read it: no bidi or zero-width marks, no ellipsis, arrows, dashes or
    bullets (a comma pause instead), no "=", "~", "@" or "$" (money becomes words), no file names, flags,
    paths or hashes, and the usual product names and initialisms as Hebrew words. Other plain Latin words stay
    (the voice reads them in English)."""
    text = strip_invisible(text)
    text = text.replace("\u2026", ". ").replace("...", ". ")
    text = _say_numbers(text)
    text = _UNSAYABLE.sub(" ", text)
    text = text.replace("ו/או", "או")
    text = re.sub(r"(?<=[^\W\d])/(?=[^\W\d])", " או ", text)  # an alternative: "כן/לא" is "כן או לא"
    text = re.sub(r"(?<!\d)/|/(?!\d)", " ", text)  # any other slash is a pause; one between digits stays
    text = re.sub(r"\$\s*(\d[\d.,]*)", r"\1 דולר", text)
    text = re.sub(r"~\s*(?=\d)", "בערך ", text)
    text = re.sub(r"\s\+\s", " ועוד ", text)
    text = re.sub(r"[=@~$+]", " ", text)
    text = _PAUSE_SYMBOLS.sub(", ", text)
    text = re.sub(r"\b([A-Za-z][A-Za-z]*)\b", lambda m: _SPOKEN_LETTERS.get(m.group(1)) or
                  _SPOKEN_NAMES.get(m.group(1).lower(), m.group(1)), text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,.;:])(?:\s*[,.;:])+", r"\1", text)
    return " ".join(text.split()).strip(" ,;:")


def spoken(text: str) -> str:
    """A line fit for the voice: no emoji, no hashes, no URLs, no brackets."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️‍‎‏⁨⁩]", "", text)
    text = re.sub(r"[#*_`\[\]{}<>|]", " ", text)
    return " ".join(text.split()).strip(" -:·")


# --- the snapshot ----------------------------------------------------------------------------

def ledger_items(folder: Optional[Path], now: float) -> Tuple[List[dict], List[dict], int]:
    """(open items waiting on the owner, items done in the last day, other open items) across all projects."""
    waiting, done, team = [], [], 0
    if not folder or not folder.is_dir():
        return waiting, done, team
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items = data.get("items") if isinstance(data, dict) else None
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict) or not isinstance(it.get("text"), str):
                continue
            updated = it.get("updated") if isinstance(it.get("updated"), (int, float)) else 0
            row = {"key": f"{path.stem}#{it.get('id')}", "project": path.stem, "text": it["text"],
                   "updated": updated}
            if it.get("status") == "open":
                if it.get("wait") == "owner":
                    waiting.append(row)
                else:
                    team += 1
            elif it.get("status") == "done" and now - updated <= DONE_WINDOW:
                done.append(row)
    waiting.sort(key=lambda r: -r["updated"])
    done.sort(key=lambda r: -r["updated"])
    return waiting, done, team


def ledger_open_by(folder: Optional[Path]) -> Dict[str, int]:
    """{project: open items not waiting on the owner} (the team's work in progress), for the staff edition."""
    out: Dict[str, int] = {}
    if not folder or not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items = data.get("items") if isinstance(data, dict) else None
        n = sum(1 for it in (items if isinstance(items, list) else [])
                if isinstance(it, dict) and it.get("status") == "open" and it.get("wait") != "owner")
        if n:
            out[path.stem] = n
    return out


_QUOTA_LINE = re.compile(r"^\s*([A-Za-z][\w .-]{0,30}?)\s*:\s*(.*)$")
_QUOTA_WINDOW = re.compile(r"(?:\b(5h|week)\s+)?(\d{1,3})%")
WEEKLY_ONLY = ("claude", "codex")  # their plans have no 5-hour window: only the weekly number counts


def team_quota(command: Optional[List[str]]) -> List[dict]:
    """[{label, pct, window}] parsed from a team-quota command, one row per line; [] when not configured.
    Claude and Codex lines give their weekly number only (a "5h" one is never read for them); other
    tools (AGY, OpenCode) keep their first number and its window."""
    if not command:
        return []
    try:
        out = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in out.splitlines():
        m = _QUOTA_LINE.match(line)
        if not m:
            continue
        label = m.group(1).strip()
        found = [(w or "", int(p)) for w, p in _QUOTA_WINDOW.findall(m.group(2))]
        if label.lower().split()[0] in WEEKLY_ONLY:
            found = [(w, p) for w, p in found if w == "week"] or [(w, p) for w, p in found if w == ""][:1]
            found = [("week", p) for _, p in found]
        if found:
            window, pct = found[0]
            rows.append({"label": label, "pct": pct, "window": window or "5h"})
    return rows


def _at_from_age(now: float, age) -> float:
    """When an item appeared, from its age in seconds. A missing age means now; one that is not a finite
    number, or is negative, means the item is old (time 0): never an error."""
    if age is None:
        return now
    value = _num(age)
    try:
        return now - value if value is not None and value >= 0 else 0.0
    except (OverflowError, ValueError, TypeError):
        return 0.0


def snapshot(state: dict, asking: Iterable[str], ledger: Tuple[List[dict], List[dict], int],
             quota_rows: List[dict], now: float, agents_done: Optional[List[dict]] = None,
             team_by: Optional[Dict[str, int]] = None) -> dict:
    """One plain dict of what matters, from the panel's build_state() output and the extra sources."""
    sessions = [s for s in state.get("sessions") or [] if s.get("state") != "ended"]
    asking = set(asking)
    waiting, done, team_open = ledger
    quota = []
    sensitive = []
    for q in state.get("quota") or []:
        account = str(q.get("account") or "")
        for address in _EMAIL.findall(account + " " + str(q.get("label") or "")):
            sensitive.extend([address, address.split("@", 1)[0]])
    for index, q in enumerate(state.get("quota") or []):
        for window, value in (("week", q.get("weekly")),):  # weekly only: no 5-hour window
            if isinstance(value, (int, float)):
                label = str(q.get("label") or "")
                if not label or private_speech(label, sensitive) != label:
                    label = "חשבון קלוד הראשי" if index == 0 else "חשבון קלוד הנוסף"
                quota.append({"label": label, "window": window,
                              "pct": int(round(value))})
    quota += [{"label": r["label"], "window": r.get("window") or "5h", "pct": r["pct"]} for r in quota_rows]
    return {
        "at": int(now),
        "decisions": [{"key": "d:" + d["id"], "text": d.get("title") or "", "topic": d.get("topic") or "",
                       "at": _at_from_age(now, d.get("age"))} for d in state.get("decisions") or []],
        "reminders": [{"key": "r:" + hashlib.sha256((r.get("topic", "") + r.get("text", "")).encode()).hexdigest()[:12],
                       "text": r.get("text") or "", "topic": r.get("topic") or "",
                       "at": _at_from_age(now, r.get("age"))} for r in state.get("reminders") or []],
        "asking": [{"key": "s:" + s["id"], "text": s.get("name") or "", "at": now} for s in sessions
                   if s.get("id") in asking],
        "ledger": [{"key": "l:" + r["key"], "project": r["project"], "text": r["text"], "at": r["updated"]}
                   for r in waiting],
        "done": [{"key": "x:" + r["key"], "project": r["project"], "text": r["text"], "updated": r["updated"]}
                 for r in done],
        "sessions": [{"id": s.get("id") or "", "name": s.get("name") or "", "state": s.get("state") or "",
                      "now": s.get("now") or "", "summary": s.get("summary") or "", "age": s.get("age")}
                     for s in sessions if s.get("id")],
        "private_accounts": sensitive,
        "agents_done": [dict(a) for a in agents_done or []],
        "working": sum(1 for s in sessions if s.get("state") == "working"),
        "idle": sum(1 for s in sessions if s.get("state") == "idle"),
        "team_open": team_open,
        "team_open_by": dict(team_by or {}),  # per project; only the staff edition reads it (radio_staff)
        "quota": [q for q in quota if q["pct"] >= QUOTA_WARN],
    }


def _num(value) -> Optional[float]:
    """A finite number, or None (text, None, bool, nan, inf and integers too large for a float are never a time).
    Total: it never raises."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def _now(snap: dict) -> float:
    return _num(snap.get("at")) or time.time()


def _age(item: dict, now: float, field: str = "at") -> Optional[float]:
    """Seconds since the item's time, or None when it has no usable time: missing, not a number, or in the
    future beyond CLOCK_SKEW. Never raises."""
    try:
        at = _num(item.get(field)) if isinstance(item, dict) else None
        if at is None or now - at < -CLOCK_SKEW:
            return None
        return max(0.0, now - at)
    except (OverflowError, ValueError, TypeError):
        return None


def radio_ready(raw: str, text: str) -> bool:
    """Short, clean Hebrew the voice can read as it is: at most 90 characters, no digits, Latin letters,
    brackets or ellipsis in the original text. Anything else is phrased first (radio.localize)."""
    return len(text) <= 90 and not re.search(r"[\d()\[\]{}<>…A-Za-z]", raw or "")


_FRAGMENT = re.compile(r"[;+\u2192()\[\]{}]|#\d|\bOwner\s+\d{4}", re.I)


def plain_title(text: str) -> bool:
    """A note short and plain enough to be translated word for word and still sound like a sentence: one clause,
    at most 14 words, no list separators, brackets, ids or dates. Telegraphic
    notes ("a + b; c (see #12)") are not: a literal translation of those is word salad, so they are phrased as a
    whole line or not read."""
    text = " ".join(str(text or "").split())
    return (0 < len(text) <= 120 and len(text.split()) <= 14 and not _FRAGMENT.search(text)
            and text.count(",") <= 1 and text.count(":") <= 1)


def done_fallback(item: dict, names: Dict[str, str], lang: str = "he") -> Optional[str]:
    """What is said about a finished item whose own words cannot be read (no whole Hebrew line): a task of the
    project, by its Hebrew name (the caller's "done" lead-in says it finished). None without a sayable name."""
    where = _name(item["project"], names) if item.get("project") else ""
    where = spoken(where)
    if not where or (lang == "he" and not hebrew_enough(where)):
        return None
    return f"משימה בפרויקט {short(where, 40)}" if lang == "he" else f"A task in {where}"


def is_fresh(part: str, item: dict, snap: dict) -> bool:
    """A session waiting for an answer right now is always fresh; any other waiting item is fresh while its
    age is within FRESH_WINDOW. An item with no usable time is old: it is never read by guess."""
    if part == "asking":
        return True
    age = _age(item, _now(snap))
    return age is not None and age <= FRESH_WINDOW


def _sort_at(item: dict) -> float:
    return _num(item.get("at")) or 0.0


def waiting_split(snap: dict) -> Tuple[List[Tuple[str, dict]], int]:
    """([(part, item)] fresh, newest first; the number of older items still waiting). The backlog is only counted."""
    fresh, old = [], 0
    for part in ATTENTION_PARTS:
        for item in snap.get(part) or []:
            if is_fresh(part, item, snap):
                fresh.append((part, item))
            else:
                old += 1
    fresh.sort(key=lambda pi: -_sort_at(pi[1]))
    return fresh, old


def recent_done(snap: dict, last: Optional[dict] = None) -> List[dict]:
    """Items finished within FRESH_WINDOW and not yet told (`last["done"]`), whatever `last` says: an item
    finished longer ago is never read or counted. The one filter for news(), build_news(), build_script(),
    the rewriter's facts and the digest."""
    now, told = _now(snap), set((last or {}).get("done") or [])
    out = [i for i in snap.get("done") or [] if i["key"] not in told
           and (_age(i, now, "updated") is not None and _age(i, now, "updated") <= FRESH_WINDOW)]
    return sorted(out, key=lambda i: -(_num(i.get("updated")) or 0.0))


def fresh_facts(snap: dict) -> dict:
    """The facts an external rewriter may use: fresh waiting items and recent done ones, the rest only counted."""
    fresh, old = waiting_split(snap)
    return {**{part: [i for p, i in fresh if p == part] for part in ("decisions", "asking", "ledger")},
            "done": recent_done(snap), "older_waiting_count": old, "working": snap.get("working", 0),
            "idle": snap.get("idle", 0)}


def attention_keys(snap: dict) -> List[str]:
    """Keys of the fresh waiting items only: old backlog never counts as something new."""
    return sorted(i["key"] for _, i in waiting_split(snap)[0])


def told_keys(snap: dict, last: Optional[dict] = None) -> List[str]:
    """What the radio has announced or has to announce: the fresh keys, plus keys it announced before that still
    wait (even after they aged out of the window). Time passing alone never changes this set."""
    present = {i["key"] for part in ATTENTION_PARTS for i in snap.get(part) or []}
    return sorted(set(attention_keys(snap)) | (set((last or {}).get("attention") or []) & present))


def digest(snap: dict, last: Optional[dict] = None) -> str:
    """Stable fingerprint of what the radio would talk about (not of its wording or the clock): the told or
    to-be-told waiting keys (told_keys), the recent done items not yet announced, the working count and the
    quota bands. An item that merely ages out of the window, or an old backlog item that comes or goes, does
    not change it."""
    facts = {"attention": told_keys(snap, last), "done": sorted(i["key"] for i in recent_done(snap, last)),
             "working": snap.get("working", 0), "quota": sorted(f"{q['label']}/{q['window']}/{q['pct'] // 5}"
                                                                for q in snap.get("quota") or [])}
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()[:16]


def _bands(snap: dict) -> List[str]:
    return sorted(f"{q['label']}/{q['window']}/{q['pct'] // 5}" for q in snap.get("quota") or [])


def news(snap: dict, last: dict) -> List[Tuple[str, dict]]:
    """What happened since the last spoken update, besides new things waiting for the owner (those are
    'urgent'): ("done", item) for a task-list item finished, ("finished", session) for a session that was
    working and now waits, ("agent", record) for a background agent that ended, ("quota", row) for a quota
    window that crossed into a new 5 % band above QUOTA_WARN. Empty before the first update."""
    if not last:
        return []
    out: List[Tuple[str, dict]] = []
    out += [("done", i) for i in recent_done(snap, last)]
    was_working = set(last.get("working_ids") or [])
    if not snap.get("replies_spoken"):  # with every reply said on the air (radio_talk), never twice
        out += [("finished", s) for s in snap.get("sessions") or []
                if s["id"] in was_working and s["state"] == "idle"]
    since, said = float(last.get("at") or 0), set(last.get("agents") or [])
    out += [("agent", a) for a in snap.get("agents_done") or [] if a["key"] not in said and float(a["at"]) > since]
    bands = set(last.get("quota_bands") or [])
    out += [("quota", q) for q in snap.get("quota") or []
            if f"{q['label']}/{q['window']}/{q['pct'] // 5}" not in bands]
    return out


def decide(snap: dict, last: dict, now: float, every: float, news_gap: Optional[float] = None) -> Optional[str]:
    """'urgent' when something fresh and new waits for the owner (old backlog never is); 'news' when something else happened (news()) and at
    least `news_gap` seconds passed since the last update (None: no short updates, e.g. the paid minutes run
    low); 'periodic' when `every` seconds passed and the facts changed; otherwise None (the radio stays quiet)."""
    said = set(last.get("attention") or [])
    if any(k not in said for k in attention_keys(snap)):
        return "urgent"
    since = now - float(last.get("at") or 0)
    if news_gap is not None and since >= news_gap and news(snap, last):
        return "news"
    if since >= every and digest(snap, last) != last.get("digest"):
        return "periodic"
    return None


def remember(snap: dict, now: float, last: Optional[dict] = None) -> dict:
    """What the radio keeps after an update. `last`: the previous record, so that an item announced before and
    still waiting stays "told" after it ages out of the window."""
    new = {"at": now, "attention": told_keys(snap, last),
           "done": sorted(i["key"] for i in snap.get("done") or []),
           "working_ids": sorted(s["id"] for s in snap.get("sessions") or [] if s["state"] == "working"),
           "agents": sorted(a["key"] for a in snap.get("agents_done") or []),
           "quota_bands": _bands(snap)}
    new["digest"] = digest(snap, new)
    return new


def build_news(snap: dict, last: dict, names: Dict[str, str], lang: str = "he", report: Optional[dict] = None) -> str:
    """A short spoken line for news(): a few sentences, never the full rundown. `report` (a dict the caller
    owns) is filled with the same facts as plain lines, for the alerts channel."""
    he = lang == "he"
    items = news(snap, last)
    rep = report if report is not None else {}
    rep.update({"kind": "news", "team": [], "done": [], "quota": []})
    out = ["עדכון קצר." if he else "A short update."]
    finished = [s for k, s in items if k == "finished"]
    if len(finished) == 1:
        label = spoken(finished[0]["name"])
        line = (f"הסשן {short(label, 60)} סיים לעבוד וממתין." if he and hebrew_enough(label) else
                "סשן אחד סיים לעבוד וממתין." if he else f"Session {label} finished and waits.")
        out.append(line)
        rep["team"].append(line)
    elif finished:
        line = (f"{count_he(len(finished), 'סשן', 'סשנים')} סיימו לעבוד וממתינים." if he else
                f"{len(finished)} sessions finished and wait.")
        out.append(line)
        rep["team"].append(line)
    agents = [a for k, a in items if k == "agent"]
    if agents:
        line = (("סוכן רקע אחד סיים." if len(agents) == 1 else
                 f"{count_he(len(agents), 'סוכן רקע', 'סוכני רקע')} סיימו.") if he else
                f"{len(agents)} background agent(s) finished.")
        out.append(line)
        rep["team"].append(line)
    done = [i for k, i in items if k == "done"]
    lines = [line for line in (_item_line(i, names, lang) or done_fallback(i, names, lang) for i in done[:2]) if line]
    for line in lines:
        out.append(("הושלם: " if he else "Done: ") + _sentence(line))
    rep["done_count"], rep["done"] = len(done), [_sentence(line) for line in lines]
    if done and not lines:  # titles that cannot be said (English in the Hebrew voice): their count, never
        n = len(done)       # a bare greeting that also marks them as told (review of #151)
        out.append(f"{count_he(n, 'פריט', 'פריטים')} {'הושלם' if n == 1 else 'הושלמו'} ברשימת המשימות." if he else
                   f"{n} task list item{'' if n == 1 else 's'} done.")
    elif len(done) > len(lines):  # some were read: the others are counted aloud before they count as told
        n = len(done) - len(lines)
        out.append(("ועוד אחד הושלם." if n == 1 else f"ועוד {number_he(n)} הושלמו.") if he else
                   f"And {n} more done.")
    for q in [q for k, q in items if k == "quota"][:1]:
        line = _quota_line(q, he)
        out.append(line)
        rep["quota"].append(line)
    return whole(private_speech(" ".join(out), snap.get("private_accounts") or []), MAX_SCRIPT)


def _quota_line(q: dict, he: bool) -> str:
    safe_label = private_speech(q.get("label") or "חשבון")
    label = _SPOKEN_LABELS.get(safe_label.lower(), safe_label) if he else safe_label
    which = f"המכסה של {label} לחמש השעות" if q["window"] == "5h" else f"המכסה השבועית של {label}"
    return (f"שים לב: {which} הגיעה ל{number_he(q['pct'])} אחוז." if he else
            f"Note: {label} quota ({q['window']}) is at {q['pct']}%.")


# --- the words -------------------------------------------------------------------------------

def _name(project: str, names: Dict[str, str]) -> str:
    return names.get(project) or project


def project_label(item: dict, names: Dict[str, str]) -> str:
    """The Hebrew project name to put before an item's line, or "" (no name, or one the Hebrew voice cannot say)."""
    where = _name(item["project"], names) if item.get("project") else spoken(item.get("topic") or "")
    return where if where and hebrew_enough(where) else ""


def _item_line(item: dict, names: Dict[str, str], lang: str) -> Optional[str]:
    text = spoken(item.get("say") or item.get("text") or "")  # "say": a Hebrew text the radio prepared (translation)
    if not text or (lang == "he" and not hebrew_enough(text)):
        return None
    text = say_short(text, 110)
    where = _name(item["project"], names) if item.get("project") else spoken(item.get("topic") or "")
    if where and lang == "he" and not hebrew_enough(where):
        where = ""
    return f"{where}: {text}" if where else text


def _backlog(n: int, he: bool) -> str:
    """The one sentence about the older things that wait: only how many, never what."""
    if he:
        return (f"חוץ מזה מחכה בקוקפיט {count_he(n, 'דבר', 'דברים')} ישן יותר." if n == 1 else
                f"חוץ מזה מחכים בקוקפיט {count_he(n, 'דבר', 'דברים')} ישנים יותר.")
    return f"Besides that, {n} older item{'' if n == 1 else 's'} still wait{'s' if n == 1 else ''} in the cockpit."


def _more(n: int, he: bool) -> str:
    """'ועוד שלושה חדשים.': the fresh items not read in this update (beyond the slots, or unsayable)."""
    if he:
        return "ועוד אחד חדש." if n == 1 else f"ועוד {number_he(n)} חדשים."
    return f"And {n} more new."


def _new_waiting(snap: dict, last: dict) -> List[Tuple[str, dict]]:
    said = set(last.get("attention") or [])
    return [(p, i) for p, i in waiting_split(snap)[0] if i["key"] not in said]


def to_read(snap: dict, last: dict, kind: str) -> List[dict]:
    """The items this update may read aloud (at most MAX_ITEMS + SPARE new waiting ones and two done ones), for
    the caller to prepare a Hebrew text for ("say") before the script is built. `kind`: "news" or the full one."""
    items = [] if kind == "news" else [i for p, i in _new_waiting(snap, last)[:MAX_ITEMS + SPARE] if p != "asking"]
    return items + recent_done(snap, last)[:2]


def build_script(snap: dict, last: dict, names: Dict[str, str], lang: str = "he",
                 when: Optional[time.struct_time] = None, report: Optional[dict] = None) -> str:
    """The words for one update: fresh things first, one sentence for the older backlog (only at the start, or
    when nothing is fresh), progress, work and quota. Old waiting items are never read one by one, and every
    fresh item is either read or counted in the same update. `report` (a dict the caller owns) is filled with
    the same facts as plain lines (what was read, counted, done, the team, quotas), for the alerts channel."""
    when = when or time.localtime(_now(snap))
    rep = report if report is not None else {}
    rep.update({"kind": "rundown", "clock": f"{when.tm_hour}:{when.tm_min:02d}", "status": "", "new": [], "more": 0,
                "done": [], "done_count": 0, "team": [], "quota": [], "backlog": ""})
    said = set(last.get("attention") or [])
    he = lang == "he"
    out: List[str] = []
    out.append(f"רדיו הצוות, השעה {clock_he(when.tm_hour, when.tm_min)}." if he else
               f"Team radio, {when.tm_hour}:{when.tm_min:02d}.")

    attention, old = waiting_split(snap)
    new = [(p, i) for p, i in attention if i["key"] not in said]
    if attention:
        n, m = len(attention), len(new)
        phrase = count_he(n if m < n else m, "דבר", "דברים")
        if not m:
            out.append(("עדיין מחכה לך דבר אחד." if n == 1 else f"עדיין מחכים לך {phrase}.") if he else
                       f"{n} thing{'' if n == 1 else 's'} still wait{'s' if n == 1 else ''} for you.")
            rep["status"] = out[-1]
        elif m == n:
            out.append(("יש דבר אחד חדש שמחכה לך." if n == 1 else f"יש {phrase} חדשים שמחכים לך.") if he else
                       f"{n} new thing{'' if n == 1 else 's'} wait{'s' if n == 1 else ''} for you.")
        else:
            out.append(("יש דבר אחד שמחכה לך." if n == 1 else f"יש {phrase} שמחכים לך.") if he else
                        f"{n} thing{'' if n == 1 else 's'} wait{'s' if n == 1 else ''} for you.")
        prefix = (("חדש: " if m == 1 else "החדשים: ") if he else "New: ") if said and m and m < n else ""
        told = 0
        for part, item in (new if new else attention):
            if told >= MAX_ITEMS:
                break
            line = _item_line(item, names, lang)
            if part == "asking":
                label = spoken(item["text"])
                line = (f"הסשן {label} מחכה לתשובה שלך" if he and hebrew_enough(label) else
                        "סשן אחד מחכה לתשובה שלך" if he else f"session {label} waits for your answer")
            elif part == "decisions" and line:
                line = ("החלטה בקוקפיט, " if he else "a decision in the cockpit: ") + line
            if line:
                out.append(("" if told else prefix) + _sentence(line))
                rep["new"].append(_sentence(line))
                told += 1
        rep["more"] = max(0, m - told)
        if m and not told:  # nothing could be read: say so, once, for all the new ones
            out.append(("את החדש אי אפשר להקריא בעברית." if m == 1 else "את החדשים אי אפשר להקריא בעברית.") if he else
                       "The new one cannot be read aloud." if m == 1 else "The new ones cannot be read aloud.")
        elif m > told:  # the slots are full or the rest cannot be read: counted, never dropped
            out.append(_more(m - told, he))
        if old and not last:  # a start: one sentence about the backlog
            out.append(_backlog(old, he))
            rep["backlog"] = out[-1]
    elif old:
        out.append("אין שום דבר חדש שמחכה לך." if he else "Nothing new waits for you.")
        rep["status"] = out[-1]
        out.append(_backlog(old, he))
        rep["backlog"] = out[-1]
    else:
        out.append("כרגע שום דבר לא מחכה לך." if he else "Nothing waits for you right now.")
        rep["status"] = out[-1]

    active = [s for s in snap.get("sessions") or [] if s.get("state") == "working"]
    active.sort(key=lambda s: _num(s.get("age")) if _num(s.get("age")) is not None else float("inf"))
    work_lines = []
    for session in active[:4]:
        name = say_short(spoken(session.get("name") or ""), 55)
        candidates = [spoken(session.get(key) or "") for key in ("now", "summary")]
        detail = next((value for value in candidates if value and (not he or hebrew_enough(value))), "")
        detail = say_short(detail, 70) if detail else ("העבודה נמשכת" if he else "work continues")
        if name:
            work_lines.append(_sentence(f"{name}: {detail}"))

    # done: only what was finished within FRESH_WINDOW and not told before (one filter: recent_done). The real
    # count is said, at most two titles are read.
    fresh = recent_done(snap, last)
    if fresh:
        since = ("מאז העדכון הקודם" if last else "בשעות האחרונות") if he else ""
        out.append((f"{since} הושלמו {count_he(len(fresh), 'דבר', 'דברים')}." if len(fresh) > 1 else
                    f"{since} הושלם דבר אחד.") if he else f"{len(fresh)} item(s) done.")
        rep["done_count"] = len(fresh)
        for item in fresh[:2]:
            line = _item_line(item, names, lang) or done_fallback(item, names, lang)
            if line:
                out.append(_sentence(line))
                rep["done"].append(out[-1])

    if last:
        for session in (s for s in snap.get("sessions") or []
                        if s.get("id") in set(last.get("working_ids") or []) and s.get("state") == "idle"):
            summary = spoken(session.get("summary") or "")
            if summary and (not he or hebrew_enough(summary)):
                label = say_short(spoken(session.get("name") or ""), 45)
                line = _sentence(("מאז העדכון הקודם, " if he else "Since the last update, ") +
                                 (f"{label}: " if label else "") + say_short(summary, 90))
                out.append(line)
                rep["done"].append(line)
                break

    w, idle = snap.get("working", 0), snap.get("idle", 0)
    if w or idle:
        out.append(((f"עכשיו עובדים {count_he(w, 'סשן', 'סשנים')}" if w != 1 else "עכשיו עובד סשן אחד")
                    if w else "אף סשן לא עובד עכשיו") +
                   (f", ו{number_he(idle)} ממתינים." if idle > 1 else ", ואחד ממתין." if idle else ".") if he else
                   f"{w} sessions working, {idle} idle.")
        rep["team"].append(out[-1])

    out.extend(work_lines)
    rep["team"].extend(work_lines)

    for q in (snap.get("quota") or [])[:2]:
        out.append(_quota_line(q, he))
        rep["quota"].append(out[-1])

    out.append("זה הכול לעכשיו." if he else "That's all for now.")
    script = " ".join(out)
    # Keep waiting titles, milestones and the newest work ahead of optional closing and quota details.
    optional = list(reversed(rep["quota"])) + [out[-1], rep.get("backlog", "")]
    optional += [line for line in rep["team"] if line not in work_lines]
    optional += list(reversed(rep["new"][2:])) + list(reversed(work_lines))
    for line in optional:
        if len(script) <= MAX_SCRIPT:
            break
        if line and line in out:
            out.remove(line)
            script = " ".join(out)
    while len(script) > MAX_SCRIPT and len(out) > 3:
        out.pop()
        script = " ".join(out)
    return whole(private_speech(script, snap.get("private_accounts") or []), MAX_SCRIPT)


def _sentence(line: str) -> str:
    line = line.rstrip(" .")
    return line if line.endswith(("?", "!")) else line + "."


from .i18n import RADIO_SPOKEN_LABELS as _SPOKEN_LABELS


def card_sections(snap: dict, names: Dict[str, str], lang: str = "he") -> List[dict]:
    """The picture's sections: what waits, what moved forward, the sessions."""
    he = lang == "he"
    attention = [(p, i) for p in ("decisions", "asking", "ledger", "reminders") for i in snap.get(p) or []]
    lines = []
    for part, item in attention[:6]:
        where = _name(item["project"], names) if item.get("project") else item.get("topic") or ""
        mark = {"decisions": "🗳 ", "asking": "❓ ", "reminders": "📌 "}.get(part, "")
        lines.append(isolate(short(mark + (f"{where}: " if where else "") + item["text"], 80)))
    sections = [{"head": (f"⏳ מחכה לך ({len(attention)})" if he else f"⏳ Waiting for you ({len(attention)})")
                 if attention else ("✅ שום דבר לא מחכה לך" if he else "✅ Nothing waits for you"),
                 "tone": "warn" if attention else "ok", "lines": lines}]
    done = snap.get("done") or []
    if done:
        sections.append({"head": "✅ הושלם היום" if he else "✅ Done today", "tone": "ok",
                         "lines": [isolate(short(f"{_name(i['project'], names)}: {i['text']}", 80)) for i in done[:3]]})
    sections.append({"head": (f"🟢 {snap.get('working', 0)} עובדים · 💤 {snap.get('idle', 0)} ממתינים" if he else
                              f"🟢 {snap.get('working', 0)} working · 💤 {snap.get('idle', 0)} idle"), "lines": []})
    return sections


def card_foot(snap: dict, quota_rows: List[dict]) -> str:
    rows = [f"{r['label']} {r['pct']}%" for r in quota_rows[:4]]
    return isolate(" · ".join(rows)) if rows else ""


# --- the card's captions ----------------------------------------------------------------------

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_GREETING = re.compile(r"^(?:רדיו הצוות, השעה [^.]*|Team radio, [^.]*|עדכון קצר|A short update)\.\s*")


def caption_chunks(text: str, limit: int = 120) -> List[str]:
    """The spoken text in caption-sized pieces: whole sentences, joined while they fit in `limit` characters."""
    out: List[str] = []
    for sentence in (p.strip() for p in _SENTENCE.split(" ".join(text.split()))):
        if not sentence:
            continue
        if out and len(out[-1]) + 1 + len(sentence) <= limit:
            out[-1] += " " + sentence
        else:
            out.append(sentence)
    return out


def chunk_at(chunks: List[str], fraction: float) -> int:
    """The chunk being said once `fraction` of the clip has played (speech time follows the characters)."""
    total = sum(len(c) for c in chunks) or 1
    done = 0
    for n, c in enumerate(chunks):
        done += len(c)
        if fraction * total < done:
            return n
    return len(chunks) - 1


def headline(text: str) -> str:
    """What the card keeps after an update: its first sentences after the station's greeting."""
    rest = _GREETING.sub("", " ".join(text.split()), count=1)
    chunks = caption_chunks(rest, 110)
    return chunks[0] if chunks else ""


def card_lines(text: str, most: int, width: int = 64) -> List[str]:
    """Wrapped lines for the card (bidi-isolated runs), at most `most`, the last one shortened."""
    lines: List[str] = []
    for word in text.split():
        if lines and len(lines[-1]) + 1 + len(word) <= width:
            lines[-1] += " " + word
        else:
            lines.append(word)
    if len(lines) > most:
        lines = lines[:most]
        lines[-1] = short(lines[-1] + " …", width)
    return [isolate(line) for line in lines]
