import json

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import accounts, autopilot, dashboard, flood, identity, recover, sessionlog
from ctb.config import Config, state_dir
from ctb.telegram import TelegramError


@pytest.fixture
def setup_dashboard(monkeypatch):
    monkeypatch.setattr(dashboard, "text", lambda *a, **k: "new text")
    monkeypatch.setattr(dashboard, "stuck_check", lambda *a, **k: None)
    monkeypatch.setattr(flood, "optional_call", lambda bot, method, params, **kw: bot.call(method, params))
    for module, names in ((recover, ("limit_check",)), (sessionlog, ("auto",)),
                          (autopilot, ("run_autopilot",)), (identity, ("check",)),
                          (accounts, ("refresh_usage", "check_default_account", "rebalance"))):
        for name in names:
            monkeypatch.setattr(module, name, lambda *a, **k: None)
    path = state_dir() / "dashboard.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": 55, "text": "old text", "pinned": True}))
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7), path


class EditErrorBot(FakeBot):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def call(self, method, params=None, timeout=15):
        if method == "editMessageText":
            self.calls.append((method, params))
            raise self.error
        return super().call(method, params, timeout)


@pytest.mark.parametrize("description,methods", [
    ("Bad Request: message to edit not found", ["editMessageText", "sendMessage", "pinChatMessage"]),
    ("Bad Request: MESSAGE_ID_INVALID", ["editMessageText", "sendMessage", "pinChatMessage"]),
    ("Bad Request: message can't be edited", ["editMessageText", "unpinChatMessage", "sendMessage", "pinChatMessage"]),
])
def test_replaces_only_confirmed_unusable_messages(setup_dashboard, description, methods):
    cfg, path = setup_dashboard
    bot = EditErrorBot(TelegramError(description, 400))
    assert dashboard.run(cfg, bot=bot) == 0
    assert [method for method, _ in bot.calls] == methods
    sent = bot.methods("sendMessage")[0]["message_id"]
    assert bot.methods("pinChatMessage")[0]["message_id"] == sent
    assert json.loads(path.read_text()) == {"id": sent, "text": "new text", "pinned": True}
    if bot.methods("unpinChatMessage"):
        assert bot.methods("unpinChatMessage")[0]["message_id"] == 55


@pytest.mark.parametrize("description,code,expected", [
    ("timeout", None, 1),
    ("message can't be edited", 500, 1),
    ("Bad Request: message is not modified", 400, 0),
])
def test_other_edit_errors_never_create_a_duplicate(setup_dashboard, description, code, expected):
    cfg, path = setup_dashboard
    bot = EditErrorBot(TelegramError(description, code))
    assert dashboard.run(cfg, bot=bot) == expected
    assert [method for method, _ in bot.calls] == ["editMessageText"]
    assert json.loads(path.read_text())["id"] == 55


def test_unpin_failure_does_not_create_a_replacement(setup_dashboard):
    cfg, path = setup_dashboard
    bot = EditErrorBot(TelegramError("Bad Request: message can't be edited", 400))
    bot.fail["unpinChatMessage"] = "timeout"
    assert dashboard.run(cfg, bot=bot) == 1
    assert [method for method, _ in bot.calls] == ["editMessageText", "unpinChatMessage"]
    assert json.loads(path.read_text())["id"] == 55
