"""Agent blocks: small declarative pieces an agent puts into the Cockpit's Morning Brief (`ctb block post`).

A block is data, never markup: a JSON object of one known kind with a fixed set of fields, checked when it is
written and again on every read. The Cockpit owns all rendering, colours and placement. Kinds in this release:

- `status`: one sentence on where a work line stands ("Three of eight screens are built. Next: filters.").
- `checklist`: up to eight short items, each done or not.
- `needs_you`: one line that points at an open question this same session asked (a `ctb card choice/ask` or a
  `ctb decide` id). It never asks or approves anything itself; the owner answers on the question it names.

Every block has `line` (the work line, a project key such as a ledger file stem), `title`, and `expires_in`
(seconds, default 48 hours). The same session may send the block again with its `id` to update it.

Guardrails: unknown fields are refused; every string is cleaned (bidi controls out, whitespace folded, too long
refused) and credential-filtered when written, and the panel filters again at its response boundary; markup-like
text is refused; per-session and global caps; one update per block every few seconds and an hourly write budget
per session; a block greys after its kind's freshness window or when its session ends, and is dropped after it
expires; the owner can mute a session from the Cockpit (its blocks fold to one row, and it cannot raise
anything under "waiting for you"). The calling session is proven exactly as for cards (decisions.caller_session).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import re
import secrets
import sys
import time
from typing import List, Optional

from . import decisions as D
from .config import Config, private_dir, state_dir, write_private

STATE_FILE = "blocks.json"
MUTE_FILE = "blocks-mute.json"
KINDS = ("status", "checklist", "needs_you")
FIELDS = {"status": {"text"}, "checklist": {"items"}, "needs_you": {"text", "card"}}
COMMON = {"kind", "line", "title", "expires_in", "id"}
LINE_CHARS = 40
TITLE_CHARS = 80
TEXT_CHARS = 160
ITEM_CHARS = 60
MAX_ITEMS = 8
MAX_PER_SESSION = 12
MAX_OPEN = 60
MIN_GAP = 5.0  # seconds between two writes of the same block
HOURLY = 60  # writes per session per hour
EXPIRES_DEFAULT = 48 * 3600
EXPIRES_MIN = 60
EXPIRES_MAX = 7 * 24 * 3600
TTL = {"status": 6 * 3600, "checklist": 24 * 3600, "needs_you": 24 * 3600}  # then shown as stale
MAX_JSON = 8 * 1024
ID_RE = D.ID_RE
MARKUP = re.compile(r"<\s*[A-Za-z/!?]|\]\(|javascript:|data:", re.IGNORECASE)


class BlockError(D.DecisionError):
    pass


@contextlib.contextmanager
def _locked():
    base = private_dir(state_dir())
    with open(base / (STATE_FILE + ".lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError:
            raise BlockError("busy, try again", "busy") from None
        yield base / STATE_FILE


def _text(raw, limit: int, what: str, required: bool = True) -> str:
    if raw is not None and not isinstance(raw, str):
        raise BlockError(f"the {what} must be text")
    try:
        text = D.clean(raw, limit, what, required=required)
    except D.DecisionError as e:
        raise BlockError(e.reason, e.kind) from None
    if MARKUP.search(text):
        raise BlockError(f"the {what} holds markup or a link; blocks are plain text (the Cockpit draws them)")
    return text


def _body(kind: str, raw: dict) -> dict:
    """The kind's own fields, cleaned and checked; a BlockError names what is wrong."""
    if kind == "status":
        return {"text": _text(raw.get("text"), TEXT_CHARS, "text")}
    if kind == "checklist":
        items = raw.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
            raise BlockError(f"a checklist has 1 to {MAX_ITEMS} items")
        out = []
        for it in items:
            if not isinstance(it, dict) or set(it) - {"text", "done"} or not isinstance(it.get("done", False), bool):
                raise BlockError('a checklist item is {"text": "...", "done": true|false}')
            out.append({"text": _text(it.get("text"), ITEM_CHARS, "item"), "done": bool(it.get("done", False))})
        return {"items": out}
    card = raw.get("card")
    if not isinstance(card, str) or not ID_RE.fullmatch(card):
        raise BlockError("needs_you names the open question it points at: \"card\": its 16-character id")
    return {"text": _text(raw.get("text"), TEXT_CHARS, "text"), "card": card}


def parse(raw) -> dict:
    """A block request (the JSON an agent sends) checked against the schema: kind, its fields, nothing else."""
    if not isinstance(raw, dict):
        raise BlockError("a block is one JSON object")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise BlockError(f"unknown block kind {kind!r}; one of: {', '.join(KINDS)}")
    extra = set(raw) - COMMON - FIELDS[kind]
    if extra:
        raise BlockError(f"unknown field(s) for a {kind} block: {', '.join(sorted(extra))}")
    expires = raw.get("expires_in", EXPIRES_DEFAULT)
    if type(expires) is not int or not EXPIRES_MIN <= expires <= EXPIRES_MAX:
        raise BlockError(f"expires_in is whole seconds, {EXPIRES_MIN} to {EXPIRES_MAX}")
    ident = raw.get("id", "")
    if ident and (not isinstance(ident, str) or not ID_RE.fullmatch(ident)):
        raise BlockError("id is the 16-character id a post printed")
    line = _text(raw.get("line"), LINE_CHARS, "line")
    return {"kind": kind, "line": line, "title": _text(raw.get("title"), TITLE_CHARS, "title", required=False),
            "expires_in": expires, "id": ident, **_body(kind, raw)}


def _record(ident, raw) -> Optional[dict]:
    """One stored block, checked again (the file may have been edited by hand)."""
    if not isinstance(raw, dict) or not isinstance(ident, str) or not ID_RE.fullmatch(ident):
        return None
    if not isinstance(raw.get("session"), str) or not raw["session"] or not D._num(raw.get("updated")):
        return None
    if not D._num(raw.get("created")) or not D._num(raw.get("expires")):
        return None
    try:
        body = parse({k: raw.get(k) for k in COMMON | FIELDS.get(raw.get("kind"), set()) if k in raw and k != "id"})
    except BlockError:
        return None
    body.pop("id", None)
    return dict(body, id=ident, session=raw["session"], created=float(raw["created"]),
                updated=float(raw["updated"]), expires=float(raw["expires"]))


def _load(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    blocks = data.get("blocks") if isinstance(data.get("blocks"), dict) else {}
    writes = data.get("writes") if isinstance(data.get("writes"), dict) else {}
    return {"blocks": {k: r for k, r in ((k, _record(k, v)) for k, v in blocks.items()) if r},
            "writes": {s: [float(t) for t in v if D._num(t)] for s, v in writes.items()
                       if isinstance(s, str) and isinstance(v, list)}}


def _save(path, data: dict, now: float) -> None:
    data["blocks"] = {k: r for k, r in data["blocks"].items() if r["expires"] > now}
    data["writes"] = {s: [t for t in v if now - t < 3600] for s, v in data["writes"].items()}
    data["writes"] = {s: v for s, v in data["writes"].items() if v}
    write_private(path, json.dumps(data, ensure_ascii=False))


def _asks_of(session: str) -> set:
    """The open questions this session asked: its ask/choice cards and its decisions."""
    from . import cards
    out = {r["id"] for r in cards.open_cards() if r["session"] == session and r["kind"] in cards.ASKS}
    out |= {r["id"] for r in D.open_decisions() if r["session"] == session}
    return out


def post(session: str, raw, now: Optional[float] = None) -> dict:
    """A new block, or with "id" this session's own block replaced in place (its kind stays the same)."""
    now = time.time() if now is None else now
    req = parse(raw)
    if req["kind"] == "needs_you" and req["card"] not in _asks_of(session):
        raise BlockError("needs_you points only at an open question this session asked (card or decision id)")
    with _locked() as path:
        data = _load(path)
        live = {k: r for k, r in data["blocks"].items() if r["expires"] > now}
        writes = [t for t in data["writes"].get(session, []) if now - t < 3600]
        if len(writes) >= HOURLY:
            raise BlockError(f"too many block writes: at most {HOURLY} an hour per session", "busy")
        ident = req.pop("id")
        if ident:
            old = live.get(ident)
            if old is None or old["session"] != session:
                raise BlockError(f"no block {ident} from this session", "unknown")
            if old["kind"] != req["kind"]:
                raise BlockError(f"block {ident} is a {old['kind']} block")
            if now - old["updated"] < MIN_GAP:
                raise BlockError(f"wait a few seconds between updates of one block (at most one per {int(MIN_GAP)} s)",
                                 "busy")
            created = old["created"]
        else:
            if len(live) >= MAX_OPEN:
                raise BlockError(f"there are already {MAX_OPEN} blocks; close one with `ctb block close <id>`")
            if sum(1 for r in live.values() if r["session"] == session) >= MAX_PER_SESSION:
                raise BlockError(f"this session already has {MAX_PER_SESSION} blocks; update one with its id "
                                 "or close one")
            ident = secrets.token_hex(8)
            while ident in data["blocks"]:
                ident = secrets.token_hex(8)
            created = now
        rec = dict(req, id=ident, session=session, created=created, updated=now,
                   expires=now + req["expires_in"])
        if _record(ident, rec) is None:
            raise BlockError("the block is not valid")
        data["blocks"][ident] = rec
        data["writes"][session] = writes + [now]
        _save(path, data, now)
    return rec


def close(session: str, ident: str, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    with _locked() as path:
        data = _load(path)
        rec = data["blocks"].get(ident)
        if rec is None or rec["session"] != session or rec["expires"] <= now:
            raise BlockError(f"no block {ident} from this session", "unknown")
        del data["blocks"][ident]
        _save(path, data, now)
    return rec


def all_blocks(now: Optional[float] = None) -> List[dict]:
    """Every block that has not expired, oldest first. Read without the lock (writes replace the file)."""
    now = time.time() if now is None else now
    data = _load(state_dir() / STATE_FILE)
    return sorted((r for r in data["blocks"].values() if r["expires"] > now), key=lambda r: (r["created"], r["id"]))


# --- the owner's mute ------------------------------------------------------------------------

SHORT_RE = re.compile(r"[0-9a-f-]{8}")


def muted() -> set:
    """The sessions (first 8 characters) the owner muted in the Cockpit."""
    try:
        data = json.loads((state_dir() / MUTE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return set()
    return {s for s in data if isinstance(s, str) and SHORT_RE.fullmatch(s)} if isinstance(data, list) else set()


def set_muted(short: str, on: bool) -> set:
    if not isinstance(short, str) or not SHORT_RE.fullmatch(short):
        raise ValueError("a session's first 8 characters")
    with _locked():
        found = muted()
        found = (found | {short}) if on else (found - {short})
        write_private(private_dir(state_dir()) / MUTE_FILE, json.dumps(sorted(found)[:200]))
    return found


def view(rec: dict, now: float, ended: set, mute: set) -> dict:
    """A block as the Cockpit gets it: no full session id, its age, and whether it is stale or muted."""
    age = max(0, int(now - rec["updated"]))
    short = rec["session"][:8]
    out = {"id": rec["id"], "kind": rec["kind"], "line": rec["line"], "title": rec["title"], "age": age,
           "session": short, "stale": age > TTL[rec["kind"]] or rec["session"] in ended, "muted": short in mute}
    out["why"] = "ended" if rec["session"] in ended else ("old" if out["stale"] else "")
    for key in ("text", "items", "card"):
        if key in rec:
            out[key] = json.loads(json.dumps(rec[key]))
    return out


# --- the command ---------------------------------------------------------------------------

def _read_json(source: str) -> object:
    if source == "-":
        raw = sys.stdin.read(MAX_JSON + 1)
    else:
        with open(source, encoding="utf-8") as f:
            raw = f.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise BlockError(f"the JSON is too long (at most {MAX_JSON} bytes)")
    try:
        return json.loads(raw)
    except ValueError:
        raise BlockError("not valid JSON") from None


def run(cfg: Config, action: str, source: str = "-", ident: str = "") -> int:
    """`ctb block post --json <file|->`, `ctb block close <id>`, `ctb block list`: 0 when it worked, 1 with a
    one-line reason when it was refused. The session is always the one the command runs inside."""
    try:
        sid = D.caller_session()
        if action == "list":
            mine = [r for r in all_blocks() if r["session"] == sid]
            print("\n".join(f"{r['id']}  {r['kind']}  {r['line']}" for r in mine) if mine else "(no blocks)")
            return 0
        if action == "close":
            rec = close(sid, (ident or "").strip().lower())
            print(f"block closed: {rec['kind']} on {rec['line']}")
            return 0
        if action != "post":
            raise BlockError(f"unknown block command {action}")
        rec = post(sid, _read_json(source))
        print(f"block {rec['id']} is in the Cockpit ({rec['kind']} on {rec['line']}); update it by sending it "
              f"again with \"id\": \"{rec['id']}\"")
        return 0
    except D.DecisionError as e:
        print(f"Harnessgram: {e.reason}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"Harnessgram: cannot read the block ({type(e).__name__})", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Harnessgram: blocks are not available ({type(e).__name__})", file=sys.stderr)
        return 1
