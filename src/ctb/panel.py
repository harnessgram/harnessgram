"""The control panel: a Telegram Mini App served on loopback.

`GET /` is one static page; `POST /api/state` returns the open decisions, the owner's reminders, the
sessions and the quota as JSON, `POST /api/decide` records the owner's yes/no on one decision, and
`POST /api/radio` turns the team radio on or off ({"on": true|false}; see radio.py), `/api/radio-say` speaks
the owner's words on it, `/api/model` sets the model new sessions start with (models.set_default),
`/api/setting` changes one bridge setting (panel_settings.py) and `/api/dismiss` clears stale decisions.
`POST /api/youtube` and `GET /oauth/youtube/start|callback` are the YouTube staff radio's controls and its web
sign-in (panel_youtube.py: the OAuth routes are bound to a ticket from the owner's signed request and to the
browser that took it). Cockpit
apps (cockpit_apps.py) add `GET /app/<name>/`, `GET /app/<name>/media/<path>` and `POST /app/<name>/api`. All
need a request that carries Telegram's signed `initData` of the paired owner (checked exactly as the
Telegram docs describe: HMAC-SHA256 with a key derived from the bot token). Anything else gets a bare 403;
an answer needs a fresher login (DECIDE_MAX_AGE) than reading does (MAX_AGE).
The server binds to 127.0.0.1; how it is reached from a phone (a private network or a public HTTPS route)
is up to the operator, see docs/panel.md ("What it needs", "Exposure options").
Neither the token nor initData is ever logged or echoed.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import socket
import stat
import threading
import time
from typing import Optional
from urllib.parse import parse_qsl, unquote, urlsplit

from . import accounts, bank, board, blocks, brief, cards, cockpit_apps, decisions, models, panel_page, panel_settings, panel_text, panel_view, status, usage, session_usage
from . import auto_progress, mac_resources, panel_youtube
from .config import Config, home_chat, state_dir
from .inbox import PanelQueueFull
from .sessions_menu import collect
from .topics import TopicMap
from .panel_page import PAGE, STRINGS  # noqa: F401  (the page and its strings table)

MAX_BODY = 64  # the page posts "{}"; a larger body only holds a thread
MAX_DECIDE_BODY = 1280  # {"id", "choice", "note"}: a 300-character note takes up to 1200 bytes
MAX_CONNECTIONS = 16
MAX_INIT_DATA = 4096
MAX_AGE = 3600  # seconds an auth_date stays valid for reading the state
DECIDE_MAX_AGE = 900  # and for answering a decision
FUTURE_SLACK = 30
TIMEOUT = 10
DEADLINE = 5  # seconds a whole connection may take; slow senders cannot hold the 16 slots
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
          "/api/setting": MAX_BODY, "/api/dismiss": 1280, "/api/radio-say": 2600, "/api/card": MAX_DECIDE_BODY, "/api/say": 2600, "/api/bank": MAX_BODY,
          "/api/unsay": MAX_BODY, "/api/close": 1280, "/api/remind": 2600, "/api/screen": MAX_BODY,
          "/api/compact": MAX_BODY, "/api/room": 2600,
          "/api/brief-seen": MAX_BODY, "/api/block-mute": MAX_BODY}
ACTIONS = {"/api/model": "_model", "/api/setting": "_setting", "/api/dismiss": "_dismiss", "/api/radio-say": "_radio_say",
           "/api/card": "_card", "/api/say": "_say", "/api/bank": "_bank", "/api/unsay": "_unsay",
           "/api/close": "_close", "/api/remind": "_remind", "/api/screen": "_screen",
           "/api/compact": "_compact", "/api/room": "_room",
           "/api/brief-seen": "_brief_seen", "/api/block-mute": "_block_mute"}
ROOM_KEYS = {"start": ({"topic", "minutes"}, {"project"}), "pause": (set(), set()), "resume": (set(), {"text"}),
             "summarize": (set(), set()), "end": (set(), set()), "close": (set(), set()),
             "send": ({"room", "task", "session"}, set())}
ROOM_ID = re.compile(r"[0-9a-f]{8}")         # radio_room.start: secrets.token_hex(4)
ROOM_TASK = re.compile(r"t\d[0-9a-f]{6}")    # radio_room.Conductor._summary  # the brainstorming room's actions: (required, optional) keys
ROOM_ERRORS = {"bad": 400, "unknown": 404, "disabled": 409, "radio_off": 409, "busy": 409, "closed": 409, "rules": 409}
SCREEN_ROWS = 40  # rows of a session's terminal the page shows
LEFT_BEHIND = 24 * 3600  # an open session nobody touched for a day is "left behind": the page offers to close it
MAX_CLOSE = 20
REMINDER_REF = re.compile(r"([0-9a-f]{8}):(\d{1,6})")  # the topic's hash and the item's id, as the board's buttons
START_PARAM = re.compile(r"(?:t[0-9a-f]{8}|q[0-9a-f]{16})")  # what panel_pin puts in a topic's button
APP_GET = re.compile(r"/app/([a-z0-9-]{1,24})/(?:media/([^?#]+))?")
APP_API = re.compile(r"/app/([a-z0-9-]{1,24})/api")
AUTH_PARAM = "auth"
MEDIA_DEADLINE = 120  # seconds one media reply may take (a range of at most cockpit_apps.MEDIA_CHUNK bytes)
HOST = re.compile(r"[A-Za-z0-9.-]{1,253}(?::\d{1,5})?")
YOUTUBE_BODY = 512  # {"action": ..., "armed"|"on"|"keys": ...}
ROUTES["/api/youtube"] = YOUTUBE_BODY  # routed before the off check (_youtube_post: stop and disconnect pass)
YOUTUBE_KEYS = {"connect": set(), "arm": set(), "start": {"armed"}, "stop": set(), "disconnect": set(),
                "enable": {"on"}, "projects": {"keys"}}
YOUTUBE_WHILE_OFF = ("stop", "disconnect")  # they only reduce exposure: allowed while mirroring is off
OAUTH_DEADLINE = 20  # seconds the sign-in callback may take (one token exchange with Google)
MAX_OAUTH_PATH = 4096


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
    """The initData carried in `?auth=` (URL-encoded), or "" when it is missing, repeated or malformed."""
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True) if query else []
    except ValueError:
        return ""
    found = [v for k, v in pairs if k == AUTH_PARAM]
    return found[0] if len(found) == 1 else ""


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
                      states: Optional[dict] = None) -> tuple:
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
              "here": (r["chat"], r["thread"]) == here}
             for r in sorted(records, key=lambda r: (-r["created"], r["id"]))]
    found = board.open_reminders()
    reminders = [{"text": i["s"], "due": i.get("d", ""), "topic": by_place.get((chat, thread), ""),
                  "age": _age(now, i["at"]), "url": panel_view.topic_url(chat, thread), "here": (chat, thread) == here,
                  "ref": f"{board.key_hash(board.key_of(chat, thread))}:{i['i']}" if type(i.get("i")) is int else "",
                  "old": panel_view.is_old(now, i["at"])}
                 for chat, thread, i in sorted(found, key=lambda f: -f[2]["at"])[:MAX_REMINDERS]]
    return asked, reminders


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
            out["remind"].append({"text": i["s"], "due": i.get("d", "")})
        elif i["k"] == "check":
            out["check"].append({"text": i["s"], "done": i["x"]})
        else:
            out["link"].append({"text": i["s"], "url": i["u"]})
    return out


_NOT_TEXT = frozenset(("id", "ref", "state", "account", "model"))  # identifiers, never agent prose


def _secret_free(value, key: str = ""):
    """Every agent-written string inside `value` (rows, nested fields such as a card's options or figures)
    credential-filtered in place with the alerts channel's cleaning (creds.redact_channel)."""
    from .creds import redact_channel, redact_pair
    from .work_lines import clean_text
    if isinstance(value, dict):
        if isinstance(value.get("label"), str) and isinstance(value.get("value"), str):
            value["label"], value["value"] = redact_pair(clean_text(value["label"]), clean_text(value["value"]))
        for k, v in value.items():
            value[k] = clean_text(v) if isinstance(v, str) and k in _NOT_TEXT else _secret_free(v, k)
    elif isinstance(value, list):
        value[:] = [_secret_free(v, key) for v in value]
    elif isinstance(value, str):
        return redact_channel(clean_text(value))
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


def _default_model() -> dict:
    """The model new sessions start with, and the choices the panel offers (models.MODELS)."""
    current = models.default_model()
    # The setting may hold an alias ("opus"): its choice is still shown as the selected one.
    return {"id": models.IDS.get(current.lower(), current), "label": models.display_name(current),
            "choices": [{"key": key, "id": ident, "label": name} for key, ident, name in models.MODELS]}


def _compact_at() -> int:
    try:
        from .compact import auto_compact_limit
        return auto_compact_limit()
    except Exception:
        return 0


def build_state(cfg: Config, now: Optional[float] = None, start: str = "", light: bool = False,
                tr: Optional["panel_text.Translator"] = None) -> dict:
    """Decisions, reminders, sessions and quota, from the same readers the bot's own menus use. With the
    start parameter of a session topic's pinned button, also that topic's own view under "topic".
    `light` skips transcript details and the Mac resource sampler.
    With a translator `tr`, agent-written text gets a Hebrew copy beside it (see _hebrew); "pending" counts
    the texts still being translated, so the page asks again shortly."""
    now = time.time() if now is None else now
    topics = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg)
    scope = _topic_of(topics, start)
    entries = collect(topics, now=now)
    asked, reminders = waiting_for_owner(topics, now, (scope["chat"], scope["thread"]) if scope else None,
                                         {e.session: e.state for e in entries})
    here = scope["session"] if scope else None
    shown = [e for e in entries if not e.closed or e.session == here]
    if here and not any(e.session == here for e in shown[:MAX_SESSIONS]):
        # the topic's own session always makes the list (review of #136: cut after
        # MAX_SESSIONS first, the topic's panel said its live session was gone)
        shown = shown[:MAX_SESSIONS - 1] + [e for e in shown if e.session == here][:1]
    usage_snapshot = session_usage.snapshot([(e.session, e.name) for e in entries], now, cfg.usage_prices)
    sessions = []
    paths = {}
    for e in shown[:MAX_SESSIONS]:
        path = None if light else status.transcript_of(e.session)
        paths[e.session] = path
        # every transcript read is cached by the file's size and mtime: a frequent poll reads only what changed
        ctx, model = auto_progress.cached(path, "usage", status.usage_of, (0, ""))
        model = usage.session_model(e.session, path)[0] or models.display_name(model)  # "Opus 5.5", after a /model too
        live = not light and e.state != "ended"
        folder, branch = auto_progress.cached(path, "place", status.workplace, ("", ""))
        sessions.append({"id": e.session[:8], "name": e.name, "state": e.state,
                         "age": None if e.age is None else int(e.age), "context": ctx, "model": model,
                         "account": _session_account(e.session), "bg": _background(path, e.state, now),
                         "now": panel_view.current_step(e.session) if live and e.state == "working" else "",
                         "summary": auto_progress.cached(path, "summary", panel_view.last_summary, "") if live else "",
                         "url": panel_view.topic_url(home_chat(cfg) if e.chat is None else e.chat, e.thread),
                         "here": bool(scope) and e.session == scope["session"],
                         "folder": folder, "branch": "" if branch in ("HEAD", "") else branch,
                         "waiting": [] if light or e.state == "ended" else waiting_words(e.session, now),
                         "compact": _session_compact(e.session),  # its own threshold; None = the global one
                         "left": left_behind(e), "usage": usage_snapshot["sessions"].get(e.session)})
    names = {e.session: e.name for e in entries}
    quota = []
    for name in accounts.names():
        u = accounts._read(accounts.usage_path(name), {})
        if not u:
            continue
        quota.append({"account": name, "label": accounts.label(name),
                      # weekly only, with its reset and the banked resets when known (no 5-hour window)
                      "weekly": u.get("weekly"), "weekly_resets": u.get("weekly_resets") or None,
                      "banked": usage.banked_resets(u),
                      "at": u.get("at")})
    out = {"ok": True, "now": int(now), "decisions": asked, "reminders": reminders, "sessions": sessions,
           "usage_totals": usage_snapshot["totals"], "quota": quota, "compact_at": _compact_at(), "default_model": _default_model(),
           "settings": panel_settings.read()}
    if not light:
        out["mac"] = mac_resources.snapshot()
    out["bank"] = bank.view()
    out["apps"] = cockpit_apps.listing()
    out["tasks"] = open_tasks(topics, now, (scope["chat"], scope["thread"]) if scope else None)
    ended = {e.session for e in entries if e.state == "ended"}
    states = {e.session: e.state for e in entries}
    if not light:
        _auto_cards(cfg, entries, paths, {s["id"]: s["bg"] for s in sessions}, now)
    # each card's way into its session's own topic: the same t.me link the sessions list carries (no chat ids)
    links = {e.session: panel_view.topic_url(home_chat(cfg) if e.chat is None else e.chat, e.thread) for e in entries}
    backgrounds = {e.session: s["bg"] for e, s in zip(shown[:MAX_SESSIONS], sessions)}  # full ids: short ones can collide
    out["cards"] = [dict(cards.view(r, now), topic=names.get(r["session"], ""), topic_url=links.get(r["session"], ""),
                         **_card_activity(r, paths, backgrounds, states),
                         ended=r["session"] in ended and (r["kind"] != "progress" or bool(r.get("auto_closed"))),
                         auto_closed=bool(r.get("auto_closed")),
                         stale=r["kind"] == "progress" and r["state"] == "open" and not r.get("automatic") and
                               states.get(r["session"]) == "working" and cards.is_stale(r, now),
                         old=panel_view.is_old(now, r.get("updated")),
                         here=bool(scope) and r["session"] == scope["session"])
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
    for key in ("decisions", "reminders", "tasks", "sessions", "cards", "topic", "deliveries", "brief", "usage_totals"):
        _secret_free(out.get(key))
    waiting: list = []
    if scope:
        _hebrew(tr, [out["topic"]], ("name",), waiting)
        for kind in ("remind", "check", "link"):
            _hebrew(tr, out["topic"]["board"][kind], ("text",), waiting)
    _hebrew(tr, asked, ("title", "detail", "yes", "no", "topic"), waiting)
    _hebrew(tr, reminders, ("text", "topic"), waiting)
    _hebrew(tr, out["tasks"], ("text", "topic"), waiting)
    _hebrew(tr, sessions, ("name", "summary"), waiting)
    if out["usage_totals"]:
        _hebrew(tr, out["usage_totals"]["top"], ("name",), waiting)
    _hebrew(tr, out["cards"], ("title", "detail", "topic", "live", "task"), waiting)
    _hebrew(tr, out["deliveries"], ("title",), waiting)
    if out.get("brief"):
        _hebrew(tr, out["brief"]["lines"], ("text",), waiting)
        _hebrew(tr, out["brief"]["waiting"], ("text", "note", "detail"), waiting)
        _hebrew(tr, out["brief"]["noticed"], ("text",), waiting)
    # The last pass, after the translations were added: a translation (or a cached one) is returned clean too.
    for key in ("decisions", "reminders", "tasks", "sessions", "cards", "topic", "deliveries", "brief", "usage_totals"):
        _secret_free(out.get(key))
    out["pending"] = len(waiting)
    if getattr(cfg, "radio", None):
        from . import radio, radio_room
        out["radio"] = dict(radio.operation(), on=bool(radio.job_pid()), **radio.minutes(cfg))
        try:
            if not out["radio"]["on"] and not out["radio"]["pending"]:
                radio_room.stop_live("radio_off")  # no station runs it: the room is over, never left "open"
            out["room"] = radio_room.view(cfg, now)  # the brainstorming room (its text is cleaned when stored)
        except Exception:
            pass
    return out


def _card_activity(rec: dict, paths: dict, backgrounds: dict, states: dict) -> dict:
    """Mid-turn activity for an agent card, using the same incremental task cache and job counts as sessions.
    No tool output, descriptions, or new transcript scanners are used."""
    if (rec["kind"] != "progress" or rec["state"] != "open" or rec.get("automatic")
            or cards.finished(rec) or states.get(rec["session"]) == "ended"):
        return {}
    tasks = auto_progress.task_list(paths.get(rec["session"]))
    subject = next((t["content"] for t in tasks if t.get("status") == "in_progress"), "")
    return {"task": cards.subject_line(subject), "bg": backgrounds.get(rec["session"], 0)}


def _auto_cards(cfg: Config, entries: list, paths: dict, bg: dict, now: float) -> None:
    """The bridge's own progress card for every working session whose agent made none (auto_progress.py),
    titled with the topic's name; closed once the session is idle or ended. Never raises."""
    try:
        from .i18n import t
        fallback = t("todo_plan", lang=cfg.target_language)
        rows, used = [], [p for p in paths.values() if p]
        for e in entries:
            row = {"session": e.session, "state": e.state, "bg": bg.get(e.session[:8], 0),
                   "chat": home_chat(cfg) if e.chat is None else e.chat, "thread": e.thread,
                   "title": (e.name or "").strip() or fallback}
            if e.state == "working" or row["bg"]:
                row["path"] = paths[e.session] if e.session in paths else status.transcript_of(e.session)
                used.append(row["path"])
            rows.append(row)
        auto_progress.sync(rows, now)
        auto_progress.forget(p for p in used if p)
    except Exception:
        pass


# --- the page ----------------------------------------------------------------------------

def render_page(lang: str, nonce: str) -> bytes:
    return panel_page.render_page(lang, nonce)


def csp(nonce: str) -> str:
    return panel_page.csp(nonce, FRAME_ANCESTORS)


# --- the server --------------------------------------------------------------------------

MIRROR_OFF = {"ok": False, "error": "mirroring is off"}


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
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
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
        if self._off():
            return
        if not self._body_ok(0):
            return
        if self.path.startswith("/oauth/youtube/"):
            self._youtube_get()
            return
        if self.path.startswith("/app/"):
            self._app_get()
            return
        if urlsplit(self.path).path != "/":  # t.me/<bot>/<app> links add a tgWebAppStartParam query
            self._json(404, {"error": "not found"})
            return
        nonce = secrets.token_urlsafe(16)
        self._send(200, render_page(self.server.lang, nonce), "text/html; charset=utf-8",
                   {"Content-Security-Policy": csp(nonce)})

    def do_POST(self):
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
        decide = self.path != "/api/state"  # every other route acts: it needs the fresher login
        if not self._body_ok(ROUTES[self.path]):
            return
        init = self.headers.get(INIT_HEADER, "")
        if not validate_init_data(init, self.server.bot_token, self.server.owner_id,
                                  max_age=DECIDE_MAX_AGE if decide else MAX_AGE):
            self._json(403, {"error": "forbidden"})
            return
        if self.path == "/api/radio":
            self._radio()
            return
        if self.path in ACTIONS:
            getattr(self, ACTIONS[self.path])()
            return
        if decide:
            self._decide()
            return
        try:
            data = build_state(self.server.cfg, start=start_param_of(init), tr=self.server.translator)
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        data["youtube"] = self.server.youtube_view()
        self._json(200, data)

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
            elif action == "arm":
                # one locked read: the title shown, the channel id and the generation the token is bound to
                conn, v = Y.snapshot(), Y.view(srv.cfg, True, True)
                if not v["enabled"] or not conn.get("connected") or not conn.get("channel_id") \
                        or conn.get("live_enabled") is False or v["live"] or v["pending"]:
                    self._json(409, {"error": "not ready"})
                    return
                token = srv.youtube_flow.arm(conn["generation"], conn["channel_id"])
                self._json(200, {"ok": True, "armed": token, "channel": str(conn.get("channel_title") or "")[:100]})
                return
            elif action == "start":
                confirmed = srv.youtube_flow.fire(req["armed"])
                conn = Y.connection()
                if confirmed is None or confirmed != (conn["generation"], conn.get("channel_id")):
                    self._json(409, {"error": "confirm again"})  # unknown, used, expired, or another channel now
                    return
                if not Y.settings(srv.cfg)["enabled"]:
                    self._json(409, {"error": "disabled"})
                    return
                rid = Y.put_start_request(*confirmed)  # one file per request; the start checks it with Google
                srv.youtube_spawn("start", rid)
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

    def _oauth_page(self, code: int, ok: bool, extra: Optional[dict] = None) -> None:
        nonce = secrets.token_urlsafe(16)
        headers = {"Content-Security-Policy": panel_youtube.PAGE_CSP.format(n=nonce)}
        headers.update(extra or {})
        self._send(code, panel_youtube.page(self.server.lang, ok, nonce), "text/html; charset=utf-8", headers)

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

    def _decide(self) -> None:
        """The owner's tap on one decision: {"id", "choice": "yes"|"no", "note"}. Answered once."""
        try:
            req = json.loads(self._body.decode("utf-8"))
        except ValueError:
            req = None
        if not isinstance(req, dict) or set(req) - {"id", "choice", "note"}:
            self._json(400, {"error": "bad request"})
            return
        ident, choice, note = req.get("id"), req.get("choice"), req.get("note", "")
        if not (isinstance(ident, str) and decisions.ID_RE.fullmatch(ident) and choice in ("yes", "no")
                and isinstance(note, str) and len(note) <= decisions.NOTE_CHARS):
            self._json(400, {"error": "bad request"})
            return
        try:
            # One check for every decision, fresh or old: a page that was open while its session ended, or while the
            # same question was asked again, never answers it (an answer here can approve a sensitive action). Only
            # age alone ("old") stays answerable. Same refusal as a decision that was closed meanwhile.
            if stale_reasons_now(self.server.cfg).get(ident) in ("ended", "replaced"):
                self._json(409, {"error": "closed", "state": "stale", "choice": ""})
                return
            rec = decisions.answer(self.server.cfg, self.server.inbox(), ident, choice, note)
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
                         "delivery": self.server.inbox().delivery_state(rec["session"], rec["delivery_key"])})

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
        refs = req.get("refs") if req is not None and set(req) == {"refs"} else None
        if not isinstance(refs, list) or not refs or len(refs) > MAX_REMINDERS \
                or not all(isinstance(r, str) and REMINDER_REF.fullmatch(r) for r in refs):
            self._json(400, {"error": "bad request"})
            return
        try:
            out = clear_reminders(self.server.cfg, self.server.bot(), refs)
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        self._json(200, dict(out, ok=True))

    def _setting(self) -> None:
        """One bridge setting: {"name": ..., "value": ...} (panel_settings.SETTINGS)."""
        req = self._request()
        if req is None or set(req) != {"name", "value"} or not panel_settings.allowed(req["name"], req["value"]):
            self._json(400, {"error": "bad request"})
            return
        try:
            panel_settings.write(req["name"], req["value"])
        except Exception:
            self._json(500, {"error": "unavailable"})
            return
        self._json(200, {"ok": True, "settings": panel_settings.read()})

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

    def _extend(self, seconds: float) -> None:
        """A longer deadline for this connection, once the request is read and its sender authenticated."""
        self._deadline.cancel()
        self._deadline = threading.Timer(seconds, self._cut)
        self._deadline.daemon = True
        self._deadline.start()

    def _app_get(self) -> None:
        """An app's page or one of its media files. The owner's initData comes in the header, or in the query as
        `?auth=` because an iframe, <video> or <img> cannot set a header; it is checked exactly as for reading
        the state (same signature, owner and MAX_AGE freshness). The check comes before any lookup, so a
        request without it learns nothing about which apps or files exist."""
        parts = urlsplit(self.path)
        m = APP_GET.fullmatch(parts.path)
        if not m:
            self._json(404, {"error": "not found"})
            return
        init = self.headers.get(INIT_HEADER) or query_auth(parts.query)
        if not validate_init_data(init, self.server.bot_token, self.server.owner_id, max_age=MAX_AGE):
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
            self.send_header("Cache-Control", "private, max-age=300")  # the URL carries the owner's login
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
        so it needs the fresher login, like the panel's other actions."""
        if not self._body_ok(cockpit_apps.API_BODY):
            return
        init = self.headers.get(INIT_HEADER, "")
        if not validate_init_data(init, self.server.bot_token, self.server.owner_id, max_age=DECIDE_MAX_AGE):
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
        self._json(code, data)

    def _no(self):
        self._json(405, {"error": "method not allowed"})

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _no

    def log_message(self, format, *args):
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


def clear_reminders(cfg: Config, bot, refs: list) -> dict:
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
            return board.finish(topic, found[0], time.time())
        try:
            board.apply(cfg, bot, chat, thread, change)
            cleared += 1
        except board.BoardError:
            pass
    return {"cleared": cleared}


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
        self.youtube_flow = panel_youtube.Flow()
        # Import now what the state and the actions use later (the routing and settings code is large): a first
        # import inside a request could outlast DEADLINE on a busy machine.
        from . import agents_topic, autopilot, delivery, inbox, progress, voice  # noqa: F401
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    # the YouTube staff radio: what tests replace with fakes (the Keychain, Google, the detached CLI)
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


def serve(cfg: Config, port: int = 8765, host: str = "127.0.0.1") -> int:
    from . import buildstamp
    server = PanelServer((host, port), cfg)
    buildstamp.record_running("panel")  # doctor tells when an install left this copy on older code
    try:
        server.serve_forever()
    finally:
        buildstamp.clear_running("panel")
    return 0
