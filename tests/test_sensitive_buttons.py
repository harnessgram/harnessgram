"""Sensitive-button markers for the Harnessgram app (sensitive_buttons.py): what is marked, what never is, the
64-byte limit, the strip before any handler, and the "sensitive" flag of a decision."""
import json

import pytest

from conftest import GOOD_TOKEN
from ctb import inbox as I, sensitive_buttons as SB, telegram


def kb(*buttons):
    return {"inline_keyboard": [[b] for b in buttons]}


def cb(text, data):
    return {"text": text, "callback_data": data}


@pytest.mark.parametrize("data", [
    "more:/account add", "more:/account add work-2", "more:/account remove work-2 yes",
    "more:/account use work-2", "more:/account move work-2", "more:/account move work-2 now",
    "mb:a:123456789:0123456789abcdef", "mb:r:123456789", "sm:xy:0123456789ab:0o",
    "alerts:use:-1001234567890", "al:r:full", "al:f:on",
])
def test_every_acting_sensitive_button_is_marked(data):
    out = SB.mark_markup(kb(cb("Go", data)))
    assert out["inline_keyboard"][0][0]["callback_data"] == "hg!" + data


@pytest.mark.parametrize("data", [
    "more:/account remove work-2",       # the picker step: its confirmation is the acting button
    "more:/account remove",
    "more:/account", "mb:i:123:0123456789abcdef", "mb:d:123:0123456789abcdef",
    "sm:x1:0123456789ab:0o", "sm:x2:0123456789ab:0o", "sm:d:0123456789ab:0o",
    "al:r:summary", "al:r:off", "al:f:off", "close:yes", "dl:now:5", "more:dismiss", "noop", "k:enter",
    "more:/account remove work-2 yes extra",
])
def test_refusals_steps_and_ordinary_buttons_stay_unmarked(data):
    markup = kb(cb("Go", data))
    assert SB.mark_markup(markup)["inline_keyboard"][0][0]["callback_data"] == data


def test_only_the_acting_button_of_a_confirmation_is_marked():
    markup = {"inline_keyboard": [[cb("✅ Yes, remove work", "more:/account remove work yes")],
                                  [cb("No", "more:/account")], [cb("Close", "more:dismiss")]]}
    out = SB.mark_markup(markup)
    assert [r[0]["callback_data"] for r in out["inline_keyboard"]] == [
        "hg!more:/account remove work yes", "more:/account", "more:dismiss"]
    assert markup["inline_keyboard"][0][0]["callback_data"] == "more:/account remove work yes"  # input untouched


@pytest.mark.parametrize("label, marked", [
    ("Deploy to production", True), ("Delete the old branch", True), ("Publish the video", True),
    ("Pay the invoice", True), ("Submit for App Review", True), ("מחיקה של הקבצים", True),
    ("Run the tests", False), ("Option B", False), ("No", False), ("Cancel", False), ("לא", False),
])
def test_option_buttons_are_marked_by_their_label(label, marked):
    for data in ("agr:12:0", "dg:x:1:0123456789ab"):
        got = SB.mark_markup(kb(cb(label, data)))["inline_keyboard"][0][0]["callback_data"]
        assert got == ("hg!" + data if marked else data), (label, data)


def test_marking_is_idempotent_and_leaves_other_buttons_alone():
    markup = kb(cb("Go", "hg!al:r:full"), {"text": "Open", "url": "https://t.me/x"}, "junk", cb("x", 5))
    assert SB.mark_markup(markup) is markup
    once = SB.mark_markup(kb(cb("Go", "al:r:full")))
    assert SB.mark_markup(once) is once
    assert SB.mark_title(SB.mark_title("Answer")) == "\U0001F510 Answer"


def test_every_listed_shape_fits_the_callback_limit_at_its_longest():
    longest = ["more:/account add " + "a" * 20, "more:/account remove " + "a" * 20 + " yes",
               "more:/account use " + "a" * 20, "more:/account move " + "a" * 20 + " now",
               "mb:a:" + "9" * 16 + ":" + "f" * 16, "mb:r:" + "9" * 16, "sm:xy:" + "f" * 12 + ":99999o",
               "alerts:use:-" + "9" * 19, "agr:" + "9" * 9 + ":99", "dg:x:99:" + "f" * 12]
    for data in longest:
        assert SB.callback_sensitive(data, "Delete everything"), data
        assert len((SB.CALLBACK_MARK + data).encode()) <= SB.CALLBACK_LIMIT, data


def test_a_marked_button_that_would_pass_the_limit_is_left_out_never_sent_unmarked(capsys, monkeypatch):
    # Every listed shape is bounded below the limit (the test above); a future, wider shape must fail closed.
    monkeypatch.setattr(SB, "SENSITIVE_CALLBACKS", SB.SENSITIVE_CALLBACKS + (__import__("re").compile(r"wide:.+"),))
    over = "wide:" + "f" * 57  # 62 bytes: fits alone, not with the marker
    out = SB.mark_markup({"inline_keyboard": [[cb("Delete forever", over), cb("Cancel", "sm:d:x:0o")],
                                              [cb("Delete forever", over)]]})
    assert out["inline_keyboard"] == [[cb("Cancel", "sm:d:x:0o")]]
    err = capsys.readouterr().err
    assert "left out" in err and over not in err


def test_every_bot_call_leaves_marked(monkeypatch):
    sent = []
    bot = telegram.Bot(GOOD_TOKEN)
    monkeypatch.setattr(bot, "_guarded", lambda chat, url, data, headers, timeout: sent.append(json.loads(data)) or {})
    bot.call("sendMessage", {"chat_id": 1, "text": "x", "reply_markup": kb(cb("✅ Use this channel", "alerts:use:-100"),
                                                                           cb("Not now", "more:dismiss"))})
    bot.call("editMessageReplyMarkup", {"chat_id": 1, "message_id": 2, "reply_markup": kb(cb("Go", "mb:r:5"))})
    bot.call("sendMessage", {"chat_id": 1, "text": "plain"})
    rows = sent[0]["reply_markup"]["inline_keyboard"]
    assert [r[0]["callback_data"] for r in rows] == ["hg!alerts:use:-100", "more:dismiss"]
    assert sent[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "hg!mb:r:5"
    assert "reply_markup" not in sent[2]


def test_the_marker_is_stripped_before_any_handler_reads_the_callback(monkeypatch):
    seen = []
    monkeypatch.setattr(I, "_guarded_button", lambda q, *a, **k: seen.append(q["data"]) or "button")
    for data in ("hg!more:/account remove work yes", "more:/account remove work yes"):
        update = {"update_id": 1, "callback_query": {"id": "q", "from": {"id": 7}, "data": data}}
        assert I.handle(update, cfg=_cfg(), bot=None, topics=None, inbox=None, media=None, started=0) == "button"
        assert update["callback_query"]["data"] == data  # the update itself is not changed
    assert seen == ["more:/account remove work yes"] * 2
    assert I._unmarked({"callback_query": {"data": "hg!hg!x"}})["callback_query"]["data"] == "hg!x"  # one marker only
    plain = {"message": {"text": "hg!x"}}
    assert I._unmarked(plain) is plain


def _cfg():
    from ctb.config import Config
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, target_language="en")


@pytest.mark.parametrize("rec, sensitive", [
    # the allow-list: only a decision asked as routine can be answered outside the app's Face ID
    ({"title": "Ship the fix?", "detail": "", "yes": "Yes", "no": "No", "routine": True}, False),
    ({"title": "Run the tests again?", "detail": "takes a minute", "yes": "Run", "no": "Hold", "routine": True}, False),
    ({"title": "Ship the fix?", "detail": "", "yes": "Yes", "no": "No"}, True),            # old record: no flag
    ({"title": "Ship the fix?", "detail": "", "yes": "Yes", "no": "No", "routine": False}, True),
    ({"title": "Ship the fix?", "detail": "", "yes": "Yes", "no": "No", "routine": "true"}, True),
    ({"title": "Buy a domain for 12 EUR?", "detail": "", "yes": "Buy", "no": "No"}, True),  # missed by the words
    # routine, but the words still name a sensitive area (either label counts)
    ({"title": "Promote build 13 to production?", "detail": "", "yes": "Yes", "no": "No", "routine": True}, True),
    ({"title": "Go ahead?", "detail": "This deletes the old backups", "yes": "Yes", "no": "No", "routine": True}, True),
    ({"title": "Next step?", "detail": "", "yes": "Publish now", "no": "Hold", "routine": True}, True),
    ({"title": "Keep the old backups?", "detail": "", "yes": "Keep", "no": "Delete backups", "routine": True}, True),
    ({"title": "Spend it?", "detail": "₪40 for the domain", "yes": "Yes", "no": "No", "routine": True}, True),
    ({"title": "Use the second account?", "detail": "", "yes": "Yes", "no": "No", "routine": True}, True),
    ({"title": None, "detail": "", "yes": "Yes", "no": "No", "routine": True}, True),   # damaged: fail closed
    ({"title": "Ship?", "detail": "", "yes": "Yes", "routine": True}, True),              # no "no" label: damaged
    ("not a record", True),
])
def test_a_decision_is_sensitive_unless_asked_as_routine_with_plain_words(rec, sensitive):
    assert SB.decision_sensitive(rec) is sensitive


def test_invisible_characters_cannot_hide_a_sensitive_word():
    from ctb import sensitive_buttons as sb
    rec = {"routine": True, "title": "Keep the backups?", "detail": "", "yes": "Keep", "no": "De‍lete backups"}
    assert sb.decision_sensitive(rec)
    for ch in ("​", "‍", "­", "⁠", "﻿", "‎"):
        assert sb.text_sensitive(f"De{ch}lete backups")
    assert sb.label_sensitive("Ｄｅｌｅｔｅ backups")


def test_a_bidi_mark_between_scripts_and_other_invisibles_still_read_as_sensitive():
    from ctb import sensitive_buttons as sb
    assert sb.text_sensitive("שלום‏Delete")
    for ch in ("️", "͏", "\U000e0100", "ㅤ"):
        assert sb.text_sensitive(f"De{ch}lete backups"), repr(ch)
    for k in ("title", "detail", "yes", "no"):
        rec = {"routine": True, "title": "Keep?", "detail": "", "yes": "Keep", "no": "Skip"}
        rec[k] = "שלום‏Delete"
        assert sb.decision_sensitive(rec), k
    assert sb.text_sensitive("\u05e9\u05dc\u05d5\u05dd\u200fDe\u200dlete")
    assert not sb.text_sensitive("Keep the photos \U0001F44D\ufe0f")  # emoji style after a symbol
    assert not sb.text_sensitive("Keep the photos \U0001F44D")


def test_only_plainly_visible_characters_pass_the_word_rule():
    from ctb import sensitive_buttons as sb
    for ch in ("᠋", "᠏", "́", " ", " ", "\u0007", "឴", "\U000e0001"):
        assert sb.text_sensitive(f"De{ch}lete"), repr(ch)
        assert sb.label_sensitive(f"De{ch}lete"), repr(ch)
    assert not sb.text_sensitive("Keep the photos? כן, להמשיך! 12:30 ✓")


def test_emoji_style_selector_after_a_symbol_is_plain():
    from ctb import sensitive_buttons as sb
    assert not sb.text_sensitive("⚠️ Keep the photos? ✔️")
    assert sb.text_sensitive("De️lete")


def test_braille_blank_and_refusal_worded_actions_are_sensitive():
    from ctb import sensitive_buttons as sb
    assert sb.text_sensitive("De⠀lete backups")
    assert sb.label_sensitive("No, delete backups")
    rec = {"routine": True, "title": "Keep the backups?", "detail": "", "yes": "Keep", "no": "No, delete backups"}
    assert sb.decision_sensitive(rec)
    assert not sb.label_sensitive("No") and not sb.label_sensitive("Cancel") and not sb.label_sensitive("לא")
