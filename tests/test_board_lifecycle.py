"""Reminder aging/restoration and conservative deadline housekeeping."""
import json
from datetime import datetime, timedelta

import pytest

from ctb import board, cli, heartbeat, panel, reminder_lifecycle as life
from ctb.config import Config, state_dir, write_private
from conftest import FakeBot, GOOD_TOKEN

NOW = datetime(2026, 11, 12, 12).timestamp()
CHAT = -1001234567890
OTHER = -1009876543210


def reminder(ident=1, text="Keep this reminder", at=NOW, **extra):
    return dict(k="remind", i=ident, s=text, at=at, x=False, **extra)


def seed(rows, other=None):
    data = {f"{CHAT}:10": dict(board._blank(), items=rows)}
    if other:
        data[f"{OTHER}:20"] = dict(board._blank(), items=other)
    write_private(state_dir() / board.STATE_FILE, json.dumps(data))


@pytest.fixture
def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, chat_id=CHAT)


def test_aging_uses_due_plus_three_days_or_fourteen_without_due():
    assert not life.is_old(reminder(at=NOW - 14 * life.DAY), NOW)
    assert life.is_old(reminder(at=NOW - 14 * life.DAY - 1), NOW)
    due = "2026-11-01T12:00:00"
    end = life.deadline(due)
    assert not life.is_old(reminder(d=due), end + 3 * life.DAY)
    assert life.is_old(reminder(d=due), end + 3 * life.DAY + 1)
    assert not life.is_old(reminder(at=NOW - 100 * life.DAY, d="2026-12-01"), NOW)
    assert life.deadline("until 18:00") is None
    assert life.deadline("2026-02-30") is None


@pytest.mark.parametrize("text", [
    "Submit by 2026-11-01", "Deadline: 2026-11-01", "Due on 2026-11-01",
    "Please approve until 2026-11-01T12:00:00Z", "לאשר עד 2026-11-01",
])
def test_only_explicit_past_deadlines_can_be_swept(text):
    assert life.expired_text(reminder(text=text), NOW)


@pytest.mark.parametrize("text", [
    "Keep this", "Reminder from 2026-11-01", "Pay by 2027-11-01", "Review by Friday",
    "Submit by 01/11", "Submit by 18:00", "Daily until 2026-11-01", "Every year by 2026-11-01",
    "Not due 2026-11-01", "Deadline extended from 2026-11-01", "Submit by 2026-11-01 or 2026-12-01",
    "Due 2026-02-30", "לא להגיש עד 2026-11-01", "Deadline 2026-11-01 was postponed",
    "Don't clear until 2026-11-01", "Due 2026-11-01T25:00", "Due 2026-11-01T12:00 PST",
    "Due 2026-11-01 if approved",
])
def test_sweep_preserves_ambiguous_recurring_future_or_undated_text(text):
    assert not life.expired_text(reminder(text=text, at=NOW - 100 * life.DAY), NOW)


def test_date_only_deadline_expires_after_the_entire_day():
    start = datetime(2026, 11, 1).timestamp()
    row = reminder(text="Submit by 2026-11-01")
    assert not life.expired_text(row, start + 23 * 3600)
    assert life.expired_text(row, (datetime(2026, 11, 1) + timedelta(days=1)).timestamp() + 1)


def test_sweep_owner_scope_safety_idempotency_and_counts_only(cfg, capsys):
    seed([reminder(1, "Submit by 2026-11-01"), reminder(2),
          reminder(3, "Submit by 2026-11-01", d="2026-12-01"),
          reminder(4, "Submit by 2026-11-01", r=NOW - 10),
          dict(k="check", i=5, s="Submit by 2026-11-01", at=NOW, x=False)],
         [reminder(1, "Submit by 2026-11-01")])
    assert board.sweep(cfg, FakeBot(), NOW) == {"cleared": 1, "topics": 1}
    assert [i["i"] for i in board.items_of(CHAT, 10)] == [2, 3, 4, 5]
    assert len(board.items_of(OTHER, 20)) == 1
    assert board.sweep(cfg, FakeBot(), NOW) == {"cleared": 0, "topics": 0}
    assert not capsys.readouterr().out


def test_restore_is_durable_preserves_history_and_can_be_cleared(cfg, monkeypatch):
    original = NOW - 30 * life.DAY
    seed([reminder(at=original)])
    ref = f"{board.key_hash(board.key_of(CHAT, 10))}:1"
    monkeypatch.setattr(panel.time, "time", lambda: NOW)
    assert panel.clear_reminders(cfg, FakeBot(), [ref], action="restore") == {"restored": 1}
    row = board.items_of(CHAT, 10)[0]
    assert row["at"] == original and row["r"] == NOW
    assert not life.is_old(row, NOW)
    assert life.is_old(row, NOW + life.UNDATED_AGE + 1)
    assert panel.clear_reminders(cfg, FakeBot(), [ref]) == {"cleared": 1}
    assert not board.open_reminders()


def test_changes_are_visible_before_optional_telegram_refresh(cfg, monkeypatch):
    seed([reminder()])
    def during_sync(*args, **kw):
        assert not board.items_of(CHAT, 10)
        return False
    monkeypatch.setattr(board, "_sync", during_sync)
    board.apply(cfg, FakeBot(), CHAT, 10, lambda topic: board.finish(topic, topic["items"][0], NOW))


def test_panel_clear_never_waits_for_telegram_and_heartbeat_repairs_message(cfg, monkeypatch):
    seed([reminder()])
    with board._locked() as path:
        data = board._load(path)
        data[f"{CHAT}:10"]["id"] = 123
        write_private(path, json.dumps(data))
    monkeypatch.setattr(board, "_sync", lambda *a, **kw: pytest.fail("HTTP clear must never wait on Telegram"))
    ref = f"{board.key_hash(board.key_of(CHAT, 10))}:1"
    bot = FakeBot()
    assert panel.clear_reminders(cfg, bot, [ref]) == {"cleared": 1}
    assert bot.calls == []
    assert board._load(state_dir() / board.STATE_FILE)[f"{CHAT}:10"]["dirty"] is True
    assert board.refresh_dirty(cfg, bot) == 1
    assert bot.methods("editMessageText")[0]["message_id"] == 123
    assert not board._load(state_dir() / board.STATE_FILE)[f"{CHAT}:10"].get("dirty")


def test_late_telegram_edit_never_erases_a_concurrent_change(cfg, monkeypatch):
    seed([reminder()])
    with board._locked() as path:
        data = board._load(path)
        data[f"{CHAT}:10"].update(id=123, dirty=True)
        write_private(path, json.dumps(data))
    def concurrent(*a, **kw):
        with board._locked() as path:
            data = board._load(path)
            data[f"{CHAT}:10"]["items"].append(reminder(2))
            data[f"{CHAT}:10"].pop("dirty", None)
            write_private(path, json.dumps(data))
    monkeypatch.setattr(board, "_call", concurrent)
    assert board.refresh_dirty(cfg, FakeBot()) == 0
    row = board._load(state_dir() / board.STATE_FILE)[f"{CHAT}:10"]
    assert [i["i"] for i in row["items"]] == [1, 2] and row["dirty"] is True


def test_daily_sweep_is_shared_across_restarts_and_logs_no_content(cfg, monkeypatch):
    seen, logs = [], []
    monkeypatch.setattr(board, "sweep", lambda cfg, bot, now: seen.append(now) or {"cleared": 2, "topics": 1})
    from ctb import mirror
    monkeypatch.setattr(mirror, "log", logs.append)
    board.daily_sweep(cfg, FakeBot(), NOW)
    board.daily_sweep(cfg, FakeBot(), NOW + 30)
    board.daily_sweep(cfg, FakeBot(), NOW + life.DAY)
    assert seen == [NOW, NOW + life.DAY]
    assert logs == ["board sweep: cleared=2 topics=1"] * 2


def test_daily_sweep_retries_after_failure(cfg, monkeypatch):
    def broken(*a):
        raise OSError("unavailable")
    monkeypatch.setattr(board, "sweep", broken)
    with pytest.raises(OSError):
        board.daily_sweep(cfg, FakeBot(), NOW)
    seen = []
    monkeypatch.setattr(board, "sweep", lambda *a: seen.append(True) or {"cleared": 0, "topics": 0})
    board.daily_sweep(cfg, FakeBot(), NOW + 30)
    assert seen == [True]


def test_cli_sweep_needs_no_session(cfg, monkeypatch, capsys):
    monkeypatch.setattr(board, "sweep", lambda cfg, bot, now=None: {"cleared": 0, "topics": 0})
    assert board.run(cfg, "sweep", bot=FakeBot()) == 0
    assert capsys.readouterr().out == "cleared=0 topics=0\n"
    assert cli._parser().parse_args(["board", "sweep"]).board_cmd == "sweep"


def test_heartbeat_runs_the_same_sweep_daily(cfg, monkeypatch):
    seen = []
    monkeypatch.setattr(board, "daily_sweep", lambda cfg, bot, now: seen.append(now))
    heartbeat.run_once(cfg, FakeBot(), NOW)
    assert seen == [NOW]
