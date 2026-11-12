"""Every Telegram message really reaches its session: take-then-ack, put-back and visible receipts.

A hook (PostToolUse, Stop, UserPromptSubmit) or the wake waiter takes queued messages under a lease
(Inbox.take(lease=...)): they move to the session's in-flight file, and the hook's output carries the
lease's nonce. Claude Code writes what it hands the model to the transcript, so the nonce showing up
there is the proof of delivery. Only then:

* the lease is settled ("proven"): the keys are remembered as delivered, so no copy is ever handed over again;
* the owner's messages get the 👌 and their "will be delivered" receipts are deleted.

The proof is looked for right away by a detached child of the hook (run_confirm), and every few seconds
by the listener's sweeper (Sweeper.run), which also repairs what went wrong (a message sent right near
the end of a turn must never be missed):

* a lease goes back to the front of the queue only once its output can no longer reach the model: a
  synchronous hook's (Stop, PostToolUse, UserPromptSubmit) when Claude Code made its next model call or ended
  the turn without it (verdict; the next hook does this itself before it takes, so the message goes
  first and in order, and newer ones wait while an earlier one is undecided: before_take); its
  session's process is gone; or the hook that took it has exited and the session sits idle at an empty
  prompt with a quiet transcript while the nonce is absent (a cancelled or timed-out Stop hook, output
  Claude Code dropped); or it reached only Claude Code's own command queue, which was then handed over
  without it (0dec98e6). One still waiting in that queue at an idle prompt is handed over
  by a typed prompt, never duplicated by a put-back;
* a queued message that nothing picked up while the session sits idle at an empty prompt is typed in
  ("📩 Telegram"; the UserPromptSubmit hook hands it over), whatever the busy mark or a stale waiter says;
* a closed session with a fresh message is reopened, at most every few minutes;
* a message still not delivered a minute after it was sent gets a short visible notice, which the
  real delivery's 👌 deletes.

Put-back and proof race only in a narrow way: a late proof of a lease that was already put back takes the
copy out of the queue again (Inbox.settle), and take() skips keys already proven.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from .config import Config

TICK = 3.0                 # seconds between sweeps
CONFIRM_WAIT = 30.0        # how long the hook's detached child looks for its own proof
LOST_ABSENT = 60.0         # a hook output that never reached the transcript at all is lost after this
LOST_QUEUED = 60.0         # output still only in Claude Code's queue: lost once the session is idle this long after
LOST_DEAD = 10.0           # the session's process is gone: nothing can consume the output any more
QUIET = 20.0               # seconds without transcript writes that make an idle prompt truly idle
NUDGE_AFTER = 15.0         # a queued message on an idle session that the waiter has not taken by then
WAITING_NOTICE = 60.0      # the owner sees a notice when a message is still not delivered after this
NOTICE_MAX_AGE = 24 * 3600  # older stranded messages are only logged, never announced days later
NUDGE_BACKOFF_MAX = 1800.0  # a live session that takes nothing is still nudged this often, however old the message
REOPEN_EVERY = 300.0       # seconds between reopen attempts of a closed session
REOPEN_MAX_AGE = 3600.0    # a closed session is reopened for messages younger than this
WORKERS = 4                # sessions swept at once (each by one thread at a time)
LOCK_WAIT = 2.0            # seconds the sweeper waits for a session's lock; then that session waits a tick
PRUNE_PASSES = 20          # a transcript no lease looked at for this many passes leaves the index
TOMBSTONE = 900.0          # a put-back lease is kept this long so that a late proof still counts

# The bridge's own framed block, as framing.fenced / framing.receipt write it. Telegram texts are scrubbed of
# these tags (framing.sanitize), so only the bridge can put one in a hook's output.
TAG = re.compile(r'<telegram-(?:message|delivery) nonce="([0-9a-f]{16})"')


def _texts(value) -> List[str]:
    """The strings in a field that is a string or a list of strings / text blocks (never tool results)."""
    if isinstance(value, str):
        return [value]
    out = []
    for part in value if isinstance(value, list) else []:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            out.append(part["text"])
    return out


def trusted(event: dict) -> Tuple[str, List[str]]:
    """("consumed"|"queued"|"", texts): the one field of a transcript record where Claude Code writes a hook's
    output, never any other text. A delivery is proven only by its framed block in:

    * a hook_additional_context attachment (PostToolUse / UserPromptSubmit / a background agent's step);
    * a hook_blocking_error attachment (a Stop hook's block: Claude Code 2.1 hands the model its reason from it,
      and writes no user record; unread, every Stop-hook delivery stayed unproven and came back as a copy);
    * a queued_command attachment (a queued Stop-hook block handed over mid-turn);
    * a user record whose content is plain text (a Stop or wake hook's block as the next prompt);
    * "queued": an enqueue in Claude Code's command queue (not handed over yet).

    Never from an assistant record, a tool result (a transcript or test file the session read), a system
    record or an API request blob, where an earlier nonce can be quoted (review of #141, P2-5)."""
    kind = event.get("type")
    if kind == "attachment":
        att = event.get("attachment")
        if isinstance(att, dict):
            if att.get("type") == "hook_additional_context":
                return "consumed", _texts(att.get("content"))
            if att.get("type") == "queued_command":
                return "consumed", _texts(att.get("prompt"))
            if att.get("type") == "hook_blocking_error":  # a Stop hook's block, as Claude Code 2.1 records it
                err = att.get("blockingError")
                return "consumed", _texts(err.get("blockingError") if isinstance(err, dict) else err)
        return "", []
    if kind == "user":
        content = (event.get("message") or {}).get("content") if isinstance(event.get("message"), dict) else None
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return "", []
        return "consumed", _texts(content)
    if kind == "queue-operation" and event.get("operation") == "enqueue":
        return "queued", _texts(event.get("content"))
    return "", []


def _drains(line: bytes) -> bool:
    """A record after which Claude Code's command queue has been handed over (a dequeue or a new prompt)."""
    if b'"queue-operation"' not in line and b'"type":"user"' not in line:
        return False
    try:
        event = json.loads(line)
    except ValueError:
        return False
    if not isinstance(event, dict):
        return False
    if event.get("type") == "queue-operation":
        return event.get("operation") in ("dequeue", "remove")
    return event.get("type") == "user" and not event.get("isMeta")


class _File:
    __slots__ = ("base", "pos", "ident", "found", "waiting", "pass_no", "lock")

    def __init__(self, base: int):
        self.base, self.pos, self.ident, self.pass_no = base, base, None, -1
        self.lock = threading.Lock()
        self.found: Dict[str, list] = {}  # nonce -> [state, drained]
        self.waiting: set = set()          # nonces only queued so far (neither consumed nor drained)


class TranscriptIndex:
    """Every transcript a lease can be proven in, read once and incrementally (review of #141, P2-10): each
    new complete line is looked at once per pass for all leases together, and every framed block in a trusted
    field is remembered by nonce, so a lease registered later is answered from the index without a re-read."""

    def __init__(self):
        self.files: Dict[str, _File] = {}
        self.pass_no = 0
        self.bytes_read = 0
        self._lock = threading.Lock()

    def new_pass(self) -> None:
        self.pass_no += 1

    def prune(self, before: int) -> None:
        """Forget the files no lease has looked at since pass `before`."""
        with self._lock:
            for path in [p for p, f in self.files.items() if f.pass_no < before]:
                self.files.pop(path, None)

    def state(self, path: str, nonce: str, start: int = 0) -> Tuple[str, bool]:
        if not path or not nonce:
            return "absent", False
        with self._lock:  # the map only; each file is read under its own lock, so sessions never wait on each other
            f = self.files.get(path)
            if f is None or start < f.base:
                f = self.files[path] = _File(max(0, start))  # first look, or a lease older than what was read
        with f.lock:
            if f.pass_no != self.pass_no:
                f.pass_no = self.pass_no
                self._read(path, f)
            state, drained = f.found.get(nonce, ("absent", False))
            return state, drained

    def _read(self, path: str, f: _File) -> None:
        """Index the complete lines written since the last read (a line being written is read next time)."""
        try:
            with open(path, "rb") as fh:
                st = os.fstat(fh.fileno())
                ident = (st.st_dev, st.st_ino)
                if f.ident not in (None, ident) or st.st_size < f.pos:  # replaced or truncated: start over
                    f.base = f.pos = 0
                    f.found.clear()
                    f.waiting.clear()
                f.ident = ident
                if st.st_size == f.pos:
                    return
                fh.seek(f.pos)
                for line in fh:
                    if not line.endswith(b"\n"):
                        break
                    f.pos += len(line)
                    self.bytes_read += len(line)  # a statistic (tests, measurements)
                    self._line(f, line)
        except OSError:
            return

    @staticmethod
    def _line(f: _File, line: bytes) -> None:
        here: set = set()
        if b"telegram-" in line and b"nonce=" in line:
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            if isinstance(event, dict):
                kind, texts = trusted(event)
                for text in texts:
                    for nonce in TAG.findall(text):
                        here.add(nonce)
                        if kind == "consumed":
                            f.found[nonce] = ["consumed", False]
                            f.waiting.discard(nonce)
                        elif kind == "queued" and nonce not in f.found:
                            f.found[nonce] = ["queued", False]
                            f.waiting.add(nonce)
                here.update(TAG.findall(line.decode("utf-8", "replace").replace('\\"', '"')))
        if f.waiting and _drains(line):
            for nonce in [n for n in f.waiting if n not in here]:
                f.found[nonce][1] = True
                f.waiting.discard(nonce)


def _transcript(lease: dict, session: str) -> str:
    path = lease.get("transcript") or ""
    if path and os.path.exists(path):
        return path
    try:
        from .status import transcript_of
        return transcript_of(session) or path
    except Exception:
        return path


def _sources(lease: dict, session: str) -> List[Tuple[str, int]]:
    """(path, start offset) of every file the lease's proof can be in: the session's transcript, and for a
    background agent's step (via "agent") that agent's own transcript next to it, where Claude Code writes
    the output of hooks run on its tool calls (by its id; without one, the agents' files changed since)."""
    main = _transcript(lease, session)
    out = [(main, int(lease.get("offset") or 0))] if main else []
    if lease.get("via") == "agent" and main.endswith(".jsonl"):
        agent = re.sub(r"[^A-Za-z0-9_-]", "", str(lease.get("agent_id") or ""))
        if agent:
            out.append((f"{main[:-6]}/subagents/agent-{agent}.jsonl", int(lease.get("agent_offset") or 0)))
        else:
            import glob
            since = float(lease.get("at") or 0) - 5
            for path in glob.glob(glob.escape(main[:-6]) + "/subagents/*.jsonl"):
                try:
                    if os.path.getmtime(path) >= since:
                        out.append((path, 0))
                except OSError:
                    continue
    return out


def prove(lease: dict, session: str, index: TranscriptIndex) -> Tuple[str, bool]:
    """The lease's state over all its sources ("consumed" wins over "queued" over "absent")."""
    nonce = str(lease.get("nonce"))
    states, drained = [], False
    for path, start in _sources(lease, session):
        state, dr = index.state(path, nonce, start)
        states.append(state)
        drained = drained or dr
    for state in ("consumed", "queued"):
        if state in states:
            return state, drained
    return "absent", drained


# A synchronous hook's output (Stop, PostToolUse, UserPromptSubmit) is applied before Claude Code's next model
# call, and a Stop hook's block continues the turn. The wake waiter (an async hook) and a background agent's step
# are not synchronous: their output can be recorded later.
SYNC_VIAS = ("stop", "post", "prompt")
LOOK_BACK = 4 * 1024 * 1024    # bytes before a take point searched for the record it fell in and its response
SCAN_BUDGET = 4 * 1024 * 1024  # bytes one look reads at most; the next look goes on from there (lease "scan")
SCAN_STATS = {"bytes": 0}      # a statistic (tests, measurements)


def _model_id(event: dict) -> str:
    """The response id of a main-conversation model record, "" for anything else (a background agent's sidechain
    record, a synthetic one Claude Code writes itself, a record without an id)."""
    if event.get("type") != "assistant" or event.get("isSidechain"):
        return ""
    msg = event.get("message")
    if not isinstance(msg, dict) or msg.get("model") == "<synthetic>":
        return ""
    mid = msg.get("id")
    return mid if isinstance(mid, str) else ""


def _scan_start(fh, offset: int) -> dict:
    """Where a lease's scan begins: the start of the record the take point fell in (one still being written then
    is read once complete, as the response the take happened in), and the id of the last response before it.
    "sure" is False when that start lies beyond LOOK_BACK: then nothing is ever decided "dropped"."""
    back = max(0, offset - LOOK_BACK)
    fh.seek(back)
    chunk = fh.read(offset - back)
    pos, sure = offset, True
    if chunk and not chunk.endswith(b"\n"):  # mid-record
        cut = chunk.rfind(b"\n")
        if cut >= 0:
            pos = back + cut + 1
        elif back == 0:
            pos = 0
        else:
            sure = False
    head = chunk[:pos - back]
    if back:
        head = head.partition(b"\n")[2]
    last = ""
    for line in reversed(head.splitlines()):
        if b'"assistant"' in line:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            last = _model_id(event) if isinstance(event, dict) else ""
            if last:
                break
    return {"pos": pos, "ids": [last] if last else [], "user": False, "moved": False, "queued": False,
            "sure": sure}


def verdict(lease: dict, data: bytes, carry: dict) -> str:
    """THE decision on one in-flight delivery, from one read: `data` is the transcript's complete records from
    carry["pos"] on, and `carry` the progress of the earlier looks (updated in place). Returns

    * "delivered": its framed block is in a field where Claude Code writes what it hands the model (trusted);
    * "queued": seen in Claude Code's own command queue, not handed over yet (or handed over without it);
    * "dropped": a synchronous hook's output, and Claude Code went past the point where it is applied without
      it: a new model call (a user record, then a response that is neither the one the take happened in, whose
      later parallel tool_use records stream in after its first tool's hook, nor one first seen before that
      user record, such as a final reply flushed after the Stop hook's take), or the turn's end (turn_duration;
      a Stop block continues the turn instead). The proof would come before either, so it can no longer come;
    * "pending": none of these yet.

    The nonce and the progress are judged from the same bytes, so a proof written together with a new response
    can never read as "dropped" (review of #228)."""
    nonce = str(lease.get("nonce") or "")
    sync = lease.get("via") in SYNC_VIAS and carry.get("sure", True)
    ids = set(carry.get("ids") or [])
    for line in data.splitlines():
        if nonce and nonce.encode() in line:
            try:
                event = json.loads(line)
            except ValueError:
                event = None
            if isinstance(event, dict):
                kind, texts = trusted(event)
                if any(nonce in TAG.findall(text) for text in texts):
                    if kind == "consumed":
                        return "delivered"
                    carry["queued"] = True
        if not sync or carry.get("moved") or (
                b'"assistant"' not in line and b'"user"' not in line and b'"turn_duration"' not in line):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("isSidechain"):
            continue
        if event.get("type") == "system" and event.get("subtype") == "turn_duration":
            carry["moved"] = True
        elif event.get("type") == "user":
            carry["user"] = True
        else:
            mid = _model_id(event)
            if mid and carry.get("user") and mid not in ids:
                carry["moved"] = True
            elif mid:
                ids.add(mid)
    carry["ids"] = sorted(ids)[-8:]
    if carry.get("queued"):
        return "queued"
    return "dropped" if sync and carry.get("moved") else "pending"


MARK = 64  # bytes before a saved scan position kept as its fingerprint


def mark_at(fh, pos: int) -> str:
    """The fingerprint of a transcript position: the bytes just before it."""
    fh.seek(max(0, pos - MARK))
    return fh.read(pos - max(0, pos - MARK)).hex()


def scan_valid(fh, ino, pos: int, mark) -> bool:
    """The transcript is exactly as a saved scan position expects: the same file, at least that long, and the
    same bytes just before it. Anything else (replaced, shortened, shortened and grown again) voids the scan."""
    st = os.fstat(fh.fileno())
    return isinstance(mark, str) and ino == st.st_ino and pos <= st.st_size and mark_at(fh, pos) == mark


def _found(lease: dict, data: bytes) -> bool:
    """The lease's framed block is in a field where Claude Code writes what it hands the model."""
    nonce = str(lease.get("nonce") or "")
    for line in data.splitlines():
        if nonce and nonce.encode() in line:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind, texts = trusted(event) if isinstance(event, dict) else ("", [])
            if kind == "consumed" and any(nonce in TAG.findall(text) for text in texts):
                return True
    return False


def decide(inbox, session: str, lease: dict, until: Optional[float] = None) -> str:
    """verdict() over what was appended to the lease's transcript since its last look, at most SCAN_BUDGET bytes,
    complete records only. The progress saved in the lease ("scan") is only an optimisation: when the transcript is
    not exactly as it expects (scan_valid), the scan is void for good, and from then on the answer is only
    "delivered" (a fresh search of the transcript's last SCAN_BUDGET bytes finds the proof) or "pending", never
    "dropped": the sweeper's idle rule releases it."""
    path = _transcript(lease, session)
    if not path or lease.get("via") == "agent":
        return "pending"
    saved = lease.get("scan") or {}
    try:
        with open(path, "rb") as fh:
            if not saved.get("void"):
                if saved:
                    ok = scan_valid(fh, saved.get("ino"), int(saved.get("pos") or 0), saved.get("mark"))
                    carry = dict(saved)
                else:  # the first look: the take point itself must still be where the take saw it
                    offset = int(lease.get("offset") or 0)
                    ok = scan_valid(fh, lease.get("ino"), offset, lease.get("mark"))
                    carry = dict(_scan_start(fh, offset), ino=os.fstat(fh.fileno()).st_ino) if ok else {}
                if ok:
                    start = int(carry["pos"])
                    start_mark = mark_at(fh, start)
                    fh.seek(start)
                    data = fh.read(SCAN_BUDGET)
                    ok = scan_valid(fh, carry["ino"], start, start_mark)  # still the same file after the read
            if saved.get("void") or not ok:
                size = os.fstat(fh.fileno()).st_size
                fh.seek(max(0, size - SCAN_BUDGET))
                tail = fh.read(SCAN_BUDGET)
                SCAN_STATS["bytes"] += len(tail)
                if not saved.get("void"):
                    inbox.note_scan(session, str(lease.get("nonce")), {"void": True}, until=until)
                return "delivered" if _found(lease, tail) else "pending"
    except OSError:
        return "pending"
    data = data[:data.rfind(b"\n") + 1]  # complete records only: one still being written is read next time
    SCAN_STATS["bytes"] += len(data)
    result = verdict(lease, data, carry)
    if result != "delivered" and data:
        carry["pos"] = start + len(data)
        carry["mark"] = (bytes.fromhex(start_mark) + data)[-MARK:].hex()  # from the bytes actually read
        try:
            inbox.note_scan(session, str(lease.get("nonce")), carry, until=until)
        except Exception:
            pass  # progress only: the next look reads these bytes again
    return result


def before_take(inbox, session: str, until: Optional[float] = None,
                taker_alive: Optional[Callable[[dict], bool]] = None) -> bool:
    """Called by a main-conversation hook (or the wake waiter) right before it takes the queue. A lease whose output
    was dropped goes back to the front of the queue now, so this take hands it over first, in the order the owner
    sent it. True when an earlier delivery is still undecided (its hook is still running, it waits in Claude Code's
    own queue, or a synchronous output may still be applied): newer messages then stay queued, never overtaking
    it. While the session works that lasts a step at most; when in doubt it waits, it never puts back."""
    taker_alive = taker_alive or Probe().taker_alive
    undecided = False
    for lease in inbox.leases(session):
        if lease.get("requeued_at") or lease.get("via") == "agent":
            continue  # put back already, or a background agent's step (never handed to the main conversation)
        if not os.path.exists(_transcript(lease, session)):
            # A missing transcript cannot release a Cockpit answer. Let the sweeper's
            # idle/dead-output checks decide it; newer answers must not overtake it.
            # Preserve the legacy behavior for non-panel messages without a transcript.
            undecided = undecided or taker_alive(lease) or any(
                i.get("source") == "panel" for i in lease.get("items") or [] if isinstance(i, dict))
            continue
        result = decide(inbox, session, lease, until=until)
        if result == "delivered":
            continue  # the confirm worker or the sweeper settles it
        alive = taker_alive(lease)
        if result == "dropped" and not alive:
            back = inbox.settle(session, str(lease.get("nonce")), "lost", until=until)
            if back:
                _log(session, f"put back {len(back)} via {lease.get('via')}: the model moved on without it")
            continue
        if alive or result == "queued" or lease.get("via") in SYNC_VIAS:
            undecided = True
    return undecided


def confirm(cfg: Config, items: List[dict], bot=None) -> None:
    """The proven items: 👌 on the owner's own messages and their receipts deleted (inbox.run_delivered)."""
    from .inbox import owner_refs, run_delivered
    refs = owner_refs(items)
    if refs:
        run_delivered(cfg, refs, bot=bot)


def run_confirm(cfg: Config, session: str, nonce: str, inbox=None, bot=None, wait: float = CONFIRM_WAIT,
                poll: float = 0.5, sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic) -> int:
    """The hook's detached child: wait for this lease's proof, then settle it and give the 👌.
    Without a proof in `wait` seconds it leaves the lease to the listener's sweeper."""
    from .inbox import default_inbox
    inbox = inbox or default_inbox()
    deadline = clock() + wait
    index = TranscriptIndex()
    while True:
        lease = next((d for d in inbox.leases(session) if d.get("nonce") == nonce), None)
        if lease is None:
            return 0  # settled elsewhere
        index.new_pass()
        state, _ = prove(lease, session, index)
        if state == "consumed":
            items = inbox.settle(session, nonce, "proven")
            if items:
                _log(session, f"proven via {lease.get('via') or '?'} after {time.time() - lease.get('at', time.time()):.1f}s")
                try:
                    confirm(cfg, items, bot=bot)
                except Exception:
                    pass
            return 0
        if clock() >= deadline:
            return 0
        sleep(poll)


def _log(session: str, what: str) -> None:
    try:
        from .config import private_dir, state_dir
        fd = os.open(private_dir(state_dir()) / "delivery.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {session[:8]} {what}\n")
    except Exception:
        pass


# --- the listener's sweeper ---------------------------------------------------------------------

class Probe:
    """What the sweeper needs to know about a live session, and the two things it may do to one.
    Tests replace it; the real one reads tmux, the process table and the transcript."""

    def alive(self, session: str) -> bool:
        from .inbox import accounts, recently_moving, session_pid
        if session_pid(session) is not None or recently_moving(session):
            return True
        try:
            return any(r.get("sessionId") == session for r in accounts.live_session_records())
        except Exception:
            return True  # unknown: never treat a session as gone on a failed look

    def limited(self, session: str) -> bool:
        from .wake import limit_active
        try:
            return limit_active(session)
        except Exception:
            return False

    def idle(self, session: str, transcript: str, now: float) -> bool:
        """At an empty, idle prompt and no transcript writes for QUIET seconds: no turn is running."""
        from .inbox import input_box, pane_for_session, pane_runs_session
        try:
            if transcript and now - os.path.getmtime(transcript) < QUIET:
                return False
        except OSError:
            pass
        pane = pane_for_session(session)
        if not pane or not pane_runs_session(pane, session):
            return False  # a saved pane id another session now uses says nothing about this one
        text, ready = input_box(pane)
        return text == "" and ready

    def taker_alive(self, lease: dict) -> bool:
        """The hook process that took the lease is still running: its output is not out yet."""
        try:
            pid = int(lease.get("pid") or 0)
        except (TypeError, ValueError):
            return False
        if pid <= 1 or pid == os.getpid():
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True  # exists, owned by someone else: treat as running
        return True

    def nudge(self, inbox, session: str, force: bool = False) -> bool:
        from .inbox import sweep_nudge
        return sweep_nudge(inbox, session, force=force)

    def reopen(self, inbox, session: str) -> None:
        from .inbox import _reopen_or_nudge
        threading.Thread(target=_reopen_or_nudge, args=(inbox, session), daemon=True).start()


class Sweeper:
    """`workers`: sessions are swept in parallel by up to this many threads, each session by one at a time,
    so one stuck lock or slow Telegram call never holds up the others (review of #141, P2-7). 0 sweeps
    them one after another in the caller's thread (tests)."""

    def __init__(self, cfg: Config, bot, inbox, probe: Optional[Probe] = None,
                 clock: Callable[[], float] = time.time, log: Optional[Callable[[str], None]] = None,
                 workers: int = WORKERS):
        self.cfg, self.bot, self.inbox = cfg, bot, inbox
        self.probe = probe or Probe()
        self.clock = clock
        self.log = log or (lambda what: None)
        self.index = TranscriptIndex()
        self._reopened: Dict[str, float] = {}
        self._stranded_logged: set = set()
        self._nudges: Dict[str, Tuple[int, float, str]] = {}
        self._running: set = set()
        self._running_lock = threading.Lock()
        self._pool = None
        if workers > 0:
            from concurrent.futures import ThreadPoolExecutor
            self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ctb-sweep")

    def _until(self) -> float:
        return time.monotonic() + LOCK_WAIT

    # One pass over every session with something queued or in flight.
    def sweep(self) -> None:
        from . import policy
        if not policy.on():
            return  # mirroring is off: nothing is taken from a queue or handed to a session
        from .inbox import start_turn_command_sweep
        start_turn_command_sweep(self.cfg, self.bot)
        self.index.new_pass()
        for session in self.inbox.sessions():
            with self._running_lock:
                if session in self._running:
                    continue  # its previous sweep is still running (a slow call): never two at once
                self._running.add(session)
            if self._pool is None:
                self._one(session)
            else:
                try:
                    self._pool.submit(self._one, session)
                except RuntimeError:  # shut down
                    self._done(session)
                    return
        self.index.prune(self.index.pass_no - PRUNE_PASSES)  # transcripts no lease looked at for a while
        try:
            from . import cockpit_notice
            cockpit_notice.sweep(self.cfg, self.bot)
        except Exception as exc:
            self.log(f"notice sweep: {type(exc).__name__}: {str(exc)[:120]}")

    def _one(self, session: str) -> None:
        try:
            self.sweep_session(session)
        except Exception as exc:  # one broken or stuck session never stops the others
            self.log(f"sweep {session[:8]}: {type(exc).__name__}: {str(exc)[:120]}")
        finally:
            self._done(session)

    def _done(self, session: str) -> None:
        with self._running_lock:
            self._running.discard(session)

    def idle_workers(self, timeout: float = 5.0) -> bool:
        """Wait until no session sweep is running (tests). True when none is."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._running_lock:
                if not self._running:
                    return True
            time.sleep(0.01)
        return False

    def sweep_session(self, session: str) -> None:
        now = self.clock()
        alive: Optional[bool] = None

        def is_alive() -> bool:
            nonlocal alive
            if alive is None:
                alive = self.probe.alive(session)
            return alive

        transcript = ""
        unproven: List[dict] = []
        for lease in self.inbox.leases(session):
            nonce = str(lease.get("nonce"))
            transcript = _transcript(lease, session) or transcript
            state, drained = prove(lease, session, self.index)
            if state == "absent" and lease.get("via") in SYNC_VIAS and not lease.get("requeued_at"):
                result = decide(self.inbox, session, lease, until=self._until())  # one read: proof and progress
                if result == "delivered":
                    state = "consumed"
                elif result == "dropped" and not self.probe.taker_alive(lease):
                    back = self.inbox.settle(session, nonce, "lost", now=now, until=self._until())
                    if back:
                        _log(session, f"put back {len(back)} via {lease.get('via')}: the model moved on without it")
                        self.log(f"delivery {session[:8]}: put back {len(back)} message(s), the model moved on")
                    continue
            if state == "consumed":
                items = self.inbox.settle(session, nonce, "proven", now=now, until=self._until())
                if items:
                    late = " (late, after a put-back)" if lease.get("requeued_at") else ""
                    _log(session, f"proven via {lease.get('via') or '?'} by the sweeper{late}")
                    confirm(self.cfg, items, bot=self.bot)
                continue
            if lease.get("requeued_at"):
                if now - float(lease["requeued_at"]) >= TOMBSTONE:
                    self.inbox.settle(session, nonce, "forget", now=now, until=self._until())
                continue
            age = now - float(lease.get("at") or now)
            lost = ""
            # A lease goes back to the queue only once its output can no longer reach the model (review of
            # #141, P1-2: a timeout alone let a late original and its put-back copy both arrive):
            if age >= LOST_DEAD and not is_alive():
                lost = "the session's process is gone"
            elif state == "absent" and age >= LOST_ABSENT and not self.probe.taker_alive(lease) \
                    and self._settled_idle(session, lease, transcript, now):
                # the hook that took it has exited, so Claude Code holds its output, and Claude Code sits idle
                # at an empty prompt with a quiet transcript, so that output was dropped, not pending
                lost = "the hook output never reached the transcript"
            elif state == "queued" and age >= LOST_QUEUED and self._settled_idle(session, lease, transcript, now):
                if drained:  # the queue was handed over without it, and an idle session has nothing queued
                    lost = "Claude Code's queue drained without it"
                elif self.probe.nudge(self.inbox, session, force=True):
                    # still in Claude Code's own queue at an idle prompt: a typed prompt hands the queue over,
                    # after which it is either consumed (proof) or drained (put back): never both
                    _log(session, "typed in by the sweeper to hand over Claude Code's queued command")
            if lost:
                back = self.inbox.settle(session, nonce, "lost", now=now, until=self._until())
                if back:
                    _log(session, f"put back {len(back)} via {lease.get('via') or '?'}: {lost}")
                    self.log(f"delivery {session[:8]}: put back {len(back)} message(s), {lost}")
            else:
                unproven += [i for i in lease.get("items") or [] if isinstance(i, dict)]

        queued = self.inbox.peek(session)
        if queued:
            self._deliver_queued(session, queued, transcript, now, is_alive, bool(unproven))
        self._notices(session, queued + unproven, now, is_alive)

    def _settled_idle(self, session: str, lease: dict, transcript: str, now: float) -> bool:
        """The session (and, for a background agent's step, that agent's transcript) is idle and quiet."""
        for path, _ in _sources(lease, session)[1:]:
            try:
                if now - os.path.getmtime(path) < QUIET:
                    return False
            except OSError:
                pass
        return self.probe.idle(session, transcript, now)

    def _deliver_queued(self, session: str, queued: List[dict], transcript: str, now: float,
                        is_alive, in_flight: bool) -> None:
        if self.probe.limited(session):
            return  # waits for the quota's reset (recover.py)
        times = [float(i.get("queued_at") or i.get("at") or now) for i in queued]
        oldest, newest = min(times), max(times)
        if is_alive():
            # A running session is nudged whatever the messages' age (review of #141, P2-8), at a falling
            # rate while nothing takes them: 30 s, 1, 2, 4 ... minutes, then every NUDGE_BACKOFF_MAX.
            if in_flight or now - oldest < NUDGE_AFTER:
                return  # a delivery is under way, or the waiter / next step still has time to take it
            head = str(queued[0].get("key") or oldest)
            count, next_at, seen = self._nudges.get(session, (0, 0.0, head))
            if seen != head:  # the front message changed: something was delivered, start over
                count, next_at = 0, 0.0
            if now < next_at:
                return
            if not transcript:
                try:
                    from .status import transcript_of
                    transcript = transcript_of(session) or ""
                except Exception:
                    transcript = ""
            if self.probe.idle(session, transcript, now) and self.probe.nudge(self.inbox, session):
                _log(session, f"typed in by the sweeper after {now - oldest:.0f}s")
                count += 1
                self._nudges[session] = (count, now + min(NUDGE_BACKOFF_MAX, NUDGE_AFTER * 2 ** count), head)
            return
        if now - newest < REOPEN_MAX_AGE:
            if now - self._reopened.get(session, 0.0) >= REOPEN_EVERY:
                self._reopened[session] = now
                self.probe.reopen(self.inbox, session)
            return
        # A closed session whose reopening did not take within REOPEN_MAX_AGE: automatic delivery stops here
        # (a session nobody runs is never reopened days later). The failure is made durable and visible;
        # the messages stay queued and go in once the session runs again or the next message reopens it.
        self._give_up(session, queued, now, oldest)

    def _give_up(self, session: str, queued: List[dict], now: float, oldest: float) -> None:
        from . import delivery
        from .config import home_chat
        home = home_chat(self.cfg)
        if session not in self._stranded_logged:
            self._stranded_logged.add(session)
            self.log(f"delivery {session[:8]}: {len(queued)} old message(s) still queued "
                     f"since {time.strftime('%Y-%m-%d %H:%M', time.localtime(oldest))}; the session is closed")
        for item in queued:
            if item.get("failed") or not item.get("key"):
                continue
            age = now - float(item.get("queued_at") or item.get("at") or now)
            if (item.get("source") == "owner" and type(item.get("id")) is int and type(item.get("chat")) is int
                    and age < NOTICE_MAX_AGE):  # stranded for days before this ran: only the log says so
                delivery.send_failed(self.bot, self.cfg, item["id"], None if item["chat"] == home else item["chat"])
            self.inbox.mark(session, item["key"], until=self._until(), failed=True)

    def _notices(self, session: str, items: List[dict], now: float, is_alive) -> None:
        from . import delivery
        from .config import home_chat
        home = home_chat(self.cfg)
        for item in items:
            if (item.get("noticed") or item.get("source") != "owner" or type(item.get("id")) is not int
                    or type(item.get("chat")) is not int or not item.get("key")):
                continue
            age = now - float(item.get("queued_at") or item.get("at") or now)
            if age < WAITING_NOTICE or age >= NOTICE_MAX_AGE:
                continue
            chat = None if item["chat"] == home else item["chat"]
            if not delivery.has_ack(item["id"], chat):  # a receipt already says it will be delivered
                delivery.send_waiting(self.bot, self.cfg, item["id"], chat, closed=not is_alive())
            self.inbox.mark(session, item["key"], until=self._until(), noticed=True)

    def run(self, stop: threading.Event, tick: float = TICK) -> None:
        try:
            while not stop.wait(tick):
                try:
                    self.sweep()
                except Exception as exc:
                    self.log(f"sweep failed: {type(exc).__name__}: {str(exc)[:120]}")
        finally:
            if self._pool is not None:
                self._pool.shutdown(wait=False)
