"""The Harnessgram widget device API (device_tokens.py, panel.py): pairing behind the fresh owner login, a hashed
30-day token that reaches only the two widget routes, a read-only snapshot, and answers that never touch a
sensitive decision."""
import json
import os
import time

import pytest

from ctb import decisions, device_tokens, listen_link, panel
from ctb.config import state_dir
from test_decisions import ORBIT_S, ask, queued, rig, roomy_budget, server, cfg  # noqa: F401  (fixtures)
from test_panel import OWNER, call, signed


@pytest.fixture(autouse=True)
def fresh_rate():
    panel._widget_calls.clear()
    yield
    panel._widget_calls.clear()


def pair(server, label=None):
    body = json.dumps({} if label is None else {"label": label}).encode()
    status, _, data = call(server, path="/api/device-pair", init=signed(), body=body)
    assert status == 200, data
    return json.loads(data)


def bearer(token):
    return {"Authorization": "Bearer " + token}


def widget(server, path="/api/widget-state", token=None, body=b"{}", headers=None, init=None):
    h = dict(bearer(token) if token else {}, **(headers or {}))
    return call(server, path=path, init=init, body=body, headers=h)


def answer(server, token, ident, choice="yes"):
    return widget(server, "/api/widget-decide", token, json.dumps({"id": ident, "choice": choice}).encode())


# --- pairing --------------------------------------------------------------------------------------------------

def test_pairing_needs_the_fresh_owner_login_and_stores_only_a_hash(rig, server):
    for init in (None, "garbage", signed(user_id=OWNER + 1), signed(auth_date=int(time.time()) - 1000)):
        assert call(server, path="/api/device-pair", init=init, body=b"{}")[0] == 403
    out = pair(server, "My iPhone")
    assert device_tokens.TOKEN.fullmatch(out["token"]) and out["label"] == "My iPhone"
    assert out["expires"] - out["created"] == 30 * 86400
    stored = (state_dir() / device_tokens.STORE).read_text()
    assert out["token"] not in stored and device_tokens._digest(out["token"]) in stored
    assert oct(os.stat(state_dir() / device_tokens.STORE).st_mode & 0o777) == "0o600"
    assert pair(server)["label"] == "iPhone"
    assert call(server, path="/api/device-pair", init=signed(), body=b'{"label": "x", "admin": true}')[0] == 400
    assert call(server, path="/api/device-pair", init=signed(), body=json.dumps({"label": "x" * 41}).encode())[0] == 400


def test_pairing_is_rate_limited(rig, server):
    for _ in range(device_tokens.PAIR_LIMIT):
        pair(server)
    status, _, data = call(server, path="/api/device-pair", init=signed(), body=b"{}")
    assert status == 429 and json.loads(data)["error"] == "rate" and json.loads(data)["retry"] > 0


def test_at_most_max_devices_stay_paired_oldest_first(rig, monkeypatch):
    monkeypatch.setattr(device_tokens, "PAIR_LIMIT", 100)
    tokens = [device_tokens.pair("d%d" % i, now=1000 + i)["token"] for i in range(device_tokens.MAX_DEVICES + 1)]
    assert device_tokens.check(tokens[0], now=2000) is None
    assert all(device_tokens.check(t, now=2000) for t in tokens[1:])


def test_a_token_expires_after_thirty_days(rig):
    token = device_tokens.pair(now=1000)["token"]
    assert device_tokens.check(token, now=1000 + device_tokens.TTL - 1)
    assert device_tokens.check(token, now=1000 + device_tokens.TTL) is None


# --- the token's reach ----------------------------------------------------------------------------------------

def test_the_snapshot_needs_a_paired_token_in_the_header_only(rig, server):
    token = pair(server)["token"]
    assert widget(server, token=token)[0] == 200
    bad = [None, "hgw_" + "A" * 43, token + "x", token.upper()]
    for t in bad:
        status, _, data = widget(server, token=t)
        assert status == 403 and json.loads(data) == {"error": "forbidden"}
    # Telegram initData is never accepted on a widget route, not even the owner's own fresh login
    assert widget(server, init=signed())[0] == 403
    assert widget(server, headers={"Authorization": "bearer " + token})[0] == 403
    assert widget(server, headers={"Authorization": "Basic " + token})[0] == 403
    # never in a URL
    assert call(server, path="/api/widget-state?token=" + token, body=b"{}")[0] == 404
    assert call(server, path="/api/widget-state?auth=" + token, body=b"{}", headers=bearer(token))[0] == 404


def test_two_authorization_headers_are_refused(rig, server):
    import http.client
    token = pair(server)["token"]
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.putrequest("POST", "/api/widget-state")
    conn.putheader("Authorization", "Bearer " + token)
    conn.putheader("Authorization", "Bearer " + token)
    conn.putheader("Content-Length", "2")
    conn.endheaders(b"{}")
    assert conn.getresponse().status == 403


def test_a_device_token_reaches_nothing_else(rig, server):
    token = pair(server)["token"]
    rec = ask(rig)
    for route in ("/api/state", "/api/decide", "/api/device-pair", "/api/devices", "/api/radio-player",
                  "/api/listen-link", "/api/addons"):
        status, _, data = call(server, path=route, body=b'{"action": "list"}',
                               headers=dict(bearer(token), **{panel.INIT_HEADER: token}))
        assert status == 403 and token.encode() not in data, route
    assert call(server, "GET", "/listen/" + token)[0] in (403, 404)
    assert decisions.open_decisions()[0]["id"] == rec["id"]


def test_a_listening_token_is_no_device_token(rig, server):
    listening = listen_link.mint()
    for path in panel.WIDGET_ROUTES:
        assert widget(server, path, listening)[0] == 403


def test_revoked_tokens_stop_at_once_and_settings_lists_no_secrets(rig, server):
    one, two = pair(server, "One"), pair(server, "Two")
    status, _, data = call(server, path="/api/devices", init=signed(), body=b'{"action": "list"}')
    devices = json.loads(data)["devices"]
    assert status == 200 and sorted(d["label"] for d in devices) == ["One", "Two"]
    assert set(devices[0]) == {"id", "label", "created", "expires", "used"}
    assert one["token"] not in data.decode() and device_tokens._digest(one["token"]) not in data.decode()
    state = json.loads(call(server, init=signed())[2])
    assert {d["id"] for d in state["widget_devices"]} == {one["id"], two["id"]}
    assert one["token"] not in json.dumps(state)
    assert call(server, path="/api/devices", body=json.dumps({"action": "revoke", "id": one["id"]}).encode())[0] == 403
    status, _, data = call(server, path="/api/devices", init=signed(),
                           body=json.dumps({"action": "revoke", "id": one["id"]}).encode())
    assert status == 200 and [d["id"] for d in json.loads(data)["devices"]] == [two["id"]]
    assert widget(server, token=one["token"])[0] == 403 and widget(server, token=two["token"])[0] == 200
    assert call(server, path="/api/devices", init=signed(),
                body=json.dumps({"action": "revoke", "id": one["id"]}).encode())[0] == 404
    assert call(server, path="/api/devices", init=signed(), body=b'{"action": "revoke-all"}')[0] == 200
    assert widget(server, token=two["token"])[0] == 403
    assert call(server, path="/api/devices", init=signed(), body=b'{"action": "drop"}')[0] == 400


def test_each_device_is_rate_limited(rig, server, monkeypatch):
    monkeypatch.setattr(panel, "WIDGET_CALLS", 2)
    token, other = pair(server)["token"], pair(server)["token"]
    assert [widget(server, token=token)[0] for _ in range(3)] == [200, 200, 429]
    assert widget(server, token=other)[0] == 200


# --- the snapshot ---------------------------------------------------------------------------------------------

def test_the_snapshot_is_the_parsers_subset_with_the_sensitive_flag(rig, server):
    token = pair(server)["token"]
    plain = ask(rig, title="Ship the fix?", detail="the private detail", routine=True)
    risky = ask(rig, title="Promote build 13 to production?")
    status, headers, data = widget(server, token=token)
    body = json.loads(data)
    assert status == 200 and headers["Cache-Control"] == "no-store" and body["ok"] is True
    assert set(body) == {"ok", "now", "bot_id", "decisions", "sessions", "cards", "radio_player"}
    by_id = {d["id"]: d for d in body["decisions"]}
    assert by_id[plain["id"]]["sensitive"] is False and by_id[risky["id"]]["sensitive"] is True
    assert set(by_id[plain["id"]]) <= set(panel.WIDGET_DECISION) | {k + "_he" for k in panel.WIDGET_DECISION}
    assert "the private detail" not in data.decode() and ORBIT_S not in data.decode()
    assert body["radio_player"] == {"ok": True, "enabled": False, "on": False}
    assert widget(server, token=token, body=b'{"x": 1}')[0] == 400


def test_the_owner_state_carries_the_sensitive_flag_too(rig, server):
    risky = ask(rig, title="Delete the old backups?")
    [d] = json.loads(call(server, init=signed())[2])["decisions"]
    assert d["id"] == risky["id"] and d["sensitive"] is True


# --- answers --------------------------------------------------------------------------------------------------

def test_a_plain_decision_is_answered_once_and_a_repeat_is_a_success(rig, server):
    token = pair(server)["token"]
    rec = ask(rig, title="Ship the fix?", routine=True)
    status, _, data = answer(server, token, rec["id"])
    assert status == 200 and json.loads(data)["ok"] is True
    status, _, data = answer(server, token, rec["id"])
    assert (status, json.loads(data)) == (200, {"ok": True, "already": True})
    status, _, data = answer(server, token, rec["id"], "no")
    assert status == 409 and json.loads(data) == {"error": "closed", "state": "answered", "choice": "yes"}
    assert len(queued(rig, ORBIT_S)) == 1


@pytest.mark.parametrize("title, detail, yes, no, routine", [
    # not asked as routine: sensitive whatever the words say (the allow-list; the word rule misses "Buy ... EUR")
    ("Buy a domain for 12 EUR?", "", "Buy", "", False), ("Ship the fix?", "", "", "", False),
    # asked as routine, but the words name a sensitive area: still sensitive
    ("Promote build 13 to production?", "", "", "", True), ("Go ahead?", "This deletes the old backups", "", "", True),
    ("Next step?", "", "Publish now", "", True), ("Submit to App Review?", "", "", "", True),
    ("Keep the old backups?", "", "Keep", "Delete backups", True),   # the no label counts too
])
def test_a_sensitive_decision_needs_the_app(rig, server, title, detail, yes, no, routine):
    token = pair(server)["token"]
    rec = ask(rig, title=title, detail=detail, yes=yes, no=no, routine=routine)
    for choice in ("yes", "no"):
        status, _, data = answer(server, token, rec["id"], choice)
        assert (status, json.loads(data)) == (403, {"error": "needs-app"})
    assert decisions.open_decisions()[0]["id"] == rec["id"] and queued(rig, ORBIT_S) == []
    assert widget(server, token=token)[0] == 200  # a needs-app refusal does not revoke the device


def test_the_sensitive_check_runs_under_the_lock_on_the_record_it_answers(rig, server, monkeypatch):
    token = pair(server)["token"]
    rec = ask(rig, title="Ship the fix?", routine=True)
    seen = []
    real = decisions.answer

    def spy(*a, **k):
        seen.append(k.get("check"))
        return real(*a, **k)

    monkeypatch.setattr(decisions, "answer", spy)
    assert answer(server, token, rec["id"])[0] == 200
    assert callable(seen[0])
    with pytest.raises(decisions.DecisionError) as e:
        seen[0](dict(rec, title="Deploy to production"))
    assert e.value.kind == "sensitive"


def test_bad_answers_are_refused(rig, server):
    token = pair(server)["token"]
    rec = ask(rig)
    assert answer(server, token, "0" * 16)[0] == 404
    for body in ({"id": rec["id"]}, {"id": rec["id"], "choice": "maybe"}, {"id": "x", "choice": "yes"},
                 {"id": rec["id"], "choice": "yes", "note": "hi"}, []):
        assert widget(server, "/api/widget-decide", token, json.dumps(body).encode())[0] == 400, body
    assert widget(server, "/api/widget-decide", None, json.dumps({"id": rec["id"], "choice": "yes"}).encode(),
                  init=signed())[0] == 403
    assert decisions.open_decisions()[0]["id"] == rec["id"] and queued(rig, ORBIT_S) == []


def test_a_stale_decision_is_never_answered_from_a_widget(rig, server):
    token = pair(server)["token"]
    rec = ask(rig)
    rig.states[ORBIT_S] = "ended"
    status, _, data = answer(server, token, rec["id"])
    assert status == 409 and json.loads(data)["state"] == "stale" and queued(rig, ORBIT_S) == []


def test_the_token_is_never_logged(rig, server, capsys):
    token = pair(server)["token"]
    widget(server, token=token)
    widget(server, token=token + "x")
    out = capsys.readouterr()
    assert token not in out.out + out.err


def test_the_owner_is_told_when_a_device_is_paired(rig, server):
    pair(server, "Kitchen iPad")
    for _ in range(100):
        sent = [m for m in rig.bot.methods("sendMessage") if "Kitchen iPad" in m.get("text", "")]
        if sent:
            break
        time.sleep(0.02)
    assert sent and sent[0]["chat_id"] == OWNER and "Settings" in sent[0]["text"]
    assert "hgw_" not in json.dumps(rig.bot.methods("sendMessage"))


def test_settings_lists_the_devices_with_a_revoke_each_in_both_languages():
    from ctb import panel_page
    for lang in ("he", "en"):
        page = panel_page.render_page(lang, "n").decode("utf-8")
        assert "function devicesRow(d)" in page and 'post("/api/devices",{action:"revoke"' in page
        assert "data-device-revoke" in page
    for key in ("devicesTitle", "devicesHint", "devicesAdded", "devicesRevoke", "devicesRevoked", "devicesFailed"):
        assert panel_page.STRINGS["he"][key] and panel_page.STRINGS["en"][key]
    from pathlib import Path
    screens = (Path(panel_page.__file__).parent / "cockpit_v2_screens.js").read_text(encoding="utf-8")
    assert 'name==="connect"){var drow=devicesRow(d)' in screens


def test_an_old_record_without_the_routine_mark_is_sensitive(rig, server):
    token = pair(server)["token"]
    rec = ask(rig, title="Ship the fix?", routine=True)
    path = state_dir() / decisions.STATE_FILE
    data = json.loads(path.read_text())
    for value in (None, "true", 1):  # written before the flag existed, or damaged: never routine
        if value is None:
            data[rec["id"]].pop("routine", None)
        else:
            data[rec["id"]]["routine"] = value
        path.write_text(json.dumps(data))
        assert answer(server, token, rec["id"])[0] == 403
        [d] = json.loads(widget(server, token=token)[2])["decisions"]
        assert d["sensitive"] is True
    assert queued(rig, ORBIT_S) == []


def test_routine_is_an_explicit_opt_in_of_the_asking_agent(rig):
    from ctb import cli
    parser = cli._parser()
    assert parser.parse_args(["decide", "ask", "Ship?", "--routine"]).routine is True
    assert parser.parse_args(["decide", "ask", "Ship?"]).routine is False
    assert ask(rig, title="Ship?")["routine"] is False
    assert ask(rig, title="Ship again?", routine=True)["routine"] is True


def test_both_states_carry_the_bridge_bots_numeric_id_and_never_the_token(rig, server):
    from conftest import GOOD_TOKEN
    token = pair(server)["token"]
    want = int(GOOD_TOKEN.split(":", 1)[0])
    for status, _, data in (call(server, init=signed()), widget(server, token=token)):
        body = json.loads(data)
        assert status == 200 and type(body["bot_id"]) is int and body["bot_id"] == want
        assert GOOD_TOKEN not in data.decode() and GOOD_TOKEN.split(":", 1)[1] not in data.decode()


@pytest.mark.parametrize("raw, want", [("123456:" + "A" * 35, 123456), ("", None), ("abc:def", None),
                                       ("123456", None), ("1" * 21 + ":x", None)])
def test_the_bot_id_is_only_the_tokens_leading_number(raw, want):
    assert panel.bot_id_of(raw) == want
