"""Google Calendar for "My schedule": the config section, the web sign-in (ticket -> state + PKCE + browser cookie ->
callback), the read-only event fetch with its cache and fallback, the event mapping, disconnect and the setup info.
Fakes only: the Keychain and Google are replaced; sample data, no real ids or tokens."""
import base64
import hashlib
import json
import logging
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import GOOD_TOKEN
from ctb import google_calendar as G
from ctb import panel, schedule
from ctb.config import Config, ConfigError, from_dict
from test_panel import OWNER, call, signed

BASE = "https://cockpit.example.test"
CLIENT_ID_ITEM = ("sample.google-client", "client-id")
CLIENT_SECRET_ITEM = ("sample.google-client", "client-secret")
CLIENT_ID = "cid-sample.apps.example"
CLIENT_SECRET = "sample-client-secret-value"
REFRESH = "1//sample-refresh-token-value"
ACCESS = "ya29.fake-access-token-value"
SECTION = {"client_id_keychain": list(CLIENT_ID_ITEM), "client_secret_keychain": list(CLIENT_SECRET_ITEM),
           "calendars": ["primary"]}
SECRETS = (CLIENT_ID, CLIENT_SECRET, REFRESH, ACCESS)


class Store:
    def __init__(self, **extra):
        self.items = {CLIENT_ID_ITEM: CLIENT_ID, CLIENT_SECRET_ITEM: CLIENT_SECRET}
        self.items.update(extra)
        self.reads = []

    def get(self, item):
        self.reads.append(item)
        return self.items.get(tuple(item))

    def has(self, item):
        return tuple(item) in self.items

    def set(self, item, value):
        self.items[tuple(item)] = value
        return True

    def delete(self, item):
        self.items.pop(tuple(item), None)


class Google:
    """Answers the token, revoke and events endpoints; records every call."""

    def __init__(self, items=None, events_status=200, scope=G.SCOPE, refresh=REFRESH):
        self.calls, self.items, self.events_status, self.scope, self.refresh = [], items or [], events_status, scope, refresh
        self.on_exchange = None

    def __call__(self, method, url, form=None, token="", timeout=G.TIMEOUT):
        self.calls.append((method, url, dict(form or {}), token))
        if url == G.TOKEN_URL:
            if (form or {}).get("grant_type") == "authorization_code":
                if self.on_exchange:
                    self.on_exchange()
                out = {"access_token": ACCESS, "scope": self.scope, "expires_in": 3599}
                if self.refresh:
                    out["refresh_token"] = self.refresh
                return 200, out
            return 200, {"access_token": ACCESS, "expires_in": 3599}
        if url == G.REVOKE_URL:
            return 200, {}
        if url.startswith("https://www.googleapis.com/calendar/v3/calendars/"):
            if self.events_status != 200:
                return self.events_status, {"error": {"code": self.events_status}}
            return 200, {"items": self.items}
        return 404, {}

    def kinds(self):
        out = []
        for method, url, form, _ in self.calls:
            if url == G.TOKEN_URL:
                out.append(form.get("grant_type"))
            elif url == G.REVOKE_URL:
                out.append("revoke")
            else:
                out.append("events")
        return out


@pytest.fixture
def fakes(monkeypatch):
    store, google = Store(), Google()
    monkeypatch.setattr(G, "STORE", store)
    monkeypatch.setattr(G, "REQUEST", google)
    G.reset()
    yield store, google
    G.reset()


def make_cfg(**kw):
    base = dict(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", panel_base_url=BASE,
                google_calendar=dict(SECTION))
    base.update(kw)
    return Config(**base)


@pytest.fixture
def srv(fakes):
    s = panel.PanelServer(("127.0.0.1", 0), make_cfg())
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()
    s.server_close()


def sched(s, body, init=None):
    code, hdrs, data = call(s, path="/api/schedule", init=signed() if init is None else init,
                            body=json.dumps(body).encode())
    return code, json.loads(data or b"{}"), data


def view(s):
    code, _, data = call(s, path="/api/schedule-view", init=signed(), body=b"{}")
    return code, json.loads(data or b"{}"), data


def get(s, path, headers=None):
    return call(s, method="GET", path=path, headers=headers)


def signin(s):
    """connect -> start: (state, cookie, the Google URL's query)."""
    code, j, _ = sched(s, {"action": "google-connect"})
    assert code == 200 and j["url"].startswith(BASE + G.START_PATH + "?t=")
    code, hdrs, _ = get(s, j["url"][len(BASE):], headers={"Host": "evil.example"})
    assert code == 302
    loc = urllib.parse.urlsplit(hdrs["Location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == G.AUTH_URL
    q = dict(urllib.parse.parse_qsl(loc.query))
    cookie = hdrs["Set-Cookie"].split(";")[0].split("=", 1)[1]
    return q["state"], cookie, q, hdrs


def callback(s, state, cookie, code="sample-auth-code"):
    query = urllib.parse.urlencode({"state": state, "code": code, "scope": G.SCOPE})
    return get(s, G.CALLBACK_PATH + "?" + query, headers={"Cookie": f"{G.COOKIE}={cookie}"})


def no_secret_in(*blobs):
    for blob in blobs:
        text = blob.decode("utf-8", "replace") if isinstance(blob, bytes) else str(blob)
        for secret in SECRETS:
            assert secret not in text


# --- the config section ---------------------------------------------------------------------------------------------

def test_the_section_is_empty_by_default_and_the_button_stays_hidden():
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)
    assert cfg.google_calendar == {} and G.settings(cfg) is None
    assert G.status(cfg) == {"configured": False, "connected": False}
    assert schedule.view(cfg, source_list=[])["google"] == {"configured": False, "connected": False}


@pytest.mark.parametrize("value", [
    {},
    SECTION,
    dict(SECTION, calendars=["primary", "team.sample@group.example.com"]),
    dict(SECTION, token_keychain=["sample.service", "refresh-token"]),
    {"client_id_keychain": ["Sample Client", "id"], "client_secret_keychain": ["Sample Client", "secret"]},
])
def test_good_sections(value):
    assert G.valid_section(value) and from_dict({"google_calendar": value}).google_calendar == value


@pytest.mark.parametrize("value", [
    [], "primary", {"client_id_keychain": ["a", "b"]},
    dict(SECTION, client_id_keychain=["only-one"]),
    dict(SECTION, client_secret_keychain=['quo"te', "b"]),
    dict(SECTION, client_secret_keychain=["back\\slash", "b"]),
    dict(SECTION, calendars=[]),
    dict(SECTION, calendars=["a"] * 6),
    dict(SECTION, calendars=["has space"]),
    dict(SECTION, token_keychain=["x"]),
    dict(SECTION, extra=1),
])
def test_bad_sections_are_refused(value):
    assert not G.valid_section(value)
    with pytest.raises(ConfigError):
        from_dict({"google_calendar": value})


def test_without_panel_base_url_the_button_is_not_offered(fakes):
    assert G.status(make_cfg(panel_base_url=""))["configured"] is False


# --- the Keychain helper --------------------------------------------------------------------------------------------

class Runner:
    def __init__(self, code=0, out=""):
        self.calls, self.code, self.out = [], code, out

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))

        class R:
            returncode, stdout = self.code, self.out
        return R()


def test_a_secret_reaches_security_on_stdin_only():
    run = Runner()
    assert G.Keychain(run).set(G.TOKEN_ITEM, REFRESH) is True
    argv, kw = run.calls[0]
    assert argv == ["security", "-i"] and REFRESH not in " ".join(argv)
    assert REFRESH not in kw["input"] and ("-X " + REFRESH.encode().hex()) in kw["input"]
    assert kw["input"].startswith("add-generic-password -U ")
    assert all(REFRESH not in " ".join(a) for a, _ in run.calls)


def test_a_value_that_could_break_the_command_line_is_hex_encoded():
    run = Runner()
    value = 'bad" -s "other'
    assert G.Keychain(run).set(G.TOKEN_ITEM, value) is True
    assert value not in run.calls[0][1]["input"] and value.encode().hex() in run.calls[0][1]["input"]


def test_reading_asks_for_the_password_only_and_never_prompts():
    run = Runner(out=CLIENT_ID + "\n")
    assert G.Keychain(run).get(CLIENT_ID_ITEM) == CLIENT_ID
    argv, kw = run.calls[0]
    assert argv[-1] == "-w" and argv[:2] == ["security", "find-generic-password"] and "input" not in kw


# --- the sign-in flow (unit) ----------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_a_ticket_works_once_and_expires():
    clock = Clock()
    flow = G.Flow(clock)
    t = flow.ticket(OWNER)
    assert flow.take_ticket(t) == (OWNER, 0) and flow.take_ticket(t) is None
    t2 = flow.ticket(OWNER)
    clock.now += G.TICKET_TTL + 1
    assert flow.take_ticket(t2) is None


def test_a_state_works_once_only_from_the_same_browser_and_expires():
    clock = Clock()
    flow = G.Flow(clock)
    state, challenge, cookie = flow.begin(OWNER, 0)
    assert flow.finish(state, "x" * 43) is None             # another browser: and the state is used up
    assert flow.finish(state, cookie) is None
    state, challenge, cookie = flow.begin(OWNER, 0)
    assert flow.finish("A" * 43, cookie) is None            # a state this process never issued
    verifier, owner, gen = flow.finish(state, cookie)
    assert owner == OWNER and gen == 0
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=") == challenge
    assert flow.finish(state, cookie) is None               # replay
    state, _, cookie = flow.begin(OWNER, 0)
    clock.now += G.STATE_TTL + 1
    assert flow.finish(state, cookie) is None               # expired


def test_a_disconnect_voids_every_pending_ticket():
    flow = G.Flow()
    t = flow.ticket(OWNER)
    flow.clear()
    assert flow.take_ticket(t) is None and not flow.current(0) and flow.current(1)


# --- the sign-in through the panel ----------------------------------------------------------------------------------

def test_the_whole_sign_in_stores_the_refresh_token_and_shows_a_hebrew_page(srv, fakes, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    store, google = fakes
    code, j, raw = view(srv)
    assert code == 200 and j["view"]["google"] == {"configured": True, "connected": False}
    state, cookie, q, hdrs = signin(srv)
    assert q["client_id"] == CLIENT_ID and q["redirect_uri"] == BASE + "/oauth/google/callback"
    assert q["scope"] == "https://www.googleapis.com/auth/calendar.readonly"
    assert q["access_type"] == "offline" and q["prompt"] == "consent" and q["response_type"] == "code"
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert "HttpOnly" in hdrs["Set-Cookie"] and "Secure" in hdrs["Set-Cookie"] and "Path=/oauth/google" in hdrs["Set-Cookie"]
    code, hdrs2, page = callback(srv, state, cookie)
    assert code == 200 and "החיבור הושלם" in page.decode() and 'dir="rtl"' in page.decode()
    assert "Max-Age=0" in hdrs2["Set-Cookie"] and hdrs2["Cache-Control"] == "no-store"
    assert store.items[G.TOKEN_ITEM] == REFRESH
    exchange = google.calls[0][2]
    assert exchange["grant_type"] == "authorization_code" and exchange["redirect_uri"] == BASE + G.CALLBACK_PATH
    verifier = exchange["code_verifier"]
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=") == q["code_challenge"]
    code, j, raw2 = view(srv)
    assert j["view"]["google"] == {"configured": True, "connected": True}
    out = capsys.readouterr()
    no_secret_in(raw, raw2, page, json.dumps(dict(hdrs2)), out.out, out.err, caplog.text)


def test_a_replayed_callback_is_refused_and_stores_nothing_new(srv, fakes):
    store, google = fakes
    state, cookie, _, _ = signin(srv)
    assert callback(srv, state, cookie)[0] == 200
    store.items.pop(G.TOKEN_ITEM)
    n = len(google.calls)
    code, _, page = callback(srv, state, cookie)
    assert code == 403 and "החיבור לא הושלם" in page.decode()
    assert G.TOKEN_ITEM not in store.items and len(google.calls) == n


def test_a_forged_state_or_another_browser_is_refused_before_google_is_asked(srv, fakes):
    store, google = fakes
    state, cookie, _, _ = signin(srv)
    assert callback(srv, "F" * 43, cookie)[0] == 403
    assert callback(srv, state, "C" * 43)[0] == 403        # another browser: and the state is now used up
    assert callback(srv, state, cookie)[0] == 403
    code, _, _ = get(srv, G.CALLBACK_PATH + "?state=" + "S" * 43 + "&code=x")  # no cookie at all
    assert code == 403
    assert google.calls == [] and G.TOKEN_ITEM not in store.items


def test_an_expired_state_is_refused(srv, fakes):
    clock = Clock()
    clock.now = time.time()
    srv.google_flow.clock = clock
    state, cookie, _, _ = signin(srv)
    clock.now += G.STATE_TTL + 1
    assert callback(srv, state, cookie)[0] == 403 and G.TOKEN_ITEM not in fakes[0].items


def test_a_ticket_is_used_once(srv, fakes):
    code, j, _ = sched(srv, {"action": "google-connect"})
    path = j["url"][len(BASE):]
    assert get(srv, path)[0] == 302 and get(srv, path)[0] == 403
    assert get(srv, G.START_PATH + "?t=" + "T" * 43)[0] == 403


def test_a_ticket_made_for_someone_else_is_refused(srv, fakes):
    t = srv.google_flow.ticket(OWNER + 1)
    assert get(srv, G.START_PATH + "?t=" + t)[0] == 403


@pytest.mark.parametrize("init", [
    "", signed(user_id=OWNER + 1), signed(tamper=True), signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 60)])
def test_only_the_owners_fresh_login_gets_a_sign_in_link_or_disconnects(srv, fakes, init):
    for action in ("google-connect", "google-disconnect"):
        code, j, raw = sched(srv, {"action": action}, init=init)
        assert code == 403 and "url" not in j
    assert srv.google_flow.tickets == {}


def test_the_actions_refuse_extra_keys(srv, fakes):
    assert sched(srv, {"action": "google-connect", "url": "https://evil.example"})[0] == 400


def test_without_the_section_the_actions_and_routes_answer_setup(fakes):
    s = panel.PanelServer(("127.0.0.1", 0), make_cfg(google_calendar={}))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        assert sched(s, {"action": "google-connect"})[:2] == (409, {"error": "setup"})
        assert get(s, G.START_PATH + "?t=" + "T" * 43)[0] == 409
        assert view(s)[1]["view"]["google"] == {"configured": False, "connected": False}
    finally:
        s.shutdown()
        s.server_close()


def test_a_missing_client_id_in_the_keychain_stops_at_start(srv, fakes):
    fakes[0].items.pop(CLIENT_ID_ITEM)
    code, j, _ = sched(srv, {"action": "google-connect"})
    assert get(srv, j["url"][len(BASE):])[0] == 409


@pytest.mark.parametrize("change,status", [({"scope": "openid"}, 502), ({"refresh": ""}, 502)])
def test_a_grant_without_the_calendar_scope_or_a_refresh_token_stores_nothing(srv, fakes, change, status):
    store, google = fakes
    for k, v in change.items():
        setattr(google, k, v)
    state, cookie, _, _ = signin(srv)
    assert callback(srv, state, cookie)[0] == status and G.TOKEN_ITEM not in store.items


def test_google_saying_no_is_not_exchanged(srv, fakes):
    state, cookie, _, _ = signin(srv)
    q = urllib.parse.urlencode({"state": state, "error": "access_denied"})
    assert get(srv, G.CALLBACK_PATH + "?" + q, headers={"Cookie": f"{G.COOKIE}={cookie}"})[0] == 400
    assert fakes[1].calls == []


def test_a_disconnect_during_the_code_exchange_wins(srv, fakes):
    store, google = fakes
    state, cookie, _, _ = signin(srv)
    google.on_exchange = srv.google_flow.clear   # the owner tapped Disconnect while Google answered
    assert callback(srv, state, cookie)[0] == 409
    assert G.TOKEN_ITEM not in store.items and google.kinds() == ["authorization_code", "revoke"]


def test_disconnect_deletes_the_token_revokes_it_and_shows_connect_again(srv, fakes):
    store, google = fakes
    store.items[G.TOKEN_ITEM] = REFRESH
    t = srv.google_flow.ticket(OWNER)
    code, j, raw = sched(srv, {"action": "google-disconnect"})
    assert code == 200 and j["view"]["google"] == {"configured": True, "connected": False}
    assert G.TOKEN_ITEM not in store.items
    assert google.calls[-1][:3] == ("POST", G.REVOKE_URL, {"token": REFRESH})
    assert srv.google_flow.take_ticket(t) is None   # pending sign-ins are void
    no_secret_in(raw)


def test_disconnect_still_deletes_when_the_revoke_fails(srv, fakes, monkeypatch):
    store, _ = fakes
    store.items[G.TOKEN_ITEM] = REFRESH
    monkeypatch.setattr(G, "REQUEST", lambda *a, **kw: (0, {}))
    assert sched(srv, {"action": "google-disconnect"})[0] == 200 and G.TOKEN_ITEM not in store.items


def test_the_routes_follow_the_mirroring_switch(srv, fakes, monkeypatch):
    from ctb import policy
    monkeypatch.setattr(policy, "on", lambda: False)
    assert get(srv, G.START_PATH + "?t=" + "T" * 43)[0] == 503
    assert sched(srv, {"action": "google-connect"})[0] == 503


# --- reading the calendar -------------------------------------------------------------------------------------------

def today_local(cfg):
    return schedule._local(cfg, None).date()


def test_connected_events_join_the_card_and_no_token_leaves(srv, fakes, caplog):
    caplog.set_level(logging.DEBUG)
    store, google = fakes
    store.items[G.TOKEN_ITEM] = REFRESH
    day = today_local(srv.cfg)
    google.items = [
        {"status": "confirmed", "summary": "Team sync", "start": {"dateTime": f"{day.isoformat()}T09:30:00+02:00"},
         "end": {"dateTime": f"{day.isoformat()}T10:00:00+02:00"}},
        {"status": "confirmed", "summary": "יום משפחה", "start": {"date": (day + timedelta(days=1)).isoformat()},
         "end": {"date": (day + timedelta(days=2)).isoformat()}},
        {"status": "cancelled", "summary": "Cancelled one", "start": {"dateTime": f"{day.isoformat()}T11:00:00+02:00"}},
    ]
    code, j, raw = view(srv)
    v = j["view"]
    assert code == 200 and v["calendar"] == {"configured": True, "ok": True}
    assert [(r["title"], r["time"], r["kind"]) for r in v["days"]["today"]] == [("Team sync", "09:30", "cal")]
    assert [(r["title"], r["allday"]) for r in v["days"]["tomorrow"]] == [("יום משפחה", True)]
    refresh, events = google.calls[0], google.calls[1]
    assert refresh[2]["grant_type"] == "refresh_token" and refresh[2]["refresh_token"] == REFRESH
    url = urllib.parse.urlsplit(events[1])
    assert url.path == "/calendar/v3/calendars/primary/events" and events[0] == "GET" and events[3] == ACCESS
    params = dict(urllib.parse.parse_qsl(url.query))
    assert params["singleEvents"] == "true" and params["orderBy"] == "startTime" and params["timeZone"] == "Asia/Jerusalem"
    assert params["timeMin"] == f"{day.isoformat()}T00:00:00+02:00"
    assert params["timeMax"] == f"{(day + timedelta(days=2)).isoformat()}T00:00:00+02:00"
    assert all(method in ("GET", "POST") and url != G.REVOKE_URL for method, url, _, _ in google.calls)
    no_secret_in(raw, caplog.text)


def test_events_are_reused_for_five_minutes(srv, fakes):
    store, google = fakes
    store.items[G.TOKEN_ITEM] = REFRESH
    view(srv)
    n = len(google.calls)
    view(srv)
    assert len(google.calls) == n


def test_the_source_cache_ages_out_and_the_access_token_is_reused(fakes):
    store, google = fakes
    store.items[G.TOKEN_ITEM] = REFRESH
    cfg = make_cfg()
    src = G.source(cfg)
    first = date(2026, 11, 15)
    src.events(cfg, first, first + timedelta(days=1), now=1000.0)
    src.events(cfg, first, first + timedelta(days=1), now=1000.0 + G.EVENTS_TTL - 1)
    assert google.kinds() == ["refresh_token", "events"]
    src.events(cfg, first, first + timedelta(days=1), now=1000.0 + G.EVENTS_TTL + 1)
    assert google.kinds() == ["refresh_token", "events", "events"]


@pytest.mark.parametrize("status", [500, 401, 403])
def test_a_fetch_error_keeps_the_own_items_with_a_quiet_note_and_is_not_retried_at_once(srv, fakes, status):
    store, google = fakes
    store.items[G.TOKEN_ITEM] = REFRESH
    google.events_status = status
    schedule.add(srv.cfg, "לקנות חלב היום")
    code, j, raw = view(srv)
    v = j["view"]
    assert code == 200 and v["calendar"] == {"configured": True, "ok": False}
    assert [r["title"] for r in v["days"]["today"]] == ["לקנות חלב"]
    n = len(google.calls)
    assert view(srv)[1]["view"]["calendar"]["ok"] is False and len(google.calls) == n
    no_secret_in(raw)


def test_a_refused_refresh_token_falls_back_too(srv, fakes, monkeypatch):
    store, _ = fakes
    store.items[G.TOKEN_ITEM] = REFRESH
    monkeypatch.setattr(G, "REQUEST", lambda *a, **kw: (400, {"error": "invalid_grant"}))
    v = view(srv)[1]["view"]
    assert v["calendar"] == {"configured": True, "ok": False} and v["google"]["connected"] is True


def test_not_connected_means_no_source_and_no_google_call(srv, fakes):
    assert schedule.sources(srv.cfg) == []
    assert view(srv)[1]["view"]["calendar"] == {"configured": False, "ok": True} and fakes[1].calls == []


def test_several_calendars_are_each_asked_and_ids_are_quoted(fakes):
    store, google = fakes
    store.items[G.TOKEN_ITEM] = REFRESH
    cfg = make_cfg(google_calendar=dict(SECTION, calendars=["primary", "team#x@group.example.com"]))
    G.source(cfg).events(cfg, date(2026, 11, 15), date(2026, 11, 16), now=5.0)
    paths = [urllib.parse.urlsplit(u).path for m, u, _, _ in google.calls if m == "GET"]
    assert paths == ["/calendar/v3/calendars/primary/events",
                     "/calendar/v3/calendars/team%23x%40group.example.com/events"]


# --- the event mapping ----------------------------------------------------------------------------------------------

FIRST, LAST = date(2026, 11, 15), date(2026, 11, 16)


def mapped(items, **kw):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, **kw)
    return [(e.title, e.day, e.time) for e in G.map_events(items, cfg, FIRST, LAST)]


def test_all_day_events_cover_each_day_and_the_end_date_is_exclusive():
    assert mapped([{"summary": "One day", "start": {"date": "2026-11-15"}, "end": {"date": "2026-11-16"}}]) == [
        ("One day", "2026-11-15", None)]
    assert mapped([{"summary": "Trip", "start": {"date": "2026-11-14"}, "end": {"date": "2026-11-20"}}]) == [
        ("Trip", "2026-11-15", None), ("Trip", "2026-11-16", None)]
    assert mapped([{"summary": "No end", "start": {"date": "2026-11-16"}}]) == [("No end", "2026-11-16", None)]
    assert mapped([{"summary": "Yesterday", "start": {"date": "2026-11-14"}, "end": {"date": "2026-11-15"}}]) == []


def test_timed_events_are_shown_in_the_owners_zone():
    items = [
        {"summary": "UTC", "start": {"dateTime": "2026-11-15T08:00:00Z"}},
        {"summary": "New York", "start": {"dateTime": "2026-11-15T09:00:00-05:00"}},
        {"summary": "Floating with zone", "start": {"dateTime": "2026-11-16T07:15:00", "timeZone": "Europe/London"}},
        {"summary": "Late UTC is tomorrow here", "start": {"dateTime": "2026-11-15T23:30:00Z"}},
        {"summary": "Out of range", "start": {"dateTime": "2026-11-17T10:00:00+02:00"}},
    ]
    assert mapped(items) == [("UTC", "2026-11-15", "10:00"), ("New York", "2026-11-15", "16:00"),
                             ("Floating with zone", "2026-11-16", "09:15"),
                             ("Late UTC is tomorrow here", "2026-11-16", "01:30")]
    assert mapped(items[:1], timezone="America/New_York") == [("UTC", "2026-11-15", "03:00")]


@pytest.mark.parametrize("start,end", [
    ("2026-10-25T01:30:00+03:00", "2026-10-25T01:15:00+02:00"),
    ("2026-03-27T01:30:00+02:00", "2026-03-27T03:15:00+03:00"),
])
def test_google_events_crossing_dst_keep_their_instants_and_local_display(start, end):
    day = datetime.fromisoformat(start).date()
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, timezone="Asia/Jerusalem")
    found = G.map_events([{"summary": "DST", "start": {"dateTime": start}, "end": {"dateTime": end}}],
                         cfg, day, day)
    assert len(found) == 1
    event = found[0]
    assert (event.day, event.time) == (day.isoformat(), "01:30")
    assert event.start.isoformat() == start
    assert event.end.isoformat() == end
    assert event.end.timestamp() - event.start.timestamp() == 45 * 60


@pytest.mark.parametrize("start,end", [
    ("2026-10-25T01:30:00+03:00", "2026-10-25T01:30:00+02:00"),
    ("2026-03-27T01:30:00+02:00", "2026-03-27T03:30:00+03:00"),
])
def test_google_default_duration_is_one_real_hour_across_dst(start, end):
    day = datetime.fromisoformat(start).date()
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, timezone="Asia/Jerusalem")
    found = G.map_events([{"start": {"dateTime": start}}], cfg, day, day)
    assert len(found) == 1
    assert found[0].end.isoformat() == end
    assert found[0].end.timestamp() - found[0].start.timestamp() == 60 * 60


def test_google_rejects_an_end_before_start_despite_increasing_wall_time():
    day = date(2026, 10, 25)
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, timezone="Asia/Jerusalem")
    assert G.map_events([{"start": {"dateTime": "2026-10-25T01:15:00+02:00"},
                          "end": {"dateTime": "2026-10-25T01:30:00+03:00"}}], cfg, day, day) == []


def test_cancelled_broken_and_untitled_events():
    items = [
        {"status": "cancelled", "summary": "Gone", "start": {"dateTime": "2026-11-15T10:00:00Z"}},
        {"summary": "Broken", "start": {"dateTime": "not a time"}},
        {"summary": "No start"},
        "not an object",
        {"start": {"dateTime": "2026-11-15T10:00:00Z"}},
        {"summary": "Tab\tand\nline\x07", "start": {"dateTime": "2026-11-15T11:00:00Z"}},
        {"summary": "x" * 500, "start": {"date": "2026-11-15"}, "end": {"date": "2026-11-16"}},
    ]
    got = mapped(items)
    assert got[:2] == [("—", "2026-11-15", "12:00"), ("Tab and line", "2026-11-15", "13:00")]
    assert got[2][0] == "x" * schedule.MAX_TITLE and len(got) == 3
    assert mapped("garbage") == []


# --- setup info -----------------------------------------------------------------------------------------------------

def test_setup_info_prints_the_redirect_uri_and_no_secret(fakes):
    lines = G.setup_info(make_cfg())
    assert lines == ["Authorized redirect URI: https://cockpit.example.test/oauth/google/callback",
                     "Scope: " + G.SCOPE, "Connected: no"]
    assert fakes[0].reads == []   # existence checks only
    fakes[0].items.pop(CLIENT_SECRET_ITEM)
    assert "Missing in the Keychain: client secret" in G.setup_info(make_cfg())
    no_secret_in("\n".join(lines))


def test_setup_info_gives_the_exact_config_line_to_add(fakes):
    lines = G.setup_info(Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, panel_base_url=BASE),
                         "sample.google-oauth")
    add = [l for l in lines if l.startswith("Add to the config: ")]
    assert len(add) == 1 and len(lines) == 3
    entry = json.loads("{" + add[0][len("Add to the config: "):] + "}")["google_calendar"]
    assert entry == {"client_id_keychain": ["sample.google-oauth", "client-id"],
                     "client_secret_keychain": ["sample.google-oauth", "client-secret"], "calendars": ["primary"]}
    assert G.valid_section(entry) and from_dict({"google_calendar": entry}).google_calendar == entry
    assert '"<service>", "client-id"' in "\n".join(G.setup_info(Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)))
    with pytest.raises(G.CalendarError):
        G.setup_info(make_cfg(), 'bad"name')


def test_the_cli_prints_the_setup_info(fakes, monkeypatch, capsys):
    from ctb import cli, config as C
    monkeypatch.setattr(C, "load", lambda *a, **kw: make_cfg(google_calendar={}))
    assert cli.main(["schedule", "google-setup-info", "--keychain-service", "sample.google-oauth"]) == 0
    out = capsys.readouterr().out
    assert "/oauth/google/callback" in out and '"sample.google-oauth", "client-secret"' in out
    assert cli.main(["schedule", "google-setup-info", "--keychain-service", 'x"y']) == 2


# --- a slow answer for the account that was just disconnected (connection generations) ----------------------------

def switch_account(store):
    """What the owner's taps do while a request is in flight: Disconnect account A, then sign in with account B."""
    G.disconnect(make_cfg(), G.Flow())
    store.items[G.TOKEN_ITEM] = "1//sample-refresh-token-account-b"
    G.reset()  # what complete() does after storing B's token


def test_events_for_a_disconnected_account_are_never_cached_or_shown(fakes):
    store, google = fakes
    store.items[G.TOKEN_ITEM] = REFRESH
    cfg = make_cfg()
    a = [{"summary": "Account A private", "start": {"date": "2026-11-15"}, "end": {"date": "2026-11-16"}}]
    b = [{"summary": "Account B", "start": {"date": "2026-11-15"}, "end": {"date": "2026-11-16"}}]
    google.items = a
    real = google.__call__
    calls = {"n": 0}

    def slow(method, url, form=None, token="", timeout=G.TIMEOUT):
        out = real(method, url, form=form, token=token, timeout=timeout)
        if method == "GET" and calls["n"] == 0:
            calls["n"] += 1
            switch_account(store)   # A's answer lands after the switch
            google.items = b
        return out
    G.REQUEST = slow
    first = date(2026, 11, 15)
    with pytest.raises(G.CalendarError):
        G.source(cfg).events(cfg, first, first + timedelta(days=1), now=100.0)
    assert G._EVENTS == {} and G._ACCESS == {}
    got = G.source(cfg).events(cfg, first, first + timedelta(days=1), now=101.0)
    assert [e.title for e in got] == ["Account B"]
    refreshes = [c[2]["refresh_token"] for c in google.calls if c[2].get("grant_type") == "refresh_token"]
    assert refreshes == [REFRESH, "1//sample-refresh-token-account-b"]


def test_a_token_refresh_for_a_disconnected_account_is_never_kept(fakes):
    store, google = fakes
    store.items[G.TOKEN_ITEM] = REFRESH
    cfg = make_cfg()
    real = google.__call__
    switched = {"done": False}

    def slow(method, url, form=None, token="", timeout=G.TIMEOUT):
        out = real(method, url, form=form, token=token, timeout=timeout)
        if (form or {}).get("grant_type") == "refresh_token" and not switched["done"]:
            switched["done"] = True
            switch_account(store)   # A's access token lands after the switch
        return out
    G.REQUEST = slow
    first = date(2026, 11, 15)
    with pytest.raises(G.CalendarError):
        G.source(cfg).events(cfg, first, first + timedelta(days=1), now=100.0)
    assert G._ACCESS == {} and not any(c[3] == ACCESS for c in google.calls if c[0] == "GET")  # A's token never used
    G.source(cfg).events(cfg, first, first + timedelta(days=1), now=101.0)
    used = [c[2].get("refresh_token") for c in google.calls if c[2].get("grant_type") == "refresh_token"]
    assert used == [REFRESH, "1//sample-refresh-token-account-b"]


def test_the_panel_never_shows_a_disconnected_accounts_late_answer(srv, fakes):
    store, google = fakes
    store.items[G.TOKEN_ITEM] = REFRESH
    day = today_local(srv.cfg)
    google.items = [{"summary": "Account A private", "start": {"date": day.isoformat()},
                     "end": {"date": (day + timedelta(days=1)).isoformat()}}]
    real = google.__call__
    hit = {"n": 0}

    def slow(method, url, form=None, token="", timeout=G.TIMEOUT):
        out = real(method, url, form=form, token=token, timeout=timeout)
        if method == "GET" and hit["n"] == 0:
            hit["n"] += 1
            switch_account(store)
            google.items = []
        return out
    G.REQUEST = slow
    code, j, raw = view(srv)
    assert code == 200 and b"Account A private" not in raw and j["view"]["calendar"]["ok"] is False
    code, j, raw = view(srv)
    assert b"Account A private" not in raw and j["view"]["calendar"]["ok"] is True


def test_every_test_runs_with_a_refusing_keychain_and_no_google():
    """tests/conftest.py: nothing here can reach this Mac's Keychain or Google."""
    assert G.STORE.get(G.TOKEN_ITEM) is None and G.STORE.set(G.TOKEN_ITEM, REFRESH) is False
    with pytest.raises(AssertionError):
        G.REQUEST("GET", G.TOKEN_URL)
