"""The listen button under a final reply: where it appears, the tap, the text, the cache, the limits.
Audio and Telegram are mocked; no voice engine is ever started."""
import json

import pytest
from conftest import GOOD_TOKEN, FakeBot
from ctb import listen, mirror as M
from ctb.config import Config, state_dir
from ctb.i18n import t
from ctb.topics import TopicMap

OWNER = 7
CHAT = OWNER
HEB = "עשיתי את השינוי ובדקתי שהכול עובד. הכול מוכן להמשך."


def cfg(**kw):
    base = dict(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", ui_language="he")
    base.update(kw)
    return Config(**base)


class VoiceBot(FakeBot):
    def upload(self, method, fields, file_field, filename, payload, timeout=120):
        super().upload(method, fields, file_field, filename, payload, timeout)
        return {"voice": {"file_id": "FILE1"}}


def send(tmp_path, bot=None, replies=2, **kw):
    """Send `replies` final replies into one topic (the first one carries the one-time fixed keyboard)."""
    bot = bot or FakeBot()
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    topics.ensure("sess", "Title")
    mirror = M.Mirror(cfg(**kw), bot, topics)
    for _ in range(replies):
        mirror.send_reply("Title", "sess", HEB)
    return bot, mirror


def listen_rows(bot):
    out = []
    for p in bot.methods("sendMessage") + bot.methods("editMessageReplyMarkup"):
        for row in (p.get("reply_markup") or {}).get("inline_keyboard", []):
            out += [b for b in row if b["callback_data"].startswith("ls:")]
    return out


def tap(token, bot, mine=True, session="sess", mid=55):
    q = {"id": "q1", "data": "ls:" + token, "from": {"id": OWNER},
         "message": {"message_id": mid, "chat": {"id": CHAT}, "message_thread_id": 101}}
    return listen.press(q, cfg(), bot, session, mine, background=False)


@pytest.fixture
def voice(monkeypatch):
    spoken = []
    monkeypatch.setattr(listen, "engine_ready", lambda c: True)
    monkeypatch.setattr(listen, "synthesize", lambda c, text: spoken.append(text) or b"OGG")
    return spoken


def filed(text=HEB, session="sess"):
    token = listen.new_token()
    listen.remember(token, session, text)
    return token


def toasts(bot):
    return [p["text"] for p in bot.methods("answerCallbackQuery")]


# --- the button -----------------------------------------------------------------------------

def test_button_on_the_final_reply_and_its_id_maps_to_the_shown_text(tmp_path):
    bot, _ = send(tmp_path, replies=1)
    rows = listen_rows(bot)
    assert len(rows) == 1
    assert rows[0]["text"] == t("btn_listen", lang="he") and "🔊" in rows[0]["text"]
    data = rows[0]["callback_data"]
    assert listen.CALLBACK.fullmatch(data) and len(data.encode()) <= 64
    saved = json.loads((state_dir() / "listen.json").read_text())
    assert saved[data[3:]]["session"] == "sess" and "בדקתי" in saved[data[3:]]["text"]


def test_existing_buttons_are_unchanged_and_the_first_reply_has_the_button_too(tmp_path):
    on, _ = send(tmp_path / "a")
    other = FakeBot()
    other.next_thread = 200  # the fixed keyboard is once per topic
    off, _ = send(tmp_path / "b", other, listen_button=False)
    first_on, second_on = [p.get("reply_markup") for p in on.methods("sendMessage")]
    first_off, second_off = [p.get("reply_markup") for p in off.methods("sendMessage")]
    assert "keyboard" in first_off and second_off is None
    assert first_on == first_off  # the one-time fixed keyboard is sent exactly as before ...
    late = on.methods("editMessageReplyMarkup")  # ... and the button is added to that same message
    assert len(late) == 1 and late[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("ls:")
    assert len(on.methods("sendMessage")) == len(off.methods("sendMessage"))  # no extra message
    row = second_on["inline_keyboard"]
    assert row[0][0]["callback_data"].startswith("ls:") and row[1][0]["callback_data"] == "more:unbuttons"
    assert len(listen_rows(on)) == 2 and listen_rows(off) == []  # the first reply (edited in) and the second


def test_question_replies_keep_yes_no_and_add_the_row(tmp_path):
    bot = FakeBot()
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    topics.ensure("sess", "Title")
    M.Mirror(cfg(), bot, topics).send_reply("Title", "sess", HEB + " להמשיך?")
    rows = bot.methods("sendMessage")[-1]["reply_markup"]["inline_keyboard"]
    data = [b["callback_data"] for row in rows for b in row]
    assert "say:yes" in data and "say:no" in data and data[-1].startswith("ls:")


def test_no_button_when_the_reply_is_not_hebrew(tmp_path):
    bot, _ = send(tmp_path, target_language="en")
    assert listen_rows(bot) == []


def test_no_button_on_other_messages(tmp_path):
    bot = FakeBot()
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    topics.ensure("sess", "Title")
    mirror = M.Mirror(cfg(), bot, topics)
    assert listen_rows(bot) == []
    assert "ls:" not in json.dumps(mirror._reply_keyboard("", "sess"))  # only final replies add it
    M.run_prompt(cfg(), "sess", "do it", bot=bot)
    M.alert(cfg(), "sess", "The turn ended", bot=bot)
    assert listen_rows(bot) == []


# --- the tap --------------------------------------------------------------------------------

def test_owner_only(tmp_path, voice):
    bot = FakeBot()
    token = filed()
    assert tap(token, bot, mine=False) == "ignored"
    assert toasts(bot) == [t("toast_listen_owner_only", lang="he")]
    assert voice == [] and bot.methods("sendVoice") == []


def test_tap_sends_a_voice_note_replying_to_the_message(tmp_path, voice):
    bot = VoiceBot()
    assert tap(filed(), bot) == "listen"
    assert toasts(bot) == [t("toast_listen_preparing", lang="he")]
    sent = bot.methods("sendVoice")[0]
    assert json.loads(sent["reply_parameters"])["message_id"] == 55
    assert sent["message_thread_id"] == "101" and sent["_bytes"] == 3
    assert voice and "בדקתי" in voice[0]


def test_unknown_id_or_other_topic_is_refused(tmp_path, voice):
    bot = FakeBot()
    assert tap("0" * 10, bot) == "ignored"
    assert tap(filed(), bot, session="other") == "ignored"
    assert toasts(bot) == [t("toast_listen_expired", lang="he")] * 2


def test_text_cleanup_drops_code_links_paths_and_the_label():
    raw = ("סיכום: עדכנתי את הקובץ src/ctb/mirror.py ואת ~/notes/a.md\n\n```python\nprint('x')\n```\n"
           "ראו [המדריך](https://example.com/x) וגם https://example.org/y וגם `rm -rf` ואז סיימתי.")
    text, cut = listen.clean_text(raw)
    assert not cut
    for gone in ("```", "print", "http", "example", "mirror.py", "notes", "rm -rf", "סיכום", "("):
        assert gone not in text
    assert "המדריך" in text and "סיימתי" in text


def test_cap_and_the_rest_is_in_the_topic(tmp_path):
    long = ("משפט בעברית שחוזר שוב ושוב. " * 200)
    text, cut = listen.clean_text(long)
    assert cut and len(text) <= listen.MAX_CHARS and text.endswith(".")
    said, why = listen.spoken_text(long, cfg(), "he")
    assert not why and said.endswith(t("listen_cut", lang="he"))


def test_suitability_rules_block_the_text(tmp_path, voice):
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"pattern": "אסור"}))
    c = cfg(radio={"room_suitability_file": str(rules)})
    assert listen.spoken_text("זה טקסט אסור להשמעה", c, "he") == ("", "blocked")
    assert listen.spoken_text("זה טקסט רגיל להשמעה", c, "he")[1] == ""
    rules.write_text("not json")  # rules that cannot be read: nothing is said
    assert listen.spoken_text("זה טקסט רגיל להשמעה", c, "he") == ("", "blocked")


def test_no_hebrew_text_is_not_read(voice):
    bot = FakeBot()
    assert tap(filed("only english words here"), bot) == "ignored"
    assert toasts(bot) == [t("toast_listen_nothing", lang="he")] and voice == []


def test_second_tap_resends_the_same_file_id(tmp_path, voice):
    bot = VoiceBot()
    token = filed()
    tap(token, bot)
    assert tap(token, bot) == "listen-cached"
    assert len(voice) == 1 and len(bot.methods("sendVoice")) == 2
    again = bot.calls[-1][1]
    assert again["voice"] == "FILE1" and again["reply_parameters"]["message_id"] == 55
    assert toasts(bot)[-1] == t("toast_listen_resent", lang="he")


def test_cached_resend_runs_the_text_checks_first(tmp_path, voice):
    bot = VoiceBot()
    token = filed("זה טקסט אסור להשמעה")
    tap(token, bot)
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"pattern": "אסור"}))
    q = {"id": "q", "data": "ls:" + token, "message": {"message_id": 55, "chat": {"id": CHAT}}}
    assert listen.press(q, cfg(radio={"room_suitability_file": str(rules)}), bot, "sess", True, background=False) == "ignored"
    assert len(bot.methods("sendVoice")) == 1  # nothing was sent again
    rules.write_text("broken")  # unreadable rules refuse as well
    assert listen.press(q, cfg(radio={"room_suitability_file": str(rules)}), bot, "sess", True, background=False) == "ignored"
    assert len(bot.methods("sendVoice")) == 1


def test_cached_resend_runs_in_a_thread_by_default(tmp_path, voice, monkeypatch):
    bot = VoiceBot()
    token = filed()
    tap(token, bot)
    started = []
    monkeypatch.setattr(listen, "_run", lambda job, background: started.append(background))
    q = {"id": "q", "data": "ls:" + token, "message": {"message_id": 55, "chat": {"id": CHAT}}}
    assert listen.press(q, cfg(), bot, "sess", True) == "listen-cached"
    assert started == [True] and len(bot.methods("sendVoice")) == 1  # the poll loop sent nothing itself


class FlakyBot(VoiceBot):
    def __init__(self, description):
        super().__init__()
        self.description = description

    def call(self, method, params=None, timeout=15):
        if method == "sendVoice" and params and isinstance(params.get("voice"), str):
            from ctb.telegram import TelegramError
            raise TelegramError(self.description)
        return super().call(method, params, timeout)


def test_a_transient_error_keeps_the_cached_recording_and_makes_no_new_one(tmp_path, voice):
    bot = FlakyBot("Bad Gateway")
    token = filed()
    tap(token, bot)
    assert len(voice) == 1
    assert tap(token, bot) == "listen-cached"
    assert len(voice) == 1  # no new (paid or free) generation
    assert json.loads((state_dir() / "listen.json").read_text())[token]["file_id"] == "FILE1"


def test_only_a_definite_file_gone_drops_the_id_and_makes_a_new_one(tmp_path, voice):
    bot = FlakyBot("Bad Request: wrong file identifier/HTTP URL specified")
    token = filed()
    tap(token, bot)
    tap(token, bot)
    assert len(voice) == 2
    assert len(bot.methods("sendVoice")) == 2  # the first recording and the new one (the resend itself raised)


def test_the_paid_voice_is_never_asked(tmp_path, monkeypatch):
    from ctb import radio_voice
    monkeypatch.setattr(listen.shutil, "which", lambda name: "/usr/bin/" + name)
    seen = []
    monkeypatch.setattr(radio_voice, "speak", lambda *a, **k: seen.append("paid") or (b"", ""))
    monkeypatch.setattr(radio_voice, "vertex", lambda *a, **k: seen.append("paid") or (b"", None))
    monkeypatch.setattr(radio_voice, "system_voice", lambda text, voice: seen.append(voice) or b"")
    c = cfg(engines=["gemini"], translate=True, gemini_vertex_project="p", radio={"tts_daily_minutes": 25})
    assert listen.synthesize(c, "שלום עולם") == b""
    assert seen == [listen.FREE_VOICE]


def test_rate_limit_is_twenty_an_hour(tmp_path, voice):
    bot = VoiceBot()
    for _ in range(listen.PER_HOUR):
        assert tap(filed(), bot) == "listen"
    assert tap(filed(), bot) == "ignored"
    assert toasts(bot)[-1] == t("toast_listen_limit", lang="he")
    assert len(voice) == listen.PER_HOUR
    assert listen.take_slot(now=__import__("time").time() + 3700)  # an hour later it opens again


def test_one_generation_at_a_time(voice):
    bot = FakeBot()
    assert listen._busy.acquire(blocking=False)
    try:
        assert tap(filed(), bot) == "busy"
    finally:
        listen._busy.release()
    assert toasts(bot) == [t("toast_listen_busy", lang="he")] and voice == []


def test_missing_voice_engine_gives_a_toast(monkeypatch):
    monkeypatch.setattr(listen, "_ffmpeg", lambda: None)
    bot = FakeBot()
    assert tap(filed(), bot) == "ignored"
    assert toasts(bot) == [t("toast_listen_engine", lang="he")]
    assert bot.methods("sendVoice") == []


def test_failed_synthesis_says_so_and_frees_the_lock(monkeypatch):
    monkeypatch.setattr(listen, "engine_ready", lambda c: True)
    monkeypatch.setattr(listen, "synthesize", lambda c, text: b"")
    bot = FakeBot()
    tap(filed(), bot)
    assert bot.methods("sendMessage")[-1]["text"] == t("listen_failed", lang="he")
    assert listen._busy.acquire(blocking=False)
    listen._busy.release()


def test_no_rule_file_still_runs_the_shared_suitability_gate(monkeypatch):
    seen = []
    from ctb import radio_suitability
    monkeypatch.setattr(radio_suitability, "suitable", lambda text: seen.append(text) or False)
    assert listen.spoken_text("זה טקסט רגיל להשמעה", cfg(), "he") == ("", "blocked")
    assert seen  # asked although no `room_suitability_file` is set
    monkeypatch.setattr(radio_suitability, "suitable", lambda text: True)
    assert listen.spoken_text("זה טקסט רגיל להשמעה", cfg(), "he")[1] == ""
    monkeypatch.undo()
    assert listen.spoken_text("הוא נרצח בלילה ההוא", cfg(), "he") == ("", "blocked")  # the portable word list


class RefusesEdit(FakeBot):
    def call(self, method, params=None, timeout=15):
        if method == "editMessageReplyMarkup":
            from ctb.telegram import TelegramError
            self.calls.append((method, params or {}))
            raise TelegramError("Bad Request: message can't be edited")
        return super().call(method, params, timeout)


def test_a_refused_edit_falls_back_to_a_short_message_with_the_button(tmp_path):
    bot, _ = send(tmp_path, RefusesEdit(), replies=1)
    sent = bot.methods("sendMessage")
    assert "keyboard" in sent[0]["reply_markup"]  # the fixed keyboard went out as before
    follow = sent[-1]
    assert follow["text"] == "🔊" and follow["reply_parameters"]["message_id"] == sent[0]["message_id"]
    assert follow["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("ls:")
    assert len(sent) == 2
