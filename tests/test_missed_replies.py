"""A turn's final answer reaches the topic even when its Stop worker breaks during a reinstall.

Built from the shapes seen in the live logs (no content):
- 2026-11-14 19:21:24 "stop delivery failed (ImportError: cannot import name 'reply_outbox' from 'ctb')" then
  19:21:26 "stop delivery: send had started; not retrying, no notice": the "send started" mark was written
  before run_reply, the import failed before any sendMessage, and the retry was skipped.
- 2026-11-12 21:47 (an install at 21:40-21:53): the turn ended (stop_hook_summary after 84 s of hooks), yet
  no worker log line exists and the final block never reached the delivery ledger.
Review of #342 (three duplicate-send paths): the sweep redelivers only a turn that no sender ever claimed;
a claim is taken under the session's stop lock before the first send or enqueue.
"""
import dataclasses
import fcntl
import json
import sys
import time
from datetime import datetime, timezone

import pytest

import ctb
from conftest import GOOD_TOKEN
from ctb import hooks as H
from ctb import missed_replies as MR
from ctb.config import Config, private_dir, state_dir
from test_hooks import ev
from test_mirror_reply_retry import FlakyBot

SESSION = "12664b02-0000-4000-8000-000000000000"
T_END = datetime(2026, 11, 12, 19, 48, 49, tzinfo=timezone.utc).timestamp()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@pytest.fixture
def rig(monkeypatch):
    from ctb import compact, mirror, progress, radio_talk, topics
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=False, target_language="en",
                 ui_language="en", context_footer=False, quiet_background_turns=False,
                 codex_harness_enabled=False)
    bot = FlakyBot()
    monkeypatch.setattr(mirror.Bot, "from_config", classmethod(lambda cls, cfg, **kw: bot))
    monkeypatch.setattr(mirror, "refresh_card", lambda *a, **kw: False)
    monkeypatch.setattr(mirror, "limit_offer", lambda *a, **kw: False)
    monkeypatch.setattr(mirror.usage, "compact_footer", lambda *a: "")
    monkeypatch.setattr(mirror.usage, "footer", lambda *a: "")
    alerts, logs, spawned = [], [], []
    monkeypatch.setattr(mirror, "alert", lambda *a, **k: alerts.append(a))
    monkeypatch.setattr(mirror, "log", lambda line: logs.append(line))
    monkeypatch.setattr(H, "load_config", lambda: cfg)
    monkeypatch.setattr(H, "spawn", lambda args, text=None: spawned.append((args, text)))
    monkeypatch.setattr(H, "RETRY_DELAY", 0)
    monkeypatch.setattr(H, "drop_progress", lambda *a, **k: None)
    monkeypatch.setattr(progress, "run_send_progress", lambda *a, **k: 0)
    monkeypatch.setattr(topics, "set_state", lambda *a, **k: 0)
    monkeypatch.setattr(compact, "auto_compact_after_reply", lambda *a, **k: None)
    monkeypatch.setattr(radio_talk, "on_reply", lambda *a, **k: None)
    mirror.build(cfg, bot=bot).topics.ensure(SESSION, "Test")
    return cfg, bot, alerts, logs, spawned


def write_turn(path, final="Final answer.\n\nSummary: the fix is in.", end=T_END, newer=False):
    """A session transcript the way Claude Code writes it: prompt, tool round, final text, hooks, turn end."""
    rows = [
        ev("user", "earlier question", "u0"), ev("assistant", "earlier answer", "a0"),
        json.dumps({"type": "system", "subtype": "stop_hook_summary", "timestamp": iso(end - 600)}),
        ev("user", "do the thing", "u1"),
        ev("assistant", blocks=[{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}], uuid="a1"),
        ev("user", blocks=[{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}], uuid="u2"),
        ev("assistant", final, "a2", timestamp=iso(end - 86)),
        json.dumps({"type": "attachment", "attachment": {"type": "hook_success", "hookEvent": "Stop"}}),
        json.dumps({"type": "system", "subtype": "stop_hook_summary", "hookCount": 5, "timestamp": iso(end),
                    "cwd": "/x/proj"}),
        json.dumps({"type": "system", "subtype": "turn_duration", "timestamp": iso(end + 18)}),
    ]
    if newer:
        rows.append(ev("user", "next question", "u3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n")
    return path


@pytest.fixture
def transcript(tmp_path):
    return write_turn(tmp_path / "projects" / "-x-proj" / f"{SESSION}.jsonl")


def mirrored_before():
    H._mark(SESSION, ["a0:0"])  # the bridge mirrored this session before: it has a ledger


def worker_data(path):
    return {"session_id": SESSION, "transcript_path": str(path), "cwd": "/x/proj",
            "_offset": path.stat().st_size}


# --- 1. a worker's own install retry still delivers when nothing was sent ----------------------------

def test_import_failure_before_any_send_is_retried_and_delivered(rig, transcript, monkeypatch):
    """2026-11-14 19:21: `from . import reply_outbox` failed mid-install before any send; the worker
    said "send had started" and the final answer was never delivered."""
    cfg, bot, alerts, logs, spawned = rig
    data = worker_data(transcript)
    with monkeypatch.context() as broken:  # the package is half replaced: reply_outbox cannot be imported
        broken.delattr(ctb, "reply_outbox")
        broken.setitem(sys.modules, "ctb.reply_outbox", None)
        assert H.finish_stop(data) == 0
    assert not bot.delivered
    assert not any("send had started" in line for line in logs)
    assert spawned and spawned[0][0] == ["finish-stop"]
    retry = json.loads(spawned[0][1])
    assert retry["_attempt"] == 1 and retry["_claim"]
    assert H.finish_stop(retry) == 0  # the fresh process sees the finished install
    assert [p["text"].count("Final answer.") for p in bot.delivered] == [1]
    assert "a2:0" in H._seen(SESSION) and not alerts
    assert H.read_claim(SESSION, "a2:0")["done"] is True


def test_failure_after_telegram_accepted_a_part_is_not_resent(rig, transcript, monkeypatch):
    cfg, bot, alerts, logs, spawned = rig
    from ctb import mirror

    real = mirror.Mirror._post_now

    def send_then_break(self, *a, **k):
        real(self, *a, **k)
        raise ImportError("half installed")
    monkeypatch.setattr(mirror.Mirror, "_post_now", send_then_break)
    assert H.finish_stop(worker_data(transcript)) == 1
    assert len(bot.delivered) == 1
    assert not spawned and any("send had started" in line for line in logs)
    assert H.read_claim(SESSION, "a2:0")["accepted"] is True


def test_on_accept_fires_once_on_the_first_accepted_part(rig):
    from ctb import mirror
    cfg, bot, *_ = rig
    calls = []
    sender = mirror.build(cfg, bot=bot)
    sender.on_accept = lambda: calls.append(1)
    bot.errors = [mirror.TelegramError("bad", 400)]
    assert not sender._post_now(cfg.owner_chat_id, sender.topics, SESSION, "Test", "x")
    assert calls == []
    assert sender._post_now(cfg.owner_chat_id, sender.topics, SESSION, "Test", "one")
    assert sender._post_now(cfg.owner_chat_id, sender.topics, SESSION, "Test", "two")
    assert calls == [1]


@pytest.mark.parametrize("fails", [1, 99])
def test_a_notice_that_failed_mid_install_is_tried_again_at_most_three_times(rig, transcript, monkeypatch, fails):
    cfg, bot, alerts, logs, spawned = rig
    from ctb import mirror
    write_turn(transcript, end=time.time())
    mirrored_before()
    real = mirror.Mirror._post_now

    def send_then_break(self, *a, **k):
        real(self, *a, **k)
        raise ImportError("half installed")
    monkeypatch.setattr(mirror.Mirror, "_post_now", send_then_break)
    assert H.finish_stop(worker_data(transcript)) == 1
    later = H.read_claim(SESSION, "a2:0")["at"] + MR.SETTLE + 1
    tries = []

    def notify(session):
        tries.append(session)
        if len(tries) <= fails:
            raise ImportError("reply_outbox missing during install")
    for i in range(6):
        MR.sweep(cfg, now=later + 60 * i, spawn_fn=lambda args, text=None: spawned.append((args, text)),
                 root=str(transcript.parent.parent), force=True, notify=notify)
    assert not spawned and len(bot.delivered) == 1  # never a resend
    assert len(tries) == (2 if fails == 1 else MR.NOTICE_TRIES)  # retried after a failure, never a flood


# --- 2. the heartbeat redelivers a turn that never started ------------------------------------------

def sweep(cfg, transcript, now, spawned, notices=None):
    return MR.sweep(cfg, now=now, spawn_fn=lambda args, text=None: spawned.append((args, text)),
                    root=str(transcript.parent.parent), force=True,
                    notify=lambda s: (notices if notices is not None else []).append(s))


def test_sweep_restarts_the_worker_for_a_missed_final_reply(rig, transcript):
    """2026-11-12 21:47: the worker never ran (install window); nothing was logged or retried."""
    cfg, bot, alerts, logs, spawned = rig
    mirrored_before()
    assert sweep(cfg, transcript, T_END + 300, spawned) == 1 and spawned[0][0] == ["finish-stop"]
    data = json.loads(spawned[0][1])
    assert data["session_id"] == SESSION and data["_swept"] is True and data["_claim"]
    assert H.last_reply(H.read_final_transcript(data, wait=0)) == "Final answer.\n\nSummary: the fix is in."
    line = next(line for line in logs if "missed final reply" in line)
    assert "Final answer" not in line
    assert H.finish_stop(data) == 0
    assert [p["text"].count("Final answer.") for p in bot.delivered] == [1]
    spawned.clear()
    notices = []
    assert sweep(cfg, transcript, T_END + 900, spawned, notices) == 0
    assert not spawned and not notices


def test_a_worker_that_could_not_start_is_started_again_by_the_next_sweep(rig, transcript):
    cfg, bot, alerts, logs, spawned = rig
    mirrored_before()

    def broken(args, text=None):
        raise OSError("console script missing mid-install")
    assert MR.sweep(cfg, now=T_END + 300, spawn_fn=broken, root=str(transcript.parent.parent), force=True,
                    notify=lambda s: None) == 0
    assert H.read_claim(SESSION, "a2:0") is None  # nothing was sent: the claim was given back
    assert sweep(cfg, transcript, T_END + 400, spawned) == 1 and spawned[0][0] == ["finish-stop"]


def test_a_notice_neither_sent_nor_queued_is_not_counted_as_given(rig, transcript, monkeypatch):
    cfg, bot, alerts, logs, spawned = rig
    from ctb import mirror
    write_turn(transcript, end=time.time())
    mirrored_before()
    real = mirror.Mirror._post_now

    def send_then_break(self, *a, **k):
        real(self, *a, **k)
        raise ImportError("half installed")
    monkeypatch.setattr(mirror.Mirror, "_post_now", send_then_break)
    assert H.finish_stop(worker_data(transcript)) == 1
    later = H.read_claim(SESSION, "a2:0")["at"] + MR.SETTLE + 1
    answers = iter([False, True])
    tries = []

    def notify(session):
        tries.append(session)
        return next(answers)
    for i in range(4):
        MR.sweep(cfg, now=later + 60 * i, spawn_fn=lambda args, text=None: spawned.append((args, text)),
                 root=str(transcript.parent.parent), force=True, notify=notify)
    assert len(tries) == 2 and H.read_claim(SESSION, "a2:0")["noticed"] is True and not spawned


def test_sweep_still_delivers_when_a_newer_prompt_started(rig, tmp_path):
    cfg, bot, alerts, logs, spawned = rig
    path = write_turn(tmp_path / "projects" / "p" / f"{SESSION}.jsonl", newer=True)
    mirrored_before()
    assert sweep(cfg, path, T_END + 300, spawned) == 1
    assert H.last_reply(H.read_final_transcript(json.loads(spawned[0][1]), wait=0)).startswith("Final answer.")


# --- 3. review of #342: no turn is ever delivered twice ----------------------------------------------

def test_a_live_worker_that_starts_after_the_sweep_claimed_hands_off(rig, transcript):
    """P1 1: the sweep checked "no worker" and let go of the lock; the live worker then started. Now the
    sweep's claim is taken under the lock, so the late live worker sends nothing."""
    cfg, bot, alerts, logs, spawned = rig
    mirrored_before()
    assert sweep(cfg, transcript, T_END + 300, spawned) == 1
    swept = json.loads(spawned[0][1])
    assert H.finish_stop(worker_data(transcript)) == 0  # the late live worker
    assert bot.delivered == [] and any("another sender owns this turn" in line for line in logs)
    assert H.finish_stop(swept) == 0
    assert [p["text"].count("Final answer.") for p in bot.delivered] == [1]
    assert H.finish_stop(swept) == 0  # the same swept worker twice: the claim is done
    assert len(bot.delivered) == 1


def test_a_live_worker_that_sent_a_part_then_died_is_never_resent(rig, transcript, monkeypatch):
    """P1 1, other order: the live worker claimed, Telegram accepted part 1, then the worker failed."""
    cfg, bot, alerts, logs, spawned = rig
    from ctb import mirror
    write_turn(transcript, end=time.time())  # live clock: the claim is stamped with real time
    mirrored_before()
    real = mirror.Mirror._post_now

    def send_then_break(self, *a, **k):
        real(self, *a, **k)
        raise ImportError("half installed")
    monkeypatch.setattr(mirror.Mirror, "_post_now", send_then_break)
    assert H.finish_stop(worker_data(transcript)) == 1
    assert len(bot.delivered) == 1
    later = H.read_claim(SESSION, "a2:0")["at"] + MR.SETTLE + 1
    notices = []
    for now in (later, later + 600):
        assert sweep(cfg, transcript, now, spawned, notices) == 0
    assert not spawned and notices == [SESSION]  # one short notice, never a resend
    assert len(bot.delivered) == 1


def test_a_later_turn_of_the_same_session_does_not_reopen_an_earlier_one(rig, tmp_path):
    """P1 2: one "started" key per session let turn B's success overwrite turn A's state."""
    cfg, bot, alerts, logs, spawned = rig
    path = tmp_path / "projects" / "p" / f"{SESSION}.jsonl"
    write_turn(path)  # turn A ends at T_END with final block a2
    a_view = worker_data(path)
    with open(path, "a") as f:  # turn B
        f.write("\n".join([
            ev("user", "and the next thing", "u9"), ev("assistant", "Second answer.", "b2"),
            json.dumps({"type": "system", "subtype": "stop_hook_summary", "timestamp": iso(T_END + 60)}),
        ]) + "\n")
    mirrored_before()
    with H.stop_lock(SESSION):  # turn A: claimed, part 1 accepted, then the worker process was killed
        H.take_claim(SESSION, "a2:0")
        H.update_claim(SESSION, "a2:0", accepted=True, at=T_END)
    b_view = dict(worker_data(path), _offset=path.stat().st_size)
    assert H.finish_stop(b_view) == 0  # turn B delivers normally
    assert [p["text"] for p in bot.delivered if "Second answer." in p["text"]]
    assert "b2:0" in H._seen(SESSION) and "a2:0" not in H._seen(SESSION)
    sent = len(bot.delivered)
    notices = []
    assert sweep(cfg, path, T_END + 600, spawned, notices) == 0
    assert not spawned and notices == [SESSION] and len(bot.delivered) == sent
    assert a_view  # turn A's view still ends at its own stop record


def test_a_crash_between_accept_and_the_mark_is_never_resent(rig, transcript):
    """P1 3: the process died after Telegram accepted (or the outbox queued) a part but before any mark.
    The claim was written before that first send, so the sweep leaves the turn alone."""
    cfg, bot, alerts, logs, spawned = rig
    mirrored_before()
    with H.stop_lock(SESSION):
        H.take_claim(SESSION, "a2:0", now=T_END)  # the claim only: accepted never got recorded
    notices = []
    assert sweep(cfg, transcript, T_END + 300, spawned, notices) == 0
    assert sweep(cfg, transcript, T_END + 900, spawned, notices) == 0
    assert not spawned and notices == [SESSION]


def test_a_claim_whose_worker_alerted_gets_no_second_notice(rig, transcript, monkeypatch):
    cfg, bot, alerts, logs, spawned = rig
    from ctb import mirror
    mirrored_before()
    write_turn(transcript, end=time.time())
    monkeypatch.setattr(mirror, "run_reply", lambda *a, **k: 1)  # delivery failed; the worker alerted
    assert H.finish_stop(worker_data(transcript)) == 0
    assert alerts
    notices = []
    assert sweep(cfg, transcript, time.time() + 600, spawned, notices) == 0
    assert not spawned and not notices


@pytest.mark.parametrize("case", ["delivered", "fresh", "old", "never_mirrored", "worker_busy", "mirror_off",
                                  "no_uuid"])
def test_sweep_leaves_a_turn_alone(rig, transcript, case):
    cfg, bot, alerts, logs, spawned = rig
    now = T_END + 300
    if case != "never_mirrored":
        mirrored_before()
    if case == "delivered":
        H._mark(SESSION, ["a2:0"])
    elif case == "fresh":
        now = T_END + 30  # its worker may still be starting on a busy machine
    elif case == "old":
        now = T_END + MR.WINDOW + 60
    elif case == "mirror_off":
        cfg = dataclasses.replace(cfg, mirror=False)
    elif case == "no_uuid":
        transcript.write_text(transcript.read_text().replace('"uuid": "a2"', '"x": 1'))
    lock = None
    if case == "worker_busy":
        lock = open(private_dir(state_dir() / "stop-locks") / SESSION, "a")
        fcntl.flock(lock, fcntl.LOCK_EX)
    notices = []
    try:
        assert sweep(cfg, transcript, now, spawned, notices) == 0
    finally:
        if lock:
            lock.close()
    assert not spawned and not notices


def test_sweep_runs_at_most_once_a_minute(rig, transcript):
    cfg, *_ = rig
    mirrored_before()
    MR._LAST[0] = 0.0
    seen = []
    root = str(transcript.parent.parent)
    assert MR.sweep(cfg, now=T_END + 300, spawn_fn=lambda *a, **k: seen.append(a), root=root) == 1
    assert MR.sweep(cfg, now=T_END + 310, spawn_fn=lambda *a, **k: seen.append(a), root=root) == 0
    assert len(seen) == 1


def test_a_partly_written_last_line_is_ignored(rig, transcript):
    with open(transcript, "a") as f:
        f.write('{"type": "assistant", "message": {"role": "assis')
    rows = MR._tail_rows(str(transcript))
    assert all(raw.endswith("}\n") for _, raw in rows)
    assert all(transcript.read_bytes()[off:off + 1] == b"{" for off, _ in rows)


def test_the_incomplete_notice_is_localized():
    from ctb.i18n import t
    assert t("alert_reply_incomplete", lang="he") != t("alert_reply_incomplete", lang="en")
