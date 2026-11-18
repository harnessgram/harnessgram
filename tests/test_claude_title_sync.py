"""Claude Code session names follow their Telegram topic titles through guarded fakes."""
from __future__ import annotations

import json
import subprocess

import pytest

from conftest import FakeBot, GOOD_TOKEN
from ctb import inbox as I, title_sync as TS, topic_names as N, topics, wake
from ctb.config import Config, state_dir, write_private
from ctb.topics import TopicMap, save_session_name

SID = "sess-title-1"
GROUP = -1001234567890
REAL_SAFE_SEND = I.send_idle_text


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=42, chat_id=GROUP, target_language="he")


@pytest.fixture(autouse=True)
def no_live_terminal(monkeypatch):
    """Every rename test must opt into an explicit fake terminal result."""
    monkeypatch.setattr(I, "send_idle_text", lambda session, text: False)


def test_idle_session_receives_rename_once_per_title(monkeypatch):
    sent = []
    monkeypatch.setattr(I, "send_idle_text", lambda session, text: sent.append((session, text)) or True)
    TS.want(SID, "מסלול: תיקון ניתוב")
    assert TS.flush(SID)
    assert not TS.flush(SID)
    assert sent == [(SID, "/rename מסלול: תיקון ניתוב")]


def test_busy_session_keeps_rename_queued(monkeypatch):
    sent = []
    monkeypatch.setattr(I, "send_idle_text", lambda session, text: sent.append(text) or False)
    TS.want(SID, "גשר: עבודה")
    assert not TS.flush(SID)
    assert TS.due(SID) == "גשר: עבודה"
    monkeypatch.setattr(I, "send_idle_text", lambda session, text: True)
    assert TS.flush(SID)
    assert TS.due(SID) == ""


def test_dialog_refusal_keeps_rename_queued(monkeypatch):
    monkeypatch.setattr(I, "send_idle_text", lambda session, text: False)
    TS.want(SID, "מזווה: ניהול")
    assert not TS.flush(SID)
    assert TS.due(SID) == "מזווה: ניהול"


def test_safe_send_uses_guarded_path(monkeypatch):
    calls = []
    monkeypatch.setattr(I, "pane_for_session", lambda session: "%7")
    monkeypatch.setattr(I, "_claim_terminal", lambda session: True)
    monkeypatch.setattr(I, "_release_terminal", lambda session: calls.append(("release", session)))
    monkeypatch.setattr(I, "pane_raw", lambda pane: "idle")
    monkeypatch.setattr(I, "_dialog_open", lambda body, session: False)
    monkeypatch.setattr(I, "_type_command", lambda session, pane, text, **kw: calls.append(
        (session, pane, text, kw)) or "sent")
    assert REAL_SAFE_SEND(SID, "/rename גשר: בטוח")
    assert calls == [(SID, "%7", "/rename גשר: בטוח",
                      {"verify_session": True, "guard_held": True}), ("release", SID)]


def test_safe_send_refuses_a_dialog(monkeypatch):
    typed = []
    monkeypatch.setattr(I, "pane_for_session", lambda session: "%7")
    monkeypatch.setattr(I, "_claim_terminal", lambda session: True)
    monkeypatch.setattr(I, "_release_terminal", lambda session: None)
    monkeypatch.setattr(I, "pane_raw", lambda pane: "permission dialog")
    monkeypatch.setattr(I, "_dialog_open", lambda body, session: True)
    monkeypatch.setattr(I, "_type_command", lambda *args, **kwargs: typed.append(args) or "sent")
    assert not REAL_SAFE_SEND(SID, "/rename גשר: אסור")
    assert typed == []


def test_safe_send_catches_terminal_timeout(monkeypatch):
    monkeypatch.setattr(I, "pane_for_session", lambda session: "%7")
    monkeypatch.setattr(I, "_claim_terminal", lambda session: True)
    monkeypatch.setattr(I, "_release_terminal", lambda session: None)
    monkeypatch.setattr(I, "pane_raw", lambda pane: (_ for _ in ()).throw(
        subprocess.TimeoutExpired("tmux", 1)))
    assert not REAL_SAFE_SEND(SID, "/rename safe")


def test_manual_topic_rename_only_queues(monkeypatch):
    sent = []
    monkeypatch.setattr(I, "send_idle_text", lambda session, text: sent.append(text) or False)
    bot = FakeBot()
    write_private(state_dir() / "topics.json", json.dumps({SID: {"thread": 7, "name": "old"}}))
    assert topics.topic_rename(cfg(), "מזווה: ניהול", session=SID, bot=bot) == 0
    assert TS.due(SID) == "מזווה: ניהול"
    assert sent == []


def test_automatic_topic_rename_only_queues(monkeypatch):
    sent = []
    monkeypatch.setattr(I, "send_idle_text", lambda session, text: sent.append(text) or False)
    bot = FakeBot()
    tmap = TopicMap(state_dir() / "topics.json", bot, GROUP, cfg=cfg())
    tmap.ensure(SID, "Documents: היי")
    save_session_name(SID, "Documents: היי")
    assert N.apply(cfg(), bot, tmap, SID, "Documents: היי", "גשר: פתיחה")
    assert TS.due(SID) == "גשר: פתיחה"
    assert sent == []


def test_session_start_does_not_flush_queued_title(monkeypatch, capsys):
    TS.want(SID, "גשר: חזרה")
    sent = []
    monkeypatch.setattr(TS, "flush", lambda session: sent.append(session) or True)
    monkeypatch.setattr(wake, "_own_chain", lambda: [10])
    monkeypatch.setattr(wake, "_is_session_process", lambda session, chain: True)
    monkeypatch.setattr(wake, "_pane_is_ours", lambda pane, chain: True)
    monkeypatch.setattr(wake, "load_config", lambda: Config())
    monkeypatch.setenv("TMUX_PANE", "%3")
    assert wake.hook_start(json.dumps({"session_id": SID, "source": "resume"})) == 0
    assert sent == []
    assert capsys.readouterr().out == ""


def test_custom_title_prevents_queued_rename(tmp_path, monkeypatch):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({"type": "custom-title", "customTitle": "My title"}) + "\n")
    monkeypatch.setattr("ctb.status.transcript_of", lambda session: str(transcript))
    TS.want(SID, "גשר: בתור")
    assert TS.due(SID) == ""


def test_bidi_isolates_are_removed_for_send_and_ours(monkeypatch):
    sent = []
    monkeypatch.setattr(I, "send_idle_text", lambda session, text: sent.append(text) or True)
    TS.want(SID, "גשר: \u2068API client\u2069")
    assert TS.ours(SID, "גשר: API client")
    assert TS.flush(SID)
    assert sent == ["/rename גשר: API client"]


def test_ours_accepts_english_and_legacy_isolated_names():
    TS.want(SID, "Documents: routing fix")
    assert TS.ours(SID, "Documents: routing fix")
    assert TS.ours(SID, "Documents: \u2068routing fix\u2069")
    assert not TS.ours(SID, "Documents: another fix")


def test_title_sync_addon_off_does_not_queue(monkeypatch):
    monkeypatch.setattr("ctb.addons.enabled", lambda config, key: False)
    bot = FakeBot()
    write_private(state_dir() / "topics.json", json.dumps({SID: {"thread": 7, "name": "old"}}))
    assert topics.topic_rename(cfg(), "English legacy name", session=SID, bot=bot) == 0
    assert TS.due(SID) == ""
