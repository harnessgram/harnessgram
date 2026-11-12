"""Quota recovery: one announcement per limit, a freed account takes the session, saved messages are
delivered (through the framed hook, never typed as the owner's words) once the quota returns."""
import json
import time

import pytest

from conftest import GOOD_TOKEN
from ctb import alerts, inbox as I, recover, usage, wake
from ctb.config import Config, state_dir, write_private
from ctb.topics import TopicMap

OWNER = 7
SID = "sess-limit-1"
RESET = 1_800_000_000.0


def cfg(**kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, **dict({"ui_language": "en"}, **kw))


@pytest.fixture
def rig(bot, monkeypatch):
    thread = TopicMap(state_dir() / "topics.json", bot, OWNER).ensure(SID, "Title")
    box = I.Inbox(state_dir() / "inbox")
    error = {"id": "e1", "reset": RESET, "clock": "16:30"}
    monkeypatch.setattr(usage, "limit_of", lambda s: error if s == SID and error else None)
    monkeypatch.setattr(recover.accounts, "auto_enabled", lambda: False)
    bot.calls.clear()
    return bot, box, thread, error


def tick(rig, now, **kw):
    bot, box, *_ = rig
    recover.limit_check(cfg(), bot=bot, now=now, inbox=box, **kw)
    return [p["text"] for p in bot.methods("sendMessage")]


def test_limit_is_announced_once(rig):
    assert tick(rig, RESET - 3000) == ["⏸️ Claude's quota ran out. It resets at 16:30. Messages you write here are "
                                        "saved and delivered automatically when it returns."]
    assert tick(rig, RESET - 2000) == tick(rig, RESET - 1000)  # nothing new is sent
    assert len(rig[0].methods("sendMessage")) == 1


def test_saved_messages_are_delivered_after_the_reset_and_announced_once(rig, monkeypatch):
    bot, box, thread, error = rig
    tick(rig, RESET - 100)
    box.put(SID, {"text": "do the thing", "id": 1, "chat": OWNER})
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    nudged = []
    monkeypatch.setattr(recover, "_deliver", lambda s, q: nudged.append(s) or "nudged")
    bot.calls.clear()
    assert tick(rig, RESET + 10) == []               # inside the grace period
    sent = tick(rig, RESET + 70)
    assert sent == ["▶️ The quota is back. Delivering your saved messages to the session."] and nudged == [SID]
    tick(rig, RESET + 130)                           # nudged a minute ago: not again yet
    assert nudged == [SID]
    tick(rig, RESET + 70 + recover.NUDGE_RETRY + 1)  # still queued: nudged again, but said only once
    assert len(bot.methods("sendMessage")) == 1 and nudged == [SID, SID]
    box.take(SID)
    tick(rig, RESET + 190)                           # the queue is empty: the limit is done with
    assert SID not in json.loads((state_dir() / "limit-state.json").read_text())


def test_nothing_is_said_when_nothing_waited(rig, monkeypatch):
    tick(rig, RESET - 100)
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    rig[0].calls.clear()
    assert tick(rig, RESET + 100) == []
    assert SID not in json.loads((state_dir() / "limit-state.json").read_text())


def test_a_busy_prompt_is_retried_and_a_missing_pane_is_reported_once(rig, monkeypatch):
    bot, box, *_ = rig
    tick(rig, RESET - 100)
    box.put(SID, {"text": "hi", "id": 1, "chat": OWNER})
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    answers = iter(["later", "no-pane", "no-pane"])
    monkeypatch.setattr(recover, "_deliver", lambda s, q: next(answers))
    bot.calls.clear()
    assert tick(rig, RESET + 70) == []
    assert "not in a terminal" in tick(rig, RESET + 130)[0]
    assert len(tick(rig, RESET + 190)) == 1


def test_a_late_queue_is_not_quota_returned(rig, monkeypatch):
    bot, box, *_ = rig
    tick(rig, RESET - 100)
    box.put(SID, {"text": "hi", "id": 1, "chat": OWNER})
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    monkeypatch.setattr(recover, "_deliver", lambda s, q: pytest.fail("must not deliver"))
    bot.calls.clear()
    assert tick(rig, RESET + recover.GIVE_UP + 5) == []


def test_a_freed_account_takes_the_session_while_it_waits(rig, monkeypatch):
    bot, box, thread, _ = rig
    monkeypatch.setattr(recover.accounts, "auto_enabled", lambda: True)
    monkeypatch.setattr(recover.accounts, "fallback_plan", lambda s, now=None: {"fresh": ["spare"]})
    spawned = []
    sent = tick(rig, RESET - 500, spawn_fn=spawned.append)
    assert spawned == [["account", "auto-move", f"--session={SID}", f"--thread={thread}", "--target=spare"]]
    assert sent[-1] == "🔁 Quota is free on account spare. Moving the session there and continuing."
    tick(rig, RESET - 400, spawn_fn=spawned.append)
    assert len(spawned) == 1                          # once per limit


def test_a_move_that_succeeded_ends_the_old_limit(rig, monkeypatch):
    bot, box, *_ = rig
    tick(rig, RESET - 1000)
    box.put(SID, {"text": "hi", "id": 1, "chat": OWNER})
    monkeypatch.setattr(usage, "limit_of", lambda s: None)       # the moved session no longer shows the error
    write_private(state_dir() / "limit-cleared" / SID, "e1")
    monkeypatch.setattr(recover, "_deliver", lambda s, q: pytest.fail("the new account serves it"))
    bot.calls.clear()
    assert tick(rig, RESET - 900) == []
    assert SID not in json.loads((state_dir() / "limit-state.json").read_text())


def test_recovery_copies_to_the_alerts_channel(rig, monkeypatch):
    bot, box, *_ = rig
    write_private(state_dir() / "alerts-channel.json", json.dumps({"id": -100, "title": "A", "bot": ""}))
    alerts._trusted[(str(state_dir()), -100)] = time.monotonic()   # registered through the listener
    tick(rig, RESET - 100)
    box.put(SID, {"text": "hi", "id": 1, "chat": OWNER})
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    monkeypatch.setattr(recover, "_deliver", lambda s, q: "nudged")
    bot.calls.clear()
    tick(rig, RESET + 70)
    assert [p["chat_id"] for p in bot.methods("sendMessage")] == [OWNER, -100]


# --- messages written to a limited session stay queued ------------------------------------

def test_the_wake_hook_holds_queued_messages_while_the_limit_lasts(monkeypatch):
    wake._limit_seen.clear()
    now = [1000.0]
    monkeypatch.setattr(usage, "limit_of", lambda s: {"id": "x", "reset": 2000.0, "clock": "1"})
    assert wake.limit_active("s", clock=lambda: now[0]) is True
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    now[0] = 1010.0                                   # cached for LIMIT_RECHECK seconds
    assert wake.limit_active("s", clock=lambda: now[0]) is True
    now[0] = 1100.0
    assert wake.limit_active("s", clock=lambda: now[0]) is False


def test_the_waiter_does_not_hand_over_messages_to_a_limited_session(tmp_path, monkeypatch):
    box = I.Inbox(tmp_path / "inbox")
    box.put("s", {"text": "queued while limited", "id": 1, "chat": OWNER})
    state = {"limited": True}
    monkeypatch.setattr(wake, "limit_active", lambda s: state["limited"])
    monkeypatch.setattr(wake.time, "sleep", lambda s: state.update(limited=False))   # the quota returns during the wait
    code, text = wake.wait_for_message({"session_id": "s", "transcript_path": ""}, inbox=box, poll=0, settle=0,
                                       max_wait=60)
    assert code == 2 and "queued while limited" in text


def test_the_inbox_nudge_skips_a_limited_session(tmp_path, monkeypatch):
    box = I.Inbox(tmp_path / "inbox")
    box.put("s", {"text": "x", "id": 1, "chat": OWNER})
    monkeypatch.setattr(wake, "limit_active", lambda s: True)
    monkeypatch.setattr(I, "pane_for_session", lambda s: pytest.fail("must not look for a pane"))
    I._nudge_without_waiter(box, "s", settle=0)


def test_only_the_dashboard_tick_runs_recovery(monkeypatch, bot):
    from ctb import dashboard
    runs = []
    monkeypatch.setattr(recover, "limit_check", lambda cfg, **kw: runs.append(kw.get("bot")))
    dashboard.run(Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER), bot=bot)
    assert runs == [bot]
    dashboard.run(Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER), daily=True, bot=bot)
    dashboard.run(Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER), print_only=True, bot=bot)
    assert runs == [bot]


# --- the nudge uses the inbox's own conditions ---------------------------------------------

def _pane(monkeypatch, waiter=False, busy=False, prompt=True, empty=True):
    from ctb import autopilot
    typed = []
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "_waiter_alive", lambda s: waiter)
    monkeypatch.setattr(I, "main_working", lambda s: busy)
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: prompt)
    monkeypatch.setattr(I, "_empty_prompt", lambda p: empty)
    monkeypatch.setattr(autopilot, "type_into", lambda p, text: typed.append(text))
    return typed


def test_the_nudge_waits_for_a_free_idle_empty_prompt_without_a_waiter(monkeypatch):
    for kw in ({"waiter": True}, {"busy": True}, {"prompt": False}, {"empty": False}):
        typed = _pane(monkeypatch, **kw)
        assert recover._deliver(SID, True) == "later" and typed == []
    typed = _pane(monkeypatch)
    assert recover._deliver(SID, True) == "nudged" and typed == ["📩 Telegram"]


def test_a_nudge_is_not_repeated_within_five_minutes_even_when_still_queued(rig, monkeypatch):
    bot, box, *_ = rig
    tick(rig, RESET - 100)
    box.put(SID, {"text": "hi", "id": 1, "chat": OWNER})
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    calls = []
    monkeypatch.setattr(recover, "_deliver", lambda s, q: calls.append(1) or "nudged")
    for dt in (70, 100, 200, 340, 400):
        tick(rig, RESET + dt)
    assert len(calls) == 2           # at +70 and again once 300 s have passed (+400)


def test_no_freed_move_right_after_the_limit_notice_moved_it(rig, monkeypatch):
    # Live 2026-11-01: the limit notice and this check each started a move at once; they typed over
    # each other and every attempt failed.
    monkeypatch.setattr(recover.accounts, "auto_enabled", lambda: True)
    monkeypatch.setattr(recover.accounts, "fallback_plan", lambda s, now=None: {"fresh": ["spare"]})
    (state_dir() / "limit-offered").mkdir(parents=True, exist_ok=True)
    (state_dir() / "limit-offered" / SID).touch()
    spawned = []
    tick(rig, RESET - 500, spawn_fn=spawned.append)
    assert spawned == []


def test_a_second_move_of_the_same_session_is_skipped(monkeypatch):
    import fcntl
    from ctb import accounts
    accounts.add("spare")
    from ctb.config import private_dir
    lock = private_dir(state_dir() / "moving") / (I.safe_name("abc") + ".lock")
    monkeypatch.setattr(I, "_account_move", lambda *a, **k: pytest.fail("a move is already running"))
    with open(lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert I.account_move(cfg(), None, "abc", "spare", lambda *a, **k: None) is False
    monkeypatch.setattr(I, "_account_move", lambda *a, **k: True)
    assert I.account_move(cfg(), None, "abc", "spare", lambda *a, **k: None) is True


# --- the owner spends a saved reset: the quota is back days before the error's own reset time -------------------

def test_an_early_reset_delivers_the_saved_messages_at_once(rig, monkeypatch):
    bot, box, thread, error = rig
    tick(rig, RESET - 200_000)                       # the limit is announced, days before its reset
    box.put(SID, {"text": "do the thing", "id": 1, "chat": OWNER})
    monkeypatch.setattr(usage, "limit_of", lambda s: None)   # limit_of sees the fresh reading (reset_early)
    monkeypatch.setattr(usage, "reset_early", lambda s, hit: True)
    nudged = []
    monkeypatch.setattr(recover, "_deliver", lambda s, q: nudged.append(s) or "nudged")
    bot.calls.clear()
    assert tick(rig, RESET - 190_000) == ["▶️ The quota is back. Delivering your saved messages to the session."]
    assert nudged == [SID]


def test_a_session_the_limit_interrupted_goes_on_by_itself_once(rig, monkeypatch):
    bot, box, thread, error = rig
    tick(rig, RESET - 200_000)
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    monkeypatch.setattr(usage, "reset_early", lambda s, hit: True)
    monkeypatch.setattr(usage, "interrupted_by_limit", lambda s: True)
    typed = []
    monkeypatch.setattr(recover, "_deliver", lambda s, q, line=recover.NUDGE_TEXT: typed.append(line) or "nudged")
    bot.calls.clear()
    assert tick(rig, RESET - 190_000) == ["▶️ The quota is back. The session continues the work the limit interrupted."]
    assert typed == [recover.RESUME_TEXT]
    tick(rig, RESET - 189_000)
    assert typed == [recover.RESUME_TEXT]            # once: it is never told to continue twice


def test_a_session_that_was_not_interrupted_is_left_alone(rig, monkeypatch):
    tick(rig, RESET - 200_000)
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    monkeypatch.setattr(usage, "reset_early", lambda s, hit: True)
    monkeypatch.setattr(usage, "interrupted_by_limit", lambda s: False)
    monkeypatch.setattr(recover, "_deliver", lambda *a, **k: pytest.fail("nothing to type"))
    rig[0].calls.clear()
    assert tick(rig, RESET - 190_000) == []


def test_without_a_fresh_reading_the_limit_holds_until_its_own_reset(rig, monkeypatch):
    bot, box, thread, error = rig
    tick(rig, RESET - 200_000)
    box.put(SID, {"text": "do the thing", "id": 1, "chat": OWNER})
    monkeypatch.setattr(usage, "reset_early", lambda s, hit: False)
    monkeypatch.setattr(recover, "_deliver", lambda *a, **k: pytest.fail("the quota is not back"))
    bot.calls.clear()
    assert tick(rig, RESET - 190_000) == []


def test_reset_early_needs_a_reading_taken_after_the_limit_that_shows_room(monkeypatch):
    from ctb import accounts
    monkeypatch.setattr(accounts, "origin_account", lambda s: "main")   # the account the session runs on,
    monkeypatch.setattr(accounts, "account_for", lambda s=None: pytest.fail("the default for NEW sessions"))
    reading = {"at": "2026-10-29T10:05:00Z", "weekly": 3}
    monkeypatch.setattr(accounts, "_read", lambda path, default=None: reading)
    monkeypatch.setattr(accounts, "spent", lambda name, now=None: False)
    hit = accounts.iso_epoch("2026-10-29T10:00:00Z")
    assert usage.reset_early("s", hit)
    assert not usage.reset_early("s", hit + 600)     # the reading is older than the limit: it proves nothing
    monkeypatch.setattr(accounts, "spent", lambda name, now=None: True)
    assert not usage.reset_early("s", hit)           # still spent


def test_interrupted_by_limit_reads_the_last_row(tmp_path, monkeypatch):
    project = tmp_path / "projects" / "example"
    project.mkdir(parents=True)
    monkeypatch.setattr(usage, "PROJECTS", tmp_path / "projects")
    limit = {"type": "assistant", "isApiErrorMessage": True, "apiError": "usage_limit_reached"}
    path = project / "s.jsonl"
    path.write_text(json.dumps({"type": "user"}) + "\n" + json.dumps(limit) + "\n")
    assert usage.interrupted_by_limit("s")
    path.write_text(json.dumps(limit) + "\n" + json.dumps({"type": "user"}) + "\n")
    assert not usage.interrupted_by_limit("s")       # a prompt came after it
    path.write_text(json.dumps(limit) + "\n" + json.dumps({"type": "assistant"}) + "\n")
    assert not usage.interrupted_by_limit("s") and not usage.interrupted_by_limit("missing")


def test_an_early_reset_keeps_the_session_on_its_account(rig, monkeypatch):
    """The owner spent the reset to go on with this account: a free other account must not take the session."""
    bot, box, thread, error = rig
    tick(rig, RESET - 200_000)
    box.put(SID, {"text": "do the thing", "id": 1, "chat": OWNER})
    monkeypatch.setattr(usage, "limit_of", lambda s: None)
    monkeypatch.setattr(usage, "reset_early", lambda s, hit: True)
    monkeypatch.setattr(recover.accounts, "auto_enabled", lambda: True)
    monkeypatch.setattr(recover.accounts, "fallback_plan", lambda s, now=None: {"fresh": ["spare"]})
    monkeypatch.setattr(recover, "_retry_deferred", lambda *a, **k: False)
    monkeypatch.setattr(recover, "_deliver", lambda s, q, line=recover.NUDGE_TEXT: "nudged")
    spawned = []
    bot.calls.clear()
    sent = tick(rig, RESET - 190_000, spawn_fn=spawned.append)
    assert spawned == [] and sent == ["▶️ The quota is back. Delivering your saved messages to the session."]


def test_the_line_that_resumes_a_session_is_not_held_by_its_idle_waiter(monkeypatch):
    """Every stopped session keeps a waiter that wakes only for a queued message. With nothing queued it never
    wakes, so it must not hold back the line that tells the interrupted session to go on."""
    from ctb import autopilot
    typed = []
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "_waiter_alive", lambda s: True)
    monkeypatch.setattr(I, "main_working", lambda s: False)
    monkeypatch.setattr(I, "_empty_prompt", lambda pane: True)
    monkeypatch.setattr(autopilot, "at_prompt", lambda pane: True)
    monkeypatch.setattr(autopilot, "type_into", lambda pane, line: typed.append(line))
    assert recover._deliver(SID, True) == "later" and typed == []   # queued: the waiter delivers by itself
    assert recover._deliver(SID, False, recover.RESUME_TEXT) == "nudged" and typed == [recover.RESUME_TEXT]


def test_notices_and_tool_results_after_the_limit_are_not_a_new_prompt(tmp_path, monkeypatch):
    project = tmp_path / "projects" / "example"
    project.mkdir(parents=True)
    monkeypatch.setattr(usage, "PROJECTS", tmp_path / "projects")
    limit = {"type": "assistant", "isApiErrorMessage": True, "apiError": "usage_limit_reached"}
    meta = {"type": "user", "isMeta": True, "message": {"content": "a background task finished"}}
    tool = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}
    path = project / "s.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in (limit, meta, tool, ["not a row"], {"type": "system"})) + "\n")
    assert usage.interrupted_by_limit("s")
    path.write_text("\n".join(json.dumps(r) for r in (limit, {"type": "user", "message": {"content": "go on"}})) + "\n")
    assert not usage.interrupted_by_limit("s")
