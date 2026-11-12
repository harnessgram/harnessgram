"""Terminal hotfixes preserve private-topic routing and app-only approval authority."""
import json
import subprocess

import pytest

from conftest import BoxedTmux

pytestmark = pytest.mark.real_type_command  # the real gate (BoxedTmux gives it a box)

from ctb import inbox as I
from ctb.config import Config, state_dir, write_private
from ctb.topics import TopicMap

OWNER = 7


def setup(bot):
    cfg = Config(owner_chat_id=OWNER)
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure("session", "Title")
    write_private(state_dir() / "tmux-panes.json", json.dumps({"session": "%12"}))
    return cfg, topics, thread, I.Inbox(state_dir() / "inbox")


def message(text, thread):
    return {"message_id": 44, "message_thread_id": thread, "date": 2000000000,
            "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}, "text": text}


def terminal(monkeypatch, body="❯"):
    calls = []
    monkeypatch.setattr(I, "_terminal_background", lambda session, action: (action(), True)[1])
    monkeypatch.setattr(I, "pane_text", lambda pane: body)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    def tmux(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "")
    monkeypatch.setattr(I, "_tmux", BoxedTmux(tmux))
    return calls


def test_commands_only_target_owner_known_private_topic(bot, monkeypatch):
    cfg, topics, thread, box = setup(bot)
    calls = terminal(monkeypatch)
    m = message("/review", thread)
    assert I.handle({"message": m}, cfg, bot, topics, box, None, 0) == "cli"
    keys = [c for c in calls if c[0] == "send-keys"]  # the box is read first (capture-pane), then typed and sent
    assert keys == [("send-keys", "-t", "%12", "-l", "/review"), ("send-keys", "-t", "%12", "Enter")]
    calls.clear()
    for field, replacement in (("from", {"id": 99}), ("chat", {"id": OWNER, "type": "supergroup"}),
                               ("message_thread_id", 9999)):
        forged = dict(m, **{field: replacement})
        # the owner's own unknown topic gets the project picker, never the terminal
        expected = "picker" if field == "message_thread_id" else "ignored"
        assert I.handle({"message": forged}, cfg, bot, topics, box, None, 0) == expected
    assert not calls and not box.pending("session")


@pytest.mark.parametrize("text", ["/permissions", "/allowed_tools", "/LOGIN@the_bot", "/compact\n/permissions",
                                  "/context\x1b[A", "/context token=" + "a" * 24])
def test_blocked_and_injected_terminal_commands_never_send(bot, monkeypatch, text):
    cfg, topics, thread, box = setup(bot)
    calls = terminal(monkeypatch)
    assert I.handle({"message": message(text, thread)}, cfg, bot, topics, box, None, 0) == "refused"
    assert not calls


def test_any_valid_slash_command_and_remote_alias_are_preserved(bot, monkeypatch):
    cfg, topics, thread, box = setup(bot)
    calls = terminal(monkeypatch)
    for text in ("/custom-skill check this", "/remote_control"):
        I.handle({"message": message(text, thread)}, cfg, bot, topics, box, None, 0)
    keys = [c for c in calls if c[0] == "send-keys"]
    assert keys[0][-1] == "/custom-skill check this"
    assert keys[2][-1] == "/remote-control"


def test_plain_numbered_prose_and_sensitive_dialog_have_no_choice_buttons():
    for body in ("1. First\n2. Next", "Select model\nAllow this tool?\n❯ 1. Yes\n  2. No",
                 "Do you want to proceed?\n❯ 1. Yes\n  2. No"):
        assert not any(b["callback_data"].startswith("k:1") for row in I.menu_buttons(body)["inline_keyboard"] for b in row)
    body = "Select model\n❯ 1. Sonnet\n  2. Opus"
    buttons = I.menu_buttons(body)["inline_keyboard"]
    assert buttons[0][0]["text"] == "1. Sonnet"
    assert buttons[0][0]["callback_data"] == "k:1:" + I._screen_stamp(body)


def test_stale_and_sensitive_callback_keys_cannot_approve(bot, monkeypatch):
    cfg, _, thread, _ = setup(bot)
    calls = terminal(monkeypatch, "Select model\n❯ 1. Sonnet\n  2. Opus")
    m = message("", thread)
    for data in ("k:1", "k:1:old"):
        I.keypad_press({"id": "q", "data": data}, cfg, bot, m, "session")
    assert not calls
    body = "Permission: allow Bash?\n❯ 1. Yes\n  2. No"
    monkeypatch.setattr(I, "pane_text", lambda pane: body)
    I.keypad_press({"id": "q", "data": "k:1:" + I._screen_stamp(body)}, cfg, bot, m, "session")
    assert not calls
    I.keypad_press({"id": "q", "data": "k:esc"}, cfg, bot, m, "session")
    assert calls == [("send-keys", "-t", "%12", "Escape")]


def test_safe_current_selection_works_and_edit_does_not_lose_topic(bot, monkeypatch):
    cfg, _, thread, _ = setup(bot)
    body = "Select model\n❯ 1. Sonnet\n  2. Opus"
    calls = terminal(monkeypatch, body)
    I.keypad_press({"id": "q", "data": "k:2:" + I._screen_stamp(body)}, cfg, bot, message("", thread), "session")
    assert calls == [("send-keys", "-t", "%12", "2")]
    assert bot.methods("editMessageText")[-1]["message_id"] == 44


def test_terminal_commands_and_manual_enter_refuse_approval_prompt(bot, monkeypatch):
    cfg, topics, thread, box = setup(bot)
    calls = terminal(monkeypatch, "Do you want to proceed?\n❯ 1. Yes\n  2. No")
    for text in ("/compact", "/key enter", "/key 1"):
        I.handle({"message": message(text, thread)}, cfg, bot, topics, box, None, 0)
    assert not calls


def test_pane_targets_are_exact_and_capture_redacts(monkeypatch):
    write_private(state_dir() / "tmux-panes.json", json.dumps({"bad": "-a", "expr": "session:0", "good": "%12"}))
    assert I.pane_for_session("bad") is None and I.pane_for_session("expr") is None
    assert I.pane_for_session("good") == "%12"
    secret = "ghp_" + "A" * 30
    monkeypatch.setattr(I, "_tmux", lambda *args: subprocess.CompletedProcess(args, 0, "  " + secret + "\n────────\n❯"))
    assert secret not in I.pane_text("%12")
    assert "[redacted]" in I.pane_text("%12")


def test_commands_scoped_to_owner_and_diagnostic_file_private(bot):
    I.set_commands(bot, Config(owner_chat_id=OWNER))
    commands = bot.methods("setMyCommands")
    assert all(p["scope"] == {"type": "chat", "chat_id": OWNER} for p in commands)
    assert {"screen", "context", "remote_control", "key", "cli"} <= {c["command"] for c in commands[0]["commands"]}
    I.poll_log("poll failed #1: TelegramError")
    path = state_dir() / "poll.log"
    assert path.stat().st_mode & 0o777 == 0o600
    assert "TelegramError" in path.read_text()
