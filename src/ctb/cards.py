"""Agent cards: live cards a session publishes to the owner's control panel (`ctb card ...`).

Five kinds. Three only show something and are updated in place by the session that made them:
`progress` (steps and a percentage), `link` (one https link with an open button) and `numbers` (up to three
figures). Two ask the owner something, once: `choice` (two to six options) and `ask` (a short typed answer).
An answer reaches the asking session exactly like a decision's (see decisions.py): fixed server wording,
the session's own text quoted, queued to its inbox as a "panel" message. Nothing the owner does on a card
is approval for a sensitive action, and the text the session receives says so.

The state file, its lock, the text rules and the proof of the calling session are the decisions module's.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import secrets
import sys
import time
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from . import decisions as D
from .config import Config, home_chat, private_dir, state_dir, write_private

STATE_FILE = "cards.json"
PROGRESS_STALE_AFTER = 30 * 60
PROGRESS_CLOSE_AFTER = 6 * 60 * 60  # legacy constant; unfinished work is not closed by age
PROGRESS_FINISHED_GRACE = 10 * 60
PROGRESS_ENDED_GRACE = 5 * 60
KINDS = ("progress", "choice", "ask", "link", "numbers")
ASKS = ("choice", "ask")  # the owner answers these once; the others only show
TITLE_CHARS = 120
DETAIL_CHARS = 600
STEP_CHARS = 40
LABEL_CHARS = 30
VALUE_CHARS = 14
URL_CHARS = 500
ANSWER_CHARS = 300
MAX_STEPS = 6
MAX_OPTIONS = 6
MAX_FIGURES = 3
MAX_OPEN = 60
MAX_OPEN_PER_SESSION = 8
KEEP_CLOSED = 60
ID_RE = D.ID_RE


class CardError(D.DecisionError):
    pass


def _clean(raw, limit: int, what: str, **kw) -> str:
    try:
        return D.clean(raw, limit, what, **kw)
    except D.DecisionError as e:
        raise CardError(e.reason, e.kind) from None


@contextlib.contextmanager
def _locked(until: Optional[float] = None):
    base = private_dir(state_dir())
    with open(base / (STATE_FILE + ".lock"), "a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX if until is None else fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if until is None:  # a blocking lock that failed (a signal, no locks left): say so, never a TypeError
                    raise CardError("busy, try again", "busy") from None
                if time.monotonic() >= until:
                    raise CardError("busy, try again", "busy") from None
                time.sleep(0.05)
        yield base / STATE_FILE


def _strings(value, limit: int, most: int) -> Optional[List[str]]:
    if not isinstance(value, list) or len(value) > most:
        return None
    if not all(isinstance(s, str) and s and len(s) <= limit for s in value):
        return None
    return list(value)


def _body(kind: str, raw: dict) -> Optional[dict]:
    """The kind's own fields, checked again on every read (the file may have been edited by hand)."""
    if kind == "progress":
        steps = _strings(raw.get("steps", []), STEP_CHARS, MAX_STEPS)
        at, percent = raw.get("at"), raw.get("percent")
        if steps is None or type(at) is not int or not 0 <= at <= len(steps):
            return None
        if percent is not None and (type(percent) is not int or not 0 <= percent <= 100):
            return None
        body = {"steps": steps, "at": at, "percent": percent}
        times = raw.get("step_at")
        if isinstance(times, list) and len(times) == at and all(D._num(x) and x >= 0 for x in times):
            body["step_at"] = [float(x) for x in times]  # when each finished step was reached (the radio's board)
        return body
    if kind == "choice":
        options = _strings(raw.get("options"), LABEL_CHARS, MAX_OPTIONS)
        return {"options": options} if options and len(options) >= 2 else None
    if kind == "ask":
        return {}
    if kind == "link":
        url, label = raw.get("url"), raw.get("label", "")
        if not isinstance(url, str) or not isinstance(label, str) or len(label) > LABEL_CHARS or not _https(url):
            return None
        return {"url": url, "label": label}
    figures = raw.get("figures")
    if not isinstance(figures, list) or not 1 <= len(figures) <= MAX_FIGURES:
        return None
    out = []
    for f in figures:
        if not isinstance(f, dict) or not isinstance(f.get("label"), str) or not isinstance(f.get("value"), str):
            return None
        if not f["label"] or not f["value"] or len(f["label"]) > LABEL_CHARS or len(f["value"]) > VALUE_CHARS:
            return None
        out.append({"label": f["label"], "value": f["value"]})
    return {"figures": out}


def _https(url: str) -> bool:
    """One plain https link: a host, no login part, no whitespace or control characters."""
    if not url or len(url) > URL_CHARS or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname) and parts.username is None and parts.password is None


def _record(ident, raw) -> Optional[dict]:
    if not isinstance(raw, dict) or not isinstance(ident, str) or not ID_RE.fullmatch(ident):
        return None
    kind, state = raw.get("kind"), raw.get("state")
    if kind not in KINDS or state not in ("open", "answered", "closed"):
        return None
    if not all(isinstance(raw.get(k), str) for k in ("session", "title", "detail")) or not raw["session"] or not raw["title"]:
        return None
    if type(raw.get("chat")) is not int or type(raw.get("thread")) is not int:
        return None
    if not D._num(raw.get("created")) or not D._num(raw.get("updated")):
        return None
    body = _body(kind, raw)
    if body is None:
        return None
    rec = {"id": ident, "kind": kind, "session": raw["session"], "title": raw["title"], "detail": raw["detail"],
           "chat": raw["chat"], "thread": raw["thread"], "created": float(raw["created"]),
           "updated": float(raw["updated"]), "state": state, "pick": None, "text": "", "closed": 0.0, **body}
    for key in ("finished_since", "ended_since"):
        if kind == "progress" and D._num(raw.get(key)):
            rec[key] = float(raw[key])
    if kind == "progress" and D._num(raw.get("nudged_at")):
        rec["nudged_at"] = float(raw["nudged_at"])
    if kind == "progress" and raw.get("automatic") is True:
        rec["automatic"] = True
    if kind == "progress" and raw.get("auto_closed") is True:
        rec["auto_closed"] = True
    if state != "open":
        rec["closed"] = float(raw["closed"]) if D._num(raw.get("closed")) else 0.0
    if state == "answered":
        if raw.get("final_state") == "delivered" and D._num(raw.get("final_at")):
            rec.update(final_state="delivered", final_at=raw["final_at"])
        if isinstance(raw.get("delivery_key"), str) and D.ID_RE.fullmatch(raw["delivery_key"]):
            rec["delivery_key"] = raw["delivery_key"]
        pick = raw.get("pick")
        if kind == "choice" and type(pick) is int and 0 <= pick < len(rec["options"]):
            rec["pick"] = pick
        if kind == "ask" and isinstance(raw.get("text"), str):
            rec["text"] = raw["text"]
    D._notice_fields(rec, raw)
    return rec


def _load(path) -> Dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: r for k, r in ((k, _record(k, v)) for k, v in data.items()) if r}


def _save(path, data: Dict[str, dict]) -> None:
    closed = sorted((r for r in data.values() if r["state"] != "open"), key=lambda r: r["closed"], reverse=True)
    for gone in closed[KEEP_CLOSED:]:
        del data[gone["id"]]
    write_private(path, json.dumps(data, ensure_ascii=False))


def open_cards() -> List[dict]:
    """Every open card, oldest first. Read without the lock: writes replace the file atomically."""
    data = _load(state_dir() / STATE_FILE)
    return sorted((r for r in data.values() if r["state"] == "open"), key=lambda r: (r["created"], r["id"]))


def finished(rec: dict) -> bool:
    return rec.get("kind") == "progress" and (rec.get("percent") == 100 or
        bool(rec.get("steps")) and rec.get("at") == len(rec["steps"]))


def is_stale(rec: dict, now: float) -> bool:
    return not finished(rec) and now - rec["updated"] > PROGRESS_STALE_AFTER


def reconcile_progress(ended: set, now: Optional[float] = None, elapsed: Optional[float] = None) -> List[dict]:
    """Close after sustained observations; elapsed time is independent of wall-clock changes."""
    now = time.time() if now is None else now
    elapsed = time.monotonic() if elapsed is None else elapsed
    with _locked() as path:
        data = _load(path)
        changed = False
        for rec in data.values():
            if rec["kind"] != "progress" or rec["state"] != "open":
                continue
            for key, applies, grace in (("finished_since", finished(rec), PROGRESS_FINISHED_GRACE),
                                        ("ended_since", rec["session"] in ended, PROGRESS_ENDED_GRACE)):
                since = rec.get(key)
                if not applies:
                    if key in rec:
                        del rec[key]
                        changed = True
                elif since is None or elapsed < since:
                    rec[key] = elapsed
                    changed = True
                elif elapsed - since >= grace:
                    rec.update(state="closed", closed=now, auto_closed=True)
                    changed = True
                    break
        if changed:
            _save(path, data)
        return sorted((r for r in data.values() if r["state"] == "open" or r.get("auto_closed")),
                      key=lambda r: (r["created"], r["id"]))


def dismiss_progress(ident: str) -> bool:
    """Dismiss only an automatically closed progress card."""
    with _locked() as path:
        data = _load(path)
        rec = data.get(ident)
        if not rec or rec["state"] != "closed" or not rec.get("auto_closed"):
            return False
        rec.pop("auto_closed", None)
        _save(path, data)
        return True


def stale_reminder(session: str, now: float) -> str:
    """Claim one stale card reminder under the same lock as updates."""
    with _locked() as path:
        data = _load(path)
        last = max((r.get("nudged_at", 0) for r in data.values()
                    if r["session"] == session and r["kind"] == "progress"), default=0)
        if last and 0 <= now - last < PROGRESS_STALE_AFTER:
            return ""
        eligible = [r for r in data.values() if r["session"] == session and r["kind"] == "progress"
                    and r["state"] == "open" and is_stale(r, now)
                    and (not r.get("nudged_at") or r.get("nudged_at", 0) < r["updated"] or now < r.get("nudged_at", 0))]
        if not eligible:
            return ""
        rec = min(eligible, key=lambda r: (r.get("nudged_at", 0), r["updated"]))
        rec["nudged_at"] = now
        _save(path, data)
        return rec["id"]


def sync_todos(session: str, chat: int, thread: int, todos: list, title: str,
               now: Optional[float] = None) -> Optional[dict]:
    """Reflect a complete TodoWrite snapshot in this session's own automatic card."""
    if not session or not isinstance(todos, list):
        return None
    now = time.time() if now is None else now
    from .creds import redact
    valid = []
    for item in todos:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str) or item.get("status") not in ("pending", "in_progress", "completed"):
            continue
        try:
            text = _clean(redact(item["content"])[:STEP_CHARS], STEP_CHARS, "step")
        except CardError:
            continue
        valid.append((text, item["status"]))
    completed = [x for x in valid if x[1] == "completed"]
    remaining = [x for x in valid if x[1] == "in_progress"] + [x for x in valid if x[1] == "pending"]
    shown_done = completed[-min(2, max(0, MAX_STEPS - len(remaining))):] if len(remaining) < MAX_STEPS else []
    ordered = (shown_done + remaining)[:MAX_STEPS]
    body = {"steps": [x[0] for x in ordered], "at": len(shown_done),
            "percent": min(99 if remaining else 100, round(100 * len(completed) / len(valid))) if valid else 0}
    with _locked() as path:
        data = _load(path)
        rec = next((r for r in data.values() if r["session"] == session and r["kind"] == "progress"
                    and r["state"] == "open" and r.get("automatic")), None)
        manual = next((r for r in data.values() if r["session"] == session and r["kind"] == "progress"
                         and r["state"] == "open" and not r.get("automatic")), None)
        if manual:
            if rec:
                rec.update(state="closed", closed=now)
                _save(path, data)
            return None
        if valid and not remaining:
            if rec:
                rec.update(updated=now, step_at=step_times(rec, body, now), **body)
                rec.update(state="closed", closed=now, auto_closed=True)
                _save(path, data)
            return rec
        if rec:
            if all(rec.get(key) == value for key, value in body.items()):
                return rec
            rec.update(updated=now, step_at=step_times(rec, body, now), **body)
        else:
            if sum(r["state"] == "open" for r in data.values()) >= MAX_OPEN or sum(
                r["state"] == "open" and r["session"] == session for r in data.values()) >= MAX_OPEN_PER_SESSION:
                return None
            ident = secrets.token_hex(8)
            while ident in data:
                ident = secrets.token_hex(8)
            rec = {"id": ident, "kind": "progress", "session": session, "chat": chat, "thread": thread,
                   "title": _clean(title, TITLE_CHARS, "title"), "detail": "", "created": now, "updated": now,
                   "state": "open", "closed": 0.0, "automatic": True, "step_at": [now] * body["at"], **body}
            data[ident] = rec
        _save(path, data)
        return rec


# --- the agent's side -------------------------------------------------------------

def _fields(kind: str, steps=None, at=None, percent=None, options=None, url="", label="", figures=None) -> dict:
    """The kind's fields from what the command line gave, cleaned and checked; a CardError says what is wrong."""
    if kind == "progress":
        steps = [_clean(s, STEP_CHARS, "step") for s in (steps or [])]
        if len(steps) > MAX_STEPS:
            raise CardError(f"at most {MAX_STEPS} steps")
        at = 0 if at is None else at
        if type(at) is not int or not 0 <= at <= len(steps):
            raise CardError(f"--at is the number of finished steps, 0 to {len(steps)}")
        if percent is not None and (type(percent) is not int or not 0 <= percent <= 100):
            raise CardError("--percent is a whole number from 0 to 100")
        if not steps and percent is None:
            raise CardError("a progress card needs --step or --percent")
        return {"steps": steps, "at": at, "percent": percent}
    if kind == "choice":
        options = [_clean(o, LABEL_CHARS, "option") for o in (options or [])]
        if not 2 <= len(options) <= MAX_OPTIONS:
            raise CardError(f"a choice card needs 2 to {MAX_OPTIONS} options")
        if len({"".join(o.lower().split()) for o in options}) != len(options):
            raise CardError("two options read the same")
        return {"options": options}
    if kind == "ask":
        return {}
    if kind == "link":
        if not _https(url or ""):
            raise CardError("a link card needs one https:// link (no login part, no spaces)")
        from .creds import hide_owner_links, redact_channel
        if hide_owner_links(redact_channel(url)) != url:
            raise CardError("the link holds something that looks like a credential or a private sign-in link; "
                            "send it without that part (it is shown in the control panel)")
        return {"url": url, "label": _clean(label, LABEL_CHARS, "label", required=False)}
    out = []
    for f in figures or []:
        name, sep, value = str(f).partition("=")
        if not sep:
            raise CardError("a figure is written label=value")
        from .creds import redact_pair
        name, value = redact_pair(_clean(name, LABEL_CHARS, "figure label"), _clean(value, VALUE_CHARS, "figure value"))
        out.append({"label": name, "value": value})
    if not 1 <= len(out) <= MAX_FIGURES:
        raise CardError(f"a numbers card needs 1 to {MAX_FIGURES} figures")
    return {"figures": out}


def publish(session: str, chat: int, thread: int, kind: str, title: str, detail: str = "", ident: str = "",
            now: Optional[float] = None, **fields) -> dict:
    """A new card, or with `ident` this session's own open card of the same kind replaced in place.
    A card that asks the owner something is never changed after it was published."""
    if kind not in KINDS:
        raise CardError(f"unknown card kind {kind}")
    now = time.time() if now is None else now
    if ident and kind == "progress":
        # an update says only what moved: the steps, the title and the detail it leaves out stay as they were
        old = next((r for r in _load(state_dir() / STATE_FILE).values() if (r["state"] == "open" or r.get("auto_closed")) and r["id"] == ident and r["session"] == session
                    and r["kind"] == kind), None)
        if old is None:  # said before the title and step checks, so a wrong id is named as the problem
            raise CardError(f"no open card {ident} from this session", "unknown")
        if old is not None:
            if fields.get("percent") is None:
                fields["percent"] = old.get("percent")
            if not fields.get("steps"):
                fields["steps"] = list(old.get("steps") or [])
                if fields.get("at") is None:
                    fields["at"] = old.get("at")
            title = title if (title or "").strip() else old["title"]
            detail = detail if (detail or "").strip() else old.get("detail", "")
    body = _fields(kind, **fields)
    title = _clean(title, TITLE_CHARS, "title")
    detail = _clean(detail, DETAIL_CHARS, "detail", multiline=True, required=False)
    with _locked() as path:
        data = _load(path)
        if ident:
            old = data.get(ident)
            if old is None or old["session"] != session or (old["state"] != "open" and not (kind == "progress" and old.get("auto_closed"))):
                raise CardError(f"no open card {ident} from this session", "unknown")
            if old["kind"] != kind:
                raise CardError(f"card {ident} is a {old['kind']} card")
            if kind in ASKS:
                raise CardError("a card that asks the owner is not edited; close it and publish a new one")
            raw = dict(old, title=title, detail=detail, updated=now, **body)
            if kind == "progress":
                raw.update(state="open", closed=0.0)
                for key in ("auto_closed", "finished_since", "ended_since"):
                    raw.pop(key, None)
                raw["step_at"] = step_times(old, body, now)
        else:
            mine = [r for r in data.values() if r["state"] == "open" and not r.get("automatic")]
            if len(mine) >= MAX_OPEN:
                raise CardError(f"there are already {MAX_OPEN} open cards; close one with `card close <id>`")
            if sum(1 for r in mine if r["session"] == session) >= MAX_OPEN_PER_SESSION:
                raise CardError(f"this session already has {MAX_OPEN_PER_SESSION} open cards; "
                                "update one with --id or close one with `card close <id>`")
            ident = secrets.token_hex(8)
            while ident in data:
                ident = secrets.token_hex(8)
            raw = {"kind": kind, "session": session, "chat": chat, "thread": thread, "title": title,
                   "detail": detail, "created": now, "updated": now, "state": "open", **body}
            if kind == "progress":
                raw["step_at"] = [now] * body["at"]
        if kind == "progress" and ident not in data:
            for other in data.values():
                if other["session"] == session and other["kind"] == "progress" and other["state"] == "open" and other.get("automatic"):
                    other.update(state="closed", closed=now)
        rec = _record(ident, raw)
        if rec is None:
            raise CardError("the card is not valid")
        data[ident] = rec
        _save(path, data)
    return rec


def step_times(old: dict, body: dict, now: float) -> List[float]:
    """The finish time of each finished step after an update: a step that was already finished under the same
    wording keeps its time, a newly finished one is stamped `now`."""
    before = list(old.get("step_at") or [])
    steps, old_steps = body["steps"], list(old.get("steps") or [])
    out = []
    for n in range(body["at"]):
        was_done = n < old.get("at", 0) and n < len(old_steps) and old_steps[n] == steps[n]
        if not was_done:
            out.append(now)
        else:  # finished before this update: its own time, or 0 (unknown) for a card written before times were kept
            out.append(before[n] if n < len(before) else 0.0)
    return out


def close(session: str, ident: str, now: Optional[float] = None) -> dict:
    """Take one of this session's own open cards off the panel."""
    now = time.time() if now is None else now
    with _locked() as path:
        data = _load(path)
        rec = data.get(ident)
        if rec is None or rec["session"] != session:
            raise CardError(f"no card {ident} from this session", "unknown")
        if rec["state"] != "open":
            raise CardError(f"card {ident} is already {rec['state']}", "closed")
        rec.update(state="closed", closed=now, notice_pending=True)
        _save(path, data)
    return rec


def close_session(session: str, now: Optional[float] = None) -> int:
    """Every open card of a session that ended; how many were closed."""
    now = time.time() if now is None else now
    with _locked() as path:
        data = _load(path)
        mine = [r for r in data.values() if r["session"] == session and r["state"] == "open"]
        for rec in mine:
            rec.update(state="closed", closed=now, notice_pending=True)
        if mine:
            _save(path, data)
    return len(mine)


# --- the owner's side ---------------------------------------------------------------

def delivered_text(rec: dict) -> str:
    """What the session reads: the server's wording, its own title and option quoted, the owner's words quoted."""
    if rec["kind"] == "choice":
        first = (f"🔘 The owner chose option {rec['pick'] + 1} of {len(rec['options'])} "
                 f"(button: {D.quoted(rec['options'][rec['pick']])}) on a card in the control panel.")
    else:
        first = f"✍️ The owner answered a card in the control panel: {D.quoted(rec['text'])}"
    return "\n".join([first, f"The question this session asked: {D.quoted(rec['title'])}", D.PANEL_APPROVES])


def answer(cfg: Config, inbox, ident: str, pick=None, text: str = "", now: Optional[float] = None) -> dict:
    """Record the owner's answer once and queue it to the asking session (the same order and the same
    rollback as decisions.answer: saved closed first, reopened only when the message was never queued)."""
    from .inbox import LockBusy
    from .progress import append_boundary
    now = time.time() if now is None else now
    until = time.monotonic() + D.LOCK_WAIT
    with _locked(until) as path:
        data = _load(path)
        rec = data.get(ident) if isinstance(ident, str) else None
        if rec is None:
            raise CardError("no such card", "unknown")
        if rec["kind"] not in ASKS:
            raise CardError("this card does not ask anything", "unknown")
        if rec["state"] != "open":
            raise CardError(f"already {rec['state']}", "closed", rec["state"])
        if rec["kind"] == "choice":
            if type(pick) is not int or not 0 <= pick < len(rec["options"]):
                raise CardError("choose one of the options")
            rec.update(pick=pick)
        else:
            rec.update(text=_clean(text, ANSWER_CHARS, "answer", multiline=True, owner_text=True))
        found = D._where(cfg, rec)
        rec.update(state="answered", closed=now, delivery_key=secrets.token_hex(8), notice_pending=True)
        _save(path, data)
        try:
            inbox.put(rec["session"], {"text": delivered_text(rec), "at": int(now), "id": None, "source": "panel",
                                       "chat": found[0] if found else home_chat(cfg),
                                       "key": rec["delivery_key"]}, until=until)
        except BaseException as e:
            rec.update(state="open", pick=None, text="", closed=0.0, notice_pending=False)
            rec.pop("delivery_key", None)
            _save(path, data)
            if isinstance(e, LockBusy):
                raise CardError("busy, try again", "busy") from None
            raise
    try:
        append_boundary(rec["session"], until=max(until, time.monotonic() + 0.5))
    except (OSError, TimeoutError) as exc:
        from .inbox import poll_log
        poll_log(f"panel boundary {rec['session'][:8]}: {type(exc).__name__}")
    return dict(rec, where=found)


def wake(inbox, rec: dict) -> None:
    """After an answer: wake the session like a button tap. Never raises."""
    from .inbox import _deliver_button_word, record_wake_error
    try:
        _deliver_button_word(inbox, rec["session"])
    except Exception as exc:
        record_wake_error(inbox, rec["session"], exc)


def view(rec: dict, now: float) -> dict:
    """A card as the panel page gets it (no session id beyond its short form, no chat ids)."""
    out = {"id": rec["id"], "kind": rec["kind"], "title": rec["title"], "detail": rec["detail"],
           "age": max(0, int(now - rec["updated"])), "session": rec["session"][:8]}
    for key in ("steps", "at", "percent", "options", "url", "label", "figures"):
        if key in rec:
            out[key] = rec[key]
    return out


# --- the commands -------------------------------------------------------------------

def _line(rec: dict) -> str:
    return f"{rec['id']}  {rec['kind']}  {rec['title']}"


def run(cfg: Config, action: str, title: str = "", detail: str = "", ident: str = "", **fields) -> int:
    """`ctb card <action>`: 0 when it worked, 1 with a one-line reason when it was refused. The session is
    always the one the command runs inside (decisions.caller_session)."""
    try:
        sid = D.caller_session()
        chat, thread = D._place_of(cfg, sid)
        ident = (ident or "").strip().lower()
        if action == "list":
            mine = [r for r in open_cards() if r["session"] == sid]
            print("\n".join(_line(r) for r in mine) if mine else "(no open cards)")
            return 0
        if action == "close":
            rec = close(sid, ident)
            if rec["kind"] in ASKS:
                from . import cockpit_notice
                cockpit_notice.update(cfg, rec, False)
            print(f"card closed: {rec['title']}")
            return 0
        if action not in KINDS:
            raise CardError(f"unknown card command {action}")
        rec = publish(sid, chat, thread, action, title, detail, ident, **fields)
        if action in ASKS:
            from . import cockpit_notice
            cockpit_notice.update(cfg, rec, True)
        waits = " and waits for the owner's answer" if action in ASKS else ""
        print(f"card {rec['id']} is in the control panel{waits}: {rec['title']}")
        return 0
    except D.DecisionError as e:
        print(f"Harnessgram: {e.reason}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Harnessgram: cards are not available ({type(e).__name__})", file=sys.stderr)
        return 1
