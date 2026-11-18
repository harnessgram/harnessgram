import json
from types import SimpleNamespace

from conftest import GOOD_TOKEN
from ctb import config, flood, panel_pin, topics
from ctb.config import Config, set_live_activity_in_cockpit, write_private
from ctb.telegram import TelegramError


class Map:
    def __init__(self):
        self.found = SimpleNamespace(chat=-1001, thread=17)
        self.refreshed = []

    def place_of(self, _session):
        return self.found

    def refresh(self, session):
        self.refreshed.append(session)


class Bot:
    def __init__(self, deleted_status=False):
        self.calls = []
        self.deleted_status = deleted_status

    def call(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "editMessageText" and self.deleted_status:
            self.deleted_status = False
            raise TelegramError("message to edit not found", 400)
        return {"message_id": 901} if method == "sendMessage" else {"ok": True}


def setup(monkeypatch, language="he", deleted_status=False):
    monkeypatch.setenv("CTB_LIVE_ACTIVITY_COCKPIT", "1")
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=-1001, ui_language=language, translate=False)
    set_live_activity_in_cockpit(True)
    mapping = Map()
    bot = Bot(deleted_status)
    monkeypatch.setattr(topics, "TopicMap", lambda *_args, **_kwargs: mapping)
    monkeypatch.setattr(panel_pin, "app_url", lambda *_args: "https://example.test/cockpit")
    monkeypatch.setattr(flood, "optional_call", lambda b, method, params, **_kwargs: b.call(method, params))
    return cfg, mapping, bot


def test_cockpit_status_uses_shared_cleanup_refresh_and_recreates_deleted_message(monkeypatch):
    cfg, mapping, bot = setup(monkeypatch, deleted_status=True)
    path = topics.session_state_path("s")
    write_private(path, json.dumps({"state": "waiting", "stuck_alert": 1, "all": [101],
                                    "placeholder": 102, "status_id": 103,
                                    "cockpit_status_id": 700, "cockpit_status_at": 0}))

    topics.set_state(cfg, "s", "busy", bot=bot)

    state = json.loads(path.read_text())
    assert state["state"] == "busy" and "stuck_alert" not in state
    assert "placeholder" not in state and "status_id" not in state
    assert {p["message_id"] for m, p in bot.calls if m == "deleteMessage"} == {101, 102, 103}
    assert state["cockpit_status_id"] == 901 and state["cockpit_status_at"] > 0
    assert mapping.refreshed == ["s"]
    sent = [p for m, p in bot.calls if m == "sendMessage"][-1]
    assert sent["text"] == "⚙️ בעבודה"
    assert sent["reply_markup"]["inline_keyboard"][0][0]["text"] == "פתיחה בקוקפיט"


def test_skipped_optional_status_does_not_advance_timestamp(monkeypatch):
    optional_call = flood.optional_call
    cfg, mapping, bot = setup(monkeypatch, language="en")
    path = topics.session_state_path("s")
    write_private(path, json.dumps({"state": "waiting", "cockpit_status_at": 12}))
    monkeypatch.setattr(flood, "optional_call", optional_call)
    monkeypatch.setattr(flood, "allow_optional", lambda *_args, **_kwargs: False)

    topics.set_state(cfg, "s", "busy", bot=bot)

    state = json.loads(path.read_text())
    assert state["cockpit_status_at"] == 12
    assert "cockpit_status_id" not in state
    assert mapping.refreshed == ["s"]


def test_done_state_ignores_late_bottom_refresh(monkeypatch):
    cfg, mapping, bot = setup(monkeypatch)
    path = topics.session_state_path("s")
    original = {"state": "done", "main_idle": True, "stuck_alert": 44,
                "cockpit_status_id": 700, "cockpit_status_at": 12}
    write_private(path, json.dumps(original))

    topics.set_state(cfg, "s", "bottom", bot=bot, status="late progress")

    assert json.loads(path.read_text()) == original
    assert bot.calls == []
    assert mapping.refreshed == []


def test_unchanged_telegram_status_counts_as_a_successful_refresh(monkeypatch):
    cfg, mapping, bot = setup(monkeypatch, language="en")
    path = topics.session_state_path("s")
    write_private(path, json.dumps({"state": "busy", "cockpit_status_id": 700,
                                    "cockpit_status_at": 0}))

    def unchanged(method, params):
        bot.calls.append((method, dict(params)))
        raise TelegramError("Bad Request: message is not modified", 400)

    bot.call = unchanged
    topics.set_state(cfg, "s", "busy", bot=bot)

    state = json.loads(path.read_text())
    assert state["cockpit_status_id"] == 700
    assert state["cockpit_status_at"] > 0
    assert mapping.refreshed == ["s"]
