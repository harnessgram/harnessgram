"""Terminal-free Claude transport for API-key sessions.

The runner deliberately knows nothing about Telegram.  Its durable public surface is
a private Unix socket and a metadata-only JSONL event stream.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import selectors
import signal
import errno
import hashlib
import socket
import struct
import subprocess
import sys
import time
import uuid as uuidlib
from typing import Callable, Optional

from . import config as C

BACKOFF = (2, 10, 60)
SOCKET_PATH_LIMIT = 104
HEALTHY_UPTIME = 300
# Silence only counts as a stall when nothing legitimate is outstanding (no open tool and no open permission request).
STALL_SECONDS = 15 * 60
CLIENT_DEADLINE = 5.0
CLIENT_MAX_BYTES = 1024 * 1024
MAX_EVENT_LINE = 64 * 1024 * 1024
# Claude Code keeps the first 200 characters of a long project folder name and appends a hash suffix.
PROJECT_NAME_LIMIT = 200
# Darwin <sys/un.h> and <sys/ucred.h>: SOL_LOCAL, LOCAL_PEERCRED and XUCRED_VERSION.
DARWIN_SOL_LOCAL = 0
DARWIN_LOCAL_PEERCRED = 1
XUCRED_VERSION = 0
XUCRED_SIZE = 76
ALLOWED_COMMANDS = {"send", "interrupt", "set_model", "set_mode", "answer", "stop"}
EVENT_TYPES = {"system", "assistant", "user", "result", "rate_limit_event",
               "control_request", "control_response", "control_cancel_request"}
EVENT_SUBTYPES = {"init", "compact_boundary", "status", "success", "error", "interrupt"}
BLOCK_TYPES = {"text", "tool_use", "tool_result", "thinking", "redacted_thinking", "image"}
RATE_LIMIT_STATUSES = {"allowed", "allowed_warning", "rejected"}
CONTROL_SUBTYPES = {"interrupt", "set_model", "set_permission_mode", "can_use_tool"}


class RunnerError(RuntimeError):
    """A safe runner error (its text never contains child output or environment)."""


def runner_dir() -> Path:
    return C.private_dir(C.state_dir() / "runners")


def session_record(session: str) -> Path:
    return runner_dir() / (C.safe_name(session) + ".json")


def load_record(session: str) -> dict:
    data = C.read_map(session_record(session))
    return data if isinstance(data, dict) else {}


def claude_project_folder(cwd: str) -> str:
    """Claude Code's project folder name: every character other than [A-Za-z0-9] becomes '-'.

    The CLI applies the rule to UTF-16 code units, so a character outside the Basic Multilingual Plane
    yields two dashes.
    """
    return "".join(ch if ch.isascii() and ch.isalnum() else ("--" if ord(ch) > 0xFFFF else "-") for ch in cwd)


def _safe_session_id(value: str) -> bool:
    return bool(value) and len(value) <= 128 and value[0].isalnum() and all(
        ch.isascii() and (ch.isalnum() or ch == "-") for ch in value)


def _api_key_session(record: dict) -> bool:
    """Check the declared authentication method, never the key or helper output."""
    return record.get("auth_mode") == "api_key"


def _safe_count(value) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    return 0


def _allow(value, allowed: set[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "other"


def event_summary(event: dict, elapsed_ms: int, *, digest: Callable[[str], str] | None = None) -> dict:
    """Reduce a CLI event to non-content telemetry safe for the append-only log."""
    typ = _allow(event.get("type"), EVENT_TYPES)
    out = {"at": time.time(), "type": typ, "elapsed_ms": max(0, int(elapsed_ms))}
    if typ == "system":
        out["subtype"] = _allow(event.get("subtype"), EVENT_SUBTYPES)
        if event.get("subtype") == "init":
            out["session_id"] = digest(str(event["session_id"])) if digest and event.get("session_id") else bool(event.get("session_id"))
            out["capability_count"] = _safe_count(event.get("capabilities"))
        if event.get("subtype") == "compact_boundary" and isinstance(event.get("duration_ms"), (int, float)):
            out["duration_ms"] = event["duration_ms"]
    elif typ in ("assistant", "user"):
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        out["block_count"] = len(content)
        out["block_types"] = [_allow(block.get("type"), BLOCK_TYPES) for block in content if isinstance(block, dict)]
        replay_id = event.get("uuid") or message.get("uuid")
        if typ == "user" and isinstance(replay_id, str):
            out["uuid"] = digest(replay_id) if digest else True
    elif typ == "result":
        out["subtype"] = _allow(event.get("subtype"), EVENT_SUBTYPES)
        for key in ("num_turns", "duration_ms", "duration_api_ms"):
            if isinstance(event.get(key), (int, float)):
                out[key] = event[key]
    elif typ == "rate_limit_event":
        info = event.get("rate_limit_info") if isinstance(event.get("rate_limit_info"), dict) else {}
        out["status"] = _allow(info.get("status"), RATE_LIMIT_STATUSES)
    elif typ in ("control_request", "control_response", "control_cancel_request"):
        request = event.get("request") if isinstance(event.get("request"), dict) else {}
        out["subtype"] = _allow(request.get("subtype", event.get("subtype")), CONTROL_SUBTYPES)
        if isinstance(event.get("request_id"), str):
            out["request_id"] = digest(event["request_id"]) if digest else True
    return out


class Runner:
    def __init__(self, session: str, record: dict, *, env: Optional[dict] = None,
                 popen: Callable = subprocess.Popen, sleep: Callable = time.sleep,
                 socket_dir: str = ""):
        self.session = session
        self.record = dict(record)
        self.env = dict(os.environ if env is None else env)
        self.popen, self.sleep = popen, sleep
        self.root = runner_dir()
        self.socket_root = (C.private_dir(Path(os.path.realpath(os.path.expanduser(socket_dir))))
                            if socket_dir else self.root)
        stem = hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]
        # macOS limits a Unix socket path to 104 bytes; a short digest keeps the real state path well inside it.
        self.socket_path = self.socket_root / (stem + ".sock")
        self.lock_path = self.root / (stem + ".lock")
        self.events_path = self.root / (stem + ".events.jsonl")
        self.state_root = self.root / stem  # created (0700) on first write, after the socket-path check
        self.delivery_path = self.state_root / "delivery.json"
        self.process_path = self.state_root / "child.json"
        self.child = None
        self.server = None
        self.lock_file = None
        self.claude_session = str(record.get("resume_session_id", ""))
        self.pending = {}
        self._log_key = os.urandom(16)
        self.started = time.monotonic()
        self.last_output = self.started
        self.turn_open = False
        self.stopping = False
        self.completed_turns = 0
        self.child_broken = False
        self.open_tools: set[str] = set()
        self.open_requests: set[str] = set()
        self.owner_uid = os.getuid()
        self.client_deadline = CLIENT_DEADLINE
        self._out_buffer = bytearray()
        self._discarding = False

    def validate(self, headless_enabled: bool) -> None:
        if not headless_enabled:
            raise RunnerError("headless_disabled")
        if self.record.get("runner_mode", "tmux") != "headless":
            raise RunnerError("runner_mode_tmux")
        if not _api_key_session(self.record):
            raise RunnerError("api_key_required")

    def argv(self) -> list[str]:
        argv = [str(self.record.get("claude_binary") or "claude"), "-p", "--input-format", "stream-json",
                "--output-format", "stream-json", "--verbose", "--permission-prompt-tool", "stdio",
                "--replay-user-messages"]
        if self.claude_session:
            argv += ["--resume", self.claude_session]
        for option, key in (("--model", "model"), ("--permission-mode", "permission_mode"),
                            ("--max-budget-usd", "max_budget_usd")):
            value = self.record.get(key)
            if value not in (None, ""):
                argv += [option, str(value)]
        return argv

    def acquire(self) -> None:
        self.lock_file = open(self.lock_path, "a+", encoding="utf-8")
        os.chmod(self.lock_path, 0o600)
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            self.lock_file = None
            raise RunnerError("runner_already_running") from None
        self._load_delivery()
        self._cleanup_recorded_child()

    @staticmethod
    def _atomic_json(path: Path, value: object) -> None:
        C.private_dir(path.parent)
        temporary = path.with_name(path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(value, output, separators=(",", ":"), ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _save_delivery(self) -> None:
        self._atomic_json(self.delivery_path, {"pending": self.pending,
                                                "resume_session_id": self.claude_session})

    def _load_delivery(self) -> None:
        try:
            value = json.loads(self.delivery_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return
        if not isinstance(value, dict):
            return
        pending = value.get("pending")
        if isinstance(pending, dict):
            self.pending = {key: item for key, item in pending.items()
                            if self._canonical_uuid(key) and isinstance(item, dict)}
        if isinstance(value.get("resume_session_id"), str):
            self.claude_session = value["resume_session_id"]

    @staticmethod
    def _canonical_uuid(value: object) -> bool:
        if not isinstance(value, str) or len(value) != 36 or value != value.lower():
            return False
        try:
            return str(uuidlib.UUID(value)) == value
        except (ValueError, AttributeError):
            return False

    @staticmethod
    def _process_identity(pid: int) -> str | None:
        if os.name == "posix" and Path("/proc").is_dir():
            try:
                # The comm field may contain spaces and ')'; fields following its final ')' are stable.
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                return "linux:" + fields[19]
            except (FileNotFoundError, ProcessLookupError, IndexError, OSError):
                return None
        if os.uname().sysname == "Darwin":
            return Runner._darwin_process_identity(pid)
        return None

    @staticmethod
    def _darwin_process_identity(pid: int) -> str | None:
        """Read the microsecond process start time from ``kern.proc.pid``.

        Darwin's kinfo_proc ABI exposes ``p_starttime`` in the leading union of
        its external-process structure.
        """
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)

        class Timeval(ctypes.Structure):
            _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_int)]

        class ProcessStart(ctypes.Union):
            _fields_ = [("p_starttime", Timeval), ("_links", ctypes.c_void_p * 2)]

        class ExternProc(ctypes.Structure):
            _fields_ = [("p_un", ProcessStart)]

        class KinfoProc(ctypes.Structure):
            _fields_ = [("kp_proc", ExternProc)]

        mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
        size = ctypes.c_size_t()
        if libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0) or not size.value:
            return None
        data = (ctypes.c_ubyte * size.value)()
        if libc.sysctl(mib, 4, data, ctypes.byref(size), None, 0):
            return None
        if size.value < ctypes.sizeof(KinfoProc):
            return None
        process = ctypes.cast(data, ctypes.POINTER(KinfoProc)).contents
        seconds = process.kp_proc.p_un.p_starttime.tv_sec
        micros = process.kp_proc.p_un.p_starttime.tv_usec
        return f"darwin:{seconds}:{micros}" if seconds > 0 and 0 <= micros < 1_000_000 else None

    @staticmethod
    def _stop_group(pgid: int, timeout: float, child=None) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if child is not None:
                    child.poll()
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    return
                time.sleep(min(.05, max(0, deadline - time.monotonic())))
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        raise RunnerError("runner_process_group_survived")

    def _cleanup_recorded_child(self) -> None:
        try:
            record = json.loads(self.process_path.read_text(encoding="utf-8"))
            pid, pgid, identity = int(record["pid"]), int(record["pgid"]), record["identity"]
        except (FileNotFoundError, OSError, ValueError, TypeError, KeyError):
            self.process_path.unlink(missing_ok=True)
            return
        # Never signal a numeric pid/group after its recorded leader has been reused.
        if self._process_identity(pid) == identity:
            self._stop_group(pgid, 5)
        self.process_path.unlink(missing_ok=True)

    def _id_digest(self, value: str) -> str:
        return hashlib.blake2s(value.encode(), key=self._log_key, digest_size=8).hexdigest()

    def _transcript_path(self) -> Path | None:
        """Locate the CLI's own transcript for the current session id.

        The computed project folder is tried first; when it does not exist (a long, hash-suffixed name or a
        naming change) the projects root is searched one level deep for ``<session_id>.jsonl``.
        """
        session = self.claude_session
        if not _safe_session_id(session):
            return None
        cwd = os.path.realpath(os.path.expanduser(str(self.record.get("cwd") or Path.home())))
        root = Path(os.path.expanduser(self.env.get("CLAUDE_CONFIG_DIR") or "~/.claude")) / "projects"
        folder = claude_project_folder(cwd)
        name = session + ".jsonl"
        direct = root / folder / name
        if len(folder) <= PROJECT_NAME_LIMIT:
            if direct.is_file():
                return direct
            candidates = []
        else:
            candidates = [path for path in root.glob(folder[:PROJECT_NAME_LIMIT] + "-*/" + name) if path.is_file()]
        if not candidates:
            candidates = [path for path in root.glob("*/" + name) if path.is_file()]
        if not candidates:
            return direct
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def _transcript_uuids(self) -> set[str] | None:
        path = self._transcript_path()
        if path is None:
            return None
        found, skipped = set(), 0
        try:
            with path.open("rb") as transcript:
                for line in transcript:
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        # A torn or corrupt line must not disable reconciliation of every other record.
                        skipped += 1
                        continue
                    if isinstance(item, dict) and item.get("type") == "user" and self._canonical_uuid(item.get("uuid")):
                        found.add(item["uuid"])
        except OSError:
            return None
        if skipped:
            self.append({"at": time.time(), "type": "transcript_skipped_lines", "count": skipped})
        return found

    def _reconcile_pending(self, *, resend: bool) -> None:
        if not self.pending:
            return
        if not self.claude_session:
            # No system/init was ever seen, so no session (and no transcript) exists that could hold these
            # messages; the next child starts a new session and they are resent there.
            delivered: set[str] | None = set()
        else:
            delivered = self._transcript_uuids()
        if delivered is None:
            self.append({"at": time.time(), "type": "transcript_unreadable",
                         "pending_count": len(self.pending)})
            return
        changed = False
        for ident, message in tuple(self.pending.items()):
            if ident in delivered:
                self.pending.pop(ident)
                changed = True
            elif resend:
                try:
                    self.write(message)
                except RunnerError:
                    break
                self.turn_open = True
        if changed:
            self._save_delivery()

    def listen(self) -> None:
        # sockaddr_un.sun_path is 104 bytes on macOS, including its terminating NUL.
        real_socket_path = os.path.realpath(os.path.abspath(self.socket_path))
        if len(os.fsencode(real_socket_path)) + 1 > SOCKET_PATH_LIMIT:
            raise RunnerError("runner_socket_path_too_long")
        if self.socket_path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(.2)
                probe.connect(str(self.socket_path))
            except socket.timeout:
                # A listener that is slow to accept is still alive; never steal its socket.
                raise RunnerError("runner_already_running")
            except (ConnectionRefusedError, FileNotFoundError):
                self.socket_path.unlink(missing_ok=True)
            except OSError as exc:
                # A leftover non-socket file (ENOTSOCK on macOS) is stale too.
                if exc.errno != errno.ENOTSOCK:
                    raise
                self.socket_path.unlink(missing_ok=True)
            else:
                raise RunnerError("runner_already_running")
            finally:
                probe.close()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self.server.listen(8)
        self.server.setblocking(False)

    def spawn(self) -> None:
        child_env = dict(self.env)
        child_env["HARNESSGRAM_RUNNER"] = "1"
        cwd = os.path.realpath(os.path.expanduser(str(self.record.get("cwd") or Path.home())))
        kwargs = {}
        if os.name == "posix" and Path("/proc").is_dir():
            parent_pid = os.getpid()
            def set_parent_death_signal():
                import ctypes
                libc = ctypes.CDLL(None, use_errno=True)
                if libc.prctl(1, signal.SIGKILL) != 0:
                    os._exit(127)
                if os.getppid() != parent_pid:
                    os._exit(127)
            kwargs["preexec_fn"] = set_parent_death_signal
        self.child = self.popen(self.argv(), cwd=cwd, env=child_env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                start_new_session=True, **kwargs)
        identity = self._process_identity(self.child.pid)
        if identity is None:
            self.child.kill()
            self.child.wait()
            raise RunnerError("child_identity_unavailable")
        self._atomic_json(self.process_path, {"pid": self.child.pid, "pgid": os.getpgid(self.child.pid),
                                              "identity": identity})
        self.started = time.monotonic()
        self.last_output = self.started
        self.child_broken = False
        self.turn_open = False
        self.open_tools.clear()
        self.open_requests.clear()
        self._out_buffer.clear()
        self._discarding = False

    def append(self, summary: dict) -> None:
        fd = os.open(self.events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(summary, separators=(",", ":"), ensure_ascii=True) + "\n").encode())
        finally:
            os.close(fd)

    def consume(self, line: str | bytes) -> None:
        self.last_output = time.monotonic()
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            self.append({"at": time.time(), "type": "protocol_error"})
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init" and isinstance(event.get("session_id"), str):
            self.claude_session = event["session_id"]
            self._save_delivery()
        self._track_open_work(event)
        if kind == "result":
            self.turn_open = False
            self.completed_turns += 1
        self.append(event_summary(event, (time.monotonic() - self.started) * 1000,
                                  digest=self._id_digest))

    @staticmethod
    def _blocks(event: dict) -> list:
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = message.get("content")
        return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []

    def _track_open_work(self, event: dict) -> None:
        """Track tool calls and CLI permission requests that legitimately keep a turn silent."""
        kind = event.get("type")
        if kind == "assistant":
            for block in self._blocks(event):
                if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                    self.open_tools.add(block["id"])
        elif kind == "user":
            for block in self._blocks(event):
                if block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                    self.open_tools.discard(block["tool_use_id"])
        elif kind == "control_request" and isinstance(event.get("request_id"), str):
            self.open_requests.add(event["request_id"])
        elif kind == "control_cancel_request" and isinstance(event.get("request_id"), str):
            self.open_requests.discard(event["request_id"])
        elif kind == "result":
            self.open_tools.clear()
            self.open_requests.clear()

    def stalled(self, now: float | None = None) -> bool:
        if not self.turn_open or self.open_tools or self.open_requests:
            return False
        return (time.monotonic() if now is None else now) - self.last_output >= STALL_SECONDS

    def write(self, payload: dict) -> None:
        if not self.child or not self.child.stdin or self.child_broken:
            raise RunnerError("child_unavailable")
        try:
            self.child.stdin.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n")
            self.child.stdin.flush()
        except (OSError, ValueError):
            # A closed or broken stdin means the child has exited; the run loop restarts it.
            self.child_broken = True
            raise RunnerError("child_unavailable") from None

    def read_child(self) -> bool:
        """Read the child's stdout fd without blocking and consume every complete line.

        Returns False at end of file.  A buffered ``readline`` is never used under the selector: it can pull
        several lines into a hidden buffer that the selector cannot see.
        """
        fd = self.child.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except (BlockingIOError, InterruptedError):
                return True
            except OSError:
                return False
            if not chunk:
                return False
            self._feed(chunk)

    def _feed(self, chunk: bytes) -> None:
        self._out_buffer += chunk
        while True:
            end = self._out_buffer.find(b"\n")
            if end < 0:
                break
            line = bytes(self._out_buffer[:end])
            del self._out_buffer[:end + 1]
            if self._discarding:
                self._discarding = False
                continue
            if line.strip():
                self.consume(line)
        if len(self._out_buffer) > MAX_EVENT_LINE:
            self._out_buffer.clear()
            self._discarding = True
            self.append({"at": time.time(), "type": "protocol_error", "reason": "line_too_long"})

    def drain_child(self) -> None:
        """After the child has exited, consume what remains in the pipe, including a final unterminated line."""
        if not self.child or not self.child.stdout or self.child.stdout.closed:
            return
        os.set_blocking(self.child.stdout.fileno(), False)
        if self.read_child() and self.child.poll() is None:
            return  # still running (or a grandchild holds the pipe): a partial line may yet be completed
        rest = bytes(self._out_buffer)
        self._out_buffer.clear()
        if rest.strip() and not self._discarding:
            self.consume(rest)
        self._discarding = False

    def command(self, command: dict) -> dict:
        kind = command.get("command") or command.get("type")
        if kind not in ALLOWED_COMMANDS:
            raise RunnerError("unknown_command")
        if kind == "send":
            ident = command.get("uuid")
            if not self._canonical_uuid(ident):
                raise RunnerError("invalid_uuid")
            delivered = self._transcript_uuids()
            if delivered is not None and ident in delivered:
                if ident in self.pending:
                    self.pending.pop(ident)
                    self._save_delivery()
                return {"accepted": True, "uuid": ident, "state": "delivered"}
            if ident in self.pending:
                return {"accepted": True, "uuid": ident, "state": "pending"}
            content = command.get("content")
            if not isinstance(content, (str, list)):
                raise RunnerError("invalid_message")
            wire = {"type": "user", "uuid": ident, "message": {"role": "user", "content": content},
                    "parent_tool_use_id": None}
            self.pending[ident] = wire
            self._save_delivery()
            try:
                self.write(wire)
            except RunnerError:
                # Stored durably; the restarted child receives it during reconciliation.
                return {"accepted": True, "uuid": ident, "state": "pending"}
            self.turn_open = True
            return {"accepted": True, "uuid": ident}
        if kind == "interrupt":
            request = {"subtype": "interrupt"}
        elif kind == "set_model":
            request = {"subtype": "set_model", "model": str(command.get("model", ""))}
        elif kind == "set_mode":
            request = {"subtype": "set_permission_mode", "mode": str(command.get("mode", ""))}
        elif kind == "answer":
            response = command.get("response")
            if not isinstance(response, dict):
                raise RunnerError("invalid_answer")
            request_id = str(command.get("request_id", ""))
            self.write({"type": "control_response", "request_id": request_id, "response": response})
            self.open_requests.discard(request_id)
            return {"accepted": True}
        else:
            self.stopping = True
            self.interrupt_child()
            return {"accepted": True}
        self.write({"type": "control_request", "request_id": str(uuidlib.uuid4()), "request": request})
        return {"accepted": True}

    def interrupt_child(self) -> None:
        if self.child and self.child.poll() is None:
            try:
                os.killpg(self.child.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    def stop_child(self, timeout: float = 5) -> None:
        """Escalate against the entire session and reap its leader."""
        child = self.child
        if not child:
            return
        pgid = child.pid
        self._stop_group(pgid, timeout, child)
        # wait() is required even when poll() has already observed termination.
        child.wait()
        self.process_path.unlink(missing_ok=True)
        for stream in (child.stdin, child.stdout):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        raise RunnerError("runner_process_group_survived")

    @staticmethod
    def _peer_uid(conn: socket.socket) -> int:
        """Return the connected peer's effective uid from the kernel (never from client data)."""
        if sys.platform == "darwin":
            level = getattr(socket, "SOL_LOCAL", DARWIN_SOL_LOCAL)
            option = getattr(socket, "LOCAL_PEERCRED", DARWIN_LOCAL_PEERCRED)
            raw = conn.getsockopt(level, option, XUCRED_SIZE)
            if len(raw) < 8:
                raise RunnerError("runner_peer_credentials_unavailable")
            version, uid = struct.unpack_from("=II", raw, 0)
            if version != XUCRED_VERSION:
                raise RunnerError("runner_peer_credentials_unavailable")
            return uid
        if hasattr(socket, "SO_PEERCRED"):
            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII"))
            _pid, uid, _gid = struct.unpack("iII", raw)
            return uid
        raise RunnerError("runner_peer_credentials_unavailable")

    def _client(self, conn: socket.socket) -> None:
        deadline = time.monotonic() + self.client_deadline
        raw = b""
        while b"\n" not in raw and len(raw) <= CLIENT_MAX_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("client_deadline")
            conn.settimeout(remaining)
            part = conn.recv(65536)
            if not part:
                break
            raw += part
        try:
            data = json.loads(raw.split(b"\n", 1)[0])
            if not isinstance(data, dict):
                raise ValueError
            reply = self.command(data)
        except (ValueError, UnicodeError, RunnerError) as exc:
            reply = {"accepted": False, "error": str(exc) if isinstance(exc, RunnerError) else "invalid_json"}
        conn.settimeout(max(.1, deadline - time.monotonic()))
        conn.sendall((json.dumps(reply, separators=(",", ":")) + "\n").encode())

    def _accept_client(self) -> None:
        """Serve one connection; any failure closes that connection only, never the runner."""
        try:
            conn, _ = self.server.accept()
        except (BlockingIOError, InterruptedError, ConnectionAbortedError):
            return
        with conn:
            try:
                if self._peer_uid(conn) != self.owner_uid:
                    self.append({"at": time.time(), "type": "client_rejected", "reason": "peer_uid"})
                    return
            except (OSError, RunnerError, struct.error):
                self.append({"at": time.time(), "type": "client_rejected", "reason": "peer_unverified"})
                return
            try:
                self._client(conn)
            except OSError as exc:
                reason = "timeout" if isinstance(exc, socket.timeout) else "io"
                self.append({"at": time.time(), "type": "client_error", "reason": reason})

    def run(self) -> int:
        self.acquire()
        failures = 0
        try:
            self.listen()
            while not self.stopping:
                self.spawn()
                if self.stopping:
                    break
                self._reconcile_pending(resend=True)
                stdout_fd = self.child.stdout.fileno()
                os.set_blocking(stdout_fd, False)
                selector = selectors.DefaultSelector()
                selector.register(self.server, selectors.EVENT_READ, "server")
                selector.register(stdout_fd, selectors.EVENT_READ, "stdout")
                stalled = False
                while not self.stopping and not self.child_broken and self.child.poll() is None:
                    for key, _ in selector.select(timeout=1):
                        if key.data == "server":
                            self._accept_client()
                        elif not self.read_child():
                            selector.unregister(stdout_fd)
                    if self.stalled():
                        self.append({"at": time.time(), "type": "stall", "elapsed_ms": STALL_SECONDS * 1000})
                        stalled = True
                        break
                selector.close()
                if not stalled:
                    self.drain_child()
                if self.stopping:
                    break
                self.stop_child()
                if self.completed_turns or time.monotonic() - self.started >= HEALTHY_UPTIME:
                    failures = 0
                    self.completed_turns = 0
                else:
                    failures += 1
                self.append({"at": time.time(), "type": "restart", "attempt": failures})
                self.sleep(BACKOFF[min(max(failures, 1) - 1, len(BACKOFF) - 1)])
            return 0
        finally:
            self.stop_child()
            if self.server:
                self.server.close()
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass
            if self.lock_file:
                self.lock_file.close()


def cli(session: str, *, dry_run: bool = False) -> int:
    """CLI entry point.  Only declarations are inspected; key material is never read."""
    try:
        cfg = C.load()
        runner = Runner(session, load_record(session), socket_dir=cfg.runner_socket_dir)
        runner.validate(cfg.headless_enabled)
        if dry_run:
            return 0
        return runner.run()
    except (C.ConfigError, OSError, RunnerError) as exc:
        # Stable codes are intentionally non-secret and suitable for supervisors.
        print("HarnessGram runner: " + (str(exc) if isinstance(exc, RunnerError) else "configuration_error"),
              file=os.sys.stderr)
        return 1
