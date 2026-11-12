"""Scheduled tasks: proof-based detection, one topic per task and chat, mute of ordinary updates only, and
closing a session the bridge cannot stop. Wrong silence is the worst outcome: every doubt mirrors normally."""
import io
import json
import sys
import time

import pytest

from ctb import cli, hooks, inbox as I, mirror, progress, scheduled, sessions_menu as sm, topics as T
from ctb.config import Config, state_dir, write_private
from ctb.topics import TopicMap

OWNER = 12345
TASK = "nightly-report"


def cfg():
    return Config(bot_token="123456:ABCDEF", owner_chat_id=OWNER)


def tag(name=TASK):
    return f'<scheduled-task name="{name}" file="/tasks/{name}/SKILL.md">\nRun the report.\n</scheduled-task>'


SCHEDULER = {"entrypoint": "claude-desktop", "promptSource": "system",
             "origin": {"kind": "task-notification", "subkind": "scheduled-trigger", "fireReason": "scheduled"}}
HUMAN = {"entrypoint": "cli", "promptSource": "typed", "origin": {"kind": "human"}}


def user_row(text, **fields):
    return {"type": "user", **fields, "message": {"role": "user", "content": text}}


@pytest.fixture
def claude(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / "claude" / "projects" / "p").mkdir(parents=True)
    (tmp_path / "claude" / "sessions").mkdir(parents=True)
    return tmp_path / "claude" / "projects" / "p"


def write(folder, sid, *rows, tail=""):
    path = folder / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + tail)
    return str(path)


def queue_rows(sid, at="2026-01-05T08:00:00.000Z"):
    return [{"type": "queue-operation", "operation": "enqueue", "sessionId": sid, "timestamp": at},
            {"type": "queue-operation", "operation": "dequeue", "sessionId": sid, "timestamp": at}]


def run(folder, sid, at="2026-01-05T08:00:00.000Z"):
    """A finished scheduled run: queue rows, the scheduler's first user row, a reply."""
    return write(folder, sid, *queue_rows(sid, at), user_row(tag(), **SCHEDULER),
                 {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}})


def tmap(bot):
    return TopicMap(state_dir() / "topics.json", bot, OWNER)


# --- 1. detection needs proof -----------------------------------------------------------------------------

def test_parse_accepts_only_a_well_formed_tag_at_the_start():
    assert scheduled.parse_task(tag()) == TASK
    for odd in ("two words", 'a"b', "../etc", "a/b", "<x>", "x" * 65, "שם", "-lead", "a\nb", ""):
        assert scheduled.parse_task(f'<scheduled-task name="{odd}" file="f">') == "", odd
    assert scheduled.parse_task("please look at " + tag()) == ""
    assert scheduled.parse_task('<scheduled-task name="nightly-report" file="f"') == ""
    assert scheduled.parse_task(None) == ""


def test_only_the_schedulers_first_row_is_proof(claude):
    assert scheduled.task_of("s1", run(claude, "s1")) == TASK
    pasted = write(claude, "s2", user_row(tag(), **HUMAN))  # the owner pasted the tag: not a scheduled run
    assert scheduled.task_of("s2", pasted) == ""
    no_origin = write(claude, "s3", user_row(tag(), entrypoint="claude-desktop"))
    assert scheduled.task_of("s3", no_origin) == ""
    later = write(claude, "s4", user_row("fix the login page", **HUMAN), user_row(tag(), **SCHEDULER))
    assert scheduled.task_of("s4", later) == ""
    meta_first = write(claude, "s5", user_row("<local-command-caveat>", isMeta=True, **SCHEDULER),
                       user_row(tag(), **SCHEDULER))
    assert scheduled.task_of("s5", meta_first) == ""


def test_undecided_is_ordinary(claude):
    cases = {
        "no-user-row": write(claude, "u1", *queue_rows("u1")),
        "partial-row": write(claude, "u2", *queue_rows("u2"), tail=json.dumps(user_row(tag(), **SCHEDULER))),
        "corrupt-row": write(claude, "u3", tail="{not json\n" + json.dumps(user_row(tag(), **SCHEDULER)) + "\n"),
        "missing": str(claude / "nope.jsonl"),
        "a-directory": str(claude),
    }
    for sid, path in cases.items():
        assert scheduled.decide(path) is None, sid
        assert scheduled.task_of(sid, path) == "", sid


def test_corrupt_state_files_mirror_normally(claude):
    run(claude, "x1")
    for name in (scheduled.MUTED_FILE, scheduled.TOPICS_FILE, "closed-topics.json"):
        write_private(state_dir() / name, "{broken")
    assert scheduled.suppressed("x1", "reply") is False
    assert scheduled.task_of("x1") == TASK


def test_the_answer_comes_from_the_given_transcript_not_a_cache(claude):
    sched = run(claude, "same-id")
    assert scheduled.task_of("same-id", sched) == TASK
    other = write(claude, "other-file", user_row("hello", **HUMAN))
    assert scheduled.task_of("same-id", other) == ""  # the same id with another transcript inherits nothing
    assert not list(state_dir().glob("scheduled-sessions*"))


# --- one topic per chat and task ---------------------------------------------------------------------------

def test_two_runs_share_one_topic_and_typed_text_goes_to_the_newest(claude, bot):
    run(claude, "run-a")
    run(claude, "run-b")
    topics = tmap(bot)
    thread = topics.ensure("run-a", "t")
    assert topics.ensure("run-b", "t") == thread
    assert len(bot.methods("createForumTopic")) == 1 and bot.methods("createForumTopic")[0]["name"] == TASK
    assert topics.place_of("run-a").thread == thread  # the older run keeps mirroring into it
    assert topics.session_at(OWNER, thread) == "run-b"
    topics.ensure("run-a", "renamed")  # the older run touching its entry does not take the topic back
    assert topics.session_at(OWNER, thread) == "run-b"


def test_real_order_late_detection_remaps_and_leaves_the_old_topic_alone(claude, bot):
    """SessionStart (no user row yet) -> the prompt makes an ordinary topic -> first row -> reply."""
    topics = tmap(bot)
    run(claude, "run-a")
    shared = topics.ensure("run-a", "t")
    write(claude, "run-b", *queue_rows("run-b"))
    stray = topics.ensure("run-b", "busy")                   # undecided: an ordinary topic for now
    assert stray != shared
    run(claude, "run-b")                                      # the first user row lands
    assert topics.ensure("run-b", "reply") == shared
    assert topics.session_at(OWNER, shared) == "run-b"
    assert not bot.methods("closeForumTopic") and not bot.methods("deleteForumTopic")
    assert not [p for p in bot.methods("editForumTopic") if p["message_thread_id"] == stray]


def test_a_task_topic_is_shared_only_inside_its_own_chat(claude, bot):
    run(claude, "run-a")
    scheduled.remember_topic(TASK, -100777, 55, "elsewhere")  # the same task name in another group
    assert tmap(bot).ensure("run-a", "t") != 55
    assert scheduled.topic_of(OWNER, TASK) == tmap(bot).place_of("run-a").thread


def test_ordinary_sessions_keep_their_own_topics(claude, bot):
    write(claude, "plain-a", user_row("hello", **HUMAN))
    write(claude, "plain-b", user_row(tag(), **HUMAN))
    topics = tmap(bot)
    assert topics.ensure("plain-a", "A") != topics.ensure("plain-b", "B")


def test_a_topic_closed_when_a_run_ended_is_reopened_for_the_next(claude, bot):
    run(claude, "run-a")
    run(claude, "run-b")
    topics = tmap(bot)
    thread = topics.ensure("run-a", "t")
    sm._set_mark("run-a", thread, auto=True, chat=OWNER)
    assert topics.ensure("run-b", "t") == thread
    assert bot.methods("reopenForumTopic") == [{"chat_id": OWNER, "message_thread_id": thread}]
    assert sm.closed_marks() == {}


def test_a_scheduled_task_topic_is_never_auto_closed(claude, bot, monkeypatch):
    run(claude, "run-a")
    topics = tmap(bot)
    topics.ensure("run-a", "t")
    monkeypatch.setattr(I, "_closed", lambda session: True)
    monkeypatch.setattr(sm, "collect", lambda t, now=None: [sm.Entry("run-a", TASK, topics.place_of("run-a").thread,
                                                                     99999, "ended", False)])
    assert sm.close_after_end(cfg(), bot, topics, "run-a") is False
    assert not bot.methods("closeForumTopic")
    write(claude, "plain-a", user_row("hello", **HUMAN))  # an ordinary session still closes as before
    topics.ensure("plain-a", "p")
    monkeypatch.setattr(sm, "collect", lambda t, now=None: [sm.Entry("plain-a", "p", topics.place_of("plain-a").thread,
                                                                     99999, "ended", False)])
    assert sm.close_after_end(cfg(), bot, topics, "plain-a") is True


def test_typed_text_goes_to_the_run_that_started_last_whatever_the_detection_order(claude, bot):
    topics = tmap(bot)
    run(claude, "run-new", at="2026-01-05T12:00:00.000Z")
    shared = topics.ensure("run-new", "t")
    write(claude, "run-old", *queue_rows("run-old", "2026-01-05T08:00:00.000Z"))  # the older run, still undecided
    topics.ensure("run-old", "busy")
    run(claude, "run-old", at="2026-01-05T08:00:00.000Z")
    assert topics.ensure("run-old", "reply") == shared  # detected after the newer run
    assert topics.session_at(OWNER, shared) == "run-new"


def test_a_deleted_task_topic_is_replaced(claude, bot):
    run(claude, "run-a")
    run(claude, "run-b")
    topics = tmap(bot)
    thread = topics.ensure("run-a", "t")
    topics.forget("run-a")  # what the mirror does when Telegram says the thread is gone
    assert scheduled.topic_of(OWNER, TASK) is None
    assert topics.ensure("run-b", "t") != thread


# --- mute: only ordinary sends are skipped --------------------------------------------------------------------

def test_a_muted_task_skips_ordinary_sends(claude, bot, monkeypatch):
    run(claude, "run-m")
    scheduled.set_muted(TASK, True)
    monkeypatch.setattr(mirror, "build", lambda c: pytest.fail("an ordinary reply was sent"))
    assert T.set_state(cfg(), "run-m", "busy", bot=bot) == 0
    assert mirror.run_reply(cfg(), "t", "run-m", "Paused; nothing to do.", False) == 0
    assert mirror.run_file(cfg(), "t", "run-m", "/nope", "") == 0
    assert progress._send_progress(cfg(), "run-m", None, bot, 0, lambda s: None, time.time, True) == 0
    monkeypatch.setattr(mirror.time, "sleep", lambda s: None)
    monkeypatch.setattr(mirror, "screen_error", lambda session: "")
    assert mirror.run_notify(cfg(), "run-m", "idle_prompt", bot=bot) == 0
    assert bot.calls == []


def test_only_the_listed_kinds_are_ever_held_back(claude, monkeypatch):
    run(claude, "run-m")
    scheduled.set_muted(TASK, True)
    monkeypatch.setattr(mirror, "screen_error", lambda session: "")
    assert scheduled.SUPPRESSED == {"progress", "reply", "idle", "status", "typing", "rename", "radio"}
    for kind in scheduled.SUPPRESSED:
        assert scheduled.suppressed("run-m", kind, "all good")
    for kind in ("permission", "question", "error", "limit", "lifecycle", "file-request", ""):
        assert not scheduled.suppressed("run-m", kind)
    for failure in ("You've hit your limit · resets 5pm", "API Error: 401 authentication_error",
                    "Invalid API key · Please run /login", "Credit balance is too low"):
        assert not scheduled.suppressed("run-m", "reply", failure), failure
    monkeypatch.setattr(mirror, "screen_error", lambda session: "Not logged in")
    assert not scheduled.suppressed("run-m", "reply", "done") and not scheduled.suppressed("run-m", "idle")


def test_local_state_is_written_while_the_status_card_is_held_back(claude, bot):
    run(claude, "run-m")
    topics = tmap(bot)
    topics.ensure("run-m", "t")
    scheduled.set_muted(TASK, True)
    calls = len(bot.calls)
    T.set_state(cfg(), "run-m", "busy", bot=bot)
    assert T.session_state("run-m")["state"] == "busy"
    T.set_state(cfg(), "run-m", "done", bot=bot)
    state = T.session_state("run-m")
    assert state["state"] == "done" and state["main_idle"] is True
    assert len(bot.calls) == calls  # not one Telegram call


def test_lifecycle_still_runs_for_a_muted_task(claude, monkeypatch):
    run(claude, "run-m")
    scheduled.set_muted(TASK, True)
    seen = []
    monkeypatch.setattr(mirror, "run_lifecycle", lambda cfg, session, kind, *a, **k: seen.append((session, kind)))
    monkeypatch.setattr(cli.C, "load", lambda: cfg())
    assert cli.main(["lifecycle", "--session=run-m", "--kind=session-end"]) == 0
    assert seen == [("run-m", "session-end")]  # its local state (agents card, session end) is kept


def test_local_state_is_written_for_a_muted_run_without_a_topic(claude, bot):
    run(claude, "run-m")
    scheduled.set_muted(TASK, True)
    T.set_state(cfg(), "run-m", "busy", bot=bot)
    assert T.session_state("run-m")["state"] == "busy" and "main_idle" not in T.session_state("run-m")
    T.set_state(cfg(), "run-m", "waiting", bot=bot)
    assert T.session_state("run-m")["state"] == "waiting"
    T.set_state(cfg(), "run-m", "done", bot=bot)
    state = T.session_state("run-m")
    assert state["state"] == "done" and state["main_idle"] is True
    assert bot.calls == [] and tmap(bot).place_of("run-m") is None  # no topic made, no Telegram call


def sends(bot):
    return [(p.get("message_thread_id"), p.get("text", "")) for p in bot.methods("sendMessage")]


@pytest.fixture
def wired(bot, monkeypatch):
    """The real senders, with the fake bot standing in for Telegram."""
    monkeypatch.setattr(mirror.Bot, "from_config", classmethod(lambda cls, c: bot))
    monkeypatch.setattr(mirror.time, "sleep", lambda s: None)
    monkeypatch.setattr("ctb.dialogs.send_card", lambda *a, **k: False)
    monkeypatch.setattr(mirror, "screen_error", lambda session: "")
    return bot


def test_a_muted_tasks_limit_and_error_replies_are_sent(claude, wired):
    run(claude, "run-m")
    thread = tmap(wired).ensure("run-m", "t")
    scheduled.set_muted(TASK, True)
    for text, fragment in (("You've hit your limit · resets 5pm", "hit your limit"),
                           ("API Error: 401 authentication_error", "authentication_error")):
        before = len(sends(wired))
        mirror.run_reply(cfg(), "t", "run-m", text, False)
        assert any(t == thread and fragment in body for t, body in sends(wired)[before:]), text


def test_a_muted_tasks_permission_request_and_idle_error_are_sent(claude, wired, monkeypatch):
    run(claude, "run-m")
    scheduled.set_muted(TASK, True)
    mirror.run_notify(cfg(), "run-m", "permission_prompt", bot=wired)  # no topic yet: the task topic is made
    assert wired.methods("createForumTopic")[0]["name"] == TASK
    thread = tmap(wired).place_of("run-m").thread
    assert sends(wired)[-1][0] == thread and "🔔" in sends(wired)[-1][1]
    monkeypatch.setattr(mirror, "screen_error", lambda session: "Not logged in")
    mirror.run_notify(cfg(), "run-m", "idle_prompt", bot=wired)  # an idle ring that carries an error
    assert sends(wired)[-1][0] == thread and "Not logged in" in sends(wired)[-1][1]


def test_an_essential_notice_reopens_a_closed_topic_and_retries(claude, wired):
    write(claude, "plain-a", user_row("hello", **HUMAN))  # any session, not only scheduled ones
    thread = tmap(wired).ensure("plain-a", "p")
    wired.fail["sendMessage"] = "Bad Request: TOPIC_CLOSED"
    mirror.alert(cfg(), "plain-a", "Something failed")
    assert wired.methods("reopenForumTopic") == [{"chat_id": OWNER, "message_thread_id": thread}]
    assert sends(wired)[-1][0] == thread and "Something failed" in sends(wired)[-1][1]
    assert len(sends(wired)) == 2  # the refused send and its retry


def test_an_essential_notice_recreates_a_deleted_topic_and_retries(claude, wired):
    run(claude, "run-m")
    old = tmap(wired).ensure("run-m", "t")
    scheduled.set_muted(TASK, True)
    wired.fail["sendMessage"] = "Bad Request: message thread not found"
    mirror.run_notify(cfg(), "run-m", "permission_prompt", bot=wired)
    new = tmap(wired).place_of("run-m").thread
    assert new != old and len(wired.methods("createForumTopic")) == 2
    assert sends(wired)[-1][0] == new and "🔔" in sends(wired)[-1][1]
    assert scheduled.topic_of(OWNER, TASK) == new


def test_unmute_restores_and_hooks_always_run(claude, bot, monkeypatch):
    path = run(claude, "run-m")
    scheduled.set_muted(TASK, True)
    ran = []
    monkeypatch.setattr(hooks, "hook_post", lambda text: ran.append(text) or 0)
    payload = json.dumps({"session_id": "run-m", "transcript_path": path, "hook_event_name": "PostToolUse"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert cli.main(["hook-post"]) == 0
    assert ran == [payload]
    scheduled.set_muted(TASK, False)
    T.set_state(cfg(), "run-m", "busy", bot=bot)
    assert bot.methods("createForumTopic")


def test_mute_preferences_survive_the_temporary_maps(claude):
    scheduled.set_muted(TASK, True)
    for i in range(scheduled.KEEP + 20):
        scheduled.remember_topic(f"t{i}", OWNER, i, f"s{i}")
    assert scheduled.is_muted(TASK)
    assert json.loads((state_dir() / scheduled.MUTED_FILE).read_text()) == {TASK: True}


def test_tasks_cli(capsys):
    assert cli.main(["tasks", "list"]) == 0
    assert "No scheduled task" in capsys.readouterr().out
    assert cli.main(["tasks", "mute", TASK]) == 0 and scheduled.is_muted(TASK)
    cli.main(["tasks", "list"])
    assert f"{TASK}\tmuted" in capsys.readouterr().out
    assert cli.main(["tasks", "unmute", TASK]) == 0 and not scheduled.is_muted(TASK)
    assert cli.main(["tasks", "mute", "bad name"]) == 2
    assert cli.main(["tasks", "mute"]) == 2


# --- the More menu: mute buttons carry their run; close is unchanged ---------------------------------------------

SID_A = "1f0e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
SID_B = "2f0e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"


def press(bot, topics, thread, data):
    q = {"id": "q", "data": data, "from": {"id": OWNER},
         "message": {"message_id": 10, "chat": {"id": OWNER}, "message_thread_id": thread}}
    return I.handle_button(q, cfg(), bot, topics, I.Inbox(state_dir() / "inbox"))


def callbacks(params):
    return [b["callback_data"] for row in params["reply_markup"]["inline_keyboard"] for b in row]


def test_mute_buttons_carry_the_full_session_id_within_the_limit(claude, bot):
    run(claude, SID_A)
    write(claude, "plain-a", user_row("hello", **HUMAN))
    data = [c for c in callbacks({"reply_markup": I.more_markup("en", session=SID_A)}) if c.startswith("more:t")]
    assert data == [f"more:tmute:{SID_A}"] and len(data[0].encode()) <= 64
    scheduled.set_muted(TASK, True)
    assert f"more:tunmute:{SID_A}" in callbacks({"reply_markup": I.more_markup("en", session=SID_A)})
    assert len(f"more:tunmute:{SID_A}".encode()) <= 64
    assert not any(c.startswith("more:t") for c in callbacks({"reply_markup": I.more_markup("en", session="plain-a")}))


def test_an_outdated_mute_button_is_refused_and_changes_nothing(claude, bot):
    run(claude, SID_A)
    run(claude, SID_B)
    topics = tmap(bot)
    thread = topics.ensure(SID_A, "t")
    other = thread + 50
    for data in ("more:tmute:", f"more:tmute:{SID_B}", "more:tmute:nosuchsession"):
        assert press(bot, topics, thread, data) == "ignored", data  # no id, a run without this topic, unknown
    assert press(bot, topics, other, f"more:tmute:{SID_A}") == "ignored"  # sent in another topic
    assert bot.methods("answerCallbackQuery")[-1]["text"] == I.t("toast_button_outdated")
    assert not scheduled.is_muted(TASK)
    assert press(bot, topics, thread, f"more:tmute:{SID_A}") == "menu" and scheduled.is_muted(TASK)
    shown = bot.methods("editMessageText")[-1]
    assert f"more:tunmute:{SID_A}" in callbacks(shown) and "more:dismiss" in callbacks(shown)
    assert press(bot, topics, thread, f"more:tunmute:{SID_A}") == "menu" and not scheduled.is_muted(TASK)


def test_no_close_or_detach_path_for_non_terminal_sessions(claude, bot, monkeypatch):
    monkeypatch.setattr(I, "pane_for_session", lambda session: None)
    run(claude, "run-a")
    topics = tmap(bot)
    thread = topics.ensure("run-a", "t")
    assert press(bot, topics, thread, "close:yes") == "close"
    assert bot.methods("editMessageText")[-1]["text"] == I.t("close_terminal_only")
    assert not bot.methods("closeForumTopic")
    assert sm.closed_marks() == {}
    assert not hasattr(sm, "detach") and not hasattr(scheduled, "detached")


@pytest.fixture
def limit_ready(monkeypatch):
    """limit_offer's account lookups, made local and deterministic."""
    from ctb import accounts, identity
    monkeypatch.setattr(accounts, "note_limit", lambda *a, **k: None)
    monkeypatch.setattr(accounts, "limited_account", lambda session: "main")
    monkeypatch.setattr(identity, "check", lambda *a, **k: None)
    monkeypatch.setattr(accounts, "fallback_plan", lambda session: {"auto": False, "fresh": [], "others": ["spare"],
                                                                     "here": "main"})
    monkeypatch.setattr(mirror.alerts, "alert", lambda *a, **k: None)


def test_limit_offer_of_a_muted_task_is_sent_into_its_topic(claude, wired, limit_ready):
    run(claude, "run-m")
    thread = tmap(wired).ensure("run-m", "t")
    scheduled.set_muted(TASK, True)
    assert mirror.limit_offer(cfg(), wired, "run-m", "You've hit your limit · resets 5pm") is True
    sent = wired.methods("sendMessage")[-1]
    assert (sent["chat_id"], sent["message_thread_id"]) == (OWNER, thread)
    assert "more:/account move spare" in [b["callback_data"] for row in sent["reply_markup"]["inline_keyboard"]
                                           for b in row]


def test_limit_offer_reopens_a_closed_topic_and_retries(claude, wired, limit_ready):
    run(claude, "run-m")
    thread = tmap(wired).ensure("run-m", "t")
    scheduled.set_muted(TASK, True)
    wired.fail["sendMessage"] = "Bad Request: TOPIC_CLOSED"
    assert mirror.limit_offer(cfg(), wired, "run-m", "You've hit your limit · resets 5pm") is True
    assert wired.methods("reopenForumTopic") == [{"chat_id": OWNER, "message_thread_id": thread}]
    assert [p["message_thread_id"] for p in wired.methods("sendMessage")] == [thread, thread]  # refused, then sent
