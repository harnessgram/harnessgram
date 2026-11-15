"""Opt-in Cockpit presentation data. Existing owner APIs retain all authority.

The public asset catalogue is fixed: no user-supplied paths or private media.
Project status is computed here, never inferred by the browser from wording.
"""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

from . import brief, panel_view, work_lines, work_map
from .config import state_dir, write_private

MODES = ("off", "preview", "on")
MODE_FILE = "cockpit-ui-v2"
# Project scenes may illustrate a project; the rest are fixed screen decorations.
SCENES = frozenset({"plane", "book", "books", "visor", "clapper", "catalog", "generic"})
# "What the system knows" (knowledge.py): its tiles and screen headers, cropped from the approved design.
KNOWLEDGE_ASSETS = frozenset({"k-brain", "k-rules", "k-memories", "k-tools", "k-docs", "k-skills", "k-lock",
                              "k-clipboard", "k-vault", "k-calendar"})
ASSETS = SCENES | {"header", "artwork", "bell", "gears", "server", "mic", "laptop"} | KNOWLEDGE_ASSETS
ASSET_ROOT = Path(__file__).parent / "assets" / "cockpit"
# Versioned URLs permit an immutable cache without leaving old artwork after an upgrade.
ASSET_PREFIX = "/assets/cockpit/v1/"


def mode() -> str:
    try:
        value = (state_dir() / MODE_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return "off"
    return value if value in MODES else "off"


def set_mode(value: str) -> None:
    if value not in MODES:
        raise ValueError("unknown Cockpit mode")
    write_private(state_dir() / MODE_FILE, value)


def enabled(value: str, *, owner: bool) -> bool:
    return value == "on" or value == "preview" and owner


# Automatic art when the private config names none: generic subject words only (owner project names stay in
# private configuration). Latin words match the start of a word, Hebrew ones anywhere inside it (prefix letters).
# First match wins, so the more specific scene comes first.
ART_WORDS = (
    ("books", ("library", "books", "ספרים", "ספריה", "ספרייה")),
    ("clapper", ("studio", "video", "film", "movie", "clip", "youtube", "podcast", "episode", "סטודיו", "וידאו", "סרט",
                 "הפקה", "פודקאסט")),
    ("catalog", ("store", "shop", "catalog", "product", "order", "checkout", "חנות", "קטלוג", "מוצר", "הזמנ")),
    ("visor", ("quest", "vr", "xr", "headset", "bridge", "harnessgram", "telegram", "bot", "cockpit", "משקפ", "מציאות",
               "גשר", "בוט")),
    ("plane", ("flight", "fly", "pilot", "plane", "aviation", "airport", "טיסה", "טיסות", "מטוס", "טייס")),
    ("book", ("learn", "lesson", "study", "course", "quiz", "school", "class", "book", "לימוד", "שיעור", "חידון", "תלמיד")),
)
# Every project scene except the last resort: the stable spread for names no word matches.
SPREAD = ("plane", "book", "books", "visor", "clapper", "catalog")


def word_asset(*names: str):
    """The scene a project's own words call for (ART_WORDS), or None."""
    words = [w for n in names for w in re.split(r"[\W_]+", work_lines.project_name(n or "").casefold())]
    for scene, keys in ART_WORDS:
        for k in keys:
            if any(w.startswith(k) if k.isascii() else k in w for w in words):
                return scene
    return None


def spread_asset(key: str, taken=()) -> str:
    """A stable scene for a key no word matches: a hash picks the start, then the first scene not yet taken.

    A hash, not Python's per-process hash(): the same project gets the same picture on every start.
    """
    key = work_lines.line_key(key or "")
    if not key:
        return "generic"
    start = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:4], "big") % len(SPREAD)
    order = SPREAD[start:] + SPREAD[:start]
    return next((a for a in order if a not in taken), order[0])


def auto_asset(*names: str) -> str:
    """A scene picked by the bridge itself; "generic" only when there is no name at all."""
    return word_asset(*names) or spread_asset(next((n for n in names if work_lines.line_key(n or "")), ""))


def _configured(key: str, cfg):
    mapping = ((getattr(cfg, "brief", None) or {}).get("project_art") or {}) if cfg else {}
    configured = next((v for k, v in mapping.items() if work_lines.line_key(k) == key), None) if isinstance(mapping, dict) else None
    return configured if isinstance(configured, str) and configured in SCENES else None


def asset(name: str, cfg=None, display: str = "") -> str:
    """An illustration is decoration, never a project identity or authorization.

    A valid private `brief.project_art` entry wins; otherwise the bridge picks one by itself (auto_asset).
    """
    key = work_lines.line_key(name)
    if not key:
        return "generic"
    return _configured(key, cfg) or auto_asset(name, display)


def _spread_projects(groups: dict, cfg) -> None:
    """Projects that neither the config nor their own words place get different scenes from each other and from
    the placed ones while there are scenes left; the choice depends only on the keys, never on the order."""
    free, taken = [], set()
    for key, g in groups.items():
        placed = _configured(key, cfg) or word_asset(key, g["name"])
        if placed:
            taken.add(placed)
        else:
            free.append(key)
    for key in sorted(free):
        groups[key]["asset"] = spread_asset(key, taken)
        taken.add(groups[key]["asset"])


def _place(cfg, e) -> str:
    """The Telegram topic a session lives in, as the same link the classic response gives decisions."""
    chat = getattr(e, "chat", None)
    if chat is None:
        chat = getattr(cfg, "chat_id", None) if (getattr(cfg, "chat_id", None) or 0) < 0 else getattr(cfg, "owner_chat_id", None)
    return panel_view.topic_url(chat, getattr(e, "thread", None))


def _percent(card: dict):
    n = card.get("percent")
    if n is None and card.get("steps") and isinstance(card.get("at"), (int, float)):
        n = 100 * card["at"] / len(card["steps"])
    if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n):
        return None
    return round(max(0, min(100, n)))


IDEA_AGE = 24 * 3600  # an open ledger item older than this is an idea waiting on the project page
IDEAS_SHOWN = 6


def _lead(cfg, g: dict, entries, live: dict) -> str:
    """The session a tap on a waiting idea asks: the project's configured lead (idle_lines.leads), else its most
    recently active live session; "" when it has none (the page then only shows the item's words)."""
    from . import idle_lines
    try:
        lead = idle_lines.find_lead(cfg, g["key"], entries)
    except Exception:
        lead = None
    if lead and lead[:8] in live:
        return lead[:8]
    mine = [live[s] for s in g["sessions"] if s in live]
    mine.sort(key=lambda s: s.get("age") if isinstance(s.get("age"), (int, float)) else float("inf"))
    return mine[0]["id"] if mine else ""


def presentation(cfg, state: dict, entries, now: float) -> dict:
    """Group existing state by the bridge's project rules; never merge decisions.

    Unknown progress stays unknown. Completed requests alone do not mean a
    project shipped. An explicit completed progress card is required for that.
    """
    groups, session_keys, name_keys, place_keys = {}, {}, {}, {}
    names = {work_lines.line_key(p.name): p.name for p in getattr(cfg, "projects", [])}
    names.update(brief._names(cfg))

    def group(key, name=""):
        key = key or work_lines.line_key(name) or "general"
        if key not in groups:
            groups[key] = {"key": key, "name": names.get(key) or name or key, "asset": asset(key, cfg, names.get(key) or name),
                           "status": "on_track", "percent": None, "waiting": 0, "age": None,
                           "url": "", "summary": "", "_progress_age": float("inf"), "_blocked": False,
                           # Owner waits from the request ledger and from the brief's work lines (often the same
                           # ledger items): the larger count joins decisions and cards, never both.
                           "_request_wait": 0, "_line_wait": 0, "_ideas": [],
                           # References into classic lists; the browser filters those lists, never copies.
                           "sessions": [], "topics": [], "reminders": []}
        return groups[key]

    for e in entries:
        key = brief.session_line(cfg, e.chat, e.name) or work_lines.line_key(e.name)
        session_keys[e.session[:8]] = key
        name_keys.setdefault(e.name, set()).add(key)
        place = _place(cfg, e)
        if place:
            place_keys.setdefault(place, set()).add(key)

    def owner(place, topic):
        """The one project an owner item belongs to, or "" when it cannot be told apart.

        The topic's place (group chat and thread) decides first. A bare topic name is
        trusted only when no other project has a topic of that name; otherwise the item
        stays off every project page (Now still shows it), never on the wrong one.
        """
        if place:
            # A known place that matches no live topic (e.g. the topic left the list) is never
            # re-guessed by name: another project's same-named topic must not claim it.
            keys = place_keys.get(place, set())
            return next(iter(keys)) if len(keys) == 1 else ""
        keys = name_keys.get(topic)
        if keys is None:
            keys = {work_lines.line_key((topic or "").split(":", 1)[0])} - {""}
        return next(iter(keys)) if len(keys) == 1 else ""
    for s in state.get("sessions", []):
        key = session_keys.get(s["id"], work_lines.line_key(s["name"].split(":", 1)[0]))
        g = group(key, s["name"].split(":", 1)[0])
        g["sessions"].append(s["id"])
        age = s.get("age")
        if g["age"] is None or age is not None and age < g["age"]:
            g.update(age=age, summary=s.get("now") or s.get("summary") or "", url=s.get("url", ""))
            if s.get("now_he") or s.get("summary_he"):
                g["summary_he"] = s.get("now_he") or s.get("summary_he")
    for r in state.get("requests", []):
        g = group(work_lines.line_key(r["project"]), r["project"])
        if isinstance(r.get("updated"), (int, float)):
            age = max(0, int(now - r["updated"]))
            if g["age"] is None or age < g["age"]:
                g["age"] = age
        if r["status"] == "open" and r["wait"] == "owner":
            g["_request_wait"] += 1
        age = r.get("age")
        if r["status"] == "open" and isinstance(age, (int, float)) and not isinstance(age, bool) and age > IDEA_AGE:
            g["_ideas"].append(r)
    for d in state.get("decisions", []):
        key = owner(d.get("url"), d.get("topic"))
        d["project_key"] = key
        d["project_asset"] = asset(key, cfg, names.get(key, ""))
        d["project_name"] = names.get(key) or d.get("topic", "").split(":", 1)[0]
        # Old decisions are still open; replaced/ended decisions retain their
        # original warning and cannot be mistaken for fresh approvals.
        if key and (not d.get("stale") or d["stale"] == "old"):
            group(key, d.get("topic", "").split(":", 1)[0])["waiting"] += 1
    for c in state.get("cards", []):
        key = session_keys.get(c.get("session")) or work_lines.line_key(c.get("topic", "").split(":", 1)[0])
        c["project_key"], c["project_asset"] = key or "general", asset(key, cfg, names.get(key, ""))
        g = group(key, c.get("topic", "").split(":", 1)[0])
        if isinstance(c.get("age"), (int, float)) and (g["age"] is None or c["age"] < g["age"]):
            g["age"] = c["age"]
        if c["kind"] in ("ask", "choice") and not c.get("ended"):
            g["waiting"] += 1
        if c["kind"] == "progress" and not c.get("auto_closed") and c.get("age", 0) < g["_progress_age"]:
            g.update(percent=_percent(c), _progress_age=c.get("age", 0),
                     _blocked=bool(c.get("stale")), _shipped=bool(c.get("finished") and not c.get("auto_closed")))
    for line in (state.get("brief") or {}).get("lines", []):
        g = group(line["key"], line["name"])
        if not g["summary"]:
            g["summary"] = line.get("text", "")
            if line.get("text_he"):
                g["summary_he"] = line["text_he"]
        if g["age"] is None:
            g["age"] = line.get("age")
        if not g["url"]:
            g["url"] = line.get("url", "")
        checklist = line.get("checklist") or {}
        items = checklist.get("items") or []
        if g["percent"] is None and items:
            g["percent"] = round(100 * sum(bool(i.get("done")) for i in items) / len(items))
        if line.get("why") or line.get("tone") == "bad":
            g["_blocked"] = True
        n = line.get("waiting")
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            g["_line_wait"] = max(g["_line_wait"], n)
    # Topics and reminders join a project the bridge already knows; they never
    # create one, so finished history cannot resurrect an old project tile.
    for t in state.get("topic_cards", []):
        g = groups.get(work_lines.line_key(t.get("project", "")))
        if g is not None:
            g["topics"].append(t["key"])
    for r in state.get("reminders", []):
        g = groups.get(owner(r.get("url"), r.get("topic") or ""))
        if g is not None and r.get("ref"):
            g["reminders"].append(r["ref"])
    _spread_projects(groups, cfg)
    # The per-project work map (work_map.py): lines of work, waiting-for-the-owner ones first. Only for projects
    # already known here; the add-on flag `addons.work_map` switches it off.
    try:
        maps = work_map.build_all(cfg, entries, now) if work_map.enabled(cfg) else {}
    except Exception:
        maps = {}
    for key, g in groups.items():
        # The Cockpit groups sessions under the project's display name ("גשר") while the map lives under the
        # ledger's key ("harnessgram"): look it up through the same join work_map uses, never by the bare name.
        found = maps.get(work_map.map_key(cfg, key))
        if found:
            g["work_map"] = found["lines"]
            g["work_map_counts"] = found["counts"]
    # Decisions and cards show the same picture as their project's tile.
    for item in state.get("decisions", []) + state.get("cards", []):
        g = groups.get(item.get("project_key"))
        if g is not None:
            item["project_asset"] = g["asset"]
    live = {s["id"]: s for s in state.get("sessions", []) if s.get("state") != "ended" and not s.get("left")}
    for g in groups.values():
        ideas = sorted(g.pop("_ideas"), key=lambda r: -r["age"])
        g["ideas"] = [dict({k: r[k] for k in ("project", "id", "title", "wait", "age")},
                           **({"title_he": r["title_he"]} if r.get("title_he") else {})) for r in ideas[:IDEAS_SHOWN]]
        g["ideas_more"] = max(0, len(ideas) - IDEAS_SHOWN)
        g["lead"] = _lead(cfg, g, entries, live) if ideas else ""
        g["waiting"] += max(g["_request_wait"], g["_line_wait"])
        g["stale"] = g["age"] is None or g["age"] > 24 * 3600
        g["status"] = ("waiting" if g["waiting"] else "blocked" if g["_blocked"] else
                       "shipped" if g.get("_shipped") and g["percent"] == 100 else "on_track")
        for key in ("_progress_age", "_blocked", "_shipped", "_request_wait", "_line_wait"):
            g.pop(key, None)
    out = {"projects": list(groups.values()), "as_of": int(now), "since": (state.get("brief") or {}).get("since")}
    if not work_map.enabled(cfg):
        out["work_map_off"] = True  # the project pages then draw no work map
    return out
