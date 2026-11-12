"""The decisions board: a question an agent leaves for the owner in the control panel (`ctb decide ask|list|cancel`).

A decision is a title, an optional detail and two buttons. It lives in a small private state file guarded
by a file lock (the panel server and the CLI are separate processes). The owner answers once, with a tap
in the control panel. The answer is queued to the asking session's inbox as a "panel" message (fixed
server wording, the session's own question and labels quoted as text it wrote) and the choice, without
the note, is shown as a notice in the session's own topic. The panel verified the owner, so a yes there
approves exactly the action the question names and nothing else; the text the session receives says so. The CLI only acts for the session it runs inside: that
session's own Claude Code process must be an ancestor of the calling process.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import time
import unicodedata
from typing import Dict, List, Optional, Tuple

from . import fmt
from .board import BIDI_CONTROLS
from .framing import PANEL_APPROVES
from .config import Config, home_chat, private_dir, state_dir, write_private
from .i18n import t
from .identity import isolate
from .projects import owner_chats
from .pins import SUPERGROUP_PREFIX

STATE_FILE = "decisions.json"
TITLE_CHARS = 200
DETAIL_CHARS = 1500
LABEL_CHARS = 30
NOTE_CHARS = 300
MAX_OPEN = 50
MAX_OPEN_PER_SESSION = 5
LOCK_WAIT = 2.0  # seconds the panel waits for the decisions and inbox locks before answering 503
KEEP_CLOSED = 50  # answered or cancelled decisions kept, newest first, so a late tap gets a clear refusal
ID_RE = re.compile(r"[0-9a-f]{16}")
_CALL_TIMEOUT = 2


class DecisionError(Exception):
    """A refusal; `.reason` is one line for the person (or agent) who asked and `.kind` says why
    ("unknown", "closed", "busy" or "invalid"). A closed decision also carries its `.state` and `.choice`."""

    def __init__(self, reason: str, kind: str = "invalid", state: str = "", choice: str = ""):
        super().__init__(reason)
        self.reason, self.kind, self.state, self.choice = reason, kind, state, choice


# A label must not read as the opposite answer: the owner sees only the label on the button.
READS_AS_NO = {"no", "nope", "לא", "cancel", "deny", "reject", "decline", "refuse", "stop", "abort", "ביטול", "בטל",
               "דחה", "דחייה", "עצור"}
READS_AS_YES = {"yes", "yep", "yeah", "כן", "ok", "okay", "approve", "approved", "confirm", "accept", "agree",
                "proceed", "go", "אשר", "אישור", "מאשר", "אישרתי", "אשרו", "בסדר", "אוקי", "מסכים"}


def reads_as(label: str, words: set) -> bool:
    """Case-insensitive and spaces- and punctuation-blind: the whole label or its first word is one of `words`."""
    first = (re.findall(r"\w+", label.lower()) or [""])[0]
    return "".join(re.findall(r"\w+", label.lower())) in words or first in words


# --- what may be stored ---------------------------------------------------------

QUOTE_MARKS = re.compile("[\u00ab\u00bb\u2039\u203a\u300a\u300b\u226a\u226b\u27ea\u27eb]")  # « » and their look-alikes


def plain(text: str) -> str:
    """Without characters nobody can see but a model can read: controls (a newline and a tab stay), format
    characters (zero-width, direction marks, the invisible tag block) except the emoji joiner."""
    return "".join(c for c in text if c in "\n\t\u200d" or unicodedata.category(c) not in ("Cc", "Cf"))


def quoted(text: str) -> str:
    """Text inside « » in what the session receives: the quote marks themselves cannot be forged."""
    return "«" + " ".join(QUOTE_MARKS.sub("", text).split()) + "»"


def clean(raw, limit: int, what: str, multiline: bool = False, required: bool = True,
          owner_text: bool = False) -> str:
    """Plain text: bidi controls removed (an agent's text must not reorder the lines around it), whitespace
    collapsed per line, at most `limit` characters. Too long is refused, never silently cut. Agent-written
    text is shown remotely (the control panel, the radio, Telegram), so credentials are redacted with the
    alerts channel's cleaning (creds.redact_channel); `owner_text` (the owner's own answer) is kept as written."""
    stripped = plain(BIDI_CONTROLS.sub("", str(raw or "")))
    lines = [" ".join(line.split()) for line in stripped.splitlines()] if multiline else [" ".join(stripped.split())]
    text = "\n".join(lines).strip() if multiline else lines[0]
    if multiline:
        text = re.sub(r"\n{3,}", "\n\n", text)
    if not text and required:
        raise DecisionError(f"the {what} is empty")
    if len(text) > limit:
        raise DecisionError(f"the {what} is too long ({len(text)} characters, at most {limit})")
    if not owner_text:  # checked as written; "[redacted]" may make a short label value a little longer
        from .creds import redact_channel
        text = redact_channel(text)[:limit]
    return text


# --- state ----------------------------------------------------------------------

@contextlib.contextmanager
def _locked(until: Optional[float] = None):
    """The state file's lock. With `until` (a time.monotonic() deadline) it is polled, and a lock still taken
    at the deadline is a "busy" refusal instead of a hang."""
    base = private_dir(state_dir())
    with open(base / (STATE_FILE + ".lock"), "a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX if until is None else fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= until:
                    raise DecisionError("busy, try again", "busy") from None
                time.sleep(0.05)
        yield base / STATE_FILE


def _num(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float)) and value == value
            and abs(value) != float("inf"))


def _record(ident, raw) -> Optional[dict]:
    if not isinstance(raw, dict) or not isinstance(ident, str) or not ID_RE.fullmatch(ident):
        return None
    strings = ("session", "title", "detail", "yes", "no")
    if not all(isinstance(raw.get(k), str) for k in strings) or not raw["session"] or not raw["title"]:
        return None
    if type(raw.get("chat")) is not int or type(raw.get("thread")) is not int or not _num(raw.get("created")):
        return None
    state = raw.get("state")
    if state not in ("open", "answered", "cancelled"):
        return None
    rec = {"id": ident, **{k: raw[k] for k in strings}, "chat": raw["chat"], "thread": raw["thread"],
           "created": float(raw["created"]), "state": state, "choice": "", "note": "", "closed": 0.0}
    if state != "open":
        rec["closed"] = float(raw["closed"]) if _num(raw.get("closed")) else 0.0
    if state == "answered":
        if raw.get("final_state") == "delivered" and _num(raw.get("final_at")):
            rec.update(final_state="delivered", final_at=raw["final_at"])
        if isinstance(raw.get("delivery_key"), str) and ID_RE.fullmatch(raw["delivery_key"]):
            rec["delivery_key"] = raw["delivery_key"]
        rec["choice"] = raw.get("choice") if raw.get("choice") in ("yes", "no") else ""
        rec["note"] = raw.get("note") if isinstance(raw.get("note"), str) else ""
    _notice_fields(rec, raw)
    return rec


def _notice_fields(rec, raw):
    message = raw.get("notice_message")
    if type(message) is int and message != 0:
        rec["notice_message"] = message
    for key in ("notice_alerted", "notice_pending"):
        if raw.get(key) is True:
            rec[key] = True


def _load(path) -> Dict[str, dict]:
    """The decisions by id, with anything malformed dropped (a hand-edited or half-written file must never
    break a command or the panel)."""
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


def open_decisions() -> List[dict]:
    """Every open decision, oldest first. Read without the lock: writes replace the file atomically."""
    data = _load(state_dir() / STATE_FILE)
    return sorted(({k: v for k, v in r.items() if not k.startswith("notice_")}
                   for r in data.values() if r["state"] == "open"), key=lambda r: (r["created"], r["id"]))


# --- the agent's side -------------------------------------------------------------

def create(session: str, chat: int, thread: int, title: str, detail: str = "", yes: str = "", no: str = "",
           lang: str = "en", now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    rec = {"session": session, "chat": chat, "thread": thread,
           "title": clean(title, TITLE_CHARS, "title"),
           "detail": clean(detail, DETAIL_CHARS, "detail", multiline=True, required=False),
           "yes": clean(yes, LABEL_CHARS, "yes label", required=False) or t("decide_yes", lang=lang),
           "no": clean(no, LABEL_CHARS, "no label", required=False) or t("decide_no", lang=lang),
           "created": now, "state": "open"}
    if reads_as(rec["yes"], READS_AS_NO):
        raise DecisionError(f"the yes label {rec['yes']!r} reads as a no; name what yes does, e.g. \"Ship it\"")
    if reads_as(rec["no"], READS_AS_YES):
        raise DecisionError(f"the no label {rec['no']!r} reads as a yes; name what no does, e.g. \"Hold\"")
    if "".join(re.findall(r"\w+", rec["yes"].lower())) == "".join(re.findall(r"\w+", rec["no"].lower())):
        raise DecisionError("the yes and no labels are the same")
    with _locked() as path:
        data = _load(path)
        if sum(1 for r in data.values() if r["state"] == "open") >= MAX_OPEN:
            raise DecisionError(f"there are already {MAX_OPEN} open decisions; cancel one with `decide cancel <id>`")
        if sum(1 for r in data.values() if r["state"] == "open" and r["session"] == session) >= MAX_OPEN_PER_SESSION:
            raise DecisionError(f"this session already has {MAX_OPEN_PER_SESSION} open decisions; "
                                "wait for an answer or cancel one with `decide cancel <id>`")
        ident = secrets.token_hex(8)
        while ident in data:
            ident = secrets.token_hex(8)
        data[ident] = rec = _record(ident, rec)
        _save(path, data)
    return rec


def cancel(session: str, ident: str, now: Optional[float] = None) -> dict:
    """Withdraw one of this session's own open decisions."""
    now = time.time() if now is None else now
    with _locked() as path:
        data = _load(path)
        rec = data.get(ident)
        if rec is None or rec["session"] != session:
            raise DecisionError(f"no decision {ident} from this session", "unknown")
        if rec["state"] != "open":
            raise DecisionError(f"decision {ident} is already {rec['state']}", "closed")
        rec.update(state="cancelled", closed=now, notice_pending=True)
        _save(path, data)
    return rec


def dismiss(idents, now: Optional[float] = None, until: Optional[float] = None) -> List[str]:
    """The owner clears decisions that no longer wait on him (the panel's "clear all" under its stale ones):
    each listed decision that is still open is closed as cancelled. The asking session is not told: these
    are the ones whose session ended, that were asked again, or that are long past their moment. Returns
    the ids closed."""
    now = time.time() if now is None else now
    wanted = {i for i in idents if isinstance(i, str) and ID_RE.fullmatch(i)}
    closed = []
    with _locked(until) as path:
        data = _load(path)
        for ident in sorted(wanted):
            rec = data.get(ident)
            if rec is not None and rec["state"] == "open":
                rec.update(state="cancelled", closed=now, notice_pending=True)
                closed.append(ident)
        if closed:
            _save(path, data)
    return closed


# --- the owner's answer -------------------------------------------------------------

def _where(cfg: Config, rec: dict) -> Optional[Tuple[int, int]]:
    """Where the session's topic is now (it can have moved), else where it was when the question was asked,
    as long as the owner is still obeyed in that chat."""
    from .topics import place
    found = place(cfg, rec["session"])
    if found is not None:
        return found.chat, found.thread
    return (rec["chat"], rec["thread"]) if rec["chat"] in owner_chats(cfg) else None


def delivered_text(rec: dict, choice: str, note: str) -> str:
    """What the session reads. The wording is the server's; the question and the labels are quoted as text
    this session wrote, the note as the owner's own."""
    word, mark, label = ("YES", "✅", rec["yes"]) if choice == "yes" else ("NO", "❌", rec["no"])
    lines = [f"{mark} The owner tapped {word} (button: {quoted(label)}) in the control panel.",
             f"The question this session asked: {quoted(rec['title'])}"]
    if note:
        lines.append(f"The owner's note: {quoted(note)}")
    lines.append(PANEL_APPROVES)
    return "\n".join(lines)


def answer(cfg: Config, inbox, ident: str, choice: str, note: str = "", now: Optional[float] = None) -> dict:
    """Record the owner's answer once and queue it to the asking session. The decision is saved as closed
    first and the message queued second; a queueing that failed before the message was written reopens it
    under the same lock (a delivered answer is never reopened). A busy lock is
    waited for at most LOCK_WAIT seconds in all (DecisionError "busy")."""
    from .inbox import LockBusy
    from .progress import append_boundary
    if choice not in ("yes", "no"):
        raise DecisionError("choose yes or no")
    note = clean(note, NOTE_CHARS, "note", multiline=True, required=False, owner_text=True)
    now = time.time() if now is None else now
    until = time.monotonic() + LOCK_WAIT
    with _locked(until) as path:
        data = _load(path)
        rec = data.get(ident) if isinstance(ident, str) else None
        if rec is None:
            raise DecisionError("no such decision", "unknown")
        if rec["state"] != "open":
            raise DecisionError(f"already {rec['state']}", "closed", rec["state"], rec["choice"])
        found = _where(cfg, rec)
        rec.update(state="answered", choice=choice, note=note, closed=now, delivery_key=secrets.token_hex(8),
                   notice_pending=True)
        _save(path, data)
        try:
            inbox.put(rec["session"], {"text": delivered_text(rec, choice, note), "at": int(now), "id": None,
                                       "source": "panel", "chat": found[0] if found else home_chat(cfg),
                                       "key": rec["delivery_key"]},
                      until=until)
        except BaseException as e:
            rec.update(state="open", choice="", note="", closed=0.0, notice_pending=False)
            rec.pop("delivery_key", None)
            _save(path, data)
            if isinstance(e, LockBusy):
                raise DecisionError("busy, try again", "busy") from None
            raise
    try:  # only for an answer that was delivered, outside the locks, and never for longer than the wait
        append_boundary(rec["session"], until=max(until, time.monotonic() + 0.5))
    except (OSError, TimeoutError) as exc:
        from .inbox import poll_log
        poll_log(f"panel boundary {rec['session'][:8]}: {type(exc).__name__}")
    return dict(rec, where=found)


def announce(cfg: Config, bot, inbox, rec: dict) -> bool:
    """Wake the session and mark its existing topic notice closed; the answer stays private."""
    from .inbox import _deliver_button_word, record_wake_error
    try:
        _deliver_button_word(inbox, rec["session"])
    except Exception as exc:
        record_wake_error(inbox, rec["session"], exc)
    from . import cockpit_notice
    cockpit_notice.later(cfg, rec, bot)
    return False


# --- the commands -------------------------------------------------------------------

def _claude_pids(session: str) -> List[int]:
    from .inbox import claude_pids
    return claude_pids(session)


def _ancestors(pid: int) -> List[int]:
    from .inbox import process_ancestors
    return process_ancestors(pid)


def caller_session() -> str:
    """The session this process runs inside, proven: the session id from the environment names a live Claude
    Code process (from its own session record), and that process is an ancestor of this one. Nothing else
    counts, not a pane or a tmux session name, so a session cannot pass as another one. Fails closed."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not sid:
        raise DecisionError("no session: run it inside a Claude Code session")
    try:
        pids = _claude_pids(sid)
        chain = _ancestors(os.getpid()) if pids else []
    except (OSError, subprocess.SubprocessError, ValueError):
        pids, chain = [], []
    if not pids:
        raise DecisionError("cannot verify this session: no running Claude Code process is recorded for it")
    if not set(pids) & set(chain):
        raise DecisionError("refused: this command is not running inside that session's Claude Code process")
    return sid


def _place_of(cfg: Config, sid: str) -> Tuple[int, int]:
    from .topics import place
    found = place(cfg, sid)
    if found is None:
        raise DecisionError("this session has no Telegram topic")
    if not str(found.chat).startswith(SUPERGROUP_PREFIX):
        raise DecisionError("decisions need a topic in a forum group")
    return found.chat, found.thread


def _fail(reason: str) -> int:
    print(f"Harnessgram: {reason}", file=sys.stderr)
    return 1


def run(cfg: Config, action: str, title: str = "", detail: str = "", yes: str = "", no: str = "",
        ident: str = "", bot=None) -> int:
    """`ctb decide <action>`: 0 when it worked, 1 with a one-line reason when it was refused. The session is
    always the one the command runs inside (see caller_session)."""
    try:
        sid = caller_session()
        chat, thread = _place_of(cfg, sid)
        if action == "list":
            mine = [r for r in open_decisions() if r["session"] == sid]
            for r in mine:
                print(f"{r['id']}  {r['title']}  [{r['yes']} / {r['no']}]")
            if not mine:
                print("(no open decisions)")
            return 0
        if action == "cancel":
            rec = cancel(sid, ident.strip().lower())
            from . import cockpit_notice
            cockpit_notice.update(cfg, rec, False, bot)
            print(f"decision cancelled: {rec['title']}")
            return 0
        if action != "ask":
            raise DecisionError(f"unknown decide command {action}")
        if not cfg.ready:
            raise DecisionError("bot_token / owner_chat_id missing in the config")
        lang = getattr(cfg, "ui_language", "") or "en"
        rec = create(sid, chat, thread, title, detail, yes, no, lang)
        from . import cockpit_notice
        cockpit_notice.update(cfg, rec, True, bot)
        print(f"decision {rec['id']} is waiting in the control panel: {rec['title']}")
        return 0
    except DecisionError as e:
        return _fail(e.reason)
    except Exception as e:
        return _fail(f"decisions are not available ({type(e).__name__})")
