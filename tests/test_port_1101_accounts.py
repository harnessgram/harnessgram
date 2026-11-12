"""Tests for ported accounts and quota features (Chunk 2):
1. Candidate sorting by (is_big_plan, used%) with emailAddress verification
2. Limit hit cooldown & freeing on window reset or newer reading
3. Small-plan-first default account switching
4. Idle rebalance with 3h cooldown and opt-out
5. StopFailure hook registration, dedupe, and no topic rename
6. Limit-cleared marker & auto move now
7. Reopening a closed session picks a roomier account
8. Weekly-only accounts (no 5h window) display in footer, dashboard, and keyboard
"""
import datetime
import hashlib
import json
import os
import time
from pathlib import Path

from conftest import GOOD_TOKEN, FakeBot
from ctb import accounts, hooks as H, inbox as I, mirror as M, usage
from ctb.config import Config, state_dir, write_private
from ctb.install import desired_hooks
from ctb.topics import TopicMap

OWNER = 12345


def cfg(**kw):
    base = dict(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    base.update(kw)
    return Config(**base)


def setup_account(name: str, email: str, tier: str, is_main: bool = False):
    """Set up account config, cached email, and profiles."""
    if not is_main:
        accounts.add(name)
        cfg_json = accounts.profiles_dir() / name / ".claude.json"
    else:
        cfg_json = accounts.claude_json()
    cfg_json.parent.mkdir(parents=True, exist_ok=True)
    cfg_json.write_text(json.dumps({
        "oauthAccount": {
            "emailAddress": email,
            "organizationRateLimitTier": tier
        }
    }))
    emails_file = state_dir() / "account-emails.json"
    emails = json.loads(emails_file.read_text()) if emails_file.exists() else {}
    emails[name] = email
    write_private(emails_file, json.dumps(emails))


def write_usage(name: str, five_hour: float, weekly: float, five_resets: float = 0,
                weekly_resets: float = 0, at: str = ""):
    data = {
        "five_hour": five_hour,
        "weekly": weekly,
        "five_hour_resets": five_resets,
        "weekly_resets": weekly_resets,
        "at": at or "2026-11-01T00:00:00Z"
    }
    write_private(accounts.usage_path(name), json.dumps(data))


def iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


# --- 1. Account choice when one runs out: sort by (is_big_plan, used%) ----------

def test_big_plan_requires_matching_email_and_20x():
    setup_account("main", "main@example.com", "default_claude_max_20x", is_main=True)
    setup_account("pro1", "pro1@example.com", "default_claude_pro")
    setup_account("copied", "copied@example.com", "default_claude_max_20x")

    assert accounts.is_big_plan("main") is True
    assert accounts.is_big_plan("pro1") is False

    # A copied profile carrying another account's email must not be trusted
    copied_json = accounts.profiles_dir() / "copied" / ".claude.json"
    copied_json.write_text(json.dumps({
        "oauthAccount": {
            "emailAddress": "stolen@example.com",  # does not match cached_email
            "organizationRateLimitTier": "default_claude_max_20x"
        }
    }))
    assert accounts.is_big_plan("copied") is False


def test_candidates_sorted_small_first_and_lowest_used_first():
    now = 1_000_000.0
    setup_account("main", "main@example.com", "default_claude_max_20x", is_main=True)
    setup_account("pro1", "pro1@example.com", "default_claude_pro")
    setup_account("pro2", "pro2@example.com", "default_claude_pro")

    # main has 10% used, pro1 has 40% used, pro2 has 20% used
    write_usage("main", 10, 10, five_resets=now + 3600, weekly_resets=now + 86400)
    write_usage("pro1", 40, 30, five_resets=now + 3600, weekly_resets=now + 86400)
    write_usage("pro2", 20, 15, five_resets=now + 3600, weekly_resets=now + 86400)

    # When fallback_plan is called from session running on main:
    plan = accounts.fallback_plan("s1", now=now, env={"CLAUDE_CONFIG_DIR": ""})
    assert plan["here"] == "main"
    # pro2 (20%) should come before pro1 (40%)
    assert plan["fresh"] == ["pro2", "pro1"]

    # When fallback_plan is called from pro1:
    plan_pro1 = accounts.fallback_plan("s2", now=now, env={"CLAUDE_CONFIG_DIR": str(accounts.config_dir("pro1"))})
    # smaller plan pro2 (20%) before big plan main (10%)
    assert plan_pro1["fresh"] == ["pro2", "main"]


# --- 2. Limit hit cooldown & freeing --------------------------------------------

def test_limit_hit_makes_account_full_unless_freed():
    now = 1_000_000.0
    setup_account("pro1", "pro1@example.com", "pro")
    # a reading from before the hit below (the hit is 1 hour ago, the reading 2 hours ago)
    write_usage("pro1", 20, 10, five_resets=now + 7200, weekly_resets=now + 86400, at=iso(now - 7200))

    # 1. Without limit hit: used is 20
    assert accounts.used("pro1", now=now) == 20.0

    # 2. Limit hit 1 hour ago: counts as 100
    accounts.note_limit("pro1", now=now - 3600)
    assert accounts.used("pro1", now=now) == 100.0

    # 3. Freed exception 1: 5-hour window reset AFTER the hit (t_hit < reset < now)
    write_usage("pro1", 10, 5, five_resets=now - 1800, weekly_resets=now + 86400, at=iso(now - 7200))
    assert accounts.used("pro1", now=now) == 5.0

    # 4. Freed exception 2: usage reading newer than hit shows level < 95%
    # hit 2 hours ago (now - 7200), reading 30 mins ago (now - 1800) with level 45%
    accounts.note_limit("pro1", now=now - 7200)
    write_usage("pro1", 45, 30, five_resets=now + 3600, weekly_resets=now + 86400, at=iso(now - 1800))
    assert accounts.used("pro1", now=now) == 45.0


# --- 3. Small-plan-first default switching --------------------------------------

def test_default_account_switches_to_small_plan_with_room():
    from ctb.config import config_path
    write_private(config_path(), '{"release_defaults_v1": true, "auto_account_switch": true}')
    now = 1_000_000.0
    setup_account("main", "main@example.com", "default_claude_max_20x", is_main=True)
    setup_account("pro1", "pro1@example.com", "pro")
    accounts.use("main")
    assert accounts.current() == "main"

    # main is at 10%, pro1 is at 50% (< 80%)
    write_usage("main", 10, 10, five_resets=now + 3600, weekly_resets=now + 86400)
    write_usage("pro1", 50, 40, five_resets=now + 3600, weekly_resets=now + 86400)

    # Should switch default to smaller plan (pro1) even though main has room
    target = accounts.check_default_account(now=now)
    assert target == "pro1"
    assert accounts.current() == "pro1"

    # When pro1 reaches 95%, it switches to roomiest (main)
    write_usage("pro1", 95, 90, five_resets=now + 3600, weekly_resets=now + 86400)
    target2 = accounts.check_default_account(now=now)
    assert target2 == "main"
    assert accounts.current() == "main"


# --- 4. Idle rebalance ----------------------------------------------------------

def test_idle_rebalance_moves_session_without_continue(monkeypatch):
    from ctb.config import config_path
    write_private(config_path(), '{"release_defaults_v1": true, "auto_account_switch": true}')
    now = 1_000_000.0
    setup_account("main", "main@example.com", "default_claude_max_20x", is_main=True)
    setup_account("pro1", "pro1@example.com", "pro")
    write_usage("main", 20, 20, five_resets=now + 3600, weekly_resets=now + 86400)
    write_usage("pro1", 30, 30, five_resets=now + 3600, weekly_resets=now + 86400)

    # Set up session on main
    sid = "sess-idle-1"
    accounts.record(sid, "main")

    # Set up topics map
    topics_file = state_dir() / "topics.json"
    write_private(topics_file, json.dumps({sid: {"thread": 101, "name": "Test Topic"}}))

    # Create session file under claude_home() / sessions / 999.json
    sessions_dir = accounts.claude_home() / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    sfile = sessions_dir / "999.json"
    sfile.write_text(json.dumps({
        "sessionId": sid,
        "pid": 999,
        "status": "idle",
        "statusUpdatedAt": (now - 2000) * 1000,  # 2000s ago (> 1800s)
        "tmux": "%1"
    }))

    # Mock os.kill to believe PID 999 is alive
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    # Mock subprocess.run for pgrep: no child shells
    import subprocess
    real_run = subprocess.run
    def fake_run(cmd, *a, **kw):
        if cmd[0] == "pgrep":
            return type("Proc", (), {"stdout": "1000 sleep\n", "returncode": 0})()
        return real_run(cmd, *a, **kw)
    monkeypatch.setattr(subprocess, "run", fake_run)

    spawned = []
    res = accounts.rebalance(now=now, spawn_fn=lambda args: spawned.append(args))
    assert res == (sid, "pro1")
    assert spawned == [["account", "auto-move", f"--session={sid}", "--thread=101", "--target=pro1", "--idle"]]

    # 3-hour cooldown prevents moving the same session again immediately
    spawned.clear()
    res2 = accounts.rebalance(now=now + 100, spawn_fn=lambda args: spawned.append(args))
    assert res2 is None
    assert spawned == []

    # Opt-out via auto_enabled:
    accounts.toggle_auto()  # turn off
    assert accounts.auto_enabled() is False
    res3 = accounts.rebalance(now=now + 4 * 3600, spawn_fn=lambda args: spawned.append(args))
    assert res3 is None
    accounts.toggle_auto()  # turn back on


# --- 5. StopFailure hook & dedupe -----------------------------------------------

def test_stop_failure_hook_registered_in_settings():
    hooks = desired_hooks()
    assert "StopFailure" in hooks
    cmds = [h["command"] for group in hooks["StopFailure"] for h in group["hooks"]]
    assert any("hook-stop-failure" in c for c in cmds)


def test_stop_failure_triggers_limit_offer_with_dedupe(monkeypatch):
    bot = FakeBot()
    c = cfg()
    setup_account("main", "main@example.com", "default_claude_max_20x", is_main=True)
    setup_account("pro1", "pro1@example.com", "pro")
    write_usage("pro1", 20, 20, five_resets=time.time() + 3600, weekly_resets=time.time() + 86400)

    sid = "sess-fail-1"
    topics_file = state_dir() / "topics.json"
    write_private(topics_file, json.dumps({sid: {"thread": 202, "name": "Topic Name"}}))
    accounts.record(sid, "main")

    monkeypatch.setattr(H, "load_config", lambda: c)
    monkeypatch.setattr(M.Bot, "from_config", lambda config: bot)

    spawned = []
    monkeypatch.setattr(H, "spawn", lambda args: spawned.append(args))

    payload = {
        "session_id": sid,
        "error": "API Error: You've hit your limit",
        "last_assistant_message": "API Error: You've hit your limit for today. Resets 8pm.",
        "transcript_path": "/tmp/t.jsonl"
    }

    # First call: fires limit offer
    res1 = H.hook_stop_failure(json.dumps(payload))
    assert res1 == 0
    msgs = bot.methods("sendMessage")
    assert len(msgs) == 1
    assert msgs[0]["message_thread_id"] == 202
    # Verify topic name is untouched
    saved_topics = json.loads(topics_file.read_text())
    assert saved_topics[sid]["name"] == "Topic Name"

    # Second call within 300s: deduped! No second message or move
    bot.calls.clear()
    res2 = H.hook_stop_failure(json.dumps(payload))
    assert res2 == 0
    assert bot.methods("sendMessage") == []


# --- 6. Limit-cleared marker & auto move now -----------------------------------

def test_limit_cleared_marker_clears_error(tmp_path, monkeypatch):
    sid = "sess-clear-1"
    now = time.time()
    reset_epoch = now + 7200
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    # reset_time reads the clock on Jerusalem time; build it there so CI in UTC agrees
    reset_clock = datetime.fromtimestamp(reset_epoch, ZoneInfo("Asia/Jerusalem")).strftime("%H:%M")
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Create transcript with limit error
    proj_dir = tmp_path / "projects" / "p1"
    proj_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(usage, "PROJECTS", tmp_path / "projects")

    t_file = proj_dir / f"{sid}.jsonl"
    err_row = {
        "type": "assistant",
        "isApiErrorMessage": True,
        "apiError": "usage_limit_reached",
        "timestamp": stamp,
        "message": {"content": [{"type": "text", "text": f"Claude AI usage limit reached. resets {reset_clock}"}]}
    }
    t_file.write_text(json.dumps(err_row) + "\n")

    # Initial limit_of sees the error
    lim = usage.limit_of(sid)
    assert lim is not None
    err_id = lim["id"]

    # Mark cleared (as auto_move does)
    mark_path = state_dir() / "limit-cleared" / sid
    mark_path.parent.mkdir(parents=True, exist_ok=True)
    mark_path.write_text(err_id)

    # limit_of now returns None because of cleared marker
    assert usage.limit_of(sid) is None

    # limit_notice in inbox does not hold or reply with quota error
    bot = FakeBot()
    m = {"message_id": 50, "text": "Hello"}
    assert I.limit_notice(bot, OWNER, m, sid) is False


# --- 7. Reopening closed session picks roomier account -------------------------

def test_resume_session_switches_when_nearly_out(monkeypatch):
    now = 1_000_000.0
    setup_account("pro1", "pro1@example.com", "pro")
    setup_account("pro2", "pro2@example.com", "pro")
    write_usage("pro1", 96, 96, five_resets=now + 3600, weekly_resets=now + 86400)
    write_usage("pro2", 20, 20, five_resets=now + 3600, weekly_resets=now + 86400)

    sid = "sess-resume-1"
    accounts.record(sid, "pro1")
    assert accounts.account_for(sid) == "pro1"

    tmux_cmds = []
    def fake_tmux(*args):
        tmux_cmds.append(args)
        # no window of that name exists yet; the new one shows the prompt
        return type("Proc", (), {"returncode": 1 if args[0] == "display" else 0, "stdout": "❯ "})()
    monkeypatch.setattr(I, "_tmux", fake_tmux)
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    ok = I.resume_session(sid)
    assert ok is True
    # Session account record was updated to pro2!
    assert accounts.account_for(sid) == "pro2"
    # Shell command in tmux launch used pro2
    launched_cmd = " ".join(next(c for c in tmux_cmds if c[0] == "new-session"))
    assert "pro2" in launched_cmd


# --- 8. Weekly-only accounts (no 5h window) -----------------------------------

def test_weekly_only_quota_display():
    now = 1_000_000.0
    setup_account("ultra", "ultra@example.com", "default_claude_max_20x")
    # Ultra account has weekly and weekly_resets, but NO five_hour_resets
    write_usage("ultra", 0, 42, five_resets=0, weekly_resets=now + 86400, at=iso(now - 60))

    # 1. claude_five_hour returns None
    assert usage.claude_five_hour(account="ultra", now=now) is None

    # 2. quota_rows does not contain 5h
    rows = usage.quota_rows(account="ultra", now=now)
    assert not any(w == "5h" for w, _, _, _ in rows)
    assert any(w == "week" and p == 42.0 for w, _, p, _ in rows)

    # 3. quota_line names only the weekly number and its reset time
    qline = usage.quota_line("he", account="ultra", now=now)
    assert "שבועית 42%" in qline and "5 שעות" not in qline and "↻" in qline

    # 4. live_keyboard shows weekly and reset clock
    kb = M.live_keyboard(qline, lang="he")
    info_row = kb["keyboard"][0]
    info_text = " · ".join(btn["text"] for btn in info_row)
    assert "⏱️" not in info_text and "📅 42%" in info_text


def test_stop_failure_marks_the_session_idle_and_nudges(monkeypatch):
    """A failed turn gets no Stop: the session must not stay "busy" with messages queued behind it."""
    from ctb import inbox as I, topics as T
    sid = "sess-fail-idle"
    c = cfg()
    monkeypatch.setattr(H, "load_config", lambda: c)
    states, nudged = [], []
    monkeypatch.setattr(T, "set_state", lambda config, session, state, **kw: states.append((session, state)))
    monkeypatch.setattr(I, "_nudge_without_waiter", lambda inbox, session, settle=6.0: nudged.append(session))
    I.default_inbox().put(sid, {"text": "hello", "id": 1})
    H.hook_stop_failure(json.dumps({"session_id": sid, "error": "API Error: 500 overloaded"}))
    assert states == [(sid, "done")]
    assert nudged == [sid]


def test_stop_failure_keeps_a_newer_turn_and_ignores_agents(monkeypatch, tmp_path):
    from ctb import inbox as I, topics as T, mirror as Mi
    sid = "sess-fail-newer"
    c = cfg()
    monkeypatch.setattr(H, "load_config", lambda: c)
    states = []
    monkeypatch.setattr(T, "set_state", lambda config, session, state, **kw: states.append(state))
    monkeypatch.setattr(I, "_nudge_without_waiter", lambda *a, **k: None)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant"}) + "\n")
    # the owner types while the limit offer is still running: a new turn starts
    monkeypatch.setattr(Mi, "limit_offer", lambda *a: transcript.open("a").write(
        json.dumps({"type": "user", "message": {"content": "next"}}) + "\n"))
    limit = "You've hit your limit · resets 8pm"
    H.hook_stop_failure(json.dumps({"session_id": sid, "error": limit, "transcript_path": str(transcript)}))
    H.hook_stop_failure(json.dumps({"session_id": sid, "error": "API Error: 500", "agent_id": "a1"}))
    assert states == []


def test_big_plan_shows_no_five_hour_anywhere():
    now = 1_000_000.0
    setup_account("ultra", "ultra@example.com", "default_claude_max_20x")
    write_usage("ultra", 0, 47, five_resets=now + 3600, weekly_resets=now + 86400, at=iso(now - 60))
    assert accounts.is_big_plan("ultra") is True
    for lang in ("he", "en"):
        qline = usage.quota_line(lang, account="ultra", now=now)
        assert "47%" in qline and "5h" not in qline and "5 שעות" not in qline
        kb = M.live_keyboard(qline, lang=lang)
        assert "⏱️" not in " ".join(b["text"] for b in kb["keyboard"][0])
    assert usage.claude_five_hour(account="ultra", now=now) is None
    assert not any(w == "5h" for w, _, _, _ in usage.quota_rows(account="ultra", now=now))
    assert "5h" not in "\n".join(usage.quota_lines(account="ultra", now=now))


def test_every_account_shows_the_weekly_number_only():
    # 2026-11-10: the plans have no 5-hour limit, only weekly (plus banked resets); even a record that
    # still carries a 5-hour reading shows the weekly number alone.
    now = 1_000_000.0
    setup_account("pro1", "pro1@example.com", "default_claude_max_5x")
    write_usage("pro1", 12, 47, five_resets=now + 3600, weekly_resets=now + 86400, at=iso(now - 60))
    assert accounts.is_big_plan("pro1") is False
    qline = usage.quota_line("he", account="pro1", now=now)
    assert "5 שעות" not in qline and "שבועית 47%" in qline and "↻" in qline
    kb = M.live_keyboard(qline, lang="he")
    assert "⏱️" not in " ".join(b["text"] for b in kb["keyboard"][0])
    assert not any(w == "5h" for w, _, _, _ in usage.quota_rows(account="pro1", now=now))
