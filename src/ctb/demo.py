"""Isolated, in-memory App Store review bridge.

This module intentionally imports neither the bridge configuration nor any integration
module.  It is safe to run in a container which has no home directory or credentials.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
import copy
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import socket
import threading
import time
from typing import Optional

MAX_BODY = 4096
SAFE_POSTS = {"/api/state", "/api/decide", "/api/widget-state", "/api/widget-decide",
              "/api/radio-player", "/api/live-activity-token"}
PAIR_POSTS = {"/api/demo-pair", "/api/device-pair", "/api/pair"}
DEFAULT_TOKEN_WINDOW = 30 * 24 * 60 * 60
PAIR_LIMIT = 5
GLOBAL_PAIR_LIMIT = 60
PAIR_PERIOD = 60
MAX_THROTTLE_CLIENTS = 4096
TOKEN_CLOCK_SKEW = 60


def sample_state(now: float) -> dict:
    """Return the canonical fixture.  Timestamps are relative, while content is fixed."""
    return {
        "ok": True, "demo": True, "now": int(now),
        "sessions": [
            {"id": "demo-web", "name": "Storefront refresh", "state": "working",
             "now": "Running accessibility checks", "model": "Sonnet", "account": "Demo"},
            {"id": "demo-api", "name": "Checkout API", "state": "waiting",
             "now": "Waiting for your decision", "model": "Codex", "account": "Demo"},
            {"id": "demo-doc", "name": "Release notes", "state": "idle",
             "now": "Ready for another instruction", "model": "Sonnet", "account": "Demo"},
        ],
        "decisions": [
            {"id": "demo-decision-1", "title": "Publish the preview build?",
             "detail": "All demo checks passed. This only changes the temporary review display.",
             "yes": "Publish preview", "no": "Keep testing", "topic": "Checkout API",
             "age": 75, "stale": "", "sensitive": False},
            {"id": "demo-sensitive-1", "title": "Use a production credential?",
             "detail": "Sensitive actions are unavailable in the review demo.",
             "yes": "Use credential", "no": "Cancel", "topic": "Storefront refresh",
             "age": 180, "stale": "", "sensitive": True},
        ],
        "quota": [
            {"name": "Demo workspace", "used": 37, "remaining": 63, "reset": "in 2h 18m"},
            {"name": "Review allowance", "used": 12, "remaining": 88, "reset": "tomorrow"},
        ],
        "cards": [{"id": "demo-card-1", "kind": "progress", "title": "Storefront refresh",
                   "topic": "Storefront refresh", "percent": 68, "steps": ["Layout", "VoiceOver", "Snapshot"],
                   "at": 1, "updated": int(now) - 12, "finished": False}],
        "radio": {"on": True, "title": "Harnessgram Review Radio", "now": "Demo team status briefing",
                  "listeners": 1},
        "reminders": [{"text": "Review the preview build", "topic": "Checkout API", "age": 240}],
    }


class DemoStore:
    """Process-local state that returns to its fixture at the next hour boundary."""
    def __init__(self, code: str, secret: str, clock=time.time,
                 token_window: int = DEFAULT_TOKEN_WINDOW):
        if not isinstance(code, str) or len(code) < 12:
            raise ValueError("demo code must contain at least 12 characters")
        if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32 or code == secret:
            raise ValueError("an independent signing secret of at least 32 bytes is required")
        if type(token_window) is not int or token_window < 1:
            raise ValueError("token window must be a positive number of seconds")
        self.code, self.secret, self.clock = code, secret, clock
        self.token_window = token_window
        self.bucket = -1
        self.state = {}
        self._pair_attempts = OrderedDict()
        self._global_pair_attempts = []
        self._lock = threading.RLock()

    def current(self) -> dict:
        with self._lock:
            now = self.clock()
            bucket = int(now // 3600)
            if bucket != self.bucket:
                self.bucket, self.state = bucket, sample_state(now)
            self.state["now"] = int(now)
            return copy.deepcopy(self.state)

    def _token(self, issued: int) -> str:
        message = self.code.encode("utf-8") + b"|" + str(issued).encode("ascii")
        digest = hmac.new(self.secret.encode("utf-8"), message, hashlib.sha256).digest()
        signature = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return f"hgw_{issued}.{signature}"

    def pair(self, code: object, client: str = "local") -> Optional[str]:
        return self.pair_with_status(code, client)[0]

    def pair_with_status(self, code: object, client: str = "local") -> tuple[Optional[str], bool]:
        with self._lock:
            now = self.clock()
            self._global_pair_attempts = [
                stamp for stamp in self._global_pair_attempts if now - stamp < PAIR_PERIOD]
            attempts = self._pair_attempts.pop(client, [])
            attempts = [stamp for stamp in attempts if now - stamp < PAIR_PERIOD]
            if (len(attempts) >= PAIR_LIMIT or
                    len(self._global_pair_attempts) >= GLOBAL_PAIR_LIMIT):
                self._pair_attempts[client] = attempts
                return None, True
            attempts.append(now)
            self._global_pair_attempts.append(now)
            self._pair_attempts[client] = attempts
            while len(self._pair_attempts) > MAX_THROTTLE_CLIENTS:
                self._pair_attempts.popitem(last=False)
            if not isinstance(code, str) or not hmac.compare_digest(
                    code.encode("utf-8"), self.code.encode("utf-8")):
                return None, False
            return self._token(int(now)), False

    def authorized(self, header: str) -> bool:
        if not isinstance(header, str):
            return False
        with self._lock:
            if not header.startswith("Bearer hgw_"):
                return False
            token = header[len("Bearer "):]
            try:
                issued_text, _signature = token[4:].split(".", 1)
                issued = int(issued_text)
            except (ValueError, TypeError):
                return False
            now = self.clock()
            if issued < 0 or issued > now + TOKEN_CLOCK_SKEW or now - issued > self.token_window:
                return False
            expected = self._token(issued)
            return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))

    def decide(self, ident: object, choice: object) -> tuple[int, dict]:
        with self._lock:
            rows = self.current()["decisions"]
            row = next((item for item in rows if item["id"] == ident), None)
            if row is None:
                return 404, {"error": "not found"}
            if row.get("sensitive"):
                return 403, {"error": "sensitive actions are disabled in demo mode"}
            if choice not in ("yes", "no"):
                return 400, {"error": "bad request"}
            self.state["decisions"] = [item for item in self.state["decisions"] if item["id"] != ident]
            self.state["last_decision"] = {"id": ident, "choice": choice}
            return 200, {"ok": True, "demo": True}


class DemoHTTPServer(http.server.ThreadingHTTPServer):
    """Threaded server with a hard bound on live client connections."""
    daemon_threads = True

    def __init__(self, server_address, handler_class, max_connections: int = 64):
        self._connections = threading.BoundedSemaphore(max_connections)
        super().__init__(server_address, handler_class)

    def process_request(self, request: socket.socket, client_address) -> None:
        if not self._connections.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n"
                                b"Content-Length: 0\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()


def handler_for(store: DemoStore, trusted_proxies=()):
    trusted_proxies = frozenset(trusted_proxies)

    class Handler(http.server.BaseHTTPRequestHandler):
        timeout = 10
        server_version = "DemoServer"
        sys_version = ""

        def _json(self, status: int, data: dict) -> None:
            body = json.dumps(data, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Optional[dict]:
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > MAX_BODY:
                    return None
                value = json.loads(self.rfile.read(size) or b"{}")
                return value if isinstance(value, dict) else None
            except (ValueError, RecursionError):
                return None

        def do_GET(self):
            if self.path == "/healthz":
                self._json(200, {"ok": True, "demo": True})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            body = self._body()
            if body is None:
                self._json(400, {"error": "bad request"}); return
            if self.path in PAIR_POSTS:
                client = self.client_address[0]
                if client in trusted_proxies:
                    for forwarded in reversed(
                            self.headers.get("X-Forwarded-For", "").split(",")):
                        try:
                            candidate = str(ipaddress.ip_address(forwarded.strip()))
                        except ValueError:
                            continue
                        if candidate not in trusted_proxies:
                            client = candidate
                            break
                token, throttled = store.pair_with_status(body.get("code"), client)
                if throttled:
                    self._json(429, {"error": "too many requests"})
                elif token:
                    self._json(200, {"ok": True, "token": token, "demo": True})
                else:
                    self._json(403, {"error": "forbidden"})
                return
            if self.path not in SAFE_POSTS:
                self._json(403, {"error": "action unavailable in demo mode"}); return
            if not store.authorized(self.headers.get("Authorization", "")):
                self._json(403, {"error": "forbidden"}); return
            if self.path in ("/api/state", "/api/widget-state"):
                self._json(200, store.current()); return
            if self.path in ("/api/decide", "/api/widget-decide"):
                status, result = store.decide(body.get("id"), body.get("choice")); self._json(status, result); return
            if self.path == "/api/radio-player":
                self._json(200, {**store.current()["radio"], "demo": True, "stream_url": None}); return
            self._json(200, {"ok": True, "demo": True, "registered": False})

        def log_message(self, _format, *_args):
            return
    return Handler


def serve(host: str, port: int, code: Optional[str] = None, secret: Optional[str] = None) -> None:
    code = code or os.environ.get("CTB_DEMO_CODE", "")
    secret = secret or os.environ.get("CTB_DEMO_SIGNING_SECRET", "")
    try:
        token_window = int(os.environ.get("CTB_DEMO_TOKEN_WINDOW_SECONDS", str(DEFAULT_TOKEN_WINDOW)))
    except ValueError as error:
        raise ValueError("CTB_DEMO_TOKEN_WINDOW_SECONDS must be an integer") from error
    store = DemoStore(code, secret, token_window=token_window)
    trusted_proxies = {
        item.strip() for item in os.environ.get("CTB_DEMO_TRUSTED_PROXIES", "").split(",")
        if item.strip()
    }
    DemoHTTPServer((host, port), handler_for(store, trusted_proxies)).serve_forever()
