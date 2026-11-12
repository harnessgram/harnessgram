"""Loopback-only file server for explicitly configured large-file sharing."""
from __future__ import annotations

import http.server
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import state_dir
from .mirror import SHARE_DAYS


class ShareHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        from . import policy
        if not policy.on():  # mirroring is off: nothing is served
            self.send_error(503)
            return
        parts = [unquote(p) for p in urlsplit(self.path).path.strip("/").split("/")]
        if len(parts) != 2 or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[0]) or not re.fullmatch(r"[\w.-]+", parts[1]) or parts[1] in (".", ".."):
            self.send_error(404)
            return
        root = (state_dir() / "share").resolve()
        path = root / parts[0] / parts[1]
        try:
            if (path.is_symlink() or path.parent.is_symlink() or not path.is_file()
                    or path.parent.stat().st_mtime < time.time() - SHARE_DAYS * 86400):
                raise OSError
            with path.open("rb") as stream:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(path.stat().st_size))
                self.end_headers()
                if not _copy_while_on(stream, self.wfile):
                    self.close_connection = True  # switched off mid-download: the rest is not sent
        except OSError:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


CHUNK = 64 * 1024


def _copy_while_on(stream, out) -> bool:
    """The file in chunks, the mirroring switch checked before each one. False when it went off part-way:
    the rest is not sent (the caller closes the connection, so the download stops short)."""
    from . import policy
    while True:
        chunk = stream.read(CHUNK)
        if not chunk:
            return True
        if not policy.on():
            return False
        out.write(chunk)


def serve(port: int) -> None:
    http.server.ThreadingHTTPServer(("127.0.0.1", port), ShareHandler).serve_forever()
