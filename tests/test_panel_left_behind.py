"""The Cockpit's "left behind" group and its reminders that can be marked done."""
import json
import time

import pytest

from ctb import board, panel, panel_page, sessions_menu
from ctb.config import state_dir
from test_decisions import HOME, ORBIT, THREAD, cfg, queued, rig, roomy_budget, server  # noqa: F401
from test_panel import call, signed

ORBIT_S = "a1b2c3d4-0000-4000-8000-000000000001"
HOME_S = "e5f6a7b8-0000-4000-8000-000000000002"
DAY = 24 * 3600


def entry(session, age, state="idle", closed=False, chat=ORBIT, thread=THREAD):
    return sessions_menu.Entry(session, "name", thread, age, state, closed, chat)


def send(server, path, **body):
    status, _, data = call(server, path=path, init=signed(), body=json.dumps(body).encode())
    return status, json.loads(data)


def test_only_an_open_quiet_session_older_than_a_day_is_left_behind():
    assert panel.left_behind(entry(ORBIT_S, DAY + 1))
    assert panel.left_behind(entry(ORBIT_S, DAY + 1, state="ended"))
    assert not panel.left_behind(entry(ORBIT_S, DAY - 1))
    assert not panel.left_behind(entry(ORBIT_S, DAY + 1, state="working"))
    assert not panel.left_behind(entry(ORBIT_S, DAY + 1, closed=True))
    assert not panel.left_behind(entry(ORBIT_S, None))


def test_close_closes_only_what_the_server_counts_as_left_behind(rig, server, monkeypatch):
    rows = [entry(ORBIT_S, DAY * 2), entry(HOME_S, 60, chat=None)]
    monkeypatch.setattr(panel, "collect", lambda topics, now=None: rows)
    status, out = send(server, "/api/close", sessions=[ORBIT_S[:8], HOME_S[:8]])
    assert (status, out) == (200, {"ok": True, "closed": 1, "skipped": 1})    # the fresh one stays open
    closes = [p for m, p in rig.bot.calls if m == "closeForumTopic"]
    assert closes == [{"chat_id": ORBIT, "message_thread_id": THREAD}]
    assert ORBIT_S in sessions_menu.closed_marks() and HOME_S not in sessions_menu.closed_marks()
    assert queued(rig, ORBIT_S) == []                                          # closing never wakes the session
    for bad in ({"sessions": []}, {"sessions": ["zz"]}, {"sessions": [ORBIT_S[:8]] * 2}, {"ids": [ORBIT_S[:8]]}):
        assert send(server, "/api/close", **bad)[0] == 400
    assert call(server, path="/api/close", init="", body=b'{"sessions":["a1b2c3d4"]}')[0] in (401, 403)


def test_a_reminder_is_cleared_from_the_page_without_waking_its_session(rig, server):
    now = time.time()

    def add(topic):
        board.add_remind(topic, "pay the invoice", "", now - 4 * DAY)
        return board.add_remind(topic, "call back", "", now)
    board.apply(rig.cfg, rig.bot, ORBIT, THREAD, add)
    topics = panel.TopicMap(state_dir() / "topics.json", None, HOME, cfg=rig.cfg)
    _, reminders = panel.waiting_for_owner(topics, now)
    by_text = {r["text"]: r for r in reminders}
    assert by_text["pay the invoice"]["old"] is True and by_text["call back"]["old"] is False
    ref = by_text["pay the invoice"]["ref"]
    assert panel.REMINDER_REF.fullmatch(ref)
    assert send(server, "/api/remind", refs=[ref]) == (200, {"ok": True, "cleared": 1})
    assert [i["s"] for _, _, i in board.open_reminders()] == ["call back"]
    assert queued(rig, ORBIT_S) == []
    assert send(server, "/api/remind", refs=[ref]) == (200, {"ok": True, "cleared": 0})   # already gone
    assert send(server, "/api/remind", refs=["nope"])[0] == 400


def test_the_page_offers_the_ways_out():
    page = panel_page.PAGE if hasattr(panel_page, "PAGE") else panel_page.page()
    for needle in ("/api/close", "/api/remind", "leftList", "oldList", "sureBtn"):
        assert needle in page
