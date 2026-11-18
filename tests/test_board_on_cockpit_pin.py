"""The board and live waiting count share the topic's Cockpit pin."""
import json

from conftest import GOOD_TOKEN, FakeBot
from ctb import board, panel_pin
from ctb.telegram import TelegramError
from ctb.config import Config, state_dir, write_private

CHAT = -1001234567890
THREAD = 10
PIN = 555


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=CHAT, chat_id=CHAT,
                  target_language="en", ui_language="en", panel_button=True)


def place(config, **extra):
    panel_pin._username[config.bot_token] = "DemoBot"
    write_private(state_dir() / "panel-pins.json", json.dumps({
        f"{CHAT}:{THREAD}": dict(id=PIN, v=panel_pin.VERSION, pinned=True, count=0, **extra)}))


def add(config, bot, text):
    def change(topic):
        topic["items"].append(dict(k="remind", i=topic["next"], s=text, at=1.0, x=False))
        topic["next"] += 1
    return board.apply(config, bot, CHAT, THREAD, change)


def test_board_edits_cockpit_pin_without_pin_churn():
    config, bot = cfg(), FakeBot()
    place(config)
    assert add(config, bot, "Call the bank")[1]
    edit = bot.methods("editMessageText")[-1]
    assert edit["message_id"] == PIN and "Call the bank" in edit["text"]
    assert edit["reply_markup"]["inline_keyboard"][0][0]["url"].startswith("https://t.me/DemoBot")
    assert not bot.methods("sendMessage") and not bot.methods("pinChatMessage")


def test_deleted_cockpit_pin_is_marked_for_replacement():
    config, bot = cfg(), FakeBot()
    place(config)
    bot.fail["editMessageText"] = TelegramError("Bad Request: message to edit not found", 400)
    assert not add(config, bot, "Call the bank")[1]
    assert board.view(CHAT, THREAD)["gone"] == PIN
    assert panel_pin.pin_message(CHAT, THREAD) == 0


def test_frozen_cockpit_pin_keeps_the_standalone_board_visible():
    config, bot = cfg(), FakeBot()
    place(config, frozen=True)
    assert add(config, bot, "Call the bank")[1]
    assert panel_pin.pin_message(CHAT, THREAD) == 0
    assert "Call the bank" in bot.methods("sendMessage")[-1]["text"]


def test_oversize_pin_drops_links_but_keeps_reminders_and_checks():
    config = cfg()
    topic = board._blank()
    topic["items"] = [
        {"k": "remind", "i": 1, "s": "Call the bank", "at": 1.0, "x": False},
        {"k": "check", "i": 2, "s": "Approve invoice", "at": 1.0, "x": False},
        *[{"k": "link", "i": n + 3, "s": "Reference", "u": "https://example.com/" + "x" * 900,
           "at": 1.0, "x": False} for n in range(4)],
    ]
    content = board.pin_content(config, CHAT, THREAD, "Cockpit", [], topic)
    assert "Call the bank" in content["text"] and "Approve invoice" in content["text"]
    assert "example.com" not in content["text"] and len(content["text"]) <= board.PIN_TEXT_MAX
    assert "4 links in the Cockpit" in content["text"]


def test_oversize_pin_summarizes_dropped_links_in_hebrew():
    config = cfg()
    config.ui_language = "he"
    topic = board._blank()
    topic["items"] = [{"k": "link", "i": n, "s": "Reference",
                       "u": "https://example.com/" + "x" * 900, "at": 1.0, "x": False}
                      for n in range(1, 5)]
    text = board.pin_content(config, CHAT, THREAD, "Cockpit", [], topic)["text"]
    assert "\u200f🔗 4 קישורים בקוקפיט" in text


def test_sync_replaces_the_old_board_with_a_link_before_unpinning():
    config, bot = cfg(), FakeBot()
    place(config)
    topic = board._blank()
    topic.update(id=77, pinned=True, next=2,
                 items=[{"k": "check", "i": 1, "s": "Ship it", "at": 1.0, "x": False}])
    write_private(state_dir() / board.STATE_FILE, json.dumps({f"{CHAT}:{THREAD}": topic}))

    assert board.apply(config, bot, CHAT, THREAD, lambda row: None)[1]

    calls = [(method, params) for method, params in bot.calls
             if method in ("editMessageText", "unpinChatMessage")]
    assert [method for method, _ in calls] == ["editMessageText", "editMessageText", "unpinChatMessage"]
    assert calls[1][1]["message_id"] == 77 and f"/{THREAD}/{PIN}" in calls[1][1]["text"]
    assert board.board_message(CHAT, THREAD) == 0


def test_sync_keeps_the_old_board_when_its_link_edit_fails():
    class OldEditFails(FakeBot):
        def call(self, method, params=None, timeout=15):
            if method == "editMessageText" and params["message_id"] == 77:
                self.calls.append((method, params))
                raise TelegramError("Bad Gateway", 502)
            return super().call(method, params, timeout)

    config, bot = cfg(), OldEditFails()
    place(config)
    topic = board._blank()
    topic.update(id=77, pinned=True, next=2,
                 items=[{"k": "check", "i": 1, "s": "Ship it", "at": 1.0, "x": False}])
    write_private(state_dir() / board.STATE_FILE, json.dumps({f"{CHAT}:{THREAD}": topic}))

    assert not board.apply(config, bot, CHAT, THREAD, lambda row: None)[1]
    assert board.board_message(CHAT, THREAD) == 77
    assert not bot.methods("unpinChatMessage")


def test_migrate_old_forgets_a_deleted_standalone_board():
    config, bot = cfg(), FakeBot()
    topic = board._blank()
    topic.update(id=77, pinned=True, onpin=True)
    write_private(state_dir() / board.STATE_FILE, json.dumps({f"{CHAT}:{THREAD}": topic}))
    bot.fail["editMessageText"] = TelegramError("Bad Request: message to edit not found", 400)

    board.migrate_old(config, bot, CHAT, THREAD, PIN, now=1000)

    saved = board.view(CHAT, THREAD)
    assert saved["id"] == 0 and saved["pinned"] is False
    board.migrate_old(config, bot, CHAT, THREAD, PIN, now=2000)
    assert len(bot.methods("editMessageText")) == 1
