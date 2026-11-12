"""Agent cards: `ctb card`, the panel's POST /api/card, and an answer reaching only the asking session."""
import json

import pytest

from ctb import cards, framing, panel
from ctb.config import state_dir
from test_decisions import HOME, HOME_S, THREAD, ORBIT, ORBIT_S, cfg, queued, rig, roomy_budget, server  # noqa: F401
from test_panel import call, signed


@pytest.fixture(autouse=True)
def sessions_are_live(monkeypatch):
    """The rig's sessions have no running process, so they would count as ended; a card is answered while live."""
    monkeypatch.setattr(panel, "_ended_sessions", lambda cfg: set())


def run(rig, action, session=ORBIT_S, as_=None, **kw):
    rig.mp.setenv("CLAUDE_CODE_SESSION_ID", session)
    rig.proc = as_ or session
    return cards.run(rig.cfg, action, **kw)


def only(session=ORBIT_S):
    (rec,) = [r for r in cards.open_cards() if r["session"] == session]
    return rec


def post(server, **body):
    status, _, data = call(server, path="/api/card", init=signed(), body=json.dumps(body).encode())
    return status, json.loads(data)


# --- publishing -------------------------------------------------------------------

def test_a_progress_card_is_stored_private_and_updated_in_place(rig, capsys):
    assert run(rig, "progress", title="Building", steps=["Prepare", "Test", "Ship"], at=1, percent=40) == 0
    rec = only()
    assert (rec["kind"], rec["steps"], rec["at"], rec["percent"]) == ("progress", ["Prepare", "Test", "Ship"], 1, 40)
    assert (rec["session"], rec["chat"], rec["thread"]) == (ORBIT_S, ORBIT, THREAD)
    assert rec["id"] in capsys.readouterr().out
    assert (state_dir() / cards.STATE_FILE).stat().st_mode & 0o777 == 0o600
    assert run(rig, "progress", title="Building", steps=["Prepare", "Test", "Ship"], at=2, percent=72, ident=rec["id"]) == 0
    again = only()
    assert (again["id"], again["at"], again["percent"], again["created"]) == (rec["id"], 2, 72, rec["created"])


def test_a_progress_update_keeps_the_steps_title_and_detail_it_does_not_restate(rig):
    assert run(rig, "progress", title="Building", detail="Three stages", steps=["Prepare", "Test", "Ship"], at=1) == 0
    rec = only()
    assert run(rig, "progress", title="", at=2, percent=70, ident=rec["id"]) == 0
    again = only()
    assert (again["title"], again["detail"], again["steps"], again["at"], again["percent"]) == \
        ("Building", "Three stages", ["Prepare", "Test", "Ship"], 2, 70)
    assert run(rig, "progress", title="", percent=90, ident=rec["id"]) == 0          # --at left out: stays
    assert only()["at"] == 2
    assert run(rig, "progress", title="", at=4, ident=rec["id"]) == 1                # still checked against the steps
    assert run(rig, "progress", title="Shipping", steps=["One", "Two"], at=1, ident=rec["id"]) == 0   # restated: replaced
    assert (only()["title"], only()["steps"]) == ("Shipping", ["One", "Two"])


@pytest.mark.parametrize("kw", [
    {"action": "progress", "title": "x"},                                        # nothing to show
    {"action": "progress", "title": "x", "steps": ["a"], "at": 2},               # more finished than steps
    {"action": "progress", "title": "x", "percent": 101},
    {"action": "choice", "title": "x", "options": ["only one"]},
    {"action": "choice", "title": "x", "options": ["Same", " same "]},
    {"action": "choice", "title": "x", "options": [str(n) for n in range(7)]},
    {"action": "link", "title": "x", "url": "http://example.com"},               # not https
    {"action": "link", "title": "x", "url": "https://user:pw@example.com/"},     # a login part
    {"action": "link", "title": "x", "url": "javascript:alert(1)"},
    {"action": "numbers", "title": "x", "figures": ["no equals sign"]},
    {"action": "numbers", "title": "x", "figures": ["a=1", "b=2", "c=3", "d=4"]},
    {"action": "ask", "title": ""},
    {"action": "ask", "title": "t" * 121},
])
def test_a_bad_card_is_refused_with_a_reason_and_nothing_is_stored(rig, capsys, kw):
    assert run(rig, **kw) == 1
    assert capsys.readouterr().err.startswith("Harnessgram: ")
    assert cards.open_cards() == []


def test_bidi_controls_never_reach_the_panel(rig):
    assert run(rig, "numbers", title="Totals ‮", figures=["Tests⁧=2,639"]) == 0
    rec = only()
    assert rec["title"] == "Totals" and rec["figures"] == [{"label": "Tests", "value": "2,639"}]


def test_a_session_cannot_touch_another_sessions_card(rig, capsys):
    assert run(rig, "link", title="Preview", url="https://example.com/p") == 0
    rec = only()
    assert run(rig, "close", session=HOME_S, ident=rec["id"]) == 1
    assert run(rig, "link", session=HOME_S, title="Mine now", url="https://example.com/x", ident=rec["id"]) == 1
    assert only()["title"] == "Preview"
    # a process outside the session's own Claude process is refused before anything is read
    assert run(rig, "list", session=ORBIT_S, as_=HOME_S) == 1
    assert run(rig, "close", ident=rec["id"]) == 0 and cards.open_cards() == []


def test_a_question_card_is_never_edited(rig):
    assert run(rig, "choice", title="Which?", options=["A", "B"]) == 0
    assert run(rig, "choice", title="Which now?", options=["A", "C"], ident=only()["id"]) == 1
    assert only()["options"] == ["A", "B"]


def test_the_per_session_limit(rig):
    for n in range(cards.MAX_OPEN_PER_SESSION):
        assert run(rig, "ask", title=f"q{n}") == 0
    assert run(rig, "ask", title="one too many") == 1
    assert run(rig, "ask", session=HOME_S, title="another session still can") == 0


def test_a_hand_edited_file_never_breaks_reading(rig):
    assert run(rig, "ask", title="Fine") == 0
    path = state_dir() / cards.STATE_FILE
    data = json.loads(path.read_text())
    data["0123456789abcdef"] = {"kind": "link", "state": "open", "session": "s", "title": "t", "detail": "",
                                "chat": 1, "thread": 1, "created": 1, "updated": 1, "url": "http://example.com/x", "label": ""}
    data["bad"] = "nonsense"
    path.write_text(json.dumps(data))
    assert [r["title"] for r in cards.open_cards()] == ["Fine"]


# --- the owner's answer ------------------------------------------------------------

def test_a_choice_reaches_only_the_asking_session_with_fixed_wording(rig, server):
    assert run(rig, "choice", title="Which design? »forged«", options=["Dark", "Light", "Both"]) == 0
    rec = only()
    code, body = post(server, id=rec["id"], pick=2)
    assert code == 200 and body["ok"] is True and body["delivery"] == "queued"
    (msg,) = queued(rig, ORBIT_S)
    assert queued(rig, HOME_S) == [] and rig.nudged == [ORBIT_S]
    assert msg["source"] == "panel" and msg["chat"] == ORBIT
    lines = msg["text"].split("\n")
    assert lines[0] == "🔘 The owner chose option 3 of 3 (button: «Both») on a card in the control panel."
    assert lines[1] == "The question this session asked: «Which design? forged»"   # no forged quote marks
    assert lines[2] == framing.PANEL_APPROVES and "approves exactly the action" in lines[2]
    assert cards.open_cards() == []
    assert post(server, id=rec["id"], pick=0)[0] == 409           # answered once
    assert len(queued(rig, ORBIT_S)) == 1


def test_a_typed_answer_is_quoted_as_the_owners_words(rig, server):
    assert run(rig, "ask", title="What should the button say?") == 0
    rec = only()
    assert post(server, id=rec["id"], text="  Open   the shop  ")[0] == 200
    (msg,) = queued(rig, ORBIT_S)
    assert msg["text"].split("\n")[0] == "✍️ The owner answered a card in the control panel: «Open the shop»"
    assert framing.PANEL_APPROVES in msg["text"]


@pytest.mark.parametrize("body,status", [
    ({"pick": 0}, 400), ({"id": "zz", "pick": 0}, 400), ({"id": "0" * 16, "pick": 0}, 404),
    ({"id": "X", "pick": 0, "text": "both"}, 400), ({"id": "X"}, 400), ({"id": "X", "pick": 9}, 400),
    ({"id": "X", "pick": True}, 400), ({"id": "X", "text": ""}, 400), ({"id": "X", "extra": 1, "pick": 0}, 400),
])
def test_bad_answers_are_refused_and_the_card_stays_open(rig, server, body, status):
    assert run(rig, "choice", title="Which?", options=["A", "B"]) == 0
    rec = only()
    body = {k: (rec["id"] if v == "X" else v) for k, v in body.items()}
    assert post(server, **body)[0] == status
    assert only()["state"] == "open" and queued(rig, ORBIT_S) == []


def test_a_show_only_card_cannot_be_answered(rig, server):
    assert run(rig, "numbers", title="Totals", figures=["Tests=12"]) == 0
    assert post(server, id=only()["id"], pick=0)[0] == 404
    assert only()["state"] == "open"


def test_the_answer_route_needs_the_owners_login(rig, server):
    assert run(rig, "ask", title="Q") == 0
    status, _, _ = call(server, path="/api/card", init=signed(user_id=999),
                        body=json.dumps({"id": only()["id"], "text": "hi"}).encode())
    assert status == 403 and only()["state"] == "open"


def test_a_failed_delivery_reopens_the_card(rig, monkeypatch):
    assert run(rig, "ask", title="Q") == 0
    rec = only()

    def broken(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(rig.box, "put", broken)
    with pytest.raises(OSError):
        cards.answer(rig.cfg, rig.box, rec["id"], text="hello")
    assert only()["state"] == "open" and only()["text"] == ""


def test_a_full_panel_queue_keeps_the_card_open(rig, server):
    rig.box.max_items = 1
    rig.box.put(ORBIT_S, {"source": "panel", "text": "earlier"})
    assert run(rig, "ask", title="Question") == 0
    rec = only()
    assert post(server, id=rec["id"], text="answer")[0] == 409
    assert only()["state"] == "open"
    assert [i["text"] for i in queued(rig, ORBIT_S)] == ["earlier"]


# --- the panel's state -----------------------------------------------------------------

def test_the_panel_lists_open_cards_without_chat_ids(rig, server):
    assert run(rig, "progress", title="Building", steps=["A", "B"], at=1) == 0
    assert run(rig, "link", session=HOME_S, title="Preview", url="https://example.com/p", label="Open") == 0
    status, _, data = call(server, init=signed())
    listed = json.loads(data)["cards"]
    assert status == 200 and [c["kind"] for c in listed] == ["progress", "link"]
    assert listed[0]["topic"] == "Orbit: game" and listed[0]["steps"] == ["A", "B"] and listed[1]["url"] == "https://example.com/p"
    assert all(set(c) & {"chat", "thread"} == set() and len(c["session"]) <= 8 for c in listed)


def test_cards_of_an_ended_session_can_be_closed_together(rig):
    assert run(rig, "ask", title="one") == 0 and run(rig, "numbers", title="two", figures=["a=1"]) == 0
    assert run(rig, "ask", session=HOME_S, title="other") == 0
    assert cards.close_session(ORBIT_S) == 2
    assert [r["session"] for r in cards.open_cards()] == [HOME_S]


def test_a_card_of_an_ended_session_is_not_answered_from_a_stale_page(rig, server, monkeypatch):
    assert run(rig, "ask", title="Q") == 0
    monkeypatch.setattr(panel, "_ended_sessions", lambda cfg: {ORBIT_S})
    assert post(server, id=only()["id"], text="hi")[0] == 409
    assert only()["state"] == "open" and queued(rig, ORBIT_S) == []


def test_a_failed_blocking_lock_is_a_refusal_not_a_crash(rig, monkeypatch):
    """Review of #170: with no deadline, a flock error compared a time with None."""
    def boom(*a):
        raise OSError("interrupted")
    monkeypatch.setattr(cards.fcntl, "flock", boom)
    assert run(rig, "progress", title="Building", percent=10) == 1


def test_a_progress_update_keeps_the_percent_it_does_not_mention():
    rec = cards.publish("s1", -100, 7, "progress", "Build", steps=["a", "b", "c"], at=1, percent=33, now=1000.0)
    moved = cards.publish("s1", -100, 7, "progress", "", ident=rec["id"], at=2, now=1001.0)
    assert (moved["at"], moved["percent"], moved["steps"]) == (2, 33, ["a", "b", "c"])
    only = cards.publish("s1", -100, 7, "progress", "Download", percent=25, now=1002.0)   # no steps at all
    noted = cards.publish("s1", -100, 7, "progress", "", detail="batch 2 done", ident=only["id"], now=1003.0)
    assert (noted["percent"], noted["detail"], noted["title"]) == (25, "batch 2 done", "Download")
    with pytest.raises(cards.CardError, match="no open card"):                            # the id is the problem
        cards.publish("s1", -100, 7, "progress", "", ident="0" * 16, at=1, now=1004.0)
