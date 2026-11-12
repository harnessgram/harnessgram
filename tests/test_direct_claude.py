"""What the owner changes or does directly in Claude Code reaches the topic: the permission mode, the agent and
the output style on the status card (and a line when the mode or agent changes), /clear and /resume, an Escape,
a /model typed in Claude, and the bridge's own menus marking the true current state."""
import json

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import direct, heartbeat, hooks, install, mirror, usage
from ctb.config import Config, state_dir, write_private
from ctb.topics import TopicMap

OWNER = 7
S1 = "aaaa1111-2222-3333-4444-555566667777"
S2 = "bbbb1111-2222-3333-4444-555566667777"


def cfg(**kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", **kw)


def sends(bot):
    return [p for m, p in bot.calls if m == "sendMessage"]


# --- recording ------------------------------------------------------------------------------------

def test_a_first_report_is_quiet_and_a_change_is_returned():
    assert direct.record(S1, mode="bypassPermissions") == []
    assert direct.record(S1, mode="bypassPermissions") == []
    assert direct.record(S1, mode="plan") == [("mode", "bypassPermissions", "plan")]
    assert direct.record(S1, mode="nonsense") == []  # only Claude Code's own modes
    assert direct.current(S1) == {"mode": "plan"}


def test_hook_input_gives_the_mode_and_a_subagents_hooks_are_ignored():
    direct.from_hook({"session_id": S1, "permission_mode": "default", "hook_event_name": "PostToolUse"})
    assert direct.from_hook({"session_id": S1, "permission_mode": "acceptEdits", "agent_id": "x"}) == []
    assert direct.from_hook({"session_id": S1, "permission_mode": "acceptEdits", "agent_type": "reviewer"}) == [
        ("mode", "default", "acceptEdits")]
    assert direct.current(S1) == {"mode": "acceptEdits", "agent": "reviewer"}


def test_the_status_line_gives_the_style_and_the_agent():
    direct.from_status({"session_id": S1, "output_style": {"name": "default"}})
    assert direct.from_status({"session_id": S1, "output_style": {"name": "Explanatory"},
                               "agent": {"name": "reviewer"}}) == [("agent", "", "reviewer"), ("style", "", "Explanatory")]
    assert direct.from_status({"session_id": S1, "output_style": {"name": "Explanatory"}}) == [
        ("agent", "reviewer", "")]  # no "agent" in the JSON: the main agent again


def test_a_mode_change_seen_by_a_hook_starts_one_topic_note(monkeypatch):
    spawned = []
    monkeypatch.setattr(hooks, "spawn", lambda args, text=None: spawned.append(args))
    hooks._direct({"session_id": S1, "permission_mode": "bypassPermissions"})
    hooks._direct({"session_id": S1, "permission_mode": "plan"})
    hooks._direct({"session_id": S1, "permission_mode": "plan"})
    assert spawned == [["direct-note", f"--session={S1}", "--field=mode", "--value=plan"]]


# --- the topic and the card ---------------------------------------------------------------------------

def test_the_note_line_and_the_card_show_the_mode_and_agent(monkeypatch):
    bot = FakeBot()
    c = cfg()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure(S1, "T")
    monkeypatch.setattr(mirror, "refresh_card", lambda *a, **k: True)
    direct.record(S1, mode="plan", agent="reviewer", style="Explanatory")
    bot.calls.clear()
    direct.run_note(c, S1, "mode", "plan", bot=bot)
    assert "מצב ההרשאות שונה" in sends(bot)[-1]["text"] and "תכנון" in sends(bot)[-1]["text"]
    lines = direct.card_lines(S1, "he")
    assert lines[0].startswith("🔐 מצב:") and "⁨reviewer⁩" in lines[1] and "⁨Explanatory⁩" in lines[2]
    # a saved card text gets today's lines in place of the old ones
    old = "‏🧠 הקשר 120 אלף\n🤖 ⁨Opus 5.5⁩ · 🎚️ גבוהה\n🔐 מצב: ⚠️ בלי הגבלות\n👤 main"
    new = direct.restamp(old, S1, "he").split("\n")
    assert new[2].startswith("🔐 מצב: 📝") and new[3].startswith("🧩") and new[-1] == "👤 main"
    assert sum(l.startswith("🔐") for l in new) == 1


def test_a_change_made_in_claude_redraws_the_card_at_the_context_pace(monkeypatch):
    monkeypatch.setattr(usage, "session_model", lambda session, transcript=None: ("Opus 5.5", "high"))
    before = mirror.card_data_key(S1)
    direct.record(S1, mode="plan")
    after = mirror.card_data_key(S1)
    assert before != after and mirror._only_context_moved(before, after)  # CONTEXT_BACKOFF, not CARD_BACKOFF
    monkeypatch.setattr(usage, "session_model", lambda session, transcript=None: ("Sonnet 5.5", "high"))
    assert mirror._only_context_moved(after, mirror.card_data_key(S1))  # a /model typed in Claude too


def test_footer_lines_carry_the_mode(tmp_path, monkeypatch):
    path = tmp_path / f"{S1}.jsonl"
    path.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5", "content": [],
                                "usage": {"input_tokens": 1000, "cache_read_input_tokens": 50000}}}) + "\n")
    direct.record(S1, mode="acceptEdits")
    lines = usage.footer_lines(cfg(), str(path))
    assert any(l.startswith("🔐 מצב:") for l in lines)


# --- /clear and /resume ---------------------------------------------------------------------------------

def test_clear_moves_the_topic_to_the_new_conversation_and_says_so(monkeypatch):
    bot = FakeBot()
    tm = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = tm.ensure(S1, "T")
    monkeypatch.setattr(mirror, "refresh_card", lambda *a, **k: True)
    bot.calls.clear()
    direct.run_note(cfg(), S2, "switch", f"clear:{S1}", bot=bot)
    tm = TopicMap(state_dir() / "topics.json", bot, OWNER)
    assert tm.thread_of(S2) == thread and tm.thread_of(S1) is None
    assert "השיחה נוקתה" in sends(bot)[-1]["text"] and sends(bot)[-1]["message_thread_id"] == thread


def test_resume_into_a_conversation_with_its_own_topic_tells_both(monkeypatch):
    bot = FakeBot()
    tm = TopicMap(state_dir() / "topics.json", bot, OWNER)
    t1, t2 = tm.ensure(S1, "A"), tm.ensure(S2, "B")
    monkeypatch.setattr(mirror, "refresh_card", lambda *a, **k: True)
    bot.calls.clear()
    direct.run_note(cfg(), S2, "switch", f"resume:{S1}", bot=bot)
    got = {p["message_thread_id"]: p["text"] for p in sends(bot)}
    assert "חודשה" in got[t2] and "עבר" in got[t1]
    assert TopicMap(state_dir() / "topics.json", bot, OWNER).thread_of(S1) == t1  # each keeps its topic


def test_a_switch_value_must_name_a_session(monkeypatch):
    bot = FakeBot()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure(S1, "T")
    bot.calls.clear()
    assert direct.run_note(cfg(), S2, "switch", "clear:../../x", bot=bot) == 0 and sends(bot) == []


# --- Escape typed in Claude ----------------------------------------------------------------------------

def interrupted_transcript(tmp_path):
    path = tmp_path / "t.jsonl"
    rows = [{"type": "user", "uuid": "u1", "message": {"content": "do it"}},
            {"type": "assistant", "uuid": "a1", "message": {"content": [{"type": "text", "text": "Working"}]}},
            {"type": "user", "uuid": "u2", "timestamp": "2026-11-10T10:00:00Z",
             "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}}]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_an_escape_in_claude_settles_the_busy_topic_once(tmp_path, monkeypatch):
    from ctb import inbox
    bot = FakeBot()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure(S1, "T")
    path = interrupted_transcript(tmp_path)
    settled = []
    monkeypatch.setattr(heartbeat, "pane_spinner", lambda s: None)
    monkeypatch.setattr(inbox, "_interrupted_idle", lambda c, b, s: settled.append(s))
    bot.calls.clear()
    assert heartbeat.escaped_in_claude(cfg(), bot, S1, find=lambda s: str(path))
    assert settled == [S1] and "נעצר ב-" in sends(bot)[-1]["text"]
    assert not heartbeat.escaped_in_claude(cfg(), bot, S1, find=lambda s: str(path))  # said once
    assert len(sends(bot)) == 1


def test_a_stop_from_telegram_is_not_reported_as_an_escape_in_claude(tmp_path, monkeypatch):
    from ctb import inbox, status
    path = interrupted_transcript(tmp_path)
    monkeypatch.setattr(status, "transcript_of", lambda s: str(path))
    monkeypatch.setattr(inbox, "session_busy", lambda s: False)
    inbox._interrupted_idle(cfg(), FakeBot(), S1)  # the bridge's own Stop button ran this
    monkeypatch.setattr(heartbeat, "pane_spinner", lambda s: None)
    assert not heartbeat.escaped_in_claude(cfg(), FakeBot(), S1, find=lambda s: str(path))


def test_a_new_turn_after_the_escape_is_not_an_interrupt(tmp_path):
    path = interrupted_transcript(tmp_path)
    with open(path, "a") as f:
        f.write(json.dumps({"type": "user", "uuid": "u3", "message": {"content": "next"}}) + "\n")
    assert direct.interrupted(str(path)) == ""


# --- /model typed in Claude, and the menus ---------------------------------------------------------------

def test_a_model_switch_in_claude_is_saved_at_once(monkeypatch):
    spawned = []
    monkeypatch.setattr(hooks, "spawn", lambda args, text=None: spawned.append(args))
    monkeypatch.setattr(hooks, "load_config", lambda: cfg())
    hooks.hook_lifecycle(json.dumps({"session_id": S1, "hook_event_name": "PostModelSwitch",
                                     "from_model": "claude-opus-5-5", "to_model": "claude-sonnet-5-5",
                                     "effort": {"level": "medium"}}))
    saved = json.loads((state_dir() / "session-models.json").read_text())
    assert saved[S1] == "Sonnet 5.5|medium"
    assert spawned == [["direct-note", f"--session={S1}", "--field=model", "--value="]]
    assert "PostModelSwitch" in install.desired_hooks()


def test_the_effort_and_mode_menus_mark_the_current_state(monkeypatch):
    from ctb import inbox
    said = []
    inbox.effort_menu(lambda text, **kw: said.append(kw), cfg(), "high")
    labels = [b["text"] for row in said[0]["reply_markup"]["inline_keyboard"] for b in row]
    assert [l for l in labels if l.startswith("✅")] == ["✅ גבוהה"]
    bot = FakeBot()
    monkeypatch.setattr(inbox, "pane_for_session", lambda s: None)  # no window to read the mode from
    direct.record(S1, mode="plan")
    inbox.send_mode_menu(bot, cfg(), {"chat": {"id": OWNER}, "message_thread_id": 5}, S1)
    sent = sends(bot)[-1]
    marked = [b["text"] for row in sent["reply_markup"]["inline_keyboard"] for b in row if b["text"].startswith("✅")]
    assert len(marked) == 1 and marked[0].endswith(direct.mode_label("plan", "he"))
