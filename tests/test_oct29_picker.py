from types import SimpleNamespace

import pytest

from ctb import inbox as I
from ctb.config import Config
from ctb.topics import TopicMap


@pytest.fixture
def context(tmp_path, bot, monkeypatch):
    cfg = Config(owner_chat_id=7)
    topics = TopicMap(tmp_path / "topics.json", bot, 7)
    box = I.Inbox(tmp_path / "box")
    monkeypatch.setattr(I, "projects", lambda *a, **k: ["/projects/one", "/projects/general"])
    msg = {"chat": {"id": 7, "type": "private"}, "from": {"id": 7},
           "message_thread_id": 42, "message_id": 12, "date": 2000000000}
    media = SimpleNamespace(content_of=lambda m: m.get("text", ""))
    return cfg, topics, box, msg, media


@pytest.mark.parametrize("text", ["do this", "/compact"])
def test_new_topic_picker_and_chosen(context, bot, monkeypatch, text):
    cfg, topics, box, msg, media = context
    assert I.handle({"message": dict(msg, text=text)}, cfg, bot, topics, box, media, 0) == "picker"
    button = bot.methods("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0][0]
    assert len(button["callback_data"].encode()) <= 64
    calls = []
    # the first task is typed once the prompt is up ("❯"), so the screen is polled before typing
    monkeypatch.setattr(I, "_tmux", lambda *args: calls.append(args) or SimpleNamespace(returncode=0, stdout="❯ "))
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(I.threading, "Thread", ImmediateThread)
    q = {"id": "q", "from": {"id": 7}, "message": msg, "data": button["callback_data"]}
    assert I.handle_button(q, cfg, bot, topics, box) == "picker"
    assert topics.session_for_thread(42)
    calls[:] = [c for c in calls if c[0] != "capture-pane"]
    assert calls[0][0] == "new-session" and calls[0][calls[0].index("-c") + 1] == "/projects/one"
    assert calls[1][-1] == text and calls[2][-1] == "Enter"
    assert I.handle_button(q, cfg, bot, topics, box) == "ignored"


def test_skill_and_non_topic(context, bot):
    cfg, topics, box, msg, media = context
    I._skill_pick(cfg, bot, msg, "example")
    assert I.handle({"message": dict(msg, text="work")}, cfg, bot, topics, box, media, 0) == "picker"
    import json
    assert json.loads((I.state_dir() / "projects-pending.json").read_text())["42"]["task"] == "/example work"
    main = dict(msg, message_thread_id=None)
    I._skill_pick(cfg, bot, main, "example")
    assert I.handle({"message": dict(main, text="work")}, cfg, bot, topics, box, media, 0) == "unsupported"


def test_picker_auth_stale_and_refused(context, bot, monkeypatch):
    cfg, topics, box, msg, media = context
    assert I.handle({"message": dict(msg, text="first")}, cfg, bot, topics, box, media, 0) == "picker"
    old = bot.methods("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert I.handle({"message": dict(msg, text="second")}, cfg, bot, topics, box, media, 0) == "picker"
    current = bot.methods("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    monkeypatch.setattr(I, "_tmux", lambda *args: pytest.fail("unexpected launch"))
    q = {"id": "q", "from": {"id": 8}, "message": msg, "data": current}
    assert I.handle_button(q, cfg, bot, topics, box) == "ignored"
    q.update({"from": {"id": 7}, "data": old})
    assert I.handle_button(q, cfg, bot, topics, box) == "ignored"
    assert I.handle({"message": dict(msg, text="x" * (I.MAX_TEXT + 1))}, cfg, bot, topics, box, media, 0) == "refused"
    assert not topics.session_for_thread(42)
