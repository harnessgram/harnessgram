import json

from ctb import panel_settings, progress
from ctb.config import Config
from ctb.telegram import TelegramError
from conftest import GOOD_TOKEN
from test_handoff_links import OAUTH


def test_compact_card_hides_command_and_output_but_keeps_error():
    cfg = Config(translate=False)
    entry = {"row": {"l": "⚙️ Run checks", "d": "uv run pytest private-suite",
                     "output": "many lines of stdout", "error": "test failed"}}

    short = progress.card_text(entry, cfg, compact=True)
    full = progress.card_text(entry, cfg, compact=False)

    assert "Run checks" in short and "private-suite" not in short and "stdout" not in short
    assert "⚠️ test failed" in short
    assert "private-suite" in full and "<blockquote expandable>many lines of stdout" in full


def test_topic_choice_is_only_stored_when_it_overrides_global(monkeypatch, tmp_path):
    monkeypatch.setattr(progress, "state_dir", lambda: tmp_path)
    cfg = Config(compact_actions=False)

    assert progress.compact_actions_on(Config(), "session-a") is False
    assert progress.compact_actions_on(cfg, "session-a") is False
    progress.set_compact_actions(cfg, "session-a", True)
    assert progress.compact_actions_on(cfg, "session-a") is True
    progress.set_compact_actions(cfg, "session-a", False)
    assert not (tmp_path / "compact-actions" / "session-a").exists()
    cfg.compact_actions = True
    assert progress.compact_actions_on(cfg, "session-a") is True


def test_stored_error_is_redacted_before_compact_card(monkeypatch, tmp_path):
    monkeypatch.setattr(progress, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(progress, "setting_on", lambda _name: True)
    monkeypatch.setattr("ctb.policy.on", lambda: True)
    monkeypatch.setattr("ctb.hooks.spawn", lambda _args: None)
    secret = "sk-test-fake-secret-value"
    signin = OAUTH
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=7, progress=True, progress_details=True, translate=False)

    progress.append_progress("session-a", {"tool_name": "Run", "tool_response": {"error": f"token={secret} {signin}"}}, cfg)

    row = json.loads((tmp_path / "progress" / "session-a.jsonl").read_text().splitlines()[0])
    shown = progress.card_text({"row": row}, cfg, compact=True)
    assert secret not in row["error"] and secret not in shown
    assert signin not in row["error"] and signin not in shown


class CardBot:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def call(self, method, params):
        self.calls.append((method, params))
        if self.error:
            raise self.error
        return {}


def test_action_card_expands_during_and_after_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(progress, "state_dir", lambda: tmp_path)
    pdir = tmp_path / "progress"
    pdir.mkdir()
    cfg = Config(owner_chat_id=7, compact_actions=True, translate=False)
    state = {"id": 42, "cursor": 1, "cards": [{"row": {"l": "Run", "d": "details"}}]}
    (pdir / "session-a.msg").write_text(json.dumps(state))
    bot = CardBot()

    assert progress.toggle_card_details(cfg, bot, "session-a", 42, True)
    assert "details" in bot.calls[-1][1]["text"]
    progress.run_end_progress(cfg, "session-a", root=pdir, bot=bot)
    assert progress.toggle_card_details(cfg, bot, "session-a", 42, False)
    assert "details" not in bot.calls[-1][1]["text"]


def test_cards_on_both_sides_of_text_remain_expandable(monkeypatch, tmp_path):
    monkeypatch.setattr(progress, "state_dir", lambda: tmp_path)
    monkeypatch.setattr("ctb.hooks._seen", lambda _session: set())
    monkeypatch.setattr("ctb.hooks._mark", lambda _session, _ids: None)
    monkeypatch.setattr("ctb.topics.set_state", lambda *_args, **_kwargs: None)
    ids = iter((41, 42, 43))

    def send(_bot, method, _params, **_kwargs):
        return {"message_id": next(ids)} if method == "sendMessage" else {}

    monkeypatch.setattr(progress.flood, "optional_call", send)
    pdir = tmp_path / "progress"
    pdir.mkdir()
    state_path = pdir / "session-a.msg"
    cfg = Config(owner_chat_id=7, compact_actions=True, translate=False)
    rows = [
        {"kind": "tool", "l": "First", "d": "first details"},
        {"kind": "text", "uuid": "text-1", "text": "Between cards"},
        {"kind": "tool", "l": "Second", "d": "second details"},
    ]

    progress._send_ordered(cfg, "session-a", rows, {"cursor": 0, "cards": []}, state_path,
                           9, CardBot(), lambda: 100.0, True, chat=7)

    bot = CardBot()
    assert progress.toggle_card_details(cfg, bot, "session-a", 41, True)
    assert "first details" in bot.calls[-1][1]["text"]
    assert progress.toggle_card_details(cfg, bot, "session-a", 43, True)
    assert "second details" in bot.calls[-1][1]["text"]


def test_card_archives_are_compact_only_and_bounded(tmp_path):
    state = {"id": 1, "cards": [{"row": {"l": "Run"}}]}
    progress._archive_card(state, tmp_path, "session-a", False)
    assert not list(tmp_path.glob("*.card-*"))
    for message_id in range(progress.CARD_ARCHIVE_KEEP + 2):
        state["id"] = message_id
        progress._archive_card(state, tmp_path, "session-a", True)
    assert len(list(tmp_path.glob("session-a.card-*"))) == progress.CARD_ARCHIVE_KEEP


def test_action_card_toggle_swallows_telegram_edit_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(progress, "state_dir", lambda: tmp_path)
    pdir = tmp_path / "progress"
    pdir.mkdir()
    state = {"id": 42, "cards": [{"row": {"l": "Run", "d": "details"}}]}
    (pdir / "session-a.msg").write_text(json.dumps(state))
    cfg = Config(owner_chat_id=7, compact_actions=True, translate=False)

    assert progress.toggle_card_details(
        cfg, CardBot(TelegramError("Bad Request: message is not modified", 400)), "session-a", 42, False)
    assert not progress.toggle_card_details(
        cfg, CardBot(TelegramError("Bad Request: message can't be edited", 400)), "session-a", 42, True)


def test_cockpit_setting_uses_the_config_switch_and_clears_topic_overrides(monkeypatch, tmp_path):
    current = {"value": True}
    monkeypatch.setattr(panel_settings, "_config_get", lambda name: current["value"])
    monkeypatch.setattr(panel_settings, "_config_set", lambda name, value: current.update(value=value))
    monkeypatch.setattr(progress, "state_dir", lambda: tmp_path)
    overrides = tmp_path / "compact-actions"
    overrides.mkdir()
    (overrides / "session-a").write_text("on")
    get, set_value, allowed = panel_settings.SETTINGS["compact_actions"]

    assert allowed == (False, True) and get() is True
    set_value(False)
    assert current["value"] is False
    assert not (overrides / "session-a").exists()


def test_action_card_controls_can_be_removed():
    markup = progress.action_markup("en", expanded=False)

    assert markup["inline_keyboard"][0][0]["callback_data"] == "more:actionexpand"
    assert markup["inline_keyboard"][-1][0]["callback_data"] == "more:unbuttons"
