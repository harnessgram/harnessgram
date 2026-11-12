import json

import pytest

from ctb import hooks as H
from ctb import wake as W
from ctb.config import Config
from ctb.inbox import default_inbox


def ev(role, text=None, uuid=None, blocks=None, **extra):
    content = blocks if blocks is not None else [{"type": "text", "text": text}]
    d = {"type": role, "message": {"role": role, "content": content if role == "assistant" or blocks else text}}
    if uuid:
        d["uuid"] = uuid
    d.update(extra)
    return json.dumps(d)


def test_reply_is_text_since_the_last_real_user_prompt():
    lines = [ev("user", "first"), ev("assistant", "old reply", "a1"),
             ev("user", "second"), ev("assistant", "part one", "a2"),
             ev("user", blocks=[{"type": "tool_result", "content": "x"}]),   # tool result: not a prompt
             ev("assistant", "part two", "a3")]
    assert H.last_reply(lines) == "part one\n\npart two"


def test_meta_user_events_do_not_reset():
    lines = [ev("user", "q"), ev("assistant", "answer", "a1"), ev("user", "system noise", isMeta=True)]
    assert H.last_reply(lines) == "answer"


def test_is_not_target():
    assert H.is_not_target("This is a long English reply that clearly goes past forty letters.", "he")
    assert not H.is_not_target("Short.", "he")
    assert not H.is_not_target("זוהי תשובה ארוכה בעברית שעוברת ארבעים אותיות בקלות רבה מאוד. ok", "he")
    assert not H.is_not_target("A long English reply that goes past forty letters, no doubt.", "es")


def stop_data(tmp_path, lines):
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join(lines) + "\n")
    return {"session_id": "sess-1", "transcript_path": str(t), "cwd": "/x/proj"}


def test_stop_translates_summary_mirrors_and_dedupes(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(H, "spawn", lambda args, text=None: spawned.append((args, text)))
    monkeypatch.setattr(H, "translate_text", lambda t, cfg, fast=False: "HE(" + t + ")")
    cfg = Config(bot_token="t", owner_chat_id=1, translate=True)
    lines = [ev("user", "do it"), ev("assistant", "Work done.\nSummary: All good.", "a1")]
    data = stop_data(tmp_path, lines)

    out = H.stop_output(data, lines, cfg)
    assert out["systemMessage"] == "‏HE(Work done.\n\nAll good.)"
    assert spawned[0][0][:1] == ["mirror"] and "--translate" in spawned[0][0]
    assert "Summary: All good." in spawned[0][1]

    # Stop fires again with the same transcript (e.g. after a continue): nothing is repeated.
    spawned.clear()
    assert H.stop_output(data, lines, cfg) == {}
    assert spawned == []


def test_stop_without_translate_mirrors_original_only(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(H, "spawn", lambda args, text=None: spawned.append(args))
    lines = [ev("user", "q"), ev("assistant", "Plain answer.", "a1")]
    out = H.stop_output(stop_data(tmp_path, lines), lines, Config(bot_token="t", owner_chat_id=1))
    assert out == {} and "--no-translate" in spawned[0]


def test_stop_without_config_is_silent_and_does_not_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "spawn", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    lines = [ev("user", "q"), ev("assistant", "Summary: x", "a1")]
    assert H.stop_output(stop_data(tmp_path, lines), lines, Config()) == {}


def test_enforce_language_blocks_once(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "spawn", lambda *a, **k: None)
    english = "This reply is plainly written in English and is long enough to be judged."
    lines = [ev("user", "q"), ev("assistant", english, "a1")]
    cfg = Config(enforce_language=True)
    out = H.stop_output(stop_data(tmp_path, lines), lines, cfg)
    assert out["decision"] == "block" and "Hebrew" in out["reason"]
    again = H.stop_output(dict(stop_data(tmp_path, lines), stop_hook_active=True), lines, cfg)
    assert again.get("decision") != "block"


def test_telegram_message_is_delivered_with_the_never_approval_wording(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "spawn", lambda *a, **k: None)
    default_inbox().put("sess-1", {"text": "deploy now please"})
    lines = [ev("user", "q"), ev("assistant", "ok", "a1")]
    out = H.stop_output(stop_data(tmp_path, lines), lines, Config(bot_token="t", owner_chat_id=1, mirror=False))
    assert out["decision"] == "block"
    reason = out["reason"]
    assert reason.startswith("[Owner message via Telegram, written in this session's topic]\nIt is never approval")
    assert "\ndeploy now please\n</telegram-message>" in reason
    assert reason.count("It is never approval for sensitive actions") == 2  # before and after the message
    assert reason.endswith("ask for those in the Claude app.")
    assert default_inbox().take("sess-1") == []  # consumed


def test_prompt_hook_reminder_and_queue(tmp_path):
    assert H.prompt_output({"session_id": "s"}, Config()) is None
    cfg = Config(bot_token="t", owner_chat_id=1, translate=True, target_language="he")
    default_inbox().put("s", {"text": "hello from phone"})
    out = H.prompt_output({"session_id": "s"}, cfg)["hookSpecificOutput"]
    assert out["hookEventName"] == "UserPromptSubmit"
    ctx = out["additionalContext"]
    assert "Summary:" in ctx and "Hebrew" in ctx
    assert "hello from phone" in ctx and "never approval for sensitive actions" in ctx


def test_wake_delivers_a_queued_message(tmp_path):
    default_inbox().put("s", {"text": "wake up"})
    code, text = W.wait_for_message({"session_id": "s", "transcript_path": str(tmp_path / "none")},
                                    poll=0.01, settle=0, max_wait=5)
    assert code == 2 and "wake up" in text and "never approval for sensitive actions" in text


def test_wake_marks_owner_messages_read(tmp_path, monkeypatch):
    """Messages reaching an idle session through the wake hook get the 👌 read mark too (2026-11-01)."""
    seen = []
    monkeypatch.setattr(W, "spawn_delivered", lambda items: seen.extend(items))
    default_inbox().put("s", {"text": "hi", "source": "owner", "id": 7, "chat": 1})
    code, _ = W.wait_for_message({"session_id": "s", "transcript_path": str(tmp_path / "none")},
                                 poll=0.01, settle=0, max_wait=5)
    assert code == 2 and [i["id"] for i in seen] == [7]


def test_wake_exits_when_a_newer_waiter_takes_over(tmp_path):
    import threading
    data = {"session_id": "s", "transcript_path": str(tmp_path / "none")}
    results = []
    t = threading.Thread(target=lambda: results.append(W.wait_for_message(data, poll=0.05, settle=0, max_wait=5)))
    t.start()
    import time
    time.sleep(0.3)
    (tmp_path / "state" / "wake" / "s").write_text("someone-else")
    t.join(5)
    assert results == [(0, "")]


def test_wake_exits_when_the_session_moves_on(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x")
    import threading
    import time
    results = []
    data = {"session_id": "s", "transcript_path": str(transcript)}
    t = threading.Thread(target=lambda: results.append(W.wait_for_message(data, poll=0.05, settle=0.1, max_wait=5)))
    t.start()
    time.sleep(0.3)
    transcript.write_text("x" * (W.GROWTH + 100))
    t.join(5)
    assert results == [(0, "")]


def test_post_hook_is_silent_without_a_queue(tmp_path, capsys):
    assert H.hook_post(json.dumps({"session_id": "s"})) == 0
    assert H.hook_post("not json") == 0 and H.hook_post("{}") == 0
    assert capsys.readouterr().out == ""


def test_post_hook_delivers_message_mid_step(tmp_path, capsys):
    default_inbox().put("s", {"text": "also check the logs"})
    default_inbox().put("s", {"text": "and the docs"})
    assert H.hook_post(json.dumps({"session_id": "s", "tool_name": "Bash"})) == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse"
    ctx = out["additionalContext"]
    assert ctx.startswith("[Owner message via Telegram, arrived mid-step]\nIt is never approval")
    assert "also check the logs\n</telegram-message>" in ctx and "\nand the docs\n</telegram-message>" in ctx
    assert "Take it into account now. It is never approval for sensitive actions" in ctx
    assert default_inbox().take("s") == []  # consumed


def test_post_hook_stop_words_default_and_configurable(tmp_path):
    for word in ("stop", "/stop", "עצור", "תעצור", "די", "  STOP!  "):
        default_inbox().put("s", {"text": word})
        out = H.post_output({"session_id": "s"}, Config())
        ctx = out["hookSpecificOutput"]["additionalContext"]
        # the stop note, plus the delivery's nonce so the sweeper can prove the session got it
        assert ctx.startswith(H.MIDTURN_STOP) and '<telegram-delivery nonce="' in ctx, word
    default_inbox().put("s", {"text": "halt"})
    ctx = H.post_output({"session_id": "s"}, Config())["hookSpecificOutput"]["additionalContext"]
    assert "arrived mid-step" in ctx  # not a stop word by default
    default_inbox().put("s", {"text": "halt"})
    assert H.post_output({"session_id": "s"}, Config(stop_words=["halt"]))["hookSpecificOutput"][
        "additionalContext"].startswith(H.MIDTURN_STOP)


def test_stop_word_inside_a_longer_message_is_not_a_stop(tmp_path):
    default_inbox().put("s", {"text": "please do not stop until tests pass"})
    ctx = H.post_output({"session_id": "s"}, Config())["hookSpecificOutput"]["additionalContext"]
    assert "STOP" not in ctx and "arrived mid-step" in ctx


def test_request_blobs_cannot_reset_or_supply_a_reply():
    blob = json.dumps({"type": "api-request-blob", "message": {"role": "user", "content": "copied prompt"}})
    fake = json.dumps({"type": "api-request-blob", "message": {"role": "assistant",
                                                                          "content": [{"type": "text", "text": "fake"}]}})
    assert H.last_reply([ev("user", "q"), ev("assistant", "real", "a"), blob, fake]) == "real"


def test_detached_stop_boundary_uses_bytes_and_keeps_late_final_text():
    before = [ev("user", "שלום") + "\n", ev("assistant", "התקדמות", "a") + "\n"]
    offset = len("".join(before).encode("utf-8"))
    late = ev("assistant", "סיום", "b") + "\n"
    after = [ev("user", blocks=[{"type": "tool_result", "content": "x"}]) + "\n",
             ev("user", "next") + "\n", ev("assistant", "next reply", "c") + "\n"]
    kept = H.this_turn_only(before + [late] + after, offset)
    assert H.last_reply(kept) == "התקדמות\n\nסיום"
    assert kept == before + [late, after[0]]
    summary = json.dumps({"type": "system", "subtype": "turn_duration"}) + "\n"
    assert H.this_turn_only(before + [late, summary] + after, offset) == before + [late]


def test_stop_launches_private_worker_without_waiting_or_translating(tmp_path, monkeypatch, capsys):
    cfg = Config(bot_token="t", owner_chat_id=1, translate=True)
    monkeypatch.setattr(H, "load_config", lambda: cfg)
    monkeypatch.setattr(H, "read_final_transcript", lambda *a, **k: (_ for _ in ()).throw(AssertionError("waited")))
    monkeypatch.setattr(H, "translate_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError("translated")))
    spawned = []
    monkeypatch.setattr(H, "spawn", lambda args, text=None: spawned.append((args, text)))
    data = stop_data(tmp_path, [ev("user", "q"), ev("assistant", "answer", "a")])
    default_inbox().put("sess-1", {"text": "next instruction"})
    assert H.hook_stop(json.dumps(data)) == 0
    args, payload = spawned[0]
    assert args == ["finish-stop"]
    assert json.loads(payload)["_offset"] == (tmp_path / "t.jsonl").stat().st_size
    result = json.loads(capsys.readouterr().out)
    assert result["decision"] == "block" and "never approval" in result["reason"]


def test_finish_stop_alerts_delivery_failure_without_marking_seen(tmp_path, monkeypatch):
    from ctb import mirror, progress, topics
    cfg = Config(bot_token="t", owner_chat_id=1)
    monkeypatch.setattr(H, "load_config", lambda: cfg)
    monkeypatch.setattr(progress, "run_send_progress", lambda *a, **k: 0)
    monkeypatch.setattr(topics, "set_state", lambda *a, **k: 0)
    monkeypatch.setattr(H, "drop_progress", lambda *a, **k: None)
    monkeypatch.setattr(mirror, "run_reply", lambda *a, **k: 1)
    alerts = []
    monkeypatch.setattr(mirror, "alert", lambda *args: alerts.append(args))
    data = stop_data(tmp_path, [ev("user", "q"), ev("assistant", "answer", "a")])
    assert H.finish_stop(data) == 0
    assert alerts and "delivery failed" in alerts[0][2]
    assert H._seen("sess-1") == set()


def test_finish_stop_keeps_original_turn_and_reply_routing(tmp_path, monkeypatch):
    from ctb import mirror, progress, topics
    cfg = Config(bot_token="t", owner_chat_id=1)
    monkeypatch.setattr(H, "load_config", lambda: cfg)
    monkeypatch.setattr(progress, "run_send_progress", lambda *a, **k: 0)
    states = []
    monkeypatch.setattr(topics, "set_state", lambda *a, **k: states.append(a))
    dropped = []
    monkeypatch.setattr(H, "drop_progress", lambda *a, **k: dropped.append(a))
    sent = []
    monkeypatch.setattr(mirror, "run_reply", lambda *a, **k: sent.append((a, k)) or 0)
    data = stop_data(tmp_path, [ev("user", "q"), ev("assistant", "first", "a")])
    data["_offset"] = (tmp_path / "t.jsonl").stat().st_size
    data["_answer_to"] = {"chat": 1, "id": 22}
    with (tmp_path / "t.jsonl").open("a") as f:
        f.write(ev("user", "next") + "\n" + ev("assistant", "next answer", "b") + "\n")
    assert H.finish_stop(data) == 0
    assert sent[0][0][3] == "first" and sent[0][1]["answer_to"] == {"chat": 1, "id": 22}
    assert not states and not dropped
    assert H.finish_stop(data) == 0 and len(sent) == 1  # dedupe a repeated Stop


@pytest.mark.parametrize("prompt,attachment,quiet,expected", [
    ("<task-notification>monitor event</task-notification>", None, True, False),
    ("<task-notification>background command completed</task-notification>", None, True, False),
    # Stop-hook feedback continues the turn it blocked (here the owner's): it never makes a turn quiet.
    ("<task-notification>Stop hook feedback</task-notification>", None, True, True),
    ("<task-notification>event</task-notification>", None, False, True),
    ("a message in the app", None, True, True),
    ("📩 Telegram", None, True, True),
    ("<task-notification>feedback</task-notification>\n" + H.STOP_HEADER + "\nhello", None, True, True),
    ("<task-notification>event</task-notification>", H.MIDTURN_HEADER + "\nhello", True, True),
])
def test_finish_stop_quiets_only_background_turns(tmp_path, monkeypatch, prompt, attachment, quiet, expected):
    from ctb import mirror, progress, topics
    cfg = Config(bot_token="t", owner_chat_id=1, quiet_background_turns=quiet)
    monkeypatch.setattr(H, "load_config", lambda: cfg)
    monkeypatch.setattr(progress, "run_send_progress", lambda *a, **k: 0)
    monkeypatch.setattr(topics, "set_state", lambda *a, **k: 0)
    monkeypatch.setattr(H, "drop_progress", lambda *a, **k: None)
    monkeypatch.setattr(mirror, "alert", lambda *a, **k: pytest.fail("unexpected alert"))
    sent = []
    monkeypatch.setattr(mirror, "run_reply", lambda *a, **k: sent.append(a[3]) or 0)
    lines = [ev("user", "previous"), ev("assistant", "old", "old"), ev("user", prompt)]
    if attachment:
        lines.append(json.dumps({"type": "attachment", "attachment": {"type": "queued_command", "prompt": attachment}}))
    lines.append(ev("assistant", "answer", "new"))
    data = stop_data(tmp_path, lines)
    assert H.finish_stop(data) == 0
    assert bool(sent) is expected
    assert "new:0" in H._seen("sess-1")
    assert H.finish_stop(data) == 0 and len(sent) == int(expected)


def test_quiet_background_default_for_existing_and_new_config():
    from ctb.config import from_dict
    assert Config().quiet_background_turns
    assert from_dict({}).quiet_background_turns
    assert not from_dict({"quiet_background_turns": False}).quiet_background_turns


def test_background_classification_keeps_owner_messages_in_same_turn():
    notice = ev("user", "<task-notification>event</task-notification>")
    assert not H.background_only_turn([ev("user", "owner message"), notice])
    assert not H.background_only_turn([notice, ev("user", H.MIDTURN_HEADER + "\nhello", isMeta=True)])
    assert H.background_only_turn([ev("user", "earlier"), ev("assistant", "answer"), notice])
    assert not H.background_only_turn([ev("assistant", "answer")])


def test_dialog_cards_bypass_generic_notification_throttle(tmp_path, monkeypatch):
    from ctb import inbox
    monkeypatch.setattr(H, "load_config", lambda: Config(bot_token="t", owner_chat_id=1))
    monkeypatch.setattr(inbox, "pane_for_session", lambda s: "%1")
    spawned = []
    monkeypatch.setattr(H, "spawn", lambda args, text=None: spawned.append(args))
    data = json.dumps({"session_id": "s", "notification_type": "permission_prompt"})
    H.hook_notify(data)
    H.hook_notify(data)
    assert spawned == [["notify", "--session=s", "--type=permission_prompt"]] * 2


def test_notification_card_uses_the_owner_topic_and_safe_markup(monkeypatch, bot):
    from ctb import inbox, mirror, topics
    from ctb.config import state_dir
    cfg = Config(bot_token="t", owner_chat_id=1, target_language="he")
    topics.TopicMap(state_dir() / "topics.json", bot, 1).ensure("s", "Title")
    monkeypatch.setattr(mirror.time, "sleep", lambda _: None)
    from ctb import dialogs
    body = "Question <private>\n❯ 1. Yes\n  2. No\n\nEnter to select · Esc to cancel"
    monkeypatch.setattr(dialogs, "open_screen", lambda s: ("%1", body, dialogs.parse(body), True))
    assert mirror.run_notify(cfg, "s", "elicitation_dialog", bot=bot) == 0
    sent = bot.methods("sendMessage")[-1]
    assert sent["chat_id"] == 1 and sent["message_thread_id"] == 101
    assert "&lt;private&gt;" in sent["text"] and "כתוב כאן" in sent["text"]
    assert [b["callback_data"].split(":")[1] for row in sent["reply_markup"]["inline_keyboard"] for b in row] == ["e", "s"]
    assert "disable_notification" not in sent or sent["disable_notification"] is False


def test_prompt_starts_working_state_in_a_detached_helper(monkeypatch):
    monkeypatch.setattr(H, "load_config", lambda: Config(bot_token="t", owner_chat_id=1))
    spawned = []
    monkeypatch.setattr(H, "spawn", lambda args, text=None: spawned.append(args))
    assert H.hook_prompt(json.dumps({"session_id": "s"})) == 0
    assert spawned == [["session-state", "--session=s", "--state=busy"]]


def test_topic_check_hint_languages_and_counter(tmp_path):
    from ctb.config import state_dir, write_private
    from ctb.topics import TopicMap

    # Save a known topic name for session s1
    from conftest import FakeBot
    tmap = TopicMap(state_dir() / "topics.json", FakeBot(), 1)
    tmap.ensure("s1", "demo: task")

    count_file = state_dir() / "topic-check" / "s1"

    # Counter 7 -> 8: fires with Hebrew
    write_private(count_file, "7")
    hint_he = H.topic_check_hint("s1", Config(target_language="he", auto_topic_names=False))
    assert hint_he is not None
    assert 'rename it to a short name in Hebrew "project: task" with ctb topic-rename "<name>"' in hint_he
    assert "Hebrew only, no English words" not in hint_he

    # Counter 15 -> 16: fires with English (say "a short name", not "in English")
    write_private(count_file, "15")
    hint_en = H.topic_check_hint("s1", Config(target_language="en", auto_topic_names=False))
    assert hint_en is not None
    assert 'rename it to a short name "project: task" with ctb topic-rename "<name>"' in hint_en
    assert "in English" not in hint_en

    # Counter 23 -> 24: fires with unset language (say "a short name")
    write_private(count_file, "23")
    hint_unset = H.topic_check_hint("s1", Config(target_language="", auto_topic_names=False))
    assert hint_unset is not None
    assert 'rename it to a short name "project: task" with ctb topic-rename "<name>"' in hint_unset

    # Counter 31 -> 32: fires with Spanish
    write_private(count_file, "31")
    hint_es = H.topic_check_hint("s1", Config(target_language="es", auto_topic_names=False))
    assert hint_es is not None
    assert 'rename it to a short name in Spanish "project: task" with ctb topic-rename "<name>"' in hint_es

    # Counter 32 -> 33: does not fire (returns None)
    hint_none = H.topic_check_hint("s1", Config(target_language="he", auto_topic_names=False))
    assert hint_none is None


def test_post_hook_keeps_messages_from_background_agents(tmp_path, capsys):
    """Regression: an owner message was delivered to a writer agent's tool call and never reached the
    main conversation."""
    default_inbox().put("s", {"text": "pin these too"})
    H.hook_post(json.dumps({"session_id": "s", "tool_name": "Bash", "agent_id": "a1", "agent_type": "writer"}))
    assert "pin these too" not in capsys.readouterr().out
    assert default_inbox().pending("s")
    H.hook_post(json.dumps({"session_id": "s", "tool_name": "Bash"}))
    assert "pin these too" in capsys.readouterr().out


def test_a_lone_owner_stop_word_still_reaches_a_background_agent(tmp_path, capsys):
    """A long foreground Agent call must be haltable: only the stop word is delivered to the agent's
    tool call; normal messages queued beside it stay for the main conversation."""
    default_inbox().put("s", {"text": "also pin these", "source": "owner", "id": 1, "chat": 7})
    default_inbox().put("s", {"text": "stop", "source": "owner", "id": 2, "chat": 7})
    default_inbox().put("s", {"text": "stop", "source": "team"})        # a team member's stop is not one
    agent = json.dumps({"session_id": "s", "tool_name": "Bash", "agent_id": "a1"})
    H.hook_post(agent)
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "STOP" in out and "also pin these" not in out
    left = default_inbox().take("s")
    assert [m["text"] for m in left] == ["also pin these", "stop"] and left[1]["source"] == "team"


def test_a_background_agent_without_a_stop_word_gets_nothing(tmp_path, capsys):
    default_inbox().put("s", {"text": "also pin these", "source": "owner"})
    H.hook_post(json.dumps({"session_id": "s", "tool_name": "Bash", "agent_id": "a1"}))
    assert capsys.readouterr().out == ""
    assert default_inbox().pending("s")


def _stop_env(monkeypatch, tmp_path):
    from ctb import mirror, progress, topics
    cfg = Config(bot_token="t", owner_chat_id=1)
    monkeypatch.setattr(H, "load_config", lambda: cfg)
    monkeypatch.setattr(progress, "run_send_progress", lambda *a, **k: 0)
    monkeypatch.setattr(topics, "set_state", lambda *a, **k: 0)
    monkeypatch.setattr(H, "drop_progress", lambda *a, **k: None)
    monkeypatch.setattr(H, "RETRY_DELAY", 0)
    alerts, logs, spawned = [], [], []
    monkeypatch.setattr(mirror, "alert", lambda *a: alerts.append(a))
    monkeypatch.setattr(mirror, "log", lambda line: logs.append(line))
    monkeypatch.setattr(H, "spawn", lambda args, text=None: spawned.append((args, text)))
    return mirror, alerts, logs, spawned


def test_midinstall_import_error_retries_once_without_notice_and_logs_type(tmp_path, monkeypatch):
    mirror, alerts, logs, spawned = _stop_env(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise ModuleNotFoundError("No module named 'ctb.compact' bot123456:ABCDEFGHIJKLMNOPQRSTUV")
    monkeypatch.setattr(H, "mirror_files", boom)  # fails before any send
    data = stop_data(tmp_path, [ev("user", "q"), ev("assistant", "secret answer", "a")])
    assert H.finish_stop(data) == 0
    assert not alerts
    assert spawned and spawned[0][0] == ["finish-stop"] and json.loads(spawned[0][1])["_attempt"] == 1
    line = next(l for l in logs if "stop delivery failed" in l)
    assert "ModuleNotFoundError" in line and "secret answer" not in line and "ABCDEFGHIJKL" not in line


def test_retry_that_fails_again_shows_the_notice(tmp_path, monkeypatch):
    mirror, alerts, logs, spawned = _stop_env(monkeypatch, tmp_path)
    monkeypatch.setattr(H, "mirror_files", lambda *a, **k: (_ for _ in ()).throw(ImportError("x")))
    data = stop_data(tmp_path, [ev("user", "q"), ev("assistant", "answer", "a")])
    data["_attempt"] = 1
    assert H.finish_stop(data) == 1
    assert alerts and not spawned


def test_no_notice_when_reply_was_already_delivered(tmp_path, monkeypatch):
    mirror, alerts, logs, spawned = _stop_env(monkeypatch, tmp_path)
    data = stop_data(tmp_path, [ev("user", "q"), ev("assistant", "answer", "a")])

    def delivered_then_fail(*a, **k):
        H._mark("sess-1", [k_ for k_, _ in H.read_blocks(H.read_final_transcript(data, wait=0.0))])
        raise ImportError("half installed")
    monkeypatch.setattr(mirror, "run_reply", delivered_then_fail)
    assert H.finish_stop(data) == 0
    assert not alerts and not spawned


def test_non_install_error_is_not_retried_but_notified(tmp_path, monkeypatch):
    mirror, alerts, logs, spawned = _stop_env(monkeypatch, tmp_path)
    monkeypatch.setattr(mirror, "run_reply", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
    data = stop_data(tmp_path, [ev("user", "q"), ev("assistant", "answer", "a")])
    assert H.finish_stop(data) == 1
    assert alerts and not spawned


def test_wait_idle_waits_for_a_running_stop_worker(monkeypatch):
    import fcntl
    assert H.wait_idle(0.5) is True
    with H._worker_gate():
        assert H.wait_idle(0.3, poll=0.05) is False
    assert H.wait_idle(0.5) is True


def test_no_retry_and_single_send_when_error_follows_a_send(tmp_path, monkeypatch):
    mirror, alerts, logs, spawned = _stop_env(monkeypatch, tmp_path)
    sends = []

    def send_then_fail(*a, **k):
        sends.append(1)
        raise AttributeError("'str' object has no attribute 'get'")
    monkeypatch.setattr(mirror, "run_reply", send_then_fail)
    data = stop_data(tmp_path, [ev("user", "q"), ev("assistant", "answer", "a")])
    H.finish_stop(data)
    assert len(sends) == 1 and not spawned and not alerts


def test_already_delivered_still_settles_topic(tmp_path, monkeypatch):
    from ctb import topics
    mirror, alerts, logs, spawned = _stop_env(monkeypatch, tmp_path)
    states = []
    monkeypatch.setattr(topics, "set_state", lambda cfg, s, st: states.append(st))
    data = stop_data(tmp_path, [ev("user", "q"), ev("assistant", "answer", "a")])
    data["_offset"] = (tmp_path / "t.jsonl").stat().st_size

    def delivered_then_fail(*a, **k):
        H._mark("sess-1", [k_ for k_, _ in H.read_blocks(H.read_final_transcript(data, wait=0.0))])
        raise ImportError("x")
    monkeypatch.setattr(mirror, "run_reply", delivered_then_fail)
    assert H.finish_stop(data) == 0
    assert states == ["done"]


def test_bad_install_wait_env_falls_back(tmp_path, monkeypatch):
    from ctb import install as I
    monkeypatch.setenv("CTB_INSTALL_WAIT", "abc")
    seen = []
    monkeypatch.setattr(H, "wait_idle", lambda t: seen.append(t) or True)
    try:
        I.install(dry_run=False, service=False, wake=False, start=False, schedules=False,
                  settings_file=tmp_path / "s.json")
    except Exception:
        pass
    assert seen == [30.0]


def test_owner_text_in_any_hook_shape_keeps_the_turn_mirrored():
    """Review of #256: owner text reaches the transcript as hook context, a blocking error or a queued prompt."""
    notice = ev("user", "<task-notification>event</task-notification>")
    answer = ev("assistant", "answer")
    for row in (
        {"type": "attachment", "attachment": {"type": "hook_additional_context",
                                              "content": ["[Owner also wrote in this session's Telegram topic (chat message):"]}},
        {"type": "attachment", "attachment": {"type": "hook_blocking_error", "blockingError": H.STOP_HEADER + " hi"}},
        {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "a typed message"}},
        {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "\U0001F4E9 Telegram"}},
    ):
        assert not H.background_only_turn([notice, json.dumps(row), answer])
    assert H.background_only_turn([notice, answer])
    assert not H.background_only_turn([notice, answer], answer_to={"message_id": 5})


def test_stop_feedback_after_an_owner_answer_stays_an_owner_turn():
    owner, answer = ev("user", "a question"), ev("assistant", "answer")
    feedback = ev("user", "<task-notification><summary>Stop hook feedback</summary></task-notification>")
    assert not H.background_only_turn([owner, answer, feedback, ev("assistant", "more")])


def test_stop_feedback_in_a_background_turn_stays_quiet_and_old_prompt_records_do_not_count():
    notice, answer = ev("user", "<task-notification>event</task-notification>"), ev("assistant", "answer")
    feedback = ev("user", "<task-notification><summary>Stop hook feedback</summary></task-notification>")
    assert H.background_only_turn([notice, answer, feedback, ev("assistant", "more")])
    record = json.dumps({"type": "last-prompt", "lastPrompt": "\U0001F4E9 Telegram"})
    assert H.background_only_turn([notice, record, answer])


def test_a_background_turn_that_hit_the_plan_limit_is_still_mirrored(tmp_path, monkeypatch):
    from ctb import mirror, progress, topics
    cfg = Config(bot_token="t", owner_chat_id=1)
    monkeypatch.setattr(H, "load_config", lambda: cfg)
    monkeypatch.setattr(progress, "run_send_progress", lambda *a, **k: 0)
    monkeypatch.setattr(topics, "set_state", lambda *a, **k: 0)
    monkeypatch.setattr(H, "drop_progress", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(mirror, "run_reply", lambda *a, **k: sent.append(a[3]) or 0)
    lines = [ev("user", "<task-notification>event</task-notification>"),
             ev("assistant", "You've hit your limit \u00b7 resets 5pm", "new")]
    assert H.finish_stop(stop_data(tmp_path, lines)) == 0
    assert sent
