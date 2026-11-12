"""Unit tests for Oct 30 inbox and compact behaviours:
- keyboard_back() + _watch_buttons() in inbox (debounced)
- Auto compaction: auto_compact_limit, auto_compact in inbox, cold_compact, auto_compact_after_reply
- Menus: more menu split into session page and global page, close dialog, account menu per-session vs default
- Read-only CLI views Escape after capture, local context info tap
- Project picker cancel button and tmux:go one-tap button
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conftest import BoxedTmux

from ctb import accounts, compact, inbox as I
from ctb.config import Config, state_dir, write_private
from ctb.topics import TopicMap

OWNER = 12345


def cfg():
    return Config(bot_token="123456:ABCDEF", owner_chat_id=OWNER)


class TextMedia:
    def content_of(self, m):
        return m.get("text") or ""


def setup_inbox(tmp_path, bot):
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    thread = topics.ensure("sess-1", "Project: Task")
    box = I.Inbox(tmp_path / "state" / "inbox")
    return topics, box, thread


# --- Behaviour 3: keyboard_back() + _watch_buttons() -------------------------

def test_watch_buttons_triggers_keyboard_back(tmp_path, bot, monkeypatch):
    """Sending an inline_keyboard to a thread triggers keyboard_back for that thread."""
    called = []
    monkeypatch.setattr(I, "keyboard_back", lambda thread, **kw: called.append(thread))

    wrapped_call = I._watch_buttons(bot.call)
    wrapped_call("sendMessage", {
        "chat_id": OWNER,
        "message_thread_id": 42,
        "text": "Hello",
        "reply_markup": {"inline_keyboard": [[{"text": "btn", "callback_data": "x"}]]},
    })
    assert called == [42]

    # Non-inline keyboard or no message_thread_id does not trigger keyboard_back
    wrapped_call("sendMessage", {"chat_id": OWNER, "text": "Hello"})
    assert called == [42]


def test_keyboard_back_debounced(tmp_path, bot, monkeypatch):
    """keyboard_back resets any pending timer and calls mirror.status_message once."""
    status_called = []
    from ctb import mirror
    monkeypatch.setattr(mirror, "status_message", lambda *args, **kw: status_called.append(args))

    write_private(state_dir() / "telegram-status-text.json", json.dumps({"55": "footer status"}))

    timers = []
    class FakeTimer:
        def __init__(self, interval, function, args=None, kwargs=None):
            self.interval = interval
            self.function = function
            self.args = args or []
            self.kwargs = kwargs or {}
            self.cancelled = False
            self.daemon = False
            timers.append(self)

        def cancel(self):
            self.cancelled = True

        def start(self):
            pass

    monkeypatch.setattr(threading, "Timer", FakeTimer)
    monkeypatch.setattr(I.threading, "Timer", FakeTimer)

    # Trigger multiple times with short delay
    I.keyboard_back(55, delay=0.05, cfg=cfg(), bot=bot)
    I.keyboard_back(55, delay=0.05, cfg=cfg(), bot=bot)

    # Deterministically flush the active timer
    for t in timers:
        if not t.cancelled:
            t.function(*t.args, **t.kwargs)

    assert len(status_called) == 1
    assert status_called[0][1] == 55  # thread
    assert status_called[0][2] == "footer status"  # footer


# --- Behaviour 4: Auto compaction --------------------------------------------

def test_auto_compact_limit_cycling(tmp_path):
    """Limits step through [0, 200, 250, 300, 400, 600, 0]."""
    assert compact.auto_compact_limit() == 0
    compact.set_auto_compact_limit(0)
    assert compact.next_auto_compact_limit() == 200
    compact.set_auto_compact_limit(200)
    assert compact.next_auto_compact_limit() == 250
    compact.set_auto_compact_limit(250)
    assert compact.next_auto_compact_limit() == 300
    compact.set_auto_compact_limit(300)
    assert compact.next_auto_compact_limit() == 400
    compact.set_auto_compact_limit(400)
    assert compact.next_auto_compact_limit() == 600
    compact.set_auto_compact_limit(600)
    assert compact.next_auto_compact_limit() == 0


@pytest.mark.real_type_command
def test_auto_compact_inbox(tmp_path, bot, monkeypatch):
    """auto_compact sends Hebrew notification and triggers /compact command."""
    topics, box, thread = setup_inbox(tmp_path, bot)
    monkeypatch.setattr(I, "pane_for_session", lambda session: "%1")
    monkeypatch.setattr(I, "pane_text", lambda pane, lines=45: "idle prompt")
    monkeypatch.setattr(I, "session_busy", lambda session: False)
    tmux = BoxedTmux()  # an empty input box that the real gate types into
    monkeypatch.setattr(I, "_tmux", tmux)
    monkeypatch.setattr(I.time, "sleep", lambda s: None)
    followed = []
    monkeypatch.setattr(I, "_start_compact_progress", lambda *a, **k: followed.append(a))

    res = I.auto_compact("sess-1", 250_000, cold=False, cfg=cfg(), bot=bot, topics=topics, sleep_fn=lambda s: None)
    assert res is True
    assert tmux.sent == ["/compact"] and len(followed) == 1

    # Check Hebrew message sent
    messages = bot.methods("sendMessage")
    assert any("🗜️ דחיסה אוטומטית: ההקשר הגיע ל-250 אלף" in m["text"] for m in messages)

    # Cold compaction Hebrew note
    tmux.sent.clear()
    I.compact.marker("sess-1").unlink(missing_ok=True)
    I.compact.precompact_marker("sess-1").unlink(missing_ok=True)
    res_cold = I.auto_compact("sess-1", 200_000, cold=True, cfg=cfg(), bot=bot, topics=topics, sleep_fn=lambda s: None)
    assert res_cold is True
    assert any("🗜️ דחיסה אוטומטית לפני ההודעה שלך" in m["text"] for m in bot.methods("sendMessage"))


def test_cold_compact_conditions(tmp_path, monkeypatch):
    """cold_compact triggers compaction if idle > 1h, limit set, and ctx >= 150k."""
    session = "sess-cold"
    transcript = str(tmp_path / "transcript.jsonl")
    Path(transcript).write_text("{}", encoding="utf-8")

    from ctb import usage
    monkeypatch.setattr(usage, "context_tokens", lambda path: (160_000, None))

    # 1. Limit is 0 -> False
    compact.set_auto_compact_limit(0)
    assert compact.cold_compact(session, transcript, idle_since=time.time() - 4000) is False

    # 2. Limit is set, but idle < 1h -> False
    compact.set_auto_compact_limit(200)
    assert compact.cold_compact(session, transcript, idle_since=time.time() - 1000) is False

    # 3. Context < 150k -> False
    monkeypatch.setattr(usage, "context_tokens", lambda path: (140_000, None))
    assert compact.cold_compact(session, transcript, idle_since=time.time() - 4000) is False

    # 4. Meets conditions -> starts compaction and waits for marker
    monkeypatch.setattr(usage, "context_tokens", lambda path: (180_000, None))
    compact_called = []

    def mock_auto_compact(sess, ctx, cold):
        compact_called.append((sess, ctx, cold))
        compact.marker(sess).parent.mkdir(parents=True, exist_ok=True)
        compact.marker(sess).write_text(str(time.time()), encoding="utf-8")

    res = compact.cold_compact(session, transcript, idle_since=time.time() - 4000,
                               auto_compact_fn=mock_auto_compact, sleep=lambda s: None)
    assert res is True
    assert compact_called == [(session, 180_000, True)]


def test_auto_compact_after_reply(tmp_path, monkeypatch):
    """auto_compact_after_reply triggers if transcript tokens reach limit."""
    session = "sess-reply"
    transcript = str(tmp_path / "transcript.jsonl")
    Path(transcript).write_text("{}", encoding="utf-8")

    compact.set_auto_compact_limit(200)  # 200k tokens
    from ctb import usage
    monkeypatch.setattr(usage, "context_tokens", lambda path: (210_000, None))

    called = []
    res = compact.auto_compact_after_reply(session, transcript, auto_compact_fn=lambda s, ctx, cold: called.append((s, ctx, cold)))
    assert res is True
    assert called == [(session, 210_000, False)]


# --- Behaviour 7: Menus (more split + account split) ------------------------

def test_more_menu_session_and_global_pages(tmp_path, bot, monkeypatch):
    """More menu shows session page first; more:global and more:back switch pages in place."""
    topics, box, thread = setup_inbox(tmp_path, bot)

    # Initial more menu
    I.more_menu(bot, cfg(), {"message_thread_id": thread})
    sent = bot.methods("sendMessage")[-1]
    assert sent["text"] == I.MORE_TEXT
    buttons = [btn["callback_data"] for row in sent["reply_markup"]["inline_keyboard"] for btn in row]
    assert "more:global" in buttons
    assert "close:ask" in buttons
    assert "more:/account" in buttons

    # Tap more:global -> switches to global page in place
    q_global = {
        "id": "q1",
        "data": "more:global",
        "from": {"id": OWNER},
        "message": {"message_id": sent.get("message_id", 1), "chat": {"id": OWNER}, "message_thread_id": thread},
    }
    res = I.handle_button(q_global, cfg(), bot, topics, box)
    assert res == "menu"
    edited = bot.methods("editMessageText")[-1]
    assert edited["text"] == I.GLOBAL_TEXT
    global_buttons = [btn["callback_data"] for row in edited["reply_markup"]["inline_keyboard"] for btn in row]
    assert "more:back" in global_buttons
    assert "more:autocompact" in global_buttons
    assert "more:delivery" in global_buttons
    assert "more:voiceconfirm" in global_buttons
    assert "more:/account default" in global_buttons

    # Tap more:back -> switches back to session page
    q_back = dict(q_global, data="more:back")
    res2 = I.handle_button(q_back, cfg(), bot, topics, box)
    assert res2 == "menu"
    edited2 = bot.methods("editMessageText")[-1]
    assert edited2["text"] == I.MORE_TEXT


@pytest.mark.real_type_command
def test_more_menu_close_dialog(tmp_path, bot, monkeypatch):
    """close:ask prompts for confirmation; close:yes exits tmux pane and edits in place."""
    topics, box, thread = setup_inbox(tmp_path, bot)
    monkeypatch.setattr(I, "pane_for_session", lambda session: "%1")
    tmux_calls = []
    monkeypatch.setattr(I, "_tmux", BoxedTmux(lambda *args: tmux_calls.append(args) or MagicMock(returncode=0)))

    # Ask
    q_ask = {
        "id": "q2",
        "data": "close:ask",
        "from": {"id": OWNER},
        "message": {"message_id": 10, "chat": {"id": OWNER}, "message_thread_id": thread},
    }
    assert I.handle_button(q_ask, cfg(), bot, topics, box) == "close"
    edited = bot.methods("editMessageText")[-1]
    assert "🛑 לסגור את הסשן?" in edited["text"]

    # Confirm yes
    q_yes = dict(q_ask, data="close:yes")
    assert I.handle_button(q_yes, cfg(), bot, topics, box) == "close"
    for _ in range(60):  # the close runs off the poll loop now
        if "✅ הסשן נסגר" in bot.methods("editMessageText")[-1]["text"]:
            break
        time.sleep(0.05)
    assert any("/exit" in arg for call in tmux_calls for arg in call)
    edited_yes = bot.methods("editMessageText")[-1]
    assert "✅ הסשן נסגר" in edited_yes["text"]


def test_account_command_dual_mode(tmp_path, bot, monkeypatch):
    """Per-session menu has 📍 and moves this session; global menu has ⭐ and sets default."""
    monkeypatch.setattr(accounts, "names", lambda: ["work", "personal"])
    monkeypatch.setattr(accounts, "current", lambda: "work")
    monkeypatch.setattr(accounts, "account_for", lambda session=None: "personal" if session else "work")
    monkeypatch.setattr(accounts, "email", lambda name, **kw: f"{name}@example.com")
    monkeypatch.setattr(accounts, "auto_enabled", lambda: True)

    m = {"message_id": 20, "message_thread_id": 100, "chat": {"id": OWNER}}

    # 1. Per-session menu (session provided, rest is "")
    session_msgs = []
    I.account_command(cfg(), bot, m, "sess-1", "", lambda text, **kw: session_msgs.append((text, kw)))
    assert len(session_msgs) == 1
    text, kw = session_msgs[0]
    assert "👤 <b>חשבון לסשן הזה</b>" in text
    assert "📍" in text
    markup = kw["reply_markup"]["inline_keyboard"]
    # Account buttons are English-only labels to avoid phone RTL scrambling
    # personal has 📍 because accounts.account_for("sess-1") == "personal"
    btn_personal = [b for row in markup for b in row if "personal" in b["text"]][0]
    assert "📍" in btn_personal["text"]
    assert btn_personal["callback_data"] == "more:/account"
    btn_work = [b for row in markup for b in row if "work" in b["text"]][0]
    assert btn_work["callback_data"] == "more:/account move work"

    # 2. Global menu (rest is "default")
    global_msgs = []
    I.account_command(cfg(), bot, m, "sess-1", "default", lambda text, **kw: global_msgs.append((text, kw)))
    assert len(global_msgs) == 1
    g_text, g_kw = global_msgs[0]
    assert "🌐 <b>חשבון לסשנים חדשים</b>" in g_text
    assert "⭐" in g_text
    g_markup = g_kw["reply_markup"]["inline_keyboard"]
    btn_work_g = [b for row in g_markup for b in row if "work" in b["text"]][0]
    assert "⭐" in btn_work_g["text"]
    assert btn_work_g["callback_data"] == "more:/account use work"


# --- Behaviour 8: Read-only CLI views Escape & Context Info tap -------------

@pytest.mark.real_type_command
def test_readonly_cli_views_send_escape(tmp_path, bot, monkeypatch):
    """Read-only CLI views like /context, /usage get Escape sent after capture."""
    monkeypatch.setattr(I, "pane_for_session", lambda session: "%1")
    monkeypatch.setattr(I, "pane_text", lambda pane, lines=45: "CLI context output")
    monkeypatch.setattr(I, "session_busy", lambda session: False)
    tmux_calls = []
    monkeypatch.setattr(I, "_tmux", BoxedTmux(lambda *args: tmux_calls.append(args) or MagicMock(returncode=0)))

    # Synchronous execution of background worker for test
    monkeypatch.setattr(I, "_terminal_background", lambda session, action: action() or True)

    m = {"message_id": 30, "message_thread_id": 100, "chat": {"id": OWNER}}
    res = I.cli_command(cfg(), bot, m, "sess-1", "/context")
    assert res == "cli"
    # Ensure Escape was sent to pane
    escape_calls = [c for c in tmux_calls if "Escape" in c]
    assert len(escape_calls) >= 1
    assert escape_calls[0] == ("send-keys", "-t", "%1", "Escape")


def test_context_info_button_local_answer(tmp_path, bot, monkeypatch):
    """Tapping 📏 150 אלף answers locally without terminal CLI dispatch."""
    topics, box, thread = setup_inbox(tmp_path, bot)
    monkeypatch.setattr(I, "keyboard_back", lambda *args, **kw: None)

    msg = {
        "message_id": 40,
        "date": int(time.time()),
        "text": "📏 150 אלף",
        "message_thread_id": thread,
        "chat": {"id": OWNER, "type": "private"},
        "from": {"id": OWNER},
    }
    update = {"update_id": 100, "message": msg}
    res = I.handle(update, cfg(), bot, topics, box, TextMedia(), 0)
    assert res == "context"

    # Verify local answer content
    sent = bot.methods("sendMessage")[-1]
    assert "📏 גודל השיחה בזיכרון של קלוד" in sent["text"]
    assert "150 אלף" in sent["text"] and "מתוך" not in sent["text"]  # the window is not known: no made-up total
    assert "כשזה גדל, כדאי ללחוץ 🗜️ דחיסה." in sent["text"]


# --- Behaviour 11: Project picker cancel button & tmux:go -------------------

def test_project_picker_cancel_button(tmp_path, bot, monkeypatch):
    """Project picker cancel button (n:x or n:<token>:x) cancels and edits message."""
    topics, box, thread = setup_inbox(tmp_path, bot)
    monkeypatch.setattr(I, "projects", lambda *a, **k: ["/tmp/proj1", "/tmp/proj2"])

    # Launch picker
    picker_m = {"message_thread_id": 500, "message_id": 49, "text": "start new"}
    I.project_picker(cfg(), bot, picker_m, picker_m["text"])
    sent = bot.methods("sendMessage")[-1]
    markup = sent["reply_markup"]["inline_keyboard"]
    cancel_btn = [b for row in markup for b in row if "ביטול" in b["text"]][0]
    token = cancel_btn["callback_data"].split(":")[1]

    # Save pending project
    pending_path = state_dir() / "projects-pending.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    assert str(500) in pending

    # Tap cancel
    q_cancel = {
        "id": "q_c",
        "data": f"n:{token}:x",
        "from": {"id": OWNER},
        "message": {"message_id": sent["message_id"], "chat": {"id": OWNER}, "message_thread_id": 500},
    }
    res = I.handle_button(q_cancel, cfg(), bot, topics, box)
    assert res == "cancelled"

    # Pending removed
    pending_after = json.loads(pending_path.read_text(encoding="utf-8"))
    assert str(500) not in pending_after

    # Message edited
    edited = bot.methods("editMessageText")[-1]
    assert "✖️ בוטל. לא נפתח סשן." in edited["text"]


def test_tmux_go_one_tap_button(tmp_path, bot, monkeypatch):
    """When a session has no tmux pane, cli_command offers tmux:go button; tapping invokes move_to_tmux."""
    monkeypatch.setattr(I, "pane_for_session", lambda session: None)

    m = {"message_id": 60, "message_thread_id": 100, "chat": {"id": OWNER}}
    res = I.cli_command(cfg(), bot, m, "sess-1", "/screen")
    assert res == "unsupported"

    sent = bot.methods("sendMessage")[-1]
    assert "⚠️ הסשן רץ מחוץ למסך המנוהל" in sent["text"]
    btn = sent["reply_markup"]["inline_keyboard"][0][0]
    assert btn["text"] == "🔄 העבר עכשיו"
    assert btn["callback_data"] == "tmux:go"

    # Tap tmux:go
    moved = []
    monkeypatch.setattr(I, "move_to_tmux", lambda cfg, bot, m, session: moved.append(session))
    topics, box, thread = setup_inbox(tmp_path, bot)
    q_go = {
        "id": "q_go",
        "data": "tmux:go",
        "from": {"id": OWNER},
        "message": {"message_id": sent.get("message_id", 1), "chat": {"id": OWNER}, "message_thread_id": thread},
    }
    res_btn = I.handle_button(q_go, cfg(), bot, topics, box)
    assert res_btn == "tmux"
    time.sleep(0.05)
    assert moved == ["sess-1"]


def test_watch_buttons_ignores_other_chats(monkeypatch, bot):
    """Review of 3b3c59a: a button message in the team chat must not re-send the owner's status."""
    called = []
    monkeypatch.setattr(I, "keyboard_back", lambda thread, **kw: called.append(thread))
    wrapped_call = I._watch_buttons(bot.call, OWNER)
    markup = {"inline_keyboard": [[{"text": "btn", "callback_data": "x"}]]}
    wrapped_call("sendMessage", {"chat_id": -100777, "message_thread_id": 42, "text": "t", "reply_markup": markup})
    assert called == []
    wrapped_call("sendMessage", {"chat_id": OWNER, "message_thread_id": 42, "text": "t", "reply_markup": markup})
    assert called == [42]
