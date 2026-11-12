"""A task written in a forum group's General topic opens its own topic with the project picker."""
from ctb import inbox as I
from ctb.config import Config

GROUP = -1001234567890


def test_general_task_opens_topic_with_picker(tmp_path, bot, monkeypatch):
    monkeypatch.setattr(I, "projects", lambda *a, **k: ["/tmp/proj1", "/tmp/proj2"])
    cfg = Config(bot_token="123456:ABCDEF", owner_chat_id=42, chat_id=GROUP, ui_language="he")
    m = {"message_id": 7, "chat": {"id": GROUP}, "text": "לבנות דף נחיתה\nפרטים"}
    assert I.general_start(cfg, bot, m, m["text"]) == "picker"
    created = bot.methods("createForumTopic")[-1]
    assert created["name"] == "לבנות דף נחיתה"
    sends = bot.methods("sendMessage")
    picker = [p for p in sends if "inline_keyboard" in str(p.get("reply_markup"))][-1]
    assert picker.get("message_thread_id")
    guide = sends[-1]
    assert "message_thread_id" not in guide
    assert "https://t.me/c/1234567890/" in guide["text"]
    assert all(not ("https" in line and any("֐" <= c <= "׿" for c in line))
               for line in guide["text"].splitlines())


def test_private_chat_general_is_ignored(bot):
    cfg = Config(bot_token="123456:ABCDEF", owner_chat_id=42)
    assert I.general_start(cfg, bot, {"chat": {"id": 42}, "text": "hi"}, "hi") == "ignored"


def test_second_general_message_reuses_waiting_topic(tmp_path, bot, monkeypatch):
    monkeypatch.setattr(I, "projects", lambda *a, **k: ["/tmp/proj1"])
    cfg = Config(bot_token="123456:ABCDEF", owner_chat_id=42, chat_id=GROUP, ui_language="he")
    I.general_start(cfg, bot, {"message_id": 1, "chat": {"id": GROUP}, "text": "משימה"}, "משימה")
    assert I.general_start(cfg, bot, {"message_id": 2, "chat": {"id": GROUP}, "text": "ok"}, "ok") == "picker-waiting"
    assert len(bot.methods("createForumTopic")) == 1
    import json
    from ctb.config import state_dir
    pending = json.loads((state_dir() / "projects-pending.json").read_text())
    assert any(job["task"] == "משימה\nok" for job in pending.values())


def test_keyboard_fallback_when_footer_has_other_percent(monkeypatch):
    from ctb import mirror, usage
    monkeypatch.setattr(usage, "quota_line", lambda lang, **kw: "\n⏳ שבועית 41%")
    rows = mirror.live_keyboard("📏 253 אלף · Codex 51%", lang="he")["keyboard"]
    assert {"text": "📅 41%"} in rows[0]
