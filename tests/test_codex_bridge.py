"""App-server integration against a socketpair and the suite's fake Telegram bot."""
from contextlib import suppress
import json
import socket
from unittest.mock import MagicMock
import threading
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN
from ctb import codex_bridge as B, inbox as I, panel
from ctb.codex_app_server import CodexProtocol, CodexServer
from ctb.config import Config, state_dir
from ctb.topics import TopicMap

THREAD = "00000000-0000-7000-8000-000000000001"
KEY = "codex:default:" + THREAD
CHAT = -1001234567890
OWNER = 123


class Peer:
    def __init__(self, **options):
        self.client, self.server = socket.socketpair()
        self.server.settimeout(3)
        self.protocol = CodexProtocol(lambda msg: self.write(self.client, msg), **options)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.stream = self.server.makefile("rb")

    def _read(self):
        with self.client.makefile("rb") as stream:
            self.protocol.serve(stream)

    @staticmethod
    def write(sock, msg):
        sock.sendall((json.dumps(msg) + "\n").encode())

    def receive(self):
        return json.loads(self.stream.readline())

    def notify(self, method, **params):
        message = {"method": method, "params": {"threadId": THREAD, **params}}
        self.write(self.server, message)

    def ask(self, method, ident, **params):
        message = {"method": method, "id": ident, "params": {"threadId": THREAD, **params}}
        self.write(self.server, message)

    def during(self, action, replies):
        results = []
        thread = threading.Thread(target=lambda: results.append(action()), daemon=True)
        thread.start()
        try:
            calls = []
            for method, reply in replies:
                request = self.receive()
                calls.append(request)
                assert request["method"] == method
                self.write(self.server, {"id": request["id"], "result": reply})
            thread.join(3)
            assert not thread.is_alive()
            assert len(results) == 1
            return results[0], calls
        finally:
            thread.join(3)

    def flush(self):
        self.during(lambda: self.protocol.request("thread/loaded/list"), [("thread/loaded/list", {"data": []})])

    def close(self):
        with suppress(OSError):
            self.server.shutdown(socket.SHUT_RDWR)
        self.stream.close()
        self.server.close()
        self.reader.join(3)
        self.client.close()


@pytest.fixture
def rig(bot, monkeypatch, tmp_path):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=CHAT, translate=False,
                 target_language="en", ui_language="en", pin_replies=False, codex_harness_enabled=True)
    topics = TopicMap(state_dir() / "topics.json", bot, CHAT, cfg=cfg)
    server = CodexServer()
    bridge = B.CodexBridge(cfg, bot, topics, server)
    monkeypatch.setattr(B.policy, "on", lambda: True)
    monkeypatch.setattr(server, "ensure_started", lambda: "fake")
    peers = []
    result = {"thread": {"id": THREAD, "status": {"type": "idle"}}, "model": "synthetic", "reasoningEffort": "high"}
    initial = Peer()
    peers.append(initial)
    _, calls = initial.during(lambda: initial.protocol.start_thread(cwd=".", model="synthetic", effort="high"),
                              [("thread/start", result)])
    assert calls[0]["params"]["config"] == {"model_reasoning_effort": "high"}
    assert calls[0]["params"]["approvalsReviewer"] == "user"
    B.register(server, initial.protocol.threads[THREAD])

    def connect(*, turn=None, recover=None):
        peer = Peer(owner_id=OWNER)
        peers.append(peer)

        def reconnect(**options):
            peer.protocol.on_event = options["on_event"]
            return peer.protocol

        monkeypatch.setattr(server, "connect", reconnect)
        response = result if turn is None else dict(result, thread={"id": THREAD, "status": {"type": "active"}})
        replies = [("thread/resume", response)]
        if turn is not None or recover is not None:
            replies.append(("thread/turns/list", {"data": [turn or recover]}))
        _, calls = peer.during(bridge.tick, replies)
        assert calls[0]["params"] == {"threadId": THREAD, "excludeTurns": True, "approvalsReviewer": "user",
                                      "config": {"model_reasoning_effort": "high"}}
        return peer

    peer = connect()
    yield SimpleNamespace(bridge=bridge, topics=topics, bot=bot, cfg=cfg, peer=peer, connect=connect,
                          box=I.Inbox(tmp_path / "inbox"), media=SimpleNamespace(content_of=lambda m: m["text"]))
    bridge.disconnect()
    for member in peers:
        member.close()


def update(rig, *, actor=OWNER, text="Continue", data=None, mid=980, reply=None):
    group = {"chat": {"id": CHAT, "type": "supergroup"}, "message_thread_id": rig.topics.thread_of(KEY)}
    message = dict(group, date=2_000_000_000, message_id=mid, text=text, **{"from": {"id": actor}})
    if reply:
        message["reply_to_message"] = {"message_id": reply, "from": {"is_bot": True}, "text": B.CARD_MARKER}
    incoming = {"message": message} if data is None else {
        "callback_query": {"id": "test", "data": data, "message": message, "from": {"id": actor}}}
    return I.handle(incoming, rig.cfg, rig.bot, rig.topics, rig.box, rig.media, 0, codex_bridge=rig.bridge)


def dispatch(rig):
    rig.peer.flush()
    rig.bridge.tick()


def test_text_progress_usage_reconnect_and_interrupt(rig):
    assert len(rig.bot.methods("createForumTopic")) == 1
    process, calls = rig.peer.during(lambda: update(rig), [("turn/start", {"turn": {"id": "turn-a", "status": "inProgress"}})])
    assert process == "queued"
    assert "Continue" in calls[0]["params"]["input"][0]["text"]
    assert calls[0]["params"]["input"][0]["text_elements"] == []
    assert calls[0]["params"]["effort"] == "high"
    rig.peer.notify("turn/started", turn={"id": "turn-a", "status": "inProgress"})
    rig.peer.notify("item/agentMessage/delta", turnId="turn-a", itemId="item-a", delta="Streamed")
    rig.peer.notify("thread/tokenUsage/updated", turnId="turn-a", tokenUsage={"total": {"totalTokens": 29}})
    dispatch(rig)
    assert any("Writing a reply" in m["text"] for m in rig.bot.methods("editMessageText"))
    assert B.sessions()[KEY]["usage"]["total"]["totalTokens"] == 29
    _, calls = rig.peer.during(lambda: update(rig, text="One more"), [("turn/steer", {"turnId": "turn-a"})])
    assert calls[0]["params"]["expectedTurnId"] == "turn-a"
    mid = rig.bridge.progress[THREAD][2]
    assert update(rig, actor=321, data="cx:stop", mid=mid) == "ignored"
    _, calls = rig.peer.during(lambda: update(rig, data="cx:stop", mid=mid), [("turn/interrupt", {})])
    assert calls[0]["params"] == {"threadId": THREAD, "turnId": "turn-a"}
    assert update(rig, data="cx:stop", mid=mid) == "stale-codex"
    item = {"id": "item-a", "type": "agentMessage", "phase": "final_answer", "text": "Synthetic final answer"}
    rig.peer.notify("item/completed", turnId="turn-a", completedAtMs=5, item=item)
    dispatch(rig)
    rig.bridge.disconnect()
    rig.peer = rig.connect(recover={"id": "turn-a", "status": "completed", "items": [item]})
    rig.peer.notify("item/completed", turnId="turn-a", completedAtMs=5, item=item)
    rig.peer.notify("turn/completed", turn={"id": "turn-a", "status": "completed"})
    dispatch(rig)
    assert len(rig.bot.methods("createForumTopic")) == 1
    replies = [m for m in rig.bot.methods("sendMessage") if "Synthetic final answer" in m["text"]]
    assert len(replies) == 1 and replies[0]["message_thread_id"] == rig.topics.thread_of(KEY)
    assert B.sessions()[KEY]["status"] == "idle"


def test_permission_and_request_resolution(rig):
    rig.peer.notify("turn/started", turn={"id": "turn-a", "status": "inProgress"})
    rig.peer.ask("item/commandExecution/requestApproval", 11, turnId="turn-a", itemId="item-a",
                 startedAtMs=1, command="printf example", availableDecisions=["accept", "decline"])
    dispatch(rig)
    card = next(iter(rig.bridge.cards.values()))
    assert B.sessions()[KEY]["status"] == "waiting_for_approval"
    assert update(rig, actor=456, text="1", reply=card.message) == "ignored"
    assert update(rig, text="3", reply=card.message) == "stale-codex"
    assert update(rig, text="1", reply=card.message) == "codex-answer"
    assert rig.peer.receive() == {"id": 11, "result": {"decision": "accept"}}
    assert update(rig, text="1", reply=card.message) == "stale-codex"
    dispatch(rig)
    rig.peer.ask("item/permissions/requestApproval", "perms", turnId="turn-a", itemId="item-b",
                 startedAtMs=2, cwd="/workspace", permissions={"network": {"enabled": True}})
    dispatch(rig)
    second = next(iter(rig.bridge.cards.values()))
    rig.peer.notify("serverRequest/resolved", requestId="perms")
    dispatch(rig)
    assert update(rig, text="1", reply=second.message) == "stale-codex"
    assert B.sessions()[KEY]["answer"] is None


def test_quote_heavy_approval_never_offers_a_truncated_allow(rig, monkeypatch):
    command = 'printf ' + '"' * 450
    monkeypatch.setattr(I, "relay_screen_text", lambda value: pytest.fail("approval used screen relay"))
    rig.peer.ask("item/commandExecution/requestApproval", 91, turnId="turn-a", itemId="item-a",
                 command=command, availableDecisions=["accept", "decline"])
    dispatch(rig)
    card = next(iter(rig.bridge.cards.values()))
    sent = rig.bot.methods("sendMessage")[-1]
    labels = [button["text"] for row in sent.get("reply_markup", {}).get("inline_keyboard", []) for button in row]
    assert "Allow once" not in labels
    assert card.choices == (None, "decline")


def test_large_user_question_falls_back_to_safe_native_card(rig):
    options = [{"label": f"Option {number} " + "x" * 120,
                "description": "y" * 130} for number in range(18)]
    question = "Choose from this user-provided list."
    rig.peer.ask("item/tool/requestUserInput", "large-question", turnId="turn-a", itemId="item-q",
                 questions=[{"id": "choice", "header": "Choice", "question": question,
                             "options": options}])
    dispatch(rig)
    card = next(iter(rig.bridge.cards.values()))
    sent = rig.bot.methods("sendMessage")[-1]
    labels = [button["text"] for row in sent.get("reply_markup", {}).get("inline_keyboard", [])
              for button in row]
    assert "Sensitive question" in sent["text"]
    assert labels == []
    assert all(choice is None for choice in card.choices)


def test_command_worker_survives_unexpected_exception(rig):
    first_ran = threading.Event()
    second_ran = threading.Event()

    def fail():
        first_ran.set()
        raise RuntimeError("synthetic")

    rig.bridge._queue_command(fail)
    rig.bridge._queue_command(second_ran.set)
    assert first_ran.wait(1)
    assert second_ran.wait(1)


def test_rejected_queued_answer_can_be_retried(rig, monkeypatch):
    rig.peer.ask("item/tool/requestUserInput", "retry", turnId="turn-a", itemId="item-q",
                 questions=[{"id": "choice", "header": "Color", "question": "Pick a color.",
                             "options": [{"label": "Blue", "description": "Deep blue"}]}])
    dispatch(rig)
    card = next(iter(rig.bridge.cards.values()))
    failed = threading.Event()

    def reject(*args, **kwargs):
        failed.set()
        raise ProtocolError("stale")

    monkeypatch.setattr(rig.bridge.protocol, "answer", reject)
    assert update(rig, text="1", reply=card.message) == "codex-answer"
    assert failed.wait(1)
    for _ in range(100):
        if any("Answer failed" in item.get("text", "") for item in rig.bot.methods("editMessageText")):
            break
        __import__("time").sleep(.01)
    assert rig.bridge.requests["retry"].answers == {}
    assert any("Answer failed" in item["text"] for item in rig.bot.methods("editMessageText"))
    assert update(rig, text="1", reply=card.message) == "codex-answer"


def test_inbox_commands_are_handed_off_without_waiting(rig, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def slow_send(*args):
        entered.set()
        release.wait(2)

    monkeypatch.setattr(rig.bridge.protocol, "send_text", slow_send)
    started = __import__("time").monotonic()
    assert update(rig, text="queued work") == "queued"
    assert __import__("time").monotonic() - started < .5
    assert entered.wait(1)
    release.set()


def test_bridge_shutdown_leaves_detached_server_running(rig, monkeypatch):
    close = MagicMock()
    monkeypatch.setattr(rig.bridge.server, "close", close)
    stop = threading.Event()
    stop.set()
    rig.bridge.run(stop)
    close.assert_not_called()


def test_phase_unknown_reply_cache_is_bounded(rig):
    for number in range(600):
        rig.bridge._agent_item(THREAD, {"turnId": f"turn-{number}", "item": {
            "id": f"item-{number}", "type": "agentMessage", "text": "sample"}})
    assert len(rig.bridge.unknown) == 512


def test_progress_updates_hold_bridge_lock(rig):
    bridge = rig.bridge

    class LockedProgress(dict):
        def _check(self):
            assert bridge._lock._is_owned()

        def get(self, *args):
            self._check()
            return super().get(*args)

        def __setitem__(self, *args):
            self._check()
            return super().__setitem__(*args)

        def pop(self, *args):
            self._check()
            return super().pop(*args)

        def values(self):
            self._check()
            return super().values()

    with bridge._lock:
        bridge.progress = LockedProgress(bridge.progress)
        bridge.progress[THREAD] = ("turn-lock", CHAT, 900, 0)
        active = bridge.progress.get(THREAD)
    bridge._progress(THREAD, active[0], 1)
    with bridge._lock:
        bridge.progress = dict(bridge.progress)


def test_stop_mark_cannot_be_overwritten_by_progress(rig, monkeypatch):
    rig.peer.notify("turn/started", turn={"id": "turn-stop", "status": "inProgress"})
    dispatch(rig)
    mid = rig.bridge.progress[THREAD][2]
    monkeypatch.setattr(rig.bridge.protocol, "interrupt", MagicMock())
    assert update(rig, data="cx:stop", mid=mid) == "codex-action"
    rig.bridge._progress(THREAD, "turn-stop", 1)
    assert rig.bridge.progress[THREAD][3] == -1


def test_question_buttons_and_free_text_bound_to_each_card(rig):
    rig.peer.ask("item/tool/requestUserInput", "questions", turnId="turn-a", itemId="item-q", isBlocking=True,
                 questions=[{"id": "choice", "header": "Color", "question": "Pick a color.",
                             "options": [{"label": "Blue", "description": "Deep blue"}]},
                            {"id": "free", "header": "Title", "question": "Which title?", "options": None}])
    dispatch(rig)
    cards = dict((c.question, (token, c)) for token, c in rig.bridge.cards.items())
    token, choice = cards["choice"]
    _, free = cards["free"]
    assert update(rig, data=f"cx:{token}:1", mid=free.message) == "stale-codex"
    assert update(rig, data="more:unbuttons", mid=choice.message) == "codex-action"
    assert update(rig, data=f"cx:{token}:1", mid=choice.message) == "stale-codex"
    assert update(rig, text="1", reply=choice.message) == "codex-answer"
    assert update(rig, text="1", reply=choice.message) == "stale-codex"
    assert update(rig, text="Example title", reply=free.message) == "codex-answer"
    assert rig.peer.receive() == {"id": "questions", "result": {
        "answers": {"choice": {"answers": ["Blue"]}, "free": {"answers": ["Example title"]}}}}


def test_cockpit_projection_does_not_expose_legacy_controls(rig):
    row = next(s for s in panel.build_state(rig.cfg, light=True)["sessions"] if s.get("harness") == "codex")
    assert row["native_status"] == "idle"
    assert row["model"] == "synthetic" and row["effort"] == "high"
    assert row["url"].endswith("/" + str(rig.topics.thread_of(KEY)))
    assert "forkable" not in row and "folder" not in row
    rig.bridge.disconnect()
    assert B.sessions()[KEY]["status"] == "unavailable"


def test_reconnect_recovers_active_turn_for_steering(rig):
    rig.peer.server.shutdown(socket.SHUT_WR)
    assert rig.peer.protocol.closed.wait(3)
    rig.peer = rig.connect(turn={"id": "turn-external", "status": "inProgress"})
    _, calls = rig.peer.during(lambda: update(rig), [("turn/steer", {"turnId": "turn-external"})])
    assert calls[0]["params"]["expectedTurnId"] == "turn-external"


def test_stale_socket_is_replaced_without_real_process(monkeypatch, tmp_path):
    import errno
    server = CodexServer()
    server.directory = tmp_path
    server.socket_path = tmp_path / "app.sock"
    server.socket_path.touch()
    calls = []

    def spawn(args, **kwargs):
        calls.append(args)
        assert kwargs["env"]["CODEX_HOME"] == str(server.home)
        assert "PRIVATE_SAMPLE" not in kwargs["env"]
        assert kwargs["env"]["HTTPS_PROXY"] == "http://proxy.invalid"
        assert kwargs["env"]["OPENAI_SAMPLE"] == "existing"
        assert not server.socket_path.exists()
        server.socket_path.touch()
        return SimpleNamespace(poll=lambda: None)

    probe = MagicMock()
    probe.__enter__.return_value.connect.side_effect = [
        ConnectionRefusedError(errno.ECONNREFUSED, "orphaned socket"), None,
    ]
    monkeypatch.setattr(server, "_socket_identity", lambda: server.socket_path.stat().st_ino)
    monkeypatch.setattr("ctb.codex_app_server.socket.socket", lambda *args: probe)
    monkeypatch.setattr("ctb.codex_app_server.shutil.which", lambda _: "/fake/codex")
    monkeypatch.setattr("ctb.codex_app_server.subprocess.Popen", spawn)
    monkeypatch.setenv("PRIVATE_SAMPLE", "must-not-pass")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("OPENAI_SAMPLE", "existing")
    assert server.ensure_started() == server.endpoint
    assert server.ensure_started() == server.endpoint
    assert len(calls) == 1
    assert server.socket_path.stat().st_mode & 0o777 == 0o600


def test_sensitive_approval_requires_native_grant_and_accepts_owner_decline(rig):
    rig.peer.ask("item/commandExecution/requestApproval", 51, turnId="turn-a", itemId="item-a",
                 startedAtMs=1, command="deploy to production", availableDecisions=["accept", "decline"])
    dispatch(rig)
    token, card = next(iter(rig.bridge.cards.items()))
    assert update(rig, data=f"cx:{token}:1", mid=card.message) == "stale-codex"
    assert update(rig, text="1", reply=card.message) == "stale-codex"
    assert update(rig, text="2", reply=card.message) == "codex-answer"
    assert rig.peer.receive() == {"id": 51, "result": {"decision": "decline"}}


def test_permission_grant_contains_only_requested_permissions_for_the_turn(rig):
    profile = {"network": {"enabled": True}}
    rig.peer.ask("item/permissions/requestApproval", 52, turnId="turn-a", itemId="item-b",
                 startedAtMs=1, cwd="/workspace", permissions=profile)
    dispatch(rig)
    card = next(iter(rig.bridge.cards.values()))
    assert update(rig, text="1", reply=card.message) == "codex-answer"
    assert rig.peer.receive() == {"id": 52, "result": {"permissions": profile, "scope": "turn"}}


def test_approval_from_old_connection_cannot_answer_reused_request_id(rig):
    params = {"turnId": "turn-a", "itemId": "item-a", "startedAtMs": 1, "command": "printf example"}
    rig.peer.ask("item/commandExecution/requestApproval", 51, **params)
    dispatch(rig)
    old = next(iter(rig.bridge.cards.values()))
    rig.bridge.disconnect()
    rig.peer = rig.connect()
    rig.peer.ask("item/commandExecution/requestApproval", 51, **params)
    dispatch(rig)
    new = next(iter(rig.bridge.cards.values()))
    assert new.message != old.message
    assert update(rig, text="1", reply=old.message) == "stale-codex"
    assert update(rig, text="1", reply=new.message) == "codex-answer"
    assert rig.peer.receive() == {"id": 51, "result": {"decision": "accept"}}


def test_ambiguous_delivery_stays_visible_and_is_not_retried(rig, monkeypatch):
    attempts = []
    monkeypatch.setattr(B.Mirror, "send_reply", lambda *args, **kwargs: attempts.append(args) or 0)
    item = {"id": "item-a", "type": "agentMessage", "phase": "final_answer", "text": "Final response"}
    for _ in range(2):
        rig.peer.notify("item/completed", turnId="turn-a", completedAtMs=5, item=item)
        dispatch(rig)
    assert len(attempts) == 1
    assert B.sessions()[KEY]["delivery_unconfirmed"] is True


def _failure_reply(rig, mid):
    for _ in range(200):
        replies = [m for m in rig.bot.methods("sendMessage")
                   if (m.get("reply_parameters") or {}).get("message_id") == mid]
        if replies:
            return replies[-1]
        __import__("time").sleep(.01)
    raise AssertionError("no delivery failure reply")


def test_refused_steer_tells_the_owner_it_was_not_delivered(rig):
    rig.peer.server.shutdown(socket.SHUT_WR)
    assert rig.peer.protocol.closed.wait(3)
    rig.peer = rig.connect(turn={"id": "turn-gone", "status": "inProgress"})
    assert update(rig, text="Steer this", mid=981) == "queued"
    request = rig.peer.receive()
    assert request["method"] == "turn/steer" and request["params"]["expectedTurnId"] == "turn-gone"
    rig.peer.write(rig.peer.server, {"id": request["id"], "error": {"code": -32600, "message": "no active turn"}})
    reply = _failure_reply(rig, 981)
    assert reply["text"] == B.t("codex_not_delivered", lang="en")
    assert reply["message_thread_id"] == rig.topics.thread_of(KEY)
    assert reply["chat_id"] == CHAT


def test_timed_out_send_says_it_may_not_have_been_delivered(rig, monkeypatch):
    def slow(*args, **kwargs):
        raise TimeoutError("Codex app-server request timed out")

    monkeypatch.setattr(rig.bridge.protocol, "request", slow)
    assert update(rig, text="Maybe lost", mid=982) == "queued"
    reply = _failure_reply(rig, 982)
    assert reply["text"] == B.t("codex_maybe_not_delivered", lang="en")
    assert B.t("codex_not_delivered", lang="en") not in [m.get("text") for m in rig.bot.methods("sendMessage")]


def test_refused_compact_replies_in_hebrew(rig, monkeypatch):
    rig.cfg.ui_language = "he"

    def refuse(*args, **kwargs):
        raise B.NotDelivered("Codex app-server rejected the request")

    monkeypatch.setattr(rig.bridge.protocol, "request", refuse)
    assert update(rig, text="/compact", mid=983) == "codex-action"
    assert _failure_reply(rig, 983)["text"] == B.t("codex_not_delivered", lang="he")
