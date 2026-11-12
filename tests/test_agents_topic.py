"""Optional agents topic (issue #25): background agents report in "<session> · agents", closed when they are all done."""
from __future__ import annotations

import json

from conftest import GOOD_TOKEN, FakeBot as _FakeBot
from ctb import agents_topic, hooks as H, i18n, inbox as I, mirror as M
from ctb.agents_topic import AGENTS_TOPIC
from ctb.config import AGENT_NOTICES, Config, set_setting, setting_on, state_dir
from ctb.install import desired_hooks
from ctb.telegram import TelegramError
from ctb.topics import TopicMap



class FakeBot(_FakeBot):
    """Like the shared fake, but a failure carries Telegram's error code (400), except a "timeout" (no answer)."""

    def call(self, method, params=None, timeout=15):
        if method in self.fail:
            self.calls.append((method, params or {}))
            desc = self.fail.pop(method)
            raise TelegramError(desc, None if desc in ("timeout", "network error") else 400)
        return super().call(method, params, timeout)


CHAT = -1001234567
SID = "sess-agents-1"


def cfg(**kw):
    kw.setdefault("target_language", "en")
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, chat_id=CHAT, **kw)


def main_thread(bot):
    return TopicMap(state_dir() / "topics.json", bot, CHAT).ensure(SID, "Build the parser")


def start(bot, agent_id, c=None):
    return M.run_lifecycle(c or cfg(), SID, "subagent-start", "worker", bot=bot, agent_id=agent_id)


def stop(bot, agent_id, desc="audit", c=None):
    return M.run_lifecycle(c or cfg(), SID, "subagent-stop", "worker", desc, bot=bot, agent_id=agent_id)


def sent_to(bot, thread):
    return [p for p in bot.methods("sendMessage") if p.get("message_thread_id") == thread]


def test_toggle_off_uses_the_agents_card_and_no_topic(monkeypatch):
    bot, spawned = FakeBot(), []
    thread = main_thread(bot)
    assert not setting_on(AGENTS_TOPIC)
    monkeypatch.setattr(H, "load_config", lambda: cfg())
    monkeypatch.setattr(H, "spawn", lambda args: spawned.append(args))
    H.hook_lifecycle(json.dumps({"session_id": SID, "hook_event_name": "SubagentStart", "agent_id": "a1"}))
    H.hook_lifecycle(json.dumps({"session_id": SID, "hook_event_name": "SubagentStop", "agent_id": "a1"}))
    assert [a[2] for a in spawned] == ["--kind=subagent-start", "--kind=subagent-stop"]  # the card counts them
    assert start(bot, "a1") == 0 and stop(bot, "a1") == 0
    assert {p["message_thread_id"] for p in bot.methods("sendMessage")} == {thread}  # one card in the main topic
    assert not any(m in ("createForumTopic", "closeForumTopic", "reopenForumTopic")
                   for m, _ in bot.calls if m != "createForumTopic" or _.get("name") != "Build the parser")
    assert not (state_dir() / "agents-topics").exists()


def test_hook_routes_start_and_every_stop_when_the_topic_is_on(monkeypatch):
    spawned = []
    set_setting(AGENTS_TOPIC, True)  # AGENT_NOTICES stays off
    monkeypatch.setattr(H, "load_config", lambda: cfg())
    monkeypatch.setattr(H, "spawn", lambda args: spawned.append(args))
    H.hook_lifecycle(json.dumps({"session_id": SID, "hook_event_name": "SubagentStart", "agent_id": "a1",
                                 "agent_type": "worker"}))
    for _ in range(2):  # the 60 second throttle does not apply: every end must be counted
        H.hook_lifecycle(json.dumps({"session_id": SID, "hook_event_name": "SubagentStop", "agent_id": "a1"}))
    assert [a[2] for a in spawned] == ["--kind=subagent-start", "--kind=subagent-stop", "--kind=subagent-stop"]
    assert "--agent-id=a1" in spawned[0] and "--agent-id=a1" in spawned[1]


def test_topic_is_created_once_per_session_and_collects_the_notices():
    bot = FakeBot()
    set_setting(AGENTS_TOPIC, True)
    main = main_thread(bot)
    start(bot, "a1")
    start(bot, "a2")
    stop(bot, "a1", "first task")
    creates = [p for p in bot.methods("createForumTopic") if "agents" in p["name"]]
    assert len(creates) == 1 and creates[0]["name"] == "Build the parser · agents"
    thread = TopicMap(state_dir() / "topics.json", bot, CHAT).thread_of(SID)
    assert thread == main
    agents = [p for p in bot.methods("sendMessage") if p.get("message_thread_id") != main]
    assert len(agents) == 3 and "first task" in agents[-1]["text"]
    assert all(p["disable_notification"] for p in agents)
    assert sent_to(bot, main) == []  # nothing in the main topic while agents run
    assert bot.methods("closeForumTopic") == []  # a2 is still running
    other = FakeBot()  # another session gets its own topic
    TopicMap(state_dir() / "topics.json", other, CHAT).ensure("sess-other", "Other")
    M.run_lifecycle(cfg(), "sess-other", "subagent-start", "worker", bot=other, agent_id="x")
    assert len([p for p in other.methods("createForumTopic") if "agents" in p["name"]]) == 1


def test_topic_closes_with_one_link_line_when_all_agents_finish():
    bot = FakeBot()
    set_setting(AGENTS_TOPIC, True)
    main = main_thread(bot)
    start(bot, "a1")
    start(bot, "a2")
    stop(bot, "a1")
    stop(bot, "a2")
    agents_thread = next(p["message_thread_id"] for p in bot.methods("sendMessage") if p["message_thread_id"] != main)
    closes = bot.methods("closeForumTopic")
    assert len(closes) == 1 and closes[0] == {"chat_id": CHAT, "message_thread_id": agents_thread}
    lines = sent_to(bot, main)
    assert len(lines) == 1
    assert lines[0]["text"].endswith(f"\nhttps://t.me/c/1234567/{agents_thread}")
    assert i18n.t("agents_topic_done", lang="en") in lines[0]["text"]


def test_a_new_agent_reopens_the_closed_topic_and_it_closes_again():
    bot = FakeBot()
    set_setting(AGENTS_TOPIC, True)
    main = main_thread(bot)
    start(bot, "a1")
    stop(bot, "a1")
    assert len(bot.methods("closeForumTopic")) == 1
    start(bot, "a2")
    reopens = bot.methods("reopenForumTopic")
    assert len(reopens) == 1 and reopens[0]["message_thread_id"] == bot.methods("closeForumTopic")[0]["message_thread_id"]
    assert len([p for p in bot.methods("createForumTopic") if "agents" in p["name"]]) == 1
    stop(bot, "a2")
    assert len(bot.methods("closeForumTopic")) == 2 and len(sent_to(bot, main)) == 2


def test_stop_without_a_start_hook_reports_but_posts_no_all_finished_line():
    bot = FakeBot()
    set_setting(AGENTS_TOPIC, True)
    main = main_thread(bot)
    stop(bot, "one", "old hooks")  # hooks installed before SubagentStart existed
    stop(bot, "two", "old hooks")
    assert len([p for p in bot.methods("createForumTopic") if "agents" in p["name"]]) == 1
    assert len([p for p in bot.methods("sendMessage") if p["message_thread_id"] != main]) == 2
    assert sent_to(bot, main) == [] and bot.methods("closeForumTopic") == []  # the end of the last agent is unknown
    M.run_lifecycle(cfg(), SID, "session-end", bot=bot)
    # the session end still closes it, and then the session's own topic
    assert [p["message_thread_id"] for p in bot.methods("closeForumTopic")].count(main) == 1
    assert len(bot.methods("closeForumTopic")) == 2


def test_unknown_agent_id_never_removes_another_running_agent():
    bot = FakeBot()
    set_setting(AGENTS_TOPIC, True)
    main_thread(bot)
    start(bot, "a1")
    stop(bot, "ghost")  # an id that never started
    assert bot.methods("closeForumTopic") == []
    start(bot, "a1")  # a late duplicate start of a running id is just a refresh
    stop(bot, "a1")
    assert len(bot.methods("closeForumTopic")) == 1
    start(bot, "a1")  # a late start of an already finished id is ignored: no reopen, nothing running
    assert bot.methods("reopenForumTopic") == []
    start(bot, "a2")
    stop(bot, "a2")
    assert len(bot.methods("closeForumTopic")) == 2


def test_empty_agent_id_ends_the_oldest_running_agent():
    bot = FakeBot()
    set_setting(AGENTS_TOPIC, True)
    main_thread(bot)
    agents_topic.agent_started(cfg(), SID, "a1", "w", bot, now=100.0)
    agents_topic.agent_started(cfg(), SID, "a2", "w", bot, now=200.0)
    agents_topic.agent_finished(cfg(), SID, "", "x", bot, None, now=300.0)
    assert bot.methods("closeForumTopic") == []
    agents_topic.agent_finished(cfg(), SID, "a2", "y", bot, None, now=301.0)
    assert len(bot.methods("closeForumTopic")) == 1


def test_no_topic_rights_falls_back_to_the_main_topic_and_backs_off():
    bot = FakeBot()
    set_setting(AGENTS_TOPIC, True)
    main = main_thread(bot)
    bot.fail["createForumTopic"] = "Bad Request: not enough rights to create a topic"
    start(bot, "a1")
    stop(bot, "a1", "lost?")
    texts = [p["text"] for p in sent_to(bot, main)]
    assert len(texts) == 1 and "lost?" in texts[0]  # the notice arrived in the main topic
    assert len([p for p in bot.methods("createForumTopic") if "agents" in p["name"]]) == 1  # no retry storm
    assert bot.methods("closeForumTopic") == []


def test_deleted_topic_is_recreated_and_a_failing_send_falls_back():
    bot = FakeBot()
    set_setting(AGENTS_TOPIC, True)
    main = main_thread(bot)
    start(bot, "a1")
    bot.fail["sendMessage"] = "Bad Request: message thread not found"  # the owner deleted the topic
    stop(bot, "a1", "after delete")
    assert len([p for p in bot.methods("createForumTopic") if "agents" in p["name"]]) == 2
    assert "after delete" in [p for p in bot.methods("sendMessage") if p["message_thread_id"] != main][-1]["text"]

    start(bot, "a2")
    bot.fail["sendMessage"] = "Forbidden: bot was blocked"
    stop(bot, "a2", "forbidden")
    assert any("forbidden" in p["text"] for p in sent_to(bot, main))  # never lost


def test_failed_close_keeps_the_topic_open_and_posts_no_link():
    bot = FakeBot()
    set_setting(AGENTS_TOPIC, True)
    main = main_thread(bot)
    start(bot, "a1")
    bot.fail["closeForumTopic"] = "Bad Request: not enough rights"
    stop(bot, "a1")
    assert sent_to(bot, main) == []
    start(bot, "a2")  # still open: no reopen needed
    assert bot.methods("reopenForumTopic") == []


def test_session_end_closes_an_open_agents_topic():
    bot = FakeBot()
    set_setting(AGENTS_TOPIC, True)
    main = main_thread(bot)
    start(bot, "a1")
    M.run_lifecycle(cfg(), SID, "session-end", bot=bot)
    closed = [p["message_thread_id"] for p in bot.methods("closeForumTopic")]
    assert len(closed) == 2 and main in closed  # the agents topic, then the session's own topic


def test_stale_agents_do_not_hold_the_topic_open(monkeypatch):
    bot = FakeBot()
    set_setting(AGENTS_TOPIC, True)
    main_thread(bot)
    agents_topic.agent_started(cfg(), SID, "ghost", "worker", bot, now=1000.0)
    agents_topic.agent_finished(cfg(), SID, "a2", "done", bot, None, now=1000.0 + agents_topic.STALE_AFTER + 1)
    assert len(bot.methods("closeForumTopic")) == 1


def test_settings_toggle_and_strings_exist_in_both_languages(monkeypatch):
    bot = FakeBot()
    topics = TopicMap(state_dir() / "topics.json", bot, 7)
    thread = topics.ensure(SID, "Title")
    for lang in ("en", "he"):
        off = i18n.t("global_btn_agents_topic", lang=lang, state=i18n.t("agents_topic_off", lang=lang))
        assert off in [b["text"] for r in I.global_markup(lang=lang)["inline_keyboard"] for b in r]
        for key in ("agents_topic_on", "agents_topic_suffix", "agents_topic_started", "agents_topic_done"):
            assert i18n.t(key, lang=lang)
    q = {"id": "q", "from": {"id": 7}, "data": "more:agentstopic",
         "message": {"chat": {"id": 7, "type": "private"}, "message_thread_id": thread, "message_id": 50, "text": "x"}}
    assert I.handle_button(q, Config(bot_token=GOOD_TOKEN, owner_chat_id=7), bot, topics,
                           I.Inbox(state_dir() / "inbox")) == "settings"
    assert setting_on(AGENTS_TOPIC) and not setting_on(AGENT_NOTICES)
    on = i18n.t("global_btn_agents_topic", lang="he", state=i18n.t("agents_topic_on", lang="he"))
    assert on in [b["text"] for r in bot.methods("editMessageText")[-1]["reply_markup"]["inline_keyboard"] for b in r]


def test_hebrew_topic_name_and_link_line_follow_the_language_rules():
    bot = FakeBot()
    set_setting(AGENTS_TOPIC, True)
    main = main_thread(bot)
    c = cfg(target_language="he")
    start(bot, "a1", c)
    stop(bot, "a1", "audit", c)
    name = [p["name"] for p in bot.methods("createForumTopic") if "סוכנים" in p["name"]]
    assert name == ["Build the parser · סוכנים"]
    line = sent_to(bot, main)[0]["text"]
    assert all(not (i18n.t("agents_topic_done", lang="he") in part and "http" in part) for part in line.split("\n"))


def test_install_registers_the_subagent_start_hook():
    assert "SubagentStart" in desired_hooks()


def test_concurrent_agent_ends_are_not_throttled_or_blocked_by_the_old_lock(monkeypatch):
    import fcntl
    spawned = []
    set_setting(AGENTS_TOPIC, True)
    monkeypatch.setattr(H, "load_config", lambda: cfg())
    monkeypatch.setattr(H, "spawn", lambda args: spawned.append(args))
    lock_dir = state_dir() / "lifecycle"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / (SID + ".lock"), "a") as held:  # another hook process holds the old throttle lock
        fcntl.flock(held, fcntl.LOCK_EX)
        for agent in ("a1", "a2", "a3"):
            H.hook_lifecycle(json.dumps({"session_id": SID, "hook_event_name": "SubagentStop", "agent_id": agent}))
    assert len(spawned) == 3
    assert not (lock_dir / SID).exists()  # no throttle mark either


def test_a_timeout_does_not_duplicate_the_notice_in_the_main_topic():
    bot = FakeBot()
    set_setting(AGENTS_TOPIC, True)
    main = main_thread(bot)
    start(bot, "a1")
    bot.fail["sendMessage"] = "timeout"  # may have arrived
    stop(bot, "a1", "maybe sent")
    assert not any("maybe sent" in p["text"] for p in sent_to(bot, main))


def test_a_short_429_is_waited_out_once_and_a_long_one_falls_back(monkeypatch):
    sleeps = []
    monkeypatch.setattr(agents_topic.time, "sleep", lambda n: sleeps.append(n))

    class Limited(FakeBot):
        def __init__(self, after):
            super().__init__()
            self.after, self.limited = after, False

        def call(self, method, params=None, timeout=15):
            if method == "sendMessage" and (params or {}).get("message_thread_id") != self.main and self.limited:
                self.limited = False
                self.calls.append((method, dict(params)))
                raise TelegramError("Too Many Requests", 429, self.after)
            return super().call(method, params, timeout)

    set_setting(AGENTS_TOPIC, True)
    for after, waited in ((2, [2]), (30, [])):
        bot = Limited(after)
        bot.main = main_thread(bot)
        start(bot, "a1")
        bot.limited = True
        sleeps.clear()
        stop(bot, "a1", f"limited {after}")
        assert sleeps == waited
        in_agents = [p for p in bot.methods("sendMessage") if p["message_thread_id"] != bot.main
                     and f"limited {after}" in p["text"]]
        in_main = [p for p in sent_to(bot, bot.main) if f"limited {after}" in p["text"]]
        assert len(in_agents) + len(in_main) >= 1 and (len(in_agents) == 2 if waited else len(in_main) == 1)
        SID_STATE = state_dir() / "agents-topics"
        for f in SID_STATE.glob("*"):
            f.unlink()


def test_input_in_an_agents_topic_is_answered_once_and_never_starts_a_session():
    bot = FakeBot()
    set_setting(AGENTS_TOPIC, True)
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=7, target_language="en")
    topics = TopicMap(state_dir() / "topics.json", bot, 7)
    topics.ensure(SID, "Title")
    M.run_lifecycle(c, SID, "subagent-start", "worker", bot=bot, agent_id="a1")
    thread = agents_topic.json.loads(next((state_dir() / "agents-topics").glob("*.json")).read_text())["thread"]
    assert agents_topic.input_ignored(thread + 999) is None

    def write(text):
        m = {"message_id": 3, "date": 2_000_000_000, "text": text, "chat": {"id": 7, "type": "private"},
             "from": {"id": 7}, "message_thread_id": thread}
        return I.handle({"update_id": 1, "message": m}, c, bot, topics, I.Inbox(state_dir() / "inbox"), None, 0)

    bot.calls.clear()
    assert write("hello there") == "agents-topic" and write("again") == "agents-topic"
    sent = bot.methods("sendMessage")
    assert len(sent) == 1 and sent[0]["message_thread_id"] == thread
    assert sent[0]["text"] == i18n.t("agents_topic_no_input", lang="en")
    assert not any("inline_keyboard" in str(p.get("reply_markup", "")) for p in sent)
