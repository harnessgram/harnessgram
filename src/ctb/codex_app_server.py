"""Stateful Codex app-server JSON-RPC client.

The protocol engine is transport agnostic so tests can use a socketpair.  The
production listener is owned by :class:`CodexServer`; it never puts credentials
or prompts in argv.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass, field
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import struct
import subprocess
import threading
import time
from typing import Callable, Optional

from .config import state_dir, write_private


APPROVAL_METHODS = frozenset({
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
})


class ProtocolError(RuntimeError):
    pass


class NotDelivered(ProtocolError):
    """The request certainly did not take effect: refused before sending, or answered with an error."""


@dataclass
class ThreadState:
    thread_id: str
    model: Optional[str] = None
    effort: Optional[str] = None
    active_turn_id: Optional[str] = None
    status: str = "idle"
    usage: dict = field(default_factory=dict)
    revision: int = 0


@dataclass(frozen=True)
class Approval:
    request_id: object
    method: str
    params: dict
    choices: tuple


class CodexProtocol:
    """Newline JSON-RPC protocol state machine used with stdio/socketpair.

    ``feed`` is deliberately public: a WebSocket adapter supplies one decoded
    message at a time, while synthetic tests use ``serve`` with a socketpair.
    """
    def __init__(self, send: Callable[[dict], None], *, owner_id=None,
                 on_event: Optional[Callable[[str, dict], None]] = None,
                 cursor_path: Optional[Path] = None):
        self._send = send
        self.owner_id = owner_id
        self.on_event = on_event or (lambda method, params: None)
        self.cursor_path = cursor_path
        self.threads: dict[str, ThreadState] = {}
        self.approvals: dict[object, Approval] = {}
        self._answered = OrderedDict()
        self._seen = OrderedDict()
        self.closed = threading.Event()
        self._commands = threading.Lock()
        self._transport = None
        self._next_id = 1
        self._pending: dict[object, tuple[threading.Event, dict]] = {}
        self._lock = threading.RLock()
        self._load_cursor()

    def _load_cursor(self):
        if not self.cursor_path:
            return
        try:
            data = json.loads(self.cursor_path.read_text())
            self._seen.update(dict.fromkeys(data.get("seen", [])[-512:]))
        except (OSError, ValueError, TypeError):
            pass

    def _save_cursor(self):
        if self.cursor_path:
            write_private(self.cursor_path, json.dumps({"seen": list(self._seen)[-512:]}))

    def close(self):
        with self._lock:
            self.closed.set()
            self.approvals.clear()
            for done, box in self._pending.values():
                box["disconnected"] = True
                done.set()
            self._pending.clear()
        if self._transport is not None:
            self._transport.close()

    def notify(self, method):
        self._write({"method": method})

    def _write(self, message):
        try:
            self._send(message)
        except OSError:
            self.close()
            raise

    def request(self, method, params=None, timeout=10):
        with self._lock:
            if self.closed.is_set():
                raise NotDelivered("Codex app-server disconnected")
            request_id = self._next_id
            self._next_id += 1
            done, box = threading.Event(), {}
            self._pending[request_id] = (done, box)
        try:
            self._write({"method": method, "id": request_id, "params": params or {}})
            if not done.wait(timeout):
                raise TimeoutError("Codex app-server request timed out")
            if "disconnected" in box:
                raise ProtocolError("Codex app-server disconnected")
            if "error" in box:
                raise NotDelivered("Codex app-server rejected the request")
            return box.get("result", {})
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def initialize(self):
        result = self.request("initialize", {"clientInfo": {
            "name": "harnessgram", "title": "Harnessgram", "version": "1"
        }, "capabilities": {"experimentalApi": True, "requiredHumanApproval": True}})
        self.notify("initialized")
        return result

    def start_thread(self, *, cwd=None, model=None, effort=None):
        params = {"approvalsReviewer": "user"}
        if cwd is not None:
            params["cwd"] = str(Path(cwd).resolve())
        if model is not None:
            params["model"] = model
        if effort is not None:
            if not isinstance(effort, str) or not effort.strip():
                raise ValueError("A reasoning effort value is required")
            params["config"] = {"model_reasoning_effort": effort}
        result = self.request("thread/start", params)
        thread_id = result["thread"]["id"]
        with self._lock:
            state = self.threads.setdefault(thread_id, ThreadState(thread_id))
            state.model = result.get("model", model)
            state.effort = result.get("reasoningEffort") or effort
        return thread_id

    def attach(self, thread_id, effort=None):
        """Subscribe to a known thread and recover the live turn after reconnect."""
        with self._lock:
            state = self.threads.setdefault(thread_id, ThreadState(thread_id))
            revision = state.revision
            state.status = "connecting"
        params = {"threadId": thread_id, "excludeTurns": True, "approvalsReviewer": "user"}
        if effort:
            params["config"] = {"model_reasoning_effort": effort}
        result = self.request("thread/resume", params)
        if result["thread"]["id"] != thread_id:
            raise ProtocolError("Codex app-server returned a different thread")
        status = result["thread"].get("status", {})
        turns = []
        if status.get("type") == "active":
            turns = self.request("thread/turns/list", {
                "threadId": thread_id, "limit": 1, "itemsView": "notLoaded",
            }).get("data", [])
        with self._lock:
            if state.model is None:
                state.model = result.get("model")
                state.effort = result.get("reasoningEffort") or effort
            if state.revision == revision:
                turn = next((t for t in turns if t.get("status") == "inProgress"), {})
                state.active_turn_id = turn.get("id")
                state.status = {"active": "working", "idle": "idle"}.get(status.get("type"), "unavailable")
                if status.get("activeFlags"):
                    state.status = "waiting_for_approval"
            return state

    def send_text(self, thread_id, text):
        with self._commands:
            with self._lock:
                state = self.threads[thread_id]
                turn_id, revision = state.active_turn_id, state.revision
                status, effort = state.status, state.effort
            params = {"threadId": thread_id, "input": [{"type": "text", "text": text, "text_elements": []}]}
            if turn_id:
                params["expectedTurnId"] = turn_id
                return self.request("turn/steer", params)
            if status != "idle":
                raise NotDelivered("Codex thread state is unavailable")
            if effort:
                params["effort"] = effort
            result = self.request("turn/start", params)
            turn = result["turn"]
            with self._lock:
                if state.revision == revision:
                    state.active_turn_id = turn["id"] if turn.get("status", "inProgress") == "inProgress" else None
                    self._refresh_turn(state)
            return result

    def interrupt(self, thread_id, expected_turn_id=None):
        with self._lock:
            state = self.threads.get(thread_id)
            turn_id = state.active_turn_id if state else None
            if not turn_id or (expected_turn_id is not None and expected_turn_id != turn_id):
                raise ProtocolError("Thread has no matching active turn")
        return self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def compact(self, thread_id):
        return self.request("thread/compact/start", {"threadId": thread_id})

    def answer(self, request_id, choice, *, actor_id, expected=None):
        with self._lock:
            approval = self.approvals.get(request_id)
            if self.owner_id is None or type(actor_id) is not int or actor_id != self.owner_id:
                raise PermissionError("Only the paired owner may answer")
            if (self.closed.is_set() or approval is None or
                    (expected is not None and approval is not expected)):
                raise ProtocolError("Approval is stale or already answered")
            state = self.threads.get(approval.params.get("threadId"))
            if (state and state.active_turn_id and approval.params.get("turnId")
                    and state.active_turn_id != approval.params["turnId"]):
                raise ProtocolError("Approval belongs to an earlier turn")
            result = self._answer_result(approval, choice)
            self._remember_answered(request_id)
            self.approvals.pop(request_id)
            thread = self.threads.get(approval.params.get("threadId"))
            if thread and thread.active_turn_id:
                self._refresh_turn(thread)
            # An ambiguous write consumes the choice so it is never reapplied.
            self._write({"id": request_id, "result": result})

    def _remember_answered(self, request_id):
        self._answered[request_id] = None
        self._answered.move_to_end(request_id)
        if len(self._answered) > 512:
            self._answered.popitem(last=False)

    @staticmethod
    def _answer_result(approval, choice):
        method = approval.method
        if method == "item/tool/requestUserInput":
            questions = approval.params.get("questions", [])
            if not questions or not isinstance(choice, dict) or choice.keys() != {q["id"] for q in questions}:
                raise ProtocolError("Answer every current question")
            for q in questions:
                value = choice[q["id"]]
                options = q.get("options") or []
                if (q.get("isSecret") or not isinstance(value, str) or not value.strip()
                        or (options and not q.get("isOther") and value not in {o["label"] for o in options})):
                    raise ProtocolError("Answer was not offered")
            return {"answers": {key: {"answers": [value]} for key, value in choice.items()}}
        if choice not in approval.choices:
            raise ProtocolError("Choice was not offered")
        if method == "item/permissions/requestApproval":
            return {"permissions": approval.params["permissions"] if choice == "grant" else {}, "scope": "turn"}
        if method == "mcpServer/elicitation/request":
            return {"action": choice}
        return {"decision": choice}

    @staticmethod
    def _choices(method, params):
        if method == "item/permissions/requestApproval":
            return ("grant", "decline") if isinstance(params.get("permissions"), dict) else ()
        if method == "mcpServer/elicitation/request":
            return ("decline", "cancel")
        if method == "item/tool/requestUserInput":
            return ()
        supported = ("accept", "decline", "cancel")
        if params.get("kind") != "humanCommand":
            supported += ("acceptForSession",)
        offered = params.get("availableDecisions")
        if isinstance(offered, list):
            return tuple(choice for choice in offered if choice in supported)
        return supported

    def _refresh_turn(self, state):
        if any(a.params.get("threadId") == state.thread_id for a in self.approvals.values()):
            state.status = "waiting_for_approval"
        else:
            state.status = "working" if state.active_turn_id else "idle"

    def feed(self, message):
        if not isinstance(message, dict):
            raise ProtocolError("Invalid JSON-RPC message")
        request_id = message.get("id")
        method, params = message.get("method"), message.get("params") or {}
        thread_id = params.get("threadId")
        with self._lock:
            if self.closed.is_set():
                return
            if "result" in message or "error" in message:
                pending = self._pending.pop(request_id, None)
                if pending:
                    pending[1].update(message)
                    pending[0].set()
                return
            state = self.threads.get(thread_id)
            if request_id is not None and method in APPROVAL_METHODS:
                if request_id in self._answered or request_id in self.approvals:
                    return
                approval = Approval(request_id, method, params, self._choices(method, params))
                self.approvals[request_id] = approval
                if state:
                    state.status = "waiting_for_approval"
                params = {**params, "requestId": request_id, "choices": approval.choices}
            elif method == "serverRequest/resolved":
                resolved = params.get("requestId")
                approval = self.approvals.get(resolved)
                if approval and thread_id and approval.params.get("threadId") != thread_id:
                    return
                self.approvals.pop(resolved, None)
                self._remember_answered(resolved)
                if state and state.active_turn_id:
                    self._refresh_turn(state)
            elif state and method == "turn/started":
                state.revision += 1
                state.active_turn_id = params["turn"]["id"]
                self._refresh_turn(state)
            elif state and method == "turn/completed":
                turn_id = params["turn"]["id"]
                for ident, approval in list(self.approvals.items()):
                    if approval.params.get("threadId") == thread_id and approval.params.get("turnId") == turn_id:
                        self._remember_answered(ident)
                        del self.approvals[ident]
                if state.active_turn_id in (None, turn_id):
                    state.revision += 1
                    state.active_turn_id = None
                self._refresh_turn(state)
            elif state and method == "thread/status/changed":
                status = params["status"]
                if status.get("type") in ("notLoaded", "systemError"):
                    state.revision += 1
                    state.active_turn_id = None
                    state.status = "unavailable"
                elif status.get("activeFlags"):
                    state.status = "waiting_for_approval"
                elif state.active_turn_id or status.get("type") == "idle":
                    self._refresh_turn(state)
            elif state and method in ("thread/closed", "thread/deleted", "thread/archived"):
                state.revision += 1
                state.active_turn_id = None
                state.status = "unavailable"
                for ident, approval in list(self.approvals.items()):
                    if approval.params.get("threadId") == thread_id:
                        self._remember_answered(ident)
                        del self.approvals[ident]
            elif state and method == "thread/tokenUsage/updated":
                state.usage = params["tokenUsage"]
            elif state and method == "thread/settings/updated":
                settings = params["threadSettings"]
                state.model, state.effort = settings.get("model"), settings.get("effort")
        identity = (params.get("item") or {}).get("id")
        event_key = (f"{thread_id}:{params.get('turnId')}:{identity}"
                     if identity and method == "item/completed" else None)
        if event_key:
            if event_key in self._seen:
                return
            self._seen[event_key] = None
            if len(self._seen) > 512:
                self._seen.popitem(last=False)
            self._save_cursor()
        self.on_event(method, params)

    def serve(self, stream):
        try:
            for line in stream:
                if line:
                    self.feed(json.loads(line))
        finally:
            self.close()


class CodexServer:
    """Own one private app-server process/socket for a Codex home."""
    def __init__(self, codex_home=None):
        self.home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()
        key = hashlib.sha256(os.fsencode(self.home)).hexdigest()[:16]
        self.directory = state_dir() / "codex-app-server" / key
        socket_dir = self.directory
        if len(os.fsencode(socket_dir / "app-server.sock")) >= 104:
            socket_dir = Path("/tmp") / f"ctb-codex-{os.getuid()}" / key
        self.socket_path = socket_dir / "app-server.sock"
        self.process = None

    @property
    def endpoint(self):
        return "unix://" + str(self.socket_path)

    def _socket_identity(self):
        st = self.socket_path.lstat()
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
            raise ProtocolError("Unsafe Codex app-server socket")
        return st.st_dev, st.st_ino

    @staticmethod
    def _private_socket_dir(directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = directory.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ProtocolError("Unsafe Codex socket directory")

    def ensure_started(self):
        directory = self.socket_path.parent
        if directory != self.directory:
            self._private_socket_dir(directory.parent)
        self._private_socket_dir(directory)
        with (directory / "start.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                identity = self._socket_identity()
            except FileNotFoundError:
                identity = None
            if identity is not None:
                # Replace only a confirmed orphan, never a live or ambiguous endpoint.
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(1)
                    try:
                        probe.connect(str(self.socket_path))
                    except OSError as exc:
                        if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                            raise
                    else:
                        os.chmod(self.socket_path, 0o600)
                        return self.endpoint
                if self.socket_path.exists():
                    if self._socket_identity() != identity:
                        raise ProtocolError("Codex socket changed during health check")
                    self.socket_path.unlink()
            executable = shutil.which("codex")
            if not executable:
                raise FileNotFoundError("codex CLI is unavailable")
            allowed = ("PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "SHELL",
                       "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR")
            env = {key: value for key, value in os.environ.items()
                   if key in allowed or key.startswith(("CODEX_", "OPENAI_"))}
            env["CODEX_HOME"] = str(self.home)
            self.process = subprocess.Popen([executable, "app-server", "--listen", self.endpoint],
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, env=env, start_new_session=True)
            try:
                for _ in range(100):
                    if self.process.poll() is not None:
                        raise OSError("Codex app-server exited before creating its socket")
                    try:
                        self._socket_identity()
                    except FileNotFoundError:
                        time.sleep(.05)
                    else:
                        os.chmod(self.socket_path, 0o600)
                        return self.endpoint
                raise TimeoutError("Codex app-server socket was not ready")
            except BaseException:
                self.close()
                raise

    def connect(self, **protocol_options):
        transport = _UnixWebSocket(self.socket_path)
        protocol = CodexProtocol(transport.send, **protocol_options)
        protocol._transport = transport
        reader = threading.Thread(target=transport.read_into, args=(protocol,), daemon=True,
                                  name="codex-app-server-reader")
        reader.start()
        try:
            protocol.initialize()
        except BaseException:
            protocol.close()
            raise
        return protocol

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


class _UnixWebSocket:
    """Minimal RFC 6455 text client for app-server's private Unix socket."""
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._buffer = bytearray()
        self._write_lock = threading.Lock()
        try:
            self.sock.settimeout(3)
            self.sock.connect(str(path))
            key = base64.b64encode(os.urandom(16)).decode()
            request = ("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                       "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                       f"Sec-WebSocket-Key: {key}\r\n\r\n").encode()
            self.sock.sendall(request)
            while b"\r\n\r\n" not in self._buffer:
                part = self.sock.recv(4096)
                if not part or len(self._buffer) + len(part) > 16384:
                    raise ProtocolError("Codex app-server WebSocket handshake failed")
                self._buffer.extend(part)
            end = self._buffer.index(b"\r\n\r\n")
            response = bytes(self._buffer[:end]).split(b"\r\n")
            del self._buffer[:end + 4]
            headers = dict((field.lower(), value.strip()) for field, _, value
                           in (row.partition(b":") for row in response[1:]))
            # RFC 6455 defines this fixed handshake suffix.
            websocket_guid = "-".join(("258EAFA5", "E914", "47DA", "95CA", "C5AB0DC85B11"))
            expected = base64.b64encode(hashlib.sha1((key + websocket_guid).encode()).digest())
            if (response[0].split()[0:2] != [b"HTTP/1.1", b"101"]
                    or headers.get(b"sec-websocket-accept") != expected):
                raise ProtocolError("Codex app-server WebSocket handshake failed")
            self.sock.settimeout(3)
        except BaseException:
            self.sock.close()
            raise

    def send(self, message):
        self._frame(1, json.dumps(message, separators=(",", ":")).encode())

    def _frame(self, opcode, data):
        mask = os.urandom(4)
        head = bytearray([0x80 | opcode])
        if len(data) < 126:
            head.append(0x80 | len(data))
        elif len(data) < 65536:
            head.append(0x80 | 126)
            head.extend(struct.pack("!H", len(data)))
        else:
            head.append(0x80 | 127)
            head.extend(struct.pack("!Q", len(data)))
        body = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        with self._write_lock:
            self.sock.sendall(head + mask + body)

    def _exact(self, size):
        data = bytearray(self._buffer[:size])
        del self._buffer[:size]
        while len(data) < size:
            try:
                part = self.sock.recv(size - len(data))
            except socket.timeout:
                continue
            if not part:
                raise EOFError
            data.extend(part)
        return bytes(data)

    def read_into(self, protocol):
        fragments = bytearray()
        fragmented = False
        try:
            while True:
                first, second = self._exact(2)
                opcode, length = first & 15, second & 127
                final = bool(first & 128)
                if length == 126:
                    length = struct.unpack("!H", self._exact(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._exact(8))[0]
                if first & 0x70 or second & 0x80 or length + len(fragments) > 16 * 1024 * 1024:
                    raise ProtocolError("Invalid Codex WebSocket frame")
                payload = self._exact(length)
                if opcode == 8:
                    return
                if opcode == 9:
                    self._frame(10, payload)
                    continue
                if opcode == 10:
                    continue
                if opcode != (0 if fragmented else 1):
                    raise ProtocolError("Invalid Codex WebSocket message")
                fragments.extend(payload)
                fragmented = not final
                if final:
                    protocol.feed(json.loads(fragments))
                    fragments.clear()
        except (EOFError, OSError, ValueError, ProtocolError, KeyError, TypeError):
            return
        finally:
            protocol.close()

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
