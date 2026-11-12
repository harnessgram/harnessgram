"""The bridge's automatic progress card mirrors a working session's task list, gives way to the agent's own
card and goes away when the session is idle; the Cockpit page refreshes often while seen, less while hidden."""
import json
import subprocess

import pytest

from ctb import auto_progress, cards, config, panel, panel_page, sessions_menu, status
from ctb.config import Config
from test_panel_page import func

S = "a1b2c3d4-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    for module in (cards, config, panel):
        monkeypatch.setattr(module, "state_dir", lambda: tmp_path)
    auto_progress._cache.clear()
    return tmp_path


def _use(i, name, **inp):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": f"tu{i}", "name": name, "input": inp}]}}


def _result(i, text):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"tu{i}", "content": text}]}}


def _write(path, events, mode="w"):
    with open(path, mode, encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


SAMPLE = [
    _use(1, "TaskCreate", subject="Read the code", description="secret detail never shown"),
    _result(1, "Task #1 created successfully: Read the code"),
    _use(2, "TaskCreate", subject="Write the fix", description="x"),
    _result(2, "Task #2 created successfully: Write the fix"),
    _use(3, "TaskCreate", subject="Run the tests", description="x"),
    _result(3, "Task #3 created successfully: Run the tests"),
    _use(4, "TaskUpdate", taskId="1", status="completed"),
    _result(4, "Updated task #1 status"),
    _use(5, "TaskUpdate", taskId="2", status="in_progress"),
    _use(6, "Bash", command="cat private.txt"),
    _result(6, "file contents that must never reach a card"),
]


def test_the_task_list_is_read_from_the_transcript_incrementally(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, SAMPLE)
    assert auto_progress.task_list(str(path)) == [
        {"content": "Read the code", "status": "completed"},
        {"content": "Write the fix", "status": "in_progress"},
        {"content": "Run the tests", "status": "pending"}]
    _write(path, [_use(7, "TaskUpdate", taskId="3", status="deleted")], mode="a")
    assert [t["content"] for t in auto_progress.task_list(str(path))] == ["Read the code", "Write the fix"]
    _write(path, [_use(8, "TodoWrite", todos=[{"content": "Plan", "status": "in_progress"}])], mode="a")
    assert auto_progress.task_list(str(path)) == [{"content": "Plan", "status": "in_progress"}]
    assert auto_progress.task_list(str(tmp_path / "missing.jsonl")) == []


def test_an_unchanged_transcript_is_not_read_again(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    _write(path, SAMPLE)
    first = auto_progress.task_list(str(path))
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read again")))
    assert auto_progress.task_list(str(path)) == first
    calls = []
    assert auto_progress.cached(str(path), "k", lambda p: calls.append(p) or 5) == 5
    assert auto_progress.cached(str(path), "k", lambda p: calls.append(p) or 6) == 5 and len(calls) == 1


def test_the_automatic_card_mirrors_the_task_list_and_gives_way_to_the_agents_card():
    tasks = [{"content": "Read the code", "status": "completed"},
             {"content": "Write the fix", "status": "in_progress"},
             {"content": "Run the tests", "status": "pending"}]
    rec = cards.sync_auto(S, -100, 7, "lingo: fix", tasks, bg=2, now=100)
    assert rec["automatic"] and rec["title"] == "lingo: fix" and rec["bg"] == 2
    assert rec["steps"] == ["Read the code", "Write the fix", "Run the tests"] and rec["at"] == 1
    assert rec["percent"] == 33
    view = cards.view(rec, 100)
    assert view["automatic"] and view["bg"] == 2
    assert cards.sync_auto(S, -100, 7, "lingo: fix", tasks, bg=2, now=200)["updated"] == 100  # unchanged: no write
    tasks[1]["status"] = "completed"
    assert cards.sync_auto(S, -100, 7, "lingo: fix", tasks, bg=2, now=300)["at"] == 2
    own = cards.publish(S, -100, 7, "progress", "My card", steps=["a", "b"], at=1, percent=50, now=400)
    assert [r["id"] for r in cards.open_cards()] == [own["id"]]
    assert cards.sync_auto(S, -100, 7, "lingo: fix", tasks, now=500) is None
    assert [r["id"] for r in cards.open_cards()] == [own["id"]]


def test_a_finished_or_missing_list_leaves_a_title_only_card_and_idle_closes_it():
    rec = cards.sync_auto(S, -100, 7, "Topic", [{"content": "old", "status": "completed"}], now=100)
    assert rec["steps"] == [] and rec["percent"] is None and not cards.finished(rec)
    assert cards.close_auto({"other"}, now=150) == 0
    assert cards.close_auto({S}, now=200) == 1 and not cards.open_cards()


def test_state_keeps_the_automatic_card_for_working_sessions_only(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    _write(path, SAMPLE)
    entry = sessions_menu.Entry
    rows = [entry(S, "lingo: fix", 7, 0, "working", False, -100),
            entry("b2c3d4e5-0000-4000-8000-000000000002", "other", 8, 0, "idle", False, -100)]
    monkeypatch.setattr(panel, "collect", lambda topics, now=None: rows)
    monkeypatch.setattr(status, "transcript_of", lambda s: str(path) if s == S else None)
    monkeypatch.setattr(panel.mac_resources, "snapshot", lambda: {})
    cards.sync_auto(rows[1].session, -100, 8, "other", [{"content": "x", "status": "pending"}], now=1)
    state = panel.build_state(Config(owner_chat_id=7), now=1000.0)
    shown = [c for c in state["cards"] if c.get("automatic")]
    assert [c["title"] for c in shown] == ["lingo: fix"]
    assert shown[0]["steps"] == ["Read the code", "Write the fix", "Run the tests"] and not shown[0]["stale"]
    assert "never" not in json.dumps(state["cards"]) and "contents" not in json.dumps(state["cards"])
    rows[0] = rows[0]._replace(state="idle")
    state = panel.build_state(Config(owner_chat_id=7), now=1010.0)
    assert not [c for c in state["cards"] if c.get("automatic")]


def test_the_page_names_an_automatic_card_in_both_languages():
    for lang in ("he", "en"):
        assert panel_page.STRINGS[lang]["cardAuto"]
    assert "S.cardAuto" in panel_page.PAGE and "c.automatic" in panel_page.PAGE


def test_agent_card_payload_has_live_summary_current_task_and_existing_job_count(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    _write(path, SAMPLE + [_use(9, "TaskUpdate", taskId="2", status="in_progress",
                              subject="Inspect /Users/example/private.txt at https://example.invalid/x")])
    rows = [sessions_menu.Entry(S, "lingo: fix", 7, 0, "working", False, -100)]
    monkeypatch.setattr(panel, "collect", lambda topics, now=None: rows)
    monkeypatch.setattr(status, "transcript_of", lambda s: str(path))
    monkeypatch.setattr(panel.mac_resources, "snapshot", lambda: {})
    jobs = []
    monkeypatch.setattr(panel, "_background", lambda p, state, now: jobs.append(p) or 3)
    rec = cards.publish(S, -100, 7, "progress", "My plan", detail="Agent detail", steps=["a", "b"], at=0, now=100)
    cards.update_live(S, "Checking the fix", now=900)
    cfg = Config(owner_chat_id=7)
    payload = panel.build_state(cfg, now=1000)["cards"][0]
    assert payload["id"] == rec["id"] and not payload.get("automatic")
    assert payload["live"] == "Checking the fix" and payload["live_at"] == 900 and payload["live_age"] == 100
    assert payload["task"] == "Inspect [path] at [link]" and payload["bg"] == 3
    assert payload["detail"] == "Agent detail" and payload["steps"] == ["a", "b"]
    for leak in ("private.txt", "example", "contents", "secret detail", "cat private"):
        assert leak not in json.dumps(payload)
    assert jobs == [str(path)]  # the card reuses the session count
    with monkeypatch.context() as guard:
        guard.setattr(auto_progress, "_wanted", lambda *a: pytest.fail("task transcript scanned twice"))
        assert panel.build_state(cfg, now=1010)["cards"][0]["task"] == payload["task"]
    _write(path, [_use(10, "TaskUpdate", taskId="2", status="completed")], mode="a")
    assert panel.build_state(cfg, now=1020)["cards"][0]["task"] == ""


def test_card_activity_ignores_pending_completed_and_ended_work(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [_use(1, "TodoWrite", todos=[{"content": "future", "status": "pending"},
                                            {"content": "old", "status": "completed"}])])
    rec = cards.publish(S, -100, 7, "progress", "Plan", percent=20)
    assert panel._card_activity(rec, {S: str(path)}, {S: 2}, {S: "working"}) == {"task": "", "bg": 2}
    assert panel._card_activity(rec, {S: str(path)}, {S[:8]: 2}, {S: "ended"}) == {}
    assert panel._card_activity(dict(rec, percent=100), {S: str(path)}, {}, {S: "working"}) == {}


def test_refresh_interval_is_short_while_seen_and_backs_off_while_hidden():
    program = ("var POLL={work:10000,idle:20000,hidden:60000,most:300000};\n" + func("pollDelay") + func("anyWorking")
               + "\nconsole.log(JSON.stringify([pollDelay(false,true,0),pollDelay(false,false,0),"
                 "pollDelay(true,true,0),pollDelay(true,false,299999),pollDelay(true,false,300000),"
                 "pollDelay(true,false,900000),pollDelay(true,false,1e9),"
                 "anyWorking({sessions:[{state:'idle'},{state:'working'}]}),anyWorking({sessions:[]}),anyWorking(null)]));")
    out = json.loads(subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=5,
                                    check=True).stdout)
    assert out == [10000, 20000, 60000, 60000, 120000, 300000, 300000, True, False, False]
    assert "var POLL={work:10000,idle:20000,hidden:60000,most:300000}" in panel_page.PAGE
    assert "setInterval(function(){if(!document.hidden)load();},15000)" not in panel_page.PAGE


def test_subjects_lose_paths_links_addresses_and_secrets():
    tasks = [{"content": "Inspect /Users/someone/private/client/file", "status": "in_progress"},
             {"content": "Read ~/notes/plan.md and C:\\Users\\x\\y.txt", "status": "pending"},
             {"content": "Use sk_live_abcdefghijklmnop1234567 now", "status": "pending"},
             {"content": "Mail someone@example.com", "status": "pending"},
             {"content": "Edit src/ctb/cards.py", "status": "pending"},
             {"content": "Open https://example.com/x?token=abc", "status": "pending"}]
    rec = cards.sync_auto(S, -100, 7, "Topic", tasks, now=100)
    text = json.dumps(rec["steps"])
    for leak in ("/Users", "someone", "notes", "C:", "sk_live", "example.com", "token", "src/ctb"):
        assert leak not in text, leak
    assert "Edit cards.py" in rec["steps"] and "Fix A/B test" == cards.scrub_subject("Fix A/B test")


def test_a_partial_last_line_is_read_whole_once_it_is_finished(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, SAMPLE[:2])
    line = json.dumps(_use(9, "TaskUpdate", taskId="1", status="in_progress"))
    with open(path, "a", encoding="utf-8") as f:
        f.write(line[:20])                                             # still being written
    assert auto_progress.task_list(str(path)) == [{"content": "Read the code", "status": "pending"}]
    with open(path, "a", encoding="utf-8") as f:
        f.write(line[20:] + "\n")
    assert auto_progress.task_list(str(path)) == [{"content": "Read the code", "status": "in_progress"}]
    _write(path, [_use(10, "TaskUpdate", taskId="1", status="completed")], mode="a")
    assert auto_progress.task_list(str(path)) == [{"content": "Read the code", "status": "completed"}]


def test_tasks_created_far_back_in_a_large_transcript_are_found(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, SAMPLE[:4])
    filler = json.dumps({"type": "user", "message": {"role": "user", "content": "x" * 4000}}) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(filler * (9 * 1024 * 1024 // len(filler) + 1))              # more than the old 8 MB window
    _write(path, [_use(11, "TaskUpdate", taskId="2", status="in_progress")], mode="a")
    assert auto_progress.task_list(str(path)) == [{"content": "Read the code", "status": "pending"},
                                                  {"content": "Write the fix", "status": "in_progress"}]


def test_dates_and_numeric_slash_groups_stay_as_written():
    for text in ("Release on 2026/11/12", "Check 1/2/3 cases", "Due /2026/11/12/", "Compare 3/4"):
        assert cards.scrub_subject(text) == text
    assert cards.scrub_subject("Read client/acme/2026") == "Read [path]"


def test_the_first_scan_does_not_hold_the_shared_cache_lock(tmp_path, monkeypatch):
    import threading
    path = tmp_path / "t.jsonl"
    _write(path, SAMPLE)
    other = tmp_path / "o.jsonl"
    other.write_text("{}\n")
    scanning, go = threading.Event(), threading.Event()
    real = auto_progress._wanted

    def slow(line, tasks):
        scanning.set()
        go.wait(5)
        return real(line, tasks)
    monkeypatch.setattr(auto_progress, "_wanted", slow)
    out = []
    reader = threading.Thread(target=lambda: out.append(auto_progress.task_list(str(path))), daemon=True)
    reader.start()
    assert scanning.wait(5)
    done = threading.Event()
    threading.Thread(target=lambda: (auto_progress.cached(str(other), "k", lambda p: 1), done.set()),
                     daemon=True).start()
    try:
        assert done.wait(1.0)                                              # another reader is not held up
    finally:
        go.set()
    reader.join(5)
    assert [t["content"] for t in out[0]] == ["Read the code", "Write the fix", "Run the tests"]
    assert auto_progress.FIRST_READ <= 64 * 1024 * 1024
