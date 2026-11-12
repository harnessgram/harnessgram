"""The Claude Code session title follows the Telegram topic name (2026-11-10).

Claude Code 2.1.322 takes `hookSpecificOutput.sessionTitle` from a UserPromptSubmit hook, so the title is set
by the next prompt's hook: nothing is typed into a terminal, never mid-turn and never into another pane."""
from __future__ import annotations

import json

from conftest import FakeBot, GOOD_TOKEN
from ctb import hooks as H, title_sync as TS, topic_names as N, topics
from ctb.config import Config, state_dir, write_private
from ctb.topics import TopicMap, save_session_name

SID = "sess-title-1"
GROUP = -1001234567890


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=42, chat_id=GROUP, target_language="he")


def prompt(capsys, monkeypatch, **data):
    monkeypatch.setattr(H, "load_config", lambda: Config())  # not ready: no Telegram work, only the title
    H.hook_prompt(json.dumps({"session_id": SID, "prompt": "hi", "cwd": "/tmp", **data}))
    out = capsys.readouterr().out.strip()
    return json.loads(out).get("hookSpecificOutput", {}) if out else {}


def test_a_topic_rename_becomes_the_session_title_on_the_next_prompt(capsys, monkeypatch):
    TS.want(SID, "מסלול: תיקון ניתוב")
    spec = prompt(capsys, monkeypatch, session_title="")
    assert spec == {"hookEventName": "UserPromptSubmit", "sessionTitle": "מסלול: תיקון ניתוב"}
    assert prompt(capsys, monkeypatch, session_title="מסלול: תיקון ניתוב") == {}  # already carried: sent once
    assert prompt(capsys, monkeypatch) == {}  # the hook did not say: what we set last counts


def test_no_rename_no_title(capsys, monkeypatch):
    assert prompt(capsys, monkeypatch, session_title="whatever") == {}


def test_the_owners_own_app_rename_is_never_overwritten(tmp_path, capsys, monkeypatch):
    path = tmp_path / f"{SID}.jsonl"
    path.write_text(json.dumps({"type": "custom-title", "customTitle": "השם שלי"}) + "\n", encoding="utf-8")
    TS.want(SID, "אורה: משהו")
    assert prompt(capsys, monkeypatch, session_title="השם שלי", transcript_path=str(path)) == {}


def test_title_joins_the_prompts_own_context():
    TS.want(SID, "גשר: לוח")
    out = TS.with_title({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "x"}},
                        {"session_id": SID, "session_title": "Claude"})
    assert out["hookSpecificOutput"] == {"hookEventName": "UserPromptSubmit", "additionalContext": "x",
                                         "sessionTitle": "גשר: לוח"}


def test_manual_topic_rename_wants_the_title(monkeypatch):
    bot = FakeBot()
    write_private(state_dir() / "topics.json", json.dumps({SID: {"thread": 7, "name": "old"}}))
    assert topics.topic_rename(cfg(), "מזווה: ניהול", session=SID, bot=bot) == 0
    assert TS.due(SID, "") == "מזווה: ניהול"


def test_automatic_rename_wants_the_title():
    bot = FakeBot()
    tmap = TopicMap(state_dir() / "topics.json", bot, GROUP, cfg=cfg())
    tmap.ensure(SID, "Documents: היי")
    save_session_name(SID, "Documents: היי")
    assert N.apply(cfg(), bot, tmap, SID, "Documents: היי", "גשר: פתיחה")
    assert TS.ours(SID, "גשר: פתיחה") and not TS.ours(SID, "השם שלי")
    assert TS.due(SID, "") == "גשר: פתיחה"


def _namer_round(tmp_path, monkeypatch, app_title):
    bot = FakeBot()
    tmap = TopicMap(state_dir() / "topics.json", bot, GROUP, cfg=cfg())
    tmap.ensure(SID, "Documents: היי")
    tmap.ensure(SID, "Documents: היי")
    save_session_name(SID, "Documents: היי")
    folder = tmp_path / "claude" / "projects" / "proj"
    folder.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    rows = [{"type": "user", "message": {"role": "user", "content": "היי"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
            {"type": "custom-title", "customTitle": app_title}]
    (folder / f"{SID}.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                                         encoding="utf-8")
    N.run(cfg(), bot, tmap, ask_fn=lambda *a, **k: "גשר: משהו")
    return N.records().get(SID) or {}


def test_a_title_we_set_never_locks_the_namer(tmp_path, monkeypatch):
    TS.want(SID, "גשר: ישן")  # our earlier name, carried by the session as its app title
    assert _namer_round(tmp_path, monkeypatch, "גשר: ישן").get("why") != "app-title"


def test_the_owners_app_title_still_locks_the_namer(tmp_path, monkeypatch):
    TS.want(SID, "גשר: ישן")
    assert _namer_round(tmp_path, monkeypatch, "השם שלי").get("why") == "app-title"
