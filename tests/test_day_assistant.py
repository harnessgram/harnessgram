from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ctb import day_assistant as D


def snap():
    return {"schedule": {"today": [{"time": "09:00", "title": "בית ספר"}], "tomorrow": []},
            "chores": ["לסדר תיק"], "routines": ["ארוחת בוקר"]}


def test_briefing_order_and_waiting_cap():
    out = D.briefing(snap(), [{"name": str(i)} for i in range(5)], ["מוזג השיפור"])
    assert out["waiting"] == ["0", "1", "2"]
    assert [line.split(":", 1)[0] for line in out["lines"]] == ["בוקר טוב. הנה היום שלך.", "היום", "בבית", "מחכים לך", "בלילה הסוכנים סיימו"]
    assert "\u206809:00 בית ספר\u2069" in out["text"]
    assert "\u20680\u2069" in out["text"]


def test_nudge_timing_quiet_hours_and_dedupe():
    now = datetime(2026, 11, 18, 8, 50, 28, tzinfo=timezone.utc)
    rows = D.due_nudges(snap(), now, set())
    assert len(rows) == 1 and D.due_nudges(snap(), now, {rows[0]["key"]}) == []
    assert D.due_nudges(snap(), now.replace(second=58), {rows[0]["key"]}) == []
    assert D.due_nudges(snap(), now.replace(hour=22, minute=50), set()) == []


def test_bad_schedule_rows_are_skipped():
    now = datetime(2026, 11, 18, 8, 50, tzinfo=timezone.utc)
    bad = {"schedule": {"today": [
        {"time": "09:00", "title": None},
        {"time": "all day", "title": "event"},
    ]}}
    assert D.due_nudges(bad, now, set()) == []
    assert D.plan(bad, [{"name": "task"}], now=now) == [{"time": "09:30", "title": "task"}]


def test_tick_uses_private_radio_budget_and_veto(monkeypatch):
    now = datetime(2026, 11, 18, 7, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(D, "rest_time", lambda cfg, at: False)
    monkeypatch.setattr(D, "briefing_enabled", lambda: True)
    spoken, cards, pushed = [], [], []
    state = {}
    result = D.tick(SimpleNamespace(day_assistant={}), clock=lambda: now, snapshot=snap, state=state,
                    waiting=lambda: [], overnight=lambda: [], private_radio=spoken.append,
                    cockpit_card=cards.append, push=pushed.append, can_speak=lambda: True,
                    reserve_speech=lambda text: False)
    assert result["briefing"] and len(cards) == 1 and spoken == [] and pushed == []


def test_plan_is_a_pure_proposal():
    proposal = D.plan(snap(), [{"name": "שיעורי בית"}], now=datetime(2026, 11, 18, 7, 0))
    assert proposal == [{"time": "08:00", "title": "שיעורי בית"}]


@pytest.mark.parametrize("events,expected", [
    (["08:15"], "08:45"), (["08:30"], "08:00"), (["08:15", "08:45"], "09:15")])
def test_plan_requires_the_complete_block_to_be_free(events, expected):
    snapshot = {"schedule": {"today": [{"time": at, "title": "busy"} for at in events]}}
    assert D.plan(snapshot, [{"name": "task"}], now=datetime(2026, 11, 18, 7, 0)) == [{"time": expected, "title": "task"}]


def test_nudge_push_is_independent_of_private_radio(monkeypatch):
    now = datetime(2026, 11, 18, 8, 50, 28, tzinfo=timezone.utc)
    monkeypatch.setattr(D, "rest_time", lambda cfg, at: False)
    monkeypatch.setattr(D, "nudges_enabled", lambda: True)
    pushed, state = [], {}
    out = D.tick(SimpleNamespace(day_assistant={}), clock=lambda: now, snapshot=snap, state=state,
                 private_radio=lambda text: False, cockpit_card=lambda card: None,
                 push=pushed.append, can_speak=lambda: True, reserve_speech=lambda text: True)
    assert out["nudges"] == 1 and pushed and len(state["nudges"]) == 1


@pytest.mark.parametrize("reason", ["private_player_off", "tts_budget_exhausted"])
def test_production_adapter_couples_push_to_radio_policy(reason, tmp_path, monkeypatch):
    from ctb import radio, schedule, tower_home, waiting
    now = datetime(2026, 11, 18, 8, 50, 28, tzinfo=timezone.utc)
    monkeypatch.setattr(D, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(D, "rest_time", lambda cfg, at: False)
    monkeypatch.setattr(D, "nudges_enabled", lambda: True)
    monkeypatch.setattr(D, "home_chat", lambda cfg: 42)
    monkeypatch.setattr(schedule, "zone", lambda cfg: timezone.utc)
    monkeypatch.setattr(tower_home, "today_at_home", lambda cfg, at: snap())
    monkeypatch.setattr(waiting, "waiting_items", lambda cfg, now: ())
    monkeypatch.setattr(radio, "queue_private_schedule_summary",
                        lambda cfg, text: {"queued": False, "reason": reason})
    bot = SimpleNamespace(calls=[], call=lambda *args: bot.calls.append(args))
    out = D.heartbeat_tick(SimpleNamespace(day_assistant={}), bot, now.timestamp())
    assert out["nudges"] == 1 and bot.calls


def test_defaults_off_and_off_round_never_fetches_snapshot(monkeypatch):
    monkeypatch.setattr(D, "setting_on", lambda key: False)
    called = []
    out = D.tick(SimpleNamespace(day_assistant={"briefing": True, "nudges": True}),
                 clock=lambda: datetime(2026, 11, 18, 9, tzinfo=timezone.utc),
                 snapshot=lambda: called.append(True), state={}, private_radio=lambda text: False,
                 cockpit_card=lambda card: True, push=lambda text: None, can_speak=lambda: False,
                 reserve_speech=lambda text: False)
    assert out == {"briefing": False, "nudges": 0} and called == []


def test_late_heartbeat_delivers_once_and_failed_card_is_retried(monkeypatch):
    monkeypatch.setattr(D, "rest_time", lambda cfg, at: False)
    monkeypatch.setattr(D, "briefing_enabled", lambda: True)
    now = datetime(2026, 11, 18, 7, 31, 20, tzinfo=timezone.utc)
    state = {}
    args = dict(cfg=SimpleNamespace(day_assistant={}), clock=lambda: now, snapshot=snap, state=state,
                private_radio=lambda text: False, push=lambda text: None, can_speak=lambda: False,
                reserve_speech=lambda text: False)
    assert not D.tick(**args, cockpit_card=lambda card: False)["briefing"]
    assert "briefing" not in state
    assert D.tick(**args, cockpit_card=lambda card: True)["briefing"]
    assert not D.tick(**args, cockpit_card=lambda card: True)["briefing"]


@pytest.mark.parametrize("now", [
    datetime(2026, 11, 18, 6, 59, tzinfo=timezone.utc),
    datetime(2026, 11, 18, 22, 0, tzinfo=timezone.utc),
    datetime(2026, 11, 18, 10, 31, tzinfo=timezone.utc),
])
def test_briefing_is_never_delivered_in_quiet_hours_or_after_its_window(monkeypatch, now):
    monkeypatch.setattr(D, "rest_time", lambda cfg, at: False)
    monkeypatch.setattr(D, "briefing_enabled", lambda: True)
    cards, spoken = [], []
    out = D.tick(SimpleNamespace(day_assistant={}), clock=lambda: now, snapshot=snap, state={},
                 private_radio=spoken.append, cockpit_card=cards.append, push=lambda text: None,
                 can_speak=lambda: True, reserve_speech=lambda text: True)
    assert out["briefing"] is False and cards == [] and spoken == []


@pytest.mark.parametrize("section", [
    {"briefing": True}, {"nudges": True}, {"auto_plan": True},
    {"briefing_time": "7:30"}, {"briefing_time": "24:00"},
])
def test_config_rejects_ignored_switches_and_invalid_briefing_times(section):
    from ctb.config import ConfigError, from_dict
    with pytest.raises(ConfigError):
        from_dict({"day_assistant": section})


def test_plan_starts_at_current_minute():
    assert D.plan({"schedule": {"today": []}}, [{"name": "task"}],
                  now=datetime(2026, 11, 18, 10, 12, 1)) == [{"time": "10:13", "title": "task"}]


def test_radio_success_is_recorded_when_phone_push_throws(monkeypatch):
    monkeypatch.setattr(D, "rest_time", lambda cfg, at: False)
    monkeypatch.setattr(D, "nudges_enabled", lambda: True)
    state = {}
    def failed_push(_text):
        raise RuntimeError("phone unavailable")
    out = D.tick(SimpleNamespace(day_assistant={}),
                 clock=lambda: datetime(2026, 11, 18, 8, 50, tzinfo=timezone.utc),
                 snapshot=snap, state=state, private_radio=lambda text: True,
                 cockpit_card=lambda card: True, push=failed_push, can_speak=lambda: True,
                 reserve_speech=lambda text: True)
    assert out["nudges"] == 1 and len(state["nudges"]) == 1
