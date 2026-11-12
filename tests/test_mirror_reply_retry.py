"""Replies survive Telegram failures without racing the Stop dedupe ledger."""
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import cli, heartbeat, hooks, mirror, policy, progress, reply_outbox, topics
from ctb.config import Config, state_dir
from ctb.telegram import TelegramError
from test_hooks import ev, stop_data


class FlakyBot(FakeBot):
    def __init__(self, errors=()):
        super().__init__()
        self.errors = list(errors)
        self.attempts = []
        self.delivered = []

    def call(self, method, params=None, **kwargs):
        if method == "sendMessage":
            self.attempts.append(dict(params))
            if self.errors:
                error = self.errors.pop(0)
                if error is not None:
                    raise error
            self.delivered.append(dict(params))
        return super().call(method, params, **kwargs)


def error(code, retry_after=None):
    return TelegramError("test failure", code, retry_after)


@pytest.fixture
def rig(monkeypatch):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=False,
                 target_language="en", ui_language="en", context_footer=False,
                 quiet_background_turns=False, codex_harness_enabled=False)
    bot = FlakyBot()
    monkeypatch.setattr(mirror.Bot, "from_config", classmethod(lambda cls, cfg, **kw: bot))
    monkeypatch.setattr(mirror, "refresh_card", lambda *a, **kw: False)
    monkeypatch.setattr(mirror, "limit_offer", lambda *a, **kw: False)
    monkeypatch.setattr(mirror.usage, "compact_footer", lambda *a: "")
    monkeypatch.setattr(mirror.usage, "footer", lambda *a: "")
    monkeypatch.setattr(heartbeat, "refresh_cards", lambda *a, **kw: 0)
    monkeypatch.setattr(heartbeat.agents_card, "retry_dirty", lambda *a, **kw: 0)
    sender = mirror.build(cfg, bot=bot)
    sender.topics.ensure("sess-1", "Test")
    return cfg, bot, sender


def queue():
    with reply_outbox.locked() as path:
        return reply_outbox.load(path)


@pytest.mark.parametrize("code,waits", [(429, [2, 2]), (None, [1, 3]), (500, [1, 3]), (503, [1, 3])])
def test_two_failures_then_success(rig, monkeypatch, code, waits):
    cfg, bot, sender = rig
    bot.errors = [error(code, 2), error(code, 2)]
    slept = []
    monkeypatch.setattr(mirror.time, "sleep", slept.append)
    assert sender._post(cfg.owner_chat_id, sender.topics, "sess-1", "Test", "answer")
    assert len(bot.attempts) == 3
    assert [p["text"] for p in bot.delivered] == ["answer"]
    assert slept == waits
    assert queue() == []


def test_three_flood_waits_are_capped_and_follow_shared_gate(rig, monkeypatch):
    cfg, bot, sender = rig
    clock = [100.0]
    monkeypatch.setattr(mirror.time, "time", lambda: clock[0])
    slept = []
    def sleep(delay):
        slept.append(delay)
        clock[0] += delay
    monkeypatch.setattr(mirror.time, "sleep", sleep)
    from ctb import flood
    real_call = bot.call
    def call(method, params=None, **kw):
        if method == "sendMessage":
            flood.record_429(123, 65)
            raise error(429, 2)
        return real_call(method, params, **kw)
    bot.call = call
    assert not sender._post(123, sender.topics, "sess-1", "Test", "answer")
    assert slept == [30, 30, 5]
    assert len(queue()) == 1


@pytest.mark.parametrize("code", [401, 403])
def test_auth_errors_are_not_retried_but_retained(rig, code):
    cfg, bot, sender = rig
    bot.errors = [error(code)]
    assert not sender._post(123, sender.topics, "sess-1", "Test", "answer")
    assert len(bot.attempts) == 1
    assert len(queue()) == 1
    assert sender.dropped == 0


def test_parse_fallback_has_transient_retries_too(rig, monkeypatch):
    cfg, bot, sender = rig
    bot.errors = [TelegramError("can't parse entities", 400), error(None), error(502)]
    monkeypatch.setattr(mirror.time, "sleep", lambda _: None)
    assert sender._post(123, sender.topics, "sess-1", "Test", "<b>answer</b>")
    assert bot.delivered[0]["text"] == "answer"
    assert "parse_mode" not in bot.delivered[0]


def test_deleted_thread_is_recreated(rig):
    cfg, bot, sender = rig
    old = sender.topics.place_of("sess-1").thread
    bot.errors = [TelegramError("Bad Request: message thread not found", 400)]
    assert sender._post(123, sender.topics, "sess-1", "Test", "answer")
    assert bot.delivered[0]["message_thread_id"] != old


@pytest.mark.parametrize("language,marker", [("en", "(delayed)"), ("he", "(באיחור)")])
def test_failure_is_private_and_later_heartbeat_delivers(rig, monkeypatch, language, marker):
    cfg, bot, sender = rig
    cfg.target_language = language
    bot.errors = [error(None)] * 3
    monkeypatch.setattr(mirror.time, "sleep", lambda _: None)
    assert mirror.run_reply(cfg, "Test", "sess-1", "private answer", False,
                            answer_to={"chat": 123, "id": 42}) == 0
    entries = queue()
    assert len(entries) == 1 and "private answer" in entries[0]["text"]
    assert entries[0]["answer_to"] == {"chat": 123, "id": 42}
    assert entries[0]["thread"] == sender.topics.place_of("sess-1").thread
    path = state_dir() / reply_outbox.QUEUE_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "private answer" not in (state_dir() / "mirror.log").read_text()
    heartbeat.run_once(cfg, bot, find=lambda _: None)
    assert queue() == []
    assert len(bot.delivered) == 1
    sent = bot.delivered[0]
    assert marker + "\n" in sent["text"] and "private answer" in sent["text"]
    assert sent["reply_parameters"]["message_id"] == 42
    heartbeat.run_once(cfg, bot, find=lambda _: None)
    assert len(bot.delivered) == 1


def test_partial_reply_preserves_only_unsent_parts_in_order(rig, monkeypatch):
    cfg, bot, sender = rig
    bot.errors = [None, error(None), error(None), error(None)]
    monkeypatch.setattr(mirror.time, "sleep", lambda _: None)
    for text in ("first", "second", "third"):
        sender._post(123, sender.topics, "sess-1", "Test", text)
    assert [e["text"] for e in queue()] == ["second", "third"]
    heartbeat.run_once(cfg, bot, find=lambda _: None)
    assert [p["text"] for p in bot.delivered] == ["first", "(delayed)\nsecond", "(delayed)\nthird"]


def test_failed_alert_uses_same_queue(rig):
    cfg, bot, sender = rig
    bot.errors = [error(429, 30)]
    mirror.alert(cfg, "sess-1", "Reply delivery failed", bot=bot)
    assert len(queue()) == 1
    assert queue()[0]["options"]["record_reply"] is False
    heartbeat.run_once(cfg, bot, find=lambda _: None)
    assert queue() == []
    assert "(delayed)\n" in bot.delivered[0]["text"]
    assert "⚠️" in bot.delivered[0]["text"]


def test_queue_pressure_and_age_never_remove_pending_parts(rig):
    cfg, bot, sender = rig
    now = time.time()
    with reply_outbox.locked() as path:
        entries = []
        for i in range(reply_outbox.KEEP + 3):
            reply_outbox.append(path, entries, {"created": now, "text": f"private-{i}", "session": "s", "title": "t", "chat": 1, "thread": None, "options": {}})
        assert len(entries) == reply_outbox.KEEP + 3
        assert entries[0]["text"] == "private-0"
        entries[0]["created"] = now - 30 * 24 * 60 * 60
        reply_outbox.save(path, entries)
        fresh = reply_outbox.load(path, now)
        assert fresh == entries
    assert reply_outbox.drain(cfg, bot, now) == reply_outbox.KEEP + 3
    assert bot.delivered[0]["text"] == "(delayed)\nprivate-0"


def test_mirror_off_holds_outbox_unchanged(rig, monkeypatch):
    cfg, bot, sender = rig
    bot.errors = [error(401)]
    sender._post(123, sender.topics, "sess-1", "Test", "answer")
    path = state_dir() / reply_outbox.QUEUE_FILE
    saved = path.read_bytes()
    monkeypatch.setattr(policy, "on", lambda: False)
    heartbeat.run_once(cfg, bot)
    assert path.read_bytes() == saved
    assert bot.delivered == []
    monkeypatch.setattr(policy, "on", lambda: True)
    heartbeat.run_once(cfg, bot, find=lambda _: None)
    assert len(bot.delivered) == 1 and queue() == []


def test_closed_gate_holds_reply_and_alert(rig):
    from ctb import flood
    cfg, bot, sender = rig
    flood.record_429(123, 90)
    sender._post(123, sender.topics, "sess-1", "Test", "answer")
    mirror.alert(cfg, "sess-1", "Reply delivery failed", bot=bot)
    heartbeat.run_once(cfg, bot)
    assert bot.attempts == []
    assert len(queue()) == 2


@pytest.mark.parametrize("failures", [2, 3])
def test_raw_network_failures_retry_then_deliver_once(rig, monkeypatch, failures):
    cfg, bot, sender = rig
    bot.errors = [OSError("private network diagnostics")] * failures
    slept = []
    monkeypatch.setattr(mirror.time, "sleep", slept.append)
    assert sender._post(123, sender.topics, "sess-1", "Test", "answer") is (failures == 2)
    assert slept == [1.0, 3.0] and len(bot.attempts) == 3
    assert sender.dropped == 0
    assert len(queue()) == (failures == 3)
    heartbeat.run_once(cfg, bot, find=lambda _: None)
    assert len(bot.delivered) == 1 and "answer" in bot.delivered[0]["text"]
    heartbeat.run_once(cfg, bot, find=lambda _: None)
    assert len(bot.delivered) == 1 and queue() == []
    log_path = state_dir() / "mirror.log"
    assert not log_path.exists() or "private network diagnostics" not in log_path.read_text()


def test_direct_retry_path_cannot_bypass_a_preexisting_gate(rig):
    from ctb import flood
    cfg, bot, sender = rig
    flood.record_429(123, 90)
    assert not sender._post_now(123, sender.topics, "new-session", "Test", "answer")
    assert sender.topics.place_of("new-session") is None
    assert bot.attempts == []


def test_gate_closing_during_topic_lookup_retains_reply(rig, monkeypatch):
    from ctb import flood
    cfg, bot, sender = rig
    ensure = sender.topics.ensure
    def close_gate(*args, **kwargs):
        thread = ensure(*args, **kwargs)
        flood.record_429(123, 90)
        return thread
    monkeypatch.setattr(sender.topics, "ensure", close_gate)
    assert not sender._post(123, sender.topics, "sess-1", "Test", "answer")
    assert bot.attempts == [] and len(queue()) == 1 and sender.dropped == 0


def test_topic_lookup_network_failure_retains_reply(rig, monkeypatch):
    cfg, bot, sender = rig
    def fail(*args, **kwargs):
        raise OSError("private network diagnostics")
    monkeypatch.setattr(sender.topics, "ensure", fail)
    assert not sender._post(123, sender.topics, "sess-1", "Test", "answer")
    assert bot.attempts == [] and len(queue()) == 1 and sender.dropped == 0


def test_new_project_topic_respects_its_own_gate(rig, monkeypatch):
    from ctb import flood
    from ctb.config import ProjectConfig
    cfg, bot, _ = rig
    cfg.projects = [ProjectConfig(name="Project", chat_id=-1001234, title_prefixes=["Project"])]
    monkeypatch.setattr("ctb.status.session_cwd", lambda _: None)
    sender = mirror.build(cfg, bot=bot)
    bot.calls.clear()
    flood.record_429(-1001234, 90)
    assert not sender._post(123, sender.topics, "new-project-session", "Project: task", "answer")
    assert bot.calls == [] and len(queue()) == 1
    assert queue()[0]["chat"] == -1001234 and queue()[0]["thread"] is None
    assert reply_outbox.drain(cfg, bot) == 0 and bot.calls == []
    (state_dir() / flood.STATE_FILE).unlink()
    assert reply_outbox.drain(cfg, bot) == 1
    assert bot.delivered[0]["chat_id"] == -1001234
    assert reply_outbox.drain(cfg, bot) == 0 and len(bot.delivered) == 1


def prepare_stop(cfg, monkeypatch):
    monkeypatch.setattr(hooks, "load_config", lambda: cfg)
    monkeypatch.setattr(progress, "run_send_progress", lambda *a, **kw: 0)
    monkeypatch.setattr(topics, "set_state", lambda *a, **kw: 0)
    monkeypatch.setattr(hooks, "drop_progress", lambda *a, **kw: None)
    monkeypatch.setattr(hooks, "live_card", lambda *a, **kw: None)
    monkeypatch.setattr("ctb.compact.auto_compact_after_reply", lambda *a, **kw: None)


def test_stop_dedupe_and_two_heartbeats_racing_deliver_once(rig, tmp_path, monkeypatch):
    cfg, bot, sender = rig
    prepare_stop(cfg, monkeypatch)
    data = stop_data(tmp_path, [ev("user", "q"), ev("assistant", "answer", "answer-id")])
    bot.errors = [error(401)]
    queued, release = threading.Event(), threading.Event()
    mark = hooks._mark
    def delayed_mark(session, keys):
        queued.set()  # the outbox is saved; the Stop worker still holds its session lock
        assert release.wait(5)
        mark(session, keys)
    monkeypatch.setattr(hooks, "_mark", delayed_mark)
    with ThreadPoolExecutor(max_workers=4) as pool:
        first_stop = pool.submit(hooks._finish_stop, data)
        try:
            assert queued.wait(5)
            assert hooks._seen("sess-1") == set()
            second_stop = pool.submit(hooks._finish_stop, data)
            drains = [pool.submit(reply_outbox.drain, cfg, bot) for _ in range(2)]
            assert sorted(job.result(timeout=5) for job in drains) == [0, 1]
        finally:
            release.set()
        assert first_stop.result(timeout=5) == second_stop.result(timeout=5) == 0
    assert len(bot.delivered) == 1
    assert queue() == []
    assert hooks._seen("sess-1") == {"answer-id:0"}


def test_failed_queue_write_does_not_mark_stop_seen(rig, tmp_path, monkeypatch):
    cfg, bot, sender = rig
    prepare_stop(cfg, monkeypatch)
    data = stop_data(tmp_path, [ev("user", "q"), ev("assistant", "answer", "answer-id")])
    bot.errors = [error(401), error(401)]
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(reply_outbox, "save", fail)
    hooks._finish_stop(data)
    assert hooks._seen("sess-1") == set()


@pytest.mark.parametrize("mode", ["off", "edit", "edit-off"])
def test_queued_translation_does_not_queue_a_second_fallback(rig, monkeypatch, mode):
    cfg, bot, sender = rig
    cfg.original = mode
    cfg.target_language = "he"
    bot.errors = [error(401)]
    monkeypatch.setattr(mirror, "translate_reply", lambda text, fn, lang: fn(text))
    original = "This is the final answer with enough English letters to require translation."
    translated = "זוהי התשובה הסופית בעברית עם כל המידע שצריך לקבל בסיום העבודה."
    sender.send_reply("Test", "sess-1", original, lambda _: translated)
    entries = queue()
    assert len(entries) == 1
    assert (translated if mode == "off" else original) in mirror._plain(entries[0]["text"])
    heartbeat.run_once(cfg, bot, find=lambda _: None)
    assert len(bot.delivered) == 1 and queue() == []


def test_third_flood_wait_can_still_succeed(rig, monkeypatch):
    cfg, bot, sender = rig
    bot.errors = [error(429, 2)] * 3
    waits = []
    monkeypatch.setattr(mirror.time, "sleep", waits.append)
    assert sender._post(123, sender.topics, "sess-1", "Test", "answer")
    assert waits == [2, 2, 2] and len(bot.attempts) == 4
    assert len(bot.delivered) == 1 and queue() == []


def test_failed_heartbeat_keeps_entry_until_a_later_round(rig, monkeypatch):
    cfg, bot, sender = rig
    bot.errors = [error(None)] * 6
    monkeypatch.setattr(mirror.time, "sleep", lambda _: None)
    sender._post(123, sender.topics, "sess-1", "Test", "answer")
    saved = queue()
    heartbeat.run_once(cfg, bot, find=lambda _: None)
    assert queue() == [dict(saved[0], failures=1)] and bot.delivered == []
    heartbeat.run_once(cfg, bot, find=lambda _: None)
    assert queue() == [] and len(bot.delivered) == 1
    assert bot.delivered[0]["text"] == "(delayed)\nanswer"


def test_delayed_large_reply_fits_without_losing_parts(rig):
    cfg, bot, sender = rig
    bot.errors = [error(401)]
    original = "x" * 12000
    assert mirror.run_reply(cfg, "Test", "sess-1", original, False) == 0
    assert len(queue()) > 1
    heartbeat.run_once(cfg, bot, find=lambda _: None)
    assert queue() == []
    assert all(len(p["text"]) <= mirror.TELEGRAM_LIMIT for p in bot.delivered)
    assert sum(mirror._plain(p["text"]).count("x") for p in bot.delivered) == 12000


def test_queued_project_reply_moved_home_uses_current_chat(rig):
    from ctb import flood
    cfg, bot, sender = rig
    bot.errors = [error(401)]
    sender._post(123, sender.topics, "sess-1", "Test", "answer",
                 reply_parameters={"message_id": 42, "allow_sending_without_reply": True})
    old_chat = -1001111111111
    # The outbox still describes the previous project destination; the topic map is now home.
    with reply_outbox.locked() as path:
        entries = reply_outbox.load(path)
        entries[0]["chat"] = old_chat
        entries[0]["answer_to"]["chat"] = old_chat
        reply_outbox.save(path, entries)
    flood.record_429(old_chat, 90)  # a pause in the old group must not hold the new home topic
    home = sender.topics.place_of("sess-1")
    heartbeat.run_once(cfg, bot, find=lambda _: None)
    assert queue() == [] and len(bot.delivered) == 1
    assert bot.delivered[0]["chat_id"] == 123
    assert bot.delivered[0]["message_thread_id"] == home.thread
    assert "reply_parameters" not in bot.delivered[0]  # old chat's ids must not bind unrelated messages
    assert sender.topics.place_of("sess-1") == home


def test_partial_instant_reply_does_not_duplicate_original_parts(rig, monkeypatch):
    cfg, bot, sender = rig
    cfg.original = "edit"
    cfg.target_language = "he"
    bot.errors = [None, error(401)]
    monkeypatch.setattr(mirror, "translate_reply", lambda text, fn, lang: fn(text))
    sender.send_reply("Test", "sess-1", "x" * 12000, lambda _: "תשובה מתורגמת")
    assert len(bot.delivered) == 1 and len(queue()) > 1
    heartbeat.run_once(cfg, bot, find=lambda _: None)
    assert queue() == []
    assert sum(mirror._plain(p["text"]).count("x") for p in bot.delivered) == 12000
    assert not any("תשובה מתורגמת" in p["text"] for p in bot.delivered)


def test_a_damaged_outbox_starts_empty_and_never_stops_the_heartbeat(tmp_path):
    from ctb import reply_outbox
    path = tmp_path / "reply-outbox.json"
    path.write_text("{not json", encoding="utf-8")
    assert reply_outbox.load(path) == []
    path.write_text('[{"session": "s"}, 5]', encoding="utf-8")
    assert reply_outbox.load(path) == []


@pytest.mark.parametrize("bookkeeping", ["record_reply", "_note_sent", "_keyboard_went_out"])
def test_accepted_part_is_never_retried_or_queued_after_bookkeeping_error(rig, monkeypatch, bookkeeping):
    cfg, bot, sender = rig
    def fail(*args, **kwargs):
        raise OSError("private answer and diagnostics")
    monkeypatch.setattr(topics if bookkeeping == "record_reply" else mirror.Mirror, bookkeeping, fail)
    monkeypatch.setattr(mirror.time, "sleep", lambda _: pytest.fail("accepted sends must not retry"))
    assert sender._post(123, sender.topics, "sess-1", "Test", "private answer", record_reply=True)
    assert sender.first_reply_id is not None
    assert sender.dropped == sender.queued == 0
    assert len(bot.attempts) == len(bot.delivered) == 1 and queue() == []
    assert reply_outbox.drain(cfg, bot) == 0
    assert len(bot.delivered) == 1
    assert "private answer" not in (state_dir() / "mirror.log").read_text()


def entry(session, text, chat=123):
    return {"session": session, "text": text, "chat": chat, "thread": None,
            "title": "Test", "options": {}, "created": time.time()}


def seed_queue(entries):
    with reply_outbox.locked() as path:
        reply_outbox.save(path, entries)


def test_full_queue_accepts_new_parts_without_loss(rig, tmp_path, monkeypatch):
    from ctb import flood
    cfg, bot, sender = rig
    prepare_stop(cfg, monkeypatch)
    pending = [entry("sess-1", f"private-{i}") for i in range(reply_outbox.KEEP)]
    seed_queue(pending)
    flood.record_429(123, 90)
    data = stop_data(tmp_path, [ev("user", "q"), ev("assistant", "private answer", "answer-id")])
    hooks._finish_stop(data)
    assert len(queue()) > len(pending) and bot.attempts == []


def test_load_does_not_truncate_an_existing_overfull_queue(rig):
    pending = [entry("sess-1", str(i)) for i in range(reply_outbox.KEEP + 1)]
    seed_queue(pending)
    assert queue() == pending
    with reply_outbox.locked() as path:
        entries = reply_outbox.load(path)
        assert reply_outbox.append(path, entries, entry("sess-1", "extra"))
    assert len(queue()) == len(pending) + 1


def test_drain_skips_flood_gated_session_and_keeps_its_order(rig):
    from ctb import flood
    from ctb.config import ProjectConfig
    cfg, bot, sender = rig
    cfg.projects = [ProjectConfig(name="Other", chat_id=456)]
    sender = mirror.build(cfg, bot=bot)
    sender.topics.ensure("sess-2", "Other", thread=202, chat=456)
    pending = [entry("sess-1", "first"), entry("sess-2", "other", 456), entry("sess-1", "second")]
    seed_queue(pending)
    flood.record_429(123, 90)
    assert reply_outbox.drain(cfg, bot) == 1
    assert queue() == [pending[0], pending[2]]
    assert [p["text"] for p in bot.delivered] == ["(delayed)\nother"]
    (state_dir() / flood.STATE_FILE).unlink()
    assert reply_outbox.drain(cfg, bot) == 2 and queue() == []
    assert [p["text"] for p in bot.delivered][1:] == ["(delayed)\nfirst", "(delayed)\nsecond"]


def test_drain_moves_only_plain_fallback_400_aside_and_continues_session(rig):
    cfg, bot, sender = rig
    head = entry("sess-1", "private failing part")
    seed_queue([head, entry("sess-1", "next"), entry("sess-2", "other")])
    bad_html = TelegramError("Bad Request: can't parse entities", 400)
    bot.errors = [bad_html, error(400)]  # the HTML is refused, then its plain-text fallback too
    assert reply_outbox.drain(cfg, bot) == 2 and queue() == []
    failed = reply_outbox.load(state_dir() / reply_outbox.FAILED_FILE)
    assert len(failed) == 1 and failed[0]["text"] == head["text"]
    assert failed[0]["failures"] == 1
    assert [p["text"] for p in bot.delivered] == ["(delayed)\nnext", "(delayed)\nother"]
    logs = (state_dir() / "mirror.log").read_text()
    assert "moved aside" in logs and "private failing part" not in logs
    assert stat.S_IMODE((state_dir() / reply_outbox.FAILED_FILE).stat().st_mode) == 0o600


def test_failed_head_only_blocks_its_own_session(rig, monkeypatch):
    cfg, bot, sender = rig
    monkeypatch.setattr(mirror.time, "sleep", lambda s: None)
    first, later = entry("sess-1", "first"), entry("sess-1", "later")
    seed_queue([first, entry("sess-2", "other"), later])
    bot.errors = [error(500)] * 3
    assert reply_outbox.drain(cfg, bot) == 1
    assert queue() == [dict(first, failures=1), later]
    assert [p["text"] for p in bot.delivered] == ["(delayed)\nother"]


def test_ten_transient_failures_keep_head_pending(rig, monkeypatch):
    cfg, bot, sender = rig
    monkeypatch.setattr(mirror.time, "sleep", lambda s: None)  # the retry waits are real seconds; CI allows 20 s a test
    seed_queue([entry("sess-1", "bad"), entry("sess-1", "next")])
    for failures in range(1, 11):
        bot.errors = [error(503)] * 3
        assert reply_outbox.drain(cfg, bot) == 0
        assert [e["text"] for e in queue()] == ["bad", "next"]
        assert queue()[0]["failures"] == failures
    assert reply_outbox.load(state_dir() / reply_outbox.FAILED_FILE) == []


def test_retry_command_requeues_failed_parts(rig, capsys):
    failed = [entry("sess-1", "failed")]
    failed[0]["failures"] = 2
    reply_outbox.save(state_dir() / reply_outbox.FAILED_FILE, failed)
    seed_queue([entry("sess-1", "pending")])
    assert cli.main(["outbox", "retry"]) == 0
    assert capsys.readouterr().out == "Requeued 1 failed reply part.\n"
    assert [e["text"] for e in queue()] == ["pending", "failed"]
    assert "failures" not in queue()[1]
    assert reply_outbox.load(state_dir() / reply_outbox.FAILED_FILE) == []


@pytest.mark.parametrize("kind", ["reply", "alert", "drain"])
def test_network_wait_does_not_hold_global_queue_lock_or_other_sessions(rig, monkeypatch, kind):
    cfg, bot, sender = rig
    sender.topics.ensure("sess-2", "Other")
    entered, release = threading.Event(), threading.Event()
    if kind == "drain":
        seed_queue([entry("sess-1", "first"), entry("sess-1", "second")])
    original = mirror.send_essential if kind == "alert" else mirror.Mirror._post_now
    def wait(*args, **kwargs):
        session = args[2] if kind == "alert" else args[3]
        if session == "sess-1":
            entered.set()
            assert release.wait(5)
        return original(*args, **kwargs)
    if kind == "alert":
        monkeypatch.setattr(mirror, "send_essential", wait)
        job = lambda: mirror.alert(cfg, "sess-1", "notice", bot=bot)
    else:
        monkeypatch.setattr(mirror.Mirror, "_post_now", wait)
        job = (lambda: reply_outbox.drain(cfg, bot)) if kind == "drain" else (
            lambda: sender._post(123, sender.topics, "sess-1", "Test", "first"))
    with ThreadPoolExecutor(max_workers=3) as pool:
        busy = pool.submit(job)
        try:
            assert entered.wait(5)
            assert pool.submit(queue).result(timeout=2) is not None
            # A second heartbeat must skip the locked session rather than wait or duplicate it.
            assert pool.submit(reply_outbox.drain, cfg, bot).result(timeout=2) == 0
            other = mirror.build(cfg, bot=bot)
            assert pool.submit(other._post, 123, other.topics, "sess-2", "Other", "other").result(timeout=2)
        finally:
            release.set()
        busy.result(timeout=5)
    assert queue() == []
    assert [p["text"] for p in bot.delivered][0] == "other"
    assert len(bot.delivered) == (3 if kind == "drain" else 2)
    with reply_outbox.session_locked("sess-1", blocking=False) as acquired:
        assert acquired  # no stale in-flight state after the sender releases its lock


def test_a_400_that_is_not_bad_html_keeps_the_part_pending(rig):
    """Only a refused plain-text fallback is permanent; any other 400 (e.g. a chat not found) waits for a fix."""
    cfg, bot, sender = rig
    seed_queue([entry("sess-1", "waits")])
    bot.errors = [error(400)]
    assert reply_outbox.drain(cfg, bot) == 0 and [e["text"] for e in queue()] == ["waits"]
