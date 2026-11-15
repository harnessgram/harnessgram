"""Widget device tokens: the Harnessgram iPhone app's home and lock-screen widgets read the Cockpit without Telegram.

A widget cannot open the Mini App, so it has no Telegram `initData`. The app pairs once while the owner is signed in
(`POST /api/device-pair`, behind the fresh owner login) and gets a random device token, valid TTL seconds. The token
reaches exactly two routes: `POST /api/widget-state` (a read-only snapshot) and `POST /api/widget-decide` (an answer
to a decision that is not sensitive). It is sent only in the `Authorization: Bearer` header, never in a URL.

The bridge keeps only the SHA-256 of each token (with a label, its creation time and its last use) in the private
`widget-devices.json` of its state directory, so a stolen state file does not yield a working token, and every stored
hash is compared in constant time. At most MAX_DEVICES stay paired (the oldest is dropped first), pairing is limited
to PAIR_LIMIT per PAIR_WINDOW, and the owner revokes one device or all of them from Cockpit Settings.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import List, Optional

from .config import locked_map, read_map, state_dir

STORE = "widget-devices.json"
TTL = 30 * 86400          # a device token expires after 30 days and can be revoked sooner
MAX_DEVICES = 8
PAIR_LIMIT = 5            # pairings allowed per PAIR_WINDOW seconds
PAIR_WINDOW = 3600
USED_EVERY = 600          # the last-use time is written at most this often per device
PREFIX = "hgw_"           # never a listening token (listen_link.py) or a Telegram value
TOKEN = re.compile(r"hgw_[A-Za-z0-9_-]{43}")  # PREFIX + secrets.token_urlsafe(32): 256 random bits
DEVICE_ID = re.compile(r"[0-9a-f]{12}")
LABEL_CHARS = 40
DEFAULT_LABEL = "iPhone"


class DeviceError(Exception):
    """kind: "rate" (with `retry` seconds) | "label"."""

    def __init__(self, kind: str, retry: int = 0):
        super().__init__(kind)
        self.kind, self.retry = kind, retry


def _store():
    return state_dir() / STORE


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _live(row: object, now: float) -> bool:
    return (isinstance(row, dict) and isinstance(row.get("h"), str) and isinstance(row.get("id"), str)
            and DEVICE_ID.fullmatch(row["id"]) is not None and type(row.get("at")) is int
            and 0 <= now - row["at"] < TTL)


def clean_label(raw) -> str:
    """One short line of plain text; empty becomes DEFAULT_LABEL. Too long is refused, never cut."""
    from .decisions import DecisionError, clean
    if raw is None:
        return DEFAULT_LABEL
    if not isinstance(raw, str):
        raise DeviceError("label")
    try:
        return clean(raw, LABEL_CHARS, "label", required=False) or DEFAULT_LABEL
    except DecisionError:
        raise DeviceError("label") from None


def pair(label=None, now: Optional[float] = None) -> dict:
    """A new device: {"token", "id", "label", "created", "expires"}. The token is returned once and never kept."""
    label = clean_label(label)
    token = PREFIX + secrets.token_urlsafe(32)
    with locked_map(_store()) as data:
        now = time.time() if now is None else now
        pairs = [p for p in data.get("pairs", []) if isinstance(p, (int, float)) and 0 <= now - p < PAIR_WINDOW]
        if len(pairs) >= PAIR_LIMIT:
            data["pairs"] = pairs
            raise DeviceError("rate", int(PAIR_WINDOW - (now - min(pairs))) + 1)
        rows = [r for r in data.get("devices", []) if _live(r, now)]
        ident = secrets.token_hex(6)
        while any(r["id"] == ident for r in rows):
            ident = secrets.token_hex(6)
        rows.append({"id": ident, "h": _digest(token), "label": label, "at": int(now), "used": 0})
        data["devices"] = rows[-MAX_DEVICES:]
        data["pairs"] = pairs + [int(now)]
    return {"token": token, "id": ident, "label": label, "created": int(now), "expires": int(now) + TTL}


def check(token: object, now: Optional[float] = None) -> Optional[str]:
    """The id of the paired device this token belongs to, or None. Every stored hash is compared in constant
    time, whichever matches."""
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        return None
    want = _digest(token)
    now = time.time() if now is None else now
    found = None
    for row in read_map(_store()).get("devices", []):
        if _live(row, now) and hmac.compare_digest(row["h"], want):
            found = row
    if found is None:
        return None
    if not isinstance(found.get("used"), int) or now - found["used"] >= USED_EVERY:
        try:
            with locked_map(_store()) as data:
                for row in data.get("devices", []):
                    if isinstance(row, dict) and row.get("id") == found["id"]:
                        row["used"] = int(now)
        except OSError:
            pass  # the last-use time is a convenience; the answer stays the same
    return found["id"]


def listing(now: Optional[float] = None) -> List[dict]:
    """The paired devices, newest first, for Settings: never a hash or a token."""
    now = time.time() if now is None else now
    rows = [r for r in read_map(_store()).get("devices", []) if _live(r, now)]
    return [{"id": r["id"], "label": str(r.get("label") or DEFAULT_LABEL)[:LABEL_CHARS], "created": r["at"],
             "expires": r["at"] + TTL, "used": r["used"] if isinstance(r.get("used"), int) else 0}
            for r in sorted(rows, key=lambda r: -r["at"])]


def revoke(ident: object) -> bool:
    """Revoke one device; False when there is no such device."""
    if not isinstance(ident, str) or not DEVICE_ID.fullmatch(ident):
        return False
    if not any(isinstance(r, dict) and r.get("id") == ident for r in read_map(_store()).get("devices", [])):
        return False
    with locked_map(_store()) as data:
        before = len(data.get("devices", []))
        data["devices"] = [r for r in data.get("devices", []) if not (isinstance(r, dict) and r.get("id") == ident)]
        return len(data["devices"]) < before


def revoke_all() -> int:
    """Revoke every device at once; returns how many were active."""
    if not read_map(_store()).get("devices"):
        return 0
    with locked_map(_store()) as data:
        count = sum(1 for r in data.get("devices", []) if _live(r, time.time()))
        data["devices"] = []
    return count
