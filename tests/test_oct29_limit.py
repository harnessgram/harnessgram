import json
from types import SimpleNamespace

from ctb import inbox as I, usage
from test_inbox import TextMedia, cfg, setup, update


def transcript(tmp_path, monkeypatch, rows):
    projects = tmp_path / "projects"
    project = projects / "example"
    project.mkdir(parents=True)
    (project / "sess-1.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(usage, "PROJECTS", projects)


def limit_row():
    return {"type": "assistant", "isApiErrorMessage": True, "apiError": "usage_limit_reached",
            "timestamp": "2026-10-29T10:00:00Z",
            "message": {"content": [{"text": "Usage limit reached, resets 4:30pm"}]}}


def test_limit_notice_preserves_queue_and_throttles_per_session(tmp_path, monkeypatch, bot):
    transcript(tmp_path, monkeypatch, [limit_row()])
    now = [1_793_268_100.0]  # just after the limit row, before its 16:30 reset
    monkeypatch.setattr(I.time, "time", lambda: now[0])
    topics, box, thread = setup(tmp_path, bot)
    bot.calls.clear()
    for text in ("first", "second"):
        assert I.handle(update(text, thread), cfg(), bot, topics, box, TextMedia(), 0) == "queued"
    assert [item["text"] for item in box.take("sess-1")] == ["first", "second"]
    assert [r["reaction"][0]["emoji"] for r in bot.methods("setMessageReaction")] == ["⏳", "⏳"]
    replies = bot.methods("sendMessage")
    assert len(replies) == 1
    assert replies[0]["text"] == "\u200f📥 נשמר. יימסר כשהמכסה תחזור ב-16:30."
    assert replies[0]["reply_to_message_id"] == 5
    now[0] += 600
    assert I.handle(update("third", thread), cfg(), bot, topics, box, TextMedia(), 0) == "queued"
    assert len(bot.methods("sendMessage")) == 2


def test_latest_normal_assistant_row_has_no_notice(tmp_path, monkeypatch, bot):
    transcript(tmp_path, monkeypatch, [limit_row(), {"type": "assistant", "message": {"content": []}}])
    topics, box, thread = setup(tmp_path, bot)
    bot.calls.clear()
    assert I.handle(update("hello", thread), cfg(), bot, topics, box, TextMedia(), 0) == "queued"
    assert box.take("sess-1")[0]["text"] == "hello"
    assert not bot.methods("sendMessage")
    assert bot.methods("setMessageReaction")[-1]["reaction"][0]["emoji"] == "👀"


def test_limit_that_already_reset_gives_no_notice(tmp_path, monkeypatch):
    transcript(tmp_path, monkeypatch, [limit_row()])
    monkeypatch.setattr(usage.time, "time", lambda: 4_000_000_000.0)
    assert usage.limit_of("sess-1") is None


def test_limit_ignored_after_a_restart_on_an_account_with_room(tmp_path, monkeypatch):
    # Regression: a session moved by hand after a failed move kept its messages
    # held until the old account's reset although it ran on an account with room.
    from ctb import accounts
    transcript(tmp_path, monkeypatch, [limit_row()])
    monkeypatch.setattr(usage.time, "time", lambda: 1_793_268_100.0)
    started = {"sessionId": "sess-1", "startedAt": 1_793_268_060_000}
    monkeypatch.setattr(accounts, "live_session_records", lambda: [started])
    monkeypatch.setattr(accounts, "account_for", lambda s=None: "spare")
    monkeypatch.setattr(accounts, "spent", lambda name, now=None: False)
    assert usage.limit_of("sess-1") is None
    monkeypatch.setattr(accounts, "spent", lambda name, now=None: True)   # restarted on a spent account
    assert usage.limit_of("sess-1") is not None
    started["startedAt"] = 1_793_000_000_000                              # started before the limit
    monkeypatch.setattr(accounts, "spent", lambda name, now=None: False)
    assert usage.limit_of("sess-1") is not None


def test_a_manual_account_move_clears_the_limit_hold(tmp_path, monkeypatch, bot):
    """Regression: a limited session moved by hand (account menu); only the automatic move wrote
    the limit-cleared marker, so every later message was still "saved until 21:40"."""
    from ctb import accounts
    transcript(tmp_path, monkeypatch, [limit_row()])
    monkeypatch.setattr(I.time, "time", lambda: 1_793_268_100.0)
    topics, box, thread = setup(tmp_path, bot)
    accounts.add("spare")  # the move refuses a target whose folder is gone (removed meanwhile)
    result = [False]
    monkeypatch.setattr(I, "_account_move", lambda *a, **k: result[0])
    say = lambda text, **extra: None
    assert usage.limit_of("sess-1") is not None
    I.account_command(cfg(), bot, {"message_thread_id": thread}, "sess-1", "move spare", say)
    assert usage.limit_of("sess-1") is not None          # a move that did not happen clears nothing
    result[0] = True
    I.account_command(cfg(), bot, {"message_thread_id": thread}, "sess-1", "move spare", say)
    assert usage.limit_of("sess-1") is None
    bot.calls.clear()
    assert I.handle(update("hello", thread), cfg(), bot, topics, box, TextMedia(), 0) == "queued"
    assert not bot.methods("sendMessage")
    assert bot.methods("setMessageReaction")[-1]["reaction"][0]["emoji"] == "👀"


def test_a_manual_move_of_an_unlimited_session_writes_no_marker(tmp_path, monkeypatch, bot):
    from ctb import accounts
    from ctb.config import state_dir
    transcript(tmp_path, monkeypatch, [{"type": "assistant", "message": {"content": []}}])
    setup(tmp_path, bot)
    monkeypatch.setattr(accounts, "names", lambda: ["main", "spare"])
    monkeypatch.setattr(I, "_account_move", lambda *a, **k: True)
    I.account_command(cfg(), bot, {"message_thread_id": 1}, "sess-1", "move spare", lambda *a, **k: None)
    assert not (state_dir() / "limit-cleared" / "sess-1").exists()


RULE = "─" * 80
IDLE = "\n".join(["⏺ Done.", "", "✻ Cooked for 1m 50s · done 9:54 AM", "", RULE, "❯ ", RULE,
                   "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← 3 agents"])
RUNNING = "\n".join(["⏺ Working on it.", "", "✶ Ebbing… (16s · ↓ 1.1k tokens)", "", RULE, "❯ ", RULE,
                      "  ⏵⏵ bypass permissions on (shift+tab to cycle)"])


def stop_rig(tmp_path, monkeypatch, bot, pane, screens=None):
    """`screens`: what the pane shows, one entry per Escape pressed so far (the last one stays)."""
    topics, box, thread = setup(tmp_path, bot)
    monkeypatch.setattr(I, "pane_for_session", lambda s: pane)
    monkeypatch.setattr(I, "pane_runs_session", lambda p, s: True)
    monkeypatch.setattr(I, "_pane_from_process", lambda s: None)
    keys = []
    monkeypatch.setattr(I, "_send_keys", lambda p, k: keys.append((p, k)) or True)
    monkeypatch.setattr(I, "_terminal_background", lambda s, action: action() or True)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_text", lambda pane, lines=45: "\u276f ")
    shown = screens or [RUNNING, IDLE]
    monkeypatch.setattr(I, "pane_raw", lambda pane, lines=45: shown[min(len(keys), len(shown) - 1)])
    return topics, box, thread, keys


def test_stop_buttons_press_escape_when_a_pane_is_found(tmp_path, monkeypatch, bot):
    """2026-11-01: the Stop button was queued as a word and did nothing while the session waited."""
    from ctb import lang as L
    for key in ("btn_stop", "fixed_btn_stop"):
        for lang in ("en", "he"):
            topics, box, thread, keys = stop_rig(tmp_path, monkeypatch, bot, "%7")
            I.handle(update(L.strings(lang)[key], thread), cfg(), bot, topics, box, TextMedia(), 0)
            assert keys == [("%7", ["esc"])]
            assert box.take("sess-1") == []          # never queued as a message
    assert "נעצר" in bot.methods("sendMessage")[-1]["text"]


def test_stop_without_a_pane_says_so_and_sends_the_stop_word_only_to_a_working_session(tmp_path, monkeypatch, bot):
    """No pane: no key can be pressed. A working session gets the stop word after its current step (the
    PostToolUse hook), with a plain notice and the move button; an idle one gets no "stop" message."""
    from ctb import lang as L
    from ctb.topics import set_state
    topics, box, thread, keys = stop_rig(tmp_path, monkeypatch, bot, None)
    I.handle(update(L.strings("en")["btn_stop"], thread), cfg(), bot, topics, box, TextMedia(), 0)
    assert keys == [] and box.take("sess-1") == []
    assert "לא רצה" in bot.methods("sendMessage")[-1]["text"]
    set_state(cfg(), "sess-1", "busy", bot)
    I.handle(update(L.strings("en")["btn_stop"], thread), cfg(), bot, topics, box, TextMedia(), 0)
    assert keys == [] and [m["text"] for m in box.take("sess-1")] == ["stop"]
    last = bot.methods("sendMessage")[-1]
    assert "מחוץ למסך המנוהל" in last["text"]
    assert last["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "tmux:go"
    # a pane that no longer hosts the session counts as none
    topics, box, thread, keys = stop_rig(tmp_path, monkeypatch, bot, "%7")
    monkeypatch.setattr(I, "pane_runs_session", lambda p, s: False)
    I.handle(update(L.strings("en")["btn_stop"], thread), cfg(), bot, topics, box, TextMedia(), 0)
    assert keys == [] and [m["text"] for m in box.take("sess-1")] == ["stop"]


def test_stop_finds_the_session_pane_again_when_its_saved_id_was_reused(tmp_path, monkeypatch, bot):
    """tmux restarted and gave the saved %7 to another window: the session's own process names %9."""
    from ctb import lang as L
    topics, box, thread, keys = stop_rig(tmp_path, monkeypatch, bot, "%7")
    monkeypatch.setattr(I, "pane_runs_session", lambda p, s: p == "%9")
    monkeypatch.setattr(I, "_pane_from_process", lambda s: "%9")
    I.handle(update(L.strings("en")["btn_stop"], thread), cfg(), bot, topics, box, TextMedia(), 0)
    assert keys == [("%9", ["esc"])] and box.take("sess-1") == []


def test_stop_presses_again_until_the_turn_is_gone_and_closes_a_menu_first(tmp_path, monkeypatch, bot):
    """One Escape only closed an open menu (or the screen had not caught up): press again, bounded."""
    topics, box, thread, keys = stop_rig(tmp_path, monkeypatch, bot, "%7", screens=[RUNNING, RUNNING, IDLE])
    m = {"chat": {"id": 7, "type": "private"}, "message_thread_id": thread, "message_id": 20}
    I.cli_command(cfg(), bot, m, "sess-1", "/esc")
    assert keys == [("%7", ["esc"])] * 2 and "נעצר" in bot.methods("sendMessage")[-1]["text"]


def test_stop_never_confirms_a_turn_that_still_runs(tmp_path, monkeypatch, bot):
    topics, box, thread, keys = stop_rig(tmp_path, monkeypatch, bot, "%7", screens=[RUNNING])
    m = {"chat": {"id": 7, "type": "private"}, "message_thread_id": thread, "message_id": 20}
    I.cli_command(cfg(), bot, m, "sess-1", "/esc")
    assert len(keys) == I.STOP_PRESSES
    last = bot.methods("sendMessage")[-1]
    assert "עדיין עובד" in last["text"] and "Ebbing" in last["text"] and "נעצר" not in last["text"]
    assert last["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "more:/screen"


def test_stop_on_an_idle_prompt_presses_nothing(tmp_path, monkeypatch, bot):
    """A second Escape on an idle prompt opens Claude Code's rewind menu: never press on a quiet screen."""
    topics, box, thread, keys = stop_rig(tmp_path, monkeypatch, bot, "%7", screens=[IDLE])
    m = {"chat": {"id": 7, "type": "private"}, "message_thread_id": thread, "message_id": 20}
    I.cli_command(cfg(), bot, m, "sess-1", "/esc")
    assert keys == [] and "לא רצה" in bot.methods("sendMessage")[-1]["text"]


def test_stop_reports_background_work_that_keeps_running(tmp_path, monkeypatch, bot):
    after = IDLE.replace("← 3 agents", "2 shells · ← 3 agents")
    topics, box, thread, keys = stop_rig(tmp_path, monkeypatch, bot, "%7", screens=[RUNNING, after])
    m = {"chat": {"id": 7, "type": "private"}, "message_thread_id": thread, "message_id": 20}
    I.cli_command(cfg(), bot, m, "sess-1", "/esc")
    text = bot.methods("sendMessage")[-1]["text"]
    assert "נעצר" in text and "ברקע" in text and text.endswith("\n2 shells")


def test_running_rows_of_claude_code_are_recognised():
    assert I.turn_running(RUNNING) == "✶ Ebbing… (16s · ↓ 1.1k tokens)"
    assert I.turn_running(IDLE) == ""
    compacting = RUNNING.replace("✶ Ebbing… (16s · ↓ 1.1k tokens)",
                                 "✻ Compacting conversation… (23s · ↓ 1.2k tokens · esc to interrupt)")
    assert I.turn_running(compacting).startswith("✻ Compacting")
    waiting = RUNNING.replace("✶ Ebbing… (16s · ↓ 1.1k tokens)", "✻ Waiting for 2 background agents to finish")
    assert I.turn_running(waiting) and not I.turn_running(waiting, agents=False)
    old = "\n".join(["⏺ ok", "✻ Working… (12s · esc to interrupt)", RULE, "❯ ", RULE])
    assert I.turn_running(old)
    # a reply that only quotes the words is not a running turn
    quoted = IDLE.replace("⏺ Done.", "⏺ The hint says esc to interrupt (old versions).")
    assert I.turn_running(quoted) == ""


def test_pane_runs_session_rejects_a_reused_pane_id(monkeypatch):
    from types import SimpleNamespace
    out = ["4000 other"]
    monkeypatch.setattr(I, "_tmux", lambda *a: SimpleNamespace(returncode=0, stdout=out[0], stderr=""))
    monkeypatch.setattr(I, "session_pid", lambda s: 4100)
    parents = {"4100": "4000"}
    monkeypatch.setattr(I.subprocess, "run", lambda cmd, **k: SimpleNamespace(stdout=parents.get(cmd[-1], "1"), returncode=0))
    assert I.pane_runs_session("%3", "abcdef123456")          # the shell around Claude owns the pane
    out[0] = "5555 other"                                      # tmux restarted: %3 is another window now
    assert not I.pane_runs_session("%3", "abcdef123456")
    out[0] = "5555 tg-abcdef12"                                # the bridge's own window for this session
    assert I.pane_runs_session("%3", "abcdef123456")
    monkeypatch.setattr(I, "session_pid", lambda s: None)
    out[0] = "5555 other"
    assert not I.pane_runs_session("%3", "abcdef123456")
    assert not I.pane_runs_session("-a", "abcdef123456")


def test_escape_on_an_idle_prompt_marks_done_and_delivers_the_queue(tmp_path, monkeypatch, bot):
    """After Escape the Stop hook may never run: the topic stayed busy and queued messages never arrived."""
    from ctb.topics import set_state, session_state
    topics, box, thread, keys = stop_rig(tmp_path, monkeypatch, bot, "%7")
    set_state(cfg(), "sess-1", "busy", bot)
    assert session_state("sess-1")["state"] == "busy"
    box.put("sess-1", {"text": "next thing"})
    nudged = []
    monkeypatch.setattr(I, "default_inbox", lambda: box)
    monkeypatch.setattr(I, "_reopen_or_nudge", lambda b, s: nudged.append(s))
    monkeypatch.setattr(I.threading, "Thread", lambda target, args=(), daemon=None: SimpleNamespace(
        start=lambda: target(*args)))
    m = {"chat": {"id": 7, "type": "private"}, "message_thread_id": thread, "message_id": 20}
    I.cli_command(cfg(), bot, m, "sess-1", "/esc")
    assert session_state("sess-1")["state"] == "done" and nudged == ["sess-1"]


def test_escape_on_a_still_working_pane_leaves_the_state_alone(tmp_path, monkeypatch, bot):
    from ctb.topics import set_state, session_state
    topics, box, thread, keys = stop_rig(tmp_path, monkeypatch, bot, "%7", screens=[RUNNING])
    set_state(cfg(), "sess-1", "busy", bot)
    m = {"chat": {"id": 7, "type": "private"}, "message_thread_id": thread, "message_id": 20}
    I.cli_command(cfg(), bot, m, "sess-1", "/esc")
    assert session_state("sess-1")["state"] == "busy"


def test_stop_keeps_pressing_when_the_first_escape_only_closed_a_menu_over_the_agents_wait(tmp_path, monkeypatch, bot):
    """Review of #138: after one Escape the background-agents wait was no longer counted, so an Escape
    that only closed a menu over it answered "stopped" while the session still waited."""
    waiting = RUNNING.replace("✶ Ebbing… (16s · ↓ 1.1k tokens)", "✻ Waiting for 2 background agents to finish")
    topics, box, thread, keys = stop_rig(tmp_path, monkeypatch, bot, "%7", screens=[waiting])
    menus = [True]  # a menu is open before the first press; the press closes it
    monkeypatch.setattr(I, "_dialog_open", lambda body, session: menus[0] if not keys else False)
    assert I.stop_turn("%7", "sess-1") == ("stopped", "✻ Waiting for 2 background agents to finish")
    assert keys == [("%7", ["esc"])] * 2  # the menu, then the wait itself; never a third (rewind menu)


def test_stop_checks_the_pane_again_before_every_escape(tmp_path, monkeypatch, bot):
    """Review of #138: the pane was checked once before the background stop, then pressed up to four
    times; a session that ended in between could have its %N id reused by another window."""
    topics, box, thread, keys = stop_rig(tmp_path, monkeypatch, bot, "%7", screens=[RUNNING])
    checks = []
    monkeypatch.setattr(I, "pane_runs_session", lambda p, s: checks.append(p) or len(checks) < 2)
    assert I.stop_turn("%7", "sess-1") == ("unknown", "")
    assert keys == [("%7", ["esc"])] and checks == ["%7", "%7"]  # the second press found another window


def test_stop_presses_nothing_when_the_pane_left_the_session_before_the_first_escape(tmp_path, monkeypatch, bot):
    topics, box, thread, keys = stop_rig(tmp_path, monkeypatch, bot, "%7", screens=[RUNNING])
    monkeypatch.setattr(I, "pane_runs_session", lambda p, s: False)
    assert I.stop_turn("%7", "sess-1") == ("failed", "") and keys == []
