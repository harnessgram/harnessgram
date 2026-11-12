"""Shared session identity and adapter contract.

The default Claude profile means the existing account-routing policy, including
account moves. It is not an account name. Only that profile may read raw legacy
state keys. Discovery never enables forwarding or launches a session.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
import re
import uuid
from typing import Any, Iterable, Mapping, Optional, Protocol, Union, runtime_checkable
from urllib.parse import quote, unquote

DEFAULT_PROFILE = "default"


@dataclass(frozen=True)
class SessionRef:
    harness: str
    profile: str
    native_id: str

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", self.harness):
            raise ValueError("Invalid harness name")
        if not self.profile or not self.native_id:
            raise ValueError("Profile and native session ID are required")

    @property
    def key(self) -> str:
        """An exact, reversible state-map key; never a native ID prefix."""
        return ":".join(quote(v, safe="") for v in (self.harness, self.profile, self.native_id))

    @classmethod
    def parse(cls, value: Union[str, SessionRef]) -> SessionRef:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str) or not value:
            raise ValueError("Session identity is required")
        if not re.fullmatch(r"[a-z][a-z0-9_-]*:[A-Za-z0-9_.~%-]+:[A-Za-z0-9_.~%-]+", value):
            # Mirror titles are opaque Claude IDs too, including spaces, Unicode
            # and colons. Only an explicit harness:profile:id is namespaced.
            return cls("claude", DEFAULT_PROFILE, value)
        parts = value.split(":")
        ref = cls(*(unquote(v) for v in parts))
        if ref.key != value:
            raise ValueError("Session key must use canonical escaping")
        return ref

    @property
    def legacy_key(self) -> Optional[str]:
        if self.harness == "claude" and self.profile == DEFAULT_PROFILE:
            return self.native_id
        return None


class UnsupportedOperation(ValueError):
    """The adapter has no verified implementation for this operation."""


def legacy_topic_supported(session: Optional[str]) -> bool:
    """Shared identities are API-only until every legacy topic control is migrated."""
    if session is None:
        return True
    try:
        ref = SessionRef.parse(session)
    except ValueError:
        return False
    return ref.legacy_key == session


@dataclass(frozen=True)
class Capabilities:
    version: int = 1
    discover: bool = False
    observe: bool = False
    send: bool = False
    steer: bool = False
    interrupt: bool = False
    resume: bool = False
    tool_approvals: bool = False
    user_questions: bool = False
    children: bool = False
    usage: bool = False


@dataclass(frozen=True)
class Session:
    ref: SessionRef
    title: str = ""
    cwd: Optional[str] = None
    model: Optional[str] = None
    parent: Optional[SessionRef] = None
    alive: Optional[bool] = None
    liveness_provenance: str = "unknown"


@dataclass(frozen=True)
class Event:
    ref: SessionRef
    cursor: Optional[str] = None
    turn_id: Optional[str] = None
    item_id: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass(frozen=True)
class TurnStarted(Event):
    pass


@dataclass(frozen=True)
class AssistantText(Event):
    text: str = ""
    phase: str = "unknown"
    final: bool = False


@dataclass(frozen=True)
class ToolProgress(Event):
    tool: str = ""
    state: str = "unknown"


@dataclass(frozen=True)
class TurnEnded(Event):
    outcome: str = "unknown"


@dataclass(frozen=True)
class NeedsInput(Event):
    request_id: Optional[str] = None
    kind: str = "question"


@dataclass(frozen=True)
class Usage(Event):
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    context_tokens: Optional[int] = None
    model: Optional[str] = None


class ReceiptState(str, Enum):
    QUEUED = "queued"
    ACCEPTED = "accepted"
    OBSERVED = "observed"
    ANSWERED = "answered"


@dataclass(frozen=True)
class Receipt:
    ref: SessionRef
    state: ReceiptState = ReceiptState.QUEUED
    message_id: Optional[str] = None

    @property
    def delivered(self) -> bool:
        """Only observed ingestion or an answer proves delivery, not enqueueing."""
        return self.state in (ReceiptState.OBSERVED, ReceiptState.ANSWERED)


class Control(str, Enum):
    START = "start"
    RESUME = "resume"
    STEER = "steer"
    INTERRUPT = "interrupt"
    ANSWER = "answer"
    APPROVE = "approve"


@dataclass(frozen=True)
class ControlCommand:
    action: Control
    request_id: Optional[str] = None
    turn_id: Optional[str] = None
    text: str = ""


@runtime_checkable
class Harness(Protocol):
    def capabilities(self) -> Capabilities: ...
    def discover(self) -> Iterable[Session]: ...
    def events(self, ref: SessionRef, cursor: Optional[str] = None) -> Iterable[Event]: ...
    def send(self, ref: SessionRef, message: Mapping[str, Any]) -> Receipt: ...
    def control(self, ref: SessionRef, command: ControlCommand) -> Receipt: ...


class HarnessRegistry:
    def __init__(self):
        self._adapters = {}

    def register(self, name: str, profile: str, adapter: Harness) -> None:
        SessionRef(name, profile, "validation")
        key = (name, profile)
        if key in self._adapters:
            raise ValueError("An adapter is already registered for this harness/profile")
        self._adapters[key] = adapter

    def resolve(self, session: Union[str, SessionRef]) -> tuple[Harness, SessionRef]:
        ref = SessionRef.parse(session)
        try:
            return self._adapters[(ref.harness, ref.profile)], ref
        except KeyError:
            raise UnsupportedOperation("No adapter for this harness/profile") from None

    @staticmethod
    def lookup(state: Mapping[str, Any], session: Union[str, SessionRef], default=None):
        """Read namespaced state first; raw legacy keys belong only to Claude/default."""
        ref = SessionRef.parse(session)
        if ref.key in state:
            return state[ref.key]
        if ref.legacy_key is not None:
            return state.get(ref.legacy_key, default)
        return default

    def send(self, session: Union[str, SessionRef], message: Mapping[str, Any]) -> Receipt:
        adapter, ref = self.resolve(session)
        if not adapter.capabilities().send:
            raise UnsupportedOperation("Sending is unsupported")
        return adapter.send(ref, message)


class ClaudeHarness:
    """Delegate to legacy readers and queue without changing framing or wake policy.

    Existing hooks still own observation, questions and control. A cursor-based
    pull stream and native controls are deliberately unadvertised here; calling
    them cannot guess terminal keys or synthesize approval from ordinary text.
    """
    def __init__(self, inbox=None, topics=None):
        self._inbox = inbox
        self._topics = topics

    def capabilities(self) -> Capabilities:
        return Capabilities(discover=True, send=True)

    @staticmethod
    def _native(ref: SessionRef) -> str:
        if ref.legacy_key is None:
            raise UnsupportedOperation("Legacy Claude routing requires the default profile")
        # Do not reinterpret a nested namespaced identity as a raw Claude ID.
        # Legacy readers retain their own path checks and queue filename escaping.
        legacy = SessionRef.parse(ref.native_id)
        if legacy != ref:
            raise ValueError("Invalid native Claude session ID")
        return ref.native_id

    def discover(self) -> Iterable[Session]:
        from . import status
        from .config import home_chat, load, state_dir
        from .topics import TopicMap

        topics = self._topics
        if topics is None:
            cfg = load()
            topics = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg)
        result = {}
        state = topics.all_places()
        for key in state:
            try:
                ref = SessionRef.parse(key)
                native = self._native(ref)
            except (ValueError, UnsupportedOperation):
                continue
            if ref in result:
                continue
            info = HarnessRegistry.lookup(state, ref)
            path = status.transcript_of(native)
            cwd, _ = status.workplace(path)
            model = status.usage_of(path)[1] if path else None
            result[ref] = Session(ref, title=info.get("name") or native[:8],
                                  cwd=cwd or None, model=model or None)
        return list(result.values())

    def events(self, ref: SessionRef, cursor: Optional[str] = None) -> Iterable[Event]:
        self._native(ref)
        raise UnsupportedOperation("Claude observation remains in the existing hook pipeline")

    def send(self, ref: SessionRef, message: Mapping[str, Any]) -> Receipt:
        native = self._native(ref)
        inbox = self._inbox
        if inbox is None:
            from .config import state_dir
            from .inbox import Inbox
            inbox = Inbox(state_dir() / "inbox")
        # Preserve every origin, hold, framing and deduplication field. The
        # legacy queue supplies the time; use its key format so later evidence
        # can correlate this receipt. Writing is not ingestion.
        item = dict(message)
        item.setdefault("key", uuid.uuid4().hex[:16])
        if not isinstance(item["key"], str) or not item["key"]:
            raise ValueError("Message key must be a nonempty string")
        inbox.put(native, item)
        return Receipt(ref, message_id=item["key"])

    def control(self, ref: SessionRef, command: ControlCommand) -> Receipt:
        self._native(ref)
        raise UnsupportedOperation("No shared Claude control transport is enabled")


def registry(*, inbox=None, topics=None, cfg=None) -> HarnessRegistry:
    """Task-local dependencies keep the existing queue and topic-map policy intact."""
    result = HarnessRegistry()
    result.register("claude", DEFAULT_PROFILE, ClaudeHarness(inbox=inbox, topics=topics))
    if cfg is not None and cfg.codex_harness_enabled is True:
        from .harness_codex import CodexHarness
        try:
            adapter = CodexHarness(
                sessions_dir=cfg.codex_sessions_dir or None,
                attached=cfg.codex_attached_sessions, commentary=cfg.codex_commentary,
                mirror=cfg.mirror)
        except Exception:
            logging.getLogger(__name__).warning("Codex disabled: initialization failed")
        else:
            result.register("codex", DEFAULT_PROFILE, adapter)
    return result
