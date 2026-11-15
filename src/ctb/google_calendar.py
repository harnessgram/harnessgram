"""Google Calendar, read only, for the owner's "My schedule" card (schedule.py, docs/panel.md "My schedule").

Setup, once, by whoever runs the bridge: a Web-type OAuth client whose only redirect URI is exactly
`<panel_base_url>/oauth/google/callback` (`ctb schedule google-setup-info` prints it), its id and secret kept as two
Keychain items, and the config section

    "google_calendar": {"client_id_keychain": ["<service>", "<account>"],
                        "client_secret_keychain": ["<service>", "<account>"],
                        "calendars": ["primary"]}

An empty section (the default) hides the button. `token_keychain` ([service, account]) names the item the refresh
token is kept in; it defaults to TOKEN_ITEM.

Sign-in (OAuth 2.0 authorization code with PKCE S256 and a state; the owner taps on the phone, so there is no
loopback):

1. `POST /api/schedule {"action": "google-connect"}` (signed Telegram initData, the fresh login every action needs)
   makes a ticket: random, single use, TICKET_TTL seconds, bound to the owner's id. The page opens
   `<panel_base_url>/oauth/google/start?t=<ticket>` in the phone's browser (Google refuses sign-in in a web view).
2. `GET /oauth/google/start` takes the ticket once and makes the state (256 random bits known only to this process,
   kept as a hash, single use, STATE_TTL seconds, bound to the owner and the connection generation), the PKCE
   verifier (it never leaves this process) and a browser-binding cookie, then redirects to Google with the
   read-only calendar scope, access_type=offline and prompt=consent.
3. `GET /oauth/google/callback` needs that state (once, unexpired) and the cookie of the same browser; only then is
   the code exchanged and the refresh token stored in the Keychain (`security -i`, the value on stdin, never argv).

Reading: the refresh token is turned into an access token (memory only) and events.list is asked for today and
tomorrow on each configured calendar (singleEvents, orderBy=startTime, the owner's time zone). The events are
reused for EVENTS_TTL seconds and a failure is not retried for RETRY_AFTER seconds. Nothing is ever written to
Google. No token, code, state, client id or secret is logged, put on a page or returned by an API.

Disconnect: the Keychain item is deleted, the caches are forgotten, every pending sign-in is void and the refresh
token is revoked at Google (best effort).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from . import schedule

log = logging.getLogger("ctb.google_calendar")

SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{}/events"
TOKEN_ITEM = ("harnessgram.google-calendar", "refresh-token")
START_PATH = "/oauth/google/start"
CALLBACK_PATH = "/oauth/google/callback"
COOKIE = "hg_gc_bind"
COOKIE_PATH = "/oauth/google"
TICKET_TTL = 300     # seconds from the tap to opening the sign-in page
STATE_TTL = 600      # seconds the owner has at Google's consent screen
MAX_PENDING = 8
EVENTS_TTL = 300     # seconds fetched events are reused
RETRY_AFTER = 60     # seconds before a failed fetch is tried again
STATUS_TTL = 30      # seconds the "is there a token" Keychain check is reused
TIMEOUT = 8          # seconds per request to Google
MAX_RESPONSE = 2_000_000
MAX_CALENDARS = 5
MAX_ITEMS = 250      # events taken from one calendar's answer
MAX_SPAN_DAYS = 366
SECTION_KEYS = frozenset({"client_id_keychain", "client_secret_keychain", "calendars", "token_keychain"})
# A Keychain service or account name: no quote, backslash or control character, so it is safe inside `security -i`.
_ITEM = re.compile(r"[A-Za-z0-9._@+:-](?:[A-Za-z0-9._@+: -]{0,126}[A-Za-z0-9._@+:-])?")
_CALENDAR = re.compile(r"[A-Za-z0-9._@#+-]{1,200}")
_SECRET = re.compile(r"[A-Za-z0-9._~+/=:-]{8,4096}")
_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class CalendarError(Exception):
    """code: "setup", "refresh", "fetch", "exchange", "scope", "no_refresh", "stale", "store"."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --- the config section ---------------------------------------------------------------------------------------------

def _item_ok(value) -> bool:
    return isinstance(value, list) and len(value) == 2 and all(isinstance(v, str) and _ITEM.fullmatch(v) for v in value)


def valid_section(value) -> bool:
    if not isinstance(value, dict):
        return False
    if not value:
        return True
    if set(value) - SECTION_KEYS or not _item_ok(value.get("client_id_keychain")) \
            or not _item_ok(value.get("client_secret_keychain")):
        return False
    if "token_keychain" in value and not _item_ok(value["token_keychain"]):
        return False
    cals = value.get("calendars", ["primary"])
    return isinstance(cals, list) and 1 <= len(cals) <= MAX_CALENDARS \
        and all(isinstance(c, str) and _CALENDAR.fullmatch(c) for c in cals)


def settings(cfg) -> Optional[dict]:
    """The section in a fixed shape, or None when it is empty or unusable (then the button stays hidden)."""
    raw = getattr(cfg, "google_calendar", None)
    if not raw or not valid_section(raw):
        return None
    return {"client_id": tuple(raw["client_id_keychain"]), "client_secret": tuple(raw["client_secret_keychain"]),
            "token": tuple(raw.get("token_keychain") or TOKEN_ITEM),
            "calendars": list(dict.fromkeys(raw.get("calendars") or ["primary"]))}


def redirect_uri(base: str) -> str:
    return base.rstrip("/") + CALLBACK_PATH


# --- the macOS Keychain ---------------------------------------------------------------------------------------------

class Keychain:
    """Generic passwords, each named by (service, account). A secret reaches `security` on stdin only."""

    def __init__(self, runner: Callable = subprocess.run):
        self.run = runner

    def get(self, item: Tuple[str, str]) -> Optional[str]:
        try:
            r = self.run(["security", "find-generic-password", "-s", item[0], "-a", item[1], "-w"],
                         capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        value = (r.stdout or "").strip()
        return value if r.returncode == 0 and value else None

    def has(self, item: Tuple[str, str]) -> bool:
        """Whether the item exists, without reading its secret."""
        try:
            return self.run(["security", "find-generic-password", "-s", item[0], "-a", item[1]],
                            capture_output=True, timeout=10, stdin=subprocess.DEVNULL).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def set(self, item: Tuple[str, str], value: str) -> bool:
        if not _SECRET.fullmatch(value or "") or not all(_ITEM.fullmatch(v) for v in item):
            return False
        line = f'add-generic-password -U -s "{item[0]}" -a "{item[1]}" -w "{value}"\n'
        try:
            r = self.run(["security", "-i"], input=line, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return False
        return r.returncode == 0 and self.has(item)

    def delete(self, item: Tuple[str, str]) -> None:
        try:
            self.run(["security", "delete-generic-password", "-s", item[0], "-a", item[1]],
                     capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass


def http(method: str, url: str, form: Optional[dict] = None, token: str = "",
         timeout: float = TIMEOUT) -> Tuple[int, dict]:
    """(status, JSON object). Never raises for an HTTP error or a network failure; never logs anything."""
    data, headers = None, {"Accept": "application/json", "User-Agent": "ctb-schedule"}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, code = r.read(MAX_RESPONSE + 1), r.status
    except urllib.error.HTTPError as e:
        raw, code = (e.read(MAX_RESPONSE + 1) or b"{}"), e.code
    except (OSError, ValueError):
        return 0, {}
    if len(raw) > MAX_RESPONSE:
        return 0, {}
    try:
        out = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        out = {}
    return code, out if isinstance(out, dict) else {}


# What the code uses; tests replace both (tests/conftest.py puts a refusing fake in place for every test).
STORE = Keychain()
REQUEST: Callable = http

_LOCK = threading.Lock()        # the caches below
_SAVE_LOCK = threading.Lock()   # storing or deleting the token (a sign-in finishing against a disconnect)
# The connection generation: every sign-in and disconnect moves it on (reset). Every cache entry is keyed by the
# generation it was fetched under, and a token refresh or events request that started under an older one is thrown
# away when it lands, so a slow answer for the account that was just disconnected can never show up for the next one.
_GEN = [0]
_ACCESS: Dict[Tuple[int, Tuple[str, str]], Tuple[float, str]] = {}   # (generation, token item) -> (expires, token)
_EVENTS: Dict[tuple, Tuple[float, Optional[list], float]] = {}        # (generation, ...) -> (fetched, events, retry at)
_STATUS: Dict[Tuple[int, Tuple[str, str]], Tuple[float, bool]] = {}  # (generation, token item) -> (checked, connected)


def reset() -> None:
    """A new connection generation: forget the access token, the events and the connection check (a sign-in or a
    disconnect). Requests still in flight from before are dropped when they land."""
    with _LOCK:
        _GEN[0] += 1
        _ACCESS.clear()
        _EVENTS.clear()
        _STATUS.clear()


def generation() -> int:
    with _LOCK:
        return _GEN[0]


def connected(cfg, now: Optional[float] = None) -> bool:
    s = settings(cfg)
    if not s:
        return False
    now = time.time() if now is None else now
    gen = generation()
    with _LOCK:
        hit = _STATUS.get((gen, s["token"]))
    if hit and now - hit[0] < STATUS_TTL:
        return hit[1]
    on = STORE.has(s["token"])
    with _LOCK:
        if _GEN[0] == gen:
            _STATUS[(gen, s["token"])] = (now, on)
    return on


def status(cfg) -> dict:
    """What the card needs: is the button set up, and is an account connected. Never a secret."""
    s = settings(cfg)
    return {"configured": bool(s and getattr(cfg, "panel_base_url", "")), "connected": bool(s) and connected(cfg)}


def _client(s: dict) -> dict:
    cid, secret = STORE.get(s["client_id"]), STORE.get(s["client_secret"])
    if not cid or not secret:
        raise CalendarError("setup")
    return {"client_id": cid, "client_secret": secret}


# --- signing in -----------------------------------------------------------------------------------------------------

def _token_ok(value: object) -> bool:
    return isinstance(value, str) and 20 <= len(value) <= 100 and set(value) <= _TOKEN_CHARS


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def pkce() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class Flow:
    """The pending sign-ins of one panel process (memory only; a restart forgets them). Every ticket and state carries
    the connection generation it was made under; a disconnect moves the generation on, so they are all void."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.generation = 0
        self.tickets: Dict[str, Tuple[float, int, int]] = {}             # digest -> (expires, owner, generation)
        self.states: Dict[str, Tuple[float, str, str, int, int]] = {}   # digest -> (expires, verifier, cookie, owner, gen)

    def _prune(self) -> None:
        now = self.clock()
        for table in (self.tickets, self.states):
            for k in [k for k, v in table.items() if v[0] <= now]:
                table.pop(k, None)

    def clear(self) -> None:
        with self.lock:
            self.generation += 1
            self.tickets.clear()
            self.states.clear()

    def current(self, gen: int) -> bool:
        with self.lock:
            return gen == self.generation

    def ticket(self, owner: int) -> str:
        with self.lock:
            self._prune()
            if len(self.tickets) >= MAX_PENDING:
                self.tickets.pop(next(iter(self.tickets)))
            t = secrets.token_urlsafe(32)
            self.tickets[_digest(t)] = (self.clock() + TICKET_TTL, owner, self.generation)
            return t

    def take_ticket(self, t: object) -> Optional[Tuple[int, int]]:
        """(owner, generation), once, for a known unexpired ticket of the current generation; else None."""
        if not _token_ok(t):
            return None
        with self.lock:
            self._prune()
            got = self.tickets.pop(_digest(t), None)
            if not got or got[2] != self.generation:
                return None
        return got[1], got[2]

    def begin(self, owner: int, gen: int) -> Tuple[str, str, str]:
        """(state, PKCE challenge, cookie value) for one sign-in; the verifier stays here."""
        verifier, challenge = pkce()
        state, cookie = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.lock:
            self._prune()
            if len(self.states) >= MAX_PENDING:
                self.states.pop(next(iter(self.states)))
            self.states[_digest(state)] = (self.clock() + STATE_TTL, verifier, _digest(cookie), owner, gen)
        return state, challenge, cookie

    def finish(self, state: object, cookie: object) -> Optional[Tuple[str, int, int]]:
        """(verifier, owner, generation), once, for a known unexpired state whose browser cookie matches; else None.
        A state is used up by its first callback, whatever the outcome."""
        if not _token_ok(state):
            return None
        with self.lock:
            self._prune()
            got = self.states.pop(_digest(state), None)
        if not got or not _token_ok(cookie) or not hmac.compare_digest(got[2], _digest(cookie)):
            return None
        return got[1], got[3], got[4]


def cookie_of(header: Optional[str]) -> str:
    """The binding cookie's value from a Cookie header ("" when missing or repeated)."""
    found = []
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            found.append(value.strip())
    return found[0] if len(found) == 1 else ""


def set_cookie(value: str, max_age: int = STATE_TTL) -> str:
    return f"{COOKIE}={value}; Max-Age={max_age}; Path={COOKIE_PATH}; Secure; HttpOnly; SameSite=Lax"


def google_url(client_id: str, base: str, state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri(base), "response_type": "code", "scope": SCOPE,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent", "include_granted_scopes": "false"})


def start_location(cfg, state: str, challenge: str) -> str:
    """Where /oauth/google/start sends the browser. Only the client id is read (it is public in this URL anyway)."""
    s = settings(cfg)
    if not s or not getattr(cfg, "panel_base_url", ""):
        raise CalendarError("setup")
    cid = STORE.get(s["client_id"])
    if not cid:
        raise CalendarError("setup")
    return google_url(cid, cfg.panel_base_url, state, challenge)


def revoke(token: str) -> bool:
    try:
        return REQUEST("POST", REVOKE_URL, form={"token": token})[0] == 200
    except Exception:
        return False


def complete(cfg, flow: Flow, code: str, verifier: str, gen: int) -> None:
    """Exchange the code and keep the refresh token. CalendarError on any failure; nothing is stored then."""
    s = settings(cfg)
    if not s or not getattr(cfg, "panel_base_url", ""):
        raise CalendarError("setup")
    client = _client(s)
    status_code, out = REQUEST("POST", TOKEN_URL, form={
        "code": code, "client_id": client["client_id"], "client_secret": client["client_secret"],
        "redirect_uri": redirect_uri(cfg.panel_base_url), "grant_type": "authorization_code", "code_verifier": verifier})
    if status_code != 200 or not isinstance(out.get("access_token"), str):
        raise CalendarError("exchange")
    if SCOPE not in set(str(out.get("scope") or "").split()):
        raise CalendarError("scope")
    refresh = out.get("refresh_token")
    if not isinstance(refresh, str) or not _SECRET.fullmatch(refresh):
        raise CalendarError("no_refresh")
    with _SAVE_LOCK:
        if not flow.current(gen):  # a disconnect came while the code was being exchanged: it wins
            stale = True
        else:
            stale = False
            if not STORE.set(s["token"], refresh):
                raise CalendarError("store")
    if stale:
        revoke(refresh)
        raise CalendarError("stale")
    reset()
    flow.clear()  # a new connection: every other pending sign-in is void


def disconnect(cfg, flow: Flow) -> bool:
    """Delete the token and revoke it at Google. True when it is gone here (the revoke is best effort)."""
    s = settings(cfg)
    if not s:
        raise CalendarError("setup")
    flow.clear()
    with _SAVE_LOCK:
        token = STORE.get(s["token"])
        STORE.delete(s["token"])
        gone = not STORE.has(s["token"])
    reset()
    if token:
        revoke(token)
    return gone


# --- reading events -------------------------------------------------------------------------------------------------

def _access_token(s: dict, now: float, gen: int) -> str:
    """An access token for generation `gen`; CalendarError("stale") when a sign-in or disconnect came meanwhile."""
    with _LOCK:
        hit = _ACCESS.get((gen, s["token"]))
    if hit and now < hit[0]:
        return hit[1]
    refresh = STORE.get(s["token"])
    if not refresh:
        raise CalendarError("refresh")
    client = _client(s)
    code, out = REQUEST("POST", TOKEN_URL, form={
        "refresh_token": refresh, "client_id": client["client_id"], "client_secret": client["client_secret"],
        "grant_type": "refresh_token"})
    access = out.get("access_token")
    if code != 200 or not isinstance(access, str) or not access:
        raise CalendarError("refresh")
    life = out.get("expires_in")
    life = life if isinstance(life, int) and not isinstance(life, bool) and life > 0 else 3600
    with _LOCK:
        if _GEN[0] != gen:  # the account changed while Google answered: never keep or use this token
            raise CalendarError("stale")
        _ACCESS[(gen, s["token"])] = (now + max(30, life - 60), access)
    return access


def _rfc3339(when: date, tz) -> str:
    return datetime.combine(when, datetime.min.time(), tzinfo=tz).isoformat()


def _title(value) -> str:
    if not isinstance(value, str):
        return "—"
    return re.sub(r"\s+", " ", _CONTROL.sub(" ", value)).strip()[:schedule.MAX_TITLE] or "—"


def _moment(value: str, zone_name, tz) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        zone = tz
        if isinstance(zone_name, str) and schedule.valid_timezone(zone_name):
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(zone_name)
        moment = moment.replace(tzinfo=zone)
    return moment


def map_events(items, cfg, first: date, last: date) -> List[schedule.Event]:
    """Google events -> the card's events on first..last in the owner's zone. A cancelled or unreadable event is
    skipped; an all-day event appears on every day it covers (Google's end date is exclusive); a timed event appears
    on the day it starts, in the owner's zone."""
    tz = schedule.zone(cfg)
    out: List[schedule.Event] = []
    for it in (items if isinstance(items, list) else [])[:MAX_ITEMS]:
        if not isinstance(it, dict) or it.get("status") == "cancelled":
            continue
        start, end = it.get("start"), it.get("end")
        if not isinstance(start, dict):
            continue
        title = _title(it.get("summary"))
        try:
            if isinstance(start.get("date"), str):
                d0 = date.fromisoformat(start["date"])
                d1 = date.fromisoformat(end["date"]) if isinstance(end, dict) and isinstance(end.get("date"), str) \
                    else d0 + timedelta(days=1)
                if d1 <= d0:
                    d1 = d0 + timedelta(days=1)
                if (d1 - d0).days > MAX_SPAN_DAYS:
                    continue
                day = max(d0, first)
                while day < d1 and day <= last:
                    out.append(schedule.Event(title, day.isoformat(), None))
                    day += timedelta(days=1)
            elif isinstance(start.get("dateTime"), str):
                local = _moment(start["dateTime"], start.get("timeZone"), tz).astimezone(tz)
                if first <= local.date() <= last:
                    out.append(schedule.Event(title, local.date().isoformat(), local.strftime("%H:%M")))
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    return out


def _fetch(cfg, s: dict, first: date, last: date, now: float, gen: int) -> List[schedule.Event]:
    tz = schedule.zone(cfg)
    tz_name = getattr(cfg, "timezone", "") or schedule.DEFAULT_ZONE
    params = urllib.parse.urlencode({
        "timeMin": _rfc3339(first, tz), "timeMax": _rfc3339(last + timedelta(days=1), tz), "singleEvents": "true",
        "orderBy": "startTime", "timeZone": tz_name, "maxResults": str(MAX_ITEMS),
        "fields": "items(status,summary,start,end)"})
    token = _access_token(s, now, gen)
    found: List[schedule.Event] = []
    for cal in s["calendars"]:
        code, out = REQUEST("GET", EVENTS_URL.format(urllib.parse.quote(cal, safe="")) + "?" + params, token=token)
        if code == 401:
            with _LOCK:
                _ACCESS.pop((gen, s["token"]), None)
        if code != 200 or not isinstance(out.get("items"), list):
            raise CalendarError("fetch")
        found.extend(map_events(out["items"], cfg, first, last))
    return found


class GoogleSource(schedule.ScheduleSource):
    """The owner's Google calendars, read only."""
    kind = "google"

    def __init__(self, s: dict):
        self._s = s

    @property
    def budget(self) -> float:
        """Seconds a first fetch may take: one token refresh plus one request per calendar."""
        return TIMEOUT * (1 + len(self._s["calendars"]))

    def events(self, cfg, first: date, last: date, now: Optional[float] = None) -> List[schedule.Event]:
        now = time.time() if now is None else now
        gen = generation()
        key = (gen, self._s["token"], tuple(self._s["calendars"]), first.isoformat(), last.isoformat(),
               getattr(cfg, "timezone", ""))
        with _LOCK:
            fetched, cached, retry_at = _EVENTS.get(key, (0.0, None, 0.0))
        if cached is not None and now - fetched < EVENTS_TTL:
            return list(cached)
        if now < retry_at:
            raise CalendarError("fetch")
        try:
            fresh = _fetch(cfg, self._s, first, last, now, gen)
        except Exception as exc:
            log.warning("google calendar unavailable (%s)", exc.code if isinstance(exc, CalendarError) else type(exc).__name__)
            with _LOCK:
                if _GEN[0] == gen:
                    _EVENTS[key] = (fetched, None, now + RETRY_AFTER)
            raise CalendarError("fetch") from None
        with _LOCK:
            if _GEN[0] != gen:  # fetched for an account that was disconnected meanwhile: neither kept nor shown
                stale = True
            else:
                stale = False
                _EVENTS[key] = (now, fresh, 0.0)
        if stale:
            log.warning("google calendar unavailable (stale)")
            raise CalendarError("fetch")
        return list(fresh)


def source(cfg) -> Optional[GoogleSource]:
    """The Google source when the section is set and an account is connected; otherwise None."""
    s = settings(cfg)
    return GoogleSource(s) if s and connected(cfg) else None


# --- `ctb schedule google-setup-info` -------------------------------------------------------------------------------

CLIENT_ACCOUNTS = ("client-id", "client-secret")  # the accounts the client's two Keychain items use by convention


def config_line(service: str = "<service>") -> str:
    """The config entry that points at a client kept under `service` (accounts CLIENT_ACCOUNTS)."""
    return '"google_calendar": ' + json.dumps({"client_id_keychain": [service, CLIENT_ACCOUNTS[0]],
                                              "client_secret_keychain": [service, CLIENT_ACCOUNTS[1]],
                                              "calendars": ["primary"]})


def setup_info(cfg, service: Optional[str] = None) -> List[str]:
    """What to register with Google, the config line to add when the section is missing, and what is still missing.
    Reads no secret: Keychain items are only checked for existence. `service` is the Keychain service the client's
    items are kept under (accounts CLIENT_ACCOUNTS); without it the line carries a placeholder."""
    if service is not None and not _ITEM.fullmatch(service):
        raise CalendarError("setup")
    base = getattr(cfg, "panel_base_url", "")
    s = settings(cfg)
    lines = ["Authorized redirect URI: " + (redirect_uri(base) if base else "<panel_base_url>" + CALLBACK_PATH),
             "Scope: " + SCOPE]
    if not base:
        lines.append("Missing: panel_base_url (the Cockpit's public https origin)")
    if not s:
        lines.append("Add to the config: " + config_line(service or "<service>"))
        return lines
    missing = [name for name, item in (("client id", s["client_id"]), ("client secret", s["client_secret"]))
               if not STORE.has(item)]
    if missing:
        lines.append("Missing in the Keychain: " + ", ".join(missing))
    lines.append("Connected: " + ("yes" if STORE.has(s["token"]) else "no"))
    return lines
