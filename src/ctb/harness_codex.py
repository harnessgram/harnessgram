"""Opt-in observer for the persisted Codex rollout JSONL format (CLI 0.163).

This is not the exec --json protocol. Only explicit assistant output is mirrored;
completion summaries, reasoning, tool arguments/results and child messages are
never replies. No terminal control, exec resume, approvals or implicit attachment.
Consumers must persist each event's opaque cursor after handling the event.
The cursor contains only a bounded file/turn boundary. A small in-memory ledger
suppresses recent duplicates; cold restarts rebuild context from the file.
It contains no reply/tool bodies. Historical archived and
ephemeral sessions, UI forwarding and native request/approval transport are not
provided by this adapter. Existing shared egress policy remains authoritative.
"""
from __future__ import annotations

from collections import OrderedDict
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import replace
from typing import Any, Iterable, Mapping, Optional

from .harness import (AssistantText, Capabilities, ControlCommand, DEFAULT_PROFILE,
                      Event, Receipt, ReceiptState, Session, SessionRef, ToolProgress,
                      TurnEnded, TurnStarted, UnsupportedOperation, Usage)

MAX_RECORD_BYTES = 1024 * 1024
MAX_META_BYTES = MAX_RECORD_BYTES
MAX_QUEUE_BYTES = 64 * 1024
RECENT_EVENTS = 128
MAX_CACHED_FILES = 128


class QueueSendError(RuntimeError):
    """Native queue acceptance was not established; do not retry automatically."""


def _uuid(value: str) -> str:
    try:
        if str(uuid.UUID(value)) == value:
            return value
    except (ValueError, TypeError, AttributeError):
        pass
    raise ValueError("A complete canonical session UUID is required")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def _count(value) -> Optional[int]:
    return value if type(value) is int and value >= 0 else None


class CodexHarness:
    def __init__(self, *, sessions_dir=None, profile=DEFAULT_PROFILE, attached=(),
                 commentary=False, mirror=True, codex_home=None):
        self.profile = profile
        SessionRef("codex", profile, "validation")
        self.home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
        self.root = Path(sessions_dir).expanduser() if sessions_dir else self.home / "sessions"
        self.attached = frozenset(_uuid(s) for s in attached)
        self.commentary = commentary is True
        self.mirror = mirror is True
        self._queue_checked = False
        self._queue_retry_at = 0.0
        self._executable = None
        self._metadata = OrderedDict()
        self._contexts = OrderedDict()
        self._attachments = {}
        self._path_cache = OrderedDict()
        self._lock = threading.Lock()

    def _queue_executable(self):
        with self._lock:
            if self._queue_checked or time.monotonic() < self._queue_retry_at:
                return self._executable
            # A custom observation directory is not proof of the CLI's queue home.
            if self.root.resolve() != (self.home / "sessions").resolve():
                self._queue_checked = True
                return None
            executable = shutil.which("codex")
            if executable:
                try:
                    help_result = subprocess.run([executable, "queue", "--help"],
                                                 capture_output=True, timeout=10, check=False)
                    help_text = help_result.stdout.decode("utf-8", errors="replace")
                    if (help_result.returncode == 0 and "--thread <THREAD>" in help_text
                            and "--message <TEXT>" in help_text):
                        self._executable = executable
                except subprocess.TimeoutExpired:
                    self._queue_retry_at = time.monotonic() + 60
                    return None
                except (OSError, subprocess.SubprocessError):
                    self._queue_retry_at = time.monotonic() + 60
                    return None
            self._queue_checked = True
            return self._executable

    def capabilities(self) -> Capabilities:
        return Capabilities(discover=True, observe=True, usage=True,
                            send=bool(self._queue_executable()))

    def _check(self, ref: SessionRef):
        if ref.harness != "codex" or ref.profile != self.profile:
            raise UnsupportedOperation("Wrong harness/profile")
        _uuid(ref.native_id)
        if ref.native_id not in self.attached:
            raise UnsupportedOperation("Session is not explicitly attached")

    def _paths(self, native_id=None):
        # Archived .zst and ephemeral sessions are intentionally unsupported.
        pattern = f"**/rollout-*-{native_id}.jsonl" if native_id else "**/rollout-*.jsonl"
        for path in sorted(self.root.glob(pattern)):
            if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
                continue
            yield path

    @staticmethod
    def _meta(stream):
        try:
            line = stream.readline(MAX_META_BYTES + 1)
            if len(line) > MAX_META_BYTES or not line.endswith(b"\n"):
                return {}
            row = json.loads(line)
            return _object(row.get("payload")) if row.get("type") == "session_meta" else {}
        except (ValueError, AttributeError, UnicodeError, RecursionError):
            return {}

    def _open_file(self, path):
        # The path may change after discovery. Never follow the final symlink
        # or block on a FIFO, and validate the actual opened descriptor.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError("Session file is not regular")
            stream = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        try:
            key = (st.st_dev, st.st_ino)
            # Recheck the first line: an inode can be rewritten in place.
            line = stream.readline(MAX_RECORD_BYTES + 1)
            with self._lock:
                cached = self._metadata.get(key)
                if cached and cached[0] == line:
                    self._metadata.move_to_end(key)
                    return stream, cached[1]
            stream.seek(0)
            meta = self._meta(stream)
            with self._lock:
                if meta:
                    self._metadata[key] = (line, meta)
                    self._metadata.move_to_end(key)
                    if len(self._metadata) > MAX_CACHED_FILES:
                        self._metadata.popitem(last=False)
                else:
                    self._metadata.pop(key, None)
            return stream, meta
        except BaseException:
            stream.close()
            raise

    def discover(self) -> Iterable[Session]:
        result = {}
        for path in self._paths():
            try:
                stream, meta = self._open_file(path)
                st = os.fstat(stream.fileno())
                stream.close()
                native = _uuid(meta.get("id"))
                if meta.get("session_id", native) != native:
                    continue
            except (OSError, ValueError):
                continue
            ref = SessionRef("codex", self.profile, native)
            with self._lock:
                self._path_cache[ref] = (path, (st.st_dev, st.st_ino))
                self._path_cache.move_to_end(ref)
                if len(self._path_cache) > MAX_CACHED_FILES:
                    self._path_cache.popitem(last=False)
            # Persistence proves existence, never process liveness or attachment.
            result[ref] = Session(ref, title=native, cwd=meta.get("cwd") if isinstance(meta.get("cwd"), str) else None,
                                  liveness_provenance="persisted-rollout")
        return list(result.values())

    def _open(self, ref):
        with self._lock:
            cached = self._path_cache.get(ref)

        def invalidate_cached(path):
            if cached and path == cached[0]:
                with self._lock:
                    if self._path_cache.get(ref) == cached:
                        self._path_cache.pop(ref, None)

        def candidates():
            if cached:
                yield cached[0]
            yield from self._paths(ref.native_id)
            yield from self._paths()
        visited = set()
        for path in candidates():
            if path in visited:
                continue
            visited.add(path)
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
                    invalidate_cached(path)
                    continue
                stream, meta = self._open_file(path)
                st = os.fstat(stream.fileno())
                if cached and path == cached[0] and (st.st_dev, st.st_ino) != cached[1]:
                    # Treat a replacement as a fresh candidate, never as a cache hit.
                    invalidate_cached(path)
                if meta.get("id") == ref.native_id and meta.get("session_id", ref.native_id) == ref.native_id:
                    with self._lock:
                        self._path_cache[ref] = (path, (st.st_dev, st.st_ino))
                        self._path_cache.move_to_end(ref)
                        if len(self._path_cache) > MAX_CACHED_FILES:
                            self._path_cache.popitem(last=False)
                    stream.seek(0)
                    return stream
                stream.close()
                invalidate_cached(path)
            except OSError:
                invalidate_cached(path)
                continue
        raise UnsupportedOperation("Persisted session is unavailable")

    @staticmethod
    def _state(ref, cursor):
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError
            state = json.loads(cursor)
            if (state["v"] != 2 or state["ref"] != ref.key or type(state["offset"]) is not int
                    or state["offset"] < 0 or not isinstance(state["file"], list)
                    or len(state["file"]) != 2 or not all(type(n) is int and n >= 0 for n in state["file"])
                    or (state["turn"] is not None and (not isinstance(state["turn"], str)
                                                       or len(state["turn"]) > 256))
                    or not isinstance(state["anchor"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", state["anchor"])
                    or (state["last"] is not None and (not isinstance(state["last"], str)
                                                       or not re.fullmatch(r"[a-f0-9]{64}", state["last"])))):
                raise ValueError
            return state
        except (ValueError, KeyError, TypeError):
            raise ValueError("Invalid Codex cursor or session identity") from None

    @staticmethod
    def _anchor(stream, offset):
        stream.seek(max(0, offset - 4096))
        return hashlib.sha256(stream.read(min(offset, 4096))).hexdigest()

    @staticmethod
    def _context():
        return dict(offset=0, active=None, model=None, tools={}, seen=OrderedDict(), last=None)

    @staticmethod
    def _records(stream, stop=None):
        """Bound every allocation, discard oversized records as whole lines."""
        while stop is None or stream.tell() < stop:
            line = stream.readline(MAX_RECORD_BYTES + 1)
            if len(line) > MAX_RECORD_BYTES:
                logging.getLogger(__name__).debug("Skipping oversized rollout record")
                while line and not line.endswith(b"\n"):
                    line = stream.readline(MAX_RECORD_BYTES + 1)
                if not line.endswith(b"\n"):
                    return
                yield stream.tell(), None
                continue
            if not line.endswith(b"\n"):
                return
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                row = None
            yield stream.tell(), row if isinstance(row, dict) else None

    def _consume(self, ref, row, state):
        if row is None:
            return None
        event = self._event(ref, row, state)
        if event is None:
            return None
        identity = row
        if isinstance(event, AssistantText):
            identity = [event.phase, event.item_id or _digest(event.text)]
        elif isinstance(event, (TurnStarted, TurnEnded, ToolProgress)):
            identity = [getattr(event, "outcome", None), getattr(event, "state", None)]
        # Each ledger belongs to one exact SessionRef; no repeated URL encoding.
        key = _digest([type(event).__name__, event.turn_id, event.item_id, identity])
        if key in state["seen"]:
            return None
        state["seen"][key] = None
        if len(state["seen"]) > RECENT_EVENTS:
            state["seen"].popitem(last=False)
        state["last"] = key
        return event

    def _cursor(self, ref, stream, state):
        st = os.fstat(stream.fileno())
        offset = state["offset"]
        boundary = dict(v=2, ref=ref.key, offset=offset, file=[st.st_dev, st.st_ino],
                        anchor=self._anchor(stream, offset), turn=state["active"], last=state["last"])
        stream.seek(offset)
        cursor = json.dumps(boundary, separators=(",", ":"))
        snapshot = dict(state, tools=dict(state["tools"]), seen=OrderedDict(state["seen"]))
        with self._lock:
            self._contexts[ref] = (cursor, snapshot)
            self._contexts.move_to_end(ref)
            if len(self._contexts) > MAX_CACHED_FILES:
                self._contexts.popitem(last=False)
        return cursor

    def initial_cursor(self, ref: SessionRef, *, replay=False) -> str:
        """Attach at the latest whole record; history requires explicit replay."""
        self._check(ref)
        with self._open(ref) as stream:
            state = self._context()
            if not replay:
                for offset, row in self._records(stream):
                    self._consume(ref, row, state)
                    state["offset"] = offset
            return self._cursor(ref, stream, state)

    def _restore(self, ref, stream, cursor, boundary):
        st = os.fstat(stream.fileno())
        unchanged = (boundary["file"] == [st.st_dev, st.st_ino] and st.st_size >= boundary["offset"]
                     and self._anchor(stream, boundary["offset"]) == boundary["anchor"])
        with self._lock:
            cached = self._contexts.get(ref)
        if (unchanged and cached and cached[0] == cursor
                and cached[1]["offset"] == boundary["offset"]):
            state = dict(cached[1])
            state.update(tools=dict(state["tools"]), seen=OrderedDict(state["seen"]))
            return state
        state = self._context()
        stream.seek(0)
        # Cold restart: reconstruct context without persisting reply hashes.
        # Replacement: find the last emitted boundary before allowing replies.
        found_turn = False
        next_turn = None
        for offset, row in self._records(stream, boundary["offset"] if unchanged else None):
            previous_offset = state["offset"]
            event = self._consume(ref, row, state)
            state["offset"] = offset
            if unchanged:
                continue
            if state["active"] == boundary["turn"]:
                found_turn = True
            elif found_turn and next_turn is None:
                next_turn = previous_offset
            if event is not None and state["last"] == boundary["last"]:
                return state
        if unchanged:
            return state
        # A removed boundary in a retained turn is ambiguous: suppress that
        # turn. If rotation removed it entirely, only the replacement remains.
        if found_turn and next_turn is None:
            return state
        restart = next_turn if found_turn else 0
        state = self._context()
        stream.seek(0)
        for offset, row in self._records(stream, restart or 0):
            self._consume(ref, row, state)
            state["offset"] = offset
        return state

    def events(self, ref: SessionRef, cursor: Optional[str] = None, *, replay=False) -> Iterable[Event]:
        """With cursor=None, continue after this process's last delivered event."""
        self._check(ref)
        implicit = cursor is None and not replay
        if cursor is None:
            if replay:
                cursor = self.initial_cursor(ref, replay=True)
            else:
                with self._lock:
                    cursor = self._attachments.get(ref)
                if cursor is None:
                    initial = self.initial_cursor(ref)
                    with self._lock:
                        cursor = self._attachments.setdefault(ref, initial)
        boundary = self._state(ref, cursor)
        with self._open(ref) as stream:
            state = self._restore(ref, stream, cursor, boundary)
            stream.seek(state["offset"])
            for offset, row in self._records(stream):
                event = self._consume(ref, row, state)
                state["offset"] = offset
                if event is not None:
                    delivered = self._cursor(ref, stream, state)
                    if implicit:
                        with self._lock:
                            self._attachments[ref] = delivered
                    yield replace(event, cursor=delivered)

    def _event(self, ref, row, state):
        p = _object(row.get("payload"))
        # Explicit foreign thread/session attribution must not affect this stream.
        if any(p.get(k) not in (None, ref.native_id) for k in ("thread_id", "session_id")):
            return None
        kind, sub = row.get("type"), p.get("type")
        metadata = _object(p.get("internal_chat_message_metadata_passthrough"))
        turn = p.get("turn_id") or metadata.get("turn_id") or state["active"]
        if turn is not None and (not isinstance(turn, str) or len(turn) > 256):
            return None
        common = dict(ref=ref, turn_id=turn if isinstance(turn, str) else None,
                      timestamp=row.get("timestamp") if isinstance(row.get("timestamp"), str) else None)
        if kind == "turn_context":
            if turn != state["active"]:
                return None
            state["model"] = p.get("model") if isinstance(p.get("model"), str) and len(p["model"]) <= 256 else None
            return Usage(**common, model=state["model"])
        if kind == "event_msg" and sub == "task_started":
            if not isinstance(turn, str):
                return None
            state["active"] = turn
            state["tools"] = {}
            return TurnStarted(**common)
        if kind == "event_msg" and sub in ("task_complete", "turn_aborted", "error", "task_failed"):
            if turn != state["active"] or turn is None:
                return None  # a late old completion must not mark the current turn idle
            return TurnEnded(**common, outcome="completed" if sub == "task_complete" else "error")
        if kind == "event_msg" and sub == "token_count":
            if turn != state["active"]:
                return None
            info = _object(p.get("info"))
            usage = _object(info.get("last_token_usage"))
            return Usage(**common, input_tokens=_count(usage.get("input_tokens")),
                         output_tokens=_count(usage.get("output_tokens")),
                         context_tokens=_count(usage.get("total_tokens")), model=state["model"])
        if kind != "response_item":
            return None
        if turn != state["active"] or turn is None:
            return None
        item_id = p.get("id") or p.get("call_id")
        common["item_id"] = item_id if isinstance(item_id, str) else None
        if sub == "message":
            phase = p.get("phase")
            if (not self.mirror or p.get("role") != "assistant" or p.get("recipient", "all") != "all"
                    or phase not in (("final_answer", "final", "commentary") if self.commentary
                                     else ("final_answer", "final"))):
                return None
            content = p.get("content")
            if not isinstance(content, list):
                return None
            text = "\n".join(c["text"] for c in content if isinstance(c, dict)
                             and c.get("type") == "output_text" and isinstance(c.get("text"), str))
            from .creds import for_outside
            text = for_outside(text).strip()
            if text:
                return AssistantText(**common, text=text, phase="final" if phase != "commentary" else phase,
                                     final=phase != "commentary")
        if sub in ("function_call", "custom_tool_call"):
            name = p.get("name", "")
            name = name if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", name) else "tool"
            call = p.get("call_id")
            if isinstance(call, str) and len(call) <= 256:
                if len(state["tools"]) >= RECENT_EVENTS:
                    state["tools"].pop(next(iter(state["tools"])))
                state["tools"][call] = name
            return ToolProgress(**common, tool=name, state="started")
        if sub in ("function_call_output", "custom_tool_call_output"):
            call = p.get("call_id")
            name = state["tools"].pop(call, "tool") if isinstance(call, str) else "tool"
            return ToolProgress(**common, tool=name, state="completed")
        return None

    def send(self, ref: SessionRef, message: Mapping[str, Any]) -> Receipt:
        self._check(ref)
        # Require a still-discoverable exact session; never start/resume one.
        with self._open(ref):
            pass
        executable = self._queue_executable()
        if not executable:
            raise UnsupportedOperation("No verified native Codex queue surface")
        if not isinstance(message.get("text"), str) or not message["text"].strip():
            raise ValueError("Message text is required")
        if message.get("hold"):
            raise UnsupportedOperation("Native queue does not guarantee end-of-turn delivery")
        from .framing import deliver
        item = dict(message)
        # Ordinary queued text never grants a native RPC approval: only
        # inbox._queue_owner may set the typed flag, so drop any copy here.
        item.pop("typed", None)
        if item.get("source") == "panel":
            raise UnsupportedOperation("Codex panel approvals are unsupported")
        key = item.pop("key", None) or uuid.uuid4().hex
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key):
            raise ValueError("Invalid message key")
        text = deliver("[Message via Telegram]", [item], "Respond in the existing session.",
                       nonce=secrets.token_hex(8))
        # Verified queue help exposes only --message, not stdin/file input.
        if "\0" in text or len(text.encode("utf-8")) > MAX_QUEUE_BYTES:
            raise QueueSendError("Native queue rejected the message: text is too long or invalid")
        args = [executable, "queue", "--thread", ref.native_id, "--message", text]
        if self.profile != DEFAULT_PROFILE:
            args += ["--profile=" + self.profile]
        env = dict(os.environ, CODEX_HOME=str(self.home))
        try:
            result = subprocess.run(args, capture_output=True, timeout=20, check=False, env=env)
        except OSError as exc:
            if exc.errno == errno.E2BIG:
                raise QueueSendError("Native queue rejected the message: arguments are too long") from None
            raise QueueSendError("Native queue acceptance is unknown; no automatic retry") from None
        except subprocess.SubprocessError:
            raise QueueSendError("Native queue acceptance is unknown; no automatic retry") from None
        if result.returncode != 0:
            raise QueueSendError("Native queue rejected the message")
        return Receipt(ref, ReceiptState.ACCEPTED, key)

    def control(self, ref: SessionRef, command: ControlCommand) -> Receipt:
        self._check(ref)
        raise UnsupportedOperation("Codex control and request-bound approvals are unsupported")
