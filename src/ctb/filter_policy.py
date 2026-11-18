"""Private storage for committee policy documents.

The bridge does not verify Ed25519 signatures; the phone verifies them. The
standard-library-only bridge checks the document's structure and pinned key.
"""
import base64
import binascii
import fcntl
import json
from datetime import datetime, timezone

from . import config

# Set per install through the config's filter_committee_key; tests may pin it here.
COMMITTEE_KEY = ""


def committee_key(cfg=None):
    """The pinned committee key: COMMITTEE_KEY when set, else the config's filter_committee_key."""
    if COMMITTEE_KEY:
        return COMMITTEE_KEY
    if cfg is None:
        try:
            cfg = config.load()
        except Exception:  # noqa: BLE001 - no config means no committee
            return ""
    return str(getattr(cfg, "filter_committee_key", "") or "")
RULES = frozenset({"approvedChats", "contactsOnly", "mediaFilter", "sensitiveContent",
                   "stickerPacks", "linkPreviews", "linksOutside", "search", "stories",
                   "adMedia", "otherBotWidgets"})


class FilterPolicyError(ValueError):
    """A policy could not be accepted."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _decode(value):
    if not isinstance(value, str):
        raise FilterPolicyError("invalid base64")
    try:
        decoded = base64.b64decode(value, validate=True)
        if base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("noncanonical base64")
        return decoded
    except (ValueError, binascii.Error):
        raise FilterPolicyError("invalid base64") from None


def _validate(doc):
    if not isinstance(doc, dict) or set(doc) != {"payload", "signature", "publicKey"}:
        raise FilterPolicyError("invalid document shape")
    payload, signature, key = (_decode(doc[k]) for k in ("payload", "signature", "publicKey"))
    if len(signature) != 64 or len(key) != 32:
        raise FilterPolicyError("invalid signature or public key length")
    try:
        data = json.loads(payload.decode("utf-8"), object_pairs_hook=_object)
    except (ValueError, UnicodeError, RecursionError):
        raise FilterPolicyError("invalid payload JSON") from None
    if not isinstance(data, dict) or set(data) != {"version", "locked", "rules"}:
        raise FilterPolicyError("invalid payload shape")
    if type(data["version"]) is not int or data["version"] <= 0 or type(data["locked"]) is not bool:
        raise FilterPolicyError("invalid version or locked value")
    rules = data["rules"]
    if not isinstance(rules, dict) or not set(rules).issubset(RULES) or any(type(v) is not bool for v in rules.values()):
        raise FilterPolicyError("invalid rules")
    return data


def current():
    """Return the stored document, or None when missing or damaged."""
    try:
        path = config.state_dir() / "filter-policy.json"
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object)
        if not isinstance(data, dict) or set(data) != {"payload", "signature", "publicKey", "stored_at"}:
            return None
        if not isinstance(data["stored_at"], str) or not data["stored_at"]:
            return None
        _validate({k: data[k] for k in ("payload", "signature", "publicKey")})
        return data
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def publish(doc, pinned_key_b64):
    """Validate and store a newer document; signature verification is the phone's job."""
    payload = _validate(doc)
    if not pinned_key_b64 or doc["publicKey"] != pinned_key_b64:
        raise FilterPolicyError("public key does not match the pinned key")
    path = config.state_dir() / "filter-policy.json"
    try:
        config.private_dir(path.parent)
        with open(str(path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = current()
            if previous and payload["version"] <= _validate({k: previous[k] for k in ("payload", "signature", "publicKey")})["version"]:
                raise FilterPolicyError("version is not newer")
            stored = dict(doc, stored_at=datetime.now(timezone.utc).isoformat())
            config.write_private(path, json.dumps(stored, ensure_ascii=False))
            return stored
    except OSError as exc:
        raise FilterPolicyError("could not store policy") from exc
