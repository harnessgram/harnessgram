import html
import re
import zlib

import pytest

from ctb import cli, decisions, inbox, panel, widgets
from test_decisions import cfg, rig, ask, queued, ORBIT_S  # noqa: F401


class Bot:
    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return {"message_id": 123} if method == "sendWidget" else True


def query(cfg, stamp, sender=None, chat=None, index=0, message_id=123):
    return {"id": "query", "from": {"id": cfg.owner_chat_id if sender is None else sender},
            "chat": {"id": cfg.owner_chat_id if chat is None else chat, "type": "private"},
            "message_id": message_id, "type": {"type": "data"}, "data": f"{index}:{stamp}"}


def sent_stamp(bot):
    return re.search(r"i\+':([0-9a-f]{12})'", bot.calls[0][1]["html"])[1]


def test_size_budget_long_hebrew():
    doc = widgets.build_html("האם לאשר את הפעולה? " * 50, ["כן", "לא"], "abcdef123456")
    assert len(doc.encode()) <= 32768
    assert len(zlib.compress(doc.encode(), 9)) < 4096


def test_escaping_prevents_script_injection():
    evil = '</script><script>alert("x")</script>&'
    doc = widgets.build_html(evil, [evil, "לא"], "abcdef123456")
    assert html.escape(evil) in doc
    assert evil not in doc
    assert doc.count('<script>') == 1


def test_payload_limit_and_three_options():
    doc = widgets.build_html("שאלה", ["א", "ב", "ג"], "abcdef123456")
    assert 'b2' in doc
    for i in range(3):
        assert len(f"{i}:abcdef123456".encode()) <= 64
    with pytest.raises(ValueError):
        widgets.build_html("x", ["x", "y"], "bad")


def test_owner_answer_uses_panel_decision_path(rig, monkeypatch):
    rec = ask(rig, title="Ship this specific change?")
    bot = Bot()
    widgets.send_decision(rig.cfg, bot, rec)
    monkeypatch.setattr(panel, "stale_reasons_now", lambda cfg: {})
    monkeypatch.setattr(decisions, "announce", lambda *args: None)
    update = {"widget_query": query(rig.cfg, sent_stamp(bot))}
    assert inbox.handle(update, rig.cfg, bot, None, rig.box, None, 0) == "widget-answered"
    item = queued(rig, ORBIT_S)[0]
    assert item["source"] == "panel" and "typed" not in item
    assert "Ship this specific change?" in item["text"]
    assert decisions.PANEL_APPROVES in item["text"]
    assert bot.calls[-2:] == [("answerWidgetQuery", {"widget_query_id": "query"}),
                              ("editMessageWidgetData", {"chat_id": rig.cfg.owner_chat_id,
                                                         "message_id": 123, "chat_data": "0"})]


def test_non_owner_and_other_chat_are_ignored(rig):
    bot = Bot()
    widgets.send_sample(rig.cfg, bot)
    stamp = sent_stamp(bot)
    for q in (query(rig.cfg, stamp, sender=rig.cfg.owner_chat_id + 1),
              query(rig.cfg, stamp, chat=-123), query(rig.cfg, stamp, message_id=124)):
        assert inbox.handle({"widget_query": q}, rig.cfg, bot, None, rig.box, None, 0) == "ignored"
    assert [method for method, _ in bot.calls] == ["sendWidget"] + ["answerWidgetQuery"] * 3


def test_stale_and_replayed_decisions_are_refused(rig, monkeypatch):
    rec = ask(rig)
    bot = Bot()
    widgets.send_decision(rig.cfg, bot, rec)
    q = query(rig.cfg, sent_stamp(bot))
    monkeypatch.setattr(panel, "stale_reasons_now", lambda cfg: {rec["id"]: "replaced"})
    assert widgets.handle_query(q, rig.cfg, bot, rig.box) == "ignored"
    assert not queued(rig, ORBIT_S)
    monkeypatch.setattr(panel, "stale_reasons_now", lambda cfg: {})
    monkeypatch.setattr(decisions, "announce", lambda *args: None)
    assert widgets.handle_query(q, rig.cfg, bot, rig.box) == "widget-answered"
    assert widgets.handle_query(q, rig.cfg, bot, rig.box) == "ignored"
    assert len(queued(rig, ORBIT_S)) == 1


def test_flag_off_no_sends(rig, monkeypatch):
    monkeypatch.setattr(cli.C, "load", lambda: rig.cfg)
    monkeypatch.setattr(widgets, "send_sample", lambda *args: pytest.fail("must not send"))
    assert cli.main(["widget", "test"]) == 1


def test_sample_and_force(rig, monkeypatch):
    bot = Bot()
    monkeypatch.setattr(cli.C, "load", lambda: rig.cfg)
    from ctb.telegram import Bot as TelegramBot
    monkeypatch.setattr(TelegramBot, "from_config", lambda cfg: bot)
    assert cli.main(["widget", "test", "--force"]) == 0
    assert bot.calls[0][0] == "sendWidget"
    assert widgets.handle_query(query(rig.cfg, sent_stamp(bot), index=1), rig.cfg, bot, rig.box) == "widget-answered"
    assert bot.calls[-1][1]["chat_data"] == "1"


def test_allowed_updates_contains_widget_query():
    assert "widget_query" in inbox.ALLOWED_UPDATES
