"""Agent blocks for the Morning Brief (`ctb block`): a declarative schema checked on write and on read, the secret
filter, size and rate limits, expiry, the per-session mute and the owner's route for it. All sample text is
invented; sample secrets are fake and built from pieces."""
import json
import time

import pytest

from ctb import blocks as B
from ctb import cards, decisions, panel
from ctb.config import state_dir
from test_panel import call, server, signed  # noqa: F401

SID, OTHER = "0f3c1d2e-1111-4222-8333-944455556666", "7a7a7a7a-1111-4222-8333-944455556666"
KEY = "sk-" + "proj-" + "Q" * 30


def status(text="Three of eight screens are built.", **kw):
    return dict({"kind": "status", "line": "studio", "text": text}, **kw)


@pytest.mark.parametrize("raw, why", [
    ({"kind": "html", "line": "x", "text": "y"}, "unknown block kind"),
    (dict(status(), color="red"), "unknown field"),
    (dict(status(), style="x"), "unknown field"),
    (status("<b>bold</b>"), "markup"),
    (status("see [here](https://example.org)"), "markup"),
    (status("x" * (B.TEXT_CHARS + 1)), "too long"),
    ({"kind": "status", "line": "", "text": "y"}, "empty"),
    ({"kind": "checklist", "line": "s", "items": []}, "1 to"),
    ({"kind": "checklist", "line": "s", "items": [{"text": "a"}] * (B.MAX_ITEMS + 1)}, "1 to"),
    ({"kind": "checklist", "line": "s", "items": [{"text": "a", "done": "yes"}]}, "checklist item"),
    ({"kind": "needs_you", "line": "s", "text": "pick one", "card": "nope"}, "16-character"),
    (dict(status(), expires_in=10), "expires_in"),
    (dict(status(), expires_in=B.EXPIRES_MAX + 1), "expires_in"),
    ([status()], "one JSON object"),
])
def test_the_schema_refuses_anything_outside_it(raw, why):
    with pytest.raises(B.BlockError) as e:
        B.post(SID, raw)
    assert why in e.value.reason
    assert B.all_blocks() == []


def test_a_block_is_stored_private_and_updated_in_place():
    rec = B.post(SID, status(), now=1000.0)
    path = state_dir() / B.STATE_FILE
    assert path.stat().st_mode & 0o077 == 0
    again = B.post(SID, status("Four of eight screens are built.", id=rec["id"]), now=1010.0)
    assert again["id"] == rec["id"] and again["created"] == 1000.0 and again["updated"] == 1010.0
    (only,) = B.all_blocks(now=1011.0)
    assert only["text"] == "Four of eight screens are built."


def test_secrets_never_reach_the_file():
    B.post(SID, status("rotated " + KEY))
    B.post(SID, {"kind": "checklist", "line": "s", "items": [{"text": "key " + KEY[:40], "done": True}]})
    raw = (state_dir() / B.STATE_FILE).read_text()
    assert "Q" * 30 not in raw


def test_another_session_cannot_touch_a_block_and_the_kind_stays():
    rec = B.post(SID, status(), now=1000.0)
    with pytest.raises(B.BlockError, match="no block"):
        B.post(OTHER, status(id=rec["id"]), now=1100.0)
    with pytest.raises(B.BlockError, match="no block"):
        B.close(OTHER, rec["id"], now=1100.0)
    with pytest.raises(B.BlockError, match="is a status block"):
        B.post(SID, {"kind": "checklist", "line": "s", "items": [{"text": "a"}], "id": rec["id"]}, now=1100.0)
    assert B.close(SID, rec["id"], now=1100.0)["id"] == rec["id"] and B.all_blocks(now=1101.0) == []


def test_updates_are_throttled_and_writes_have_an_hourly_budget(monkeypatch):
    rec = B.post(SID, status(), now=1000.0)
    with pytest.raises(B.BlockError, match="wait a few seconds"):
        B.post(SID, status(id=rec["id"]), now=1000.0 + B.MIN_GAP - 1)
    monkeypatch.setattr(B, "HOURLY", 3)
    B.post(SID, status(id=rec["id"]), now=1010.0)
    B.post(SID, status(id=rec["id"]), now=1020.0)
    with pytest.raises(B.BlockError, match="an hour"):
        B.post(SID, status(id=rec["id"]), now=1030.0)
    B.post(SID, status(id=rec["id"]), now=1000.0 + 3600 + 1)  # the window moved on


def test_per_session_and_global_caps(monkeypatch):
    monkeypatch.setattr(B, "MAX_PER_SESSION", 2)
    B.post(SID, status(), now=1000.0)
    B.post(SID, status(), now=1001.0)
    with pytest.raises(B.BlockError, match="already has 2"):
        B.post(SID, status(), now=1002.0)
    monkeypatch.setattr(B, "MAX_OPEN", 3)
    B.post(OTHER, status(), now=1003.0)
    with pytest.raises(B.BlockError, match="already 3"):
        B.post(OTHER, status(), now=1004.0)


def test_a_block_expires_and_leaves_the_file():
    B.post(SID, status(expires_in=60), now=1000.0)
    assert len(B.all_blocks(now=1059.0)) == 1 and B.all_blocks(now=1061.0) == []
    B.post(SID, status(), now=2000.0)  # a later write drops the expired one from the file
    assert len(json.loads((state_dir() / B.STATE_FILE).read_text())["blocks"]) == 1


def test_needs_you_points_only_at_this_sessions_own_open_question(monkeypatch):
    monkeypatch.setattr(B, "_asks_of", lambda s: {"a" * 16} if s == SID else set())
    raw = {"kind": "needs_you", "line": "studio", "text": "Pick a caption style", "card": "a" * 16}
    assert B.post(SID, raw)["card"] == "a" * 16
    with pytest.raises(B.BlockError, match="points only at an open question"):
        B.post(OTHER, raw)
    with pytest.raises(B.BlockError, match="points only"):
        B.post(SID, dict(raw, card="b" * 16))


def test_the_open_questions_of_a_session_are_its_cards_and_decisions():
    card = cards.publish(SID, -1001234567890, 5, "choice", "Which style?", options=["Bold", "Plain"])
    cards.publish(SID, -1001234567890, 5, "progress", "Build", steps=["a"])
    dec = decisions.create(SID, -1001234567890, 5, "Ship it?", "", "Yes", "No")
    assert B._asks_of(SID) == {card["id"], dec["id"]} and B._asks_of(OTHER) == set()


def test_a_hand_edited_file_is_checked_again_on_read():
    B.post(SID, status(), now=time.time())
    path = state_dir() / B.STATE_FILE
    data = json.loads(path.read_text())
    good = next(iter(data["blocks"]))
    data["blocks"]["c" * 16] = dict(data["blocks"][good], text="<script>x</script>")
    data["blocks"]["d" * 16] = dict(data["blocks"][good], kind="html")
    data["blocks"]["bad"] = data["blocks"][good]
    path.write_text(json.dumps(data))
    assert [r["id"] for r in B.all_blocks()] == [good]


def test_the_view_says_age_staleness_and_mute():
    rec = B.post(SID, status(), now=1000.0)
    v = B.view(rec, 1000.0 + B.TTL["status"] + 1, set(), {SID[:8]})
    assert v["stale"] and v["why"] == "old" and v["muted"] and v["session"] == SID[:8] and "created" not in v
    assert B.view(rec, 1001.0, {SID}, set())["why"] == "ended"
    assert B.view(rec, 1001.0, set(), set())["stale"] is False


def test_mute_and_unmute():
    assert B.set_muted(SID[:8], True) == {SID[:8]} and B.muted() == {SID[:8]}
    assert B.set_muted(SID[:8], False) == set()
    with pytest.raises(ValueError):
        B.set_muted("../../x", True)


def test_the_owner_mutes_a_session_from_the_cockpit(server):  # noqa: F811
    body = json.dumps({"session": SID[:8], "on": True}).encode()
    code, _, data = call(server, path="/api/block-mute", init=signed(), body=body)
    assert code == 200 and json.loads(data) == {"ok": True, "muted": True} and B.muted() == {SID[:8]}
    for bad in ({"session": "x", "on": True}, {"session": SID[:8]}, {"session": SID[:8], "on": 1}):
        code, _, _ = call(server, path="/api/block-mute", init=signed(), body=json.dumps(bad).encode())
        assert code == 400
    code, _, _ = call(server, path="/api/block-mute", init=signed(user_id=8), body=body)
    assert code == 403


def test_the_command_posts_from_a_file_and_says_how_to_update(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(B.D, "caller_session", lambda: SID)
    src = tmp_path / "b.json"
    src.write_text(json.dumps(status()))
    assert B.run(None, "post", source=str(src)) == 0
    out = capsys.readouterr().out
    (rec,) = B.all_blocks()
    assert rec["id"] in out and '"id"' in out
    assert B.run(None, "list") == 0 and rec["id"] in capsys.readouterr().out
    src.write_text("{not json")
    assert B.run(None, "post", source=str(src)) == 1 and "not valid JSON" in capsys.readouterr().err
    src.write_text(" " * (B.MAX_JSON + 1))
    assert B.run(None, "post", source=str(src)) == 1 and "too long" in capsys.readouterr().err
    assert B.run(None, "close", ident=rec["id"]) == 0 and B.all_blocks() == []


def test_the_command_needs_a_proven_session(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert B.run(None, "list") == 1 and "no session" in capsys.readouterr().err


def test_the_cli_knows_the_command():
    from ctb import cli
    assert "block" in cli.GUARDED
    assert panel.ACTIONS["/api/block-mute"] == "_block_mute"


def test_the_command_line_reads_post_list_and_close():
    from ctb import cli
    p = cli._parser()
    a = p.parse_args(["block", "post", "--json", "-"])
    assert (a.cmd, a.block_cmd, a.source) == ("block", "post", "-")
    assert p.parse_args(["block", "post"]).source == "-"
    assert p.parse_args(["block", "close", "a" * 16]).id == "a" * 16
    assert p.parse_args(["block", "list"]).block_cmd == "list"
