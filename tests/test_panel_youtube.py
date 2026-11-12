"""The Cockpit's YouTube staff radio: the web sign-in (ticket -> state + PKCE + browser cookie -> callback), the
feature switch and project picker, the two-tap Start, Stop and Disconnect, and the bridge-off rule.
Fakes only: Google, the Keychain and the detached CLI are replaced; sample data, no real ids."""
import json
import threading
import time
import urllib.parse

import pytest

from conftest import GOOD_TOKEN
from ctb import panel, panel_page, panel_youtube as PY, radio_youtube as Y
from ctb.config import Config
from test_panel import OWNER, call, signed

BASE = "https://cockpit.example.test"
REFRESH_TOKEN = "1//sample-refresh-token-value"


class Store:
    def __init__(self, **items):
        self.items = dict(items)

    def get(self, name):
        return self.items.get(name)

    def has(self, name):
        return name in self.items

    def set(self, name, value):
        self.items[name] = value

    def delete(self, name):
        self.items.pop(name, None)


class Google:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, body=None, form=None, token="", timeout=20.0):
        self.calls.append((method, url, dict(form or {}), body))
        if url == Y.TOKEN_URL:
            if (form or {}).get("grant_type") == "authorization_code":
                return 200, {"access_token": "at-1", "refresh_token": REFRESH_TOKEN, "scope": Y.SCOPE}
            return 200, {"access_token": "at-2"}
        if "/channels" in url:
            return 200, {"items": [{"id": "UCsample", "snippet": {"title": "Sample channel"}}]}
        if "/liveBroadcasts" in url and method == "GET":
            return 200, {"items": []}
        return 404, {}


@pytest.fixture(autouse=True)
def no_launchd(monkeypatch):
    """Never ask this Mac's launchd: a staff radio job really running here must not change a test's answer."""
    monkeypatch.setattr(Y, "_job_on", lambda: False)
    monkeypatch.setattr(Y, "_launchctl", lambda *a: pytest.fail("launchctl must not run in tests"))


def web_store():
    return Store(**{Y.WEB_CLIENT_ID: "cid.sample.apps.example", Y.WEB_CLIENT_SECRET: "sample-web-secret"})


def make(cfg=None, store=None, base=BASE):
    cfg = cfg or Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", panel_base_url=base,
                        radio={"project_names": {"alpha": "אלפא", "beta": "Beta app"}})
    srv = panel.PanelServer(("127.0.0.1", 0), cfg)
    srv.youtube_store_obj = web_store() if store is None else store
    srv.youtube_request = Google()
    srv.spawned, srv.requests = [], []
    srv.youtube_spawn = lambda action, request=None: (srv.spawned.append(action), srv.requests.append(request))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def srv():
    s = make()
    yield s
    s.shutdown()
    s.server_close()


def yt(s, body, init=None, headers=None):
    code, hdrs, data = call(s, path="/api/youtube", init=signed() if init is None else init,
                            body=json.dumps(body).encode(), headers=headers)
    return code, hdrs, json.loads(data or b"{}")


def get(s, path, headers=None):
    return call(s, method="GET", path=path, headers=headers)


def _ticket_path(url):
    assert url.startswith(BASE + PY.START_PATH + "?t=")
    return url[len(BASE):]


def _signin(s):
    """connect -> start: (state, cookie, location query)."""
    code, _, j = yt(s, {"action": "connect"})
    assert code == 200
    code, hdrs, _ = get(s, _ticket_path(j["url"]), headers={"Host": "evil.example"})
    assert code == 302
    loc = urllib.parse.urlsplit(hdrs["Location"])
    q = dict(urllib.parse.parse_qsl(loc.query))
    cookie = hdrs["Set-Cookie"].split(";")[0].split("=", 1)[1]
    return q["state"], cookie, q, hdrs


# --- the state read ------------------------------------------------------------------------------------------

def test_state_carries_the_card_and_no_secret(srv):
    code, _, data = call(srv, init=signed())
    y = json.loads(data)["youtube"]
    assert code == 200 and y["setup"] == {"client": True, "base": True} and y["connected"] is False
    assert y["enabled"] is False and [p["key"] for p in y["projects"] if p["on"]] == []  # default: none
    assert {p["key"]: p["speakable"] for p in y["projects"]} == {"alpha": True, "beta": False}
    assert "sample-web-secret" not in data.decode() and "cid.sample" not in data.decode()


def test_setup_pending_shows_no_working_button():
    s = make(store=Store(), base="")
    try:
        y = json.loads(call(s, init=signed())[2])["youtube"]
        assert y["setup"] == {"client": False, "base": False}
        assert yt(s, {"action": "connect"})[0] == 409
        assert get(s, PY.START_PATH + "?t=" + "a" * 43)[0] == 409
    finally:
        s.shutdown()
        s.server_close()


# --- the sign-in -------------------------------------------------------------------------------------------------

def test_the_whole_sign_in_binds_only_the_browser_that_took_the_ticket(srv):
    state, cookie, q, hdrs = _signin(srv)
    assert hdrs["Location"].startswith(Y.AUTH_URL + "?")
    assert q["redirect_uri"] == BASE + PY.CALLBACK_PATH  # from the config, never the Host header
    assert q["code_challenge_method"] == "S256" and q["scope"] == Y.SCOPE and q["access_type"] == "offline"
    assert q["client_id"] == "cid.sample.apps.example" and "secret" not in hdrs["Location"]
    sc = hdrs["Set-Cookie"]
    assert "HttpOnly" in sc and "Secure" in sc and "SameSite=Lax" in sc and "Path=/oauth/youtube" in sc
    assert hdrs["Cache-Control"] == "no-store" and hdrs["Referrer-Policy"] == "no-referrer"
    code, hdrs2, page = get(srv, PY.CALLBACK_PATH + "?" + urllib.parse.urlencode({"state": state, "code": "sample-code"}),
                            headers={"Cookie": PY.COOKIE + "=" + cookie})
    assert code == 200 and "החיבור הושלם".encode() in page and b"sample-code" not in page
    assert "Max-Age=0" in hdrs2["Set-Cookie"] and hdrs2["Referrer-Policy"] == "no-referrer"
    assert srv.youtube_store_obj.items[Y.REFRESH] == REFRESH_TOKEN
    exchange = [c for c in srv.youtube_request.calls if c[2].get("grant_type") == "authorization_code"][0][2]
    assert exchange["redirect_uri"] == BASE + PY.CALLBACK_PATH and len(exchange["code_verifier"]) >= 43
    assert exchange["client_secret"] == "sample-web-secret"
    time.sleep(0.2)  # the channel is read in the background
    assert Y.connection()["connected"] and Y.connection()["channel_title"] == "Sample channel"
    assert not [c for c in srv.youtube_request.calls if c[0] in ("POST", "DELETE") and "/youtube/" in c[1]]
    text = "\n".join(p.read_text(errors="replace") for p in Y.folder().rglob("*") if p.is_file())
    assert REFRESH_TOKEN not in text and "sample-code" not in text and cookie not in text


def test_a_ticket_works_once_and_expires(srv):
    code, _, j = yt(srv, {"action": "connect"})
    path = _ticket_path(j["url"])
    assert get(srv, path)[0] == 302 and get(srv, path)[0] == 403
    srv.youtube_flow.clock = lambda: time.time() + PY.TICKET_TTL + 1
    _, _, j2 = yt(srv, {"action": "connect"})
    srv.youtube_flow.clock = lambda: time.time() + 2 * PY.TICKET_TTL + 2
    assert get(srv, _ticket_path(j2["url"]))[0] == 403


@pytest.mark.parametrize("cookie", [None, "wrong-cookie-value-xxxxxxxxxxxxxx", "both"])
def test_the_callback_needs_the_binding_cookie(srv, cookie):
    state, good, _, _ = _signin(srv)
    header = {None: {}, "both": {"Cookie": f"{PY.COOKIE}={good}; {PY.COOKIE}={good}"}}.get(
        cookie, {"Cookie": f"{PY.COOKIE}={cookie}"})
    code, _, _ = get(srv, PY.CALLBACK_PATH + "?" + urllib.parse.urlencode({"state": state, "code": "c-attacker"}),
                     headers=header)
    assert code == 403 and Y.REFRESH not in srv.youtube_store_obj.items
    # the state is used up: the right cookie afterwards does not help
    code, _, _ = get(srv, PY.CALLBACK_PATH + "?" + urllib.parse.urlencode({"state": state, "code": "c"}),
                     headers={"Cookie": f"{PY.COOKIE}={good}"})
    assert code == 403 and not [c for c in srv.youtube_request.calls if c[1] == Y.TOKEN_URL]


def test_a_forged_state_from_someone_without_the_login_binds_nothing(srv):
    code, _, _ = get(srv, PY.CALLBACK_PATH + "?state=" + "f" * 43 + "&code=attacker-code",
                     headers={"Cookie": PY.COOKIE + "=" + "g" * 43})
    assert code == 403 and srv.youtube_request.calls == [] and Y.REFRESH not in srv.youtube_store_obj.items


def test_a_replayed_callback_is_refused(srv):
    state, cookie, _, _ = _signin(srv)
    path = PY.CALLBACK_PATH + "?" + urllib.parse.urlencode({"state": state, "code": "sample-code"})
    assert get(srv, path, headers={"Cookie": f"{PY.COOKIE}={cookie}"})[0] == 200
    assert get(srv, path, headers={"Cookie": f"{PY.COOKIE}={cookie}"})[0] == 403


def test_a_denied_consent_stores_nothing(srv):
    state, cookie, _, _ = _signin(srv)
    code, _, _ = get(srv, PY.CALLBACK_PATH + "?" + urllib.parse.urlencode({"state": state, "error": "access_denied"}),
                     headers={"Cookie": f"{PY.COOKIE}={cookie}"})
    assert code == 400 and Y.REFRESH not in srv.youtube_store_obj.items


@pytest.mark.parametrize("query", ["t=a&t=b", "t=" + "a" * 43 + "&x=1", "", "t=%zz"])
def test_malformed_start_requests_are_refused(srv, query):
    assert get(srv, PY.START_PATH + ("?" + query if query else ""))[0] in (400, 403, 404)


def test_connect_needs_the_owner(srv):
    assert yt(srv, {"action": "connect"}, init="")[0] == 403
    assert yt(srv, {"action": "connect"}, init=signed(user_id=OWNER + 1))[0] == 403
    assert yt(srv, {"action": "connect"}, init=signed(auth_date=int(time.time()) - 1000))[0] == 403  # needs a fresh login
    code, _, _ = call(srv, path="/api/youtube", init=signed(), body=b"{" + b" " * 600 + b"}")
    assert code == 413
    assert yt(srv, {"action": "connect", "extra": 1})[0] == 400


# --- switch, projects, the two-tap start, stop, disconnect ----------------------------------------------------

def _connected(enabled=True, live_enabled=True, channel_id="UCsample"):
    with Y.connection_lock():
        Y._set_connection(connected=True, channel_id=channel_id, channel_title="Sample channel", live_enabled=live_enabled)
    Y.set_cockpit_prefs(enabled=enabled)


def test_switch_and_projects_go_through_the_staff_settings(srv):
    assert yt(srv, {"action": "enable", "on": True})[0] == 200
    assert Y.settings(srv.cfg)["enabled"] is True
    assert yt(srv, {"action": "projects", "keys": ["gamma"]})[0] == 400  # not a known project
    assert yt(srv, {"action": "projects", "keys": ["alpha"]})[0] == 200
    assert Y.settings(srv.cfg)["projects"] == {"alpha": "אלפא"}
    assert yt(srv, {"action": "enable", "on": "yes"})[0] == 400


def test_start_takes_two_taps_and_one_token(srv):
    _connected()
    assert yt(srv, {"action": "start", "armed": "x" * 32})[0] == 409  # no first tap
    code, _, j = yt(srv, {"action": "arm"})
    assert code == 200 and j["channel"] == "Sample channel" and srv.spawned == []
    assert yt(srv, {"action": "start", "armed": j["armed"]})[0] == 202 and srv.spawned == ["start"]
    assert yt(srv, {"action": "start", "armed": j["armed"]})[0] == 409 and srv.spawned == ["start"]


def test_start_expires_and_only_the_latest_arm_counts(srv):
    _connected()
    first = yt(srv, {"action": "arm"})[2]["armed"]
    second = yt(srv, {"action": "arm"})[2]["armed"]
    assert yt(srv, {"action": "start", "armed": first})[0] == 409
    srv.youtube_flow.clock = lambda: time.time() + PY.ARM_TTL + 1
    assert yt(srv, {"action": "start", "armed": second})[0] == 409 and srv.spawned == []


@pytest.mark.parametrize("state", ["disabled", "not connected", "not enabled on youtube"])
def test_arm_is_refused_when_not_ready(srv, state):
    if state == "disabled":
        _connected(enabled=False)
    elif state == "not enabled on youtube":
        _connected(live_enabled=False)
    else:
        Y.set_cockpit_prefs(enabled=True)
    assert yt(srv, {"action": "arm"})[0] == 409


def test_start_is_refused_when_the_switch_went_off_between_the_taps(srv):
    _connected()
    token = yt(srv, {"action": "arm"})[2]["armed"]
    Y.set_cockpit_prefs(enabled=False)
    assert yt(srv, {"action": "start", "armed": token})[0] == 409 and srv.spawned == []


def test_stop_and_disconnect_run_detached(srv):
    assert yt(srv, {"action": "stop"})[0] == 202 and yt(srv, {"action": "disconnect"})[0] == 202
    assert srv.spawned == ["stop", "disconnect"]


# --- the bridge-off rule -------------------------------------------------------------------------------------

def test_while_off_only_stop_and_disconnect_pass(srv, monkeypatch):
    from ctb import policy
    monkeypatch.setattr(policy, "on", lambda: False)
    _connected()
    for body in ({"action": "connect"}, {"action": "arm"}, {"action": "start", "armed": "x" * 32},
                 {"action": "enable", "on": True}, {"action": "projects", "keys": []}, {}):
        assert yt(srv, body, init="")[0] == 503  # before the login is even looked at
    assert yt(srv, {"action": "stop"}, init="")[0] == 403  # the exceptions still need the owner
    assert yt(srv, {"action": "stop"})[0] == 202 and yt(srv, {"action": "disconnect"})[0] == 202
    assert get(srv, PY.START_PATH + "?t=" + "a" * 43)[0] == 503
    assert get(srv, PY.CALLBACK_PATH + "?state=" + "a" * 43 + "&code=c")[0] == 503
    assert srv.spawned == ["stop", "disconnect"]


def test_the_cli_records_each_action_for_the_card(monkeypatch):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    monkeypatch.setattr(Y, "stop", lambda c: "youtube_stopped")
    assert Y.switch(cfg, "stop") == "youtube_stopped"
    assert Y.operation()["result"] == "youtube_stopped" and not Y.operation()["failed"]


# --- the page ---------------------------------------------------------------------------------------------------

def test_both_languages_have_every_card_string():
    he, en = panel_page.STRINGS["he"], panel_page.STRINGS["en"]
    keys = [k for k in he if k.startswith("yt")]
    assert len(keys) > 25 and all(he[k].strip() and en[k].strip() for k in keys)
    for k in keys:  # a Latin word inside a Hebrew line only as a short isolated name
        line = he[k]
        for run in __import__("re").findall("[A-Za-z][A-Za-z \u00a0]*", line):
            assert "⁨" + run.strip() + "⁩" in line or "⁨" + run.strip() in line, (k, run)


@pytest.mark.parametrize("value,ok", [("https://cockpit.example.test", True), ("https://cockpit.example.test:10000/", True),
                                      ("http://cockpit.example.test", False), ("https://cockpit.example.test/path", False),
                                      ("https://user@cockpit.example.test", False), ("https://x.example.test?a=b", False)])
def test_the_public_base_is_an_https_origin_only(value, ok):
    from ctb import config as C
    if ok:
        assert C.from_dict({"panel_base_url": value}).panel_base_url == value.rstrip("/")
    else:
        with pytest.raises(C.ConfigError):
            C.from_dict({"panel_base_url": value})


# --- review of #231: the connection generation, the shared lock --------------------------------------------------

def _callback(s, state, cookie, code="sample-code"):
    return get(s, PY.CALLBACK_PATH + "?" + urllib.parse.urlencode({"state": state, "code": code}),
               headers={"Cookie": f"{PY.COOKIE}={cookie}"})


def _exchanges(s):
    return [c for c in s.youtube_request.calls if c[2].get("grant_type") == "authorization_code"]


def test_an_old_callback_after_a_cockpit_disconnect_stores_nothing(srv):
    state, cookie, _, _ = _signin(srv)
    assert yt(srv, {"action": "disconnect"})[0] == 202
    assert _callback(srv, state, cookie)[0] == 403
    assert _exchanges(srv) == [] and Y.REFRESH not in srv.youtube_store_obj.items


def test_an_old_callback_after_a_disconnect_elsewhere_stores_nothing(srv):
    """The CLI (another process) disconnected: the panel's memory still holds the state, the generation does not."""
    state, cookie, _, _ = _signin(srv)
    Y.bump_generation()
    assert _callback(srv, state, cookie)[0] == 403 and _exchanges(srv) == []


def test_an_unused_ticket_dies_with_a_disconnect(srv):
    _, _, j = yt(srv, {"action": "connect"})
    yt(srv, {"action": "disconnect"})
    assert get(srv, _ticket_path(j["url"]))[0] == 403


def test_a_disconnect_during_the_code_exchange_wins(srv):
    """The callback is already exchanging the code when the owner disconnects: nothing is stored, and the token
    Google just issued is revoked."""
    real = srv.youtube_request

    def google(method, url, body=None, form=None, token="", timeout=20.0):
        out = real(method, url, body=body, form=form, token=token, timeout=timeout)
        if url == Y.TOKEN_URL and (form or {}).get("grant_type") == "authorization_code":
            Y.bump_generation()  # the disconnect lands while the exchange is in flight
        return out
    srv.youtube_request = google
    state, cookie, _, _ = _signin(srv)
    assert _callback(srv, state, cookie)[0] == 409
    assert Y.REFRESH not in srv.youtube_store_obj.items and not Y.connection().get("connected")
    assert [c for c in real.calls if c[1] == Y.REVOKE_URL and c[2].get("token") == REFRESH_TOKEN]


def test_a_sign_in_finishing_between_the_two_taps_voids_the_confirmation(srv):
    _connected(channel_id="UCchannelA")
    token = yt(srv, {"action": "arm"})[2]["armed"]
    state, cookie, _, _ = _signin(srv)  # an earlier "Connect" finishes now, onto another channel
    assert _callback(srv, state, cookie)[0] == 200
    time.sleep(0.2)
    assert yt(srv, {"action": "start", "armed": token})[0] == 409 and srv.spawned == []


def test_a_confirmed_start_hands_its_channel_to_the_start_process(srv):
    _connected(channel_id="UCchannelA")
    gen = Y.generation()
    token = yt(srv, {"action": "arm"})[2]["armed"]
    assert yt(srv, {"action": "start", "armed": token})[0] == 202 and srv.spawned == ["start"]
    rid = srv.requests[0]
    assert Y.take_start_request(rid) == {"generation": gen, "channel_id": "UCchannelA"}
    with pytest.raises(Y.YouTubeError):  # once
        Y.take_start_request(rid)


@pytest.mark.parametrize("first", ["enabled", "projects"])
def test_concurrent_saves_both_survive(srv, monkeypatch, first):
    """A disable and a project change at the same time: each is read again and changed under the lock."""
    Y.set_cockpit_prefs(enabled=True, projects=["beta"])
    real = Y.cockpit_prefs

    def slow():
        got = real()
        time.sleep(0.2)  # the window in which an unlocked save would lose the other one
        return got
    monkeypatch.setattr(Y, "cockpit_prefs", slow)
    jobs = {"enabled": lambda: Y.set_cockpit_prefs(enabled=False), "projects": lambda: Y.set_cockpit_prefs(projects=["alpha"])}
    order = [first, "projects" if first == "enabled" else "enabled"]
    threads = [threading.Thread(target=jobs[name]) for name in order]
    for t in threads:
        t.start()
        time.sleep(0.05)
    for t in threads:
        t.join(5)
    assert real() == {"enabled": False, "projects": ["alpha"]}



def test_the_first_tap_shows_the_channel_its_token_is_bound_to(srv, monkeypatch):
    """Reviewer's repro 1: a sign-in to channel B lands between reads. The title in the answer and the channel
    the token is bound to come from one locked read, and the second tap is refused for B."""
    _connected(channel_id="UCchannelA")
    real_view = Y.view

    def view_then_switch(*a, **k):
        out = real_view(*a, **k)
        with Y.connection_lock():  # the sign-in to B completes right now
            Y._set_connection(generation=Y.generation() + 1, channel_id="UCchannelB", channel_title="Channel B")
        return out
    monkeypatch.setattr(Y, "view", view_then_switch)
    code, _, j = yt(srv, {"action": "arm"})
    assert code == 200 and j["channel"] == "Sample channel"  # A's title, the one the token is bound to
    assert yt(srv, {"action": "start", "armed": j["armed"]})[0] == 409 and srv.spawned == []


def test_the_sign_in_callback_is_refused_while_live(srv):
    state, cookie, _, _ = _signin(srv)
    Y._set_state(on=True, broadcast_id="bc-1", owner_start="n")
    assert _callback(srv, state, cookie)[0] == 409 and Y.REFRESH not in srv.youtube_store_obj.items


def test_the_card_says_reconnect_and_reports_a_failed_revoke():
    from test_panel_page import func
    card = func("youtubeCard")
    assert 'y.result==="youtube_reconnect_client"' in card and "S.ytReconnect" in card
    assert 'y.result==="youtube_disconnected_revoke_failed"' in card and "S.ytRevokeFailed" in card
    for lang in ("he", "en"):
        assert panel_page.STRINGS[lang]["ytReconnect"] and panel_page.STRINGS[lang]["ytRevokeFailed"]
    Y._op(action="disconnect", at=int(time.time()), done=True, ok=True, result="youtube_disconnected_revoke_failed")
    assert Y.operation()["result"] == "youtube_disconnected_revoke_failed" and not Y.operation()["failed"]
