"""Progress freshness, session isolation, and task-list synchronization."""
import json

from ctb import cards, config, hooks, panel, panel_page, sessions_menu
from ctb.config import Config


def setup_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    for module in (cards, config, hooks, panel):
        monkeypatch.setattr(module, "state_dir", lambda: tmp_path)


def test_reconcile_closes_only_expired_progress_and_can_dismiss(tmp_path, monkeypatch):
    setup_state(tmp_path, monkeypatch)
    now = 100000.0
    fresh = cards.publish("session-a", -100, 1, "progress", "Fresh", percent=5, now=now)
    expired = cards.publish("session-b", -100, 2, "progress", "Expired", percent=96,
                            now=now - cards.PROGRESS_CLOSE_AFTER - 1)
    ended = cards.publish("session-c", -100, 3, "progress", "Ended", percent=40, now=now)
    choice = cards.publish("session-c", -100, 3, "choice", "Choice", options=["one", "two"], now=now)
    cards.reconcile_progress({"session-c"}, now, elapsed=10)
    result = {r["id"]: r for r in cards.reconcile_progress({"session-c"}, now, elapsed=10 + cards.PROGRESS_ENDED_GRACE)}
    assert result[fresh["id"]]["state"] == "open"
    assert result[choice["id"]]["state"] == "open"
    assert result[expired["id"]]["state"] == "open" and result[ended["id"]]["auto_closed"]
    assert not cards.dismiss_progress(fresh["id"])
    assert cards.dismiss_progress(ended["id"])
    assert ended["id"] not in {r["id"] for r in cards.reconcile_progress(set(), now)}
    assert expired["id"] in {r["id"] for r in cards.reconcile_progress(set(), now)}


def test_state_marks_only_working_stale_cards(tmp_path, monkeypatch):
    setup_state(tmp_path, monkeypatch)
    now = 100000.0
    entry = sessions_menu.Entry
    monkeypatch.setattr(panel, "collect", lambda topics, now=None: [
        entry("session-a", "A", 1, 0, "working", False, -100),
        entry("session-b", "B", 2, 0, "idle", False, -100)])
    fresh = cards.publish("session-a", -100, 1, "progress", "Fresh", percent=10, now=now)
    old = cards.publish("session-a", -100, 1, "progress", "Old", percent=20,
                        now=now - cards.PROGRESS_STALE_AFTER - 1)
    idle = cards.publish("session-b", -100, 2, "progress", "Idle", percent=30,
                         now=now - cards.PROGRESS_STALE_AFTER - 1)
    state = panel.build_state(Config(owner_chat_id=7), now=now, light=True)
    rows = {r["id"]: r for r in state["cards"]}
    assert not rows[fresh["id"]]["stale"] and rows[fresh["id"]]["age"] == 0
    assert rows[old["id"]]["stale"] and not rows[idle["id"]]["stale"]
    assert panel_page.STRINGS["he"]["cardStale"] and panel_page.STRINGS["en"]["cardStale"]


def test_reminder_per_card_survives_new_turn_and_is_session_scoped(tmp_path, monkeypatch):
    setup_state(tmp_path, monkeypatch)
    hooks.turn_started("session-a", now=1000)
    a = cards.publish("session-a", -100, 1, "progress", "A", percent=10, now=1000)
    b = cards.publish("session-b", -100, 2, "progress", "B", percent=20, now=1000)
    another = cards.publish("session-a", -100, 1, "progress", "Another", percent=30, now=1001)
    late = 1001 + cards.PROGRESS_STALE_AFTER
    msg = hooks.progress_nudge("session-a", now=late)
    assert a["id"] in msg and b["id"] not in msg and "ctb card progress --id " + a["id"] in msg
    hooks.turn_started("session-a", now=late + 1)
    assert not hooks.progress_nudge("session-a", now=late + 1)
    assert next(r for r in cards.open_cards() if r["id"] == b["id"]).get("nudged_at") is None
    assert another["id"] in hooks.progress_nudge("session-a", now=late + cards.PROGRESS_STALE_AFTER)


def test_todo_snapshots_update_one_automatic_card_without_touching_other_session(tmp_path, monkeypatch):
    setup_state(tmp_path, monkeypatch)
    one = [{"content": "First", "status": "in_progress"}, {"content": "Second", "status": "pending"}]
    first = cards.sync_todos("session-a", -100, 1, one, "Work plan", now=1000)
    other = cards.sync_todos("session-b", -100, 2, one, "Work plan", now=1000)
    two = [{"content": "First", "status": "completed"}, {"content": "Second", "status": "in_progress"}]
    updated = cards.sync_todos("session-a", -100, 1, two, "Work plan", now=1010)
    assert updated["id"] == first["id"] and updated["percent"] == 50 and updated["updated"] == 1010
    updated = cards.sync_todos("session-a", -100, 1, [], "Work plan", now=1020)
    assert updated["id"] == first["id"] and updated["percent"] == 0 and updated["steps"] == []
    assert next(r for r in cards.open_cards() if r["id"] == other["id"])["updated"] == 1000


def test_hook_updates_automatic_card_for_each_todowrite(tmp_path, monkeypatch):
    from ctb import decisions
    setup_state(tmp_path, monkeypatch)
    monkeypatch.setattr(hooks, "load_config", lambda: Config(target_language="en"))
    monkeypatch.setattr(decisions, "_place_of", lambda cfg, session: (-100, 1))
    monkeypatch.setattr(hooks, "typing", lambda *a: None)
    monkeypatch.setattr(hooks, "append_progress", lambda *a: None)
    monkeypatch.setattr(hooks, "periodic_reminder", lambda *a: "")
    data = {"session_id": "session-a", "tool_name": "TodoWrite", "tool_input": {"todos": [
        {"content": "First", "status": "pending"}, {"content": "Second", "status": "pending"}]}}
    assert hooks.hook_post(json.dumps(data)) == 0
    first = cards.open_cards()[0]
    data["tool_input"]["todos"][0]["status"] = "completed"
    assert hooks.hook_post(json.dumps(data)) == 0
    updated = cards.open_cards()[0]
    assert first["id"] == updated["id"] and updated["percent"] == 50
    data["agent_id"] = "child"
    data["tool_input"]["todos"][1]["status"] = "completed"
    hooks.hook_post(json.dumps(data))
    assert cards.open_cards()[0]["percent"] == 50


def test_dismiss_route_without_socket(tmp_path, monkeypatch):
    setup_state(tmp_path, monkeypatch)
    rec = cards.publish("session-a", -100, 1, "progress", "A", percent=10, now=1)
    cards.reconcile_progress({"session-a"}, now=2, elapsed=1)
    cards.reconcile_progress({"session-a"}, now=3, elapsed=1 + cards.PROGRESS_ENDED_GRACE)
    class Handler:
        def _request(self):
            return {"id": rec["id"], "dismiss": True}
        def _json(self, status, body):
            self.result = status, body
    handler = Handler()
    panel.PanelHandler._card(handler)
    assert handler.result == (200, {"ok": True})
    assert not cards.reconcile_progress(set(), now=3)


def test_freshness_boundaries_and_page_controls(tmp_path, monkeypatch):
    setup_state(tmp_path, monkeypatch)
    rec = cards.publish("session-a", -100, 1, "progress", "A", percent=10, now=1000)
    assert not cards.stale_reminder("session-a", 1000 + cards.PROGRESS_STALE_AFTER)
    assert cards.stale_reminder("session-a", 1001 + cards.PROGRESS_STALE_AFTER) == rec["id"]
    assert cards.reconcile_progress(set(), 1000 + cards.PROGRESS_CLOSE_AFTER)[0]["state"] == "open"
    assert cards.reconcile_progress(set(), 1001 + cards.PROGRESS_CLOSE_AFTER)[0]["state"] == "open"
    assert '.ac.stale' in panel_page.PAGE and 'S.cardUpdated' in panel_page.PAGE
    assert 'id:c.id,dismiss:true' in panel_page.PAGE


def test_prompt_context_uses_the_correct_hook_channel():
    out = hooks.with_reminder(None, "Update the card", "UserPromptSubmit")
    assert out["hookSpecificOutput"] == {"hookEventName": "UserPromptSubmit", "additionalContext": "Update the card"}
    out = hooks.with_reminder(out, "Another line", "UserPromptSubmit")
    assert out["hookSpecificOutput"]["additionalContext"] == "Update the card\n\nAnother line"


def test_finished_never_stale_or_reminded_and_closes_after_grace(tmp_path, monkeypatch):
    setup_state(tmp_path, monkeypatch)
    hooks.turn_started('s', now=1000)
    rec = cards.publish('s', -100, 1, 'progress', 'Done', steps=['one'], at=1, now=1000)
    assert not hooks.progress_nudge('s', now=5000)
    assert not cards.stale_reminder('s', 5000)
    assert not cards.is_stale(rec, 5000)
    assert cards.reconcile_progress(set(), now=5000, elapsed=100)[0]['state'] == 'open'
    assert cards.reconcile_progress(set(), now=999999, elapsed=100 + cards.PROGRESS_FINISHED_GRACE + 1)[0]['auto_closed']


def test_ended_requires_time_and_reopen_is_session_scoped(tmp_path, monkeypatch):
    import pytest
    setup_state(tmp_path, monkeypatch)
    rec = cards.publish('s', -100, 1, 'progress', 'Work', percent=20, now=100)
    assert cards.reconcile_progress({'s'}, now=1000000, elapsed=10)[0]['state'] == 'open'
    assert cards.reconcile_progress(set(), now=2000000, elapsed=11)[0]['state'] == 'open'
    assert cards.reconcile_progress({'s'}, now=3000000, elapsed=12)[0]['state'] == 'open'
    assert cards.reconcile_progress({'s'}, now=3000001, elapsed=12 + cards.PROGRESS_ENDED_GRACE + 1)[0]['auto_closed']
    with pytest.raises(cards.CardError):
        cards.publish('other', -100, 1, 'progress', '', ident=rec['id'], percent=30)
    reopened = cards.publish('s', -100, 1, 'progress', '', ident=rec['id'], percent=30)
    assert reopened['state'] == 'open' and not reopened.get('auto_closed')


def test_large_list_window_manual_precedence_and_bad_items(tmp_path, monkeypatch):
    setup_state(tmp_path, monkeypatch)
    todos = [{'content': str(i), 'status': 'completed' if i < 7 else 'in_progress' if i == 7 else 'pending'} for i in range(10)]
    todos.insert(0, {'content': ' ', 'status': 'pending'})
    rec = cards.sync_todos('s', -100, 1, todos, 'Plan', now=100)
    assert '7' in rec['steps'] and rec['at'] < len(rec['steps']) and rec['percent'] == 70
    manual = cards.publish('other', -100, 2, 'progress', 'Manual', percent=1, now=100)
    assert cards.sync_todos('other', -100, 2, todos, 'Plan', now=101) is None
    assert [r['id'] for r in cards.open_cards() if r['session'] == 'other'] == [manual['id']]
    complete = [{'content': 'done', 'status': 'completed'}]
    cards.sync_todos('s', -100, 1, complete, 'Plan', now=102)
    assert rec['id'] not in [r['id'] for r in cards.open_cards()]


def test_quiet_and_fresh_card_do_not_lock_and_context_is_robust(tmp_path, monkeypatch):
    setup_state(tmp_path, monkeypatch)
    hooks.turn_started('s', now=1000)
    cards.publish('s', -100, 1, 'progress', 'Work', percent=1, now=1000)
    monkeypatch.setattr(cards, 'stale_reminder', lambda *a: (_ for _ in ()).throw(AssertionError('locked')))
    assert not hooks.progress_nudge('s', now=1001)
    monkeypatch.setattr(config, 'activity_level', lambda: 'quiet')
    assert not hooks.progress_nudge('s', now=5000)
    assert hooks.with_reminder({'other': True}, 'notice')['hookSpecificOutput']['additionalContext'] == 'notice'


def test_marker_avoids_repeated_card_reads_and_detects_new_card(tmp_path, monkeypatch):
    setup_state(tmp_path, monkeypatch)
    hooks.turn_started('s', now=1000)
    cards.publish('s', -100, 1, 'progress', 'Work', percent=10, now=1000)
    assert not hooks.progress_nudge('s', now=1500)
    original = cards.open_cards
    monkeypatch.setattr(cards, 'open_cards', lambda: (_ for _ in ()).throw(AssertionError('read again')))
    assert not hooks.progress_nudge('s', now=1501)
    monkeypatch.setattr(cards, 'open_cards', original)
    cards.publish('s', -100, 1, 'progress', 'New', percent=20, now=1502)
    assert not hooks.progress_nudge('s', now=1503)


def test_automatic_cards_do_not_consume_manual_capacity(tmp_path, monkeypatch):
    setup_state(tmp_path, monkeypatch)
    monkeypatch.setattr(cards, 'MAX_OPEN', 2)
    todos = [{'content': 'work', 'status': 'in_progress'}]
    cards.sync_todos('s1', -100, 1, todos, 'Plan', now=100)
    cards.sync_todos('s2', -100, 2, todos, 'Plan', now=100)
    assert cards.publish('s3', -100, 3, 'ask', 'Question', now=101)['state'] == 'open'


def test_manual_card_supersedes_existing_automatic_and_unchanged_snapshot(tmp_path, monkeypatch):
    setup_state(tmp_path, monkeypatch)
    todos = [{'content': 'work', 'status': 'in_progress'}]
    auto = cards.sync_todos('s', -100, 1, todos, 'Plan', now=100)
    assert cards.sync_todos('s', -100, 1, todos, 'Plan', now=200)['updated'] == 100
    manual = cards.publish('s', -100, 1, 'progress', 'Manual', percent=10, now=300)
    assert [r['id'] for r in cards.open_cards()] == [manual['id']]
    assert auto['id'] != manual['id']


def test_percent_cannot_round_incomplete_list_to_finished(tmp_path, monkeypatch):
    setup_state(tmp_path, monkeypatch)
    todos = [{'content': 'done', 'status': 'completed'} for _ in range(200)]
    todos.append({'content': 'current', 'status': 'in_progress'})
    rec = cards.sync_todos('s', -100, 1, todos, 'Plan', now=100)
    assert rec['percent'] == 99 and not cards.finished(rec)
