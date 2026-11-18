"""Private APNs Live Activity token storage and heartbeat-owned delivery."""
from __future__ import annotations

import base64
import json
import re
import sys
import time
from typing import Callable, Optional

from .config import Config, locked_map, state_dir

PRODUCTION_HOST = "api.push.apple.com"
SANDBOX_HOST = "api.sandbox.push.apple.com"
KEYCHAIN_ITEM = ("harnessgram.apns-auth-key", "p8")
TOKEN_FILE = "live-activity-tokens.json"
PENDING_FILE = "live-activity-pending.json"
DELIVERY_FILE = "live-activity-delivery.json"
MAX_TOKENS_PER_DEVICE = 10
MAX_TOKEN_HEX = 512  # 256 bytes, as the app sends (APNs tokens have grown before)
THROTTLE_SECONDS = 15
MAX_RETRY_SECONDS = 5 * 60
MAX_ATTEMPTS = 8
MAX_DRAIN_SECONDS = 20
MAX_PAYLOAD_BYTES = 4096
JWT_SECONDS = 50 * 60
CLIENT_FAILURE_RETRY_SECONDS = 15 * 60
CONTENT_STATE_FIELDS = ("title", "topic", "steps", "done", "percent", "finished")
CARD_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
HEX_TOKEN = re.compile(r"[0-9a-fA-F]{2,512}")
HEX_TEXT = re.compile(r"(?:[0-9a-fA-F]{2})+")
BASE64_BODY = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
PEM_MARK = "-----BEGIN"
PKCS8_LABEL = "PRIVATE " + "KEY"


def _path():
    return state_dir() / TOKEN_FILE


def _pending_path():
    return state_dir() / PENDING_FILE


def _delivery_path():
    return state_dir() / DELIVERY_FILE


def _extras_available() -> bool:
    """Check optional sending dependencies only when the heartbeat has work to deliver."""
    try:
        import httpx  # noqa: F401
        from cryptography.hazmat.primitives import hashes  # noqa: F401
    except (ImportError, ModuleNotFoundError):
        return False
    return True


def register(device: str, card_id: object, token: object = None, env: object = None,
             ended: object = False, now: Optional[float] = None) -> bool:
    """Validate and replace one device/card token. The return value is false for an invalid request."""
    if not isinstance(device, str) or not isinstance(card_id, str) or not CARD_ID.fullmatch(card_id):
        return False
    if ended is True:
        if token is not None or env is not None:
            return False
        with locked_map(_path()) as data:
            data.pop(device + ":" + card_id, None)
        return True
    if ended not in (False, None) or not isinstance(token, str) or len(token) > MAX_TOKEN_HEX \
            or not HEX_TOKEN.fullmatch(token) or len(token) % 2 or env not in ("sandbox", "production"):
        return False
    now = time.time() if now is None else now
    key = device + ":" + card_id
    with locked_map(_path()) as data:
        data[key] = {"device": device, "card_id": card_id, "token": token.lower(), "env": env, "at": now}
        mine = sorted(((k, v) for k, v in data.items() if isinstance(v, dict) and v.get("device") == device),
                      key=lambda item: item[1].get("at", 0))
        for old, _ in mine[:-MAX_TOKENS_PER_DEVICE]:
            data.pop(old, None)
    return True


def tokens_for(card_id: str) -> list:
    from .config import read_map
    return [dict(v) for v in read_map(_path()).values()
            if isinstance(v, dict) and v.get("card_id") == card_id and isinstance(v.get("token"), str)]


def drop(row: dict) -> None:
    key = str(row.get("device")) + ":" + str(row.get("card_id"))
    with locked_map(_path()) as data:
        current = data.get(key)
        if isinstance(current, dict) and current.get("token") == row.get("token"):
            data.pop(key, None)


def drop_device(device: str) -> None:
    """Forget every Live Activity belonging to a revoked widget device."""
    with locked_map(_path()) as data:
        for key, row in list(data.items()):
            if isinstance(row, dict) and row.get("device") == device:
                data.pop(key, None)


_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
MAX_STEPS = 12  # as the app: HarnessgramCockpitParser keeps and draws up to 12 steps


def _utf8(value: object, limit: int) -> str:
    # Control characters become one space: JSON would escape each to six bytes and the lock screen cannot show them.
    text = _CONTROL.sub(" ", str(value or ""))
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def _is_state(card: dict) -> bool:
    return tuple(card) == CONTENT_STATE_FIELDS


def _without_topic(title: object, topic: object) -> tuple:
    """The lock screen shows "topic: title", so a title that already starts with the topic would print it twice
    ("Home: day: Home: day"). Drop the repeated part; a title that is only the topic leaves the topic alone."""
    title = " ".join(str(title or "").split())
    topic = " ".join(str(topic or "").split())
    if not topic or not title:
        return title, topic
    if title == topic:
        return title, ""
    for sep in (":", " -", " –", " —", " ·"):
        head = topic + sep
        if title.startswith(head):
            rest = title[len(head):].strip(" :-–—·")
            return (rest or title), topic
    return title, topic


def content_state(card: dict, now: Optional[float] = None) -> dict:
    """The exact Codable HarnessgramTaskState shape consumed by the iPhone app."""
    raw_steps = card.get("steps") if isinstance(card.get("steps"), list) else []
    names = [step for step in raw_steps if isinstance(step, str)]
    at = card.get("at") if type(card.get("at")) is int else 0
    at = max(0, min(len(names), at))
    # The app shows at most MAX_STEPS steps. A longer card shows a window that ends at the current step, so
    # the lock screen never shows every step done while work is still going.
    start = max(0, min(at - (MAX_STEPS - 1), len(names) - MAX_STEPS))
    steps = [_utf8(step, 120) for step in names[start:start + MAX_STEPS]]
    done = max(0, min(len(steps), at - start))
    percent = card.get("percent") if type(card.get("percent")) is int else None
    if percent is not None:
        percent = max(0, min(100, percent))
    elif len(names) > MAX_STEPS:
        # The app's fraction is done/steps.count, which a window would overstate; send the true share instead.
        percent = round(at * 100 / len(names))
    all_done = bool(raw_steps) and type(card.get("at")) is int and card["at"] >= len(raw_steps)
    finished = (card.get("state") == "closed" or bool(card.get("finished")) or all_done or percent == 100)
    title, topic = _without_topic(card.get("title"), card.get("topic"))
    return {"title": _utf8(title, 120), "topic": _utf8(topic, 120),
            "steps": steps, "done": done, "percent": percent, "finished": bool(finished)}


def _size(payload: dict) -> int:
    # Measured as escaped ASCII, the largest form any httpx version sends.
    return len(json.dumps(payload).encode("utf-8"))


def _fit(payload: dict) -> None:
    """Keep the payload under Apple's 4 KB limit: drop shown steps (never the current one), then shorten the texts."""
    state = payload["aps"]["content-state"]
    while _size(payload) > MAX_PAYLOAD_BYTES and state["steps"]:
        if state["done"] < len(state["steps"]) - 1:
            state["steps"].pop()  # a later step first, so the current one stays shown
        else:
            state["steps"].pop(0)
            state["done"] = max(0, state["done"] - 1)
    for key in ("topic", "title"):
        while _size(payload) > MAX_PAYLOAD_BYTES and state[key]:
            state[key] = state[key][: len(state[key]) // 2]


def _pem_text(raw: object) -> Optional[str]:
    """The .p8 key as PEM text, whatever form the Keychain read returned.

    `security find-generic-password -w` prints a value that holds a line break as hex, and a .p8 key always
    has line breaks, so a key stored as pasted comes back hex encoded. A key pasted on one line with literal
    backslash-n escapes, or as the bare base64 body without its header lines, is restored as well.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if PEM_MARK not in text and HEX_TEXT.fullmatch(text):
        try:
            decoded = bytes.fromhex(text).decode("utf-8")
        except ValueError:
            decoded = ""
        if PEM_MARK in decoded:
            text = decoded.strip()
    if PEM_MARK in text:
        if "\n" not in text:
            text = text.replace("\\n", "\n").strip()
        return text + "\n"
    body = "".join(text.split())
    if BASE64_BODY.fullmatch(body):
        lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
        return f"-----BEGIN {PKCS8_LABEL}-----\n{lines}\n-----END {PKCS8_LABEL}-----\n"
    return text


def _problem(exc: BaseException) -> str:
    """A fixed, value-free explanation of why the client could not be built."""
    if isinstance(exc, ImportError):
        return "install the 'live-activity' extra"
    if isinstance(exc, ValueError):
        return "the stored APNs key is not a readable .p8 private key"
    if isinstance(exc, RuntimeError):
        return "the APNs key is missing or unreadable, or the key or team id is not set"
    return "unexpected error"


def _reason(response) -> Optional[str]:
    if getattr(response, "status_code", None) not in (400, 403):
        return None
    try:
        reason = response.json().get("reason")
    except (AttributeError, TypeError, ValueError):
        return None
    return reason if isinstance(reason, str) else None


class APNSClient:
    """A small injectable HTTP/2 APNs client. It never logs request data."""

    def __init__(self, cfg: Config, keychain=None, http=None, clock: Callable = time.time):
        self.cfg, self.clock = cfg, clock
        if keychain is None:
            from .keychain import Keychain
            keychain = Keychain()
        self.keychain = keychain
        if http is None:
            import httpx
            http = httpx.Client(http2=True, timeout=10)
        self.http = http
        self._jwt = ""
        self._jwt_at = 0.0
        self._key: Optional[str] = None
        self.last_reason: Optional[str] = None  # APNs' reason for the latest refused send, for the log

    @staticmethod
    def _b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _sign(signing: bytes, key: str) -> bytes:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        private = serialization.load_pem_private_key(key.encode(), password=None)
        der = private.sign(signing, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def jwt(self) -> str:
        now = self.clock()
        if self._jwt and now - self._jwt_at < JWT_SECONDS:
            return self._jwt
        if self._key is None:  # held in memory after the first good read; failures are throttled by the Keychain cooldown
            self._key = _pem_text(self.keychain.get(KEYCHAIN_ITEM))
        key = self._key
        if not key or not self.cfg.apns_key_id or not self.cfg.apns_team_id:
            raise RuntimeError("APNs is not configured")
        header = self._b64(json.dumps({"alg": "ES256", "kid": self.cfg.apns_key_id}, separators=(",", ":")).encode())
        claims = self._b64(json.dumps({"iss": self.cfg.apns_team_id, "iat": int(now)}, separators=(",", ":")).encode())
        signing = (header + "." + claims).encode("ascii")
        self._jwt, self._jwt_at = header + "." + claims + "." + self._b64(self._sign(signing, key)), now
        return self._jwt

    def validate(self) -> None:
        """Read and sign with the configured key while construction errors can be reported clearly."""
        self.jwt()

    def send(self, row: dict, card: dict, event: str) -> int:
        host = PRODUCTION_HOST if row["env"] == "production" else SANDBOX_HOST
        stamp = self.clock()
        state = dict(card) if _is_state(card) else content_state(card, stamp)
        state["steps"] = list(state["steps"])  # _fit trims this copy, never the caller's queued record
        aps = {"timestamp": int(stamp), "event": event, "content-state": state}
        if event == "end":
            aps["dismissal-date"] = int(stamp + 15 * 60)
        else:
            aps["stale-date"] = int(stamp + 30 * 60)  # as the app's own local updates; a newer push refreshes it
        payload = {"aps": aps}
        _fit(payload)
        headers = {"authorization": "bearer " + self.jwt(), "apns-topic": self.cfg.apns_bundle_id + ".push-type.liveactivity",
                   "apns-push-type": "liveactivity", "apns-priority": "10" if event == "end" else "5"}
        url = "https://" + host + "/3/device/" + row["token"]
        response = self.http.post(url, json=payload, headers=headers)
        reason = _reason(response)
        if reason == "ExpiredProviderToken":
            self._jwt, self._jwt_at = "", 0.0
            headers["authorization"] = "bearer " + self.jwt()
            response = self.http.post(url, json=payload, headers=headers)
            reason = _reason(response)  # one retry only; a BadDeviceToken on it still drops the token
        if reason == "InvalidProviderToken":
            self._jwt, self._jwt_at = "", 0.0
        if response.status_code == 410 or reason == "BadDeviceToken":
            drop(row)
        self.last_reason = reason
        return response.status_code

    def close(self) -> None:
        self._key = None
        close = getattr(self.http, "close", None)
        if close:
            close()


def notify(card: dict, event: Optional[str] = None) -> None:
    """Persist the newest card state; CLI mutations never load config or start delivery work."""
    if card.get("kind") != "progress":
        return
    ident = card.get("id")
    if not isinstance(ident, str) or not CARD_ID.fullmatch(ident):
        return
    if not tokens_for(ident):
        return
    if event is None:
        # Keep this path deliberately dependency-free and cheap; cards use these terminal forms.
        steps = card.get("steps")
        terminal = (card.get("state") == "closed" or card.get("percent") == 100 or
                    isinstance(steps, list) and bool(steps) and card.get("at") == len(steps))
        event = "end" if terminal else "update"
    if event not in ("update", "end"):
        return
    stamp = time.time()
    state = content_state(card, stamp)
    with locked_map(_pending_path()) as data:
        previous = data.get(ident)
        gen = previous.get("gen", 0) + 1 if isinstance(previous, dict) and type(previous.get("gen")) is int else 1
        data.pop(ident, None)  # reinsertion makes the newest record survive bounded trimming
        data[ident] = {"state": state, "event": event, "at": stamp,
                       "attempts": 0, "next_at": stamp, "sent": [],
                       "session": card.get("session"), "chat": card.get("chat"), "thread": card.get("thread"),
                       "gen": gen}


_CLIENT = None
_CLIENT_KEY = None
_CLIENT_FAILURES = {}
_WARNED = set()


def _warn_once(kind: str, message: str) -> None:
    """Report configuration problems once per service process, without configuration values."""
    if kind not in _WARNED:
        _WARNED.add(kind)
        print(message, file=sys.stderr)


class _Deferred(RuntimeError):
    """A recent client build failed and was reported; the next try waits for its retry time."""


def _client(cfg: Config, now: float) -> APNSClient:
    """Reuse transport and provider token across heartbeat rounds."""
    global _CLIENT, _CLIENT_KEY
    key = (cfg.apns_key_id, cfg.apns_team_id, cfg.apns_bundle_id)
    if _CLIENT is None or _CLIENT_KEY != key:
        if _CLIENT_FAILURES.get(key, 0) > now:
            raise _Deferred("APNs client validation retry is deferred")
        if _CLIENT is not None:
            close = getattr(_CLIENT, "close", None)
            if close:
                close()
        candidate = APNSClient(cfg, clock=lambda: now)
        try:
            validate = getattr(candidate, "validate", None)
            if validate:
                validate()
        except Exception:
            close = getattr(candidate, "close", None)
            if close:
                close()
            _CLIENT_FAILURES[key] = now + CLIENT_FAILURE_RETRY_SECONDS
            raise
        _CLIENT_FAILURES.pop(key, None)
        _CLIENT = candidate
        _CLIENT_KEY = key
    _CLIENT.clock = lambda: now
    return _CLIENT


def _discard_client() -> None:
    global _CLIENT, _CLIENT_KEY
    if _CLIENT is not None:
        try:
            close = getattr(_CLIENT, "close", None)
            if close:
                close()
        finally:
            _CLIENT = _CLIENT_KEY = None


def drain(cfg: Config, now: Optional[float] = None) -> int:
    """Deliver persisted updates from the long-running heartbeat; transient failures remain queued."""
    now = time.time() if now is None else now
    if not (cfg.apns_key_id and cfg.apns_team_id and cfg.apns_bundle_id):
        return 0
    from .config import read_map
    pending = read_map(_pending_path())
    if not pending:
        return 0
    if not _extras_available():
        _warn_once("missing-extra", "APNs Live Activity delivery is disabled: install the 'live-activity' extra")
        return 0
    try:
        # Construction is intentionally here: optional imports and Keychain access never touch notify().
        client = _client(cfg, now)
    except _Deferred:
        return 0  # reported when the build failed; retried on its own after CLIENT_FAILURE_RETRY_SECONDS
    except Exception as exc:
        print(f"APNs Live Activity client could not be built ({type(exc).__name__}): {_problem(exc)}; "
              f"{len(pending)} updates wait, next try in {CLIENT_FAILURE_RETRY_SECONDS // 60} min", file=sys.stderr)
        return 0
    delivered = read_map(_delivery_path())
    from .topics import TopicMap
    from .config import home_chat
    places = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg).all_places()
    records = {}
    deadline = time.monotonic() + MAX_DRAIN_SECONDS
    for ident, record in pending.items():
        if not isinstance(record, dict) or record.get("next_at", 0) > now:
            continue
        card = record.get("state")
        event = record.get("event")
        if not isinstance(card, dict) or event not in ("update", "end"):
            records[ident] = "permanent"
            continue
        history = delivered.get(ident)
        last_sent = history.get("at", -THROTTLE_SECONDS) if isinstance(history, dict) else -THROTTLE_SECONDS
        if event == "update" and now < float(last_sent) + THROTTLE_SECONDS:
            continue
        records[ident] = record
    results = {}
    consecutive_exceptions = 0
    statuses = set()
    exceptions = set()
    for ident, record in records.items():
        if record == "permanent":
            continue
        if time.monotonic() >= deadline:
            break
        rows = tokens_for(ident)
        sent = set(record.get("sent", []))
        retry = False
        state = dict(record["state"])
        place = places.get(record.get("session")) if isinstance(record.get("session"), str) else None
        if isinstance(place, dict) and place.get("name"):
            # Named by session alone, as the panel does, so a session whose topic moved keeps its current name.
            state["topic"] = _utf8(place.get("name"), 120)
        for row in rows:
            marker = str(row.get("device")) + ":" + str(row.get("token"))
            if marker in sent:
                continue
            if time.monotonic() >= deadline:
                retry = True
                break
            try:
                if client is None:
                    client = _client(cfg, now)
                status = client.send(row, state, record["event"])
                consecutive_exceptions = 0
                if isinstance(status, int):
                    statuses.add(status)
            except Exception as exc:
                exceptions.add(type(exc).__name__)
                if not isinstance(exc, _Deferred):
                    print(f"APNs push failed card={ident} event={record['event']} error={type(exc).__name__}",
                          file=sys.stderr)
                _discard_client()
                client = None
                retry = True
                consecutive_exceptions += 1
                break
            if not (isinstance(status, int) and 200 <= status < 300):
                reason = getattr(client, "last_reason", None)
                reason = reason if isinstance(reason, str) and re.fullmatch(r"[A-Za-z]{1,64}", reason) else "none"
                print(f"APNs push failed card={ident} event={record['event']} status={status} reason={reason}",
                      file=sys.stderr)
            if isinstance(status, int) and (200 <= status < 300 or status == 410):
                sent.add(marker)
                if record["event"] == "end" and 200 <= status < 300:
                    drop(row)
            elif status == 429 or isinstance(status, int) and status >= 500:
                retry = True
            # Other statuses are permanent for this token.
        results[ident] = ("retry" if retry else "sent", sorted(sent))
        if consecutive_exceptions >= 3:
            break
    attempted = len(results)
    dropped = 0
    completed = []
    if records:
        with locked_map(_pending_path()) as data:
            for ident, snapshot in records.items():
                current = data.get(ident)
                # Never erase or delay a newer notification written during network I/O.
                snapshot_record = pending.get(ident) if snapshot == "permanent" else snapshot
                snapshot_gen = snapshot_record.get("gen") if isinstance(snapshot_record, dict) else None
                if not isinstance(current, dict) or current.get("gen") != snapshot_gen:
                    continue
                if snapshot == "permanent":
                    data.pop(ident, None)
                    dropped += 1
                    continue
                if ident not in results:
                    continue
                outcome, sent = results[ident]
                if outcome in ("sent", "permanent"):
                    data.pop(ident, None)
                    completed.append(ident)
                else:
                    attempts = int(snapshot.get("attempts", 0)) + 1
                    if attempts >= MAX_ATTEMPTS:
                        data.pop(ident, None)
                        dropped += 1
                        continue
                    snapshot = dict(snapshot)
                    snapshot.update(attempts=attempts, last_sent=now,
                                    next_at=now + min(MAX_RETRY_SECONDS, 2 ** min(attempts, 8)), sent=sent)
                    data[ident] = snapshot
        # Delivery history only for the generation actually delivered: a newer pending
        # notification kept above must not be throttled by its predecessor's send.
        if completed:
            with locked_map(_delivery_path()) as data:
                for ident in completed:
                    data.pop(ident, None)
                    data[ident] = {"at": now}
    if attempted or dropped:
        sent_count = sum(outcome == "sent" for outcome, _ in results.values())
        retry_count = sum(outcome == "retry" for outcome, _ in results.values()) - dropped
        codes = ",".join(str(code) for code in sorted(statuses)) or "none"
        errors = ",".join(sorted(exceptions)) or "none"
        print(f"APNs drain sent={sent_count} retry={max(0, retry_count)} dropped={dropped} "
              f"statuses={codes} exceptions={errors}", file=sys.stderr)
    return attempted
