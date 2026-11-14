import copy
import json

import pytest

from conftest import GOOD_TOKEN
from ctb import doctor, heartbeat, hook_health as H, install, panel_pin
from ctb.config import Config, state_dir


@pytest.fixture
def settings(tmp_path, monkeypatch):
    root = tmp_path / "claude-test"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    path = root / "settings.json"
    path.write_text(json.dumps({"hooks": install.desired_hooks()}))
    return path


@pytest.mark.parametrize("event", list(install.desired_hooks()))
@pytest.mark.parametrize("change", ["missing", "command", "timeout", "matcher", "disabled"])
def test_detects_missing_and_changed_registrations(event, change):
    settings = {"hooks": copy.deepcopy(install.desired_hooks())}
    assert doctor.hook_drift(settings) == []
    if change == "missing":
        del settings["hooks"][event]
    elif change == "matcher":
        settings["hooks"][event][0]["matcher"] = "wrong"
    elif change == "disabled":
        settings["hooks"][event][0]["hooks"][0]["disabled"] = True
    else:
        settings["hooks"][event][0]["hooks"][0][change] = "changed"
    assert doctor.hook_drift(settings) == [event]


def test_doctor_uses_same_drift_check(settings, monkeypatch):
    value = json.loads(settings.read_text())
    value["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 1
    settings.write_text(json.dumps(value))
    monkeypatch.setattr(doctor, "service_files", lambda svc: [])
    assert any(level == "FAIL" and "Stop: bridge hooks" in text for level, text in doctor.check_hooks())
    assert H.snapshot()["events"] == ["Stop"]


def test_one_private_safe_alert_per_state(settings, bot, monkeypatch):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=7, chat_id=-1001234567890)
    monkeypatch.setattr(panel_pin, "known_name", lambda cfg: "test_bot")
    settings.write_text('{"hooks": {}}')
    H.check(cfg, bot, 1000)
    H.check(cfg, bot, 1001)
    H.check(cfg, bot, 1300)
    assert len(bot.calls) == 1
    method, p = bot.calls[0]
    assert method == "sendMessage" and p["chat_id"] == 7 and "message_thread_id" not in p
    assert p["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://t.me/test_bot?startapp"
    assert "command" not in p["text"] and GOOD_TOKEN not in json.dumps(p)
    # The persisted incident survives a heartbeat/module restart; recovery starts a new incident.
    H.restore()
    H.check(cfg, bot, 1600)
    assert len(bot.calls) == 1
    settings.write_text('{"hooks": {}}')
    H.check(cfg, bot, 1900)
    assert len(bot.calls) == 2
    # A different broken configuration is a changed state, not a repeated warning.
    settings.write_text('{"hooks": {"Stop": []}}')
    H.check(cfg, bot, 2200)
    assert len(bot.calls) == 3


def test_restore_only_merges_hooks_in_temp_config(settings):
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo keep"}]}
    old = {"model": "keep", "permissions": {"allow": ["Read"]}, "statusLine": {"command": "keep"},
           "hooks": {"PreToolUse": [other], "UnknownEvent": [other]}}
    settings.write_text(json.dumps(old))
    assert H.restore() == {"ok": True}
    new = json.loads(settings.read_text())
    assert new == install.merge_settings(old)[0]
    assert {k: v for k, v in new.items() if k != "hooks"} == {k: v for k, v in old.items() if k != "hooks"}
    assert new["hooks"]["PreToolUse"][0] == other and new["hooks"]["UnknownEvent"] == [other]
    assert len(list(settings.parent.glob("settings.json.bak-ctb-*"))) == 1
    before = settings.stat().st_mtime_ns
    H.restore()
    assert settings.stat().st_mtime_ns == before
    assert H.view()["restored"] and H.view()["ok"]


@pytest.mark.parametrize("text", ['{broken', '[]', '{"hooks": []}', '{"hooks": {"Stop": null}}'])
def test_bad_settings_detected_without_exposing_content(settings, text):
    settings.write_text(text)
    assert not H.snapshot()["ok"]
    assert text not in json.dumps(H.view())


def test_restore_refuses_invalid_or_disabled_settings(settings):
    for text in ['{broken', '{"disableAllHooks": true}', '{"hooks": []}', '{"hooks": {"Stop": [null]}}']:
        settings.write_text(text)
        with pytest.raises(ValueError):
            H.restore()
        assert settings.read_text() == text


def test_damaged_health_bookkeeping_does_not_break_card(settings):
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / "hook-health.json").write_text('[]')
    assert H.view()["ok"]


def test_heartbeat_isolates_health_failure(monkeypatch, bot):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=7)
    monkeypatch.setattr(H, "check", lambda *a: (_ for _ in ()).throw(ValueError("private")))
    monkeypatch.setattr(heartbeat, "refresh_cards", lambda *a, **k: 0)
    assert heartbeat.run_once(cfg, bot, now=1000) == 0
