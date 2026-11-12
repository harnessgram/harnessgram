"""Adapter boundary guards; all sessions, messages and profiles are synthetic."""
from dataclasses import FrozenInstanceError
from unittest.mock import Mock, call

import pytest

from ctb import harness as H, inbox, status

SID = "11111111-1111-4111-8111-111111111111"
CLAUDE = H.SessionRef("claude", "default", SID)


def test_claude_implements_protocol_with_truthful_capabilities():
    adapter = H.ClaudeHarness()
    assert isinstance(adapter, H.Harness)
    caps = adapter.capabilities()
    assert caps.version == 1 and caps.discover and caps.send
    assert not any((caps.observe, caps.steer, caps.interrupt, caps.resume,
                    caps.tool_approvals, caps.user_questions, caps.children, caps.usage))


def test_legacy_uuid_lookup_and_namespaced_precedence():
    state = {SID: {"name": "Legacy"}}
    assert H.SessionRef.parse(SID) == CLAUDE
    assert H.HarnessRegistry.lookup(state, SID) == {"name": "Legacy"}
    assert H.HarnessRegistry.lookup(state, CLAUDE.key) == {"name": "Legacy"}
    state[CLAUDE.key] = {"name": "Migrated"}
    assert H.HarnessRegistry.lookup(state, SID) == {"name": "Migrated"}
    assert state[SID] == {"name": "Legacy"}  # a lookup never rewrites stored state


def test_same_native_id_cannot_collide_across_harnesses_or_profiles():
    codex = H.SessionRef("codex", "default", SID)
    other_profile = H.SessionRef("claude", "work", SID)
    refs = (CLAUDE, codex, other_profile)
    assert len(set(refs)) == len({ref.key for ref in refs}) == 3
    state = {SID: "legacy"}
    assert H.HarnessRegistry.lookup(state, codex) is None
    assert H.HarnessRegistry.lookup(state, other_profile) is None
    registry = H.HarnessRegistry()
    for n, ref in enumerate(refs):
        state[ref.key] = n
        adapter = Mock(spec=H.Harness)
        registry.register(ref.harness, ref.profile, adapter)
        assert registry.resolve(ref.key) == (adapter, ref)
    for n, ref in enumerate(refs):
        assert H.HarnessRegistry.lookup(state, ref) == n


def test_escaped_components_roundtrip_without_separator_collisions():
    a = H.SessionRef("codex", "work:one", "x:y/%")
    b = H.SessionRef("codex", "work", "one:x:y/%")
    assert a.key != b.key
    assert H.SessionRef.parse(a.key) == a
    assert H.SessionRef.parse(b.key) == b
    with pytest.raises(FrozenInstanceError):
        a.native_id = "changed"


@pytest.mark.parametrize("bad", ["", "claude:default:%69d"])
def test_malformed_identity_is_rejected(bad):
    with pytest.raises(ValueError):
        H.SessionRef.parse(bad)


@pytest.mark.parametrize("native", ["Project preview", "תצוגת פרויקט", "Project: preview",
                                    "project: preview: notes", "project:תצוגה:פרויקט",
                                    "Project:preview:notes:extra", "codex:default", "codex::id",
                                    "Project [preview]*?", "../session"])
def test_unscoped_legacy_ids_keep_their_exact_spelling(native):
    ref = H.SessionRef("claude", "default", native)
    assert H.SessionRef.parse(native) == ref
    assert H.SessionRef.parse(ref.key) == ref
    assert H.legacy_topic_supported(native)


def test_registry_uses_exact_ids_and_never_defaults_unknown_harness_to_claude():
    registry = H.registry()
    assert registry.resolve(SID)[1] == CLAUDE
    assert registry.resolve(SID[:8])[1].native_id == SID[:8]  # opaque, never expanded
    for ref in (H.SessionRef("codex", "default", SID), H.SessionRef("claude", "work", SID)):
        with pytest.raises(H.UnsupportedOperation):
            registry.send(ref, {"text": "hello"})
    with pytest.raises(ValueError):
        registry.register("claude", "default", H.ClaudeHarness())


def test_discovery_delegates_readers_and_does_not_opt_in_or_claim_liveness(monkeypatch):
    topics = Mock()
    codex = H.SessionRef("codex", "default", SID)
    topics.all_places.return_value = {
        SID: {"name": "Legacy"}, CLAUDE.key: {"name": "Preferred"},
        codex.key: {"name": "Other harness"}, "": {"name": "Invalid"},
    }
    find = Mock(return_value="transcript.jsonl")
    monkeypatch.setattr(status, "transcript_of", find)
    monkeypatch.setattr(status, "workplace", lambda path: ("~/example", "main"))
    monkeypatch.setattr(status, "usage_of", lambda path: (200, "example-model"))
    sessions = list(H.ClaudeHarness(topics=topics).discover())
    assert sessions == [H.Session(CLAUDE, "Preferred", "~/example", "example-model")]
    assert sessions[0].alive is None and sessions[0].liveness_provenance == "unknown"
    assert all(c.args == (SID,) for c in find.call_args_list)
    find.assert_called_once_with(SID)
    assert topics.method_calls == [call.all_places()]


def test_transcript_lookup_accepts_legacy_and_scoped_claude_only(tmp_path, monkeypatch):
    folder = tmp_path / "projects" / "example"
    folder.mkdir(parents=True)
    transcript = folder / (SID + ".jsonl")
    transcript.write_text("{}\n")
    monkeypatch.setattr(status, "projects_dir", lambda: folder.parent)
    assert status.transcript_of(SID) == str(transcript)
    assert status.transcript_of(CLAUDE.key) == str(transcript)
    for ref in (H.SessionRef("codex", "default", SID), H.SessionRef("claude", "other", SID)):
        assert status.transcript_of(ref.key) is None
    assert status.transcript_of("../" + SID) is None


@pytest.mark.parametrize("native", ["Project preview", "תצוגת פרויקט", "Project: preview",
                                    "project: preview: notes", "project:תצוגה:פרויקט",
                                    "Project:preview:notes:extra", "Project [preview]*?"])
def test_transcript_lookup_preserves_legacy_titles(tmp_path, monkeypatch, native):
    folder = tmp_path / "projects" / "example"
    folder.mkdir(parents=True)
    transcript = folder / (native + ".jsonl")
    transcript.write_text("{}\n")
    monkeypatch.setattr(status, "projects_dir", lambda: folder.parent)
    assert status.transcript_of(native) == str(transcript)
    assert status.transcript_of(H.SessionRef("claude", "default", native).key) == str(transcript)


@pytest.mark.parametrize("provenance", [
    {"source": "owner", "typed": True}, {"source": "owner", "voice": True},
    {"source": "owner", "button": "yes"}, {"source": "owner", "forwarded": True},
    {"source": "team", "user_id": 123},
])
def test_send_delegates_to_legacy_inbox_preserving_provenance(tmp_path, provenance):
    box = inbox.Inbox(tmp_path / "queue")
    message = dict(provenance, text="example", hold=True, context_prefix="context", key="example-key")
    receipt = H.registry(inbox=box).send(CLAUDE.key, message)
    assert receipt.ref == CLAUDE and receipt.state is H.ReceiptState.QUEUED
    assert receipt.message_id == "example-key"
    assert not receipt.delivered
    assert "queued_at" not in message
    queued = box.peek(SID)
    assert len(queued) == 1
    assert {key: queued[0][key] for key in message} == message
    assert (box.root / (SID + ".jsonl")).exists()


def test_failed_queue_write_cannot_return_success():
    box = Mock()
    box.put.side_effect = OSError("queue unavailable")
    with pytest.raises(OSError, match="queue unavailable"):
        H.ClaudeHarness(inbox=box).send(CLAUDE, {"text": "example"})


@pytest.mark.parametrize("key", [None, "", False, 123, []])
def test_invalid_delivery_keys_fail_before_enqueue(key):
    box = Mock()
    with pytest.raises(ValueError, match="Message key"):
        H.ClaudeHarness(inbox=box).send(CLAUDE, {"text": "example", "key": key})
    box.put.assert_not_called()


@pytest.mark.parametrize("state,delivered", [
    (H.ReceiptState.QUEUED, False), (H.ReceiptState.ACCEPTED, False),
    (H.ReceiptState.OBSERVED, True), (H.ReceiptState.ANSWERED, True),
])
def test_receipt_never_confuses_queue_acceptance_with_delivery(state, delivered):
    assert H.Receipt(CLAUDE, state).delivered is delivered


@pytest.mark.parametrize("action", list(H.Control))
def test_unsupported_control_never_guesses_keys_or_queues_text(action, monkeypatch):
    box = Mock()
    terminal = Mock(side_effect=AssertionError("terminal input must not be attempted"))
    monkeypatch.setattr(inbox, "type_command", terminal)
    monkeypatch.setattr(inbox.subprocess, "run", terminal)
    with pytest.raises(H.UnsupportedOperation):
        H.ClaudeHarness(inbox=box).control(CLAUDE, H.ControlCommand(action, text="yes"))
    box.put.assert_not_called()
    terminal.assert_not_called()


def test_pull_events_are_explicitly_unsupported_until_cursor_contract_is_ready():
    with pytest.raises(H.UnsupportedOperation):
        H.ClaudeHarness().events(CLAUDE, "example-cursor")


def test_events_preserve_native_identity_and_unknown_usage():
    metadata = dict(ref=CLAUDE, turn_id="turn-a", item_id="item-a", cursor="cursor-a",
                    timestamp="example-time")
    events = [H.TurnStarted(**metadata), H.AssistantText(**metadata, text="hello", phase="commentary"),
              H.ToolProgress(**metadata, tool="example", state="running"),
              H.TurnEnded(**metadata, outcome="failed"),
              H.NeedsInput(**metadata, request_id="request-a", kind="approval"), H.Usage(**metadata)]
    assert all(event.ref == CLAUDE and event.cursor == "cursor-a" for event in events)
    assert events[1].phase == "commentary" and not events[1].final
    assert H.AssistantText(CLAUDE, text="done", phase="final", final=True).final
    assert events[3].outcome == "failed"
    assert events[4].request_id == "request-a"
    assert events[5].input_tokens is None and events[5].context_tokens is None


def test_owner_route_resolves_scoped_claude_before_legacy_side_effects(tmp_path, monkeypatch, bot):
    from ctb.config import Config
    cfg = Config(bot_token="example", owner_chat_id=123)
    box = inbox.Inbox(tmp_path / "queue")
    logged = Mock()
    monkeypatch.setattr(inbox, "log_delivery", logged)
    monkeypatch.setattr(inbox, "limit_notice", lambda *a: True)
    monkeypatch.setattr(inbox, "main_working", lambda s: False)
    monkeypatch.setattr(inbox.dialogs, "held_notice", lambda *a: False)
    m = {"message_id": 1, "date": 1, "message_thread_id": 7}
    inbox._queue_owner(cfg, bot, box, m, CLAUDE.key, "example", typed=True)
    logged.assert_called_once_with(SID, 1)
    assert box.peek(SID)[0]["typed"] is True
    inbox._queue_owner(cfg, bot, box, m, H.SessionRef("codex", "default", SID).key, "example")
    assert logged.call_count == 1
    assert bot.methods("sendMessage")[-1]["text"]


@pytest.mark.parametrize("session", ["unknown:default:example", "claude:work:example",
                                     "claude:default:%69d", ""])
def test_refused_owner_queue_returns_before_side_effects(tmp_path, monkeypatch, bot, session):
    from ctb.config import Config

    cfg = Config(bot_token="example", owner_chat_id=123)
    box = inbox.Inbox(tmp_path / "queue")
    logged = Mock()
    bookkeeping = Mock(side_effect=AssertionError("refused target reached legacy bookkeeping"))
    monkeypatch.setattr(inbox, "poll_log", logged)
    monkeypatch.setattr(inbox, "log_delivery", bookkeeping)
    monkeypatch.setattr(inbox.sessions_menu, "reopen_for_message", bookkeeping)
    m = {"message_id": 1, "date": 1, "message_thread_id": 7,
         "chat": {"id": cfg.owner_chat_id, "type": "private"}}
    assert inbox._queue_owner(cfg, bot, box, m, session, "example") is None
    bookkeeping.assert_not_called()
    assert not box.root.exists()
    logged.assert_called_once()
    notice = bot.methods("sendMessage")[-1]
    assert notice["chat_id"] == cfg.owner_chat_id and notice["message_thread_id"] == 7
    assert notice["text"]


@pytest.mark.parametrize("session", ["Project preview", "תצוגת פרויקט", "Project: preview",
                                     "project: preview: notes", "project:תצוגה:פרויקט"])
def test_titled_topic_queues_owner_text_reacts_and_answers_taps(tmp_path, monkeypatch, bot, session):
    from ctb.config import Config
    from ctb.topics import TopicMap

    cfg = Config(bot_token="example", owner_chat_id=123)
    topics = TopicMap(tmp_path / "topics.json", bot, cfg.owner_chat_id)
    topics._save({session: {"thread": 7, "name": session}})
    box = inbox.Inbox(tmp_path / "queue")
    monkeypatch.setattr(inbox.dialogs, "typed_answer", lambda *a: None)
    monkeypatch.setattr(inbox.dialogs, "held_notice", lambda *a: False)
    monkeypatch.setattr(inbox, "limit_notice", lambda *a: False)
    monkeypatch.setattr(inbox, "main_working", lambda *a: False)
    monkeypatch.setattr(inbox, "_deliver_button_word", Mock())
    m = {"message_id": 1, "date": 2_000_000_000, "message_thread_id": 7,
         "chat": {"id": cfg.owner_chat_id, "type": "private"},
         "from": {"id": cfg.owner_chat_id}, "text": "example text"}
    media = Mock(spec=["content_of"])
    media.content_of.return_value = m["text"]
    assert inbox.handle({"message": m}, cfg, bot, topics, box, media, 0) == "queued"
    queued = box.peek(session)
    assert len(queued) == 1 and queued[0]["text"] == m["text"] and queued[0]["typed"]
    assert bot.methods("setMessageReaction")[-1]["reaction"][0]["emoji"] == "\U0001F440"
    q = {"id": "example-tap", "from": m["from"], "message": m, "data": "say:continue"}
    assert inbox.handle({"callback_query": q}, cfg, bot, topics, box, media, 0) == "queued"
    assert bot.methods("answerCallbackQuery")[-1]["callback_query_id"] == q["id"]
    assert box.peek(session)[-1]["button"] == "continue"


def test_receipts_correlate_distinct_sends_with_legacy_delivery_keys(tmp_path):
    box = inbox.Inbox(tmp_path / "queue")
    sender = H.registry(inbox=box)
    a = sender.send(SID, {"text": "first"})
    b = sender.send(SID, {"text": "second"})
    assert a.message_id and b.message_id and a.message_id != b.message_id
    assert [a.message_id, b.message_id] == [item["key"] for item in box.peek(SID)]
    assert not a.delivered and not b.delivered


@pytest.mark.parametrize("ref", [CLAUDE, H.SessionRef("codex", "default", SID),
                                 H.SessionRef("claude", "work", SID),
                                 H.SessionRef("unknown", "default", SID)])
@pytest.mark.parametrize("kind", ["message", "stop", "edit", "button"])
def test_scoped_topic_routes_fail_closed_before_legacy_control(ref, kind, tmp_path, monkeypatch, bot):
    from ctb.config import Config
    from ctb.topics import TopicMap

    cfg = Config(bot_token="example", owner_chat_id=123)
    topics = TopicMap(tmp_path / "topics.json", bot, cfg.owner_chat_id)
    topics._save({ref.key: {"thread": 7, "name": "Example"}})
    box = inbox.Inbox(tmp_path / "queue")
    legacy = Mock(side_effect=AssertionError("scoped topic reached a legacy operation"))
    monkeypatch.setattr(inbox, "stop_pane", legacy)
    monkeypatch.setattr(inbox, "cli_command", legacy)
    monkeypatch.setattr(inbox.dialogs, "typed_answer", legacy)
    monkeypatch.setattr(inbox, "_untype_queued", legacy)
    monkeypatch.setattr(inbox.sessions_menu, "note_input", legacy)
    m = {"message_id": 1, "date": 2_000_000_000, "message_thread_id": 7,
         "chat": {"id": cfg.owner_chat_id, "type": "private"},
         "from": {"id": cfg.owner_chat_id}, "text": "example"}
    update = {"message": m}
    if kind == "stop":
        m["text"] = "Stop"
    elif kind == "edit":
        update = {"edited_message": dict(m, edit_date=m["date"])}
    elif kind == "button":
        update = {"callback_query": {"id": "example", "from": m["from"], "message": m, "data": "say:yes"}}
    assert inbox.handle(update, cfg, bot, topics, box, Mock(), 0) == "unsupported-harness"
    if kind == "button":
        answers = bot.methods("answerCallbackQuery")
        assert len(answers) == 1 and answers[0]["callback_query_id"] == "example" and answers[0]["text"]
    else:
        notice = bot.methods("sendMessage")[-1]
        assert notice["chat_id"] == cfg.owner_chat_id and notice["message_thread_id"] == 7
        assert notice["text"]
    legacy.assert_not_called()
    assert box.peek(SID) == []
    assert not box.root.exists()
