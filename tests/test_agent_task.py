"""Which task and model a background agent was given: found at SubagentStart from the meta file or the parent
transcript, shown on the agents card and in the agents topic."""
from __future__ import annotations

import json
from pathlib import Path

from conftest import GOOD_TOKEN, FakeBot
from ctb import agent_task as AT, agents_card as AC, agents_topic as AP, mirror as M
from ctb.config import Config, state_dir
from ctb.topics import TopicMap

CHAT = -1001234567
SID = "sess-task-1"
NOW = 1_700_000_000.0


def cfg(**kw):
    kw.setdefault("target_language", "en")
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, chat_id=CHAT, **kw)


class Bot(FakeBot):
    def __init__(self):
        super().__init__()
        self.mid = 700

    def call(self, method, params=None, timeout=15):
        if method == "sendMessage":
            self.mid += 1
            self.calls.append((method, dict(params or {})))
            return {"message_id": self.mid}
        return super().call(method, params, timeout)


def call_row(tool_id, prompt, desc, stype="writer", model=None, effort=None, at=NOW - 5, name="Agent"):
    inp = {"description": desc, "subagent_type": stype, "prompt": prompt}
    if model:
        inp["model"] = model
    if effort:
        inp["effort"] = effort
    return {"type": "assistant", "timestamp": at, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": name, "input": inp}]}}


def write_session(tmp_path: Path, rows: list, agents: dict | None = None, session: str = SID):
    """A parent transcript plus agent files: agents = {agent_id: (first prompt, meta dict or None)}."""
    parent = tmp_path / f"{session}.jsonl"
    stamped = []
    for r in rows:
        r = dict(r)
        r["timestamp"] = __import__("datetime").datetime.fromtimestamp(
            r["timestamp"], __import__("datetime").timezone.utc).isoformat()
        stamped.append(r)
    parent.write_text("\n".join(json.dumps(r) for r in stamped) + "\n")
    folder = tmp_path / session / "subagents"
    folder.mkdir(parents=True)
    for aid, (prompt, meta) in (agents or {}).items():
        (folder / f"agent-{aid}.jsonl").write_text(json.dumps(
            {"type": "user", "isSidechain": True, "message": {"role": "user", "content": prompt}}) + "\n")
        if meta is not None:
            (folder / f"agent-{aid}.meta.json").write_text(json.dumps(meta))
    return parent, folder


def look(parent, aid, **kw):
    return AT.lookup(SID, aid, str(parent), **kw)


def meta_of(tool_id, **extra):
    return {"toolUseId": tool_id, "agentType": "writer", **extra}


def test_the_meta_link_selects_the_call_and_only_the_call_supplies_the_fields(tmp_path):
    parent, folder = write_session(
        tmp_path, [call_row("toolu_A", "p1", "Fix login tests", model="opus", effort="high")],
        {"a1": ("p1", meta_of("toolu_A", description="Other task from meta", model="haiku"))})
    assert look(parent, "a1") == {"id": "toolu_A", "task": "Fix login tests", "model": "Opus, high"}


def test_two_agents_of_one_type_started_together_each_get_their_own_call(tmp_path):
    rows = [call_row("toolu_A", "same start of a long prompt", "Fix login tests", model="opus"),
            call_row("toolu_B", "same start of a long prompt", "Update the docs", model="sonnet", effort="medium")]
    parent, folder = write_session(tmp_path, rows, {"a1": ("x", meta_of("toolu_A")), "a2": ("x", meta_of("toolu_B"))})
    assert look(parent, "a2")["task"] == "Update the docs" and look(parent, "a2")["model"] == "Sonnet, medium"
    assert look(parent, "a1")["task"] == "Fix login tests" and look(parent, "a1")["model"] == "Opus"


def test_nothing_is_guessed_without_a_verified_link(tmp_path):
    """Same-type calls, a matching prompt, a meta file naming an unknown call: none of them is shown."""
    rows = [call_row("toolu_A", "p1", "Fix login tests", model="opus")]
    parent, folder = write_session(tmp_path, rows, {
        "none": ("p1", None),  # no meta file: the prompt matches, but a prompt is not a link
        "nolink": ("p1", {"description": "Fix login tests", "model": "opus"}),  # a meta without toolUseId
        "unknown": ("p1", meta_of("toolu_ZZZ", description="Fix login tests", model="opus"))})  # call not in the transcript
    for aid in ("none", "nolink", "unknown"):
        assert look(parent, aid, budget=0.05) == {}


def test_a_model_missing_from_the_call_is_left_empty(tmp_path):
    parent, folder = write_session(tmp_path, [call_row("toolu_A", "p1", "Fix login tests")],
                                   {"a1": ("p1", meta_of("toolu_A"))})
    assert look(parent, "a1") == {"id": "toolu_A", "task": "Fix login tests", "model": ""}


def test_the_call_and_the_meta_file_may_arrive_in_any_order_within_the_budget(tmp_path):
    parent, folder = write_session(tmp_path, [], {"a1": ("p1", None)})
    meta = folder / "agent-a1.meta.json"
    steps = []

    def sleep(sec):
        steps.append(sec)
        if len(steps) == 1:  # the meta file shows up first, naming a call the transcript does not have yet
            meta.write_text(json.dumps(meta_of("toolu_A")))
        elif len(steps) == 2:  # then the parent transcript catches up
            row = call_row("toolu_A", "p1", "Fix login tests", model="opus")
            row["timestamp"] = "2023-11-14T22:13:15+00:00"
            parent.write_text(json.dumps(row) + "\n")

    now = iter(x * 0.25 for x in range(1000))
    found = look(parent, "a1", budget=5.0, sleep=sleep, clock=lambda: next(now))
    assert len(steps) == 2 and found["task"] == "Fix login tests"


def test_one_time_budget_covers_the_waiting_and_the_parsing(tmp_path):
    parent, folder = write_session(tmp_path, [], {"a1": ("p1", None)})
    slept, t = [], [0.0]

    def clock():
        return t[0]

    def sleep(sec):
        slept.append(sec)
        t[0] += sec

    assert look(parent, "a1", budget=0.6, sleep=sleep, clock=clock) == {}
    assert sum(slept) == 0.6 and max(slept) <= AT.POLL  # the last sleep is only the time that was left
    # parsing: a clock that jumps past the deadline while rows are read ends the lookup at once
    rows = [call_row("toolu_A", "p1", "Fix login tests")] + [{"type": "user", "timestamp": NOW, "message": {"content": "x"}}] * 300
    (tmp_path / "b").mkdir()
    parent, folder = write_session(tmp_path / "b", rows, {"a1": ("p1", meta_of("toolu_A"))})
    calls = [0]

    def late():
        calls[0] += 1
        return 0.0 if calls[0] == 1 else 99.0

    assert look(parent, "a1", budget=1.0, clock=late, sleep=lambda s: None) == {}


def test_a_failing_clock_or_sleep_never_raises(tmp_path):
    parent, folder = write_session(tmp_path, [], {"a1": ("p1", None)})

    def boom(*_):
        raise RuntimeError("no clock")

    assert look(parent, "a1", clock=boom) == {}
    ticks = iter(range(100))
    assert look(parent, "a1", budget=5.0, clock=lambda: float(next(ticks)), sleep=boom) == {}


def test_paths_come_from_the_session_folder_only(tmp_path):
    parent, folder = write_session(tmp_path, [call_row("toolu_A", "p1", "Fix login tests")],
                                   {"a1": ("p1", meta_of("toolu_A"))})
    assert look(parent, "a1")["task"] == "Fix login tests"
    other = tmp_path / "other-session.jsonl"
    other.write_text(parent.read_text())
    assert AT.lookup(SID, "a1", str(other), budget=0.05) == {}  # the transcript is not this session's
    assert AT.lookup(SID, "../a1", str(parent), budget=0.05) == {}  # an id that cannot be an agent's
    assert AT.lookup(SID, "a1", "", budget=0.05) == {} and AT.lookup("", "a1", str(parent), budget=0.05) == {}
    assert AT.lookup(SID, "a1", str(tmp_path / "missing" / f"{SID}.jsonl"), budget=0.05) == {}


def test_symlinks_and_odd_files_are_never_followed(tmp_path):
    secret = tmp_path / "elsewhere"
    secret.mkdir()
    (secret / "agent-a1.meta.json").write_text(json.dumps(meta_of("toolu_A")))
    (tmp_path / "ok").mkdir()
    parent, folder = write_session(tmp_path / "ok", [call_row("toolu_A", "p1", "Fix login tests")], {})
    meta = folder / "agent-a1.meta.json"
    meta.symlink_to(secret / "agent-a1.meta.json")  # a symlinked meta file
    assert look(parent, "a1", budget=0.05) == {}
    meta.unlink()
    meta.write_text(json.dumps(meta_of("toolu_A")) + " " * (AT.META_CAP + 10))  # too large
    assert look(parent, "a1", budget=0.05) == {}
    meta.unlink()
    meta.mkdir()  # not a regular file
    assert look(parent, "a1", budget=0.05) == {}
    meta.rmdir()
    meta.write_text(json.dumps(meta_of("toolu_A")))
    assert look(parent, "a1")["task"] == "Fix login tests"
    # the whole subagents folder is a link out of the session's folder
    moved = folder.with_name("moved")
    folder.rename(moved)
    folder.symlink_to(moved, target_is_directory=True)
    assert look(parent, "a1", budget=0.05) == {}
    # the parent transcript itself is a link
    folder.unlink()
    moved.rename(folder)
    real = parent.with_name("real.jsonl")
    parent.rename(real)
    parent.symlink_to(real)
    assert look(parent, "a1", budget=0.05) == {}


def test_only_one_fixed_window_of_the_transcript_is_read(tmp_path):
    big = "x" * 4000
    rows = [call_row("toolu_OLD", "p0", "Too old to read")]
    rows += [{"type": "user", "timestamp": NOW - 4, "message": {"content": big}} for _ in range(500)]
    rows.append(call_row("toolu_B", "p1", "Fix login tests"))
    parent, folder = write_session(tmp_path, rows, {"old": ("p0", meta_of("toolu_OLD")), "a1": ("p1", meta_of("toolu_B"))})
    assert parent.stat().st_size > AT.TAIL
    assert look(parent, "a1")["task"] == "Fix login tests"
    assert look(parent, "old", budget=0.05) == {}


def test_task_text_is_cleaned_limited_and_labelled_secrets_are_hidden():
    secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0" * 3
    assert secret not in AT.clean_task(f"Rotate key {secret} now")
    assert "hunter2" not in AT.clean_task("Reset the password: hunter2 for staging")
    assert "abc123" not in AT.clean_task("תיקון סיסמה: abc123 בשרת")
    assert AT.clean_task("  Fix\n  login   tests ") == "Fix login tests"
    long = AT.clean_task("word " * 40)
    assert len(long) <= AT.TASK_LIMIT and long.endswith("…")
    assert AT.model_label("claude-opus-4-8", "high") == "Opus 4.8, high"
    assert AT.model_label("opus", "") == "Opus" and AT.model_label("", "high") == ""


# -- the card ----------------------------------------------------------------------------------------

def spec_of(task, model=""):
    return {"id": "toolu_x", "task": task, "model": model}


def start(bot, aid, at, found=None, c=None, name="writer"):
    AC.agent_started(c or cfg(), SID, 11, aid, name, bot, now=at, sleep=lambda s: None, found=found)


def finish(bot, aid, at, c=None, name="", model="Opus 5, high", summary="all good", full="full"):
    AC.agent_finished(c or cfg(), SID, 11, aid, {"name": name, "model": model, "summary": summary, "full": full,
                                                  "failed": False}, bot, now=at, sleep=lambda s: None)


def test_running_card_shows_model_and_task_and_finished_card_keeps_the_task():
    bot = Bot()
    start(bot, "a1", NOW, spec_of("Fix login tests", "Opus, high"))
    assert bot.methods("sendMessage")[0]["text"] == "🤖 Agents\n⏳ writer · Opus, high · 0s\n↳ Fix login tests"
    start(bot, "a2", NOW + 1)
    finish(bot, "a1", NOW + 70, name="Fix login tests")
    text = bot.methods("editMessageText")[-1]["text"]
    assert "1. ✅ writer · Opus 5, high · 1m 10s\n↳ Fix login tests\n💬 all good" in text
    assert "2. ⏳ writer · 1m 09s" in text  # no model known while running: nothing is guessed


def test_the_task_comes_from_the_report_when_the_start_had_none():
    bot = Bot()
    start(bot, "a1", NOW)
    finish(bot, "a1", NOW + 5, name="Update the docs")
    agent = json.loads(AC._path(SID).read_text())["cards"]["1"]["agents"][0]
    assert agent["task"] == "Update the docs" and agent["type"] == "writer"


def test_collapsed_card_lists_one_numbered_line_per_agent():
    bot = Bot()
    start(bot, "a1", NOW, spec_of("Fix login tests", "Opus, high"))
    start(bot, "a2", NOW + 1, name="reviewer")
    finish(bot, "a1", NOW + 61, name="Fix login tests")
    finish(bot, "a2", NOW + 20, name="reviewer", model="")
    card = json.loads(AC._path(SID).read_text())["cards"]["1"]
    text, _ = AC.render(cfg(), card, NOW + 100)
    assert text.split("\n") == ["🤖 2 agents finished · 2 ✅", "1. ✅ Fix login tests · Opus 5, high · 1m 01s",
                                "2. ✅ reviewer · 19s"]


def full_card(n, cap_full=lambda i: True):
    agents = [{"id": str(i), "type": "w", "task": f"Task {i}", "model": "Opus", "status": "done", "started": 0,
               "ended": 9, "full": "result" if cap_full(i) else ""} for i in range(n)]
    return {"seq": 4, "collapsed": True, "agents": agents}


def test_every_button_is_numbered_like_its_line_and_only_listed_agents_have_one():
    card = full_card(9, cap_full=lambda i: i % 2 == 0)  # agents 0, 2, 4, 6, 8 have a full result
    card["agents"][1]["moved"] = True  # a moved one is neither listed nor counted
    text, markup = AC.render(cfg(), card, 100)
    lines = text.split("\n")
    assert lines[0].startswith("🤖 8 agents finished") and lines[-1] == "+2 more"
    listed = [x for x in lines[1:-1]]
    assert len(listed) == AC.COLLAPSED_LINES and listed[1].startswith("2. ✅ Task 2")
    assert markup["inline_keyboard"][-1][0]["callback_data"] == "agclose:4"
    buttons = [b for row in markup["inline_keyboard"][:-1] for b in row]
    # listed lines 1..6 are agents 0, 2, 3, 4, 5, 6: the ones with a result are lines 1 (agent 0), 2 (agent 2),
    # 4 (agent 4) and 6 (agent 6); agent 8 is hidden behind "+2 more" and has no button
    assert [(b["text"], b["callback_data"]) for b in buttons] == [
        ("📄 Full result 1", "agr:4:0"), ("📄 Full result 2", "agr:4:2"),
        ("📄 Full result 4", "agr:4:4"), ("📄 Full result 6", "agr:4:6")]
    for b in buttons:  # the line a button names shows the same agent
        n, idx = int(b["text"].rsplit(" ", 1)[1]), int(b["callback_data"].rsplit(":", 1)[1])
        assert f"Task {idx}" in listed[n - 1]
    one, markup1 = AC.render(cfg(), full_card(1), 100)
    assert markup1["inline_keyboard"][0][0]["text"] == "📄 Full result" and "1." not in one  # a lone agent: no number


def test_collapsed_card_is_capped_with_plus_n_more_and_fits_a_message():
    card = full_card(9)
    for a in card["agents"]:
        a["task"] += " " + "w" * 50
    text, _ = AC.render(cfg(), card, 100)
    lines = text.split("\n")
    assert len(lines) == 1 + AC.COLLAPSED_LINES + 1 and lines[-1] == "+3 more" and len(text) < 4096
    assert AC.render(cfg(target_language="he"), card, 100)[0].split("\n")[-1] == "\u200fועוד 3"


def test_a_card_too_long_for_one_message_drops_agents_and_their_buttons():
    agents = [{"id": str(n), "name": "w", "type": "w", "task": "t" * 60, "model": "m" * 40, "status": "done",
               "started": 0, "ended": 4, "full": "x"} for n in range(80)]
    text, markup = AC.render(cfg(), {"seq": 1, "agents": agents}, 5)
    assert len(text) <= AC.TEXT_LIMIT and "more" in text.split("\n")[-1]
    assert markup["inline_keyboard"][-1][0]["callback_data"] == "agclose:1"
    numbers = [int(b["text"].rsplit(" ", 1)[1]) for row in markup["inline_keyboard"][:-1] for b in row]
    last_listed = max(int(x.split(".")[0]) for x in text.split("\n") if x[:1].isdigit())
    assert numbers and max(numbers) <= last_listed


def test_hebrew_card_isolates_a_short_english_task_and_puts_a_long_one_on_its_own_line():
    c = cfg(target_language="he")
    short = AC._agent_lines({"type": "writer", "task": "Fix login tests", "model": "Opus, high", "status": "running",
                             "started": 0}, 5, True)
    assert short.split("\n") == ["‏⏳ ⁨writer⁩ · ⁨Opus, high⁩ · 5s", "‏↳ ⁨Fix login tests⁩"]
    long = AC._agent_lines({"type": "writer", "task": "Fix the flaky login tests today", "status": "running",
                            "started": 0}, 5, True)
    task_line = long.split("\n")[1]
    assert task_line == "‏↳ Fix the flaky login tests today"  # no isolate: nothing Hebrew shares this line
    assert not any("֐" <= ch <= "׿" for ch in task_line)
    heb = AC._agent_lines({"type": "writer", "task": "תיקון בדיקות", "status": "running", "started": 0}, 5, True)
    assert heb.split("\n")[1] == "‏↳ תיקון בדיקות"
    card = {"seq": 1, "collapsed": True, "agents": [
        {"id": "1", "type": "writer", "task": "Fix login tests", "model": "Opus", "status": "done", "started": 0, "ended": 65},
        {"id": "2", "type": "writer", "task": "Fix the flaky login tests today", "model": "Opus", "status": "failed",
         "started": 0, "ended": 9}]}
    lines = AC.render(c, card, 100)[0].split("\n")
    assert "‏1. ✅ ⁨Fix login tests⁩ · ⁨Opus⁩ · 1m 05s" in lines
    assert "‏2. ❌ Fix the flaky login tests today" in lines and "‏⁨Opus⁩ · 9s" in lines
    assert "סוכנים סיימו" in lines[0] and "1 ✅ 1 ❌" in lines[0]


def test_labelled_secrets_in_the_task_never_reach_the_card_the_collapsed_card_or_the_topic():
    for lang, task, secret in (("en", "Reset the password: hunter2 for staging", "hunter2"),
                               ("he", "תיקון סיסמה: abc123 בשרת", "abc123")):
        bot = Bot()
        c = cfg(target_language=lang)
        sid = f"sess-secret-{lang}"
        TopicMap(state_dir() / "topics.json", bot, CHAT).ensure(sid, "Build the parser")
        # the task as the report names it (the finish path) and as the type label
        AC.agent_started(c, sid, 11, "a1", f"agent {task}", bot, now=NOW, sleep=lambda s: None,
                         found={"id": "t", "task": AT.clean_task(task), "model": ""})
        AC.agent_finished(c, sid, 11, "a1", {"name": task, "summary": "ok", "full": "f"}, bot,
                          now=NOW + 5, sleep=lambda s: None)
        AP.agent_started(c, sid, "b1", f"agent {task}", bot, now=NOW,
                         found={"id": "t", "task": AT.clean_task(task), "model": ""})
        sent = json.dumps(bot.calls, ensure_ascii=False) + AC._path(sid).read_text()
        assert secret not in sent, (lang, secret)
        card = json.loads(AC._path(sid).read_text())["cards"]["1"]
        assert secret not in AC.render(c, dict(card, collapsed=True), NOW + 9)[0]


def test_free_text_summaries_keep_ordinary_numbers_while_the_task_hides_labelled_secrets():
    bot = Bot()
    start(bot, "a1", NOW, {"id": "t", "task": AT.clean_task("Reset the password: hunter2"), "model": ""})
    finish(bot, "a1", NOW + 5, name="Reset the password: hunter2", summary="Build failed, exit code: 1234")
    card = json.loads(AC._path(SID).read_text())["cards"]["1"]
    text = AC.render(cfg(), dict(card, collapsed=False), NOW + 9)[0]  # the open card shows the result line
    assert "💬 Build failed, exit code: 1234" in text and "hunter2" not in text
    assert AC.agent_summary(cfg(), SID, 11, "a1", "exit code: 1234 again", bot, now=NOW + 20, sleep=lambda s: None)
    assert "exit code: 1234 again" in json.dumps(json.loads(AC._path(SID).read_text()))


def lines_of(bot=None):
    return [(a["id"], a["status"], a["task"]) for a in json.loads(AC._path(SID).read_text())["cards"]["1"]["agents"]]


def test_a_repeated_start_is_ignored_and_a_new_call_id_after_the_end_is_a_new_run():
    bot = Bot()
    start(bot, "a1", NOW, {"id": "toolu_1", "task": "Fix login tests", "model": ""})
    start(bot, "a1", NOW + 0.1, {"id": "toolu_1", "task": "Fix login tests", "model": ""})  # the same run again
    start(bot, "x", NOW + 0.2)  # keeps the card open
    assert lines_of() == [("a1", "running", "Fix login tests"), ("x", "running", "")]
    finish(bot, "a1", NOW + 10, name="Fix login tests")
    start(bot, "a1", NOW + 10.2, {"id": "toolu_1", "task": "Fix login tests", "model": ""})  # late duplicate start
    start(bot, "a1", NOW + 10.3)  # a start with no verified id after the end: the late hook too
    assert lines_of() == [("a1", "done", "Fix login tests"), ("x", "running", "")]
    start(bot, "a1", NOW + 10.9, {"id": "toolu_2", "task": "Update the docs", "model": ""})  # a quick real re-run
    assert lines_of() == [("a1", "done", "Fix login tests"), ("x", "running", ""), ("a1", "running", "Update the docs")]
    finish(bot, "a1", NOW + 20, name="Update the docs")  # the end belongs to the running line
    agents = json.loads(AC._path(SID).read_text())["cards"]["1"]["agents"]
    assert [a["status"] for a in agents] == ["done", "running", "done"] and agents[0]["ended"] == NOW + 10


def test_a_start_that_arrives_after_its_own_end_fills_the_line_instead_of_adding_a_running_one():
    bot = Bot()
    start(bot, "x", NOW)
    finish(bot, "a1", NOW + 5, name="Fix login tests")  # the end hook won the race: the start was never seen
    start(bot, "a1", NOW + 5.5, {"id": "toolu_1", "task": "Fix login tests", "model": "Opus"})
    agents = json.loads(AC._path(SID).read_text())["cards"]["1"]["agents"]
    assert [(a["id"], a["status"]) for a in agents] == [("x", "running"), ("a1", "done")]
    assert agents[1]["model"] == "Opus 5, high" and agents[1]["run"] == "toolu_1"


def test_a_stale_moved_line_never_blocks_a_later_start_of_the_same_agent():
    bot = Bot()
    start(bot, "a1", NOW, {"id": "toolu_1", "task": "Old", "model": ""})
    st = json.loads(AC._path(SID).read_text())
    st["cards"]["1"]["agents"][0]["moved"] = True  # left behind in "running" by a card change
    AC._path(SID).write_text(json.dumps(st))
    start(bot, "a1", NOW + 2, {"id": "toolu_2", "task": "New", "model": ""})
    live = [a for c in json.loads(AC._path(SID).read_text())["cards"].values() for a in c["agents"]
            if not a.get("moved") and a["status"] == "running"]
    assert [(a["id"], a["task"]) for a in live] == [("a1", "New")]


# -- the lifecycle and the agents topic -----------------------------------------------------------------

def test_lifecycle_start_finds_the_task_in_the_transcript(tmp_path):
    parent, folder = write_session(
        tmp_path, [call_row("toolu_A", "p1", "Fix login tests", stype="worker", model="opus", effort="high",
                            name="Task")], {"a1": ("p1", meta_of("toolu_A"))})
    bot = Bot()
    TopicMap(state_dir() / "topics.json", bot, CHAT).ensure(SID, "Build the parser")
    M.run_lifecycle(cfg(), SID, "subagent-start", "worker", bot=bot, agent_id="a1", transcript=str(parent))
    assert bot.methods("sendMessage")[-1]["text"] == "🤖 Agents\n⏳ worker · Opus, high · 0s\n↳ Fix login tests"


def test_agents_topic_first_message_names_model_and_task_and_follows_the_hebrew_rules():
    bot = Bot()
    TopicMap(state_dir() / "topics.json", bot, CHAT).ensure(SID, "Build the parser")
    AP.agent_started(cfg(), SID, "a1", "writer", bot, now=NOW, found=spec_of("Fix login tests", "Opus, high"))
    assert bot.methods("sendMessage")[-1]["text"] == "▶️ Background agent started · writer · Opus, high\n↳ Fix login tests"
    c = cfg(target_language="he")
    he = AP._started_text(c, "writer", {"task": "Fix the flaky login tests today", "model": "Opus"})
    assert he.split("\n") == ["‏▶️ סוכן ברקע התחיל · ⁨writer⁩ · ⁨Opus⁩", "‏↳ Fix the flaky login tests today"]
    longtype = AP._started_text(c, "very long agent type name here", {"model": "Opus"})
    assert longtype.split("\n") == ["‏▶️ סוכן ברקע התחיל", "‏very long agent type name here", "‏⁨Opus⁩"]
    assert len(AP._started_text(cfg(), "t" * 500, {})) < 200  # a long type is cut
    bot2 = Bot()
    AP.agent_started(cfg(), "sess-x", "b1", "writer", bot2, now=NOW)
    assert bot2.methods("sendMessage")[-1]["text"] == "▶️ Background agent started · writer"  # nothing found: as before


def test_agents_topic_repeated_start_posts_nothing_and_a_new_call_id_after_the_end_is_a_new_run():
    bot = Bot()
    TopicMap(state_dir() / "topics.json", bot, CHAT).ensure(SID, "Build the parser")

    def found(i, task):
        return {"id": i, "task": task, "model": ""}

    def started():
        return [p["text"] for p in bot.methods("sendMessage") if "started" in p["text"]]

    AP.agent_started(cfg(), SID, "a1", "writer", bot, now=NOW, found=found("toolu_1", "Fix login tests"))
    AP.agent_started(cfg(), SID, "a1", "writer", bot, now=NOW + 0.1, found=found("toolu_1", "Fix login tests"))
    assert len(started()) == 1
    AP.agent_finished(cfg(), SID, "a1", "done", bot, None, now=NOW + 10)
    AP.agent_started(cfg(), SID, "a1", "writer", bot, now=NOW + 10.2, found=found("toolu_1", "Fix login tests"))
    AP.agent_started(cfg(), SID, "a1", "writer", bot, now=NOW + 10.3)  # no verified id: the late hook
    assert len(started()) == 1
    AP.agent_started(cfg(), SID, "a1", "writer", bot, now=NOW + 10.9, found=found("toolu_2", "Update the docs"))
    assert len(started()) == 2 and started()[-1].endswith("↳ Update the docs")
    # an end that came before its start: the start is the late hook, not a second run
    AP.agent_finished(cfg(), SID, "b1", "done", bot, None, now=NOW + 30)
    AP.agent_started(cfg(), SID, "b1", "writer", bot, now=NOW + 30.2, found=found("toolu_9", "Other"))
    assert len(started()) == 2
