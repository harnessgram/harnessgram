from ctb import apns
from ctb.config import Config, read_map
from test_panel import call, server, signed  # noqa: F401


def setup_function():
    # Never let a fake transport or client leak between tests.
    apns._CLIENT = apns._CLIENT_KEY = None
    apns._CLIENT_FAILURES.clear()
    apns._WARNED.clear()


def test_endpoint_accepts_owner_and_validates_strictly(server, monkeypatch):
    seen = []
    monkeypatch.setattr(apns, "register", lambda *args: seen.append(args) or True)
    body = b'{"card_id":"card","token":"aabb","env":"sandbox"}'
    assert call(server, path="/api/live-activity-token", init=signed(), body=body)[0] == 200
    assert seen[0][:4] == ("owner", "card", "aabb", "sandbox")
    assert call(server, path="/api/live-activity-token", body=body)[0] == 403
    bad = b'{"card_id":"card","token":"aabb","env":"sandbox","extra":true}'
    assert call(server, path="/api/live-activity-token", init=signed(), body=bad)[0] == 400


def test_token_requests_leave_a_trace_without_the_token(server, monkeypatch, capsys):
    monkeypatch.setattr(apns, "register", lambda *args: True)
    body = b'{"card_id":"secretcard","token":"aabbccdd","env":"production"}'
    assert call(server, path="/api/live-activity-token", init=signed(), body=body)[0] == 200
    assert call(server, path="/api/live-activity-token", body=body)[0] == 403
    err = capsys.readouterr().err
    assert "live-activity token ok who=owner ended=False env=production" in err
    assert "live-activity token forbidden" in err
    assert "aabbccdd" not in err and "secretcard" not in err


def test_problem_reports_are_logged_and_never_stored(server, monkeypatch, capsys):
    stored = []
    monkeypatch.setattr(apns, "register", lambda *args: stored.append(args) or True)
    path = "/api/live-activity-token"
    for code in ("no-token-after-grace", "push-disabled", "request-failed", "request-failed:visibility",
                 "request-failed:NSCocoaErrorDomain.-4"):
        body = ('{"card_id":"secretcard","problem":"%s"}' % code).encode()
        assert call(server, path=path, init=signed(), body=body)[0] == 200
    for bad in (b'{"card_id":"secretcard","problem":"free text here"}',
                b'{"card_id":"secretcard","problem":"request-failed:' + b"x" * 65 + b'"}',
                b'{"card_id":"secretcard","problem":"no-token-after-grace","token":"aabbccdd","env":"production"}',
                b'{"card_id":"secretcard","problem":7}',
                b'{"card_id":"bad card","problem":"push-disabled"}'):
        assert call(server, path=path, init=signed(), body=bad)[0] == 400
    # The same login rules as a token: no login, no report.
    assert call(server, path=path, body=b'{"card_id":"secretcard","problem":"push-disabled"}')[0] == 403
    assert stored == []
    err = capsys.readouterr().err
    assert "live-activity token problem who=owner problem=request-failed:visibility" in err
    assert "live-activity token problem who=owner problem=no-token-after-grace" in err
    assert "secretcard" not in err and "free text" not in err and "aabbccdd" not in err


def test_endpoint_accepts_paired_device(server, monkeypatch):
    from ctb import device_tokens
    token = device_tokens.pair(now=1000)["token"]
    monkeypatch.setattr(device_tokens, "check", lambda value: "device-id" if value == token else None)
    monkeypatch.setattr(apns, "register", lambda device, *args: device == "device-id")
    body = b'{"card_id":"card","ended":true}'
    status = call(server, path="/api/live-activity-token", body=body,
                  headers={"Authorization": "Bearer " + token})[0]
    assert status == 200


def test_token_validation_cap_and_end(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    assert not apns.register("device", "card", "not-hex", "sandbox")
    assert not apns.register("device", "card", "aa", "wrong")
    for n in range(11):
        assert apns.register("device", f"card-{n}", f"{n:02x}", "sandbox", now=n)
    rows = list(read_map(tmp_path / apns.TOKEN_FILE).values())
    assert len(rows) == 10 and {r["card_id"] for r in rows} == {f"card-{n}" for n in range(1, 11)}
    assert apns.register("device", "card-10", ended=True)
    assert len(read_map(tmp_path / apns.TOKEN_FILE)) == 9


def test_content_state_has_contract_fields():
    state = apns.content_state({"title": "Build", "topic": "Project", "steps": ["A", "B"],
                                "at": 1, "percent": 50})
    assert tuple(state) == apns.CONTENT_STATE_FIELDS
    assert state == {"title": "Build", "topic": "Project", "steps": ["A", "B"], "done": 1,
                     "percent": 50, "finished": False}


def test_content_state_bounds_steps_strings_and_done():
    huge = "🌍" * 100
    state = apns.content_state({"title": huge, "topic": huge, "steps": [huge] * 14,
                                "at": 99, "percent": None})
    assert len(state["steps"]) == 12 and state["done"] == 12 and state["percent"] == 100
    assert state["finished"] is True
    assert all(len(value.encode()) <= 120 for value in [state["title"], state["topic"], *state["steps"]])
    assert not apns.content_state({"steps": ["step"] * 14, "at": 12})["finished"]


def test_notify_only_persists_newest_record(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(apns.time, "time", lambda: 100)
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [{"token": "test-token"}])
    apns.notify({"id": "c", "kind": "progress", "percent": 1})
    with apns.locked_map(tmp_path / apns.PENDING_FILE) as data:
        data["c"]["attempts"] = 4
    apns.notify({"id": "c", "kind": "progress", "percent": 2})
    pending = read_map(tmp_path / apns.PENDING_FILE)
    assert list(pending) == ["c"] and pending["c"]["state"]["percent"] == 2
    assert set(pending["c"]["state"]) == set(apns.CONTENT_STATE_FIELDS)
    assert pending["c"]["attempts"] == 0


def test_notify_without_activity_token_keeps_no_card_data(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [])
    apns.notify({"id": "sample-card", "kind": "progress", "title": "private"})
    assert read_map(tmp_path / apns.PENDING_FILE) == {}


def test_drain_sends_then_throttles_and_coalesces(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    sent = []
    monkeypatch.setattr(apns, "_extras_available", lambda: True)
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [{"token": "aa", "env": "sandbox"}])
    monkeypatch.setattr(apns, "APNSClient", lambda cfg, clock: type(
        "Client", (), {"send": lambda self, row, card, event: sent.append((card["percent"], event)) or 200})())
    cfg = Config(apns_key_id="K", apns_team_id="T", apns_bundle_id="com.example.app")
    monkeypatch.setattr(apns.time, "time", lambda: 100)
    apns.notify({"id": "c", "kind": "progress", "percent": 1})
    assert apns.drain(cfg, 100) == 1
    apns.notify({"id": "c", "kind": "progress", "percent": 2})
    apns.notify({"id": "c", "kind": "progress", "percent": 3})
    assert apns.drain(cfg, 110) == 0
    assert apns.drain(cfg, 115) == 1
    apns.notify({"id": "c", "kind": "progress", "percent": 100})
    assert apns.drain(cfg, 130) == 1
    assert sent == [(1, "update"), (3, "update"), (100, "end")]


def test_identical_notify_during_send_survives(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(apns, "_extras_available", lambda: True)
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [{"token": "aa", "env": "sandbox"}])
    monkeypatch.setattr(apns.time, "time", lambda: 100)
    card = {"id": "c", "kind": "progress", "percent": 1}
    apns.notify(card)

    class Client:
        def __init__(self, cfg, clock): pass
        def send(self, row, state, event):
            apns.notify(card)
            return 200

    monkeypatch.setattr(apns, "APNSClient", Client)
    cfg = Config(apns_key_id="K", apns_team_id="T", apns_bundle_id="com.example.app")
    assert apns.drain(cfg, 100) == 1
    assert read_map(tmp_path / apns.PENDING_FILE)["c"]["gen"] == 2
    # The newer notification kept above is not throttled by its predecessor's send.
    assert "c" not in read_map(tmp_path / apns.DELIVERY_FILE)
    assert apns.drain(cfg, 101) == 1


def test_failed_client_validation_is_cached_and_closes_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(apns, "_extras_available", lambda: True)
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [{"token": "aa", "env": "sandbox"}])
    monkeypatch.setattr(apns.time, "time", lambda: 100)
    apns.notify({"id": "c", "kind": "progress"})
    calls, closes = [], []

    class Client:
        def __init__(self, cfg, clock): calls.append("made")
        def validate(self):
            calls.append("validate")
            raise RuntimeError("unreadable")
        def close(self): closes.append(True)

    monkeypatch.setattr(apns, "APNSClient", Client)
    cfg = Config(apns_key_id="K", apns_team_id="T", apns_bundle_id="com.example.app")
    assert apns.drain(cfg, 100) == 0
    assert apns.drain(cfg, 101) == 0
    assert calls == ["made", "validate"] and closes == [True]


def test_drain_retries_transient_response(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    statuses = iter((503, 200))
    monkeypatch.setattr(apns, "_extras_available", lambda: True)
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [{"token": "aa", "env": "sandbox"}])
    monkeypatch.setattr(apns, "APNSClient", lambda cfg, clock: type(
        "Client", (), {"send": lambda self, row, card, event: next(statuses)})())
    cfg = Config(apns_key_id="K", apns_team_id="T", apns_bundle_id="com.example.app")
    monkeypatch.setattr(apns.time, "time", lambda: 100)
    apns.notify({"id": "c", "kind": "progress", "percent": 1})
    assert apns.drain(cfg, 100) == 1
    assert "c" in read_map(tmp_path / apns.PENDING_FILE)
    assert apns.drain(cfg, 101) == 0
    assert apns.drain(cfg, 115) == 1
    assert read_map(tmp_path / apns.PENDING_FILE) == {}


def test_drain_reuses_client_and_does_not_resend_successful_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    rows = [{"device": "phone-a", "token": "token-a", "env": "sandbox"},
            {"device": "phone-b", "token": "token-b", "env": "sandbox"}]
    monkeypatch.setattr(apns, "tokens_for", lambda ident: rows)
    monkeypatch.setattr(apns, "_extras_available", lambda: True)
    made, calls = [], []
    statuses = {"token-a": [200], "token-b": [503, 200]}
    class FakeClient:
        def __init__(self, cfg, clock):
            made.append(self)
            self.clock = clock
        def send(self, row, state, event):
            calls.append(row["token"])
            return statuses[row["token"]].pop(0)
        def close(self):
            pass
    monkeypatch.setattr(apns, "APNSClient", FakeClient)
    monkeypatch.setattr(apns.time, "time", lambda: 100)
    apns.notify({"id": "sample-card", "kind": "progress", "percent": 1})
    cfg = Config(apns_key_id="key", apns_team_id="team", apns_bundle_id="com.example.app")
    assert apns.drain(cfg, 100) == 1
    assert apns.drain(cfg, 102) == 1
    assert len(made) == 1 and calls == ["token-a", "token-b", "token-b"]


def test_transport_error_closes_reused_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [{"device": "phone", "token": "test-token", "env": "sandbox"}])
    monkeypatch.setattr(apns, "_extras_available", lambda: True)
    closed = []
    class FakeClient:
        def __init__(self, cfg, clock): self.clock = clock
        def send(self, *args): raise OSError("offline")
        def close(self): closed.append(True)
    monkeypatch.setattr(apns, "APNSClient", FakeClient)
    monkeypatch.setattr(apns.time, "time", lambda: 100)
    apns.notify({"id": "sample-card", "kind": "progress"})
    cfg = Config(apns_key_id="key", apns_team_id="team", apns_bundle_id="com.example.app")
    assert apns.drain(cfg, 100) == 1 and closed == [True]


def test_successful_end_removes_activity_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    row = {"device": "phone", "card_id": "sample-card", "token": "test-token", "env": "sandbox"}
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [row])
    monkeypatch.setattr(apns, "_extras_available", lambda: True)
    monkeypatch.setattr(apns, "APNSClient", lambda cfg, clock: type(
        "Client", (), {"send": lambda self, row, state, event: 200})())
    dropped = []
    monkeypatch.setattr(apns, "drop", dropped.append)
    monkeypatch.setattr(apns.time, "time", lambda: 100)
    apns.notify({"id": "sample-card", "kind": "progress", "state": "closed"}, "end")
    cfg = Config(apns_key_id="key", apns_team_id="team", apns_bundle_id="com.example.app")
    assert apns.drain(cfg, 100) == 1 and dropped == [row]


def test_attempt_limit_discards_permanent_retry_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [{"device": "phone", "token": "test-token", "env": "sandbox"}])
    monkeypatch.setattr(apns, "_extras_available", lambda: True)
    monkeypatch.setattr(apns, "APNSClient", lambda cfg, clock: type(
        "Client", (), {"send": lambda self, row, state, event: 503})())
    monkeypatch.setattr(apns.time, "time", lambda: 100)
    apns.notify({"id": "sample-card", "kind": "progress"})
    with apns.locked_map(tmp_path / apns.PENDING_FILE) as data:
        data["sample-card"]["attempts"] = apns.MAX_ATTEMPTS - 1
    cfg = Config(apns_key_id="key", apns_team_id="team", apns_bundle_id="com.example.app")
    assert apns.drain(cfg, 100) == 1
    assert read_map(tmp_path / apns.PENDING_FILE) == {}


def test_drain_without_config_or_optional_extra_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [{"token": "test-token"}])
    apns.notify({"id": "c", "kind": "progress", "percent": 1})
    assert apns.drain(Config(), 100) == 0
    monkeypatch.setattr(apns, "_extras_available", lambda: False)
    cfg = Config(apns_key_id="K", apns_team_id="T", apns_bundle_id="com.example.app")
    assert apns.drain(cfg, 100) == 0
    assert apns.drain(cfg, 101) == 0
    assert "c" in read_map(tmp_path / apns.PENDING_FILE)


def test_missing_extra_warning_is_emitted_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [{"token": "private-token"}])
    monkeypatch.setattr(apns, "_extras_available", lambda: False)
    apns.notify({"id": "private-title", "kind": "progress", "title": "Never log me"})
    cfg = Config(apns_key_id="K", apns_team_id="T", apns_bundle_id="com.example.app")
    apns.drain(cfg, 100)
    apns.drain(cfg, 101)
    err = capsys.readouterr().err
    assert err.count("live-activity") == 1
    assert "private-token" not in err and "Never log me" not in err


def test_finish_is_end_and_priority_ten(monkeypatch):
    posted = []
    http = type("HTTP", (), {"post": lambda self, url, **kw: posted.append(kw) or type("R", (), {"status_code": 200})()})()
    client = apns.APNSClient(Config(apns_bundle_id="com.example.app"), keychain=object(), http=http)
    monkeypatch.setattr(client, "jwt", lambda: "signed")
    client.send({"token": "aa", "env": "production"}, {"title": "Done", "percent": 100}, "end")
    assert posted[0]["headers"]["apns-priority"] == "10"
    assert posted[0]["json"]["aps"]["event"] == "end"
    assert posted[0]["json"]["aps"]["dismissal-date"] == posted[0]["json"]["aps"]["timestamp"] + 900


def test_payload_strings_are_bounded_below_apns_limit(monkeypatch):
    posted = []
    http = type("HTTP", (), {"post": lambda self, url, **kw: posted.append(kw) or type("R", (), {"status_code": 200})()})()
    client = apns.APNSClient(Config(apns_bundle_id="com.example.app"), keychain=object(), http=http)
    monkeypatch.setattr(client, "jwt", lambda: "signed")
    huge = "🌍" * 5000
    client.send({"token": "test-token", "env": "sandbox"},
                {"title": huge, "detail": huge, "steps": [huge], "at": 0}, "update")
    import json
    assert len(json.dumps(posted[0]["json"], ensure_ascii=False).encode()) < apns.MAX_PAYLOAD_BYTES


def test_bad_device_is_dropped(monkeypatch):
    dropped = []
    response = type("R", (), {"status_code": 410})()
    http = type("HTTP", (), {"post": lambda self, *a, **kw: response})()
    client = apns.APNSClient(Config(apns_bundle_id="com.example.app"), keychain=object(), http=http)
    monkeypatch.setattr(client, "jwt", lambda: "signed")
    monkeypatch.setattr(apns, "drop", dropped.append)
    row = {"token": "aa", "env": "sandbox"}
    client.send(row, {}, "update")
    assert dropped == [row]


def test_invalid_provider_token_clears_cached_jwt(monkeypatch):
    response = type("R", (), {"status_code": 403, "json": lambda self: {"reason": "InvalidProviderToken"}})()
    client = apns.APNSClient(Config(apns_bundle_id="com.example.app"), keychain=object(),
                             http=type("HTTP", (), {"post": lambda self, *a, **kw: response})())
    client._jwt, client._jwt_at = "cached", 10
    monkeypatch.setattr(client, "jwt", lambda: "cached")
    client.send({"token": "test-token", "env": "sandbox"}, {}, "update")
    assert client._jwt == "" and client._jwt_at == 0


def test_expired_provider_token_refreshes_and_retries(monkeypatch):
    responses = iter((
        type("R", (), {"status_code": 403, "json": lambda self: {"reason": "ExpiredProviderToken"}})(),
        type("R", (), {"status_code": 200})(),
    ))
    posted = []
    http = type("HTTP", (), {"post": lambda self, *args, **kwargs: posted.append(kwargs) or next(responses)})()
    client = apns.APNSClient(Config(apns_bundle_id="com.example.app"), keychain=object(), http=http)
    tokens = iter(("old-jwt", "new-jwt"))
    monkeypatch.setattr(client, "jwt", lambda: next(tokens))
    assert client.send({"token": "test-token", "env": "sandbox"}, {}, "update") == 200
    assert len(posted) == 2 and posted[1]["headers"]["authorization"] == "bearer new-jwt"


def test_jwt_is_cached_then_refreshed():
    reads = []
    keychain = type("Keys", (), {"get": lambda self, item: reads.append(item) or "fake-key"})()
    now = [1000.0]
    class Client(apns.APNSClient):
        @staticmethod
        def _sign(signing, key):
            return signing[-1:] * 64
    client = Client(Config(apns_key_id="KEY", apns_team_id="TEAM"), keychain=keychain,
                    http=object(), clock=lambda: now[0])
    first = client.jwt()
    now[0] += apns.JWT_SECONDS - 1
    assert client.jwt() == first
    now[0] += 2
    assert client.jwt() != first
    assert reads == [apns.KEYCHAIN_ITEM]


def test_drain_names_home_and_project_cards_by_session(tmp_path, monkeypatch):
    import json
    from ctb.config import ProjectConfig
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    (tmp_path / "topics.json").write_text(json.dumps({"home-session": {"thread": 7, "name": "Home topic"}}))
    (tmp_path / "topics-projects.json").write_text(json.dumps(
        {"project-session": {"chat": -200, "thread": 9, "name": "Project topic"}}))
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [{"device": "phone", "token": ident, "env": "sandbox"}])
    monkeypatch.setattr(apns, "_extras_available", lambda: True)
    seen = {}
    monkeypatch.setattr(apns, "APNSClient", lambda cfg, clock: type(
        "Client", (), {"send": lambda self, row, state, event: seen.setdefault(row["token"], state["topic"]) and 200})())
    monkeypatch.setattr(apns.time, "time", lambda: 100)
    # Card records hold the real home chat id, never 0; a moved topic still resolves by session.
    apns.notify({"id": "home-card", "kind": "progress", "session": "home-session", "chat": -100, "thread": 7})
    apns.notify({"id": "project-card", "kind": "progress", "session": "project-session", "chat": -200, "thread": 3})
    cfg = Config(chat_id=-100, apns_key_id="key", apns_team_id="team", apns_bundle_id="com.example.app",
                 projects=[ProjectConfig(name="P", chat_id=-200)])
    assert apns.drain(cfg, 100) == 2
    assert seen == {"home-card": "Home topic", "project-card": "Project topic"}


def test_long_cards_show_a_window_ending_at_the_current_step():
    twelve = ["s%d" % i for i in range(12)]
    whole = apns.content_state({"steps": twelve, "at": 9})
    assert whole["steps"] == twelve and whole["done"] == 9 and whole["percent"] is None  # as the app draws it
    steps = ["s%d" % i for i in range(20)]
    state = apns.content_state({"steps": steps, "at": 15})
    assert len(state["steps"]) == 12 and state["steps"][state["done"]] == "s15" and not state["finished"]
    assert state["percent"] == 75
    assert apns.content_state({"steps": steps, "at": 2})["steps"][0] == "s0"
    end = apns.content_state({"steps": steps, "at": 20})
    assert end["done"] == 12 and end["finished"] and end["steps"][-1] == "s19"


def test_trimming_keeps_the_current_step_and_the_callers_state(monkeypatch):
    posted = []
    http = type("HTTP", (), {"post": lambda self, url, **kw: posted.append(kw) or type("R", (), {"status_code": 200})()})()
    client = apns.APNSClient(Config(apns_bundle_id="com.example.app"), keychain=object(), http=http)
    monkeypatch.setattr(client, "jwt", lambda: "signed")
    monkeypatch.setattr(apns, "MAX_PAYLOAD_BYTES", 900)
    state = apns.content_state({"title": "T", "steps": ["\u05d0" * 60 for _ in range(12)], "at": 0})
    queued = [list(state["steps"])]
    client.send({"token": "a", "env": "sandbox"}, state, "update")
    sent = posted[0]["json"]["aps"]["content-state"]
    assert sent["done"] == 0 and 0 < len(sent["steps"]) < 12
    assert state["steps"] == queued[0]


def test_payload_is_trimmed_to_the_apns_limit(monkeypatch):
    import json
    posted = []
    http = type("HTTP", (), {"post": lambda self, url, **kw: posted.append(kw) or type("R", (), {"status_code": 200})()})()
    client = apns.APNSClient(Config(apns_bundle_id="com.example.app"), keychain=object(), http=http)
    monkeypatch.setattr(client, "jwt", lambda: "signed")
    huge = "א🌍\x01" * 200
    client.send({"token": "test-token", "env": "sandbox"},
                {"title": huge, "topic": huge, "steps": [huge] * 8, "at": 3}, "update")
    payload = posted[0]["json"]
    assert len(json.dumps(payload).encode()) <= apns.MAX_PAYLOAD_BYTES
    state = payload["aps"]["content-state"]
    assert "\x01" not in json.dumps(state, ensure_ascii=False) and 0 <= state["done"] <= len(state["steps"])


def test_bad_device_on_the_refreshed_retry_drops_the_token(monkeypatch):
    responses = iter((
        type("R", (), {"status_code": 403, "json": lambda self: {"reason": "ExpiredProviderToken"}})(),
        type("R", (), {"status_code": 400, "json": lambda self: {"reason": "BadDeviceToken"}})(),
    ))
    http = type("HTTP", (), {"post": lambda self, *a, **kw: next(responses)})()
    client = apns.APNSClient(Config(apns_bundle_id="com.example.app"), keychain=object(), http=http)
    monkeypatch.setattr(client, "jwt", lambda: "signed")
    dropped = []
    monkeypatch.setattr(apns, "drop", dropped.append)
    row = {"token": "test-token", "env": "sandbox"}
    assert client.send(row, {}, "update") == 400 and dropped == [row]


def test_three_consecutive_exceptions_stop_the_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [{"device": "phone", "token": ident, "env": "sandbox"}])
    monkeypatch.setattr(apns, "_extras_available", lambda: True)
    sent = []
    class FakeClient:
        def __init__(self, cfg, clock): pass
        def send(self, row, state, event):
            sent.append(row["token"])
            raise OSError("offline")
        def close(self): pass
    monkeypatch.setattr(apns, "APNSClient", FakeClient)
    monkeypatch.setattr(apns.time, "time", lambda: 100)
    apns.notify({"id": "card-a", "kind": "progress"})
    apns.notify({"id": "card-b", "kind": "progress"})
    apns.notify({"id": "card-c", "kind": "progress"})
    apns.notify({"id": "card-d", "kind": "progress"})
    cfg = Config(apns_key_id="key", apns_team_id="team", apns_bundle_id="com.example.app")
    apns.drain(cfg, 100)
    assert len(sent) == 3 and set(read_map(tmp_path / apns.PENDING_FILE)) == {
        "card-a", "card-b", "card-c", "card-d"}


def test_one_failing_record_does_not_block_later_update_and_log_is_private(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(apns, "_extras_available", lambda: True)
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [
        {"device": "private-device", "token": ident + "-private-token", "env": "sandbox"}])
    calls = []
    class FakeClient:
        def __init__(self, cfg, clock): pass
        def send(self, row, state, event):
            calls.append(row["token"])
            if row["token"].startswith("bad"):
                raise OSError("payload title must stay private")
            return 200
        def close(self): pass
    monkeypatch.setattr(apns, "APNSClient", FakeClient)
    monkeypatch.setattr(apns.time, "time", lambda: 100)
    apns.notify({"id": "bad-card", "kind": "progress", "title": "Private bad title"})
    apns.notify({"id": "good-card", "kind": "progress", "title": "Private good title"})
    cfg = Config(apns_key_id="key", apns_team_id="team", apns_bundle_id="com.example.app")
    assert apns.drain(cfg, 100) == 2
    assert calls == ["bad-card-private-token", "good-card-private-token"]
    assert set(read_map(tmp_path / apns.PENDING_FILE)) == {"bad-card"}
    err = capsys.readouterr().err
    assert "sent=1 retry=1 dropped=0" in err and "exceptions=OSError" in err
    for secret in ("private-device", "private-token", "Private bad title", "Private good title"):
        assert secret not in err


def test_a_256_byte_token_is_accepted_and_a_longer_one_is_not():
    # The app sends tokens up to 256 bytes (512 hex); APNs tokens have grown in length before.
    from ctb import apns
    assert apns.HEX_TOKEN.fullmatch("ab" * 256) and apns.MAX_TOKEN_HEX == 512
    assert not apns.HEX_TOKEN.fullmatch("ab" * 257)


def test_lock_screen_title_never_repeats_the_topic():
    from ctb.apns import content_state

    same = content_state({"title": "בית: סדר יום", "topic": "בית: סדר יום", "steps": ["a"], "at": 0})
    assert same["title"] == "בית: סדר יום" and same["topic"] == ""
    prefixed = content_state({"title": "בית: סדר יום: בדיקות", "topic": "בית: סדר יום", "steps": [], "at": 0})
    assert prefixed["title"] == "בדיקות" and prefixed["topic"] == "בית: סדר יום"
    other = content_state({"title": "בדיקות", "topic": "בית: סדר יום", "steps": [], "at": 0})
    assert other["title"] == "בדיקות" and other["topic"] == "בית: סדר יום"


def test_title_topic_match_is_exact_so_length_changing_case_folds_never_cut_the_title():
    from ctb.apns import content_state

    folded = content_state({"title": "SSSS: Work", "topic": "ßß", "steps": [], "at": 0})
    assert folded["title"] == "SSSS: Work" and folded["topic"] == "ßß"
    dashed = content_state({"title": "בית - בדיקות", "topic": "בית", "steps": [], "at": 0})
    assert dashed["title"] == "בדיקות"


def _fake_pem(body="QUJD" * 20):
    # Built at run time from parts: no key-shaped literal lives in the repository.
    label = "PRIVATE " + "KEY"
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "\n".join([f"-----BEGIN {label}-----", *lines, f"-----END {label}-----"]) + "\n"


def test_keychain_hex_output_of_a_multiline_key_is_decoded_before_signing():
    # `security find-generic-password -w` prints any value holding a line break as hex; a .p8 always has them.
    pem = _fake_pem()
    keychain = type("Keys", (), {"get": lambda self, item: pem.encode().hex()})()
    signed_with = []

    class Client(apns.APNSClient):
        @staticmethod
        def _sign(signing, key):
            signed_with.append(key)
            return b"\0" * 64

    Client(Config(apns_key_id="KEY", apns_team_id="TEAM"), keychain=keychain, http=object()).jwt()
    assert signed_with == [pem]


def test_key_text_forms_are_normalised_and_other_values_left_alone():
    pem = _fake_pem()
    body = "QUJD" * 20
    assert apns._pem_text(pem.encode().hex().upper()) == pem
    assert apns._pem_text(pem.strip()) == pem
    assert apns._pem_text(pem.strip().replace("\n", "\\n")) == pem
    assert apns._pem_text(body) == pem
    assert apns._pem_text(" \n") is None and apns._pem_text(None) is None
    assert apns._pem_text("fake-key") == "fake-key"
    # Hex that is not a PEM key stays as it is, so a wrong item fails clearly instead of signing garbage.
    assert apns._pem_text("deadbeef") == "deadbeef"


def test_real_p8_key_stored_as_hex_signs_a_verifiable_token():
    pytest = __import__("pytest")
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    keychain = type("Keys", (), {"get": lambda self, item: pem.encode().hex()})()
    token = apns.APNSClient(Config(apns_key_id="KEY", apns_team_id="TEAM"), keychain=keychain, http=object()).jwt()
    head, claims, sig = token.split(".")
    raw = __import__("base64").urlsafe_b64decode(sig + "==")
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    private.public_key().verify(der, (head + "." + claims).encode(), ec.ECDSA(hashes.SHA256()))


def test_unreadable_key_is_reported_per_try_and_delivery_resumes_on_its_own(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(apns, "_extras_available", lambda: True)
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [
        {"device": "phone", "token": "private-token", "env": "production"}])
    monkeypatch.setattr(apns.time, "time", lambda: 100)
    readable = [False]
    sent = []

    class Client:
        def __init__(self, cfg, clock): pass
        def validate(self):
            if not readable[0]:
                raise ValueError("Could not deserialize key data")
        def send(self, row, state, event):
            sent.append(event)
            return 200
        def close(self): pass

    monkeypatch.setattr(apns, "APNSClient", Client)
    apns.notify({"id": "card", "kind": "progress", "percent": 10})
    cfg = Config(apns_key_id="key", apns_team_id="team", apns_bundle_id="com.example.app")
    assert apns.drain(cfg, 100) == 0
    err = capsys.readouterr().err
    assert "client could not be built (ValueError)" in err and "not a readable .p8" in err
    assert "1 updates wait" in err and "private-token" not in err and "deserialize" not in err
    assert apns.drain(cfg, 101) == 0 and capsys.readouterr().err == ""  # deferred quietly
    readable[0] = True
    # No restart: the heartbeat's next pass after the retry window builds a client and delivers.
    assert apns.drain(cfg, 100 + apns.CLIENT_FAILURE_RETRY_SECONDS + 1) == 1
    assert sent == ["update"] and read_map(tmp_path / apns.PENDING_FILE) == {}


def test_a_discarded_client_never_stops_later_drains(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(apns, "_extras_available", lambda: True)
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [
        {"device": "phone", "token": "private-token", "env": "production"}])
    monkeypatch.setattr(apns.time, "time", lambda: 100)
    made, closed = [], []
    outcomes = iter((OSError("stalled stream"), 200, 200))

    class Client:
        def __init__(self, cfg, clock): made.append(self)
        def send(self, row, state, event):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        def close(self): closed.append(self)

    monkeypatch.setattr(apns, "APNSClient", Client)
    cfg = Config(apns_key_id="key", apns_team_id="team", apns_bundle_id="com.example.app")
    apns.notify({"id": "card", "kind": "progress", "percent": 10})
    assert apns.drain(cfg, 100) == 1
    err = capsys.readouterr().err
    assert "APNs push failed card=card event=update error=OSError" in err and "private-token" not in err
    assert closed == [made[0]] and apns._CLIENT is None
    record = read_map(tmp_path / apns.PENDING_FILE)["card"]
    assert record["attempts"] == 1
    assert apns.drain(cfg, record["next_at"]) == 1
    assert len(made) == 2 and read_map(tmp_path / apns.PENDING_FILE) == {}
    apns.notify({"id": "card", "kind": "progress", "percent": 20})
    assert apns.drain(cfg, record["next_at"] + apns.THROTTLE_SECONDS) == 1
    assert len(made) == 2  # the rebuilt client is reused


def test_refused_send_logs_status_and_apns_reason_without_the_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(apns, "_extras_available", lambda: True)
    monkeypatch.setattr(apns, "tokens_for", lambda ident: [
        {"device": "private-device", "token": "private-token", "env": "production"}])
    monkeypatch.setattr(apns.time, "time", lambda: 100)
    response = type("R", (), {"status_code": 400, "json": lambda self: {"reason": "TopicDisallowed"}})()
    http = type("HTTP", (), {"post": lambda self, *a, **kw: response})()

    class Client(apns.APNSClient):
        def __init__(self, cfg, clock):
            super().__init__(cfg, keychain=object(), http=http, clock=clock)
        def jwt(self):
            return "signed"

    monkeypatch.setattr(apns, "APNSClient", Client)
    apns.notify({"id": "card", "kind": "progress", "title": "Private title"})
    cfg = Config(apns_key_id="key", apns_team_id="team", apns_bundle_id="com.example.app")
    assert apns.drain(cfg, 100) == 1
    err = capsys.readouterr().err
    assert "APNs push failed card=card event=update status=400 reason=TopicDisallowed" in err
    for secret in ("private-device", "private-token", "Private title", "signed"):
        assert secret not in err
