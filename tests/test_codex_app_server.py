"""Synthetic app-server protocol coverage; no Codex process is started."""
import json
import socket
import threading

import pytest

from ctb.codex_app_server import CodexProtocol, ProtocolError


class FakeServer:
    def __init__(self):
        self.client, self.server = socket.socketpair()
        self.sent = []
        self.protocol = CodexProtocol(self.send, owner_id=7, on_event=lambda m, p: self.sent.append((m, p)))

    def send(self, message):
        self.client.sendall((json.dumps(message) + "\n").encode())

    def reply(self, request, result):
        self.protocol.feed({"id": request["id"], "result": result})

    def call(self, function, result):
        box = {}
        thread = threading.Thread(target=lambda: box.setdefault("value", function()))
        thread.start()
        request = json.loads(self.server.makefile().readline())
        self.reply(request, result)
        thread.join()
        return request, box["value"]


def test_start_progress_reply_and_usage():
    fake = FakeServer()
    request, thread_id = fake.call(lambda: fake.protocol.start_thread(cwd="."),
                                   {"thread": {"id": "thread-1"}})
    assert request["method"] == "thread/start" and thread_id == "thread-1"
    fake.protocol.feed({"method": "turn/started", "params": {
        "threadId": thread_id, "turn": {"id": "turn-1"}}})
    fake.protocol.feed({"method": "item/agentMessage/delta", "params": {
        "threadId": thread_id, "itemId": "reply-1", "delta": "Hello"}})
    fake.protocol.feed({"method": "thread/tokenUsage/updated", "params": {
        "threadId": thread_id, "tokenUsage": {"totalTokens": 12}}})
    assert fake.protocol.threads[thread_id].status == "working"
    assert fake.protocol.threads[thread_id].usage == {"totalTokens": 12}
    assert fake.sent[-2][0] == "item/agentMessage/delta"


def test_start_vs_steer_and_interrupt():
    fake = FakeServer()
    fake.protocol.threads["thread-1"] = __import__(
        "ctb.codex_app_server", fromlist=["ThreadState"]).ThreadState("thread-1")
    request, _ = fake.call(lambda: fake.protocol.send_text("thread-1", "first"),
                           {"turn": {"id": "turn-1"}})
    assert request["method"] == "turn/start"
    request, _ = fake.call(lambda: fake.protocol.send_text("thread-1", "more"),
                           {"turnId": "turn-1"})
    assert request["method"] == "turn/steer"
    assert request["params"]["expectedTurnId"] == "turn-1"
    request, _ = fake.call(lambda: fake.protocol.interrupt("thread-1"), {})
    assert request == {"method": "turn/interrupt", "id": request["id"],
                       "params": {"threadId": "thread-1", "turnId": "turn-1"}}


def test_interrupt_unknown_thread_is_a_protocol_error():
    fake = FakeServer()
    with pytest.raises(ProtocolError, match="no matching active turn"):
        fake.protocol.interrupt("missing")


def test_approval_round_trip_stale_duplicate_and_non_owner():
    fake = FakeServer()
    message = {"id": 88, "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1",
        "availableDecisions": ["accept", "decline"]}}
    fake.protocol.feed(message)
    assert fake.protocol.approvals[88].choices == ("accept", "decline")
    with pytest.raises(PermissionError):
        fake.protocol.answer(88, "accept", actor_id=9)
    fake.protocol.answer(88, "decline", actor_id=7)
    answer = json.loads(fake.server.makefile().readline())
    assert answer == {"id": 88, "result": {"decision": "decline"}}
    with pytest.raises(ProtocolError):
        fake.protocol.answer(88, "decline", actor_id=7)
    fake.protocol.feed(message)
    assert 88 not in fake.protocol.approvals


def test_resolved_expires_approval_and_completed_is_deduplicated(tmp_path):
    events = []
    protocol = CodexProtocol(lambda message: None, owner_id=7,
                             on_event=lambda method, params: events.append(method),
                             cursor_path=tmp_path / "cursor.json")
    protocol.feed({"id": 4, "method": "item/fileChange/requestApproval", "params": {
        "threadId": "thread-1", "availableDecisions": ["decline"]}})
    protocol.feed({"method": "serverRequest/resolved", "params": {"requestId": 4}})
    with pytest.raises(ProtocolError):
        protocol.answer(4, "decline", actor_id=7)
    completed = {"method": "item/completed", "params": {
        "threadId": "thread-1", "item": {"id": "reply-1", "type": "agentMessage", "text": "Done"}}}
    protocol.feed(completed)
    protocol.feed(completed)
    assert events.count("item/completed") == 1
    restarted = CodexProtocol(lambda message: None, cursor_path=tmp_path / "cursor.json",
                              on_event=lambda method, params: events.append(method))
    restarted.feed(completed)
    assert events.count("item/completed") == 1


def test_socketpair_websocket_initialization_and_eof(monkeypatch, tmp_path):
    import base64
    import hashlib
    from ctb import codex_app_server as app

    client, peer = socket.socketpair()
    peer.settimeout(3)

    class ConnectedSocket:
        def connect(self, address):
            assert address == str(tmp_path / "app.sock")

        def __getattr__(self, name):
            return getattr(client, name)

    server = app.CodexServer()
    server.socket_path = tmp_path / "app.sock"
    monkeypatch.setattr(app.socket, "socket", lambda *args: ConnectedSocket())
    protocol = []
    thread = threading.Thread(target=lambda: protocol.append(server.connect()), daemon=True)
    thread.start()

    def frame(message):
        payload = json.dumps(message).encode()
        return bytes((0x81, len(payload))) + payload

    with peer, peer.makefile("rb") as stream:
        headers = {}
        while (line := stream.readline()) not in (b"\r\n", b""):
            if b": " in line:
                key, value = line.strip().split(b": ", 1)
                headers[key] = value
        suffix = b"-".join((b"258EAFA5", b"E914", b"47DA", b"95CA", b"C5AB0DC85B11"))
        accept = base64.b64encode(hashlib.sha1(headers[b"Sec-WebSocket-Key"] + suffix).digest())
        peer.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")

        def receive():
            first, second = stream.read(2)
            assert first == 0x81 and second & 128
            length = second & 127
            if length == 126:
                length = int.from_bytes(stream.read(2), "big")
            mask = stream.read(4)
            return json.loads(bytes(byte ^ mask[i % 4] for i, byte in enumerate(stream.read(length))))

        init = receive()
        assert init["method"] == "initialize"
        assert init["params"]["capabilities"] == {"experimentalApi": True, "requiredHumanApproval": True}
        peer.sendall(frame({"id": init["id"], "result": {}}))
        assert receive() == {"method": "initialized"}
        thread.join(3)
        assert len(protocol) == 1
        peer.shutdown(socket.SHUT_RDWR)

    assert protocol[0].closed.wait(3)


def test_late_completion_expires_only_the_old_turns_approvals():
    from ctb.codex_app_server import ThreadState

    sent = []
    protocol = CodexProtocol(sent.append, owner_id=7)
    protocol.threads["thread-1"] = ThreadState("thread-1")

    def turn(method, turn_id):
        protocol.feed({"method": method, "params": {"threadId": "thread-1", "turn": {"id": turn_id}}})

    def approval(ident, turn_id):
        protocol.feed({"method": "item/commandExecution/requestApproval", "id": ident,
                       "params": {"threadId": "thread-1", "turnId": turn_id}})

    turn("turn/started", "first")
    approval(1, "first")
    turn("turn/started", "second")
    approval(2, "second")
    turn("turn/completed", "first")
    assert protocol.threads["thread-1"].active_turn_id == "second"
    with pytest.raises(ProtocolError):
        protocol.answer(1, "accept", actor_id=7)
    protocol.answer(2, "decline", actor_id=7)
    assert sent == [{"id": 2, "result": {"decision": "decline"}}]


def test_answer_history_is_bounded():
    protocol = CodexProtocol(lambda message: None)
    for ident in range(600):
        protocol._remember_answered(ident)
    assert len(protocol._answered) == 512
    assert 0 not in protocol._answered and 599 in protocol._answered


def test_request_timeout_does_not_close_connection():
    protocol = CodexProtocol(lambda message: None)
    with pytest.raises(TimeoutError):
        protocol.request("sample", timeout=.01)
    assert not protocol.closed.is_set()
