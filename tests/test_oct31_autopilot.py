"""Tests for Oct 31 features: autopilot, away-mode, restart-session, and topic-check / topic-rename."""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from ctb import accounts, autopilot, cli, dashboard, heartbeat, hooks, inbox, topics
from ctb.config import Config, state_dir, write_private
from ctb.telegram import TelegramError
from conftest import FakeBot


def create_session(session_id: str, thread_id: int = 101, name: str = "Test Task") -> None:
    """Register a session in the topic map, pane map, and session state."""
    topics_file = state_dir() / "topics.json"
    data = {}
    if topics_file.exists():
        data = json.loads(topics_file.read_text(encoding="utf-8"))
    data[session_id] = {"thread": thread_id, "name": name, "visible_name": f"⚙️ {name}"}
    write_private(topics_file, json.dumps(data))

    panes_file = state_dir() / "tmux-panes.json"
    panes = {}
    if panes_file.exists():
        panes = json.loads(panes_file.read_text(encoding="utf-8"))
    panes[session_id] = "%1"
    write_private(panes_file, json.dumps(panes))


def create_transcript(session_id: str, events: list, mtime: float = None) -> Path:
    """Create a transcript jsonl in claude projects directory."""
    projects = Path.home() / ".claude" / "projects" / "test-proj"
    projects.mkdir(parents=True, exist_ok=True)
    tpath = projects / f"{session_id}.jsonl"
    lines = [json.dumps(ev) for ev in events]
    tpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(tpath, (mtime, mtime))
    return tpath


def test_autopilot_quota_back_continue(monkeypatch, bot):
    session = "sess-quota-1"
    create_session(session, thread_id=201)

    now = 1000000.0
    evs = [
        {"type": "user", "message": {"content": "do task"}},
        {"type": "assistant", "uuid": "u-quota-1",
         "message": {"content": [{"type": "text", "text": "Waiting for the 5-hour quota to reset before I continue."}]}}
    ]
    create_transcript(session, evs, mtime=now - 300)

    # Set usage file: 50%
    write_private(state_dir() / "claude-usage.json", json.dumps({"five_hour": 50}))

    typed = []
    monkeypatch.setattr(autopilot, "session_pid", lambda s: 12345)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: True)
    monkeypatch.setattr(autopilot, "type_into", lambda p, t: typed.append((p, t)))

    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    autopilot.run_autopilot(cfg, bot=bot, now=now)

    assert len(typed) == 1
    assert typed[0][0] == "%1"
    assert "The Claude quota is back" in typed[0][1]

    msgs = bot.methods("sendMessage")
    assert any("המכסה חזרה" in m.get("text", "") for m in msgs)

    # Verify memory recorded uuid
    mem = autopilot.load_memory()
    assert mem.get(session, {}).get("last") == "u-quota-1"

    # Running again: must NOT nudge again because uuid is already recorded
    typed.clear()
    bot.calls.clear()
    autopilot.run_autopilot(cfg, bot=bot, now=now)
    assert len(typed) == 0
    assert len(bot.calls) == 0


def test_autopilot_quota_back_reset_passed(monkeypatch, bot):
    session = "sess-quota-reset"
    create_session(session, thread_id=202)

    now = 1000000.0
    evs = [
        {"type": "user", "message": {"content": "build it"}},
        {"type": "assistant", "uuid": "u-quota-2",
         "message": {"content": [{"type": "text", "text": "Pausing work until usage limit resets."}]}}
    ]
    create_transcript(session, evs, mtime=now - 200)

    # 5-hour is 95%, but reset passed in the past
    write_private(state_dir() / "claude-usage.json",
                  json.dumps({"five_hour": 95, "five_hour_resets": now - 10}))

    typed = []
    monkeypatch.setattr(autopilot, "session_pid", lambda s: 12345)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: True)
    monkeypatch.setattr(autopilot, "type_into", lambda p, t: typed.append((p, t)))

    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    autopilot.run_autopilot(cfg, bot=bot, now=now)

    assert len(typed) == 1
    assert "The Claude quota is back" in typed[0][1]
    assert any("המכסה חזרה" in m.get("text", "") for m in bot.methods("sendMessage"))


def test_autopilot_quota_back_blocked_when_full(monkeypatch, bot):
    session = "sess-quota-full"
    create_session(session, thread_id=203)

    now = 1000000.0
    evs = [
        {"type": "user", "message": {"content": "build"}},
        {"type": "assistant", "uuid": "u-quota-3",
         "message": {"content": [{"type": "text", "text": "Waiting for 5-hour limit to reset."}]}}
    ]
    create_transcript(session, evs, mtime=now - 200)

    # 5-hour is 90% (>= 85) and reset is in the future
    write_private(state_dir() / "claude-usage.json",
                  json.dumps({"five_hour": 90, "five_hour_resets": now + 3600}))

    typed = []
    monkeypatch.setattr(autopilot, "session_pid", lambda s: 12345)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: True)
    monkeypatch.setattr(autopilot, "type_into", lambda p, t: typed.append((p, t)))

    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    autopilot.run_autopilot(cfg, bot=bot, now=now)

    assert len(typed) == 0
    assert len(bot.calls) == 0


def test_autopilot_away_mode(monkeypatch, bot):
    session = "sess-away-1"
    create_session(session, thread_id=204)

    now = 1000000.0
    evs = [
        {"type": "user", "message": {"content": "work"}},
        {"type": "assistant", "uuid": "u-away-1",
         "message": {"content": [{"type": "text", "text": "Which architecture option should we take? Option A or B?"}]}}
    ]
    create_transcript(session, evs, mtime=now - 300)

    # Enable away mode
    autopilot.set_away(True, now=now - 60)
    assert autopilot.away_active()

    typed = []
    monkeypatch.setattr(autopilot, "session_pid", lambda s: 12345)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: True)
    monkeypatch.setattr(autopilot, "type_into", lambda p, t: typed.append((p, t)))

    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    autopilot.run_autopilot(cfg, bot=bot, now=now)

    assert len(typed) == 1
    assert "Autopilot: the owner is away" in typed[0][1]

    msgs = bot.methods("sendMessage")
    assert any("מצב היעדרות: הסשן ממשיך לבד" in m.get("text", "") for m in msgs)

    mem = autopilot.load_memory()
    assert mem.get(session, {}).get("away_nudges") == 1
    assert mem.get(session, {}).get("last") == "u-away-1"


def test_autopilot_away_mode_stops_on_idle(monkeypatch, bot):
    session = "sess-away-idle"
    create_session(session, thread_id=205)

    now = 1000000.0
    evs = [
        {"type": "user", "message": {"content": "work"}},
        {"type": "assistant", "uuid": "u-away-idle",
         "message": {"content": [{"type": "text", "text": "AUTOPILOT-IDLE nothing safe left to do."}]}}
    ]
    create_transcript(session, evs, mtime=now - 300)

    autopilot.set_away(True, now=now - 60)

    typed = []
    monkeypatch.setattr(autopilot, "session_pid", lambda s: 12345)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: True)
    monkeypatch.setattr(autopilot, "type_into", lambda p, t: typed.append((p, t)))

    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    autopilot.run_autopilot(cfg, bot=bot, now=now)

    assert len(typed) == 0
    assert len(bot.calls) == 0


def test_autopilot_away_mode_max_25_nudges(monkeypatch, bot):
    session = "sess-away-max"
    create_session(session, thread_id=206)

    now = 1000000.0
    evs = [
        {"type": "user", "message": {"content": "work"}},
        {"type": "assistant", "uuid": "u-away-25",
         "message": {"content": [{"type": "text", "text": "Waiting on decision."}]}}
    ]
    create_transcript(session, evs, mtime=now - 300)

    autopilot.set_away(True, now=now - 60)
    mem = {session: {"away_nudges": 25, "last": "older-uuid"}}
    autopilot.save_memory(mem)

    typed = []
    monkeypatch.setattr(autopilot, "session_pid", lambda s: 12345)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: True)
    monkeypatch.setattr(autopilot, "type_into", lambda p, t: typed.append((p, t)))

    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    autopilot.run_autopilot(cfg, bot=bot, now=now)

    # Exceeded max 25 nudges -> no nudge
    assert len(typed) == 0

    # Turning away mode off clears away_nudges
    autopilot.set_away(False)
    autopilot.run_autopilot(cfg, bot=bot, now=now)
    mem2 = autopilot.load_memory()
    assert "away_nudges" not in mem2.get(session, {})


def test_autopilot_only_at_prompt(monkeypatch, bot):
    session = "sess-busy"
    create_session(session, thread_id=207)

    now = 1000000.0
    evs = [
        {"type": "user", "message": {"content": "work"}},
        {"type": "assistant", "uuid": "u-busy",
         "message": {"content": [{"type": "text", "text": "Waiting for quota."}]}}
    ]
    create_transcript(session, evs, mtime=now - 300)

    typed = []
    monkeypatch.setattr(autopilot, "session_pid", lambda s: 12345)
    # Pane is NOT at prompt (busy running code)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: False)
    monkeypatch.setattr(autopilot, "type_into", lambda p, t: typed.append((p, t)))

    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    autopilot.run_autopilot(cfg, bot=bot, now=now)

    assert len(typed) == 0


def test_autopilot_dry_run(monkeypatch, bot, capsys):
    monkeypatch.setattr(autopilot, "account_room", lambda *a, **k: 10)
    session = "sess-dryrun"
    create_session(session, thread_id=208)

    now = 1000000.0
    evs = [
        {"type": "user", "message": {"content": "work"}},
        {"type": "assistant", "uuid": "u-dry",
         "message": {"content": [{"type": "text", "text": "Waiting for 5-hour quota reset."}]}}
    ]
    create_transcript(session, evs, mtime=now - 300)

    typed = []
    monkeypatch.setattr(autopilot, "session_pid", lambda s: 12345)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: True)
    monkeypatch.setattr(autopilot, "type_into", lambda p, t: typed.append((p, t)))

    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    autopilot.run_autopilot(cfg, bot=bot, dry_run=True, now=now)

    assert len(typed) == 0
    assert len(bot.calls) == 0
    captured = capsys.readouterr()
    assert session[:8] in captured.out
    assert "quota" in captured.out


def test_away_toggle_in_global_menu(bot):
    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    inbox_inst = inbox.Inbox(state_dir() / "inbox")
    tmap = topics.TopicMap(state_dir() / "topics.json", bot, cfg.owner_chat_id)

    # Initial state: away mode is off
    assert not autopilot.away_active()
    gm = inbox.global_markup()
    assert any("🌙 מצב היעדרות: כבוי" in btn["text"] for row in gm["inline_keyboard"] for btn in row)

    # Toggle on via more:away
    q = {"from": {"id": 1}, "data": "more:away", "message": {"chat": {"id": 1}, "message_id": 50}}
    res = inbox.more_button(q, cfg, bot, q["message"], tmap, inbox_inst, "sess-x")
    assert res == "settings"
    assert autopilot.away_active()
    gm = inbox.global_markup()
    assert any("🌙 מצב היעדרות: פעיל" in btn["text"] for row in gm["inline_keyboard"] for btn in row)

    # Toggle off via more:away
    res = inbox.more_button(q, cfg, bot, q["message"], tmap, inbox_inst, "sess-x")
    assert res == "settings"
    assert not autopilot.away_active()
    gm = inbox.global_markup()
    assert any("🌙 מצב היעדרות: כבוי" in btn["text"] for row in gm["inline_keyboard"] for btn in row)


def test_away_auto_off_on_owner_message(bot, monkeypatch):
    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    session = "sess-owner-msg"
    create_session(session, thread_id=301)

    autopilot.set_away(True)
    assert autopilot.away_active()

    def no_tmux(*args):  # the message reaches a session whose pane cannot be read: as with no tmux at all
        raise OSError("tmux unavailable")

    monkeypatch.setattr(inbox, "_tmux", no_tmux)
    # The queued message wakes the session from a daemon thread that can outlive this test (and then
    # reach the real tmux once the stub above is gone): the hand-over is not what this test checks.
    monkeypatch.setattr(inbox, "_reopen_or_nudge", lambda box, session: None)
    tmap = topics.TopicMap(state_dir() / "topics.json", bot, cfg.owner_chat_id)
    inbox_inst = inbox.Inbox(state_dir() / "inbox")
    media = inbox.Media(cfg, bot)

    msg = {
        "message_id": 99,
        "date": time.time(),
        "chat": {"id": 1, "type": "private"},
        "from": {"id": 1, "is_bot": False},
        "message_thread_id": 301,
        "text": "hello from phone"
    }

    inbox.handle({"message": msg}, cfg, bot, tmap, inbox_inst, media, time.time())

    assert not autopilot.away_active()
    sends = bot.methods("sendMessage")
    assert any("ברוך שובך. מצב היעדרות כובה" in m.get("text", "") for m in sends)


def test_restart_session_buttons_and_kill(monkeypatch, bot):
    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    session = "sess-restart"
    create_session(session, thread_id=302)

    inbox_inst = inbox.Inbox(state_dir() / "inbox")
    tmap = topics.TopicMap(state_dir() / "topics.json", bot, cfg.owner_chat_id)

    # 1. More markup has restart button
    mm = inbox.more_markup()
    assert any(btn.get("callback_data") == "close:restart" for row in mm["inline_keyboard"] for btn in row)

    # 2. close:restart asks for confirmation
    q_ask = {"from": {"id": 1}, "data": "close:restart", "message": {"chat": {"id": 1}, "message_id": 70}}
    res = inbox.more_button(q_ask, cfg, bot, q_ask["message"], tmap, inbox_inst, session)
    assert res == "close"
    edits = bot.methods("editMessageText")
    assert any("להפעיל את הסשן מחדש?" in e.get("text", "") for e in edits)

    # 3. close:restartyes
    killed_signals = []
    resumed = []
    pids_alive = [11111]

    monkeypatch.setattr(inbox, "session_pid", lambda s: pids_alive[0] if pids_alive else None)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="node /bin/claude\n"))

    def fake_kill(pid, sig):
        killed_signals.append((pid, sig))
        pids_alive.clear()  # process terminates

    monkeypatch.setattr(os, "kill", fake_kill)
    def fake_resume(s):
        resumed.append(s)
        pids_alive.append(33333)  # the resumed claude process comes up
        return True

    monkeypatch.setattr(inbox, "resume_session", fake_resume)

    # Run background tasks synchronously for test
    monkeypatch.setattr(inbox, "_terminal_background", lambda s, action: action() or True)

    q_yes = {"from": {"id": 1}, "data": "close:restartyes", "message": {"chat": {"id": 1}, "message_id": 71}}
    res = inbox.more_button(q_yes, cfg, bot, q_yes["message"], tmap, inbox_inst, session)
    assert res == "close"

    # Verify SIGTERM (15) was sent
    assert (11111, 15) in killed_signals
    # Verify session was resumed
    assert session in resumed
    # The final message reports success, not "did not come up"
    assert any("הסשן הופעל מחדש (2/2)" in e.get("text", "") for e in bot.methods("editMessageText"))


def test_restart_session_does_not_kill_non_claude(monkeypatch, bot):
    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    session = "sess-restart-other"
    create_session(session, thread_id=303)

    inbox_inst = inbox.Inbox(state_dir() / "inbox")
    tmap = topics.TopicMap(state_dir() / "topics.json", bot, cfg.owner_chat_id)

    killed_signals = []
    resumed = []

    monkeypatch.setattr(inbox, "session_pid", lambda s: 22222)
    # Process is python or bash, NOT claude
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="/usr/bin/python3 other.py\n"))
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed_signals.append((pid, sig)))
    monkeypatch.setattr(inbox, "resume_session", lambda s: resumed.append(s))
    monkeypatch.setattr(inbox, "_terminal_background", lambda s, action: action() or True)

    q_yes = {"from": {"id": 1}, "data": "close:restartyes", "message": {"chat": {"id": 1}, "message_id": 72}}
    inbox.more_button(q_yes, cfg, bot, q_yes["message"], tmap, inbox_inst, session)

    # Process was NOT killed because command line did not contain "claude"
    assert len(killed_signals) == 0
    assert len(resumed) == 0


def test_topic_check_every_8th_prompt(bot):
    cfg = Config(bot_token="tok", owner_chat_id=1, auto_topic_names=False, topic_check_reminders=True)  # the old hint, for sessions without auto-naming
    session = "sess-topic-check"
    create_session(session, thread_id=401, name="My Task")

    data = {"session_id": session, "prompt": "regular prompt"}

    # Prompts 1 to 7: no hint
    for i in range(1, 8):
        out = hooks.prompt_output(data, cfg)
        context = (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "Topic check:" not in context

    # Prompt 8: hint appears!
    out = hooks.prompt_output(data, cfg)
    assert out is not None
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "Topic check: this session's Telegram topic is named \"My Task\"" in context
    assert 'ctb topic-rename "<name>"' in context

    # Prompt 9 to 15: no hint
    for i in range(9, 16):
        out = hooks.prompt_output(data, cfg)
        context = (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "Topic check:" not in context

    # Prompt 16: hint appears again!
    out = hooks.prompt_output(data, cfg)
    assert out is not None
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "Topic check:" in context


def test_topic_rename_cli_and_override_wins_over_app_title(bot):
    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    session = "sess-rename-override"
    create_session(session, thread_id=501, name="Old Task")

    tmap = topics.TopicMap(state_dir() / "topics.json", bot, cfg.owner_chat_id)

    # Rename via topic_rename
    ret = topics.topic_rename(cfg, "New Hebrew Task", session=session, bot=bot)
    assert ret == 0

    # Ensure topic map was updated
    all_topics = tmap.all()
    assert all_topics[session]["name"] == "New Hebrew Task"

    # Subsequent ensure() with an app title must NOT overwrite the renamed name
    tmap.ensure(session, "Claude App Generated Title")
    all_topics_after = tmap.all()
    assert all_topics_after[session]["name"] == "New Hebrew Task"


def test_only_the_dashboard_pass_invokes_autopilot(monkeypatch, bot):
    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    autopilot_runs = []
    monkeypatch.setattr(autopilot, "run_autopilot", lambda cfg, **kw: autopilot_runs.append(True) or 0)

    # Dashboard run
    dashboard.run(cfg, bot=bot)
    assert len(autopilot_runs) >= 1

    # Heartbeat run_once must NOT run it as well: one place only (the 5-minute dashboard pass)
    before = len(autopilot_runs)
    heartbeat.run_once(cfg, bot=bot)
    assert len(autopilot_runs) == before


def test_cli_subcommands(monkeypatch):
    called = []
    monkeypatch.setattr(autopilot, "run_autopilot", lambda cfg, dry_run=False: called.append(("autopilot", dry_run)) or 0)
    monkeypatch.setattr(topics, "topic_rename", lambda cfg, name, session: called.append(("rename", name, session)) or 0)
    # Save a minimal config
    from ctb.config import config_path
    write_private(config_path(), json.dumps({"bot_token": "123456:ABCdefghijklmnopqrstuvwxyz0123456789", "owner_chat_id": 1}))

    res = cli.main(["autopilot", "--dry-run"])
    assert res == 0
    assert called == [("autopilot", True)]

    called.clear()
    res = cli.main(["topic-rename", "project: task", "sess-1"])
    assert res == 0
    assert called == [("rename", "project: task", "sess-1")]



# --- account_room: the fuller of the 5-hour and weekly windows ---------------------------------------

def test_account_room_is_max_of_both_windows():
    now = 1000000.0
    usage = state_dir() / "claude-usage.json"
    write_private(usage, json.dumps({"five_hour": 30, "five_hour_resets": now + 100,
                                     "weekly": 88, "weekly_resets": now + 100000}))
    assert autopilot.account_room("s", now=now) == 88

    # A window whose reset has passed counts as 0
    write_private(usage, json.dumps({"five_hour": 30, "five_hour_resets": now + 100,
                                     "weekly": 88, "weekly_resets": now - 5}))
    assert autopilot.account_room("s", now=now) == 30

    # Ultra: no five_hour_resets key and no 5-hour use, only the weekly window counts
    write_private(usage, json.dumps({"weekly": 91, "weekly_resets": now + 100}))
    assert autopilot.account_room("s", now=now) == 91

    # Unknown usage stays unknown
    usage.unlink()
    assert autopilot.account_room("s", now=now) is None


def test_autopilot_blocked_by_full_weekly_window(monkeypatch, bot):
    session = "sess-weekly-full"
    create_session(session, thread_id=209)
    now = 1000000.0
    evs = [{"type": "user", "message": {"content": "x"}},
           {"type": "assistant", "uuid": "u-weekly",
            "message": {"content": [{"type": "text", "text": "Waiting for the quota to reset."}]}}]
    create_transcript(session, evs, mtime=now - 300)
    write_private(state_dir() / "claude-usage.json",
                  json.dumps({"five_hour": 10, "five_hour_resets": now + 100,
                              "weekly": 92, "weekly_resets": now + 50000}))
    typed = []
    monkeypatch.setattr(autopilot, "session_pid", lambda s: 1)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: True)
    monkeypatch.setattr(autopilot, "type_into", lambda p, t: typed.append(t))
    autopilot.run_autopilot(Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True), bot=bot, now=now)
    assert typed == []


def test_quota_prompt_says_window_at_percent(monkeypatch, bot):
    session = "sess-prompt-text"
    create_session(session, thread_id=210)
    now = 1000000.0
    evs = [{"type": "user", "message": {"content": "x"}},
           {"type": "assistant", "uuid": "u-prompt",
            "message": {"content": [{"type": "text", "text": "Waiting for the quota to reset."}]}}]
    create_transcript(session, evs, mtime=now - 300)
    write_private(state_dir() / "claude-usage.json", json.dumps({"five_hour": 40, "weekly": 62}))
    typed = []
    monkeypatch.setattr(autopilot, "session_pid", lambda s: 1)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: True)
    monkeypatch.setattr(autopilot, "type_into", lambda p, t: typed.append(t))
    autopilot.run_autopilot(Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True), bot=bot, now=now)
    assert len(typed) == 1 and "(window at 62%)" in typed[0]


# --- plugins page -------------------------------------------------------------------------------------

PLUGIN_JSON = json.dumps([{"id": "zeta@market", "enabled": False}, {"id": "alpha@market", "enabled": True},
                          {"enabled": True}])


def _fake_claude(monkeypatch, calls, list_out=PLUGIN_JSON, toggle_rc=0):
    monkeypatch.setattr(accounts, "claude_binary", lambda: "claude")
    monkeypatch.setattr(inbox, "_terminal_background", lambda s, action: action() or True)
    def fake_run(cmd, **kw):
        calls.append((list(cmd), kw.get("env")))
        if cmd[:3] == ["claude", "plugin", "list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=list_out, stderr="")
        return subprocess.CompletedProcess(cmd, toggle_rc, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)


def test_more_menu_has_plugins_button_next_to_account():
    row = next(r for r in inbox.more_markup()["inline_keyboard"]
               if any(b["callback_data"] == "more:/account" for b in r))
    assert [b["callback_data"] for b in row] == ["more:/account", "more:plugins"]
    assert row[1]["text"] == "🧩 תוספים"


def test_plugin_list_sorted_and_skips_items_without_id(monkeypatch):
    calls = []
    _fake_claude(monkeypatch, calls)
    assert inbox.plugin_list("s1") == [("alpha@market", True), ("zeta@market", False)]
    # also the {"installed": [...]} shape
    _fake_claude(monkeypatch, calls, list_out=json.dumps({"installed": [{"id": "b@m", "enabled": 1}]}))
    assert inbox.plugin_list("s1") == [("b@m", True)]


def test_plugins_page_rows_and_restart_button(monkeypatch):
    _fake_claude(monkeypatch, [])
    text, markup = inbox.plugins_page("s1")
    rows = markup["inline_keyboard"]
    assert [r[0]["text"] for r in rows[:2]] == ["✅ alpha", "⬜ zeta"]
    assert [r[0]["callback_data"] for r in rows] == ["more:pl:0", "more:pl:1", "more:back"]
    assert "תוספים בחשבון של הסשן הזה" in text
    # after a toggle a restart button appears before "back"
    text2, markup2 = inbox.plugins_page("s1", note="✅ הופעל: \nzeta")
    assert [r[0]["callback_data"] for r in markup2["inline_keyboard"]][-2:] == ["close:restart", "more:back"]
    assert text2.endswith("zeta")


def test_plugins_buttons_open_and_toggle(monkeypatch, bot):
    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    inbox_inst = inbox.Inbox(state_dir() / "inbox")
    tmap = topics.TopicMap(state_dir() / "topics.json", bot, cfg.owner_chat_id)
    calls = []
    _fake_claude(monkeypatch, calls)
    msg = {"chat": {"id": 1}, "message_id": 80}

    q = {"from": {"id": 1}, "data": "more:plugins", "message": msg}
    assert inbox.more_button(q, cfg, bot, msg, tmap, inbox_inst, "sess-pl") == "plugins"
    assert "תוספים בחשבון" in bot.methods("editMessageText")[-1]["text"]

    # index 0 is alpha (enabled): pressing it disables it
    q = {"from": {"id": 1}, "data": "more:pl:0", "message": msg}
    inbox.more_button(q, cfg, bot, msg, tmap, inbox_inst, "sess-pl")
    assert (["claude", "plugin", "disable", "alpha@market"]) in [c for c, _ in calls]
    last = bot.methods("editMessageText")[-1]
    assert "⬜ כובה: " in last["text"] and "יחול אחרי הפעלה מחדש" in last["text"]
    assert any(b["callback_data"] == "close:restart" for r in last["reply_markup"]["inline_keyboard"] for b in r)

    # index 1 is zeta (disabled): pressing it enables it
    q = {"from": {"id": 1}, "data": "more:pl:1", "message": msg}
    inbox.more_button(q, cfg, bot, msg, tmap, inbox_inst, "sess-pl")
    assert (["claude", "plugin", "enable", "zeta@market"]) in [c for c, _ in calls]
    assert "✅ הופעל: " in bot.methods("editMessageText")[-1]["text"]


def test_plugins_toggle_failure_and_bad_index(monkeypatch, bot):
    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    inbox_inst = inbox.Inbox(state_dir() / "inbox")
    tmap = topics.TopicMap(state_dir() / "topics.json", bot, cfg.owner_chat_id)
    msg = {"chat": {"id": 1}, "message_id": 81}
    _fake_claude(monkeypatch, [], toggle_rc=1)
    q = {"from": {"id": 1}, "data": "more:pl:0", "message": msg}
    inbox.more_button(q, cfg, bot, msg, tmap, inbox_inst, "sess-pl")
    assert "⚠️ השינוי נכשל: " in bot.methods("editMessageText")[-1]["text"]

    # a stale button (list changed) shows the error page with a back button
    q = {"from": {"id": 1}, "data": "more:pl:9", "message": msg}
    inbox.more_button(q, cfg, bot, msg, tmap, inbox_inst, "sess-pl")
    last = bot.methods("editMessageText")[-1]
    assert "לא הצלחתי לקרוא את רשימת התוספים" in last["text"]
    assert last["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "more:back"


def test_plugins_use_the_sessions_account_env(monkeypatch):
    (accounts.profiles_dir() / "work").mkdir(parents=True)
    accounts.record("sess-w", "work")
    calls = []
    _fake_claude(monkeypatch, calls)
    inbox.plugin_list("sess-w")
    assert calls[0][1]["CLAUDE_CONFIG_DIR"] == str(accounts.profiles_dir() / "work")


# --- the REAL at_prompt on captured screens (only tmux is mocked) ------------------------------------

def _screen(monkeypatch, text, rc=0):
    monkeypatch.setattr(inbox, "_tmux", lambda *a: subprocess.CompletedProcess(a, rc, stdout=text, stderr=""))


IDLE_BOX = """● Done. The PR is open.

╭──────────────────────────────────────────────╮
│ ❯                                            │
╰──────────────────────────────────────────────╯
  ? for shortcuts
"""

PERMISSION_MENU = """ Bash command

   rm -rf build/

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for rm commands
   3. No, and tell Claude what to do differently (esc)

 Esc to cancel
"""

PLAN_MENU = """ Would you like to proceed?

 ❯ 1. Yes, and bypass permissions
   2. Yes, manually approve edits
   3. No, keep planning
"""

BUSY = """● Running tests…
  ✻ Working… (12s · esc to interrupt)

╭──────────────────────────────────────────────╮
│ ❯                                            │
╰──────────────────────────────────────────────╯
"""


def test_at_prompt_real_screens(monkeypatch):
    _screen(monkeypatch, IDLE_BOX)
    assert autopilot.at_prompt("%1") is True
    _screen(monkeypatch, "❯ \n")  # bare prompt without a frame
    assert autopilot.at_prompt("%1") is True
    for menu in (PERMISSION_MENU, PLAN_MENU):
        _screen(monkeypatch, menu)
        assert autopilot.at_prompt("%1") is False
    _screen(monkeypatch, "Select one\n ❯ 2. Option\n")
    assert autopilot.at_prompt("%1") is False
    _screen(monkeypatch, "Enter to select · ↑/↓ to navigate\n ❯ pick\n")
    assert autopilot.at_prompt("%1") is False
    _screen(monkeypatch, BUSY)
    assert autopilot.at_prompt("%1") is False
    _screen(monkeypatch, IDLE_BOX, rc=1)
    assert autopilot.at_prompt("%1") is False


def test_at_prompt_tmux_missing_is_false(monkeypatch):
    def boom(*a):
        raise OSError("tmux unavailable")
    monkeypatch.setattr(inbox, "_tmux", boom)
    assert autopilot.at_prompt("%1") is False


# --- type_into checks tmux return codes ---------------------------------------------------------------

def test_type_into_failure_records_no_nudge_and_sends_no_note(monkeypatch, bot):
    monkeypatch.setattr(autopilot, "account_room", lambda *a, **k: 10)
    session = "sess-sendfail"
    create_session(session, thread_id=211)
    now = 1000000.0
    evs = [{"type": "user", "message": {"content": "x"}},
           {"type": "assistant", "uuid": "u-fail",
            "message": {"content": [{"type": "text", "text": "Waiting for the quota to reset."}]}}]
    create_transcript(session, evs, mtime=now - 300)
    monkeypatch.setattr(autopilot, "session_pid", lambda s: 1)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: True)
    sent = []

    def fake_tmux(*args):
        sent.append(args)
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="no pane")
    monkeypatch.setattr(inbox, "_tmux", fake_tmux)
    autopilot.run_autopilot(Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True), bot=bot, now=now)
    keys = [a for a in sent if a[0] == "send-keys"]  # the screen is read first (a tip notice is closed)
    assert keys and keys[0][:4] == ("send-keys", "-t", "%1", "-l")
    assert not bot.methods("sendMessage")
    assert autopilot.load_memory().get(session, {}).get("last") is None


def test_type_into_raises_when_enter_fails(monkeypatch):
    monkeypatch.setattr(inbox, "_tmux", lambda *a: subprocess.CompletedProcess(
        a, 1 if a[-1] == "Enter" else 0, stdout=""))  # the text goes in, Enter fails
    with pytest.raises(OSError):
        autopilot.type_into("%1", "hello")


# --- quota nudges are bounded ---------------------------------------------------------------------------

def _quota_session(name, thread, uuid):
    create_session(name, thread_id=thread)
    evs = [{"type": "user", "message": {"content": "x"}},
           {"type": "assistant", "uuid": uuid,
            "message": {"content": [{"type": "text", "text": "Waiting for the quota to reset."}]}}]
    create_transcript(name, evs, mtime=1000000.0 - 300)


def test_quota_nudges_limited_to_three_per_five_hours(monkeypatch, bot):
    now = 1000000.0
    _quota_session("sess-qn", 212, "u-qn")
    write_private(state_dir() / "claude-usage.json", json.dumps({"five_hour": 10, "weekly": 10}))
    typed = []
    monkeypatch.setattr(autopilot, "session_pid", lambda s: 1)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: True)
    monkeypatch.setattr(autopilot, "type_into", lambda p, t: typed.append(t))
    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    autopilot.save_memory({"sess-qn": {"quota_nudges": [now - 100, now - 200, now - 300]}})
    autopilot.run_autopilot(cfg, bot=bot, now=now)
    assert typed == []
    # two of them older than 5 hours: room for another
    autopilot.save_memory({"sess-qn": {"quota_nudges": [now - 100, now - 6 * 3600, now - 7 * 3600]}})
    autopilot.run_autopilot(cfg, bot=bot, now=now)
    assert len(typed) == 1
    assert autopilot.load_memory()["sess-qn"]["quota_nudges"] == [now - 100, now]


def test_quota_nudge_needs_room_under_70(monkeypatch, bot):
    now = 1000000.0
    _quota_session("sess-q70", 213, "u-q70")
    write_private(state_dir() / "claude-usage.json", json.dumps({"five_hour": 75, "weekly": 10}))
    typed = []
    monkeypatch.setattr(autopilot, "session_pid", lambda s: 1)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: True)
    monkeypatch.setattr(autopilot, "type_into", lambda p, t: typed.append(t))
    autopilot.run_autopilot(Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True), bot=bot, now=now)
    assert typed == []


# --- away mode ends by itself ---------------------------------------------------------------------------

def _owner_prompt(content, ts, **extra):
    return dict({"type": "user", "timestamp": ts, "message": {"content": content}}, **extra)


def test_owner_back_detects_a_hand_typed_prompt():
    since = time.time() - 3600
    later = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(since + 600))
    soon = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(since + 30))
    create_transcript("sess-ob", [
        _owner_prompt("[Autopilot: the owner is away] go on", later),   # our own nudge
        _owner_prompt("<command-name>/x</command-name>", later),        # system wrapper
        _owner_prompt("meta", later, isMeta=True),
        _owner_prompt([{"type": "text", "text": "blocks are tool results, not typing"}], later),
        _owner_prompt("typed too early", soon),                         # within since+60
    ])
    assert autopilot.owner_back(since) is False
    create_transcript("sess-ob2", [_owner_prompt("what is the status?", later)])
    assert autopilot.owner_back(since) is True
    # a transcript untouched since away began is not even read
    old = time.time() - 10
    create_transcript("sess-ob3", [_owner_prompt("old", later)], mtime=old)
    assert autopilot.owner_back(time.time()) is False


def test_away_ends_when_owner_typed_in_any_session(monkeypatch, bot):
    now = time.time()
    autopilot.set_away(True)
    since = json.loads(autopilot.away_path().read_text())["since"]
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(since + 600))
    create_transcript("sess-typed", [_owner_prompt("I am back", ts)])
    autopilot.run_autopilot(Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True), bot=bot, now=now)
    assert not autopilot.away_active()


def test_dry_run_keeps_away_file_even_when_owner_back(bot):
    now = time.time()
    autopilot.set_away(True)
    since = json.loads(autopilot.away_path().read_text())["since"]
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(since + 600))
    create_transcript("sess-typed2", [_owner_prompt("I am back", ts)])
    autopilot.run_autopilot(Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True), bot=bot, dry_run=True, now=now)
    assert autopilot.away_active()


def test_away_expires_after_48_hours(bot):
    autopilot.set_away(True)
    since = json.loads(autopilot.away_path().read_text())["since"]
    autopilot.run_autopilot(Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True), bot=bot, now=since + 47 * 3600)
    assert autopilot.away_active()
    autopilot.run_autopilot(Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True), bot=bot, now=since + 49 * 3600)
    assert not autopilot.away_active()


# --- one pass at a time -----------------------------------------------------------------------------------

def test_overlapping_pass_does_nothing(monkeypatch, bot):
    monkeypatch.setattr(autopilot, "account_room", lambda *a, **k: 10)
    import fcntl
    _quota_session("sess-lock", 214, "u-lock")
    typed = []
    monkeypatch.setattr(autopilot, "session_pid", lambda s: 1)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: True)
    monkeypatch.setattr(autopilot, "type_into", lambda p, t: typed.append(t))
    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    with open(state_dir() / "autopilot.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert autopilot.run_autopilot(cfg, bot=bot, now=1000000.0) == 0
        assert typed == []
    autopilot.run_autopilot(cfg, bot=bot, now=1000000.0)
    assert len(typed) == 1


# --- claude binary + plugins off the poll loop ----------------------------------------------------------

def test_claude_binary_finds_local_bin_when_path_is_short(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr("shutil.which", lambda name: None)
    local = Path.home() / ".local" / "bin"
    local.mkdir(parents=True)
    exe = local / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert accounts.claude_binary() == str(exe)


def test_plugins_use_the_resolved_binary_and_run_in_background(monkeypatch, bot):
    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    inbox_inst = inbox.Inbox(state_dir() / "inbox")
    tmap = topics.TopicMap(state_dir() / "topics.json", bot, cfg.owner_chat_id)
    calls, queued = [], []
    _fake_claude(monkeypatch, calls)
    monkeypatch.setattr(accounts, "claude_binary", lambda: "/home/u/.local/bin/claude")
    monkeypatch.setattr(inbox, "_terminal_background", lambda s, action: queued.append(action) or True)

    def fake_run(cmd, **kw):
        calls.append((list(cmd), kw.get("env")))
        return subprocess.CompletedProcess(cmd, 0, stdout=PLUGIN_JSON, stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    msg = {"chat": {"id": 1}, "message_id": 90}
    q = {"from": {"id": 1}, "data": "more:plugins", "message": msg}
    assert inbox.more_button(q, cfg, bot, msg, tmap, inbox_inst, "sess-bg") == "plugins"
    assert calls == [] and not bot.methods("editMessageText")  # nothing ran on the poll loop
    queued[0]()
    assert calls[0][0][0] == "/home/u/.local/bin/claude"
    assert bot.methods("editMessageText")


def test_plugins_busy_when_session_already_has_a_terminal_action(monkeypatch, bot):
    cfg = Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True)
    inbox_inst = inbox.Inbox(state_dir() / "inbox")
    tmap = topics.TopicMap(state_dir() / "topics.json", bot, cfg.owner_chat_id)
    monkeypatch.setattr(inbox, "_terminal_background", lambda s, action: False)
    msg = {"chat": {"id": 1}, "message_id": 91}
    q = {"from": {"id": 1}, "data": "more:plugins", "message": msg}
    assert inbox.more_button(q, cfg, bot, msg, tmap, inbox_inst, "sess-busy") == "busy"


def test_away_mode_never_wakes_a_session_left_behind_long_before_the_owner_left(monkeypatch, bot):
    """Away mode continues the sessions that were really in use; an old idle one stays asleep."""
    now = 1000000.0
    evs = [{"type": "user", "message": {"content": "work"}},
           {"type": "assistant", "uuid": "u-old", "message": {"content": [{"type": "text", "text": "Option A or B?"}]}}]
    create_session("sess-old", thread_id=401)
    create_transcript("sess-old", evs, mtime=now - 60 - (autopilot.AWAY_RECENT_HOURS + 1) * 3600)
    create_session("sess-fresh", thread_id=402)
    create_transcript("sess-fresh", [evs[0], dict(evs[1], uuid="u-fresh")], mtime=now - 60 - 3600)
    autopilot.set_away(True, now=now - 60)
    typed = []
    monkeypatch.setattr(autopilot, "session_pid", lambda s: 12345)
    monkeypatch.setattr(autopilot, "pane_for_session", lambda s: "pane-" + s)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: True)
    monkeypatch.setattr(autopilot, "type_into", lambda p, t: typed.append(p))
    autopilot.run_autopilot(Config(bot_token="tok", owner_chat_id=1, quota_auto_continue=True), bot=bot, now=now)
    assert typed == ["pane-sess-fresh"]
    assert autopilot.in_away_scope(now - 3600, now) and not autopilot.in_away_scope(now - 7 * 3600, now)
