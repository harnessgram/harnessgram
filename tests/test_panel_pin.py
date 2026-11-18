"""The control-panel button is pinned once in every session topic (General's pin is invisible there)."""
import pytest

from conftest import GOOD_TOKEN, FakeBot
import json

from ctb import flood, panel, panel_pin, telegram as T
from ctb.config import Config, state_dir
from ctb.topics import TopicMap

GROUP = -1001234567890
REAL_ACTIVE = panel_pin._active


def test_removed_legacy_button_api_is_not_exported():
    assert not hasattr(panel_pin, "button")


class Bot(FakeBot):
    def __init__(self):
        super().__init__()
        self.codes = {}

    def call(self, method, params=None, timeout=15):
        if method == "getMe":
            self.calls.append((method, {}))
            return {"username": "DemoBot"}
        if method in self.codes:
            self.calls.append((method, params or {}))
            raise T.TelegramError(self.fail.pop(method), self.codes.pop(method))
        return super().call(method, params, timeout)


@pytest.fixture(autouse=True)
def roomy(monkeypatch):
    monkeypatch.setattr(flood, "OPTIONAL_PER_MIN", 1000)
    monkeypatch.setattr(panel_pin, "_active", lambda topics, now: {
        f"{e['chat']}:{e['thread']}" for e in topics.all_places().values()})
    panel_pin._username.clear()


def setup(n):
    bot = Bot()
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=7, chat_id=GROUP, target_language="en", ui_language="en",
                 panel_button=True)
    topics = TopicMap(state_dir() / "topics.json", bot, GROUP)
    threads = [topics.ensure(f"s{i}", f"Demo {i}") for i in range(n)]
    bot.calls.clear()
    return cfg, bot, topics, threads


def test_each_topic_gets_one_pinned_panel_button():
    cfg, bot, topics, threads = setup(2)
    assert panel_pin.run(cfg, bot, topics, now=1000) == 2
    sends = bot.methods("sendMessage")
    assert sorted(p["message_thread_id"] for p in sends) == sorted(threads)
    for p in sends:  # each topic's button opens the panel on that topic
        url = p["reply_markup"]["inline_keyboard"][0][0]["url"]
        assert url == f"https://t.me/DemoBot?startapp={panel.topic_param(GROUP, p['message_thread_id'])}"
    assert len(bot.methods("pinChatMessage")) == 2
    bot.calls.clear()
    assert panel_pin.run(cfg, bot, topics, now=2000) == 0  # placed once, never again
    assert not bot.methods("sendMessage") and not bot.methods("pinChatMessage")


def test_backfill_is_spread_over_rounds():
    cfg, bot, topics, _ = setup(panel_pin.PER_ROUND + 2)
    assert panel_pin.run(cfg, bot, topics, now=1000) == panel_pin.PER_ROUND
    assert panel_pin.run(cfg, bot, topics, now=1060) == 2


def test_refused_pin_is_retried_later_without_a_second_message():
    cfg, bot, topics, _ = setup(1)
    bot.fail["pinChatMessage"], bot.codes["pinChatMessage"] = "Bad Request: not enough rights", 400
    panel_pin.run(cfg, bot, topics, now=1000)
    assert len(bot.methods("sendMessage")) == 1
    bot.calls.clear()
    panel_pin.run(cfg, bot, topics, now=1060)
    assert not bot.calls  # waiting for the retry time
    panel_pin.run(cfg, bot, topics, now=1000 + panel_pin.RETRY + 1)
    assert not bot.methods("sendMessage") and len(bot.methods("pinChatMessage")) == 1


def test_flood_gate_closed_stops_the_round(monkeypatch):
    cfg, bot, topics, _ = setup(2)
    gate = {"open": False}
    real = flood.allow_optional
    monkeypatch.setattr(flood, "allow_optional", lambda *a, **k: gate["open"] and real(*a, **k))
    assert panel_pin.run(cfg, bot, topics, now=1000) == 0
    gate["open"] = True
    assert panel_pin.run(cfg, bot, topics, now=1060) == 2


def test_old_shared_link_is_edited_once_to_the_topic_link():
    cfg, bot, topics, threads = setup(1)
    (state_dir() / "panel-pins.json").write_text(json.dumps({f"{GROUP}:{threads[0]}": {"id": 55, "pinned": True}}))
    assert panel_pin.run(cfg, bot, topics, now=1000) == 1
    edit = bot.methods("editMessageText")[0]
    assert edit["message_id"] == 55 and panel.topic_param(GROUP, threads[0]) in \
        edit["reply_markup"]["inline_keyboard"][0][0]["url"]
    assert not bot.methods("sendMessage") and not bot.methods("pinChatMessage")
    bot.calls.clear()
    assert panel_pin.run(cfg, bot, topics, now=1060) == 0 and not bot.calls


def test_unmodified_edit_counts_as_done():
    cfg, bot, topics, threads = setup(1)
    (state_dir() / "panel-pins.json").write_text(json.dumps({f"{GROUP}:{threads[0]}": {"id": 55, "pinned": True}}))
    bot.fail["editMessageText"], bot.codes["editMessageText"] = "Bad Request: message is not modified", 400
    panel_pin.run(cfg, bot, topics, now=1000)
    bot.calls.clear()
    assert panel_pin.run(cfg, bot, topics, now=1060) == 0 and not bot.calls


def test_old_topics_are_left_alone(monkeypatch):
    """Posting bumps a topic to the top of the group: only topics in use get the button."""
    cfg, bot, topics, threads = setup(2)
    monkeypatch.setattr(panel_pin, "_active", lambda topics, now: {f"{GROUP}:{threads[1]}"})
    assert panel_pin.run(cfg, bot, topics, now=1000) == 1
    assert [p["message_thread_id"] for p in bot.methods("sendMessage")] == [threads[1]]


def test_active_skips_ended_and_closed_sessions(monkeypatch):
    from ctb import sessions_menu
    E = sessions_menu.Entry
    cfg, bot, topics, _ = setup(0)
    monkeypatch.setattr(sessions_menu, "collect", lambda topics, now=None: [
        E("a", "A", 1, 10.0, "working", False), E("b", "B", 2, 60.0, "ended", False),
        E("c", "C", 3, panel_pin.ACTIVE_AGE + 1, "ended", False), E("d", "D", 4, 5.0, "idle", True),
        E("e", "E", 5, None, "ended", False), E("f", "F", 6, 5.0, "idle", False, -100777)])
    assert REAL_ACTIVE(topics, 0) == {f"{GROUP}:1", f"{GROUP}:2", "-100777:6"}


@pytest.mark.parametrize("code", [500, 502, None])
def test_a_transient_edit_failure_is_retried_and_never_marks_the_old_button_current(code):
    """Review of #136: any failed edit of an old button marked it current for good, so a Telegram 5xx
    left the shared link in that topic forever."""
    cfg, bot, topics, threads = setup(1)
    key = f"{GROUP}:{threads[0]}"
    (state_dir() / "panel-pins.json").write_text(json.dumps({key: {"id": 55, "pinned": True}}))
    bot.fail["editMessageText"], bot.codes["editMessageText"] = "Internal Server Error", code
    panel_pin.run(cfg, bot, topics, now=1000)
    assert json.loads((state_dir() / "panel-pins.json").read_text())[key].get("v") != panel_pin.VERSION
    bot.calls.clear()
    assert panel_pin.run(cfg, bot, topics, now=1060) == 0 and not bot.calls  # not at once
    assert panel_pin.run(cfg, bot, topics, now=1000 + panel_pin.TRANSIENT_RETRY + 1) == 1
    assert bot.methods("editMessageText")[0]["message_id"] == 55 and not bot.methods("sendMessage")
    assert json.loads((state_dir() / "panel-pins.json").read_text())[key]["v"] == panel_pin.VERSION


def test_a_button_telegram_refuses_to_edit_for_good_is_left_as_it_is():
    cfg, bot, topics, threads = setup(1)
    (state_dir() / "panel-pins.json").write_text(json.dumps({f"{GROUP}:{threads[0]}": {"id": 55, "pinned": True}}))
    bot.fail["editMessageText"], bot.codes["editMessageText"] = "Bad Request: message can't be edited", 400
    panel_pin.run(cfg, bot, topics, now=1000)
    assert panel_pin.pin_message(GROUP, threads[0]) == 0
    bot.calls.clear()
    assert panel_pin.run(cfg, bot, topics, now=1000 + panel_pin.RETRY + 1) == 0 and not bot.calls


# --- on demand: the keyboard's panel button and /panel (2026-11-10: some topics show no panel) ---

def test_send_link_pins_in_a_topic_that_has_none_and_run_then_skips_it():
    cfg, bot, topics, threads = setup(1)
    assert panel_pin.send_link(cfg, bot, GROUP, 555)  # an old topic with no session and no pinned button
    (sent,) = bot.methods("sendMessage")
    assert sent["message_thread_id"] == 555
    assert sent["reply_markup"]["inline_keyboard"][0][0]["url"] == \
        f"https://t.me/DemoBot?startapp={panel.topic_param(GROUP, 555)}"
    assert len(bot.methods("pinChatMessage")) == 1
    bot.calls.clear()
    assert panel_pin.send_link(cfg, bot, GROUP, 555)  # asked again: a shortcut message, no second pin
    assert len(bot.methods("sendMessage")) == 1 and not bot.methods("pinChatMessage")


def test_send_link_in_a_topic_already_pinned_does_not_pin_again():
    cfg, bot, topics, threads = setup(1)
    panel_pin.run(cfg, bot, topics, now=1000)
    bot.calls.clear()
    assert panel_pin.send_link(cfg, bot, GROUP, threads[0])
    assert len(bot.methods("sendMessage")) == 1 and not bot.methods("pinChatMessage")


def test_send_link_in_general_opens_the_overview():
    cfg, bot, topics, threads = setup(0)
    assert panel_pin.send_link(cfg, bot, GROUP, None)
    (sent,) = bot.methods("sendMessage")
    assert "message_thread_id" not in sent
    assert sent["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://t.me/DemoBot?startapp=panel"
    assert not bot.methods("pinChatMessage")


def test_the_fixed_keyboard_has_the_panel_button_in_both_languages():
    from ctb import mirror
    from ctb.i18n import both_langs
    from ctb.inbox import _keyboard_word
    he, en = both_langs("fixed_btn_panel")
    assert {"text": he} in mirror.KEYBOARD["keyboard"][-1] and {"text": en} in mirror.KEYBOARD_EN["keyboard"][-1]
    assert _keyboard_word(he) and _keyboard_word(en)  # a tap is never read as an answer to a dialog


def test_panel_taps_accept_only_the_complete_label_with_an_optional_count():
    from ctb.i18n import both_langs
    from ctb.inbox import _keyboard_word, _panel_button
    for label in both_langs("fixed_btn_panel"):
        for text in (label, label + " · 3", label.replace("🎛", "🎛️") + " · 12"):
            assert _panel_button(text) and _keyboard_word(text)
        for text in (label + " · 0", label + " · 3 extra", label + " please"):
            assert not _panel_button(text) and not _keyboard_word(text)


def test_the_panel_message_previews_what_waits_in_the_topic(monkeypatch):
    from ctb import decisions, board
    cfg, bot, topics, threads = setup(2)
    here, other = threads
    monkeypatch.setattr(decisions, "open_decisions", lambda: [
        {"id": "d1", "title": "Publish the release?", "detail": "", "yes": "", "no": "", "session": "s0",
         "chat": GROUP, "thread": here, "created": 900.0},
        {"id": "d2", "title": "Elsewhere", "detail": "", "yes": "", "no": "", "session": "s1",
         "chat": GROUP, "thread": other, "created": 950.0}])
    monkeypatch.setattr(board, "open_reminders", lambda: [(GROUP, here, {"s": "Approve the design", "at": 800.0})])
    monkeypatch.setattr(board, "items_of", lambda chat, thread: [{"k": "check", "s": "a", "x": True},
                                                                 {"k": "check", "s": "b", "x": False}])
    text = panel_pin.preview(cfg, topics, GROUP, here, now=1000.0)
    assert "Decisions waiting for you: 1" in text and "• Publish the release?" in text
    assert "Elsewhere" not in text  # another topic's decision stays out of this topic's preview
    assert "Reminders: 1" in text and "• Approve the design" in text and "Checklist: 1 of 2 done" in text
    general = panel_pin.preview(cfg, topics, GROUP, None, now=1000.0)
    assert "Decisions waiting for you: 2" in general and "Elsewhere · Demo 1" in general
    assert panel_pin.send_link(cfg, bot, GROUP, here, topics=topics)
    assert "Publish the release?" in bot.methods("sendMessage")[-1]["text"]


def test_an_empty_topic_says_nothing_waits(monkeypatch):
    from ctb import decisions, board
    cfg, bot, topics, threads = setup(1)
    monkeypatch.setattr(decisions, "open_decisions", lambda: [])
    monkeypatch.setattr(board, "open_reminders", lambda: [])
    assert "Nothing here waits for you" in panel_pin.preview(cfg, topics, GROUP, threads[0], now=1000.0)


def test_no_panel_button_until_the_panel_is_set_up(tmp_path, monkeypatch):
    monkeypatch.setattr(panel_pin.Path, "home", classmethod(lambda cls: tmp_path))
    cfg, bot, topics, threads = setup(1)
    cfg.panel_button = None
    assert not panel_pin.enabled(cfg)
    assert panel_pin.run(cfg, bot, topics, now=1000) == 0 and not bot.methods("sendMessage")
    job = tmp_path / "Library" / "LaunchAgents" / f"{panel_pin.PANEL_JOB}.plist"
    job.parent.mkdir(parents=True)
    job.write_text("<plist/>")
    assert panel_pin.enabled(cfg)
    cfg.panel_button = False
    assert not panel_pin.enabled(cfg)


# --- one pinned message: the replies index carries the button ---------------------------------------------------

def _index(cfg, bot, thread, mid=55):
    from ctb import pins
    assert pins.update_index(cfg, bot, thread, mid, "a reply", now=1500.0)
    return pins


def test_the_pinned_replies_index_does_not_duplicate_the_cockpit_button():
    cfg, bot, topics, (thread,) = setup(1)
    panel_pin.run(cfg, bot, topics, now=1000)        # the listener learns the bot's name
    panel_pin._username.clear()                      # a reply's hook is another process: no Telegram call for it
    bot.calls.clear()
    _index(cfg, bot, thread)
    (sent,) = bot.methods("sendMessage")
    assert "reply_markup" not in sent and not bot.methods("getMe")
    _index(cfg, bot, thread, mid=56)
    assert "reply_markup" not in bot.methods("editMessageText")[-1]


def test_the_panel_pin_stays_when_the_index_is_updated():
    cfg, bot, topics, (thread,) = setup(1)
    panel_pin.run(cfg, bot, topics, now=1000)
    old = panel_pin.pin_message(GROUP, thread)
    _index(cfg, bot, thread)
    bot.calls.clear()
    assert panel_pin.run(cfg, bot, topics, now=2000) == 0
    assert panel_pin.pin_message(GROUP, thread) == old
    assert not bot.methods("unpinChatMessage")


def test_the_cockpit_pin_is_re_pinned_after_the_index():
    cfg, bot, topics, (thread,) = setup(1)
    panel_pin.run(cfg, bot, topics, now=1000)
    old = panel_pin.pin_message(GROUP, thread)
    bot.calls.clear()
    _index(cfg, bot, thread)
    assert [p["message_id"] for p in bot.methods("pinChatMessage")][-1] == old
    assert len(bot.methods("pinChatMessage")) == 2


def test_a_v2_folded_pin_is_restored():
    cfg, bot, topics, (thread,) = setup(1)
    panel_pin.run(cfg, bot, topics, now=1000)
    key = f"{GROUP}:{thread}"
    state = json.loads((state_dir() / "panel-pins.json").read_text())
    old = state[key]["id"]
    state[key].update(v=2, folded=True)
    (state_dir() / "panel-pins.json").write_text(json.dumps(state))
    bot.calls.clear()
    assert panel_pin.run(cfg, bot, topics, now=2000) == 1
    assert bot.methods("editMessageText")[-1]["message_id"] == old
    assert bot.methods("pinChatMessage")[-1]["message_id"] == old
    assert panel_pin.pin_message(GROUP, thread) == old


def test_old_board_migrates_only_after_it_is_shown_on_the_cockpit_pin():
    from ctb import board
    from ctb.config import write_private
    cfg, bot, topics, (thread,) = setup(1)
    panel_pin.run(cfg, bot, topics, now=1000)
    pin = panel_pin.pin_message(GROUP, thread)
    row = board._blank()
    row.update(id=77, pinned=True, onpin=False, next=2,
               items=[{"k": "check", "i": 1, "s": "Ship it", "at": 1.0, "x": False}])
    write_private(state_dir() / board.STATE_FILE, json.dumps({f"{GROUP}:{thread}": row}))
    bot.calls.clear()
    assert panel_pin.run(cfg, bot, topics, now=2000) == 0
    assert not bot.methods("unpinChatMessage")

    row["onpin"] = True
    write_private(state_dir() / board.STATE_FILE, json.dumps({f"{GROUP}:{thread}": row}))
    assert panel_pin.run(cfg, bot, topics, now=3000) == 0
    moved = [p for p in bot.methods("editMessageText") if p["message_id"] == 77][-1]
    assert f"/{thread}/{pin}" in moved["text"]
    assert bot.methods("unpinChatMessage")[-1]["message_id"] == 77
    assert board.board_message(GROUP, thread) == 0


def test_existing_idle_pin_refreshes_but_no_new_pin_is_posted(monkeypatch):
    from ctb import waiting
    cfg, bot, topics, threads = setup(2)
    monkeypatch.setattr(waiting, 'waiting_count', lambda cfg: 2)
    monkeypatch.setattr(panel_pin, '_active', lambda *a: set())
    (state_dir() / 'panel-pins.json').write_text(json.dumps({f'{GROUP}:{threads[0]}': {'id': 55, 'pinned': True, 'v': panel_pin.VERSION, 'count': 1}}))
    assert panel_pin.run(cfg, bot, topics, now=1000) == 1
    assert len(bot.methods('editMessageText')) == 1 and not bot.methods('sendMessage')
