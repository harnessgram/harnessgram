"""The staff edition of the team radio on YouTube: an unlisted live stream the owner can share with his staff.

    harnessgram radio youtube status [--live] | connect | start | stop | disconnect

Off by default (`radio.youtube.enabled`). It is a second, independent radio, not a second output of the owner's:
its own launchd job (com.claude-telegram-bridge.radio-youtube), its own script and card (radio_staff: an
allowlist over the team's state, deny by default), its own encoder and RTMP connection, its own folder
(<state>/radio-youtube). A failure here never touches the Telegram radio, and the Telegram radio never starts
this one.

The bridge's off switch (policy.on, `"mirror": false`) is read again each round: while off nothing is created
or streamed, a live broadcast is ended, and turning the bridge on again never resumes it by itself.

Going live is a publication, so only an explicit owner action reaches it: `start` (the CLI, or a Cockpit button
that runs the same function) creates the broadcast and loads the job. Nothing else creates a broadcast; the job
itself refuses to stream unless `start` recorded an owner start for the broadcast it is about to feed.

Credentials: `connect` runs Google's installed-app OAuth (loopback redirect + PKCE) with the OAuth client the
`client_command` prints (for example the Desktop client the youtube-kit tools already use). The refresh token,
the ingest URL and the stream key are kept in the macOS Keychain (`keychain_service`), written through
`security -i` on stdin so they never appear on a command line, and read into memory only. No file, log or
status output holds any of them.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import plistlib
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import launch, radio, radio_card, radio_script, radio_staff, radio_voice
from .config import Config, HOME, private_dir, state_dir, write_private

LABEL = "com.claude-telegram-bridge.radio-youtube"
MARKER = "ctb-radio-youtube"
SERVICE = "harnessgram.radio-youtube"
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/youtube/v3"
STREAM_TITLE = "Harnessgram staff radio"   # the reusable ingest stream this radio owns (its title marks it)
MAX_HOURS = 11            # YouTube archives a stream only when it is shorter than 12 hours
POLL = 30
MAX_FAILURES = 5
FAILURE_WINDOW = 900
CONNECT_TIMEOUT = 600
_SAFE_SECRET = re.compile(r"[A-Za-z0-9._~+/=:-]{8,4096}")
# the Keychain accounts under SERVICE; REFRESH_CLIENT: the id of the OAuth client that issued the refresh token
REFRESH, INGEST_URL, STREAM_KEY = "refresh-token", "ingest-url", "stream-key"
REFRESH_CLIENT = "refresh-client-id"


class YouTubeError(Exception):
    """A user-facing failure (an i18n key in .key); never carries a secret or an API body."""

    def __init__(self, key: str, **kw):
        super().__init__(key)
        self.key, self.kw = key, kw


# --- settings and files ------------------------------------------------------------------------------

def settings(cfg: Config) -> dict:
    base = radio.settings(cfg)
    raw = (getattr(cfg, "radio", None) or {}).get("youtube")
    raw = raw if isinstance(raw, dict) else {}
    s = radio_staff.profile(raw, base["lang"])
    client = raw.get("client_command")
    s["client_command"] = client if isinstance(client, list) and all(isinstance(a, str) for a in client) else []
    svc = raw.get("keychain_service")
    s["keychain_service"] = svc if isinstance(svc, str) and re.fullmatch(r"[\w.-]{3,80}", svc) else SERVICE
    hours = raw.get("max_hours")
    s["max_hours"] = float(hours) if isinstance(hours, (int, float)) and not isinstance(hours, bool) and \
        0.1 <= hours <= MAX_HOURS else float(MAX_HOURS)
    for k in ("bed", "bed_db", "video_encoder", "say_voice", "path", "ledger_dir"):
        s[k] = base[k]
    # The Cockpit's own choices (its "enabled" switch and project picker) override the config file's; they are
    # read here every time, so the job's every-round reload (StaffStation.refresh) follows a tap at once.
    prefs = cockpit_prefs()
    if isinstance(prefs.get("enabled"), bool):
        s["enabled"] = prefs["enabled"]
    if isinstance(prefs.get("projects"), list):
        names = dict(base["project_names"])
        names.update(raw.get("projects") if isinstance(raw.get("projects"), dict) else {})
        picked = [k for k in prefs["projects"] if isinstance(k, str) and k in catalog_keys(s, names)]
        s["projects"] = {k: names.get(k) or k for k in picked[:radio_staff.MAX_PROJECTS]}
    return s


PREFS = "cockpit.json"   # in folder(): {"enabled": bool, "projects": [ledger keys]}, written by the Cockpit only


def cockpit_prefs() -> dict:
    data = _read(folder() / PREFS)
    return data if isinstance(data, dict) else {}


def set_cockpit_prefs(**kw) -> dict:
    """Save the Cockpit's switch or project picks (the only writer of cockpit.json). Read again and changed
    under the connection lock, only the fields asked for, so two saves never undo each other."""
    with connection_lock():
        prefs = cockpit_prefs()
        if "enabled" in kw:
            prefs["enabled"] = bool(kw["enabled"])
        if "projects" in kw:
            prefs["projects"] = [k for k in kw["projects"] if isinstance(k, str)][:radio_staff.MAX_PROJECTS]
        _write(folder() / PREFS, prefs)
        return prefs


# --- the connection: who is signed in, under which generation ----------------------------------------------
# One lock (a file lock: the panel, the CLI and the job are separate processes) covers every change of the
# connection and of the Cockpit's choices: a finished sign-in, disconnect, a settings save, and Start's last
# check before it creates a broadcast. The generation counts every successful sign-in and every disconnect;
# a confirmation or a sign-in issued under an older generation is refused.

CONN = "connection.json"   # {generation, connected, channel_id, channel_title, live_enabled, connected_at}
LOCK_WAIT = 10.0


@contextmanager
def connection_lock(wait: float = LOCK_WAIT):
    fd = os.open(str(folder() / "connection.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.time() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise YouTubeError("youtube_busy") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


LEGACY = ("connected", "channel_id", "channel_title", "live_enabled")  # an older version kept these in state.json


def connection() -> dict:
    """The connection record. An older version kept it in state.json: read from there only while connection.json
    does not exist yet. When both exist and say different things (a downgrade and upgrade), neither is
    believed: not connected until the next sign-in (which writes connection.json and clears the old fields)."""
    st = _state()
    old = {k: st[k] for k in LEGACY if k in st}
    data = _read(folder() / CONN)
    if not data:
        data = old
    elif old and any(old.get(k) != data.get(k) for k in old):
        data = dict(data, connected=False, channel_id="", channel_title="", conflict=True)
    gen = data.get("generation")
    data["generation"] = gen if isinstance(gen, int) and not isinstance(gen, bool) and gen >= 0 else 0
    return data


def snapshot() -> dict:
    """One locked read of the connection: what the owner is shown and what a confirmation is bound to come from
    this same read."""
    with connection_lock():
        return connection()


def generation() -> int:
    return connection()["generation"]


def _set_connection(**kw) -> dict:
    """Change the connection record. The caller holds connection_lock()."""
    data = connection()
    data.update(kw)
    _write(folder() / CONN, data)
    return data


def bump_generation() -> int:
    """A new generation: every pending sign-in and Start confirmation issued before it is void."""
    with connection_lock():
        return _set_connection(generation=generation() + 1)["generation"]


def catalog_keys(s: dict, names: Dict[str, str]) -> List[str]:
    """The projects the staff edition could name: the ledger's project files, plus any project named in the
    settings. Keys only; the ledger's contents are not read here."""
    keys = list(names)
    folder_ = Path(os.path.expanduser(s["ledger_dir"])) if s.get("ledger_dir") else None
    if folder_ is not None and folder_.is_dir():
        keys += [p.stem for p in sorted(folder_.glob("*.json"))]
    return [k for k in dict.fromkeys(keys) if re.fullmatch(r"[\w.-]{1,64}", k)][:50]


def catalog(cfg: Config) -> List[dict]:
    """[{key, name, speakable, on}] for the Cockpit's project picker. `speakable`: the staff name passes the
    staff edition's language gate (a name that does not is skipped on air, so the picker says so)."""
    s = settings(cfg)
    base = radio.settings(cfg)
    raw = (getattr(cfg, "radio", None) or {}).get("youtube")
    names = dict(base["project_names"])
    names.update(raw.get("projects") if isinstance(raw, dict) and isinstance(raw.get("projects"), dict) else {})
    return [{"key": k, "name": names.get(k) or k, "speakable": bool(radio_staff.gate(names.get(k) or k, s["lang"], 40)),
             "on": k in s["projects"]} for k in catalog_keys(s, names)]


def folder() -> Path:
    return private_dir(state_dir() / "radio-youtube")


def _read(path: Path) -> dict:
    return radio._read(path)


def _write(path: Path, data: dict) -> None:
    write_private(path, json.dumps(data, ensure_ascii=False))


def log(line: str) -> None:
    """One short line in radio-youtube.log. Callers never pass a URL, a key, a token or an API body."""
    path = folder() / "radio-youtube.log"
    try:
        if path.exists() and path.stat().st_size > 200_000:
            path.replace(path.with_suffix(".log.1"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
        os.chmod(path, 0o600)
    except OSError:
        pass


@contextmanager
def _locked():
    """One start or stop at a time (the CLI and a Cockpit button may race)."""
    fd = os.open(str(folder() / "control.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# --- secrets (macOS Keychain) ------------------------------------------------------------------------

class KeychainStore:
    """Generic passwords under one service. Values reach `security` on stdin only (`security -i`)."""

    def __init__(self, service: str = SERVICE, runner: Callable = subprocess.run):
        self.service, self.run = service, runner

    def get(self, name: str) -> Optional[str]:
        try:
            r = self.run(["security", "find-generic-password", "-s", self.service, "-a", name, "-w"],
                         capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        value = (r.stdout or "").strip()
        return value if r.returncode == 0 and value else None

    def has(self, name: str) -> bool:
        """Whether the item exists, without reading its secret."""
        try:
            return self.run(["security", "find-generic-password", "-s", self.service, "-a", name],
                            capture_output=True, timeout=10, stdin=subprocess.DEVNULL).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def set(self, name: str, value: str) -> None:
        if not _SAFE_SECRET.fullmatch(value or ""):
            raise YouTubeError("youtube_secret_store_failed")
        line = f'add-generic-password -U -s "{self.service}" -a "{name}" -w "{value}"\n'
        try:
            r = self.run(["security", "-i"], input=line, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            raise YouTubeError("youtube_secret_store_failed") from None
        if r.returncode or not self.has(name):
            raise YouTubeError("youtube_secret_store_failed")

    def delete(self, name: str) -> None:
        try:
            self.run(["security", "delete-generic-password", "-s", self.service, "-a", name],
                     capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass


# --- HTTP, OAuth and the YouTube Live API ----------------------------------------------------------

def http(method: str, url: str, body: Optional[dict] = None, form: Optional[dict] = None,
         token: str = "", timeout: float = 20.0) -> Tuple[int, dict]:
    """(status, JSON body). Never raises for an HTTP error; never logs anything."""
    data, headers = None, {"Accept": "application/json"}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read() or b"{}", e.code
    except (OSError, ValueError):
        return 0, {}
    try:
        out = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        out = {}
    return code, out if isinstance(out, dict) else {}


def load_client(command: List[str]) -> dict:
    """{client_id, client_secret} from the configured command's output: a Google client file ({"installed": ..}
    or {"web": ..}) or a flat object. Kept in memory."""
    if not command:
        raise YouTubeError("youtube_no_client")
    try:
        r = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        data = json.loads(r.stdout.decode("utf-8", "replace") or "{}") if r.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        data = {}
    data = data.get("installed") or data.get("web") or data if isinstance(data, dict) else {}
    cid, secret = data.get("client_id"), data.get("client_secret")
    if not isinstance(cid, str) or not cid or not isinstance(secret, str):
        raise YouTubeError("youtube_no_client")
    return {"client_id": cid, "client_secret": secret}


def pkce() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def auth_url(client: dict, redirect: str, state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent select_account"})


class OAuth:
    def __init__(self, client: dict, request: Callable = http):
        self.client, self.request = client, request

    def exchange(self, code: str, verifier: str, redirect: str) -> dict:
        status, out = self.request("POST", TOKEN_URL, form={
            "code": code, "client_id": self.client["client_id"], "client_secret": self.client["client_secret"],
            "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier})
        if status != 200 or not out.get("access_token"):
            raise YouTubeError("youtube_connect_failed")
        granted = set(str(out.get("scope") or "").split())
        if SCOPE not in granted and "https://www.googleapis.com/auth/youtube" not in granted:
            raise YouTubeError("youtube_scope_missing")
        return out

    def refresh(self, refresh_token: str) -> str:
        status, out = self.request("POST", TOKEN_URL, form={
            "refresh_token": refresh_token, "client_id": self.client["client_id"],
            "client_secret": self.client["client_secret"], "grant_type": "refresh_token"})
        if status != 200 or not out.get("access_token"):
            raise YouTubeError("youtube_reconnect")
        return out["access_token"]

    def revoke(self, token: str) -> bool:
        return revoke_token(token, self.request)


def revoke_token(token: str, request: Callable = http) -> bool:
    """Revoke a token at Google. The request carries the token only: no client is needed (or loaded)."""
    try:
        return request("POST", REVOKE_URL, form={"token": token})[0] == 200
    except Exception:
        return False


def _reason(out: dict) -> str:
    try:
        errors = out["error"]["errors"]
        return str(errors[0].get("reason") or "") if errors else ""
    except (KeyError, TypeError, AttributeError, IndexError):
        return ""


class YouTube:
    """The few Live API calls the staff radio makes. Each costs 1 quota unit (Data API v3 quota table); a start is about 6 units, a stop 2 to 3, of the default 10,000 a day."""

    def __init__(self, token: Callable[[], str], request: Callable = http):
        self.token, self.request = token, request

    def _call(self, method: str, path: str, params: dict, body: Optional[dict] = None) -> dict:
        status, out = self.request(method, API + path + "?" + urllib.parse.urlencode(params), body=body,
                                   token=self.token())
        if status in (200, 204):
            return out
        reason = _reason(out)
        if reason == "liveStreamingNotEnabled":
            raise YouTubeError("youtube_not_enabled")
        if status in (401,):
            raise YouTubeError("youtube_reconnect")
        raise YouTubeError("youtube_api_failed", why=(reason or str(status))[:40])

    def my_channel(self) -> dict:
        items = self._call("GET", "/channels", {"part": "snippet", "mine": "true"}).get("items") or []
        if not items:
            raise YouTubeError("youtube_no_channel")
        return {"id": items[0].get("id") or "", "title": (items[0].get("snippet") or {}).get("title") or ""}

    def live_enabled(self) -> bool:
        """Read-only: listing the channel's broadcasts fails with liveStreamingNotEnabled when live is off."""
        try:
            self._call("GET", "/liveBroadcasts", {"part": "id", "mine": "true", "maxResults": "1"})
        except YouTubeError as e:
            if e.key == "youtube_not_enabled":
                return False
            raise
        return True

    def find_stream(self) -> Optional[dict]:
        out = self._call("GET", "/liveStreams", {"part": "id,snippet,cdn,contentDetails", "mine": "true",
                                                 "maxResults": "50"})
        for item in out.get("items") or []:
            if (item.get("snippet") or {}).get("title") == STREAM_TITLE and \
                    (item.get("contentDetails") or {}).get("isReusable") is True:
                return item
        return None

    def insert_stream(self) -> dict:
        return self._call("POST", "/liveStreams", {"part": "snippet,cdn,contentDetails,status"}, {
            "snippet": {"title": STREAM_TITLE},
            "cdn": {"ingestionType": "rtmp", "resolution": "variable", "frameRate": "variable"},
            "contentDetails": {"isReusable": True}})

    def insert_broadcast(self, title: str) -> dict:
        start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._call("POST", "/liveBroadcasts", {"part": "snippet,status,contentDetails"}, {
            "snippet": {"title": title, "scheduledStartTime": start},
            "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False},
            # only fields liveBroadcasts.insert documents as settable; latency stays YouTube's default (normal).
            # No monitor stream: with it on, YouTube requires a testing step before live, and autoStart is used.
            "contentDetails": {"enableAutoStart": True, "enableAutoStop": True, "enableDvr": True,
                               "recordFromStart": True, "monitorStream": {"enableMonitorStream": False}}})

    def bind(self, broadcast: str, stream: str) -> dict:
        return self._call("POST", "/liveBroadcasts/bind", {"id": broadcast, "streamId": stream, "part": "id,status"})

    def life_cycle(self, broadcast: str) -> str:
        items = self._call("GET", "/liveBroadcasts", {"part": "status", "id": broadcast}).get("items") or []
        return str((items[0].get("status") or {}).get("lifeCycleStatus") or "") if items else "gone"

    def transition(self, broadcast: str, to: str) -> dict:
        return self._call("POST", "/liveBroadcasts/transition", {"broadcastStatus": to, "id": broadcast,
                                                                 "part": "status"})

    def delete_broadcast(self, broadcast: str) -> None:
        self._call("DELETE", "/liveBroadcasts", {"id": broadcast})


def ingest(stream: dict) -> Tuple[str, str]:
    """(RTMPS server URL, stream key) of a liveStream resource; RTMPS preferred."""
    info = (stream.get("cdn") or {}).get("ingestionInfo") or {}
    url = info.get("rtmpsIngestionAddress") or info.get("ingestionAddress") or ""
    key = info.get("streamName") or ""
    if not isinstance(url, str) or not url.startswith(("rtmps://", "rtmp://")) or not isinstance(key, str) or not key:
        raise YouTubeError("youtube_api_failed", why="no ingest address")
    return url, key


class Service:
    """Everything with credentials in one place, so tests swap the store and the HTTP layer for fakes."""

    def __init__(self, s: dict, store=None, request: Callable = http, client: Optional[dict] = None):
        self.s = s
        self.store = store or KeychainStore(s["keychain_service"])
        self.request = request
        self._given = client   # a client the caller holds (the Cockpit's Web client): one candidate, not an override
        self._client: Optional[dict] = None
        self._token: Tuple[str, float] = ("", 0.0)

    def client(self) -> dict:
        """The client the stored refresh token was issued to. Read-only: nothing is ever written here.
        - With a record (the id store_credentials kept beside the token): the caller's or the Cockpit's Web client
          when its id matches; else the client_command's output when its id matches (the command runs only then);
          no match, a failing command, or two different secrets for the id: "reconnect".
        - Without a record (a token from before records existed): the Cockpit's Web client, only when no
          client_command is configured; in every other case "reconnect" (never "the only client left")."""
        if self._client is not None:
            return self._client
        recorded = self.store.get(REFRESH_CLIENT)
        web = web_client(self.store)
        if not recorded:
            if not web and not self.s["client_command"] and not self.store.get(REFRESH):
                raise YouTubeError("youtube_no_client")  # nothing set up and nothing stored: the setup is missing
            if self.s["client_command"] or not web:
                raise YouTubeError("youtube_reconnect_client")
            self._client = web
            return web
        found = {(c["client_id"], c["client_secret"]) for c in (self._given, web) if c and c["client_id"] == recorded}
        if not found and self.s["client_command"]:
            try:
                cmd = load_client(self.s["client_command"])
            except YouTubeError:
                cmd = None
            if cmd and cmd["client_id"] == recorded:
                found.add((cmd["client_id"], cmd["client_secret"]))
        if len(found) != 1:
            raise YouTubeError("youtube_reconnect_client")
        cid, secret = found.pop()
        self._client = {"client_id": cid, "client_secret": secret}
        return self._client

    def signin_client(self) -> dict:
        """The client a new CLI sign-in uses: the caller's, else client_command's, else the Cockpit's Web client."""
        if self._given:
            return self._given
        if self.s["client_command"]:
            return load_client(self.s["client_command"])
        web = web_client(self.store)
        if not web:
            raise YouTubeError("youtube_no_client")
        return web

    def oauth(self) -> OAuth:
        return OAuth(self.client(), self.request)

    def access_token(self) -> str:
        token, until = self._token
        if token and time.time() < until:
            return token
        refresh = self.store.get(REFRESH)
        if not refresh:
            raise YouTubeError("youtube_not_connected")
        token = self.oauth().refresh(refresh)
        self._token = (token, time.time() + 45 * 60)
        return token

    def api(self) -> YouTube:
        return YouTube(self.access_token, self.request)


# --- state -----------------------------------------------------------------------------------------------

def _state() -> dict:
    return _read(folder() / "state.json")


def _set_state(**kw) -> dict:
    st = _state()
    st.update(kw)
    _write(folder() / "state.json", st)
    return st


# --- connect / disconnect ----------------------------------------------------------------------------

def _wait_for_code(port_server: HTTPServer, expect_state: str, read_line: Callable[[], str],
                   timeout: float) -> str:
    """The authorization code: from the loopback redirect (the browser on this Mac), or from the redirect
    address pasted on stdin (a phone, where 127.0.0.1 does not open). The state must match."""
    got: Dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # never log the request line: it carries the code
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ok = (q.get("state") or [""])[0] == expect_state and (q.get("code") or [""])[0]
            if ok:
                got["code"] = q["code"][0]
                done.set()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("Connected. You can close this tab." if ok else "Not accepted.").encode())

    port_server.RequestHandlerClass = Handler
    threading.Thread(target=port_server.serve_forever, daemon=True).start()

    def pasted():
        while not done.is_set():
            try:
                line = read_line()
            except (EOFError, OSError, ValueError):
                return
            if not line:
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(line.strip()).query)
            if (q.get("state") or [""])[0] == expect_state and (q.get("code") or [""])[0]:
                got["code"] = q["code"][0]
                done.set()
    threading.Thread(target=pasted, daemon=True).start()
    done.wait(timeout)
    port_server.shutdown()
    if not got.get("code"):
        raise YouTubeError("youtube_connect_timeout")
    return got["code"]


def connect(cfg: Config, service: Optional[Service] = None, show: Callable[[str], None] = print,
            read_line: Callable[[], str] = sys.stdin.readline, timeout: float = CONNECT_TIMEOUT) -> str:
    """Sign in to YouTube and pick the channel (Google's own account picker). Stores only the refresh token
    (Keychain) and the channel's id and title (state). Read-only on YouTube: nothing is created."""
    s = settings(cfg)
    from . import policy
    if policy.explicitly_off():  # the bridge is off: no new sign-in (status, stop and disconnect still work)
        raise YouTubeError("mirror_off")
    svc = service or Service(s)
    if _job_on():
        raise YouTubeError("youtube_stop_first")
    client = svc.signin_client()
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(24)
    gen = generation()  # before the sign-in opens: a disconnect or another sign-in meanwhile voids this one
    show(auth_url(client, redirect, state, challenge))
    code = _wait_for_code(server, state, read_line, timeout)
    tokens = OAuth(client, svc.request).exchange(code, verifier, redirect)
    return describe_channel(svc, store_credentials(svc, tokens, gen, client))


def _live() -> bool:
    """A broadcast job runs or an owner start is recorded: the connection must not change under it."""
    st = _state()
    return _job_on() or bool(st.get("on") or st.get("owner_start") or st.get("broadcast_id"))


def store_credentials(svc: "Service", tokens: dict, expect: int, client: dict) -> int:
    """THE one place a sign-in's credential is stored (the CLI and the Cockpit both come here); returns the new
    generation. `expect` is the generation captured before the sign-in opened. Under the connection lock:
    - refused when the generation moved since (a disconnect or another sign-in, even during the code exchange),
      or while a broadcast runs or an owner start is recorded ("stop first"); the token just obtained is then
      revoked (best effort) and nothing is stored;
    - otherwise, in this order, each step safe to crash after: (1) a new generation, marked not connected;
      (2) the credential in the Keychain, then the id of `client`, the client that did the code exchange (the
      caller hands it over; nothing is loaded or run here); every later refresh picks its client by this
      record; (3) the same generation marked connected. A confirmation issued under the old generation never
      matches again, and a start finds "not connected" until step 3 is written."""
    client_id = client.get("client_id") if isinstance(client, dict) else None
    if not isinstance(expect, int) or not tokens.get("refresh_token") or not tokens.get("access_token") \
            or not isinstance(client_id, str) or not client_id or not isinstance(client.get("client_secret"), str):
        raise YouTubeError("youtube_connect_failed")
    refused = ""
    with connection_lock():
        if generation() != expect:
            refused = "youtube_connect_stale"
        elif _live():
            refused = "youtube_stop_first"
        else:
            gen = expect + 1
            _set_connection(generation=gen, connected=False, channel_id="", channel_title="", live_enabled=None)
            svc.store.delete(REFRESH_CLIENT)  # never an old record beside a new token
            svc.store.set(REFRESH, tokens["refresh_token"])
            svc.store.set(REFRESH_CLIENT, client_id)
            svc._client = {"client_id": client_id, "client_secret": client["client_secret"]}
            svc._token = (tokens["access_token"], time.time() + 45 * 60)
            _set_connection(generation=gen, connected=True, connected_at=int(time.time()))
            st = _state()
            if any(k in st for k in LEGACY):  # the older version's copy no longer disagrees with this one
                _write(folder() / "state.json", {k: v for k, v in st.items() if k not in LEGACY})
    if refused:
        revoke_token(tokens["refresh_token"], svc.request)  # the token only: no client needed
        log("connect: a sign-in was not stored (%s)" % refused)
        raise YouTubeError(refused)
    return gen


def describe_channel(svc: "Service", expect: Optional[int] = None) -> str:
    """Read-only: the channel's title and whether it can stream live. Nothing is created on YouTube. Written only
    while the connection is still the one `expect` names (a later sign-in or disconnect is never overwritten)."""
    api = svc.api()
    chan = api.my_channel()
    enabled = api.live_enabled()
    with connection_lock():
        if expect is None or generation() == expect:
            _set_connection(connected=True, channel_id=chan["id"], channel_title=chan["title"], live_enabled=enabled)
    log("connect: connected" + ("" if enabled else " (live streaming is not enabled on the channel yet)"))
    return "youtube_connected" if enabled else "youtube_connected_not_enabled"


# --- the Cockpit's sign-in client (a Web-type OAuth client, kept in the Keychain) -----------------------------

WEB_CLIENT_ID, WEB_CLIENT_SECRET = "web-client-id", "web-client-secret"  # accounts next to REFRESH


def web_client(store) -> Optional[dict]:
    cid, secret = store.get(WEB_CLIENT_ID), store.get(WEB_CLIENT_SECRET)
    return {"client_id": cid, "client_secret": secret} if cid and secret else None


# --- operations the Cockpit asks for (run detached; the card follows them) ----------------------------------

OP_TIMEOUT = 300
ACTIONS = ("start", "stop", "disconnect")


def _op(**fields) -> None:
    try:
        _write(folder() / "op.json", fields)
    except OSError:
        pass


def switch(cfg: Config, action: str, request: Optional[str] = None) -> str:
    """start, stop or disconnect, recorded in op.json (in progress, then its result) for the Cockpit card.
    `request`: the Cockpit's start request id (`--request`); a start with one is a Cockpit start and needs that
    request's confirmation, a start without one is the owner's own CLI start. Nothing is inferred."""
    if action not in ACTIONS:
        raise YouTubeError("youtube_api_failed", why="unknown action")
    prev = _read(folder() / "op.json")
    asked = (int(prev.get("asked") or 0) if prev.get("action") == action and not prev.get("done") else 0) \
        or int(time.time())
    _op(action=action, at=int(time.time()), asked=asked, done=False)
    key, ok = "youtube_job_failed", False
    try:
        if action == "start":
            key = start(cfg, expect=take_start_request(request)) if request is not None else start(cfg)
        else:
            key = {"stop": stop, "disconnect": disconnect}[action](cfg)
        ok = key in ("youtube_started", "youtube_already_on", "youtube_stopped", "youtube_disconnected",
                     "youtube_disconnected_revoke_failed")  # disconnected here; the card shows the revoke note
        return key
    except YouTubeError as e:
        key = e.key
        raise
    finally:
        _op(action=action, at=int(time.time()), asked=asked, done=True, ok=ok, result=key)


START_REQUESTS = "start-requests"  # one 0600 file per Cockpit start request: <id>.json
START_REQUEST_TTL = 120
REQUEST_ID = re.compile(r"[0-9a-f]{32}")


def _requests() -> Path:
    return private_dir(folder() / START_REQUESTS)


def _drop_expired_requests(now: float) -> None:
    for f in _requests().glob("*"):
        try:
            if now - f.stat().st_mtime > START_REQUEST_TTL:
                f.unlink()
        except OSError:
            pass


def put_start_request(gen: int, channel_id: str) -> str:
    """Record the channel the owner confirmed for one Cockpit start; returns the request id for `--request`."""
    now = time.time()
    _drop_expired_requests(now)
    rid = secrets.token_hex(16)
    write_private(_requests() / f"{rid}.json", json.dumps({"generation": gen, "channel_id": channel_id, "at": int(now)}))
    return rid


def take_start_request(rid: str) -> dict:
    """The confirmation of Cockpit start request `rid`, consumed atomically (renamed away first, so two starts can
    never both read it). Missing, already used, malformed or expired: refused (YouTubeError)."""
    if not isinstance(rid, str) or not REQUEST_ID.fullmatch(rid):
        raise YouTubeError("youtube_confirm_missing")
    src, used = _requests() / f"{rid}.json", _requests() / f"{rid}.taken-{os.getpid()}"
    try:
        os.rename(src, used)  # atomic: only one process gets it
    except OSError:
        raise YouTubeError("youtube_confirm_missing") from None
    data = _read(used)
    try:
        used.unlink()
    except OSError:
        raise YouTubeError("youtube_confirm_missing") from None
    at = data.get("at")
    if not isinstance(at, (int, float)) or not 0 <= time.time() - at <= START_REQUEST_TTL \
            or not isinstance(data.get("generation"), int) or not isinstance(data.get("channel_id"), str) \
            or not data["channel_id"]:
        raise YouTubeError("youtube_confirm_missing")
    return {"generation": data["generation"], "channel_id": data["channel_id"]}


def operation(now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    op = _read(folder() / "op.json")
    age = now - float(op.get("at") or 0)
    pending = op.get("action") if not op.get("done") and 0 <= age < OP_TIMEOUT else ""
    recent = bool(op.get("done")) and 0 <= age < OP_TIMEOUT
    return {"pending": pending if pending in ACTIONS else "",
            "failed": recent and op.get("ok") is False,
            "result": str(op.get("result") or "") if recent else ""}


def spawn(action: str, request: Optional[str] = None) -> None:
    """`harnessgram radio youtube <action> [--request <id>]` detached (the Cockpit answers at once; start takes
    seconds). A Cockpit start always carries its request id."""
    if action not in ACTIONS or (request is not None and not REQUEST_ID.fullmatch(request)):
        return
    now = int(time.time())
    _op(action=action, at=now, asked=now, done=False)
    extra = ["--request", request] if request is not None else []
    subprocess.Popen(launch.argv() + ["radio", "youtube", action] + extra, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def view(cfg: Config, client_ready: bool, base_ready: bool, now: Optional[float] = None) -> dict:
    """The Cockpit card's state, from local files only (no network, no secret read): one read in /api/state."""
    from . import policy
    now = time.time() if now is None else now
    s = settings(cfg)
    st, run, conn = _state(), _read(folder() / "status.json"), connection()
    since = st.get("since") if isinstance(st.get("since"), (int, float)) else None
    live = bool(st.get("on") and st.get("broadcast_id") and st.get("owner_start"))
    left = max(0.0, since + s["max_hours"] * 3600 - now) / 3600 if live and since else None
    return {"enabled": s["enabled"], "setup": {"client": client_ready, "base": base_ready},
            "connected": bool(conn.get("connected")), "channel": str(conn.get("channel_title") or "")[:100],
            "live_enabled": conn.get("live_enabled") if isinstance(conn.get("live_enabled"), bool) else None,
            "live": live, "since": int(since) if live and since else None,
            "hours_left": round(left, 1) if left is not None else None,
            "ended": not live and run.get("pipeline") in ("ended", "failed", "stopped"),
            "failed": bool(run.get("gave_up")), "cleanup": len(_pending()),
            "bridge_off": not policy.on(), "projects": catalog(cfg), **operation(now)}


def disconnect(cfg: Config, service: Optional[Service] = None) -> str:
    """Forget the channel: revoke the grant at Google (best effort), remove the Keychain items and the state."""
    s = settings(cfg)
    svc = service or Service(s)
    if _live():  # live: end the broadcast first, with the credential it was started with, then disconnect
        stop(cfg, svc)
    if svc.store.get(REFRESH):
        _queue_cleanup(_state().get("broadcast_id"))
        try:
            finish_pending(svc)  # while the sign-in still works: end what this radio left open
        except YouTubeError:
            pass
    with connection_lock():  # a sign-in finishing meanwhile either lands before (and is removed here) or is stale
        refresh = svc.store.get(REFRESH)
        revoked = revoke_token(refresh, svc.request) if refresh else True  # the token only: no client is loaded
        for name in (REFRESH, REFRESH_CLIENT, INGEST_URL, STREAM_KEY):
            svc.store.delete(name)
        _set_connection(generation=generation() + 1, connected=False, channel_id="", channel_title="",
                        live_enabled=None)
        _write(folder() / "state.json", {"cleanup": _pending()})  # pending ids are never lost
    # Disconnected here either way; when Google did not confirm the revoke, say so (the grant may still be listed
    # in the Google account's third-party access page, where it can be removed by hand).
    log("disconnect: done" + ("" if revoked else " (Google did not confirm the revoke)"))
    return "youtube_disconnected" if revoked else "youtube_disconnected_revoke_failed"


# --- launchd -------------------------------------------------------------------------------------------

def plist_path() -> Path:
    return HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def plist_bytes(extra_path: Optional[List[str]] = None) -> bytes:
    env = {"PATH": radio.job_path(extra_path or [])}
    env.update({n: os.environ[n] for n in ("CTB_CONFIG_DIR", "CTB_STATE_DIR") if os.environ.get(n)})
    return plistlib.dumps({
        "Label": LABEL, "ProgramArguments": launch.argv() + ["radio", "youtube", "run"],
        "WorkingDirectory": str(folder()), "EnvironmentVariables": env,
        "StandardErrorPath": str(state_dir() / "radio-youtube.err"),
        "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 15,
        "ProcessType": "Interactive",
    })


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)


def _job_on() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        return _launchctl("print", f"gui/{os.getuid()}/{LABEL}").returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _load_job(s: dict) -> None:
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".ctb-tmp")
    tmp.write_bytes(plist_bytes(s["path"]))
    os.replace(tmp, path)
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    if _launchctl("bootstrap", f"gui/{os.getuid()}", str(path)).returncode:
        raise YouTubeError("youtube_job_failed")


def _unload_job() -> None:
    if _job_on():
        _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
        for _ in range(40):
            if not _job_on():
                break
            time.sleep(0.25)
    try:
        plist_path().unlink()
    except OSError:
        pass


def _kill_leftover_encoder() -> None:
    pid = _read(folder() / "status.json").get("ffmpeg_pid")
    if not isinstance(pid, int) or pid <= 1:
        return
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    except OSError:
        return
    if "ffmpeg" in cmd and MARKER in cmd:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


# --- start / stop / status: the owner's explicit actions --------------------------------------------

def start(cfg: Config, service: Optional[Service] = None, load_job: Callable[[dict], None] = _load_job,
          expect: Optional[dict] = None) -> str:
    """Go live (a publication: the owner runs this himself, from the CLI or the Cockpit button). Creates the
    reusable ingest stream once, a new unlisted broadcast bound to it, then loads the job that feeds it."""
    with _locked():
        return _start(cfg, service, load_job, expect)


def _start(cfg: Config, service: Optional[Service], load_job: Callable[[dict], None],
           expect: Optional[dict] = None) -> str:
    s = settings(cfg)
    if sys.platform != "darwin" and load_job is _load_job:
        raise YouTubeError("youtube_macos_only")
    if not s["enabled"]:
        raise YouTubeError("youtube_disabled")
    from . import policy
    if not policy.on():  # the bridge is off: nothing is created on YouTube
        raise YouTubeError("mirror_off")
    if _job_on():
        return "youtube_already_on"
    svc = service or Service(s)
    if not svc.store.has(REFRESH):
        raise YouTubeError("youtube_not_connected")
    _queue_cleanup(_state().get("broadcast_id"))  # an earlier broadcast is never overwritten: it waits for cleanup
    _set_state(broadcast_id="")
    finish_pending(svc)  # and is finished first when YouTube answers; ids that still fail stay in the list
    svc.client()  # resolved now, outside the lock: a client_command never runs while the lock is held
    with connection_lock():
        # The rule: the account that will publish is checked against what the owner confirmed, at the last
        # moment, with the real credential. Under the lock (no sign-in or disconnect can land until the broadcast
        # is bound): the confirmed channel is the channel to match (a Cockpit start: its request's channel and
        # generation; the owner's CLI start: the stored connection's channel); then Google is asked, with the
        # access token that will create the broadcast, which channel that credential belongs to.
        conn = connection()
        if not conn.get("connected") or not conn.get("channel_id"):
            raise YouTubeError("youtube_not_connected")
        if expect is not None and (conn["generation"] != expect["generation"]
                                   or conn["channel_id"] != expect["channel_id"]):
            log("start: the connected channel changed after the confirmation; nothing was created")
            raise YouTubeError("youtube_channel_changed")
        want = expect["channel_id"] if expect is not None else conn["channel_id"]
        svc._token = ("", 0.0)
        api = svc.api()
        if api.my_channel()["id"] != want:
            log("start: the credential belongs to another channel than the confirmed one; nothing was created")
            raise YouTubeError("youtube_channel_changed")
        return _create(s, svc, load_job, api, conn["generation"])


def _create(s: dict, svc: "Service", load_job: Callable[[dict], None], api: "YouTube", gen: int) -> str:
    if not api.live_enabled():
        _set_connection(live_enabled=False)
        raise YouTubeError("youtube_not_enabled")
    stream = api.find_stream() or api.insert_stream()
    url, key = ingest(stream)
    svc.store.set(INGEST_URL, url)
    svc.store.set(STREAM_KEY, key)
    broadcast = api.insert_broadcast(s["title"])
    bid = broadcast.get("id") or ""
    if not bid:
        raise YouTubeError("youtube_api_failed", why="no broadcast id")
    nonce = secrets.token_hex(8)
    try:
        api.bind(bid, stream.get("id") or "")
        _set_connection(live_enabled=True)
        _set_state(on=True, broadcast_id=bid, stream_id=stream.get("id") or "", since=int(time.time()),
                   owner_start=nonce, start_generation=gen)  # the job ends the broadcast if the connection changes
        for name in ("last.json", "failures.json", "card.png"):  # a fresh start: nothing from a former broadcast
            try:
                (folder() / name).unlink()
            except OSError:
                pass
        load_job(s)
    except BaseException as e:
        _abort(svc, bid)
        if isinstance(e, YouTubeError) or not isinstance(e, Exception):
            raise
        log("start: could not set up (%s)" % e.__class__.__name__)
        raise YouTubeError("youtube_job_failed") from None
    log("start: broadcast created; the job feeds it")
    return "youtube_started"


def _pending() -> List[str]:
    got = _state().get("cleanup")
    return [b for b in got if isinstance(b, str) and b] if isinstance(got, list) else []


def _queue_cleanup(bid: object) -> None:
    """Remember a broadcast that still has to be ended or deleted. An id is never dropped until YouTube confirms."""
    if isinstance(bid, str) and bid and bid not in _pending():
        _set_state(cleanup=_pending() + [bid])


def _finish(api: YouTube, bid: str) -> None:
    phase = api.life_cycle(bid)
    if phase in ("live", "testing", "liveStarting", "testStarting"):
        api.transition(bid, "complete")
    elif phase in ("created", "ready"):
        api.delete_broadcast(bid)  # never went live: remove the empty broadcast this radio created


def finish_pending(svc: Service) -> bool:
    """End every broadcast waiting for cleanup; True when none is left. Only ids this radio recorded."""
    left = []
    for bid in _pending():
        try:
            _finish(svc.api(), bid)
        except YouTubeError:
            left.append(bid)
    _set_state(cleanup=left)
    return not left


def _abort(svc: Service, bid: str) -> None:
    try:
        _unload_job()
    except Exception:
        pass
    _set_state(on=False, owner_start="", broadcast_id="")
    _queue_cleanup(bid)
    finish_pending(svc)


def end_broadcast(svc: Service) -> bool:
    """End (or, when it never went live, delete) the broadcast this radio created, and the ones still waiting."""
    st = _state()
    _queue_cleanup(st.get("broadcast_id"))
    _set_state(broadcast_id="", on=False, owner_start="")
    return finish_pending(svc)


def stop(cfg: Config, service: Optional[Service] = None) -> str:
    with _locked():
        return _stop(cfg, service)


def _stop(cfg: Config, service: Optional[Service]) -> str:
    s = settings(cfg)
    _unload_job()
    _kill_leftover_encoder()
    st = _read(folder() / "status.json")
    if st:
        st.update(pipeline="stopped", ffmpeg_pid=None)
        _write(folder() / "status.json", st)
    svc = service or Service(s)
    ended = end_broadcast(svc)
    log("stop: off" + ("" if ended else " (a broadcast may still be open)"))
    return "youtube_stopped" if ended else "youtube_stopped_left"


def status(cfg: Config, live: bool = False, service: Optional[Service] = None) -> dict:
    """Works offline: settings, the stored state and the job. No secret is read (only whether it exists)."""
    s = settings(cfg)
    st, run = _state(), _read(folder() / "status.json")
    store = (service.store if service else KeychainStore(s["keychain_service"]))
    out = {"enabled": s["enabled"], "projects": len(s["projects"]), "sections": s["sections"],
           "connected": bool(connection().get("connected")) and store.has(REFRESH),
           "channel": connection().get("channel_title") or "", "live_enabled": connection().get("live_enabled"),
           "on": _job_on(), "broadcast": bool(st.get("broadcast_id")), "pipeline": run.get("pipeline", ""),
           "failures": run.get("failures", 0), "gave_up": bool(run.get("gave_up")),
           "cleanup_pending": len(_pending()), "last_update": run.get("last_update")}
    if live and out["connected"]:
        svc = service or Service(s)
        try:
            api = svc.api()
            out["live_enabled"] = api.live_enabled()
            if st.get("broadcast_id"):
                out["life_cycle"] = api.life_cycle(st["broadcast_id"])
        except YouTubeError as e:
            out["live_error"] = e.key
    return out


# --- the job: the staff station --------------------------------------------------------------------------

def _load_settings() -> dict:
    from . import config as C
    return settings(C.load())


class StaffStation:
    """`harnessgram radio youtube run`: the launchd job. Speaks and draws the staff edition only.

    The owner's settings are read again on every round and before every (re)connect: turning the staff radio
    off ends the broadcast at once, a project taken off the allowlist is gone from the next update, and the
    recorded owner start and the broadcast's end time (from the owner's `since`) are checked each time."""

    def __init__(self, cfg: Config, service: Optional[Service] = None,
                 reload: Optional[Callable[[], dict]] = None):
        self.cfg = cfg
        self.s = settings(cfg)
        self.reload = reload or _load_settings
        self.dir = folder()
        self.card = self.dir / "card.png"
        self.mixer = radio.Mixer(radio.make_bed(self.s["bed"], self.s["bed_db"]))
        self.quit = threading.Event()
        self.pipe: Optional[radio.Pipeline] = None
        self.service = service
        self.ending = ""  # why the job ends itself: "" (it does not), "off", "owner", "ended"
        # Every connection starts a new generation with the identification card on screen (connect()). A real
        # card is drawn only from a gather begun under the current generation, so an update that read the
        # settings before a reconnect can never put its (older) card back on air.
        self.draw_lock = threading.Lock()
        self.generation = 0
        self.wake = threading.Event()
        self.status = {"pid": os.getpid(), "started": int(time.time()), "pipeline": "starting", "failures": 0}

    def _status(self, **kw) -> None:
        self.status.update(kw)
        _write(self.dir / "status.json", self.status)

    # the owner's settings and permission, checked again and again
    def refresh(self) -> bool:
        """Read the settings again; False (and `ending` set, `quit` raised) when the job must not go on."""
        try:
            self.s = self.reload()
        except Exception as e:  # unreadable settings: nothing but the station id from now on
            log("settings: could not read them (%s); identification only" % e.__class__.__name__)
            self.s = dict(self.s, projects={})
        from . import policy
        why = ""
        if not self.s["enabled"]:
            why = "off"
        elif not policy.on():  # the bridge-off switch, read again each time (never cached)
            why = "bridge_off"
        elif not self.owner_started():
            why = "owner"
        elif _state().get("start_generation") != generation():
            why = "connection_changed"  # the connection changed under a running broadcast: it ends
        elif time.time() >= self.end_time():
            why = "ended"
        if why:
            self.ending = why
            self.quit.set()
            return False
        return True

    def owner_started(self) -> bool:
        """The job feeds only a broadcast an owner `start` created and recorded (never one of its own)."""
        st = _state()
        return bool(self.s["enabled"] and st.get("on") and st.get("broadcast_id") and st.get("owner_start"))

    def end_time(self) -> float:
        """The owner's start time plus max_hours: a restart of the job never extends a broadcast."""
        since = _state().get("since")
        since = float(since) if isinstance(since, (int, float)) and not isinstance(since, bool) else 0.0
        return since + self.s["max_hours"] * 3600

    # content
    def gather(self, now: float) -> dict:
        """The same snapshot the owner's radio reads, plus the per-project open counts. radio_staff then keeps
        only what the allowlist names. No quota command and no questions are read here at all."""
        from .panel import build_state
        state = build_state(self.cfg, now=now, light=True)
        ledger_dir = Path(os.path.expanduser(self.s["ledger_dir"])) if self.s["ledger_dir"] else None
        ledger = radio_script.ledger_items(ledger_dir, now)
        return radio_script.snapshot(state, [], ledger, [], now, team_by=radio_script.ledger_open_by(ledger_dir))

    def draw(self, view: dict, generation: Optional[int] = None) -> bool:
        """The staff card, only while `generation` is still the current one (None: the identification card, which
        is always allowed). When drawing fails, a plain frame replaces it (an older card is never left on air)."""
        with self.draw_lock:
            if generation is not None and generation != self.generation:
                return False
            self._draw(view)
            return True

    def _draw(self, view: dict) -> None:
        if radio.board_on() and self._draw_board(view):
            return
        title, sections = radio_staff.render_inputs(view)
        ok = False
        try:
            ok = radio_card.render(radio.folder() / "bin", self.card, title,
                                   time.strftime("%H:%M", time.localtime(view["at"])), sections, "",
                                   rtl=view["lang"] == "he")
        except Exception as e:
            log("card: could not draw (%s)" % e.__class__.__name__)
        if not ok:
            try:
                self.card.unlink()  # never left on air: a plain frame, or no picture at all
            except OSError:
                pass
            radio_card.plain(self.card)

    def _draw_board(self, view: dict) -> bool:
        """The staff flight board: built from `view` (radio_staff.staff_view) through radio_board.staff_slide only,
        so it shows allowlisted names and counts and nothing else. False: the plain staff card is drawn instead."""
        from . import radio_board, radio_paint
        try:
            tmp = self.card.with_name(".board-" + self.card.name)
            if radio_paint.paint(radio.folder() / "bin", [radio_paint.layout(radio_board.staff_slide(view), str(tmp))]) \
                    and tmp.exists():
                os.replace(tmp, self.card)
                return True
        except Exception as e:
            log("board: could not draw (%s)" % e.__class__.__name__)
        return False

    def id_card(self) -> None:
        """The identification-only card (no project, no count), drawn before any connection is opened and
        whenever the state cannot be read."""
        self.draw(radio_staff.staff_view({"at": time.time()}, dict(self.s, projects={})))

    def speak(self, text: str) -> bool:
        """The free local voice only: the staff edition never spends the owner's paid voice minutes."""
        pcm = radio_voice.system_voice(text, self.s["say_voice"])
        if not pcm:
            log("speak: no voice available")
            return False
        from . import policy
        if not policy.on():
            return False  # switched off while the voice was being made: dropped, never queued
        self.mixer.add(radio_voice.intro() + pcm)
        log(f"speak: {radio_voice.seconds_of(pcm):.0f} s")
        return True

    def update(self) -> None:
        generation = self.generation  # taken before the settings are read: a later connect makes this one stale
        if not self.refresh():
            return
        now = time.time()
        try:
            snap = self.gather(now)
        except Exception as e:
            log("update: could not read the state (%s); identification only" % e.__class__.__name__)
            self.id_card()
            return
        last = _read(self.dir / "last.json")
        view = radio_staff.staff_view(snap, self.s, last)
        if not self.draw(view, generation):
            return  # a connection was opened meanwhile: the next update draws under the fresh settings
        kind = radio_staff.decide(view, last, now, self.s["every_minutes"] * 60)
        if kind and self.speak(radio_staff.build_script(view)):
            _write(self.dir / "last.json", radio_staff.remember(view, now, last))
            self._status(last_update=int(now), last_kind=kind)

    def content_loop(self) -> None:
        next_look = 0.0
        while not self.quit.is_set():
            woke = self.wake.wait(1.0)
            if self.quit.is_set():
                break
            if woke or time.time() >= next_look:
                self.wake.clear()
                next_look = time.time() + POLL
                self.update()

    def note_failure(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        path = self.dir / "failures.json"
        at = [float(x) for x in _read(path).get("at", []) if isinstance(x, (int, float)) and 0 <= now - x < FAILURE_WINDOW]
        at.append(now)
        try:
            _write(path, {"at": at[-50:]})
        except OSError:
            pass
        return len(at)

    # the stream
    def run(self) -> int:
        signal.signal(signal.SIGTERM, lambda *_: self.quit.set())
        signal.signal(signal.SIGINT, lambda *_: self.quit.set())
        try:
            return self._run()
        except Exception as e:
            log("run: unexpected failure (%s)" % e.__class__.__name__)
            self.quit.set()
            if self.pipe is not None:
                self.pipe.close()
            return self.give_up() if self.note_failure() >= MAX_FAILURES else 1

    def _run(self) -> int:
        if not self.refresh():
            return self._ended()
        svc = self.service or Service(self.s)
        url, key = svc.store.get(INGEST_URL), svc.store.get(STREAM_KEY)
        if not url or not key:
            log("run: the stream key is not available")
            return self.give_up()
        encoder = radio.pick_encoder(self.s["video_encoder"])
        threading.Thread(target=self.content_loop, daemon=True, name="radio-youtube-content").start()
        threading.Thread(target=self._clock, daemon=True, name="radio-youtube-clock").start()
        while not self.quit.is_set():
            self.pipe = self.connect(url, key, encoder)
            if self.pipe is None:
                break
            why = self.pipe.run()
            if self.quit.is_set():
                break
            if why == radio.MIRROR_OFF:  # the bridge was switched off mid-stream (the pipeline checks each second)
                self.ending = "bridge_off"
                self.quit.set()
                break
            count = self.note_failure()
            log(f"pipeline: {why[:80]}; restart {count}/{MAX_FAILURES}")
            self._status(pipeline="restarting", failures=count)
            if count >= MAX_FAILURES:
                return self.give_up()
            self.quit.wait(min(60, 2 ** count))  # bounded backoff
        if self.ending:
            return self._ended()
        self._status(pipeline="stopped", ffmpeg_pid=None)
        return 0

    def connect(self, url: str, key: str, encoder: str) -> Optional[radio.Pipeline]:
        """The ONE way this job opens a connection (first and every reconnect). In this order: read the settings
        and the owner's permission again; start a new generation and put the identification-only card on screen
        (no project, no count); only then build the pipeline. The first real card follows from a fresh gather
        under the settings just read (the content loop is woken). None when the job must not go on."""
        if not self.refresh():
            return None
        with self.draw_lock:
            self.generation += 1
            self._draw(radio_staff.staff_view({"at": time.time()}, dict(self.s, projects={})))
        self.wake.set()
        pipe = radio.Pipeline(url, key, self.card, encoder, self.mixer, lambda pid: self._status(ffmpeg_pid=pid),
                              quit=self.quit, err_path=self.dir / "ffmpeg.err", marker=MARKER)
        self._status(pipeline="live")
        return pipe

    def _clock(self) -> None:
        """Checks the end time between rounds too, so max_hours holds to the minute."""
        while not self.quit.wait(20):
            if time.time() >= self.end_time():
                self.ending = "ended"
                self.quit.set()

    def _ended(self) -> int:
        log({"off": "run: the staff radio was turned off in the settings; ending the broadcast",
             "owner": "run: no owner start is recorded; not streaming",
             "bridge_off": "run: the bridge was turned off; ending the broadcast (a new start is the owner's)",
             "connection_changed": "run: the YouTube connection changed; ending the broadcast",
             "ended": "run: max_hours reached; ending the broadcast"}.get(self.ending, "run: ending"))
        return self.give_up(end=self.ending != "owner" or bool(_state().get("broadcast_id")),
                            why="failed" if self.ending == "owner" else "ended")

    def give_up(self, end: bool = True, why: str = "failed") -> int:
        """Too many failures, turned off, no owner start, or max_hours: end this radio's broadcast and unload this
        job only (the Telegram radio is another job)."""
        self.status.update(pipeline=why, gave_up=why == "failed", ffmpeg_pid=None)
        try:
            _write(self.dir / "status.json", self.status)
        except OSError:
            pass
        if end:
            try:
                end_broadcast(self.service or Service(self.s))
            except Exception:
                pass
        try:
            plist_path().unlink()
        except OSError:
            pass
        subprocess.Popen(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return 0


# --- the CLI -----------------------------------------------------------------------------------------------

def cli(cfg: Config, action: str, live: bool = False, request: Optional[str] = None) -> int:
    from .i18n import t
    lang = radio.settings(cfg)["lang"]
    try:
        if action == "run":
            return StaffStation(cfg).run()
        if action == "status":
            print(json.dumps(status(cfg, live=live), ensure_ascii=False, indent=1))
        elif action == "connect":
            print(t("youtube_connect_open", lang=lang), file=sys.stderr)
            print(t(connect(cfg), lang=lang))
        elif action in ACTIONS:  # recorded for the Cockpit card (op.json), whoever asked
            if request is not None and action != "start":
                return 2
            print(t(switch(cfg, action, request), lang=lang))
        else:
            return 2
        return 0
    except YouTubeError as e:
        if e.key == "mirror_off":
            from . import policy
            print(policy.OFF_NOTICE, file=sys.stderr)  # the bridge's shared off notice
        else:
            print(t(e.key, lang=lang, **e.kw), file=sys.stderr)
        return 1
