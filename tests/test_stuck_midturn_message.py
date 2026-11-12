"""A message the Stop hook took is never stuck behind a lease nobody will release, and never overtaken.

Regression: a message was written in a session's topic while it worked (it showed "will be delivered between steps"). The Stop
hook took the text under a lease, but its block never reached the model (the hook output was dropped), and the
session kept working: background monitor events started one turn after another. The sweeper put a lease back
only once the session sat idle, so the text stayed in flight for about two minutes, while a picture sent a
minute later was handed over at the next tool call, before the text. The text came only as a put-back copy.

A synchronous hook's output (Stop, PostToolUse, UserPromptSubmit) is applied before Claude Code's next model
call, so a new model call after the take that does not carry the nonce proves the output was dropped: the next
hook puts it back and takes it first, in order. Until then a newer message waits behind it.
"""
import json
import os
import time

import pytest

from ctb import hooks as H, sweeper as SW, wake as W
from ctb.config import Config
from ctb.inbox import default_inbox

OWNER = 1
SESSION = "s-sample"


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
    """A busy session: alive, never idle (each notification starts the next turn)."""

    def __init__(self):
        self.is_idle = False
        self.nudged = []

    def alive(self, session):
        return True

    def limited(self, session):
        return False

    def idle(self, session, transcript, now):
        return self.is_idle

    def taker_alive(self, lease):
        return False

    def nudge(self, inbox, session, force=False):
        self.nudged.append(session)
        return True

    def reopen(self, inbox, session):
        pass


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def no_children(monkeypatch):
    monkeypatch.setattr(H, "spawn_delivered", lambda items: None)
    monkeypatch.setattr(W, "spawn_delivered", lambda items: None)


@pytest.fixture
def world(tmp_path):
    cfg = Config(bot_token="t", owner_chat_id=OWNER)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    box, bot, probe = default_inbox(), Bot(), Probe()
    sw = SW.Sweeper(cfg, bot, box, probe=probe, clock=Clock(), workers=0)
    return {"box": box, "bot": bot, "probe": probe, "sw": sw, "t": transcript, "cfg": cfg}


def owner_msg(text, mid):
    return {"text": text, "id": mid, "chat": OWNER, "source": "owner", "at": int(time.time())}


def write(t, event):
    with open(t, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def prompt(t, text):
    write(t, {"type": "user", "message": {"role": "user", "content": text}})


def tool_result(t, tool):
    write(t, {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool, "content": "ok"}]}})


def model(t, mid, kind="text", tool=""):
    block = {"type": "tool_use", "id": tool, "name": "Bash", "input": {}} if kind == "tool_use" else \
        {"type": "text", "text": "sample reply"}
    write(t, {"type": "assistant", "message": {"id": mid, "role": "assistant", "model": "sample", "content": [block]}})


def stop_hooks_done(t, turn_end=True):
    """The Stop hooks finished. The turn's end record follows, unless a queued prompt (a background event)
    is handed over straight away, as Claude Code does then."""
    write(t, {"type": "system", "subtype": "stop_hook_summary", "hookCount": 1, "preventedContinuation": False})
    if turn_end:
        write(t, {"type": "system", "subtype": "turn_duration", "durationMs": 1000})


def context(t, out):
    """Claude Code attaches a PostToolUse / UserPromptSubmit hook's additionalContext."""
    write(t, {"type": "attachment", "attachment": {"type": "hook_additional_context",
                                                   "content": [out["hookSpecificOutput"]["additionalContext"]]}})


def data(w):
    return {"session_id": SESSION, "transcript_path": str(w["t"])}


def stop_hook(w):
    """The Stop hook: take under a lease and frame the messages with its nonce."""
    msgs = H.telegram_messages(SESSION, data=data(w), via="stop")
    return msgs, (H.delivery_text(msgs) if msgs else "")


def post_hook(w):
    return H.post_output(data(w), Config())


def delivered_ids(out):
    text = out["hookSpecificOutput"]["additionalContext"] if out else ""
    return [mid for mid, needle in ((51, "the text"), (52, "the picture")) if needle in text], text


def turn_ending_with_the_stop_hook_dropped(w, turn_end=True):
    t, box = w["t"], w["box"]
    prompt(t, "do the work")
    model(t, "msg_a", "tool_use", "tool_a")
    tool_result(t, "tool_a")
    box.put(SESSION, owner_msg("the text, sent while the session works", 51))
    model(t, "msg_b")  # the turn's final reply
    msgs, _ = stop_hook(w)
    assert [m["id"] for m in msgs] == [51]
    stop_hooks_done(t, turn_end)  # ... and its block never reached the model: the output was dropped


def test_text_from_a_dropped_stop_hook_is_handed_over_at_the_next_turn_boundary(world):
    """The incident's records (2026-11-11): the turn ended (turn_duration) without the Stop hook's block, and a
    background event started the next turn. Before the fix that turn's hooks skipped the text (a lease owned it)
    and the next tool call handed over only the later picture; the text waited for an idle prompt."""
    t, box = world["t"], world["box"]
    turn_ending_with_the_stop_hook_dropped(world)
    prompt(t, "<task-notification>monitor event</task-notification>")  # a notification starts the next turn
    msgs = H.telegram_messages(SESSION, data=data(world), via="prompt")
    assert [m["id"] for m in msgs] == [51]  # the next turn boundary
    write(t, {"type": "attachment", "attachment": {"type": "hook_additional_context",  # the prompt hook's context
                                                   "content": [H.delivery_text(msgs)]}})
    model(t, "msg_c", "tool_use", "tool_c")
    box.put(SESSION, owner_msg("the picture, sent a minute later", 52))
    out = post_hook(world)
    assert delivered_ids(out)[0] == [52]
    context(t, out)
    world["sw"].sweep()
    assert sorted(world["bot"].reactions("👌")) == [51, 52]
    assert not box.pending(SESSION) and not [d for d in box.leases(SESSION) if not d.get("requeued_at")]
    model(t, "msg_d", "tool_use", "tool_d")
    assert post_hook(world) is None  # never twice


def test_text_from_a_dropped_stop_hook_comes_first_at_the_next_tool_call(world):
    """Same, when the queued event is handed over straight after the Stop hooks (no turn end record): the next
    turn's prompt hook cannot tell yet; the next tool call can, and hands over the text before the picture."""
    t, box = world["t"], world["box"]
    turn_ending_with_the_stop_hook_dropped(world, turn_end=False)
    prompt(t, "<task-notification>monitor event</task-notification>")
    assert H.telegram_messages(SESSION, data=data(world), via="prompt") == []
    model(t, "msg_c", "tool_use", "tool_c")
    box.put(SESSION, owner_msg("the picture, sent a minute later", 52))
    out = post_hook(world)
    ids, text = delivered_ids(out)
    assert ids == [51, 52], "the earlier text must come with the picture, and before it"
    assert text.index("the text") < text.index("the picture")
    assert not box.pending(SESSION)
    # exactly one live delivery: the put-back lease is only a tombstone, the new one carries both
    live = [d for d in box.leases(SESSION) if not d.get("requeued_at")]
    assert len(live) == 1 and [i["id"] for i in live[0]["items"]] == [51, 52]
    context(t, out)
    tool_result(t, "tool_c")
    world["sw"].sweep()
    assert sorted(world["bot"].reactions("👌")) == [51, 52]
    assert not box.pending(SESSION) and not [d for d in box.leases(SESSION) if not d.get("requeued_at")]
    model(t, "msg_d", "tool_use", "tool_d")
    assert post_hook(world) is None  # never twice


def test_a_newer_picture_waits_behind_the_undecided_text_never_overtakes_it(world):
    """The picture is already queued when the next turn's prompt hook runs, before any new model call could
    decide the text's lease: it waits one step and goes with the text, after it."""
    t, box = world["t"], world["box"]
    turn_ending_with_the_stop_hook_dropped(world, turn_end=False)
    box.put(SESSION, owner_msg("the picture", 52))
    prompt(t, "<task-notification>monitor event</task-notification>")
    assert H.telegram_messages(SESSION, data=data(world), via="prompt") == []  # held: an older one is in flight
    assert [i["id"] for i in box.peek(SESSION)] == [52]
    model(t, "msg_c", "tool_use", "tool_c")
    ids, _ = delivered_ids(post_hook(world))
    assert ids == [51, 52]


def test_text_dropped_twice_by_consecutive_stop_hooks_is_still_delivered_in_order(world):
    """Each notification-started turn ends with another Stop hook whose output is dropped too."""
    t, box = world["t"], world["box"]
    turn_ending_with_the_stop_hook_dropped(world)
    prompt(t, "<task-notification>monitor event 1</task-notification>")
    model(t, "msg_c")  # a short turn: just a reply
    msgs, _ = stop_hook(world)  # the model moved on without the first lease: it is taken again, first
    assert [m["id"] for m in msgs] == [51]
    stop_hooks_done(t)  # dropped again
    prompt(t, "<task-notification>monitor event 2</task-notification>")
    model(t, "msg_d", "tool_use", "tool_d")
    box.put(SESSION, owner_msg("the picture", 52))
    ids, _ = delivered_ids(post_hook(world))
    assert ids == [51, 52]


def test_the_sweeper_puts_back_a_missed_lease_in_a_busy_session(world):
    """No idle prompt is needed once a new model call proves the output was dropped."""
    t, box, sw = world["t"], world["box"], world["sw"]
    turn_ending_with_the_stop_hook_dropped(world)
    prompt(t, "<task-notification>monitor event</task-notification>")
    model(t, "msg_c", "tool_use", "tool_c")
    sw.clock.now = time.time() + 5
    sw.sweep()
    back = box.peek(SESSION)
    assert [i["id"] for i in back] == [51] and back[0]["redelivered"] == 1


def test_a_stop_block_the_model_received_is_never_put_back(world):
    t, box, sw = world["t"], world["box"], world["sw"]
    prompt(t, "do the work")
    box.put(SESSION, owner_msg("the text", 51))
    model(t, "msg_a")
    _, text = stop_hook(world)
    prompt(t, text)  # Claude Code continued the turn with the block
    model(t, "msg_b", "tool_use", "tool_b")
    tool_result(t, "tool_b")
    model(t, "msg_c")
    sw.clock.now = time.time() + 5
    sw.sweep()
    assert not box.pending(SESSION) and box.leases(SESSION) == []
    assert world["bot"].reactions("👌") == [51]


def test_a_final_reply_flushed_after_the_take_is_not_a_new_model_call(world):
    """The Stop hook can run before the turn's last reply reaches the transcript; while the other Stop hooks
    still run, that late record must not read as "the model moved on" (the block may still be applied)."""
    t, box, sw = world["t"], world["box"], world["sw"]
    prompt(t, "do the work")
    model(t, "msg_a", "tool_use", "tool_a")
    tool_result(t, "tool_a")
    box.put(SESSION, owner_msg("the text", 51))
    stop_hook(world)
    model(t, "msg_b")  # flushed late, after the take
    stop_hooks_done(t, turn_end=False)  # the other Stop hooks are still running
    sw.clock.now = time.time() + 5
    sw.sweep()
    assert not box.pending(SESSION) and len(box.leases(SESSION)) == 1


def test_parallel_tool_calls_of_one_response_are_not_a_new_model_call(world):
    """With streaming tool calls the hook of the first tool can run before the response's next tool_use record is
    written: same response id, so the first hook's output is still on its way."""
    t, box, sw = world["t"], world["box"], world["sw"]
    prompt(t, "do the work")
    model(t, "msg_a", "tool_use", "tool_a")
    box.put(SESSION, owner_msg("the text", 51))
    assert post_hook(world) is not None  # the first tool's hook took it
    tool_result(t, "tool_a")
    model(t, "msg_a", "tool_use", "tool_b")  # same response, second tool
    sw.clock.now = time.time() + 5
    sw.sweep()
    assert not box.pending(SESSION) and len(box.leases(SESSION)) == 1


def test_a_wake_lease_is_left_to_the_idle_rule_and_holds_nothing_back(world):
    """The wake waiter is an async hook: Claude Code may record its output after a model call that was already
    running, so a model call is no proof there. It keeps the older rules, and newer messages are not held."""
    t, box, sw = world["t"], world["box"], world["sw"]
    prompt(t, "do the work")
    model(t, "msg_a")
    box.put(SESSION, owner_msg("the text", 51))
    msgs = box.take(SESSION, lease=H.delivery_lease(data(world), "wake"))
    assert [m["id"] for m in msgs] == [51]
    prompt(t, "<task-notification>monitor event</task-notification>")
    model(t, "msg_b", "tool_use", "tool_b")
    sw.clock.now = time.time() + 5
    sw.sweep()
    assert not box.pending(SESSION)  # not put back by the new rule
    box.put(SESSION, owner_msg("the picture", 52))
    ids, _ = delivered_ids(post_hook(world))
    assert ids == [52]


def test_a_background_agents_stop_word_is_never_held(world):
    """An undecided earlier delivery holds newer messages back in the main conversation, never an owner stop word
    taken at a background agent's step."""
    box = world["box"]
    turn_ending_with_the_stop_hook_dropped(world, turn_end=False)
    box.put(SESSION, owner_msg("stop", 53))
    assert H.telegram_messages(SESSION, data=data(world), via="prompt") == []  # the main conversation waits
    out = H.post_output(dict(data(world), agent_id="agent1"), Config(), stop_only=True)
    assert out and "STOP" in out["hookSpecificOutput"]["additionalContext"]


def test_lease_nonce_is_unchanged_by_the_reclaim(world):
    """The put-back copies get a fresh lease; the framing still carries exactly one nonce."""
    t = world["t"]
    turn_ending_with_the_stop_hook_dropped(world)
    prompt(t, "<task-notification>monitor event</task-notification>")
    model(t, "msg_c", "tool_use", "tool_c")
    out = post_hook(world)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert len(set(SW.TAG.findall(text))) == 1


def blocking_error(t, reason):
    """How Claude Code 2.1 records a Stop hook's block that it hands to the model (no user record follows)."""
    write(t, {"type": "attachment", "attachment": {
        "type": "hook_blocking_error", "hookName": "Stop", "hookEvent": "Stop",
        "blockingError": {"blockingError": reason, "command": "sample-stop-hook"}}})


def test_a_stop_block_recorded_as_a_blocking_error_is_proven_never_sent_twice(world):
    """Before: only a user record counted, so an honoured Stop block stayed unproven, got no read mark, and once
    the session idled the sweeper put it back: the model got the same text twice."""
    t, box, sw = world["t"], world["box"], world["sw"]
    prompt(t, "do the work")
    box.put(SESSION, owner_msg("the text", 51))
    model(t, "msg_a")
    _, text = stop_hook(world)
    blocking_error(t, text)
    stop_hooks_done(t)
    model(t, "msg_b", "tool_use", "tool_b")  # the continuation's model call
    tool_result(t, "tool_b")
    model(t, "msg_c")
    sw.clock.now = time.time() + 10 * SW.LOST_ABSENT
    world["probe"].is_idle = True
    sw.sweep()
    assert box.leases(SESSION) == [] and not box.pending(SESSION)
    assert world["bot"].reactions("👌") == [51]
    box.put(SESSION, owner_msg("the picture", 52))
    model(t, "msg_d", "tool_use", "tool_d")
    ids, _ = delivered_ids(post_hook(world))
    assert ids == [52]


def test_an_idle_session_gets_the_dropped_text_at_the_turns_end_then_the_picture(world):
    """The turn ended with the Stop hook's block dropped and nothing else woke the session. The turn's end record
    decides the lease: the wake waiter hands the text over right away, ahead of a picture sent meanwhile."""
    box = world["box"]
    turn_ending_with_the_stop_hook_dropped(world)  # its records end with turn_duration
    box.put(SESSION, owner_msg("the picture", 52))
    code, text = W.wait_for_message(data(world), inbox=box, poll=0.01, settle=0, max_wait=2)
    assert code == 2 and text.index("the text") < text.index("the picture")
    assert not box.pending(SESSION)


def test_a_stop_hook_still_running_never_reads_as_dropped(world):
    """No turn end and no new model call yet (the other Stop hooks still run): the lease is left alone and a newer
    message waits, so the block, if applied, is never followed by a put-back copy."""
    t, box, sw = world["t"], world["box"], world["sw"]
    prompt(t, "do the work")
    model(t, "msg_a")
    box.put(SESSION, owner_msg("the text", 51))
    stop_hook(world)
    write(t, {"type": "system", "subtype": "stop_hook_summary", "hookCount": 1})
    sw.clock.now = time.time() + 5
    sw.sweep()
    assert not box.pending(SESSION) and len(box.leases(SESSION)) == 1
    box.put(SESSION, owner_msg("the picture", 52))
    assert H.telegram_messages(SESSION, data=data(world), via="prompt") == []
    assert [i["id"] for i in box.peek(SESSION)] == [52]


# --- review of #228 ---------------------------------------------------------------------------------------------

def test_review1_proof_written_between_two_reads_is_never_a_put_back(world, monkeypatch):
    """The delivery proof and a new response land right after the sweeper's proof look: the decision to put back
    is made from one read, which sees the proof, so an already delivered message is never handed over twice."""
    t, box, sw = world["t"], world["box"], world["sw"]
    prompt(t, "do the work")
    model(t, "msg_a")
    box.put(SESSION, owner_msg("yes", 51))
    _, text = stop_hook(world)
    real_prove = SW.prove

    def prove_then_writer_appends(lease, session, index):
        state = real_prove(lease, session, index)
        blocking_error(t, text)  # the block reached the model ...
        stop_hooks_done(t, turn_end=False)
        model(t, "msg_b", "tool_use", "tool_b")  # ... and the continuation's model call followed
        tool_result(t, "tool_b")
        model(t, "msg_c")
        return state

    monkeypatch.setattr(SW, "prove", prove_then_writer_appends)
    sw.clock.now = time.time() + 5
    sw.sweep()
    monkeypatch.setattr(SW, "prove", real_prove)
    assert not box.pending(SESSION), "a delivered message was put back"
    assert H.telegram_messages(SESSION, data=data(world), via="prompt") == []


def test_review2_a_record_half_written_at_the_take_is_the_source_response(world):
    """The take happened while the response's first tool_use record was still being written: once complete it is
    the source response, so its next parallel tool_use is not a new model call."""
    t, box, sw = world["t"], world["box"], world["sw"]
    prompt(t, "do the work")
    line = json.dumps({"type": "assistant", "message": {"id": "msg_b", "role": "assistant", "model": "sample",
                       "content": [{"type": "tool_use", "id": "tool_a", "name": "Bash", "input": {}}]}}) + "\n"
    with open(t, "a", encoding="utf-8") as f:
        f.write(line[:40])  # half written
    box.put(SESSION, owner_msg("the text", 51))
    assert post_hook(world) is not None  # the first tool's hook took it, mid-record
    with open(t, "a", encoding="utf-8") as f:
        f.write(line[40:])
    tool_result(t, "tool_a")
    model(t, "msg_b", "tool_use", "tool_b")  # same response, second tool
    sw.clock.now = time.time() + 5
    sw.sweep()
    assert not box.pending(SESSION) and len(box.leases(SESSION)) == 1


def test_review2_an_unfinished_record_decides_nothing(world):
    t, box, sw = world["t"], world["box"], world["sw"]
    turn_ending_with_the_stop_hook_dropped(world, turn_end=False)
    prompt(t, "<task-notification>monitor event</task-notification>")
    with open(t, "a", encoding="utf-8") as f:
        f.write('{"type": "assistant", "message": {"id": "msg_c"')  # the new response, still being written
    sw.clock.now = time.time() + 5
    sw.sweep()
    assert not box.pending(SESSION)


def test_review3_a_delivery_in_claude_codes_queue_still_goes_first(world):
    """Seen enqueued, then the queue was handed over without it while events keep the session busy: a newer
    picture waits; it never overtakes the text (the text comes back once safely put back)."""
    t, box = world["t"], world["box"]
    prompt(t, "do the work")
    model(t, "msg_a")
    box.put(SESSION, owner_msg("the text", 51))
    msgs = box.take(SESSION, lease=H.delivery_lease(data(world), "wake"))
    write(t, {"type": "queue-operation", "operation": "enqueue", "content": H.delivery_text(msgs)})
    write(t, {"type": "queue-operation", "operation": "dequeue"})
    prompt(t, "<task-notification>monitor event</task-notification>")
    model(t, "msg_b", "tool_use", "tool_b")
    box.put(SESSION, owner_msg("the picture", 52))
    assert post_hook(world) is None
    assert [i["id"] for i in box.peek(SESSION)] == [52]


def test_review4_each_hook_reads_only_what_was_appended(world):
    """A delivery left undecided while the session works long: every hook used to re-read the transcript from the
    take point. The scan's progress is kept in the lease, so a second look reads nothing new."""
    t, box = world["t"], world["box"]
    turn_ending_with_the_stop_hook_dropped(world, turn_end=False)
    filler = "x" * 1000
    with open(t, "a", encoding="utf-8") as f:
        for n in range(3000):  # ~3 MB of records that decide nothing (progress notes, attachments)
            f.write(json.dumps({"type": "progress", "n": n, "data": filler}) + "\n")
    SW.SCAN_STATS["bytes"] = 0
    assert SW.before_take(box, SESSION) is True
    first = SW.SCAN_STATS["bytes"]
    assert first >= 3_000_000
    SW.SCAN_STATS["bytes"] = 0
    assert SW.before_take(box, SESSION) is True
    assert SW.SCAN_STATS["bytes"] < 10_000
    lease = box.leases(SESSION)[0]
    assert lease["scan"]["pos"] == t.stat().st_size


def test_review4_one_look_reads_a_bounded_amount(world, monkeypatch):
    t, box = world["t"], world["box"]
    turn_ending_with_the_stop_hook_dropped(world, turn_end=False)
    with open(t, "a", encoding="utf-8") as f:
        for n in range(2000):
            f.write(json.dumps({"type": "progress", "n": n, "data": "x" * 1000}) + "\n")
    monkeypatch.setattr(SW, "SCAN_BUDGET", 500_000)
    SW.SCAN_STATS["bytes"] = 0
    SW.before_take(box, SESSION)
    assert SW.SCAN_STATS["bytes"] <= 500_000


def test_review4_a_replaced_transcript_starts_the_scan_over(world):
    t, box = world["t"], world["box"]
    turn_ending_with_the_stop_hook_dropped(world, turn_end=False)
    assert SW.before_take(box, SESSION) is True
    t.write_text("")  # replaced (a /clear, a move): shorter than the saved progress
    assert SW.before_take(box, SESSION) is True  # nothing read twice, nothing decided from nothing
    assert not box.pending(SESSION)


# --- review of #228, round 2: the saved scan position is only an optimisation --------------------------------

def _scanned_once(world):
    """A dropped-looking Stop delivery whose scan progress was saved once (no decision yet)."""
    turn_ending_with_the_stop_hook_dropped(world, turn_end=False)
    assert SW.before_take(world["box"], SESSION) is True
    scan = world["box"].leases(SESSION)[0]["scan"]
    assert scan["pos"] == world["t"].stat().st_size and not scan.get("void")
    return world["box"].leases(SESSION)[0]["nonce"]


def _delivered_then_moved_on(world, nonce_text):
    t = world["t"]
    blocking_error(t, nonce_text)  # the proof, written where the shorter file ends ...
    with open(t, "a", encoding="utf-8") as f:  # ... then enough to grow past the saved position
        for n in range(20):
            f.write(json.dumps({"type": "progress", "n": n, "data": "y" * 400}) + "\n")
    model(t, "msg_x", "tool_use", "tool_x")
    tool_result(t, "tool_x")
    model(t, "msg_y", "tool_use", "tool_y")


def _check_never_twice(world):
    box = world["box"]
    assert SW.before_take(box, SESSION) is False  # delivered: nothing holds newer messages
    assert not box.pending(SESSION), "a delivered message was put back"
    world["sw"].clock.now = time.time() + 5
    world["sw"].sweep()
    assert not box.pending(SESSION) and box.leases(SESSION) == []
    assert world["bot"].reactions("👌") == [51]


def _stop_text(world):
    lease = world["box"].leases(SESSION)[0]
    return H.delivery_text([dict(i, _lease=lease["nonce"], _session=SESSION) for i in lease["items"]])


def test_round2_shortened_seen_short_then_proof_and_growth_is_never_dropped(world):
    """The reviewer's sequence: progress saved at the end, the same file shortened (and looked at while short), the
    proof written at the new end, the file grown past the old position: the stale position must not hide it."""
    t, box = world["t"], world["box"]
    _scanned_once(world)
    text = _stop_text(world)
    keep = t.read_bytes()[:200]
    with open(t, "r+b") as f:
        f.truncate(0)
        f.write(keep[:keep.rfind(b"\n") + 1])
    assert SW.before_take(box, SESSION) is True  # seen short: the scan is void, nothing decided
    assert box.leases(SESSION)[0]["scan"].get("void") is True
    _delivered_then_moved_on(world, text)
    _check_never_twice(world)


def test_round2_truncated_and_regrown_past_the_old_position_between_looks(world):
    t = world["t"]
    _scanned_once(world)
    text = _stop_text(world)
    with open(t, "r+b") as f:
        f.truncate(0)
    _delivered_then_moved_on(world, text)  # never looked at while short: only the fingerprint can tell
    _check_never_twice(world)


def test_round2_replaced_by_rename(world):
    t = world["t"]
    _scanned_once(world)
    text = _stop_text(world)
    other = t.with_name("t.new")
    other.write_bytes(t.read_bytes()[:100].rpartition(b"\n")[0] + b"\n")
    os.replace(other, t)
    _delivered_then_moved_on(world, text)
    _check_never_twice(world)


def test_round2_a_void_scan_never_reads_as_dropped(world):
    """Once void, the lease is only ever delivered (a fresh bounded search finds the proof) or pending: the old idle
    rule releases it, never the model-progress rule."""
    t, box = world["t"], world["box"]
    _scanned_once(world)
    with open(t, "r+b") as f:
        f.truncate(0)
    prompt(t, "<task-notification>monitor event</task-notification>")
    model(t, "msg_x", "tool_use", "tool_x")
    tool_result(t, "tool_x")
    model(t, "msg_y")
    stop_hooks_done(t)
    assert SW.before_take(box, SESSION) is True
    world["sw"].clock.now = time.time() + 5
    world["sw"].sweep()
    assert not box.pending(SESSION)
    assert box.leases(SESSION)[0]["scan"].get("void") is True
