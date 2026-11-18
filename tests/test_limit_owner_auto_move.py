"""Owner traffic recovers exhausted sessions using fresh quota evidence only."""
import json
from datetime import datetime, timezone

import pytest

from ctb import accounts, hooks, identity, inbox as I, recover, usage
from ctb.config import Config, write_private
from test_inbox import TextMedia, setup, update

NOW = 1_800_000_000.0
SID = "sess-1"
LIMIT = {"id": "limit-one", "reset": NOW + 5 * 86400, "clock": "21.11 16:00"}


def reading(name, percent, age=0):
    write_private(accounts.usage_path(name), json.dumps({
        "at": datetime.fromtimestamp(NOW - age, timezone.utc).isoformat(),
        "five_hour": 0, "weekly": percent, "weekly_resets": NOW + 6 * 86400,
    }))


@pytest.fixture
def world(tmp_path, monkeypatch, bot):
    cfg = Config(bot_token="test", owner_chat_id=7, ui_language="he", auto_account_switch=True)
    topics, box, thread = setup(tmp_path, bot)
    spawned, refreshed = [], []
    monkeypatch.setattr(I.time, "time", lambda: NOW)
    monkeypatch.setattr(usage, "limit_of", lambda s: LIMIT)
    monkeypatch.setattr(identity, "check", lambda *a, **kw: None)
    monkeypatch.setattr(accounts, "origin_account", lambda s: "main")
    monkeypatch.setattr(accounts, "distinct_others", lambda here: ["spare", "reserve"])
    monkeypatch.setattr(accounts, "quota_twins", lambda n: [])
    monkeypatch.setattr(accounts, "is_big_plan", lambda n: False)
    monkeypatch.setattr(accounts, "auto_enabled", lambda: True)
    monkeypatch.setattr(accounts, "ensure_usage", lambda **kw: refreshed.append(kw))
    monkeypatch.setattr(hooks, "spawn", spawned.append)
    monkeypatch.setattr(I, "_reopen_or_nudge", lambda *a: pytest.fail("ordinary wake raced the move"))
    bot.calls.clear()
    reading("main", 100)

    def send():
        return I.handle(update(".", thread), cfg, bot, topics, box, TextMedia(), 0)

    return cfg, box, bot, spawned, refreshed, send


def test_owner_message_to_spent_session_moves_to_best_fresh_account(world):
    cfg, box, bot, spawned, refreshed, send = world
    reading("spare", 0)
    reading("reserve", 65)
    assert send() == "queued"
    assert box.pending(SID) == 1
    assert spawned == [["account", "auto-move", "--session=sess-1", "--thread=101", "--target=spare"]]
    notice = bot.methods("sendMessage")[-1]["text"]
    assert "עובר אוטומטית לחשבון spare" in notice and "יימסר כשהמכסה" not in notice
    assert refreshed == []
    assert send() == "queued"  # concurrent/repeated owner traffic does not launch another move
    assert len(spawned) == 1


def test_no_account_available_keeps_message_and_announces_reset(world):
    cfg, box, bot, spawned, refreshed, send = world
    reading("spare", 100)
    reading("reserve", 100)
    assert send() == "queued"
    assert box.pending(SID) == 1 and spawned == []
    assert "יימסר כשהמכסה תחזור ב-21.11 16:00" in bot.methods("sendMessage")[-1]["text"]
    assert len(refreshed) == 1


def test_new_headroom_retries_immediately_despite_previous_hold_notice(world):
    cfg, box, bot, spawned, refreshed, send = world
    reading("spare", 100)
    reading("reserve", 100)
    assert send() == "queued" and spawned == []
    reading("spare", 0)
    assert send() == "queued" and spawned[0][-1] == "--target=spare"
    assert "עובר אוטומטית" in bot.methods("sendMessage")[-1]["text"]


def test_more_owner_traffic_does_not_launch_a_second_move_while_target_state_changes(world):
    cfg, box, bot, spawned, refreshed, send = world
    reading("spare", 0)
    assert send() == "queued" and len(spawned) == 1
    reading("spare", 100)
    reading("reserve", 0)
    assert send() == "queued" and len(spawned) == 1


@pytest.mark.parametrize("percent", [0, 100])
def test_stale_reading_is_neither_room_nor_spent(world, percent):
    cfg, box, bot, spawned, refreshed, send = world
    reading("spare", percent, accounts.MOVE_USAGE_MAX_AGE + 1)
    assert not accounts.spent("spare", NOW)
    assert not accounts.has_headroom("spare", NOW)
    assert "spare" not in accounts.fallback_plan(SID, NOW)["fresh"]
    assert send() == "queued" and spawned == [] and refreshed
    assert "יימסר כשהמכסה" in bot.methods("sendMessage")[-1]["text"]


def test_stale_account_can_be_selected_after_successful_fresh_check(world, monkeypatch):
    cfg, box, bot, spawned, refreshed, send = world
    reading("spare", 100, accounts.MOVE_USAGE_MAX_AGE + 1)
    monkeypatch.setattr(accounts, "ensure_usage", lambda **kw: reading("spare", 0))
    assert send() == "queued" and spawned[0][-1] == "--target=spare"


def test_stale_account_cannot_hide_limit_after_restart(world, monkeypatch):
    reading("spare", 0, accounts.MOVE_USAGE_MAX_AGE + 1)
    monkeypatch.setattr(accounts, "live_session_records", lambda: [{"sessionId": SID, "startedAt": NOW * 1000}])
    monkeypatch.setattr(accounts, "account_for", lambda s: "spare")
    assert not usage._restarted_with_room(SID, NOW - 86400)


def test_stale_twin_does_not_change_fresh_accounts_quota_or_ranking(world, monkeypatch):
    reading("spare", 0)
    reading("old-profile", 100, accounts.MOVE_USAGE_MAX_AGE + 1)
    monkeypatch.setattr(accounts, "quota_twins", lambda n: ["old-profile"] if n == "spare" else [])
    assert accounts.has_headroom("spare", NOW)
    assert accounts.used("spare", NOW) == 0


@pytest.mark.parametrize("age, fresh", [(12 * 3600, True), (12 * 3600 + 1, False), (-1, False)])
def test_freshness_boundary(world, age, fresh):
    reading("spare", 0, age)
    assert accounts.has_headroom("spare", NOW) is fresh


def test_fallback_uses_session_origin_not_worker_environment(world, monkeypatch):
    reading("spare", 0)
    monkeypatch.setattr(accounts, "origin_account", lambda s: "spare")
    monkeypatch.setattr(accounts, "limited_account", lambda *a: "main")
    monkeypatch.setattr(accounts, "distinct_others", lambda here: ["reserve"] if here == "spare" else ["spare"])
    plan = accounts.fallback_plan(SID, NOW)
    assert plan["here"] == "spare" and plan["fresh"] == []


def test_disabled_auto_move_does_not_spawn(world, monkeypatch):
    cfg, box, bot, spawned, refreshed, send = world
    cfg.auto_account_switch = False
    monkeypatch.setattr(accounts, "auto_enabled", lambda: False)
    reading("spare", 0)
    assert send() == "queued" and spawned == [] and refreshed == []
    notice = bot.methods("sendMessage")[-1]
    assert "יימסר כשהמכסה" not in notice["text"]
    assert notice["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "more:/account move spare"


def test_live_toggle_off_is_respected_even_with_older_listener_settings(world, monkeypatch):
    cfg, box, bot, spawned, refreshed, send = world
    reading("spare", 0)
    monkeypatch.setattr(accounts, "auto_enabled", lambda: False)
    assert send() == "queued" and spawned == []
    assert "reply_markup" in bot.methods("sendMessage")[-1]


def test_real_session_record_takes_precedence_over_worker_login(monkeypatch):
    accounts.add("spare")
    accounts.record(SID, "spare")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(accounts.claude_home()))
    monkeypatch.setattr(accounts, "auto_enabled", lambda: True)
    monkeypatch.setattr(accounts, "real_accounts", lambda: [["main"], ["spare"]])
    monkeypatch.setattr(accounts, "logged_out", lambda name: False)
    monkeypatch.setattr(accounts, "better_twins", lambda name: [])
    monkeypatch.setattr(accounts, "quota_twins", lambda name: [])
    reading("main", 100)
    reading("spare", 0)
    plan = accounts.fallback_plan(SID, NOW)
    assert plan["here"] == "spare" and plan["fresh"] == []


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("auto", [False, True])
@pytest.mark.parametrize("has_limit", [False, True])
def test_deferred_owner_moves_retry_when_quiet_independently_of_auto(world, monkeypatch, tmp_path,
                                                                   manual, auto, has_limit):
    cfg, box, bot, spawned, refreshed, send = world
    cfg.auto_account_switch = auto
    reading("spare", 0)
    monkeypatch.setattr(accounts, "auto_enabled", lambda: auto)
    monkeypatch.setattr(usage, "limit_of", lambda s: LIMIT if has_limit else None)
    monkeypatch.setattr(recover, "_just_moved", lambda s: False)
    monkeypatch.setattr(recover, "_finish_resume", lambda s: None)
    safe = {"now": False}
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a, **k: safe["now"])
    monkeypatch.setattr(accounts, "restart_is_safe_or_gone", lambda *a, **k: safe["now"])
    accounts.defer_move(SID, "spare", NOW - 200, origin="main",
                        limit=LIMIT if has_limit else None, manual=manual)
    state = {SID: dict(LIMIT, updated=NOW)} if has_limit else {}

    def tick(at):
        recover._one(cfg, bot, box, SID, 101, state, tmp_path / "limit-state.json",
                     at, spawned.append, "he")

    tick(NOW)
    assert spawned == []  # no retry while the session is busy
    safe["now"] = True
    tick(NOW + 1)
    expected = manual or (auto and has_limit)
    assert len(spawned) == int(expected)
    if expected:
        assert spawned[0][-1] == "--target=spare"
        assert accounts.deferred_move(SID, NOW + 1)["manual"] is manual
        tick(NOW + 20)
        assert len(spawned) == 1  # the ordinary retry throttle still applies
        tick(NOW + 70)
        assert len(spawned) == 2
    assert cfg.auto_account_switch is auto and accounts.auto_enabled() is auto


def test_owner_deferral_does_not_move_to_an_unrequested_account(world, monkeypatch):
    reading("reserve", 0)
    monkeypatch.setattr(accounts, "auto_enabled", lambda: False)
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a, **k: True)
    accounts.defer_move(SID, "spare", NOW - 200, origin="main", manual=True)
    spawned = []
    assert recover._retry_deferred(None, None, SID, 101, NOW, spawned.append, {}, None)
    assert spawned == [] and accounts.deferred_move(SID, NOW)["target"] == "spare"


def test_deferred_manual_choice_is_preserved_on_retry_and_a_new_automatic_limit(world):
    accounts.defer_move(SID, "spare", NOW - 200, manual=True, limit=LIMIT)
    accounts.defer_move(SID, "spare", NOW - 100)
    assert accounts.deferred_move(SID, NOW)["manual"] is True
    accounts.defer_move(SID, "reserve", NOW, limit=dict(LIMIT, id="another-limit"), manual=False)
    waiting = accounts.deferred_move(SID, NOW)
    assert waiting["manual"] is True and waiting["target"] == "spare"
    assert waiting["limit_id"] == LIMIT["id"]
