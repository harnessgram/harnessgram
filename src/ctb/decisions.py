"""The decisions board: a question an agent leaves for the owner in the control panel (`ctb decide ask|list|cancel`).

A decision is a title, an optional detail (markdown-lite: paragraphs, "- " bullets, **bold**; shown as text, never
as markup), optional pictures (private copies, served only to the owner) and https links, and two buttons. It lives in a small private state file guarded
by a file lock (the panel server and the CLI are separate processes). The owner answers once, with a tap
in the control panel, with an optional comment in their own words; they may also send a comment alone, which
answers nothing and leaves the decision open (`reply`). The answer is queued to the asking session's inbox as a "panel" message (fixed
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

from . import decision_sign
from .board import BIDI_CONTROLS
from . import framing
from .framing import PANEL_APPROVES
from .config import Config, home_chat, private_dir, state_dir, write_private
from .i18n import t
from .identity import isolate
from .projects import owner_chats
from .pins import SUPERGROUP_PREFIX

STATE_FILE = "decisions.json"
TITLE_CHARS = 200
DETAIL_CHARS = 4000
LABEL_CHARS = 30
NOTE_CHARS = 1000
LEGACY_DETAIL_CHARS = 1500
LEGACY_NOTE_CHARS = 300
MAX_IMAGES = 6
MAX_LINKS = 6
LINK_LABEL_CHARS = 60
URL_CHARS = 2048
MAX_REPLIES = 20  # comments sent without deciding, per decision; the newest REPLIES_SHOWN are shown back
REPLIES_SHOWN = 5
MAX_OPEN = 50
MAX_OPEN_PER_SESSION = 5
LOCK_WAIT = 2.0  # seconds the panel waits for the decisions and inbox locks before answering 503
KEEP_CLOSED = 50  # answered or cancelled decisions kept, newest first, so a late tap gets a clear refusal
ID_RE = re.compile(r"[0-9a-f]{16}")
PERM_RE = ID_RE  # the id of the Claude Code permission request a decision answers (permission_request.py)
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
           "created": float(raw["created"]), "state": state, "choice": "", "note": "", "closed": 0.0,
           # only an explicit True: an old, damaged or unmarked record is never routine (sensitive_buttons.py)
           "routine": raw.get("routine") is True}
    if type(raw.get("msg")) is int and raw["msg"] > 0:
        rec["msg"] = raw["msg"]
    images = _images(raw.get("images"))
    if images:
        rec["images"] = images
    links = _links(raw.get("links"))
    if links:
        rec["links"] = links
    replies = raw.get("replies")
    if isinstance(replies, list):
        kept = [{"at": float(r["at"]), "text": r["text"]} for r in replies[-MAX_REPLIES:]
                if isinstance(r, dict) and _num(r.get("at")) and isinstance(r.get("text"), str)
                and 0 < len(r["text"]) <= NOTE_CHARS]
        if kept:
            rec["replies"] = kept
    if raw.get("action") == "emergency_credit":
        rec["action"] = "emergency_credit"
        rec["routine"] = False
    if isinstance(raw.get("perm"), str) and PERM_RE.fullmatch(raw["perm"]):
        rec["perm"] = raw["perm"]  # answered only through permission_request.decide, never queued as a message
        # The exact request, verbatim (never cleaned: the owner approves what Claude Code will run), and whether the
        # phone may only decline it (permission_request.describe).
        rec["request"] = raw["request"] if isinstance(raw.get("request"), str) and len(raw["request"]) <= 4000 else ""
        rec["decline_only"] = raw.get("decline_only") is not False or not rec["request"]
    if state != "open":
        rec["closed"] = float(raw["closed"]) if _num(raw.get("closed")) else 0.0
    if state == "answered":
        if raw.get("answered_via") in decision_sign.VIAS | {"mcp"}:
            rec["answered_via"] = raw["answered_via"]
        if raw.get("final_state") == "delivered" and _num(raw.get("final_at")):
            rec.update(final_state="delivered", final_at=raw["final_at"])
        if isinstance(raw.get("delivery_key"), str) and ID_RE.fullmatch(raw["delivery_key"]):
            rec["delivery_key"] = raw["delivery_key"]
        rec["choice"] = raw.get("choice") if raw.get("choice") in ("yes", "no") else ""
        rec["note"] = raw.get("note") if isinstance(raw.get("note"), str) else ""
        if decision_sign.well_formed(raw.get("answer_sig")):  # the bridge's signature (decision_sign.py), kept as is
            rec["answer_sig"] = {k: raw["answer_sig"][k] for k in decision_sign.SIG_FIELDS}
    _notice_fields(rec, raw)
    return rec


def _images(raw) -> List[str]:
    """Stored picture ids, or [] for anything malformed (an old record has none)."""
    from . import card_media
    if not isinstance(raw, list) or len(raw) > MAX_IMAGES:
        return []
    ids = [i for i in raw if isinstance(i, str) and card_media.ID.fullmatch(i)]
    return ids if len(ids) == len(raw) and len(set(ids)) == len(ids) else []


def _links(raw) -> List[dict]:
    """Stored links, re-checked on every load: each one https only, or the whole list is dropped."""
    if not isinstance(raw, list) or len(raw) > MAX_LINKS:
        return []
    out = []
    for item in raw:
        if (not isinstance(item, dict) or not isinstance(item.get("url"), str) or not isinstance(item.get("label"), str)
                or len(item["label"]) > LINK_LABEL_CHARS or not https_link(item["url"])):
            return []
        out.append({"label": item["label"], "url": item["url"]})
    return out


def https_link(url: str) -> bool:
    """One plain https link: a host, no login part, no whitespace or control characters, nothing that reads as a
    credential or a private sign-in link (the same rules as a link card)."""
    if not isinstance(url, str) or not url or len(url) > URL_CHARS or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in url) or BIDI_CONTROLS.search(url):
        return False
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - a malformed port raises here
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
        return False
    from .creds import hide_owner_links, redact_channel
    return hide_owner_links(redact_channel(url)) == url


def parse_link(raw: str) -> dict:
    """`--link "label=https://..."` (or a bare https link) as {"label", "url"}; refused when it is not one https link."""
    text = str(raw or "").strip()
    if text.lower().startswith("https://"):
        label, url = "", text
    elif "=" in text:
        label, url = (part.strip() for part in text.split("=", 1))
    else:
        raise DecisionError("a link is label=https://... (or just the https:// link)")
    if not https_link(url):
        raise DecisionError("each link must be one https:// link (no login part, no spaces, no credentials or "
                            "private sign-in links)")
    return {"label": clean(label, LINK_LABEL_CHARS, "link label", required=False), "url": url}


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
    _sweep_media(data.values())


def _sweep_media(records, now: Optional[float] = None) -> None:
    """Under the decisions lock: a picture lives while its decision is open and a short grace after it closes (a late
    tap still sees it); files no record names are removed after the same grace (a copy made before the lock)."""
    from . import card_media
    if not any(r.get("images") for r in records) and not (state_dir() / card_media.DECISION_DIR).exists():
        return
    card_media.cleanup(list(records), now, name=card_media.DECISION_DIR)


def image_available(ident: str, now: Optional[float] = None) -> bool:
    """Whether `ident` is a picture of a decision that is open (or closed within the grace)."""
    from . import card_media
    if not isinstance(ident, str) or not card_media.ID.fullmatch(ident):
        return False
    now = time.time() if now is None else now
    return any(ident in r.get("images", []) and (r["state"] == "open" or now - r["closed"] < card_media.GRACE)
               for r in _load(state_dir() / STATE_FILE).values())


def image(ident: str):
    """(bytes, content type) of one decision picture the owner may see, or None."""
    from . import card_media
    return card_media.read(ident, name=card_media.DECISION_DIR) if image_available(ident) else None


def open_decisions() -> List[dict]:
    """Every open decision, oldest first. Read without the lock: writes replace the file atomically."""
    data = _load(state_dir() / STATE_FILE)
    from . import permission_request
    return sorted(({k: v for k, v in r.items() if not k.startswith("notice_")}
                   for r in data.values() if r["state"] == "open"
                   and (not r.get("perm") or permission_request.pending(r["perm"]))),
                  key=lambda r: (r["created"], r["id"]))


# --- the agent's side -------------------------------------------------------------

def create(session: str, chat: int, thread: int, title: str, detail: str = "", yes: str = "", no: str = "",
           lang: str = "en", now: Optional[float] = None, perm: str = "", request: str = "",
           decline_only: bool = True, routine: bool = False, action: str = "", images=(), links=(),
           rich: bool = False) -> dict:
    """`images`: paths of pictures the owner needs to decide (PNG, JPEG or WebP; private, metadata-free copies are
    stored, the sources are never named). `links`: "label=https://..." strings or {"label", "url"} dicts.
    `perm`: the id of a Claude Code permission request (permission_request.py only). Its answer goes to that
    request's hook, never to the session as a message. `routine`: the asking agent says this question approves
    nothing sensitive, so the owner may also answer it from a phone widget. Text that names a sensitive area keeps it
    sensitive anyway (sensitive_buttons.py)."""
    now = time.time() if now is None else now
    if perm and not PERM_RE.fullmatch(perm):
        raise DecisionError("bad permission request id")
    rec = {"session": session, "chat": chat, "thread": thread,
           "title": clean(title, TITLE_CHARS, "title"),
           "detail": clean(detail, DETAIL_CHARS if rich else LEGACY_DETAIL_CHARS, "detail", multiline=True,
                           required=False),
           "yes": clean(yes, LABEL_CHARS, "yes label", required=False) or t("decide_yes", lang=lang),
           "no": clean(no, LABEL_CHARS, "no label", required=False) or t("decide_no", lang=lang),
           "created": now, "state": "open", "routine": routine is True and not perm,
           **({"perm": perm, "request": request, "decline_only": bool(decline_only)} if perm else {}),
           **({"action": action} if action == "emergency_credit" else {})}
    if reads_as(rec["yes"], READS_AS_NO):
        raise DecisionError(f"the yes label {rec['yes']!r} reads as a no; name what yes does, e.g. \"Ship it\"")
    if reads_as(rec["no"], READS_AS_YES):
        raise DecisionError(f"the no label {rec['no']!r} reads as a yes; name what no does, e.g. \"Hold\"")
    if "".join(re.findall(r"\w+", rec["yes"].lower())) == "".join(re.findall(r"\w+", rec["no"].lower())):
        raise DecisionError("the yes and no labels are the same")
    images, links = list(images or ()), list(links or ())
    if (images or links) and not rich:
        raise DecisionError("rich decisions are disabled")
    if (images or links) and perm:
        raise DecisionError("a permission request carries no pictures or links")
    if len(images) > MAX_IMAGES:
        raise DecisionError(f"at most {MAX_IMAGES} pictures")
    if len(links) > MAX_LINKS:
        raise DecisionError(f"at most {MAX_LINKS} links")
    parsed = [dict(link) if isinstance(link, dict) else parse_link(link) for link in links]
    if parsed:
        if _links(parsed) != parsed:
            raise DecisionError("each link must be one https:// link with a short label")
        rec["links"] = parsed
    copied: List[str] = []
    if images:
        from . import card_media
        try:
            copied = card_media.store_many(images, name=card_media.DECISION_DIR)
        except card_media.MediaError as e:
            raise DecisionError(str(e)) from None
        rec["images"] = copied
    try:
        return _create_locked(session, rec)
    except BaseException:
        if copied:
            from . import card_media
            card_media.discard(copied, name=card_media.DECISION_DIR)
        raise


def _create_locked(session: str, rec: dict) -> dict:
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


def settle_permission(ident: str, state: str, choice: str = "", now: Optional[float] = None) -> Optional[dict]:
    """Close an open permission decision (permission_request.py): "answered" with the owner's choice, or
    "cancelled" when its request closed. The topic card says what happened; nothing is queued."""
    now = time.time() if now is None else now
    with _locked() as path:
        data = _load(path)
        rec = data.get(ident) if isinstance(ident, str) else None
        if rec is None or rec["state"] != "open" or not rec.get("perm"):
            return None
        if state == "answered" and choice in ("yes", "no"):
            rec.update(state="answered", choice=choice, closed=now, final_state="delivered", final_at=now)
        else:
            rec.update(state="cancelled", closed=now)
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


def delivered_text(rec: dict, choice: str, note: str, source: str = "panel") -> str:
    """What the session reads. The wording is the server's; the question and the labels are quoted as text
    this session wrote, the note as the owner's own."""
    word, mark, label = ("YES", "✅", rec["yes"]) if choice == "yes" else ("NO", "❌", rec["no"])
    if source == "mcp":
        lines = [f"{mark} An MCP connector, not the owner, answered {word} (button: {quoted(label)}).",
                 f"The question this session asked: {quoted(rec['title'])}"]
        if note:
            lines.append(f"The connector's note: {quoted(note)}")
        lines.append(framing.NEVER_APPROVAL)
        return "\n".join(lines)
    lines = [f"{mark} The owner tapped {word} (button: {quoted(label)}) in the control panel.",
             f"The question this session asked: {quoted(rec['title'])}"]
    if note:
        lines.append(f"The owner's note: {quoted(note)}")
    lines.append(PANEL_APPROVES)
    return "\n".join(lines)


def answer(cfg: Config, inbox, ident: str, choice: str, note: str = "", now: Optional[float] = None,
           check=None, by: Optional[int] = None, via: str = "", session: Optional[str] = None) -> dict:
    """Record the owner's answer once and queue it to the asking session. The decision is saved as closed
    first and the message queued second; a queueing that failed before the message was written reopens it
    under the same lock (a delivered answer is never reopened). A busy lock is
    waited for at most LOCK_WAIT seconds in all (DecisionError "busy"). `check(rec)`, when given, sees the open
    record under the lock before anything is changed and may refuse it by raising DecisionError (the widget
    route refuses a sensitive decision this way, so no other answer path can slip between its check and the write).
    `by` (the Telegram user id the calling route verified, never a value from a request body) and `via` (the route:
    decision_sign.VIAS) are signed into the record's `answer_sig`; without them, or when signing is unavailable, the
    answer is saved unsigned and still delivered.
    `session`, when given, limits the answer to that session's own decisions: another session's decision is refused
    exactly like a missing one, before any check that would tell its state (closed, permission request)."""
    from .inbox import LockBusy
    from .progress import append_boundary
    if choice not in ("yes", "no"):
        raise DecisionError("choose yes or no")
    note_limit = NOTE_CHARS if cfg.rich_decisions_enabled is True else LEGACY_NOTE_CHARS
    note = clean(note, note_limit, "note", multiline=True, required=False, owner_text=True)
    now = time.time() if now is None else now
    key = decision_sign.key_for_answer(by, via)  # outside the lock: a slow Keychain never holds it
    until = time.monotonic() + LOCK_WAIT
    unsigned = ""
    with _locked(until) as path:
        data = _load(path)
        rec = data.get(ident) if isinstance(ident, str) else None
        if rec is None or (session is not None and rec["session"] != session):
            raise DecisionError("no such decision", "unknown")
        if rec["state"] != "open":
            raise DecisionError(f"already {rec['state']}", "closed", rec["state"], rec["choice"])
        if rec.get("perm"):  # a Claude Code permission request: answered by permission_request.decide only
            raise DecisionError("a permission request is answered by its own hook", "invalid")
        if check is not None:
            check(dict(rec))
        found = _where(cfg, rec)
        if rec.get("action") == "emergency_credit":
            # A bridge action, never an instruction sent to the model. The durable answer
            # is consumed by the heartbeat; closing under this lock makes a tap single-use.
            from . import credit_mode
            if choice == "yes":
                credit_mode.check_answer(cfg, rec, now)
            rec.update(state="answered", choice=choice, note="", closed=float(now),
                       delivery_key=secrets.token_hex(8), notice_pending=True,
                       final_state="delivered", final_at=float(now))
            _save(path, data)
            return dict(rec, where=found)
        rec.update(state="answered", choice=choice, note=note, closed=float(now), delivery_key=secrets.token_hex(8),
                   notice_pending=True, answered_via="mcp" if via == "mcp" else (via or "panel"))
        if key is not None:
            try:
                rec["answer_sig"] = decision_sign.sign(rec, by, via, key)
            except Exception as e:  # noqa: BLE001 - a failure to sign never blocks the owner's answer
                unsigned = type(e).__name__
        _save(path, data)
        try:
            source = "mcp" if via == "mcp" else "panel"
            queued = {"text": delivered_text(rec, choice, note, source), "at": int(now), "id": None,
                      "source": "panel", "chat": found[0] if found else home_chat(cfg),
                      "key": rec["delivery_key"], "decision": rec["id"]}
            if source == "mcp":
                queued["source"] = "mcp"
            inbox.put(rec["session"], queued,
                      until=until)
        except BaseException as e:
            rec.update(state="open", choice="", note="", closed=0.0, notice_pending=False)
            rec.pop("delivery_key", None)
            rec.pop("answer_sig", None)  # a reopened decision carries no signature of an answer that never was
            rec.pop("answered_via", None)
            _save(path, data)
            if isinstance(e, LockBusy):
                raise DecisionError("busy, try again", "busy") from None
            raise
    if unsigned:
        from .inbox import poll_log
        poll_log(f"decision sign: unsigned ({unsigned})")
    try:  # only for an answer that was delivered, outside the locks, and never for longer than the wait
        append_boundary(rec["session"], until=max(until, time.monotonic() + 0.5))
    except (OSError, TimeoutError) as exc:
        from .inbox import poll_log
        poll_log(f"panel boundary {rec['session'][:8]}: {type(exc).__name__}")
    return dict(rec, where=found)


def reply_text(rec: dict, text: str) -> str:
    """What the session reads for a comment sent without deciding. It answers nothing and approves nothing."""
    return "\n".join([
        "💬 The owner wrote a comment on this session's open question in the control panel, without answering it.",
        f"The question this session asked: {quoted(rec['title'])}",
        f"The owner's comment, in their own words: {quoted(text)}",
        "This is NOT an answer and NOT an approval: no button was tapped, nothing is approved by it (not this "
        "question's action and no other sensitive action), and the question stays open until the owner taps "
        "one of its buttons. Read the comment, adjust, and wait for the owner's answer.",
    ])


def reply(cfg: Config, inbox, ident: str, text: str, now: Optional[float] = None,
          session: Optional[str] = None) -> dict:
    """The owner's comment on an open decision, sent alone: queued to the asking session as the owner's own words,
    marked as not an answer. The decision stays open and unanswered; nothing is signed, nothing is approved. At most
    MAX_REPLIES per decision (a flood of comments must not bury the session's inbox)."""
    if cfg.rich_decisions_enabled is not True:
        raise DecisionError("rich decisions are disabled")
    from .inbox import LockBusy
    from .progress import append_boundary
    text = clean(text, NOTE_CHARS, "comment", multiline=True, owner_text=True)
    now = time.time() if now is None else now
    until = time.monotonic() + LOCK_WAIT
    with _locked(until) as path:
        data = _load(path)
        rec = data.get(ident) if isinstance(ident, str) else None
        if rec is None or (session is not None and rec["session"] != session):
            raise DecisionError("no such decision", "unknown")
        if rec["state"] != "open":
            raise DecisionError(f"already {rec['state']}", "closed", rec["state"], rec["choice"])
        if rec.get("perm") or rec.get("action"):  # answered by its hook or by the bridge, never read by a session
            raise DecisionError("this decision takes no comment", "invalid")
        before = list(rec.get("replies", []))
        if len(before) >= MAX_REPLIES:
            raise DecisionError(f"at most {MAX_REPLIES} comments on one decision", "invalid")
        rec["replies"] = before + [{"at": float(now), "text": text}]
        _save(path, data)
        found = _where(cfg, rec)
        key = secrets.token_hex(8)
        try:
            inbox.put(rec["session"], {"text": reply_text(rec, text), "at": int(now), "id": None, "source": "panel",
                                       "chat": found[0] if found else home_chat(cfg), "key": key,
                                       "decision": rec["id"], "reply": True}, until=until)
        except BaseException as e:
            if before:
                rec["replies"] = before
            else:
                rec.pop("replies", None)
            _save(path, data)
            if isinstance(e, LockBusy):
                raise DecisionError("busy, try again", "busy") from None
            raise
    try:
        append_boundary(rec["session"], until=max(until, time.monotonic() + 0.5))
    except (OSError, TimeoutError) as exc:
        from .inbox import poll_log
        poll_log(f"panel boundary {rec['session'][:8]}: {type(exc).__name__}")
    return dict(rec, where=found, reply_key=key)


def announce_reply(inbox, rec: dict) -> None:
    """Wake the session for a comment; the topic notice stays open (the decision is still waiting)."""
    from .inbox import _deliver_button_word, record_wake_error
    try:
        _deliver_button_word(inbox, rec["session"])
    except Exception as exc:
        record_wake_error(inbox, rec["session"], exc)


def announce(cfg: Config, bot, inbox, rec: dict) -> bool:
    """Wake the session and mark its existing topic notice closed; the answer stays private."""
    from .inbox import _deliver_button_word, record_wake_error
    if rec.get("action") == "emergency_credit":
        from . import cockpit_notice
        cockpit_notice.later(cfg, rec, bot)
        return False
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
    print(f"HarnessGram: {reason}", file=sys.stderr)
    return 1


def run(cfg: Config, action: str, title: str = "", detail: str = "", yes: str = "", no: str = "",
        ident: str = "", bot=None, routine: bool = False, images=(), links=()) -> int:
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
        rich = cfg.rich_decisions_enabled is True
        rec = create(sid, chat, thread, title, detail, yes, no, lang, routine=routine, images=images, links=links,
                     rich=rich)
        from . import cockpit_notice
        cockpit_notice.update(cfg, rec, True, bot)
        print(f"decision {rec['id']} is waiting in the control panel: {rec['title']}")
        if not rec["detail"] and not rec.get("images") and not rec.get("links"):
            print("tip: include what the owner needs to decide: --detail with the context and the options, "
                  "--image for screenshots or designs, --link for pages to check")
        return 0
    except DecisionError as e:
        return _fail(e.reason)
    except Exception as e:
        return _fail(f"decisions are not available ({type(e).__name__})")
