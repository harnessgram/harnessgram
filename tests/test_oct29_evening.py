"""The 2026-10-29 evening live changes: heartbeat, topic icons, translated cards and notices, cache rule,
stuck alert exceptions. All network and subprocess calls are mocked."""
import json
import threading
import os
import time

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import cli, dashboard, heartbeat, hooks, mirror, progress, topic_icons, topics, translate
from ctb.config import AGENT_NOTICES, Config, set_setting, state_dir
from ctb.projects import Place
from ctb.topics import PLACEHOLDER, TopicMap, session_state, set_state


@pytest.fixture
def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7)


def tcfg(**kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, translate=True, engines=["gemini"],
                  translate_cache=False, **kw)


def rig(bot):
    tm = TopicMap(state_dir() / "topics.json", bot, 7)
    tm.ensure("s", "Title")
    return tm


# 1. heartbeat ---------------------------------------------------------------------------------

def test_heartbeat_refreshes_busy_active_sessions_only(cfg, bot, tmp_path):
    rig(bot)
    set_state(cfg, "s", "busy", bot)
    first = session_state("s")["placeholder"]
    assert bot.methods("sendMessage")[-1]["text"] == PLACEHOLDER
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("{}")
    now = os.path.getmtime(transcript) + 10
    assert heartbeat.run_once(cfg, bot, now=now, find=lambda s: str(transcript)) == 1
    new = session_state("s")["placeholder"]
    assert new != first
    # the old placeholder is always deleted, even though it was the tracked "current" one
    assert first in {p["message_id"] for p in bot.methods("deleteMessage")}
    assert session_state("s")["all"] == [new]
    # stalled transcript: no refresh
    assert heartbeat.run_once(cfg, bot, now=now + 100, find=lambda s: str(transcript)) == 0
    # idle session: no refresh
    set_state(cfg, "s", "done", bot)
    assert heartbeat.run_once(cfg, bot, now=now, find=lambda s: str(transcript)) == 0


def test_heartbeat_cli_and_loop(cfg, monkeypatch):
    args = cli._parser().parse_args(["heartbeat", "--once"])
    assert args.cmd == "heartbeat" and args.once
    rounds = []
    monkeypatch.setattr(heartbeat, "run_once", lambda c, bot=None: rounds.append(1))
    monkeypatch.setattr(heartbeat.Bot, "from_config", classmethod(lambda cls, c: FakeBot()))
    # the loop's revive work reads the live tmux server: isolated as test_revive.py does (a set `revived`, from
    # the background revive or an earlier test, made the round call revive.snapshot -> tmux ls)
    monkeypatch.setattr(heartbeat.revive, "revived", threading.Event())
    monkeypatch.setattr(heartbeat.revive, "revive_in_background", lambda: None)
    monkeypatch.setattr(heartbeat.revive, "snapshot", lambda *a, **k: None)
    sleeps = []

    def sleep(n):
        sleeps.append(n)
        if len(sleeps) == 2:
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        heartbeat.run(cfg, every=12, sleep_fn=sleep)
    assert len(rounds) == 2 and sleeps == [12, 12]


# 2. topic icons -------------------------------------------------------------------------------

class IconBot(FakeBot):
    def call(self, method, params=None, timeout=15):
        if method == "getForumTopicIconStickers":
            self.calls.append((method, params or {}))
            return [{"emoji": e, "custom_emoji_id": "id" + str(i)} for i, e in enumerate(["💬", "🎬", "🔞", "💡"])]
        return super().call(method, params, timeout)


def test_topic_icons_pick_banned_and_only_on_name_change(cfg, monkeypatch):
    bot = IconBot()
    tm = rig(bot)
    monkeypatch.setattr(translate, "ask", lambda prompt, c: "🔞")  # banned: must not be used
    assert topic_icons.run(cfg, bot) == 1
    edit = [p for p in bot.methods("editForumTopic") if "icon_custom_emoji_id" in p][-1]
    assert edit["icon_custom_emoji_id"] == "id3" and edit["message_thread_id"] == tm.thread_of("s")  # 💡 fallback
    bot.calls.clear()
    assert topic_icons.run(cfg, bot) == 0 and not bot.calls  # same name: nothing to do
    tm.ensure("s", "Telegram bridge work")
    monkeypatch.setattr(translate, "ask", lambda prompt, c: "")  # Gemini down: keyword fallback
    bot.calls.clear()
    assert topic_icons.run(cfg, bot) == 1
    assert bot.methods("editForumTopic")[-1]["icon_custom_emoji_id"] == "id0"  # 💬


def test_topic_icons_gemini_choice_and_dashboard_cli_hook(cfg, monkeypatch):
    bot = IconBot()
    rig(bot)
    seen = []
    monkeypatch.setattr(translate, "ask", lambda prompt, c: seen.append(prompt) or "🎬")
    topic_icons.run(cfg, bot)
    assert bot.methods("editForumTopic")[-1]["icon_custom_emoji_id"] == "id1"
    assert "🔞" not in seen[0].split("List:")[1]
    assert cli._parser().parse_args(["topic-icons"]).cmd == "topic-icons"


def test_ask_uses_the_gemini_routes(monkeypatch):
    seen = []
    monkeypatch.setattr(translate, "_gemini_call", lambda prompt, c: seen.append(prompt) or "ok")
    assert translate.ask("raw prompt", Config()) == "ok" and seen == ["raw prompt"]
    monkeypatch.setattr(translate, "_gemini_call", lambda prompt, c: (_ for _ in ()).throw(OSError()))
    assert translate.ask("x", Config()) == ""


# 3. question cards ----------------------------------------------------------------------------

def test_card_gets_hebrew_text_and_labels_but_same_callbacks(monkeypatch):
    markup = {"inline_keyboard": [[{"text": "1. Yes", "callback_data": "k:1:abc"}],
                                  [{"text": "2. No", "callback_data": "k:2:abc"}],
                                  [{"text": "✖️ ביטול", "callback_data": "k:esc"}]]}
    monkeypatch.setattr(mirror, "translate_text", lambda t, c, fast=False: "1. כן\n2. לא" if t.startswith("1.") else "שאלה <בעברית>")
    text, out = mirror.translate_card(tcfg(target_language="he"), "<pre>Do you want to proceed?</pre>",
                                      "Do you want to proceed?\n1. Yes\n2. No", markup)
    assert "בעברית:" in text and "&lt;בעברית&gt;" in text and len(text) < 4096
    assert [b["text"] for r in out["inline_keyboard"] for b in r] == ["1. כן", "2. לא", "✖️ ביטול"]
    assert [b["callback_data"] for r in out["inline_keyboard"] for b in r] == ["k:1:abc", "k:2:abc", "k:esc"]
    assert all(len(b["callback_data"].encode()) <= 64 for r in out["inline_keyboard"] for b in r)


def test_card_unchanged_when_translate_off_or_fails(monkeypatch):
    markup = {"inline_keyboard": [[{"text": "1. Yes", "callback_data": "k:1:abc"}]]}
    monkeypatch.setattr(mirror, "translate_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    off = Config(translate=False)
    assert mirror.translate_card(off, "T", "Proceed with this change now?", markup) == ("T", markup)
    text, out = mirror.translate_card(tcfg(target_language="he"), "T", "Proceed with this change now?", markup)
    assert text == "T" and out["inline_keyboard"][0][0]["text"] == "1. Yes"


def test_run_notify_sends_the_translated_card_to_the_owner_topic(cfg, bot, monkeypatch):
    tm = rig(bot)
    body = "Proceed with this change now?\n❯ 1. Yes\n  2. No\n\nEnter to select · Esc to cancel"
    from ctb import dialogs
    monkeypatch.setattr(dialogs, "open_screen", lambda s: ("%1", body, dialogs.parse(body), True))
    monkeypatch.setattr(mirror.time, "sleep", lambda s: None)
    monkeypatch.setattr(mirror, "translate_text", lambda t, c, fast=False: "1. כן" if t == "1. Yes" else "להמשיך?")
    c = tcfg(target_language="he")
    c.bot_token, c.owner_chat_id = GOOD_TOKEN, 7
    assert mirror.run_notify(c, "s", "permission_prompt", bot=bot) == 0
    sent = bot.methods("sendMessage")[-1]
    assert sent["chat_id"] == 7 and sent["message_thread_id"] == tm.thread_of("s")
    # 2026-11-10: the question goes out at once as shown, and is then edited to its translation
    assert "להמשיך?" not in sent["text"] and sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("dg:e:")
    edited = bot.methods("editMessageText")[-1]
    assert "להמשיך?" in edited["text"] and edited["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("dg:e:")


# 4. progress headings -------------------------------------------------------------------------

def test_progress_heading_translated_only_detached(cfg, bot, monkeypatch):
    rig(bot)
    seen = []
    monkeypatch.setattr("ctb.translate.translate_text", lambda t, c, fast=False: seen.append(t) or "בודק קבצים")
    row = {"kind": "action", "t": 1.0, "l": "⚙️ Checking files\n     ↳ ls -la", "d": "ls -la", "output": "total 4"}
    c = tcfg(target_language="he")
    c.bot_token, c.owner_chat_id = GOOD_TOKEN, 7
    monkeypatch.setattr("ctb.translate.ask", lambda prompt, cfg, **k: "")  # the heading prompt fails: plain translation
    progress._send_ordered(c, "s", [row], {"cursor": 0, "cards": []}, state_dir() / "p.msg", 101, bot,
                           time.time, False)
    # 2026-11-10: the step shows at once in English; the background job translates its heading after
    text = [p for m, p in bot.calls if m == "sendMessage" and p.get("parse_mode") == "HTML"][-1]["text"]
    assert "Checking files" in text and seen == []
    assert progress.jobs_waiting("s", root=state_dir())  # this test keeps the card state in the state dir
    monkeypatch.setattr(progress, "_refresh_card", lambda *a, **k: None)
    progress.run_jobs(c, "s", root=state_dir(), bot=bot)
    shown = progress.card_text({"row": row}, c)
    assert "בודק קבצים" in shown and "<code>ls -la</code>" in shown and "total 4" in shown
    assert seen == ["Checking files"]  # never the command or the output
    seen.clear()
    bot.calls.clear()
    progress._send_ordered(c, "s", [row], {"cursor": 0, "cards": []}, state_dir() / "p.msg", 101, bot,
                           time.time, True)  # fast: inside the Stop path
    # no engine call in the Stop path; the heading translated before comes from the cache at once
    assert seen == [] and any("בודק קבצים" in p["text"] for p in bot.methods("sendMessage"))


# 5. SubagentStop notice -----------------------------------------------------------------------

def test_agents_card_names_agent_task_model_effort_and_summary(cfg, bot, tmp_path, monkeypatch):
    agent = tmp_path / "agent-1.jsonl"
    rows = [{"type": "user", "message": {"role": "user", "content": "Review the diff"}},
            {"type": "assistant", "message": {"role": "assistant", "model": "claude-sonnet-5-5",
                                             "content": [{"type": "text", "text": "Found two bugs. Summary: two bugs in parser"}]}}]
    agent.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (tmp_path / "agent-1.meta.json").write_text(json.dumps({"description": "Review parser diff", "effort": "high"}))
    monkeypatch.setattr(mirror.TopicMap, "place_of", lambda self, session: Place(self.chat_id, 12))
    monkeypatch.setattr(mirror, "translate_text", lambda t, c, fast=False: "שני באגים בפרסר")
    c = tcfg(target_language="he")
    c.bot_token, c.owner_chat_id = GOOD_TOKEN, 7
    mirror.run_lifecycle(c, "s", "subagent-stop", "reviewer", "", bot=bot, agent_transcript=str(agent))
    saved = json.loads((state_dir() / "agents-cards" / "s.json").read_text())
    agent = saved["cards"]["1"]["agents"][0]
    assert agent["name"] == "Review parser diff" and agent["model"] == "Sonnet 5.5, high"
    assert agent["summary"] == "שני באגים בפרסר" and "Found two bugs" in agent["full"]


def test_subagent_notice_is_detached_with_paths(cfg, monkeypatch):
    set_setting(AGENT_NOTICES, True)
    calls = []
    monkeypatch.setattr(hooks, "load_config", lambda: cfg)
    monkeypatch.setattr(hooks, "spawn", lambda args: calls.append(args))
    hooks.hook_lifecycle(json.dumps({"session_id": "s", "hook_event_name": "SubagentStop",
                                     "agent_transcript_path": "/a/agent.jsonl", "transcript_path": "/a/s.jsonl"}))
    assert "--agent-transcript=/a/agent.jsonl" in calls[0] and "--transcript=/a/s.jsonl" in calls[0]
    ns = cli._parser().parse_args(["lifecycle", "--session=s", "--kind=subagent-stop",
                                   "--agent-transcript=/a/agent.jsonl"])
    assert ns.agent_transcript == "/a/agent.jsonl"


# 6. translation cache -------------------------------------------------------------------------

def test_fast_hook_translation_is_not_cached(monkeypatch):
    monkeypatch.setattr(translate, "google", lambda *a: "quick")
    monkeypatch.setattr(translate, "gemini", lambda *a: "better")
    c = Config(engines=["gemini", "google"])
    assert translate.translate_text("Hello there friend", c, fast=True) == "quick"
    assert not (state_dir() / "translate-cache").exists() or not list((state_dir() / "translate-cache").iterdir())
    assert translate.translate_text("Hello there friend", c, fast=False) == "better"  # the mirror uses Gemini
    assert translate.translate_text("Hello there friend", c, fast=True) == "better"  # and the cache serves it


# 7. stuck alert -------------------------------------------------------------------------------

def stuck_rig(bot, tmp_path, rows):
    tm = rig(bot)
    set_state(Config(bot_token=GOOD_TOKEN, owner_chat_id=7), "s", "busy", bot)
    t = tmp_path / "s.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    os.utime(t, (1000, 1000))
    bot.calls.clear()
    return tm, str(t)


WAKE = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "ScheduleWakeup", "id": "w", "input": {}}]}}
USER = {"type": "user", "message": {"role": "user", "content": "do the thing"}}


def test_stuck_alert_skips_wakeup_and_background_jobs(cfg, bot, tmp_path):
    tm, path = stuck_rig(bot, tmp_path, [USER, WAKE])
    dashboard.stuck_check(cfg, tm.all(), bot, now=5000, find=lambda s: path)
    assert not bot.methods("sendMessage")  # wakeup armed after the last real user message
    bg = {"type": "assistant", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "message": {"content": [{"type": "tool_use", "name": "Bash", "id": "b", "input": {"run_in_background": True}}]}}
    tm, path = stuck_rig(bot, tmp_path, [WAKE, USER, bg])
    dashboard.stuck_check(cfg, tm.all(), bot, now=time.time() + 5000, find=lambda s: path)
    assert not bot.methods("sendMessage")  # a wakeup before the user message does not count, but a job runs


def test_stuck_alert_still_fires_when_truly_stuck(cfg, bot, tmp_path):
    tm, path = stuck_rig(bot, tmp_path, [WAKE, USER])  # the wakeup is older than the user message
    dashboard.stuck_check(cfg, tm.all(), bot, now=5000, find=lambda s: path)
    assert "⚠️" in bot.methods("sendMessage")[-1]["text"]


def test_agent_actions_are_tagged_and_bare_commands_not_translated(tmp_path):
    import json as _json
    from ctb import progress
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("")
    agents = tmp_path / "s" / "subagents"
    agents.mkdir(parents=True)
    (agents / "agent-a1.meta.json").write_text(_json.dumps({"description": "Port fixes", "model": "sonnet"}))
    data = {"tool_name": "Bash", "tool_input": {"command": "cd /repo && git status"},
            "agent_id": "a1", "transcript_path": str(transcript)}
    assert progress.label(data) == "🤖 [Port fixes · Sonnet] cd /repo && git"
    assert progress.label({"tool_name": "Bash", "tool_input": {"command": "ls -la"}}) == "⚙️ ls -la"
    assert progress.agent_name({"agent_id": "../x", "transcript_path": str(transcript)}) == ""


def test_heartbeat_counts_agent_activity(tmp_path):
    import os
    from ctb import heartbeat
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("")
    os.utime(transcript, (1000, 1000))
    agents = tmp_path / "s" / "subagents"
    agents.mkdir(parents=True)
    (agents / "agent-a1.jsonl").write_text("")
    os.utime(agents / "agent-a1.jsonl", (5000, 5000))
    assert heartbeat.last_activity(str(transcript)) == 5000


def test_heartbeat_sends_once_per_round_and_takes_turns(cfg, bot, tmp_path):
    tm = TopicMap(state_dir() / "topics.json", bot, 7)
    for sid in ("a", "b", "c"):
        tm.ensure(sid, sid)
        set_state(cfg, sid, "busy", bot)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}")
    now = os.path.getmtime(transcript) + 10
    before = {sid: session_state(sid)["placeholder"] for sid in "abc"}
    sends = len(bot.methods("sendMessage"))
    for _ in range(3):
        assert heartbeat.run_once(cfg, bot, now=now, find=lambda s: str(transcript)) == 1
    assert len(bot.methods("sendMessage")) - sends == 3  # never one send per session per round
    assert all(session_state(sid)["placeholder"] != before[sid] for sid in "abc")  # each got its turn
