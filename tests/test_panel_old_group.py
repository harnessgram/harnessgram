"""The Cockpit's "for me" tab folds everything that waited for the owner over three days into ONE collapsed group
("ישנים" / "Older"): the server decides what is old (its own clock, one named constant), the badge counts only the
fresh items, the group shows its own count and is absent when nothing is old."""
import json
import re
import threading
import time

import pytest

from conftest import GOOD_TOKEN
from ctb import cards, decisions, panel, panel_page, panel_view, sessions_menu
from ctb.config import Config, state_dir, write_private
from test_panel import OWNER, call, signed
from test_panel_page import func, js

CHAT = -1001234567
E = sessions_menu.Entry
DAY = 24 * 3600


@pytest.fixture
def server():
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    srv = panel.PanelServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def topics(*sessions):
    write_private(state_dir() / "topics.json", json.dumps(
        {s: {"thread": 10 + i, "name": f"T{i}", "chat": CHAT, "home": False} for i, s in enumerate(sessions)}))


def live(monkeypatch, states):
    monkeypatch.setattr(panel, "collect", lambda topics, now=None: [
        E(s, s[:4], 10 + i, 30.0, st, False, CHAT) for i, (s, st) in enumerate(states.items())])


# --- the server decides what is old ---------------------------------------------------------------

def test_the_age_threshold_is_one_named_constant_of_three_days():
    assert panel_view.OLD_AFTER == 3 * DAY
    assert panel_view.STALE_AFTER == panel_view.OLD_AFTER  # a decision is "stale for age" at the very same line
    assert not hasattr(panel, "OLD_REMINDER")


def test_grouping_by_age_at_the_boundary():
    now = 1_000_000.0
    old = panel_view.OLD_AFTER
    assert panel_view.is_old(now, now - old) is False        # exactly three days: still fresh
    assert panel_view.is_old(now, now - old - 1) is True     # one second more: old
    assert panel_view.is_old(now, now - old + 1) is False
    assert panel_view.is_old(now, now) is False and panel_view.is_old(now, now + 5 * DAY) is False  # a future time


@pytest.mark.parametrize("garbage", [None, "", "yesterday", "123", True, False, [], {}, float("nan")])
def test_a_missing_or_garbage_time_counts_as_fresh(garbage):
    assert panel_view.is_old(1_000_000.0, garbage) is False


def test_the_state_marks_decisions_reminders_and_cards_old_from_the_servers_clock(server, monkeypatch):
    topics("s1-session")
    live(monkeypatch, {"s1-session": "idle"})
    now = time.time()
    fresh = decisions.create("s1-session", CHAT, 10, "Fresh question?", now=now - 3 * DAY + 60)
    old = decisions.create("s1-session", CHAT, 10, "Another old question?", now=now - 3 * DAY - 60)
    from ctb import board
    write_private(state_dir() / board.STATE_FILE, json.dumps({f"{CHAT}:10": {"id": 0, "items": [
        {"k": "remind", "i": 1, "s": "Old reminder", "at": now - 4 * DAY},
        {"k": "remind", "i": 2, "s": "New reminder", "at": now - 60}]}}))
    cards.publish("s1-session", CHAT, 10, "choice", "Which one?", "", options=["a", "b"])
    body = json.loads(call(server, init=signed())[2])
    assert {d["id"]: d["old"] for d in body["decisions"]} == {fresh["id"]: False, old["id"]: True}
    assert {r["text"]: r["old"] for r in body["reminders"]} == {"Old reminder": True, "New reminder": False}
    assert [c["old"] for c in body["cards"]] == [False]


def test_an_old_decision_stays_answerable_and_can_still_be_cleared_in_bulk(server, monkeypatch):
    topics("s1-session")
    live(monkeypatch, {"s1-session": "idle"})
    old = decisions.create("s1-session", CHAT, 10, "Old question?", now=time.time() - 4 * DAY)
    body = json.loads(call(server, init=signed())[2])
    assert body["decisions"][0]["old"] is True and body["decisions"][0]["stale"] == "old"
    assert call(server, path="/api/dismiss", init=signed(), body=json.dumps({"ids": [old["id"]]}).encode())[0] == 200


# --- the page ---------------------------------------------------------------------------------------

def test_both_languages_have_the_group_strings():
    he, en = panel_page.STRINGS["he"], panel_page.STRINGS["en"]
    assert he.keys() == en.keys()
    assert he["oldList"] == "ישנים" and en["oldList"] == "Older"
    assert he["oldHint"].strip() and en["oldHint"].strip()
    assert he["staleList"] != he["oldList"] and en["staleList"] != en["oldList"]  # two groups never share a title
    assert not re.search(r"[A-Za-z]", he["oldList"] + he["oldHint"])  # English and Hebrew never share a line


def test_the_group_is_one_collapsed_details_whose_state_is_a_try_catch_convenience():
    code, group = js(), func("oldGroup")
    assert 'OLDKEY="ctb-panel-old-open"' in code
    assert 'try{opened.old=localStorage.getItem(OLDKEY)==="1";}catch(e){}' in code  # closed unless this viewer opened it
    assert 'el("details","fold oldg")' in group and "det.open=!!opened.old" in group
    assert 'try{localStorage.setItem(OLDKEY,det.open?"1":"0");}catch(e){}' in group
    assert "if(!n)return 0" in group  # nothing old: nothing rendered
    assert 'el("span","n",nf.format(n))' in group  # the count sits on the group
    assert "S.oldList" in group and "S.oldHint" in group


def test_the_page_trusts_the_servers_flag_not_the_phones_clock():
    for name in ("inOld", "oldCards", "waitingCount", "oldGroup"):
        body = func(name)
        assert "Date.now" not in body and "new Date" not in body
    assert "x.old" in func("inOld") and "c.old" in func("oldCards")


def test_the_badge_and_hero_count_only_fresh_items_and_the_group_sits_before_the_sessions():
    count = func("waitingCount")
    assert "!inOld(x)" in count and "!c.old" in count
    assert "waitingCount(d)" in func("tabs") and "waitingCount(d)" in func("hero")
    all_ = func("renderAll")
    assert all_.index("oldGroup(") < all_.index("sec(S.sessions")
    assert "aged" in all_  # the old reminders move into the group instead of their own fold
    assert "remold" not in js()


def test_old_items_leave_the_stack_and_keep_the_same_cards_inside_the_group():
    assert "!inOld(x)" in func("decisions") and 'x.stale!=="old"' in func("decisions")
    group = func("oldGroup")
    assert "decision(x,showTopic)" in group and "cardGroups(cards,body,false)" in group and "remRow(r" in group
    assert "!c.old" in func("cardSections")
    assert "oldGroup(" in func("renderHere")  # the topic's own tab keeps its old items reachable too


def test_logical_css_only_for_the_group():
    page = panel_page.PAGE
    start = page.index("/* the old group")
    css = page[start:page.index(".srow{", start)]
    assert not re.search(r"\b(left|right|margin-left|margin-right|padding-left|padding-right)\b", css)
    assert "min-block-size:48px" in css  # one-handed: a full-width 48px tap target


def test_an_old_item_never_shows_the_new_badge_and_mark_seen_stays_global():
    assert "isNew(d)&&!d.old" in func("decision")
    seen = func("markSeen")  # one timestamp for the whole page: it covers old items too
    assert "seenAt=Date.now()" in seen and "d.old" not in seen
    assert "open.some(isNew)" in func("renderAll")  # the button is offered for the fresh items that carry the badge


def test_an_answered_old_card_leaves_the_group_and_its_count():
    assert "!cardDone[c.id]" in func("oldCards")
    group = func("oldGroup")  # the count is the number of items still waiting; the group is dropped when none is left
    assert "var n=decs.length+cards.length+rems.length;if(!n)return 0;" in group


def test_the_bulk_button_sends_only_what_the_rendered_state_marked_old():
    group = func("oldGroup")
    assert 'x.stale==="old"' in group  # decisions: the ids the server said are only-old (the server re-checks them)
    assert "rems.map(function(r){return r.ref;})" in group and "aged" in func("renderAll")
    assert "r.old" in func("renderAll")  # reminders: only those the state flagged old
    assert "sureBtn(S.clearOld" in group  # and the second tap is still needed
