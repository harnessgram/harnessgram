"""A session that ends closes its topic; a closed topic untouched for seven days is deleted."""
import json
import os
import time

from conftest import FakeBot
from ctb import inbox, sessions_menu as sm
from ctb.config import Config, state_dir, write_private
from ctb.mirror import run_lifecycle
from ctb.telegram import TelegramError
from test_session_manager import CHAT, DAY, add, cfg, env, tmap  # noqa: F401


def closes(bot):
    return [c["message_thread_id"] for c in bot.methods("closeForumTopic")]


def deletes(bot):
    return [c["message_thread_id"] for c in bot.methods("deleteForumTopic")]


def end(bot, sid):
    return run_lifecycle(cfg(), sid, "session-end", bot=bot)


def test_session_end_posts_notice_then_closes_and_records_the_time(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 21, "Dead", 3600)
    end(bot, "dead-sess-1")
    assert "שבעה ימים" in bot.methods("editMessageText")[-1]["text"]
    assert closes(bot) == [21]
    assert sm.closed_marks()["dead-sess-1"]["thread"] == 21
    assert abs(sm.closed_marks()["dead-sess-1"]["at"] - time.time()) < 5


def test_session_end_does_not_close_a_running_session(env):
    bot = FakeBot()
    add(env, "live-sess-1", 22, "Live", 5, live=True)
    end(bot, "live-sess-1")
    assert closes(bot) == []
    assert "live-sess-1" not in sm.closed_marks()


def test_session_end_does_not_close_a_session_being_moved(env):
    bot = FakeBot()
    add(env, "move-sess-1", 23, "Moving", 5)
    inbox.mark_planned_restart("move-sess-1")
    assert end(bot, "move-sess-1") == 0
    assert closes(bot) == []
    assert bot.methods("sendMessage") == []


def test_moving_marker_without_restart_marker_still_keeps_the_topic_open(env):
    bot = FakeBot()
    add(env, "move-sess-2", 24, "Moving", 5)
    (state_dir() / "moving").mkdir(parents=True, exist_ok=True)
    (state_dir() / "moving" / "move-sess-2").touch()
    end(bot, "move-sess-2")
    assert closes(bot) == []


def test_home_topic_is_never_closed(env):
    bot = FakeBot()
    add(env, "home-sess-1", 1, "General", 3600)
    end(bot, "home-sess-1")
    assert closes(bot) == []


def test_writing_in_a_closed_topic_reopens_it_and_resets_the_clock(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 21, "Dead", 3600)
    end(bot, "dead-sess-1")
    assert sm.reopen_for_message(cfg(), bot, "dead-sess-1") is True
    assert [c["message_thread_id"] for c in bot.methods("reopenForumTopic")] == [21]
    assert "dead-sess-1" not in sm.closed_marks()
    assert sm.auto_tidy(cfg(), bot, tmap(bot), now=time.time() + 30 * DAY) == 0
    assert deletes(bot) == []


def test_reopen_for_message_ignores_a_session_that_was_not_closed(env):
    bot = FakeBot()
    add(env, "open-sess-1", 25, "Open", 3600)
    assert sm.reopen_for_message(cfg(), bot, "open-sess-1") is False
    assert bot.methods("reopenForumTopic") == []


def test_queueing_an_owner_message_reopens_the_topic(env, monkeypatch):
    # The reopen thread must not outlive the test: it once ran `tmux new-session ... claude --resume` on the live
    # server after teardown had restored the real environment (2026-11-10).
    started = []
    monkeypatch.setattr(inbox, "_reopen_or_nudge", lambda box, session: started.append(session))
    bot = FakeBot()
    add(env, "dead-sess-1", 21, "Dead", 3600)
    end(bot, "dead-sess-1")
    box = inbox.Inbox(state_dir() / "inbox")
    inbox._queue_owner(cfg(), bot, box, {"message_id": 5, "date": 1, "message_thread_id": 21}, "dead-sess-1", "hi")
    assert "dead-sess-1" not in sm.closed_marks()
    assert len(bot.methods("reopenForumTopic")) == 1
    __import__("time").sleep(0.2)
    assert started == ["dead-sess-1"]  # the session would be reopened, by the (stubbed) background step


def test_seven_days_after_the_close_the_topic_is_deleted_and_forgotten(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 21, "Dead", 10 * DAY)
    end(bot, "dead-sess-1")
    now = time.time()
    assert sm.auto_tidy(cfg(), bot, tmap(bot), now=now + sm.AUTO_DELETE_AFTER + 60) == 1
    assert deletes(bot) == [21]
    assert "dead-sess-1" not in tmap(bot).all()
    assert sm.closed_marks() == {}


def test_nothing_is_deleted_before_seven_days(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 21, "Dead", 10 * DAY)
    end(bot, "dead-sess-1")
    assert sm.auto_tidy(cfg(), bot, tmap(bot), now=time.time() + sm.AUTO_DELETE_AFTER - 3600) == 0
    assert deletes(bot) == []
    assert "dead-sess-1" in sm.closed_marks()


def test_a_message_after_the_close_saves_the_topic(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 21, "Dead", 10 * DAY)
    end(bot, "dead-sess-1")
    path = env / "claude" / "projects" / "p" / "dead-sess-1.jsonl"
    later = time.time() + 2 * DAY
    os.utime(path, (later, later))
    assert sm.auto_tidy(cfg(), bot, tmap(bot), now=time.time() + 8 * DAY) == 0
    assert deletes(bot) == []


def test_a_running_session_is_never_deleted_and_its_topic_is_opened(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 21, "Dead", 10 * DAY)
    end(bot, "dead-sess-1")
    path = env / "claude" / "projects" / "p" / "dead-sess-1.jsonl"
    later = time.time() + 60
    os.utime(path, (later, later))
    (env / "claude" / "sessions" / "21.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "dead-sess-1"}))
    assert sm.auto_tidy(cfg(), bot, tmap(bot), now=time.time() + 8 * DAY) == 0
    assert deletes(bot) == []
    assert [c["message_thread_id"] for c in bot.methods("reopenForumTopic")] == [21]
    assert sm.closed_marks() == {}


def test_deletions_per_round_are_limited(env):
    bot = FakeBot()
    for i in range(5):
        add(env, f"dead-sess-{i}", 30 + i, f"Dead {i}", 10 * DAY)
        end(bot, f"dead-sess-{i}")
    now = time.time() + sm.AUTO_DELETE_AFTER + 60
    assert sm.auto_tidy(cfg(), bot, tmap(bot), now=now) == sm.DELETE_PER_ROUND
    assert sm.auto_tidy(cfg(), bot, tmap(bot), now=now) == 2


def test_flood_limit_stops_the_round(env):
    class Flooding(FakeBot):
        def call(self, method, params=None, **kw):
            if method == "deleteForumTopic":
                raise TelegramError("Too Many Requests", code=429)
            return super().call(method, params, **kw)

    bot = Flooding()
    for i in range(3):
        add(env, f"dead-sess-{i}", 30 + i, f"Dead {i}", 10 * DAY)
        end(bot, f"dead-sess-{i}")
    assert sm.auto_tidy(cfg(), bot, tmap(bot), now=time.time() + sm.AUTO_DELETE_AFTER + 60) == 0
    assert len(sm.closed_marks()) == 3


def _marks(**changes):
    data = sm.closed_marks()
    for session, ch in changes.items():
        data[session] = dict(data[session], **ch)
    write_private(state_dir() / sm.CLOSED_FILE, json.dumps(data))


def _later():
    return time.time() + sm.AUTO_DELETE_AFTER + 60


def test_manual_close_and_tidy_records_are_never_auto_deleted(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 21, "Dead", 10 * DAY)
    sm._close_topic(cfg(), bot, tmap(bot), sm.collect(tmap(bot))[0])
    assert sm.closed_marks()["dead-sess-1"]["auto"] is False
    assert sm.auto_tidy(cfg(), bot, tmap(bot), now=_later()) == 0
    assert deletes(bot) == []


def test_topic_reopened_service_message_clears_the_record(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 21, "Dead", 3600)
    end(bot, "dead-sess-1")
    sm.note_input(cfg(), bot, {"message": {"chat": {"id": CHAT}, "message_thread_id": 21, "forum_topic_reopened": {},
                                           "from": {"id": 999}}})
    assert sm.closed_marks() == {}
    assert bot.methods("reopenForumTopic") == []


def test_any_owner_input_clears_the_record_even_when_reopen_fails(env):
    class Refusing(FakeBot):
        def call(self, method, params=None, **kw):
            if method == "reopenForumTopic":
                raise TelegramError("Bad Request: not enough rights", code=400)
            return super().call(method, params, **kw)

    bot = Refusing()
    add(env, "dead-sess-1", 21, "Dead", 3600)
    end(bot, "dead-sess-1")
    sm.note_input(cfg(), bot, {"callback_query": {"from": {"id": 1}, "message": {"chat": {"id": CHAT}, "message_thread_id": 21}}})
    assert sm.closed_marks() == {}
    end2 = sm.auto_tidy(cfg(), bot, tmap(bot), now=_later())
    assert end2 == 0 and deletes(bot) == []


def test_input_from_someone_else_leaves_the_record(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 21, "Dead", 3600)
    end(bot, "dead-sess-1")
    sm.note_input(cfg(), bot, {"message": {"chat": {"id": CHAT}, "message_thread_id": 21, "from": {"id": 77}}})
    assert "dead-sess-1" in sm.closed_marks()


def test_edit_and_sticker_in_a_closed_topic_reopen_it(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 21, "Dead", 3600)
    end(bot, "dead-sess-1")
    sm.note_input(cfg(), bot, {"edited_message": {"chat": {"id": CHAT}, "message_thread_id": 21, "from": {"id": 1},
                                                  "sticker": {}}})
    assert sm.closed_marks() == {} and len(bot.methods("reopenForumTopic")) == 1


def test_attempts_not_successes_are_limited_and_a_failure_stops_the_round(env):
    class Failing(FakeBot):
        tries = 0

        def call(self, method, params=None, **kw):
            if method == "deleteForumTopic":
                self.tries += 1
                raise TelegramError("Bad Request: boom", code=400)
            return super().call(method, params, **kw)

    bot = Failing()
    for i in range(5):
        add(env, f"dead-sess-{i}", 30 + i, f"Dead {i}", 10 * DAY)
        end(bot, f"dead-sess-{i}")
    sm.auto_tidy(cfg(), bot, tmap(bot), now=_later())
    assert bot.tries == 1
    assert len(sm.closed_marks()) == 5


def test_a_gone_topic_does_not_stop_the_round(env):
    class Gone(FakeBot):
        def call(self, method, params=None, **kw):
            if method == "deleteForumTopic":
                self.methods_called = getattr(self, "methods_called", 0) + 1
                if self.methods_called == 1:
                    raise TelegramError("Bad Request: TOPIC_ID_INVALID", code=400)
            return super().call(method, params, **kw)

    bot = Gone()
    for i in range(2):
        add(env, f"dead-sess-{i}", 30 + i, f"Dead {i}", 10 * DAY)
        end(bot, f"dead-sess-{i}")
    assert sm.auto_tidy(cfg(), bot, tmap(bot), now=_later()) == 2
    assert sm.closed_marks() == {}


def test_record_changed_before_the_delete_is_not_deleted(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 21, "Dead", 10 * DAY)
    end(bot, "dead-sess-1")
    e = sm.collect(tmap(bot))[0]
    old_at = sm.closed_marks()["dead-sess-1"]["at"]
    _marks(**{"dead-sess-1": {"at": old_at + 1000}})  # reopened and closed again meanwhile
    assert sm._delete_topic(cfg(), bot, tmap(bot), e, at=old_at) == "changed"
    assert deletes(bot) == []


def test_bad_records_are_skipped_and_do_not_abort_the_round(env):
    bot = FakeBot()
    for i in range(4):
        add(env, f"dead-sess-{i}", 30 + i, f"Dead {i}", 10 * DAY)
        end(bot, f"dead-sess-{i}")
    _marks(**{"dead-sess-0": {"at": None}, "dead-sess-1": {"at": "x"}, "dead-sess-2": {"at": -5}})
    assert sm.auto_tidy(cfg(), bot, tmap(bot), now=_later()) == 1
    assert deletes(bot) == [33]


def test_records_of_another_chat_are_not_deleted(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 21, "Dead", 10 * DAY)
    end(bot, "dead-sess-1")
    _marks(**{"dead-sess-1": {"chat": CHAT + 1}})
    assert sm.auto_tidy(cfg(), bot, tmap(bot), now=_later()) == 0
    _marks(**{"dead-sess-1": {"chat": None}})
    assert sm.auto_tidy(cfg(), bot, tmap(bot), now=_later()) == 0


def test_sessions_in_the_revive_record_are_skipped(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 21, "Dead", 10 * DAY)
    end(bot, "dead-sess-1")
    assert sm.auto_tidy(cfg(), bot, tmap(bot), now=_later(), skip={"dead-sess-1"}) == 0


def test_heartbeat_waits_for_the_revive_to_finish(env, monkeypatch):
    from ctb import heartbeat, revive
    seen = []
    monkeypatch.setattr(sm, "auto_tidy", lambda *a, **k: seen.append(1) or 0)
    c = Config(bot_token="tok", owner_chat_id=1, chat_id=CHAT)
    monkeypatch.setattr(heartbeat, "_TIDIED", [0.0])
    revive.revived.clear()
    heartbeat.run_once(c, bot=FakeBot())
    assert seen == []
    revive.revived.set()
    try:
        revive.reviving.set()
        heartbeat.run_once(c, bot=FakeBot())
        assert seen == []
        revive.reviving.clear()
        heartbeat.run_once(c, bot=FakeBot())
        assert seen == [1]
    finally:
        revive.revived.clear()
        revive.reviving.clear()


def test_notice_is_edited_to_the_deletion_warning_after_the_close(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 21, "Dead", 3600)
    end(bot, "dead-sess-1")
    methods = [m for m, _ in bot.calls if m in ("sendMessage", "closeForumTopic", "editMessageText")]
    assert methods == ["sendMessage", "closeForumTopic", "editMessageText"]
    assert "שבעה ימים" not in bot.methods("sendMessage")[-1]["text"]
    assert "שבעה ימים" in bot.methods("editMessageText")[-1]["text"]
    assert "dead-sess-1" in sm.closed_marks()


def test_failed_close_keeps_the_plain_notice(env):
    bot = FakeBot()
    bot.fail["closeForumTopic"] = "Bad Request: boom"
    add(env, "dead-sess-1", 21, "Dead", 3600)
    end(bot, "dead-sess-1")
    assert bot.methods("editMessageText") == [] and sm.closed_marks() == {}


def test_failed_edit_reopens_the_topic_and_drops_the_record(env):
    bot = FakeBot()
    bot.fail["editMessageText"] = "Bad Request: boom"
    add(env, "dead-sess-1", 21, "Dead", 3600)
    end(bot, "dead-sess-1")
    assert len(bot.methods("reopenForumTopic")) == 1 and sm.closed_marks() == {}


def test_rejected_notice_means_no_close(env):
    bot = FakeBot()
    bot.fail["sendMessage"] = "Bad Request: TOPIC_CLOSED"
    add(env, "dead-sess-1", 21, "Dead", 3600)
    end(bot, "dead-sess-1")
    assert closes(bot) == [] and sm.closed_marks() == {}


def test_mode_switch_resets_the_close_records(env):
    from ctb import cli
    bot = FakeBot()
    add(env, "dead-sess-1", 21, "Dead", 3600)
    end(bot, "dead-sess-1")
    cli._reset_topics_for_mode_switch()
    assert sm.closed_marks() == {}
