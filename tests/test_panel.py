"""The read-only control panel: initData validation, the HTTP surface and the state it returns."""
import hashlib
import hmac
import http.client
import json
import threading
import time
from urllib.parse import urlencode

import pytest

from conftest import GOOD_TOKEN
from ctb import panel
from ctb.config import Config, state_dir, write_private

OWNER = 7


def signed(token=GOOD_TOKEN, user_id=OWNER, auth_date=None, tamper=False, extra=None):
    fields = {"auth_date": str(int(time.time()) if auth_date is None else auth_date), "query_id": "AAH",
              "user": json.dumps({"id": user_id, "first_name": "Owner"})}
    fields.update(extra or {})
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        fields["hash"] = "0" * 64
    return urlencode(fields)


@pytest.fixture
def server():
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    srv = panel.PanelServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method="POST", path="/api/state", init=None, body=b"{}", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    h = dict(headers or {})
    if init is not None:
        h[panel.INIT_HEADER] = init
    conn.request(method, path, body=body if method == "POST" else None, headers=h)
    r = conn.getresponse()
    data = r.read()
    out = (r.status, dict(r.getheaders()), data)
    conn.close()
    return out


def test_valid_init_data_accepted(server):
    code, hdrs, data = call(server, init=signed())
    assert code == 200
    body = json.loads(data)
    assert body["ok"] is True and body["sessions"] == [] and body["quota"] == []


@pytest.mark.parametrize("init", [
    lambda: signed(tamper=True),
    lambda: signed(auth_date=int(time.time()) - 3601),
    lambda: signed(auth_date=int(time.time()) + 600),
    lambda: signed(user_id=OWNER + 1),
    lambda: signed(token="654321:" + "B" * 35),
    lambda: "",
    lambda: "garbage",
])
def test_bad_init_data_gets_403_and_no_data(server, init):
    code, _, data = call(server, init=init())
    assert code == 403
    assert json.loads(data) == {"error": "forbidden"}


def test_missing_header_is_403(server):
    code, _, data = call(server)
    assert code == 403 and b"sessions" not in data


def test_duplicate_keys_rejected():
    assert not panel.validate_init_data(signed() + "&auth_date=1", GOOD_TOKEN, OWNER)


def test_body_size_limit(server):
    code, _, data = call(server, init=signed(), body=b"x" * (panel.MAX_BODY + 1))
    assert code == 413 and b"sessions" not in data


def test_only_two_routes(server):
    assert call(server, "GET", "/api/state")[0] == 404
    assert call(server, "GET", "/etc/passwd")[0] == 404
    assert call(server, "POST", "/")[0] == 404
    assert call(server, "PUT", "/")[0] == 405
    assert call(server, "DELETE", "/api/state")[0] == 405


def test_page_has_csp_and_embeds(server):
    code, hdrs, data = call(server, "GET", "/")
    assert code == 200
    csp = hdrs["Content-Security-Policy"]
    assert "frame-ancestors https://web.telegram.org https://*.telegram.org" in csp
    assert "script-src https://telegram.org/js/telegram-web-app.js 'nonce-" in csp
    assert "X-Frame-Options" not in hdrs and "Access-Control-Allow-Origin" not in hdrs
    html = data.decode()
    assert 'dir="rtl"' in html and "telegram-web-app.js" in html
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert html.count(f'nonce="{nonce}"') == 2
    assert GOOD_TOKEN not in html


def test_english_page_is_ltr():
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="en")
    assert b'dir="ltr"' in panel.render_page(cfg.ui_language, "n")


def test_state_shape_from_state_dirs(server, tmp_path):
    topics = {"abcd1234-ffff": {"thread": 11, "name": "Pantry deploy"},
              "zzzz9999-aaaa": {"thread": 12, "name": "Old one"}}
    write_private(state_dir() / "topics.json", json.dumps(topics))
    write_private(state_dir() / "claude-usage-main.json", json.dumps(
        {"five_hour": 12, "weekly": 40, "five_hour_resets": "2030-01-01T10:00:00Z",
         "weekly_resets": "2030-01-05T10:00:00Z", "at": "2030-01-01T05:00:00Z"}))
    code, _, data = call(server, init=signed())
    body = json.loads(data)
    assert code == 200
    assert {s["id"] for s in body["sessions"]} == {"abcd1234", "zzzz9999"}
    s = body["sessions"][0]
    assert set(s) == {"id", "name", "state", "age", "context", "compact", "model", "account", "bg", "now", "summary", "url", "here",
                      "waiting", "folder", "branch", "left", "usage"}
    assert "topic" not in body
    assert s["state"] in ("working", "idle", "ended")
    q = body["quota"][0]
    assert q["account"] == "main" and "five_hour" not in q and q["weekly"] == 40  # weekly only
    assert q["banked"] is None  # no banked-reset count in the record: nothing is guessed


def test_no_secrets_in_responses_or_logs(server, capsys):
    outputs = [call(server, init=signed())[2], call(server, init=signed(tamper=True))[2], call(server, "GET", "/")[2]]
    init = signed()
    call(server, init=init)
    seen = capsys.readouterr()
    for blob in outputs:
        assert GOOD_TOKEN.encode() not in blob
    for text in (seen.out, seen.err):
        assert GOOD_TOKEN not in text and init not in text


def raw(srv, data: bytes) -> bytes:
    import socket
    s = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5)
    s.sendall(data)
    out = b""
    while chunk := s.recv(65536):
        out += chunk
    s.close()
    return out


def test_one_reply_per_connection_even_with_smuggled_bytes(server):
    # Behind a reusing proxy, leftover bytes must never be served as a second request.
    smuggled = b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"
    out = raw(server, b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(smuggled) + smuggled)
    assert out.count(b"HTTP/1.") == 1 and b"Connection: close" in out


def test_chunked_body_refused(server):
    out = raw(server, b"POST /api/state HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
                      b"0\r\n\r\nGET / HTTP/1.1\r\nHost: x\r\n\r\n")
    assert out.startswith(b"HTTP/1.0 400") and out.count(b"HTTP/1.") == 1


def test_head_has_no_body_and_errors_are_json(server):
    out = raw(server, b"HEAD / HTTP/1.1\r\nHost: x\r\n\r\n")
    assert b" 405 " in out.split(b"\r\n")[0] and out.endswith(b"\r\n\r\n")
    bad = raw(server, b"BOGUS / HTTP/1.1\r\nHost: x\r\n\r\n")
    assert b"application/json" in bad and b"Python" not in bad and b"no-store" in bad


def test_page_served_with_telegram_start_param(server):
    code, hdrs, data = call(server, method="GET", path="/?tgWebAppStartParam=x")
    assert code == 200 and "script-src 'self'" not in hdrs["Content-Security-Policy"]


def test_connections_beyond_limit_are_closed(server):
    import socket
    held = [socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
            for _ in range(panel.MAX_CONNECTIONS)]
    time.sleep(0.3)
    try:
        try:
            refused = raw(server, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        except ConnectionResetError:
            refused = b""
        assert refused == b""
    finally:
        for s in held:
            s.close()
    time.sleep(0.3)
    assert raw(server, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n").startswith(b"HTTP/1.0 200")


def test_slow_sender_is_cut_at_the_deadline(server, monkeypatch):
    import socket
    monkeypatch.setattr(panel, "DEADLINE", 0.5)
    s = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
    s.sendall(b"GET / HTTP/1.1\r\n")
    start = time.time()
    try:
        assert s.recv(1024) == b""
    except ConnectionResetError:
        pass
    assert time.time() - start < 3
    s.close()


def test_topic_button_opens_that_topics_view(server):
    from ctb import board, decisions
    topics = {"abcd1234-ffff": {"thread": 11, "name": "Pantry deploy"},
              "zzzz9999-aaaa": {"thread": 12, "name": "Other"}}
    write_private(state_dir() / "topics.json", json.dumps(topics))
    chat = server.cfg.owner_chat_id
    write_private(state_dir() / board.STATE_FILE, json.dumps({f"{chat}:11": {"id": 0, "items": [
        {"k": "remind", "i": 1, "s": "Approve the banner", "at": 5},
        {"k": "check", "i": 2, "s": "Ship it", "x": True, "at": 6},
        {"k": "link", "i": 3, "s": "Preview", "u": "https://example.com/p", "at": 7}]}}))
    decisions.create("abcd1234-ffff", chat, 11, "Merge now?")
    decisions.create("zzzz9999-aaaa", chat, 12, "Elsewhere?")
    code, _, data = call(server, init=signed(extra={"start_param": panel.topic_param(chat, 11)}))
    body = json.loads(data)
    assert code == 200 and body["topic"]["name"] == "Pantry deploy"
    b = body["topic"]["board"]
    assert b["remind"] == [{"text": "Approve the banner", "due": ""}]
    assert b["check"] == [{"text": "Ship it", "done": True}]
    assert b["link"] == [{"text": "Preview", "url": "https://example.com/p"}]
    assert {d["title"]: d["here"] for d in body["decisions"]} == {"Merge now?": True, "Elsewhere?": False}
    assert [s["id"] for s in body["sessions"] if s["here"]] == ["abcd1234"]
    assert "11" not in json.dumps(body["topic"])  # ids stay on the server


def test_unknown_or_malformed_start_param_gives_the_general_view(server):
    for param in ("tdeadbeef", "panel", "t../../x"):
        code, _, data = call(server, init=signed(extra={"start_param": param}))
        assert code == 200 and "topic" not in json.loads(data)


def test_the_topics_own_session_is_listed_even_past_the_session_cap(monkeypatch):
    """Review of #136: the list was cut at MAX_SESSIONS before the topic's own session was added, so a
    topic's panel could say its live session was gone."""
    from ctb import sessions_menu
    E = sessions_menu.Entry
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    write_private(state_dir() / "topics.json", json.dumps(
        {f"s{i}-session": {"thread": 10 + i, "name": f"S{i}"} for i in range(4)}))
    monkeypatch.setattr(panel, "MAX_SESSIONS", 2)
    monkeypatch.setattr(panel, "collect", lambda topics, now=None: [
        E(f"s{i}-session", f"S{i}", 10 + i, 5.0, "idle", False) for i in range(4)])
    body = panel.build_state(cfg, start=panel.topic_param(OWNER, 13))
    assert [s["id"] for s in body["sessions"]] == ["s0-sessi", "s3-sessi"]
    assert [s["here"] for s in body["sessions"]] == [False, True]
    assert len(panel.build_state(cfg)["sessions"]) == 2  # without a topic: the first ones, as before


def test_a_sessions_folder_and_branch_come_from_its_transcript(tmp_path, monkeypatch):
    import os
    from ctb import status
    monkeypatch.setenv("HOME", str(tmp_path))
    t = tmp_path / "t.jsonl"
    t.write_text('not json\n' + json.dumps({"type": "user", "cwd": str(tmp_path / "work" / "shop"), "gitBranch": "feat/cart"}) + "\n")
    assert status.workplace(str(t)) == (os.path.join("~", "work", "shop"), "feat/cart")
    t.write_text(json.dumps({"cwd": "/srv/app‮"}) + "\n")
    assert status.workplace(str(t)) == ("/srv/app", "")              # no invisible characters, no branch said
    assert status.workplace(None) == ("", "") and status.workplace(str(tmp_path / "missing")) == ("", "")
