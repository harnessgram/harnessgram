"""The full session log: built from the transcript without a model, secrets hidden, sent once per task."""
import json

from conftest import GOOD_TOKEN
from ctb import cli, dashboard, sessionlog
from ctb.config import Config, state_dir
from ctb.topics import TopicMap, set_state

OWNER = 7
SID = "sess-log-1"
SECRET = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # gitleaks:allow (dummy value for the redaction test)


def cfg(**kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, **dict({"ui_language": "en"}, **kw))


def tool(n, name="Bash", **inp):
    return [{"type": "assistant", "uuid": f"a{n}", "timestamp": f"2026-11-01T10:{n:02d}:00Z",
             "message": {"content": [{"type": "tool_use", "id": f"t{n}", "name": name,
                                      "input": dict({"description": f"step {n}"}, **inp)}]}},
            {"type": "user", "uuid": f"r{n}", "timestamp": f"2026-11-01T10:{n:02d}:01Z",
             "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{n}", "content": "ok output"}]}}]


def transcript(tmp_path, n_tools):
    rows = [{"type": "user", "uuid": "u0", "timestamp": "2026-11-01T09:59:00Z", "message": {"content": "please fix it"}}]
    for n in range(1, n_tools + 1):
        rows += tool(n, command=f"run {n}" if n != 3 else f"curl -H 'Authorization: Bearer {SECRET}' x")
    rows.append({"type": "assistant", "uuid": "done", "timestamp": "2026-11-01T11:00:00Z",
                 "message": {"content": [{"type": "text", "text": "All done."}]}})
    path = tmp_path / f"{SID}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    return str(path)


def test_the_page_lists_messages_actions_and_hides_secrets(tmp_path):
    page, actions, last = sessionlog.build(SID, transcript(tmp_path, 4), None, "Billing", "en")
    assert actions == 4 and last == "done"
    assert "please fix it" in page and "All done." in page and "step 4" in page and "ok output" in page
    assert SECRET not in page and "ghp_" not in page
    assert 'dir="ltr"' in page and "<title>Full log: Billing</title>" in page
    he, *_ = sessionlog.build(SID, transcript(tmp_path, 1), None, "חיוב", "he")
    assert 'dir="rtl"' in he and "תיעוד מלא: חיוב" in he


def test_only_new_rows_after_the_last_log(tmp_path):
    path = transcript(tmp_path, 4)
    page, actions, _ = sessionlog.build(SID, path, "r2", "x", "en")
    assert actions == 2 and "step 3" in page and "step 2" not in page


def test_long_output_is_cut(tmp_path):
    path = tmp_path / "t.jsonl"
    rows = tool(1)
    rows[1]["message"]["content"][0]["content"] = "z" * (sessionlog.OUTPUT_CAP + 50)
    path.write_text("\n".join(json.dumps(r) for r in rows))
    page, *_ = sessionlog.build(SID, str(path), None, "x", "en")
    assert "z" * sessionlog.OUTPUT_CAP in page and "z" * (sessionlog.OUTPUT_CAP + 1) not in page
    assert "(shortened)" in page


def rig(bot, tmp_path, n):
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Billing")
    path = transcript(tmp_path, n)
    return thread, (lambda s: path if s == SID else None)


def test_send_uploads_a_document_once_and_remembers_where_it_stopped(bot, tmp_path):
    thread, find = rig(bot, tmp_path, 3)
    assert sessionlog.send(cfg(), SID, bot=bot, find=find) is True
    method, fields = next((m, p) for m, p in bot.calls if m == "sendDocument")
    assert fields["message_thread_id"] == thread and fields["caption"] == "📒 Full log of the task · 3 actions"
    assert fields["disable_notification"] == "true" and fields["_file"].startswith("log-")
    assert sessionlog.send(cfg(), SID, bot=bot, find=find, min_actions=1) is False   # nothing new since


def test_everything_ignores_the_marks(bot, tmp_path):
    thread, find = rig(bot, tmp_path, 2)
    assert sessionlog.send(cfg(), SID, bot=bot, find=find)
    assert sessionlog.send(cfg(), SID, everything=True, bot=bot, find=find)
    assert len(bot.methods("sendDocument")) == 2


def grow(tmp_path, n_tools):
    """The session continues: a new transcript with more tool calls (same session id)."""
    return transcript(tmp_path, n_tools)


def test_auto_is_off_by_default(bot, tmp_path):
    thread, find = rig(bot, tmp_path, sessionlog.MIN_ACTIONS)
    set_state(cfg(), SID, "done", bot=bot)
    assert Config().session_log_auto is False
    assert sessionlog.auto(cfg(), bot=bot, find=find) == 0 and not (state_dir() / sessionlog.MARKS).exists()


def test_first_run_records_history_as_already_sent(bot, tmp_path):
    thread, find = rig(bot, tmp_path, 30)                  # a long finished task from before it was enabled
    set_state(cfg(), SID, "done", bot=bot)
    on = cfg(session_log_auto=True)
    assert sessionlog.auto(on, bot=bot, find=find) == 0 and not bot.methods("sendDocument")
    assert json.loads((state_dir() / sessionlog.MARKS).read_text())[SID] == "done"
    assert sessionlog.auto(on, bot=bot, find=find) == 0     # unchanged: still nothing


def test_auto_sends_only_what_is_new_after_the_baseline(bot, tmp_path):
    thread, find = rig(bot, tmp_path, 5)
    on = cfg(session_log_auto=True)
    set_state(cfg(), SID, "done", bot=bot)
    sessionlog.auto(on, bot=bot, find=find)                 # baseline
    path = tmp_path / f"{SID}.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    for n in range(10, 10 + sessionlog.MIN_ACTIONS):        # a new task of exactly the threshold
        rows += tool(n)
    rows.append({"type": "assistant", "uuid": "done2", "timestamp": "2026-11-01T12:00:00Z",
                 "message": {"content": [{"type": "text", "text": "Finished."}]}})
    set_state(cfg(), SID, "busy", bot=bot)
    path.write_text("\n".join(json.dumps(r) for r in rows))
    assert sessionlog.auto(on, bot=bot, find=find) == 0     # still working
    set_state(cfg(), SID, "done", bot=bot)
    assert sessionlog.auto(on, bot=bot, find=find) == 1
    page = bot.methods("sendDocument")[0]
    assert page["caption"] == f"📒 Full log of the task · {sessionlog.MIN_ACTIONS} actions"
    assert sessionlog.auto(on, bot=bot, find=find) == 0     # nothing new


def test_a_short_task_is_not_read_again_until_the_file_changes(bot, tmp_path, monkeypatch):
    thread, find = rig(bot, tmp_path, 2)
    on = cfg(session_log_auto=True)
    set_state(cfg(), SID, "done", bot=bot)
    sessionlog.auto(on, bot=bot, find=find)                 # baseline
    path = tmp_path / f"{SID}.jsonl"
    path.write_text(path.read_text() + "\n" + json.dumps(tool(30)[0]))   # a little more: below the threshold
    reads = []
    real = sessionlog.build
    monkeypatch.setattr(sessionlog, "build", lambda *a, **k: reads.append(1) or real(*a, **k))
    assert sessionlog.auto(on, bot=bot, find=find) == 0 and len(reads) == 1
    for _ in range(3):
        assert sessionlog.auto(on, bot=bot, find=find) == 0
    assert len(reads) == 1                                  # cached by size and time
    path.write_text(path.read_text() + "\n" + json.dumps(tool(31)[0]))
    sessionlog.auto(on, bot=bot, find=find)
    assert len(reads) == 2


def test_the_dashboard_tick_sends_logs_but_not_the_daily_summary(monkeypatch, bot):
    runs = []
    monkeypatch.setattr(sessionlog, "auto", lambda c, **kw: runs.append(kw.get("bot")) or 0)
    dashboard.run(cfg(), bot=bot)
    dashboard.run(cfg(), daily=True, bot=bot)
    assert runs == [bot]


def test_cli_command(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(sessionlog, "send", lambda c, s, everything=False, **kw: seen.append((s, everything)) or True)
    monkeypatch.setattr(cli.C, "load", lambda: cfg())
    assert cli.main(["session-log", SID, "--all"]) == 0
    assert seen == [(SID, True)] and capsys.readouterr().out.strip() == "sent"
