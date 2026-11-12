"""The Cockpit's Morning Brief: a personal front page computed from state the bridge already holds.

Behind one runtime switch (`brief_home` in the panel settings, off by default). When it is on, /api/state carries
a "brief" object and the page's home tab draws it instead of the classic home; when it is off nothing here runs.

What the brief holds, every fact with its own age and a stale flag, so nothing old looks alive:
- `headline`: counts for one sentence (lines moving, lines stuck, items waiting for the owner).
- `since`: what changed since the owner last looked (a per-viewer marker the page moves when it is left).
- `waiting`: what waits for the owner, newest first, each with exactly one action (the page decides its label).
- `lines`: one entry per line of work: one sentence, its source and age, and a tone decided by facts only
  (a stale card, no movement while nobody works, a live session), never by the sentence itself.
- `noticed`: what the Cockpit noticed by itself (a card that stopped moving, a job that died, a promise that
  did not move for days, a source it could not read).
- `teams` and `mac`: one compact row (who works, quota about to go unused, the Mac's own reading if present).
- `ask`: the fixed questions the ask box offers (their answers come in a later release).

Sources: the sessions and cards of panel.build_state, the agent blocks (blocks.py), a work-lines provider
(work_lines.py, the owner ledger when configured), an optional events file (one JSON object per line, written by
a watcher: {"kind", "line", "text", "at"}), and `state["mac"]` when the Mac reader is installed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import blocks as B
from . import panel_view, work_lines
from .config import Config, private_dir, set_setting, setting_on, state_dir, write_private

SWITCH = "cockpit-brief-on"
SEEN_FILE = "brief-seen.json"
FRESH = {"session": 15 * 60, "card": 30 * 60, "ledger": 24 * 3600, "quota": 6 * 3600, "mac": 5 * 60,
         "event": 24 * 3600, "decision": panel_view.OLD_AFTER}
MOVING = 2 * 3600  # a line touched this recently is moving
STUCK_AFTER = 24 * 3600  # open work that has not moved this long, with nobody on it, is stuck
PROMISE_AFTER = 3 * 24 * 3600  # an open promise this old is "noticed"
CARD_STALE = 30 * 60  # a progress card with no update this long while its session is not working
WASTE_USED = 70  # weekly quota used below this percent ...
WASTE_RESET = 24 * 3600  # ... with a reset this close is about to go unused
EVENT_WINDOW = 24 * 3600
EVENT_BYTES = 256 * 1024
EVENT_KINDS = ("job_died", "review_findings", "check_failed", "note")
EVENT_TEXT = 200
MAX_LINES = 12
MAX_WAITING = 12
MAX_NOTICED = 8
ASK = ("waiting", "stuck", "working", "changed", "mac")
TONES = ("bad", "wait", "ok", "idle")  # the order lines are shown in
_seen_lock = threading.Lock()


def on() -> bool:
    return setting_on(SWITCH)


def set_on(value: bool) -> None:
    set_setting(SWITCH, value)


# --- the per-viewer "since you last looked" marker ---------------------------------------------

def seen_at(viewer: int) -> float:
    try:
        data = json.loads((state_dir() / SEEN_FILE).read_text(encoding="utf-8"))
        value = data.get(str(viewer)) if isinstance(data, dict) else None
    except (OSError, ValueError, RecursionError):
        return 0.0
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0.0


def mark_seen(viewer: int, at: float, now: Optional[float] = None) -> float:
    """Move the viewer's marker forward to `at` (never into the future, never backwards)."""
    with _seen_lock:
        return _mark_seen(viewer, at, now)


def _mark_seen(viewer: int, at: float, now: Optional[float]) -> float:
    now = time.time() if now is None else now
    at = min(float(at), now)
    base = private_dir(state_dir())
    try:
        data = json.loads((base / SEEN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    old = data.get(str(viewer))
    if isinstance(old, (int, float)) and not isinstance(old, bool) and old >= at:
        return float(old)
    data = {k: v for k, v in data.items() if isinstance(v, (int, float))}
    data[str(viewer)] = at
    write_private(base / SEEN_FILE, json.dumps(dict(list(data.items())[-20:])))
    return at


# --- helpers ---------------------------------------------------------------------------------

def _age(now: float, then) -> Optional[int]:
    if isinstance(then, bool) or not isinstance(then, (int, float)) or not then:
        return None
    try:
        delta = now - then
        return max(0, int(delta)) if math.isfinite(delta) else None
    except (OverflowError, ValueError):
        return None


def fact(kind: str, age: Optional[int]) -> dict:
    """A fact's age and whether it is past its kind's freshness window (an unknown age is stale)."""
    return {"age": age, "stale": age is None or age > FRESH[kind]}


def _names(cfg: Config) -> Dict[str, str]:
    brief = getattr(cfg, "brief", None) or {}
    radio = getattr(cfg, "radio", None) or {}
    out = {}
    for src in (radio.get("project_names"), brief.get("project_names")):
        if isinstance(src, dict):
            out.update({work_lines.line_key(k): work_lines.project_name(v) for k, v in src.items()
                        if isinstance(k, str) and isinstance(v, str) and work_lines.line_key(k)
                        and work_lines.project_name(v)})
    return out


def session_line(cfg: Config, chat, name: str) -> str:
    """The work line a session belongs to: its project group, else a configured title prefix, else the text
    before the ":" of its title ("Studio: filters" is on "studio"); "" when none of these says."""
    from . import projects
    for p in getattr(cfg, "projects", []) or []:
        if chat is not None and p.chat_id == chat:
            return work_lines.line_key(p.name)
    found = projects.title_project(cfg, name) if name else None
    if found:
        return work_lines.line_key(found.name)
    if ":" in (name or ""):
        return work_lines.line_key(name.split(":", 1)[0])
    return ""


def read_events(path: Optional[Path], now: float) -> List[dict]:
    """The watcher's recent findings (the newest EVENT_BYTES of a JSON-lines file), newest first."""
    if not path or not path.is_file():
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - EVENT_BYTES))
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("kind") not in EVENT_KINDS or not isinstance(ev.get("text"), str):
            continue
        at = ev.get("at")
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not 0 <= now - at <= EVENT_WINDOW:
            continue
        text = " ".join(ev["text"].split())[:EVENT_TEXT]
        if text:
            out.append({"kind": ev["kind"], "line": work_lines.line_key(ev.get("line") or ""), "text": text,
                        "at": float(at)})
    return sorted(out, key=lambda e: -e["at"])


def _events_path(cfg: Config) -> Optional[Path]:
    value = (getattr(cfg, "brief", None) or {}).get("events_file")
    return Path(os.path.expanduser(value)) if isinstance(value, str) and value.strip() else None


def _resets_in(value, now: float) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return value - now
    if isinstance(value, str) and value:
        from datetime import datetime
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() - now
        except ValueError:
            return None
    return None


# --- the brief ---------------------------------------------------------------------------------

def build(cfg: Config, state: dict, entries, now: float, provider: Optional[work_lines.Provider] = None,
          block_rows: Optional[List[dict]] = None, card_rows: Optional[List[dict]] = None,
          events: Optional[List[dict]] = None, seen: Optional[float] = None) -> dict:
    """The brief from `state` (panel.build_state's answer so far) and the sessions' `entries`. The keyword
    arguments replace a source (tests and screenshots); left out, each is read from its usual place."""
    from . import cards
    names = _names(cfg)
    provider = work_lines.from_config(cfg) if provider is None else provider
    items = provider.items(now) if provider is not None else []
    if block_rows is None:
        try:
            block_rows = B.all_blocks(now)
        except Exception:
            block_rows = []
    card_rows = cards.open_cards() if card_rows is None else card_rows
    events = read_events(_events_path(cfg), now) if events is None else events
    seen = seen_at(getattr(cfg, "owner_user_id", 0)) if seen is None else seen
    mute = B.muted()
    ended = {e.session for e in entries if e.state == "ended"}
    session_state = {e.session: e.state for e in entries}
    by_short = {s["id"]: s for s in state.get("sessions") or []}
    session_key = {e.session: session_line(cfg, e.chat, e.name) for e in entries}

    lines: Dict[str, dict] = {}

    def line(key: str, shown: str = "") -> dict:
        shown = work_lines.project_name(shown)
        if key not in lines:
            lines[key] = {"key": key, "name": names.get(key) or shown or key, "open": 0, "waiting": 0, "live": False,
                          "updated": 0.0, "url": "", "sessions": [], "why": "", "bad_at": 0.0, "candidates": [],
                          "checklist": None, "muted": False, "muted_by": set()}
        elif shown and lines[key]["name"] == key and not names.get(key):
            lines[key]["name"] = shown
        return lines[key]

    waiting: List[dict] = []
    done_since = 0
    # 1. the work-lines source (the owner ledger)
    for it in items:
        if not it["line"]:
            continue
        ln = line(it["line"], it["name"])
        ln["updated"] = max(ln["updated"], it["updated"])
        if it["status"] == "done":
            if seen and it["updated"] > seen:
                done_since += 1
            continue
        if it["wait"] == "owner":
            ln["waiting"] += 1
            ident = hashlib.sha256(json.dumps([it["line"], it["id"]]).encode("utf-8")).hexdigest()[:24]
            waiting.append({"kind": "ledger", "id": "ledger-" + ident, "line": it["line"], "text": it["text"],
                            "detail": it.get("note", ""), "created": it["updated"],
                            **fact("ledger", _age(now, it["updated"]))})
            continue
        ln["open"] += 1
        ln["candidates"].append(("ledger", it["updated"], it["text"], ""))
    # 2. the sessions
    for e in entries:
        key = session_key.get(e.session, "")
        if not key or e.state == "ended":
            continue
        ln = line(key, (e.name or "").split(":", 1)[0].strip())
        s = by_short.get(e.session[:8], {})
        last = now - e.age if e.age is not None else 0.0
        ln["updated"] = max(ln["updated"], last)
        ln["sessions"].append(e.session[:8])
        if e.state == "working":
            ln["live"] = True
        if s.get("url") and (not ln["url"] or e.state == "working"):
            ln["url"] = s["url"]
        said = s.get("now") or s.get("summary") or ""
        if said and last:
            ln["candidates"].append(("session", last, said, e.session[:8]))
    # 3. the agents' blocks
    asks_note: Dict[str, tuple] = {}
    for rec in block_rows:
        v = B.view(rec, now, ended, mute)
        key = work_lines.line_key(v["line"])
        if not key:
            continue
        ln = line(key, v["line"])
        ln["updated"] = max(ln["updated"], rec["updated"])
        if v["muted"]:
            ln["muted"] = True
            ln["muted_by"].add(v["session"])
            continue
        if v["kind"] == "status":
            ln["candidates"].append(("status" if not v["stale"] else "status_old", rec["updated"], v["text"], v["session"]))
        elif v["kind"] == "checklist":
            if ln["checklist"] is None or ln["checklist"]["age"] > v["age"]:
                ln["checklist"] = {"id": v["id"], "title": v["title"], "items": v["items"], "age": v["age"],
                                   "stale": v["stale"], "session": v["session"]}
        elif v["kind"] == "needs_you" and not v["stale"]:
            asks_note[v["card"]] = (v["text"], v["session"])
    # 4. cards: a progress card that stopped moving marks its line, and is noticed
    noticed: List[dict] = []
    for rec in card_rows:
        if rec.get("kind") != "progress" or rec.get("state", "open") != "open":
            continue
        st = session_state.get(rec["session"], "ended")
        quiet = now - rec["updated"]
        why = "ended" if st == "ended" else ("no_update" if quiet > CARD_STALE and st != "working" else "")
        if not why:
            continue
        key = session_key.get(rec["session"], "")
        url = panel_view.topic_url(rec.get("chat"), rec.get("thread"))
        noticed.append({"kind": "stale_card", "why": why, "line": key, "text": rec["title"], "url": url,
                        "at": rec["updated"], **fact("card", _age(now, rec["updated"]))})
        if key:
            ln = line(key)
            ln["why"] = ln["why"] or "stale_card"
            ln["bad_at"] = max(ln["bad_at"], rec["updated"] + CARD_STALE)
            ln["url"] = ln["url"] or url
    for ev in events:
        noticed.append({"kind": ev["kind"], "why": "", "line": ev["line"], "text": ev["text"], "url": "",
                        "at": ev["at"], **fact("event", _age(now, ev["at"]))})
        if ev["kind"] in ("job_died", "check_failed") and ev["line"]:
            ln = line(ev["line"])
            ln["why"] = ln["why"] or ev["kind"]
            ln["bad_at"] = max(ln["bad_at"], ev["at"])
    promises = sorted((it for it in items if it["status"] == "open" and it["wait"] != "owner"
                       and it["updated"] and now - it["updated"] > PROMISE_AFTER), key=lambda it: it["updated"])
    for it in promises[:3]:
        noticed.append({"kind": "promise", "why": "", "line": it["line"], "text": it["text"], "url": "",
                        "at": it["updated"], **fact("ledger", _age(now, it["updated"]))})
    if provider is not None and not provider.ok:
        noticed.append({"kind": "source_down", "why": provider.name, "line": "", "text": "", "url": "", "at": now,
                        "age": 0, "stale": False})
    # 5. the decisions and question cards that wait for the owner (the page draws them with their own buttons)
    for d in state.get("decisions") or []:
        if d.get("stale") and d.get("stale") != "old":
            continue
        waiting.append({"kind": "decision", "id": d["id"], "line": session_line(cfg, None, d.get("topic") or ""),
                        "text": d.get("title", ""),
                        "note": asks_note.get(d["id"], ("", ""))[0],
                        "note_by": asks_note.get(d["id"], ("", ""))[1], "created": now - (d.get("age") or 0),
                        "age": d.get("age"), "stale": d.get("stale") == "old"})
    for c in state.get("cards") or []:
        if c.get("kind") not in cards.ASKS or c.get("ended"):
            continue
        short = {e.session[:8]: session_key.get(e.session, "") for e in entries}
        waiting.append({"kind": "card", "id": c["id"],
                        "line": short.get(c.get("session"), "") or session_line(cfg, None, c.get("topic") or ""),
                        "text": c.get("title", ""),
                        "note": asks_note.get(c["id"], ("", ""))[0],
                        "note_by": asks_note.get(c["id"], ("", ""))[1], "created": now - (c.get("age") or 0),
                        "age": c.get("age"), "stale": bool(c.get("old"))})
    # 6. each line's sentence and tone
    out_lines = []
    for ln in lines.values():
        if not (ln["open"] or ln["waiting"] or ln["live"] or ln["candidates"] or ln["why"] or ln["checklist"]
                or ln["muted"]):
            continue
        fresh = [c for c in ln["candidates"] if c[0] == "status"]
        pick = max(fresh or ln["candidates"], key=lambda c: c[1]) if ln["candidates"] else None
        src = "status" if pick and pick[0].startswith("status") else (pick[0] if pick else "")
        kind = {"status": "ledger", "ledger": "ledger", "session": "session"}.get(src, "ledger")
        age = _age(now, pick[1]) if pick else _age(now, ln["updated"])
        info = fact(kind, age)
        if pick and pick[0] == "status_old":
            info["stale"] = True
        quiet = now - ln["updated"] if ln["updated"] else None
        if not ln["why"] and ln["open"] and not ln["live"] and quiet is not None and quiet > STUCK_AFTER:
            ln["why"], ln["bad_at"] = "no_move", ln["updated"] + STUCK_AFTER
        tone = ("bad" if ln["why"] else "wait" if ln["waiting"] else
                "ok" if ln["live"] or (quiet is not None and quiet < MOVING) else "idle")
        out_lines.append({"key": ln["key"], "name": ln["name"], "text": pick[2] if pick else "", "source": src,
                          "by": pick[3] if pick else "", "tone": tone, "why": ln["why"], "live": ln["live"],
                          "open": ln["open"], "waiting": ln["waiting"], "url": ln["url"], "muted": ln["muted"],
                          "muted_by": sorted(ln["muted_by"])[:4],
                          "sessions": ln["sessions"][:4], "checklist": ln["checklist"], "bad_at": ln["bad_at"],
                          **info})
    out_lines.sort(key=lambda x: (TONES.index(x["tone"]), x["age"] if x["age"] is not None else 1 << 30))
    out_lines = out_lines[:MAX_LINES]
    line_url = {x["key"]: x["url"] for x in out_lines}
    line_name = {x["key"]: x["name"] for x in out_lines}
    for w in waiting:
        w["url"] = line_url.get(w["line"], "") if w["kind"] == "ledger" else ""
        w["line_name"] = line_name.get(w["line"], names.get(w["line"], w["line"]))
    waiting.sort(key=lambda w: (w["stale"], w["age"] if w["age"] is not None else 1 << 30))
    noticed.sort(key=lambda n: -n["at"])
    for n in noticed:
        n["line_name"] = line_name.get(n["line"], n["line"])
    stuck_since = sum(1 for x in out_lines if x["tone"] == "bad" and seen and x["bad_at"] > seen)
    new_since = sum(1 for w in waiting if seen and w["created"] > seen)
    for x in out_lines:
        x.pop("bad_at")
    for w in waiting:
        w.pop("created")
    for n in noticed:
        n.pop("at")
    sessions = state.get("sessions") or []
    working = [s for s in sessions if s.get("state") == "working"]
    unused = []
    quota_age = None
    for q in state.get("quota") or []:
        used, left = q.get("weekly"), _resets_in(q.get("weekly_resets"), now)
        if isinstance(used, (int, float)) and left is not None and 0 < left < WASTE_RESET and used < WASTE_USED:
            unused.append({"label": q.get("label") or q.get("account") or "", "left": max(0, 100 - round(used))})
        a = _age(now, q.get("at"))
        if a is not None:
            quota_age = a if quota_age is None else max(quota_age, a)
    mac = state.get("mac")
    mac_view = None
    if isinstance(mac, dict) and mac.get("available"):
        mac_view = {"status": mac.get("status") if mac.get("status") in ("ok", "warn", "critical") else "unknown",
                    **fact("mac", _age(now, mac.get("sampled_at")))}
    return {
        "headline": {"moving": sum(1 for x in out_lines if x["tone"] == "ok"),
                     "stuck": sum(1 for x in out_lines if x["tone"] == "bad"), "waiting": len(waiting),
                     "lines": len(out_lines)},
        "since": None if not seen else {"age": _age(now, seen), "done": done_since, "stuck": stuck_since,
                                        "new": new_since},
        "waiting": waiting[:MAX_WAITING],
        "lines": out_lines,
        "noticed": noticed[:MAX_NOTICED],
        "teams": {"working": len(working), "idle": sum(1 for s in sessions if s.get("state") == "idle"),
                  "jobs": sum(int(s.get("bg") or 0) for s in working), "unused": unused,
                  **fact("quota", quota_age)},
        "mac": mac_view,
        "ask": list(ASK),
        "source": provider.name if provider is not None else "",
    }
