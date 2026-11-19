"""The control panel: a Telegram Mini App served on loopback.

`GET /` is one static page; `POST /api/state` returns the open decisions, the owner's reminders, the
sessions and the quota as JSON, `POST /api/decide` records the owner's yes/no on one decision, and
`POST /api/radio` turns the team radio on or off ({"on": true|false}; see radio.py), `/api/radio-say` speaks
the owner's words on it, `/api/model` sets the model new sessions start with (models.set_default),
`/api/setting` changes one bridge setting (panel_settings.py) and `/api/dismiss` clears stale decisions.
`POST /api/youtube` and `GET /oauth/youtube/start|callback` are the YouTube staff radio's controls and its web
sign-in (panel_youtube.py: the OAuth routes are bound to a ticket from the owner's signed request and to the
browser that took it). `GET /oauth/google/start|callback` is the same kind of web sign-in for the owner's Google
Calendar, read only (google_calendar.py; its ticket comes from `POST /api/schedule {"action": "google-connect"}`). Cockpit
apps (cockpit_apps.py) add `GET /app/<name>/`, `GET /app/<name>/media/<path>` and `POST /app/<name>/api`.
These accept only short-lived, app-scoped tokens from `POST /api/app-token`; the parent's full signed
Telegram `initData` never enters a frame. Owner APIs require that full login (HMAC-SHA256 with a key
derived from the bot token). Anything else gets a bare 403; an answer needs a fresher login
(DECIDE_MAX_AGE) than reading does (MAX_AGE).
The server binds to 127.0.0.1; how it is reached from a phone (a private network or a public HTTPS route)
is up to the operator, see docs/panel.md ("What it needs", "Exposure options").
Neither the token nor initData is ever logged or echoed.
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import functools
import hashlib
import hmac
import http.server
import json
import os
import queue
import re
import secrets
import socket
import stat
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, unquote, urlsplit

from . import accounts, bank, board, blocks, brief, card_media, cards, cockpit_apps, credit_mode, decisions, models, panel_page, panel_settings, panel_text, panel_view, status, usage, session_usage
from . import auto_progress, idle_lines, live_activity, mac_resources, panel_youtube, panel_my_music, owner_requests, reminder_lifecycle, topic_cards, topic_status
from . import state_cache, sensitive_buttons, device_tokens, apns
from . import voice_copilot, tower_home
from . import owner_request_actions, cockpit_v2, app_request, mac_tools, topic_cleanup, schedule, google_calendar, addons, project_art, family_board, family_print, listen_link, briefing
from .config import Config, home_chat, state_dir, write_private
from .i18n import t
from .creds import redact_channel, redact_pair
from .work_lines import clean_text
from .inbox import PanelQueueFull
from .sessions_menu import collect
from .topics import TopicMap
from .panel_page import PAGE, STRINGS  # noqa: F401  (the page and its strings table)

MAX_BODY = 64  # the page posts "{}"; a larger body only holds a thread
MAX_DECIDE_BODY = 4400  # {"id", "choice", "note"}: a 1000-character note takes up to 4000 bytes
MAX_CARD_BODY = 1280  # an answer to a card: {"id", "pick"|"text"}
MAX_CONNECTIONS = 16
MAX_INIT_DATA = 4096
MAX_AGE = 3600  # seconds an auth_date stays valid for reading the state
DECIDE_MAX_AGE = 900  # and for answering a decision
FUTURE_SLACK = 30
TIMEOUT = 10
DEADLINE = 5  # seconds a whole connection may take; slow senders cannot hold the 16 slots
# Seconds an authenticated owner's state read may take. A busy Mac builds the state in several seconds, often past
# DEADLINE; a cut connection reached the page as a network failure, and its first load then said "no access".
STATE_DEADLINE = 30
CLIENT_ERROR_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]{0,39}")
CLIENT_ERROR_AT = re.compile(r"(?:[A-Za-z0-9_$.]{0,60}:[0-9]{1,7}:[0-9]{1,7})?")
INIT_HEADER = "X-Telegram-Init-Data"
FRAME_ANCESTORS = "https://web.telegram.org https://*.telegram.org"
MAX_SESSIONS = 60
MAX_REMINDERS = 30
SAY_CHARS = 600  # radio.MAX_SAY
SAY_TO_CHARS = 600  # a message the owner writes to one session from the panel
SAY_KEY = re.compile(r"[0-9a-f]{12}")  # one message written in the panel, while it waits for its session
MAX_WAITING = 5
SEND_ID = re.compile(r"[0-9a-f]{16,32}")  # the page's own id of one send, the same for every retry of it
SENDS_FILE = "panel-sends.json"  # the sends already queued: {send id: {"session", "key", "at"}}
SENDS_KEPT = 24 * 3600
SHORT_ID = re.compile(r"[0-9a-f-]{8}")  # the session id as the page knows it: its first 8 characters
WRITTEN_IN_PANEL = decisions.PANEL_APPROVES
# route: largest body it takes ("{}" for the state; an id list for dismiss; 600 characters to say)
ROUTES = {"/api/state": MAX_BODY, "/api/decide": MAX_DECIDE_BODY, "/api/radio": MAX_BODY, "/api/model": MAX_BODY,
          "/api/setting": MAX_BODY, "/api/dismiss": 1280, "/api/radio-say": 2600, "/api/card": MAX_CARD_BODY, "/api/say": 2600, "/api/bank": MAX_BODY,
          "/api/unsay": MAX_BODY, "/api/close": 1280, "/api/remind": 2600, "/api/screen": MAX_BODY,
          "/api/compact": MAX_BODY, "/api/room": 2600, "/api/podcast": 1280, "/api/radio-music": 256,
          "/api/brief-seen": MAX_BODY, "/api/block-mute": MAX_BODY, "/api/fork": 256,
          "/api/hooks-restore": MAX_BODY, "/api/sessions-reload": MAX_BODY, "/api/app-token": MAX_BODY,
          "/api/card-media-token": MAX_BODY}
ACTIONS = {"/api/model": "_model", "/api/setting": "_setting", "/api/dismiss": "_dismiss", "/api/radio-say": "_radio_say",
           "/api/card": "_card", "/api/say": "_say", "/api/bank": "_bank", "/api/unsay": "_unsay",
           "/api/close": "_close", "/api/remind": "_remind", "/api/screen": "_screen",
           "/api/compact": "_compact", "/api/room": "_room", "/api/podcast": "_podcast", "/api/radio-music": "_radio_music",
           "/api/brief-seen": "_brief_seen", "/api/block-mute": "_block_mute", "/api/fork": "_fork",
           "/api/hooks-restore": "_hooks_restore", "/api/sessions-reload": "_sessions_reload"}
ROOM_KEYS = {"start": ({"topic", "minutes"}, {"project"}), "pause": (set(), set()), "resume": (set(), {"text"}),
             "summarize": (set(), set()), "end": (set(), set()), "close": (set(), set()),
             "send": ({"room", "task", "session"}, set())}
# the podcast queue's actions: (required, optional) keys. "add" takes a picked {"feed", "episode"} or a {"link"}.
PODCAST_KEYS = {"episodes": ({"feed"}, set()), "add": (set(), {"feed", "episode", "link"}), "control": ({"op"}, set()),
                "remove": ({"id"}, set())}
PODCAST_ERRORS = {"bad": 400, "url": 400, "unknown": 404, "full": 409, "dup": 409, "rules": 409, "unsuitable": 422,
                  "explicit": 422, "notext": 422, "too_big": 413, "feed": 502, "fetch": 502}
PODCAST_DEADLINE = 25  # seconds a feed fetch may hold the connection (radio_podcast.FETCH_DEADLINE plus a margin)
ROOM_ID = re.compile(r"[0-9a-f]{8}")         # radio_room.start: secrets.token_hex(4)
ROOM_TASK = re.compile(r"t\d[0-9a-f]{6}")    # radio_room.Conductor._summary  # the brainstorming room's actions: (required, optional) keys
ROOM_ERRORS = {"bad": 400, "unknown": 404, "disabled": 409, "radio_off": 409, "busy": 409, "closed": 409, "rules": 409}
SCREEN_ROWS = 40  # rows of a session's terminal the page shows
LEFT_BEHIND = 24 * 3600  # an open session nobody touched for a day is "left behind": the page offers to close it
MAX_CLOSE = 20
REMINDER_REF = re.compile(r"([0-9a-f]{8}):(\d{1,6})")  # the topic's hash and the item's id, as the board's buttons
START_PARAM = re.compile(r"(?:t[0-9a-f]{8}|[qr][0-9a-f]{16})")  # what panel links put in a button
APP_GET = re.compile(r"/app/([a-z0-9-]{1,24})/(?:media/([^?#]+))?")
APP_API = re.compile(r"/app/([a-z0-9-]{1,24})/api")
AUTH_PARAM = "auth"
APP_TOKEN_HEADER = "X-Cockpit-App-Token"
APP_TOKEN_TTL = 600
CARD_MEDIA_GET = re.compile(r"/card-media/([0-9a-f]{32})")
MEDIA_DEADLINE = 120  # seconds one media reply may take (a range of at most cockpit_apps.MEDIA_CHUNK bytes)
HOST = re.compile(r"[A-Za-z0-9.-]{1,253}(?::\d{1,5})?")
YOUTUBE_BODY = 512  # {"action": ..., "armed"|"on"|"keys": ...}
ROUTES["/api/youtube"] = YOUTUBE_BODY  # routed before the off check (_youtube_post: stop and disconnect pass)
ROUTES["/api/request"] = 1024
ACTIONS["/api/request"] = "_owner_request"
ROUTES["/api/request-detail"] = 1024  # {"project", "id"}: one item's full text, note and steps; a read, like /api/state
ACTIONS["/api/request-detail"] = "_owner_request_detail"
ROUTES["/api/request-target"] = 256  # {"key"}: which ledger row a widget deep link names; a read, like /api/state
ACTIONS["/api/request-target"] = "_owner_request_target"
ROUTES["/api/topic-detail"] = 1024  # {"key": [project, "topic"|"item", slug|id]}: one topic's timeline; a read, like /api/state
ACTIONS["/api/topic-detail"] = "_topic_detail"
ROUTES["/api/app-request"] = 2600  # {"text": <= 500 characters}: the owner's "request an app" action (app_request.py)
ACTIONS["/api/app-request"] = "_app_request"
# What the system knows (knowledge.py): owner-only, behind the same fresh owner login as every other action.
ROUTES["/api/knowledge"] = 4096
ACTIONS["/api/knowledge"] = "_knowledge"
# Old topics the owner may close (topic_cleanup.py): owner-only, behind the same fresh login as every action.
ROUTES["/api/topic-cleanup"] = 1280
ACTIONS["/api/topic-cleanup"] = "_topic_cleanup"
# What HarnessGram adds to your agents (addons.py): owner-only, behind the same fresh login as every action.
ROUTES["/api/addons"] = 256  # {"action": "view"} | {"action": "set", "key", "on"}
ACTIONS["/api/addons"] = "_addons"
ROUTES["/api/packs"] = 512
ACTIONS["/api/packs"] = "_packs"
ROUTES["/api/packs-edit"] = 512
ACTIONS["/api/packs-edit"] = "_packs_edit"
PACK_KEYS = {"view": set()}
PACK_WRITE_KEYS = {"preview": {"pack", "on"}, "set": {"pack", "on"},
                   "move": {"item", "pack"}, "export": {"pack", "dir"}}
ADDON_KEYS = {"view": set(), "set": {"key", "on"}}
ADDON_ERRORS = {"bad": 400, "unknown": 404, "locked": 409}
KNOWLEDGE_KEYS = {"view": set(), "scan": set(), "toggle": {"id", "on"}, "undo": {"log"}, "suggest": {"id", "text"}}
KNOWLEDGE_ERRORS = {"bad": 400, "unknown": 404, "locked": 409, "changed": 409, "forbidden": 409}
# The owner's personal schedule (schedule.py): changes need the fresh owner login, the read is a state-style read.
ROUTES["/api/schedule"] = 2048  # {"action": "add", "text"} | {"action": "done", "id", "done"} | {"action": "remove", "id"}
ACTIONS["/api/schedule"] = "_schedule"
ROUTES["/api/schedule-view"] = 256  # {}: Today and Tomorrow (own items and calendar events)
ACTIONS["/api/schedule-view"] = "_schedule_view"
ROUTES["/api/briefing-dismiss"] = 256  # {}: the owner puts away today's "Good morning" card (briefing.py)
ACTIONS["/api/briefing-dismiss"] = "_briefing_dismiss"
SCHEDULE_KEYS = {"add": {"text"}, "done": {"id", "done"}, "remove": {"id"},
                 "swap": {"date", "id"}, "skip": {"date", "id"}, "extend": {"date", "id", "minutes"},
                 "google-connect": set(), "google-disconnect": set()}
SCHEDULE_ERRORS = {"text": 400, "unknown": 404, "full": 409, "conflict": 409, "unavailable": 503}
# The family home board (family_board.py): changes need the fresh owner login, the read is a state-style read.
ROUTES["/api/family"] = 1024  # {"action": "check"|"add"|"edit"|"remove"|"clear", ...}
ACTIONS["/api/family"] = "_family"
ROUTES["/api/family-view"] = 256  # {}: the week, the daily routine, the shopping list and the meal plan
ACTIONS["/api/family-view"] = "_family_view"
ROUTES["/api/client-error"] = 256  # {"name", "at"}: the page could not draw the state it received
ACTIONS["/api/client-error"] = "_client_error"
ROUTES["/api/family-print"] = 256  # {}: a 10-minute link that opens the print page ("open as a page")
ACTIONS["/api/family-print"] = "_family_print"
ROUTES["/api/my-music"] = MAX_BODY
ACTIONS["/api/my-music"] = "_my_music"
ROUTES["/api/radio-programs"] = 8192
ACTIONS["/api/radio-programs"] = "_radio_programs"
FAMILY_KEYS = {"check": ({"id", "done"}, set()), "add": ({"list", "text"}, {"day"}), "edit": ({"id"}, {"text", "day"}),
               "remove": ({"id"}, set()), "clear": (set(), set())}
FAMILY_ERRORS = {"text": 400, "unknown": 404, "full": 409, "shabbat": 409, "unavailable": 503}
# The owner's own picture for a project (project_art.py): a change needs the fresh owner login; the image read is a
# state-style read (the page keeps images in memory, so it needs bytes behind the owner's login, not a public URL).
ROUTES["/api/project-art"] = 1_500_000  # {"action": "set", "project", "data": base64} | {"action": "scene", "project", "scene"} | {"action": "clear", "project"}
ACTIONS["/api/project-art"] = "_project_art"
ROUTES["/api/project-art-image"] = 256  # {"name": <file stem>}: the picture's bytes
ACTIONS["/api/project-art-image"] = "_project_art_image"
ART_KEYS = {"set": {"project", "data"}, "scene": {"project", "scene"}, "clear": {"project"}}
ART_ERRORS = {"bad": 400, "type": 415, "needs_pillow": 415, "size": 413, "decode": 400}
ROUTES["/api/mac-tools"] = 256  # {"action": "dry-run"|"clean"|"maintenance", "confirm"?}: starts a Mac maintenance job (mac_tools.py)
ACTIONS["/api/mac-tools"] = "_mac_tools"
ROUTES["/api/mac-tools-status"] = MAX_BODY  # {}: the running job, the last report and the Mac's state; a read, like /api/state
ACTIONS["/api/mac-tools-status"] = "_mac_tools_status"
for _voice_route, _voice_size in (("start", 64), ("audio", 22000), ("heartbeat", 128),
                                  ("events", 128), ("stop", 128), ("confirm", 128)):
    ROUTES["/api/voice-copilot/" + _voice_route] = _voice_size
    ACTIONS["/api/voice-copilot/" + _voice_route] = "_voice_copilot"
YOUTUBE_KEYS = {"connect": set(), "arm": set(), "start": {"armed"}, "stop": set(), "disconnect": set(),
                "enable": {"on"}, "projects": {"keys"}, "mirror-arm": set(),
                "mirror-on": {"armed"}, "mirror-off": set()}
YOUTUBE_WHILE_OFF = ("stop", "disconnect", "mirror-off")  # they only reduce exposure: allowed while mirroring is off
OAUTH_DEADLINE = 20  # seconds the sign-in callback may take (one token exchange with Google)
MAX_OAUTH_PATH = 4096
RADIO_TOKEN_TTL = 600
RADIO_FILE = re.compile(r"live\.m3u8|segment-[0-9]{1,20}\.ts")
RADIO_FRESH = 15  # stale/crashed encoders must not leave playable audio behind


def radio_token(secret: str, owner: int, now: Optional[float] = None) -> str:
    expiry = int(time.time() if now is None else now) + RADIO_TOKEN_TTL
    payload = f"radio-player:{owner}:{expiry}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{owner}.{expiry}.{signature}"


def valid_radio_token(token: str, secret: str, owner: int, now: Optional[float] = None) -> bool:
    if not secret or not owner or not isinstance(token, str) or len(token) > 128:
        return False
    if not re.fullmatch(r"[0-9]{1,20}\.[0-9]{1,12}\.[0-9a-f]{64}", token):
        return False
    who, expiry, signature = token.split(".")
    now = time.time() if now is None else now
    if int(who) != owner or not now < int(expiry) <= now + RADIO_TOKEN_TTL:
        return False
    want = hmac.new(secret.encode(), f"radio-player:{who}:{expiry}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature)


def radio_player_on(cfg: Config) -> bool:
    from . import radio
    return bool(cfg.radio) and radio.settings(cfg)["radio_player"]  # always true while the group stream is off


def radio_audio(name: str) -> bytes:
    """Regular files beneath fresh private HLS; legacy HLS only while an older radio still runs."""
    from . import buildstamp, radio
    if not RADIO_FILE.fullmatch(name):
        raise OSError("not a radio file")
    root_fd = None
    for directory in (radio.PRIVATE_HLS, "hls"):
        if directory == "hls" and "radio" not in buildstamp.stale():
            break
        try:
            candidate = os.open(radio.folder() / directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            playlist = os.stat("live.m3u8", dir_fd=candidate, follow_symlinks=False)
            if not stat.S_ISREG(playlist.st_mode) or time.time() - playlist.st_mtime > RADIO_FRESH:
                continue
            root_fd = candidate
            break
        except OSError:
            pass
        finally:
            if root_fd is None:
                os.close(candidate)
    if root_fd is None:
        raise OSError("radio off")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    finally:
        os.close(root_fd)
    with os.fdopen(fd, "rb") as stream:
        st = os.fstat(stream.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > 256_000:
            raise OSError("not a radio file")
        return stream.read(256_001)


def listen_now(lang: str) -> dict:
    """{"title", "updated"} for the listen link's lock screen (radio_now.py): what the radio is saying, or the
    station's name while the radio is off or has said nothing yet."""
    from . import radio, radio_now
    station = listen_link.TEXT["he" if str(lang).startswith("he") else "en"]["station"]
    try:
        radio_audio("live.m3u8")
    except OSError:
        return {"title": station, "updated": 0}
    now = radio_now.read(radio.folder())
    return {"title": now.get("title") or station, "updated": now.get("updated", 0)}


ROUTES["/api/radio-player"] = MAX_BODY
# The private "Listen in Safari" link (listen_link.py): {"action": "create"|"revoke"}, behind the fresh owner login.
ROUTES["/api/listen-link"] = 256
ACTIONS["/api/listen-link"] = "_listen_link"
LISTEN_KEYS = {"create": set(), "revoke": set()}
# Widget devices (device_tokens.py). Pairing and the Settings list need the fresh owner login like every action. The
# two widget routes take only a device token, in the Authorization header: never initData, never a URL query string.
ROUTES["/api/device-pair"] = 256  # {"label"?}: a new device token, returned once
ACTIONS["/api/device-pair"] = "_device_pair"
ROUTES["/api/devices"] = 256  # {"action": "list"} | {"action": "revoke", "id"} | {"action": "revoke-all"}
ACTIONS["/api/devices"] = "_devices"
DEVICE_KEYS = {"list": set(), "revoke": {"id"}, "revoke-all": set()}
ROUTES["/api/widget-state"] = MAX_BODY  # {}: the widget's read-only snapshot
ROUTES["/api/widget-decide"] = 256  # {"id", "choice"}: an answer to a decision that is not sensitive
ROUTES["/api/live-activity-token"] = 768
WIDGET_ROUTES = ("/api/widget-state", "/api/widget-decide")
BEARER = re.compile(r"Bearer (hgw_[A-Za-z0-9_-]{43})")  # device_tokens.TOKEN
WIDGET_CALLS = 30  # requests per device per minute; the app refreshes every few minutes at most
_widget_calls: dict = {}
_widget_lock = threading.Lock()
# What a widget shows, field by field (HarnessgramCockpitKit's parser reads these); "<field>_he" rides along.
WIDGET_DECISION = ("id", "title", "yes", "no", "topic", "url", "link", "stale", "age", "sensitive")
WIDGET_SESSION = ("id", "name", "state", "now", "url")
WIDGET_CARD = ("id", "kind", "title", "topic", "steps", "at", "percent", "topic_url", "finished", "ended", "stale", "updated")


def _widget_rate(device: str, now: Optional[float] = None) -> bool:
    """True while this device stays within WIDGET_CALLS a minute (kept in memory: a restart forgets it)."""
    now = time.monotonic() if now is None else now
    with _widget_lock:
        calls = [t for t in _widget_calls.get(device, []) if now - t < 60]
        if len(calls) >= WIDGET_CALLS:
            _widget_calls[device] = calls
            return False
        _widget_calls[device] = calls + [now]
        for other in [d for d, c in _widget_calls.items() if not c or now - c[-1] >= 60]:
            _widget_calls.pop(other, None)
        return True


def bot_id_of(token: str) -> Optional[int]:
    """The bridge bot's numeric user id, so the HarnessGram app can pin the bot by id from an authenticated source.
    Telegram's bot token starts with that id (the `id` getMe returns), so it is read from the token already held in
    memory: no network call on a request, and only the id, never the secret part, ever leaves this function."""
    head, sep, _ = str(token or "").partition(":")
    if not sep or not head.isdigit() or len(head) > 20:
        return None
    return int(head)


def _pick(row: dict, fields: tuple) -> dict:
    out = {k: row.get(k) for k in fields if k in row}
    out.update({k + "_he": row[k + "_he"] for k in fields if k + "_he" in row})
    return out


def widget_view(cfg: Config, state: dict, bot_id: Optional[int] = None) -> dict:
    """The widget's snapshot: the open decisions (with "sensitive"), the working sessions, the progress cards and the
    radio's on/off. A copy of the owner's state, cut to what a home or lock screen shows; no detail, no notes."""
    from . import waiting
    enabled = radio_player_on(cfg)
    try:
        live = enabled and bool(radio_audio("live.m3u8"))
    except OSError:
        live = False
    return {"ok": True, "now": state.get("now"), "bot_id": bot_id, "waiting_count": waiting.waiting_count(cfg),
            "appSwitches": state.get("appSwitches") or cockpit_v2.app_switches(),
            "decisions": [_pick(d, WIDGET_DECISION) for d in state.get("decisions") or []],
            "sessions": [_pick(s, WIDGET_SESSION) for s in state.get("sessions") or [] if s.get("state") == "working"],
            "cards": [_pick(c, WIDGET_CARD) for c in state.get("cards") or [] if c.get("kind") == "progress"],
            "radio_player": {"ok": True, "enabled": enabled, "on": live},
            "today": state.get("ownerToday") or _EMPTY_TODAY}


_EMPTY_TODAY = {"done_today": 0, "total_today": 0, "waiting": []}


def _widget_today(cfg: Config, now: float, rows: list) -> dict:
    """The widget's "today" block from rows already read; a failure gives an empty block, never a failed state."""
    try:
        return owner_requests.today(cfg, now, rows=rows)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        from .inbox import poll_log
        poll_log(f"widget today failed: {type(exc).__name__}")
        return dict(_EMPTY_TODAY, waiting=[])


def app_csp(host: str = "") -> str:
    """The policy of an app's page: inline code only (the page is one file), media and images from this server,
    no network calls (the API goes through the panel page), framed only by the panel inside Telegram, and
    sandboxed even when it is opened on its own. The request's own host is listed beside 'self' in case a
    browser resolves 'self' against the frame's opaque origin."""
    own = "'self'" + (" " + host if host and HOST.fullmatch(host) else "")
    return ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            f"img-src {own} data: blob:; media-src {own} blob:; font-src {own} data:; connect-src 'none'; "
            f"base-uri 'none'; form-action 'none'; frame-ancestors 'self' {FRAME_ANCESTORS}; "
            "sandbox allow-scripts allow-forms")


def query_auth(query: str) -> str:
    """The app token carried in `?auth=`, or "" when it is missing, repeated or malformed."""
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True) if query else []
    except ValueError:
        return ""
    found = [v for k, v in pairs if k == AUTH_PARAM]
    return found[0] if len(found) == 1 else ""


def app_token(secret: str, owner: int, name: str, now: Optional[float] = None,
              expires: Optional[int] = None) -> str:
    """An app-only capability; domain-separated from Telegram and the radio player's signing key."""
    expiry = int(time.time() if now is None else now) + APP_TOKEN_TTL
    if expires is not None:
        expiry = min(expiry, expires)
    payload = f"app1.{name}.{owner}.{expiry}"
    key = hmac.new(secret.encode(), b"cockpit-app-token:v1", hashlib.sha256).digest()
    signature = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def valid_app_token(token: str, secret: str, owner: int, name: str, now: Optional[float] = None) -> bool:
    if not secret or not owner or not isinstance(token, str) or len(token) > 160:
        return False
    if not re.fullmatch(r"app1\.[a-z0-9-]{1,24}\.[0-9]{1,20}\.[0-9]{1,12}\.[0-9a-f]{64}", token):
        return False
    _, app, who, expiry, signature = token.split(".")
    now = time.time() if now is None else now
    if app != name or int(who) != owner or not now < int(expiry) <= now + APP_TOKEN_TTL:
        return False
    key = hmac.new(secret.encode(), b"cockpit-app-token:v1", hashlib.sha256).digest()
    want = hmac.new(key, token.rsplit(".", 1)[0].encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature)


def card_media_token(secret: str, owner: int, ident: str, expires: int) -> str:
    payload = f"card1.{ident}.{owner}.{expires}"
    key = hmac.new(secret.encode(), b"cockpit-card-media:v1", hashlib.sha256).digest()
    return payload + "." + hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def valid_card_media_token(token: str, secret: str, owner: int, ident: str, now=None) -> bool:
    if not secret or not owner or not isinstance(token, str) or not re.fullmatch(
            r"card1\.[0-9a-f]{32}\.[0-9]{1,20}\.[0-9]{1,12}\.[0-9a-f]{64}", token):
        return False
    _, image_id, who, expiry, _ = token.split(".")
    now = time.time() if now is None else now
    return (image_id == ident and int(who) == owner and now < int(expiry) <= now + APP_TOKEN_TTL
            and hmac.compare_digest(token, card_media_token(secret, owner, ident, int(expiry))))


# --- Telegram initData -------------------------------------------------------------------

def validate_init_data(init_data: str, bot_token: str, owner_id: int, now: Optional[float] = None,
                       max_age: int = MAX_AGE) -> bool:
    """True only for a fresh (at most `max_age` seconds old), correctly signed initData whose user is the owner."""
    if not init_data or len(init_data) > MAX_INIT_DATA or not bot_token or not owner_id:
        return False
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    fields = dict(pairs)
    if len(fields) != len(pairs):
        return False  # a repeated key is never legitimate
    given = fields.pop("hash", "")
    if not given:
        return False
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want.encode(), given.lower().encode()):
        return False
    now = time.time() if now is None else now
    try:
        auth_date = int(fields.get("auth_date", ""))
    except ValueError:
        return False
    if auth_date > now + FUTURE_SLACK or now - auth_date > max_age:
        return False
    try:
        user = json.loads(fields.get("user", ""))
        return isinstance(user, dict) and type(user.get("id")) is int and user["id"] == owner_id
    except ValueError:
        return False


# --- what the panel shows ----------------------------------------------------------------

def _session_account(session: str) -> str:
    try:
        return usage.claude_account(session)
    except Exception:
        return ""


def _age(now: float, then: float) -> int:
    return max(0, int(now - then))


def topic_param(chat: int, thread: int) -> str:
    """The Mini App start parameter that opens the panel on one topic (`t` + the board's short key hash).
    Chat and thread ids never leave the server; Telegram signs the parameter inside initData."""
    return "t" + board.key_hash(board.key_of(chat, thread))


def start_param_of(init_data: str) -> str:
    """The start parameter of an initData that validate_init_data already accepted, or ""."""
    value = dict(parse_qsl(init_data, keep_blank_values=True)).get("start_param", "")
    return value if START_PARAM.fullmatch(value) else ""


def waiting_for_owner(topics: TopicMap, now: float, here: Optional[tuple] = None,
                      states: Optional[dict] = None, *, reminder_limit: Optional[int] = MAX_REMINDERS) -> tuple:
    """The open decisions and the open pin-board reminders, newest first, with the title of and a link to the
    topic each belongs to; each is marked "here" when it belongs to the topic `here` = (chat, thread). With
    the sessions' `states` ({session: working|idle|ended}), a decision that no longer waits on the owner says
    why under "stale" (panel_view.stale_reasons); "" when it is fresh. Session ids stay on the server."""
    places = topics.all_places()
    by_session = {s: " ".join(str(e.get("name") or s[:8]).split()) for s, e in places.items()}
    by_place = {(e.get("chat"), e.get("thread")): by_session[s] for s, e in places.items()}
    records = decisions.open_decisions()
    stale = panel_view.stale_reasons(records, states, now) if states is not None else {}
    asked = [{"id": r["id"], "title": r["title"], "detail": r["detail"], "yes": r["yes"], "no": r["no"],
              "topic": by_session.get(r["session"], ""), "age": _age(now, r["created"]),
              "stale": stale.get(r["id"], ""), "old": panel_view.is_old(now, r["created"]), "url": panel_view.topic_url(r["chat"], r["thread"]),
              "link": panel_view.waiting_link(r),
              "here": (r["chat"], r["thread"]) == here,
              # a Claude Code permission request: its exact text (shown as is, never translated), Yes only if shown whole
              **({"request": r.get("request", ""), "decline_only": bool(r.get("decline_only", True))}
                 if r.get("perm") else {}),
              # the HarnessGram app answers a sensitive one only in the app after Face ID, never from a widget
              "sensitive": sensitive_buttons.decision_sensitive(r),
              **({"rich_decision": True} if getattr(topics.cfg, "rich_decisions_enabled", False) else {}),
              **(_decision_extras(r, now) if getattr(topics.cfg, "rich_decisions_enabled", False) else {})}
             for r in sorted(records, key=lambda r: (-r["created"], r["id"]))]
    found = board.open_reminders()
    reminders = [{"text": i["s"], "due": i.get("d", ""), "topic": by_place.get((chat, thread), ""),
                  "age": _age(now, i["at"]), "url": panel_view.topic_url(chat, thread), "here": (chat, thread) == here,
                  "ref": f"{board.key_hash(board.key_of(chat, thread))}:{i['i']}" if type(i.get("i")) is int else "",
                  "old": reminder_lifecycle.is_old(i, now)}
                 for chat, thread, i in sorted(found, key=lambda f: -f[2]["at"])[:reminder_limit]]
    return asked, reminders


def _decision_extras(r: dict, now: float) -> dict:
    """What a decision carries beyond its text, each key only when present (an older record has none): "images"
    (opaque picture ids; the bytes only through /api/card-media-token and /card-media/<id>, behind the owner's
    login), "links" ([{"label", "url"}], https only, checked again on load) and "replies" (the owner's own comments
    sent without deciding, newest last, as [{"text", "age"}])."""
    out: dict = {}
    if r.get("images"):
        out["images"] = list(r["images"])
    if r.get("links"):
        out["links"] = [{"label": link["label"], "url": link["url"]} for link in r["links"]]
    if r.get("replies"):
        out["replies"] = [{"text": x["text"], "age": _age(now, x["at"])} for x in r["replies"][-decisions.REPLIES_SHOWN:]]
    return out


def open_tasks(topics: TopicMap, now: float, here: Optional[tuple] = None) -> list:
    """The project-wide checklist: every topic's checklist items that are not ticked yet, newest first, each
    with the title of and a link to its topic."""
    by_place = {(e.get("chat"), e.get("thread")): " ".join(str(e.get("name") or s[:8]).split())
                for s, e in topics.all_places().items()}
    return [{"text": i["s"], "topic": by_place.get((chat, thread), ""), "age": _age(now, i["at"]),
             "url": panel_view.topic_url(chat, thread), "here": (chat, thread) == here}
            for chat, thread, i in board.open_checks()[:MAX_REMINDERS]]


def stale_reasons_now(cfg: Config, now: Optional[float] = None) -> dict:
    """{decision id: reason} for the open decisions that no longer wait on the owner, computed as the panel shows them."""
    now = time.time() if now is None else now
    topics = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg)
    states = {e.session: e.state for e in collect(topics, now=now)}
    return panel_view.stale_reasons(decisions.open_decisions(), states, now)


def stale_ids(cfg: Config, now: Optional[float] = None) -> set:
    """The open decisions that no longer wait on the owner, computed as the panel shows them."""
    return set(stale_reasons_now(cfg, now))


def _topic_of(topics: TopicMap, start: str) -> Optional[dict]:
    """The topic a start parameter names: its place, session and title; None when no topic matches."""
    if not start:
        return None
    for session, e in topics.all_places().items():
        chat, thread = e.get("chat"), e.get("thread")
        if type(chat) is int and type(thread) is int and topic_param(chat, thread) == start:
            return {"chat": chat, "thread": thread, "session": session,
                    "name": " ".join(str(e.get("name") or session[:8]).split())}
    return None


def _board_view(chat: int, thread: int) -> dict:
    out = {"remind": [], "check": [], "link": []}
    for i in board.items_of(chat, thread):
        if i["k"] == "remind":
            out["remind"].append({"text": i["s"], "due": i.get("d", ""),
                                  "ref": f"{board.key_hash(board.key_of(chat, thread))}:{i['i']}",
                                  "age": _age(time.time(), i["at"]),
                                  "old": reminder_lifecycle.is_old(i, time.time())})
        elif i["k"] == "check":
            out["check"].append({"text": i["s"], "done": i["x"]})
        else:
            out["link"].append({"text": i["s"], "url": i["u"]})
    return out


_NOT_TEXT = frozenset(("id", "ref", "state", "account", "model"))  # identifiers, never agent prose


@functools.lru_cache(maxsize=16384)
def _clean_memo(value: str) -> str:
    return redact_channel(clean_text(value))


def _clean_string(value: str) -> str:
    """One agent-written string, filtered. The answer depends on the string alone and the same strings come
    back on every build (and in the final pass of the same build), so a short one is remembered."""
    return _clean_memo(value) if len(value) <= 2048 else redact_channel(clean_text(value))


def _secret_free(value, key: str = ""):
    """Every agent-written string inside `value` (rows, nested fields such as a card's options or figures)
    credential-filtered in place with the alerts channel's cleaning (creds.redact_channel)."""
    if isinstance(value, dict):
        if isinstance(value.get("label"), str) and isinstance(value.get("value"), str):
            value["label"], value["value"] = redact_pair(clean_text(value["label"]), clean_text(value["value"]))
        for k, v in value.items():
            value[k] = clean_text(v) if isinstance(v, str) and k in _NOT_TEXT else _secret_free(v, k)
    elif isinstance(value, list):
        # These are server-generated capabilities' identifiers, validated by
        # cards._body; treating them as prose redacts the 32-hex media IDs.
        value[:] = [v if key == "images" and isinstance(v, str) and card_media.ID.fullmatch(v)
                    else _secret_free(v, key) for v in value]
    elif isinstance(value, str):
        return _clean_string(value)
    return value


def _hebrew(tr, rows, fields, waiting: list) -> None:
    """Add "<field>_he" beside each agent-written field: the Hebrew text, or None while it is being translated
    (counted in `waiting`). Nothing is added when no translation is needed or the translator is off."""
    if tr is None:
        return
    for row in rows:
        for field in fields:
            value = tr.he(row.get(field))
            if value is None:
                row[field + "_he"] = None
                waiting.append(field)
            elif value:
                row[field + "_he"] = value


def _hebrew_cards(tr, rows, waiting: list) -> None:
    """Translate all card copy, including nested figures and lists, beside the originals.

    Only protocol identifiers and links are exempt. New agent-written string fields are
    translated by default rather than depending on a second, easily outdated field list.
    String lists keep their wire shape and get a parallel <field>_he list.
    """
    if tr is None:
        return
    for row in rows:
        for field, value in list(row.items()):
            if field in ("id", "kind", "session", "url", "topic_url", "link", "images") or field.endswith("_he"):
                continue
            if isinstance(value, str):
                _hebrew(tr, [row], (field,), waiting)
            elif isinstance(value, dict):
                _hebrew_cards(tr, [value], waiting)
            elif isinstance(value, list):
                translated = []
                has_translation = False
                for item in value:
                    if isinstance(item, dict):
                        _hebrew_cards(tr, [item], waiting)
                    if isinstance(item, str):
                        copy = {"text": item}
                        _hebrew(tr, [copy], ("text",), waiting)
                        translated.append(copy.get("text_he"))
                        has_translation |= "text_he" in copy
                    else:
                        translated.append(None)
                if has_translation:
                    row[field + "_he"] = translated


def _hebrew_view(tr, value, waiting: list) -> None:
    """Translate late presentation copy, including work-map lines, never protocol values.

    Presentation is built after the classic lists; its newly derived project
    names and work-map text need their own pass before pending is counted.
    """
    if isinstance(value, list):
        for item in value:
            _hebrew_view(tr, item, waiting)
    elif isinstance(value, dict):
        fields = ("name", "title", "detail", "summary", "text", "what", "impact", "note", "project_name", "project")
        _hebrew(tr, [value], tuple(f for f in fields if isinstance(value.get(f), str)), waiting)
        for key, item in list(value.items()):
            if not key.endswith("_he") and isinstance(item, (dict, list)):
                _hebrew_view(tr, item, waiting)


def _background(path: Optional[str], state: str, now: Optional[float] = None) -> int:
    """How many background agents and commands a live session is running (0 for an ended one). Read again only
    when the transcript changed, or every ten minutes (a job that never reported ends after hours)."""
    if not path or state == "ended":
        return 0
    try:
        bucket = int((time.time() if now is None else now) // 600)
        return auto_progress.cached(path, f"bg{bucket}", lambda p: len(usage.background_of(p)), 0)
    except Exception:
        return 0


def _live_activity(session: str, now: float) -> dict:
    """The already-rendered progress and agent state for a session, without any transcript or network I/O."""
    from .config import read_map, safe_name
    progress_state = read_map(state_dir() / "progress" / f"{safe_name(session)}.msg")
    steps = progress_state.get("activity", [])
    if not isinstance(steps, list):
        steps = []
    agent_state = read_map(state_dir() / "agents-cards" / f"{safe_name(session)}.json")
    cards_by_id = agent_state.get("cards", {})
    cards_list = list(cards_by_id.values()) if isinstance(cards_by_id, dict) else []
    agents = []
    for card in cards_list[-2:]:
        if isinstance(card, dict):
            agents.extend(a for a in card.get("agents", []) if isinstance(a, dict) and not a.get("moved"))
    return {"steps": [str(x) for x in steps[-20:]], "agents": agents[-12:],
            "updated": progress_state.get("activity_at"), "ended": bool(progress_state.get("ended"))}


def _default_model() -> dict:
    """The model new sessions start with, and the choices the panel offers (models.MODELS)."""
    current = models.default_model()
    # The setting may hold an alias ("opus"): its choice is still shown as the selected one.
    return {"id": models.IDS.get(current.lower(), current), "label": models.display_name(current),
            "choices": [{"key": key, "id": ident, "label": name} for key, ident, name in models.MODELS]}


def quota_labels(names) -> dict:
    """One distinct label per account: the short e-mail label, plus the account's own name when two accounts
    would otherwise show the same label (a stale cached e-mail must not make two quota rows look identical)."""
    base = {n: accounts.label(n) for n in names}
    seen: dict = {}
    for label in base.values():
        seen[label] = seen.get(label, 0) + 1
    return {n: (f"{label} ({n})" if seen[label] > 1 and label != n else label) for n, label in base.items()}


def _compact_at() -> int:
    try:
        from .compact import auto_compact_limit
        return auto_compact_limit()
    except Exception:
        return 0


def build_state(cfg: Config, now: Optional[float] = None, start: str = "", light: bool = False,
                tr: Optional["panel_text.Translator"] = None, full_ids: bool = False) -> dict:
    """Decisions, reminders, sessions and quota, from the same readers the bot's own menus use. With the
    start parameter of a session topic's pinned button, also that topic's own view under "topic".
    `light` skips transcript details and the Mac resource sampler.
    With a translator `tr`, agent-written text gets a Hebrew copy beside it (see _hebrew); "pending" counts
    the texts still being translated, so the page asks again shortly.
    `full_ids` (the MCP server only, never the page) adds each session's, card's and ledger row's full session id as
    "session_id" / "session", so a caller is matched exactly and never by the short id two sessions may share."""
    from . import waiting
    now = time.time() if now is None else now
    topics = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg)
    scope = _topic_of(topics, start)
    entries = collect(topics, now=now)
    native = {}
    if cfg.codex_harness_enabled is True:
        from .codex_bridge import sessions as codex_sessions
        from .sessions_menu import Entry
        native = codex_sessions()
        places = topics.all_places()
        for key, row in native.items():
            place = topics.place_of(key)
            if place and place.chat == home_chat(cfg):
                state = "working" if row["status"] in ("working", "waiting_for_approval") else "idle"
                age = max(0, now - (row["updated"] or now))
                name = places.get(key, {}).get("name") or "Codex"
                entries.append(Entry(key, name, place.thread, age, state, False))
        entries.sort(key=lambda e: (e.age is None, e.age or 0))
    settings = panel_settings.read()
    # v2 must be able to expand every reminder included in the shared waiting count.
    asked, reminders = waiting_for_owner(topics, now, (scope["chat"], scope["thread"]) if scope else None,
                                         {e.session: e.state for e in entries},
                                         reminder_limit=None if settings.get("ui_v2") in ("preview", "on") else MAX_REMINDERS)
    here = scope["session"] if scope else None
    shown = [e for e in entries if not e.closed or e.session == here]
    if here and not any(e.session == here for e in shown[:MAX_SESSIONS]):
        # the topic's own session always makes the list (review of #136: cut after
        # MAX_SESSIONS first, the topic's panel said its live session was gone)
        shown = shown[:MAX_SESSIONS - 1] + [e for e in shown if e.session == here][:1]
    usage_snapshot = session_usage.snapshot([(e.session, e.name) for e in entries if e.session not in native],
                                            now, cfg.usage_prices)
    sessions = []
    paths = {}
    for e in shown[:MAX_SESSIONS]:
        if e.session in native:
            from .codex_bridge import total_tokens
            rec = native[e.session]
            chat = home_chat(cfg)
            tokens = total_tokens(rec.get("usage") or {})
            sessions.append({"id": e.session, "name": e.name, "state": e.state, "harness": "codex",
                             "native_status": rec["status"], "model": rec["model"], "effort": rec["effort"],
                             "delivery_unconfirmed": rec["delivery_unconfirmed"],
                             "age": int(e.age), "codex_tokens": tokens, "bg": 0,
                             "here": bool(scope) and e.session == scope["session"],
                             "url": panel_view.topic_url(chat, e.thread),
                             **{action + "_url": panel_view.waiting_link(
                                 {"chat": chat, "thread": e.thread, "msg": rec[action]})
                                 for action in ("stop", "answer") if rec[action]}})
            continue
        path = None if light else status.transcript_of(e.session)
        paths[e.session] = path
        # every transcript read is cached by the file's size and mtime: a frequent poll reads only what changed
        ctx, model = auto_progress.cached(path, "usage", status.usage_of, (0, ""))
        model = usage.session_model(e.session, path)[0] or models.display_name(model)  # "Opus 5.5", after a /model too
        live = not light and e.state != "ended"
        folder, branch = auto_progress.cached(path, "place", status.workplace, ("", ""))
        sessions.append({"id": e.session[:8], "name": e.name, "state": e.state,
                         "forkable": bool(re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", e.session)),
                         "age": None if e.age is None else int(e.age), "context": ctx, "model": model,
                         "account": _session_account(e.session), "bg": _background(path, e.state, now),
                         "credit": credit_mode.status_line(e.session, now),
                         "now": panel_view.current_step(e.session) if live and e.state == "working" else "",
                         "summary": auto_progress.cached(path, "summary", panel_view.last_summary, "") if live else "",
                         "url": panel_view.topic_url(home_chat(cfg) if e.chat is None else e.chat, e.thread),
                         "here": bool(scope) and e.session == scope["session"],
                         "folder": folder, "branch": "" if branch in ("HEAD", "") else branch,
                         "waiting": [] if light or e.state == "ended" else waiting_words(e.session, now),
                         "compact": _session_compact(e.session),  # its own threshold; None = the global one
                         "left": left_behind(e), "usage": usage_snapshot["sessions"].get(e.session),
                         **({"activity": _live_activity(e.session, now)}
                            if settings.get("live_activity_cockpit") else {}),
                         **({"session_id": e.session} if full_ids else {})})
    names = {e.session: e.name for e in entries}
    quota = []
    labels = quota_labels(accounts.names())
    for name in accounts.names():
        u = accounts._read(accounts.usage_path(name), {})
        if not u:
            continue
        quota.append({"account": name, "label": labels[name],
                      # weekly only, with its reset and the banked resets when known (no 5-hour window)
                      "weekly": u.get("weekly"), "weekly_resets": u.get("weekly_resets") or None,
                      "banked": usage.banked_resets(u),
                      "at": u.get("at")})
    ledger_rows = [dict(r, steps=r["steps"][-owner_requests.MAX_NOTES:])
                   for r in owner_requests.read(now, owner_requests.ledger_dir(cfg), history=True, dormant_after=owner_requests.dormant_seconds(cfg))
                   if r["status"] != "done" or 0 <= now - r["updated"] <= owner_requests.DONE_DAYS]
    out = {"ok": True, "now": int(now), "waiting_count": waiting.waiting_count(cfg, now=now, requests=ledger_rows), "decisions": asked, "reminders": reminders, "sessions": sessions,
           "usage_totals": usage_snapshot["totals"], "quota": quota, "compact_at": _compact_at(), "default_model": _default_model(),
           "settings": settings, "appSwitches": cockpit_v2.app_switches()}
    out["tower_pending"] = tower_home.pending(now)
    from . import hook_health
    out["hook_health"] = hook_health.view()
    if not light:
        out["mac"] = mac_resources.snapshot()
    out["bank"] = bank.view()
    out["ownerToday"] = _widget_today(cfg, now, ledger_rows)  # the widget's "today", inside the cached state
    out["apps"] = cockpit_apps.listing()
    out["tasks"] = open_tasks(topics, now, (scope["chat"], scope["thread"]) if scope else None)
    out["requests"] = [dict(r, steps=r["steps"][-owner_requests.MAX_NOTES:]) for r in ledger_rows
                       if r["status"] != "done" or 0 <= now - r["updated"] <= owner_requests.DONE_DAYS]
    ended = {e.session for e in entries if e.state == "ended"}
    states = {e.session: e.state for e in entries}
    if not light:
        _auto_cards(cfg, [e for e in entries if e.session not in native], paths,
                    {s["id"]: s["bg"] for s in sessions}, now)
    # each card's way into its session's own topic: the same t.me link the sessions list carries (no chat ids)
    links = {e.session: panel_view.topic_url(home_chat(cfg) if e.chat is None else e.chat, e.thread) for e in entries}
    backgrounds = {e.session: s["bg"] for e, s in zip(shown[:MAX_SESSIONS], sessions)}  # full ids: short ones can collide
    memo: dict = {}  # per session, for this build: a session's cards share one task list and one activity line
    out["cards"] = [dict(cards.view(r, now), topic=names.get(r["session"], ""), topic_url=links.get(r["session"], ""),
                         **({"link": panel_view.waiting_link(r)} if r["kind"] in cards.ASKS else {}),
                         **_card_activity(r, paths, backgrounds, states, now, memo),
                         ended=r["session"] in ended and (r["kind"] != "progress" or bool(r.get("auto_closed"))),
                         auto_closed=bool(r.get("auto_closed")),
                         finished=cards.finished(r),
                         stale=r["kind"] == "progress" and r["state"] == "open" and not r.get("automatic") and
                               _card_stale(r, states, now, memo),
                         old=panel_view.is_old(now, r.get("updated")) or
                             cards.finished(r) and now - r["updated"] >= cards.PROGRESS_FOLD_AFTER,
                         here=bool(scope) and r["session"] == scope["session"],
                         **({"session_id": r["session"]} if full_ids else {}))
                    for r in cards.reconcile_progress(ended, now)]
    out["deliveries"] = [] if light else delivery_receipts(now=now)
    for receipt in out["deliveries"]:
        receipt["here"] = bool(here) and receipt["session"] == here[:8]
    if scope:
        out["topic"] = {"name": scope["name"], "board": _board_view(scope["chat"], scope["thread"])}
    if brief.on():  # the Morning Brief home (brief.py): only behind its switch, so the classic home is untouched
        try:
            out["brief"] = brief.build(cfg, out, entries, now)
        except Exception:
            out["brief"] = None
    # The response boundary: agent-written text leaves this machine for the browser (and the radio), so it is
    # credential-filtered here too - also what was saved before the creation-time filter existed.
    for key in ("decisions", "reminders", "requests", "tasks", "sessions", "cards", "topic", "deliveries", "brief", "usage_totals"):
        _secret_free(out.get(key))
    waiting: list = []
    _secret_free(ledger_rows)
    # Archived titles must not take the translator's bounded queue ahead of live work.
    owner_requests.translate(sorted(ledger_rows, key=lambda r: r["status"] == "done"), tr, waiting)
    if scope:
        out["topic"]["status_cards"] = topic_status.cards(
            ledger_rows, scope["session"], now, lang=cfg.ui_language)
    # Keep the existing checklist's translated fields and seven-day completion window.
    out["requests"] = [dict(r, steps=r["steps"][-owner_requests.MAX_NOTES:]) for r in ledger_rows
                       if r["status"] != "done" or 0 <= now - r["updated"] <= owner_requests.DONE_DAYS]
    from . import request_sweep
    out["topic_cards"] = topic_cards.slim(topic_cards.build(ledger_rows, now, request_sweep.cached_states(cfg)), now)
    try:  # open team items nobody moved (idle_lines.py): after the translation, so their Hebrew titles come along
        out["idle_lines"] = idle_lines.view(cfg, out["requests"], now)
    except Exception:
        out["idle_lines"] = {"count": 0, "items": []}
    # Only what the list shows goes out; the full text, note and steps come from /api/request-detail on expand.
    out["requests"] = owner_requests.slim(out["requests"], now, keep_session=full_ids)
    if scope:
        _hebrew(tr, [out["topic"]], ("name",), waiting)
        for kind in ("remind", "check", "link"):
            _hebrew(tr, out["topic"]["board"][kind], ("text",), waiting)
    _hebrew(tr, asked, ("title", "detail", "yes", "no", "topic"), waiting)
    _hebrew(tr, reminders, ("text", "topic"), waiting)
    _hebrew(tr, out["tasks"], ("text", "topic"), waiting)
    _hebrew(tr, sessions, ("name", "summary", "now"), waiting)
    if out["usage_totals"]:
        _hebrew(tr, out["usage_totals"]["top"], ("name",), waiting)
    _hebrew_cards(tr, out["cards"], waiting)
    _hebrew(tr, out["deliveries"], ("title",), waiting)
    if out.get("brief"):
        _hebrew(tr, out["brief"]["lines"], ("text",), waiting)
        _hebrew(tr, out["brief"]["waiting"], ("text", "note", "detail"), waiting)
        _hebrew(tr, out["brief"]["noticed"], ("text",), waiting)
    # The last pass, after the translations were added: a translation (or a cached one) is returned clean too.
    for key in ("decisions", "reminders", "requests", "tasks", "sessions", "cards", "topic", "deliveries", "brief", "usage_totals",
                "idle_lines", "topic_cards"):
        _secret_free(out.get(key))
    out["pending"] = len(waiting)
    if out["settings"].get("ui_v2") in ("preview", "on"):
        # Derive presentation only from the classic response. In particular,
        # V2 must not run the brief's additional readers when its switch is off.
        out["cockpit_v2"] = cockpit_v2.presentation(cfg, out, entries, now)
        try:  # today's "Good morning" card (briefing.py): present only when one was made today and not put away
            morning = briefing.card(cfg, now)
            if morning:
                _secret_free(morning)  # filter before any text can enter the translation queue
                for section in ("overnight", "waiting"):
                    _hebrew(tr, morning.get(section, []), ("what", "impact"), waiting)
                out["morning"] = morning
        except Exception:
            pass
        # Presentation also annotates decisions/cards with project identity.
        # Apply the same boundary filter to those newly derived fields.
        out["addons"] = addons.flags(cfg)  # the page hides what is switched off (cockpit_v2_addons.js)
        if addons.enabled(cfg, "home_context"):
            from . import autopilot, home_context, home_inputs
            moment = datetime.fromtimestamp(now, schedule.zone(cfg))
            try:
                away = autopilot.away_active()
            except Exception:
                away = False
            out["home_context"] = home_context.context(cfg, moment, inputs={
                "shabbat_times": home_inputs.rest_state(cfg, moment), "decisions": out.get("decisions", ()),
                "sessions": out.get("sessions", ()), "cards": out.get("cards", ()),
                **home_inputs.at_mac_inputs(out.get("sessions", ()), now, away)})
        try:  # old topics the owner may close (topic_cleanup.py); only behind ui_v2 and its add-on switch
            out["topic_cleanup"] = topic_cleanup.view(cfg, topics, now) if out["addons"]["topic_cleanup"] else None
        except Exception:
            out["topic_cleanup"] = None
        # Late readers may add fresh agent/config copy. Scrub it before any
        # engine sees it, then scrub returned translations at the boundary too.
        for key in ("cockpit_v2", "decisions", "cards", "topic_cleanup"):
            _secret_free(out.get(key))
        _hebrew(tr, out["decisions"] + out["cards"], ("project_name", "recommendation"), waiting)
        _hebrew_view(tr, out["cockpit_v2"], waiting)
        _hebrew_view(tr, out["topic_cleanup"], waiting)
        for key in ("cockpit_v2", "decisions", "cards", "topic_cleanup", "morning"):
            _secret_free(out.get(key))
    out["pending"] = len(waiting)
    if getattr(cfg, "radio", None):
        from . import radio, radio_room
        out["radio"] = dict(radio.operation(), on=bool(radio.job_pid()), **radio.minutes(cfg), **radio.mode(cfg))
        try:  # Settings shows the revoke row only while a listening link exists (listen_link.py)
            out["listen_link"] = {"on": addons.enabled(cfg, "listen_link"), "active": listen_link.active()}
        except Exception:
            out["listen_link"] = {"on": False, "active": 0}
        try:
            if not out["radio"]["on"] and not out["radio"]["pending"]:
                radio_room.stop_live("radio_off")  # no station runs it: the room is over, never left "open"
            out["room"] = radio_room.view(cfg, now)  # the brainstorming room (its text is cleaned when stored)
        except Exception:
            pass
        try:
            from . import radio_podcast
            out["podcast"] = radio_podcast.view(cfg, now)
        except Exception:
            pass
        try:
            from . import radio_music
            out["radio_music"] = {"enabled": radio_music.enabled(), "now": radio_music.playback()}
        except Exception:
            out["radio_music"] = {"enabled": False, "now": {}}
    return out


def _fresh_activity(rec: dict, states: dict, now: float, memo: Optional[dict] = None) -> dict:
    if states.get(rec["session"]) != "working":
        return {}
    if memo is not None and ("activity", rec["session"]) in memo:
        row = memo[("activity", rec["session"])]
    else:
        row = live_activity.read(rec["session"])
        if memo is not None:
            memo[("activity", rec["session"])] = row
    return row if row and 0 <= now - row["at"] < 900 else {}


def _card_stale(rec: dict, states: dict, now: float, memo: Optional[dict] = None) -> bool:
    if _fresh_activity(rec, states, now, memo):
        return False
    if states.get(rec["session"]) == "working":
        return cards.is_stale(rec, now)
    return not cards.finished(rec) and now - max(rec["updated"], rec.get("live_at", 0)) >= 900


def _current_task(session: str, paths: dict, memo: Optional[dict]) -> str:
    """The subject of the session's in-progress task, as a card line. Read once per session and build."""
    if memo is not None and ("task", session) in memo:
        return memo[("task", session)]
    tasks = auto_progress.task_list(paths.get(session))
    line = cards.subject_line(next((t["content"] for t in tasks if t.get("status") == "in_progress"), ""))
    if memo is not None:
        memo[("task", session)] = line
    return line


def _card_activity(rec: dict, paths: dict, backgrounds: dict, states: dict, now: Optional[float] = None,
                   memo: Optional[dict] = None) -> dict:
    """Attach sanitized tool activity and the current task without changing the saved plan.
    `memo` (one dict for a whole build) keeps what is read per session, not per card."""
    now = time.time() if now is None else now
    if (rec["kind"] != "progress" or rec["state"] != "open"
            or cards.finished(rec) or states.get(rec["session"]) == "ended"):
        return {}
    out = {} if rec.get("automatic") else {"task": _current_task(rec["session"], paths, memo),
                                           "bg": backgrounds.get(rec["session"], 0)}
    activity = _fresh_activity(rec, states, now, memo)
    if activity:
        out["age"] = max(0, int(now - max(rec["updated"], activity["at"])))
        if not rec.get("live") or now - rec.get("live_at", 0) >= 300:
            out.update(activity=activity["text"], activity_age=max(0, int(now - activity["at"])))
    return out


def _auto_cards(cfg: Config, entries: list, paths: dict, bg: dict, now: float) -> None:
    """The bridge's own progress card for every working session whose agent made none (auto_progress.py),
    titled with the topic's name; closed once the session is idle or ended. Never raises."""
    try:
        from .i18n import t
        fallback = t("todo_plan", lang=cfg.target_language)
        rows, used = [], [p for p in paths.values() if p]
        # Only the visible session slice has fresh background counts. Keep the watcher count for
        # unshown idle sessions until its next authoritative scan; zero would close a running card.
        known_bg = {c["session"]: c.get("bg", 0) for c in cards.open_cards() if c.get("automatic")}
        for e in entries:
            row = {"session": e.session, "state": e.state, "bg": bg.get(e.session[:8], known_bg.get(e.session, 0)),
                   "chat": home_chat(cfg) if e.chat is None else e.chat, "thread": e.thread,
                   "title": (e.name or "").strip() or fallback}
            if e.state == "working" or row["bg"]:
                row["path"] = paths[e.session] if e.session in paths else status.transcript_of(e.session)
                used.append(row["path"])
            rows.append(row)
        auto_progress.sync(rows, now, cfg)
        auto_progress.forget(p for p in used if p)
    except Exception:
        pass


# --- the page ----------------------------------------------------------------------------

def render_page(lang: str, nonce: str, native: bool = False) -> bytes:
    return panel_page.render_page(lang, nonce, native=native)


def csp(nonce: str) -> str:
    return panel_page.csp(nonce, FRAME_ANCESTORS)


PANEL_ASSET_ROOT = Path(__file__).with_name("assets") / "panel"
PANEL_ICON_ROUTES = {
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon-32.png": ("favicon-32.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/icons/icon-192.png": ("icon-192.png", "image/png"),
    "/icons/icon-512.png": ("icon-512.png", "image/png"),
    "/icons/icon-maskable-512.png": ("icon-maskable-512.png", "image/png"),
    "/icons/icon-monochrome.svg": ("icon-monochrome.svg", "image/svg+xml"),
}


def web_manifest() -> bytes:
    return json.dumps({
        "name": "Cockpit", "short_name": "Cockpit", "start_url": "/", "display": "standalone",
        "background_color": "#83cee9", "theme_color": "#17365d",
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            {"src": "/icons/icon-monochrome.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "monochrome"},
        ],
    }, separators=(",", ":")).encode("utf-8")


# --- the server --------------------------------------------------------------------------

MIRROR_OFF = {"ok": False, "error": "mirroring is off"}


# Routes that change nothing: they need no action-fresh signature and leave the kept state alone.
READ_ONLY_ROUTES = ("/api/state", "/api/packs", "/api/project-art-image", "/api/radio-player", "/api/mac-tools-status", "/api/schedule-view", "/api/family-view",
                    "/api/client-error", "/api/widget-state", "/api/request-detail", "/api/request-target",
                    "/api/topic-detail", "/api/my-music")
# Only start opens a paid call and needs action freshness. Continuation is bound to its random call id
# AND the full owner login; it never carries typed/panel approval authority. Audio can trigger a voice relay.
VOICE_CONTINUATION_ROUTES = tuple("/api/voice-copilot/" + action for action in
                                  ("events", "audio", "heartbeat", "stop"))


class PanelHandler(http.server.BaseHTTPRequestHandler):
    timeout = TIMEOUT
    # HTTP/1.0 and close after every reply: behind a reusing proxy (Tailscale serve) leftover bytes
    # on a kept-alive connection could be read as another user's request.
    server_version = "panel"

    def setup(self):
        super().setup()
        self._deadline = threading.Timer(DEADLINE, self._cut)
        self._deadline.daemon = True
        self._deadline.start()

    def _cut(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self._deadline.cancel()
        try:
            super().finish()
        except OSError:
            pass
    sys_version = ""

    def _my_music(self) -> None:
        self._json(200, panel_my_music.load(self.server.cfg))

    def _radio_programs(self) -> None:
        from . import radio_programs
        req = self._request()
        if req is None:
            self._json(400, {"error": "bad request"})
            return
        try:
            action = req.get("action")
            if action == "view" and set(req) == {"action"}:
                result = radio_programs.view()
            elif action == "preset" and set(req) == {"action", "preset"}:
                radio_programs.select_preset(req["preset"])
                result = radio_programs.view()
            elif action == "save" and set(req) == {"action", "preset", "blocks"}:
                radio_programs.save(req["blocks"], req["preset"])
                result = radio_programs.view()
            elif action == "start" and set(req) <= {"action", "preset", "blocks"}:
                radio_programs.start(req.get("blocks"), req.get("preset"))
                result = radio_programs.view()
            else:
                raise ValueError("action")
        except (TypeError, ValueError):
            self._json(400, {"error": "bad request"})
            return
        self._json(200, {"ok": True, "program": result})

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self._json(code, {"error": "bad request" if code < 500 else "unavailable"})

    def _body_ok(self, limit: int) -> bool:
        """Refuse chunked or oversized bodies before reading; returns False after replying."""
        self._body = b""
        if self.headers.get("Transfer-Encoding") is not None:
            self._json(400, {"error": "bad request"})
            return False
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if length < 0 or length > limit:
            self._json(413, {"error": "too large"})
            return False
        if length:
            self._body = self.rfile.read(length)
        return True

    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[dict] = None) -> None:
        self.close_connection = True
        self.send_response(code)
        self.send_header("Connection", "close")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", (extra or {}).get("Cache-Control", "no-store"))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            if k != "Cache-Control":
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                   {"Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"})

    def _off(self) -> bool:
        """Mirroring is off (policy.py): every request gets the same fixed answer before anything is read,
        built or done."""
        from . import policy
        if policy.on():
            return False
        self._json(503, MIRROR_OFF)
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in PANEL_ICON_ROUTES or path == "/manifest.webmanifest":
            if not self._body_ok(0):
                return
            if path == "/manifest.webmanifest":
                self._send(200, web_manifest(), "application/manifest+json",
                           {"Cache-Control": "public, max-age=3600"})
                return
            name, content_type = PANEL_ICON_ROUTES[path]
            try:
                body = (PANEL_ASSET_ROOT / name).read_bytes()
            except OSError:
                self._json(404, {"error": "not found"})
                return
            self._send(200, body, content_type, {"Cache-Control": "public, max-age=31536000, immutable",
                       "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"})
            return
        if self.path.startswith(cockpit_v2.ASSET_PREFIX):
            if not self._body_ok(0):
                return
            name = urlsplit(self.path).path[len(cockpit_v2.ASSET_PREFIX):]
            if name not in {s + ".webp" for s in cockpit_v2.ASSETS}:
                self._json(404, {"error": "not found"})
                return
            try:
                body = (cockpit_v2.ASSET_ROOT / name).read_bytes()
            except OSError:
                self._json(404, {"error": "not found"})
                return
            self._send(200, body, "image/webp", {"Cache-Control": "public, max-age=31536000, immutable",
                       "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"})
            return
        if self._off():
            return
        if not self._body_ok(0):
            return
        if self.path.startswith("/oauth/youtube/"):
            self._youtube_get()
            return
        if self.path.startswith(google_calendar.COOKIE_PATH + "/"):
            self._google_get()
            return
        if self.path.startswith("/app/"):
            self._app_get()
            return
        if self.path.startswith("/card-media/"):
            self._card_media_get()
            return
        if urlsplit(self.path).path == "/family/print":
            self._family_print_get()
            return
        if self.path.startswith("/radio/"):
            self._radio_get()
            return
        if self.path.startswith("/listen/"):
            self._listen_get()
            return
        if urlsplit(self.path).path != "/":  # t.me/<bot>/<app> links add a tgWebAppStartParam query
            self._json(404, {"error": "not found"})
            return
        nonce = secrets.token_urlsafe(16)
        native = getattr(getattr(self.server, "cfg", None), "cockpit_native", False) is True
        self._send(200, render_page(self.server.lang, nonce, native=native), "text/html; charset=utf-8",
                   {"Content-Security-Policy": csp(nonce)})

    def _state_cache(self):
        return getattr(getattr(self, "server", None), "state_cache", None)

    def do_POST(self):
        # Anything but a state read may change what the state shows: the next read builds afresh.
        cache = self._state_cache()
        mutating = cache is not None and self.path not in READ_ONLY_ROUTES and self.path not in VOICE_CONTINUATION_ROUTES
        if mutating:
            cache.clear()
        try:
            self._post()
        finally:
            if mutating:
                cache.clear()

    def _post(self):
        if self.path == "/api/youtube":
            self._youtube_post()  # its own off check: stop and disconnect still work while off
            return
        if self._off():
            return
        if APP_API.fullmatch(self.path):
            self._app_api(APP_API.fullmatch(self.path).group(1))
            return
        if self.path not in ROUTES:
            self._json(404, {"error": "not found"})
            return
        if self.path == "/api/live-activity-token":
            self._live_activity_token()
            return
        if self.path in WIDGET_ROUTES:
            self._widget_post()  # a device token only; initData is never read on these two routes
            return
        decide = self.path not in READ_ONLY_ROUTES
        if not self._body_ok(ROUTES[self.path]):
            return
        init = self.headers.get(INIT_HEADER, "")
        if not validate_init_data(init, self.server.bot_token, self.server.owner_id,
                                  max_age=DECIDE_MAX_AGE if decide and self.path not in VOICE_CONTINUATION_ROUTES else MAX_AGE):
            self._json(403, {"error": "forbidden"})
            return
        if self.path == "/api/radio":
            self._radio()
            return
        if self.path == "/api/app-token":
            self._app_token(init)
            return
        if self.path == "/api/card-media-token":
            self._card_media_token(init)
            return
        if self.path == "/api/radio-player":
            enabled = radio_player_on(self.server.cfg)
            try:
                live = enabled and bool(radio_audio("live.m3u8"))
            except OSError:
                live = False
            self._json(200, {"ok": True, "enabled": enabled, "on": live,
                             "listen_link": enabled and addons.enabled(self.server.cfg, "listen_link"),
                             "token": radio_token(self.server.bot_token, self.server.owner_id) if enabled else "",
                             "expires": int(time.time()) + RADIO_TOKEN_TTL})
            return
        if self.path in ACTIONS:
            getattr(self, ACTIONS[self.path])()
            return
        if decide:
            self._decide()
            return
        self._extend(STATE_DEADLINE)  # the sender is the authenticated owner; the build itself can take seconds
        start = start_param_of(init)
        began = time.monotonic()
        try:
            # Requests that arrive together share one build, and its answer is reused for a few seconds. An answer
            # still waiting for translations is not kept: the page asks again shortly and must see them arrive.
            def build():
                return build_state(self.server.cfg, start=start, tr=self.server.translator)
            cache = self._state_cache()
            data = dict(build() if cache is None else cache.get(
                (self.server.owner_id, start), build, keep=lambda built: not built.get("pending")))
        except Exception as exc:
            log_failure("state build", exc)
            self._json(500, {"error": "unavailable"})
            return
        built = time.monotonic()
        data["youtube"] = self.server.youtube_view()
        extras = time.monotonic()
        data["bot_id"] = bot_id_of(self.server.bot_token)  # the app pins the bridge bot by this id (never the token)
        try:  # Settings lists the paired widget devices (device_tokens.py): labels and dates, never a hash
            data["widget_devices"] = device_tokens.listing()
        except Exception:
            data["widget_devices"] = []
        data["app_request"] = app_request.configured(self.server.cfg)  # the Tools tile appears only when it is set up
        data["mac_tools"] = mac_tools.actions(self.server.cfg) if mac_tools.configured(self.server.cfg) else False
        # This flag is emitted only after full owner authentication. A preview
        # cannot be selected by a query parameter or an app-scoped capability.
        data["ui_v2_enabled"] = cockpit_v2.enabled(data.get("settings", {}).get("ui_v2", "off"), owner=True)
        try:
            self._json(200, data)
        finally:  # also when the phone already gave up and the write fails (#339)
            log_slow("state", began, built=built, youtube=extras, sent=time.monotonic(), pending=data.get("pending", 0))

    def _radio_get(self) -> None:
        parts = urlsplit(self.path)
        try:
            pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            pairs = []
        token = pairs[0][1] if len(pairs) == 1 and pairs[0][0] == "token" else ""
        if len(self.path) > 512 or not valid_radio_token(token, self.server.bot_token, self.server.owner_id):
            self._json(403, {"error": "forbidden"})
            return
        name = parts.path.removeprefix("/radio/")
        if not RADIO_FILE.fullmatch(name) or not radio_player_on(self.server.cfg):
            self._json(404, {"error": "not found"})
            return
        try:
            body = radio_audio(name)
            if name == "live.m3u8":
                lines = body.decode("utf-8").splitlines()
                # Relative media URLs do NOT inherit the playlist's query. Sign each segment;
                # reject any unexpected URI instead of allowing the player to fetch it.
                if not lines or lines[0] != "#EXTM3U":
                    raise ValueError("bad playlist")
                for i, line in enumerate(lines):
                    if line and not line.startswith("#"):
                        if not re.fullmatch(r"segment-[0-9]{1,20}\.ts", line):
                            raise ValueError("bad segment")
                        lines[i] = line + "?token=" + token
                    elif line.startswith("#") and "URI=" in line:
                        raise ValueError("unexpected URI")
                body = ("\n".join(lines) + "\n").encode()
        except (OSError, ValueError):
            self._json(404, {"error": "not found"})
            return
        self._send(200, body, "application/vnd.apple.mpegurl" if name == "live.m3u8" else "video/mp2t")
        from . import radio, radio_listeners
        radio_listeners.player_fetch(radio.folder())

    def _listen_get(self) -> None:
        """The Listen in Safari page and its audio. The token in the path is the only credential: it is checked
        against the stored hashes before anything else, and it opens the page, the playlist and the segments only.
        Add-on off, radio not set up or the radio player switched off (as for /radio/): 404 for every path, whatever the token."""
        cfg = self.server.cfg
        if not radio_player_on(cfg) or not addons.enabled(cfg, "listen_link"):
            self._json(404, {"error": "not found"})
            return
        parts = listen_link.parse(self.path) if len(self.path) <= 512 else None
        if parts is None:
            # Not a page or audio path (traversal, extra segments, odd characters): never served. A valid token
            # still gets "not found"; anything else is "forbidden", so a probe learns nothing about the paths.
            token = self.path[len("/listen/"):].split("/", 1)[0].split("?", 1)[0]
            if listen_link.valid(token):
                self._json(404, {"error": "not found"})
            else:
                self._json(403, {"error": "forbidden"})
            return
        token, name = parts
        if not listen_link.valid(token):
            self._json(403, {"error": "forbidden"})
            return
        if not name:
            nonce = secrets.token_urlsafe(16)
            self._send(200, listen_link.render_page(self.server.lang, nonce).encode("utf-8"),
                       "text/html; charset=utf-8", {"Content-Security-Policy": listen_link.csp(nonce)})
            return
        if name == "now.json":   # the live title only: never starts the radio and is not a listening signal
            self._json(200, listen_now(self.server.lang))
            return
        from . import radio, radio_listeners, radio_play, radio_quiet
        n = radio_quiet.parse_name(name)
        if n is not None:   # a silent segment of the "starting" playlist: built in memory, nothing on disk
            self._send(200, radio_quiet.segment(n), "video/mp2t")
            radio_listeners.player_fetch(radio.folder())
            return
        try:
            try:
                body = radio_audio(name)
            except OSError:
                # No fresh audio. Pressing Play is itself the signal to start the radio (radio_play.py): the first
                # playlist request starts it (private audio only, rate limited, guarded) and gets a live playlist of
                # silent segments that slides until real audio exists; every other case is the old "not found".
                if name != "live.m3u8" or radio_play.request(cfg) != radio_play.STARTING:
                    raise
                self._send(200, radio_play.quiet_playlist(), "application/vnd.apple.mpegurl")
                radio_listeners.player_fetch(radio.folder())
                return
            if name == "live.m3u8":
                lines = body.decode("utf-8").splitlines()
                # Segments are relative to /listen/<token>/, so they carry the token; reject anything else.
                if not lines or lines[0] != "#EXTM3U":
                    raise ValueError("bad playlist")
                for line in lines:
                    if line.startswith("#") and "URI=" in line:
                        raise ValueError("unexpected URI")
                    if line and not line.startswith("#") and not re.fullmatch(r"segment-[0-9]{1,20}\.ts", line):
                        raise ValueError("bad segment")
                # Right after silence was served, the first real segment starts a new timeline: say so.
                body = radio_play.bridge_playlist(body.decode("utf-8")).encode("utf-8")
        except (OSError, ValueError):
            self._json(404, {"error": "not found"})
            return
        self._send(200, body, "application/vnd.apple.mpegurl" if name == "live.m3u8" else "video/mp2t")
        radio_listeners.player_fetch(radio.folder())

    def _listen_link(self) -> None:
        """{"action": "create"}: a new listening token and its path. {"action": "revoke"}: every token at once (it
        also works with the add-on off, since it only reduces exposure)."""
        req = self._request()
        action = req.get("action") if isinstance(req, dict) else None
        if not isinstance(action, str) or action not in LISTEN_KEYS or set(req) != LISTEN_KEYS[action] | {"action"}:
            self._json(400, {"error": "bad request"})
            return
        cfg = self.server.cfg
        try:
            if action == "revoke":
                self._json(200, {"ok": True, "revoked": listen_link.revoke()})
                return
            if not radio_player_on(cfg) or not addons.enabled(cfg, "listen_link"):
                self._json(404, {"error": "not found"})
                return
            self._json(200, {"ok": True, "path": "/listen/" + listen_link.mint(),
                             "base": getattr(cfg, "panel_base_url", "") or ""})
        except Exception:
            self._json(503, {"error": "unavailable"})

    # --- widget devices (device_tokens.py) --------------------------------------------------------------------

    def _device_pair(self) -> None:
        """{"label"?}: pairs one device and returns its token once (only the hash is kept)."""
        req = self._request()
        if req is None or set(req) - {"label"}:
            self._json(400, {"error": "bad request"})
            return
        try:
            out = device_tokens.pair(req.get("label"))
        except device_tokens.DeviceError as e:
            if e.kind == "rate":
                self._json(429, {"error": "rate", "retry": e.retry})
            else:
                self._json(400, {"error": "bad request"})
            return
        except Exception:
            self._json(503, {"error": "unavailable"})
            return
        self._json(200, dict(out, ok=True))
        announce = getattr(self.server, "device_paired", None)
        if announce is not None:
            announce(out["label"])

    def _devices(self) -> None:
        """Settings' list of paired devices and its one-tap revoke."""
        req = self._request()
        action = req.get("action") if isinstance(req, dict) else None
        if not isinstance(action, str) or action not in DEVICE_KEYS or set(req) != DEVICE_KEYS[action] | {"action"}:
            self._json(400, {"error": "bad request"})
            return
        try:
            if action == "revoke" and not device_tokens.revoke(req["id"]):
                self._json(404, {"error": "not found"})
                return
            if action == "revoke-all":
                device_tokens.revoke_all()
            self._json(200, {"ok": True, "devices": device_tokens.listing()})
        except Exception:
            self._json(503, {"error": "unavailable"})

    def _widget_post(self) -> None:
        if not self._body_ok(ROUTES[self.path]):
            return
        headers = self.headers.get_all("Authorization") or []
        match = BEARER.fullmatch(headers[0]) if len(headers) == 1 else None
        device = device_tokens.check(match.group(1)) if match else None
        if device is None:
            self._json(403, {"error": "forbidden"})
            return
        if not _widget_rate(device):
            self._json(429, {"error": "rate", "retry": 60})
            return
        if self.path == "/api/widget-state":
            self._widget_state()
        else:
            self._widget_decide()

    def _live_activity_token(self) -> None:
        """Accept a paired app bearer or the same fresh owner login used by mutating Cockpit APIs."""
        if not self._body_ok(ROUTES[self.path]):
            return
        headers = self.headers.get_all("Authorization") or []
        match = BEARER.fullmatch(headers[0]) if len(headers) == 1 else None
        device = device_tokens.check(match.group(1)) if match else None
        if device is not None and not _widget_rate(device):
            log_token("rate", who="device")
            self._json(429, {"error": "rate", "retry": 60})
            return
        if device is None:
            init = self.headers.get(INIT_HEADER, "")
            # A token only lets the phone receive the progress cards the same login can already read, so this route
            # takes a login of read age (the app polls with one), not the shorter answer age.
            if not validate_init_data(init, self.server.bot_token, self.server.owner_id, max_age=MAX_AGE):
                log_token("forbidden", bearer=bool(match), init=bool(init))
                self._json(403, {"error": "forbidden"})
                return
            device = "owner"
        req = self._request()
        who = "owner" if device == "owner" else "device"
        if not isinstance(req, dict) or not isinstance(req.get("card_id"), str):
            log_token("bad", who=who, shape=type(req).__name__)
            self._json(400, {"error": "bad request"})
            return
        if "problem" in req:
            # The app's one-line diagnosis of why no token came (never stored): an allow-listed code only.
            code = req.get("problem")
            if set(req) != {"card_id", "problem"} or not apns.CARD_ID.fullmatch(req["card_id"]) \
                    or not isinstance(code, str) or not LA_PROBLEM.fullmatch(code):
                log_token("rejected", who=who, keys="problem")
                self._json(400, {"error": "bad request"})
                return
            log_token("problem", who=who, problem=code)
            self._json(200, {"ok": True})
            return
        ended = req.get("ended", False)
        valid_keys = {"card_id", "ended"} if ended is True else {"card_id", "token", "env"}
        if set(req) != valid_keys or not apns.register(device, req["card_id"], req.get("token"), req.get("env"), ended):
            log_token("rejected", who=who, keys=",".join(sorted(k for k in req if k in ("card_id", "ended", "token", "env")) or "-"),
                      env=req.get("env") if req.get("env") in ("sandbox", "production") else "?")
            self._json(400, {"error": "bad request"})
            return
        log_token("ok", who=who, ended=ended is True, env=req.get("env") if req.get("env") in ("sandbox", "production") else "-")
        self._json(200, {"ok": True})

    def _widget_state(self) -> None:
        if self._body.strip() not in (b"", b"{}"):
            self._json(400, {"error": "bad request"})
            return
        self._extend(STATE_DEADLINE)  # the sender holds a paired device's token; the build can take seconds
        began = time.monotonic()
        try:
            def build():
                return build_state(self.server.cfg, tr=getattr(self.server, "translator", None))
            cache = self._state_cache()
            # The owner's own state build, shared with the Cockpit's reads (the same key as its general view).
            data = build() if cache is None else cache.get((self.server.owner_id, ""), build,
                                                           keep=lambda built: not built.get("pending"))
            view = widget_view(self.server.cfg, data, bot_id_of(self.server.bot_token))
        except Exception as exc:
            log_failure("widget state", exc)
            self._json(500, {"error": "unavailable"})
            return
        built = time.monotonic()
        try:
            self._json(200, view)
        finally:
            log_slow("widget state", began, built=built, sent=time.monotonic())

    def _widget_decide(self) -> None:
        """{"id", "choice"}: a widget's answer. A sensitive decision is refused with "needs-app" (the app answers it
        after Face ID), checked under the decisions lock. Answering the same way again is a success, not an error."""
        req = self._request()
        if req is None or set(req) != {"id", "choice"}:
            self._json(400, {"error": "bad request"})
            return
        ident, choice = req["id"], req["choice"]
        if not (isinstance(ident, str) and decisions.ID_RE.fullmatch(ident) and choice in ("yes", "no")):
            self._json(400, {"error": "bad request"})
            return

        def not_sensitive(rec: dict) -> None:
            if sensitive_buttons.decision_sensitive(rec):
                raise decisions.DecisionError("needs the app", "sensitive")

        try:
            if stale_reasons_now(self.server.cfg).get(ident) in ("ended", "replaced"):
                self._json(409, {"error": "closed", "state": "stale", "choice": ""})
                return
            # by: the owner this server serves (the device token was paired by the owner), never the request body
            rec = decisions.answer(self.server.cfg, self.server.inbox(), ident, choice, "", check=not_sensitive,
                                   by=self.server.owner_id, via="widget")
        except PanelQueueFull:
            self._json(409, {"error": "queue full"})
            return
        except decisions.DecisionError as e:
            if e.kind == "sensitive":
                self._json(403, {"error": "needs-app"})
            elif e.kind == "closed" and e.state == "answered" and e.choice == choice:
                self._json(200, {"ok": True, "already": True})
            elif e.kind == "closed":
                self._json(409, {"error": "closed", "state": e.state, "choice": e.choice})
            else:
                code = {"unknown": 404, "busy": 503}.get(e.kind, 400)
                self._json(code, {"error": {404: "not found", 503: "busy"}.get(code, "bad request")})
            return
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        decisions.announce(self.server.cfg, self.server.bot(), self.server.inbox(), rec)
        self._json(200, {"ok": True, "key": rec["delivery_key"],
                         "delivery": rec.get("final_state") or self.server.inbox().delivery_state(rec["session"], rec["delivery_key"])})

    # --- the YouTube staff radio (panel_youtube.py) ----------------------------------------------------------

    def _youtube_post(self) -> None:
        """{"action": ...}: connect (a sign-in ticket), enable {"on"}, projects {"keys"}, arm then start
        {"armed"} (two taps), stop, disconnect. Owner only, with the fresh login every action needs."""
        from . import policy
        if not self._body_ok(YOUTUBE_BODY):
            return
        req = self._request()
        action = req.get("action") if req is not None else None
        if not policy.on() and action not in YOUTUBE_WHILE_OFF:
            self._json(503, MIRROR_OFF)
            return
        if not validate_init_data(self.headers.get(INIT_HEADER, ""), self.server.bot_token, self.server.owner_id,
                                  max_age=DECIDE_MAX_AGE):
            self._json(403, {"error": "forbidden"})
            return
        if action not in YOUTUBE_KEYS or set(req) != {"action"} | YOUTUBE_KEYS[action]:
            self._json(400, {"error": "bad request"})
            return
        from . import radio_youtube as Y
        srv = self.server
        try:
            if action == "connect":
                if not all(srv.youtube_ready()):
                    self._json(409, {"error": "setup"})
                    return
                ticket = srv.youtube_flow.ticket(srv.owner_id, Y.generation())
                self._json(200, {"ok": True, "url": srv.cfg.panel_base_url + panel_youtube.START_PATH + "?t=" + ticket})
                return
            if action == "enable":
                if not isinstance(req["on"], bool):
                    self._json(400, {"error": "bad request"})
                    return
                Y.set_cockpit_prefs(enabled=req["on"])
            elif action == "projects":
                keys = req["keys"]
                known = {p["key"] for p in Y.catalog(srv.cfg)}
                if not (isinstance(keys, list) and len(keys) <= 8 and all(isinstance(k, str) and k in known for k in keys)):
                    self._json(400, {"error": "bad request"})
                    return
                Y.set_cockpit_prefs(projects=list(dict.fromkeys(keys)))
            elif action in ("arm", "mirror-arm"):
                # one locked read: the title shown, the channel id and the generation the token is bound to
                conn, v = Y.snapshot(), Y.view(srv.cfg, True, True)
                if (action == "arm" and not v["enabled"]) or not conn.get("connected") or not conn.get("channel_id") \
                        or conn.get("live_enabled") is False or v["live"] or v["pending"]:
                    self._json(409, {"error": "not ready"})
                    return
                token = srv.youtube_flow.arm(conn["generation"], conn["channel_id"])
                self._json(200, {"ok": True, "armed": token, "channel": str(conn.get("channel_title") or "")[:100]})
                return
            elif action in ("start", "mirror-on"):
                confirmed = srv.youtube_flow.fire(req["armed"])
                conn = Y.connection()
                if confirmed is None or confirmed != (conn["generation"], conn.get("channel_id")):
                    self._json(409, {"error": "confirm again"})  # unknown, used, expired, or another channel now
                    return
                if action == "start" and not Y.settings(srv.cfg)["enabled"]:
                    self._json(409, {"error": "disabled"})
                    return
                rid = Y.put_start_request(*confirmed)  # one file per request; the start checks it with Google
                srv.youtube_spawn(action, rid)
                self._json(202, {"ok": True, "pending": True})
                return
            elif action == "disconnect":
                srv.youtube_flow.clear()  # every pending sign-in and confirmation is void at once
                Y.bump_generation()       # and so is a sign-in already exchanging its code (checked again on save)
                srv.youtube_spawn(action)
                self._json(202, {"ok": True, "pending": True})
                return
            else:  # stop
                srv.youtube_spawn(action)
                self._json(202, {"ok": True, "pending": True})
                return
        except Y.YouTubeError as e:  # the connection lock stayed busy (another change is being saved)
            self._json(503 if e.key == "youtube_busy" else 500, {"error": "busy" if e.key == "youtube_busy" else "unavailable"})
            return
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        self._json(200, {"ok": True, "youtube": srv.youtube_view()})

    def _oauth_page(self, code: int, ok: bool, extra: Optional[dict] = None, google: bool = False) -> None:
        nonce = secrets.token_urlsafe(16)
        headers = {"Content-Security-Policy": panel_youtube.PAGE_CSP.format(n=nonce)}
        headers.update(extra or {})
        texts = {}
        if google:
            from .i18n import GOOGLE_CALENDAR_DONE, GOOGLE_CALENDAR_FAILED
            texts = {"done": GOOGLE_CALENDAR_DONE, "failed": GOOGLE_CALENDAR_FAILED}
        self._send(code, panel_youtube.page(self.server.lang, ok, nonce, **texts), "text/html; charset=utf-8", headers)

    def _oauth_query(self) -> Optional[dict]:
        """The query of an OAuth GET as a dict; None for a malformed, repeated-key or oversized one."""
        parts = urlsplit(self.path)
        try:
            pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True) if parts.query else []
        except ValueError:
            return None
        names = [k for k, _ in pairs]
        if len(self.path) > MAX_OAUTH_PATH or len(names) != len(set(names)):
            return None
        return dict(pairs)

    def _google_get(self) -> None:
        """The Google Calendar web sign-in: /oauth/google/start?t=<ticket> and /oauth/google/callback?state=..&code=..
        Nothing in the request is logged; replies are no-store and no-referrer (_send)."""
        srv, G = self.server, google_calendar
        q, path = self._oauth_query(), urlsplit(self.path).path
        if q is None:
            self._oauth_page(400, False, google=True)
            return
        if not G.status(srv.cfg)["configured"]:
            self._oauth_page(409, False, google=True)
            return
        if path == G.START_PATH and set(q) == {"t"}:
            got = srv.google_flow.take_ticket(q["t"])
            if got is None or got[0] != srv.owner_id:
                self._oauth_page(403, False, google=True)  # unknown, used, expired, or issued before a disconnect
                return
            state, challenge, cookie = srv.google_flow.begin(*got)
            try:
                location = G.start_location(srv.cfg, state, challenge)
            except G.CalendarError:
                self._oauth_page(409, False, google=True)
                return
            self._send(302, b"", "text/plain; charset=utf-8", {
                "Location": location, "Set-Cookie": G.set_cookie(cookie),
                "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"})
            return
        if path != G.CALLBACK_PATH or "state" not in q:
            self._oauth_page(404, False, google=True)
            return
        clear = {"Set-Cookie": G.set_cookie("", 0)}
        got = srv.google_flow.finish(q["state"], G.cookie_of(self.headers.get("Cookie")))
        if got is None or got[1] != srv.owner_id or not srv.google_flow.current(got[2]):
            self._oauth_page(403, False, clear, google=True)
            return
        verifier, _, gen = got
        if "error" in q or not q.get("code") or len(q["code"]) > 512:
            self._oauth_page(400, False, clear, google=True)
            return
        self._extend(OAUTH_DEADLINE)  # one exchange with Google: more than the usual few seconds
        try:
            G.complete(srv.cfg, srv.google_flow, q["code"], verifier, gen)
        except G.CalendarError as e:
            self._oauth_page(409 if e.code in ("stale", "setup") else 502, False, clear, google=True)
            return
        except Exception:
            self._oauth_page(502, False, clear, google=True)
            return
        self._oauth_page(200, True, clear, google=True)

    def _youtube_get(self) -> None:
        """The web sign-in: /oauth/youtube/start?t=<ticket> and /oauth/youtube/callback?state=..&code=..
        Nothing in the request is logged; replies are no-store and no-referrer (_send)."""
        srv = self.server
        parts = urlsplit(self.path)
        try:
            pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True) if parts.query else []
        except ValueError:
            pairs = None
        names = [k for k, _ in pairs or []]
        if pairs is None or len(self.path) > MAX_OAUTH_PATH or len(names) != len(set(names)):
            self._oauth_page(400, False)
            return
        q = dict(pairs)
        if not all(srv.youtube_ready()):
            self._oauth_page(409, False)
            return
        if parts.path == panel_youtube.START_PATH and set(q) == {"t"}:
            from . import radio_youtube as Y
            got = srv.youtube_flow.take_ticket(q["t"])
            if got is None or got[0] != srv.owner_id or got[1] != Y.generation():
                self._oauth_page(403, False)  # unknown, used, expired, or issued before a disconnect / sign-in
                return
            state, challenge, cookie = srv.youtube_flow.begin(*got)
            client = srv.youtube_client()
            self._send(302, b"", "text/plain; charset=utf-8", {
                "Location": panel_youtube.google_url(client["client_id"], srv.cfg.panel_base_url, state, challenge),
                "Set-Cookie": panel_youtube.set_cookie(cookie),
                "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"})
            return
        if parts.path != panel_youtube.CALLBACK_PATH or "state" not in q:
            self._oauth_page(404, False)
            return
        clear = {"Set-Cookie": panel_youtube.set_cookie("", 0)}
        from . import radio_youtube as Y
        got = srv.youtube_flow.finish(q["state"], panel_youtube.cookie_of(self.headers.get("Cookie")))
        if got is None or got[1] != Y.generation():
            self._oauth_page(403, False, clear)
            return
        verifier, gen = got
        if "error" in q or not q.get("code") or len(q["code"]) > 512:
            self._oauth_page(400, False, clear)
            return
        self._deadline.cancel()  # one exchange with Google: more than the usual few seconds
        self._deadline = threading.Timer(OAUTH_DEADLINE, self._cut)
        self._deadline.daemon = True
        self._deadline.start()
        try:
            svc = srv.youtube_service()
            issuer = srv.youtube_client()  # the Web client: it issues the token, and is recorded beside it
            tokens = Y.OAuth(issuer, srv.youtube_request or Y.http).exchange(
                q["code"], verifier, panel_youtube.redirect_uri(srv.cfg.panel_base_url))
            # checked again under the connection lock right before storing: a disconnect during the exchange wins
            new_gen = Y.store_credentials(svc, tokens, gen, issuer)  # the one storing function; no command runs
        except Y.YouTubeError as e:
            self._oauth_page(409 if e.key in ("youtube_connect_stale", "youtube_stop_first") else 502, False, clear)
            return
        except Exception:
            self._oauth_page(502, False, clear)
            return
        srv.youtube_flow.clear()  # a new connection: earlier sign-ins and Start confirmations are void
        threading.Thread(target=srv.youtube_describe, args=(svc, new_gen), daemon=True).start()
        self._oauth_page(200, True, clear)

    def _hooks_restore(self) -> None:
        from . import hook_health
        if self._request() != {}:
            self._json(400, {"error": "bad request"})
            return
        try:
            self._json(200, hook_health.restore())
        except (OSError, ValueError):
            self._json(409, {"error": "restore_failed"})

    def _sessions_reload(self) -> None:
        from . import session_reload
        if self._request() != {}:
            self._json(400, {"error": "bad request"})
            return
        session_reload.start(self.server.cfg)
        self._json(202, {"ok": True})

    def _decide(self) -> None:
        """The owner's tap on one decision: {"id", "choice": "yes"|"no", "note"}. Answered once. With
        {"id", "choice": "reply", "note"} the note (required) is sent alone as the owner's comment: it answers
        nothing, approves nothing and the decision stays open (decisions.reply)."""
        try:
            req = json.loads(self._body.decode("utf-8"))
        except ValueError:
            req = None
        if not isinstance(req, dict) or set(req) - {"id", "choice", "note"}:
            self._json(400, {"error": "bad request"})
            return
        ident, choice, note = req.get("id"), req.get("choice"), req.get("note", "")
        rich = self.server.cfg.rich_decisions_enabled is True
        choices = ("yes", "no", "reply") if rich else ("yes", "no")
        note_limit = decisions.NOTE_CHARS if rich else decisions.LEGACY_NOTE_CHARS
        if not (isinstance(ident, str) and decisions.ID_RE.fullmatch(ident) and choice in choices
                and isinstance(note, str) and len(note) <= note_limit and (choice != "reply" or note.strip())):
            self._json(400, {"error": "bad request"})
            return
        try:
            # One check for every decision, fresh or old: a page that was open while its session ended, or while the
            # same question was asked again, never answers it (an answer here can approve a sensitive action). Only
            # age alone ("old") stays answerable. Same refusal as a decision that was closed meanwhile.
            if stale_reasons_now(self.server.cfg).get(ident) in ("ended", "replaced"):
                self._json(409, {"error": "closed", "state": "stale", "choice": ""})
                return
            if choice == "reply":
                self._decide_reply(ident, note)
                return
            if any(r["id"] == ident and r.get("perm") for r in decisions.open_decisions()):
                # A Claude Code permission request: the answer goes to its waiting hook (permission_request.py).
                from . import permission_request
                result = permission_request.decide(self.server.cfg, self.server.bot(), ident, choice)
                if result == "ok":
                    self._json(200, {"ok": True, "key": None, "delivery": "delivered"})
                else:
                    self._json(409, {"error": "closed", "state": "stale", "choice": ""})
                return
            # by: validate_init_data proved the Telegram user is this server's owner; never the request body
            rec = decisions.answer(self.server.cfg, self.server.inbox(), ident, choice, note,
                                   by=self.server.owner_id, via="panel")
        except PanelQueueFull:
            self._json(409, {"error": "queue full"})
            return
        except decisions.DecisionError as e:
            code = {"unknown": 404, "closed": 409, "busy": 503}.get(e.kind, 400)
            body = {"error": {404: "not found", 409: "closed", 503: "busy"}.get(code, "bad request")}
            if e.kind == "closed":
                body.update(state=e.state, choice=e.choice)
            self._json(code, body)
            return
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        decisions.announce(self.server.cfg, self.server.bot(), self.server.inbox(), rec)
        self._json(200, {"ok": True, "key": rec["delivery_key"],
                         "delivery": rec.get("final_state") or self.server.inbox().delivery_state(rec["session"], rec["delivery_key"])})

    def _decide_reply(self, ident: str, note: str) -> None:
        """A comment without an answer: queued as the owner's own words, marked as approving nothing. The decision
        stays open; its topic notice is not touched. Errors propagate to _decide's handlers."""
        rec = decisions.reply(self.server.cfg, self.server.inbox(), ident, note)
        decisions.announce_reply(self.server.inbox(), rec)
        self._json(200, {"ok": True, "reply": True, "key": rec["reply_key"],
                         "delivery": self.server.inbox().delivery_state(rec["session"], rec["reply_key"])})

    def _radio(self) -> None:
        """The owner's radio switch: {"on": true|false}. Answers at once; the switch runs detached."""
        try:
            req = json.loads(self._body.decode("utf-8"))
        except ValueError:
            req = None
        if not isinstance(req, dict) or set(req) != {"on"} or not isinstance(req["on"], bool):
            self._json(400, {"error": "bad request"})
            return
        if not getattr(self.server.cfg, "radio", None):
            self._json(404, {"error": "not found"})
            return
        try:
            self.server.radio_switch("start" if req["on"] else "stop")
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        self._json(202, {"ok": True, "pending": True})

    def _model(self) -> None:
        """The owner's choice of the model new sessions start with: {"model": "<key from models.MODELS>"}."""
        try:
            req = json.loads(self._body.decode("utf-8"))
        except ValueError:
            req = None
        if not isinstance(req, dict) or set(req) != {"model"} or req["model"] not in models.IDS:
            self._json(400, {"error": "bad request"})
            return
        try:
            models.set_default(req["model"])
        except ValueError:
            self._json(409, {"error": "settings"})
            return
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        self._json(200, {"ok": True, "label": models.LABELS[req["model"]]})

    def _request(self) -> Optional[dict]:
        try:
            req = json.loads(self._body.decode("utf-8"))
        except ValueError:
            req = None
        return req if isinstance(req, dict) else None

    def _card(self) -> None:
        """The owner's answer on one agent card: {"id", "pick": n} for a choice, {"id", "text"} for a question."""
        req = self._request()
        if req is None:
            self._json(400, {"error": "bad request"})
            return
        dismiss = set(req) == {"id", "dismiss"} and req.get("dismiss") is True
        if not dismiss and (set(req) - {"id", "pick", "text"} or ("pick" in req) == ("text" in req)):
            self._json(400, {"error": "bad request"})
            return
        ident, pick, text = req.get("id"), req.get("pick"), req.get("text", "")
        if set(req) == {"id", "dismiss"} and req.get("dismiss") is True:
            if not isinstance(ident, str) or not cards.ID_RE.fullmatch(ident):
                self._json(400, {"error": "bad request"})
                return
            try:
                closed = cards.dismiss_progress(ident)
            except cards.CardError:
                self._json(503, {"error": "busy"})
                return
            self._json(200 if closed else 404, {"ok": closed})
            return
        if not (isinstance(ident, str) and cards.ID_RE.fullmatch(ident) and isinstance(text, str)
                and len(text) <= cards.ANSWER_CHARS and (pick is None or type(pick) is int)):
            self._json(400, {"error": "bad request"})
            return
        try:
            from . import helper_jobs
            tapped = helper_jobs.resolve_card(self.server.cfg, ident, pick)
            if tapped is not None:  # a card a helper agent made: the tap is handled here, never queued to a session
                code = tapped["status"]
                self._json(code, {"ok": True, "key": "", "delivery": "delivered"} if tapped["ok"] else
                           {"error": {400: "bad request", 409: "closed", 503: "busy"}.get(code, "unavailable")})
                return
            asking = next((r["session"] for r in cards.open_cards() if r["id"] == ident), None)
            if asking is not None and asking in _ended_sessions(self.server.cfg):
                self._json(409, {"error": "closed"})  # a stale page: an ended session is never woken by an answer
                return
            rec = cards.answer(self.server.cfg, self.server.inbox(), ident, pick, text)
        except PanelQueueFull:
            self._json(409, {"error": "queue full"})
            return
        except cards.CardError as e:
            code = {"unknown": 404, "closed": 409, "busy": 503}.get(e.kind, 400)
            self._json(code, {"error": {404: "not found", 409: "closed", 503: "busy"}.get(code, "bad request")})
            return
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        cards.wake(self.server.inbox(), rec)
        from . import cockpit_notice
        cockpit_notice.later(self.server.cfg, rec, self.server.bot())
        self._json(200, {"ok": True, "key": rec["delivery_key"],
                         "delivery": self.server.inbox().delivery_state(rec["session"], rec["delivery_key"])})

    def _say(self) -> None:
        """The owner's words to one live session: {"session": <first 8 characters>, "text": ...}."""
        req = self._request()
        short, text = (req.get("session"), req.get("text")) \
            if req is not None and set(req) in ({"session", "text"}, {"session", "text", "send"}) else (None, None)
        send = req.get("send") if req is not None else None
        if not (isinstance(short, str) and SHORT_ID.fullmatch(short) and isinstance(text, str) and text.strip()
                and len(text) <= SAY_TO_CHARS and (send is None or (isinstance(send, str) and SEND_ID.fullmatch(send)))):
            self._json(400, {"error": "bad request"})
            return
        try:
            rec = say_to(self.server.cfg, self.server.inbox(), short, text, send=send)
        except PanelQueueFull:
            self._json(409, {"error": "queue full"})
            return
        except cards.CardError as e:
            code = {"unknown": 404, "closed": 409, "busy": 503}.get(e.kind, 400)
            self._json(code, {"error": {404: "not found", 409: "closed", 503: "busy"}.get(code, "bad request")})
            return
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        if not rec.get("again"):
            cards.wake(self.server.inbox(), rec)
        from . import cockpit_notice
        cockpit_notice.later(self.server.cfg, rec, self.server.bot())
        self._json(200, {"ok": True, "key": rec["key"],
                         "delivery": self.server.inbox().delivery_state(rec["session"], rec["key"])})

    def _fork(self) -> None:
        """Both arm and start use the standard fresh owner initData guard in do_POST."""
        from . import session_fork
        from .i18n import t
        req = self._request()
        action = req.get("action") if req else None
        keys = {"action", "session"} if action == "arm" else {"action", "session", "armed"}
        if (not req or set(req) != keys or action not in ("arm", "start")
                or not isinstance(req["session"], str) or not SHORT_ID.fullmatch(req["session"])
                or (action == "start" and (not isinstance(req["armed"], str)
                                          or not re.fullmatch(r"[0-9a-f]{16}", req["armed"])))):
            self._json(400, {"error": "bad request"})
            return
        cfg = self.server.cfg
        topics = TopicMap(state_dir() / "topics.json", self.server.bot(), home_chat(cfg), cfg=cfg)
        matches = [sid for sid in topics.all_places() if sid[:8] == req["session"]]
        if len(matches) != 1:
            self._json(404, {"error": "not found"})
            return
        sid = matches[0]
        try:
            if action == "arm":
                armed = session_fork.arm(cfg, topics, sid, "panel")
                self._json(200, {"ok": True, "armed": armed})
            else:
                session_fork.start(cfg, self.server.bot(), topics, sid, "panel", req["armed"])
                self._json(202, {"ok": True})
        except session_fork.ForkError as exc:
            self._json(409, {"error": exc.key, "message": t(exc.key, lang=cfg.ui_language)})
        except (OSError, ValueError, KeyError, TypeError):
            self._json(503, {"error": "unavailable"})

    def _screen(self) -> None:
        """What one live session's terminal shows right now: {"session": <first 8 characters>}. The text is
        the bridge's own redacted copy of the screen (the one its cards use); nothing is typed into it."""
        req = self._request()
        short = req.get("session") if req is not None and set(req) == {"session"} else None
        if not (isinstance(short, str) and SHORT_ID.fullmatch(short)):
            self._json(400, {"error": "bad request"})
            return
        try:
            text = screen_of(self.server.cfg, short)
        except cards.CardError:
            self._json(404, {"error": "not found"})
            return
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        self._json(200, {"ok": True, "text": text})

    def _compact(self) -> None:
        """One session's own automatic-compaction threshold: {"session": <first 8 characters>, "limit": null (the
        global one) | 0 (never) | a step in thousands of tokens}."""
        req = self._request()
        short, limit = (req.get("session"), req.get("limit")) if req is not None and set(req) == {"session", "limit"} \
            else (None, -1)
        from .compact import AUTO_COMPACT_STEPS
        if not (isinstance(short, str) and SHORT_ID.fullmatch(short)
                and (limit is None or (type(limit) is int and limit in AUTO_COMPACT_STEPS))):
            self._json(400, {"error": "bad request"})
            return
        try:
            compact_limit_to(self.server.cfg, short, limit)
        except cards.CardError:
            self._json(404, {"error": "not found"})
            return
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        self._json(200, {"ok": True, "limit": limit})

    def _unsay(self) -> None:
        """Take back words written in the panel that the session has not read yet:
        {"session": <first 8 characters>, "key": ...}. 409 when the session already took them."""
        req = self._request()
        short, key = (req.get("session"), req.get("key")) if req is not None and set(req) == {"session", "key"} \
            else (None, None)
        if not (isinstance(short, str) and SHORT_ID.fullmatch(short) and isinstance(key, str) and SAY_KEY.fullmatch(key)):
            self._json(400, {"error": "bad request"})
            return
        try:
            text = unsay(self.server.cfg, self.server.inbox(), short, key)
        except cards.CardError as e:
            self._json(404 if e.kind == "unknown" else 503, {"error": "not found" if e.kind == "unknown" else "busy"})
            return
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        if text is None:
            self._json(409, {"error": "delivered"})
            return
        self._json(200, {"ok": True, "text": text})

    def _dismiss(self) -> None:
        """Clear stale decisions: {"ids": [...]}. Only ids the server itself counts as stale are closed."""
        req = self._request()
        ids = req.get("ids") if req is not None and set(req) == {"ids"} else None
        if not isinstance(ids, list) or not ids or len(ids) > decisions.MAX_OPEN \
                or not all(isinstance(i, str) and decisions.ID_RE.fullmatch(i) for i in ids):
            self._json(400, {"error": "bad request"})
            return
        try:
            stale = stale_ids(self.server.cfg)
            records = {r["id"]: r for r in decisions.open_decisions()}
            closed = decisions.dismiss([i for i in ids if i in stale], until=time.monotonic() + decisions.LOCK_WAIT)
        except decisions.DecisionError:
            self._json(503, {"error": "busy"})
            return
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        from . import cockpit_notice
        for ident in closed:
            if ident in records:
                cockpit_notice.later(self.server.cfg, records[ident], self.server.bot())
        self._json(200, {"ok": True, "closed": closed})

    def _topic_cleanup(self) -> None:
        """Old topics: {"action": "close"|"keep"|"unkeep"|"reopen", "ids": [short ids], "confirm": n}. Closing more than
        one topic needs "confirm" equal to the number of ids (the count the owner saw and confirmed). Nothing is
        ever deleted."""
        from . import topic_cleanup
        if not addons.enabled(self.server.cfg, "topic_cleanup"):
            self._json(404, {"error": "not found"})
            return
        req = self._request()
        action = req.get("action") if req is not None else None
        ids = req.get("ids") if req is not None else None
        if (req is None or set(req) - {"action", "ids", "confirm"} or action not in ("close", "keep", "unkeep", "reopen")
                or not isinstance(ids, list) or not ids or len(ids) > topic_cleanup.MAX_LISTED
                or len(set(ids)) != len(ids)
                or not all(isinstance(i, str) and SHORT_ID.fullmatch(i) for i in ids)):
            self._json(400, {"error": "bad request"})
            return
        if action == "close" and len(ids) > 1 and (type(req.get("confirm")) is not int or req["confirm"] != len(ids)):
            self._json(409, {"error": "confirm the count"})
            return
        cfg = self.server.cfg
        topics = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg)
        try:
            if action in ("keep", "unkeep"):
                out = getattr(topic_cleanup, action)(cfg, topics, ids)
            elif action == "close":
                out = topic_cleanup.close(cfg, self.server.bot(), topics, ids)
            else:
                out = topic_cleanup.reopen(cfg, self.server.bot(), topics, ids)
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        self._json(200, dict(out, ok=True))

    def _close(self) -> None:
        """Close left-behind sessions' topics: {"sessions": [short ids]}."""
        req = self._request()
        ids = req.get("sessions") if req is not None and set(req) == {"sessions"} else None
        if not isinstance(ids, list) or not ids or len(ids) > MAX_CLOSE or len(set(ids)) != len(ids) \
                or not all(isinstance(i, str) and SHORT_ID.fullmatch(i) for i in ids):
            self._json(400, {"error": "bad request"})
            return
        try:
            out = close_left(self.server.cfg, self.server.bot(), ids)
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        self._json(200, dict(out, ok=True))

    def _remind(self) -> None:
        """Clear reminders: {"refs": ["<topic hash>:<item id>", ...]}."""
        req = self._request()
        refs = req.get("refs") if req is not None and set(req) in ({"refs"}, {"refs", "action"}) else None
        action = req.get("action", "clear") if req is not None else "clear"
        if not isinstance(refs, list) or not refs or len(refs) > MAX_REMINDERS \
                or action not in ("clear", "restore") \
                or not all(isinstance(r, str) and REMINDER_REF.fullmatch(r) for r in refs):
            self._json(400, {"error": "bad request"})
            return
        try:
            out = clear_reminders(self.server.cfg, self.server.bot(), refs, action=action)
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        self._json(200, dict(out, ok=True))

    def _owner_request(self) -> None:
        req = self._request()
        if not isinstance(req, dict) or set(req) != {"project", "id", "action"}:
            self._json(400, {"error": "bad request"})
            return
        action = req.get("action")
        try:
            owner_request_actions.act(self.server.cfg, req["project"], req["id"], action)
            if action == "push":
                owner_request_actions.schedule(self.server.cfg)
        except owner_request_actions.AlreadyClosed:
            self._json(409, {"error": "already closed"})
            return
        except (ValueError, TypeError, KeyError):
            self._json(409, {"error": "request changed or unavailable"})
            return
        except OSError:
            self._json(503, {"error": "unavailable"})
            return
        self._json(200, {"ok": True})

    def _owner_request_detail(self) -> None:
        """{"project", "id"}: the full text, latest note and steps of one ledger item. Owner only (the same login as
        /api/state); nothing is written. The text passes the same credential filter as the state."""
        req = self._request()
        if not isinstance(req, dict) or set(req) != {"project", "id"}:
            self._json(400, {"error": "bad request"})
            return
        try:
            item = owner_requests.detail(owner_requests.ledger_dir(self.server.cfg), req["project"], req["id"],
                                         time.time(), self.server.translator)
        except Exception:
            self._json(503, {"error": "unavailable"})
            return
        if item is None:
            self._json(404, {"error": "unknown request"})
            return
        _secret_free(item)
        self._json(200, dict(item, ok=True))

    def _owner_request_target(self) -> None:
        """{"key": 16 hex}: the {project, id} of the ledger row a widget deep link names. Owner only (the same login
        as /api/state); nothing is written; an unknown key is a 404."""
        req = self._request()
        key = req.get("key") if isinstance(req, dict) and set(req) == {"key"} else None
        if not isinstance(key, str):
            self._json(400, {"error": "bad request"})
            return
        try:
            now = time.time()
            rows = owner_requests.read(now, owner_requests.ledger_dir(self.server.cfg), history=True,
                                       dormant_after=owner_requests.dormant_seconds(self.server.cfg))
            found = owner_requests.resolve_key(rows, key)
        except Exception:
            self._json(503, {"error": "unavailable"})
            return
        if found is None:
            self._json(404, {"error": "unknown request"})
            return
        self._json(200, {"ok": True, "project": found[0], "id": found[1]})

    def _topic_detail(self) -> None:
        """{"key": [project, "topic"|"item", slug|id]}: the timeline notes of one topic card, newest first. Owner only
        (the same login as /api/state); nothing is written. The text passes the same credential filter."""
        req = self._request()
        key = req.get("key") if isinstance(req, dict) and set(req) == {"key"} else None
        if not (isinstance(key, list) and len(key) == 3 and isinstance(key[0], str) and key[1] in ("topic", "item")
                and (isinstance(key[2], str) or type(key[2]) is int)):
            self._json(400, {"error": "bad request"})
            return
        now = time.time()
        try:
            rows = [r for r in owner_requests.read(now, owner_requests.ledger_dir(self.server.cfg), history=True)
                    if r["status"] != "done" or 0 <= now - r["updated"] <= owner_requests.DONE_DAYS]
            notes = topic_cards.timeline(rows, key, now, self.server.translator)
        except Exception:
            self._json(503, {"error": "unavailable"})
            return
        if notes is None:
            self._json(404, {"error": "unknown topic"})
            return
        out = {"notes": notes}
        _secret_free(out)
        self._json(200, dict(out, ok=True))

    def _app_request(self) -> None:
        """{"text": ...}: one App Store link or id for the approval command (app_request.py). Owner only, fresh login;
        the text is checked before anything runs and is never logged."""
        req = self._request()
        text = req.get("text") if req is not None and set(req) == {"text"} else None
        if not app_request.configured(self.server.cfg):
            self._json(404, {"error": "not found"})
            return
        # The command may run up to app_request.TIMEOUT, plus the kill wait and the Hebrew translation: the 5 s
        # connection limit would close the socket before the answer. The sender is authenticated by now.
        self._extend(app_request.TIMEOUT + 3 + app_request.TRANSLATE_WAIT + 5)
        try:
            out = app_request.request(self.server.cfg, text, translator=self.server.translator,
                                      gate=self.server.app_request_gate, runner=self.server.app_request_runner)
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        if out["ok"]:
            self._json(200, out)
        elif out["error"] == "rate":
            self._json(429, {"error": "rate", "retry": out["retry"]})
        else:
            self._json(400, {"error": "bad request", "why": out.get("why", "text")})

    def _knowledge(self) -> None:
        """{"action": "view"|"scan"} the index; {"action": "toggle", "id", "on"}; {"action": "undo", "log"};
        {"action": "suggest", "id", "text"} (a ledger item for an agent; nothing is edited here)."""
        from . import knowledge
        req = self._request()
        action = req.get("action") if isinstance(req, dict) else None
        if not isinstance(action, str) or action not in KNOWLEDGE_KEYS or set(req) != KNOWLEDGE_KEYS[action] | {"action"}:
            self._json(400, {"error": "bad request"})
            return
        cfg = self.server.cfg
        try:
            if action in ("view", "scan"):
                self._json(200, knowledge.view(cfg, rescan=action == "scan"))
                return
            if action == "toggle":
                entry = knowledge.toggle(cfg, req["id"], req["on"], origin="cockpit")
            elif action == "undo":
                entry = knowledge.undo(cfg, req["log"], origin="cockpit")
            else:
                entry = knowledge.suggest(cfg, req["id"], req["text"], origin="cockpit")
        except knowledge.KnowledgeError as exc:
            self._json(KNOWLEDGE_ERRORS.get(exc.code, 400), {"error": exc.code})
            return
        except OSError:
            self._json(503, {"error": "unavailable"})
            return
        entry.pop("text", None)
        self._json(200, {"ok": True, "entry": entry})

    def _addons(self) -> None:
        """{"action": "view"}: every add-on with its switch; {"action": "set", "key", "on"}: switch one globally. A locked
        safety add-on answers 409. The answer to a set carries the previous value, so the page can undo in one tap."""
        req = self._request()
        action = req.get("action") if isinstance(req, dict) else None
        if not isinstance(action, str) or action not in ADDON_KEYS or set(req) != ADDON_KEYS[action] | {"action"}:
            self._json(400, {"error": "bad request"})
            return
        cfg = self.server.cfg
        try:
            if action == "view":
                self._json(200, addons.view(cfg))
                return
            if not isinstance(req["key"], str) or not isinstance(req["on"], bool):
                raise addons.AddonError("bad")
            out = addons.set_on(cfg, req["key"], req["on"])
        except addons.AddonError as exc:
            self._json(ADDON_ERRORS.get(exc.code, 400), {"error": exc.code})
            return
        except Exception:
            self._json(503, {"error": "unavailable"})
            return
        self._json(200, {"ok": True, "key": out["key"], "on": out["on"], "previous": out["previous"],
                         "item": addons.item(cfg, addons.BY_KEY[out["key"]])})

    def _packs(self) -> None:
        """Owner-only pack grouping view."""
        from . import packs
        req = self._request()
        action = req.get("action") if isinstance(req, dict) else None
        if action not in PACK_KEYS or set(req) != PACK_KEYS[action] | {"action"}:
            self._json(400, {"error": "bad request"}); return
        try:
            self._json(200, packs.view(self.server.cfg))
        except OSError:
            self._json(503, {"error": "unavailable"})

    def _project_art(self) -> None:
        """Set (an uploaded image), pick a scene, or clear the owner's picture of one project."""
        req = self._request()
        action = req.get("action") if isinstance(req, dict) else None
        if not isinstance(action, str) or action not in ART_KEYS or set(req) != ART_KEYS[action] | {"action"}:
            self._json(400, {"error": "bad request"})
            return
        project = req["project"]
        if not isinstance(project, str) or len(project) > 200 or not project_art.name_for(project):
            self._json(400, {"error": "bad request"})
            return
        try:
            if action == "clear":
                project_art.clear(project)
            else:
                if action == "set":
                    data = req["data"]
                    if not isinstance(data, str) or len(data) > project_art.MAX_BYTES * 4 // 3 + 8:
                        raise project_art.ArtError("size" if isinstance(data, str) else "bad")
                    try:
                        raw = base64.b64decode(data, validate=True)
                    except ValueError:
                        raise project_art.ArtError("bad")
                else:
                    try:
                        raw = cockpit_v2.scene_bytes(req["scene"]) if isinstance(req["scene"], str) else b""
                    except (KeyError, OSError):
                        self._json(404, {"error": "unknown"})
                        return
                project_art.store(project, raw)
        except project_art.ArtError as exc:
            self._json(ART_ERRORS.get(exc.code, 400), {"error": exc.code})
            return
        except OSError:
            self._json(503, {"error": "unavailable"})
            return
        self._json(200, {"ok": True, "art": project_art.stamp(project)})

    def _project_art_image(self) -> None:
        req = self._request()
        name = req.get("name") if isinstance(req, dict) and set(req) == {"name"} else None
        try:
            body = project_art.read(name)
        except OSError:
            self._json(404, {"error": "not found"})
            return
        ctype = {"jpeg": "image/jpeg", "png": "image/png"}.get(project_art.kind_of(body), "image/webp")
        self._send(200, body, ctype, {"Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"})
    def _packs_edit(self) -> None:
        """Owner-confirmed pack mutations; unlike the view this route is not read-only."""
        from . import knowledge, packs
        req = self._request(); action = req.get("action") if isinstance(req, dict) else None
        if not isinstance(action, str) or action not in PACK_WRITE_KEYS or set(req) != PACK_WRITE_KEYS[action] | {"action"}:
            self._json(400, {"error": "bad request"}); return
        try:
            if action == "preview": out = packs.preview(self.server.cfg, req["pack"], req["on"])
            elif action == "set": out = packs.set_pack(self.server.cfg, req["pack"], req["on"])
            elif action == "move": out = packs.move(self.server.cfg, req["item"], req["pack"])
            else: out = packs.export_pack(self.server.cfg, req["pack"], req["dir"])
            self._json(200, out)
        except packs.Unknown:
            self._json(404, {"error": "unknown"})
        except (packs.PackError, TypeError):
            self._json(400, {"error": "bad request"})
        except knowledge.KnowledgeError as exc:
            self._json(KNOWLEDGE_ERRORS.get(exc.code, 400), {"error": exc.code})
        except addons.AddonError as exc:
            self._json(ADDON_ERRORS.get(exc.code, 400), {"error": exc.code})
        except OSError:
            self._json(503, {"error": "unavailable"})

    def _schedule_off(self) -> bool:
        """The `schedule_card` add-on is off: the schedule routes are not there (404)."""
        if addons.enabled(self.server.cfg, "schedule_card"):
            return False
        self._json(404, {"error": "not found"})
        return True

    def _schedule_answer(self, item: Optional[dict] = None) -> None:
        sources = schedule.sources(self.server.cfg)
        if sources:
            self._extend(schedule.fetch_budget(sources) + 5)  # a first calendar fetch
        try:
            view = schedule.view(self.server.cfg, source_list=sources)
            try:
                saved = json.loads((state_dir() / "day-assistant.json").read_text(encoding="utf-8"))
                card = saved.get("card") if isinstance(saved, dict) else None
                if isinstance(card, dict) and card.get("date") == view.get("today") \
                        and isinstance(card.get("lines"), list):
                    view["day_card"] = {"title": str(card.get("title", ""))[:100],
                                        "lines": [str(line)[:500] for line in card["lines"][:8]]}
            except (OSError, ValueError):
                pass
            out = {"ok": True, "view": view}
            if item:
                out["item"] = {k: item[k] for k in ("id", "title", "date", "time", "done")}
            self._json(200, out)
        except Exception:
            self._json(500, {"error": "unavailable"})

    def _schedule_view(self) -> None:
        """{}: Today and Tomorrow for the Now card. Owner only; a calendar source is read only when one is configured."""
        if self._schedule_off():
            return
        req = self._request()
        if req is None or req:
            self._json(400, {"error": "bad request"})
            return
        self._schedule_answer()

    def _schedule(self) -> None:
        """{"action": "add", "text"}, {"action": "done", "id", "done"} or {"action": "remove", "id"} on the owner's own
        list (schedule.py). Owner only, fresh login; the text is never logged. Answers with the new view."""
        if self._schedule_off():
            return
        req = self._request()
        action = req.get("action") if req is not None else None
        if not isinstance(action, str) or action not in SCHEDULE_KEYS or set(req) != SCHEDULE_KEYS[action] | {"action"}:
            self._json(400, {"error": "bad request"})
            return
        if action in ("google-connect", "google-disconnect"):
            self._schedule_google(action)
            return
        try:
            if action == "add":
                row = schedule.add(self.server.cfg, req["text"])
            elif action == "done":
                row = schedule.set_done(req["id"], req["done"])
            elif action in ("swap", "extend", "skip"):
                from . import schedule_hours
                sources = schedule.sources(self.server.cfg)
                if sources:
                    self._extend(schedule.fetch_budget(sources) + 5)
                refreshed = schedule.view(self.server.cfg, source_list=sources)
                if not all(v["available"] for v in refreshed["timeline"].values()):
                    raise schedule.ScheduleError("unavailable")
                schedule_hours.change(self.server.cfg, req["date"], req["id"], action, req.get("minutes"))
                row = None
            else:
                schedule.remove(req["id"])
                row = None
        except schedule.ScheduleError as exc:
            self._json(SCHEDULE_ERRORS.get(exc.code, 400), {"error": exc.code})
            return
        self._schedule_answer(row)

    def _family_answer(self, item: Optional[dict] = None, extra: Optional[dict] = None) -> None:
        try:
            out = {"ok": True, "view": family_board.view(self.server.cfg)}
            if item:
                out["item"] = {k: item[k] for k in ("id", "text", "day", "done", "list")}
            out.update(extra or {})
            self._json(200, out)
        except Exception:
            self._json(500, {"error": "unavailable"})

    def _client_error(self) -> None:
        """{"name": "TypeError", "at": "render:2950:12"}: the page received the state but could not draw it. One
        stderr line with the error's type and where the page threw it; no message and no data, so a report can
        never carry the owner's content into the log."""
        req = self._request()
        name, at = (req.get("name"), req.get("at", "")) if req is not None else (None, None)
        if (set(req or {"": 0}) - {"name", "at"} or not isinstance(name, str) or not isinstance(at, str)
                or not CLIENT_ERROR_NAME.fullmatch(name) or not CLIENT_ERROR_AT.fullmatch(at)):
            self._json(400, {"error": "bad request"})
            return
        sys.stderr.write("%s panel: page render failed: %s at %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), name, at or "?"))
        sys.stderr.flush()
        self._json(200, {"ok": True})

    def _family_view(self) -> None:
        """{}: the family board. Owner only; a read with the same login age as /api/state."""
        req = self._request()
        if req is None or req:
            self._json(400, {"error": "bad request"})
            return
        self._family_answer()

    def _family(self) -> None:
        """{"action": "check", "id", "done"}, {"action": "add", "list", "text", "day"?}, {"action": "edit", "id", "text"?,
        "day"?}, {"action": "remove", "id"} or {"action": "clear"} on the family board (family_board.py). Owner only,
        fresh login; the text is never logged. Answers with the new view."""
        req = self._request()
        action = req.get("action") if req is not None else None
        if not isinstance(action, str) or action not in FAMILY_KEYS:
            self._json(400, {"error": "bad request"})
            return
        need, may = FAMILY_KEYS[action]
        keys = set(req) - {"action"}
        if not need <= keys or keys - need - may or (action == "edit" and not keys - {"id"}):
            self._json(400, {"error": "bad request"})
            return
        extra = None
        try:
            if action == "check":
                row = family_board.check(self.server.cfg, req["id"], req["done"])
            elif action == "add":
                row = family_board.add(req["list"], req["text"], req.get("day"))
            elif action == "edit":
                row = family_board.edit(req["id"], req.get("text"), req["day"] if "day" in req else family_board._KEEP)
            elif action == "remove":
                row = family_board.remove(req["id"])
                extra = {"removed": {k: row[k] for k in ("id", "text", "day", "list")}}
                row = None
            else:
                row, extra = None, {"cleared": family_board.clear_checked()}
        except family_board.BoardError as exc:
            self._json(FAMILY_ERRORS.get(exc.code, 400), {"error": exc.code})
            return
        self._family_answer(row, extra)

    def _family_print(self) -> None:
        """{}: a link to the A4 print page, good for 10 minutes and never past the owner's fresh login."""
        req = self._request()
        if req is None or req:
            self._json(400, {"error": "bad request"})
            return
        init = self.headers.get(INIT_HEADER, "")
        expires = min(int(time.time()) + family_print.TTL, int(dict(parse_qsl(init))["auth_date"]) + DECIDE_MAX_AGE)
        if expires <= time.time():
            self._json(403, {"error": "forbidden"})
            return
        value = family_print.token(self.server.bot_token, self.server.owner_id, expires)
        self._json(200, {"ok": True, "url": "/family/print?%s=%s" % (AUTH_PARAM, value), "expires": expires})

    def _family_print_get(self) -> None:
        parts = urlsplit(self.path)
        if not family_print.valid_token(query_auth(parts.query), self.server.bot_token, self.server.owner_id):
            self._json(403, {"error": "forbidden"})
            return
        nonce = secrets.token_urlsafe(16)
        try:
            body = family_print.render(family_board.view(self.server.cfg), nonce)
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        self._send(200, body, "text/html; charset=utf-8", {"Cache-Control": "private, no-store",
                   "Content-Security-Policy": family_print.CSP.format(n=nonce), "Referrer-Policy": "no-referrer"})

    def _schedule_google(self, action: str) -> None:
        """google-connect: a one-time sign-in link for the phone's browser (google_calendar.py). google-disconnect:
        the token is deleted here and revoked at Google; answers with the new view."""
        srv, G = self.server, google_calendar
        if not G.status(srv.cfg)["configured"]:
            self._json(409, {"error": "setup"})
            return
        if action == "google-connect":
            ticket = srv.google_flow.ticket(srv.owner_id)
            self._json(200, {"ok": True, "url": srv.cfg.panel_base_url + G.START_PATH + "?t=" + ticket})
            return
        self._extend(G.TIMEOUT + 10)  # the revoke at Google
        try:
            gone = G.disconnect(srv.cfg, srv.google_flow)
        except Exception:
            gone = False
        if not gone:
            self._json(503, {"error": "unavailable"})
            return
        self._schedule_answer()

    def _briefing_dismiss(self) -> None:
        """{}: put away today's morning briefing card. Owner only, fresh login; it changes nothing else."""
        req = self._request()
        if req is None or req:
            self._json(400, {"error": "bad request"})
            return
        try:
            briefing.dismiss(self.server.cfg, time.time())
        except OSError:
            self._json(503, {"error": "unavailable"})
            return
        self._json(200, {"ok": True})

    def _mac_tools(self) -> None:
        """{"action": ..., "confirm"?}: starts one Mac maintenance job and answers at once with its id (mac_tools.py).
        Owner only, fresh login. The real cleanup needs the id of a preview from the last ten minutes."""
        req = self._request()
        action = req.get("action") if req is not None else None
        confirm = req.get("confirm") if req is not None else None
        shape = {"dry-run": lambda: "confirm" not in req, "clean": lambda: isinstance(confirm, str) and len(confirm) <= 32,
                 "maintenance": lambda: confirm is True}
        if req is None or set(req) - {"action", "confirm"} or not isinstance(action, str) or action not in shape or not shape[action]():
            self._json(400, {"error": "bad request"})
            return
        if not mac_tools.available(self.server.cfg, action):
            self._json(404, {"error": "not found"})
            return
        try:
            out = self.server.mac_tools_manager.start(self.server.cfg, action, confirm)
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        if out["ok"]:
            self._json(200, out)
        else:
            self._json(404 if out["error"] == "unconfigured" else 409, {"error": out["error"], **({"kind": out["kind"]} if "kind" in out else {})})

    def _mac_tools_status(self) -> None:
        if not mac_tools.configured(self.server.cfg):
            self._json(404, {"error": "not found"})
            return
        try:
            self._json(200, self.server.mac_tools_manager.status(self.server.cfg))
        except Exception:
            self._json(500, {"error": "unavailable"})

    def _voice_copilot(self) -> None:
        """Signed owner-only HTTP streaming; no Google credentials ever cross this boundary."""
        action = self.path.rsplit("/", 1)[-1]
        try:
            voice_copilot.require_enabled()
            req = self._request()
            keys = {"start": set(), "audio": {"session", "data"}, "heartbeat": {"session"},
                    "events": {"session"}, "stop": {"session"}, "confirm": {"id", "digest"}}
            if not isinstance(req, dict) or set(req) != keys[action]:
                raise voice_copilot.Refused("bad")
            manager = self.server.voice_copilot
            if action == "confirm":
                try:
                    result = tower_home.confirm(self.server.cfg, req["id"], req["digest"])
                except KeyError:
                    self._json(409, {"ok": False, "error": "expired",
                                     "message": t("tower_confirm_expired", lang=self.server.cfg.ui_language)})
                    return
                self._json(200, {"ok": True, "confirmed": result})
                return
            if action == "start":
                self._json(200, manager.start())
                return
            call = manager.get(req["session"])
            if action == "stop":
                call.stop()
                self._json(200, {"ok": True})
            elif action == "audio":
                call.audio(req["data"])
                self._json(200, {"ok": True})
            elif action == "heartbeat":
                call.heartbeat()
                self._json(200, {"ok": True})
            else:
                if not call.consumer.acquire(blocking=False):
                    raise voice_copilot.Refused("busy")
                try:
                    self._voice_events(call)
                finally:
                    call.consumer.release()
        except voice_copilot.Refused as exc:
            self._json({"bad": 400, "cap": 429, "unavailable": 503}.get(exc.code, 409),
                       {"ok": False, "error": exc.code, "message": voice_copilot.MESSAGES[exc.code]})
        except Exception:
            # In particular don't pass transport errors to log_failure: they may carry a token.
            self._json(503, {"ok": False, "error": "unavailable",
                             "message": voice_copilot.MESSAGES["unavailable"]})

    def _voice_events(self, call) -> None:
        self._extend(voice_copilot.HARD_SECONDS + 10)
        self.close_connection = True
        self.send_response(200)
        for name, value in {"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-store",
                            "X-Accel-Buffering": "no", "Connection": "close",
                            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}.items():
            self.send_header(name, value)
        self.end_headers()
        try:
            while True:
                if not voice_copilot.enabled():
                    call.stop("disabled")
                try:
                    event = call.events.get(timeout=2)
                except queue.Empty:
                    event = ({"type": "closed", "reason": "closed", "message": voice_copilot.MESSAGES["closed"]}
                             if call.stopped.is_set() else {"type": "heartbeat"})
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
                if event["type"] == "closed":
                    break
        except (OSError, ValueError):
            pass
        finally:
            call.stop()  # a detached phone never leaves a billed session running

    def _setting(self) -> None:
        """One bridge setting: {"name": ..., "value": ...} (panel_settings.SETTINGS)."""
        req = self._request()
        if req is None or set(req) != {"name", "value"} or not panel_settings.allowed(req["name"], req["value"]):
            self._json(400, {"error": "bad request"})
            return
        try:
            results = panel_settings.write(req["name"], req["value"])
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        response = {"ok": True, "settings": panel_settings.read()}
        if req["name"] == "default_effort" and isinstance(results, list):
            response["profiles"] = results
            response["partial"] = any(not result.get("ok") for result in results)
        self._json(200, response)

    def _brief_seen(self) -> None:
        """The Morning Brief's "since you last looked" marker: {"at": <the state's "now" the owner saw>}."""
        req = self._request()
        at = req.get("at") if req is not None and set(req) == {"at"} else None
        if type(at) is not int or at <= 0:
            self._json(400, {"error": "bad request"})
            return
        try:
            done = brief.mark_seen(self.server.owner_id, at)
        except OSError:
            self._json(500, {"error": "unavailable"})
            return
        self._json(200, {"ok": True, "at": int(done)})

    def _block_mute(self) -> None:
        """Mute or unmute one session's blocks in the brief: {"session": <first 8 characters>, "on": bool}."""
        req = self._request()
        if (req is None or set(req) != {"session", "on"} or not isinstance(req["on"], bool)
                or not isinstance(req["session"], str) or not blocks.SHORT_RE.fullmatch(req["session"])):
            self._json(400, {"error": "bad request"})
            return
        try:
            found = blocks.set_muted(req["session"], req["on"])
        except (OSError, blocks.BlockError):
            self._json(503, {"error": "busy"})
            return
        self._json(200, {"ok": True, "muted": req["session"] in found})

    def _bank(self) -> None:
        """The reset bank: {"name": <provider>, "n": <saved resets>} (bank.write), or
        {"name": <provider>, "expires": "YYYY-MM-DD" | null} (bank.set_expiry)."""
        req = self._request()
        count = req is not None and set(req) == {"name", "n"} and bank.allowed(req["name"], req["n"])
        expiry = (req is not None and set(req) == {"name", "expires"} and bank.allowed(req["name"], 0)
                  and (req["expires"] is None or bank.valid_expiry(req["expires"])))
        if not (count or expiry):
            self._json(400, {"error": "bad request"})
            return
        try:
            if count:
                bank.write(req["name"], req["n"])
            else:
                bank.set_expiry(req["name"], req["expires"])
        except ValueError:
            self._json(400, {"error": "bad request"})
            return
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        self._json(200, {"ok": True, "bank": bank.view()})

    def _radio_say(self) -> None:
        """The owner's words, spoken on the radio now: {"text": ...} (`ctb radio say`)."""
        req = self._request()
        text = req.get("text") if req is not None and set(req) == {"text"} else None
        if not isinstance(text, str) or not text.strip() or len(text) > SAY_CHARS:
            self._json(400, {"error": "bad request"})
            return
        if not getattr(self.server.cfg, "radio", None):
            self._json(404, {"error": "not found"})
            return
        from . import radio
        try:
            radio.say(text)
        except radio.RadioError:
            self._json(409, {"error": "off"})
            return
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        self._json(200, {"ok": True})

    def _podcast(self) -> None:
        """The radio's podcast queue (radio_podcast.py): {"action": "episodes", "feed"} lists a configured feed's
        newest episodes; "add" {"feed", "episode"} (an id from that list) or {"link"} queues one after the radio's
        suitability rules; "control" {"op": play|pause|skip|stop}; "remove" {"id"}. Owner only, with the fresh
        login every action needs. An episode is only ever taken from a feed the owner configured, never from the
        page's say-so."""
        from . import radio_podcast as P
        req = self._request()
        action = req.get("action") if req is not None else None
        need, may = PODCAST_KEYS.get(action, (None, None)) if isinstance(action, str) else (None, None)
        if need is None or not (need <= set(req) - {"action"} <= need | may) or not all(
                isinstance(v, str) for k, v in req.items() if k != "action"):
            self._json(400, {"error": "bad request"})
            return
        if action == "add" and set(req) - {"action"} not in ({"link"}, {"feed", "episode"}):
            self._json(400, {"error": "bad request"})
            return
        cfg = self.server.cfg
        if not getattr(cfg, "radio", None):
            self._json(404, {"error": "not found"})
            return
        try:
            if action in ("episodes", "add"):
                self._extend(PODCAST_DEADLINE)
            if action == "episodes":
                feed = P.find_feed(cfg, req["feed"])
                if feed is None:
                    raise P.PodcastError("unknown")
                found = P.episodes(cfg, feed)
                self._json(200, {"ok": True, "episodes": [
                    {"id": e["id"], "title": e["title"], "seconds": e["seconds"], "published": e["published"]}
                    for e in found if not e["explicit"]]})
                return
            if action == "add":
                if "link" in req:
                    P.add_link(cfg, req["link"].strip())
                else:
                    P.add_picked(cfg, req["feed"], req["episode"])
            elif action == "control":
                P.control(req["op"])
            else:
                if not P.ITEM_ID.fullmatch(req["id"]):
                    raise P.PodcastError("bad")
                P.remove(req["id"])
        except P.PodcastError as e:
            self._json(PODCAST_ERRORS.get(e.kind, 400), {"error": e.kind})
            return
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        self._json(200, {"ok": True, "podcast": P.view(cfg)})

    def _radio_music(self) -> None:
        """Owner-only like/skip controls for the item on the private stream."""
        from . import radio_music
        req = self._request()
        if req is None or set(req) != {"action", "id"} or req.get("action") not in ("like", "skip"):
            self._json(400, {"error": "bad request"})
            return
        try:
            if req["action"] == "like":
                radio_music.like(req["id"])
            else:
                radio_music.request_skip(req["id"])
        except ValueError:
            self._json(404, {"error": "not found"})
            return
        self._json(200, {"ok": True})

    def _room(self) -> None:
        """The brainstorming room (radio_room.py): {"action": "start", "topic", "minutes", "project"?} | pause |
        resume {"text"?} | summarize | end | close | send {"task": n, "session": <first 8 characters>}. A task
        reaches a session only through "send", as a held proposal that approves nothing (room_send)."""
        from . import radio_room
        req = self._request()
        action = req.get("action") if req is not None else None
        need, may = ROOM_KEYS.get(action, (None, None)) if isinstance(action, str) else (None, None)
        if need is None or not (need <= set(req) - {"action"} <= need | may):
            self._json(400, {"error": "bad request"})
            return
        if not getattr(self.server.cfg, "radio", None):
            self._json(404, {"error": "not found"})
            return
        cfg = self.server.cfg
        try:
            if action == "start":
                minutes, project = req["minutes"], req.get("project", "")
                if type(minutes) is not int:
                    raise radio_room.RoomError("bad")
                room = radio_room.start(cfg, req["topic"], minutes, project)
                self.server.room_topic(room)
            elif action == "send":
                rid, task, short = req["room"], req["task"], req["session"]
                if not all(isinstance(v, str) for v in (rid, task, short)) or not (
                        ROOM_ID.fullmatch(rid) and ROOM_TASK.fullmatch(task) and SHORT_ID.fullmatch(short)):
                    raise radio_room.RoomError("bad")
                room_send(cfg, self.server.inbox(), short, rid, task)
            else:
                text = req.get("text", "")
                if not isinstance(text, str) or len(text) > radio_room.OWNER_CHARS:
                    raise radio_room.RoomError("bad")
                radio_room.control(action, words=text)
        except radio_room.RoomError as e:
            code = ROOM_ERRORS.get(e.kind, 400)
            self._json(code, {"error": e.kind})
            return
        except cards.CardError as e:
            code = {"unknown": 404, "closed": 409, "busy": 503}.get(e.kind, 400)
            self._json(code, {"error": {404: "not found", 409: "closed", 503: "busy"}.get(code, "bad request")})
            return
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        self._json(200, {"ok": True, "room": radio_room.view(cfg)})

    # --- cockpit apps ------------------------------------------------------------------------

    def _card_media_token(self, init: str) -> None:
        req = self._request()
        if (req is None or set(req) != {"id"} or not isinstance(req["id"], str)
                or not card_media.ID.fullmatch(req["id"])):
            self._json(400, {"error": "bad request"})
            return
        decision_image = self.server.cfg.rich_decisions_enabled is True and decisions.image_available(req["id"])
        if not cards.image_available(req["id"]) and not decision_image:
            self._json(404, {"error": "not found"})
            return
        expires = min(int(time.time()) + APP_TOKEN_TTL, int(dict(parse_qsl(init))["auth_date"]) + DECIDE_MAX_AGE)
        if expires <= time.time():
            self._json(403, {"error": "forbidden"})
            return
        token = card_media_token(self.server.bot_token, self.server.owner_id, req["id"], expires)
        self._json(200, {"token": token, "expires": expires})

    def _card_media_get(self) -> None:
        parts = urlsplit(self.path)
        match = CARD_MEDIA_GET.fullmatch(parts.path)
        if not match:
            self._json(404, {"error": "not found"})
            return
        if not valid_card_media_token(query_auth(parts.query), self.server.bot_token, self.server.owner_id, match[1]):
            self._json(403, {"error": "forbidden"})
            return
        found = cards.image(match[1])
        if found is None and self.server.cfg.rich_decisions_enabled is True:
            found = decisions.image(match[1])
        if found is None:
            self._json(404, {"error": "not found"})
            return
        self._extend(MEDIA_DEADLINE)
        self._send(200, *found, {"Cache-Control": "private, no-store"})

    def _app_token(self, init: str) -> None:
        """Only the authenticated parent can mint; never extend the owner's action-login window."""
        req = self._request()
        if req is None or set(req) != {"app"} or not isinstance(req["app"], str) or not cockpit_apps.NAME.fullmatch(req["app"]):
            self._json(400, {"error": "bad request"})
            return
        try:
            app = cockpit_apps.find(req["app"])
        except Exception:
            app = None
        if app is None:
            self._json(404, {"error": "not found"})
            return
        expires = min(int(time.time()) + APP_TOKEN_TTL,
                      int(dict(parse_qsl(init))["auth_date"]) + DECIDE_MAX_AGE)
        if expires <= time.time():
            self._json(403, {"error": "forbidden"})
            return
        token = app_token(self.server.bot_token, self.server.owner_id, req["app"], expires=expires)
        self._json(200, {"token": token, "expires": expires})

    def _extend(self, seconds: float) -> None:
        """A longer deadline for this connection, once the request is read and its sender authenticated."""
        old = getattr(self, "_deadline", None)
        if old is None:
            return  # a handler without a connection (tests build some directly): nothing to cut
        old.cancel()
        self._deadline = threading.Timer(seconds, self._cut)
        self._deadline.daemon = True
        self._deadline.start()

    def _app_get(self) -> None:
        """An app's page or media, authenticated only by a token scoped to this exact app.
        The query supports iframe/media elements; the check precedes every app/file lookup."""
        parts = urlsplit(self.path)
        m = APP_GET.fullmatch(parts.path)
        if not m:
            self._json(404, {"error": "not found"})
            return
        token = self.headers.get(APP_TOKEN_HEADER) or query_auth(parts.query)
        if not valid_app_token(token, self.server.bot_token, self.server.owner_id, m.group(1)):
            self._json(403, {"error": "forbidden"})
            return
        try:
            app = cockpit_apps.find(m.group(1))
        except Exception:
            app = None
        if app is None:
            self._json(404, {"error": "not found"})
            return
        if m.group(2) is None:
            try:
                with open(app.page, "rb") as f:
                    page = f.read(cockpit_apps.MAX_PAGE + 1)
            except OSError:
                page = b""
            if not page or len(page) > cockpit_apps.MAX_PAGE:
                self._json(404, {"error": "not found"})
                return
            self._send(200, page, "text/html; charset=utf-8",
                       {"Content-Security-Policy": app_csp(self.headers.get("Host", ""))})
            return
        try:
            rel = unquote(m.group(2), errors="strict")
        except UnicodeDecodeError:
            rel = ""
        found = cockpit_apps.media_file(app, rel) if rel else None
        if found is None:
            self._json(404, {"error": "not found"})
            return
        self._media(*found)

    def _media(self, fd: int, ctype: str) -> None:
        """Take ownership of the verified descriptor and stream it, whole or one byte range."""
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                self._json(404, {"error": "not found"})
                return
            size = st.st_size
            want = cockpit_apps.parse_range(self.headers.get("Range"), size)
            if want == cockpit_apps.UNSATISFIABLE:
                self._send(416, b"", "text/plain; charset=utf-8", {"Content-Range": f"bytes */{size}"})
                return
            start, end = want if want else (0, size - 1)
            length = max(0, end - start + 1)
            self._extend(MEDIA_DEADLINE)
            self.close_connection = True
            self.send_response(206 if want else 200)
            self.send_header("Connection", "close")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            if want:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")  # do not serve cached media after token expiry
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
            self.end_headers()
            f.seek(start)
            left = length
            from . import policy
            try:
                while left > 0:
                    block = f.read(min(cockpit_apps.READ_BLOCK, left))
                    if not block:
                        break
                    if not policy.on():
                        break  # mirroring went off mid-stream: the rest is not sent (the connection closes)
                    self.wfile.write(block)
                    left -= len(block)
            except OSError:
                pass  # the player hung up or moved on to another range

    def _app_api(self, name: str) -> None:
        """One call to an app's API: the JSON body goes to the app's command (cockpit_apps.run_command). It acts,
        authenticated with this app's short-lived token, never the owner's full login."""
        if not self._body_ok(cockpit_apps.API_BODY):
            return
        token = self.headers.get(APP_TOKEN_HEADER, "")
        if not valid_app_token(token, self.server.bot_token, self.server.owner_id, name):
            _app_log(name, 403)
            self._json(403, {"error": "forbidden"})
            return
        try:
            app = cockpit_apps.find(name)
        except Exception:
            app = None
        if app is None or not app.command:
            self._json(404, {"error": "not found"})
            return
        if not isinstance(self._request(), dict):
            self._json(400, {"error": "bad request"})
            return
        self._extend(cockpit_apps.API_TIMEOUT + 5)
        try:
            code, data = cockpit_apps.run_command(app, self._body)
        except Exception:
            code, data = 502, {"error": "app failed"}
        _app_log(name, code)
        self._json(code, data)

    def _no(self):
        self._json(405, {"error": "method not allowed"})

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _no

    def log_message(self, format, *args):
        pass


SLOW_SECONDS = 3.0


def log_slow(what: str, began: float, **marks) -> None:
    """One stderr line (the panel's launchd log) when a request took longer than SLOW_SECONDS: the time to each
    named step since `began`, and plain counts. Never any data. Owner report #339: the iPhone Cockpit tab gave up
    after 25 s and the panel kept no trace of how long its answer took."""
    try:
        total = max(v for v in marks.values() if isinstance(v, float)) - began if marks else 0.0
        if total < SLOW_SECONDS:
            return
        parts = " ".join("%s=%.1fs" % (k, v - began) if isinstance(v, float) else "%s=%s" % (k, v)
                         for k, v in marks.items())
        sys.stderr.write("%s panel: slow %s %.1fs: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), what, total, parts))
        sys.stderr.flush()
    except Exception:
        pass


# Why a Live Activity has no push token, as the app reports it: request-failed:<ActivityAuthorizationError case or
# NSError domain.code>, no-token-after-grace or push-disabled. Nothing else is accepted or logged.
LA_PROBLEM = re.compile(r"(?:no-token-after-grace|push-disabled|request-failed(?::[A-Za-z0-9._-]{1,64})?)")


def log_token(outcome: str, **facts) -> None:
    """One stderr line per Live Activity token request: the outcome and plain facts (who: device/owner, ended,
    env, or the app's allow-listed problem code). Never the token, card id or login. Owner report #361: the phone never registered and the panel kept no
    trace of whether it tried."""
    try:
        parts = " ".join("%s=%s" % (k, v) for k, v in facts.items())
        sys.stderr.write("%s panel: live-activity token %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), outcome, parts))
        sys.stderr.flush()
    except Exception:
        pass


def log_failure(what: str, exc: BaseException) -> None:
    """One line on stderr (the panel's launchd log): the exception's type and the frame that raised it. Never its
    message or arguments, which can quote the owner's data."""
    try:
        frames = traceback.extract_tb(exc.__traceback__)
        where = "%s:%d in %s" % (os.path.basename(frames[-1].filename), frames[-1].lineno, frames[-1].name) if frames else "?"
        sys.stderr.write("%s panel: %s failed: %s at %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), what,
                                                              type(exc).__name__, where))
        sys.stderr.flush()
    except Exception:  # logging must never break the reply
        pass


def _app_log(name: str, code: int) -> None:
    """One line per app API call (time, app, status): no body, no token, no user data. Bounded to ~200 lines."""
    try:
        path = state_dir() / "app-api.log"
        lines = path.read_text().splitlines()[-199:] if path.exists() else []
        lines.append(f"{time.strftime('%H:%M:%S')} {name} {int(code)}")
        write_private(path, "\n".join(lines) + "\n")
    except (OSError, ValueError):
        pass


def _ended_sessions(cfg: Config) -> set:
    topics = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg)
    return {e.session for e in collect(topics) if e.state == "ended"}


def compact_limit_to(cfg: Config, short: str, limit: Optional[int], now: Optional[float] = None) -> None:
    """Set the own compaction threshold of the one live session the page calls `short` (None = the global one)."""
    from . import compact
    now = time.time() if now is None else now
    topics = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg)
    found = [e for e in collect(topics, now=now) if e.session[:8] == short and not e.closed]
    if len(found) != 1 or found[0].state == "ended":
        raise cards.CardError("no such session", "unknown")
    e = found[0]
    if e.chat is not None and e.chat not in decisions.owner_chats(cfg):
        raise cards.CardError("no such session", "unknown")
    compact.set_session_limit(e.session, limit)


def _session_compact(session: str) -> Optional[int]:
    try:
        from .compact import session_limit
        return session_limit(session)
    except Exception:
        return None


def screen_of(cfg: Config, short: str, now: Optional[float] = None) -> str:
    """The bottom rows of the terminal of the one live session the page calls `short`, redacted as everything
    that leaves the machine is; "" when the session has no terminal the bridge can read."""
    from . import inbox as I
    now = time.time() if now is None else now
    topics = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg)
    found = [e for e in collect(topics, now=now) if e.session[:8] == short and not e.closed]
    if len(found) != 1 or found[0].state == "ended":
        raise cards.CardError("no such session", "unknown")
    e = found[0]
    if e.chat is not None and e.chat not in decisions.owner_chats(cfg):
        raise cards.CardError("no such session", "unknown")
    # stop_pane, not pane_for_session: a saved pane id outlives a tmux restart and is then reused by another
    # window, so only a pane proven to host this session is ever read (security review of the live screen)
    pane = I.stop_pane(e.session)
    if not pane:
        return ""
    try:
        body = I.pane_text(pane, lines=SCREEN_ROWS)
    except OSError:
        return ""
    body = I.relay_screen_text(body)
    rows = [r.rstrip() for r in body.replace("\u200f", "").splitlines()]
    while rows and not rows[-1].strip():
        rows.pop()
    return "\n".join(rows[-SCREEN_ROWS:])


@contextlib.contextmanager
def _sends(until: float, now: float, inbox):
    """The record of the sends already queued, under its lock: one send id is queued once, however often the
    page retries it. Without it a retry after a lost answer queued the same words again as a fresh item, and
    a "yes" meant for one question could then approve the session's next one (security review)."""
    from .config import private_dir, write_private
    base = private_dir(state_dir())
    with open(base / (SENDS_FILE + ".lock"), "a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= until:
                    raise cards.CardError("busy, try again", "busy") from None
                time.sleep(0.05)
        try:
            seen = json.loads((base / SENDS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            seen = {}
        seen = {k: v for k, v in seen.items() if isinstance(v, dict)} if isinstance(seen, dict) else {}
        before = json.dumps(seen, sort_keys=True)
        for key, rec in list(seen.items()):
            if rec.get("final_state") not in ("delivered", "cancelled"):
                state = "cancelled" if rec.get("cancelled") else (
                    inbox.delivery_state(rec["session"], rec["key"])
                    if isinstance(rec.get("session"), str) and isinstance(rec.get("key"), str) else "unknown")
                if state in ("delivered", "cancelled"):
                    rec.update(final_state=state, final_at=now)
            if rec.get("final_state") in ("delivered", "cancelled") and now - rec.get("final_at", now) >= SENDS_KEPT:
                del seen[key]
        yield seen
        if json.dumps(seen, sort_keys=True) != before:
            write_private(base / SENDS_FILE, json.dumps(seen))


def say_to(cfg: Config, inbox, short: str, text: str, now: Optional[float] = None, send: Optional[str] = None) -> dict:
    """Queue the owner's words to the one live session the page calls `short`. The session reads the server's
    wording with the owner's words quoted, marked as coming from the panel (it counts as the owner's own word).
    `send`: the page's id of this send; a retry carrying an id already queued queues nothing and gets the first
    answer back with "again"."""
    from .inbox import LockBusy
    from .progress import append_boundary
    now = time.time() if now is None else now
    text = cards._clean(text, SAY_TO_CHARS, "message", multiline=True, owner_text=True)
    topics = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg)
    found = [e for e in collect(topics, now=now) if e.session[:8] == short and not e.closed]
    if len(found) != 1:
        raise cards.CardError("no such session", "unknown")
    e = found[0]
    if e.state == "ended":
        raise cards.CardError("this session has ended", "closed")
    if e.chat is not None and e.chat not in decisions.owner_chats(cfg):
        raise cards.CardError("no such session", "unknown")  # a topic in a chat the owner is not obeyed in
    until = time.monotonic() + decisions.LOCK_WAIT
    body = "\n".join([f"✍️ The owner wrote to this session from the control panel: {decisions.quoted(text)}",
                      WRITTEN_IN_PANEL])
    key = secrets.token_hex(6)
    item = {"text": body, "at": int(now), "id": None, "source": "panel", "key": key, "said": text,
            "chat": home_chat(cfg) if e.chat is None else e.chat}
    try:
        # "said" is the owner's own words, kept only so the page can show them while they wait and hand them
        # back for an edit; the session reads "text"
        with _sends(until, now, inbox) as seen:
            first = seen.get(send) if send else None
            if first is not None:  # a retry of a send that did arrive: nothing is queued twice
                return {"session": str(first.get("session") or e.session), "key": str(first.get("key") or ""),
                        "again": True}
            inbox.put(e.session, item, until=until)
            seen[send or key] = {"session": e.session, "key": key, "at": now, "text": text}
    except LockBusy:
        raise cards.CardError("busy, try again", "busy") from None
    try:
        append_boundary(e.session, until=max(until, time.monotonic() + 0.5))
    except (OSError, TimeoutError) as exc:
        from .inbox import poll_log
        poll_log(f"panel boundary {e.session[:8]}: {type(exc).__name__}")
    return {"session": e.session, "key": key}


def room_send(cfg: Config, inbox, short: str, room_id: str, task_id: str, now: Optional[float] = None) -> str:
    """Queue one task of one brainstorming room (the ids the page showed) to the one live session the page calls
    `short` (the owner tapped "send to session" on it). The task is claimed first, atomically and checked against
    the room it came from (radio_room.claim), so simultaneous taps and stale pages cannot claim the same task;
    abandoned claims expire and the item is queued under one fixed key. Unlike the owner's own words from the
    panel, it is framed as a button tap (never an approval) and held until the end of the session's current turn,
    so a running session is never interrupted. Returns the session's topic name."""
    from . import radio_room
    from .inbox import LockBusy
    now = time.time() if now is None else now
    topics = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg)
    found = [e for e in collect(topics, now=now) if e.session[:8] == short and not e.closed]
    if len(found) != 1:
        raise cards.CardError("no such session", "unknown")
    e = found[0]
    if e.state == "ended":
        raise cards.CardError("this session has ended", "closed")
    if e.chat is not None and e.chat not in decisions.owner_chats(cfg):
        raise cards.CardError("no such session", "unknown")
    name = getattr(e, "name", "") or short
    claim_id = secrets.token_hex(16)
    task = radio_room.claim(room_id, task_id, name, now, claim_id)
    try:
        body = radio_room.proposal_item(task, radio_room.load().get("topic") or "")
        item = {"text": body, "at": int(now), "id": None, "source": "owner", "button": True, "hold": True,
                "room": True, "key": radio_room.delivery_key(room_id, task_id),
                "chat": home_chat(cfg) if e.chat is None else e.chat}
        inbox.put(e.session, item, until=time.monotonic() + decisions.LOCK_WAIT)
    except LockBusy:
        radio_room.unclaim(room_id, task_id, claim_id)
        raise cards.CardError("busy, try again", "busy") from None
    except BaseException:
        radio_room.unclaim(room_id, task_id, claim_id)
        raise
    radio_room.mark_sent(room_id, task_id, name, claim_id, now)
    return name


def left_behind(e) -> bool:
    """An open session that is not working and that nobody touched for a day."""
    age = getattr(e, "age", None)
    return not e.closed and e.state != "working" and age is not None and age >= LEFT_BEHIND


def close_left(cfg: Config, bot, shorts: list, now: Optional[float] = None) -> dict:
    """Close the Telegram topics of the left-behind sessions the page names. Only a session the server itself
    counts as left behind is closed (a stale page cannot close a session that woke up); the terminal is not
    touched, and writing in the topic reopens it."""
    from . import sessions_menu
    now = time.time() if now is None else now
    topics = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg)
    entries = collect(topics, now=now)
    closed = skipped = 0
    for short in shorts:
        found = [e for e in entries if e.session[:8] == short and left_behind(e)]
        if (len(found) != 1 or getattr(found[0], "thread", None) is None
                or (found[0].chat is not None and found[0].chat not in decisions.owner_chats(cfg))):
            skipped += 1
            continue
        result = sessions_menu._close_topic(cfg, bot, topics, found[0])
        if result in ("ok", "gone"):
            closed += 1
        else:
            skipped += 1
            if result == "flood":  # Telegram asked to slow down: the rest wait for the next tap
                skipped += len(shorts) - closed - skipped
                break
    return {"closed": closed, "skipped": skipped}


def clear_reminders(cfg: Config, bot, refs: list, action: str = "clear") -> dict:
    """Clear the reminders the page names (each "topic hash:item id"), as a tap on the board's own button does,
    and redraw each board. The sessions are not woken: clearing ten old reminders must not start ten agents."""
    wanted = {m.groups() for m in map(REMINDER_REF.fullmatch, refs) if m}
    cleared = 0
    for chat, thread, item in board.open_reminders():
        ref = (board.key_hash(board.key_of(chat, thread)), str(item["i"]))
        if ref not in wanted or chat not in decisions.owner_chats(cfg):
            continue

        def change(topic: dict, ident=item["i"]) -> dict:
            found = [i for i in topic["items"] if i["i"] == ident and i["k"] == "remind"]
            if not found:
                raise board.BoardError("stale")
            if action == "restore":
                found[0]["r"] = time.time()
                return found[0]
            return board.finish(topic, found[0], time.time())
        try:
            board.apply(cfg, bot, chat, thread, change, sync=False)
            cleared += 1
        except board.BoardError:
            pass
    return {"restored" if action == "restore" else "cleared": cleared}


def _panel_words(item: dict) -> bool:
    return (item.get("source") == "panel" and isinstance(item.get("key"), str)
            and bool(SAY_KEY.fullmatch(item["key"])) and isinstance(item.get("said"), str))


def delivery_receipts(inbox=None, now: Optional[float] = None) -> list:
    """Undelivered answers survive page reloads and age; a queue take is in flight, never received.
    Reads only the existing atomic state/queue/lease/proof records. No terminal actions on a state poll.
    Old answers without a key have no reliable receipt and are not guessed to be delivered."""
    from .inbox import Inbox
    inbox = inbox if inbox is not None else Inbox(state_dir() / "inbox")
    now = time.time() if now is None else now
    receipts = []
    for module, kind in ((cards, "card"), (decisions, "decision")):
        with module._locked(time.monotonic() + decisions.LOCK_WAIT) as path:
            records = module._load(path)
            changed = False
            for rec in records.values():
                if rec["state"] != "answered" or not rec.get("delivery_key"):
                    continue
                state = rec.get("final_state") or inbox.delivery_state(rec["session"], rec["delivery_key"])
                if state == "delivered" and not rec.get("final_state"):
                    rec.update(final_state=state, final_at=now)
                    changed = True
                receipts.append((rec["session"], rec["delivery_key"], kind, rec["id"], rec["title"],
                                 rec["closed"], state, rec.get("final_at")))
            if changed:
                module._save(path, records)
    with _sends(time.monotonic() + decisions.LOCK_WAIT, now, inbox) as sends:
        for rec in sends.values():
            if (isinstance(rec.get("session"), str) and isinstance(rec.get("key"), str)
                    and decisions._num(rec.get("at"))):
                state = rec.get("final_state") or inbox.delivery_state(rec["session"], rec["key"])
                receipts.append((rec["session"], rec["key"], "say", rec["key"],
                                 rec.get("text", "") if isinstance(rec.get("text", ""), str) else "",
                                 rec["at"], state, rec.get("final_at")))
    out = []
    for sid, key, kind, ident, title, at, state, final_at in sorted(
            receipts, key=lambda r: (r[6] in ("delivered", "cancelled"), -r[5])):
        if state == "cancelled" or (state == "delivered" and final_at is not None
                                    and now - final_at >= SENDS_KEPT):
            continue
        out.append({"key": key, "id": ident, "kind": kind, "session": sid[:8], "title": title,
                    "state": state, "age": _age(now, at),
                    "retrying": state in ("queued", "in_flight") and inbox.wake_error(sid, key)})
        if len(out) >= 60:
            break
    _secret_free(out)
    return out


def waiting_words(session: str, now: float, inbox=None) -> list:
    """What the owner wrote to this session in the panel that it has not read yet, oldest first."""
    if inbox is None:
        from .inbox import Inbox
        inbox = Inbox(state_dir() / "inbox")
    try:
        items = [dict(i, state="queued") for i in inbox.peek(session)]
        items += [dict(i, state="in_flight") for lease in inbox.leases(session) if not lease.get("requeued_at")
                  for i in lease.get("items") or [] if isinstance(i, dict)]
    except Exception:
        return []
    unique = {i["key"]: i for i in items if _panel_words(i)}
    return [{"key": i["key"], "text": i["said"], "age": _age(now, i.get("at") or now), "state": i["state"],
             "retrying": inbox.wake_error(session, i["key"])}
            for i in sorted(unique.values(), key=lambda i: i.get("queued_at") or i.get("at") or now)][:MAX_WAITING]


def unsay(cfg: Config, inbox, short: str, key: str, now: Optional[float] = None) -> Optional[str]:
    """Remove the owner's still-waiting words `key` from the session the page calls `short`. Returns the words,
    or None when the session already took them (nothing is removed then)."""
    from .inbox import LockBusy
    now = time.time() if now is None else now
    topics = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg)
    found = [e for e in collect(topics, now=now) if e.session[:8] == short]
    if len(found) != 1:
        raise cards.CardError("no such session", "unknown")
    taken = []

    def keep(item: dict) -> bool:
        if not taken and _panel_words(item) and item["key"] == key:
            taken.append(item["said"])
            return False
        return True
    try:
        with _sends(time.monotonic() + decisions.LOCK_WAIT, now, inbox) as seen:
            inbox.filter(found[0].session, keep)
            if taken:
                for rec in seen.values():
                    if rec.get("session") == found[0].session and rec.get("key") == key:
                        rec.update(cancelled=True, final_state="cancelled", final_at=now)
    except LockBusy:
        raise cards.CardError("busy, try again", "busy") from None
    return taken[0] if taken else None


class PanelServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # a client hanging up early is not worth a traceback in the launchd log

    def __init__(self, addr, cfg: Config, bot=None, inbox=None):
        super().__init__(addr, PanelHandler)
        self.cfg = cfg
        self._bot, self._inbox = bot, inbox
        self.bot_token = cfg.token()
        self.owner_id = cfg.owner_user_id
        self.lang = cfg.ui_language
        self.translator = panel_text.Translator(cfg)
        self.state_cache = state_cache.StateCache()
        self.voice_copilot = voice_copilot.Manager(cfg, self.inbox)
        self.youtube_flow = panel_youtube.Flow()
        self.google_flow = google_calendar.Flow()
        # Import now what the state and the actions use later (the routing and settings code is large): a first
        # import inside a request could outlast DEADLINE on a busy machine.
        from . import agents_topic, autopilot, delivery, inbox, progress, voice  # noqa: F401
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def server_close(self):
        if hasattr(self, "voice_copilot"):
            self.voice_copilot.close()
        super().server_close()

    # the YouTube staff radio: what tests replace with fakes (the Keychain, Google, the detached CLI)
    app_request_gate = app_request.GATE   # tests replace these two
    app_request_runner = None
    mac_tools_manager = mac_tools.MANAGER   # tests replace this
    youtube_request = None
    youtube_store_obj = None
    _youtube_ready_at = (0.0, False)

    def youtube_store(self):
        if self.youtube_store_obj is None:
            from . import radio_youtube as Y
            self.youtube_store_obj = Y.KeychainStore(Y.settings(self.cfg)["keychain_service"])
        return self.youtube_store_obj

    def youtube_client(self) -> Optional[dict]:
        from . import radio_youtube as Y
        return Y.web_client(self.youtube_store())

    def youtube_ready(self):
        """(the web sign-in client is in the Keychain, the panel's public base is configured). The Keychain
        check (whether the items exist; no secret is read) is remembered for a minute."""
        from . import radio_youtube as Y
        at, client = self._youtube_ready_at
        if time.time() - at > 60:
            store = self.youtube_store()
            client = store.has(Y.WEB_CLIENT_ID) and store.has(Y.WEB_CLIENT_SECRET)
            self._youtube_ready_at = (time.time(), client)
        return client, bool(self.cfg.panel_base_url)

    def youtube_service(self):
        from . import radio_youtube as Y
        return Y.Service(Y.settings(self.cfg), store=self.youtube_store(), client=self.youtube_client(),
                         request=self.youtube_request or Y.http)

    def youtube_describe(self, svc, gen=None) -> None:
        from . import radio_youtube as Y
        try:
            Y.describe_channel(svc, gen)
        except Exception:
            pass  # the card shows "connected" without the channel name until the next sign-in or status

    def youtube_view(self) -> Optional[dict]:
        from . import radio_youtube as Y
        try:
            return Y.view(self.cfg, *self.youtube_ready())
        except Exception:
            return None

    def youtube_spawn(self, action: str, request: Optional[str] = None) -> None:
        from . import radio_youtube as Y
        Y.spawn(action, request)

    def radio_switch(self, action: str) -> None:
        from . import radio
        radio.spawn(action)

    def room_topic(self, room: dict) -> None:
        """The room's topic in the home group, made (or reused) and told about the new room, off the request's
        thread (a Telegram call must not hold the panel's DEADLINE). Tests replace it."""
        from . import radio_room

        def work():
            radio_room.ensure_topic(self.cfg, self.bot(), room.get("lang") or "he", room.get("topic") or "")
        threading.Thread(target=work, daemon=True, name="room-topic").start()

    def device_paired(self, label: str) -> None:
        """Tell the owner in the bot's private chat that a widget device was paired (a replayed login would show up
        here), off the request's thread so a Telegram call never holds the panel's DEADLINE. Tests replace it."""
        from .i18n import t
        from .identity import isolate

        def work():
            try:
                self.bot().call("sendMessage", {"chat_id": self.cfg.owner_chat_id,
                                                "text": t("widget_paired", lang=self.lang, label=isolate(label, self.lang))})
            except Exception:
                pass  # the pairing stands; Settings lists it either way
        threading.Thread(target=work, daemon=True, name="device-paired").start()

    def bot(self):
        if self._bot is None:
            from .telegram import Bot
            self._bot = Bot.from_config(self.cfg)
        return self._bot

    def inbox(self):
        if self._inbox is None:
            from .inbox import Inbox
            self._inbox = Inbox(state_dir() / "inbox", self.cfg.max_queue)
        return self._inbox

    def process_request(self, request, client_address):
        # A flood of slow connections must not grow threads without bound: extra ones are closed.
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


REFRESH_TIMEOUT = 30  # seconds the Studio refresh may take in its own process


def refresh_installed_apps(run=None) -> bool:
    """Rewrite the Studio's app folder from this code when it is installed: its page is a copy, so an update that
    skipped `harnessgram studio install` left the Cockpit on an old page that never loaded (owner, 16.11).

    The install loads the Studio's adapter, which may hang, so it runs in its own process with a time limit, and
    serve() starts it on a background thread: the panel never waits for it. Never installs it where it is not,
    never raises."""
    import logging
    import subprocess
    import sys
    try:
        from .cockpit_apps import apps_dir
        if not (apps_dir() / "studio" / "manifest.json").is_file():
            return False
        run = run or subprocess.run
        # -I: no current folder, user site or PYTHON* variables on the path, so only this install's `ctb` loads.
        # The working folder stays the panel's own (relative state settings keep their meaning), and the apps
        # folder goes over as an absolute path.
        base = os.path.abspath(apps_dir())
        done = run([sys.executable, "-I", "-c", "import sys; from ctb.studio import install; install.install(sys.argv[1])",
                    base], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=REFRESH_TIMEOUT, check=False)
        return done.returncode == 0
    except Exception as exc:
        logging.getLogger(__name__).warning("Studio refresh failed (%s)", type(exc).__name__)
        return False


def serve(cfg: Config, port: int = 8765, host: str = "127.0.0.1") -> int:
    from . import buildstamp
    server = PanelServer((host, port), cfg)
    buildstamp.record_running("panel")  # doctor tells when an install left this copy on older code
    import threading
    threading.Thread(target=refresh_installed_apps, name="studio-refresh", daemon=True).start()
    try:
        server.serve_forever()
    finally:
        buildstamp.clear_running("panel")
    return 0
