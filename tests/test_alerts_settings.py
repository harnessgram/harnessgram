"""/alerts: the owner chooses from the phone what reaches the alerts channel."""
import json
import os
import stat
import time

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import alerts as A
from ctb import config as C
from ctb import inbox as I
from ctb import mirror as M
from ctb.telegram import TelegramError
from ctb.topics import TopicMap

OWNER, STRANGER, GROUP, CHANNEL = 7, 99, -1001234567890, -1009876543210
HANDOFF = "https://box.tailabc123.ts.net/__secure-handoff/" + "0123456789abcdef" * 3
SECRET = "sk-ant-api03-" + "a1B2" * 12


def make_cfg(**extra):
    C.save_raw({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "target_language": "en", **extra})
    return C.load()


def register(title="Alerts", bot="the_bot", trusted=True):
    """The owner registered the channel through the listener (which is what makes its id trusted)."""
    C.write_private(C.state_dir() / A.FILE, json.dumps({"id": CHANNEL, "title": title, "bot": bot}))
    if trusted:
        A._trusted[(str(C.state_dir()), CHANNEL)] = time.monotonic()


def choose(**choices):
    """The owner's saved choices (signed, as /alerts saves them)."""
    A.save_settings(C.Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER), {**A.DEFAULTS, **choices}, CHANNEL)


def to_channel(bot, method="sendMessage"):
    return [p for p in bot.methods(method) if p.get("chat_id") == CHANNEL]


@pytest.fixture
def cfg():
    return make_cfg()


# --- defaults keep today's behaviour -----------------------------------------------------------------------------

def test_defaults_with_no_settings_file():
    assert A.settings() == {"waiting": "on", "system": "on", "replies": "summary", "files": "off", "radio": "on", "cockpit": "on"}
    assert [k for k in A.KINDS if A.enabled(k)] == ["waiting", "system", "summary", "radio", "cockpit"]
    assert not A.enabled("unknown")


def test_defaults_alert_goes_out_like_before(cfg):
    register()
    bot = FakeBot()
    assert A.alert(cfg, "hello", "s1", bot=bot, silent=True, url="https://t.me/c/1/2/3", kind="summary")
    (sent,) = to_channel(bot)
    assert sent["text"] == "hello" and sent["disable_notification"] is True
    assert sent["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://t.me/c/1/2/3"


@pytest.mark.parametrize("content", ["", "{", "[]", '"x"', "null", '{"waiting": 1, "replies": ["full"], "files": null}',
                                     '{"waiting": "maybe", "system": "ON", "replies": "all", "files": "yes"}'])
def test_corrupt_or_unknown_settings_mean_defaults(content):
    (C.state_dir()).mkdir(parents=True, exist_ok=True)
    (C.state_dir() / A.SETTINGS_FILE).write_text(content)
    assert A.settings() == A.DEFAULTS


def test_valid_values_survive_next_to_invalid_ones():
    choose(waiting="off", replies="bogus", files="on", system=["x"])
    assert A.settings() == {"waiting": "off", "system": "on", "replies": "summary", "files": "on", "radio": "on", "cockpit": "on"}


def test_set_option_validates_and_the_file_is_private():
    assert not A.set_option("waiting", "full") and not A.set_option("nope", "on") and not A.set_option("files", "")
    assert not (C.state_dir() / A.SETTINGS_FILE).exists()
    assert A.set_option("replies", "full")
    path = C.state_dir() / A.SETTINGS_FILE
    assert A.settings()["replies"] == "full" and stat.S_IMODE(path.stat().st_mode) == 0o600


# --- each setting gates its category ---------------------------------------------------------------------------

@pytest.mark.parametrize("choices,kind,passes", [
    ({"waiting": "off"}, "waiting", False), ({"waiting": "on"}, "waiting", True),
    ({"system": "off"}, "system", False), ({"system": "on"}, "system", True),
    ({"replies": "off"}, "summary", False), ({"replies": "full"}, "summary", False),
    ({"replies": "summary"}, "summary", True),
    ({"waiting": "off", "replies": "off"}, "system", True),      # one switch never touches another
])
def test_each_toggle_gates_its_category(cfg, choices, kind, passes):
    register()
    choose(**choices)
    bot = FakeBot()
    assert A.alert(cfg, "x", "s", bot=bot, kind=kind) is passes
    assert len(to_channel(bot)) == int(passes)


def test_no_channel_means_nothing_is_sent(cfg):
    bot = FakeBot()
    assert not A.alert(cfg, "x", bot=bot) and not A.send_reply(cfg, "x", bot=bot) and not bot.calls


def test_a_change_takes_effect_at_once_without_restart(cfg):
    register()
    bot = FakeBot()
    assert A.alert(cfg, "one", bot=bot)
    A.set_option("system", "off", cfg)
    assert not A.alert(cfg, "two", bot=bot)
    A.set_option("system", "on", cfg)
    assert A.alert(cfg, "three", bot=bot)
    assert [p["text"] for p in to_channel(bot)] == ["one", "three"]


def test_waiting_off_leaves_no_mark_so_turning_it_on_rings_at_once(cfg):
    register()
    choose(waiting="off")
    bot = FakeBot()
    M._notify_alert(cfg, "s1", bot)
    assert not bot.calls and not (C.state_dir() / "notify").exists()
    A.set_option("waiting", "on", cfg)
    M._notify_alert(cfg, "s1", bot)
    assert len(to_channel(bot)) == 1
    M._notify_alert(cfg, "s1", bot)                            # the ten-minute spacing still holds
    assert len(to_channel(bot)) == 1


def test_system_events_gate(cfg, monkeypatch):
    register()
    from ctb import recover
    bot = FakeBot()
    recover._say(cfg, bot, 5, "limit is back", "s1", alert=True)
    assert len(to_channel(bot)) == 1
    choose(system="off")
    recover._say(cfg, bot, 5, "limit is back", "s1", alert=True)
    assert len(to_channel(bot)) == 1 and len(bot.methods("sendMessage")) == 3   # the topic message still goes


# --- replies: summary, full, off --------------------------------------------------------------------------------

class Rig:
    def __init__(self, tmp_path, **raw):
        self.cfg = make_cfg(chat_id=GROUP, **raw)
        register()
        self.bot = FakeBot()
        self.topics = TopicMap(C.state_dir() / "topics.json", self.bot, GROUP)
        self.mirror = M.Mirror(self.cfg, self.bot, self.topics)

    def reply(self, text, translate_fn=None):
        self.bot.calls.clear()
        self.mirror.send_reply("Title", "s1", text, translate_fn)
        return to_channel(self.bot)

    @staticmethod
    def bodies(sent):
        """The channel messages without their "💬 topic name" header line."""
        return [p["text"].split("\n", 1)[1] if "💬" in p["text"].split("\n", 1)[0] else p["text"] for p in sent]

    def topic_text(self):
        return "\n".join(p["text"] for p in self.bot.methods("sendMessage") if p.get("chat_id") == GROUP)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_default_reply_copy_is_the_summary_only(rig):
    sent = rig.reply("Long body of the answer.\n\nSummary: all done")
    assert rig.bodies(sent) == ["all done"] and sent[0]["disable_notification"] is True


def test_replies_off_sends_nothing(rig):
    choose(replies="off")
    assert rig.reply("Body.\n\nSummary: done") == []
    assert "Body." in rig.topic_text()                          # the topic is untouched


def test_full_replaces_the_summary_and_is_plain_silent_text(rig):
    choose(replies="full")
    sent = rig.reply("**Body** with `code` and [a link](https://example.com/x).\n\nSummary: all done")
    assert len(sent) == 1
    (p,) = sent
    assert rig.bodies(sent) == ["Body with code and a link.\n\nall done"] and p["text"].startswith("💬 Title\n")          # no duplicate summary message
    assert "parse_mode" not in p and p["disable_notification"] is True
    assert p["reply_markup"]["inline_keyboard"][0][0]["url"].startswith("https://t.me/c/1234567890/")


def test_full_is_sent_even_without_a_summary_line(rig):
    choose(replies="full")
    assert rig.bodies(rig.reply("Just an answer.")) == ["Just an answer."]


def test_full_sends_the_translation_the_owner_got(tmp_path):
    rig = Rig(tmp_path, translate=True, target_language="he")
    choose(replies="full")
    sent = rig.reply("Hello there, this is the answer in English.", translate_fn=lambda t: "שלום, זו התשובה בעברית.")
    assert rig.bodies(sent) == ["שלום, זו התשובה בעברית."]


def test_long_full_reply_is_split_capped_and_points_to_the_topic(cfg):
    register()
    choose(replies="full")
    bot = FakeBot()
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 150 for i in range(60))
    assert A.send_reply(cfg, text, "s1", bot=bot, url="https://t.me/c/1/2/3")
    sent = to_channel(bot)
    assert len(sent) == A.MAX_PARTS and all(len(p["text"]) <= 4096 for p in sent)
    assert sent[0]["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://t.me/c/1/2/3"
    assert "(1/5)" in sent[0]["text"] or "1/5" in sent[0]["text"]
    assert "rest of the reply is in the topic" in sent[-1]["text"]
    assert sent[-1]["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://t.me/c/1/2/3"
    assert all(p["disable_notification"] is True and "reply_markup" not in p for p in sent[1:-1])


def test_a_reply_that_fits_in_the_cap_has_no_cut_note(cfg):
    register()
    choose(replies="full")
    bot = FakeBot()
    A.send_reply(cfg, "\n\n".join("para " * 300 for _ in range(4)), "s1", bot=bot)
    sent = to_channel(bot)
    assert 1 < len(sent) <= A.MAX_PARTS and not any("in the topic" in p["text"] for p in sent)


def test_full_reply_stops_at_the_first_failed_part(cfg):
    register()
    choose(replies="full")

    class Flaky(FakeBot):
        def call(self, method, params=None, timeout=15):
            if method == "sendMessage" and len(self.methods("sendMessage")) >= 1:
                raise TelegramError("boom")
            return super().call(method, params, timeout)
    bot = Flaky()
    assert not A.send_reply(cfg, "\n\n".join("para " * 300 for _ in range(20)), "s1", bot=bot)   # no raise
    assert len(bot.methods("sendMessage")) == 1                                                    # the second failed, no third


def test_full_reply_is_always_redacted(cfg):
    register()
    choose(replies="full")
    bot = FakeBot()
    A.send_reply(cfg, f"key {SECRET} and token: {'Zq9' * 10} here", "s1", bot=bot)
    (p,) = to_channel(bot)
    assert SECRET not in p["text"] and "Zq9Zq9" not in p["text"] and "[redacted]" in p["text"]


def test_alert_text_is_always_redacted_too(cfg):
    register()
    bot = FakeBot()
    A.alert(cfg, f"oops {SECRET}", "s1", bot=bot)
    assert SECRET not in to_channel(bot)[0]["text"]


def test_owner_only_links_never_reach_the_channel(rig):
    choose(replies="full")
    sent = rig.reply(f"Open {HANDOFF} to hand over the key.\n\nSummary: form ready")
    assert HANDOFF in rig.topic_text()                          # the owner's copy has the link
    blob = json.dumps(sent)
    assert "__secure-handoff" not in blob and "hgl.invalid" not in blob and "0123456789abcdef" not in blob
    assert "[redacted]" in sent[0]["text"]
    choose(replies="summary")
    blob = json.dumps(rig.reply(f"Open {HANDOFF}\n\nSummary: open {HANDOFF} now"))
    assert "__secure-handoff" not in blob and "hgl.invalid" not in blob


def test_owner_only_signin_links_never_reach_the_channel(rig):
    choose(replies="full")
    link = ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
            "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=user%3Aprofile"
            "&code_challenge=Zx9aQ-k2LmN8pRtVwYbCdEfGhIjKlMnOpQrSt&code_challenge_method=S256"
            "&state=Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56Qr78St90Uv")
    blob = json.dumps(rig.reply(f"Sign in: {link}"))
    assert "oauth/authorize" not in blob and "code_challenge" not in blob and "hgl.invalid" not in blob


def test_a_failing_channel_never_blocks_the_topic_reply(rig):
    choose(replies="full")
    rig.bot.fail = {}

    class ChannelDown(FakeBot):
        def call(self, method, params=None, timeout=15):
            if (params or {}).get("chat_id") == CHANNEL:
                raise TelegramError("channel gone")
            return super().call(method, params, timeout)
    rig.bot = rig.mirror.bot = ChannelDown()
    assert rig.mirror.send_reply("Title", "s1", "Body of the answer.\n\nSummary: ok") > 0
    assert "Body of the answer." in rig.topic_text() and rig.mirror.dropped == 0


# --- files --------------------------------------------------------------------------------------------------------

def send_a_file(rig, tmp_path, name="notes.txt", data=b"hello file", **kw):
    path = tmp_path / name
    path.write_bytes(data)
    rig.bot.calls.clear()
    rig.mirror.send_file("Title", "s1", str(path), **kw)
    return path


def test_files_are_off_by_default(tmp_path):
    rig = Rig(tmp_path)
    send_a_file(rig, tmp_path)
    assert len(rig.bot.methods("sendDocument")) == 1 and to_channel(rig.bot, "sendDocument") == []


def test_files_on_copies_to_the_channel_silently_with_the_topic_name(tmp_path):
    rig = Rig(tmp_path)
    choose(files="on")
    send_a_file(rig, tmp_path, caption="the notes")
    topic = [p for p in rig.bot.methods("sendDocument") if p["chat_id"] == GROUP]
    (copy,) = to_channel(rig.bot, "sendDocument")
    assert len(topic) == 1 and copy["_file"] == "notes.txt" and copy["disable_notification"] == "true"
    assert copy["caption"] == "💬 Title\n📎 the notes" and "message_thread_id" not in copy


def test_photos_keep_their_method(tmp_path):
    rig = Rig(tmp_path)
    choose(files="on")
    send_a_file(rig, tmp_path, "pic.png", b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    assert len(to_channel(rig.bot, "sendPhoto")) == 1


def test_the_channel_caption_is_redacted(tmp_path):
    rig = Rig(tmp_path)
    choose(files="on")
    send_a_file(rig, tmp_path, caption=f"key {SECRET}")
    assert SECRET not in to_channel(rig.bot, "sendDocument")[0]["caption"]


def test_a_failed_channel_copy_never_affects_the_topic(tmp_path):
    rig = Rig(tmp_path)
    choose(files="on")

    class UploadDown(FakeBot):
        def upload(self, method, fields, file_field, filename, payload, timeout=120):
            if fields.get("chat_id") == CHANNEL:
                raise TelegramError("channel down")
            return super().upload(method, fields, file_field, filename, payload, timeout)
    rig.bot = rig.mirror.bot = UploadDown()
    send_a_file(rig, tmp_path)                                    # does not raise
    assert len([p for p in rig.bot.methods("sendDocument") if p["chat_id"] == GROUP]) == 1


def test_a_failed_topic_upload_sends_no_copy(tmp_path):
    rig = Rig(tmp_path)
    choose(files="on")

    class TopicDown(FakeBot):
        def upload(self, method, fields, *a, **k):
            if fields.get("chat_id") != CHANNEL:
                raise TelegramError("nope")
            return super().upload(method, fields, *a, **k)
    rig.bot = rig.mirror.bot = TopicDown()
    with pytest.raises(TelegramError):
        send_a_file(rig, tmp_path)
    assert to_channel(rig.bot, "sendDocument") == []


def test_files_inside_the_bridges_own_folders_are_never_copied(tmp_path):
    rig = Rig(tmp_path)
    choose(files="on")
    path = C.state_dir() / "secret-notes.txt"
    path.write_bytes(b"inside state")
    rig.bot.calls.clear()
    rig.mirror.send_file("Title", "s1", str(path))
    assert to_channel(rig.bot, "sendDocument") == []
    link = tmp_path / "alias.txt"                                 # a link into the folder is followed
    link.symlink_to(path)
    assert M.inside_bridge_folders(os.path.realpath(link))
    assert not M.inside_bridge_folders(os.path.realpath(tmp_path / "plain.txt"))


def test_files_without_a_channel_send_nothing_extra(tmp_path):
    rig = Rig(tmp_path)
    (C.state_dir() / A.FILE).unlink()
    choose(files="on")
    send_a_file(rig, tmp_path)
    assert len(rig.bot.methods("sendDocument")) == 1


# --- the /alerts message and its buttons ----------------------------------------------------------------------------

class Phone:
    def __init__(self, tmp_path):
        self.cfg = make_cfg()
        self.bot = FakeBot()
        self.topics = TopicMap(tmp_path / "state" / "topics.json", self.bot, OWNER)
        self.box = I.Inbox(tmp_path / "state" / "inbox")

    def handle(self, update):
        return I.handle(update, self.cfg, self.bot, self.topics, self.box, None, 1000, None)

    def command(self, text="/alerts", uid=OWNER, chat=OWNER, chat_type="private"):
        m = {"message_id": 9, "date": 2_000_000_000, "text": text, "chat": {"id": chat, "type": chat_type},
             "from": {"id": uid}}
        return self.handle({"update_id": 3, "message": m})

    def press(self, data, uid=OWNER, chat=OWNER, chat_type="private", mid=50):
        q = {"id": "cb1", "from": {"id": uid}, "data": data,
             "message": {"message_id": mid, "chat": {"id": chat, "type": chat_type}}}
        return self.handle({"update_id": 2, "callback_query": q})

    def shown(self):
        return self.bot.methods("sendMessage")[-1]


@pytest.fixture
def phone(tmp_path):
    return Phone(tmp_path)


def labels(params):
    return [b["text"] for row in params["reply_markup"]["inline_keyboard"] for b in row]


def test_alerts_without_a_channel_says_how_to_connect_in_one_line(phone):
    assert phone.command() == "alerts-settings"
    p = phone.shown()
    assert "\n" not in p["text"] and "administrator" in p["text"] and "reply_markup" not in p


def test_alerts_shows_five_buttons_with_todays_defaults(phone):
    register("My alerts")
    phone.command()
    p = phone.shown()
    assert labels(p) == ["🔔 Waiting for you: ✅ on", "⚙️ System events: ✅ on", "💬 Replies: summary",
                         "📎 Files (up to 50 MB): ⛔ off", "📻 Radio summaries: ✅ on", "🎛 Cockpit questions: ✅ on",
                         "✖️ Close menu"]
    assert "My alerts" in p["text"] and "Everyone in the channel" not in p["text"]
    assert [b["callback_data"] for row in p["reply_markup"]["inline_keyboard"] for b in row] == [
        "al:w:off", "al:s:off", "al:r:full", "al:f:on", "al:o:off", "al:c:off", "more:dismiss"]


def test_a_tap_changes_the_setting_and_edits_the_same_message(phone):
    register()
    phone.command()
    assert phone.press("al:r:full", mid=77) == "alerts-changed"
    assert A.settings()["replies"] == "full"
    (edit,) = phone.bot.methods("editMessageText")
    assert edit["message_id"] == 77 and edit["chat_id"] == OWNER and "full text" in labels(edit)[2]
    assert edit["reply_markup"]["inline_keyboard"][2][0]["callback_data"] == "al:r:off"      # cycles on
    assert len(phone.bot.methods("sendMessage")) == 1                                       # no new message
    phone.press("al:r:off")
    phone.press("al:w:off")
    assert A.settings() == {"waiting": "off", "system": "on", "replies": "off", "files": "off", "radio": "on", "cockpit": "on"}


def test_turning_files_on_warns_plainly(phone):
    register()
    phone.press("al:f:on")
    assert "Everyone in the channel will see them" in phone.bot.methods("answerCallbackQuery")[-1]["text"]
    edit = phone.bot.methods("editMessageText")[-1]
    assert "Everyone in the channel sees every file" in edit["text"] and "⛔ off" not in labels(edit)[3]
    phone.press("al:f:off")
    assert "Everyone" not in phone.bot.methods("editMessageText")[-1]["text"]


@pytest.mark.parametrize("who", [dict(uid=STRANGER), dict(uid=OWNER, chat=GROUP, chat_type="supergroup"),
                                 dict(uid=OWNER, chat=STRANGER), dict(uid=STRANGER, chat=GROUP, chat_type="supergroup")])
def test_only_the_owner_in_the_home_chat_can_press(phone, who):
    register()
    for data in ("al:w:off", "al:s:off", "al:r:full", "al:f:on"):
        assert phone.press(data, **who) == "ignored"
    assert A.settings() == A.DEFAULTS and not (C.state_dir() / A.SETTINGS_FILE).exists()
    assert [p["text"] for p in phone.bot.methods("answerCallbackQuery")] == ["Not available here"] * 4
    assert not phone.bot.methods("editMessageText") and not phone.bot.methods("sendMessage")


def test_a_non_owner_cannot_open_the_settings(phone):
    register()
    assert phone.command(uid=STRANGER) != "alerts-settings"
    assert phone.command(uid=OWNER, chat=GROUP, chat_type="supergroup") != "alerts-settings"
    assert not phone.bot.methods("sendMessage") or all("Waiting for you" not in p["text"]
                                                       for p in phone.bot.methods("sendMessage"))


def test_a_bot_sender_is_refused_inside_the_module_too(phone):
    register()
    q = {"id": "c", "from": {"id": OWNER, "is_bot": True}, "data": "al:w:off",
         "message": {"message_id": 1, "chat": {"id": OWNER, "type": "private"}}}
    assert A.button(q, phone.cfg, phone.bot) == "ignored" and A.settings() == A.DEFAULTS


@pytest.mark.parametrize("data", ["al:w:full", "al:s:summary", "al:r:bogus", "al:r:", "al:r", "al:x:on", "al:W:on",
                                  "al:w:on:1", "al:w:on ", "al:w:on\n", "al:w:ON", "al::on", "al:f:yes", "al:w:\u0661",
                                  "al:w:off;al:f:on", "al:f:on\nal:f:on", "al:rr:full", "al:", "al:f:on" + "x" * 5000])
def test_unknown_or_malformed_values_change_nothing(phone, data):
    register()
    assert phone.press(data) == "alerts-ignored"
    assert A.settings() == A.DEFAULTS and not (C.state_dir() / A.SETTINGS_FILE).exists()
    assert phone.bot.methods("answerCallbackQuery")[-1]["text"] == "Not available here"
    assert not phone.bot.methods("editMessageText")


def test_a_press_after_the_channel_was_removed_changes_nothing(phone):
    assert phone.press("al:f:on") == "alerts-ignored"
    assert A.settings() == A.DEFAULTS and "No alerts channel" in phone.bot.methods("editMessageText")[-1]["text"]


def test_a_failing_write_is_reported_not_raised(phone, monkeypatch):
    register()
    monkeypatch.setattr(A, "write_private", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    assert phone.press("al:w:off") == "alerts-ignored"
    assert "Could not save" in phone.bot.methods("answerCallbackQuery")[-1]["text"]
    assert A.settings() == A.DEFAULTS


def test_the_channel_proposal_button_still_works(phone, monkeypatch):
    seen = []
    monkeypatch.setattr(A, "confirm", lambda q, cfg, bot: seen.append(q["data"]) or "registered")
    assert phone.press(f"alerts:use:{CHANNEL}") == "alerts-registered"
    assert seen == [f"alerts:use:{CHANNEL}"]
    assert phone.bot.methods("answerCallbackQuery")[-1]["text"] == "✅ The alerts channel is connected."
    seen.clear()
    register()
    phone.press("al:w:off")
    assert seen == []                                            # the settings prefix never reaches confirm()


def test_alerts_is_in_the_command_menu():
    bot = FakeBot()
    I.set_commands(bot, make_cfg())
    assert any(c["command"] == "alerts" and c["description"] for call in bot.methods("setMyCommands")
               for c in call["commands"])


# --- review round: guard per finding -----------------------------------------------------------------------------

from ctb import creds, flood  # noqa: E402


@pytest.mark.parametrize("text,secret", [
    ("db postgres://app:hunter22@db.example.com:5432/x ok", "hunter22"),
    ("password: hunter2", "hunter2"),
    ("pwd=abc", "abc"),
    ("סיסמה: abc12", "abc12"),
    ("הסיסמה היא abc12", "abc12"),
    ("קוד אימות: 482913", "482913"),
    ("your one-time code: 482913", "482913"),
])
def test_1_known_secret_shapes_are_cleaned(text, secret):
    assert secret not in creds.redact_channel(text)


@pytest.mark.parametrize("text", ["the password is wrong", "הסיסמה שלך ארוכה", "error code: 404", "git show a1b2c3d4e5f6",
                                  "see https://github.com/x/y and ssh://git@github.com/x", "verification code is needed"])
def test_1_ordinary_text_is_left_alone(text):
    assert creds.redact(text) == text and creds.redact_channel(text) == text


def test_1_full_mode_warns_that_readers_see_everything(phone):
    register()
    choose(replies="full")
    phone.command()
    assert "Everyone in the channel can read full replies" in phone.shown()["text"]
    phone.press("al:r:full")
    assert "Full replies are on" in phone.bot.methods("answerCallbackQuery")[-1]["text"]
    assert "always cleaned" not in phone.shown()["text"] and "cannot catch everything" in phone.shown()["text"]


def test_1_full_reply_cleans_labelled_secrets(cfg):
    register()
    choose(replies="full")
    bot = FakeBot()
    A.send_reply(cfg, "db postgres://app:hunter22@db.example.com/x\npassword: hunter2\nקוד אימות: 482913", "s", bot=bot)
    text = to_channel(bot)[0]["text"]
    assert not any(x in text for x in ("hunter22", "hunter2", "482913"))


@pytest.mark.parametrize("rel", [".ssh/id_rsa", ".aws/credentials", ".gnupg/key", ".config/gh/hosts.yml", ".netrc",
                                 "proj/.env", "proj/.env.local", "certs/server.pem", "certs/a.key", "x/id_ed25519",
                                 "Library/Keychains/login.keychain-db"])
def test_2_credential_paths_are_never_copied(tmp_path, rel):
    rig = Rig(tmp_path)
    choose(files="on")
    path = tmp_path / "home" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"harmless bytes")
    rig.bot.calls.clear()
    rig.mirror.send_file("Title", "s1", str(path))
    assert len(rig.bot.methods("sendDocument")) == 1 and to_channel(rig.bot, "sendDocument") == []   # topic unchanged


def test_2_symlink_to_a_credential_is_resolved(tmp_path):
    rig = Rig(tmp_path)
    choose(files="on")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_bytes(b"k")
    (tmp_path / "innocent.txt").symlink_to(tmp_path / ".ssh" / "id_rsa")
    assert not M.channel_may_copy(rig.cfg, str(tmp_path / "innocent.txt"))


def test_2_claude_home_profiles_and_api_files_dir_are_denied(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    for var in ("CTB_CLAUDE_HOME", "CTB_PROFILES_DIR"):
        folder = tmp_path / var
        folder.mkdir()
        (folder / "x.txt").write_text("x")
        monkeypatch.setenv(var, str(folder))
        assert not M.channel_may_copy(rig.cfg, str(folder / "x.txt"))
    served = tmp_path / "served"
    served.mkdir()
    (served / "f.txt").write_text("x")
    rig.cfg.api_files_dir = str(served)
    assert not M.channel_may_copy(rig.cfg, str(served / "f.txt"))
    (tmp_path / "ok.txt").write_text("fine")
    assert M.channel_may_copy(rig.cfg, str(tmp_path / "ok.txt"))


def test_2_a_file_holding_a_secret_pattern_is_not_copied(tmp_path):
    rig = Rig(tmp_path)
    choose(files="on")
    send_a_file(rig, tmp_path, data=b"notes\nkey " + SECRET.encode() + b"\n")
    assert len(rig.bot.methods("sendDocument")) == 1 and to_channel(rig.bot, "sendDocument") == []


def test_2_the_check_runs_before_the_file_is_read(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    choose(files="on")
    seen = []
    real = M.read_regular_file
    monkeypatch.setattr(M, "channel_may_copy", lambda cfg, path: seen.append("check") or False)
    monkeypatch.setattr(M, "read_regular_file", lambda *a: seen.append("read") or real(*a))
    send_a_file(rig, tmp_path)
    assert seen == ["check", "read"]


def test_3_a_new_channel_starts_from_the_defaults_and_shows_the_panel(cfg):
    register()
    choose(files="on", replies="full")
    bot = FakeBot()
    chat = {"id": -1005555, "type": "channel", "title": "Other"}
    A._register(chat, cfg, bot)
    assert A.settings() == A.DEFAULTS
    assert any("Alerts channel: " in p["text"] and p.get("reply_markup") for p in bot.methods("sendMessage")
               if p["chat_id"] == OWNER)


def test_3_reconnecting_the_same_channel_keeps_the_choices_and_removal_forgets_them(cfg):
    register()
    choose(files="on")
    A._register({"id": CHANNEL, "type": "channel", "title": "Alerts"}, cfg, FakeBot())
    assert A.settings()["files"] == "on"
    update = {"chat": {"id": CHANNEL, "type": "channel"}, "from": {"id": OWNER}, "new_chat_member": {"status": "left"}}
    assert A.joined(update, cfg, FakeBot()) == "removed"
    assert A.settings() == A.DEFAULTS


def test_5_secrets_in_the_topic_name_and_filename_are_cleaned(tmp_path):
    rig = Rig(tmp_path)
    cfg = rig.cfg
    C.write_private(C.state_dir() / "topics.json", json.dumps({"s1": {"name": f"deploy {SECRET}", "thread": 5}}))
    choose(replies="full", files="on")
    bot = FakeBot()
    A.send_reply(cfg, "hello", "s1", bot=bot)
    assert SECRET not in json.dumps(to_channel(bot))
    A.send_file(cfg, "s1", "t", "sendDocument", "document", f"{SECRET}.txt", b"x", bot=bot)
    assert SECRET not in json.dumps(to_channel(bot, "sendDocument"))


def test_6_channel_sends_go_through_the_flood_gate(cfg):
    register()
    choose(replies="full", files="on")
    flood.record_429(CHANNEL, 30)
    bot = FakeBot()
    assert not A.alert(cfg, "x", bot=bot) and not A.send_reply(cfg, "x", bot=bot)
    assert not A.send_file(cfg, "s", "t", "sendDocument", "document", "a.txt", b"x", bot=bot)
    assert bot.calls == []


def test_6_a_failed_waiting_ring_does_not_silence_the_session(cfg):
    register()

    class Down(FakeBot):
        def call(self, method, params=None, timeout=15):
            raise TelegramError("down")
    M._notify_alert(cfg, "s1", Down())
    bot = FakeBot()
    M._notify_alert(cfg, "s1", bot)
    assert len(to_channel(bot)) == 1


def test_low_budget_is_bounded_and_the_cut_note_is_left(cfg, monkeypatch):
    register()
    choose(replies="full")
    bot = FakeBot()
    timeouts = []
    real = bot.call

    def call(method, params=None, timeout=15):
        timeouts.append(timeout)
        return real(method, params, timeout)
    bot.call = call
    clock = iter([0, 0, 0, 100, 100, 100, 100, 100, 100, 100])
    monkeypatch.setattr(A.time, "monotonic", lambda: next(clock))
    text = "\n\n".join("para " * 300 for _ in range(10))
    assert not A.send_reply(cfg, text, "s", bot=bot, url="https://t.me/c/1/2/3")
    sent = to_channel(bot)
    assert "rest of the reply is in the topic" in sent[-1]["text"] and max(timeouts) <= 5


def test_low_unheld_handoff_link_loses_its_host(cfg):
    register()
    bot = FakeBot()
    A.alert(cfg, f"open {HANDOFF}", "s", bot=bot)
    assert "tailabc123" not in to_channel(bot)[0]["text"]


def test_low_hebrew_title_is_isolated_and_texts_are_current(phone):
    phone.cfg.ui_language = "he"
    register("Chan")
    phone.command()
    assert "⁨Chan⁩" in phone.shown()["text"]
    assert "50" in labels(phone.shown())[3]
    from ctb.i18n import t
    for lang in ("en", "he"):
        assert "Only what needs you" not in t("alerts_channel_intro", lang=lang)


# --- re-review round ---------------------------------------------------------------------------------------------

import threading  # noqa: E402

from ctb import telegram as T  # noqa: E402
from test_hotfixes import running_relay, short_relay_state  # noqa: E402,F401  (the fixture and the relay runner)

class AdminBot(FakeBot):
    """A bot that answers getChat and getChatAdministrators about the chat `admin_of`."""
    admin_of = CHANNEL
    kind = "channel"

    def call(self, method, params=None, timeout=15):
        if method in ("getChat", "getChatAdministrators"):
            self.calls.append((method, params or {}))
            if (params or {}).get("chat_id") != self.admin_of:
                raise TelegramError("chat not found")
            if method == "getChat":
                return {"id": self.admin_of, "type": self.kind}
            return [{"user": {"id": OWNER}}]
        return super().call(method, params, timeout)


def plant(chat_id, **extra):
    """What a session can do: write the two files itself (no signature, not through the listener)."""
    C.write_private(C.state_dir() / A.FILE, json.dumps({"id": chat_id, "title": "Planted", "bot": "b"}))
    C.write_private(C.state_dir() / A.SETTINGS_FILE, json.dumps({"replies": "full", "files": "on", **extra}))


def test_r1_planted_files_send_nothing_anywhere(cfg):
    plant(-1009999)
    bot = FakeBot()
    assert not A.alert(cfg, "x", bot=bot) and not A.send_reply(cfg, "x", bot=bot)
    assert not A.send_file(cfg, "s", "t", "sendDocument", "document", "a.txt", b"x", bot=bot)
    M._notify_alert(cfg, "s1", bot)
    assert [m for m, p in bot.calls if m in ("sendMessage", "sendDocument")] == []


def test_r1_planted_settings_do_not_turn_on_full_replies_or_files(cfg):
    register()                                       # the real, trusted channel
    plant(CHANNEL)                                   # but the settings are the session's
    bot = FakeBot()
    assert not A.send_reply(cfg, "hello", bot=bot)
    assert not A.send_file(cfg, "s", "t", "sendDocument", "document", "a.txt", b"x", bot=bot)
    assert A.settings(cfg) == A.DEFAULTS and A.settings()["replies"] == "full"      # only the unsigned read sees it
    assert A.alert(cfg, "ring", bot=bot) and len(to_channel(bot)) == 1             # the defaults still work


def test_r1_settings_signed_by_the_owner_count_and_a_changed_file_does_not(cfg):
    register()
    choose(replies="full")
    assert A.settings(cfg)["replies"] == "full"
    path = C.state_dir() / A.SETTINGS_FILE
    path.write_text(path.read_text().replace('"full"', '"off"'))                    # a session edits the file
    assert A.settings(cfg)["replies"] == "full"                                    # the signed choice stands
    A._saved.clear()                                                               # a process that knew nothing
    assert A.settings(cfg) == A.DEFAULTS


def test_m1_an_old_signed_copy_written_back_changes_nothing(cfg):
    register()
    A.set_option("replies", "full", cfg)
    old = (C.state_dir() / A.SETTINGS_FILE).read_text()                              # signed, replies=full
    A.set_option("replies", "off", cfg)
    A.set_option("files", "off", cfg)
    (C.state_dir() / A.SETTINGS_FILE).write_text(old)                                # rolled back by a session
    assert A.settings(cfg)["replies"] == "off"
    assert A.public_state(cfg, AdminBot())["settings"]["replies"] == "off"           # what the listener serves
    bot = FakeBot()
    assert not A.send_reply(cfg, "hello", "s", bot=bot) and to_channel(bot) == []
    A.set_option("system", "off", cfg)                                               # the owner's next tap still works
    assert A.settings(cfg)["system"] == "off" and A.settings(cfg)["replies"] == "off"


def test_m1_a_copy_signed_for_another_channel_does_not_work_here(cfg):
    register()
    A.save_settings(cfg, {**A.DEFAULTS, "replies": "full", "files": "on"}, CHANNEL - 5)   # another channel's copy
    assert A.settings(cfg) == A.DEFAULTS
    A.save_settings(cfg, {**A.DEFAULTS, "replies": "full"}, CHANNEL)
    assert A.settings(cfg)["replies"] == "full"


def test_m1_a_copy_from_before_a_reconnect_stays_dead(cfg):
    register()
    A.set_option("replies", "full", cfg)
    old = (C.state_dir() / A.SETTINGS_FILE).read_text()
    update = {"chat": {"id": CHANNEL, "type": "channel"}, "from": {"id": OWNER}, "new_chat_member": {"status": "left"}}
    assert A.joined(update, cfg, FakeBot()) == "removed"
    A._register({"id": CHANNEL, "type": "channel", "title": "A"}, cfg, FakeBot())     # connected again
    assert A.settings(cfg) == A.DEFAULTS
    (C.state_dir() / A.SETTINGS_FILE).write_text(old)
    assert A.settings(cfg) == A.DEFAULTS


def test_m1_the_counter_only_goes_up(cfg):
    register()
    seen = []
    for value in ("full", "off", "summary"):
        A.set_option("replies", value, cfg)
        seen.append(json.loads((C.state_dir() / A.SETTINGS_FILE).read_text())["n"])
    assert seen == sorted(set(seen))


def test_m1_a_sender_with_the_token_asks_the_listener_first(cfg, monkeypatch):
    register()
    choose(replies="full")                            # what the file says
    told = {"channel": {"id": CHANNEL, "title": "A", "bot": "b"}, "settings": dict(A.DEFAULTS, replies="off")}
    monkeypatch.setattr(A, "relay_call", lambda request, timeout=15, sock_path=None: told)
    got = A.state(cfg, FakeBot())
    assert got and got[1]["replies"] == "off"
    told["channel"] = None                            # the listener trusts no channel: the file cannot say otherwise
    assert A.state(cfg, FakeBot()) is None

    def down(request, timeout=15, sock_path=None):
        raise TelegramError("credential_unavailable")
    monkeypatch.setattr(A, "relay_call", down)
    got = A.state(cfg, FakeBot())                     # listener down: the token sender checks itself
    assert got and got[1]["replies"] == "full"


def test_r1_a_session_without_the_token_asks_the_listener(cfg, short_relay_state):
    """No token means the relay: the sender gets the channel and the choices from the listener, and the
    file it could write decides nothing."""
    register()
    choose(replies="full")
    plant(-1009999)                                  # a session now rewrites both files
    listener = AdminBot()
    with running_relay(listener, cfg):
        remote = T.Bot(None)
        assert A.state(cfg, remote) is None          # the file's id is not one the owner administers
        register()                                   # the owner registers through the listener
        choose(replies="full")
        got = A.state(cfg, remote)
        assert got and got[0]["id"] == CHANNEL and got[1]["replies"] == "full"
        assert A.send_reply(cfg, "hello", "s", bot=remote)
    assert [p["chat_id"] for p in listener.methods("sendMessage")] == [CHANNEL]


def test_r1_a_listener_that_is_down_means_no_alert(cfg, short_relay_state):
    register()
    assert A.state(cfg, T.Bot(None)) is None


def test_r2_the_channel_is_checked_as_a_channel(cfg):
    register(trusted=False)
    group = AdminBot()
    group.kind = "supergroup"                        # the owner administers it, but it is not a channel
    assert A.trusted_channel(cfg, group) is None
    A._untrusted.clear()
    assert A.trusted_channel(cfg, AdminBot()) == CHANNEL


def test_r2_a_planted_id_the_owner_does_not_administer_is_refused_and_not_asked_again_at_once(cfg):
    plant(-1009999)
    bot = AdminBot()                                 # administers CHANNEL only
    assert A.trusted_channel(cfg, bot) is None and A.trusted_channel(cfg, bot) is None
    assert len(bot.methods("getChat")) == 1


def test_r2_a_registered_channel_needs_no_call_until_the_trust_expires(cfg, monkeypatch):
    A._register({"id": CHANNEL, "type": "channel", "title": "A"}, cfg, FakeBot())
    bot = AdminBot()
    assert A.trusted_channel(cfg, bot) == CHANNEL and bot.calls == []
    now = time.monotonic()
    monkeypatch.setattr(A.time, "monotonic", lambda: now + A.TRUST_FOR + 1)      # an hour later
    assert A.trusted_channel(cfg, bot) == CHANNEL and len(bot.methods("getChatAdministrators")) == 1
    gone = FakeBot()                                 # the owner is no longer an administrator
    A._trusted.clear()
    A._untrusted.clear()
    assert A.trusted_channel(cfg, gone) is None


def test_r2_removing_the_bot_ends_the_trust(cfg):
    A._register({"id": CHANNEL, "type": "channel", "title": "A"}, cfg, FakeBot())
    update = {"chat": {"id": CHANNEL, "type": "channel"}, "from": {"id": OWNER}, "new_chat_member": {"status": "left"}}
    assert A.joined(update, cfg, FakeBot()) == "removed"
    register(trusted=False)                          # the file comes back
    assert A.trusted_channel(cfg, FakeBot()) is None


def test_r1_the_relay_refuses_a_channel_only_the_file_names(short_relay_state):
    register(trusted=False)
    bot = FakeBot()
    with running_relay(bot):
        with pytest.raises(T.TelegramError, match="chat_not_relayed"):
            T.Bot(None).call("sendMessage", {"chat_id": CHANNEL, "text": "ring"})
    assert bot.methods("sendMessage") == []


def test_r7_the_trust_helpers_keep_no_global_statement_above_a_docstring():
    import ast
    import inspect
    for fn in (A.joined, A._register, A.trusted_channel):
        body = ast.parse(inspect.getsource(fn).lstrip()).body[0].body
        assert not isinstance(body[0], ast.Global)


@pytest.mark.parametrize("text,secret", [
    ("DB_PASSWORD=hunter2", "hunter2"), ("export PGPASSWORD=s3cret", "s3cret"), ("MYSQL_PWD=abc", "abc"),
    ('{"password": "hunter2", "user": "bob"}', "hunter2"), ("The password is hunter2", "hunter2"),
    ("mysql --password hunter2 -u bob", "hunter2"), ("psql --password=ab", "=ab"),
    ("api_key: abc123", "abc123"), ("PIN: 4821", "4821"), ("your code is 482913", "482913"),
    ("the verification code is ABCDEF", "ABCDEF"), ("Your OTP is 4821.", "4821"),
])
def test_r3_channel_full_mode_hides_short_labelled_secrets(cfg, text, secret):
    register()
    choose(replies="full")
    bot = FakeBot()
    A.send_reply(cfg, "log:\n" + text, "s", bot=bot)
    sent = to_channel(bot)[0]["text"]
    assert secret not in sent and "[redacted]" in sent


@pytest.mark.parametrize("text", [
    "def f(password: str, token: str)", "token: ${{ secrets.GITHUB_TOKEN }}", "otp=generate_otp()",
    "pwd: /home/app/project", "Secret: none", "OTP: enabled", "Input token: 1,234", "access code: see the doc",
    "הסוד: להתמיד", "the password is wrong", "the pin is stuck", "error code: 404", "verification code is needed",
])
def test_r2_ordinary_text_with_a_label_word_is_not_mangled_in_the_channel(text):
    assert creds.redact_channel(text) == text


@pytest.mark.parametrize("text", [
    "password: hunter2", "pwd=abc", "קוד אימות: 482913", "DB_PASSWORD=hunter2", "The password is hunter2",
    "postgres://app:hunter22@db.example.com/x", "--password hunter2", "your code is 482913",
])
def test_r2_topic_replies_keep_the_text_as_before_the_alerts_work(text):
    """redact() (used for every topic reply) has none of the label-based cleaning: that is channel-only."""
    cleaned = creds.redact(text)
    assert cleaned == text


def test_r2_a_topic_reply_is_not_mangled_but_the_channel_copy_is_cleaned(tmp_path):
    rig = Rig(tmp_path)
    choose(replies="full")
    body = "Use `def f(password: str, token: str)`; token: ${{ secrets.GITHUB_TOKEN }}; otp=generate_otp(); PIN: 4821"
    rig.reply(body)
    assert "def f(password: str, token: str)" in rig.topic_text() and "otp=generate_otp()" in rig.topic_text()
    assert "PIN: 4821" in rig.topic_text()
    sent = to_channel(rig.bot)[0]["text"]
    assert "PIN: [redacted]" in sent and "generate_otp()" in sent and "password: str" in sent


@pytest.mark.parametrize("name", ["prod.env", "staging.ENV", "secrets.yaml", "my-Secret-notes.txt", ".bash_history",
                                  "zsh_history", ".psql_history"])
def test_r4_more_names_are_never_copied(tmp_path, name):
    rig = Rig(tmp_path)
    path = tmp_path / name
    path.write_text("harmless")
    assert not M.channel_may_copy(rig.cfg, str(path))


@pytest.mark.parametrize("folder", [".codex", ".gemini"])
def test_r4_agent_homes_are_denied(tmp_path, folder):
    rig = Rig(tmp_path)
    path = tmp_path / folder / "sessions" / "x.txt"
    path.parent.mkdir(parents=True)
    path.write_text("harmless")
    assert not M.channel_may_copy(rig.cfg, str(path))


def test_r4_a_hard_linked_file_is_never_copied(tmp_path):
    rig = Rig(tmp_path)
    (tmp_path / "original.txt").write_text("harmless")
    os.link(tmp_path / "original.txt", tmp_path / "innocent.txt")
    assert not M.channel_may_copy(rig.cfg, str(tmp_path / "innocent.txt"))
    (tmp_path / "single.txt").write_text("harmless")
    assert M.channel_may_copy(rig.cfg, str(tmp_path / "single.txt"))


@pytest.mark.parametrize("data", [b"DB_PASSWORD=hunter2\n", b"x\nPIN: 4821\n", b'{"password": "abc"}',
                                  b"a" * 40 + b"B1" * 10, b"Authorization: Bearer abc.def"])
def test_r4_looks_secret_runs_the_full_cleaning_patterns(data):
    assert M.looks_secret(data)
    assert not M.looks_secret(b"plain notes\nnothing to see here\n")


# --- final review round ------------------------------------------------------------------------------------------

import re as _re  # noqa: E402


def test_r3_the_file_scan_waits_for_the_cheap_checks(tmp_path, monkeypatch):
    rig = Rig(tmp_path)                              # files are off by default
    scanned = []
    monkeypatch.setattr(M, "looks_secret", lambda payload: scanned.append(1) or False)
    send_a_file(rig, tmp_path)
    assert scanned == []
    choose(files="on")
    send_a_file(rig, tmp_path)
    assert scanned == [1]


def test_r3_no_trusted_channel_means_no_scan(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    choose(files="on")
    register(trusted=False)
    A._trusted.clear()
    monkeypatch.setattr(M, "looks_secret", lambda payload: pytest.fail("scanned"))
    send_a_file(rig, tmp_path)
    assert to_channel(rig.bot, "sendDocument") == []


def test_r3_a_scan_that_cannot_finish_fails_closed(monkeypatch):
    assert M.looks_secret(b"x" * (M.SCAN_LIMIT + 1))                # too big to scan: not copied
    clock = iter([0.0, 100.0])
    monkeypatch.setattr(M.time, "monotonic", lambda: next(clock, 100.0))
    assert M.looks_secret(b"plain text " * 100)                      # past the time bound: not copied
    monkeypatch.setattr(M.time, "monotonic", lambda: 0.0)
    assert not M.looks_secret(b"plain text " * 100)


@pytest.mark.parametrize("shape", ["a-" * 100000, "1" * 200000, "a1" * 100000, "a." * 100000, "a/" * 100000,
                                   "1:" * 100000, "password " * 25000, "http://" * 25000, "a_" * 100000])
def test_r3_cleaning_is_linear_on_crafted_input(shape):
    start = time.monotonic()
    creds.redact_channel(shape)
    assert time.monotonic() - start < 2.0


@pytest.mark.parametrize("text,secret", [
    ("DB_PASS=hunter9", "hunter9"), ("pass: s3cr", "s3cr"), ("pw=abc", "abc"), ("password hunter2", "hunter2"),
    ("password:\n  hunter2\nnext: 1", "hunter2"), ("mysql -u root -phunter2", "hunter2"),
    ("mysql -u root -p hunter2 db", "hunter2"), ("curl -u bob:hunter2 https://x.test", "hunter2"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"), ("Your Telegram code: 48291", "48291"),
    ("OTP 482913", "482913"), ("code: 482913", "482913"), ("PASSWORD='correct horse'", "correct horse"),
    ('{"password": "correct horse"}', "correct horse"), ("סיסמה: משהו", "משהו"), ("הסיסמה היא סודי", "סודי"),
])
def test_r4_more_forms_are_cleaned_in_the_channel_only(text, secret):
    assert secret not in creds.redact_channel(text)


@pytest.mark.parametrize("text", [
    "password hunter2", "pw=abc", "DB_PASS=hunter9", "mysql -phunter2", "OTP 482913", "code: 482913",
    "סיסמה: משהו", "curl -u bob:hunter2 https://x.test",
])
def test_r4_topic_replies_are_untouched_by_the_new_forms(text):
    assert creds.redact(text) == text


@pytest.mark.parametrize("text", [
    "mysql -h x -P 3306 -u root", "ssh -p 22 host", "git push -u origin main", "tests pass: ok", "password reset link",
    "exit code: 1", "password:\n  - a", "Basic usage of the tool", "curl -U x https://a.test", "the code: see below",
])
def test_r4_ordinary_commands_and_prose_are_left_alone(text):
    assert creds.redact_channel(text) == text


@pytest.mark.parametrize("name", ["prod.env.local", "app.env.prod", "env.prod", "creds.txt", "credsfile", ".htpasswd",
                                  "pass.txt", "gh-token.json", "service-account-prod.json", "my-api-key.txt", "kubeconfig",
                                  "cookies.sqlite", "Login Data"])
def test_r5_more_names_are_never_copied(tmp_path, name):
    rig = Rig(tmp_path)
    path = tmp_path / name
    path.write_text("harmless")
    assert not M.channel_may_copy(rig.cfg, str(path))


@pytest.mark.parametrize("rel", [".agents/rules.md", ".local/share/keyrings/x.txt"])
def test_r5_more_folders_are_denied(tmp_path, rel):
    rig = Rig(tmp_path)
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text("harmless")
    assert not M.channel_may_copy(rig.cfg, str(path))
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "notes.txt").write_text("fine")
    assert M.channel_may_copy(rig.cfg, str(tmp_path / "local" / "notes.txt"))


def test_r5_a_file_swapped_after_the_check_is_not_copied(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    choose(files="on")
    path = tmp_path / "notes.txt"
    path.write_bytes(b"hello file")
    other = tmp_path / "other.txt"
    other.write_bytes(b"other")
    real_check = M.channel_may_copy

    def check_then_swap(cfg, p):
        ok = real_check(cfg, p)
        path.unlink()
        other.rename(path)                           # a different file is now behind the same name
        return ok
    monkeypatch.setattr(M, "channel_may_copy", check_then_swap)
    rig.bot.calls.clear()
    rig.mirror.send_file("Title", "s1", str(path))
    assert to_channel(rig.bot, "sendDocument") == []


@pytest.mark.parametrize("shape", ["mysql " * 7000, "curl " * 8000, "mysql -x " * 5000, "curl -u " * 5000])
def test_m2_command_flag_patterns_are_bounded(shape):
    start = time.monotonic()
    creds.redact_channel(shape)
    assert time.monotonic() - start < 0.5


def test_m2_the_scan_looks_at_its_time_limit_between_small_slices(monkeypatch):
    assert M.SCAN_SLICE <= 64 << 10
    ticks = iter(range(1000))
    monkeypatch.setattr(M.time, "monotonic", lambda: next(ticks) * 1.0)             # one second per look
    assert M.looks_secret(b"plain text\n" * 20000)                                  # many slices: the limit is hit


# --- a settings file signed before the radio choice was added keeps the owner's choices ------------------------

def _old_shape(cfg, choices, counter=5):
    """A file as the previous version wrote it: four keys, signed over those four."""
    mac = A._mac(cfg, {"choices": choices, "channel": CHANNEL, "n": counter})
    C.write_private(C.state_dir() / A.SETTINGS_FILE, json.dumps(dict(choices, n=counter, mac=mac)))


def test_a_file_signed_in_the_previous_shape_keeps_the_owners_choices_and_is_signed_again(cfg):
    register()
    old = {"waiting": "off", "system": "off", "replies": "full", "files": "on"}
    _old_shape(cfg, old)
    got = A.settings(cfg)
    assert got == {**old, "radio": "on", "cockpit": "on"}  # nothing reset, the new choices take their defaults
    raw = json.loads((C.state_dir() / A.SETTINGS_FILE).read_text())
    assert set(raw) == {"waiting", "system", "replies", "files", "radio", "cockpit", "n", "mac"} and raw["n"] > 5
    assert A._read_signed(cfg, CHANNEL)[1] == got  # the re-saved file verifies in the current shape
    A._saved.clear()  # a fresh process reads the same
    assert A.settings(cfg) == got and not A.enabled("system", cfg)


def test_a_tampered_old_shape_file_is_still_refused(cfg):
    register()
    _old_shape(cfg, {"waiting": "on", "system": "on", "replies": "summary", "files": "off"})
    raw = json.loads((C.state_dir() / A.SETTINGS_FILE).read_text())
    raw["files"] = "on"
    C.write_private(C.state_dir() / A.SETTINGS_FILE, json.dumps(raw))
    A._saved.clear()
    assert A.settings(cfg)["files"] == "off"
