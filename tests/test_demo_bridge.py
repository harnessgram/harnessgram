import http.client
import json
import threading

import pytest

from ctb import cli, demo


def request(server, path, body=None, token=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    headers = {"Content-Type": "application/json", **(headers or {})}
    if token:
        headers["Authorization"] = "Bearer " + token
    connection.request("POST", path, json.dumps(body or {}).encode(), headers)
    response = connection.getresponse()
    data = json.loads(response.read())
    connection.close()
    return response.status, data


def running(store):
    server = demo.DemoHTTPServer(("127.0.0.1", 0), demo.handler_for(store))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    return server, thread


CODE = "REVIEW-CODE-1234"
SECRET = "separate-signing-secret-32-bytes-long"


def test_pairing_is_stateless_multi_device_restart_safe_and_uses_signature():
    store = demo.DemoStore(CODE, SECRET, clock=lambda: 100)
    assert store.pair("wrong") is None
    token = store.pair(CODE, "device-one")
    assert token.startswith("hgw_100.") and store.authorized("Bearer " + token)
    assert store.pair(CODE, "device-two") == token
    assert demo.DemoStore(CODE, SECRET, clock=lambda: 100).authorized("Bearer " + token)
    assert not demo.DemoStore(CODE, "different-signing-secret-32-bytes!", clock=lambda: 100).authorized("Bearer " + token)


def test_token_window_and_startup_credential_requirements():
    now = [9.0]
    store = demo.DemoStore(CODE, SECRET, clock=lambda: now[0], token_window=10)
    token = store.pair(CODE)
    now[0] = 10
    assert store.authorized("Bearer " + token)
    now[0] = 19
    assert store.authorized("Bearer " + token)
    now[0] = 19.01
    assert not store.authorized("Bearer " + token)
    now[0] = 100
    future = store.pair(CODE, "future-source")
    now[0] = 39
    assert not store.authorized("Bearer " + future)
    with pytest.raises(ValueError):
        demo.DemoStore("short", SECRET)
    with pytest.raises(ValueError):
        demo.DemoStore(CODE, "too-short")
    with pytest.raises(ValueError):
        demo.DemoStore(CODE, SECRET, token_window=True)


def test_pair_throttle_is_per_client_and_bounded():
    now = [100]
    store = demo.DemoStore(CODE, SECRET, clock=lambda: now[0])
    for _ in range(5):
        assert store.pair("wrong", "busy-client") is None
    assert store.pair(CODE, "busy-client") is None
    assert store.pair(CODE, "other-client")
    for number in range(demo.MAX_THROTTLE_CLIENTS + 1):
        now[0] += demo.PAIR_PERIOD
        store.pair("wrong", f"client-{number}")
    assert len(store._pair_attempts) == demo.MAX_THROTTLE_CLIENTS


def test_pair_global_throttle_and_http_429():
    store = demo.DemoStore(CODE, SECRET, clock=lambda: 100)
    for number in range(demo.GLOBAL_PAIR_LIMIT):
        assert store.pair("wrong", f"client-{number}") is None
    assert store.pair_with_status(CODE, "new-client") == (None, True)
    server, thread = running(store)
    try:
        assert request(server, "/api/demo-pair", {"code": CODE})[0] == 429
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_forwarded_client_is_used_only_for_trusted_proxy():
    store = demo.DemoStore(CODE, SECRET, clock=lambda: 100)
    server = demo.DemoHTTPServer(("127.0.0.1", 0), demo.handler_for(store, {"127.0.0.1"}))
    thread = threading.Thread(target=server.serve_forever); thread.start()
    try:
        for _ in range(demo.PAIR_LIMIT):
            assert request(server, "/api/demo-pair", {"code": "bad"},
                           headers={"X-Forwarded-For": "198.51.100.1"})[0] == 403
        assert request(server, "/api/demo-pair", {"code": CODE},
                       headers={"X-Forwarded-For": "198.51.100.1"})[0] == 429
        assert request(server, "/api/demo-pair", {"code": CODE},
                       headers={"X-Forwarded-For": "198.51.100.2"})[0] == 200
    finally:
        server.shutdown(); server.server_close(); thread.join()

    untrusted_store = demo.DemoStore(CODE, SECRET, clock=lambda: 100)
    server = demo.DemoHTTPServer(("127.0.0.1", 0), demo.handler_for(untrusted_store))
    thread = threading.Thread(target=server.serve_forever); thread.start()
    try:
        for number in range(demo.PAIR_LIMIT):
            assert request(server, "/api/demo-pair", {"code": "bad"},
                           headers={"X-Forwarded-For": f"198.51.100.{number}"})[0] == 403
        assert request(server, "/api/demo-pair", {"code": CODE},
                       headers={"X-Forwarded-For": "198.51.100.99"})[0] == 429
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_forwarded_client_ignores_forged_leftmost_address():
    store = demo.DemoStore(CODE, SECRET, clock=lambda: 100)
    server = demo.DemoHTTPServer(("127.0.0.1", 0), demo.handler_for(store, {"127.0.0.1"}))
    thread = threading.Thread(target=server.serve_forever); thread.start()
    try:
        for number in range(demo.PAIR_LIMIT):
            assert request(server, "/api/demo-pair", {"code": "bad"}, headers={
                "X-Forwarded-For": f"198.51.100.{number + 10}, 203.0.113.1"
            })[0] == 403
        assert request(server, "/api/demo-pair", {"code": CODE}, headers={
            "X-Forwarded-For": "198.51.100.99, 203.0.113.1"
        })[0] == 429
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_forwarded_client_walks_back_through_trusted_proxy_chain():
    store = demo.DemoStore(CODE, SECRET, clock=lambda: 100)
    trusted = {"127.0.0.1", "192.0.2.10", "192.0.2.11"}
    server = demo.DemoHTTPServer(("127.0.0.1", 0), demo.handler_for(store, trusted))
    thread = threading.Thread(target=server.serve_forever); thread.start()
    try:
        for _ in range(demo.PAIR_LIMIT):
            assert request(server, "/api/demo-pair", {"code": "bad"}, headers={
                "X-Forwarded-For": "203.0.113.5, 192.0.2.10, 192.0.2.11"
            })[0] == 403
        assert request(server, "/api/demo-pair", {"code": CODE}, headers={
            "X-Forwarded-For": "203.0.113.5, 192.0.2.10, 192.0.2.11"
        })[0] == 429
        assert request(server, "/api/demo-pair", {"code": CODE}, headers={
            "X-Forwarded-For": "203.0.113.6, 192.0.2.10, 192.0.2.11"
        })[0] == 200
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_api_fixture_decisions_reset_and_sensitive_actions_refuse():
    now = [100.0]
    store = demo.DemoStore(CODE, SECRET, clock=lambda: now[0])
    token = store.pair(CODE)
    server, thread = running(store)
    try:
        status, state = request(server, "/api/state", token=token)
        assert status == 200 and {row["state"] for row in state["sessions"]} == {"working", "waiting", "idle"}
        assert state["quota"] and state["radio"]["on"]
        assert all(0 <= card["at"] < len(card["steps"]) for card in state["cards"])
        assert request(server, "/api/decide", {"id": "demo-sensitive-1", "choice": "yes"}, token)[0] == 403
        assert request(server, "/api/decide", {"id": "demo-decision-1", "choice": "yes"}, token)[0] == 200
        assert len(request(server, "/api/widget-state", token=token)[1]["decisions"]) == 1
        now[0] = 3700
        assert len(request(server, "/api/state", token=token)[1]["decisions"]) == 2
        assert request(server, "/api/radio-player", token=token)[1]["title"]
        assert request(server, "/api/live-activity-token", {"card_id": "demo-card-1"}, token)[1]["registered"] is False
        assert request(server, "/api/radio", {"on": False}, token)[0] == 403
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_unauthorized_and_bad_requests_fail_closed():
    store = demo.DemoStore(CODE, SECRET)
    server, thread = running(store)
    try:
        assert request(server, "/api/state")[0] == 403
        assert request(server, "/api/demo-pair", {"code": "bad"})[0] == 403
        assert request(server, "/api/device-pair", {"code": CODE})[0] == 200
        assert request(server, "/api/anything")[0] == 403
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_non_ascii_credentials_fail_closed_and_responses_are_hardened():
    store = demo.DemoStore(CODE, SECRET)
    assert store.pair("\N{SNOWMAN}") is None
    assert not store.authorized("Bearer \N{SNOWMAN}")
    server, thread = running(store)
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        response.read()
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert response.getheader("Server") == "DemoServer "
        connection.close()
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_recursive_json_is_rejected(monkeypatch):
    store = demo.DemoStore(CODE, SECRET)
    monkeypatch.setattr(demo.json, "loads", lambda _body: (_ for _ in ()).throw(RecursionError()))
    server, thread = running(store)
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("POST", "/api/state", b"{}", {"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 400
        response.read()
        connection.close()
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_server_limits_connections_and_handlers_have_timeout():
    assert demo.handler_for(demo.DemoStore(CODE, SECRET)).timeout == 10
    server = demo.DemoHTTPServer(("127.0.0.1", 0), demo.handler_for(demo.DemoStore(CODE, SECRET)), max_connections=1)
    assert server.daemon_threads is True
    assert server._connections.acquire(blocking=False)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    connection.request("GET", "/healthz")
    assert connection.getresponse().status == 503
    connection.close()
    thread.join()
    server._connections.release()
    server.server_close()


def test_cli_demo_never_loads_real_config(monkeypatch):
    called = {}
    monkeypatch.setattr(demo, "serve", lambda host, port: called.update(host=host, port=port))
    monkeypatch.setattr(cli.C, "load", lambda: (_ for _ in ()).throw(AssertionError("config loaded")))
    assert cli.main(["demo", "serve", "--host", "0.0.0.0", "--port", "9090"]) == 0
    assert called == {"host": "0.0.0.0", "port": 9090}


def test_serve_reads_configurable_token_window_without_real_config(monkeypatch):
    captured = {}
    monkeypatch.setenv("CTB_DEMO_CODE", CODE)
    monkeypatch.setenv("CTB_DEMO_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("CTB_DEMO_TOKEN_WINDOW_SECONDS", "600")
    monkeypatch.setattr(demo, "DemoStore", lambda code, secret, token_window: captured.update(
        code=code, secret=secret, token_window=token_window))
    monkeypatch.setattr(demo, "DemoHTTPServer", lambda *_args: type(
        "FakeServer", (), {"serve_forever": lambda self: captured.update(served=True)})())
    demo.serve("127.0.0.1", 8080)
    assert captured == {"code": CODE, "secret": SECRET, "token_window": 600, "served": True}
