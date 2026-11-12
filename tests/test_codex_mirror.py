"""Synthetic sessions and a fake Telegram bot; no native queue or live account."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from conftest import GOOD_TOKEN
from ctb import codex_mirror as M, harness as H, heartbeat, inbox as I
from ctb.config import Config, state_dir
from ctb.harness_codex import CodexHarness
from ctb.topics import TopicMap, session_state

SID = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
REF = H.SessionRef("codex", "default", SID)


def row(kind, **payload):
    return {"type": kind, "payload": payload}


def reply(text="Example reply", *, item="reply-a", phase="final_answer"):
    return row("response_item", type="message", id=item, role="assistant", phase=phase,
               content=[{"type": "output_text", "text": text}])


def append(path, *rows):
    with path.open("a", encoding="utf-8") as f:
        for entry in rows:
            f.write(json.dumps(entry) + "\n")


@pytest.fixture
def rig(tmp_path, bot):
    root = tmp_path / "sessions"
    root.mkdir()
    paths = {}
    for sid in (SID, OTHER):
        path = root / ("rollout-example-" + sid + ".jsonl")
        append(path, row("session_meta", id=sid),
               row("event_msg", type="task_started", turn_id="initial-turn"))
        paths[sid] = path
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=123, translate=False,
                 target_language="en", ui_language="en", pin_replies=False,
                 codex_harness_enabled=True, codex_attached_sessions=[SID],
                 codex_sessions_dir=str(root))
    topics = TopicMap(state_dir() / "topics.json", bot, cfg.owner_chat_id, cfg=cfg)
    box = I.Inbox(tmp_path / "inbox")
    return SimpleNamespace(cfg=cfg, bot=bot, paths=paths, topics=topics, box=box)


def texts(rig):
    return [p["text"] for p in rig.bot.methods("sendMessage") if "Codex\n\n" in p.get("text", "")]


def incoming(rig, **extra):
    return dict(message_id=17, date=2_000_000_000,
                message_thread_id=rig.topics.thread_of(REF.key),
                chat={"id": rig.cfg.owner_chat_id, "type": "private"},
                **{"from": {"id": rig.cfg.owner_chat_id}}, text="Please continue", **extra)


def handle(rig, m, *, kind="message", media=None):
    media = media or SimpleNamespace(content_of=lambda m: m.get("text") or m.get("caption") or "")
    return I.handle({kind: m}, rig.cfg, rig.bot, rig.topics, rig.box, media, 0)


def test_disabled_no_calls_or_state(rig, monkeypatch):
    rig.cfg.codex_harness_enabled = False
    registry = Mock(side_effect=AssertionError("disabled adapter was used"))
    monkeypatch.setattr(M, "registry", registry)
    assert M.poll(rig.cfg, rig.bot) == 0
    registry.assert_not_called()
    assert rig.bot.calls == []
    assert not (state_dir() / "codex-mirror").exists()


def test_topic_created_once_only_for_attached_session(rig):
    M.poll(rig.cfg, rig.bot)
    M.poll(rig.cfg, rig.bot)  # a new registry/adapter on every call
    assert len(rig.bot.methods("createForumTopic")) == 1
    assert "Codex" in rig.bot.methods("createForumTopic")[0]["name"]
    assert set(rig.topics.all_places()) == {REF.key}
    assert rig.topics.thread_of(REF.key)


def test_reply_mirrored_once_across_restarts_cursor_persisted(rig):
    append(rig.paths[SID], reply("Historical reply", item="history"))
    M.poll(rig.cfg, rig.bot)
    assert texts(rig) == []
    path = state_dir() / "codex-mirror" / (SID + ".json")
    initial = json.loads(path.read_text())["cursor"]
    assert initial
    append(rig.paths[SID], reply())
    assert M.poll(rig.cfg, rig.bot) == 1
    saved = json.loads(path.read_text())["cursor"]
    assert saved != initial and "Example reply" not in saved
    assert len(texts(rig)) == 1
    assert M.poll(rig.cfg, rig.bot) == 0
    assert len(texts(rig)) == 1
    assert path.stat().st_mode & 0o777 == 0o600
    reply_send = next(p for p in rig.bot.methods("sendMessage") if "Codex\n\n" in p.get("text", ""))
    assert reply_send["message_thread_id"] == rig.topics.thread_of(REF.key)
    assert not reply_send.get("reply_markup")  # no Claude controls/native approvals


@pytest.mark.parametrize("enabled", [False, True])
def test_commentary_off_by_default(rig, enabled):
    assert rig.cfg.codex_commentary is False
    rig.cfg.codex_commentary = enabled
    M.poll(rig.cfg, rig.bot)
    append(rig.paths[SID], reply("Progress", phase="commentary"), reply("Final", item="final"))
    M.poll(rig.cfg, rig.bot)
    assert len(texts(rig)) == (2 if enabled else 1)
    assert any("Progress" in t for t in texts(rig)) is enabled


def test_backlog_cap(rig):
    M.poll(rig.cfg, rig.bot)
    append(rig.paths[SID], *(reply("Reply " + str(n), item=str(n)) for n in range(57)))
    assert M.poll(rig.cfg, rig.bot) == M.EVENTS_PER_SESSION
    assert len(texts(rig)) == 50
    assert M.poll(rig.cfg, rig.bot) == 7
    assert len(texts(rig)) == 57
    assert M.poll(rig.cfg, rig.bot) == 0


def test_cursor_is_written_after_each_handled_event(rig, monkeypatch):
    M.poll(rig.cfg, rig.bot)
    path = state_dir() / "codex-mirror" / (SID + ".json")
    cursor = json.loads(path.read_text())["cursor"]
    append(rig.paths[SID], reply("First", item="first"), reply("Second", item="second"),
           row("event_msg", type="task_complete", turn_id="initial-turn"))
    observer = CodexHarness(sessions_dir=rig.cfg.codex_sessions_dir, attached=[SID])
    expected = [e.cursor for e in observer.events(REF, cursor)]
    writes, original = [], M.write_private

    def save(target, text):
        original(target, text)
        writes.append(json.loads(target.read_text())["cursor"])

    monkeypatch.setattr(M, "write_private", save)
    assert M.poll(rig.cfg, rig.bot) == 3
    assert writes == expected


def test_one_failing_session_does_not_block_another(rig, monkeypatch, caplog):
    rig.cfg.codex_attached_sessions = [SID, OTHER]
    M.poll(rig.cfg, rig.bot)
    original = CodexHarness.events

    def events(self, ref, cursor):
        if ref.native_id == SID:
            raise RuntimeError("private diagnostic and content")
        return original(self, ref, cursor)

    monkeypatch.setattr(CodexHarness, "events", events)
    append(rig.paths[OTHER], reply("Other session reply"))
    assert M.poll(rig.cfg, rig.bot) == 1
    assert len(texts(rig)) == 1 and "Other session reply" in texts(rig)[0]
    assert "private diagnostic" not in caplog.text
    assert "session poll failed" in caplog.text


def test_lifecycle_status_and_tools_usage_have_no_reply_messages(rig):
    M.poll(rig.cfg, rig.bot)
    append(rig.paths[SID], row("event_msg", type="task_started", turn_id="turn-a"),
           row("turn_context", turn_id="turn-a", model="example-model"),
           row("response_item", type="function_call", name="example_tool", call_id="call-a",
               arguments="private tool input"))
    assert M.poll(rig.cfg, rig.bot) == 3
    assert session_state(REF.key)["state"] == "busy"
    assert texts(rig) == []
    assert all("private tool input" not in p.get("text", "") for p in rig.bot.methods("sendMessage"))
    append(rig.paths[SID], row("event_msg", type="task_complete", turn_id="turn-a"))
    assert M.poll(rig.cfg, rig.bot) == 1
    assert session_state(REF.key)["state"] == "done"
    assert texts(rig) == []


def test_egress_sanitizer_is_the_existing_mirror(rig, monkeypatch):
    from ctb import mirror
    redact = Mock(wraps=mirror.redact)
    monkeypatch.setattr(mirror, "redact", redact)
    M.poll(rig.cfg, rig.bot)
    secret = "sk-" + "a" * 48  # synthetic credential shape
    append(rig.paths[SID], reply("Result " + secret))
    M.poll(rig.cfg, rig.bot)
    assert texts(rig)
    assert secret not in json.dumps(rig.bot.calls)
    redact.assert_called_once()
    assert redact.call_args.args[0].startswith("Codex\n\n")


def test_failed_reply_keeps_previous_cursor_for_retry(rig):
    M.poll(rig.cfg, rig.bot)
    path = state_dir() / "codex-mirror" / (SID + ".json")
    before = path.read_text()
    append(rig.paths[SID], reply())
    from ctb.telegram import TelegramError
    rig.bot.fail["sendMessage"] = TelegramError("Forbidden", 403)
    assert M.poll(rig.cfg, rig.bot) == 0
    assert path.read_text() == before
    assert M.poll(rig.cfg, rig.bot) == 1
    from ctb import reply_outbox
    assert reply_outbox.drain(rig.cfg, rig.bot) == 0
    assert len(texts(rig)) == 2  # one rejected attempt, one accepted send; no queued copy


def test_heartbeat_runs_opted_in_poll_only(rig, monkeypatch):
    poll = Mock(return_value=0)
    monkeypatch.setattr(M, "poll", poll)
    monkeypatch.setattr(heartbeat, "refresh_cards", lambda *a, **k: 0)
    monkeypatch.setattr(heartbeat.agents_card, "retry_dirty", lambda *a: None)
    heartbeat.run_once(rig.cfg, rig.bot)
    poll.assert_called_once_with(rig.cfg, rig.bot)
    rig.cfg.codex_harness_enabled = False
    heartbeat.run_once(rig.cfg, rig.bot)
    assert poll.call_count == 1


def test_owner_text_goes_through_queue_owner(rig, monkeypatch):
    M.poll(rig.cfg, rig.bot)
    queue = Mock()
    monkeypatch.setattr(I, "_queue_owner", queue)
    assert handle(rig, incoming(rig)) == "queued"
    args, kwargs = queue.call_args
    assert args[4:7] == (REF.key, "Please continue", "Please continue")
    assert kwargs["typed"] is True
    assert kwargs["forwarded_text"] == ""


def test_queue_owner_sends_codex_without_legacy_wake_or_hold(rig, monkeypatch):
    M.poll(rig.cfg, rig.bot)
    send = Mock(return_value=H.Receipt(REF, H.ReceiptState.ACCEPTED))
    monkeypatch.setattr(CodexHarness, "capabilities", lambda self: H.Capabilities(send=True))
    monkeypatch.setattr(CodexHarness, "send", send)
    monkeypatch.setattr(I.delivery, "default_mode", lambda: "end")
    monkeypatch.setattr(I, "_reopen_or_nudge", Mock(side_effect=AssertionError("legacy wake")))
    assert handle(rig, incoming(rig)) == "queued"
    ref, item = send.call_args.args
    assert ref == REF and item["source"] == "owner" and not item.get("typed")
    assert "hold" not in item
    assert rig.box.sessions() == []


def test_codex_send_failure_answers_briefly_without_retry(rig, monkeypatch):
    M.poll(rig.cfg, rig.bot)
    send = Mock(side_effect=RuntimeError("private native diagnostic"))
    monkeypatch.setattr(CodexHarness, "capabilities", lambda self: H.Capabilities(send=True))
    monkeypatch.setattr(CodexHarness, "send", send)
    handle(rig, incoming(rig))
    send.assert_called_once()
    notices = rig.bot.methods("sendMessage")
    assert "not confirmed" in notices[-1]["text"]
    assert "private native diagnostic" not in json.dumps(rig.bot.calls)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("kind", ["message", "edited_message", "callback_query"])
def test_non_owner_gets_silence(rig, enabled, kind):
    M.poll(rig.cfg, rig.bot)
    rig.cfg.codex_harness_enabled = enabled
    rig.bot.calls.clear()
    m = incoming(rig)
    if kind == "callback_query":
        m = {"id": "tap", "from": {"id": 999}, "message": m, "data": "say:yes"}
    else:
        m["from"] = {"id": 999}
    assert handle(rig, m, kind=kind) == "ignored"
    assert rig.bot.calls == []


@pytest.mark.parametrize("kind", ["callback_query", "edited_message", "command", "keyboard", "disabled", "detached"])
def test_panel_approval_and_unsupported_input_stay_refused(rig, kind, monkeypatch):
    M.poll(rig.cfg, rig.bot)
    queue = Mock(side_effect=AssertionError("unsupported input queued"))
    monkeypatch.setattr(I, "_queue_owner", queue)
    m, update_kind = incoming(rig), "message"
    if kind == "callback_query":
        m = {"id": "tap", "from": {"id": 123}, "message": m, "data": "say:yes"}
        update_kind = kind
    elif kind == "edited_message":
        update_kind = kind
        m["edit_date"] = m["date"]
    elif kind == "command":
        m["text"] = "/panel"
    elif kind == "keyboard":
        from ctb.i18n import t
        m["text"] = t("fixed_btn_stop", lang="en")
    elif kind == "disabled":
        rig.cfg.codex_harness_enabled = False
    elif kind == "detached":
        rig.cfg.codex_attached_sessions = []
    assert handle(rig, m, kind=update_kind) == "unsupported-harness"
    queue.assert_not_called()


def test_forwarded_text_never_becomes_owner_text(rig, monkeypatch):
    M.poll(rig.cfg, rig.bot)
    queue = Mock()
    monkeypatch.setattr(I, "_queue_owner", queue)
    m = incoming(rig)
    m["forward_origin"] = {"type": "hidden_user", "sender_user_name": "Example"}
    assert handle(rig, m) == "forward-refused"
    queue.assert_not_called()


def test_forwarded_file_preserves_provenance(rig, monkeypatch):
    M.poll(rig.cfg, rig.bot)
    queue = Mock()
    monkeypatch.setattr(I, "_queue_owner", queue)
    m = incoming(rig)
    m.pop("text")
    m.update(document={"file_id": "synthetic"}, caption="Someone else's caption",
             forward_origin={"type": "hidden_user", "sender_user_name": "Example"})
    media = SimpleNamespace(content_parts=lambda m: ("Forwarded file", m.get("caption") or ""))
    assert handle(rig, m, media=media) == "queued"
    assert queue.call_args.args[5] == ""
    assert queue.call_args.kwargs == {"typed": False, "forwarded_text": "Forwarded file"}


@pytest.mark.parametrize("origin", ["voice", "_voice"])
def test_queue_owner_preserves_voice_provenance(rig, monkeypatch, origin):
    M.poll(rig.cfg, rig.bot)
    send = Mock(return_value=H.Receipt(REF, H.ReceiptState.ACCEPTED))
    monkeypatch.setattr(CodexHarness, "capabilities", lambda self: H.Capabilities(send=True))
    monkeypatch.setattr(CodexHarness, "send", send)
    m = incoming(rig)
    m[origin] = True
    I._queue_owner(rig.cfg, rig.bot, rig.box, m, REF.key, "Approved transcript", typed=True)
    item = send.call_args.args[1]
    assert item["voice"] is True and not item.get("typed")


def test_topic_creation_failure_never_posts_in_general(rig):
    rig.bot.fail["createForumTopic"] = "Forbidden"
    M.poll(rig.cfg, rig.bot)
    assert not rig.bot.methods("sendMessage")
    assert rig.topics.thread_of(REF.key) is None


@pytest.mark.parametrize("kind", ["voice", "audio", "video_note"])
@pytest.mark.parametrize("lang", ["en", "he"])
def test_codex_recordings_rejected_before_transcription(rig, monkeypatch, kind, lang):
    from ctb.i18n import t
    M.poll(rig.cfg, rig.bot)
    rig.cfg.ui_language = lang
    queue, review = Mock(), Mock()
    monkeypatch.setattr(I, "_queue_owner", queue)
    monkeypatch.setattr(I, "voice_draft", review)
    m = incoming(rig)
    m.pop("text")
    m[kind] = {"file_id": "synthetic"}
    media = SimpleNamespace(content_of=Mock(side_effect=AssertionError("transcribed")))
    assert handle(rig, m, media=media) == "unsupported-harness"
    assert rig.bot.methods("sendMessage")[-1]["text"] == t("codex_voice_unsupported", lang=lang)
    review.assert_not_called()
    queue.assert_not_called()


def test_zero_reply_delivery_keeps_cursor_and_retries(rig, monkeypatch):
    M.poll(rig.cfg, rig.bot)
    path = state_dir() / "codex-mirror" / (SID + ".json")
    before = path.read_text()
    append(rig.paths[SID], reply())
    original = M.Mirror.send_reply
    monkeypatch.setattr(M.Mirror, "send_reply", lambda *args: 0)
    assert M.poll(rig.cfg, rig.bot) == 0
    assert path.read_text() == before
    monkeypatch.setattr(M.Mirror, "send_reply", original)
    assert M.poll(rig.cfg, rig.bot) == 1
    assert len(texts(rig)) == 1


def test_codex_topic_owner_rename_recorded(rig):
    from ctb import topic_names
    M.poll(rig.cfg, rig.bot)
    m = incoming(rig, forum_topic_edited={"name": "Chosen name"})
    assert handle(rig, m) == "topic-renamed"
    assert topic_names.records()[REF.key]["locked"] is True
    assert rig.topics.all()[REF.key]["name"] == "Chosen name"


@pytest.mark.parametrize("kind,handler,result", [
    ("forum_topic_edited", "drop_rename_notice", "rename-notice"),
    ("pinned_message", "drop_pin_notice", "pin-notice"),
])
def test_codex_bot_service_notices_removed(rig, monkeypatch, kind, handler, result):
    M.poll(rig.cfg, rig.bot)
    drop = Mock(return_value=True)
    monkeypatch.setattr(I.pins, handler, drop)
    m = incoming(rig, **{kind: {"name": "example"}})
    m["from"] = {"id": 42, "is_bot": True}
    assert handle(rig, m) == result
    drop.assert_called_once_with(m, rig.cfg, rig.bot)


def test_codex_excluded_from_last_session_and_general_offers(rig):
    from ctb.topics import session_state_path
    M.poll(rig.cfg, rig.bot)
    path = session_state_path(REF.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    assert I.last_session(rig.topics) is None
    assert I.last_session() is None
    assert I._recent_topics(rig.topics) == []


def test_codex_queue_never_marks_typed(rig, monkeypatch):
    M.poll(rig.cfg, rig.bot)
    send = Mock(return_value=H.Receipt(REF, H.ReceiptState.ACCEPTED))
    monkeypatch.setattr(CodexHarness, "capabilities", lambda self: H.Capabilities(send=True))
    monkeypatch.setattr(CodexHarness, "send", send)
    I._queue_owner(rig.cfg, rig.bot, rig.box, incoming(rig), REF.key, "yes", typed=True)
    assert "typed" not in send.call_args.args[1]


def test_heartbeat_survives_codex_poll_setup_failure(rig, monkeypatch, caplog):
    monkeypatch.setattr(M, "poll", Mock(side_effect=RuntimeError("private content")))
    refresh = Mock(return_value=0)
    monkeypatch.setattr(heartbeat, "refresh_cards", refresh)
    monkeypatch.setattr(heartbeat.agents_card, "retry_dirty", lambda *a: None)
    heartbeat.run_once(rig.cfg, rig.bot)
    refresh.assert_called_once()
    assert "Codex mirror poll failed" in caplog.text
    assert "private content" not in caplog.text


def test_deleted_topic_recreation_failure_keeps_reply_for_retry(rig):
    M.poll(rig.cfg, rig.bot)
    path = state_dir() / "codex-mirror" / (SID + ".json")
    before = path.read_text()
    append(rig.paths[SID], reply())
    rig.bot.fail["sendMessage"] = "message thread not found"
    rig.bot.fail["createForumTopic"] = "Forbidden"
    assert M.poll(rig.cfg, rig.bot) == 0
    assert path.read_text() == before
    assert M.poll(rig.cfg, rig.bot) == 1
    assert path.read_text() != before
    from ctb import reply_outbox
    assert reply_outbox.drain(rig.cfg, rig.bot) == 0
    assert M.poll(rig.cfg, rig.bot) == 0


def test_car_mode_cannot_route_to_codex(rig, monkeypatch):
    M.poll(rig.cfg, rig.bot)
    monkeypatch.setattr(I, "car_mode_active", lambda: True)
    monkeypatch.setattr(I, "last_session", lambda topics: REF.key)
    start = Mock(return_value="picker")
    monkeypatch.setattr(I, "general_start", start)
    queue = Mock()
    monkeypatch.setattr(I, "_queue_owner", queue)
    m = incoming(rig)
    m.pop("message_thread_id")
    assert handle(rig, m) == "picker"
    queue.assert_not_called()
    start.assert_called_once()


def test_stale_codex_recording_gets_no_response(rig, monkeypatch):
    M.poll(rig.cfg, rig.bot)
    rig.bot.calls.clear()
    m = incoming(rig, voice={"file_id": "synthetic"})
    m["date"] = 1
    assert I.handle({"message": m}, rig.cfg, rig.bot, rig.topics, rig.box, None, 100) == "ignored"
    assert rig.bot.calls == []
