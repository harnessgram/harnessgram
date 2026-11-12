"""Synthetic rollout fixtures: field shapes match local records, content is invented."""
import errno
import io
import json
import os
from pathlib import Path
import subprocess
from concurrent.futures import ThreadPoolExecutor
import time
from unittest.mock import Mock, call

import pytest

from ctb import harness as H
from ctb.config import Config, ConfigError, from_dict
from ctb.harness_codex import (CodexHarness, QueueSendError, MAX_META_BYTES,
                               MAX_RECORD_BYTES, MAX_QUEUE_BYTES, RECENT_EVENTS)

SID = "00000000-0000-4000-8000-000000000001"
CHILD = "00000000-0000-4000-8000-000000000002"
REF = H.SessionRef("codex", "default", SID)


def row(kind, **payload):
    return {"timestamp": "sample-time", "type": kind, "payload": payload}


def start(turn="turn-a"):
    return row("event_msg", type="task_started", turn_id=turn,
               turn_attribution={"turn_id": turn, "root_turn_id": turn,
                                 "parent_turn_id": None, "initiating_agent_path": None})


def message(text="Example reply", *, phase="final_answer", turn="turn-a", item="message-a", **extra):
    return row("response_item", type="message", id=item, role="assistant", phase=phase,
               content=[{"type": "output_text", "text": text, "annotations": [], "logprobs": []}],
               internal_chat_message_metadata_passthrough={"turn_id": turn, "create_time": 1.0,
                                                          "content_item_kinds": []}, **extra)


def encode(rows):
    return b"".join((json.dumps(r, ensure_ascii=False) + "\n").encode() for r in rows)


@pytest.fixture
def rollout(tmp_path):
    home = tmp_path / "codex-home"
    root = home / "sessions" / "sample"
    root.mkdir(parents=True)
    path = root / "rollout-sample.jsonl"
    meta = row("session_meta", id=SID, session_id=SID, cwd="/example/project", cli_version="0.163.0",
               source="cli", base_instructions={"text": "Internal sample instruction"})
    path.write_bytes(encode([meta]))
    adapter = CodexHarness(codex_home=home, attached=[SID], commentary=True)
    return adapter, path, meta


def append(path, *rows):
    with path.open("ab") as f:
        f.write(encode(rows))


def replies(events):
    return [e for e in events if isinstance(e, H.AssistantText)]


def test_native_discovery_does_not_attach_or_claim_liveness(rollout):
    adapter, path, meta = rollout
    observer = CodexHarness(sessions_dir=adapter.root)
    sessions = list(observer.discover())
    assert sessions == [H.Session(REF, title=SID, cwd="/example/project", liveness_provenance="persisted-rollout")]
    assert sessions[0].alive is None
    with pytest.raises(H.UnsupportedOperation):
        list(observer.events(REF, replay=True))
    # Filename is not authority, metadata is. Compressed archives are not parsed.
    path.with_name("rollout-other.jsonl.zst").write_bytes(b"not JSON")
    path.write_bytes(encode([dict(meta, payload=dict(meta["payload"], id="short"))]))
    assert list(observer.discover()) == []


def test_final_commentary_tools_and_private_records_are_distinct(rollout):
    adapter, path, _ = rollout
    append(path, start(), row("turn_context", turn_id="turn-a", model="example-model", effort="high"),
           message("Progress", phase="commentary", item="comment-a"),
           message("Hidden reasoning", phase="analysis", item="reason-a"),
           row("response_item", type="reasoning", summary=[{"text": "Hidden summary"}], encrypted_content="hidden"),
           row("response_item", type="message", role="developer", phase="final_answer",
               content=[{"type": "output_text", "text": "Hidden instruction"}]),
           row("response_item", type="function_call", id="call-item", call_id="call-a", name="example_tool",
               arguments='{"text":"Private tool argument"}'),
           row("response_item", type="function_call_output", call_id="call-a", output="Private tool output"),
           message(), row("event_msg", type="task_complete", turn_id="turn-a", last_agent_message="Example reply"))
    events = list(adapter.events(REF, replay=True))
    assert [(e.text, e.phase, e.final) for e in replies(events)] == [
        ("Progress", "commentary", False), ("Example reply", "final", True)]
    assert [(e.tool, e.state) for e in events if isinstance(e, H.ToolProgress)] == [
        ("example_tool", "started"), ("example_tool", "completed")]
    assert any(isinstance(e, H.Usage) and e.model == "example-model" for e in events)
    assert isinstance(events[-1], H.TurnEnded) and events[-1].outcome == "completed"
    assert "Hidden" not in repr(events) and "Private tool" not in repr(events)


def test_cursor_restart_and_each_individual_event_are_replay_safe(rollout):
    adapter, path, _ = rollout
    append(path, start(), message(), row("event_msg", type="task_complete", turn_id="turn-a"))
    events = list(adapter.events(REF, replay=True))
    for index, event in enumerate(events):
        fresh = CodexHarness(codex_home=adapter.home, attached=[SID], commentary=True)
        rest = list(fresh.events(REF, event.cursor))
        assert rest == events[index + 1:]
    assert list(adapter.events(REF, events[-1].cursor)) == []
    assert "Example reply" not in events[-1].cursor


def test_partial_utf8_record_is_not_consumed(rollout):
    adapter, path, _ = rollout
    append(path, start())
    cursor = list(adapter.events(REF, replay=True))[-1].cursor
    raw = encode([message("שלום")])
    boundary = raw.index("ש".encode()) + 1
    with path.open("ab") as f:
        f.write(raw[:boundary])
    assert list(adapter.events(REF, cursor)) == []
    with path.open("ab") as f:
        f.write(raw[boundary:])
    result = list(adapter.events(REF, cursor))
    assert [e.text for e in replies(result)] == ["שלום"]
    assert list(adapter.events(REF, result[-1].cursor)) == []


@pytest.mark.parametrize("replace_inode", [False, True])
def test_replacement_replays_context_without_duplicate_replies(rollout, replace_inode):
    adapter, path, meta = rollout
    append(path, start(), message())
    cursor = list(adapter.events(REF, replay=True))[-1].cursor
    old = message()
    old["timestamp"] = "rewritten-time"
    replacement = encode([meta, start(), old, message("New reply", item="message-b")])
    if replace_inode:
        other = path.with_suffix(".replacement")
        other.write_bytes(replacement)
        other.replace(path)
    else:
        path.write_bytes(replacement)
    assert [e.text for e in replies(adapter.events(REF, cursor))] == ["New reply"]


def test_truncation_to_new_turn_does_not_replay_old_answer(rollout):
    adapter, path, meta = rollout
    append(path, start(), message("Old answer " * 50))
    cursor = list(adapter.events(REF, replay=True))[-1].cursor
    path.write_bytes(encode([meta, start("turn-b"), message("New", turn="turn-b", item="new")]))
    assert [e.text for e in replies(adapter.events(REF, cursor))] == ["New"]


def test_late_older_turn_completion_cannot_end_new_turn(rollout):
    adapter, path, _ = rollout
    append(path, start(), start("turn-b"),
           row("event_msg", type="task_complete", turn_id="turn-a", last_agent_message="Late summary"),
           message("Late response", turn="turn-a"),
           message("Current", turn="turn-b", item="current"),
           row("event_msg", type="task_failed", turn_id="turn-b", message="Private failure details"))
    events = list(adapter.events(REF, replay=True))
    assert [e.text for e in replies(events)] == ["Current"]
    assert [(e.turn_id, e.outcome) for e in events if isinstance(e, H.TurnEnded)] == [("turn-b", "error")]


def test_usage_is_provider_specific_and_unknown_stays_unknown(rollout):
    adapter, path, _ = rollout
    append(path, start(), row("event_msg", type="token_count", info=None),
           row("event_msg", type="token_count", info={"last_token_usage": {
               "input_tokens": 100, "output_tokens": 7, "cached_input_tokens": 80, "total_tokens": 107},
               "model_context_window": 1000}))
    usage = [e for e in adapter.events(REF, replay=True) if isinstance(e, H.Usage)]
    assert usage[0].input_tokens is None and usage[0].context_tokens is None
    assert (usage[1].input_tokens, usage[1].output_tokens, usage[1].context_tokens) == (100, 7, 107)
    assert usage[1].model is None


@pytest.mark.parametrize("ref", [H.SessionRef("claude", "default", SID), H.SessionRef("codex", "work", SID),
                                 H.SessionRef("codex", "default", SID[:8])])
def test_exact_harness_profile_and_id_required(rollout, ref):
    adapter, _, _ = rollout
    with pytest.raises((ValueError, H.UnsupportedOperation)):
        list(adapter.events(ref))


def test_cursor_cannot_cross_profile_or_harness(rollout):
    adapter, path, _ = rollout
    append(path, start())
    cursor = list(adapter.events(REF, replay=True))[-1].cursor
    other = CodexHarness(codex_home=adapter.home, profile="work", attached=[SID])
    with pytest.raises(ValueError):
        list(other.events(H.SessionRef("codex", "work", SID), cursor))


@pytest.mark.parametrize("cursor", ["bad", "null", "[]", "{}", '{"v":2}'])
def test_bad_cursor_refused(rollout, cursor):
    with pytest.raises(ValueError):
        list(rollout[0].events(REF, cursor))


def test_child_messages_cannot_emit_parent_replies_or_touch_inbox(rollout, monkeypatch):
    from ctb.inbox import Inbox
    forbidden = Mock(side_effect=AssertionError("child touched inbox"))
    monkeypatch.setattr(Inbox, "take", forbidden)
    adapter, path, _ = rollout
    append(path, start(), dict(message("Child response"), payload=dict(message()["payload"], thread_id=CHILD)),
           row("event_msg", type="task_complete", thread_id=CHILD, turn_id="turn-a"),
           row("event_msg", type="item_completed", thread_id=SID, turn_id="turn-a",
               item={"type": "SubAgentActivity", "agent_thread_id": CHILD}),
           row("response_item", type="agent_message", author="worker", recipient="parent", content=[]),
           message("Parent response"))
    assert [e.text for e in replies(adapter.events(REF, replay=True))] == ["Parent response"]
    forbidden.assert_not_called()


@pytest.mark.parametrize("action", list(H.Control))
def test_stale_disconnected_and_all_native_controls_refused(rollout, action):
    adapter, path, _ = rollout
    path.unlink()  # no connected transport; persisted request IDs confer no authority
    with pytest.raises(H.UnsupportedOperation):
        adapter.control(REF, H.ControlCommand(action, request_id="old-request", turn_id="old-turn", text="yes"))


def test_mirror_off_commentary_off_and_sanitization(rollout):
    adapter, path, _ = rollout
    append(path, start(), message("Progress", phase="commentary"),
           message("password: example-secret", item="final"))
    safe = CodexHarness(codex_home=adapter.home, attached=[SID])
    texts = replies(safe.events(REF, replay=True))
    assert len(texts) == 1 and "example-secret" not in texts[0].text
    off = CodexHarness(codex_home=adapter.home, attached=[SID], mirror=False, commentary=True)
    assert replies(off.events(REF, replay=True)) == []


@pytest.fixture
def native_queue(rollout, monkeypatch):
    adapter, _, _ = rollout
    monkeypatch.setattr("ctb.harness_codex.shutil.which", lambda name: "/example/bin/codex")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, b"--thread <THREAD> --message <TEXT>", b""))
    monkeypatch.setattr("ctb.harness_codex.subprocess.run", run)
    assert adapter.capabilities().send
    run.reset_mock()
    return adapter, run


@pytest.mark.parametrize("origin", [{"source": "owner", "typed": True}, {"source": "team", "name": "Example"},
                                  {"voice": True}, {"button": "yes"}, {"forwarded": True}])
def test_native_queue_preserves_provenance_but_acceptance_is_not_delivery(native_queue, origin):
    from ctb import framing
    adapter, run = native_queue
    receipt = adapter.send(REF, dict(origin, text="Example input", key="example-key"))
    args = run.call_args.args[0]
    assert args[:4] == ["/example/bin/codex", "queue", "--thread", SID]
    text = args[5]
    assert "Example input" in text and framing.OWNER_YES not in text
    assert framing.NEVER_APPROVAL in text
    if origin.get("forwarded"):
        assert framing.FORWARDED_HEADER in text
    if origin.get("source") == "team":
        assert "SUGGESTION" in text
    if origin.get("voice"):
        assert framing.VOICE_MARK in text
    if origin.get("button"):
        assert framing.BUTTON_MARK in text
    assert receipt.state == H.ReceiptState.ACCEPTED and not receipt.delivered
    assert receipt.message_id == "example-key"
    assert run.call_args.kwargs["env"]["CODEX_HOME"] == str(adapter.home)


@pytest.mark.parametrize("failure", [1, subprocess.TimeoutExpired("codex", 20), OSError("example error")])
def test_failed_or_ambiguous_queue_never_claims_delivery_or_retries(native_queue, failure):
    adapter, run = native_queue
    if isinstance(failure, int):
        run.return_value = subprocess.CompletedProcess([], failure, b"private stdout", b"private stderr")
    else:
        run.side_effect = failure
    with pytest.raises(QueueSendError) as error:
        adapter.send(REF, {"text": "Example"})
    assert "private" not in str(error.value)
    assert run.call_count == 1


def test_missing_native_surface_fails_closed_without_exec_or_keystrokes(rollout, monkeypatch):
    adapter, _, _ = rollout
    monkeypatch.setattr("ctb.harness_codex.shutil.which", lambda name: None)
    assert not adapter.capabilities().send
    with pytest.raises(H.UnsupportedOperation):
        adapter.send(REF, {"text": "Example"})


def test_unverified_help_is_not_a_queue_surface(rollout, monkeypatch):
    adapter, _, _ = rollout
    monkeypatch.setattr("ctb.harness_codex.shutil.which", lambda name: "/example/bin/codex")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, b"exec resume", b""))
    monkeypatch.setattr("ctb.harness_codex.subprocess.run", run)
    assert not adapter.capabilities().send
    assert not adapter.capabilities().send
    assert run.call_count == 1


@pytest.mark.parametrize("item", [{"text": "Example", "hold": True}, {"text": "yes", "source": "panel"},
                                {"text": ""}])
def test_unsupported_queue_semantics_cannot_fall_back(native_queue, item):
    adapter, run = native_queue
    with pytest.raises((ValueError, H.UnsupportedOperation)):
        adapter.send(REF, item)
    run.assert_not_called()


def test_opt_in_registry_and_config_are_strict(rollout):
    adapter, _, _ = rollout
    with pytest.raises(H.UnsupportedOperation):
        H.registry(cfg=Config()).resolve(REF)
    cfg = from_dict({"codex_harness_enabled": True, "codex_sessions_dir": str(adapter.root),
                     "codex_attached_sessions": [SID], "mirror": False})
    registered, ref = H.registry(cfg=cfg).resolve(REF)
    assert isinstance(registered, H.Harness) and ref == REF
    assert not registered.mirror and not registered.commentary
    assert not registered.capabilities().send  # quota directory is not a queue-home binding
    for raw in ({"codex_harness_enabled": "true"}, {"codex_attached_sessions": [123]},
                {"codex_commentary": 1}):
        with pytest.raises(ConfigError):
            from_dict(raw)


def test_unknown_schema_and_malformed_whole_records_do_not_leak(rollout):
    adapter, path, _ = rollout
    append(path, start())
    with path.open("ab") as f:
        f.write(b"not-json\n[]\n{\"payload\":null}\n")
    append(path, row("unknown", text="Private"), message("Valid"))
    assert [e.text for e in replies(adapter.events(REF, replay=True))] == ["Valid"]


def test_partial_line_without_newline_is_not_a_complete_reply(rollout):
    adapter, path, _ = rollout
    append(path, start())
    cursor = list(adapter.events(REF, replay=True))[-1].cursor
    with path.open("ab") as f:
        f.write(encode([message()]).rstrip(b"\n"))
    assert list(adapter.events(REF, cursor)) == []
    with path.open("ab") as f:
        f.write(b"\n")
    assert len(replies(adapter.events(REF, cursor))) == 1


def test_foreign_replacement_and_missing_session_fail_closed(rollout):
    adapter, path, meta = rollout
    append(path, start(), message())
    cursor = list(adapter.events(REF, replay=True))[-1].cursor
    path.write_bytes(encode([dict(meta, payload=dict(meta["payload"], id=CHILD, session_id=CHILD)), start(), message()]))
    with pytest.raises(H.UnsupportedOperation):
        list(adapter.events(REF, cursor))
    with pytest.raises(H.UnsupportedOperation):
        adapter.send(REF, {"text": "Example"})


def test_symlink_outside_session_root_is_not_discovered(rollout, tmp_path):
    adapter, path, meta = rollout
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(encode([meta, start(), message()]))
    path.unlink()
    path.symlink_to(outside)
    assert list(adapter.discover()) == []


def test_custom_tool_pairs_and_late_usage(rollout):
    adapter, path, _ = rollout
    append(path, start(), row("turn_context", turn_id="turn-a", model="old-model"),
           start("turn-b"), row("turn_context", turn_id="turn-b", model="new-model"),
           row("turn_context", turn_id="turn-a", model="old-model"),
           row("event_msg", type="token_count", turn_id="turn-a", info={"last_token_usage": {"total_tokens": 9}}),
           row("response_item", type="custom_tool_call", call_id="custom", name="example_patch", input="Private input"),
           row("response_item", type="custom_tool_call_output", call_id="custom", output=["Private output"]))
    events = list(adapter.events(REF, replay=True))
    assert [e.model for e in events if isinstance(e, H.Usage)] == ["old-model", "new-model"]
    assert [(e.tool, e.state) for e in events if isinstance(e, H.ToolProgress)] == [
        ("example_patch", "started"), ("example_patch", "completed")]


def test_profile_queue_is_bound_to_explicit_home(rollout, monkeypatch):
    adapter, _, _ = rollout
    other = CodexHarness(codex_home=adapter.home, profile="work", attached=[SID])
    monkeypatch.setattr("ctb.harness_codex.shutil.which", lambda name: "/example/bin/codex")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, b"--thread <THREAD> --message <TEXT>", b""))
    monkeypatch.setattr("ctb.harness_codex.subprocess.run", run)
    receipt = other.send(H.SessionRef("codex", "work", SID), {"text": "Example"})
    assert run.call_args.args[0][-1] == "--profile=work"
    assert receipt.ref.profile == "work" and not receipt.delivered


def test_first_attachment_starts_now_and_remembers_an_idle_attachment(rollout):
    adapter, path, _ = rollout
    append(path, start(), message("Historical answer"))
    assert list(adapter.events(REF)) == []
    append(path, message("New answer", item="new"))
    assert [e.text for e in replies(adapter.events(REF))] == ["New answer"]


def test_initial_cursor_survives_restart_and_replay_is_explicit(rollout):
    adapter, path, _ = rollout
    append(path, start(), message("Historical answer"))
    cursor = adapter.initial_cursor(REF)
    assert json.loads(cursor)["offset"] == path.stat().st_size
    append(path, message("New answer", item="new"))
    fresh = CodexHarness(codex_home=adapter.home, attached=[SID])
    assert [e.text for e in replies(fresh.events(REF, cursor))] == ["New answer"]
    assert [e.text for e in replies(fresh.events(REF, replay=True))] == ["Historical answer", "New answer"]
    beginning = fresh.initial_cursor(REF, replay=True)
    assert json.loads(beginning)["offset"] == 0
    assert len(replies(fresh.events(REF, beginning))) == 2


def test_five_thousand_events_have_bounded_cursor_and_linear_cost(rollout):
    adapter, path, _ = rollout
    rows = []
    for n in range(1000):
        turn = f"turn-{n}"
        rows.extend([start(turn), row("turn_context", turn_id=turn, model="example"),
                     row("response_item", type="function_call", call_id=f"call-{n}", name="example"),
                     message("Example reply", turn=turn, item=f"message-{n}"),
                     row("event_msg", type="task_complete", turn_id=turn)])
    append(path, *rows)
    before = time.perf_counter()
    events = list(adapter.events(REF, replay=True))
    elapsed = time.perf_counter() - before
    assert len(events) == 5000
    assert elapsed < 0.75, f"5000 events took {elapsed:.3f}s"
    assert max(len(e.cursor.encode()) for e in events) < 1024
    assert not {"seen", "tools", "model", "active"}.intersection(json.loads(events[-1].cursor))
    assert len(adapter._contexts[REF][1]["seen"]) <= RECENT_EVENTS


def test_recent_duplicate_records_are_suppressed_after_cold_restart(rollout):
    adapter, path, _ = rollout
    append(path, start(), message(), message())
    events = list(adapter.events(REF, replay=True))
    assert len(replies(events)) == 1
    fresh = CodexHarness(codex_home=adapter.home, attached=[SID])
    assert list(fresh.events(REF, events[-1].cursor)) == []


def test_ignored_tail_cannot_change_cached_cursor_context(rollout):
    adapter, path, _ = rollout
    append(path, start(), message(), row("unknown", text="Ignored"))
    cursor = list(adapter.events(REF, replay=True))[-1].cursor
    append(path, message("New", item="new"))
    result = list(adapter.events(REF, cursor))
    fresh = CodexHarness(codex_home=adapter.home, attached=[SID], commentary=True)
    assert result == list(fresh.events(REF, cursor))


def test_missing_replacement_boundary_suppresses_ambiguous_retained_turn(rollout):
    adapter, path, meta = rollout
    append(path, start(), message())
    cursor = list(adapter.events(REF, replay=True))[-1].cursor
    path.write_bytes(encode([meta, start(), message("Uncertain", item="changed"),
                            start("turn-b"), message("New", turn="turn-b", item="new")]))
    assert [e.text for e in replies(adapter.events(REF, cursor))] == ["New"]


def test_replacement_does_not_replay_older_turns_on_cold_restart(rollout):
    adapter, path, meta = rollout
    append(path, start(), message(), start("turn-b"), message("Latest", turn="turn-b", item="latest"))
    cursor = list(adapter.events(REF, replay=True))[-1].cursor
    path.write_bytes(encode([meta, start(), message(), start("turn-b"),
                            message("Latest", turn="turn-b", item="latest"),
                            message("New", turn="turn-b", item="new")]))
    fresh = CodexHarness(codex_home=adapter.home, attached=[SID])
    assert [e.text for e in replies(fresh.events(REF, cursor))] == ["New"]


def test_queue_fence_is_random_and_independent_of_receipt_key(native_queue, monkeypatch):
    adapter, run = native_queue
    nonce = Mock(side_effect=["0123456789abcdef", "fedcba9876543210"])
    monkeypatch.setattr("ctb.harness_codex.secrets.token_hex", nonce)
    for expected in ("0123456789abcdef", "fedcba9876543210"):
        receipt = adapter.send(REF, {"text": "Example input", "key": "fixed-key"})
        framed = run.call_args.args[0][5]
        assert expected in framed and "fixed-key" not in framed
        assert receipt.message_id == "fixed-key"
    assert nonce.call_args_list == [call(8), call(8)]


@pytest.mark.parametrize("text", ["a" * MAX_QUEUE_BYTES, "ש" * (MAX_QUEUE_BYTES // 2), "bad\0text"])
def test_queue_rejects_oversized_or_invalid_framed_text_before_running(native_queue, text):
    adapter, run = native_queue
    with pytest.raises(QueueSendError, match="rejected"):
        adapter.send(REF, {"text": text})
    run.assert_not_called()


def test_queue_e2big_is_definite_rejection_without_retry(native_queue):
    adapter, run = native_queue
    run.side_effect = OSError(errno.E2BIG, "example")
    with pytest.raises(QueueSendError, match="rejected") as exc:
        adapter.send(REF, {"text": "Example"})
    assert "unknown" not in str(exc.value)
    assert run.call_count == 1


def test_open_checks_descriptor_and_flags_during_symlink_race(rollout, tmp_path, monkeypatch):
    adapter, path, meta = rollout
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(encode([meta]))
    real_open = os.open

    def swap_then_open(filename, flags):
        assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
        path.unlink()
        path.symlink_to(outside)
        return real_open(filename, flags)

    monkeypatch.setattr("ctb.harness_codex.os.open", swap_then_open)
    assert list(adapter.discover()) == []


def test_fifo_is_rejected_without_blocking_or_metadata_read(rollout, monkeypatch):
    adapter, path, _ = rollout
    path.unlink()
    os.mkfifo(path)
    original = Path.open

    def refuse_blocking_open(self, *args, **kwargs):
        if self == path:
            pytest.fail("FIFO must use a nonblocking descriptor open")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse_blocking_open)
    meta = Mock(side_effect=AssertionError("Nonregular file reached metadata parser"))
    monkeypatch.setattr(adapter, "_meta", meta)
    assert list(adapter.discover()) == []
    with pytest.raises(H.UnsupportedOperation):
        adapter.initial_cursor(REF)
    meta.assert_not_called()


def test_metadata_is_cached_by_inode_across_appends_and_reloaded_on_replacement(rollout, monkeypatch):
    adapter, path, _ = rollout
    meta = Mock(wraps=adapter._meta)
    monkeypatch.setattr(adapter, "_meta", meta)
    for _ in range(3):
        assert len(list(adapter.discover())) == 1
        with adapter._open(REF):
            pass
    assert meta.call_count == 1
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000))
    assert len(list(adapter.discover())) == 1
    append(path, row("unknown"))
    assert len(list(adapter.discover())) == 1
    assert meta.call_count == 1
    other = path.with_suffix(".new")
    other.write_bytes(path.read_bytes())
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000))
    other.replace(path)
    assert len(list(adapter.discover())) == 1
    assert meta.call_count == 2


def test_metadata_and_event_reads_are_capped_and_oversized_lines_are_skipped(rollout):
    class BoundedRead(io.BytesIO):
        def readline(self, size=-1):
            assert 0 < size <= MAX_RECORD_BYTES + 1
            return super().readline(size)

    oversized_meta = encode([row("session_meta", id=SID, text="x" * MAX_META_BYTES)])
    assert CodexHarness._meta(BoundedRead(oversized_meta)) == {}
    adapter, path, _ = rollout
    append(path, start(), message("x" * MAX_RECORD_BYTES), message("Safe", item="safe"))
    assert [e.text for e in replies(adapter.events(REF, replay=True))] == ["Safe"]
    stream = BoundedRead(b"x" * (MAX_RECORD_BYTES * 2) + b"\n" + encode([start()]))
    assert len(list(adapter._records(stream))) == 2


@pytest.mark.parametrize("bad_id", ["not-a-uuid", SID.upper().replace("0001", "ABC1")])
def test_registry_disables_codex_on_invalid_attached_id_without_logging_input(bad_id, caplog):
    cfg = from_dict({"codex_harness_enabled": True, "codex_attached_sessions": [SID, bad_id]})
    registry = H.registry(cfg=cfg)
    with pytest.raises(H.UnsupportedOperation):
        registry.resolve(REF)
    assert registry.resolve(H.SessionRef("claude", "default", "example"))
    assert "Codex disabled" in caplog.text and bad_id not in caplog.text


def test_context_snapshot_avoids_rebuilding_after_ignored_tail(rollout, monkeypatch):
    adapter, path, _ = rollout
    append(path, start(), message(), row("unknown", text="ignored"))
    cursor = list(adapter.events(REF, replay=True))[-1].cursor
    assert adapter._contexts[REF][1]["offset"] == json.loads(cursor)["offset"]
    append(path, message("Next", item="next"))
    consume = Mock(wraps=adapter._consume)
    monkeypatch.setattr(adapter, "_consume", consume)
    assert [event.text for event in replies(adapter.events(REF, cursor))] == ["Next"]
    assert consume.call_count == 2


def test_implicit_cursor_advances_after_each_delivered_event(rollout):
    adapter, path, _ = rollout
    append(path, start())
    assert list(adapter.events(REF)) == []
    append(path, message("First"), message("Second", item="second"))
    events = iter(adapter.events(REF))
    assert next(events).text == "First"
    events.close()
    assert [event.text for event in replies(adapter.events(REF))] == ["Second"]
    assert list(adapter.events(REF)) == []


def test_path_cache_prefers_uuid_and_revalidates_replacement(rollout, monkeypatch):
    adapter, path, meta = rollout
    target = path.with_name(f"rollout-sample-{SID}.jsonl")
    path.rename(target)
    other = path.with_name("rollout-000-other.jsonl")
    other.write_bytes(encode([row("session_meta", id=CHILD)]))
    opened = Mock(wraps=adapter._open_file)
    monkeypatch.setattr(adapter, "_open_file", opened)
    with adapter._open(REF):
        pass
    assert [call.args[0] for call in opened.call_args_list] == [target]
    opened.reset_mock()
    with adapter._open(REF):
        pass
    assert [call.args[0] for call in opened.call_args_list] == [target]
    replacement = target.with_suffix(".new")
    replacement.write_bytes(encode([row("session_meta", id=CHILD)]))
    replacement.replace(target)
    with pytest.raises(H.UnsupportedOperation):
        adapter._open(REF)
    assert REF not in adapter._path_cache


def test_large_and_initially_incomplete_metadata_is_discovered(rollout):
    adapter, path, meta = rollout
    large = dict(meta, payload=dict(meta["payload"], instructions="x" * (70 * 1024)))
    data = encode([large])
    path.write_bytes(data[:-1])
    assert list(adapter.discover()) == []
    with path.open("ab") as stream:
        stream.write(b"\n")
    assert [session.ref for session in adapter.discover()] == [REF]


def test_queue_probe_timeout_retries_after_sixty_seconds(rollout, monkeypatch):
    adapter, _, _ = rollout
    now = [100.0]
    monkeypatch.setattr("ctb.harness_codex.time.monotonic", lambda: now[0])
    monkeypatch.setattr("ctb.harness_codex.shutil.which", lambda name: "/example/bin/codex")
    run = Mock(side_effect=[subprocess.TimeoutExpired("codex", 10),
                            subprocess.CompletedProcess([], 0, b"--thread <THREAD> --message <TEXT>", b"")])
    monkeypatch.setattr("ctb.harness_codex.subprocess.run", run)
    assert not adapter.capabilities().send
    now[0] = 159.9
    assert not adapter.capabilities().send and run.call_count == 1
    now[0] = 160.0
    assert adapter.capabilities().send and run.call_count == 2


def test_concurrent_cache_access_preserves_results(rollout):
    adapter, path, _ = rollout
    append(path, start(), message())
    cursor = adapter.initial_cursor(REF, replay=True)
    def read(_):
        return list(adapter.discover()), [e.text for e in replies(adapter.events(REF, cursor))]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(read, range(32)))
    assert all(len(sessions) == 1 and texts == ["Example reply"] for sessions, texts in results)
    assert len(adapter._metadata) == len(adapter._contexts) == len(adapter._path_cache) == 1


def test_registry_hides_unexpected_initialization_error(rollout, monkeypatch, caplog):
    cfg = from_dict({"codex_harness_enabled": True, "codex_attached_sessions": [SID]})
    monkeypatch.setattr("ctb.harness_codex.CodexHarness", Mock(side_effect=RuntimeError("private detail")))
    registry = H.registry(cfg=cfg)
    with pytest.raises(H.UnsupportedOperation):
        registry.resolve(REF)
    assert "Codex disabled" in caplog.text and "private detail" not in caplog.text


def test_oversized_record_logs_without_content(rollout, caplog):
    adapter, path, _ = rollout
    append(path, start(), message("private-content" + "x" * MAX_RECORD_BYTES))
    with caplog.at_level("DEBUG", logger="ctb.harness_codex"):
        list(adapter.events(REF, replay=True))
    assert "Skipping oversized rollout record" in caplog.text
    assert "private-content" not in caplog.text
