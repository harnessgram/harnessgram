"""Guards for the English/Hebrew UI dictionaries."""
import re
import string

from ctb.i18n import EN, HE, t

HEBREW = re.compile(r"[֐-׿]")


def _fields(s):
    return sorted({f for _, f, _, _ in string.Formatter().parse(s) if f})


def test_every_key_exists_in_both_languages():
    assert set(EN) - set(HE) == set()
    assert set(HE) - set(EN) == set()


def test_placeholders_match():
    bad = [k for k in EN if _fields(EN[k]) != _fields(HE[k])]
    assert bad == []


def test_english_has_no_hebrew_letters():
    assert [k for k, v in EN.items() if HEBREW.search(v)] == []


def test_placeholders_never_clash_with_t_arguments():
    # t(key, lang, **kw): a "{key}" or "{lang}" placeholder can never be filled
    assert [k for k, v in EN.items() if {"key", "lang"} & set(_fields(v))] == []


def test_lookup_and_fallback():
    k = next(iter(EN))
    assert t(k, lang="en") == EN[k]
    assert t(k, lang="he") == HE[k]
    assert t("no_such_key_xyz", lang="he") == "no_such_key_xyz"


def test_english_text_for_claude_is_not_swallowed_as_a_command(tmp_path, monkeypatch):
    # PR #15 review: "history of X" and bare "car" went to the bot, never to Claude
    from conftest import FakeBot
    from ctb import inbox as I
    from ctb.topics import TopicMap
    from test_features import OWNER, cfg, make_session, update_for
    bot = FakeBot()
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    t_id = topics.ensure("a", "Alpha")
    make_session(tmp_path, monkeypatch, "a", 5)
    box = I.Inbox(tmp_path / "inbox")
    for text in ("history of the auth module, why?", "car", "screen"):
        r = I.handle(update_for(text, thread=t_id), cfg(ui_language="he"), bot, topics, box, None, 0)
        assert r not in ("history", "car", "screen"), (text, r)
    assert I.handle(update_for("car", thread=t_id), cfg(ui_language="en"), bot, topics, box, None, 0) == "car"
    I.set_car_mode(False)
