"""The pin board: one pinned message per session topic with links, a checklist and reminders, edited in
place, with buttons for the owner."""
import json
import re

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import board, cli, config as C, flood, hooks as H, i18n, inbox as I, telegram as T
from ctb.config import Config, state_dir
from ctb.topics import TopicMap, record_reply

OWNER = 7
GROUP = -1001234567890
SID = "sess-board-1"


class Bot(FakeBot):
    """FakeBot whose failures can carry a status code."""

    def __init__(self):
        super().__init__()
        self.codes = {}

    def call(self, method, params=None, timeout=15):
        if method in self.codes:
            self.calls.append((method, params or {}))
            raise T.TelegramError(self.fail.pop(method), self.codes.pop(method))
        return super().call(method, params, timeout)


@pytest.fixture
def bot():
    return Bot()


@pytest.fixture(autouse=True)
def roomy_budget(monkeypatch):
    monkeypatch.setattr(flood, "OPTIONAL_PER_MIN", 1000)  # the caps are under test here, not the flood budget


def make_cfg(**kw):
    base = dict(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP, target_language="en", ui_language="en")
    base.update(kw)
    return Config(**base)


@pytest.fixture
def cfg():
    return make_cfg()


@pytest.fixture
def thread(bot):
    return TopicMap(state_dir() / "topics.json", bot, GROUP).ensure(SID, "Demo")


def run(cfg, bot, action, text="", **kw):
    return board.run(cfg, action, session=SID, text=text, bot=bot, **kw)


def state():
    return json.loads((state_dir() / board.STATE_FILE).read_text(encoding="utf-8"))


def key(thread):
    return f"{GROUP}:{thread}"


def shown(bot):
    """The text of the board message as it is now (the last send or edit)."""
    return ([p for p in bot.calls if p[0] in ("sendMessage", "editMessageText")][-1][1])["text"]


def traffic(bot):
    """Every call except the one that made the topic."""
    return [c for c in bot.calls if c[0] != "createForumTopic"]


def test_add_remove_and_list(cfg, bot, thread, capsys):
    assert run(cfg, bot, "add", "Preview build", url="https://example.com/p") == 0
    assert run(cfg, bot, "check", "Review the diff") == 0
    assert run(cfg, bot, "remind", "Approve the release", due="until 18:00") == 0
    capsys.readouterr()
    assert run(cfg, bot, "list") == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["1. ⏰ Approve the release (until 18:00)", "2. ☐ Review the diff",
                   "3. 📎 Preview build https://example.com/p"]
    assert run(cfg, bot, "remove", n=2) == 0
    assert run(cfg, bot, "done", n=1) == 0  # clearing a reminder removes it
    run(cfg, bot, "list")
    assert capsys.readouterr().out.splitlines()[-1] == "1. 📎 Preview build https://example.com/p"
    assert run(cfg, bot, "remove", n=1) == 0
    run(cfg, bot, "list")
    assert "(the board is empty)" in capsys.readouterr().out
    assert "Nothing on the board yet." in shown(bot)  # the message stays, empty


def test_sections_are_shown_only_when_filled_in_the_fixed_order(cfg, bot, thread):
    run(cfg, bot, "add", "A link", url="https://example.com/a")
    text = shown(bot)
    assert "Links" in text and "Checklist" not in text and "Reminders" not in text
    run(cfg, bot, "check", "A task")
    run(cfg, bot, "remind", "A reminder")
    text = shown(bot)
    assert text.index("Reminders") < text.index("Checklist") < text.index("Links")
    body = [l for l in text.split("\n") if re.match(r"\d+\. ", l)]
    assert body[0].startswith("1. ⏰ A reminder") and body[1] == "2. ☐ A task"  # numbered across the board
    assert body[2] == '3. <a href="https://example.com/a">A link</a>'
    run(cfg, bot, "done", n=1)
    assert "Reminders" not in shown(bot)


def test_message_is_created_and_pinned_once_then_edited(cfg, bot, thread):
    for i in range(3):
        run(cfg, bot, "check", f"task {i}")
    sends = [p for p in bot.methods("sendMessage") if p.get("parse_mode") == "HTML"]
    assert len(sends) == 1 and sends[0]["message_thread_id"] == thread and sends[0]["disable_notification"] is True
    assert len(bot.methods("pinChatMessage")) == 1 and len(bot.methods("editMessageText")) == 2
    pin, edit = bot.methods("pinChatMessage")[0], bot.methods("editMessageText")[-1]
    assert pin["message_id"] == edit["message_id"] == state()[key(thread)]["id"] and pin["chat_id"] == GROUP
    assert not bot.methods("deleteMessage") and not bot.methods("unpinChatMessage")
    assert "task 0" in edit["text"] and "task 2" in edit["text"]


def test_state_key_and_private_file(cfg, bot, thread):
    run(cfg, bot, "check", "x")
    assert list(state()) == [f"{GROUP}:{thread}"]
    topic = state()[key(thread)]
    assert topic["pinned"] is True and topic["items"][0]["s"] == "x" and topic["items"][0]["k"] == "check"
    assert (state_dir() / board.STATE_FILE).stat().st_mode & 0o777 == 0o600


def test_link_cap_drops_the_oldest(cfg, bot, thread):
    for i in range(14):
        run(cfg, bot, "add", f"link {i}", url=f"https://example.com/{i}")
    titles = [i["s"] for i in state()[key(thread)]["items"]]
    assert titles == [f"link {i}" for i in range(2, 14)] and len(titles) == board.LIMITS["link"] == 12


def test_same_link_is_not_listed_twice(cfg, bot, thread):
    run(cfg, bot, "add", "first", url="https://example.com/x")
    run(cfg, bot, "add", "second", url="https://example.com/x")
    assert [i["s"] for i in state()[key(thread)]["items"]] == ["second"]


def test_check_cap_is_15_and_ticked_items_make_room(cfg, bot, thread, capsys):
    for i in range(15):
        assert run(cfg, bot, "check", f"task {i}") == 0
    capsys.readouterr()
    assert run(cfg, bot, "check", "one too many") == 1  # all open: nothing to drop
    assert "checklist is full" in capsys.readouterr().err
    run(cfg, bot, "done", n=3)  # task 2
    assert run(cfg, bot, "check", "fits now") == 0
    texts = [i["s"] for i in state()[key(thread)]["items"]]
    assert "task 2" not in texts and texts[-1] == "fits now" and len(texts) == 15


def test_reminder_cap_is_8(cfg, bot, thread, capsys):
    for i in range(8):
        assert run(cfg, bot, "remind", f"r{i}") == 0
    capsys.readouterr()
    assert run(cfg, bot, "remind", "r8") == 1 and "already 8 reminders" in capsys.readouterr().err
    assert len(state()[key(thread)]["items"]) == 8


def test_only_five_ticked_items_stay_the_oldest_ticked_go_first(cfg, bot, thread):
    for i in range(8):
        run(cfg, bot, "check", f"task {i}")
    topic = lambda: state()[key(thread)]
    for name in ("task 0", "task 1", "task 2", "task 3", "task 4", "task 5", "task 6"):
        n = [i["s"] for i in board.ordered(topic())].index(name) + 1
        assert run(cfg, bot, "done", n=n) == 0
    ticked = [i["s"] for i in topic()["items"] if i["x"]]
    assert ticked == ["task 2", "task 3", "task 4", "task 5", "task 6"]  # 0 and 1 went first
    assert "task 0" not in shown(bot) and "☑ <s>task 6</s>" in shown(bot)  # ticked items stay, struck through


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "ftp://example.com/x", "file:///etc/passwd", "tg://resolve?domain=x",
                                 "example.com/page", "https://", "https://user:pw@example.com/x", "https://a b.com",
                                 "mailto:a@b.c", "//example.com"])
def test_urls_other_than_http_and_tme_are_refused(cfg, bot, thread, capsys, bad):
    assert run(cfg, bot, "add", "title", url=bad) == 1
    err = capsys.readouterr().err
    assert err.startswith("Harnessgram: ") and err.count("\n") == 1 and ("http(s) and t.me" in err or "valid link" in err)
    assert not traffic(bot) and not (state_dir() / board.STATE_FILE).exists()


@pytest.mark.parametrize("good,expected", [("https://example.com/a?b=1#c", "https://example.com/a?b=1#c"),
                                           ("http://example.com", "http://example.com"),
                                           ("https://t.me/c/1/2/3", "https://t.me/c/1/2/3"),
                                           ("t.me/somechannel", "https://t.me/somechannel")])
def test_accepted_urls(good, expected):
    assert board.clean_url(good) == expected


def test_url_error_is_a_clear_one_liner(cfg, bot, thread, capsys):
    run(cfg, bot, "add", "t", url="ftp://x.com")
    err = capsys.readouterr().err
    assert err == "Harnessgram: only http(s) and t.me links can go on the board\n"


def test_titles_are_capped_cleaned_and_escaped(cfg, bot, thread):
    run(cfg, bot, "add", "  <b>x</b>\n‮ " + "w" * 200, url="https://example.com/a")
    item = state()[key(thread)]["items"][0]
    assert len(item["s"]) == board.TITLE_CHARS and item["s"].endswith("…") and "‮" not in item["s"] and "\n" not in item["s"]
    assert "<b>x</b>" not in shown(bot) and "&lt;b&gt;x&lt;/b&gt;" in shown(bot)
    assert board.clean_text("w" * 80, 80) == "w" * 80  # not hex: a long hex run is redacted like a token


def test_empty_title_is_refused(cfg, bot, thread, capsys):
    assert run(cfg, bot, "check", "  \n ") == 1 and "empty" in capsys.readouterr().err


def test_add_without_url_links_the_latest_reply(cfg, bot, thread, capsys):
    assert run(cfg, bot, "add", "Last reply") == 1 and "no reply" in capsys.readouterr().err
    record_reply(GROUP, thread, 321)
    assert run(cfg, bot, "add", "Last reply") == 0
    assert f'href="https://t.me/c/1234567890/{thread}/321"' in shown(bot)


def test_a_flood_skip_heals_on_the_next_add(cfg, bot, thread, capsys):
    run(cfg, bot, "check", "first")
    flood.record_429(GROUP, 60)
    assert run(cfg, bot, "check", "second") == 0  # saved, not shown yet
    assert "saved" in capsys.readouterr().out and len(bot.methods("editMessageText")) == 0
    assert [i["s"] for i in state()[key(thread)]["items"]] == ["first", "second"]
    (state_dir() / "flood.json").unlink()  # the gate opens
    run(cfg, bot, "check", "third")
    text = shown(bot)
    assert "first" in text and "second" in text and "third" in text


def test_a_skipped_creation_is_made_by_the_next_change(cfg, bot, thread):
    flood.record_429(GROUP, 60)
    assert run(cfg, bot, "check", "first") == 0
    assert not traffic(bot) and state()[key(thread)]["id"] == 0
    (state_dir() / "flood.json").unlink()
    run(cfg, bot, "check", "second")
    assert len(bot.methods("sendMessage")) == 1 and len(bot.methods("pinChatMessage")) == 1
    assert "first" in shown(bot) and "second" in shown(bot)


def test_every_call_goes_through_the_flood_budget(cfg, bot, thread, monkeypatch):
    monkeypatch.setattr(flood, "OPTIONAL_PER_MIN", 2)
    for i in range(5):
        run(cfg, bot, "check", f"t{i}")
    assert len(bot.methods("sendMessage")) + len(bot.methods("pinChatMessage")) + len(bot.methods("editMessageText")) == 2


def test_a_deleted_board_message_is_made_again(cfg, bot, thread):
    run(cfg, bot, "check", "a")
    bot.fail["editMessageText"] = "Bad Request: message to edit not found"
    bot.codes["editMessageText"] = 400
    run(cfg, bot, "check", "b")
    assert len(bot.methods("sendMessage")) == 2 and len(bot.methods("pinChatMessage")) == 2


def test_missing_pin_right_backs_off(cfg, bot, thread):
    bot.fail["pinChatMessage"] = "Bad Request: not enough rights to pin a message"
    bot.codes["pinChatMessage"] = 400
    assert run(cfg, bot, "check", "a") == 0
    assert state()[key(thread)]["pinned"] is False and state()[key(thread)]["pin_retry_at"] > 0
    run(cfg, bot, "check", "b")
    assert len(bot.methods("pinChatMessage")) == 1  # no retry inside the back-off
    assert "cannot pin the board" in (state_dir() / "mirror.log").read_text()


def test_no_topic_session_or_ready_bridge_exits_nonzero_with_one_line(cfg, bot, capsys, monkeypatch):
    assert board.run(cfg, "check", session="nobody", text="x", bot=bot) == 1  # no topic for this session
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "no Telegram topic" in err
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert board.run(cfg, "check", text="x", bot=bot) == 1 and "no session" in capsys.readouterr().err
    TopicMap(state_dir() / "topics.json", bot, GROUP).ensure(SID, "Demo")
    assert board.run(make_cfg(bot_token=""), "check", session=SID, text="x", bot=bot) == 1
    assert "missing in the config" in capsys.readouterr().err
    assert board.run(make_cfg(chat_id=0), "check", session=SID, text="x", bot=bot) == 1  # a private chat has no forum group
    assert not bot.methods("sendMessage") or all("Board" not in p["text"] for p in bot.methods("sendMessage"))


def test_session_from_the_environment_and_a_crash_never_escapes(cfg, bot, thread, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    assert board.run(cfg, "check", text="from env", bot=bot) == 0
    monkeypatch.setattr(board, "apply", lambda *a, **k: 1 / 0)
    assert board.run(cfg, "check", text="boom", bot=bot) == 1 and "not available" in capsys.readouterr().err


def test_refusals_for_wrong_numbers(cfg, bot, thread, capsys):
    run(cfg, bot, "add", "L", url="https://example.com/l")
    capsys.readouterr()
    assert run(cfg, bot, "done", n=1) == 1 and "link" in capsys.readouterr().err
    assert run(cfg, bot, "remove", n=5) == 1 and "no item 5" in capsys.readouterr().err
    run(cfg, bot, "check", "T")
    run(cfg, bot, "done", n=1)
    assert run(cfg, bot, "done", n=1) == 1 and "already ticked" in capsys.readouterr().err


def test_hebrew_board_has_hebrew_headers_and_isolated_english_titles(bot, thread):
    cfg = make_cfg(ui_language="he", target_language="he")
    run(cfg, bot, "add", "Preview build", url="https://example.com/p")
    run(cfg, bot, "check", "לבדוק את ה-PR")
    run(cfg, bot, "remind", "אישור release", due="עד 18:00")
    text = shown(bot)
    assert i18n.HE["board_header"] in text and i18n.HE["board_remind"] in text and i18n.HE["board_links"] in text
    assert ">⁨Preview build⁩</a>" in text
    assert "⁨לבדוק את ה-PR⁩" in text and "⁨אישור release⁩" in text
    assert all(l.startswith("‏") for l in text.split("\n") if re.match(r"‏?\d+\. ", l))
    en = make_cfg()
    assert "⁨" not in board.render(en, state()[key(thread)])
    assert board.render(en, state()[key(thread)]).startswith("<b>📌 Board</b>")


def test_strings_exist_in_both_languages():
    for name in ("header", "remind", "checks", "links", "empty", "btn_tick", "btn_clear", "toast_tick", "toast_clear",
                 "toast_stale"):
        assert i18n.EN["board_" + name] and i18n.HE["board_" + name]
    assert re.search("[֐-׿]", i18n.HE["board_btn_tick"]) is None  # a bare button: icon, number and text only


# --- the owner's buttons ------------------------------------------------------

class Tap:
    """Presses a board button the way Telegram delivers it."""

    def __init__(self, cfg, bot, thread, tmp_path, monkeypatch):
        self.cfg, self.bot, self.thread = cfg, bot, thread
        self.topics = TopicMap(state_dir() / "topics.json", bot, GROUP)
        self.box = I.Inbox(tmp_path / "inbox")
        self.nudged = []
        monkeypatch.setattr(I, "_deliver_button_word", lambda inbox, session, *a, **k: self.nudged.append(session))

    def message_id(self):
        return state()[key(self.thread)]["id"]

    def buttons(self):
        edit = [p for p in self.bot.calls if p[0] in ("sendMessage", "editMessageText") and "reply_markup" in p[1]][-1][1]
        return [b for row in edit["reply_markup"]["inline_keyboard"] for b in row]

    def press(self, data, sender=OWNER, mid=None, thread=None, chat=GROUP):
        q = {"id": "q", "from": {"id": sender}, "data": data,
             "message": {"message_id": mid or self.message_id(), "chat": {"id": chat, "type": "supergroup"},
                         "message_thread_id": thread or self.thread}}
        return I.handle_button(q, self.cfg, self.bot, self.topics, self.box)

    def queued(self):
        return [i["text"] for i in self.box.take(SID)]


@pytest.fixture
def tap(cfg, bot, thread, tmp_path, monkeypatch):
    return Tap(cfg, bot, thread, tmp_path, monkeypatch)


def test_buttons_are_reminders_first_then_open_checks_at_most_eight(cfg, bot, thread, tap):
    for i in range(9):
        run(cfg, bot, "check", f"task {i}")
    run(cfg, bot, "add", "L", url="https://example.com/l")
    run(cfg, bot, "remind", "wait for me")
    run(cfg, bot, "remind", "me too")
    run(cfg, bot, "done", n=4)  # tick task 1 (items: 2 reminders, 9 checks)
    labels = [b["text"] for b in tap.buttons()]
    assert len(labels) == board.MAX_BUTTONS == 8
    assert labels[0].startswith("✔️ 1 · wait for me") and labels[1].startswith("✔️ 2 · me too")
    assert labels[2].startswith("☑️ 3 · task 0") and not any("task 1" in l for l in labels)  # ticked: no button
    assert not any("example" in l or l.endswith("· L") for l in labels)
    for b in tap.buttons():
        assert re.fullmatch(r"bd:[0-9a-f]{8}:\d+", b["callback_data"]) and len(b["callback_data"].encode()) <= 64


def test_tapping_ticks_a_check_item_and_tells_the_session(cfg, bot, thread, tap):
    run(cfg, bot, "check", "Review the diff")
    run(cfg, bot, "check", "Other")
    button = tap.buttons()[0]
    assert tap.press(button["callback_data"]) == "board"
    items = state()[key(thread)]["items"]
    assert [i["x"] for i in items] == [True, False]
    assert "☑ <s>Review the diff</s>" in shown(bot)
    assert [b["text"] for b in tap.buttons()] == ["☑️ 2 · Other"]  # the ticked item lost its button
    assert tap.queued() == ["✅ owner ticked: Review the diff"] and tap.nudged == [SID]
    assert bot.methods("answerCallbackQuery")[-1]["text"] == "Ticked"


def test_tapping_clears_a_reminder(cfg, bot, thread, tap):
    run(cfg, bot, "remind", "Approve", due="today")
    assert tap.press(tap.buttons()[0]["callback_data"]) == "board"
    assert state()[key(thread)]["items"] == [] and "Reminders" not in shown(bot)
    assert bot.methods("editMessageText")[-1]["reply_markup"] == {"inline_keyboard": []}
    assert tap.queued() == ["✅ owner cleared reminder: Approve"]


def test_a_tap_is_answered_even_when_the_flood_gate_is_closed(cfg, bot, thread, tap):
    run(cfg, bot, "check", "a")
    data = tap.buttons()[0]["callback_data"]
    flood.record_429(GROUP, 60)
    assert tap.press(data) == "board" and "☑" in shown(bot)


def test_stale_taps_are_refused(cfg, bot, thread, tap):
    run(cfg, bot, "check", "a")
    run(cfg, bot, "check", "b")
    data = tap.buttons()[0]["callback_data"]
    assert tap.press(data) == "board"
    tap.queued()
    assert tap.press(data) == "ignored"  # already ticked
    assert bot.methods("answerCallbackQuery")[-1]["text"] == "This board has changed. Use the latest one."
    run(cfg, bot, "remove", n=1)
    assert tap.press(data) == "ignored"  # the item is gone
    old_message = tap.buttons()[0]["callback_data"]
    assert tap.press(old_message, mid=tap.message_id() + 99) == "ignored"  # not the board message
    assert tap.queued() == [] and tap.nudged == [SID]


def test_taps_from_others_or_with_bad_data_are_refused(cfg, bot, thread, tap):
    run(cfg, bot, "check", "a")
    data = tap.buttons()[0]["callback_data"]
    assert tap.press(data, sender=999) == "ignored"
    assert tap.press(data, chat=-1005550001) == "ignored"
    assert tap.press(data, thread=thread + 1) == "ignored"  # another topic: the hash and the session do not match
    for bad in ("bd:", "bd:zzzzzzzz:1", "bd:" + data.split(":")[1], f"bd:{data.split(':')[1]}:x", f"bd:{data.split(':')[1]}:1:2",
                f"bd:{'0' * 8}:1", "bd:" + data.split(":")[1] + ":99999999"):
        assert tap.press(bad) == "ignored", bad
    assert state()[key(thread)]["items"][0]["x"] is False and tap.queued() == [] and not tap.nudged
    assert tap.press(data) == "board"  # the real one still works


def test_no_buttons_outside_the_chats_the_owner_is_obeyed_in(bot, tap, thread):
    cfg = make_cfg()
    topic = {"id": 5, "items": [{"k": "check", "i": 1, "s": "x", "x": False, "at": 0.0}]}
    assert board.keyboard(cfg, GROUP, thread, topic) and board.keyboard(cfg, -1005550001, thread, topic) == []


def test_a_project_group_session_has_its_board_and_taps_in_its_own_group(bot, tmp_path, monkeypatch):
    orbit = -1001111111111
    cfg = C.from_dict({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "chat_id": GROUP, "target_language": "en",
                       "projects": [{"name": "Orbit", "chat_id": orbit}]})
    tmap = TopicMap(state_dir() / "topics.json", bot, GROUP, cfg=cfg)
    tmap.ensure("home-s", "Home work", thread=5)
    tmap.ensure("orbit-s", "Orbit: game", thread=5, chat=orbit)  # the same thread number in both groups
    box = I.Inbox(tmp_path / "inbox")
    nudged = []
    monkeypatch.setattr(I, "_deliver_button_word", lambda inbox, session, *a, **k: nudged.append(session))
    assert board.run(cfg, "remind", session="orbit-s", text="Approve", bot=bot) == 0
    assert board.run(cfg, "check", session="home-s", text="Home task", bot=bot) == 0
    assert list(state()) == [f"{orbit}:5", f"{GROUP}:5"]
    away = [p for m, p in bot.calls if m in ("sendMessage", "pinChatMessage") and p.get("chat_id") == orbit]
    assert len(away) == 2 and "Approve" in away[0]["text"] and away[0]["message_thread_id"] == 5
    assert all(p.get("chat_id") == GROUP for m, p in bot.calls if m in ("sendMessage", "pinChatMessage") and p not in away)
    markup = away[0]["reply_markup"]["inline_keyboard"][0][0]

    def tap(chat, sender=OWNER, data=markup["callback_data"], mid=None):
        mid = mid or state().get(f"{chat}:5", {}).get("id", 1)
        q = {"id": "q", "from": {"id": sender}, "data": data,
             "message": {"message_id": mid, "chat": {"id": chat, "type": "supergroup"}, "message_thread_id": 5}}
        return I.handle_button(q, cfg, bot, tmap, box)

    assert tap(GROUP) == "ignored"  # the hash belongs to the Orbit topic, not to thread 5 of the home group
    assert tap(orbit, sender=999) == "ignored" and tap(-1005550001) == "ignored"
    assert tap(orbit) == "board"
    edit = bot.methods("editMessageText")[-1]
    assert edit["chat_id"] == orbit and "Reminders" not in edit["text"]
    assert box.take("orbit-s")[0]["text"] == "✅ owner cleared reminder: Approve" and nudged == ["orbit-s"]
    assert box.take("home-s") == [] and state()[f"{GROUP}:5"]["items"][0]["x"] is False


def test_corrupt_state_is_ignored(cfg, bot, thread):
    (state_dir() / board.STATE_FILE).write_text('{"%s": {"items": [{"k": "x"}, 5, {"k": "link", "i": 1, "s": "t"}], "id": "no"}}'
                                                 % key(thread))
    assert run(cfg, bot, "check", "fine") == 0
    assert [i["s"] for i in state()[key(thread)]["items"]] == ["fine"]


# --- the command line and the hint ---------------------------------------------

def test_cli_parses_every_board_command(cfg, bot, thread, monkeypatch, capsys):
    monkeypatch.setattr(C, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(T.Bot, "from_config", classmethod(lambda cls, c, **k: bot))
    for args in (["add", "Title", "https://example.com/z"], ["check", "do it"], ["remind", "wait", "--due", "until 18:00"]):
        assert cli.main(["board", *args, "--session", SID]) == 0, args
    assert cli.main(["board", "done", "2", "--session", SID]) == 0
    assert cli.main(["board", "list", "--session", SID]) == 0
    out = capsys.readouterr().out
    assert "1. ⏰ wait (until 18:00)" in out and "2. ☑ do it" in out and "3. 📎 Title https://example.com/z" in out
    assert cli.main(["board", "add", "t", "ftp://x.com", "--session", SID]) == 1
    assert cli.main(["board", "remove", "9", "--session", SID]) == 1
    with pytest.raises(SystemExit):
        cli.main(["board"])


def test_hint_opens_the_session_and_rides_with_the_topic_check(tmp_path):
    group = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP, auto_topic_names=False)
    first = H.topic_check_hint("fresh", group)
    assert first == H.BOARD_HINT and "ctb board add" in first and "ctb board check" in first and "ctb board remind" in first
    assert [H.topic_check_hint("fresh", group) for _ in range(6)] == [None] * 6  # prompts 2 to 7
    TopicMap(state_dir() / "topics.json", FakeBot(), GROUP).ensure("fresh", "demo: task")
    eighth = H.topic_check_hint("fresh", group)
    assert eighth.startswith("Topic check:") and eighth.endswith(H.BOARD_HINT)
    private = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)
    assert H.topic_check_hint("solo", private) is None  # no forum group: no board
    out = H.prompt_output({"session_id": "third", "prompt": "hi"}, group)
    assert "Pin board:" in out["hookSpecificOutput"]["additionalContext"]


def test_the_hint_teaches_the_card_commands():
    from ctb import hooks
    for kind in ("progress", "choice", "ask", "link", "numbers", "close"):
        assert f"ctb card {kind}" in hooks.BOARD_HINT
    assert "approves exactly what its question names" in hooks.BOARD_HINT
