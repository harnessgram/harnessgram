"""Minimal Telegram Bot API client (standard library only).

Errors never carry the request URL (it contains the bot token) or the raw
response body: only Telegram's own short `description` text.
"""
from __future__ import annotations

import json
import http.client
import os
import re
import secrets
import socket
import stat
import urllib.parse
import time
import urllib.error
import urllib.request
from typing import Optional

DEFAULT_API = "https://api.telegram.org"
API = DEFAULT_API  # the base a Bot uses unless it was given its own (config api_base)
DOWNLOAD_LIMIT = 20 * 1024 * 1024  # cloud Bot API getFile limit
CLOUD_UPLOAD_LIMIT = 50 * 1024 * 1024  # cloud Bot API upload limit
LOCAL_LIMIT = 2000 * 1024 * 1024  # a self-hosted `telegram-bot-api --local` server, both directions
COPY_CHUNK = 1024 * 1024


def download_limit(local: bool = False) -> int:
    return LOCAL_LIMIT if local else DOWNLOAD_LIMIT


def upload_limit(local: bool = False) -> int:
    return LOCAL_LIMIT if local else CLOUD_UPLOAD_LIMIT


def valid_api_base(url: str) -> bool:
    """https anywhere, plain http only to this machine (the token is part of every URL)."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return False
    if not host or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and host in ("localhost", "127.0.0.1", "::1"))


def _connect(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, **kwargs):
    """Connect IPv4 first, with a short connect timeout and the original read timeout."""
    host, port = address
    connect_timeout = min(timeout, 4) if isinstance(timeout, (int, float)) else 4
    error = None
    for family, socktype, proto, _, sockaddr in sorted(
            socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM), key=lambda item: item[0] != socket.AF_INET):
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(connect_timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            sock.settimeout(timeout if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT else None)
            return sock
        except OSError as exc:
            error = exc
            if sock is not None:
                sock.close()
    if error:
        raise error
    raise OSError("no addresses")


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect


class _HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        # No check_hostname here: Python 3.12 removed both HTTPSHandler._check_hostname and the
        # keyword of HTTPSConnection (the context already carries the setting). Passing it made every
        # HTTPS call fail with "network error" on 3.12 and newer.
        return self.do_open(_HTTPSConnection, req, context=self._context)


_OPENER = urllib.request.build_opener(_HTTPHandler(), _HTTPSHandler())
# A plain-http base is this machine's own server (see valid_api_base): never through a proxy,
# the bot token is part of every URL.
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPHandler(), _HTTPSHandler())


def _opener_for(url: str):
    return _LOCAL_OPENER if url.startswith("http://") else _OPENER


class TelegramError(Exception):
    def __init__(self, description: str, code: Optional[int] = None, retry_after: Optional[float] = None, *, api_rejected: bool = False):
        super().__init__(description)
        self.api_rejected = api_rejected
        self.description = description
        self.code = code
        self.retry_after = retry_after  # 429's parameters.retry_after, when Telegram sent it


class FloodSkipped(TelegramError):
    """An optional call that was not made (flood gate closed or budget spent); see flood.py.
    Callers treat it like any failed send and retry on their next tick."""

    def __init__(self, description: str = "flood gate closed", retry_after: float = 0.0):
        super().__init__(description, 429, retry_after)
        self.skipped = True


def _timed_out(exc: BaseException) -> bool:
    """A socket timeout, also when URLError wraps it in .reason (a refused connection is not one)."""
    cause = getattr(exc, "reason", None)
    return isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(cause, (TimeoutError, socket.timeout))


TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")


def full_width(params: Optional[dict]) -> Optional[dict]:
    # one button per row so labels are never cut on the phone;
    # only rows of tiny labels (digits, yes/no) stay side by side.
    if not isinstance(params, dict):
        return params
    markup = params.get("reply_markup")
    rows = markup.get("inline_keyboard") if isinstance(markup, dict) else None
    if not isinstance(rows, list):
        return params
    flat = []
    for row in rows:
        if isinstance(row, list) and len(row) > 1 and any(len(str((b or {}).get("text", ""))) > 6 for b in row):
            flat.extend([b] for b in row)
        else:
            flat.append(row)
    return dict(params, reply_markup=dict(markup, inline_keyboard=flat))


def clean_token(token: str) -> str:
    """Strip whitespace and check the shape of a bot token. The message never contains the value:
    a malformed token ends up inside URLs and would leak through exception text."""
    token = (token or "").strip()
    if not TOKEN_RE.match(token):
        raise TelegramError("bot token has an invalid format (expected 123456:ABC...)")
    return token


MAX_FLOOD_WAIT = 5.0


def _short_wait(retry_after) -> float:
    """Seconds to wait before retrying a 429, or 0 to fail at once. A longer pause is never slept
    through: a sleeping send blocked the listener's single loop for 61 s per message while all
    topics shared one group's flood limit; the caller drops or retries later."""
    try:
        wait = float(retry_after) if retry_after is not None else 3.0
    except (ValueError, TypeError):
        wait = 3.0
    return max(wait, 1.0) if wait <= MAX_FLOOD_WAIT else 0.0


# Requests that send nothing but ids (polling, a file the owner sent, lookups): the mirroring backstop lets them
# through; every other request carries content and is refused while mirroring is off.
READ_ONLY = frozenset({"getUpdates", "getMe", "getFile", "getChat", "getChatMember", "getChatMemberCount",
                       "getChatAdministrators", "getForumTopicIconStickers"})


class Bot:
    def __init__(self, token: Optional[str] = None, base: Optional[str] = None, local: bool = False,
                 files_dir: str = ""):
        self._token = clean_token(token) if token else None
        self._base = (base or "").rstrip("/") or None  # None = the module-level API
        self.local = bool(local)  # a `telegram-bot-api --local` server: 2000 MB, files on disk
        self.files_dir = files_dir or ""  # where that server keeps its files (the only place we read)

    @classmethod
    def from_config(cls, cfg, strict: bool = False) -> Bot:
        """strict: the token must be readable (errors raise); otherwise no token = send via the relay."""
        from .creds import get_token
        token = cfg.token() if strict else get_token(cfg)
        base = None if cfg.api_base == DEFAULT_API else cfg.api_base
        return cls(token, base, cfg.api_local, cfg.api_files_dir)

    @property
    def api(self) -> str:
        return self._base or API

    @property
    def download_limit(self) -> int:
        return download_limit(self.local)

    @property
    def upload_limit(self) -> int:
        return upload_limit(self.local)

    def __repr__(self) -> str:
        return "Bot(<hidden>)"

    def _request(self, url: str, data: bytes, headers: dict, timeout: float):
        # Everything that can carry the URL (Request, urlopen, InvalidURL, ...) is inside this
        # try, and any failure becomes a generic error raised `from None`: a traceback must never
        # contain the token.
        from . import flood
        wait_ok = not flood.optional_active()  # optional traffic never sleeps through a 429
        method = url.rsplit("/", 1)[-1]
        for attempt in range(3):
            if method not in READ_ONLY:  # the backstop (policy.py): nothing with content goes out while off
                from . import policy
                if not policy.on():
                    raise TelegramError("not sent: mirroring is off", 403)
            try:
                req = urllib.request.Request(url, data=data, headers=headers)
                with _opener_for(url).open(req, timeout=timeout) as r:
                    body = json.loads(r.read(4 * 1024 * 1024))
            except urllib.error.HTTPError as e:
                # An HTTP answer means Telegram decided: nothing was sent.
                code, description, retry_after, api_rejected = e.code, "request rejected", None, False
                try:
                    payload = json.loads(e.read(65536))
                    if isinstance(payload, dict):
                        api_rejected = payload.get("ok") is False and payload.get("error_code") == e.code
                        description = str(payload.get("description") or description)
                        raw_after = (payload.get("parameters") or {}).get("retry_after")
                        if isinstance(raw_after, (int, float)) and raw_after > 0:
                            retry_after = float(raw_after)
                except Exception:
                    pass
                if code == 429 and wait_ok and attempt < 2 and _short_wait(retry_after):  # brief pause: wait, retry
                    time.sleep(_short_wait(retry_after))
                    continue
                gone = "thread not found" in description.lower()
                desc = "request rejected: thread not found" if (gone and description == "request rejected") else description
                if gone and "thread not found" not in desc.lower():
                    desc = f"{desc}: thread not found"
                raise TelegramError(desc, code, retry_after, api_rejected=api_rejected) from None
            except Exception as e:
                if _timed_out(e):
                    raise TelegramError("timeout") from None
                raise TelegramError("network error") from None
            if not isinstance(body, dict) or not body.get("ok"):
                body = body if isinstance(body, dict) else {}
                code = body.get("error_code")
                retry_after = (body.get("parameters") or {}).get("retry_after")
                if code == 429 and wait_ok and attempt < 2 and _short_wait(retry_after):
                    time.sleep(_short_wait(retry_after))
                    continue
                description = str(body.get("description") or "request rejected")
                gone = "thread not found" in description.lower()
                desc = "request rejected: thread not found" if (gone and description == "request rejected") else description
                if gone and "thread not found" not in desc.lower():
                    desc = f"{desc}: thread not found"
                raise TelegramError(desc, code, retry_after, api_rejected=body.get("ok") is False and isinstance(code, int))
            return body["result"]
        raise TelegramError("request rejected", 429, None)

    def _guarded(self, chat, url: str, data: bytes, headers: dict, timeout: float):
        """_request, closing the shared flood gate (flood.py) when Telegram still says 429."""
        try:
            return self._request(url, data, headers, timeout)
        except TelegramError as exc:
            if exc.code == 429 and not getattr(exc, "skipped", False):
                try:
                    from . import flood
                    flood.record_429(chat, exc.retry_after)
                except Exception:
                    pass  # never replace the real 429 with a gate bookkeeping error
            raise

    def call(self, method: str, params: Optional[dict] = None, timeout: float = 15):
        params = full_width(params)
        if self.local:
            reject_file_uris(params)
        if not self._token:
            from .creds import relay_call
            try:
                return relay_call({"kind": "api", "method": method, "params": params, "timeout": timeout}, timeout)
            except TelegramError as exc:
                if exc.code == 429:  # the listener's relay saw it too; this keeps our own view in step
                    try:
                        from . import flood
                        flood.record_429((params or {}).get("chat_id"), exc.retry_after)
                    except Exception:
                        pass
                raise
        return self._guarded((params or {}).get("chat_id"), f"{self.api}/bot{self._token}/{method}",
                             json.dumps(params or {}).encode(), {"Content-Type": "application/json"}, timeout)

    def upload(self, method: str, fields: dict, file_field: str, filename: str, payload: bytes,
               timeout: float = 120):
        """multipart/form-data POST with one file (sendDocument / sendPhoto)."""
        if self.local:
            reject_file_uris(fields)
        if not self._token:
            import base64
            from .creds import relay_call
            return relay_call({
                "kind": "upload",
                "method": method,
                "params": fields,
                "file_field": file_field,
                "filename": filename,
                "payload": base64.b64encode(payload).decode("ascii"),
                "timeout": timeout,
            }, timeout)
        boundary = "ctb" + secrets.token_hex(16)
        parts = self._form_fields(boundary, fields)
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename) or "file"
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{safe}"\r\n'
                      "Content-Type: application/octet-stream\r\n\r\n").encode())
        parts.append(payload)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        return self._guarded(fields.get("chat_id"), f"{self.api}/bot{self._token}/{method}", b"".join(parts),
                             {"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout)

    @staticmethod
    def _form_fields(boundary: str, fields: dict) -> list:
        return [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
                for name, value in fields.items()]

    @property
    def direct(self) -> bool:
        """True when this Bot holds the token itself (a session without Keychain access sends via the relay)."""
        return bool(self._token)

    def upload_path(self, method: str, fields: dict, file_field: str, path: str, timeout: float = 1800):
        """Local server only: the file is named by a file:// URI and read from disk by the server,
        so a file of up to 2000 MB never sits in memory here. `path` must be a file this process
        controls (mirror stages a private copy): the server re-opens it by name. Never relayed."""
        if not self.local:
            raise TelegramError("sending a file by path needs a local Bot API server")
        if not self._token:
            raise TelegramError("sending a file by path needs the bot token (the relay carries bytes only)")
        reject_file_uris(fields)
        check_regular_file(path, self.upload_limit)
        boundary = "ctb" + secrets.token_hex(16)
        parts = self._form_fields(boundary, dict(fields, **{file_field: file_uri(path)}))
        parts.append(f"--{boundary}--\r\n".encode())
        return self._guarded(fields.get("chat_id"), f"{self.api}/bot{self._token}/{method}", b"".join(parts),
                             {"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout)

    def download_to(self, file_id: str, out, limit: Optional[int] = None) -> int:
        """Stream a file into the binary file object `out`; returns the byte count. On a local server
        getFile answers with an absolute path that is copied from disk (inside files_dir only)."""
        limit = limit or self.download_limit
        info = self.call("getFile", {"file_id": file_id})
        file_path = str((info or {}).get("file_path") or "")
        if self.local and os.path.isabs(file_path):
            return self._copy_local(file_path, out, limit)
        url = f"{self.api}/file/bot{self._token}/{file_path}"
        try:
            with _opener_for(url).open(url, timeout=60) as r:
                return copy_limited(r, out, limit)
        except TelegramError:
            raise
        except Exception:
            raise TelegramError("download failed") from None

    def _copy_local(self, file_path: str, out, limit: int) -> int:
        real = inside_dir(file_path, self.files_dir)
        if real is None:
            raise TelegramError("local file is outside api_files_dir (or api_files_dir is not set)")
        try:
            f, _ = open_regular_file(real, limit)
            with f:
                return copy_limited(f, out, limit)
        except TelegramError:
            raise
        except OSError:
            raise TelegramError("download failed") from None

    def download(self, file_id: str, limit: Optional[int] = None) -> bytes:
        import io
        buf = io.BytesIO()
        self.download_to(file_id, buf, limit)
        return buf.getvalue()


def file_uri(path: str) -> str:
    """The file:// value a local Bot API server accepts in place of an upload."""
    if not os.path.isabs(path) or any(ord(c) < 32 for c in path):
        raise TelegramError("invalid file path")
    return "file://" + urllib.parse.quote(path, safe="/")


TEXT_KEYS = ("text", "caption")  # message text is never a file reference (and may begin with "File:")


def reject_file_uris(value, key=None) -> None:
    """A local Bot API server reads any file named by a file: value, so no caller-supplied value
    (however nested, in any letter case) may start with it; only upload_path adds one itself."""
    if isinstance(value, str):
        if key not in TEXT_KEYS and value.lstrip().lower().startswith("file:"):
            raise TelegramError("file_uri_not_allowed")
    elif isinstance(value, dict):
        for k, item in value.items():
            reject_file_uris(k)
            reject_file_uris(item, k)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_file_uris(item, key)


def inside_dir(path: str, root: str) -> Optional[str]:
    """The real path of `path` when it lies inside `root` (symlinks resolved on both), else None."""
    if not root or not os.path.isabs(root) or not os.path.isabs(path):
        return None
    base, real = os.path.realpath(root), os.path.realpath(path)
    if base == os.sep or real == base or not real.startswith(base + os.sep):
        return None
    return real


def copy_limited(src, out, limit: int) -> int:
    """Copy in chunks; more than `limit` bytes is an error (never the whole file in memory)."""
    total = 0
    while chunk := src.read(min(COPY_CHUNK, limit + 1 - total)):
        total += len(chunk)
        if total > limit:
            raise TelegramError("file too large")
        out.write(chunk)
    return total


def open_regular_file(path: str, limit: int):
    """Open a regular, non-empty file of at most `limit` bytes without following a symlink swapped in
    at the last moment. Returns (file, size)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    f = os.fdopen(fd, "rb")
    st = os.fstat(f.fileno())
    if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= limit:
        f.close()
        raise TelegramError("file is empty, not regular, or too large")
    return f, st.st_size


def check_regular_file(path: str, limit: int) -> int:
    f, size = open_regular_file(path, limit)
    f.close()
    return size


def read_regular_file(path: str, limit: int, stat_out: Optional[list] = None) -> bytes:
    """Read a regular file without following a symlink swapped in at the last moment. `stat_out`, when
    given, gets the fstat of the file that was actually opened (to compare with an earlier check)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= limit:
            raise TelegramError("file is empty, not regular, or too large")
        if stat_out is not None:
            stat_out.append(st)
        return f.read(limit + 1)[:limit]
