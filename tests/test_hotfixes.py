"""Guard tests for the 5 live hotfixes ported to the packaged bridge:
1. Stop hook final-text race (hebrew-reply-guard: _ends_with_text + read_final_transcript)
2. Secret redaction (telegram-midturn redact: keep long lowercase names readable)
3. Reply threading (telegram-reply-mirror answer_to + inbox.take)
4. Delivery latency log (delivery.log)
5. Keychain relay for remote sessions (relay_serve + relay_call via Bot(None))
"""
import json
import fcntl
from contextlib import contextmanager
import os
import re
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path

import pytest

from conftest import FakeBot, GOOD_TOKEN
from ctb import creds as C
from ctb import hooks as H
from ctb import inbox as I
from ctb import mirror as M
from ctb import telegram as T
from ctb.config import Config, TeamConfig, private_dir, state_dir, write_private
from ctb.topics import TopicMap

OWNER = 7


def ev(role, text=None, uuid=None, blocks=None, **extra):
    content = blocks if blocks is not None else [{"type": "text", "text": text}]
    d = {"type": role, "message": {"role": role, "content": content if role == "assistant" or blocks else text}}
    if uuid:
        d["uuid"] = uuid
    d.update(extra)
    return json.dumps(d)


# --- 1. Stop hook final-text race --------------------------------------------

def test_ends_with_text():
    # Only assistant text block -> True
    lines = [
        ev("user", "run query"),
        ev("assistant", blocks=[{"type": "text", "text": "Here is the result."}]),
    ]
    assert H._ends_with_text(lines) is True

    # Assistant tool_use only -> False
    lines = [
        ev("user", "run query"),
        ev("assistant", blocks=[{"type": "tool_use", "name": "bash", "input": {"command": "ls"}}]),
    ]
    assert H._ends_with_text(lines) is False

    # Assistant both text and tool_use -> False (tool_use is present)
    lines = [
        ev("user", "run query"),
        ev("assistant", blocks=[
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "name": "bash", "input": {"command": "ls"}},
        ]),
    ]
    assert H._ends_with_text(lines) is False

    # User event (tool_result) follows assistant -> False
    lines = [
        ev("assistant", blocks=[{"type": "text", "text": "Here is the result."}]),
        ev("user", blocks=[{"type": "tool_result", "content": "file contents"}]),
    ]
    assert H._ends_with_text(lines) is False


def test_read_final_transcript_polls_until_text(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    lines = [
        ev("user", "start"),
        ev("assistant", blocks=[{"type": "tool_use", "name": "bash"}]),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def append_final_text():
        time.sleep(0.1)
        final_line = ev("assistant", blocks=[{"type": "text", "text": "Task completed successfully."}])
        with open(transcript, "a", encoding="utf-8") as f:
            f.write(final_line + "\n")

    writer = threading.Thread(target=append_final_text)
    writer.start()

    read_lines = H.read_final_transcript({"transcript_path": str(transcript)}, wait=1.5, poll=0.05)
    writer.join()

    assert len(read_lines) == 3
    assert H._ends_with_text(read_lines) is True


def test_read_final_transcript_timeout_returns_current(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    lines = [
        ev("user", "start"),
        ev("assistant", blocks=[{"type": "tool_use", "name": "bash"}]),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # When no text arrives, wait expires and current lines are returned without error
    read_lines = H.read_final_transcript({"transcript_path": str(transcript)}, wait=0.1, poll=0.03)
    assert len(read_lines) == 2


# --- 2. Secret redaction -----------------------------------------------------

def test_redact_preserves_long_lowercase_names():
    # Long migration filename (>40 chars, lowercase + numbers + underscores) stays untouched
    migration = "20261028_0001_create_user_profiles_table_with_constraints.py"
    assert len(migration) > 40
    assert C.redact(migration) == migration

    # Long lowercase hyphenated identifier stays untouched
    long_name = "some-very-long-feature-flag-identifier-for-rollout-control"
    assert len(long_name) > 40
    assert C.redact(long_name) == long_name


def test_redact_hides_32_plus_mixed_case_and_digits():
    # 32+ char token with lowercase, uppercase, AND digits -> redacted
    mixed_token = "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9aB1cD"  # gitleaks:allow (dummy value for the redaction test)
    assert len(mixed_token) >= 32
    assert C.redact(mixed_token) == "[redacted]"
    assert C.redact(f"token={mixed_token}; next") == "[redacted] next"

    # Known credential formats are also redacted
    assert C.redact("ghp_" + "A" * 30) == "[redacted]"
    assert C.redact("sk-ant-" + "b" * 30) == "[redacted]"


def test_redact_detail_hides_bearer():
    assert C.redact_detail("Authorization: Bearer secret-auth-value") == "Authorization: [redacted]"


# --- 3. Reply threading ------------------------------------------------------

def test_inbox_take_writes_answer_to(tmp_path):
    box = I.Inbox(tmp_path / "state" / "inbox")
    box.put("sess-xyz", {"text": "question", "id": 445566, "chat": OWNER, "at": time.time()})

    items = box.take("sess-xyz")
    assert len(items) == 1

    marker = state_dir() / "answer-to" / "sess-xyz"
    assert marker.is_file()
    assert json.loads(marker.read_text(encoding="utf-8"))["id"] == 445566


def test_consume_answer_to_reads_and_unlinks():
    answer_dir = private_dir(state_dir() / "answer-to")
    write_private(answer_dir / "sess-abc", json.dumps({"id": 12345, "chat": OWNER, "at": time.time()}))

    val = I.default_inbox().consume_answer_to("sess-abc")
    assert val == {"id": 12345, "chat": OWNER}
    assert not (answer_dir / "sess-abc").exists()

    # Second consume is None
    assert I.default_inbox().consume_answer_to("sess-abc") is None


def test_consume_answer_to_ignores_expired():
    answer_dir = private_dir(state_dir() / "answer-to")
    path = answer_dir / "sess-old"
    write_private(path, json.dumps({"id": 99999, "chat": OWNER, "at": time.time() - 700}))

    # Set mtime to 700 seconds ago (> 600s)
    old_time = time.time() - 700
    os.utime(path, (old_time, old_time))

    val = I.default_inbox().consume_answer_to("sess-old")
    assert val is None
    assert not path.exists()


def test_mirror_send_reply_threads_first_part_only(tmp_path):
    bot = FakeBot()
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    cfg = Config(bot_token="t", owner_chat_id=OWNER, target_language="en", original="before")
    mirror = M.Mirror(cfg, bot, topics)

    mirror.send_reply("Title", "sess-thread", "Here is my answer to you.",
                      answer_to={"chat": OWNER, "id": 888123})

    sends = bot.methods("sendMessage")
    assert len(sends) >= 1
    # First message carries reply_parameters pointing to the answered message
    assert sends[0].get("reply_parameters") == {
        "message_id": 888123,
        "allow_sending_without_reply": True,
    }
    # Marker was consumed
    assert I.default_inbox().consume_answer_to("sess-thread") is None


# --- 4. Delivery latency log -------------------------------------------------

def test_delivery_log_appended_on_receive_and_take(tmp_path):
    bot = FakeBot()
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, OWNER)
    thread = topics.ensure("sess-log", "Title")
    box = I.Inbox(tmp_path / "state" / "inbox")

    class SimpleMedia:
        def content_of(self, m):
            return m.get("text") or ""

    # Message arriving with date 5s ago
    msg_date = time.time() - 5
    upd = {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "date": msg_date,
            "text": "test latency",
            "message_thread_id": thread,
            "chat": {"id": OWNER, "type": "private"},
            "from": {"id": OWNER},
        },
    }
    cfg = Config(bot_token="t", owner_chat_id=OWNER)
    res = I.handle(upd, cfg, bot, topics, box, SimpleMedia(), 0)
    assert res == "queued"

    log_path = state_dir() / "delivery.log"
    assert log_path.is_file()
    # Check mode is 0600
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600

    content = log_path.read_text(encoding="utf-8")
    assert "received after" in content
    assert "sess-log" in content
    # No message content in log
    assert "test latency" not in content

    # Now take items from inbox
    items = box.take("sess-log")
    assert len(items) == 1

    content_after_take = log_path.read_text(encoding="utf-8")
    lines = content_after_take.strip().splitlines()
    assert len(lines) == 2
    # Second line has latency from take
    assert re.search(r"\d\d:\d\d:\d\d sess-log \d+s", lines[1])


# --- 5. Keychain relay for remote sessions -----------------------------------

@pytest.fixture
def short_relay_state(monkeypatch):
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        monkeypatch.setenv("CTB_STATE_DIR", directory)
        yield


def test_relay_serve_and_bot_fallback(short_relay_state):
    stop_event = threading.Event()
    fake_bot = FakeBot()
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)

    # Start relay server on state_dir() / "relay.sock"
    server_thread = threading.Thread(
        target=I.relay_serve,
        args=(cfg,),
        kwargs={"stop_event": stop_event, "bot": fake_bot},
        daemon=True,
    )
    server_thread.start()

    # Wait for socket to appear
    sock_path = C.relay_socket_path()
    for _ in range(30):
        if sock_path.is_socket():
            break
        time.sleep(0.05)
    assert sock_path.is_socket()

    # Socket must be owner-only (mode 0600)
    assert stat.S_IMODE(sock_path.stat().st_mode) == 0o600

    try:
        # A Bot created without a token (token=None) falls back to relay_call
        remote_bot = T.Bot(None)
        res = remote_bot.call("sendMessage", {"chat_id": OWNER, "text": "relayed call"})
        assert fake_bot.methods("sendMessage")[-1]["text"] == "relayed call"

        # Upload via relay
        remote_bot.upload("sendDocument", {"chat_id": OWNER}, "document", "notes.txt", b"hello relay")
        assert len(fake_bot.methods("sendDocument")) == 1
        assert fake_bot.methods("sendDocument")[0]["_file"] == "notes.txt"

        # getUpdates must be rejected
        with pytest.raises(T.TelegramError, match="method_not_relayed"):
            remote_bot.call("getUpdates")
    finally:
        stop_event.set()
        server_thread.join(timeout=2.0)


def test_relay_request_too_large(short_relay_state, monkeypatch):
    monkeypatch.setattr(I, "RELAY_REQUEST_LIMIT", 65536)
    stop_event = threading.Event()
    fake_bot = FakeBot()
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)

    server_thread = threading.Thread(
        target=I.relay_serve,
        args=(cfg,),
        kwargs={"stop_event": stop_event, "bot": fake_bot},
        daemon=True,
    )
    server_thread.start()

    sock_path = C.relay_socket_path()
    for _ in range(30):
        if sock_path.is_socket():
            break
        time.sleep(0.05)

    try:
        # Cross the byte limit; the server must reject before accumulating more.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(str(sock_path))
            chunk = b"A" * (1024 * 1024)  # 1 MB chunk
            try:
                for _ in range(1):
                    conn.sendall(chunk)
                conn.shutdown(socket.SHUT_WR)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the server answers and closes as soon as the limit is crossed
            reply = conn.recv(4096)
            data = json.loads(reply.decode("utf-8"))
            assert data.get("error") == "request_too_large"
    finally:
        stop_event.set()
        server_thread.join(timeout=2.0)


def test_ends_with_text_ignores_api_request_blob():
    """2026-10-28: an api-request-blob record with system text made the hook stop waiting early."""
    from ctb import hooks
    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}}),
             json.dumps({"type": "api-request-blob", "message": {"role": "system", "content": [{"type": "text", "text": "sys"}]}})]
    assert hooks._ends_with_text(lines) is False


# --- Independent review regression guards (findings 1-9) -----------------------

@contextmanager
def running_relay(bot=None, cfg=None):
    stop = threading.Event()
    errors = []
    def run():
        try:
            I.relay_serve(cfg or Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER),
                          bot=bot or FakeBot(), stop_event=stop)
        except BaseException as e:
            errors.append(e)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    path = C.relay_socket_path()
    try:
        deadline = time.monotonic() + 10  # a loaded machine can starve the thread for seconds
        while not path.is_socket() and not errors and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not errors
        assert path.is_socket()
        yield path
    finally:
        stop.set()
        thread.join(10)
        assert not thread.is_alive()
        assert not errors


def test_relay_socket_never_visible_before_listening(short_relay_state, monkeypatch):
    """Issue #78: the socket path appeared at bind(), before listen(); an early client got ECONNREFUSED."""
    real_listen = socket.socket.listen
    def slow_listen(self, *args):
        time.sleep(0.5)  # widen the bind-to-listen window that a loaded machine opens at random
        return real_listen(self, *args)
    monkeypatch.setattr(socket.socket, "listen", slow_listen)
    with running_relay(FakeBot()) as path:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(str(path))  # refused if the path was published before the socket listened


@pytest.mark.parametrize("method", ["getUpdates", "getupdates", "GETUPDATES", "setWebhook",
                                    "deleteWebhook", "logOut", "close", "sendmessage"])
def test_review_1_relay_allowlist(short_relay_state, method):
    bot = FakeBot()
    with running_relay(bot):
        with pytest.raises(T.TelegramError, match="method_not_relayed"):
            C.relay_call({"method": method, "params": {"chat_id": OWNER}})
    assert bot.calls == []


@pytest.mark.parametrize("req,error", [
    ({"method": "sendMessage", "params": {"chat_id": 999}}, "chat_not_relayed"),
    ({"method": "sendMessage", "params": {}}, "chat_not_relayed"),
    ({"kind": "upload", "method": "sendDocument", "params": {"chat_id": 999}}, "chat_not_relayed"),
    ({"method": "sendMessage", "params": [OWNER]}, "invalid_params"),
    ({"kind": "unexpected", "method": "sendMessage"}, "method_not_relayed"),
])
def test_review_1_relay_validates_target(short_relay_state, req, error):
    bot = FakeBot()
    with running_relay(bot):
        with pytest.raises(T.TelegramError, match=error):
            C.relay_call(req)
    assert bot.calls == []


@pytest.mark.parametrize("value", ["a1" * 32, "A1" * 32, "lowcasecredential987654321"])
def test_review_2_flag_and_hex_redaction(value):
    assert value not in C.redact_detail("deploy --token " + value)
    assert value not in C.redact_detail("auth: " + value)
    assert C.redact("a1" * 32) == "[redacted]"
    sha = "a1" * 20
    assert C.redact_detail("git show " + sha) == "git show " + sha


@pytest.mark.parametrize("value", ["a1" * 20, "a" * 40, "1" * 40, "Ab12" * 10])
def test_review_2_bare_hex_requires_git_context(value):
    assert C.redact(value) == "[redacted]"
    assert C.redact("unlabelled value: " + value) == "unlabelled value: [redacted]"
    for context in ("commit ", "checkout ", "sha: ", "rev=", "git show "):
        assert C.redact(context + value) == context + value
    assert C.redact(value + " Fix relay") == value + " Fix relay"
    assert C.redact("[main " + value + "] Fix relay") == "[main " + value + "] Fix relay"
    assert C.redact("commit " + "a1" * 32) == "commit [redacted]"


def test_review_3_duplicate_listener_does_not_touch_relay(monkeypatch):
    base = private_dir(state_dir())
    sentinel = base / "relay.sock"
    sentinel.write_text("existing relay")
    starts = []
    monkeypatch.setattr(I.threading.Thread, "start", lambda self: starts.append(self))
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)
    with open(base / "updates.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert I.serve(cfg) == 1
    assert sentinel.read_text() == "existing relay"
    assert starts == []


def test_review_3_credentials_checked_before_relay(monkeypatch):
    starts = []
    def unavailable(self):
        raise RuntimeError("credentials unavailable")
    monkeypatch.setattr(Config, "token", unavailable)
    monkeypatch.setattr(I.threading.Thread, "start", lambda self: starts.append(self))
    with pytest.raises(RuntimeError, match="credentials unavailable"):
        I.serve(Config(owner_chat_id=OWNER))
    assert starts == []
    assert not C.relay_socket_path().exists()
    # A failed credential lookup also releases the listener lease.
    with open(state_dir() / "updates.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_review_3_cleanup_preserves_replacement_socket(short_relay_state):
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with running_relay() as path:
            path.unlink()
            replacement.bind(str(path))
        assert path.is_socket()
    finally:
        replacement.close()
        path.unlink(missing_ok=True)


def test_review_4_idle_connections_expire_and_cap_workers(short_relay_state, monkeypatch):
    monkeypatch.setattr(I, "RELAY_CONNECTION_LIMIT", 2)
    monkeypatch.setattr(I, "RELAY_IO_TIMEOUT", 0.25)
    acquired = threading.Event()
    real_semaphore = threading.BoundedSemaphore
    class ObservedSemaphore:
        def __init__(self, n):
            self.inner = real_semaphore(n)
            self.count = 0
        def acquire(self, **kwargs):
            result = self.inner.acquire(**kwargs)
            if result:
                self.count += 1
                if self.count == 2:
                    acquired.set()
            return result
        def release(self):
            self.inner.release()
    monkeypatch.setattr(I.threading, "BoundedSemaphore", ObservedSemaphore)
    connections = []
    try:
        with running_relay() as path:
            for _ in range(2):
                c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                c.settimeout(2)
                c.connect(str(path))
                connections.append(c)
            assert acquired.wait(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as excess:
                excess.settimeout(1)
                excess.connect(str(path))
                assert excess.recv(1) == b""  # refused without a third worker
            for c in connections:
                assert json.loads(c.recv(1024))["error"] == "relay_timeout"
            assert C.relay_call({"method": "sendMessage", "params": {"chat_id": OWNER}})
    finally:
        for c in connections:
            c.close()


@pytest.mark.parametrize("text", [
    "https://github.com/example-user/example-repo/pull/123",
    "/home/user/projects/migration_20261028_add_user_profiles_table.sql",
])
def test_review_5_structural_paths_and_urls_stay_readable(text):
    assert C.redact(text) == text
    assert C.redact_detail(text) == text
    secret = "a1" * 32
    url = "https://example.com/path?token=" + secret
    assert C.redact(url).startswith("https://example.com/path?")
    assert secret not in C.redact(url)


def test_review_5_segments_preserve_hosts_short_paths_and_known_ids():
    secret = "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9aB1cD"  # gitleaks:allow (dummy value for the redaction test)
    uuid = "aB12cd34-Ef56-7890-aB12-cD34ef567890"
    sha = "Ab12" * 10
    urls = [
        "https://github.com/example-user/example-repo/pull/123",
        "https://github.com/example-user/example-repo/commit/" + sha,
        "https://example.supabase.co/rest/v1/items",
        "https://supabase.com/dashboard/org/" + uuid + "/settings",
    ]
    for text in urls:
        assert C.redact(text) == text
        assert C.redact_detail(text) == text
    for prefix in ("https://example.com/short/", "/home/user/projects/"):
        text = prefix + secret + "/ok"
        assert C.redact(text) == prefix + "[redacted]/ok"
        assert C.redact_detail(text) == prefix + "[redacted]/ok"
    # Several short segments must never combine into one random-looking value.
    short_path = "https://example.com/Ab12Cd34/Ef56Gh78/Ij90Kl12/Mn34Op56"
    assert C.redact(short_path) == short_path


@pytest.mark.parametrize("host", ["aB3dE5gH7jK9mN1pQ3sT5vW7yZ9aB1cD.example.com",
                                 "a1" * 20 + ".example.com"])
def test_review_5_long_host_is_preserved_but_userinfo_and_path_are_redacted(host):
    secret = "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9aB1cD"  # gitleaks:allow (dummy value for the redaction test)
    url = "https://" + host + "/short/" + secret + "/ok"
    expected = "https://" + host + "/short/[redacted]/ok"
    assert C.redact(url) == expected
    assert C.redact_detail(url) == expected
    with_userinfo = "https://user:" + secret + "@" + host + "/ok"
    assert C.redact(with_userinfo) == "https://user:[redacted]@" + host + "/ok"


def test_review_6_partial_trailing_record_waits_for_completion(tmp_path):
    path = tmp_path / "transcript.jsonl"
    prefix = ev("user", "current") + "\n" + ev("assistant", "Working...") + "\n"
    final = ev("assistant", "The final answer", uuid="final")
    cut = len(final) // 2
    path.write_text(prefix + final[:cut])
    assert not H._ends_with_text(path.read_text().splitlines(True))
    def complete():
        time.sleep(0.08)
        with path.open("a") as f:
            f.write(final[cut:] + "\n")
    writer = threading.Thread(target=complete)
    writer.start()
    lines = H.read_final_transcript({"transcript_path": str(path)}, wait=1, poll=0.01)
    writer.join()
    assert json.loads(lines[-1])["uuid"] == "final"
    assert H._ends_with_text(lines)
    assert not H._ends_with_text([ev("assistant", "previous"), ev("user", "new prompt")])
    assert not H._ends_with_text([ev("assistant", "  ")])


def test_review_6_timeout_skips_stale_text_and_next_stop_retries(tmp_path, monkeypatch):
    from ctb import progress, topics
    path = tmp_path / "transcript.jsonl"
    prefix = ev("user", "current") + "\n" + ev("assistant", "Earlier progress", uuid="old") + "\n"
    final = ev("assistant", "Final answer", uuid="final")
    path.write_text(prefix + final[:20])
    # Exercise the actual deadline branch without a three-second sleep.
    ticks = iter([0.0, 3.0])
    monkeypatch.setattr(H.time, "monotonic", lambda: next(ticks))
    assert H.read_final_transcript({"transcript_path": str(path)}) == []

    box = I.default_inbox()
    box.put("s", {"id": 11, "chat": OWNER, "text": "question"})
    box.take("s")
    marker = state_dir() / "answer-to" / "s"
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, translate=False)
    sent = []
    monkeypatch.setattr(H, "load_config", lambda: cfg)
    monkeypatch.setattr(H, "spawn", lambda args, text=None: sent.append((args, text)))
    real_read = H.read_final_transcript
    monkeypatch.setattr(H, "read_final_transcript", lambda data, **kw: real_read(data, wait=0))
    monkeypatch.setattr(H.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(progress, "run_send_progress", lambda *a, **kw: 0)
    monkeypatch.setattr(topics, "set_state", lambda *a, **kw: 0)
    monkeypatch.setattr(H, "drop_progress", lambda *a: None)
    monkeypatch.setattr(M, "alert", lambda *a: None)
    mirrors = []
    monkeypatch.setattr(M, "run_reply", lambda *a, **kw: mirrors.append((a, kw)) or 0)
    data = json.dumps({"session_id": "s", "transcript_path": str(path)})
    assert H.hook_stop(data) == 0
    worker = json.loads(next(t for a, t in sent if a[0] == "finish-stop"))
    assert H.finish_stop(worker) == 0
    assert mirrors == []
    assert marker.exists()
    assert H._seen("s") == set()

    path.write_text(prefix + final + "\n")
    sent.clear()
    assert H.hook_stop(data) == 0
    worker = json.loads(next(t for a, t in sent if a[0] == "finish-stop"))
    assert H.finish_stop(worker) == 0
    assert len(mirrors) == 1
    assert "Final answer" in mirrors[0][0][3]
    assert mirrors[0][1]["answer_to"]["id"] == 11
    assert not marker.exists()


@pytest.mark.parametrize("transcript", ["missing", "empty", "seen", "read_error"])
def test_review_7_hook_preserves_target_without_new_reply(tmp_path, monkeypatch, transcript):
    from ctb import progress, topics
    box = I.default_inbox()
    box.put("s", {"id": 11, "chat": OWNER, "text": "question"})
    box.take("s")
    path = tmp_path / "transcript.jsonl"
    if transcript == "empty":
        path.write_text(ev("assistant", "  ") + "\n")
    elif transcript == "seen":
        path.write_text(ev("assistant", "Already sent", uuid="old") + "\n")
        H._mark("s", ["old:0"])
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, translate=False)
    monkeypatch.setattr(H, "load_config", lambda: cfg)
    if transcript == "read_error":
        def fail(data, **kw):
            raise OSError("read failed")
        monkeypatch.setattr(H, "read_final_transcript", fail)
    else:
        real_read = H.read_final_transcript
        monkeypatch.setattr(H, "read_final_transcript", lambda data, **kw: real_read(data, wait=0))
    monkeypatch.setattr(progress, "run_send_progress", lambda *a, **kw: 0)
    monkeypatch.setattr(topics, "set_state", lambda *a, **kw: 0)
    monkeypatch.setattr(H, "drop_progress", lambda *a: None)
    monkeypatch.setattr(M, "alert", lambda *a: None)
    monkeypatch.setattr(M, "run_reply", lambda *a, **kw: pytest.fail("no fresh reply should be delivered"))
    sent = []
    monkeypatch.setattr(H, "spawn", lambda args, text=None: sent.append((args, text)))
    assert H.hook_stop(json.dumps({"session_id": "s", "transcript_path": str(path)})) == 0
    worker = json.loads(next(t for a, t in sent if a[0] == "finish-stop"))
    assert H.finish_stop(worker) == (1 if transcript == "read_error" else 0)
    assert box.consume_answer_to("s") == {"chat": OWNER, "id": 11}


def test_review_new_relay_allows_only_owner_and_configured_team(short_relay_state):
    bot = FakeBot()
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, team=TeamConfig(chat_id=-999))
    with running_relay(bot, cfg):
        remote = T.Bot(None)
        for chat in (OWNER, -999, str(-999)):
            remote.call("sendMessage", {"chat_id": chat, "text": "allowed"})
            remote.upload("sendDocument", {"chat_id": chat}, "document", "notes.txt", b"notes")
        for chat in (0, -998, 999, "@other_chat"):
            with pytest.raises(T.TelegramError, match="chat_not_relayed"):
                remote.call("sendMessage", {"chat_id": chat, "text": "denied"})
            with pytest.raises(T.TelegramError, match="chat_not_relayed"):
                remote.upload("sendDocument", {"chat_id": chat}, "document", "notes.txt", b"notes")
    assert len(bot.methods("sendMessage")) == 3
    assert len(bot.methods("sendDocument")) == 3


def test_relay_allows_the_registered_alerts_channel_for_sending_only(short_relay_state):
    from ctb import alerts
    channel = -1007777
    write_private(state_dir() / alerts.FILE, json.dumps({"id": channel, "title": "A", "bot": "b"}))

    class Admin(FakeBot):  # the owner administers the channel, so the file's id is verified once
        def call(self, method, params=None, timeout=15):
            if method == "getChat":
                return {"id": channel, "type": "channel"}
            if method == "getChatAdministrators":
                return [{"user": {"id": OWNER}}]
            return super().call(method, params, timeout)
    bot = Admin()
    with running_relay(bot):
        remote = T.Bot(None)
        remote.call("sendMessage", {"chat_id": channel, "text": "ring"})
        remote.upload("sendDocument", {"chat_id": channel}, "document", "n.txt", b"n")
        for method in ("editMessageText", "deleteMessage", "pinChatMessage", "sendChatAction"):
            with pytest.raises(T.TelegramError, match="chat_not_relayed"):
                remote.call(method, {"chat_id": channel, "message_id": 1, "text": "x"})
        for chat in (-1007778, 0, "@other"):   # any other chat stays refused
            with pytest.raises(T.TelegramError, match="chat_not_relayed"):
                remote.call("sendMessage", {"chat_id": chat, "text": "denied"})
    assert len(bot.methods("sendMessage")) == 1 and len(bot.methods("sendDocument")) == 1


def test_review_new_relay_disabled_team_does_not_allow_zero(short_relay_state):
    with running_relay():
        for chat in (0, -999):
            with pytest.raises(T.TelegramError, match="chat_not_relayed"):
                T.Bot(None).call("sendMessage", {"chat_id": chat, "text": "denied"})


def test_review_7_detached_reply_keeps_frozen_target(tmp_path, monkeypatch):
    box = I.default_inbox()
    box.put("s", {"id": 11, "chat": OWNER, "text": "old"})
    box.take("s")
    pending = []
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, translate=False, target_language="en")
    monkeypatch.setattr(H, "spawn", lambda args, text=None: pending.append((args, text)))
    H.stop_output({"session_id": "s"}, [ev("assistant", "Old answer", uuid="a")], cfg)
    args, text = next((a, t) for a, t in pending if a[0] == "mirror")
    assert "--reply-chat=7" in args and "--reply-to=11" in args
    box.put("s", {"id": 22, "chat": OWNER, "text": "new"})
    box.take("s")
    # Execute the old child after a newer turn's target has been written.
    from ctb import cli
    bot = FakeBot()
    m = M.Mirror(cfg, bot, TopicMap(tmp_path / "topics.json", bot, OWNER))
    monkeypatch.setattr(cli.C, "load", lambda: cfg)
    monkeypatch.setattr(cli, "_read_text", lambda args: text)
    monkeypatch.setattr(M, "build", lambda cfg: m)
    assert cli._cmd_mirror(cli._parser().parse_args(args)) == 0
    assert bot.methods("sendMessage")[0]["reply_parameters"]["message_id"] == 11
    assert box.consume_answer_to("s") == {"chat": OWNER, "id": 22}


def test_review_7_writes_and_claims_share_session_lock(monkeypatch):
    box = I.default_inbox()
    box.put("s", {"id": 1, "chat": OWNER})
    inside = threading.Event()
    release = threading.Event()
    real_write = I.write_private
    def write(path, text):
        if path.parent.name == "answer-to":
            inside.set()
            assert release.wait(2)
        real_write(path, text)
    monkeypatch.setattr(I, "write_private", write)
    taker = threading.Thread(target=box.take, args=("s",))
    taker.start()
    assert inside.wait(1)
    result = []
    claimer = threading.Thread(target=lambda: result.append(box.consume_answer_to("s")))
    claimer.start()
    claimer.join(0.05)
    assert claimer.is_alive()  # cannot read/delete between the writer's operations
    release.set()
    taker.join(2)
    claimer.join(2)
    assert result == [{"chat": OWNER, "id": 1}]


def test_review_8_team_message_id_never_threads_in_owner_chat(tmp_path):
    box = I.default_inbox()
    box.put("s", {"id": 123, "chat": -999, "source": "team", "text": "team question"})
    box.take("s")
    target = box.consume_answer_to("s")
    assert target == {"chat": -999, "id": 123}
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)
    bot = FakeBot()
    m = M.Mirror(cfg, bot, TopicMap(tmp_path / "topics.json", bot, OWNER))
    m.send_reply("Title", "s", "Answer", answer_to=target)
    assert "reply_parameters" not in bot.methods("sendMessage")[0]


def test_review_9_relay_accepts_full_50_mib_document(short_relay_state):
    bot = FakeBot()
    with running_relay(bot):
        T.Bot(None).upload("sendDocument", {"chat_id": OWNER}, "document", "large.bin",
                           b"x" * (50 * 1024 * 1024))
    assert bot.methods("sendDocument")[0]["_bytes"] == 50 * 1024 * 1024



def test_review_4_trickle_does_not_extend_total_read_deadline(short_relay_state, monkeypatch):
    # Wide margins so a loaded machine does not flake it: a restarted budget would wait the full 0.8 s.
    monkeypatch.setattr(I, "RELAY_IO_TIMEOUT", 0.8)
    with running_relay() as path:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(3)
            conn.connect(str(path))
            conn.sendall(b"{")
            time.sleep(0.5)
            conn.sendall(b" ")
            # The second byte must not restart the entire read budget.
            started = time.monotonic()
            assert json.loads(conn.recv(1024))["error"] == "relay_timeout"
            assert time.monotonic() - started < 0.7


def test_review_7_prompt_clears_previous_target(monkeypatch):
    box = I.default_inbox()
    box.put("s", {"id": 1, "chat": OWNER})
    box.take("s")
    H.prompt_output({"session_id": "s"}, Config(owner_chat_id=OWNER, bot_token=GOOD_TOKEN))
    assert box.consume_answer_to("s") is None


def test_review_7_stop_preserves_target_updated_during_transcript_wait(tmp_path, monkeypatch):
    from ctb import progress, topics
    box = I.default_inbox()
    box.put("s", {"id": 1, "chat": OWNER})
    box.take("s")
    def read(data, **kw):
        box.put("s", {"id": 2, "chat": OWNER})
        box.take("s")
        return []
    monkeypatch.setattr(H, "read_final_transcript", read)
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)
    monkeypatch.setattr(H, "load_config", lambda: cfg)
    monkeypatch.setattr(progress, "run_send_progress", lambda *a, **kw: 0)
    monkeypatch.setattr(topics, "set_state", lambda *a, **kw: 0)
    monkeypatch.setattr(H, "drop_progress", lambda *a: None)
    monkeypatch.setattr(M, "alert", lambda *a: None)
    pending = []
    monkeypatch.setattr(H, "spawn", lambda args, text=None: pending.append((args, text)))
    assert H.hook_stop(json.dumps({"session_id": "s", "transcript_path": str(tmp_path / "missing")})) == 0
    worker = json.loads(next(t for a, t in pending if a[0] == "finish-stop"))
    assert worker["_answer_to"]["id"] == 1  # original turn remains frozen
    assert H.finish_stop(worker) == 0
    assert box.consume_answer_to("s") == {"chat": OWNER, "id": 2}


@pytest.mark.parametrize("replacement, delivery_code, expected", [(False, 0, None),
                                                               (False, 1, 1),
                                                               (True, 0, 2),
                                                               (True, 1, 2)])
def test_detached_stop_acknowledges_only_successful_frozen_target(
        tmp_path, monkeypatch, replacement, delivery_code, expected):
    from ctb import progress, topics
    box = I.default_inbox()
    box.put("s", {"id": 1, "chat": OWNER})
    box.take("s")
    path = tmp_path / "transcript.jsonl"
    path.write_text(ev("user", "question") + "\n" + ev("assistant", "Answer", uuid="a") + "\n")
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)
    monkeypatch.setattr(H, "load_config", lambda: cfg)
    monkeypatch.setattr(progress, "run_send_progress", lambda *a, **kw: 0)
    monkeypatch.setattr(topics, "set_state", lambda *a, **kw: 0)
    monkeypatch.setattr(H, "drop_progress", lambda *a: None)
    monkeypatch.setattr(M, "alert", lambda *a: None)
    pending, delivered = [], []
    monkeypatch.setattr(H, "spawn", lambda args, text=None: pending.append((args, text)))
    def deliver(*args, **kw):
        delivered.append(kw["answer_to"])
        return delivery_code
    monkeypatch.setattr(M, "run_reply", deliver)
    assert H.hook_stop(json.dumps({"session_id": "s", "transcript_path": str(path)})) == 0
    if replacement:
        box.put("s", {"id": 2, "chat": OWNER})
        box.take("s")
    worker = json.loads(next(t for a, t in pending if a[0] == "finish-stop"))
    assert H.finish_stop(worker) == 0
    assert delivered[0]["chat"] == OWNER and delivered[0]["id"] == 1
    assert box.consume_answer_to("s") == ({"chat": OWNER, "id": expected} if expected else None)



def test_review_7_reply_metadata_failure_does_not_lose_delivery(monkeypatch):
    box = I.default_inbox()
    item = {"id": 1, "chat": OWNER, "text": "keep this message"}
    box.put("s", item)
    real_write = I.write_private
    def write(path, text):
        if path.parent.name == "answer-to":
            raise OSError("metadata unavailable")
        real_write(path, text)
    monkeypatch.setattr(I, "write_private", write)
    assert box.take("s") == [item]
    assert not box.pending("s")


def test_review_4_blocked_response_write_releases_worker(short_relay_state, monkeypatch):
    monkeypatch.setattr(I, "RELAY_CONNECTION_LIMIT", 1)
    monkeypatch.setattr(I, "RELAY_IO_TIMEOUT", 0.2)
    released = threading.Event()
    real_semaphore = threading.BoundedSemaphore
    class ObservedSemaphore:
        def __init__(self, n):
            self.inner = real_semaphore(n)
        def acquire(self, **kwargs):
            return self.inner.acquire(**kwargs)
        def release(self):
            self.inner.release()
            released.set()
    class LargeResponseBot(FakeBot):
        def call(self, method, params=None, timeout=15):
            if (params or {}).get("text") == "large-response":
                return {"value": "x" * (2 * 1024 * 1024)}
            return super().call(method, params, timeout)
    monkeypatch.setattr(I.threading, "BoundedSemaphore", ObservedSemaphore)
    with running_relay(LargeResponseBot()) as path:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(1)
            conn.connect(str(path))
            conn.sendall(json.dumps({"method": "sendMessage", "params": {
                "chat_id": OWNER, "text": "large-response"}}).encode())
            conn.shutdown(socket.SHUT_WR)
            # Deliberately never read the large response; sendall must time out.
            assert released.wait(2)
            assert C.relay_call({"method": "sendMessage", "params": {"chat_id": OWNER}})


def test_reply_keyboard_matches_status_card_layout(tmp_path):
    # One keyboard everywhere: replies used the old 9-button layout while the status card used the
    # labeled one, so topics looked different from each other.
    bot = FakeBot()
    for lang in ("en", "he"):
        cfg = Config(bot_token="t", owner_chat_id=OWNER, ui_language=lang)
        m = M.Mirror(cfg, bot, TopicMap(tmp_path / "topics.json", bot, OWNER))
        assert M.keyboard_signature(m._reply_keyboard()) == M.keyboard_signature(M.get_keyboard(lang))
