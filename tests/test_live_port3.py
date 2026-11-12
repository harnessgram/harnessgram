import json
import os
import time

import pytest

from conftest import GOOD_TOKEN
from ctb import agents_card, cli, hooks, inbox, mirror, progress, telegram, topics, usage, wake
from ctb.config import AGENT_NOTICES, Config, TeamConfig, set_setting, state_dir
from ctb.projects import Place


@pytest.fixture
def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7)


def test_compact_notice(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(wake, "load_config", lambda: cfg)
    monkeypatch.setattr(wake, "spawn", lambda args: calls.append(args))
    assert wake.hook_start(json.dumps({"session_id": "s", "source": "compact"})) == 0
    assert calls == [["lifecycle", "--session=s", "--kind=compact-done"]]


def test_lifecycle_dispatch_counts_every_agent_end_and_ignores_clear(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(hooks, "load_config", lambda: cfg)
    monkeypatch.setattr(hooks, "spawn", lambda args: calls.append(args))
    for event, extra in [("PreCompact", {}), ("SessionEnd", {"reason": "clear"}),
                         ("SessionEnd", {}), ("SubagentStop", {}), ("SubagentStop", {})]:
        assert hooks.hook_lifecycle(json.dumps({"session_id": "s", "hook_event_name": event, **extra})) == 0
    assert [x[2] for x in calls] == ["--kind=precompact", "--kind=session-end", "--kind=subagent-stop", "--kind=subagent-stop"]
    assert cli._parser().parse_args(["hook-lifecycle"]).cmd == "hook-lifecycle"


def test_lifecycle_messages(cfg, bot, monkeypatch):
    monkeypatch.setattr(mirror.TopicMap, "place_of", lambda self, session: Place(self.chat_id, 12))
    states = []
    monkeypatch.setattr(topics, "set_state", lambda *args, **kw: states.append(args[2]))
    for kind in ("precompact", "compact-done", "session-end", "subagent-stop"):
        assert mirror.run_lifecycle(cfg, "s", kind, "worker", "task", bot=bot) == 0
    messages = bot.methods("sendMessage")
    assert messages[0]["text"] == "\u200f🗜️ מתחילה דחיסה… חכה להודעה שהיא הסתיימה לפני שתכתוב."
    assert messages[1]["text"] == "\u200f✅ הדחיסה הסתיימה. אפשר להמשיך לכתוב."
    assert not messages[2].get("disable_notification") and states == ["done"]
    assert "1 ✅" in messages[3]["text"] and "disable_notification" not in messages[3]  # the agents card, collapsed


def test_keyboard_and_answer_receipt(cfg, bot, tmp_path):
    tm = topics.TopicMap(tmp_path / "topics.json", bot, 7)
    thread = tm.ensure("s", "test")
    box = inbox.Inbox(tmp_path / "inbox", 50)
    m = {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "date": time.time(),
         "message_thread_id": thread, "message_id": 5, "text": "/keyboard"}
    assert inbox.handle({"message": m}, cfg, bot, tm, box, None, 0) == "keyboard"
    assert "keyboard" in bot.methods("sendMessage")[-1]["reply_markup"]
    topics.record_reply(7, thread, 5)
    q = {"id": "q", "from": {"id": 7}, "message": m, "data": "say:yes"}
    assert inbox.handle_button(q, cfg, bot, tm, box) == "queued"
    assert box.take("s")[0]["text"] == "כן"
    assert bot.methods("editMessageReplyMarkup")[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "noop"
    q["data"] = "noop"
    assert inbox.handle_button(q, cfg, bot, tm, box) == "ignored"
    inbox.set_commands(bot, cfg)
    assert any(c["command"] == "keyboard" for c in bot.methods("setMyCommands")[0]["commands"])


def test_topic_name_persisted(monkeypatch):
    name = topics.name_for_task("/work/project", "one two three four five six")
    topics.save_session_name("s", name)
    assert hooks.session_title(None, "s") == "project: one two three four five"


def test_question_keyboard(cfg, bot, tmp_path):
    m = mirror.Mirror(cfg, bot, topics.TopicMap(tmp_path / "topics", bot, 7))
    m.send_reply("title", "s", "Ready?")
    buttons = bot.methods("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["say:no", "say:yes"]


def test_changed_files_and_compact_footer(cfg, tmp_path):
    p = tmp_path / "transcript"
    rows = [{"type": "user", "message": {"content": "old"}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": "/a/old"}}]}},
            {"type": "user", "message": {"content": "new"}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": f"/a/{i}"}} for i in range(4)]}}]
    p.write_text("\n".join(json.dumps(x) for x in rows))
    assert usage.changed_line(str(p)) == "✏️ קבצים ששונו: 4 (מוצגים 3)\n0, 1, 2"
    assert usage.changed_line(str(p), "en") == "✏️ Files changed: 4 (showing 3)\n0, 1, 2"
    assert "✏️" not in usage.compact_footer(cfg, str(p))


@pytest.mark.parametrize("error,expected", [(TimeoutError(), "טלגרם לא ענה בזמן"),
    (telegram.TelegramError("thread not found"), "הנושא נמחק"),
    (telegram.TelegramError("slow", 429), "טלגרם ביקש להאט"),
    (ConnectionError(), "אין חיבור לאינטרנט"), (ValueError(), "שגיאה לא צפויה")])
def test_failure_reasons(error, expected):
    assert mirror.reason_he(error) == expected


def test_progress():
    label = progress.label({"tool_name": "TodoWrite", "tool_input": {"todos": [
        {"status": "completed", "content": "done"}, {"status": "in_progress", "content": "working"}]}})
    assert "(1/2)" in label and "✅" in label and "⏳" in label
    label = progress.label({"tool_name": "Bash", "tool_input": {"command": "codex exec -m gpt-test"}})
    assert "🤖" in label and "[Codex gpt-test · ברקע]" in label


def test_large_file_and_prune(cfg, bot, tmp_path, monkeypatch):
    path = tmp_path / "big"
    path.write_bytes(b"data")
    monkeypatch.setattr(mirror, "UPLOAD_LIMIT", 1)
    cfg.share_base_url = "https://example.test/files"
    m = mirror.Mirror(cfg, bot, topics.TopicMap(tmp_path / "topics", bot, 7))
    m.send_file("t", "s", str(path))
    assert "https://example.test/files/" in bot.methods("sendMessage")[-1]["text"]
    old = state_dir() / "share" / "old"
    old.mkdir()
    os.utime(old, (1, 1))
    assert mirror.share_prune(now=time.time()) == 1
    assert not any(method.startswith("sendDocument") for method, _ in bot.calls)


def test_team_failure_does_not_drop_owner(cfg, bot, tmp_path):
    cfg.team = TeamConfig(chat_id=-7, shared_sessions=["s"])
    class BrokenTopics:
        def ensure(self, session, title):
            return None
    m = mirror.Mirror(cfg, bot, topics.TopicMap(tmp_path / "topics", bot, 7), BrokenTopics())
    assert m.send_reply("t", "s", "hello") == 1
    assert m.dropped == 0
    assert len(bot.methods("sendMessage")) == 1


def test_retry_429_and_parse_only(cfg, bot, tmp_path, monkeypatch):
    tm = topics.TopicMap(tmp_path / "topics", bot, 7)
    tm.ensure("s", "t")
    orig = bot.call
    failures = [telegram.TelegramError("slow", 429, 99)]
    def call(method, params=None, timeout=15):
        if method == "sendMessage" and failures:
            raise failures.pop()
        return orig(method, params, timeout)
    monkeypatch.setattr(bot, "call", call)
    delays = []
    monkeypatch.setattr(mirror.time, "sleep", delays.append)
    assert mirror.Mirror(cfg, bot, tm).send_reply("t", "s", "hello") == 1
    assert delays == [30]
    failures.append(telegram.TelegramError("forbidden", 403))
    assert mirror.Mirror(cfg, bot, tm).send_reply("t", "s", "hello") == 0
    failures.append(telegram.TelegramError("can't parse entities", 400))
    assert mirror.Mirror(cfg, bot, tm).send_reply("t", "s", "hello") == 1
    assert "parse_mode" not in bot.methods("sendMessage")[-1]


def test_upload_timeout(monkeypatch):
    bot = telegram.Bot(GOOD_TOKEN)
    calls = []
    monkeypatch.setattr(bot, "_request", lambda *args: calls.append(args) or {})
    bot.upload("sendDocument", {}, "document", "x", b"data")
    assert calls[0][-1] == 120
    bot.upload("sendDocument", {}, "document", "x", b"data", timeout=42)
    assert calls[1][-1] == 42


def test_footer_never_truncates_reply(cfg, bot, tmp_path):
    m = mirror.Mirror(cfg, bot, topics.TopicMap(tmp_path / "topics", bot, 7))
    text = "x" * 4000
    m.send_reply("t", "s", text, footer="f" * 1000)
    messages = bot.methods("sendMessage")
    assert all(len(p["text"]) <= 4096 for p in messages)
    assert sum(p["text"].count("x") for p in messages) == 4000


def test_stale_button_does_not_send_keys(cfg, bot, monkeypatch):
    monkeypatch.setattr(inbox, "pane_for_session", lambda s: "%1")
    screens = iter(["original", "changed"])
    monkeypatch.setattr(inbox, "pane_text", lambda p: next(screens))
    monkeypatch.setattr(inbox, "_safe_menu", lambda body: True)
    monkeypatch.setattr(inbox, "_terminal_say", lambda *a: None)
    monkeypatch.setattr(inbox, "_tmux", lambda *a: pytest.fail("sent stale key"))
    stamp = inbox._screen_stamp("original")
    inbox.keypad_press({"id": "q", "data": "k:1:" + stamp}, cfg, bot, {}, "s")
    assert bot.methods("answerCallbackQuery")[-1]["text"] == "השאלה כבר לא פתוחה"


def test_share_publish_rejects_symlink_and_copies_verified_file(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"original")
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(OSError):
        mirror.share_publish(str(link), "https://example.test", tmp_path / "share")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        mirror.share_publish(str(fifo), "https://example.test", tmp_path / "share")

    real_open = mirror.os.open
    def replace_after_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if str(path) == str(source):
            source.unlink()
            source.symlink_to(link)
        return fd
    monkeypatch.setattr(mirror.os, "open", replace_after_open)
    url = mirror.share_publish(str(source), "https://example.test", tmp_path / "share")
    assert (tmp_path / "share" / url.split("/")[-2] / "file").read_bytes() == b"original"  # an opaque name


def test_share_rejects_expired_at_request_time(tmp_path):
    import io
    from ctb import share
    root = state_dir() / "share" / "token"
    root.mkdir(parents=True)
    (root / "file").write_bytes(b"data")
    handler = share.ShareHandler.__new__(share.ShareHandler)
    handler.path = "/token/file"
    handler.wfile = io.BytesIO()
    statuses = []
    handler.send_error = lambda status: statuses.append(status)
    handler.send_response = lambda status: statuses.append(status)
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler.do_GET()
    assert statuses == [200] and handler.wfile.getvalue() == b"data"
    os.utime(root, (time.time() - 8 * 86400,) * 2)
    statuses.clear()
    handler.do_GET()
    assert statuses == [404]


def test_answer_is_consumed_once_even_after_restart(cfg, bot, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    tm = topics.TopicMap(tmp_path / "topics", bot, 7)
    thread = tm.ensure("s", "test")
    root = tmp_path / "inbox"
    message = {"chat": {"id": 7, "type": "private"}, "message_thread_id": thread, "message_id": 5}
    topics.record_reply(7, thread, 5)
    def press(data):
        q = {"id": data, "from": {"id": 7}, "message": message, "data": data}
        return inbox.handle_button(q, cfg, bot, tm, inbox.Inbox(root))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(press, ["say:yes", "say:no"]))
    assert sorted(results) == ["ignored", "queued"]
    assert len(inbox.Inbox(root).take("s")) == 1
    assert press("say:continue") == "ignored"
    assert bot.methods("answerCallbackQuery")[-1]["text"] == "כבר נשלח"
    assert inbox.Inbox(root).take("s") == []


def test_answer_history_is_bounded_and_old_replays_fail_closed(tmp_path):
    box = inbox.Inbox(tmp_path / "inbox")
    assert box.consume_answer(7, 1)
    assert box.consume_answer(7, 10002)
    assert not box.consume_answer(7, 1)
    assert json.loads((box.root / "answers.json").read_text()) == {"7": [10002]}


def test_connect_prefers_ipv4_and_restores_read_timeout(monkeypatch):
    events = []
    class Socket:
        def __init__(self, family, *args):
            events.append(("socket", family))
        def settimeout(self, value):
            events.append(("timeout", value))
        def connect(self, addr):
            events.append(("connect", addr))
        def close(self):
            events.append(("close",))
    monkeypatch.setattr(telegram.socket, "getaddrinfo", lambda *a: [
        (telegram.socket.AF_INET6, 1, 0, "", ("::1", 443)),
        (telegram.socket.AF_INET, 1, 0, "", ("127.0.0.1", 443))])
    monkeypatch.setattr(telegram.socket, "socket", Socket)
    telegram._connect(("example.test", 443), 20)
    assert events == [("socket", telegram.socket.AF_INET), ("timeout", 4),
                      ("connect", ("127.0.0.1", 443)), ("timeout", 20)]


def test_question_footer_restores_keyboard(cfg, bot, tmp_path):
    m = mirror.Mirror(cfg, bot, topics.TopicMap(tmp_path / "topics", bot, 7))
    assert m.send_reply("title", "s", "Ready?", footer="status") == 1
    question, footer = bot.methods("sendMessage")
    assert "inline_keyboard" in question["reply_markup"]
    assert "keyboard" in footer["reply_markup"] and "status" in footer["text"]
    assert footer["message_thread_id"] == question["message_thread_id"]


@pytest.mark.parametrize("key,expected", [("refresh", "🔄 מרענן"), ("esc", "✔️ נשלח: esc")])
def test_keypad_confirmation(cfg, bot, monkeypatch, key, expected):
    monkeypatch.setattr(inbox, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(inbox, "pane_text", lambda p: "screen")
    monkeypatch.setattr(inbox, "_send_keys", lambda *args: True)
    monkeypatch.setattr(inbox.time, "sleep", lambda _: None)
    inbox.keypad_press({"id": "q", "data": "k:" + key}, cfg, bot, {"message_id": 1}, "s")
    assert bot.methods("answerCallbackQuery")[-1]["text"] == expected


def test_prompt_names_session_once_and_app_title_wins(cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(hooks, "load_config", lambda: cfg)
    monkeypatch.setattr(hooks, "spawn", lambda args: None)
    monkeypatch.setattr(hooks, "prompt_output", lambda data, config: None)
    for prompt in ("first task words", "different task"):
        assert hooks.hook_prompt(json.dumps({"session_id": "s", "cwd": "/work/proj", "prompt": prompt})) == 0
    assert topics.session_name("s") == "proj: first task words"
    transcript = tmp_path / "transcript"
    transcript.write_text(json.dumps({"type": "custom-title", "customTitle": "App title"}) + "\n")
    assert hooks.session_title(str(transcript), "s") == "App title"


def test_only_newest_reply_accepts_answer(cfg, bot, tmp_path):
    tm = topics.TopicMap(tmp_path / "topics", bot, 7)
    m = mirror.Mirror(cfg, bot, tm)
    box = inbox.Inbox(tmp_path / "inbox")
    m.send_reply("t", "s", "First?")
    first = bot.methods("sendMessage")[-1]
    first_id = topics.latest_reply(7, first["message_thread_id"])
    m.send_reply("t", "s", "Second reply")
    q = {"id": "q", "from": {"id": 7}, "data": "say:yes", "message": {
        "chat": {"id": 7, "type": "private"}, "message_thread_id": first["message_thread_id"], "message_id": first_id}}
    assert inbox.handle_button(q, cfg, bot, tm, box) == "ignored"
    assert bot.methods("answerCallbackQuery")[-1]["text"] == "השאלה הזו כבר לא אחרונה"
    assert bot.methods("editMessageReplyMarkup")[-1]["reply_markup"] == {"inline_keyboard": []}
    assert box.take("s") == []


@pytest.mark.parametrize("content", ["{", "[]", '{"7": "bad"}'])
def test_corrupt_answer_history_is_safe(cfg, bot, tmp_path, content):
    tm = topics.TopicMap(tmp_path / "topics", bot, 7)
    thread = tm.ensure("s", "test")
    topics.record_reply(7, thread, 5)
    box = inbox.Inbox(tmp_path / "inbox")
    box.root.mkdir()
    (box.root / "answers.json").write_text(content)
    q = {"id": "q", "from": {"id": 7}, "data": "say:yes", "message": {
        "chat": {"id": 7}, "message_thread_id": thread, "message_id": 5}}
    assert inbox.handle_button(q, cfg, bot, tm, box) == "ignored"
    assert bot.methods("answerCallbackQuery")
    assert box.take("s") == []


@pytest.mark.parametrize("path", ["/token/..", "/token/%2F", "/token/file/extra", "/token/"])
def test_share_handler_rejects_invalid_paths(path):
    from ctb.share import ShareHandler
    handler = ShareHandler.__new__(ShareHandler)
    handler.path = path
    statuses = []
    handler.send_error = statuses.append
    handler.do_GET()
    assert statuses == [404]


def test_share_hardlink_and_size_cap(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"data")
    url = mirror.share_publish(str(source), "https://example.test", tmp_path / "share")
    target = tmp_path / "share" / url.split("/")[-2] / "file"  # an opaque name, never the original one
    assert target.stat().st_ino == source.stat().st_ino
    monkeypatch.setattr(mirror, "SHARE_LIMIT", 3)
    with pytest.raises(ValueError):
        mirror.share_publish(str(source), "https://example.test", tmp_path / "share")


@pytest.mark.parametrize("error", [OSError(), ValueError()])
def test_share_failure_sends_note(cfg, bot, tmp_path, monkeypatch, error):
    source = tmp_path / "file"
    source.write_bytes(b"data")
    cfg.share_base_url = "https://example.test"
    monkeypatch.setattr(mirror, "UPLOAD_LIMIT", 1)
    def fail(*args):
        raise error
    monkeypatch.setattr(mirror, "share_publish", fail)
    mirror.Mirror(cfg, bot, topics.TopicMap(tmp_path / "topics", bot, 7)).send_file("t", "s", str(source))
    assert mirror.L.strings(cfg.target_language)["file_too_large"] in bot.methods("sendMessage")[-1]["text"]


def test_a_file_never_renames_its_topic(cfg, bot, tmp_path):
    source = tmp_path / "shot.png"
    source.write_bytes(b"data")
    tmap = topics.TopicMap(tmp_path / "topics", bot, 7)
    tmap.ensure("s", "project: task")
    renames = len(bot.methods("editForumTopic"))
    mirror.Mirror(cfg, bot, tmap).send_file("Claude", "s", str(source))
    assert len(bot.methods("editForumTopic")) == renames
    assert tmap.all_places()["s"]["name"] == "project: task"


def test_lifecycle_redacts_description(cfg, bot, monkeypatch):
    monkeypatch.setattr(mirror.TopicMap, "place_of", lambda self, session: Place(self.chat_id, 12))
    monkeypatch.setattr(agents_card, "redact", lambda text: "redacted")
    mirror.run_lifecycle(cfg, "s", "subagent-stop", "worker", "secret", bot=bot)
    saved = (state_dir() / "agents-cards" / "s.json").read_text()
    assert "redacted" in saved and "secret" not in saved


def test_yes_button_nudges_an_idle_session(cfg, bot, tmp_path, monkeypatch):
    tm = topics.TopicMap(tmp_path / "topics.json", bot, 7)
    thread = tm.ensure("s", "test")
    box = inbox.Inbox(tmp_path / "inbox", 50)
    m = {"chat": {"id": 7, "type": "private"}, "from": {"id": 7}, "date": time.time(),
         "message_thread_id": thread, "message_id": 6}
    topics.record_reply(7, thread, 6)
    nudged = []
    monkeypatch.setattr(inbox, "session_busy", lambda s: False)
    monkeypatch.setattr(inbox, "_reopen_or_nudge", lambda b, s: nudged.append(s))
    q = {"id": "q", "from": {"id": 7}, "message": m, "data": "say:yes"}
    assert inbox.handle_button(q, cfg, bot, tm, box) == "queued"
    for _ in range(50):
        if nudged:
            break
        time.sleep(0.02)
    assert nudged == ["s"]


def test_latest_reply_is_recorded_for_a_group_chat():
    """A group's chat id is negative; refusing it made every yes/no press in a group 'no longer current'."""
    topics.record_reply(-1001234567890, 21, 5)
    assert topics.latest_reply(-1001234567890, 21) == 5
    for chat, thread, message in ((0, 21, 6), (-100, 0, 6), (-100, 21, 0), ("-100", 21, 6)):
        topics.record_reply(chat, thread, message)
        assert topics.latest_reply(chat, thread) is None
