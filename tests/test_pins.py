"""The pinned replies index: one pinned message per session topic, edited after each reply."""
import json
import re
import threading
import time

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import creds as C, flood, inbox, mirror as M, pins, telegram as T
from ctb.config import Config, ConfigError, from_dict, state_dir
from ctb.topics import TopicMap

OWNER = 7
GROUP = -1001234567890
BOT_ID = int(GOOD_TOKEN.split(":")[0])


class Bot(FakeBot):
    """FakeBot whose failures can carry a status code, and whose edits can be told the index is gone."""

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


def make(bot, tmp_path, **kw):
    base = dict(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP, target_language="en", progress=False)
    base.update(kw)
    cfg = Config(**base)
    return M.Mirror(cfg, bot, TopicMap(tmp_path / "state" / "topics.json", bot, M.home_chat(cfg))), cfg


def state():
    return json.loads((state_dir() / pins.STATE_FILE).read_text(encoding="utf-8"))


def reply(m, text="Did the work.\n\nSummary: all good", **kw):
    return m.send_reply("proj", "s1", text, **kw)


def index_sends(bot):
    return [p for p in bot.methods("sendMessage") if "t.me/c/" in p["text"]]


def test_index_created_once_then_edited(bot, tmp_path):
    m, _ = make(bot, tmp_path)
    reply(m)
    reply(m, "Second.\n\nSummary: second one")
    reply(m, "Third.\n\nSummary: third one")
    assert len(index_sends(bot)) == 1 and len(bot.methods("pinChatMessage")) == 1  # one notice, ever
    created = index_sends(bot)[0]
    assert created["disable_notification"] is True and created["message_thread_id"] == m.topics.thread_of("s1")
    edits = bot.methods("editMessageText")
    assert len(edits) == 2
    pin = bot.methods("pinChatMessage")[0]
    assert pin["message_id"] == edits[0]["message_id"] and pin["chat_id"] == GROUP
    lines = edits[-1]["text"].split("\n")
    assert len(lines) == 3 and edits[-1]["parse_mode"] == "HTML"
    assert "all good" in lines[0] and "second one" in lines[1] and "third one" in lines[2]  # newest last
    assert not bot.methods("unpinChatMessage")


def test_links_point_at_the_reply_in_its_topic(bot, tmp_path):
    m, _ = make(bot, tmp_path)
    reply(m)
    thread = m.topics.thread_of("s1")
    first_reply = [p for p in bot.methods("sendMessage") if "Did the work" in p["text"]][0]["message_id"]
    assert f'<a href="https://t.me/c/1234567890/{thread}/{first_reply}">all good</a>' in index_sends(bot)[0]["text"]
    assert pins.link(-1001234567890, 5, 9) == "https://t.me/c/1234567890/5/9"


def test_translated_reply_is_indexed_with_the_translated_summary(bot, tmp_path):
    m, _ = make(bot, tmp_path, target_language="he")
    reply(m, translate_fn=lambda t: "עשיתי את העבודה.\n\nסיכום: הכול תקין")
    ids = {p["message_id"]: p["text"] for p in bot.methods("sendMessage")}
    for p in bot.methods("editMessageText"):  # the instant reply is edited in place to its translation
        if p["message_id"] in ids:
            ids[p["message_id"]] = p["text"]
    text = index_sends(bot)[0]["text"]
    assert text.startswith("‏") and "הכול תקין" in text and "all good" not in text
    target = int(re.search(r"/(\d+)\"", text).group(1))
    assert "עשיתי" in ids[target]  # linked to the translated message, not the English original


def test_failed_translation_links_the_original(bot, tmp_path):
    m, _ = make(bot, tmp_path, target_language="he")

    def boom(_):
        raise RuntimeError("no engine")
    reply(m, translate_fn=boom)
    ids = {p["message_id"]: p["text"] for p in bot.methods("sendMessage")}
    target = int(re.search(r"/(\d+)\"", index_sends(bot)[0]["text"]).group(1))
    assert "Did the work" in ids[target]


def test_index_recreated_when_it_was_deleted(bot, tmp_path):
    m, _ = make(bot, tmp_path)
    reply(m)
    bot.fail["editMessageText"] = "Bad Request: message to edit not found"
    bot.codes["editMessageText"] = 400
    reply(m, "Next.\n\nSummary: next one")
    assert len(index_sends(bot)) == 2 and len(bot.methods("pinChatMessage")) == 2
    new = index_sends(bot)[1]
    assert "all good" in new["text"] and "next one" in new["text"]  # the list survives the new message
    assert state()[f"{GROUP}:{m.topics.thread_of('s1')}"]["id"] == bot.methods("pinChatMessage")[1]["message_id"]


def test_gate_closed_skips_the_edit_and_the_next_reply_heals_it(bot, tmp_path):
    m, _ = make(bot, tmp_path)
    reply(m)
    flood.record_429(GROUP, 30)
    assert reply(m, "Next.\n\nSummary: next one") == 1  # the reply itself still goes out
    assert not bot.methods("editMessageText")
    (state_dir() / "flood.json").unlink()  # gate open again
    reply(m, "Last.\n\nSummary: last one")
    text = bot.methods("editMessageText")[0]["text"]
    assert all(w in text for w in ("all good", "next one", "last one"))


def test_gate_closed_skips_creation_too(bot, tmp_path):
    m, _ = make(bot, tmp_path)
    flood.record_429(GROUP, 30)
    assert reply(m) == 1
    assert not index_sends(bot) and not bot.methods("pinChatMessage")


def test_edit_and_creation_both_use_the_budget(bot, tmp_path):
    m, _ = make(bot, tmp_path)
    reply(m)
    while flood.allow_optional(GROUP):  # spend the rest of the minute's budget
        pass
    reply(m, "Next.\n\nSummary: next one")
    assert not bot.methods("editMessageText")  # one unit per edit: skipped, the reply is unaffected
    bot.fail["editMessageText"] = "Bad Request: message to edit not found"
    bot.codes["editMessageText"] = 400
    reply(m, "More.\n\nSummary: more")
    assert len(index_sends(bot)) == 1


def test_each_edit_costs_one_budget_unit(bot, tmp_path):
    m, cfg = make(bot, tmp_path)
    reply(m)
    before = len(json.loads((state_dir() / "flood.json").read_text())["sent"][str(GROUP)])
    pins.update_index(cfg, bot, m.topics.thread_of("s1"), 55, "x")
    after = len(json.loads((state_dir() / "flood.json").read_text())["sent"][str(GROUP)])
    assert after == before + 1


def test_missing_pin_right_backs_off_for_hours(bot, tmp_path):
    m, cfg = make(bot, tmp_path)
    bot.fail["pinChatMessage"] = "Bad Request: not enough rights to pin a message"
    bot.codes["pinChatMessage"] = 400
    assert reply(m) == 1  # the reply is not affected
    key = f"{GROUP}:{m.topics.thread_of('s1')}"
    assert state()[key]["pinned"] is False and state()[key]["pin_retry_at"] > time.time() + 5 * 3600
    reply(m, "Next.\n\nSummary: next one")
    reply(m, "Third.\n\nSummary: third one")
    assert len(bot.methods("pinChatMessage")) == 1 and len(bot.methods("editMessageText")) == 2  # no retry inside the back-off
    assert "cannot pin the replies index" in (state_dir() / "mirror.log").read_text()
    st = state()
    st[key]["pin_retry_at"] = time.time() - 1  # the back-off is over
    (state_dir() / pins.STATE_FILE).write_text(json.dumps(st))
    reply(m, "Fourth.\n\nSummary: fourth one")
    assert len(bot.methods("pinChatMessage")) == 2 and state()[key]["pinned"] is True


def test_pin_skipped_by_the_gate_is_retried_without_back_off(bot, tmp_path):
    m, cfg = make(bot, tmp_path)
    reply(m)
    key = f"{GROUP}:{m.topics.thread_of('s1')}"
    st = state()
    st[key]["pinned"] = False
    (state_dir() / pins.STATE_FILE).write_text(json.dumps(st))
    bot.fail["pinChatMessage"] = "network down"  # not a 400/403
    reply(m, "Next.\n\nSummary: next one")
    assert state()[key]["pin_retry_at"] == 0.0 and len(bot.methods("pinChatMessage")) == 2
    reply(m, "Third.\n\nSummary: third one")
    assert len(bot.methods("pinChatMessage")) == 3


def test_list_is_capped(bot, tmp_path, monkeypatch):
    monkeypatch.setattr(flood, "OPTIONAL_PER_MIN", 1000)  # 20 replies in a test: not the budget under test
    m, _ = make(bot, tmp_path)
    for i in range(20):
        reply(m, f"Work {i}.\n\nSummary: step {i}")
    text = bot.methods("editMessageText")[-1]["text"]
    lines = text.split("\n")
    assert len(lines) == 15 and "step 19<" in lines[-1] and "step 5<" in lines[0]
    m2, _ = make(Bot(), tmp_path, pin_limit=2, chat_id=-1005550001)
    pins.update_index(m2.cfg, m2.bot, 9, 11, "a")
    pins.update_index(m2.cfg, m2.bot, 9, 12, "b")
    pins.update_index(m2.cfg, m2.bot, 9, 13, "c")
    assert m2.bot.methods("editMessageText")[-1]["text"].count("\n") == 1


def test_long_summary_is_shortened_and_escaped():
    line = pins.index_text("**Summary** with <b> & [link](http://x) " + "word " * 40)
    assert len(line) <= pins.LINE_CHARS and line.endswith("…") and "*" not in line and "http" not in line


def test_index_line_never_mixes_hebrew_and_english():
    mixed = "בדקתי ועכשיו the five failing tests now pass בהצלחה"
    label = pins.index_text(mixed)
    assert label == "בדקתי ועכשיו" and "\n" not in label
    assert not (pins.fmt.HEB.search(label) and pins.fmt.LAT.search(label))
    # a long mixed line cut at the limit never leaves a bidi isolate open
    cut = pins.index_text("בדקתי " * 8 + "\u2068Claude\u2069 ועוד " * 3)
    assert cut.count("\u2068") == cut.count("\u2069") and len(cut) <= pins.LINE_CHARS + 1
    opened = pins.index_text("\u2068" + "word " * 30)
    assert opened.count("\u2068") == opened.count("\u2069")


def test_only_hebrew_labels_get_the_rtl_mark(bot, tmp_path):
    m, cfg = make(bot, tmp_path, target_language="he")
    pins.update_index(cfg, bot, 5, 11, "all good, done.")
    pins.update_index(cfg, bot, 5, 12, "הכול תקין")
    lines = bot.methods("editMessageText")[-1]["text"].split("\n")
    assert not lines[0].startswith("\u200f") and lines[1].startswith("\u200f")


def test_untranslated_hebrew_reply_is_unmixed_in_the_index(bot, tmp_path):
    m, _ = make(bot, tmp_path, target_language="he")
    reply(m, "בדקתי.\n\nSummary: בדקתי ועכשיו the five failing tests now pass בהצלחה")
    text = index_sends(bot)[0]["text"]
    label = re.search(r">([^<]*)</a>", text).group(1)
    assert label == "בדקתי ועכשיו"


def test_private_chat_switch_and_limit_zero_do_nothing(bot, tmp_path):
    for kw in ({"chat_id": 0}, {"pin_replies": False}, {"pin_limit": 0}):
        b = Bot()
        m, _ = make(b, tmp_path, **kw)
        assert reply(m) == 1
        assert not index_sends(b) and not b.methods("pinChatMessage") and not b.methods("editMessageText")
    assert not (state_dir() / pins.STATE_FILE).exists()


def test_basic_group_without_supergroup_id_does_nothing(bot, tmp_path):
    m, _ = make(bot, tmp_path, chat_id=-555)
    reply(m)
    assert not bot.methods("pinChatMessage")


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"x": "y", "k": {"items": "z"}}',
                                     '{"%s:5": {"id": "a", "pinned": 1, "items": [1, {"t": "x"}, {"t": 1, "s": "s", "m": 0}]}}'])
def test_malformed_state_is_dropped(bot, tmp_path, content):
    m, cfg = make(bot, tmp_path)
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / pins.STATE_FILE).write_text(content.replace("%s", str(GROUP)), encoding="utf-8")
    assert pins.update_index(cfg, bot, 5, 101, "hello")
    assert len(index_sends(bot)) == 1 and state()[f"{GROUP}:5"]["items"][0]["m"] == 101
    assert (state_dir() / pins.STATE_FILE).stat().st_mode & 0o777 == 0o600


def test_methods_used_are_relayed():
    for method in ("sendMessage", "editMessageText", "pinChatMessage", "deleteMessage"):
        assert method in inbox.RELAY_METHODS["api"]


def test_index_works_through_the_relay(bot, tmp_path, monkeypatch):
    """A session without Keychain access (Bot(None)) reaches Telegram only through the relay socket."""
    import tempfile
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        monkeypatch.setenv("CTB_STATE_DIR", directory)
        cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP, target_language="en", progress=False)
        stop, real = threading.Event(), FakeBot()
        server = threading.Thread(target=inbox.relay_serve, args=(cfg,), kwargs={"stop_event": stop, "bot": real}, daemon=True)
        server.start()
        for _ in range(60):
            if C.relay_socket_path().is_socket():
                break
            time.sleep(0.05)
        try:
            remote = T.Bot(None)
            m = M.Mirror(cfg, remote, TopicMap(state_dir() / "topics.json", remote, GROUP))
            reply(m)
            reply(m, "Next.\n\nSummary: next one")
        finally:
            stop.set()
            server.join(timeout=2)
        assert len(real.methods("pinChatMessage")) == 1 and len(real.methods("editMessageText")) == 1
        assert "next one" in real.methods("editMessageText")[0]["text"]


def notice(sender_id, is_bot=True, chat=GROUP):
    return {"update_id": 1, "message": {"message_id": 900, "chat": {"id": chat, "type": "supergroup"},
            "from": {"id": sender_id, "is_bot": is_bot}, "message_thread_id": 55,
            "pinned_message": {"message_id": 800, "chat": {"id": chat}}}}


def test_notice_of_our_pin_is_deleted_best_effort(bot, tmp_path):
    _, cfg = make(bot, tmp_path)
    assert inbox.handle(notice(BOT_ID), cfg, bot, None, None, None, 0.0) == "pin-notice"
    assert bot.methods("deleteMessage") == [{"chat_id": GROUP, "message_id": 900}]


def test_notice_of_the_owners_or_another_pin_is_kept(bot, tmp_path):
    _, cfg = make(bot, tmp_path)
    assert not pins.drop_pin_notice(notice(OWNER, is_bot=False)["message"], cfg, bot)
    assert not pins.drop_pin_notice(notice(999)["message"], cfg, bot)
    assert not pins.drop_pin_notice(notice(BOT_ID, chat=-5)["message"], cfg, bot)
    assert not bot.methods("deleteMessage")


def test_config_keys():
    cfg = from_dict({"pin_limit": 4, "pin_replies": False})
    assert (cfg.pin_limit, cfg.pin_replies) == (4, False)
    assert Config().pin_replies is True and Config().pin_limit == 15
    for bad in ({"pin_limit": -1}, {"pin_limit": "3"}, {"pin_replies": "yes"}):
        with pytest.raises(ConfigError):
            from_dict(bad)
