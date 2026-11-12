"""PR 239: real queue/hook/receipt paths, with sample panes and transcripts only."""
import fcntl
import json
import threading
import time
from types import SimpleNamespace

import pytest

from ctb import cards, decisions, hooks, inbox as I, panel, panel_page, sweeper, wake
from ctb.config import Config, state_dir
from ctb.topics import TopicMap
from test_inbox import TextMedia, setup, update
from test_review_141 import Pane

A = "a1b2c3d4-0000-4000-8000-000000000001"
B = "e5f6a7b8-0000-4000-8000-000000000002"
REAL_THREAD = threading.Thread


@pytest.fixture
def flow(tmp_path, bot, monkeypatch, real_wake):
    cfg = Config(bot_token="sample", owner_chat_id=7)
    box = I.default_inbox()
    topics = TopicMap(state_dir() / "topics.json", bot, 7)
    for n, sid in enumerate((A, B)):
        topics.ensure(sid, "Sample question", thread=n + 10)
    rows = [SimpleNamespace(session=s, state="idle", closed=False, chat=None,
                            thread=n + 10, name="Sample", age=1) for n, s in enumerate((A, B))]
    monkeypatch.setattr(panel, "collect", lambda *a, **k: rows)
    monkeypatch.setattr(I, "main_working", lambda s: False)
    monkeypatch.setattr(I, "_closed", lambda s: False)
    monkeypatch.setattr(I, "_waiter_alive", lambda s: False)
    monkeypatch.setattr(I, "recently_moving", lambda s: False)
    monkeypatch.setattr(wake, "limit_active", lambda s: False)
    monkeypatch.setattr(hooks, "spawn_delivered", lambda items: None)
    monkeypatch.setattr(I.time, "sleep", lambda n: None)
    panes = {"%10": Pane(), "%11": Pane()}
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%10" if s == A else "%11")
    monkeypatch.setattr(I, "pane_runs_session", lambda p, s, **k: p == ("%10" if s == A else "%11"))
    monkeypatch.setattr(I, "_tmux", lambda *a: panes[a[a.index("-t") + 1]](*a))
    from ctb import autopilot
    monkeypatch.setattr(autopilot, "at_prompt", lambda p: True)
    monkeypatch.setattr(autopilot, "type_into", lambda p, t: I.typed_nudge(A if p == "%10" else B, p, t))

    class Inline:
        def __init__(self, target, args=(), kwargs=None, **kw):
            self.target, self.args, self.kwargs = target, args, kwargs or {}
        def start(self):
            self.target(*self.args, **self.kwargs)
    monkeypatch.setattr(I.threading, "Thread", Inline)
    transcripts = {s: tmp_path / (s + ".jsonl") for s in (A, B)}
    for path in transcripts.values():
        path.write_text("")
    return SimpleNamespace(cfg=cfg, box=box, bot=bot, panes=panes, t=transcripts, rows=rows)


def endpoint(f, method, body):
    results = []
    handler = SimpleNamespace(_request=lambda: body, _json=lambda code, data: results.append((code, data)),
                              _body=json.dumps(body).encode(),
                              server=SimpleNamespace(cfg=f.cfg, inbox=lambda: f.box, bot=lambda: f.bot))
    getattr(panel.PanelHandler, method)(handler)
    return results[0]


def card(f, sid=A):
    return cards.publish(sid, 7, 10 if sid == A else 11, "ask", "Sample question")


def decision(f, sid=A):
    return decisions.create(sid, 7, 10 if sid == A else 11, "Sample decision")


def take(f, sid=A, via="prompt"):
    return hooks.telegram_messages(sid, data={"transcript_path": str(f.t[sid])}, via=via)


def prove(f, sid, items):
    with f.t[sid].open("a") as out:
        out.write(json.dumps({"type": "user", "message": {"content": hooks.delivery_text(items)}}) + "\n")
    sw = sweeper.Sweeper(f.cfg, f.bot, f.box, workers=0)
    sw.sweep_session(sid)


@pytest.mark.parametrize("word", ["sample owner text", "stop"])
def test_full_panel_queue_never_raises_from_handle_or_blocks_the_next_update(tmp_path, bot, word):
    topics, box, thread = setup(tmp_path, bot)
    box.max_items = 1
    box.put("sess-1", {"source": "panel", "text": "protected answer"})
    msg = update(word, thread)
    if word == "stop":
        msg = {"update_id": 1, "callback_query": {"id": "sample", "data": "say:stop",
               "from": {"id": 7}, "message": msg["message"]}}
    cursor = 0
    # Same ordering as the poll loop: the cursor advances only after handle returns.
    assert I.handle(msg, Config(bot_token="sample", owner_chat_id=7), bot, topics, box, TextMedia(), 0) == "queued"
    cursor = msg["update_id"] + 1
    other_thread = topics.ensure("second-session", "Second", thread=22)
    assert I.handle(update("second", other_thread), Config(bot_token="sample", owner_chat_id=7),
                    bot, topics, box, TextMedia(), 0) == "queued"
    assert cursor == 2
    assert [m["text"] for m in box.peek("sess-1") if not m.get("dropped")] == ["protected answer"]
    assert box.peek("sess-1")[0]["dropped"] == 1
    assert box.peek("second-session")[0]["text"] == "second"


def test_put_back_over_capacity_does_not_invent_dropped_messages(tmp_path):
    box = I.Inbox(tmp_path / "inbox", max_items=1)
    box.put("s", {"source": "panel", "text": "first"})
    box.take("s", lease={"nonce": "a" * 16, "via": "wake"})
    box.put("s", {"source": "panel", "text": "second"})
    box.settle("s", "a" * 16, "lost")
    box.put("s", {"source": "owner", "text": "one discarded message"})
    assert box.peek("s")[0]["dropped"] == 1
    assert [i["text"] for i in box.peek("s")[1:]] == ["first", "second"]


@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("kind", ["say", "card", "decision"])
def test_idle_screen_without_waiter_gets_answer_even_with_stale_busy_mark(flow, monkeypatch, busy, kind):
    monkeypatch.setattr(I, "main_working", lambda s: busy)
    if kind == "say":
        code, out = endpoint(flow, "_say", {"session": A[:8], "text": "sample answer"})
    elif kind == "card":
        code, out = endpoint(flow, "_card", {"id": card(flow)["id"], "text": "sample answer"})
    else:
        code, out = endpoint(flow, "_decide", {"id": decision(flow)["id"], "choice": "yes"})
    assert code == 200
    assert "Enter" in flow.panes["%10"].keys
    items = take(flow)
    assert len(items) == 1 and items[0]["source"] == "panel"
    assert flow.box.peek(B) == []


def test_actually_busy_session_waits_for_its_hook_without_typing(flow, monkeypatch):
    monkeypatch.setattr(I, "main_working", lambda s: True)
    flow.panes["%10"].screen = "A tool is running\nEscape to interrupt"
    assert endpoint(flow, "_say", {"session": A[:8], "text": "next step"})[0] == 200
    assert flow.panes["%10"].keys == []
    assert len(take(flow, via="post")) == 1


@pytest.mark.parametrize("screen", ["Permission required", "Choose an answer", "Draft"])
def test_no_nudge_into_dialog_or_owner_draft(flow, screen):
    if screen == "Draft":
        flow.panes["%10"].screen = flow.panes["%10"].screen.replace("❯ \n", "❯ owner draft\n")
    else:
        flow.panes["%10"].screen = screen
    assert endpoint(flow, "_say", {"session": A[:8], "text": "keep safe"})[0] == 200
    assert flow.panes["%10"].keys == []
    assert flow.box.peek(A)


def test_two_sessions_answered_seconds_apart_each_nudge_and_consume_their_own_answer(flow, monkeypatch):
    monkeypatch.setattr(I, "main_working", lambda s: True)
    one = endpoint(flow, "_card", {"id": card(flow, A)["id"], "text": "answer A"})
    two = endpoint(flow, "_decide", {"id": decision(flow, B)["id"], "choice": "no"})
    assert one[0] == two[0] == 200
    assert all("Enter" in p.keys for p in flow.panes.values())
    a, b = take(flow, A), take(flow, B)
    assert "answer A" in a[0]["text"] and "answer A" not in b[0]["text"]
    prove(flow, B, b)
    assert flow.box.delivery_state(B, two[1]["key"]) == "delivered"
    assert flow.box.delivery_state(A, one[1]["key"]) == "in_flight"
    prove(flow, A, a)
    assert flow.box.delivery_state(A, one[1]["key"]) == "delivered"


@pytest.mark.parametrize("kind", ["say", "card", "decision"])
def test_wake_error_is_visible_and_sweeper_retries_the_saved_answer(flow, monkeypatch, kind):
    def broken(*a, **kw):
        raise OSError("sample failure; do not display arbitrary exception text")
    with monkeypatch.context() as m:
        m.setattr(I, "_deliver_button_word", broken)
        if kind == "say":
            code, out = endpoint(flow, "_say", {"session": A[:8], "text": "sample"})
        elif kind == "card":
            code, out = endpoint(flow, "_card", {"id": card(flow)["id"], "text": "sample"})
        else:
            code, out = endpoint(flow, "_decide", {"id": decision(flow)["id"], "choice": "yes"})
    assert code == 200
    receipt = next(r for r in panel.delivery_receipts(flow.box) if r["key"] == out["key"])
    assert receipt["state"] == "queued" and receipt["retrying"] is True
    assert "arbitrary exception" not in json.dumps(receipt)
    sw = sweeper.Sweeper(flow.cfg, flow.bot, flow.box, clock=lambda: time.time() + 30, workers=0)
    sw.sweep_session(A)
    assert "Enter" in flow.panes["%10"].keys
    items = take(flow)
    prove(flow, A, items)
    assert next(r for r in panel.delivery_receipts(flow.box) if r["key"] == out["key"])["state"] == "delivered"


@pytest.mark.parametrize("kind", ["say", "card", "decision"])
def test_receipt_changes_only_after_transcript_proof(flow, kind):
    if kind == "say":
        code, out = endpoint(flow, "_say", {"session": A[:8], "text": "sample"})
    elif kind == "card":
        code, out = endpoint(flow, "_card", {"id": card(flow)["id"], "text": "sample"})
    else:
        code, out = endpoint(flow, "_decide", {"id": decision(flow)["id"], "choice": "yes"})
    assert code == 200 and out["delivery"] == "queued"
    receipt = lambda: next(r for r in panel.delivery_receipts(flow.box) if r["key"] == out["key"])
    assert receipt()["state"] == "queued"
    items = take(flow)
    assert receipt()["state"] == "in_flight"
    if kind == "say":
        assert panel.waiting_words(A, time.time(), flow.box)[0]["state"] == "in_flight"
    # A quoted nonce in assistant output is not evidence that the answer reached the model.
    flow.t[A].write_text(json.dumps({"type": "assistant", "message": {"content": hooks.delivery_text(items)}}) + "\n")
    sweeper.Sweeper(flow.cfg, flow.bot, flow.box, workers=0).sweep_session(A)
    assert receipt()["state"] == "in_flight"
    prove(flow, A, items)
    assert receipt()["state"] == "delivered" and receipt()["retrying"] is False
    assert flow.box.leases(A) == []


def test_discarded_lease_visible_then_put_back_before_newer_answer(flow):
    first = endpoint(flow, "_say", {"session": A[:8], "text": "first"})[1]
    take(flow, via="wake")  # Claude Code discards this async output.
    second = endpoint(flow, "_say", {"session": A[:8], "text": "second"})[1]
    assert [w["state"] for w in panel.waiting_words(A, time.time(), flow.box)] == ["in_flight", "queued"]
    sw = sweeper.Sweeper(flow.cfg, flow.bot, flow.box,
                         clock=lambda: time.time() + sweeper.LOST_ABSENT + 1, workers=0)
    sw.sweep_session(A)
    assert flow.box.delivery_state(A, first["key"]) == "queued"
    assert [i["said"] for i in flow.box.peek(A)] == ["first", "second"]
    items = take(flow)
    assert len(items) == 2
    prove(flow, A, items)
    assert all(flow.box.delivery_state(A, out["key"]) == "delivered" for out in (first, second))


def test_retry_after_delivery_returns_delivered_without_a_second_copy(flow):
    body = {"session": A[:8], "text": "sample", "send": "ab" * 12}
    first = endpoint(flow, "_say", body)[1]
    prove(flow, A, take(flow))
    again = endpoint(flow, "_say", body)[1]
    assert again["key"] == first["key"] and again["delivery"] == "delivered"
    assert flow.box.peek(A) == [] and take(flow) == []


@pytest.mark.parametrize("kind", ["say", "card", "decision"])
def test_full_queue_is_actionable_conflict_and_question_stays_open(flow, kind):
    flow.box.max_items = 1
    flow.box.put(A, {"source": "panel", "text": "earlier"})
    if kind == "say":
        code, out = endpoint(flow, "_say", {"session": A[:8], "text": "sample"})
    elif kind == "card":
        rec = card(flow)
        code, out = endpoint(flow, "_card", {"id": rec["id"], "text": "sample"})
        assert cards.open_cards()[0]["state"] == "open"
    else:
        rec = decision(flow)
        code, out = endpoint(flow, "_decide", {"id": rec["id"], "choice": "yes"})
        assert decisions.open_decisions()[0]["state"] == "open"
    assert code == 409 and out["error"] == "queue full"


def test_new_ui_delivery_strings_are_single_language():
    for lang, strings in panel_page.STRINGS.items():
        for key in ("deliveryQueued", "deliveryFlight", "deliveryDone", "deliveryUnknown", "deliveryRetry", "queueFull"):
            value = strings[key]
            assert value
            if lang == "he":
                assert not any("a" <= c.lower() <= "z" for c in value)
            else:
                assert not any("\u0590" <= c <= "\u05ff" for c in value)


def test_terminal_wake_error_is_not_silently_discarded(flow, monkeypatch):
    original = I._tmux
    def broken(*a):
        if a[0] == "send-keys":
            raise OSError("sample terminal failure")
        return original(*a)
    monkeypatch.setattr(I, "_tmux", broken)
    out = endpoint(flow, "_say", {"session": A[:8], "text": "retry me"})[1]
    assert flow.box.wake_error(A)
    assert flow.box.delivery_state(A, out["key"]) == "queued"


def test_one_slow_session_never_serializes_another_sessions_nudge(flow, monkeypatch):
    entered, release, second_done = threading.Event(), threading.Event(), threading.Event()
    def type_nudge(sid, pane):
        if sid == A:
            entered.set()
            assert release.wait(2)
        else:
            second_done.set()
        return True
    monkeypatch.setattr(I, "typed_nudge", type_nudge)
    for sid in (A, B):
        flow.box.put(sid, {"source": "panel", "text": "sample"})
    one = REAL_THREAD(target=I.sweep_nudge, args=(flow.box, A))
    two = REAL_THREAD(target=I.sweep_nudge, args=(flow.box, B))
    one.start()
    try:
        assert entered.wait(1)
        two.start()
        independent = second_done.wait(0.5)
    finally:
        release.set()
        one.join(2)
        if two.ident is not None:
            two.join(2)
    assert not one.is_alive() and not two.is_alive()
    assert independent


def test_another_process_holding_one_sessions_lock_never_blocks_the_wake(flow):
    flow.box.put(A, {"source": "panel", "text": "sample"})
    # Keep the test bounded on the old blocking implementation as well.
    done = threading.Event()
    with open(str(I._nudge_path(A)) + ".lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        worker = REAL_THREAD(target=lambda: (I.sweep_nudge(flow.box, A), done.set()))
        worker.start()
        returned = done.wait(0.5)
        fcntl.flock(held, fcntl.LOCK_UN)
    worker.join(2)
    assert not worker.is_alive() and returned
    assert flow.box.peek(A)


def test_live_waiter_takes_answer_and_receipt_waits_for_proof(flow, monkeypatch):
    monkeypatch.setattr(I, "_waiter_alive", lambda sid: True)
    monkeypatch.setattr(wake, "cold_compact", lambda *a: False)
    out = endpoint(flow, "_say", {"session": A[:8], "text": "waiter answer"})[1]
    assert flow.panes["%10"].keys == []
    code, text = wake.wait_for_message({"session_id": A, "transcript_path": str(flow.t[A])},
                                        inbox=flow.box, settle=0, poll=0, max_wait=1)
    assert code == 2 and "waiter answer" in text
    assert flow.box.delivery_state(A, out["key"]) == "in_flight"
    flow.t[A].write_text(json.dumps({"type": "user", "message": {"content": text}}) + "\n")
    sweeper.Sweeper(flow.cfg, flow.bot, flow.box, workers=0).sweep_session(A)
    assert flow.box.delivery_state(A, out["key"]) == "delivered"


def test_state_poll_exposes_receipts_after_reload_without_consuming_or_waking(flow):
    out = endpoint(flow, "_card", {"id": card(flow)["id"], "text": "sample"})[1]
    items = take(flow)
    keys = list(flow.panes["%10"].keys)
    state = panel.build_state(flow.cfg)
    rec = next(r for r in state["deliveries"] if r["key"] == out["key"])
    assert rec["state"] == "in_flight"
    assert flow.box.leases(A) and flow.panes["%10"].keys == keys
    prove(flow, A, items)
    assert next(r for r in panel.build_state(flow.cfg)["deliveries"] if r["key"] == out["key"])["state"] == "delivered"


def test_missing_transcript_does_not_let_new_answers_overtake_a_lease(flow):
    endpoint(flow, "_say", {"session": A[:8], "text": "first"})
    take(flow, via="wake")
    flow.t[A].unlink()  # no transcript writer/proof available yet, and the taking hook has exited
    endpoint(flow, "_say", {"session": A[:8], "text": "second"})
    assert take(flow) == []
    assert [i["said"] for i in flow.box.peek(A)] == ["second"]
    # Only the sweeper's established idle/dead-output checks may release the old lease.
    sw = sweeper.Sweeper(flow.cfg, flow.bot, flow.box,
                         clock=lambda: time.time() + sweeper.LOST_ABSENT + 1, workers=0)
    sw.sweep_session(A)
    assert [i["said"] for i in flow.box.peek(A)] == ["first", "second"]
    assert len(take(flow)) == 2


@pytest.mark.parametrize("kind", ["say", "card", "decision"])
def test_optional_boundary_failure_is_logged_without_undoing_the_answer(flow, monkeypatch, kind):
    from ctb import progress
    def broken(*a, **kw):
        raise OSError("sample text never copied to the log")
    monkeypatch.setattr(progress, "append_boundary", broken)
    if kind == "say":
        result = endpoint(flow, "_say", {"session": A[:8], "text": "sample"})
    elif kind == "card":
        result = endpoint(flow, "_card", {"id": card(flow)["id"], "text": "sample"})
    else:
        result = endpoint(flow, "_decide", {"id": decision(flow)["id"], "choice": "yes"})
    assert result[0] == 200 and flow.box.peek(A)
    log = (state_dir() / "poll.log").read_text()
    assert "panel boundary" in log and "OSError" in log
    assert "sample text" not in log


def test_previously_accepted_panel_replay_is_not_consumed_and_lost_at_capacity(flow, monkeypatch):
    from ctb import replay
    monkeypatch.setattr(I, "default_inbox", lambda: flow.box)
    now = time.time()
    replay.record(A, [{"source": "panel", "text": "accepted before the limit", "at": now}], now=now)
    replay._update(A, lambda entries: entries[0].update(limited_at=now))
    flow.box.max_items = 1
    flow.box.put(A, {"source": "panel", "text": "another saved answer"})
    assert I._queue_replay(flow.cfg, flow.bot, A) == 1
    assert [i["text"] for i in flow.box.peek(A)] == ["another saved answer", "accepted before the limit"]
    assert replay.take_limited(A) == []  # removed only because it is now safely queued again


def test_a_later_answer_does_not_inherit_an_older_answers_wake_error(flow, monkeypatch):
    def broken(*a, **kw):
        raise OSError("sample")
    with monkeypatch.context() as m:
        m.setattr(I, "_deliver_button_word", broken)
        old = endpoint(flow, "_say", {"session": A[:8], "text": "old"})[1]
    prove(flow, A, take(flow))
    # The next answer has a working waiter and never suffered that earlier wake failure.
    monkeypatch.setattr(I, "_waiter_alive", lambda sid: True)
    new = endpoint(flow, "_say", {"session": A[:8], "text": "new"})[1]
    receipts = {r["key"]: r for r in panel.delivery_receipts(flow.box)}
    assert receipts[old["key"]]["state"] == "delivered"
    assert receipts[new["key"]]["state"] == "queued" and receipts[new["key"]]["retrying"] is False


@pytest.mark.parametrize("kind", ["say", "card", "decision"])
def test_missing_pane_records_waiting_then_retries_the_same_answer(flow, monkeypatch, kind):
    pane_for_session = I.pane_for_session
    monkeypatch.setattr(I, "pane_for_session", lambda sid: None)
    if kind == "say":
        code, out = endpoint(flow, "_say", {"session": A[:8], "text": "sample"})
    elif kind == "card":
        code, out = endpoint(flow, "_card", {"id": card(flow)["id"], "text": "sample"})
    else:
        code, out = endpoint(flow, "_decide", {"id": decision(flow)["id"], "choice": "yes"})
    assert code == 200
    receipt = next(r for r in panel.delivery_receipts(flow.box) if r["key"] == out["key"])
    assert receipt["state"] == "queued" and receipt["retrying"] is True
    assert I.default_inbox().wake_error(A, out["key"])
    assert flow.panes["%10"].keys == []
    sw = sweeper.Sweeper(flow.cfg, flow.bot, flow.box, clock=lambda: time.time() + 31, workers=0)
    monkeypatch.setattr(sw.probe, "alive", lambda sid: True)
    for _ in range(3):
        sw.sweep_session(A)
    assert [i["key"] for i in flow.box.peek(A)] == [out["key"]]
    assert flow.panes["%10"].keys == []
    monkeypatch.setattr(I, "pane_for_session", pane_for_session)
    sw.sweep_session(A)
    assert flow.panes["%10"].keys.count("Enter") == 1
    I._nudge_without_waiter(flow.box, A, settle=0)
    sw.sweep_session(A)
    assert flow.panes["%10"].keys.count("Enter") == 1
    items = take(flow)
    assert len(items) == 1
    assert [i["key"] for lease in flow.box.leases(A) for i in lease["items"]] == [out["key"]]
    prove(flow, A, items)
    sw.sweep_session(A)
    assert take(flow) == []
    assert flow.box.delivery_state(A, out["key"]) == "delivered"
    assert not flow.box.wake_error(A, out["key"])


@pytest.mark.parametrize("kind", ["say", "card", "decision"])
@pytest.mark.parametrize("state", ["queued", "in_flight", "unknown", "delivered"])
def test_only_delivered_receipts_expire_even_after_a_new_send(flow, kind, state):
    body = {"session": A[:8], "text": "sample", "send": "ab" * 12}
    if kind == "say":
        out = endpoint(flow, "_say", body)[1]
    elif kind == "card":
        out = endpoint(flow, "_card", {"id": card(flow)["id"], "text": "sample"})[1]
    else:
        out = endpoint(flow, "_decide", {"id": decision(flow)["id"], "choice": "yes"})[1]
    if state == "unknown":
        flow.box.take(A)
    elif state in ("in_flight", "delivered"):
        items = take(flow)
        if state == "delivered":
            prove(flow, A, items)
    assert flow.box.delivery_state(A, out["key"]) == state
    observed = time.time()
    panel.delivery_receipts(flow.box, now=observed)
    later = observed + panel.SENDS_KEPT + 1

    def check():
        receipts = {r["key"]: r for r in panel.delivery_receipts(flow.box, now=later)}
        if state == "delivered":
            assert out["key"] not in receipts
        else:
            assert receipts[out["key"]]["state"] == state

    check()
    panel.say_to(flow.cfg, flow.box, B[:8], "new sample", now=later, send="cd" * 12)
    check()
    if kind == "say" and state != "delivered":
        again = panel.say_to(flow.cfg, flow.box, A[:8], "sample", now=later, send=body["send"])
        assert again["again"] is True and again["key"] == out["key"]
        assert len([i for i in flow.box.peek(A) if i["key"] == out["key"]]) == (state == "queued")


def test_owner_cancel_hides_receipt_without_requeueing_a_late_retry(flow):
    send = "ab" * 12
    out = endpoint(flow, "_say", {"session": A[:8], "text": "sample", "send": send})[1]
    assert panel.unsay(flow.cfg, flow.box, A[:8], out["key"]) == "sample"
    assert flow.box.delivery_state(A, out["key"]) == "unknown"
    assert not any(r["key"] == out["key"] for r in panel.delivery_receipts(flow.box))
    saved = json.loads((state_dir() / panel.SENDS_FILE).read_text())
    assert saved[send]["final_state"] == "cancelled"
    later = saved[send]["final_at"] + panel.SENDS_KEPT + 1
    panel.say_to(flow.cfg, flow.box, B[:8], "new sample", now=later, send="cd" * 12)
    saved = json.loads((state_dir() / panel.SENDS_FILE).read_text())
    assert send not in saved
    assert flow.box.peek(A) == [] and take(flow) == []
    assert not any(r["key"] == out["key"] for r in panel.delivery_receipts(flow.box, now=later))


@pytest.mark.parametrize("kind", ["say", "card", "decision"])
def test_final_delivery_survives_lost_proof_and_expires(flow, kind):
    if kind == "say":
        out = endpoint(flow, "_say", {"session": A[:8], "text": "sample", "send": "ab" * 12})[1]
    elif kind == "card":
        out = endpoint(flow, "_card", {"id": card(flow)["id"], "text": "sample"})[1]
    else:
        out = endpoint(flow, "_decide", {"id": decision(flow)["id"], "choice": "yes"})[1]
    prove(flow, A, take(flow))
    now = time.time()
    assert next(r for r in panel.delivery_receipts(flow.box, now=now) if r["key"] == out["key"])["state"] == "delivered"
    flow.box._acked_path(A).write_text(json.dumps([f"sample-{i}" for i in range(500)]))
    assert flow.box.delivery_state(A, out["key"]) == "unknown"
    assert next(r for r in panel.delivery_receipts(flow.box, now=now + 1) if r["key"] == out["key"])["state"] == "delivered"
    assert not any(r["key"] == out["key"] for r in panel.delivery_receipts(flow.box, now=now + panel.SENDS_KEPT))


def test_old_pending_receipt_is_shown_before_final_receipts(flow, monkeypatch):
    now = time.time()
    sends = {str(i): {"session": A, "key": str(i), "at": now - 100 + i,
                      "final_state": "delivered", "final_at": now} for i in range(60)}
    sends["pending"] = {"session": A, "key": "pending", "at": now - 1000}
    (state_dir() / panel.SENDS_FILE).write_text(json.dumps(sends))
    monkeypatch.setattr(flow.box, "delivery_state", lambda *args: "unknown")
    receipts = panel.delivery_receipts(flow.box, now=now)
    assert len(receipts) == 60
    assert receipts[0]["key"] == "pending"
