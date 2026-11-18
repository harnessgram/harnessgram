from types import SimpleNamespace

import pytest

from ctb import alerts


class Bot:
    def __init__(self):
        self.calls = []

    def call(self, method, params=None, **kwargs):
        self.calls.append((method, params, kwargs))
        return {"message_id": 1}


@pytest.fixture
def rig(monkeypatch):
    bot = Bot()
    mode = {"value": "summary"}
    monkeypatch.setattr(alerts, "state", lambda cfg, got_bot: ({"id": -1001}, {"replies": mode["value"], "quote_owner": "on"}))
    monkeypatch.setattr(alerts, "topic_name", lambda session: "Topic")
    monkeypatch.setattr(alerts, "topic_link", lambda *args: "")
    monkeypatch.setattr(alerts, "_button", lambda *args, **kwargs: None)
    monkeypatch.setattr(alerts.flood, "optional_call",
                        lambda got_bot, method, params, **kwargs: got_bot.call(method, params, **kwargs))
    monkeypatch.setattr("ctb.radio_dedupe.remember", lambda *args, **kwargs: None)
    return SimpleNamespace(bot=bot, mode=mode, cfg=SimpleNamespace(ui_language="en"))


def test_modes_off_summary_and_full(rig):
    rig.mode["value"] = "off"
    assert not alerts.send_reply_pair(rig.cfg, "question", "body\nSummary: short", bot=rig.bot)
    rig.mode["value"] = "summary"
    assert alerts.send_reply_pair(rig.cfg, "question", "body\nSummary: short", bot=rig.bot)
    assert "short" in rig.bot.calls[-1][1]["text"] and "body" not in rig.bot.calls[-1][1]["text"]
    rig.mode["value"] = "full"
    assert alerts.send_reply_pair(rig.cfg, "question", "body\nSummary: short", bot=rig.bot)
    assert "body" in rig.bot.calls[-1][1]["text"]


def test_long_hebrew_reply_is_validly_cut_with_note(rig):
    rig.mode["value"] = "full"
    rig.cfg.ui_language = "he"
    assert alerts.send_reply_pair(rig.cfg, "שאלה", "תשובה " * 1000, bot=rig.bot)
    text = rig.bot.calls[-1][1]["text"]
    assert len(text) <= 4096
    assert "המשך התשובה בנושא" in text
    assert text.endswith("</blockquote>")


def test_owner_text_is_cut_at_300_with_ellipsis(rig):
    assert alerts.send_reply_pair(rig.cfg, "q" * 400, "Summary: short", bot=rig.bot)
    text = rig.bot.calls[-1][1]["text"]
    owner = text.split("<blockquote>", 1)[1].split("</blockquote>", 1)[0]
    assert len(owner) == 298 and owner.endswith("…")


def test_summary_mode_without_summary_returns_false(rig):
    assert not alerts.send_reply_pair(rig.cfg, "question", "body only", bot=rig.bot)
    assert rig.bot.calls == []


def test_duplicate_topic_title_is_stripped_from_reply(rig):
    assert alerts.send_reply_pair(rig.cfg, "question", "body\nSummary: Topic: answer", session="s", bot=rig.bot)
    text = rig.bot.calls[-1][1]["text"]
    assert "<b>Topic</b>" in text
    assert "Topic: answer" not in text and "answer" in text
