"""The per-project work map: what the agents are doing in a project, what waits for the owner, and what each line
changes for him, in plain words.

One map per project, from state the bridge already holds (nothing here asks a model):
- the owner ledger, read-only: `<ledger_dir>/<project>.json` ({"next", "items": [{id, text, status, wait, notes,
  created, updated, impact?}]}). An item's `impact` field, or its newest note that starts with "impact:", says what
  it changes for the owner (a ledger tool's `note <id> "impact: ..."`); without one a short plain sentence
  is derived from the item's status and wording. A missing, oversized or corrupt file is an empty map.
- the live sessions of the project and their open progress cards (a running agent is a line in progress).

Each line: `what` (short), `status` (waiting | working | done), `impact`, `source`. Lines that wait for the owner
come first, then work in progress, then what was finished today. Open pull requests are shown only when a source
supplies them; the bridge itself keeps no list of them.

Surfaces: the project page of the Cockpit (cockpit_v2.presentation), a message in the project's lead topic (never
pinned: the pinned bar stays the Cockpit button) and kept current by editing it (`sync_pins`, at most one edit per EDIT_EVERY seconds, never a new message for a
change), and the owner's `/what` command (`telegram_command`). Its switch is the add-on `work_map` (addons.py).
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import html
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import board, fmt, owner_requests, work_lines
from . import lang as L
from .config import Config, private_dir, state_dir, write_private
from .i18n import t
from .identity import isolate

ADDON = "work_map"
DEFAULT_LEDGER = "~/.local/state/work-ledger"
PINS_FILE = "work-map-pins.json"
EDIT_EVERY = 120.0  # seconds between two edits of one map message
RETRY_AFTER = 60.0  # seconds before a failed create or edit is tried again
MAX_ITEMS = 300  # ledger items read per project
MAX_LINES = 40  # lines kept per project for the Cockpit
CHAT_LINES = 12  # lines shown in Telegram
WHAT_CHARS = 100
IMPACT_CHARS = 110
STATUSES = ("waiting", "working", "done")
_FIX = {"en": re.compile(r"\b(fix|fixes|bug|bugs|broken|crash|crashes|error|errors|regression)\b", re.I),
        "he": re.compile(r"תיקון|תקן|באג|שבור|קריסה|שגיאה|תקלה")}
_NEW = {"en": re.compile(r"\b(add|adds|build|builds|create|creates|new|launch|ship|ships|implement)\b", re.I),
        "he": re.compile(r"הוסף|הוספת|בנ[הי]|חדש|יצירת|השק|מימוש")}
_CHECK = {"en": re.compile(r"\b(test|tests|review|check|audit|verify|lint)\b", re.I),
          "he": re.compile(r"בדיק|בדוק|סקיר|ביקורת|אימות")}


def enabled(cfg: Config) -> bool:
    """The add-on switch (addons.py, shown in the Cockpit's add-ons screen); on unless it was switched off."""
    from . import addons
    try:
        return addons.enabled(cfg, "work_map")
    except Exception:
        return True


def lang_of(cfg: Config) -> str:
    return "he" if (getattr(cfg, "ui_language", "") or "en").startswith("he") else "en"


def ledger_dir(cfg: Config) -> Path:
    """`work_map.ledger_dir`, else the brief's or the radio's ledger folder, else the default ledger folder."""
    conf = getattr(cfg, "work_map", None) or {}
    folder = conf.get("ledger_dir")
    if not (isinstance(folder, str) and folder.strip()):
        found = owner_requests.ledger_dir(cfg)
        return found if found is not None else Path(os.path.expanduser(DEFAULT_LEDGER))
    return Path(os.path.expanduser(folder.strip()))


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# --- the ledger ---------------------------------------------------------------------------------

def ledger_items(folder: Optional[Path]) -> Dict[str, dict]:
    """{project key: {"name", "items": [{id, text, status, wait, updated, impact}]}} from the ledger folder.
    Bounded, never follows a link, and skips whatever it cannot read."""
    out: Dict[str, dict] = {}
    if folder is None:
        return out
    try:
        paths = sorted(folder.glob("*.json"))[:work_lines.MAX_PROJECTS]
    except OSError:
        return out
    for path in paths:
        name = work_lines.project_name(path.stem)
        key = work_lines.line_key(name)
        if not key:
            continue
        try:
            data = owner_requests._read(path)
        except (OSError, ValueError, RecursionError):
            continue
        items = data.get("items") if isinstance(data, dict) else None
        rows = []
        for it in (items if isinstance(items, list) else [])[:MAX_ITEMS]:
            if not isinstance(it, dict) or it.get("status") not in ("open", "done"):
                continue
            text = owner_requests._text(it.get("text"))
            created, updated = owner_requests._time(it.get("created")), owner_requests._time(it.get("updated"))
            if not text or (created is None and updated is None):
                continue
            _, impact = work_lines.split_notes(it)
            rows.append({"id": str(it.get("id", ""))[:20], "text": text, "status": it["status"],
                         "wait": "owner" if it.get("wait") == "owner" else "", "updated": updated or created,
                         "impact": owner_requests._text(impact)})
        out[key] = {"name": name, "items": rows}
    return out


# --- what it changes for the owner --------------------------------------------------------------

def derived_impact(text: str, status: str, lang: str) -> str:
    """A plain one-line effect from the line's status and wording; no model, no technical terms."""
    if status == "waiting":
        return t("wm_i_wait", lang=lang)
    for pattern, key in ((_FIX, "wm_i_fix"), (_NEW, "wm_i_new"), (_CHECK, "wm_i_check")):
        if pattern["en"].search(text) or pattern["he"].search(text):
            return t(key, lang=lang)
    return t("wm_i_done" if status == "done" else "wm_i_work", lang=lang)


# --- the map ------------------------------------------------------------------------------------

def _midnight(cfg: Config, now: float) -> float:
    from . import schedule
    local = datetime.fromtimestamp(now, schedule.zone(cfg))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _agent_line(cfg: Config, e, cards_by_session: Dict[str, dict]) -> str:
    card = cards_by_session.get(e.session)
    if card and card.get("title"):
        return str(card["title"])
    name = e.name or ""
    return name.split(":", 1)[1].strip() if ":" in name and name.split(":", 1)[1].strip() else name


def _ledger_file_keys(cfg: Config, folder: Optional[Path] = None) -> set:
    """The project keys of the ledger files that exist (their names only; nothing is opened)."""
    folder = ledger_dir(cfg) if folder is None else folder
    try:
        return {work_lines.line_key(work_lines.project_name(p.stem))
                for p in sorted(folder.glob("*.json"))[:work_lines.MAX_PROJECTS]}
    except OSError:
        return set()


def map_key(cfg: Config, key: str, folder: Optional[Path] = None) -> str:
    """The ledger's key for a session's work line. A project group's sessions are on the line of its name
    ("גשר") while its ledger file has its own key ("harnessgram"), joined by the configured display names
    (`radio.project_names` / `brief.project_names`: {"harnessgram": "גשר"}) or `brief.ledger_projects`
    ({"גשר": "harnessgram"}). Without the join one project showed as two maps, and the pinned one had only the
    live agents (15.11: one line on the pinned map while the ledger held 60 open items).

    The join never guesses, for configured aliases and display names alike. A key that is already a real ledger
    key (a configured project key, or a ledger file of that name) stays as it is, even when it is also somebody's
    alias; and an alias that leads to two different ledger keys is ambiguous, so the sessions are attached to
    neither and the key stays unchanged."""
    if not key:
        return key
    from . import brief
    b = getattr(cfg, "brief", None) or {}
    explicit = b.get("ledger_projects") if isinstance(b.get("ledger_projects"), dict) else {}
    names = brief._names(cfg)
    # Configured aliases first: {"גשר": "harnessgram"}. Two spellings of one alias that point to different
    # ledger keys are ambiguous; otherwise the display names (project_names) are the candidates.
    targets = {work_lines.line_key(v) for k, v in explicit.items()
               if isinstance(k, str) and isinstance(v, str) and work_lines.line_key(k) == key and work_lines.line_key(v)}
    if not targets:
        targets = {k for k, shown in names.items() if work_lines.line_key(shown) == key}
    if len(targets) != 1:
        return key  # no such alias, or it is ambiguous: no join
    if key in names or key in _ledger_file_keys(cfg, folder):
        return key  # a real ledger key never moves, whatever else spells it as an alias
    return next(iter(targets))


def build_all(cfg: Config, entries, now: float, folder: Optional[Path] = None,
              card_rows: Optional[List[dict]] = None) -> Dict[str, dict]:
    """{project key: map} for every project the ledger or a live session knows. A map is
    {"key", "name", "lines": [...], "counts": {waiting, working, done}}; its lines are ordered waiting first."""
    from . import brief, cards
    lang = lang_of(cfg)
    folder = ledger_dir(cfg) if folder is None else folder
    ledger = ledger_items(folder)
    if card_rows is None:
        try:
            card_rows = cards.open_cards()
        except Exception:
            card_rows = []
    by_session = {}
    for rec in card_rows:
        if rec.get("kind") == "progress" and rec.get("state", "open") == "open":
            by_session[rec.get("session")] = rec
    midnight = _midnight(cfg, now)
    maps: Dict[str, dict] = {}

    def project(key: str, name: str) -> dict:
        if key not in maps:
            maps[key] = {"key": key, "name": name or key, "lines": []}
        return maps[key]

    for key, found in ledger.items():
        m = project(key, found["name"])
        for it in found["items"]:
            if it["status"] == "done":
                if it["updated"] < midnight or it["updated"] > now + 300:
                    continue
                status = "done"
            else:
                status = "waiting" if it["wait"] == "owner" else "working"
            m["lines"].append({"what": _short(it["text"], WHAT_CHARS), "status": status, "source": "ledger",
                               "impact": _short(it["impact"], IMPACT_CHARS) or derived_impact(it["text"], status, lang),
                               "own_impact": bool(it["impact"]), "at": it["updated"]})
    names = brief._names(cfg)
    for e in entries:
        if e.state != "working":
            continue
        key = map_key(cfg, brief.session_line(cfg, e.chat, e.name), folder)
        if not key:
            continue
        what = _agent_line(cfg, e, by_session)
        if not what.strip():
            continue
        m = project(key, names.get(key) or (e.name or "").split(":", 1)[0].strip())
        if any(l["what"].casefold() == _short(what, WHAT_CHARS).casefold() for l in m["lines"]):
            continue
        card = by_session.get(e.session)
        at = card["updated"] if card else now - (e.age or 0)
        m["lines"].append({"what": _short(what, WHAT_CHARS), "status": "working", "source": "agent",
                           "impact": derived_impact(what, "working", lang), "at": at})
    for key, m in maps.items():
        if key in names:
            m["name"] = names[key]
        rank = {s: i for i, s in enumerate(STATUSES)}
        # waiting: longest-waiting first; the rest: most recent first
        m["lines"].sort(key=lambda l: (rank[l["status"]], l["at"] if l["status"] == "waiting" else -l["at"],
                                       l["source"] != "agent", l["what"]))
        m["counts"] = {s: sum(1 for l in m["lines"] if l["status"] == s) for s in STATUSES}
        m["lines"] = [{k: v for k, v in l.items() if k not in ("at",)} for l in m["lines"][:MAX_LINES]]
    return maps


def waiting_lines(maps: Dict[str, dict]) -> List[dict]:
    """Every line that waits for the owner, across projects, longest-waiting first: {project, what, impact}."""
    return [{"project": m["name"], "what": l["what"], "impact": l["impact"]}
            for m in sorted(maps.values(), key=lambda m: m["name"].casefold())
            for l in m["lines"] if l["status"] == "waiting"]


# --- the message --------------------------------------------------------------------------------

def _label(text: str, lang: str) -> str:
    shown = html.escape(text, quote=False)
    return isolate(shown, lang) if fmt.LAT.search(text) else shown


def render(cfg: Config, m: Optional[dict], limit: int = CHAT_LINES) -> str:
    """The map as the HTML of a Telegram message: Hebrew-only headings, each item's own words on their own line."""
    lang = lang_of(cfg)
    mark = L.RLM if lang == "he" else ""
    lines = [f"<b>{html.escape(t('wm_title', lang=lang), quote=False)}</b>"]
    if m is not None:
        lines.append(f"{mark}<b>{_label(m['name'], lang)}</b>")
    rows = list((m or {}).get("lines") or [])
    if not rows:
        lines.append(t("wm_empty", lang=lang))
    shown, last = rows[:limit], ""
    for row in shown:
        if row["status"] != last:
            last = row["status"]
            lines.append(f"\n<b>{html.escape(t('wm_' + last, lang=lang), quote=False)}</b>")
        lines.append(f"{mark}• {_label(row['what'], lang)}")
        lines.append(f"{mark}↳ {_label(row['impact'], lang)}")
    if len(rows) > len(shown):
        lines.append("\n" + t("wm_more", lang=lang, n=len(rows) - len(shown)))
    return "\n".join(lines)[:4000]


# --- the map message in the project's lead topic -----------------------------------------------

@contextlib.contextmanager
def _locked():
    base = private_dir(state_dir())
    with open(base / (PINS_FILE + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield base / PINS_FILE


def _load(path: Path) -> Dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def lead_place(cfg: Config, key: str, entries) -> Optional[tuple]:
    """(chat, thread) of the project's lead topic: the configured lead session's topic (`work_map.leads`, else
    `idle_lines.leads`), else the topic of its most recently active live session. None when there is none."""
    from . import brief, idle_lines
    from .config import home_chat
    live = [e for e in entries if e.state != "ended" and map_key(cfg, brief.session_line(cfg, e.chat, e.name)) == key]
    want = ((getattr(cfg, "work_map", None) or {}).get("leads") or {})
    # A lead configured under the project's former display name ("גשר") still counts after the join: both the
    # configured name and the map's key go through map_key before they are compared.
    want = next((v for k, v in want.items() if isinstance(v, str) and map_key(cfg, work_lines.line_key(k)) == key),
                "") if isinstance(want, dict) else ""
    lead = None
    if want:
        hits = [e for e in live if e.session == want or (len(want) >= 8 and e.session.startswith(want))]
        lead = hits[0] if len(hits) == 1 else None
    if lead is None:
        found = idle_lines.find_lead(cfg, key, live)
        for name in idle_lines.settings(cfg)["leads"]:  # `idle_lines.leads` under a name that maps to this key
            if found:
                break
            if name != key and map_key(cfg, work_lines.line_key(name)) == key:
                found = idle_lines.find_lead(cfg, name, live)
        lead = next((e for e in live if e.session == found), None) if found else None
    if lead is None and live:
        lead = min(live, key=lambda e: e.age if e.age is not None else 1 << 30)
    if lead is None or not isinstance(lead.thread, int) or lead.thread <= 0:
        return None
    return (home_chat(cfg) if lead.chat is None else lead.chat), lead.thread


def sync_pins(cfg: Config, bot, entries, now: float, maps: Optional[Dict[str, dict]] = None) -> int:
    """Keep one message per project current. The message is created once and then only edited, at most once per
    EDIT_EVERY seconds and only when its text changed. Returns the messages written. It is never pinned: the
    newest pin of a topic wins the pinned bar, and it must stay the Cockpit button (the board and this map move
    into the one Cockpit pin in #381). A map pinned by an earlier version is unpinned once (`_unpin_old`)."""
    if not enabled(cfg):
        return 0
    from .projects import owner_chats
    maps = build_all(cfg, entries, now) if maps is None else maps
    written = 0
    with _locked() as path:
        data = _load(path)
        _join_old_keys(cfg, data)
        _unpin_old(cfg, bot, data, now)
        for key, m in maps.items():
            if not m["lines"] and not (data.get(key) or {}).get("id"):
                continue  # nothing to show and nothing shown before
            rec = data.setdefault(key, {"id": 0, "pinned": False, "pin_retry_at": 0.0, "digest": "",
                                        "edited_at": 0.0, "retry_at": 0.0, "chat": 0, "thread": 0})
            place = (rec["chat"], rec["thread"]) if rec["id"] else lead_place(cfg, key, entries)
            if place is None or place[0] not in owner_chats(cfg) or not str(place[0]).startswith(board.SUPERGROUP_PREFIX):
                continue
            text = render(cfg, m)
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
            if rec["id"] and (digest == rec["digest"] or now - rec["edited_at"] < EDIT_EVERY):
                continue
            if now < rec["retry_at"]:
                continue
            if rec.get("pinned"):
                continue  # an old pin still waits for its unpin: never replace (and so lose) that message first
            if _write(cfg, bot, place, rec, text, now):
                rec.update(digest=digest, edited_at=now, retry_at=0.0, chat=place[0], thread=place[1])
                written += 1
            else:
                rec["retry_at"] = now + RETRY_AFTER
        write_private(path, json.dumps(data))
    return written


def _join_old_keys(cfg: Config, data: Dict[str, dict]) -> None:
    """A pinned map recorded under a project's display name ("גשר", before map_key joined it to the ledger's
    "harnessgram") moves to the joined key, so the same pinned message is edited in place: no second pin.
    Only a stored key that is such an alias moves: a key that is itself a real ledger key (a project_names key
    or an existing ledger file) is another project's own pin and stays, and so does an ambiguous alias."""
    for old in list(data):
        new = map_key(cfg, old)
        rec = data.get(old)
        if new == old or not isinstance(rec, dict) or not rec.get("id"):
            continue
        if (data.get(new) or {}).get("id"):
            continue  # both were shown: keep each as it is rather than guess which pin to drop
        data[new] = dict(rec, digest="", edited_at=0.0)  # rewritten with the joined map on this round
        del data[old]


# Telegram's answers that mean the message is no longer pinned. Not "chat not found": that says nothing about it.
_UNPIN_DONE = ("message to unpin not found", "message not found", "not pinned")


def _unpin_old(cfg: Config, bot, data: Dict[str, dict], now: float) -> None:
    """A map message pinned by an earlier version is unpinned, once: the record then reads `pinned: false`, so
    this never repeats. "message to unpin not found", "message not found" and "not pinned" count as done (nothing
    is pinned any more); any other failure, "chat not found" included, is retried after PIN_BACKOFF. Never raises."""
    from .telegram import TelegramError
    for rec in data.values():
        if not isinstance(rec, dict) or not rec.get("pinned") or not rec.get("id") or not rec.get("chat"):
            continue
        if now < rec.get("pin_retry_at", 0.0):
            continue
        try:
            board._call(bot, "unpinChatMessage", {"chat_id": rec["chat"], "message_id": rec["id"]}, False)
        except TelegramError as exc:
            text = exc.description.lower()
            if getattr(exc, "skipped", False):
                continue  # held back by the call budget: tried again with the next round
            if not (exc.code == 400 and any(ok in text for ok in _UNPIN_DONE)):
                rec["pin_retry_at"] = now + board.PIN_BACKOFF
                continue
        except Exception:
            continue
        rec.update(pinned=False, pin_retry_at=0.0)


def _write(cfg: Config, bot, place: tuple, rec: dict, text: str, now: float) -> bool:
    """Edit the map message, or create it (also when it was deleted). Never pins. Never raises."""
    from .telegram import TelegramError
    chat, thread = place
    params = {"chat_id": chat, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
    try:
        if rec["id"]:
            try:
                board._call(bot, "editMessageText", dict(params, message_id=rec["id"]), False)
            except TelegramError as exc:
                if "not modified" not in exc.description.lower():
                    if not board._gone(exc):
                        return False
                    rec.update(id=0, pinned=False, pin_retry_at=0.0)  # deleted: one new message
            if rec["id"]:
                return True
        sent = board._call(bot, "sendMessage", dict(params, message_thread_id=thread, disable_notification=True), False)
        mid = sent.get("message_id") if isinstance(sent, dict) else None
        if not board._id(mid):
            return False
        rec.update(id=mid, pinned=False)
        return True
    except Exception:
        return False


def tick(cfg: Config, bot, now: Optional[float] = None, entries=None) -> int:
    """The heartbeat's round: refresh the map messages."""
    now = time.time() if now is None else now
    if not enabled(cfg):
        return 0
    if entries is None:
        from .sessions_menu import collect
        from .topics import TopicMap
        from .config import home_chat
        entries = collect(TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg), now=now)
    return sync_pins(cfg, bot, entries, now)


# --- the owner's /what ---------------------------------------------------------------------------

def key_of_topic(cfg: Config, chat, thread, entries) -> str:
    """The project key of the topic a message came from: the project of the session that lives in this topic,
    else of the project group itself, else ''."""
    from . import brief
    from .config import home_chat
    for e in entries:
        if e.thread == thread and (home_chat(cfg) if e.chat is None else e.chat) == chat:
            key = map_key(cfg, brief.session_line(cfg, e.chat, e.name))
            if key:
                return key
    for p in getattr(cfg, "projects", []) or []:
        if p.chat_id == chat:
            return map_key(cfg, work_lines.line_key(p.name))
    return ""


def telegram_command(cfg: Config, chat, thread, reply: Callable[[str], None], now: Optional[float] = None,
                     entries=None, folder: Optional[Path] = None) -> str:
    """`/what` from the owner, in a topic: that project's map, at most CHAT_LINES lines."""
    lang = lang_of(cfg)
    if not enabled(cfg):
        reply(t("wm_off", lang=lang))
        return "what"
    now = time.time() if now is None else now
    if entries is None:
        from .sessions_menu import collect
        from .topics import TopicMap
        from .config import home_chat
        entries = collect(TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg), now=now)
    key = key_of_topic(cfg, chat, thread, entries)
    if not key:
        reply(t("wm_no_project", lang=lang))
        return "what"
    maps = build_all(cfg, entries, now, folder=folder)
    reply(render(cfg, maps.get(key) or {"key": key, "name": key, "lines": []}))
    return "what"
