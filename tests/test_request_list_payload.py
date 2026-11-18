"""The Requests list sends only what it shows; the full text comes from an owner-only detail read."""
import json
from types import SimpleNamespace

from ctb import owner_requests, panel, topic_cards
from ctb.config import Config
from conftest import GOOD_TOKEN
from test_owner_requests import NOW, put
from test_panel import signed

DAY = 86400
LIMIT = 250 * 1024
HEBREW = "בדיקת המסך החדש ואישור הנוסח מול הצוות לפני שהגרסה יוצאת לדרך "


ENGLISH = "Review the new onboarding screen and confirm the wording with the team before the release goes out. "


def ledger(tmp_path, count=300, title_chars=70, note_chars=110, notes=8):
    """`count` items over ten projects, each its own topic (so `count` cards), with English titles and notes."""
    folders = {}
    for n in range(count):
        # Open items were touched between a minute and nine days ago; finished ones three to nine days ago.
        updated = NOW - ((n % 7) + 3 if n % 10 == 0 else n % 10) * DAY - 60 - n
        row = dict(id=n // 10 + 1, text=(ENGLISH * 20)[:title_chars] + " " + str(n),
                   status="done" if n % 10 == 0 else "open", wait="owner" if n % 3 == 0 else "team",
                   created=NOW - 12 * DAY, updated=updated, topic="topic-%d" % n,
                   notes=[{"at": updated - notes + k, "text": (ENGLISH * 40)[:note_chars] + " " + str(k)} for k in range(notes)])
        folders.setdefault("project-%d" % (n % 10), []).append(row)
    for project, rows in folders.items():
        put(tmp_path, project, rows)
    return Config(brief={"ledger_dir": str(tmp_path)})


class Translator:
    """Every English text has a Hebrew one of the same length: what a fully translated Hebrew page carries."""
    def he_item(self, project, ident, updated, text):
        return self.he(text)

    def he(self, text):
        return (HEBREW * (len(text) // len(HEBREW) + 1))[:len(text)]


def state_bytes(state):
    return len(json.dumps(state, ensure_ascii=False).encode("utf-8"))


def test_state_with_300_items_and_300_cards_stays_under_250_kb(tmp_path):
    cfg = ledger(tmp_path)
    full = owner_requests.read(NOW, tmp_path, history=True)
    assert len(full) == 300
    state = panel.build_state(cfg, NOW, light=True, tr=Translator())
    assert len(state["topic_cards"]) > 150  # closed cards past the seven-day window are gone, the rest are all here
    assert len(state["requests"]) > 250
    assert state_bytes(state) < LIMIT, state_bytes(state)
    assert state_bytes(state["requests"]) + state_bytes(state["topic_cards"]) < LIMIT


def test_state_stays_small_for_untranslated_text_and_the_longest_ledger_texts(tmp_path):
    cfg = ledger(tmp_path, title_chars=1400, note_chars=500, notes=50)  # near the reader's own bounds
    state = panel.build_state(cfg, NOW, light=True)
    assert len(state["requests"]) > 250 and len(state["topic_cards"]) > 150
    assert state_bytes(state) < LIMIT, state_bytes(state)


def test_list_items_carry_only_what_the_row_shows(tmp_path):
    cfg = ledger(tmp_path, 30)
    rows = panel.build_state(cfg, NOW, light=True, tr=Translator())["requests"]
    assert rows
    for r in rows:
        assert set(r) <= {"project", "id", "title", "title_he", "status", "wait", "age", "updated", "idle", "long", "older", "urgent"}
        assert len(r["title"]) <= owner_requests.TITLE_SHOWN and len(r.get("title_he", "")) <= owner_requests.TITLE_SHOWN
    assert any(r["long"] for r in rows) and not any(r["long"] for r in rows if r["status"] == "done")
    assert all(r["title"] and r["title_he"] for r in rows)  # English pages still need the original text
    assert all(r["long"] == (r["status"] == "open" and r["idle"] >= owner_requests.LONG_WAIT) for r in rows)


def test_closed_cards_past_the_window_are_dropped_and_recent_ones_stay(tmp_path):
    cfg = ledger(tmp_path, 60)
    cards = panel.build_state(cfg, NOW, light=True, tr=Translator())["topic_cards"]
    closed = [c for c in cards if c["state"] == "done"]
    full = topic_cards.build(owner_requests.read(NOW, tmp_path, history=True), NOW)
    assert len(cards) < len(full) == 60
    kept = {tuple(c["key"]) for c in cards}
    assert all(tuple(t["key"]) in kept for t in full if t["state"] != "done")
    assert all(tuple(t["key"]) not in kept for t in full if t["state"] == "done" and NOW - t["updated"] > topic_cards.CLOSED_DAYS * DAY)
    for c in cards:
        assert set(c) <= {"key", "project", "state", "next_explicit", "title", "decision", "next", "prs",
                          "title_he", "decision_he", "next_he", "stage", "percent", "step"}


def detail_call(cfg, req, init, translator=None, path="/api/request-detail"):
    h = object.__new__(panel.PanelHandler)
    h.path = path
    h.headers = {panel.INIT_HEADER: init}
    h._body = json.dumps(req).encode()
    h._off = lambda: False
    h._body_ok = lambda _: True
    out = []
    h._json = lambda code, body: out.append((code, body))
    h.server = SimpleNamespace(cfg=cfg, bot_token=GOOD_TOKEN, owner_id=7, translator=translator)
    h.do_POST()
    return out[-1]


def test_detail_is_owner_only_and_answers_the_full_text(tmp_path):
    cfg = ledger(tmp_path, 30, title_chars=400)
    assert panel.ROUTES["/api/request-detail"] and panel.ACTIONS["/api/request-detail"] == "_owner_request_detail"
    req = {"project": "project-1", "id": 1}
    for init in ("", signed(user_id=8), signed(auth_date=1)):
        assert detail_call(cfg, req, init)[0] == 403
    code, body = detail_call(cfg, req, signed(), Translator())
    assert code == 200 and body["ok"] is True
    stored = next(r for r in owner_requests.read(NOW, tmp_path, history=True) if r["project"] == "project-1" and r["id"] == 1)
    assert len(body["title"]) > owner_requests.TITLE_SHOWN and body["title"] == stored["title"]
    assert len(body["steps"]) == 8 and body["note"] == stored["note"] and len(body["title_he"]) == len(body["title"])
    assert detail_call(cfg, {"project": "project-1", "id": 999}, signed())[0] == 404
    assert detail_call(cfg, {"project": "project-1"}, signed())[0] == 400
    assert detail_call(cfg, {"project": "project-1", "id": "1"}, signed())[0] == 404


def test_detail_text_passes_the_credential_filter(tmp_path):
    secret = "ghp_" + "a1B2c3D4e5" * 4
    put(tmp_path, "demo", [dict(id=1, text="Token " + secret, status="open", wait="owner", created=NOW - 5, updated=NOW - 1,
                                notes=["note " + secret])])
    cfg = Config(brief={"ledger_dir": str(tmp_path)})
    code, body = detail_call(cfg, {"project": "demo", "id": 1}, signed())
    assert code == 200 and secret not in json.dumps(body)


def test_topic_timeline_is_read_on_open_owner_only_and_translated(tmp_path):
    cfg = ledger(tmp_path, 30)
    key = ["project-1", "topic", "topic-1"]
    assert panel.ACTIONS["/api/topic-detail"] == "_topic_detail"
    for init in ("", signed(user_id=8), signed(auth_date=1)):
        assert detail_call(cfg, {"key": key}, init, path="/api/topic-detail")[0] == 403
    code, body = detail_call(cfg, {"key": key}, signed(), Translator(), path="/api/topic-detail")
    assert code == 200 and len(body["notes"]) == 8
    assert all(n["text"] and n["text_he"] and n["at"] for n in body["notes"])
    assert [n["at"] for n in body["notes"]] == sorted((n["at"] for n in body["notes"]), reverse=True)
    for bad in ({"key": key[:2]}, {"key": ["project-1", "other", "x"]}, {"key": "x"}, {}):
        assert detail_call(cfg, bad, signed(), path="/api/topic-detail")[0] == 400
    assert detail_call(cfg, {"key": ["project-1", "topic", "nope"]}, signed(), path="/api/topic-detail")[0] == 404
