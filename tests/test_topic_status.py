import json

from ctb import board, owner_requests, topic_status
from ctb.config import Config
from ctb.topics import TopicMap

NOW = 2_000_000.0


def row(ident=1, *, session="sess-a", status="open", wait="team", updated=NOW, note=""):
    return {"project": "bridge", "id": ident, "title": "Radio music and news", "status": status,
            "wait": wait, "updated": updated, "note": note, "session": session}


def test_ledger_session_reference_is_read_and_invalid_values_are_not_mapped(tmp_path):
    data = {"items": [
        {"id": 1, "text": "one", "status": "open", "wait": "team", "created": 1, "updated": 2,
         "session": "abc-123"},
        {"id": 2, "text": "two", "status": "open", "wait": "team", "created": 1, "updated": 2,
         "session": "not a session"},
    ]}
    (tmp_path / "bridge.json").write_text(json.dumps(data))
    rows = owner_requests.read(NOW, tmp_path, history=True)
    assert rows[0]["session"] == "abc-123"
    assert "session" not in rows[1]


def test_hebrew_status_labels_cover_ledger_states():
    rows = [row(1), row(2, note="started"), row(3, wait="owner"), row(4, status="done")]
    assert [x["status_label"] for x in topic_status.cards(rows, "sess-a", NOW)] == [
        "התקבל", "בעבודה", "מחכה לך", "הושלם"]


def test_status_labels_follow_ui_language():
    rows = [row(1), row(2, note="started"), row(3, wait="owner"), row(4, status="done")]
    assert [x["status_label"] for x in topic_status.cards(rows, "sess-a", NOW, lang="en")] == [
        "Received", "Working", "Waiting for you", "Done"]


def test_cards_are_bounded_and_credentials_are_filtered():
    secret = "ghp_" + "a" * 40
    item = row(note=secret + " " + "n" * 400)
    item["title_he"] = "כ" * 400
    cards = topic_status.cards([item] * 50, "sess-a", NOW)
    assert len(cards) == topic_status.MAX_CARDS
    assert len(cards[0]["title"]) <= topic_status.TITLE_CHARS
    assert len(cards[0]["note"]) <= topic_status.NOTE_CHARS
    assert secret not in cards[0]["note"]


def test_done_item_expires_after_one_day():
    assert topic_status.cards([row(status="done", updated=NOW - 86400)], "sess-a", NOW)
    assert not topic_status.cards([row(status="done", updated=NOW - 86401)], "sess-a", NOW)


def test_topic_scope_never_leaks_another_sessions_items():
    cards = topic_status.cards([row(1, session="sess-a"), row(2, session="sess-b")], "sess-b", NOW)
    assert [x["id"] for x in cards] == [2]


def test_pin_contains_only_aggregate_ledger_counts(monkeypatch):
    title_secret, note_secret = "DISTINCTIVE-PRIVATE-TITLE", "DISTINCTIVE-PRIVATE-NOTE"
    pin_now = board.time.time()
    rows = [row(1), row(2, note="started"), row(3, wait="owner"),
            row(4, status="done", note=note_secret, updated=pin_now)]
    rows[0]["title"] = title_secret
    monkeypatch.setattr(owner_requests, "read", lambda *args, **kwargs: rows)
    monkeypatch.setattr(TopicMap, "session_at", lambda self, chat, thread: "sess-a")
    monkeypatch.setattr("ctb.translate.cached", lambda text, cfg: text)

    cfg = Config(brief={"ledger_dir": "/unused"}, ui_language="he")
    text = board.pin_content(cfg, -1001, 7, "Cockpit", [], board._blank())["text"]

    assert title_secret not in text and note_secret not in text
    assert "התקבלו: 1" in text and "בעבודה: 1" in text
    assert "מחכים לך: 1" in text and "הושלמו היום: 1" in text
    assert topic_status.digest(topic_status.cards(rows, "sess-a", pin_now)) == "1:1:1:1"


def test_pin_totals_and_digest_include_items_beyond_card_limit(monkeypatch):
    rows = [row(i) for i in range(25)]
    monkeypatch.setattr(owner_requests, "read", lambda *args, **kwargs: rows)
    monkeypatch.setattr(TopicMap, "session_at", lambda self, chat, thread: "sess-a")
    cfg = Config(brief={"ledger_dir": "/unused"}, ui_language="en")

    text = board.pin_content(cfg, -1001, 7, "Cockpit", [], board._blank())["text"]
    before = board.status_digest(cfg, -1001, 7, NOW)
    rows[24]["note"] = "started"

    assert "Received: 25" in text
    assert before == "25:0:0:0"
    assert board.status_digest(cfg, -1001, 7, NOW) == "24:1:0:0"


def test_english_view_keeps_the_original_title_and_note():
    rows = [{"id": 7, "project": "p", "session": "sess-a", "status": "open", "updated": NOW - 60,
             "title": "Original title", "title_he": "כותרת", "note": "Original note", "note_he": "הערה"}]
    en = topic_status.cards(rows, "sess-a", NOW, lang="en")[0]
    he = topic_status.cards(rows, "sess-a", NOW, lang="he")[0]
    assert (en["title"], en["note"]) == ("Original title", "Original note")
    assert (he["title"], he["note"]) == ("כותרת", "הערה")
