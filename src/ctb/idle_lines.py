"""Idle-line nudger: the bridge keeps every open team line in the owner ledger moving.

Why: an open team item once sat untouched for twelve hours because its project's lead session was idle between
turns and its own watcher printed the item only once. The owner should never be the one who notices.

What it does (from the heartbeat, at most every CHECK_EVERY seconds):
- Detection (`idle_items`, pure): open TEAM items of the owner ledger (owner_requests.read) whose last movement
  (the ledger's `updated`, which every note, wait change or completion bumps) is older than the threshold
  (`idle_lines.hours`, default 4), leaving out parked items.
- Parking: when the item's LATEST note (read whole, owner_requests.latest_notes) contains "parked:" anywhere (any
  case), the item is parked. An optional "until YYYY-MM-DD" right after it parks it until the end of that local
  day; without a date it is parked for PARKED_DEFAULT (24 h) from its last movement. Example: "Stage 1 merged.
  parked: until <date> waiting for another team". A later note without "parked:" ends the parking.
- Nudge (`check`): only to the lead sessions named in `idle_lines.leads` ({"<ledger project>": "<session id or
  its first 8+ characters>"}); the bridge never guesses a lead. Unlisted projects show only in the Cockpit's
  "Idle lines" block (`view`). The nudge is ONE short fixed line per project, `nudge_line(project)` (under 120
  characters, so Claude Code never folds it as a paste); the session runs `ctb idle-lines --project <project>` to
  read the items. It is typed only into the session's
  own pane (its process record), with the session quiet for QUIET_FOR (30 min), its Claude process the pane's
  foreground process before typing and before Enter, and the input box empty in two reads SNAPSHOT_GAP (5 s)
  apart (inbox.type_command with verify_session). Never into a plain shell, a draft, a working, moving,
  compacting or out-of-quota session, or ahead of queued owner messages; never to Telegram. A busy session is
  tried at the next check. Each item is nudged at most once per THROTTLE (4 h).
  Remaining race (tmux has no atomic compare-and-send): text the owner starts typing in that pane in the moment
  between the last read and Enter can go out with the line.
- State: `idle-lines.json` in the bridge state folder (atomic writes), read-decide-record under an inter-process
  lock (`idle-lines.lock`). The delivery record (session, project, line, time) is written under that lock right
  before Enter, so a fast reply always finds it, and removed again when typing or Enter fails; the items are
  recorded only after a successful delivery.
- The nudged turn is quiet: hooks.background_only_turn treats a turn as background-only only when its whole
  prompt is exactly that project's line and the bridge recorded a delivery to that session within DELIVERY_WINDOW. With
  `quiet_background_turns` off, the reply reaches the topic like any other.

Configuration (`idle_lines` object in the config): "on" (default true), "hours" (default 4), "leads".
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import re
import time
from typing import Callable, Dict, List, Optional

from .config import private_dir, read_map, state_dir, write_private

STATE_FILE = "idle-lines.json"
LOCK_FILE = "idle-lines.lock"
DEFAULT_HOURS = 4.0
THROTTLE = 4 * 3600  # one nudge per item per this many seconds
CHECK_EVERY = 300  # seconds between checks from the heartbeat
QUIET_FOR = 30 * 60  # the lead must have been quiet this long
SNAPSHOT_GAP = 5.0  # seconds between the two empty-box reads
DELIVERY_WINDOW = 600  # a delivery this recent makes the fixed line's turn a background turn
PARKED_DEFAULT = 24 * 3600
MAX_ITEMS = 5  # in the Cockpit block
NUDGE_LINE = "[bridge] Idle team lines need you: run `ctb idle-lines{scope}` and start or park each one."
_SAFE_PROJECT = re.compile(r"[A-Za-z0-9._-]{1,24}")
INSTRUCTION = ("Start each one now (delegate to a free team), or add a ledger note "
               "'parked: until <date> <reason>'. Do not reply to the owner about this nudge.")
_PARKED = re.compile(r"parked:\s*(?:until\s+(\d{4}-\d{2}-\d{2}))?", re.I)
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "fish", "ksh", "tcsh", "csh", "login"})
_LAST_CHECK = [0.0]


# --- settings -----------------------------------------------------------------------------------------------

def settings(cfg) -> dict:
    raw = getattr(cfg, "idle_lines", None)
    raw = raw if isinstance(raw, dict) else {}
    hours = raw.get("hours", DEFAULT_HOURS)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= 24 * 30:
        hours = DEFAULT_HOURS
    leads = raw.get("leads") if isinstance(raw.get("leads"), dict) else {}
    return {"on": raw.get("on", True) is not False, "threshold": float(hours) * 3600,
            "leads": {str(k).casefold(): v.strip() for k, v in leads.items() if isinstance(v, str) and v.strip()}}


# --- detection (pure) ---------------------------------------------------------------------------------------

def parked_until(note: str, moved: float) -> Optional[float]:
    """The time a parking note holds the item until, or None when the note does not park it."""
    found = _PARKED.search(note or "")
    if not found:
        return None
    if found.group(1):
        try:
            day = datetime.datetime.strptime(found.group(1), "%Y-%m-%d") + datetime.timedelta(days=1)
            return day.timestamp()  # the end of that local day
        except (ValueError, OverflowError, OSError):
            pass  # an impossible date counts as no date
    return moved + PARKED_DEFAULT


def idle_items(rows: List[dict], now: float, threshold: float = DEFAULT_HOURS * 3600,
               notes: Optional[dict] = None) -> List[dict]:
    """Open team items that have not moved for `threshold` seconds and are not parked, longest idle first.
    `notes` ({(project, id): whole latest note}) replaces each row's display note for the parking check."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or r.get("status") != "open" or (r.get("wait") == "owner" and not r.get("urgent")):
            continue
        moved = r.get("updated")
        if isinstance(moved, bool) or not isinstance(moved, (int, float)):
            continue
        idle = now - float(moved)
        if idle < threshold and not r.get("urgent"):
            continue
        note = (notes or {}).get((r.get("project"), r.get("id")), r.get("note") or "")
        until = parked_until(note, float(moved))
        if until is not None and now < until:
            continue
        out.append({"project": r.get("project") or "", "id": r.get("id"), "title": r.get("title") or "",
                    "title_he": r.get("title_he") or None, "idle": int(idle),
                    "push_updated": moved if r.get("urgent") and note == "pushed by owner" else None})
    return sorted(out, key=lambda x: (-x["idle"], str(x["project"]).casefold(), str(x["id"])))


def read_items(cfg, now: float) -> List[dict]:
    """The idle items straight from the configured ledger."""
    from . import owner_requests
    folder = owner_requests.ledger_dir(cfg)
    return idle_items(owner_requests.read(now, folder), now, settings(cfg)["threshold"],
                      owner_requests.latest_notes(folder))


def listing(items: List[dict], project: str = "") -> str:
    """What `ctb idle-lines` prints: one line per item, then the instruction."""
    want = project.casefold()
    rows = [it for it in items if not want or str(it["project"]).casefold() == want]
    if not rows:
        return "No idle team lines" + (f" in {project}." if project else ".")
    lines = [f"{it['project']} #{it['id']} idle {max(1, it['idle'] // 3600)}h: {' '.join(str(it['title']).split())}"
             for it in rows]
    return "\n".join(lines + ["", INSTRUCTION])


def nudge_line(project: str) -> str:
    """The project's fixed nudge line (under 120 characters). A name that is not a plain short word is left out
    (never quoted into a shell line): the line then asks for every project's items."""
    name = str(project or "")
    return NUDGE_LINE.format(scope=f" --project {name}" if _SAFE_PROJECT.fullmatch(name) else "")


def is_bridge_prompt(text: str, session: str, now: Optional[float] = None) -> bool:
    """The turn started by the project's fixed nudge line alone, delivered by the bridge to this session moments ago."""
    if not session or not isinstance(text, str):
        return False
    rec = (_read().get("delivered") or {}).get(session)
    if not isinstance(rec, dict) or not isinstance(rec.get("line"), str) or text.strip() != rec["line"]:
        return False
    now = time.time() if now is None else now
    at = rec.get("at")
    return isinstance(at, (int, float)) and not isinstance(at, bool) and 0 <= now - at < DELIVERY_WINDOW


# --- state --------------------------------------------------------------------------------------------------

def _path():
    return state_dir() / STATE_FILE


def _read() -> dict:
    data = read_map(_path())
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    write_private(_path(), json.dumps(data, ensure_ascii=False, sort_keys=True))


@contextlib.contextmanager
def _locked():
    """One check at a time across processes: read, decide, deliver and record under one lock."""
    root = private_dir(state_dir())
    with open(root / LOCK_FILE, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _key(project: str, ident) -> str:
    return f"{str(project).casefold()}|{ident}"


def due(items: List[dict], now: float, state: Optional[dict] = None) -> List[dict]:
    """The items not nudged within THROTTLE."""
    sent = (state if state is not None else _read()).get("items") or {}
    return [it for it in items
            if not (isinstance(sent.get(_key(it["project"], it["id"])), (int, float))
                    and 0 <= now - sent[_key(it["project"], it["id"])] < THROTTLE)]


def _record(data: dict, items: List[dict], now: float) -> None:
    sent = {k: v for k, v in (data.get("items") or {}).items()
            if isinstance(v, (int, float)) and 0 <= now - v < 30 * 86400}
    for it in items:
        sent[_key(it["project"], it["id"])] = now
    data["items"] = sent


def _mark_delivery(data: dict, session: str, project: str, line: str, at: float) -> dict:
    """The delivery record, written before Enter (the caller holds the lock)."""
    kept = {k: v for k, v in (data.get("delivered") or {}).items()
            if isinstance(v, dict) and isinstance(v.get("at"), (int, float)) and 0 <= at - v["at"] < 86400}
    rec = {"at": at, "project": project, "line": line}
    kept[session] = rec
    data["delivered"] = kept
    _write(data)
    return rec


def _unmark_delivery(data: dict, session: str, rec: dict) -> None:
    """Typing or Enter failed: the record made for it goes again."""
    if (data.get("delivered") or {}).get(session) is rec:
        del data["delivered"][session]
        _write(data)


# --- the lead session and delivery --------------------------------------------------------------------------

def find_lead(cfg, project: str, entries) -> Optional[str]:
    """The project's configured lead session, when exactly one live session matches it; no guessing."""
    want = settings(cfg)["leads"].get(str(project).casefold())
    if not want:
        return None
    hits = [e.session for e in entries if e.state != "ended"
            and (e.session == want or (len(want) >= 8 and e.session.startswith(want)))]
    return hits[0] if len(hits) == 1 else None


def _foreground_claude(pane: str, session: str) -> bool:
    """The session's own Claude process is the one foreground process of the pane (not a shell, not a reused id)."""
    from . import inbox as I
    pid = I.session_pid(session)
    return bool(pid) and [p for p, _ in I._pane_claude_targets(pane)] == [pid]


def deliver(session: str, line: str, now: Optional[float] = None,
            sleep: Callable[[float], None] = time.sleep, before_enter: Optional[Callable[[], None]] = None,
            still_ok: Optional[Callable[[], bool]] = None) -> str:
    """Type `line` into the session only when it waits, quiet, at an empty Claude prompt: "sent", or why not."""
    from . import compact, inbox as I, sessions_menu
    from .wake import limit_active
    now = time.time() if now is None else now
    if I.default_inbox().pending(session) or I.main_working(session) or limit_active(session):
        return "busy"  # owner messages first; a working or out-of-quota session is tried again later
    if I.recently_moving(session) or compact.command_pending(session):
        return "busy"
    last = sessions_menu._last_activity(session)
    if last is None or now - last < QUIET_FOR:
        return "busy"  # the owner may be at that terminal
    pane = I._pane_from_process(session)  # the pane its own process names: what verify_session checks again
    if not pane:
        return "no_pane"
    command = (I._pane_command(pane) or "").strip().lstrip("-").casefold()
    if not command or command in _SHELLS or not I.pane_runs_session(pane, session) \
            or not _foreground_claude(pane, session):
        return "no_pane"  # a plain shell (Claude exited), a reused pane id or another foreground process
    first, ready = I.input_box(pane)
    if first != "" or not ready:
        return "busy"
    sleep(SNAPSHOT_GAP)
    second, ready = I.input_box(pane)
    if second != "" or not ready:
        return "busy"  # someone is typing there
    if not I._claim_terminal(session):
        return "busy"
    try:
        if still_ok is not None and not still_ok():
            return "stale"
        return I.type_command(session, pane, line, verify_session=True,
                              still_ok=lambda: _foreground_claude(pane, session)
                              and (still_ok is None or still_ok()), before_enter=before_enter)
    finally:
        I._release_terminal(session)


def check(cfg, now: Optional[float] = None, items: Optional[List[dict]] = None, entries=None,
          send: Callable[[str, str, Callable[[], None]], str] = None) -> int:
    """One round: nudge each configured lead about its project's idle items. Returns the number of nudges typed."""
    now = time.time() if now is None else now
    conf = settings(cfg)
    if not conf["on"]:
        return 0
    if items is None:
        items = read_items(cfg, now)
    if not items or not conf["leads"]:
        return 0  # nothing idle, or no lead configured: the Cockpit block is the only place
    if entries is None:
        from .config import home_chat
        from .sessions_menu import collect
        from .topics import TopicMap
        entries = collect(TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg), now=now)
    typed = 0
    with _locked():
        data = _read()
        by_project: Dict[str, List[dict]] = {}
        for it in due(items, now, data):
            by_project.setdefault(it["project"], []).append(it)
        for project, todo in sorted(by_project.items(), key=lambda p: str(p[0]).casefold()):
            lead = find_lead(cfg, project, entries)
            if not lead:
                continue
            line, made = nudge_line(project), []

            def still_open(todo=todo):
                # Re-read after terminal waits as well as before typing/Enter. Never nudge a closed,
                # parked or newly owner-waiting item from the earlier detection snapshot.
                live = {_key(it["project"], it["id"]): it for it in read_items(cfg, now)}
                todo[:] = [it for it in todo if _key(it["project"], it["id"]) in live
                           and (it.get("push_updated") is None or
                                live[_key(it["project"], it["id"])].get("push_updated") == it["push_updated"])]
                return bool(todo)

            if not still_open():
                continue

            def before_enter(project=project, lead=lead, line=line, made=made):
                if not still_open():
                    raise ValueError("idle items changed")
                # under the lock, after every pane check passed: a reply that comes at once finds its record
                made.append(_mark_delivery(data, lead, project, line, time.time()))
            try:
                result = (send(lead, line, before_enter) if send is not None else
                          deliver(lead, line, now, before_enter=before_enter, still_ok=still_open))
            except Exception:
                result = "failed"
            if result == "sent":  # the items are recorded only after the line really went in
                _record(data, todo, now)
                _write(data)
                typed += 1
            else:
                for rec in made:
                    _unmark_delivery(data, lead, rec)
    return typed


def tick(cfg, now: Optional[float] = None) -> int:
    """The heartbeat's call: a check at most every CHECK_EVERY seconds."""
    now = time.time() if now is None else now
    if now - _LAST_CHECK[0] < CHECK_EVERY:
        return 0
    _LAST_CHECK[0] = now
    return check(cfg, now)


# --- the Cockpit ---------------------------------------------------------------------------------------------

def view(cfg, rows: List[dict], now: float, notes: Optional[dict] = None) -> dict:
    """The Cockpit's "Idle lines" block: a count and up to MAX_ITEMS items; {"count": 0} when nothing is idle."""
    conf = settings(cfg)
    if not conf["on"]:
        return {"count": 0, "items": []}
    if notes is None:
        from . import owner_requests
        notes = owner_requests.latest_notes(owner_requests.ledger_dir(cfg))
    items = idle_items(rows, now, conf["threshold"], notes)
    sent = _read().get("items") or {}
    shown = []
    for it in items[:MAX_ITEMS]:
        at = sent.get(_key(it["project"], it["id"]))
        shown.append({"project": it["project"], "id": it["id"], "title": it["title"], "title_he": it["title_he"],
                      "hours": max(1, it["idle"] // 3600),
                      "nudged": isinstance(at, (int, float)) and 0 <= now - at < THROTTLE,
                      "no_lead": str(it["project"]).casefold() not in conf["leads"]})
    return {"count": len(items), "items": shown}
