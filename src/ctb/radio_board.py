"""The radio's flight board: what each active project has done, has in work and has left, and what waits on the
owner, as slides for the picture (radio_paint draws them, radio_show plays them).

Three layers, each testable on its own:
- `gather(cfg, s, now, ...)` reads the sources (owner ledger, pin-board checklists, progress cards, sessions and
  their background agents, open decisions, questions) and maps every item to a project. It only reads.
- `collect(sources, names, now)` is pure: one record per active project, merged from all sources.
- `slides(projects, now, ...)` is pure: the slide list (an overview column per project, a wall of notes per
  project, what waits for the owner), each slide a plain dict; `ticker(...)` is the running line under them.

The staff edition never reads any of this: `staff_slide(view)` takes only `radio_staff.staff_view` output
(allowlisted, gated project names and counts), so no item text can reach a staff frame.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import radio_script
from .i18n import t

BOARD_ROWS = 5            # projects in one row of columns on the overview (more: two rows)
OVERVIEW_PAGE = 8         # projects per overview page
WALL_NOTES = 3            # notes per column on a wall (then "and N more")
COMBINED_MAX = 9          # at most this many notes in all: one combined wall instead of one per project
WAITING_ROWS = 4
LANDED_WINDOW = 3600.0    # "landed in the last hour"
DONE_WINDOW = radio_script.DONE_WINDOW  # done items older than this are not shown
TEXT_CHARS = 80           # item text kept per line (the painter also truncates to the cell)
HOME = "~"                # the project key for items that belong to no project group

_HEBREW = re.compile(r"[\u0590-\u05FF]")
_LATIN = re.compile(r"[A-Za-z]")

# status: (Hebrew word, English word, colour role)
from .i18n import RADIO_BOARD_STATUS as STATUS
from .i18n import RADIO_BOARD_KINDS as KINDS


def _num(value) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value else None


CROSS_LIKE = re.compile("[+＋×✕✖✗✘✙✚✛✜✝✞✟✠†‡☦☨☩☓❌❎➕⨯⊕⊗⨁⨂]")
BIDI_MARKS = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9'’._/-]*(?:[ ,:&-]+[A-Za-z0-9][A-Za-z0-9'’._/-]*)*")
SHORT_NAME = 3            # Latin words that may stay inside a Hebrew line (isolated)


def _one_direction(line: str) -> str:
    """A line in one reading direction: in a Hebrew line (its first letter is Hebrew), a Latin run of up to
    SHORT_NAME words stays, wrapped in a bidi isolate; a longer one leaves the line (an ellipsis marks the gap).
    An English line keeps its Latin and drops the Hebrew instead. A line in one script is returned as it is."""
    if not (_HEBREW.search(line) and _LATIN.search(line)):
        return line
    if direction(line) == "ltr":  # an English line (its first letter is Latin): the Hebrew part goes
        kept = re.sub(r"[\u0590-\u05FF][\u0590-\u05FF\s'\"׳״-]*", "… ", line)
        return " ".join(kept.split()).strip(" …") or "…"

    def keep(m: "re.Match") -> str:
        run = m.group(0)
        return "\u2068" + run + "\u2069" if len(run.split()) <= SHORT_NAME else "…"
    return " ".join(LATIN_RUN.sub(keep, line).split())


def paintable(value) -> str:
    """THE choke point for the owner's board: every string that can reach a picture (item text, a translation of it,
    a project name) passes here first, before it is shortened, compared or cached. In this order:
    - a credential in an agent's note is removed whole (creds.for_outside), never drawn, stored or cached;
    - plus- and cross-like signs become a middle dot (the house rule: none of those shapes on screen);
    - the line keeps one reading direction (`_one_direction`): a short Latin name isolated, longer Latin out.
    Applying it again changes nothing (bidi marks from an earlier pass are removed first)."""
    if not isinstance(value, str) or not value.strip():
        return ""
    from .creds import for_outside
    text = BIDI_MARKS.sub("", for_outside(value, True))
    text = CROSS_LIKE.sub("·", text)
    return _one_direction(" ".join(text.split()))


def _text(value) -> str:
    """An item line for a slide: filtered, then shortened."""
    return radio_script.short(paintable(value), TEXT_CHARS) if isinstance(value, str) else ""


def project_key(name: object) -> str:
    """One spelling per project: a ledger file stem, a project group's name and a configured display name all
    fold to the same key (case and spacing ignored)."""
    return " ".join(str(name or "").split()).casefold()


# --- reading the sources ------------------------------------------------------------------------

def ledger_by_project(folder: Optional[Path]) -> Dict[str, List[dict]]:
    """{project: [{id, text, status, wait, updated}]} from the owner ledger files (<folder>/<project>.json)."""
    out: Dict[str, List[dict]] = {}
    if not folder or not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            continue
        items = data.get("items") if isinstance(data, dict) else None
        rows = []
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict) and isinstance(it.get("text"), str) and it.get("status") in ("open", "done"):
                rows.append({"id": str(it.get("id")), "text": it["text"], "status": it["status"],
                             "wait": it.get("wait") if isinstance(it.get("wait"), str) else "",
                             "updated": _num(it.get("updated")) or 0.0})
        if rows:
            out[path.stem] = rows
    return out


def agents_running(root: Path, now: float, stale: float = 6 * 3600) -> Dict[str, int]:
    """{session (first 8 characters): background agents still running}, from the agents topics' records."""
    out: Dict[str, int] = {}
    for f in root.glob("*.json") if root.is_dir() else []:
        try:
            running = json.loads(f.read_text(encoding="utf-8")).get("running") or {}
        except (OSError, ValueError, AttributeError, RecursionError):
            continue
        n = sum(1 for at in (running.values() if isinstance(running, dict) else [])
                if _num(at) is not None and 0 <= now - at < stale)
        if n:
            out[f.stem[:8]] = n
    return out


def gather(cfg, s: dict, now: float, asking: Iterable[str] = ()) -> dict:
    """Every source, read once, with each item's project already named (a key from `project_key`). Only reads:
    the board, card, decision and topic files are replaced atomically by their writers."""
    from . import board, cards, decisions, projects
    from .config import home_chat, state_dir
    from .sessions_menu import collect as session_entries
    from .topics import TopicMap
    topics = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg)
    places = topics.all_places()
    groups = {p.chat_id: p.name for p in getattr(cfg, "projects", []) or []}

    def of_chat(chat, title: str = "") -> str:
        if chat in groups:
            return project_key(groups[chat])
        found = projects.title_project(cfg, title) if title else None
        return project_key(found.name) if found else HOME

    session_project = {sid: of_chat(e.get("chat"), str(e.get("name") or "")) for sid, e in places.items()}
    entries = session_entries(topics, now=now)
    sessions = [{"id": e.session[:8], "state": e.state, "project": session_project.get(e.session, HOME)}
                for e in entries if e.state != "ended"]
    names_by_place = {(e.get("chat"), e.get("thread")): str(e.get("name") or "") for e in places.values()}
    board_items = [(of_chat(chat, names_by_place.get((chat, thread), "")), item)
                   for chat, thread, item in board.all_items()]
    progress = [(of_chat(c["chat"], names_by_place.get((c["chat"], c["thread"]), "")), c)
                for c in cards.open_cards() if c["kind"] == "progress"]
    asked = [{"project": of_chat(r["chat"], names_by_place.get((r["chat"], r["thread"]), "")), "text": r["title"],
              "at": r["created"]} for r in decisions.open_decisions()]
    short_ids = set(asking)
    questions = [{"project": session_project.get(sid, HOME), "text": str(e.get("name") or ""), "at": now}
                 for sid, e in places.items() if sid[:8] in short_ids]
    ledger_dir = Path(os.path.expanduser(s["ledger_dir"])) if s.get("ledger_dir") else None
    return {"ledger": ledger_by_project(ledger_dir), "board": board_items, "cards": progress,
            "sessions": sessions, "agents": agents_running(state_dir() / "agents-topics", now),
            "decisions": asked, "questions": questions}


# --- one record per project (pure) ------------------------------------------------------------------

def _blank(key: str) -> dict:
    return {"key": key, "done": [], "doing": [], "todo": [], "owner": [], "landed": [],
            "sessions": 0, "working": 0, "agents": 0, "last": 0.0}


def collect(sources: dict, names: Dict[str, str], now: float, hebrew: Optional[Callable[[str], str]] = None,
            lang: str = "he") -> List[dict]:
    """[{key, name, done, doing, todo, owner, landed, sessions, working, agents, status, pct}] for every project
    that has something to show, the busiest first. `names` maps a project key (or ledger stem) to its display
    name; `hebrew` may give an item's Hebrew wording (the radio's translation cache), else its own text is shown."""
    names = {project_key(k): paintable(v) for k, v in (names or {}).items() if paintable(v)}
    alias = {project_key(v): k for k, v in names.items()}   # a group named like a display name is that project
    hebrew = hebrew or (lambda text: text)
    out: Dict[str, dict] = {}

    def proj(key: str) -> dict:
        key = project_key(key) or HOME
        key = alias.get(key, key)
        return out.setdefault(key, _blank(key))

    def done(p: dict, key: str, text: str, at: float, known: bool = True) -> None:
        """`known`: the finish time is real; an unknown one (a step finished before times were kept) counts as
        done but never as a landing in the last hour."""
        text = _text(hebrew(paintable(text)))
        if not text or (known and not 0 <= now - at <= DONE_WINDOW + radio_script.CLOCK_SKEW):
            return  # an open card's step with an unknown time is still part of its checklist
        p["done"].append({"key": key, "text": text, "at": at})
        p["last"] = max(p["last"], at)
        if known and now - at <= LANDED_WINDOW:
            p["landed"].append({"key": key, "text": text, "at": at})

    def add(p: dict, part: str, text: str, **extra) -> None:
        text = _text(hebrew(paintable(text)))
        if text:
            p[part].append(dict(text=text, **extra))

    for stem, items in (sources.get("ledger") or {}).items():
        p = proj(stem)
        for it in items:
            if it["status"] == "done":
                done(p, f"l:{stem}#{it['id']}", it["text"], it["updated"])
            elif it["wait"] == "owner":
                add(p, "owner", it["text"], kind="approval", at=it["updated"])
            else:
                add(p, "todo", it["text"])
            p["last"] = max(p["last"], it["updated"] if it["status"] == "open" else 0.0)
    for key, item in sources.get("board") or []:
        p = proj(key)
        if item.get("k") == "check":
            if item.get("x"):
                done(p, f"b:{key}#{item.get('i')}", item.get("s", ""), _num(item.get("at")) or 0.0)
            else:
                add(p, "todo", item.get("s", ""))
        elif item.get("k") == "remind":
            add(p, "owner", item.get("s", ""), kind="reminder", at=_num(item.get("at")) or now)
    for key, card in sources.get("cards") or []:
        p = proj(key)
        steps, at = list(card.get("steps") or []), card.get("at") or 0
        times = list(card.get("step_at") or [])
        for n, step in enumerate(steps):
            if n < at:
                when = _num(times[n]) if n < len(times) else None
                known = bool(when and when > 0)
                done(p, f"c:{card.get('id')}#{n}", step,
                     when if known else (_num(card.get("created")) or _num(card.get("updated")) or now), known)
            elif n == at:
                add(p, "doing", step)
            else:
                add(p, "todo", step)
        p["last"] = max(p["last"], _num(card.get("updated")) or 0.0)
    for d in sources.get("decisions") or []:
        add(proj(d["project"]), "owner", d.get("text", ""), kind="decision", at=_num(d.get("at")) or now)
    for q in sources.get("questions") or []:
        add(proj(q["project"]), "owner", q.get("text", ""), kind="question", at=_num(q.get("at")) or now)
    agents = sources.get("agents") or {}
    for sess in sources.get("sessions") or []:
        p = proj(sess.get("project") or HOME)
        p["sessions"] += 1
        p["working"] += sess.get("state") == "working"
        p["agents"] += int(agents.get(sess.get("id"), 0))

    shown = []
    for key, p in out.items():
        items = len(p["done"]) + len(p["doing"]) + len(p["todo"])
        if not items and not p["owner"] and not p["working"]:
            continue  # nothing to show: an idle session alone does not make a project active
        if key == HOME and not items and not p["owner"]:
            continue
        p["done"].sort(key=lambda d: -d["at"])
        p["landed"].sort(key=lambda d: -d["at"])
        p["owner"].sort(key=lambda o: o.get("at") or 0)
        p["name"] = names.get(key) or (t("topic_project_general", lang=lang) if key == HOME else paintable(key) or "?")
        p["total"] = items
        p["pct"] = round(100 * len(p["done"]) / items) if items else 0
        p["status"] = ("owner" if p["owner"] else "go" if p["working"] and p["done"] else
                       "boarding" if p["working"] else "landed" if items and not p["doing"] and not p["todo"] else "hold")
        shown.append(p)
    shown.sort(key=lambda p: (-(p["status"] == "owner"), -p["working"], -p["last"], p["name"]))
    return shown


# --- the slides (pure) ---------------------------------------------------------------------------------

def _clock(at: float) -> str:
    return time.strftime("%H:%M", time.localtime(at))


def _age(seconds: float) -> str:
    """Waiting time as hours:minutes, at most 99:59."""
    m = max(0, int(seconds // 60))
    return f"{min(99, m // 60)}:{m % 60:02d}"


def slides(projects: List[dict], now: float, focus_index: int = 0, lang: str = "he") -> List[dict]:
    """The slide list for one rotation:
    - "overview": a column per active project (OVERVIEW_PAGE a page);
    - "wall": per project, most recently active first, three columns of notes (done / in work / next up); when
      there is little to show, ONE combined wall of every project's notes instead;
    - "waiting": what waits for the owner (only when something does).
    Every slide carries the clock, the footer's next slide and its own key (`digest`). `focus_index` is kept for
    callers that rotate the first wall; walls are listed in activity order."""
    he = lang == "he"
    clock = _clock(now)
    out: List[dict] = []
    pages = max(1, -(-len(projects) // OVERVIEW_PAGE))
    for page in range(pages):
        cols = [{"name": p["name"], "status": p["status"], "done": len(p["done"]), "total": p["total"],
                 "sessions": p["sessions"], "working": p["working"], "agents": p["agents"],
                 "waiting": len(p["owner"]), "now": p["doing"][0]["text"] if p["doing"] else ""}
                for p in projects[page * OVERVIEW_PAGE:(page + 1) * OVERVIEW_PAGE]]
        out.append({"kind": "overview", "page": page, "pages": pages, "cols": cols, "clock": clock, "lang": lang})
    notes = sum(min(len(p["done"]), 1) + len(p["doing"]) + len(p["todo"]) for p in projects)
    if projects and (len(projects) == 1 or notes > COMBINED_MAX):
        order = sorted(projects, key=lambda p: -p["last"])
        for p in order:
            out.append({"kind": "wall", "clock": clock, "lang": lang, "name": p["name"], "status": p["status"],
                        "done_n": len(p["done"]), "total": p["total"],
                        "cols": {"done": [{"text": d["text"]} for d in p["done"][:WALL_NOTES]],
                                 "doing": [{"text": d["text"]} for d in p["doing"][:WALL_NOTES]],
                                 "todo": [{"text": d["text"]} for d in p["todo"][:WALL_NOTES]]},
                        "more": {"done": max(0, len(p["done"]) - WALL_NOTES),
                                 "doing": max(0, len(p["doing"]) - WALL_NOTES),
                                 "todo": max(0, len(p["todo"]) - WALL_NOTES)},
                        "fresh": [d["text"] for d in p["landed"][:1]]})
    elif projects:  # little to show: every project's notes on one wall, each note tagged with its project
        cols = {k: [] for k in ("done", "doing", "todo")}
        for p in sorted(projects, key=lambda p: -p["last"]):
            for part in cols:
                items = p[part][:1] if part == "done" else p[part]
                cols[part] += [{"text": d["text"], "tag": p["name"]} for d in items]
        out.append({"kind": "wall", "clock": clock, "lang": lang, "name": "", "status": "",
                    "done_n": sum(len(p["done"]) for p in projects), "total": sum(p["total"] for p in projects),
                    "cols": {k: v[:WALL_NOTES] for k, v in cols.items()},
                    "more": {k: max(0, len(v) - WALL_NOTES) for k, v in cols.items()},
                    "fresh": [d["text"] for p in projects for d in p["landed"][:1]][:1]})
    waiting = sorted(((p["name"], o) for p in projects for o in p["owner"]), key=lambda x: x[1].get("at") or now)
    if waiting:
        out.append({"kind": "waiting", "clock": clock, "lang": lang, "more": max(0, len(waiting) - WAITING_ROWS),
                    "rows": [{"project": name, "kind": KINDS[o["kind"]][0 if he else 1], "text": o["text"],
                              "age": _age(now - (o.get("at") or now))} for name, o in waiting[:WAITING_ROWS]]})
    for n, sl in enumerate(out):  # the footer's "next on the board" (the rotation wraps to the first slide)
        nxt = out[(n + 1) % len(out)]
        sl["next"] = {"kind": nxt["kind"], "name": nxt.get("name", ""), "page": nxt.get("page", 0)}
    for sl in out:
        sl["key"] = digest(sl)
    return out


def ticker(projects: List[dict], now: float, lang: str = "he") -> List[dict]:
    """The running line under every slide, in full (the show walks through it a window at a time): what waits for
    the owner first, oldest first, then what landed in the last hour, newest first. Each entry keeps its parts
    apart (kind, time, project, text), so no drawn text mixes scripts. A waiting item carries no age (it would
    change every minute; the desk slide shows it)."""
    he = lang == "he"
    waits = sorted(((p["name"], o) for p in projects for o in p["owner"]), key=lambda x: x[1].get("at") or now)
    out = [{"mark": "waiting", "project": name, "kind": KINDS[o["kind"]][0 if he else 1], "text": o["text"]}
           for name, o in waits]
    landed = sorted(((p["name"], d) for p in projects for d in p["landed"]), key=lambda x: -x[1]["at"])
    out += [{"mark": "landed", "time": _clock(d["at"]), "project": name, "text": d["text"]} for name, d in landed]
    return out


def landed_keys(projects: List[dict]) -> List[str]:
    """The keys of everything that landed in the last hour (the show plays the "landed" sting for a new one)."""
    return [d["key"] for p in projects for d in p["landed"]]


def digest(slide: dict) -> str:
    body = {k: v for k, v in slide.items() if k != "key"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


# --- the staff edition: names and counts only -------------------------------------------------------------

def staff_slide(view: dict) -> dict:
    """The staff board from `radio_staff.staff_view` output ONLY: each listed project's gated label and two
    counts. No other field of the view is read, so nothing else (no item text, nothing waiting on the owner)
    can reach the picture."""
    from . import radio_staff
    lang = view.get("lang") if view.get("lang") in ("he", "en") else "he"
    rows = []
    for p in (view.get("projects") or [])[:radio_staff.MAX_PROJECTS]:
        label = radio_staff.gate(p.get("label"), lang, 40)
        if not label:
            continue
        done = len(p.get("done") or []) if "done_today" in (view.get("sections") or []) else 0
        open_n = p.get("open") if isinstance(p.get("open"), int) and not isinstance(p.get("open"), bool) else 0
        rows.append({"name": label, "done": min(done, 999), "open": min(max(open_n, 0), 999)})
    title = radio_staff.gate(view.get("title"), lang, 40) or (radio_staff.TITLE_HE if lang == "he" else radio_staff.TITLE_EN)
    sl = {"kind": "staff", "lang": lang, "title": title, "rows": rows[:radio_staff.MAX_PROJECTS],
          "clock": _clock(_num(view.get("at")) or time.time())}
    sl["key"] = digest(sl)
    return sl


_HEBREW = re.compile(r"[֐-׿]")
_LATIN = re.compile(r"[A-Za-z]")


def direction(text: str) -> str:
    """"rtl" when the text's first strong letter is Hebrew (or it has none), else "ltr": an English item keeps its
    own order on a right-to-left board instead of being mixed into a Hebrew line."""
    for ch in text:
        if _HEBREW.match(ch):
            return "rtl"
        if _LATIN.match(ch):
            return "ltr"
    return "rtl"
