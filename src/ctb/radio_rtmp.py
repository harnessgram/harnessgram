"""A minimal RTMP(S) publisher: forwards an FLV byte stream (ffmpeg's `-f flv pipe:1`) to a live-stream server.

Why not ffmpeg's own RTMP output: ffmpeg takes the server URL and the stream key only on its command line, where
any process of the same user can read them (`ps -ww`). Here the URL and the key live only in this process's
memory: they are never logged, written to disk or put on a command line.

Scope: the simple RTMP handshake, connect / releaseStream / FCPublish / createStream / publish, one chunk size,
and FLV tags sent as RTMP messages. Enough for Telegram's RTMP live streams and ffmpeg's own `-listen 1` server.
"""
from __future__ import annotations

import os
import socket
import ssl
import struct
import threading
import time
from typing import BinaryIO, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

CHUNK = 4096
HANDSHAKE = 1536
TIMEOUT = 15


class RtmpError(Exception):
    """A failure that carries no URL and no key."""


# --- AMF0 ----------------------------------------------------------------------------------

def amf_encode(value) -> bytes:
    if value is None:
        return b"\x05"
    if isinstance(value, bool):
        return b"\x01" + (b"\x01" if value else b"\x00")
    if isinstance(value, (int, float)):
        return b"\x00" + struct.pack(">d", float(value))
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return b"\x02" + struct.pack(">H", len(raw)) + raw
    if isinstance(value, dict):
        out = b"\x03"
        for k, v in value.items():
            key = k.encode("utf-8")
            out += struct.pack(">H", len(key)) + key + amf_encode(v)
        return out + b"\x00\x00\x09"
    raise TypeError("unsupported AMF value")


def amf_decode_all(data: bytes) -> List:
    out, pos = [], 0
    while pos < len(data):
        value, pos = _amf_one(data, pos)
        out.append(value)
    return out


def _amf_one(data: bytes, pos: int):
    kind = data[pos]
    pos += 1
    if kind == 0x00:
        return struct.unpack(">d", data[pos:pos + 8])[0], pos + 8
    if kind == 0x01:
        return data[pos] != 0, pos + 1
    if kind == 0x02:
        n = struct.unpack(">H", data[pos:pos + 2])[0]
        return data[pos + 2:pos + 2 + n].decode("utf-8", "replace"), pos + 2 + n
    if kind in (0x03, 0x08):
        if kind == 0x08:
            pos += 4  # the ECMA array's approximate count
        obj = {}
        while True:
            n = struct.unpack(">H", data[pos:pos + 2])[0]
            pos += 2
            if n == 0 and data[pos] == 0x09:
                return obj, pos + 1
            key = data[pos:pos + n].decode("utf-8", "replace")
            obj[key], pos = _amf_one(data, pos + n)
    if kind in (0x05, 0x06):
        return None, pos
    if kind == 0x0A:
        count = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
        items = []
        for _ in range(count):
            v, pos = _amf_one(data, pos)
            items.append(v)
        return items, pos
    raise RtmpError("unsupported AMF type in a server reply")


# --- URL -----------------------------------------------------------------------------------

def split_url(url: str) -> Tuple[str, int, bool, str, str]:
    """(host, port, tls, app, tcUrl) of an rtmp:// or rtmps:// server URL. Raises RtmpError without echoing it."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise RtmpError("bad stream URL") from None
    try:
        explicit = parts.port
    except ValueError:
        raise RtmpError("bad stream URL") from None
    if parts.scheme not in ("rtmp", "rtmps") or not parts.hostname:
        raise RtmpError("bad stream URL")
    tls = parts.scheme == "rtmps"
    port = explicit or (443 if tls else 1935)
    app = parts.path.strip("/")
    if not app:
        raise RtmpError("bad stream URL")
    host = parts.hostname
    netloc = host if explicit is None else f"{host}:{explicit}"
    return host, port, tls, app, f"{parts.scheme}://{netloc}/{app}"


# --- the connection ------------------------------------------------------------------------

class Publisher:
    """One live publish. `connect()` then `send_flv(stream)` until the stream ends or the server goes away."""

    def __init__(self, url: str, key: str, timeout: float = TIMEOUT):
        self._host, self._port, self._tls, self._app, self._tc = split_url(url)
        if not key or any(c.isspace() for c in key):
            raise RtmpError("bad stream key")
        self._key = key
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._in_chunk = 128
        self._in: Dict[int, dict] = {}
        self._replies: Dict[float, list] = {}
        self._status: List[dict] = []
        self._cv = threading.Condition()
        self._closed = False
        self._error = ""
        self._stream_id = 0
        self._txn = 0
        self.sent_bytes = 0
        self._wlock = threading.Lock()

    # --- low level
    def _open(self) -> None:
        raw = socket.create_connection((self._host, self._port), timeout=self._timeout)
        if self._tls:
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=self._host)
        raw.settimeout(self._timeout)
        self._sock = raw

    def _recv_exact(self, n: int, patient: bool = False) -> bytes:
        """`patient` (the reader thread): a quiet server is normal while we publish, so a read timeout only
        loops; the publisher's own watchdog notices a dead connection."""
        buf = bytearray()
        while len(buf) < n:
            try:
                part = self._sock.recv(n - len(buf))
            except socket.timeout:
                if patient and not self._closed:
                    continue
                raise
            if not part:
                raise RtmpError("the server closed the connection")
            buf += part
        return bytes(buf)

    def _write(self, data: bytes) -> None:
        with self._wlock:
            self._sock.sendall(data)
            self.sent_bytes += len(data)

    def _handshake(self) -> None:
        c1 = struct.pack(">II", int(time.time()) & 0xFFFFFFFF, 0) + os.urandom(HANDSHAKE - 8)
        self._write(b"\x03" + c1)
        s0 = self._recv_exact(1)
        if s0[0] != 3:
            raise RtmpError("unexpected RTMP version")
        s1 = self._recv_exact(HANDSHAKE)
        self._recv_exact(HANDSHAKE)  # S2 echoes C1; nothing to check for the simple handshake
        self._write(s1)

    def message(self, csid: int, mtype: int, payload: bytes, timestamp: int = 0, stream: int = 0) -> None:
        """One RTMP message, split into chunks of CHUNK bytes (a type-0 header, then type-3 continuations)."""
        timestamp &= 0xFFFFFFFF
        ext = timestamp >= 0xFFFFFF
        ts = 0xFFFFFF if ext else timestamp
        head = bytes([csid & 0x3F]) + ts.to_bytes(3, "big") + len(payload).to_bytes(3, "big") + bytes([mtype])
        head += struct.pack("<I", stream)
        if ext:
            head += struct.pack(">I", timestamp)
        out = bytearray(head)
        out += payload[:CHUNK]
        pos = CHUNK
        while pos < len(payload):
            out.append(0xC0 | (csid & 0x3F))
            if ext:
                out += struct.pack(">I", timestamp)
            out += payload[pos:pos + CHUNK]
            pos += CHUNK
        self._write(bytes(out))

    def _command(self, name: str, *args, stream: int = 0, csid: int = 3) -> float:
        self._txn += 1
        txn = float(self._txn)
        payload = amf_encode(name) + amf_encode(txn) + b"".join(amf_encode(a) for a in args)
        self.message(csid, 20, payload, stream=stream)
        return txn

    # --- the reader thread
    def _read_loop(self) -> None:
        try:
            while not self._closed:
                self._read_message()
        except Exception as e:  # the socket closed or the server sent garbage
            with self._cv:
                if not self._closed:
                    self._error = e.__class__.__name__ if not isinstance(e, RtmpError) else str(e)
                self._closed = True
                self._cv.notify_all()

    def _read_message(self) -> None:
        rx = lambda n: self._recv_exact(n, patient=True)  # noqa: E731
        b0 = rx(1)[0]
        fmt, csid = b0 >> 6, b0 & 0x3F
        if csid == 0:
            csid = 64 + rx(1)[0]
        elif csid == 1:
            lo, hi = rx(2)
            csid = 64 + lo + hi * 256
        st = self._in.setdefault(csid, {"ts": 0, "len": 0, "type": 0, "stream": 0, "buf": bytearray(), "delta": 0})
        if fmt <= 2:
            ts = int.from_bytes(rx(3), "big")
            if fmt <= 1:
                st["len"] = int.from_bytes(rx(3), "big")
                st["type"] = rx(1)[0]
            if fmt == 0:
                st["stream"] = struct.unpack("<I", rx(4))[0]
            st["ext"] = ts == 0xFFFFFF
            if st["ext"]:
                ts = struct.unpack(">I", rx(4))[0]
            if fmt == 0:
                st["ts"] = ts
            else:
                st["delta"] = ts
                st["ts"] += ts
        elif st.get("ext"):
            rx(4)
        need = min(self._in_chunk, st["len"] - len(st["buf"]))
        st["buf"] += rx(need)
        if len(st["buf"]) < st["len"]:
            return
        payload, st["buf"] = bytes(st["buf"]), bytearray()
        self._on_message(st["type"], payload)

    def _on_message(self, mtype: int, payload: bytes) -> None:
        if mtype == 1 and len(payload) >= 4:
            self._in_chunk = struct.unpack(">I", payload[:4])[0] & 0x7FFFFFFF
        elif mtype == 4 and len(payload) >= 6 and struct.unpack(">H", payload[:2])[0] == 6:
            # ping request: answer with a pong carrying the same timestamp
            self.message(2, 4, struct.pack(">H", 7) + payload[2:6])
        elif mtype == 20:
            values = amf_decode_all(payload)
            if len(values) < 2:
                return
            name, txn = values[0], values[1]
            with self._cv:
                if name in ("_result", "_error") and isinstance(txn, float):
                    self._replies[txn] = [name] + values[2:]
                elif name == "onStatus":
                    info = next((v for v in values[2:] if isinstance(v, dict)), {})
                    self._status.append(info)
                self._cv.notify_all()

    def _wait(self, test, what: str):
        deadline = time.monotonic() + self._timeout
        with self._cv:
            while True:
                got = test()
                if got is not None:
                    return got
                if self._closed:
                    raise RtmpError(f"connection lost while waiting for {what}")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RtmpError(f"no answer from the server ({what})")
                self._cv.wait(left)

    # --- the publish
    def connect(self) -> None:
        self._open()
        self._handshake()
        threading.Thread(target=self._read_loop, daemon=True, name="rtmp-read").start()
        self.message(2, 1, struct.pack(">I", CHUNK))
        txn = self._command("connect", {"app": self._app, "type": "nonprivate", "flashVer": "FMLE/3.0",
                                        "tcUrl": self._tc})
        reply = self._wait(lambda: self._replies.pop(txn, None), "connect")
        if reply[0] != "_result":
            raise RtmpError("the server refused the connection")
        self._command("releaseStream", None, self._key)
        self._command("FCPublish", None, self._key)
        txn = self._command("createStream", None)
        reply = self._wait(lambda: self._replies.pop(txn, None), "createStream")
        ids = [v for v in reply[1:] if isinstance(v, float)]
        if reply[0] != "_result" or not ids:
            raise RtmpError("the server did not open a stream")
        self._stream_id = int(ids[0])
        self._command("publish", None, self._key, "live", stream=self._stream_id, csid=8)

        def published():
            for info in self._status:
                code = str(info.get("code", ""))
                if code == "NetStream.Publish.Start":
                    return True
                if code.startswith("NetStream.Publish.") or code.endswith(".Failed") or "Reject" in code:
                    return False
            return None
        if not self._wait(published, "publish"):
            raise RtmpError("the server refused the publish")

    def send_flv(self, stream: BinaryIO, stop: Optional[threading.Event] = None) -> None:
        """Forward FLV tags from `stream` until it ends, the server goes away, or `stop` is set."""
        header = _read_exact(stream, 9)
        if header[:3] != b"FLV":
            raise RtmpError("the encoder did not produce FLV")
        skip = struct.unpack(">I", header[5:9])[0] - 9 + 4  # the rest of the header and PreviousTagSize0
        _read_exact(stream, skip)
        while not (stop and stop.is_set()):
            if self._closed:
                raise RtmpError(self._error or "connection lost")
            tag = stream.read(11)
            if not tag:
                return
            if len(tag) < 11:
                tag += _read_exact(stream, 11 - len(tag))
            kind = tag[0] & 0x1F
            size = int.from_bytes(tag[1:4], "big")
            ts = int.from_bytes(tag[4:7], "big") | (tag[7] << 24)
            data = _read_exact(stream, size)
            _read_exact(stream, 4)  # PreviousTagSize
            if kind == 18:
                self.message(4, 18, amf_encode("@setDataFrame") + data, ts, self._stream_id)
            elif kind in (8, 9):
                self.message(4 if kind == 8 else 6, kind, data, ts, self._stream_id)

    def close(self) -> None:
        with self._cv:
            was = self._closed
            self._closed = True
            self._cv.notify_all()
        if self._sock is None:
            return
        if not was:
            try:
                if self._stream_id:
                    self._command("FCUnpublish", None, self._key)
                    self._command("deleteStream", None, float(self._stream_id))
            except Exception:
                pass
        try:
            self._sock.close()
        except OSError:
            pass


def _read_exact(stream: BinaryIO, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        part = stream.read(n - len(buf))
        if not part:
            raise EOFError("the encoder stopped")
        buf += part
    return buf
