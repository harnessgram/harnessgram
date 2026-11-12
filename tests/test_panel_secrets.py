"""Agent-written decision, card and pin-board text is credential-filtered (creds.redact_channel, the same
cleaning as the alerts channel) when it is created and again at the control panel's /api/state boundary, so
a record saved before the filter existed is not shown either. The owner's own answers are not altered. All
sample secrets are fake and built from pieces."""
import json

import pytest

from conftest import GOOD_TOKEN
from ctb import board, cards, decisions, panel
from ctb import config as C
from ctb.config import state_dir
from ctb.topics import TopicMap

HOME, THREAD, SID = -1001234567890, 5, "0f3c1d2e-1111-4222-8333-944455556666"
KEY = "sk-" + "proj-" + "Q" * 30
GH = "gh" + "p_" + "R" * 36
PASSWORD = "password: " + "hunter" + "2hunter"
SHORT = "pw: " + "hunter" + "2hunter"  # fits a label; the alerts-channel cleaning hides the value
SECRETS = (KEY, GH, "Q" * 30, "R" * 36, "2hunter")


def leaks(value) -> list:
    text = json.dumps(value, ensure_ascii=False)
    return [s for s in SECRETS if s in text]


def make_cfg():
    return C.from_dict({"bot_token": GOOD_TOKEN, "owner_chat_id": 7, "chat_id": HOME, "target_language": "en"})


def test_a_decision_is_stored_without_credentials():
    rec = decisions.create(SID, HOME, THREAD, "Rotate " + KEY + "?", "the old one: " + GH + "\n" + PASSWORD,
                           SHORT, "Keep it")
    assert leaks(rec) == [] and leaks(decisions.open_decisions()) == []
    assert rec["title"].startswith("Rotate") and "[redacted]" in rec["detail"]


def test_every_card_field_is_stored_without_credentials():
    recs = [cards.publish(SID, HOME, THREAD, "progress", "Deploy " + KEY, PASSWORD, steps=[SHORT, "ship"]),
            cards.publish(SID, HOME, THREAD, "choice", "Which key " + GH, "", options=[SHORT, "new one"]),
            cards.publish(SID, HOME, THREAD, "link", "Preview", "", url="https://example.com/p", label=SHORT),
            cards.publish(SID, HOME, THREAD, "numbers", "Keys", "", figures=[SHORT + "=3", "count=3"])]
    assert leaks(recs) == [] and leaks(cards.open_cards()) == []


def test_pin_board_text_is_stored_without_credentials():
    assert leaks(board.clean_text("remember " + KEY, 200)) == []


@pytest.mark.parametrize("url", ["https://example.com/x?token=" + "Q" * 40,
                                 "https://example.com/x?access_token=" + "ab" * 20,
                                 "https://box.tailabc123.ts.net/__secure-handoff/" + "ab" * 24])
def test_a_link_that_redaction_would_change_is_refused_with_a_reason(url):
    """Never stored cut up (the button would lead nowhere): refused, and the agent is told why."""
    with pytest.raises(board.BoardError, match="credential"):
        board.clean_url(url)
    with pytest.raises(cards.CardError, match="credential"):
        cards.publish(SID, HOME, THREAD, "link", "Preview", "", url=url, label="open")
    assert cards.open_cards() == []


@pytest.mark.parametrize("url", ["https://github.com/o/r/commit/" + "3f" * 20,
                                 "https://example.com/blob?digest=sha256:" + "a1" * 32])
def test_a_link_with_a_legitimate_long_hex_id_is_kept_whole(url):
    assert board.clean_url(url) == url
    assert cards.publish(SID, HOME, THREAD, "link", "Build", "", url=url, label="open")["url"] == url


def test_a_labelled_digest_stays_in_text_while_an_unlabelled_hex_run_is_hidden():
    digest = "a1" * 32
    assert digest in board.clean_text("image sha256:" + digest, 200)
    assert digest in decisions.clean("checksum SHA-256 " + digest, 400, "detail")
    assert digest not in board.clean_text("value " + digest, 200)


def test_a_secret_split_between_a_figure_label_and_value_is_hidden():
    rec = cards.publish(SID, HOME, THREAD, "numbers", "Login", "", figures=["password=" + "hunter" + "2", "tries=3"])
    figures = {f["label"]: f["value"] for f in rec["figures"]}
    assert figures["password"] == "[redacted]" and figures["tries"] == "3"


def test_the_owner_answer_is_not_altered():
    """What the owner types in the panel goes to the session as written (it is his own text)."""
    assert decisions.clean("use " + KEY, 400, "note", owner_text=True).endswith(KEY)


def test_the_panel_state_never_returns_a_stored_credential(monkeypatch):
    """Records written before the filter (or by hand) are cleaned at the response boundary too."""
    cfg = make_cfg()
    TopicMap(state_dir() / "topics.json", None, HOME, cfg=cfg).ensure(SID, "Work: keys", thread=THREAD)
    rec = decisions.create(SID, HOME, THREAD, "Rotate the key?", "", "Rotate", "Keep")
    card = cards.publish(SID, HOME, THREAD, "choice", "Which?", "", options=["old", "new"])
    for name, ident, fields in ((decisions.STATE_FILE, rec["id"], {"title": "Rotate " + KEY, "detail": GH}),
                                (cards.STATE_FILE, card["id"], {"title": "Which " + GH, "detail": PASSWORD,
                                                                "options": ["pw: " + "hunter" + "2hunter", "new"]})):
        path = state_dir() / name
        data = json.loads(path.read_text())
        data[ident].update(fields)
        path.write_text(json.dumps(data))
    snap = board.snapshot(HOME, THREAD)
    snap["items"].append({"i": 99, "k": "remind", "s": "renew " + KEY, "at": 1.0, "x": False})
    snap["items"].append({"i": 98, "k": "check", "s": "check " + GH, "at": 1.0, "x": False})
    raw = json.loads((state_dir() / board.STATE_FILE).read_text()) if (state_dir() / board.STATE_FILE).exists() else {}
    raw[board.key_of(HOME, THREAD)] = snap
    (state_dir() / board.STATE_FILE).write_text(json.dumps(raw))
    state = panel.build_state(cfg, light=True)
    assert state["decisions"] and state["cards"]
    assert leaks(state) == []


def test_the_last_filter_runs_after_the_translations(monkeypatch):
    """A translation (or a cached one) that carries a secret is returned clean too."""
    cfg = make_cfg()
    TopicMap(state_dir() / "topics.json", None, HOME, cfg=cfg).ensure(SID, "Work: keys", thread=THREAD)
    decisions.create(SID, HOME, THREAD, "Rotate the key?", "", "Rotate", "Keep")

    class Leaky:
        def he(self, text):
            return ("סובב " + KEY) if text else ""
    state = panel.build_state(cfg, light=True, tr=Leaky())
    assert state["decisions"][0].get("title_he") and leaks(state) == []


def test_a_file_caption_and_name_are_redacted_when_redaction_is_on(bot, tmp_path, monkeypatch):
    from ctb import mirror
    path = tmp_path / ("notes-" + GH + ".txt")
    path.write_text("hello")
    cfg = make_cfg()
    m = mirror.Mirror(cfg, bot, TopicMap(state_dir() / "topics.json", bot, HOME, cfg=cfg))
    m.send_file("Work", SID, str(path), "key " + KEY)
    sent = bot.methods("sendDocument")
    assert sent and leaks(sent) == []


def test_a_shared_or_staged_file_never_carries_its_original_name(tmp_path):
    from ctb import mirror
    path = tmp_path / ("notes-" + GH + ".txt")
    path.write_text("hello")
    url = mirror.share_publish(str(path), "https://example.com/s", tmp_path / "share")
    assert GH not in url and url.endswith("/file.txt")
    assert mirror.opaque_name(str(path)) == "file.txt" and mirror.opaque_name("/x/" + GH) == "file"


def test_a_password_after_a_leading_symbol_is_hidden_in_the_panel():
    cfg = make_cfg()
    TopicMap(state_dir() / "topics.json", None, HOME, cfg=cfg).ensure(SID, "Work: keys", thread=THREAD)
    rec = decisions.create(SID, HOME, THREAD, "Rotate?", "", "Rotate", "Keep")
    path = state_dir() / decisions.STATE_FILE
    data = json.loads(path.read_text())
    data[rec["id"]]["detail"] = "DB_PASSWORD=" + "!Abcd" + "1234"  # saved before the filter (or by hand)
    path.write_text(json.dumps(data))
    assert "Abcd1234" not in json.dumps(panel.build_state(cfg, light=True))
