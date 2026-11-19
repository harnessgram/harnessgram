"""A decision with its whole context: a longer detail, private pictures, https links, and the owner's own comment,
sent with the answer or alone (which approves nothing)."""
import json
import time

import pytest

from ctb import card_media as M, cli, config, decision_sign, decisions, panel
from ctb.config import state_dir
from test_cards_images import _decoder, png
from test_decisions import (ORBIT_S, ask, cfg, queued, rig, roomy_budget, run, server, stored, tap)  # noqa: F401
from test_panel import OWNER, call, signed

needs_decoder = pytest.mark.skipif(not _decoder(), reason="needs Pillow or sips (CI installs Pillow)")


@pytest.fixture(autouse=True)
def enable_rich_decisions(request):
    if "rig" in request.fixturenames:
        request.getfixturevalue("rig").cfg.rich_decisions_enabled = True


@pytest.fixture
def picture(tmp_path):
    path = tmp_path / "private-screenshot-name.png"
    path.write_bytes(png(40, 30))
    return path


def mint(server, ident, init=None):
    return call(server, path="/api/card-media-token", init=signed() if init is None else init,
                body=json.dumps({"id": ident}).encode())


def fetch(server, ident, token=""):
    return call(server, method="GET", path="/card-media/" + ident + ("?auth=" + token if token else ""))


def state(server):
    code, _, data = call(server, init=signed())
    assert code == 200
    return json.loads(data)


def reply(server, ident, note, init=None):
    return tap(server, ident, choice="reply", note=note, init=init)


def test_rich_decisions_are_default_off_and_preserve_legacy_contract(rig, server):
    rig.cfg.rich_decisions_enabled = False
    assert run(rig, "ask", title="Legacy", detail="x" * decisions.LEGACY_DETAIL_CHARS) == 0
    assert run(rig, "ask", title="Too long", detail="x" * (decisions.LEGACY_DETAIL_CHARS + 1)) == 1
    assert run(rig, "ask", title="Attachment", links=["https://example.com"]) == 1
    rec = decisions.open_decisions()[0]
    shown = state(server)["decisions"][0]
    assert "rich_decision" not in shown and "images" not in shown and "links" not in shown and "replies" not in shown
    assert tap(server, rec["id"], choice="reply", note="comment")[0] == 400
    assert tap(server, rec["id"], note="n" * (decisions.LEGACY_NOTE_CHARS + 1))[0] == 400


# --- the CLI and the record --------------------------------------------------------------------------------------

@needs_decoder
def test_real_cli_stores_pictures_privately_and_links_next_to_the_decision(rig, picture, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", ORBIT_S)
    monkeypatch.setattr(config, "load", lambda: rig.cfg)
    rig.proc = ORBIT_S
    detail = "Two ways to ship.\n\n- **A**: today\n- **B**: next week\n" + "x" * 3000
    assert cli.main(["decide", "ask", "Which release plan?", "--detail", detail, "--image", str(picture),
                     "--image", str(picture), "--link", "Preview=https://example.com/preview?build=7",
                     "--link", "https://example.org/notes"]) == 0
    [rec] = decisions.open_decisions()
    assert len(rec["images"]) == 2 and len(set(rec["images"])) == 2 and all(M.ID.fullmatch(i) for i in rec["images"])
    assert rec["links"] == [{"label": "Preview", "url": "https://example.com/preview?build=7"},
                            {"label": "", "url": "https://example.org/notes"}]
    assert rec["detail"].startswith("Two ways to ship.\n\n- **A**: today")
    raw = (state_dir() / decisions.STATE_FILE).read_text()
    assert str(picture) not in raw and picture.name not in raw
    root = state_dir() / M.DECISION_DIR
    assert oct(root.stat().st_mode & 0o777) == "0o700"
    for ident in rec["images"]:
        assert oct((root / ident).stat().st_mode & 0o777) == "0o600"
        assert not (state_dir() / M.CARD_DIR / ident).exists()  # never in the cards' store, never swept by it
    assert picture.name not in capsys.readouterr().out


@pytest.mark.parametrize("link", ["http://example.com", "javascript:alert(1)", "https://user:pw@example.com/",
                                  "Name=ftp://example.com", "https://example.com/a b", "just text", "https://",
                                  "x=https://example.com/‮"])
def test_only_plain_https_links_are_accepted(rig, link, capsys):
    assert run(rig, "ask", title="Check this?", links=[link]) == 1
    assert decisions.open_decisions() == [] and "link" in capsys.readouterr().err


def test_limits_on_detail_pictures_and_links(rig, capsys):
    assert run(rig, "ask", title="Long?", detail="w" * decisions.DETAIL_CHARS) == 0
    assert run(rig, "ask", title="Too long?", detail="w" * (decisions.DETAIL_CHARS + 1)) == 1
    assert run(rig, "ask", title="Links?", links=["https://example.com/%d" % i
                                                 for i in range(decisions.MAX_LINKS + 1)]) == 1
    assert run(rig, "ask", title="Pictures?", images=["/nonexistent"] * (decisions.MAX_IMAGES + 1)) == 1
    assert [r["title"] for r in decisions.open_decisions()] == ["Long?"]


def test_a_bad_picture_refuses_the_question_and_leaves_no_copy(rig, tmp_path, picture, capsys):
    bad = tmp_path / "secret-name.svg"
    bad.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    assert run(rig, "ask", title="Which?", images=[str(picture), str(bad)]) == 1
    assert decisions.open_decisions() == []
    root = state_dir() / M.DECISION_DIR
    assert not root.exists() or not list(root.iterdir())
    assert bad.name not in capsys.readouterr().err


def test_old_records_without_the_new_fields_load_unchanged(rig, server):
    rec = ask(rig, title="Old style?", detail="why")
    data = stored()
    old = {k: v for k, v in data[rec["id"]].items() if k not in ("images", "links", "replies")}
    data[rec["id"]] = old
    (state_dir() / decisions.STATE_FILE).write_text(json.dumps(data))
    [loaded] = decisions.open_decisions()
    assert "images" not in loaded and "links" not in loaded and "replies" not in loaded
    [d] = state(server)["decisions"]
    assert "images" not in d and "links" not in d and "replies" not in d and d["detail"] == "why"
    assert tap(server, rec["id"], "yes", note="fine")[0] == 200
    assert "The owner's note: «fine»" in queued(rig, ORBIT_S)[-1]["text"]


def test_malformed_pictures_or_links_on_disk_are_dropped_not_trusted(rig):
    rec = ask(rig, title="Tampered?")
    data = stored()
    data[rec["id"]].update(images=["../../etc/passwd"], links=[{"label": "x", "url": "http://plain.example"}],
                           replies=[{"at": "now", "text": 5}])
    (state_dir() / decisions.STATE_FILE).write_text(json.dumps(data))
    [loaded] = decisions.open_decisions()
    assert "images" not in loaded and "links" not in loaded and "replies" not in loaded


def test_the_signed_answer_covers_links_and_pictures_and_old_payloads_stay_the_same():
    base = {"id": "a" * 16, "title": "t", "detail": "", "choice": "yes", "note": "", "created": 1.0, "closed": 2.0}
    plain = json.loads(decision_sign.payload(base, OWNER, "panel"))
    assert "links" not in plain and "images" not in plain
    rich = json.loads(decision_sign.payload(dict(base, links=[{"label": "L", "url": "https://example.com"}],
                                                 images=["b" * 32]), OWNER, "panel"))
    assert rich["links"] == [{"label": "L", "url": "https://example.com"}] and rich["images"] == ["b" * 32]


# --- the panel API: pictures only behind the owner's login --------------------------------------------------------

@needs_decoder
def test_pictures_are_served_only_to_the_authenticated_owner(rig, server, picture):
    rec = ask(rig, title="Which design?", images=[str(picture)],
              links=["Spec=https://example.com/spec"], detail="- **left** is calmer")
    [d] = state(server)["decisions"]
    assert d["images"] == rec["images"] and d["links"] == [{"label": "Spec", "url": "https://example.com/spec"}]
    assert d["detail"] == "- **left** is calmer"
    ident = rec["images"][0]
    # no token, a forged token, a stranger's login, a stale login: refused
    assert fetch(server, ident)[0] == 403
    assert fetch(server, ident, "card1." + ident + ".1.9999999999." + "0" * 64)[0] == 403
    assert mint(server, ident, signed(user_id=OWNER + 1))[0] == 403
    assert mint(server, ident, signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 60))[0] == 403
    assert mint(server, "c" * 32)[0] == 404  # not a picture of any decision or card
    code, _, body = mint(server, ident)
    assert code == 200
    token = json.loads(body)["token"]
    code, headers, data = fetch(server, ident, token)
    assert code == 200 and headers["Content-Type"].startswith("image/") and data
    assert "no-store" in headers["Cache-Control"] and b"private source metadata" not in data
    # a token is bound to its picture
    assert fetch(server, rec["images"][0][::-1], token)[0] in (403, 404)


@needs_decoder
def test_pictures_go_away_with_the_decision(rig, server, picture, monkeypatch):
    rec = ask(rig, title="Gone soon?", images=[str(picture)])
    ident = rec["images"][0]
    decisions.cancel(ORBIT_S, rec["id"])
    assert decisions.image_available(ident)  # a late tap within the grace still sees it
    later = time.time() + M.GRACE + 5
    assert not decisions.image_available(ident, now=later)
    with decisions._locked() as path:
        decisions._sweep_media(decisions._load(path).values(), now=later)
    assert not (state_dir() / M.DECISION_DIR / ident).exists()
    code, _, body = mint(server, ident)  # still within the grace by the clock, but the copy is gone
    assert code == 200 and fetch(server, ident, json.loads(body)["token"])[0] == 404


# --- the owner's comment ---------------------------------------------------------------------------------------

def test_a_comment_alone_approves_nothing_and_leaves_the_decision_open(rig, server):
    rec = ask(rig, title="Delete the old backups?", yes="Delete", no="Keep")
    code, _, data = reply(server, rec["id"], "Which backups exactly?\nList them first.")
    body = json.loads(data)
    assert code == 200 and body["ok"] is True and body["reply"] is True and body["key"]
    [item] = queued(rig, ORBIT_S)
    text = item["text"]
    assert text.startswith("💬 The owner wrote a comment on this session's open question")
    assert "«Which backups exactly? List them first.»" in text and "«Delete the old backups?»" in text
    assert "NOT an answer and NOT an approval" in text and "nothing is approved" in text
    assert decisions.PANEL_APPROVES not in text and "The owner tapped" not in text
    assert item["reply"] is True and item["decision"] == rec["id"]
    [still] = decisions.open_decisions()
    assert still["state"] == "open" and still["choice"] == "" and "answer_sig" not in still
    assert [r["text"] for r in still["replies"]] == ["Which backups exactly?\nList them first."]
    [d] = state(server)["decisions"]
    assert d["replies"][0]["text"] == "Which backups exactly?\nList them first." and "age" in d["replies"][0]
    assert rig.nudged == [ORBIT_S]
    # the owner can still answer afterwards, once
    assert tap(server, rec["id"], "yes", note="go ahead")[0] == 200
    last = queued(rig, ORBIT_S)[-1]["text"]
    assert last.startswith("✅ The owner tapped YES") and "«go ahead»" in last
    assert reply(server, rec["id"], "one more")[0] == 409


def test_a_comment_with_the_answer_is_delivered_as_the_owners_words(rig, server):
    rec = ask(rig, title="Ship?")
    assert tap(server, rec["id"], "no", note="Not before the review.\n\nAsk me tomorrow.")[0] == 200
    text = queued(rig, ORBIT_S)[-1]["text"]
    assert text.startswith("❌ The owner tapped NO") and "The owner's note: «Not before the review. Ask me tomorrow.»" in text


@pytest.mark.parametrize("body", [{"choice": "reply"}, {"choice": "reply", "note": "   "},
                                  {"choice": "reply", "note": "x" * (decisions.NOTE_CHARS + 1)},
                                  {"choice": "maybe", "note": "hm"}, {"choice": "reply", "note": 5}])
def test_bad_comment_requests_are_refused(rig, server, body):
    rec = ask(rig)
    code, _, _ = tap(server, rec["id"], raw=json.dumps({"id": rec["id"], **body}))
    assert code == 400
    assert queued(rig, ORBIT_S) == [] and decisions.open_decisions()[0]["state"] == "open"


def test_comments_are_capped_per_decision(rig, server, monkeypatch):
    monkeypatch.setattr(decisions, "MAX_REPLIES", 2)
    rec = ask(rig)
    assert reply(server, rec["id"], "one")[0] == 200
    assert reply(server, rec["id"], "two")[0] == 200
    assert reply(server, rec["id"], "three")[0] == 400
    assert len(queued(rig, ORBIT_S)) == 2


def test_a_permission_request_takes_no_comment_alone(rig, server):
    rec = decisions.create(ORBIT_S, -1001111111111, 5, "Run it?", perm="d" * 16, request="ls")
    with pytest.raises(decisions.DecisionError):
        decisions.reply(rig.cfg, rig.box, rec["id"], "why?")
    assert queued(rig, ORBIT_S) == []


def test_a_failed_queue_takes_the_comment_back(rig, monkeypatch):
    rec = ask(rig)

    def broken(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(rig.box, "put", broken)
    with pytest.raises(OSError):
        decisions.reply(rig.cfg, rig.box, rec["id"], "hello")
    assert "replies" not in decisions.open_decisions()[0]


# --- the same fresh login --------------------------------------------------------------------------------------

def test_sensitive_decisions_still_need_the_same_fresh_login(rig, server):
    rec = ask(rig, title="Pay the invoice?")  # not routine: sensitive
    [d] = state(server)["decisions"]
    assert d["sensitive"] is True
    stale = signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 60)
    assert tap(server, rec["id"], "yes", note="ok", init=stale)[0] == 403
    assert reply(server, rec["id"], "ok", init=stale)[0] == 403
    assert tap(server, rec["id"], "yes", init=signed(user_id=OWNER + 1))[0] == 403
    assert reply(server, rec["id"], "hi", init=signed(user_id=OWNER + 1))[0] == 403
    assert queued(rig, ORBIT_S) == [] and decisions.open_decisions()[0]["state"] == "open"
    assert tap(server, rec["id"], "yes", note="ok")[0] == 200


def test_the_decide_help_keeps_the_legacy_surface(capsys):
    with pytest.raises(SystemExit):
        cli.main(["decide", "ask", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "owner answers with a tap" in out.lower()
    assert "--image" not in out and "--link" not in out and "1500" in out
