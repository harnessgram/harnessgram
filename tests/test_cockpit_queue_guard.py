"""Panel acknowledgements must survive queue pressure until delivery."""
from types import SimpleNamespace

import pytest

from ctb import cards, decisions, inbox, panel
from test_decisions import ORBIT, ORBIT_S, ask, cfg, queued, rig, roomy_budget  # noqa: F401
from test_cards import only, run


def test_close_sends_keep_first_and_reject_second_until_taken(rig, monkeypatch):
    rig.box.max_items = 1
    monkeypatch.setattr(panel, "collect", lambda *a, **k: [
        SimpleNamespace(session=ORBIT_S, state="idle", closed=False, chat=ORBIT)])
    panel.say_to(rig.cfg, rig.box, ORBIT_S[:8], "first", send="ab" * 12)
    with pytest.raises(OSError):
        panel.say_to(rig.cfg, rig.box, ORBIT_S[:8], "second", send="cd" * 12)
    assert [i["said"] for i in queued(rig, ORBIT_S)] == ["first"]
    rig.box.take(ORBIT_S)
    panel.say_to(rig.cfg, rig.box, ORBIT_S[:8], "second", send="cd" * 12)
    assert [i["said"] for i in queued(rig, ORBIT_S)] == ["second"]


def test_panel_answer_survives_other_messages_and_keeps_order(tmp_path):
    box = inbox.Inbox(tmp_path / "inbox", max_items=2)
    box.put("sample", {"source": "owner", "text": "old"})
    box.put("sample", {"source": "panel", "text": "answer"})
    box.put("sample", {"source": "owner", "text": "new"})
    assert [i["text"] for i in box.take("sample") if not i.get("dropped")] == ["answer", "new"]


def test_full_queue_reopens_card_and_decision(rig):
    rig.box.max_items = 1
    rig.box.put(ORBIT_S, {"source": "panel", "text": "earlier"})
    assert run(rig, "ask", title="Question") == 0
    card = only()
    decision = ask(rig)
    with pytest.raises(OSError):
        cards.answer(rig.cfg, rig.box, card["id"], text="answer")
    with pytest.raises(OSError):
        decisions.answer(rig.cfg, rig.box, decision["id"], "yes")
    assert only()["state"] == "open"
    assert decisions.open_decisions()[0]["state"] == "open"
    assert [i["text"] for i in queued(rig, ORBIT_S)] == ["earlier"]


def test_success_copy_does_not_claim_receipt():
    for lang in ("he", "en"):
        for key in ("sent", "cardSent", "saySent"):
            assert ("ממתינ" if lang == "he" else "queued") in panel.STRINGS[lang][key]
