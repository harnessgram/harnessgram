"""Signed answers for the decisions board (`ctb decide`).

When the owner answers a decision, the bridge signs what was answered, by whom and through which route with an
Ed25519 key that only the bridge holds, in the macOS Keychain. Another tool (the ai-filter committee) checks that
signature against the public key (`ctb decide public-key`), so a record written into decisions.json by hand never
counts as the owner's answer.

The signature is stored on the answered record as `answer_sig`: {"payload", "signature", "key"}, each base64. The
payload is the exact signed bytes: compact, key-sorted JSON (see `payload`). `by` is the Telegram user id the route
itself verified (never a value from the request body) and `via` names the route.

The key is a raw 32-byte seed, base64, under SERVICE / ACCOUNT, created on first use. It is never logged, printed
or written to a file. Signing needs the optional `cryptography` package (the `sign` extra). When the package or the
Keychain is unavailable the answer still goes through, unsigned (one content-free poll.log line): an unsigned answer
simply does not count for a verifier, and the owner is never blocked.
"""
from __future__ import annotations

import base64
import binascii
import json
import secrets
import sys
from typing import Optional, Tuple

SERVICE = "harnessgram.decision-signing"
ACCOUNT = "ed25519-seed"
KIND = "harnessgram-decision-answer"
VERSION = 1
# panel: the Mini App (Telegram initData proves the user is the owner); widget: a paired device's token;
# chat-widget: a widget button in the owner's private chat (the callback's sender and chat are the owner).
VIAS = frozenset({"panel", "widget", "chat-widget"})
SIG_FIELDS = ("payload", "signature", "key")
MAX_PAYLOAD_B64 = 16384  # title 200 + detail 1500 + note 300 characters, at most 4 bytes each, plus the frame
INSTALL_HINT = "install the signing extra: uv tool install --reinstall 'harnessgram[sign]'"


class Unavailable(Exception):
    """Why an answer cannot be signed: a short code, never secret material."""


class KeychainStore:
    """The seed's Keychain item, through accounts.py's Keychain helpers."""

    def get(self) -> Tuple[str, Optional[str]]:
        from .accounts import _keychain_item
        return _keychain_item(SERVICE, ACCOUNT)

    def add(self, seed: str) -> bool:
        from .accounts import _keychain_add
        return _keychain_add(SERVICE, ACCOUNT, seed)


STORE = KeychainStore()  # tests put a fake in its place


def _ed25519():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError:
        raise Unavailable("no-cryptography") from None
    return ed25519


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text, size: Optional[int] = None, limit: int = MAX_PAYLOAD_B64) -> Optional[bytes]:
    if not isinstance(text, str) or not text or len(text) > limit:
        return None
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None
    return raw if size is None or len(raw) == size else None


def load_key(create: bool = True):
    """The bridge's private key, created (once, never replacing an existing item) when the Keychain has none.
    Raises Unavailable: "no-cryptography", "keychain" (could not read or write it), "no-key", "bad-seed"."""
    ed25519 = _ed25519()  # before the Keychain: without the package there is nothing to sign with
    status, seed = STORE.get()
    if status == "missing" and create:
        STORE.add(_b64(secrets.token_bytes(32)))
        status, seed = STORE.get()  # read back: another process may have created it first, and its key wins
    if status == "missing":
        raise Unavailable("no-key")
    if status != "ok":
        raise Unavailable("keychain")
    raw = _unb64(seed, 32, limit=64)
    if raw is None:
        raise Unavailable("bad-seed")  # never replaced: a verifier may already trust the key it stands for
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw)


def public_key_b64(key) -> str:
    return _b64(key.public_key().public_bytes_raw())


def valid_route(by, via) -> bool:
    return type(by) is int and by > 0 and via in VIAS


def payload(rec: dict, by: int, via: str) -> bytes:
    """The exact bytes signed for an answered record."""
    obj = {"v": VERSION, "kind": KIND, "id": rec["id"], "title": rec["title"], "detail": rec["detail"],
           "choice": rec["choice"], "note": rec["note"], "created": float(rec["created"]),
           "closed": float(rec["closed"]), "routine": rec.get("routine") is True, "by": by, "via": via}
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


def sign(rec: dict, by: int, via: str, key) -> dict:
    """The `answer_sig` of an answered record."""
    if not valid_route(by, via):
        raise Unavailable("route")
    body = payload(rec, by, via)
    return {"payload": _b64(body), "signature": _b64(key.sign(body)), "key": public_key_b64(key)}


def key_for_answer(by, via):
    """The private key for one answer, or None (with one content-free poll.log line) when it cannot be signed.
    Read before the decisions lock is taken, so a slow Keychain never holds it."""
    from .inbox import poll_log
    if not valid_route(by, via):
        poll_log("decision sign: unsigned (route)")
        return None
    try:
        return load_key()
    except Unavailable as e:
        poll_log(f"decision sign: unsigned ({e})")
    except Exception as e:  # noqa: BLE001 - a failure to sign never blocks the owner's answer
        poll_log(f"decision sign: unsigned ({type(e).__name__})")
    return None


def well_formed(sig) -> bool:
    """Whether a stored `answer_sig` has its three base64 fields with sane lengths (it is not verified here)."""
    return (isinstance(sig, dict) and set(sig) == set(SIG_FIELDS)
            and _unb64(sig["payload"]) is not None
            and _unb64(sig["signature"], 64, limit=128) is not None
            and _unb64(sig["key"], 32, limit=64) is not None)


def print_public_key() -> int:
    """`ctb decide public-key`: only the base64 raw 32-byte public key on stdout, creating the key if missing."""
    try:
        key = load_key()
    except Unavailable as e:
        reason = {"no-cryptography": "the cryptography package is missing; " + INSTALL_HINT,
                  "bad-seed": "the stored signing key is malformed (it is left as it is)",
                  }.get(str(e), "the signing key is not available in the Keychain")
        print(f"HarnessGram: decision signing is not available: {reason}", file=sys.stderr)
        return 1
    print(public_key_b64(key))
    return 0
