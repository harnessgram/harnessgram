"""Server-only Vertex Live transport. No credentials, raw errors or wire dumps leave this module."""
from __future__ import annotations

import json
import logging
import re
import socket
import threading

from . import radio_voice

DEFAULT_MODEL = "gemini-live-2.5-flash-native-audio"
MAX_WIRE = 512 * 1024
OPEN_TIMEOUT = 8
CONNECT_TIMEOUT = radio_voice.TOKEN_TIMEOUT + OPEN_TIMEOUT
SEND_TIMEOUT = 5


class Unavailable(Exception):
    pass


def send(sock, wire):
    """Bound every write, including setup, without doing network I/O on the caller.

    sendall has no write deadline in websockets 15. A daemon performs the write;
    timeout shuts down TCP to interrupt it before closing the protocol connection.
    Only a success flag crosses threads, never an exception containing wire data.
    """
    done = threading.Event()
    succeeded = []

    def write():
        try:
            sock.send(wire)
            succeeded.append(True)
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=write, daemon=True, name="voice-copilot-send").start()
    if not done.wait(SEND_TIMEOUT) or not succeeded:
        close(sock)
        raise Unavailable()


def close(sock):
    """Interrupt a blocked sendall before waiting for the WebSocket's protocol mutex.

    websockets 15's close_timeout bounds the handshake, not acquiring that mutex.
    Shut down the TCP socket first so both its sending and receiving threads wake.
    """
    transport = getattr(sock, "socket", None)
    if transport is not None:
        try:
            transport.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    try:
        sock.close()
    except Exception:
        pass


def connect(cfg, setup, connector=None):
    """Reuse the radio's token helper; the optional WebSocket extra is imported only on opt-in.

    A private, disabled logger prevents library debug logging from exposing the Authorization header.
    Tests inject the connector and token helper; they never contact Vertex or run a token command.
    """
    project = cfg.gemini_vertex_project
    region = cfg.voice_copilot_location
    model = cfg.voice_copilot_model
    if (not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", project or "") or
            not re.fullmatch(r"[a-z]+-[a-z]+[0-9]+", region or "") or
            not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", model or "")):
        raise Unavailable()  # no arbitrary host/path from config
    if connector is None:
        try:
            from websockets.sync.client import connect as connector
        except ImportError:
            raise Unavailable() from None
    token = radio_voice.access_token(cfg)  # no `why`: command output must never reach a log
    if not token:
        raise Unavailable()
    logger = logging.Logger("ctb.voice_copilot.vertex.private")
    logger.disabled = True
    url = (f"wss://{region}-aiplatform.googleapis.com/ws/"
           "google.cloud.aiplatform.v1beta1.LlmBidiService/BidiGenerateContent")
    sock = None
    try:
        sock = connector(url, additional_headers={"Authorization": "Bearer " + token},
                         open_timeout=OPEN_TIMEOUT, close_timeout=1, max_size=MAX_WIRE, max_queue=8,
                         proxy=None, logger=logger)
        setup = dict(setup, model=f"projects/{project}/locations/{region}/publishers/google/models/{model}")
        send(sock, json.dumps({"setup": setup}, ensure_ascii=False))
        return sock
    except Exception:
        if sock is not None:
            close(sock)
        raise Unavailable() from None
    finally:
        token = ""  # only the socket handshake and the radio's existing in-memory cache retain it
