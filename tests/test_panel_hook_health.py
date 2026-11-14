import json
import time
import io
from types import SimpleNamespace

import pytest

from ctb import hook_health, install, session_reload, panel
from ctb.config import Config
from conftest import GOOD_TOKEN
from test_panel import signed


@pytest.fixture
def server():
    # Exercise the real HTTP routing, body limits and signature guard without opening a port.
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=7, target_language="he")
    return SimpleNamespace(cfg=cfg, bot_token=GOOD_TOKEN, owner_id=7, translator=None, youtube_view=lambda: {})


def call(server, path="/api/state", init=None, body=b"{}"):
    handler = object.__new__(panel.PanelHandler)
    handler.server, handler.path = server, path
    handler.headers = {"Content-Length": str(len(body))}
    if init is not None:
        handler.headers[panel.INIT_HEADER] = init
    handler.rfile = io.BytesIO(body)
    replies = []
    handler._json = lambda code, value: replies.append((code, {}, json.dumps(value).encode()))
    handler.do_POST()
    return replies[0]


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    root = tmp_path / "claude-panel-test"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    path = root / "settings.json"
    path.write_text('{"model": "preserve"}')
    return path


@pytest.mark.parametrize("route", ["/api/hooks-restore", "/api/sessions-reload"])
@pytest.mark.parametrize("init", [None, lambda: signed(user_id=999), lambda: signed(tamper=True),
                                 lambda: signed(auth_date=int(time.time()) - 901)])
def test_owner_only_fresh_signed_action(server, config_dir, monkeypatch, route, init):
    before = config_dir.read_bytes()
    calls = []
    monkeypatch.setattr(session_reload, "start", lambda cfg: calls.append("reload"))
    code, _, _ = call(server, path=route, init=init() if init else None)
    assert code == 403 and not calls and config_dir.read_bytes() == before


def test_owner_restores_hooks_and_card_offers_reload(server, config_dir):
    assert call(server, path="/api/hooks-restore", init=signed())[0] == 200
    value = json.loads(config_dir.read_text())
    assert value == {"model": "preserve", "hooks": install.desired_hooks()}
    code, _, raw = call(server, init=signed())
    assert code == 200
    assert json.loads(raw)["hook_health"]["restored"]
    assert json.loads(raw)["hook_health"]["ok"]


def test_owner_starts_reload_without_holding_http(server, config_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(session_reload, "start", lambda cfg: calls.append(cfg))
    assert call(server, path="/api/sessions-reload", init=signed())[0] == 202
    assert len(calls) == 1


@pytest.mark.parametrize("route", ["/api/hooks-restore", "/api/sessions-reload"])
@pytest.mark.parametrize("body", [b'{"session": "other"}', b'[]', b'null', b'{broken'])
def test_actions_reject_unexpected_input(server, config_dir, monkeypatch, route, body):
    calls = []
    monkeypatch.setattr(session_reload, "start", lambda cfg: calls.append(cfg))
    before = config_dir.read_bytes()
    assert call(server, path=route, init=signed(), body=body)[0] == 400
    assert config_dir.read_bytes() == before and not calls


def test_background_reload_result_is_visible(config_dir, monkeypatch):
    # Synchronous stand-in for the worker keeps the test deterministic and leaves no thread.
    class Worker:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr("threading.Thread", Worker)
    monkeypatch.setattr(session_reload, "reload_sessions", lambda cfg: {"reloaded": ["one"], "skipped": [], "would_reload": []})
    session_reload.start(None)
    result = hook_health.view()["reload"]
    assert result["reloaded"] == ["one"] and not result["running"]
