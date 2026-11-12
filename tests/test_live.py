"""Live progress, notifications, delivery receipts, dispatcher, richer footer, videos, /mode buttons."""
import json
import os
import subprocess
import time

from conftest import GOOD_TOKEN, FakeBot
from ctb import dispatcher, hooks as H, inbox as I, install as INST, mirror as M, progress as P
from ctb import status, usage
from ctb.config import Config, state_dir
from ctb.topics import TopicMap

OWNER = 7


def cfg(**kw):
    base = dict(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="en")
    base.update(kw)
    return Config(**base)


# --- 1. live progress -------------------------------------------------------------------------

def test_labels_are_short_and_content_free():
    secret = "gh" + "p_" + "k" * 30
    assert P.label({"tool_name": "Bash", "tool_input": {"command": f"curl {secret}", "description": "Run tests"}}
                   ) == "⚙️ Run tests"
    assert P.label({"tool_name": "Read", "tool_input": {"file_path": "/a/b/secret-plan.md"}}
                   ) == "\U0001F4D6 secret-plan.md"
    assert "ghp_" not in P.label({"tool_name": "Bash", "tool_input": {"description": f"use {secret}"}})
    assert P.label({"tool_name": "mcp__x__do_thing", "tool_input": {}}).endswith("do thing")
    assert len(P.label({"tool_name": "Bash", "tool_input": {"description": "x" * 500}})) <= 90


def test_append_writes_private_list_and_spawns_at_most_every_3_seconds(monkeypatch):
    spawned = []
    monkeypatch.setattr(H, "spawn", lambda args, text=None: spawned.append(args))
    c = cfg()
    for _ in range(3):
        P.append_progress("s", {"tool_name": "Bash", "tool_input": {"description": "step"}}, c)
    path = state_dir() / "progress" / "s.jsonl"
    assert len(path.read_text().splitlines()) == 3 and oct(path.stat().st_mode & 0o777) == "0o600"
    assert spawned == [["progress", "--session=s"]]
    P.append_progress("s2", {"tool_name": "Bash"}, cfg(progress=False))
    assert not (state_dir() / "progress" / "s2.jsonl").exists()
    P.append_progress("s3", {"tool_name": "Bash"}, Config())
    assert not (state_dir() / "progress" / "s3.jsonl").exists()


def progress_rig(n):
    bot = FakeBot()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    path = state_dir() / "progress"
    path.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (path / "s.jsonl").write_text("".join(json.dumps({"t": now - 125 + i, "l": f"step {i}"}) + "\n"
                                          for i in range(n)))
    return bot


def test_one_message_is_sent_then_edited_with_the_last_8_lines():
    bot = progress_rig(12)
    c = cfg()
    assert P.run_send_progress(c, "s", bot=bot, throttle=0) == 0
    sent = bot.methods("sendMessage")[0]
    assert sent["disable_notification"] is True and sent["message_thread_id"] == 101
    assert "12 actions" in sent["text"] and "2 min" in sent["text"]
    assert "step 11" in sent["text"] and "step 4" in sent["text"] and "step 3" not in sent["text"]
    with open(state_dir() / "progress" / "s.jsonl", "a") as f:
        f.write(json.dumps({"t": time.time() + 5, "l": "step 12"}) + "\n")
    bot2_calls = len(bot.calls)
    P.run_send_progress(c, "s", bot=bot, throttle=0)
    assert len(bot.methods("sendMessage")) == 1 and len(bot.calls) == bot2_calls + 1  # edited, not re-sent
    assert "13 actions" in bot.methods("editMessageText")[0]["text"]
    bot.calls.clear()
    P.run_send_progress(c, "s", bot=bot, throttle=0)  # nothing new
    assert bot.calls == []


def test_progress_edits_are_throttled(monkeypatch):
    bot = progress_rig(3)
    slept = []
    P.run_send_progress(cfg(), "s", bot=bot, throttle=3, sleep_fn=slept.append)
    with open(state_dir() / "progress" / "s.jsonl", "a") as f:
        f.write(json.dumps({"t": time.time() + 9, "l": "more"}) + "\n")
    P.run_send_progress(cfg(), "s", bot=bot, throttle=3, sleep_fn=slept.append)
    assert slept and 0 < slept[0] <= 3


def test_the_stop_hook_deletes_the_progress_message():
    bot = progress_rig(2)
    c = cfg()
    P.run_send_progress(c, "s", bot=bot, throttle=0)
    assert P.run_end_progress(c, "s", bot=bot) == 0
    sent_id = [i for i, (m, _) in enumerate(bot.calls, 1) if m == "sendMessage"][0]
    assert bot.methods("deleteMessage") == [{"chat_id": OWNER, "message_id": sent_id}]
    assert not (state_dir() / "progress" / "s.jsonl").exists()
    assert not (state_dir() / "progress" / "s.msg").exists()


def test_stop_output_drops_progress_in_a_detached_child(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(H, "spawn", lambda args, text=None: spawned.append(args))
    progress_rig(2)
    t = tmp_path / "t.jsonl"
    t.write_text("")
    H.stop_output({"session_id": "s", "transcript_path": str(t)}, [], cfg())
    assert ["progress", "--session=s", "--end"] in spawned
    spawned.clear()
    H.stop_output({"session_id": "s", "transcript_path": str(t)}, [], cfg(progress=False))
    assert spawned == []


def test_post_hook_appends_a_label(monkeypatch):
    monkeypatch.setattr(H, "spawn", lambda *a, **k: None)
    C = cfg()
    monkeypatch.setattr(H, "load_config", lambda: C)
    H.hook_post(json.dumps({"session_id": "s", "tool_name": "Edit", "tool_input": {"file_path": "/x/y.py"}}))
    assert "y.py" in (state_dir() / "progress" / "s.jsonl").read_text()


# --- 2. notifications ---------------------------------------------------------------------------

def test_notify_hook_rings_once_per_ten_minutes(monkeypatch):
    spawned = []
    monkeypatch.setattr(H, "spawn", lambda args, text=None: spawned.append(args))
    monkeypatch.setattr(H, "load_config", lambda: cfg())
    data = json.dumps({"session_id": "s", "notification_type": "permission_prompt"})
    assert H.hook_notify(data) == 0 and H.hook_notify(data) == 0
    assert spawned == [["notify", "--session=s", "--type=permission_prompt"]]
    mark = state_dir() / "notify" / "s"
    os.utime(mark, (time.time() - 700, time.time() - 700))
    H.hook_notify(data)
    assert len(spawned) == 2
    H.hook_notify(json.dumps({"session_id": "other"}))
    assert spawned[-1] == ["notify", "--session=other", "--type="]
    monkeypatch.setattr(H, "load_config", lambda: Config())
    H.hook_notify(json.dumps({"session_id": "third"}))
    assert len(spawned) == 3


def test_notify_message_rings_and_depends_on_the_type():
    bot = FakeBot()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    assert M.run_notify(cfg(), "s", "permission_prompt", bot=bot) == 0
    assert M.run_notify(cfg(), "s", "mystery", bot=bot) == 0
    a, b = bot.methods("sendMessage")
    assert "disable_notification" not in a and a["message_thread_id"] == 101
    assert "approval" in a["text"] and "Needs you" in b["text"]
    bot.calls.clear()
    assert M.run_notify(cfg(), "no-topic", "idle_prompt", bot=bot) == 0 and bot.calls == []


def test_notification_hook_is_installed_and_removed():
    new, _ = INST.merge_settings({})
    n = new["hooks"]["Notification"][0]["hooks"][0]
    assert n["command"].endswith("hook-notify") and n["timeout"] == 15
    assert INST.is_hook_command(n["command"], "hook-notify")
    assert "Notification" not in INST.remove_settings(new)[0].get("hooks", {})


# --- 3. delivery receipt ----------------------------------------------------------------------------

def test_receipt_only_for_the_owners_messages(monkeypatch):
    seen = []
    monkeypatch.setattr(I.subprocess, "Popen", lambda cmd, **kw: seen.append(cmd))
    I.spawn_delivered([{"id": 5, "chat": OWNER, "source": "owner"}, {"id": 6, "chat": -100, "source": "team"},
                       {"id": 7, "chat": -100, "source": "owner"}, {"text": "no id"}])
    assert len(seen) == 1 and seen[0][-2:] == ["delivered", f"--refs={OWNER}:5,-100:7"]
    seen.clear()
    I.spawn_delivered([{"id": 6, "source": "team"}])
    assert seen == []


def test_hooks_trigger_the_receipt_after_taking(monkeypatch):
    got = []
    monkeypatch.setattr(H, "spawn_delivered", lambda items: got.append(items))
    I.default_inbox().put("s", {"text": "hi", "id": 1, "chat": OWNER, "source": "owner"})
    assert H.telegram_messages("s")[0]["text"] == "hi" and got and got[0][0]["id"] == 1


def test_run_delivered_reacts_with_ok_hand():
    bot = FakeBot()
    assert I.run_delivered(cfg(), [f"{OWNER}:5", "-100:7"], bot=bot) == 0
    assert [(p["chat_id"], p["message_id"], p["reaction"][0]["emoji"]) for p in bot.methods("setMessageReaction")
            ] == [(OWNER, 5, "\U0001F44C"), (-100, 7, "\U0001F44C")]


def test_taking_in_tests_never_spawns_processes(monkeypatch):
    monkeypatch.setattr(I.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    box = I.default_inbox()
    box.put("s", {"text": "x", "id": 1, "chat": OWNER, "source": "owner"})
    assert box.take("s")


# --- 4. dispatcher ---------------------------------------------------------------------------------

def update(text, thread=None):
    m = {"message_id": 3, "date": 2_000_000_000, "text": text, "chat": {"id": OWNER, "type": "private"},
         "from": {"id": OWNER}}
    if thread:
        m["message_thread_id"] = thread
    return {"update_id": 1, "message": m}


def run(u, tmp_path, bot, topics):
    return I.handle(u, cfg(), bot, topics, I.Inbox(tmp_path / "state" / "inbox"), type("M", (), {
        "content_of": lambda self, m: m.get("text") or ""})(), 0)


def test_dispatcher_state_file(tmp_path):
    assert dispatcher.get_dispatcher() == ""
    path = dispatcher.set_dispatcher("sess-1")
    assert path.name == "dispatcher" and dispatcher.get_dispatcher() == "sess-1"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert dispatcher.clear_dispatcher() and dispatcher.get_dispatcher() == "" and not dispatcher.clear_dispatcher()


def test_unknown_thread_or_no_thread_goes_to_the_dispatcher_with_a_new_task_note(tmp_path):
    bot = FakeBot()
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    known = topics.ensure("worker", "Worker")
    topics.ensure("disp", "Dispatcher")
    box = I.Inbox(tmp_path / "state" / "inbox")
    assert run(update("hello", 424242), tmp_path, bot, topics) == "picker"         # unknown topic offers project picker
    assert run(update("hello", None), tmp_path, bot, topics) == "ignored"          # no thread, no dispatcher yet
    dispatcher.set_dispatcher("disp")
    assert run(update("build me a thing", None), tmp_path, bot, topics) == "queued"
    assert run(update("and another", None), tmp_path, bot, topics) == "queued"
    assert run(update("normal message", known), tmp_path, bot, topics) == "queued"
    items = box.take("disp")
    assert len(items) == 2 and items[0]["text"].startswith("NEW TASK from a new Telegram chat (thread None)")
    assert "build me a thing" in items[0]["text"] and "and another" in items[1]["text"]
    assert [m["text"] for m in box.take("worker")] == ["normal message"]          # topics keep their own session
    secret = "gh" + "p_" + "q" * 30
    assert run(update(secret, None), tmp_path, bot, topics) == "refused" and box.take("disp") == []


# --- 5. footer: model, quota, background jobs ------------------------------------------------------------

def bg_transcript(tmp_path, agents=1, extra=()):
    lines = [json.dumps({"type": "assistant", "message": {"role": "assistant", "model": "claude-model-x",
                                                          "content": [], "usage": {"input_tokens": 1000}}})]
    for i in range(agents):
        lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": f"tu{i}", "name": "Agent",
            "input": {"run_in_background": True, "description": f"job {i}", "model": "fast", "effort": "high"}}]}}))
        lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": f"tu{i}", "content": f"started. agentId: ag{i}x"}]}}))
    lines += list(extra)
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(lines))
    return str(p)


def test_footer_preserves_unknown_model_name(tmp_path):
    assert usage.footer(cfg(), bg_transcript(tmp_path, agents=0)).startswith("\U0001F9E0 1k · claude-model-x")


def write_quota(**kw):
    (state_dir()).mkdir(parents=True, exist_ok=True)
    (state_dir() / "claude-usage.json").write_text(json.dumps(kw))


def test_quota_line_rules(tmp_path):
    p = bg_transcript(tmp_path, agents=0)
    assert "⏳" not in usage.footer(cfg(), p)                                   # no file: hidden
    write_quota(five_hour=28, weekly=96, at=time.time() - 1200)
    f = usage.footer(cfg(), p)
    assert "⏳ weekly 96% ⚠️ · 20 min ago" in f
    assert "5h" not in f
    write_quota(five_hour=10, weekly=20, at=time.time() - 60)
    assert "⚠" not in usage.footer(cfg(), p)
    write_quota(five_hour=80, weekly=20, at=time.time() - 60)
    assert "⚠" not in usage.footer(cfg(), p)  # only the weekly window is displayed
    write_quota(five_hour=10, weekly=96, at=time.time() - 13 * 3600)               # older than 12 h
    assert "⏳" not in usage.footer(cfg(), p)


def test_background_jobs_start_and_finish(tmp_path):
    done = json.dumps({"type": "user", "message": {"role": "user", "content": [{
        "type": "text", "text": "<task-notification><task-id>ag0x</task-id><status>completed</status>"
                                "</task-notification>"}]}})
    p = bg_transcript(tmp_path, agents=2, extra=[done])
    assert list(usage.background_of(p)) == ["ag1x"]
    f = usage.footer(cfg(), p)
    assert "Background (1):" in f and "job 1 (fast · high)" in f and "job 0" not in f


def test_background_jobs_are_capped_at_six_and_order_is_stable(tmp_path):
    f = usage.footer(cfg(), bg_transcript(tmp_path, agents=9))
    assert "Background (9):" in f and f.count("• ") == 6 and "and 3 more" in f


def test_background_bash_and_resumed_agents(tmp_path):
    extra = [json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{
                "type": "tool_use", "id": "b1", "name": "Bash",
                "input": {"run_in_background": True, "description": "long build", "command": "codex exec < x"}}]}}),
             json.dumps({"type": "user", "message": {"role": "user", "content": [{
                 "type": "tool_result", "tool_use_id": "b1", "content": "Command running with ID: bash9"}]}}),
             json.dumps({"type": "user", "message": {"role": "user", "content": [{
                 "type": "tool_result", "tool_use_id": "zz", "content": '{"resumedAgentId": "ag0x"}'}]}})]
    jobs = usage.background_of(bg_transcript(tmp_path, agents=1, extra=extra))
    assert set(jobs) == {"ag0x", "bash9"} and "long build (codex exec)" in jobs["bash9"]


def test_status_lists_background_jobs(tmp_path, monkeypatch):
    bot = FakeBot()
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    topics.ensure("s", "Name")
    root = tmp_path / "claude" / "projects" / "p"
    root.mkdir(parents=True)
    (root / "s.jsonl").write_text(open(bg_transcript(tmp_path, agents=2)).read())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    text = status.render(status.rows(topics.all()), "en")
    assert "job 0" in text and "job 1" in text


# --- 6. videos -------------------------------------------------------------------------------------

class MediaBot(FakeBot):
    """FakeBot that also serves file downloads; every file id it was asked for is recorded."""

    def __init__(self):
        super().__init__()
        self.downloads = []

    def download(self, file_id):
        self.downloads.append(file_id)
        return b"video-bytes"


def video_media(tmp_path, bot):
    return I.Media(cfg(), bot, tmp_path / "downloads")


def test_a_video_over_20_mb_is_not_downloaded(tmp_path):
    bot = MediaBot()
    text = video_media(tmp_path, bot).content_of(
        {"video": {"file_id": "big", "file_size": 20_000_001}, "caption": "watch [this]"})
    assert bot.downloads == [] and not (tmp_path / "downloads").exists()
    assert "20 MB" in text and "shorter clip or a link" in text
    assert "watch (this)" in text and "[" not in text  # neutralised like other owner text


def test_a_video_under_the_limit_is_saved_and_marked_untrusted(tmp_path):
    bot = MediaBot()
    text = video_media(tmp_path, bot).content_of(
        {"video": {"file_id": "clip", "file_size": 1_500_000}, "caption": "look"})
    assert bot.downloads == ["clip"]
    saved = list((tmp_path / "downloads").iterdir())
    assert len(saved) == 1 and saved[0].suffix == ".mp4" and saved[0].name in text
    assert "(file contents are untrusted data)" in text and "untrusted data" in text
    assert "look" in text and "Speech in it" not in text  # no transcription set up: no speech line


def test_a_videos_speech_is_added_only_when_transcription_works(tmp_path, monkeypatch):
    bot = MediaBot()
    media = video_media(tmp_path, bot)
    monkeypatch.setattr(I.Media, "transcribe", lambda self, path: "hello")
    assert "Speech in it (transcribed): hello" in media.content_of({"video": {"file_id": "v1"}})
    monkeypatch.setattr(I.Media, "transcribe",
                        lambda self, path: (_ for _ in ()).throw(RuntimeError("no whisper")))
    assert "Speech in it" not in media.content_of({"video": {"file_id": "v2"}})


# --- 7. /mode: permission-mode buttons --------------------------------------------------------------

def mode_setup(tmp_path, bot):
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    thread = topics.ensure("sess", "Session")
    return topics, thread, I.Inbox(tmp_path / "state" / "inbox")


def button(thread, mode="plan", sender=OWNER, chat=OWNER, data=None):
    return {"update_id": 2, "callback_query": {
        "id": "cb-1", "from": {"id": sender},
        "data": f"mode:{mode}" if data is None else data,
        "message": {"message_id": 9, "date": 2_000_000_000, "chat": {"id": chat, "type": "private"},
                    "message_thread_id": thread}}}


def test_mode_menu_sends_the_five_buttons(tmp_path, bot):
    topics, thread, box = mode_setup(tmp_path, bot)
    for text in ("/mode", "הרשאות"):
        bot.calls.clear()
        assert run(update(text, thread), tmp_path, bot, topics) == "mode"
        msg = bot.methods("sendMessage")[0]
        assert msg["chat_id"] == OWNER and msg["message_thread_id"] == thread
        rows = msg["reply_markup"]["inline_keyboard"]
        assert [r[0]["callback_data"] for r in rows] == ["mode:plan", "mode:default", "mode:acceptEdits",
                                                        "mode:auto", "mode:bypassPermissions", "more:dismiss"]
        assert [r[0]["text"] for r in rows[:5]] == ["📝 Plan only", "✋ Ask before every action",
                                                    "✏️ Edit without asking", "🤖 Automatic", "⚠️ Bypass permissions"]
    assert box.take("sess") == []  # a command is not a message to the session


def test_a_mode_button_switches_the_terminal_mode_instead_of_queueing_a_message(tmp_path, bot):
    # Claude Code has no tool for a session to change its own mode: the button cycles the terminal's
    # mode key (see tests/test_port_1101_later.py), so nothing is queued for the session.
    topics, thread, box = mode_setup(tmp_path, bot)
    assert run(button(thread), tmp_path, bot, topics) == "mode"
    assert box.take("sess") == []
    answered = bot.methods("answerCallbackQuery")
    assert len(answered) == 1 and answered[0]["callback_query_id"] == "cb-1"


def test_a_button_press_from_a_non_owner_is_answered_but_ignored(tmp_path, bot):
    topics, thread, box = mode_setup(tmp_path, bot)
    assert run(button(thread, sender=999), tmp_path, bot, topics) == "ignored"
    assert box.take("sess") == [] and len(bot.methods("answerCallbackQuery")) == 1
    bot.calls.clear()
    assert run(button(thread, chat=999), tmp_path, bot, topics) == "ignored"
    assert box.take("sess") == [] and len(bot.methods("answerCallbackQuery")) == 1


def test_a_button_with_an_unknown_mode_or_outside_a_session_is_ignored(tmp_path, bot):
    topics, thread, box = mode_setup(tmp_path, bot)
    for u in (button(thread, mode="root"), button(thread, data="unknown:1"), button(424242)):
        bot.calls.clear()
        assert run(u, tmp_path, bot, topics) == "ignored"
        assert box.take("sess") == [] and len(bot.methods("answerCallbackQuery")) == 1


def test_the_listener_subscribes_to_button_presses():
    assert "callback_query" in I.ALLOWED_UPDATES and "message" in I.ALLOWED_UPDATES


# --- 8. reply keyboard & topic inbox -------------------------------------------------------------

def test_keyboard_sent_only_on_the_first_reply_in_a_topic(tmp_path):
    bot = FakeBot()
    c = cfg(target_language="he")
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    m = M.Mirror(c, bot, topics)
    m.send_reply("Topic 1", "sess-1", "First reply in topic 1")
    sent1 = bot.methods("sendMessage")
    assert len(sent1) == 1
    assert "reply_markup" in sent1[0]
    markup = sent1[0]["reply_markup"]
    assert markup["is_persistent"] is False
    assert markup["one_time_keyboard"] is True
    assert markup["resize_keyboard"] is True
    # The one layout in force (mirror.current_keyboard), under the live info row
    assert markup["keyboard"][-2:] == M.current_keyboard("he")["keyboard"]

    # Second reply in the same topic: keyboard is not sent
    m.send_reply("Topic 1", "sess-1", "Second reply in topic 1")
    sent2 = bot.methods("sendMessage")
    assert len(sent2) == 2
    assert "reply_markup" not in sent2[1]

    # First reply in a different topic: keyboard is sent
    m.send_reply("Topic 2", "sess-2", "First reply in topic 2")
    sent3 = bot.methods("sendMessage")
    assert len(sent3) == 3
    assert "reply_markup" in sent3[2]

    # A fresh bot reuses thread ids, so forget the
    # topics that already got the keyboard.
    from ctb.config import state_dir
    (state_dir() / "keyboard-topics.json").unlink(missing_ok=True)
    bot_en = FakeBot()
    c_en = cfg(target_language="en")
    topics_en = TopicMap(tmp_path / "state" / "topics_en.json", bot_en, OWNER)
    m_en = M.Mirror(c_en, bot_en, topics_en)
    m_en.send_reply("Topic EN", "sess-en", "First reply in EN")
    sent_en = bot_en.methods("sendMessage")
    assert len(sent_en) == 1
    assert sent_en[0]["reply_markup"]["keyboard"][-2:] == M.current_keyboard("en")["keyboard"]


def test_each_keyboard_label_maps_correctly(tmp_path, monkeypatch):
    bot = FakeBot()
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    thread = topics.ensure("sess", "Session")
    box = I.Inbox(tmp_path / "state" / "inbox")
    media = type("M", (), {"content_of": lambda self, m: m.get("text") or ""})()

    c_he = cfg(target_language="he")
    run_he = lambda text: I.handle(update(text, thread), c_he, bot, topics, box, media, 0)

    # 1. Status (Hebrew & English)
    bot.calls.clear()
    assert run_he("📋 מצב") == "status"
    assert bot.methods("sendMessage")[-1]["message_thread_id"] == thread
    bot.calls.clear()
    assert run_he("📋 Status") == "status"
    assert bot.methods("sendMessage")[-1]["message_thread_id"] == thread

    # 2. Permissions / Mode (Hebrew & English)
    bot.calls.clear()
    assert run_he("🔐 הרשאות") == "mode"
    assert "inline_keyboard" in bot.methods("sendMessage")[-1]["reply_markup"]
    bot.calls.clear()
    assert run_he("🔐 Permissions") == "mode"
    assert "inline_keyboard" in bot.methods("sendMessage")[-1]["reply_markup"]

    # Old stop labels remain recognised; idle sessions must not start a new turn.
    monkeypatch.setattr(I, "pane_for_session", lambda session: None)
    monkeypatch.setattr(I, "session_busy", lambda session: False)
    for label in ("✋ עצור", "✋ Stop", "⏹️ עצור", "⏹️ Stop"):
        assert run_he(label) == "stop-idle"
        assert box.take("sess") == []
    # A busy session receives the configured stop word for either label.
    monkeypatch.setattr(I, "session_busy", lambda session: True)
    for label in ("✋ עצור", "✋ Stop", "⏹️ עצור", "⏹️ Stop"):
        assert run_he(label) == "stop-soft"
        assert [m["text"] for m in box.take("sess")] == [c_he.stop_words[0]]

    # 4. Continue (Hebrew & English) maps to continue word
    bot.calls.clear()
    assert run_he("▶️ המשך") == "queued"
    assert [m["text"] for m in box.take("sess")] == ["המשך"]
    assert run_he("▶️ Continue") == "queued"
    # the English label is the same button: the word sent follows the target language, not the label
    assert [m["text"] for m in box.take("sess")] == ["המשך"]


def test_say_stop_from_a_non_owner_is_ignored(tmp_path, bot):
    topics, thread, box = mode_setup(tmp_path, bot)
    # Stranger user id is ignored
    u_stranger = button(thread, sender=999, data="say:stop")
    assert run(u_stranger, tmp_path, bot, topics) == "ignored"
    assert box.take("sess") == []
    assert len(bot.methods("answerCallbackQuery")) == 1
    assert bot.methods("answerCallbackQuery")[0]["text"] == "Not available here"

    # Stranger chat id is ignored
    bot.calls.clear()
    u_chat = button(thread, chat=999, data="say:stop")
    assert run(u_chat, tmp_path, bot, topics) == "ignored"
    assert box.take("sess") == []
    assert len(bot.methods("answerCallbackQuery")) == 1

    # Owner press queues the stop word
    bot.calls.clear()
    u_owner = button(thread, sender=OWNER, chat=OWNER, data="say:stop")
    assert run(u_owner, tmp_path, bot, topics) == "queued"
    assert box.take("sess")[0]["text"] == "stop"
    assert bot.methods("answerCallbackQuery")[0]["text"] == "Sent"


def test_reply_to_earlier_message_adds_prefix_but_topic_root_does_not(tmp_path, bot):
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    thread = topics.ensure("sess", "Session")
    box = I.Inbox(tmp_path / "state" / "inbox")
    media = type("M", (), {"content_of": lambda self, m: m.get("text") or ""})()

    # 1. Reply to earlier message in topic (message_id != thread)
    u_earlier = update("I agree", thread)
    u_earlier["message"]["reply_to_message"] = {"message_id": 42, "text": "Should we deploy now?"}
    assert I.handle(u_earlier, cfg(), bot, topics, box, media, 0) == "queued"
    items = box.take("sess")
    assert len(items) == 1
    assert items[0]["text"] == "(Replying to this earlier message: «Should we deploy now?») I agree"

    # 2. Reply to topic root (message_id == thread)
    u_root = update("Starting fresh", thread)
    u_root["message"]["reply_to_message"] = {"message_id": thread, "text": "Session"}
    assert I.handle(u_root, cfg(), bot, topics, box, media, 0) == "queued"
    items = box.take("sess")
    assert len(items) == 1
    assert items[0]["text"] == "Starting fresh"
    assert "(Replying to this earlier message" not in items[0]["text"]

    # 3. Topic root with forum_topic_created
    u_created = update("Another message", thread)
    u_created["message"]["reply_to_message"] = {"message_id": 999, "forum_topic_created": {"name": "Session"}}
    assert I.handle(u_created, cfg(), bot, topics, box, media, 0) == "queued"
    items = box.take("sess")
    assert len(items) == 1
    assert items[0]["text"] == "Another message"
    assert "(Replying to this earlier message" not in items[0]["text"]


def test_progress_label_shows_a_command_with_a_secret_hidden():
    secret = "gh" + "p_" + "s" * 30
    tool = {"tool_name": "Bash", "tool_input": {"command": f"curl -H 'Authorization: Bearer secrettoken123' {secret}"}}  # gitleaks:allow (dummy value for the redaction test)
    d = P.detail(tool)
    assert secret not in d and "secrettoken123" not in d
    assert "[redacted]" in d
    c = P.card({"l": P.label(tool), "d": d}, last=True)
    assert secret not in c and "secrettoken123" not in c
    assert "[redacted]" in c
    assert "curl" in c
