"""The Tower's bounded home snapshot, confirmed writes, private narration and data-free log."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


@pytest.fixture
def cfg():
    return SimpleNamespace(ui_language="he", chat_id=-100123456789, owner_chat_id=42, projects=[])

from ctb import tower_home as H
from ctb import voice_copilot as V
from ctb import radio


def test_home_snapshot_is_bounded_titles_only(cfg, monkeypatch):
    rows = [{"title": "x" * 200, "time": "10:00"}] * 30
    monkeypatch.setattr(H.schedule, "view", lambda *a, **k: {"days": {"today": rows, "tomorrow": rows}})
    monkeypatch.setattr(H.family_board, "view", lambda *a, **k: {
        "day": 0, "shabbat": False, "days": [{"chores": [{"text": "c", "done": False}] }],
        "daily": [{"text": "r", "done": False}], "shop": [{"text": "s", "done": False}]})
    monkeypatch.setattr(H.board, "open_reminders", lambda: [(cfg.chat_id, 2, {"text": "secret title", "token": "credential"})] * 30)
    out = H.today_at_home(cfg, datetime(2026, 11, 17, tzinfo=timezone.utc), source_list=[])
    assert len(out["schedule"]["today"]) == H.MAX_ROWS
    assert max(map(len, (r["title"] for r in out["schedule"]["today"]))) == H.MAX_TITLE
    assert out["reminders"] == ["secret title"] * H.MAX_ROWS
    assert "token" not in str(out)


def test_home_snapshot_excludes_reminders_from_unknown_chats(cfg, monkeypatch):
    monkeypatch.setattr(H.schedule, "view", lambda *a, **k: {"days": {"today": [], "tomorrow": []}})
    monkeypatch.setattr(H.family_board, "view", lambda *a, **k: {
        "day": 0, "shabbat": False, "days": [{"chores": []}], "daily": [], "shop": []})
    monkeypatch.setattr(H.board, "open_reminders", lambda: [
        (-1009876543210, 1, {"text": "not the owner's reminder"}),
        (cfg.chat_id, 2, {"text": "owner reminder"})])
    out = H.today_at_home(cfg, datetime(2026, 11, 17, tzinfo=timezone.utc), source_list=[])
    assert out["reminders"] == ["owner reminder"]


def test_prepare_never_writes_until_confirm_and_expires(cfg, monkeypatch):
    writes = []
    monkeypatch.setattr(H.family_board, "add", lambda *a: writes.append(a) or {"id": "item"})
    row = H.prepare("shopping", "חלב", now=100)
    assert writes == [] and H.pending(now=101)[0]["id"] == row["id"]
    H.confirm(cfg, row["id"], row["digest"], now=102)
    assert writes == [("shop", "חלב")]
    expired = H.prepare("shopping", "לחם", now=200)
    with pytest.raises(KeyError):
        H.confirm(cfg, expired["id"], expired["digest"], now=200 + H.TTL + 1)
    assert writes == [("shop", "חלב")]


def test_day_plan_confirmation_is_bound_to_displayed_blocks(cfg, monkeypatch):
    writes = []
    monkeypatch.setattr(H.schedule, "zone", lambda cfg: timezone.utc)
    monkeypatch.setattr(H.schedule, "add_structured",
                        lambda cfg, title, day, clock, now: writes.append((title, day.isoformat(), clock)) or title)
    row = H.prepare("day_plan", "plan", blocks=[{"time": "08:00", "title": "First"},
                                                  {"time": "09:00", "title": "Second"}])
    assert H.pending()[0]["blocks"] == row["blocks"]
    with pytest.raises(KeyError):
        H.confirm(cfg, row["id"], "0" * 64)
    assert writes == []
    H.confirm(cfg, row["id"], row["digest"])
    assert writes == [("First", datetime.now(timezone.utc).date().isoformat(), "08:00"),
                      ("Second", datetime.now(timezone.utc).date().isoformat(), "09:00")]


def test_non_ascii_digest_is_a_clean_rejection(cfg):
    row = H.prepare("shopping", "milk")
    with pytest.raises(KeyError):
        H.confirm(cfg, row["id"], "לא")


def test_day_plan_card_renders_every_block_before_bound_confirm_control():
    source = H.__file__.replace("tower_home.py", "cockpit_voice_copilot.js")
    text = open(source, encoding="utf-8").read()
    start = text.index('if(p.kind==="day_plan")')
    card = text[start:text.index('box.appendChild(card)', start)]
    assert 'p.blocks||[]' in card and 'at.dir="ltr"' in card and 'title.dir="auto"' in card
    assert card.index("p.blocks||[]") < card.index("card.appendChild(b)")
    assert '{id:p.id,digest:p.digest}' in card


def test_private_radio_queue_is_off_and_budget_aware(cfg, monkeypatch):
    monkeypatch.setattr(radio, "private_audio_on", lambda: False)
    assert radio.queue_private_schedule_summary(cfg, "היום") ["reason"] == "private_player_off"
    monkeypatch.setattr(radio, "private_audio_on", lambda: True)
    monkeypatch.setattr(radio, "settings", lambda _cfg: {"tts_daily_minutes": 1})
    monkeypatch.setattr(radio.radio_listeners, "snapshot", lambda *a: {})
    monkeypatch.setattr(radio.radio_listeners, "voice_settings", lambda *a: {"tts_daily_minutes": 1})
    monkeypatch.setattr(radio.radio_voice, "paid_usage", lambda *_: SimpleNamespace(available=lambda *_: 0))
    assert radio.queue_private_schedule_summary(cfg, "היום")["reason"] == "tts_budget_exhausted"
    assert not (radio.folder() / "schedule-requests").exists()


def test_private_radio_allows_only_one_summary_and_rechecks_budget(cfg, monkeypatch):
    monkeypatch.setattr(radio, "private_audio_on", lambda: True)
    monkeypatch.setattr(radio, "settings", lambda _cfg: {"tts_daily_minutes": 1})
    monkeypatch.setattr(radio.radio_listeners, "snapshot", lambda *a: {})
    monkeypatch.setattr(radio.radio_listeners, "voice_settings", lambda *a: {"tts_daily_minutes": 1})
    available = {"value": 60}
    monkeypatch.setattr(radio.radio_voice, "paid_usage", lambda *_: SimpleNamespace(
        available=lambda *_: available["value"]))
    assert radio.queue_private_schedule_summary(cfg, "ראשון")["queued"] is True
    assert radio.queue_private_schedule_summary(cfg, "שני")["reason"] == "already_queued"
    available["value"] = 0
    spoken = []
    radio.drain_private_schedule_requests(cfg, {"tts_daily_minutes": 1}, spoken.append)
    assert spoken == []


def test_tower_log_has_outcomes_not_user_text(capsys):
    V.tower_log("end", "Refused", {"today_at_home"})
    line = capsys.readouterr().err
    assert "outcome=end" in line and "error=Refused" in line and "today_at_home" in line
    assert "my private spoken words" not in line


def test_model_supplied_tool_name_never_reaches_lifecycle_log(capsys):
    injected = "today_at_home\nFORGED lifecycle text\x1b[31m"
    V.tower_log("end", "none", {injected})
    line = capsys.readouterr().err
    assert "tools=unknown" in line
    assert "FORGED" not in line and "\x1b" not in line
