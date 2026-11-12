"""The live agents card: one edited message per batch of background agents, instead of one notice per agent."""
from __future__ import annotations

import json

from conftest import GOOD_TOKEN, FakeBot
from ctb import agents_card as AC, hooks as H, inbox as I, mirror as M
from ctb.agents_topic import AGENTS_TOPIC
from ctb.config import Config, set_setting, state_dir
from ctb.telegram import TelegramError
from ctb.topics import TopicMap

CHAT = -1001234567
SID = "sess-card-1"
T0 = 1_000_000.0


def cfg(**kw):
    kw.setdefault("target_language", "en")
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, chat_id=CHAT, **kw)


class Bot(FakeBot):
    """Sends return a fresh message id; a failure carries an error code."""

    def __init__(self):
        super().__init__()
        self.mid = 500

    def call(self, method, params=None, timeout=15):
        if method in self.fail:
            self.calls.append((method, params or {}))
            raise TelegramError(self.fail.pop(method), 400)
        if method == "sendMessage":
            self.mid += 1
            self.calls.append((method, dict(params or {})))
            return {"message_id": self.mid}
        return super().call(method, params, timeout)


def thread_of(bot):
    return TopicMap(state_dir() / "topics.json", bot, CHAT).ensure(SID, "Build the parser")


def start(bot, aid, at, name="worker", c=None, sleep=lambda s: None):
    AC.agent_started(c or cfg(), SID, 11, aid, name, bot, now=at, sleep=sleep)


def finish(bot, aid, at, name="audit", summary="all good", full="the whole report", failed=False, model="Opus 5, high",
           c=None, sleep=lambda s: None):
    AC.agent_finished(c or cfg(), SID, 11, aid, {"name": name, "model": model, "summary": summary, "full": full,
                                                  "failed": failed}, bot, now=at, sleep=sleep)


def test_first_agent_creates_a_silent_card():
    bot = Bot()
    start(bot, "a1", T0)
    sent = bot.methods("sendMessage")
    assert len(sent) == 1 and sent[0]["disable_notification"] is True and sent[0]["message_thread_id"] == 11
    assert sent[0]["text"].startswith("🤖 Agents\n⏳ worker")
    assert "reply_markup" not in sent[0]


def test_update_edits_in_place_with_result_model_and_button():
    bot = Bot()
    start(bot, "a1", T0)
    start(bot, "a2", T0 + 1)  # throttled: coalesced, flushed after the pause
    finish(bot, "a1", T0 + 70)
    edits = bot.methods("editMessageText")
    assert edits and all(e["message_id"] == 501 for e in edits)
    text = edits[-1]["text"]
    assert "1. ✅ worker · Opus 5, high · 1m 10s\n↳ audit\n💬 all good" in text and "⏳ worker" in text
    assert edits[-1]["reply_markup"]["inline_keyboard"][0][0] == {"text": "📄 Full result 1", "callback_data": "agr:1:0"}  # numbered like its line
    assert len(bot.methods("sendMessage")) == 1  # still one card


def test_collapse_to_a_summary_line_keeps_buttons_and_may_notify():
    bot = Bot()
    start(bot, "a1", T0)
    start(bot, "a2", T0 + 10)
    finish(bot, "a1", T0 + 20)
    finish(bot, "a2", T0 + 40, name="lint", failed=True, full="Error: boom")
    sent = bot.methods("sendMessage")
    assert len(sent) == 2 and "disable_notification" not in sent[1]  # the collapsed card is the one that may notify
    assert sent[1]["text"] == ("🤖 2 agents finished · 1 ✅ 1 ❌\n1. ✅ audit · Opus 5, high · 20s\n"
                               "2. ❌ lint · Opus 5, high · 30s")
    assert [b["callback_data"] for b in sent[1]["reply_markup"]["inline_keyboard"][0]] == ["agr:1:0", "agr:1:1"]
    assert [b["text"] for b in sent[1]["reply_markup"]["inline_keyboard"][0]] == ["📄 Full result 1", "📄 Full result 2"]
    assert any(p["message_id"] == 501 for p in bot.methods("deleteMessage"))  # the quiet card is replaced


def test_new_agent_after_a_collapse_starts_a_fresh_card():
    bot = Bot()
    start(bot, "a1", T0)
    finish(bot, "a1", T0 + 10)
    start(bot, "a2", T0 + 20)
    texts = [p["text"] for p in bot.methods("sendMessage")]
    assert texts[1] == "🤖 1 agent finished · 1 ✅\n✅ audit · Opus 5, high · 10s" and texts[2].startswith("🤖 Agents\n⏳ worker")
    assert bot.methods("sendMessage")[2]["disable_notification"] is True
    st = json.loads(AC._path(SID).read_text())
    assert st["current"] == 2 and "1" in st["cards"]


def test_old_card_or_card_not_at_the_bottom_starts_a_new_one_and_keeps_running_agents():
    bot = Bot()
    start(bot, "a1", T0)
    start(bot, "a2", T0 + AC.CARD_MAX_AGE + 5)  # the first card is too old
    st = json.loads(AC._path(SID).read_text())
    assert st["current"] == 2
    assert [a["id"] for a in st["cards"]["2"]["agents"]] == ["a1", "a2"]  # a1 is still running: it follows
    assert "continued" in bot.methods("editMessageText")[-1]["text"]  # the old card points to the new one
    (state_dir() / "latest-replies.json").write_text(json.dumps({f"{CHAT}:11": 9999}))  # a reply below the card
    start(bot, "a3", T0 + AC.CARD_MAX_AGE + 20)
    assert json.loads(AC._path(SID).read_text())["current"] == 3


def test_edits_are_throttled_and_coalesced():
    bot, slept = Bot(), []

    def pause(seconds):  # while the first process waits, a second and a third update arrive
        slept.append(seconds)
        start(bot, "a3", T0 + 1.0, sleep=pause)
        start(bot, "a4", T0 + 1.5, sleep=pause)

    start(bot, "a1", T0)
    start(bot, "a2", T0 + 0.5, sleep=pause)
    edits = bot.methods("editMessageText")
    assert len(edits) == 1  # one flush for all quick updates
    assert len(slept) == 1 and 2.4 < slept[0] <= AC.THROTTLE
    assert edits[0]["text"].count("⏳") == 4  # the flush shows the latest state


def test_not_modified_is_ignored_and_a_deleted_card_is_posted_again():
    bot = Bot()
    start(bot, "a1", T0)
    bot.fail["editMessageText"] = "Bad Request: message is not modified"
    finish(bot, "a1", T0 + 5, summary="")
    bot2 = Bot()
    bot2.mid = 700
    start(bot2, "b1", T0 + 100, name="other")  # a new card (the first one collapsed)
    bot2.fail["editMessageText"] = "Bad Request: message to edit not found"
    start(bot2, "b2", T0 + 110)
    texts = [p["text"] for p in bot2.methods("sendMessage")]
    assert len(texts) == 2 and texts[1].count("⏳") == 2
    st = json.loads(AC._path(SID).read_text())
    assert st["cards"][str(st["current"])]["message_id"] == 702


def test_flood_skip_keeps_the_card_dirty_and_the_next_update_catches_up(monkeypatch):
    bot = Bot()
    start(bot, "a1", T0)
    real = AC.flood.allow_optional
    monkeypatch.setattr(AC.flood, "allow_optional", lambda *a, **k: False)
    start(bot, "a2", T0 + 10)
    assert bot.methods("editMessageText") == []
    monkeypatch.setattr(AC.flood, "allow_optional", real)
    finish(bot, "a1", T0 + 20)
    assert bot.methods("editMessageText")[-1]["text"].count("⏳") == 1


def test_full_result_button_is_owner_only_and_sends_the_text_on_tap():
    bot = Bot()
    c = cfg()
    start(bot, "a1", T0)
    finish(bot, "a1", T0 + 5, full="line one\nline two")
    button = bot.methods("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert len(button.encode()) <= 64
    topics = TopicMap(state_dir() / "topics.json", bot, CHAT)
    msg = {"chat": {"id": CHAT, "type": "supergroup"}, "message_thread_id": 11, "message_id": 502}
    topics.ensure(SID, "x")
    # a stranger: nothing is sent
    stranger = {"id": "q1", "data": button, "from": {"id": 99}, "message": msg}
    before = len(bot.methods("sendMessage"))
    assert I.handle_button(stranger, c, bot, topics, None) == "ignored"
    assert len(bot.methods("sendMessage")) == before
    # the owner, in the session's own topic
    owner = {"id": "q2", "data": button, "from": {"id": 7}, "message": dict(msg, message_thread_id=topics.thread_of(SID))}
    assert I.handle_button(owner, c, bot, topics, None) == "agent-result:sent"
    reply = bot.methods("sendMessage")[-1]
    assert reply["text"] == "line one\nline two" and reply["reply_to_message_id"] == 502
    assert reply["disable_notification"] is True


def test_long_text_is_bounded_and_redacted():
    bot = Bot()
    secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0" * 3
    start(bot, "a1", T0)
    finish(bot, "a1", T0 + 5, summary="key " + secret + " " + "x" * 500, full=("y" * 9000) + " " + secret)
    st = json.loads(AC._path(SID).read_text())
    agent = st["cards"]["1"]["agents"][0]
    assert len(agent["full"]) <= AC.FULL_LIMIT and len(agent["summary"]) <= AC.SUMMARY_LIMIT
    assert secret not in json.dumps(st) and secret not in json.dumps(bot.calls)
    full = "z" * 9000
    finish(bot, "a1", T0 + 6, full=full)  # a repeated end just replaces the result
    c = cfg()
    q = {"id": "q", "data": "agr:1:0", "message": {"message_thread_id": 11, "message_id": 501}}
    AC.press(c, bot, q, SID)
    sent = [p["text"] for p in bot.methods("sendMessage")[-2:]]
    assert sum(len(x) for x in sent) == AC.FULL_LIMIT and all(len(x) <= 4096 for x in sent)


def test_hebrew_card_keeps_english_names_isolated():
    bot = Bot()
    c = cfg(target_language="he")
    start(bot, "a1", T0, name="worker", c=c)
    finish(bot, "a1", T0 + 5, name="audit the whole parser module now", c=c, model="Opus 5, high")
    text = bot.methods("sendMessage")[-1]["text"]
    assert "סוכן אחד סיים" in text and text.startswith("‏")
    bot2 = Bot()
    start(bot2, "b1", T0, name="worker", c=c)
    first = bot2.methods("sendMessage")[0]["text"]
    assert "⁨worker⁩" in first
    long = AC._agent_lines({"name": "audit the whole parser module", "status": "done", "started": 0, "ended": 5,
                            "model": "Opus 5"}, 5, True)
    assert long.split("\n")[0].strip("‏") == "audit the whole parser module"  # longer English: its own line


def test_lifecycle_routes_to_the_card_by_default_and_to_the_topic_only_when_explicit(monkeypatch):
    bot = Bot()
    thread = thread_of(bot)
    M.run_lifecycle(cfg(), SID, "subagent-start", "worker", bot=bot, agent_id="a1")
    M.run_lifecycle(cfg(), SID, "subagent-stop", "worker", "audit", bot=bot, agent_id="a1")
    sent = bot.methods("sendMessage")
    assert sent and all(p["message_thread_id"] == thread for p in sent)
    assert "Background agent finished" not in " ".join(p["text"] for p in sent)  # no per-agent notice line
    set_setting(AGENTS_TOPIC, True)
    bot2 = Bot()
    thread_of(bot2)
    M.run_lifecycle(cfg(), SID, "subagent-start", "worker", bot=bot2, agent_id="b1")
    assert any(m == "createForumTopic" and p.get("name", "").endswith("agents") for m, p in bot2.calls)


def test_hooks_always_spawn_start_and_stop(monkeypatch):
    spawned = []
    monkeypatch.setattr(H, "load_config", lambda: cfg())
    monkeypatch.setattr(H, "spawn", lambda args: spawned.append(args))
    for event in ("SubagentStart", "SubagentStop", "SubagentStop"):
        H.hook_lifecycle(json.dumps({"session_id": SID, "hook_event_name": event, "agent_id": "a1"}))
    assert [a[2] for a in spawned] == ["--kind=subagent-start", "--kind=subagent-stop", "--kind=subagent-stop"]


class FlakyBot(Bot):
    """Fails every call of the given methods (code 400) until `heal()`."""

    def __init__(self, *methods, text="Bad Request: boom"):
        super().__init__()
        self.broken, self.text = set(methods), text

    def heal(self):
        self.broken.clear()

    def call(self, method, params=None, timeout=15):
        if method in self.broken:
            self.calls.append((method, dict(params or {})))
            raise TelegramError(self.text, 400)
        return super().call(method, params, timeout)


def test_last_flush_is_retried_with_backoff_before_the_process_exits():
    bot, slept = Bot(), []
    start(bot, "a1", T0)
    bot.fail["sendMessage"] = "Bad Request: boom"  # the collapse post and its in-place fallback fail once
    bot.fail["editMessageText"] = "Bad Request: boom"
    finish(bot, "a1", T0 + 10, sleep=slept.append)
    assert slept == [AC.RETRY_DELAYS[0]]
    assert bot.methods("sendMessage")[-1]["text"].startswith("🤖 1 agent finished") or "1 ✅" in bot.methods("sendMessage")[-1]["text"]
    assert not json.loads(AC._path(SID).read_text())["cards"]["1"].get("dirty")


def test_a_failed_collapse_is_left_marked_and_the_heartbeat_delivers_it():
    bot = FlakyBot("sendMessage", "editMessageText")
    slept = []
    start(bot, "a1", T0)
    finish(bot, "a1", T0 + 10, sleep=slept.append)
    assert slept == list(AC.RETRY_DELAYS)  # a few tries, then it gives up
    assert json.loads(AC._path(SID).read_text())["cards"]["1"]["dirty"] is True
    bot.heal()
    AC.retry_dirty(cfg(), bot, now=T0 + 60)
    assert "1 ✅" in bot.methods("sendMessage")[-1]["text"]
    assert not json.loads(AC._path(SID).read_text())["cards"]["1"]["dirty"]


def test_parallel_starts_make_exactly_one_card_even_when_the_first_post_failed():
    bot = FlakyBot("sendMessage")
    start(bot, "a1", T0)  # every try fails: the card has no message yet
    bot.heal()
    start(bot, "a2", T0 + 0.5)  # inside the throttle window: it waits and coalesces
    start(bot, "a3", T0 + 0.6)
    assert len(json.loads(AC._path(SID).read_text())["cards"]) == 1
    assert bot.mid == 501  # one message was ever created
    assert bot.methods("editMessageText")[-1]["text"].count("⏳") == 3


def test_thread_not_found_deactivates_the_card_and_is_not_recreated():
    bot = Bot()
    start(bot, "a1", T0)
    bot.fail["editMessageText"] = "Bad Request: message thread not found"
    start(bot, "a2", T0 + 10)
    st = json.loads(AC._path(SID).read_text())
    assert st["cards"]["1"]["inactive"] is True and st["dead_thread"] == 11
    sends = len(bot.methods("sendMessage"))
    start(bot, "a3", T0 + 20)
    finish(bot, "a1", T0 + 30)
    assert len(bot.methods("sendMessage")) == sends and len(bot.methods("editMessageText")) == 1


def test_the_card_keeps_using_the_chat_it_was_created_in():
    bot = Bot()
    start(bot, "a1", T0)
    other = Config(bot_token=GOOD_TOKEN, owner_chat_id=7, chat_id=-100999, target_language="en")
    AC.agent_started(other, SID, 11, "a2", "worker", bot, now=T0 + 10, chat=CHAT)
    AC.agent_finished(other, SID, 11, "a1", {"name": "x", "full": "r"}, bot, now=T0 + 20, chat=CHAT)
    AC.agent_finished(other, SID, 11, "a2", {"name": "y", "full": "r"}, bot, now=T0 + 30, chat=CHAT)
    calls = [p for m, p in bot.calls if m in ("sendMessage", "editMessageText", "deleteMessage")]
    assert {p["chat_id"] for p in calls} == {CHAT}
    q = {"id": "q", "data": "agr:1:0", "message": {"chat": {"id": -100555}, "message_thread_id": 11, "message_id": 7}}
    AC.press(other, bot, q, SID)
    assert bot.methods("sendMessage")[-1]["chat_id"] == -100555


def test_a_project_group_session_gets_its_card_in_that_group_only():
    bot = Bot()
    AC.agent_started(cfg(), SID, 11, "a1", "worker", bot, now=T0, chat=-100777)
    AC.agent_finished(cfg(), SID, 11, "a1", {"name": "x", "full": "r"}, bot, now=T0 + 10, chat=-100777)
    calls = [p for m, p in bot.calls if m in ("sendMessage", "editMessageText", "deleteMessage")]
    assert calls and {p["chat_id"] for p in calls} == {-100777}


def test_session_end_delivers_the_final_collapsed_card_for_agents_still_running():
    bot = Bot()
    start(bot, "a1", T0)
    start(bot, "a2", T0 + 5)
    AC.session_ended(SID, cfg(), bot, now=T0 + 60)
    last = bot.methods("sendMessage")[-1]  # the collapsed card is re-sent, so it may notify
    assert last["text"] == "🤖 2 agents finished · 0 ✅ 2 ❌\n1. ❌ worker · 1m 00s\n2. ❌ worker · 55s" and "⏳" not in last["text"]
    assert json.loads(AC._path(SID).read_text())["current"] is None


def test_session_end_without_a_working_bot_leaves_the_card_for_the_heartbeat():
    bot = FlakyBot("sendMessage", "editMessageText")
    start(bot, "a1", T0)
    bot.heal()
    bot.broken = {"sendMessage", "editMessageText"}
    AC.session_ended(SID, cfg(), bot, now=T0 + 60)
    assert json.loads(AC._path(SID).read_text())["cards"]["1"]["dirty"] is True
    bot.heal()
    AC.retry_dirty(cfg(), bot, now=T0 + 120)
    assert "0 ✅ 1 ❌" in bot.methods("sendMessage")[-1]["text"] or "0 ✅ 1 ❌" in bot.methods("editMessageText")[-1]["text"]
