"""Exercise the real HTTP parser/handler without opening a listening socket.

Keep the live-server integration tests in test_cockpit_apps.py; these security regressions also run
in restricted environments where binding loopback is prohibited. Only transport is replaced.
"""
import io
import json
import time
from email.parser import BytesParser
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN
from ctb import panel
from ctb.config import Config
from test_cockpit_apps import make_app, media_root, token
from test_panel import OWNER, signed


class MemoryConnection:
    def __init__(self, request):
        self.request = request
        self.response = bytearray()

    def makefile(self, *args):
        return io.BytesIO(self.request)

    def sendall(self, data):
        self.response.extend(data)

    def settimeout(self, timeout):
        pass

    def shutdown(self, how):
        pass


def request(method, path, headers=None, body=None):
    raw = json.dumps({} if body is None else body).encode() if method == "POST" else b""
    headers = {"Host": "panel.example", "Content-Length": str(len(raw)), **(headers or {})}
    head = f"{method} {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
    conn = MemoryConnection(head.encode() + raw)
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    srv = SimpleNamespace(bot_token=GOOD_TOKEN, owner_id=OWNER, cfg=cfg, lang="he", translator=None,
                          youtube_view=lambda: {})
    panel.PanelHandler(conn, ("127.0.0.1", 1), srv)
    head, _, data = bytes(conn.response).partition(b"\r\n\r\n")
    status, _, fields = head.partition(b"\r\n")
    return int(status.split()[1]), BytesParser().parsebytes(fields), data


def mint(init=None):
    status, _, body = request("POST", "/api/app-token", {panel.INIT_HEADER: signed() if init is None else init},
                              {"app": "sample"})
    assert status == 200
    return json.loads(body)


def test_real_handler_only_mints_for_the_owner_and_never_extends_the_action_login():
    make_app()
    auth_date = int(time.time()) - panel.DECIDE_MAX_AGE + 60
    issued = mint(signed(auth_date=auth_date))
    assert issued["expires"] == auth_date + panel.DECIDE_MAX_AGE
    for bad in ("", token(), signed(tamper=True), signed(user_id=OWNER + 1),
                signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 1)):
        assert request("POST", "/api/app-token", {panel.INIT_HEADER: bad}, {"app": "sample"})[0] == 403


def test_real_handler_app_page_media_and_api_accept_only_the_matching_app_token():
    make_app()
    make_app("other")
    for name in ("sample", "other"):
        (media_root(name) / "private.png").write_bytes(b"private")
    scoped = mint()["token"]
    for path in ("/app/sample/", "/app/sample/media/private.png"):
        assert request("GET", path + "?auth=" + scoped)[0] == 200
        assert request("GET", path, {panel.APP_TOKEN_HEADER: scoped})[0] == 200
        assert request("GET", path, {panel.INIT_HEADER: signed()})[0] == 403
        assert request("GET", path, {panel.APP_TOKEN_HEADER: signed()})[0] == 403
    assert request("POST", "/app/sample/api", {panel.APP_TOKEN_HEADER: scoped}, {"mode": "echo"})[0] == 200
    assert request("POST", "/app/sample/api", {panel.INIT_HEADER: signed()})[0] == 403
    for path in ("/app/other/", "/app/other/media/private.png"):
        assert request("GET", path + "?auth=" + scoped)[0] == 403
        assert request("GET", path, {panel.APP_TOKEN_HEADER: scoped})[0] == 403
    assert request("POST", "/app/other/api", {panel.APP_TOKEN_HEADER: scoped})[0] == 403


def test_real_handler_rejects_app_tokens_on_all_owner_apis():
    make_app()
    scoped = mint()["token"]
    for path in panel.ROUTES:
        assert request("POST", path, {panel.INIT_HEADER: scoped})[0] == 403, path
        assert request("POST", path, {panel.APP_TOKEN_HEADER: scoped})[0] == 403, path
    assert request("POST", "/api/state", {panel.INIT_HEADER: signed()})[0] == 200


@pytest.mark.parametrize("bad", [
    lambda: token(now=time.time() - panel.APP_TOKEN_TTL - 1),
    lambda: token(now=time.time() + panel.APP_TOKEN_TTL),
    lambda: token()[:-64] + "0" * 64,
    lambda: token("other").replace(".other.", ".sample."),
    lambda: panel.app_token(GOOD_TOKEN, OWNER + 1, "sample"),
    lambda: panel.app_token("wrong-test-key", OWNER, "sample"),
])
def test_real_handler_rejects_expired_tampered_and_wrong_owner_or_key_tokens(bad):
    make_app()
    (media_root() / "private.png").write_bytes(b"private")
    for path in ("/app/sample/", "/app/sample/media/private.png"):
        assert request("GET", path + "?auth=" + bad())[0] == 403
    assert request("POST", "/app/sample/api", {panel.APP_TOKEN_HEADER: bad()})[0] == 403
