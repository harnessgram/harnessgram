"""Every Telegram message really reaches its session, even one sent right near the end of a turn.

Each test reproduces one race of the old take-and-forget delivery with a fake clock and a fake session probe:
a hook output Claude Code discarded, a queued command that drained without it, a session that died after the
take, a message queued after the Stop hook's look with a stuck busy mark, a late proof after a put-back.
"""
import json
import threading
import time

import pytest

from ctb import delivery, framing, hooks as H, sweeper as SW, wake as W
from ctb.config import Config
from ctb.inbox import Inbox, LockBusy, default_inbox

OWNER = 1


class Bot:
    def __init__(self):
        self.calls = []

    def call(self, method, params=None, timeout=15):
        self.calls.append((method, params or {}))
        return {"message_id": 900 + len(self.calls)} if method == "sendMessage" else {}

    def reactions(self, emoji):
        return [p["message_id"] for m, p in self.calls
                if m == "setMessageReaction" and p["reaction"][0]["emoji"] == emoji]


class Probe:
    def __init__(self):
        self.is_alive, self.is_idle, self.is_limited = True, False, False
        self.nudged, self.reopened = [], []

    def alive(self, session):
        return self.is_alive

    def limited(self, session):
        return self.is_limited

    def idle(self, session, transcript, now):
        return self.is_idle

    def taker_alive(self, lease):
        return False  # the hook that took it has exited

    def nudge(self, inbox, session, force=False):
        self.nudged.append(session)
        return True

    def reopen(self, inbox, session):
        self.reopened.append(session)


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def cfg():
    return Config(bot_token="t", owner_chat_id=OWNER)


@pytest.fixture
def world(tmp_path, cfg):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    box = default_inbox()
    bot, probe = Bot(), Probe()
    logs = []
    sw = SW.Sweeper(cfg, bot, box, probe=probe, clock=Clock(), log=logs.append, workers=0)
    return {"box": box, "bot": bot, "probe": probe, "sw": sw, "t": transcript, "logs": logs}


def owner_msg(text, mid):
    return {"text": text, "id": mid, "chat": OWNER, "source": "owner", "at": int(time.time())}


def record(transcript, kind, text, **extra):
    event = {"type": kind, **extra}
    if kind in ("user", "api-request-blob"):
        event["message"] = {"role": "user", "content": text}
    elif kind == "queue-operation":
        event.setdefault("operation", "enqueue")
        event["content"] = text
    with open(transcript, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def stop_take(w, session="s"):
    """What the Stop hook does: take under a lease and frame the messages with its nonce."""
    msgs = H.telegram_messages(session, data={"transcript_path": str(w["t"])}, via="stop")
    return msgs, (H.delivery_text(msgs) if msgs else "")


@pytest.fixture(autouse=True)
def no_children(monkeypatch):
    monkeypatch.setattr(H, "spawn_delivered", lambda items: None)
    monkeypatch.setattr(W, "spawn_delivered", lambda items: None)


def test_a_take_is_a_lease_and_the_framing_carries_its_nonce(world):
    world["box"].put("s", owner_msg("near the end", 11))
    msgs, text = stop_take(world)
    assert [m["id"] for m in msgs] == [11]
    nonce = framing.lease_nonce(msgs)
    assert nonce and f'nonce="{nonce}"' in text
    assert not world["box"].pending("s")  # out of the queue, as before ...
    lease = world["box"].leases("s")
    assert len(lease) == 1 and lease[0]["nonce"] == nonce and lease[0]["via"] == "stop"  # ... but not forgotten
    assert "key" not in msgs[0] and "queued_at" not in msgs[0]  # the queue's bookkeeping never reaches a session


def test_discarded_stop_hook_output_is_put_back_and_never_marked_read(world):
    """The Stop hook took the message, then Claude Code dropped its output (the hook was cancelled by the next
    prompt, or timed out): the nonce never reaches the transcript. Before: lost, yet marked 👌."""
    box, sw = world["box"], world["sw"]
    box.put("s", owner_msg("first", 11))
    box.put("s", owner_msg("second", 12))
    stop_take(world)
    box.put("s", owner_msg("third, after the take", 13))
    sw.clock.now = time.time() + SW.LOST_ABSENT - 5
    sw.sweep()
    assert [i["id"] for i in box.peek("s")] == [13]  # still within the grace period
    sw.clock.now = time.time() + SW.LOST_ABSENT + 1
    sw.sweep()
    assert [i["id"] for i in box.peek("s")] == [13]  # a turn is running: the output may still be used
    world["probe"].is_idle = True
    sw.sweep()
    back = box.peek("s")
    assert [i["id"] for i in back] == [11, 12, 13]  # back in front, in their order
    assert back[0]["redelivered"] == 1
    assert world["bot"].reactions("👌") == []
    # the next hook hands them over, under a new lease
    msgs, _ = stop_take(world)
    assert [m["id"] for m in msgs] == [11, 12, 13]


def test_proof_in_the_transcript_settles_the_lease_and_gives_the_read_mark(world):
    box, sw = world["box"], world["sw"]
    box.put("s", owner_msg("hi", 21))
    _, text = stop_take(world)
    record(world["t"], "queue-operation", text)
    record(world["t"], "user", text)
    sw.sweep()
    assert box.leases("s") == []
    assert world["bot"].reactions("👌") == [21]
    sw.clock.now = time.time() + 10 * SW.LOST_ABSENT
    sw.sweep()
    assert not box.pending("s") and world["bot"].reactions("👌") == [21]  # never twice


def test_midstep_proof_is_the_hook_additional_context_attachment(world):
    box, sw = world["box"], world["sw"]
    box.put("s", owner_msg("mid", 31))
    out = H.post_output({"session_id": "s", "transcript_path": str(world["t"])}, Config())
    ctx = out["hookSpecificOutput"]["additionalContext"]
    record(world["t"], "attachment", "", attachment={"type": "hook_additional_context", "content": [ctx]})
    sw.sweep()
    assert box.leases("s") == [] and world["bot"].reactions("👌") == [31]


def test_queued_command_that_drained_without_it_is_put_back_once_idle(world):
    """0dec98e6, 2026-11-02 02:37: the Stop hook's block reached Claude Code's command queue between a burst of
    task notifications, the queue was dequeued, and the message never became a prompt."""
    box, sw, probe = world["box"], world["sw"], world["probe"]
    box.put("s", owner_msg("lost in the queue", 41))
    _, text = stop_take(world)
    record(world["t"], "queue-operation", text, operation="enqueue")
    record(world["t"], "queue-operation", "", operation="dequeue")
    sw.clock.now = time.time() + SW.LOST_QUEUED + 1
    sw.sweep()
    assert not box.pending("s")  # the session is still busy: the queue may still hand it over
    probe.is_idle = True
    sw.sweep()
    assert [i["id"] for i in box.peek("s")] == [41]


def test_queued_command_still_waiting_in_a_busy_session_is_left_alone(world):
    box, sw = world["box"], world["sw"]
    box.put("s", owner_msg("wait for the turn", 42))
    _, text = stop_take(world)
    record(world["t"], "queue-operation", text, operation="enqueue")
    sw.clock.now = time.time() + 10 * SW.LOST_QUEUED
    sw.sweep()
    assert not box.pending("s") and len(box.leases("s")) == 1
    record(world["t"], "user", text)  # the turn ended and Claude Code handed it over
    sw.sweep()
    assert box.leases("s") == [] and world["bot"].reactions("👌") == [42]


def test_waiter_killed_after_its_take_session_gone(world):
    """The wake waiter took the message and the session's process ended (a move, /exit, a restart) before
    Claude Code read its exit-2 output."""
    box, sw, probe = world["box"], world["sw"], world["probe"]
    box.put("s", owner_msg("to a dying session", 51))
    code, text = W.wait_for_message({"session_id": "s", "transcript_path": str(world["t"])},
                                    inbox=box, poll=0.01, settle=0, max_wait=5)
    assert code == 2 and f'nonce="{box.leases("s")[0]["nonce"]}"' in text
    probe.is_alive = False
    sw.clock.now = time.time() + SW.LOST_DEAD + 1
    sw.sweep()
    assert [i["id"] for i in box.peek("s")] == [51]
    assert probe.reopened == ["s"]  # a fresh message to a closed session reopens it


def test_late_proof_after_a_put_back_removes_the_copy(world):
    box, sw = world["box"], world["sw"]
    box.put("s", owner_msg("slow proof", 61))
    _, text = stop_take(world)
    world["probe"].is_idle = True
    sw.clock.now = time.time() + SW.LOST_ABSENT + 1
    sw.sweep()
    assert [i["id"] for i in box.peek("s")] == [61]
    record(world["t"], "user", text)  # the old output was consumed after all
    sw.sweep()
    assert not box.pending("s")  # the put-back copy is gone: never handed over twice
    assert world["bot"].reactions("👌") == [61]


def test_take_skips_a_key_already_proven(world):
    box = world["box"]
    box.put("s", owner_msg("once", 71))
    msgs, text = stop_take(world)
    record(world["t"], "user", text)
    nonce = framing.lease_nonce(msgs)
    lease = box.leases("s")[0]
    assert box.settle("s", nonce, "proven") and box.settle("s", nonce, "proven") == []  # one settler wins
    box.put("s", dict(lease["items"][0]))  # a stray copy with the same key
    assert box.take("s") == []


def test_message_after_the_stop_hook_look_with_a_stuck_busy_mark_is_typed_in(world):
    """The race of the report: the Stop hook found the queue empty, the message landed a moment later, the
    Stop worker left the topic "busy" because something was queued, and the waiter was gone (or its pid
    reused). Nothing delivered it until the next turn. Now the sweeper types it in once the prompt is idle."""
    box, sw, probe = world["box"], world["sw"], world["probe"]
    assert stop_take(world) == ([], "")
    box.put("s", owner_msg("right near the end", 81))
    probe.is_idle = True
    sw.clock.now = time.time() + SW.NUDGE_AFTER - 3
    sw.sweep()
    assert probe.nudged == []  # the waiter still has time
    sw.clock.now = time.time() + SW.NUDGE_AFTER + 1
    sw.sweep()
    assert probe.nudged == ["s"]


def test_no_nudge_while_busy_limited_or_in_flight(world):
    box, sw, probe = world["box"], world["sw"], world["probe"]
    box.put("s", owner_msg("wait", 82))
    sw.clock.now = time.time() + SW.NUDGE_AFTER + 1
    sw.sweep()  # a turn is running: the next step's hook delivers it
    probe.is_idle, probe.is_limited = True, True
    sw.sweep()  # the quota is out: recover.py delivers after the reset
    assert probe.nudged == []


def test_waiting_notice_after_a_minute_deleted_by_the_delivery(world, cfg):
    box, sw, bot = world["box"], world["sw"], world["bot"]
    box.put("s", owner_msg("are you there", 91))
    sw.clock.now = time.time() + SW.WAITING_NOTICE - 5
    sw.sweep()
    assert not [c for c in bot.calls if c[0] == "sendMessage"]
    sw.clock.now = time.time() + SW.WAITING_NOTICE + 1
    sw.sweep()
    sw.sweep()
    notices = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(notices) == 1 and notices[0]["reply_parameters"]["message_id"] == 91
    assert "⏳" in notices[0]["text"]
    _, text = stop_take(world)
    record(world["t"], "user", text)
    sw.sweep()
    assert ("deleteMessage", {"chat_id": OWNER, "message_id": 900 + bot.calls.index(
        ("sendMessage", notices[0])) + 1}) in bot.calls
    assert bot.reactions("👌") == [91]


def test_no_waiting_notice_when_a_receipt_already_says_it(world, cfg):
    box, sw, bot = world["box"], world["sw"], world["bot"]
    box.put("s", owner_msg("held for the turn end", 92))
    delivery.send_ack(bot, cfg, None, 92, True)
    sent = len(bot.calls)
    sw.clock.now = time.time() + SW.WAITING_NOTICE + 1
    sw.sweep()
    assert len([c for c in bot.calls[sent:] if c[0] == "sendMessage"]) == 0


def test_stranded_days_old_items_are_only_logged(world):
    """The four one-item files of 2026-11-09: messages to sessions that never ran again. No nudge, no reopen,
    no notice days later; one line in the log."""
    box, sw, probe = world["box"], world["sw"], world["probe"]
    box.put("s", owner_msg("היי", 93))
    probe.is_alive = False
    for _ in range(3):
        sw.clock.now = time.time() + 3 * 86400
        sw.sweep()
    assert probe.reopened == [] and probe.nudged == []
    assert not [c for c in world["bot"].calls if c[0] == "sendMessage"]
    assert len([line for line in world["logs"] if "old message(s) still queued" in line]) == 1


def test_closed_session_is_reopened_at_most_every_few_minutes(world):
    box, sw, probe = world["box"], world["sw"], world["probe"]
    box.put("s", owner_msg("wake up", 94))
    probe.is_alive = False
    base = time.time()
    for dt in (1, 30, 60, SW.REOPEN_EVERY + 2):
        sw.clock.now = base + dt
        sw.sweep()
    assert probe.reopened == ["s", "s"]


def test_waiter_picks_up_a_message_queued_between_its_look_and_its_sleep(world):
    box = world["box"]
    results = []
    t = threading.Thread(target=lambda: results.append(W.wait_for_message(
        {"session_id": "s", "transcript_path": str(world["t"])}, inbox=box, poll=0.05, settle=0, max_wait=5)))
    t.start()
    time.sleep(0.2)  # the waiter has looked at least once and is sleeping
    box.put("s", owner_msg("between look and sleep", 95))
    t.join(5)
    code, text = results[0]
    nonce = box.leases("s")[0]["nonce"]
    assert code == 2 and f'nonce="{nonce}"' in text


def test_a_busy_queue_lock_never_hangs_a_hook(world, monkeypatch):
    box = world["box"]
    box.put("s", owner_msg("keep me", 96))
    fh, _ = box._locked("s")  # another process holds the session's lock
    monkeypatch.setattr(H, "TAKE_WAIT", 0.2)
    try:
        started = time.monotonic()
        assert H.telegram_messages("s", data={"transcript_path": str(world["t"])}) == []
        assert time.monotonic() - started < 2
        with pytest.raises(LockBusy):
            box.take("s", until=time.monotonic() + 0.1)
    finally:
        fh.close()
    assert [i["id"] for i in box.peek("s")] == [96]  # still queued for the next look


def test_blank_midstep_items_are_settled_not_put_back_forever(world):
    box = world["box"]
    box.put("s", {"text": "   ", "source": "owner"})
    assert H.post_output({"session_id": "s", "transcript_path": str(world["t"])}, Config()) is None
    assert box.leases("s") == []


def test_confirm_worker_gives_the_read_mark_once_the_proof_lands(world, cfg):
    box, bot = world["box"], world["bot"]
    box.put("s", owner_msg("quick", 97))
    msgs, text = stop_take(world)
    nonce = framing.lease_nonce(msgs)
    ticks = []

    def sleep(_):
        ticks.append(1)
        if len(ticks) == 3:
            record(world["t"], "user", text)

    assert SW.run_confirm(cfg, "s", nonce, inbox=box, bot=bot, wait=60, sleep=sleep) == 0
    assert bot.reactions("👌") == [97] and box.leases("s") == []
    world["sw"].sweep()
    assert bot.reactions("👌") == [97]  # the sweeper finds nothing left to confirm


def test_confirm_worker_without_proof_leaves_the_lease_to_the_sweeper(world, cfg):
    box, bot = world["box"], world["bot"]
    box.put("s", owner_msg("dropped", 98))
    msgs, _ = stop_take(world)
    clock = Clock(0.0)

    def sleep(dt):
        clock.now += dt

    SW.run_confirm(cfg, "s", framing.lease_nonce(msgs), inbox=box, bot=bot, wait=5, sleep=sleep, clock=clock)
    assert bot.reactions("👌") == [] and len(box.leases("s")) == 1


def test_scan_reads_only_complete_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    block = json.dumps('<telegram-message nonce="abcd1234abcd1234">\nhi\n</telegram-message>')
    path.write_text('{"type":"user","message":{"content":' + block + '}')  # still being written
    index = SW.TranscriptIndex()
    index.new_pass()
    assert index.state(str(path), "abcd1234abcd1234") == ("absent", False)
    assert index.files[str(path)].pos == 0
    with open(path, "a") as f:
        f.write("}\n")
    index.new_pass()
    assert index.state(str(path), "abcd1234abcd1234")[0] == "consumed"


def test_settle_lost_skips_items_already_queued_again(world):
    box = world["box"]
    box.put("s", owner_msg("dup", 99))
    msgs, _ = stop_take(world)
    lease = box.leases("s")[0]
    box.put("s", dict(lease["items"][0]))  # e.g. a replay put it back already
    assert box.settle("s", lease["nonce"], "lost") == []
    assert [i["id"] for i in box.peek("s")] == [99]


def test_inbox_sessions_lists_queued_and_in_flight(world):
    box = world["box"]
    box.put("a", owner_msg("x", 1))
    box.put("b", owner_msg("y", 2))
    H.telegram_messages("b", data={"transcript_path": str(world["t"])})
    assert box.sessions() == ["a", "b"]


def test_inbox_isolated_root(tmp_path):
    box = Inbox(tmp_path / "other")
    box.put("s", {"text": "x"})
    assert box.sessions() == ["s"]


def test_a_background_agents_stop_word_is_proven_in_its_own_transcript(world):
    """A background agent's tool call takes only the owner's stop word; Claude Code writes that hook's output
    to the agent's own transcript (<session>/subagents/agent-*.jsonl), not the main one. It must count there
    and never come back to the main conversation as a put-back."""
    box, sw = world["box"], world["sw"]
    box.put("s", owner_msg("stop", 100))
    out = H.post_output({"session_id": "s", "agent_id": "a1", "transcript_path": str(world["t"])}, Config(),
                        stop_only=True)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    side = world["t"].parent / "t" / "subagents"
    side.mkdir(parents=True)
    record(side / "agent-a1.jsonl", "attachment", "", attachment={"type": "hook_additional_context", "content": [ctx]})
    sw.clock.now = time.time() + SW.LOST_ABSENT + 1
    sw.sweep()
    assert box.leases("s") == [] and not box.pending("s")
    assert world["bot"].reactions("👌") == [100]
