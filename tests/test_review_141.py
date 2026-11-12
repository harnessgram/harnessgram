"""Guard tests for the third review of the take-then-ack delivery (PR #141, 2026-11-10).

Each test pins one finding: a message is never lost between the queue and its lease, never handed over
twice, never dropped beside a stop word, never typed into another session's pane, and only the bridge's own
framed block in a trusted transcript field proves a delivery.
"""
import re
import json
import os
import time

import pytest

from ctb import framing, hooks as H, sweeper as SW, wake as W
from ctb.config import Config
from ctb.inbox import Inbox, default_inbox

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
        self.is_alive, self.is_idle, self.is_limited, self.taker = True, False, False, False
        self.nudged, self.reopened = [], []

    def alive(self, session):
        return self.is_alive

    def limited(self, session):
        return self.is_limited

    def idle(self, session, transcript, now):
        return self.is_idle

    def taker_alive(self, lease):
        return self.taker

    def nudge(self, inbox, session, force=False):
        self.nudged.append((session, force))
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


@pytest.fixture(autouse=True)
def no_children(monkeypatch):
    monkeypatch.setattr(H, "spawn_delivered", lambda items: None)
    monkeypatch.setattr(W, "spawn_delivered", lambda items: None)


def owner_msg(text, mid):
    return {"text": text, "id": mid, "chat": OWNER, "source": "owner", "at": int(time.time())}


def record(transcript, kind, text, **extra):
    event = {"type": kind, **extra}
    if kind in ("user", "api-request-blob", "assistant"):
        event["message"] = {"role": "assistant" if kind == "assistant" else "user", "content": text}
    elif kind == "queue-operation":
        event.setdefault("operation", "enqueue")
        event["content"] = text
    with open(transcript, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def stop_take(w, session="s"):
    msgs = H.telegram_messages(session, data={"transcript_path": str(w["t"])}, via="stop")
    return msgs, (H.delivery_text(msgs) if msgs else "")


# --- P1-1: the lease is saved before the queue changes ------------------------------------------------

def test_a_lease_that_cannot_be_saved_takes_nothing(world, monkeypatch):
    box = world["box"]
    box.put("s", owner_msg("keep me", 1))

    def full_disk(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Inbox, "_save_leases", full_disk)
    msgs, text = stop_take(world)
    assert msgs == [] and text == ""  # the hook hands nothing over ...
    assert [i["id"] for i in box.peek("s")] == [1]  # ... and the message is still queued


def test_a_take_killed_between_lease_and_queue_never_loses_or_doubles(world, monkeypatch):
    """The hook died right after saving its lease, before emptying the queue: the message is in both. The
    lease owns it: the next take skips it, and if that delivery never lands it is put back exactly once."""
    box, sw, probe = world["box"], world["sw"], world["probe"]
    box.put("s", owner_msg("both places", 2))
    real_unlink = type(box.root).unlink

    def killed(self, *a, **k):
        if self.suffix == ".jsonl":
            raise KeyboardInterrupt  # the process is gone at this instant
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(type(box.root), "unlink", killed)
    with pytest.raises(KeyboardInterrupt):
        box.take("s", lease=H.delivery_lease({"transcript_path": str(world["t"])}, "stop"))
    monkeypatch.setattr(type(box.root), "unlink", real_unlink)
    assert [i["id"] for i in box.peek("s")] == [2] and len(box.leases("s")) == 1
    assert stop_take(world) == ([], "")  # the in-flight copy owns the key: no second delivery
    assert not box.pending("s")
    probe.is_idle = True
    sw.clock.now = time.time() + SW.LOST_ABSENT + 1
    sw.sweep()  # its output never reached the transcript: put back once
    assert [i["id"] for i in box.peek("s")] == [2]
    msgs, _ = stop_take(world)
    assert [m["id"] for m in msgs] == [2]


def test_a_failed_queue_rewrite_leaves_the_old_queue_whole(world, monkeypatch):
    box = world["box"]
    box.put("s", dict(owner_msg("held", 3), hold=True))
    box.put("s", owner_msg("now", 4))
    import ctb.config as C

    def no_rename(*a, **k):
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as m:
        m.setattr(C.os, "replace", no_rename)
        with pytest.raises(OSError):
            box.take("s", midturn=True)
    assert [i["id"] for i in box.peek("s")] == [3, 4]  # never a truncated queue


# --- P1-3: a stop word never swallows the other messages of its batch ---------------------------------

def test_stop_word_batch_still_hands_over_the_owners_other_messages(world):
    box = world["box"]
    box.put("s", owner_msg("stop", 10))
    box.put("s", owner_msg("and then deploy the fix to staging", 11))
    box.put("s", {"text": "a team idea", "source": "team", "name": "Dana"})
    out = H.post_output({"session_id": "s", "transcript_path": str(world["t"])}, Config())
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith(H.MIDTURN_STOP)
    assert "and then deploy the fix to staging" in ctx and "a team idea" in ctx
    nonce = box.leases("s")[0]["nonce"]
    assert f'<telegram-message nonce="{nonce}">' in ctx
    assert ctx.count("<telegram-message ") == 2  # the stop word itself is not repeated as a message


# --- P2-5: only the bridge's framed block in a trusted field proves a delivery -------------------------

@pytest.mark.parametrize("where", ["assistant", "tool_result", "api-request-blob", "bare"])
def test_a_quoted_nonce_never_proves_a_delivery(world, where):
    box, sw, probe = world["box"], world["sw"], world["probe"]
    box.put("s", owner_msg("was it really delivered?", 20))
    _, text = stop_take(world)
    nonce = box.leases("s")[0]["nonce"]
    if where == "assistant":
        record(world["t"], "assistant", text)  # the model quoting an earlier block
    elif where == "tool_result":  # the session read a transcript or a test file holding the block
        record(world["t"], "user", [{"type": "tool_result", "tool_use_id": "x", "content": text}])
    elif where == "api-request-blob":
        record(world["t"], "api-request-blob", text)
    else:
        record(world["t"], "user", f"the owner typed the bare id {nonce} nonce={nonce}")
    sw.sweep()
    assert len(box.leases("s")) == 1 and world["bot"].reactions("👌") == []


def test_the_stop_hook_block_as_next_prompt_proves_it(world):
    box, sw = world["box"], world["sw"]
    box.put("s", owner_msg("hi", 21))
    _, text = stop_take(world)
    record(world["t"], "user", "<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n"
                               "Stop hook blocking error from command \"Stop\": " + text)
    sw.sweep()
    assert box.leases("s") == [] and world["bot"].reactions("👌") == [21]


def test_a_queued_command_attachment_proves_it(world):
    box, sw = world["box"], world["sw"]
    box.put("s", owner_msg("mid-turn", 22))
    _, text = stop_take(world)
    record(world["t"], "attachment", "", attachment={"type": "queued_command", "prompt": text})
    sw.sweep()
    assert box.leases("s") == [] and world["bot"].reactions("👌") == [22]


# --- P2-10: each transcript is read once per pass, for every lease together ----------------------------

def test_many_leases_read_the_transcript_once(world):
    box, sw = world["box"], world["sw"]
    with open(world["t"], "a") as f:
        for n in range(2000):  # a long transcript before the deliveries
            f.write(json.dumps({"type": "assistant", "message": {"content": "x" * 200}}) + "\n")
    for n in range(20):
        box.put("s", owner_msg(f"m{n}", 100 + n))
        stop_take(world)
    sw.sweep()
    first = sw.index.bytes_read
    assert first < 1000  # from the first lease's offset on, once: never the history, never once per lease
    sw.sweep()
    assert sw.index.bytes_read == first  # nothing new: nothing read
    record(world["t"], "user", "next prompt")
    sw.sweep()
    assert sw.index.bytes_read - first < 200


def test_an_agent_step_is_proven_in_that_agents_own_file_only(world):
    box, sw = world["box"], world["sw"]
    side = world["t"].parent / "t" / "subagents"
    side.mkdir(parents=True)
    for other in ("agent-zzz.jsonl", "agent-a1.jsonl"):
        (side / other).write_text(json.dumps({"type": "assistant", "message": {"content": "y" * 5000}}) + "\n")
    box.put("s", owner_msg("stop", 30))
    out = H.post_output({"session_id": "s", "agent_id": "a1", "transcript_path": str(world["t"])}, Config(),
                        stop_only=True)
    lease = box.leases("s")[0]
    assert lease["agent_id"] == "a1" and lease["agent_offset"] > 0
    record(side / "agent-a1.jsonl", "attachment", "",
           attachment={"type": "hook_additional_context", "content": [out["hookSpecificOutput"]["additionalContext"]]})
    sw.sweep()
    assert box.leases("s") == [] and str(side / "agent-zzz.jsonl") not in sw.index.files


# --- P1-2: a lease goes back only once its own output can no longer reach the model --------------------

def test_no_put_back_while_the_taking_hook_still_runs_or_the_session_is_busy(world):
    """A sibling Stop hook kept Claude Code in its hook phase past a minute: our output is still pending. A
    put-back then would let the original and the copy both arrive."""
    box, sw, probe = world["box"], world["sw"], world["probe"]
    box.put("s", owner_msg("slow hooks", 40))
    _, text = stop_take(world)
    assert box.leases("s")[0]["pid"] == os.getpid()
    sw.clock.now = time.time() + 10 * SW.LOST_ABSENT
    probe.taker, probe.is_idle = True, True
    sw.sweep()  # the hook is still running: its output is not out yet
    probe.taker, probe.is_idle = False, False
    sw.sweep()  # it ended, but the session is busy (other hooks, the next turn): it may still be used
    assert not box.pending("s") and len(box.leases("s")) == 1
    record(world["t"], "user", text)  # and it was
    sw.sweep()
    assert box.leases("s") == [] and not box.pending("s") and world["bot"].reactions("👌") == [40]


def test_a_command_still_in_claude_codes_queue_is_handed_over_by_a_prompt_not_put_back(world):
    box, sw, probe = world["box"], world["sw"], world["probe"]
    box.put("s", owner_msg("in the queue", 41))
    _, text = stop_take(world)
    record(world["t"], "queue-operation", text, operation="enqueue")
    probe.is_idle = True
    sw.clock.now = time.time() + 10 * SW.LOST_QUEUED
    sw.sweep()
    assert ("s", True) in probe.nudged  # typed in, though our own queue is empty
    assert not box.pending("s") and len(box.leases("s")) == 1  # never a second copy beside the queued one


def test_real_taker_check_uses_the_pid():
    probe = SW.Probe()
    assert probe.taker_alive({"pid": os.getppid()}) is True
    assert probe.taker_alive({}) is False
    assert probe.taker_alive({"pid": 2 ** 22 + 12345}) is False


# --- P1-4 and P2-9: the sweeper types only into this session's own, idle, empty input box ---------------

from ctb import inbox as I  # noqa: E402

RULE = "─" * 40
IDLE = f"old output\n❯ an old prompt in the scrollback\n\n{RULE}\n❯ \n{RULE}\n  ⏵⏵ bypass permissions on\n"


class Pane:
    """A fake tmux pane (never the real server): what capture-pane shows, and the keys sent."""

    def __init__(self, screen=IDLE, after_typing=None):
        self.screen, self.after_typing, self.keys = screen, after_typing, []

    def __call__(self, *args):
        class R:
            returncode, stdout = 0, ""
        r = R()
        if args[0] == "capture-pane":
            r.stdout = self.screen
        elif args[0] == "send-keys":
            self.keys.append(args[-1])
            if args[-1] == "Enter":  # Claude Code sends the line: the box is empty again
                self.screen = re.sub(r"(?m)^❯ .+$", "❯ ", self.screen) if RULE in self.screen else self.screen
            if "-l" in args:
                typed = args[-1]
                self.screen = (self.after_typing if self.after_typing is not None
                               else self.screen.replace("❯ \n", f"❯ {typed}\n"))
        return r


@pytest.fixture
def pane(monkeypatch):
    p = Pane()
    monkeypatch.setattr(I, "_tmux", p)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%7")
    monkeypatch.setattr(I, "pane_runs_session", lambda pn, s, known_pid=None: True)
    monkeypatch.setattr(I.time, "sleep", lambda s: None)
    return p


def test_the_sweeper_nudge_types_and_submits_into_an_idle_empty_box(world, pane):
    world["box"].put("s", owner_msg("x", 50))
    assert I.sweep_nudge(world["box"], "s") is True
    assert pane.keys == [I.NUDGE_TEXT, "Enter"]


def test_never_into_a_pane_another_session_now_uses(world, pane, monkeypatch):
    """After a tmux restart the saved %7 belongs to another session; its screen looks idle."""
    world["box"].put("s", owner_msg("x", 51))
    monkeypatch.setattr(I, "pane_runs_session", lambda pn, s, known_pid=None: False)
    assert I.sweep_nudge(world["box"], "s") is False and pane.keys == []
    assert SW.Probe().idle("s", "", time.time()) is False  # nor is that screen read as this session's


def test_a_stale_prompt_in_the_scrollback_is_not_an_idle_box(world, pane):
    world["box"].put("s", owner_msg("x", 52))
    pane.screen = "❯ \nsomething else is running\n  esc to interrupt\n"  # no input box at the bottom
    assert I.sweep_nudge(world["box"], "s") is False and pane.keys == []
    pane.screen = f"❯ \n{RULE}\n❯ the owner's own draft\n{RULE}\n"
    assert I.sweep_nudge(world["box"], "s") is False and pane.keys == []


def test_a_turn_or_menu_appearing_while_typing_stops_before_enter(world, pane):
    world["box"].put("s", owner_msg("x", 53))
    pane.after_typing = f"{RULE}\n❯ {I.NUDGE_TEXT}\n{RULE}\n  Do you want to proceed?\n"
    assert I.sweep_nudge(world["box"], "s") is False
    assert pane.keys == [I.NUDGE_TEXT, "C-u"]  # our own text taken back out, never submitted


def test_the_owners_draft_typed_meanwhile_is_left_untouched(world, pane):
    world["box"].put("s", owner_msg("x", 54))
    pane.after_typing = f"{RULE}\n❯ my own words{I.NUDGE_TEXT}\n{RULE}\n"
    assert I.sweep_nudge(world["box"], "s") is False
    assert pane.keys == [I.NUDGE_TEXT]  # no Enter, no clearing of the owner's text


def test_no_nudge_beside_another_terminal_action_of_the_session(world, pane):
    world["box"].put("s", owner_msg("x", 55))
    assert I._claim_terminal("s")
    try:
        assert I.sweep_nudge(world["box"], "s") is False and pane.keys == []
    finally:
        I._release_terminal("s")
    assert I.sweep_nudge(world["box"], "s") is True


# --- P2-6: every wait of a hook on the session's lock shares one short deadline ------------------------

def _held(box, session="s"):
    fh, _ = box._locked(session)  # another process holds the session's lock and stalls
    return fh


def test_the_stop_hook_entry_point_never_hangs_on_a_stuck_lock(world, monkeypatch, capsys):
    box = world["box"]
    box.put("s", owner_msg("keep me", 60))
    monkeypatch.setattr(H, "TAKE_WAIT", 0.2)
    monkeypatch.setattr(H, "load_config", lambda: Config(bot_token="t", owner_chat_id=OWNER))
    monkeypatch.setattr(H, "spawn", lambda *a, **k: None)
    fh = _held(box)
    try:
        started = time.monotonic()
        assert H.hook_stop(json.dumps({"session_id": "s", "transcript_path": str(world["t"])})) == 0
        assert time.monotonic() - started < 1.0  # one shared deadline, not one per lock
    finally:
        fh.close()
    assert [i["id"] for i in box.peek("s")] == [60]


def test_the_prompt_hook_entry_point_never_hangs_on_a_stuck_lock(world, monkeypatch):
    box = world["box"]
    box.put("s", owner_msg("keep me", 61))
    monkeypatch.setattr(H, "TAKE_WAIT", 0.2)
    fh = _held(box)
    try:
        started = time.monotonic()
        H.prompt_output({"session_id": "s", "prompt": "hi", "transcript_path": str(world["t"])},
                        Config(bot_token="t", owner_chat_id=OWNER))
        assert time.monotonic() - started < 1.0
    finally:
        fh.close()
    assert [i["id"] for i in box.peek("s")] == [61]


def test_the_blank_midstep_settle_never_hangs_on_a_stuck_lock(world, monkeypatch):
    box = world["box"]
    box.put("s", {"text": "  ", "source": "owner"})
    monkeypatch.setattr(H, "TAKE_WAIT", 0.3)
    real_take = Inbox.take

    def take_then_stall(self, *a, **k):  # the lock gets stuck right after the take
        items = real_take(self, *a, **k)
        world["stall"] = _held(self)
        return items

    monkeypatch.setattr(Inbox, "take", take_then_stall)
    try:
        started = time.monotonic()
        assert H.post_output({"session_id": "s", "transcript_path": str(world["t"])}, Config()) is None
        assert time.monotonic() - started < 1.0
    finally:
        world["stall"].close()
    assert len(box.leases("s")) == 1  # left to the sweeper, never lost


# --- P2-7: one stuck session never holds up the others -------------------------------------------------

def _proven_lease(w, session, mid):
    w["box"].put(session, owner_msg("hi", mid))
    _, text = stop_take(w, session)
    record(w["t"], "user", text)


def test_a_stuck_lock_costs_one_session_a_tick_not_the_whole_sweep(world, monkeypatch):
    monkeypatch.setattr(SW, "LOCK_WAIT", 0.2)
    _proven_lease(world, "a", 70)
    _proven_lease(world, "b", 71)
    fh, _ = world["box"]._locked("a")  # a's lock is stuck
    try:
        started = time.monotonic()
        world["sw"].sweep()
        assert time.monotonic() - started < 1.5
    finally:
        fh.close()
    assert world["box"].leases("b") == [] and len(world["box"].leases("a")) == 1
    assert any("sweep a" in line and "LockBusy" in line for line in world["logs"])
    world["sw"].sweep()  # the next tick finishes a
    assert world["box"].leases("a") == []


def test_a_slow_telegram_call_for_one_session_never_delays_another(world, cfg):
    import threading as T
    gate = T.Event()

    class SlowBot(Bot):
        def call(self, method, params=None, timeout=15):
            if method == "setMessageReaction" and (params or {}).get("message_id") == 72:
                gate.wait(5)  # a's 👌 hangs on the network
            return super().call(method, params, timeout)

    bot = SlowBot()
    sw = SW.Sweeper(cfg, bot, world["box"], probe=world["probe"], clock=Clock(), log=world["logs"].append,
                    workers=2)
    _proven_lease(world, "a", 72)
    _proven_lease(world, "b", 73)
    sw.sweep()
    deadline = time.monotonic() + 3
    while world["box"].leases("b") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert world["box"].leases("b") == [] and 73 in bot.reactions("👌")  # b went through while a hangs
    sw.sweep()  # a's sweep is still running: never a second one for it
    gate.set()
    assert sw.idle_workers(5)
    assert bot.reactions("👌").count(72) == 1


# --- P2-8: never stuck forever without a visible state ---------------------------------------------------

def test_a_live_session_is_nudged_however_old_the_message_at_a_falling_rate(world):
    box, sw, probe = world["box"], world["sw"], world["probe"]
    box.put("s", owner_msg("from yesterday", 80))
    probe.is_idle = True
    base = time.time() + 2 * 86400  # two days later, the session runs and sits idle
    for dt in range(0, 600, 3):  # ten minutes of sweeps every 3 s
        sw.clock.now = base + dt
        sw.sweep()
    assert 3 <= len(probe.nudged) <= 6  # 30 s, 1, 2, 4 minutes ... never one per tick, never none
    sw.clock.now = base + 3 * 86400
    sw.sweep()
    assert len(probe.nudged) >= 4  # still retried days on (at least every NUDGE_BACKOFF_MAX)


def test_a_closed_session_that_does_not_reopen_shows_a_visible_failure_once(world):
    box, sw, probe, bot = world["box"], world["sw"], world["probe"], world["bot"]
    box.put("s", owner_msg("are you there", 81))
    probe.is_alive = False
    base = time.time()
    sw.clock.now = base + SW.WAITING_NOTICE + 1
    sw.sweep()  # the waiting notice, and a reopen that does not take
    notice = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(notice) == 1 and probe.reopened == ["s"]
    for dt in (SW.REOPEN_MAX_AGE + 5, SW.REOPEN_MAX_AGE + 400, SW.REOPEN_MAX_AGE + 4000):
        sw.clock.now = base + dt
        sw.sweep()
    edits = [p for m, p in bot.calls if m == "editMessageText"]
    assert len(edits) == 1 and "⚠️" in edits[0]["text"]  # the notice became the failure, once
    assert box.peek("s")[0]["failed"] is True  # durable: kept queued, marked
    assert len(probe.reopened) == 1  # no reopen spam for an abandoned session
    probe.is_alive, probe.is_idle = True, True  # the owner opened it again
    sw.sweep()
    assert probe.nudged
    msgs, _ = stop_take(world)
    assert [m["id"] for m in msgs] == [81] and "failed" not in msgs[0]  # bookkeeping never reaches a session
