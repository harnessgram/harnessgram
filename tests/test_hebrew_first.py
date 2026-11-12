"""The "folded-after" mode of a translated reply: the Hebrew first with the original folded under it; slow or
failed translation keeps the original first (never lost, never doubled). The default "edit" mode is covered in
test_instant_reply.py."""
import threading

from conftest import GOOD_TOKEN, FakeBot
from ctb import mirror as M
from ctb.config import Config
from ctb.topics import TopicMap

OWNER = 7
TEXT = "Plain English reply here."


def mk(tmp_path, bot, **kw):
    kw.setdefault("original", "folded-after")
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", **kw)
    return M.Mirror(c, bot, TopicMap(tmp_path / "state" / "topics.json", bot, OWNER))


def texts(bot):
    return [p["text"] for p in bot.methods("sendMessage")]


def test_fast_translation_sends_hebrew_then_folded_original(tmp_path):
    bot = FakeBot()
    sent = mk(tmp_path, bot).send_reply("P", "s", TEXT, lambda t: "תרגום מהיר")
    out = texts(bot)
    assert len(out) == 2 and sent == 2
    assert "תרגום מהיר" in out[0] and TEXT not in out[0]
    assert TEXT in out[1] and "<blockquote expandable>" in out[1]


def test_slow_translation_sends_original_first(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "TRANSLATE_BUDGET", 0.05)
    release = threading.Event()
    bot = FakeBot()

    def slow(t):
        release.wait(5)
        return "תרגום איטי"

    threading.Timer(0.3, release.set).start()
    mk(tmp_path, bot).send_reply("P", "s", TEXT, slow)
    out = texts(bot)
    assert len(out) == 2
    assert TEXT in out[0] and "blockquote" not in out[0]
    assert "תרגום איטי" in out[1]


def test_failed_translation_is_original_only(tmp_path):
    bot = FakeBot()

    def boom(t):
        raise RuntimeError("down")

    assert mk(tmp_path, bot).send_reply("P", "s", TEXT, boom) == 1
    out = texts(bot)
    assert len(out) == 1 and TEXT in out[0] and "blockquote" not in out[0]


def test_reply_parameters_on_the_first_message_only(tmp_path, monkeypatch):
    m = mk(tmp_path, FakeBot())
    chat = M.home_chat(m.cfg)
    answer = {"chat": chat, "id": 99}
    bot = FakeBot()
    m = mk(tmp_path, bot)
    m.send_reply("P", "s", TEXT, lambda t: "תרגום", answer_to=answer)
    msgs = bot.methods("sendMessage")
    assert msgs[0].get("reply_parameters", {}).get("message_id") == 99
    assert "reply_parameters" not in msgs[1]
    # slow path: the original is first and carries it
    monkeypatch.setattr(M, "TRANSLATE_BUDGET", 0.0)
    gate = threading.Event()
    threading.Timer(0.2, gate.set).start()
    bot2 = FakeBot()
    mk(tmp_path / "b", bot2).send_reply("P", "s", TEXT, lambda t: gate.wait(5) and "תרגום", answer_to=answer)
    msgs = bot2.methods("sendMessage")
    assert TEXT in msgs[0]["text"] and msgs[0].get("reply_parameters", {}).get("message_id") == 99
    assert "reply_parameters" not in msgs[1]


def test_before_keeps_the_old_order(tmp_path):
    bot = FakeBot()
    mk(tmp_path, bot, original="before").send_reply("P", "s", TEXT, lambda t: "תרגום")
    out = texts(bot)
    assert TEXT in out[0] and "blockquote" not in out[0] and "תרגום" in out[1]


def test_off_sends_hebrew_only_when_fast(tmp_path):
    bot = FakeBot()
    mk(tmp_path, bot, original="off").send_reply("P", "s", TEXT, lambda t: "תרגום")
    out = texts(bot)
    assert len(out) == 1 and "תרגום" in out[0]


class FailingBot(FakeBot):
    """Fails every sendMessage whose text contains `fail_text` (a Telegram refusal)."""

    def __init__(self, fail_text):
        super().__init__()
        self.fail_text = fail_text

    def call(self, method, params=None, timeout=15):
        if method == "sendMessage" and self.fail_text in (params or {}).get("text", ""):
            self.calls.append((method, params))
            from ctb.telegram import TelegramError
            raise TelegramError("Bad Request: boom")
        return super().call(method, params, timeout)


def delivered(bot):
    return [p["text"] for meth, p in bot.calls if meth == "sendMessage" and "תרגום" not in p["text"]]


def test_hebrew_send_fails_folded_after_sends_unfolded_original(tmp_path):
    bot = FailingBot("תרגום")
    m = mk(tmp_path, bot)
    m.send_reply("P", "s", TEXT, lambda t: "תרגום")
    out = delivered(bot)
    assert len(out) == 1 and TEXT in out[0] and "blockquote" not in out[0]


def test_hebrew_send_fails_off_sends_the_original(tmp_path):
    bot = FailingBot("תרגום")
    mk(tmp_path, bot, original="off").send_reply("P", "s", TEXT, lambda t: "תרגום")
    out = delivered(bot)
    assert len(out) == 1 and TEXT in out[0] and "blockquote" not in out[0]


def test_after_alias_is_hebrew_first(tmp_path):
    bot = FakeBot()
    mk(tmp_path, bot, original="after").send_reply("P", "s", TEXT, lambda t: "תרגום")
    out = texts(bot)
    assert "תרגום" in out[0] and "<blockquote expandable>" in out[1]


def test_held_link_survives_on_the_hebrew_first_path(tmp_path):
    token = "0123456789abcdef" * 3
    link = f"https://box.tailabc123.ts.net/__secure-handoff/{token}"
    seen = []

    def tr(t):
        seen.append(t)
        return "תרגום " + t

    bot = FakeBot()
    mk(tmp_path, bot).send_reply("P", "s", f"Open {link} now", tr)
    assert token not in seen[0]  # the engine never sees the link
    out = texts(bot)
    assert link in out[0] and link in out[1]


def test_yes_no_buttons_on_the_hebrew_stay_the_latest_reply(tmp_path):
    # The folded original under a translated question must not take the "last reply" slot, or a tap on
    # the Hebrew's yes/no buttons is refused as an old question.
    from ctb.topics import latest_reply
    bot = FakeBot()
    mk(tmp_path, bot).send_reply("P", "s", "Should I start with three groups?", lambda t: "להתחיל עם שלוש קבוצות?")
    sends = bot.methods("sendMessage")
    asked = [p for p in sends if "say:yes" in str(p.get("reply_markup"))]
    assert len(asked) == 1 and "<blockquote expandable>" in sends[-1]["text"]
    assert latest_reply(OWNER, asked[0]["message_thread_id"]) == asked[0]["message_id"]
