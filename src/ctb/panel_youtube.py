"""The Cockpit's YouTube staff radio controls: "Connect with YouTube" (a web sign-in that returns to the Cockpit's
own public address), the feature switch, the project picker, and Start / Stop / Disconnect.

Sign-in (OAuth 2.0 authorization code + PKCE + state, a Web-type client kept in the Keychain):

1. The owner taps "Connect" in the Cockpit: `POST /api/youtube {"action": "connect"}` (signed Telegram initData,
   the fresh login an action needs). The server makes a ticket: random, single-use, valid TICKET_TTL seconds,
   bound to the owner's Telegram id. The page opens `<panel_base_url>/oauth/youtube/start?t=<ticket>` in the
   phone's browser (Google refuses sign-in inside an in-app web view).
2. `GET /oauth/youtube/start` takes the ticket (once), makes the OAuth state and the PKCE verifier (both kept
   here, in memory only), sets a browser-binding cookie (random, HttpOnly, Secure, SameSite=Lax, path
   /oauth/youtube) whose hash is stored with the state, and redirects to Google.
3. `GET /oauth/youtube/callback` needs the state (once, unexpired) AND the cookie of the browser that took the
   ticket; only then is the code exchanged (with the verifier, which never left this server) and the refresh
   token stored in radio_youtube's Keychain item. A callback opened in any other browser, replayed, or
   carrying someone else's code with a state this server never issued is refused, so nobody without the
   owner's Cockpit login can bind a Google account here.

The redirect URI is always `<panel_base_url>/oauth/youtube/callback` from the config, never the request's
Host. Every reply here is no-store and no-referrer; no ticket, state, code or token is ever logged. Signing in
creates nothing on YouTube (the channel is only read).

Going live is a publication: `start` needs two taps, the second carrying a short-lived single-use token the
first one returned (`arm`), and both come only from the owner's signed Cockpit request.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import secrets
import threading
import time
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urlencode

from . import radio_youtube as Y

TICKET_TTL = 300      # seconds from the tap to opening the sign-in page
STATE_TTL = 600       # seconds the owner has at Google's consent screen
ARM_TTL = 60          # seconds between the two taps of Start
MAX_PENDING = 8
COOKIE = "hg_yt_bind"
CALLBACK_PATH = "/oauth/youtube/callback"
START_PATH = "/oauth/youtube/start"
TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _token_ok(value: object) -> bool:
    return isinstance(value, str) and 20 <= len(value) <= 100 and set(value) <= TOKEN_CHARS


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Flow:
    """The pending sign-ins and Start taps of one panel process (memory only; a restart forgets them)."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.lock = threading.Lock()
        # every entry records the connection generation (radio_youtube.generation) it was issued under
        self.tickets: Dict[str, Tuple[float, int, int]] = {}            # digest -> (expires, owner id, generation)
        self.states: Dict[str, Tuple[float, str, str, int, int]] = {}  # digest -> (expires, verifier, cookie digest, owner, generation)
        self.armed: Dict[str, Tuple[float, int, str]] = {}              # digest -> (expires, generation, channel id)

    def _prune(self) -> None:
        now = self.clock()
        for table in (self.tickets, self.states, self.armed):
            for k in [k for k, v in table.items() if v[0] <= now]:
                table.pop(k, None)

    def clear(self) -> None:
        """Void every pending sign-in and Start confirmation (a disconnect, or a finished sign-in)."""
        with self.lock:
            self.tickets.clear()
            self.states.clear()
            self.armed.clear()

    def ticket(self, owner: int, gen: int) -> str:
        with self.lock:
            self._prune()
            if len(self.tickets) >= MAX_PENDING:
                self.tickets.pop(next(iter(self.tickets)))
            t = secrets.token_urlsafe(32)
            self.tickets[_digest(t)] = (self.clock() + TICKET_TTL, owner, gen)
            return t

    def take_ticket(self, t: object) -> Optional[Tuple[int, int]]:
        """(owner id, generation) the ticket was issued with, once; None when unknown, used or expired."""
        if not _token_ok(t):
            return None
        with self.lock:
            self._prune()
            got = self.tickets.pop(_digest(t), None)
        return (got[1], got[2]) if got else None

    def begin(self, owner: int, gen: int) -> Tuple[str, str, str]:
        """(state, PKCE challenge, cookie value) for one sign-in; the verifier stays here."""
        verifier, challenge = Y.pkce()
        state, cookie = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.lock:
            self._prune()
            if len(self.states) >= MAX_PENDING:
                self.states.pop(next(iter(self.states)))
            self.states[_digest(state)] = (self.clock() + STATE_TTL, verifier, _digest(cookie), owner, gen)
        return state, challenge, cookie

    def finish(self, state: object, cookie: object) -> Optional[Tuple[str, int]]:
        """(PKCE verifier, generation), once, for a known unexpired state whose browser cookie matches; else None.
        A state is used up by its first callback, whatever the outcome."""
        if not _token_ok(state):
            return None
        with self.lock:
            self._prune()
            got = self.states.pop(_digest(state), None)
        if not got or not _token_ok(cookie) or not hmac.compare_digest(got[2], _digest(cookie)):
            return None
        return got[1], got[4]

    def arm(self, gen: int, channel_id: str) -> str:
        """The first tap of Start, bound to the connection generation and the channel the owner was shown."""
        with self.lock:
            self._prune()
            self.armed.clear()  # only the latest first tap counts
            t = secrets.token_urlsafe(24)
            self.armed[_digest(t)] = (self.clock() + ARM_TTL, gen, channel_id)
            return t

    def fire(self, t: object) -> Optional[Tuple[int, str]]:
        """(generation, channel id) the first tap confirmed, once; None when unknown, used or expired."""
        if not _token_ok(t):
            return None
        with self.lock:
            self._prune()
            got = self.armed.pop(_digest(t), None)
        return (got[1], got[2]) if got else None


def redirect_uri(base: str) -> str:
    return base.rstrip("/") + CALLBACK_PATH


def cookie_of(header: Optional[str]) -> str:
    """The binding cookie's value from a Cookie header ("" when missing or repeated)."""
    found = []
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            found.append(value.strip())
    return found[0] if len(found) == 1 else ""


def set_cookie(value: str, max_age: int = STATE_TTL) -> str:
    return f"{COOKIE}={value}; Max-Age={max_age}; Path=/oauth/youtube; Secure; HttpOnly; SameSite=Lax"


PAGE_CSP = "default-src 'none'; style-src 'nonce-{n}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"

from .i18n import YOUTUBE_DONE as _DONE
from .i18n import YOUTUBE_FAILED as _FAILED


def page(lang: str, ok: bool, nonce: str) -> bytes:
    """The small page the browser shows after the callback: plain text, fixed wording, no script."""
    lang = "he" if lang == "he" else "en"
    title, line1, line2 = (_DONE if ok else _FAILED)[lang]
    direction = "rtl" if lang == "he" else "ltr"
    return (f'<!doctype html><html lang="{lang}" dir="{direction}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">'
            f'<title>{html.escape(title)}</title><style nonce="{nonce}">body{{font:17px/1.5 -apple-system,system-ui,sans-serif;'
            'margin:0;padding:48px 20px;background:#f6f7fb;color:#111}main{max-width:420px;margin-inline:auto;'
            'background:#fff;border-radius:16px;padding:24px;box-shadow:0 1px 3px rgba(0,0,0,.08)}h1{font-size:22px;'
            'margin-block:0 8px}p{margin-block:6px}</style></head><body><main>'
            f'<h1>{html.escape(title)}</h1><p>{html.escape(line1)}</p><p>{html.escape(line2)}</p></main></body></html>'
            ).encode("utf-8")


def google_url(client_id: str, base: str, state: str, challenge: str) -> str:
    return Y.AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri(base), "response_type": "code", "scope": Y.SCOPE,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent select_account", "include_granted_scopes": "false"})
