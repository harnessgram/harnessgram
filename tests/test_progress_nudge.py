"""A long turn with no progress card is asked for one, once; a card left untouched is asked for an update."""
import pytest

from ctb import cards, config, hooks

S = "a1b2c3d4-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(hooks, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cards, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(config, "state_dir", lambda: tmp_path)
    return tmp_path


def test_a_long_turn_without_a_card_is_asked_and_rate_limited():
    assert hooks.progress_nudge(S, now=1000.0) == ""                       # no turn known: nothing
    hooks.turn_started(S, now=1000.0)
    assert hooks.progress_nudge(S, now=1000.0 + hooks.PROGRESS_AFTER - 1) == ""
    asked = hooks.progress_nudge(S, now=1000.0 + hooks.PROGRESS_AFTER)
    assert "ctb card progress" in asked and "--step" in asked
    assert hooks.progress_nudge(S, now=1000.0 + hooks.PROGRESS_AFTER + 5) == ""
    again = 1000.0 + hooks.PROGRESS_AFTER + hooks.PROGRESS_EVERY
    assert hooks.progress_nudge(S, now=again - 1) == ""                    # at most once per PROGRESS_EVERY
    assert hooks.progress_nudge(S, now=again) == hooks.PROGRESS_NUDGE
    hooks.turn_started(S, now=again + 10)                                  # a new prompt keeps the rate limit
    assert hooks.progress_nudge(S, now=again + 10 + hooks.PROGRESS_AFTER) == ""
    assert hooks.progress_nudge(S, now=again + hooks.PROGRESS_EVERY) == hooks.PROGRESS_NUDGE


def test_a_fresh_card_is_enough_and_a_stale_one_is_asked_for_an_update():
    hooks.turn_started(S, now=1000.0)
    rec = cards.publish(S, -100, 7, "progress", "Build", steps=["a", "b", "c"], at=1, percent=30, now=1100.0)
    assert hooks.progress_nudge(S, now=1000.0 + hooks.PROGRESS_AFTER) == ""
    late = 1101.0 + hooks.PROGRESS_STALE
    assert hooks.progress_nudge(S, now=late) == hooks.PROGRESS_UPDATE.format(id=rec["id"])
    assert hooks.progress_nudge(S, now=late + 60) == ""                    # not again right away
    assert not hooks.progress_nudge(S, now=late + hooks.PROGRESS_STALE)  # once until the card changes


def test_the_quiet_level_turns_the_nudge_off():
    config.set_activity_level("quiet")
    hooks.turn_started(S, now=1000.0)
    assert hooks.progress_nudge(S, now=1000.0 + hooks.PROGRESS_AFTER) == ""


def test_many_tool_calls_or_a_background_task_make_a_long_task_before_the_clock():
    hooks.turn_started(S, now=1000.0)
    for _ in range(hooks.PROGRESS_TOOLS - 1):
        hooks.tool_called(S)
    assert hooks.progress_nudge(S, now=1010.0) == ""
    hooks.tool_called(S)
    assert hooks.progress_nudge(S, now=1011.0) == hooks.PROGRESS_NUDGE
    other = "a1b2c3d4-0000-4000-8000-000000000002"
    hooks.turn_started(other, now=1000.0)
    hooks.tool_called(other, hooks.launches_background(
        {"tool_name": "Agent", "tool_input": {"prompt": "x", "run_in_background": True},
         "tool_response": {"status": "async_launched", "agentId": "a1b2c3"}}))
    assert hooks.progress_nudge(other, now=1001.0) == hooks.PROGRESS_NUDGE
    third = "a1b2c3d4-0000-4000-8000-000000000003"
    hooks.turn_started(third, now=1000.0, background=1)                   # still running from the last turn
    assert hooks.progress_nudge(third, now=1001.0) == hooks.PROGRESS_NUDGE
    assert not hooks.launches_background({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert hooks.launches_background({"tool_name": "Bash", "tool_input": {"command": "x", "run_in_background": True}})


def test_a_live_card_of_its_own_suppresses_the_reminder_but_an_automatic_one_does_not():
    hooks.turn_started(S, now=1000.0, background=1)
    cards.sync_auto(S, -100, 7, "Topic", [{"content": "step", "status": "in_progress"}], now=1000.0)
    assert hooks.progress_nudge(S, now=1001.0) == hooks.PROGRESS_NUDGE    # the bridge's card is not the agent's
    other = "a1b2c3d4-0000-4000-8000-000000000002"
    hooks.turn_started(other, now=1000.0, background=1)
    cards.publish(other, -100, 8, "progress", "Build", steps=["a", "b"], at=0, percent=5, now=1000.0)
    for t in (1001.0, 1000.0 + cards.PROGRESS_STALE_AFTER - 1):  # a card of its own: quiet until it goes stale
        assert hooks.progress_nudge(other, now=t) == ""


def test_the_post_tool_hook_counts_calls_and_reminds_mid_turn(monkeypatch):
    import json
    monkeypatch.setattr(hooks, "load_config", lambda: config.Config(owner_chat_id=7, bot_token="1:x"))
    monkeypatch.setattr(hooks, "typing", lambda *a: None)
    monkeypatch.setattr(hooks, "append_progress", lambda *a: None)
    printed = []
    monkeypatch.setattr("builtins.print", lambda text, *a, **k: printed.append(text))
    hooks.turn_started(S)
    event = json.dumps({"session_id": S, "tool_name": "Agent", "tool_input": {"prompt": "x"},
                        "tool_response": {"status": "async_launched", "agentId": "a1b2c3"}})
    hooks.hook_post(event)
    assert printed and hooks.PROGRESS_NUDGE in json.loads(printed[-1])["hookSpecificOutput"]["additionalContext"]
    printed.clear()
    hooks.hook_post(event)
    assert not any(hooks.PROGRESS_NUDGE in p for p in printed)             # rate-limited


def test_a_foreground_agent_is_not_a_background_task():
    done = {"tool_name": "Agent", "tool_input": {"prompt": "x"},
            "tool_response": {"status": "completed", "content": [{"type": "text", "text": "Report: all good"}]}}
    assert not hooks.launches_background(done)
    assert not hooks.launches_background({"tool_name": "Agent", "tool_input": {"prompt": "x"}})  # no result: unproven
    assert not hooks.launches_background({"tool_name": "Agent", "tool_input": {"run_in_background": False},
                                          "tool_response": "launched successfully"})
    hooks.turn_started(S, now=1000.0)
    hooks.tool_called(S, hooks.launches_background(done))
    assert hooks.progress_nudge(S, now=1001.0) == ""                       # a short turn stays quiet


def test_parallel_hooks_claim_one_reminder_and_lose_no_tool_call(monkeypatch):
    import threading
    import time as _time
    hooks.turn_started(S, now=1000.0, background=1)
    real = hooks._turn_state

    def slow(session):                       # widens the read-modify-write window an unlocked update would lose
        state = real(session)
        _time.sleep(0.01)
        return state
    monkeypatch.setattr(hooks, "_turn_state", slow)
    gate, out = threading.Barrier(8), []

    def hook():
        gate.wait()
        hooks.tool_called(S)
        out.append(hooks.progress_nudge(S, now=1001.0))
    threads = [threading.Thread(target=hook) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert out.count(hooks.PROGRESS_NUDGE) == 1
    assert hooks.tool_count(S) == (8, False)                                # no call lost, none waited


def test_the_prompt_hook_never_waits_long_for_the_background_search(monkeypatch):
    import time as _time
    monkeypatch.setattr(hooks, "background_running", lambda path, now=None: _time.sleep(2) or 3)
    began = _time.monotonic()
    assert hooks.background_soon("/nonexistent") == 0
    assert _time.monotonic() - began < 1.0
    monkeypatch.setattr(hooks, "background_running", lambda path, now=None: 2)
    assert hooks.background_soon("/nonexistent") == 2


def test_only_the_structured_launch_signal_counts_never_result_text():
    agent = {"tool_name": "Agent", "tool_input": {"prompt": "x"}}
    assert hooks.launches_background(dict(agent, tool_response={"status": "async_launched"}))
    assert hooks.launches_background(dict(agent, tool_response={"isAsync": True}))
    for text in ("Async agent launched successfully. agentId: a1b2c3", "moved to the background, task ID 7"):
        assert not hooks.launches_background(dict(agent, tool_response=text))
        assert not hooks.launches_background(dict(agent, tool_response={"status": "completed",
                                                                        "content": [{"type": "text", "text": text}]}))


def _hold_card_lock():
    import threading
    held, release = threading.Event(), threading.Event()

    def hold():
        with cards._locked():
            held.set()
            release.wait(10)
    threading.Thread(target=hold, daemon=True).start()
    assert held.wait(5)
    return release


def _finishes(fn, limit=2.0):
    import threading
    done = threading.Event()
    threading.Thread(target=lambda: (fn(), done.set()), daemon=True).start()
    return done.wait(limit)


def test_hooks_skip_a_busy_card_lock_instead_of_blocking(monkeypatch):
    import json
    import time as _time
    monkeypatch.setattr(hooks, "load_config", lambda: config.Config(owner_chat_id=7, bot_token="1:x"))
    monkeypatch.setattr(hooks, "typing", lambda *a: None)
    monkeypatch.setattr(hooks, "append_progress", lambda *a: None)
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)
    rec = cards.publish(S, -100, 7, "progress", "Build", steps=["a", "b"], at=0, percent=5, now=1000.0)
    hooks.turn_started(S, now=1000.0, background=1)
    release = _hold_card_lock()
    try:
        todo = json.dumps({"session_id": S, "tool_name": "TodoWrite",
                           "tool_input": {"todos": [{"content": "step", "status": "in_progress"}]}})
        began = _time.monotonic()
        assert _finishes(lambda: hooks.hook_post(todo))                    # sync_todos: skipped, not blocked
        late = 1001.0 + hooks.PROGRESS_STALE
        assert _finishes(lambda: hooks.progress_nudge(S, now=late))        # stale_reminder: skipped, not blocked
        assert _time.monotonic() - began < 2.0
    finally:
        release.set()
    assert hooks.progress_nudge(S, now=1001.0 + hooks.PROGRESS_STALE + 1) == hooks.PROGRESS_UPDATE.format(id=rec["id"])


def test_a_held_turn_lock_never_costs_a_tool_call():
    hooks.turn_started(S, now=1000.0)
    with hooks._turn_locked(S):                                            # another hook is deciding a reminder
        hooks.tool_called(S)
        hooks.tool_called(S, background=True)
    assert hooks.tool_count(S) == (2, True)
    hooks.turn_started(S, now=2000.0)                                      # a new turn counts from zero
    assert hooks.tool_count(S) == (0, False)
