"""The optional periodic reminder: a configured sentence added to a long session's context now and then."""
import json
import os

from conftest import GOOD_TOKEN
from ctb import config as C, hooks as H, inbox as I
from ctb.config import Config, state_dir

SID = "sess-rem-1"


def cfg(**kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, **kw)


def transcript(tmp_path, kb):
    path = tmp_path / f"t{kb}.jsonl"
    path.write_bytes(b"x" * (kb * 1024))
    return str(path)


def test_off_by_default_and_validated():
    assert C.from_dict({}).periodic_reminder == ""
    assert C.from_dict({"periodic_reminder": " save a lesson "}).periodic_reminder == "save a lesson"
    import pytest
    with pytest.raises(C.ConfigError):
        C.from_dict({"periodic_reminder": 5})
    with pytest.raises(C.ConfigError):
        C.from_dict({"periodic_reminder_minutes": -1})


def test_reminder_needs_a_long_transcript_and_waits_between_rounds(tmp_path):
    c = cfg(periodic_reminder="Save one lesson.", periodic_reminder_minutes=90, periodic_reminder_min_kb=100)
    short, long = transcript(tmp_path, 10), transcript(tmp_path, 200)
    assert H.periodic_reminder(SID, short, c, now=1000.0) == ""
    assert H.periodic_reminder(SID, long, c, now=1000.0) == "Save one lesson."
    assert H.periodic_reminder(SID, long, c, now=1000.0 + 89 * 60) == ""
    assert H.periodic_reminder(SID, long, c, now=1000.0 + 91 * 60) == "Save one lesson."
    assert H.periodic_reminder(SID, long, cfg(), now=9e9) == ""        # not configured: never
    assert H.periodic_reminder(SID, "/no/such/file", c, now=9e9) == ""


def test_the_post_hook_adds_the_reminder_to_its_context(tmp_path, monkeypatch, capsys):
    c = cfg(periodic_reminder="Save one lesson.", periodic_reminder_min_kb=1)
    monkeypatch.setattr(H, "load_config", lambda: c)
    monkeypatch.setattr(H, "typing", lambda *a: None)
    monkeypatch.setattr(H, "append_progress", lambda *a: None)
    data = json.dumps({"session_id": SID, "transcript_path": transcript(tmp_path, 5), "tool_name": "Bash"})
    assert H.hook_post(data) == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse" and out["additionalContext"] == "Save one lesson."
    assert H.hook_post(data) == 0 and capsys.readouterr().out == ""      # not again within the window
    # with a queued message the reminder rides along after the framed message
    os.remove(state_dir() / "reminder" / SID)
    I.default_inbox().put(SID, {"text": "hello there", "id": 1, "chat": 7, "source": "owner"})
    assert H.hook_post(data) == 0
    text = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "hello there" in text and text.endswith("Save one lesson.")


def test_a_background_agent_does_not_consume_the_reminder(tmp_path, monkeypatch, capsys):
    c = cfg(periodic_reminder="Save one lesson.", periodic_reminder_min_kb=1)
    monkeypatch.setattr(H, "load_config", lambda: c)
    monkeypatch.setattr(H, "typing", lambda *a: None)
    monkeypatch.setattr(H, "append_progress", lambda *a: None)
    t = transcript(tmp_path, 5)
    assert H.hook_post(json.dumps({"session_id": SID, "transcript_path": t, "agent_id": "a1"})) == 0
    assert capsys.readouterr().out == "" and not (state_dir() / "reminder" / SID).exists()
    assert H.hook_post(json.dumps({"session_id": SID, "transcript_path": t})) == 0     # the main one still gets it
    assert "Save one lesson." in capsys.readouterr().out
