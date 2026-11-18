import json
import os
import pytest
import socket
import time

from ctb import config as C
from ctb.runner import Runner, RunnerError, event_summary

UUID_ONE = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def short_dir():
    """macOS keeps Unix socket paths under 104 bytes, and pytest's tmp_path is often longer."""
    import shutil
    import tempfile
    path = tempfile.mkdtemp(prefix="ctb", dir="/tmp")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolated_claude_home(tmp_path, monkeypatch):
    """Transcripts are always looked up under a temporary Claude config folder, never the real one."""
    home = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


@pytest.fixture
def record(tmp_path):
    return {"runner_mode": "headless", "auth_mode": "api_key", "cwd": str(tmp_path),
            "claude_binary": "/fake/claude", "model": "haiku", "permission_mode": "plan",
            "max_budget_usd": 1}


def test_switch_mode_and_api_key_are_all_required(tmp_path, monkeypatch, record):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    runner = Runner("s", record)
    runner.validate(True)
    for enabled, change, code in (
        (False, {}, "headless_disabled"),
        (True, {"runner_mode": "tmux"}, "runner_mode_tmux"),
        (True, {"auth_mode": "subscription"}, "api_key_required"),
    ):
        denied = Runner("s", record | change)
        with pytest.raises(RunnerError, match=code):
            denied.validate(enabled)


def test_argv_is_stream_json_permission_host_and_resume(tmp_path, monkeypatch, record):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    runner = Runner("s", record)
    runner.claude_session = "resumed-id"
    argv = runner.argv()
    assert argv[:2] == ["/fake/claude", "-p"]
    assert "--input-format" in argv and "--output-format" in argv
    assert "--permission-prompt-tool" in argv and "--replay-user-messages" in argv
    assert argv[argv.index("--resume") + 1] == "resumed-id"
    assert "--max-budget-usd" in argv and "--max-turns" not in argv


def test_metadata_log_never_contains_message_tool_or_usage_content():
    secret = "not-for-the-log"
    event = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": secret},
        {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"key": secret}},
    ], "usage": {"input_tokens": 123}}, "result": secret}
    encoded = json.dumps(event_summary(event, 7))
    assert secret not in encoded and "input_tokens" not in encoded and "tool-1" not in encoded
    assert json.loads(encoded)["block_types"] == ["text", "tool_use"]


class _Input:
    def __init__(self):
        self.lines = []

    def write(self, line):
        self.lines.append(json.loads(line))

    def flush(self):
        pass


class _Child:
    def __init__(self):
        self.stdin = _Input()


def _transcript(runner, *items):
    path = runner._transcript_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in items))
    return path


def test_stream_replay_is_not_delivery_truth(tmp_path, monkeypatch, record):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    runner = Runner("s", record)
    runner.claude_session = "fake-session"
    _transcript(runner)
    runner.child = _Child()
    reply = runner.command({"command": "send", "uuid": UUID_ONE, "content": "private message"})
    assert reply == {"accepted": True, "uuid": UUID_ONE}
    assert UUID_ONE in runner.pending
    runner.consume(json.dumps({"type": "assistant", "uuid": UUID_ONE, "message": {"content": []}}))
    assert UUID_ONE in runner.pending
    runner.consume(json.dumps({"type": "user", "uuid": UUID_ONE, "message": {"role": "user"}}))
    assert UUID_ONE in runner.pending
    log = runner.events_path.read_text()
    assert UUID_ONE not in log and "private message" not in log


def test_retrying_a_uuid_writes_only_once(tmp_path, monkeypatch, record):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    runner = Runner("s", record)
    runner.claude_session = "fake-session"
    transcript = _transcript(runner)
    runner.child = _Child()
    runner.command({"command": "send", "uuid": UUID_ONE, "content": "first"})
    assert runner.command({"command": "send", "uuid": UUID_ONE, "content": "ignored"})["state"] == "pending"
    transcript.write_text(json.dumps({"type": "user", "uuid": UUID_ONE}) + "\n")
    assert runner.command({"command": "send", "uuid": UUID_ONE, "content": "ignored"})["state"] == "delivered"
    assert len(runner.child.stdin.lines) == 1


def test_socket_and_lock_are_owner_only(monkeypatch, record):
    import tempfile
    short = tempfile.mkdtemp(prefix="ctb", dir="/tmp")
    monkeypatch.setenv("CTB_STATE_DIR", short + "/state")
    runner = Runner("unsafe/session", record)
    runner.acquire()
    runner.listen()
    try:
        assert runner.socket_path.stat().st_mode & 0o777 == 0o600
        assert runner.root.stat().st_mode & 0o777 == 0o700
        second = Runner("unsafe/session", record)
        with pytest.raises(RunnerError, match="runner_already_running"):
            second.acquire()
    finally:
        runner.server.close()
        runner.socket_path.unlink()
        import shutil
        shutil.rmtree(short, ignore_errors=True)


def test_fake_cli_protocol_covers_turns_compaction_interrupt_crash_and_resume(tmp_path, monkeypatch, record):
    """The contract fixture is a subprocess speaking JSONL; it never invokes an installed Claude CLI."""
    fake = tmp_path / "fake-claude"
    fake.write_text("""#!/usr/bin/env python3
import json, sys
resume = sys.argv[sys.argv.index('--resume') + 1] if '--resume' in sys.argv else 'fake-session'
print(json.dumps({'type':'system','subtype':'init','session_id':resume,'capabilities':['interrupt_receipt_v1']}), flush=True)
for line in sys.stdin:
    item=json.loads(line)
    if item.get('type') == 'control_request':
        print(json.dumps({'type':'control_response','request_id':item['request_id'],'subtype':item['request']['subtype']}), flush=True)
        continue
    content=item['message']['content']
    if content == 'crash': sys.exit(17)
    print(json.dumps({'type':'user','uuid':item['uuid'],'message':{'role':'user'}}), flush=True)
    if content == '/compact':
        print(json.dumps({'type':'system','subtype':'status','status':'compacting'}), flush=True)
        print(json.dumps({'type':'system','subtype':'compact_boundary','pre_tokens':99,'post_tokens':10}), flush=True)
    print(json.dumps({'type':'result','subtype':'success','num_turns':1}), flush=True)
""")
    fake.chmod(0o700)
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    runner = Runner("s", record | {"claude_binary": str(fake)})
    runner.spawn()
    assert json.loads(runner.child.stdout.readline())["session_id"] == "fake-session"
    runner.child.stdin.write(json.dumps({"type": "user", "uuid": "one",
                                         "message": {"role": "user", "content": "/compact"}}) + "\n")
    runner.child.stdin.flush()
    events = [json.loads(runner.child.stdout.readline()) for _ in range(4)]
    assert [event["type"] for event in events] == ["user", "system", "system", "result"]
    runner.child.stdin.write(json.dumps({"type": "control_request", "request_id": "i",
                                         "request": {"subtype": "interrupt"}}) + "\n")
    runner.child.stdin.flush()
    assert json.loads(runner.child.stdout.readline())["subtype"] == "interrupt"
    runner.child.stdin.write(json.dumps({"type": "user", "uuid": "bad",
                                         "message": {"role": "user", "content": "crash"}}) + "\n")
    runner.child.stdin.flush()
    assert runner.child.wait(timeout=2) == 17
    runner.claude_session = "fake-session"
    assert runner.argv()[runner.argv().index("--resume") + 1] == "fake-session"


def test_socket_path_fits_macos_limit_for_a_long_session_id(monkeypatch, record):
    from pathlib import Path
    from ctb import runner as runner_module
    long_root = Path("/state-root-" + "x" * 50 + "/runners")  # as long as a real state path on macOS
    monkeypatch.setattr(runner_module, "runner_dir", lambda: long_root)
    runner = Runner("0" * 8 + "-" + "1" * 4 + "-" + "2" * 4 + "-" + "3" * 4 + "-" + "4" * 12, record)
    assert len(str(runner.socket_path).encode("utf-8")) < 104


def test_real_socket_path_limit_and_short_override(tmp_path, monkeypatch, record, short_dir):
    from ctb import runner as runner_module
    long_root = tmp_path / ("x" * 90)
    monkeypatch.setattr(runner_module, "runner_dir", lambda: C.private_dir(long_root))
    runner = Runner("s", record)
    with pytest.raises(RunnerError, match="runner_socket_path_too_long"):
        runner.listen()
    short = Runner("s", record, socket_dir=short_dir + "/sock")
    short.listen()
    try:
        assert short.socket_path.parent.stat().st_mode & 0o777 == 0o700
    finally:
        short.server.close()
        short.socket_path.unlink()


def test_stale_socket_is_replaced_but_live_socket_is_not(tmp_path, monkeypatch, record, short_dir):
    monkeypatch.setenv("CTB_STATE_DIR", short_dir + "/state")
    stale = Runner("stale", record)
    stale.socket_path.touch()
    stale.listen()
    stale.server.close()
    stale.socket_path.unlink()

    live = Runner("live", record)
    live.listen()
    try:
        contender = Runner("live", record)
        with pytest.raises(RunnerError, match="runner_already_running"):
            contender.listen()
        assert live.socket_path.exists()
    finally:
        live.server.close()
        live.socket_path.unlink()


def _ask(path, payload: bytes, timeout=2.0) -> bytes:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    client.connect(str(path))
    client.sendall(payload)
    return client


def _reply(client) -> bytes:
    data = b""
    try:
        while not data.endswith(b"\n"):
            part = client.recv(4096)
            if not part:
                break
            data += part
    except ConnectionResetError:
        pass
    finally:
        client.close()
    return data


def test_peer_uid_reads_real_kernel_credentials():
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with left, right:
        assert Runner._peer_uid(left) == os.getuid()
        assert Runner._peer_uid(right) == os.getuid()


def test_real_accept_serves_owner_and_rejects_other_uid_without_ending(monkeypatch, record, tmp_path, short_dir):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    runner = Runner("s", record, socket_dir=short_dir)
    runner.listen()
    try:
        client = _ask(runner.socket_path, b'{"command":"unknown"}\n')
        runner._accept_client()
        assert json.loads(_reply(client)) == {"accepted": False, "error": "unknown_command"}

        runner.owner_uid = os.getuid() + 1  # the real kernel uid now differs from the expected owner
        client = _ask(runner.socket_path, b'{"command":"stop"}\n')
        runner._accept_client()
        assert _reply(client) == b""
        assert runner.stopping is False

        runner.owner_uid = os.getuid()
        client = _ask(runner.socket_path, b'{"command":"unknown"}\n')
        runner._accept_client()
        assert json.loads(_reply(client))["error"] == "unknown_command"
        assert '"client_rejected"' in runner.events_path.read_text()
    finally:
        runner.server.close()
        runner.socket_path.unlink()


def test_client_io_failures_close_only_that_client(monkeypatch, record, tmp_path, short_dir):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    runner = Runner("s", record, socket_dir=short_dir)
    runner.client_deadline = .3
    runner.listen()
    try:
        silent = _ask(runner.socket_path, b"")
        started = time.monotonic()
        runner._accept_client()
        assert time.monotonic() - started < 2
        silent.close()

        gone = _ask(runner.socket_path, b'{"command":"unknown"}\n')
        gone.close()
        runner._accept_client()

        client = _ask(runner.socket_path, b'{"command":"unknown"}\n')
        runner._accept_client()
        assert json.loads(_reply(client))["error"] == "unknown_command"
        assert '"reason":"timeout"' in runner.events_path.read_text()
    finally:
        runner.server.close()
        runner.socket_path.unlink()


def test_crash_before_transcript_write_resends_once(tmp_path, monkeypatch, record):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    first = Runner("s", record)
    first.claude_session = "fake-session"
    _transcript(first)
    first.child = _Child()
    first.command({"command": "send", "uuid": UUID_ONE, "content": "private message"})

    resumed = Runner("s", record)
    resumed._load_delivery()
    assert list(resumed.pending) == [UUID_ONE]
    resumed.child = _Child()
    resumed._reconcile_pending(resend=True)
    assert len(resumed.child.stdin.lines) == 1


def test_crash_after_transcript_write_does_not_resend(tmp_path, monkeypatch, record):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    first = Runner("s", record)
    first.claude_session = "fake-session"
    first.child = _Child()
    first.command({"command": "send", "uuid": UUID_ONE, "content": "private message"})
    _transcript(first, {"type": "user", "uuid": UUID_ONE})

    after_ack = Runner("s", record)
    after_ack._load_delivery()
    after_ack.child = _Child()
    assert after_ack.command({"command": "send", "uuid": UUID_ONE,
                              "content": "not written"})["state"] == "delivered"
    assert after_ack.child.stdin.lines == []


def test_sanitizing_collision_has_distinct_runner_paths(tmp_path, monkeypatch, record):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    first, second = Runner("same/name", record), Runner("same?name", record)
    assert first.socket_path != second.socket_path
    assert first.lock_path != second.lock_path
    assert first.state_root != second.state_root
    assert first.events_path != second.events_path


def test_send_rejects_noncanonical_uuid_without_logging_it(tmp_path, monkeypatch, record):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    runner = Runner("s", record)
    runner.child = _Child()
    rejected = "NOT-A-UUID"
    with pytest.raises(RunnerError, match="invalid_uuid"):
        runner.command({"command": "send", "uuid": rejected, "content": "private message"})
    assert not runner.events_path.exists()


def test_event_allowlists_replace_unknown_values():
    summary = event_summary({"type": "assistant", "message": {"content": [{"type": "secret-kind"}]}}, 1)
    assert summary["block_types"] == ["other"]
    assert event_summary({"type": "system", "subtype": "secret-kind"}, 1)["subtype"] == "other"
    assert event_summary({"type": "rate_limit_event", "rate_limit_info": {"status": "secret-kind"}}, 1)["status"] == "other"
    assert event_summary({"type": "control_request", "request": {"subtype": "secret-kind"}}, 1)["subtype"] == "other"
    assert event_summary({"type": "private-event"}, 1)["type"] == "other"


def test_event_log_contains_no_raw_ids(tmp_path, monkeypatch, record):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    runner = Runner("s", record)
    session_id, request_id = "session-secret", "request-secret"
    runner.consume(json.dumps({"type": "system", "subtype": "init", "session_id": session_id}))
    runner.consume(json.dumps({"type": "control_response", "request_id": request_id,
                               "request": {"subtype": "interrupt"}}))
    runner.consume(json.dumps({"type": "user", "uuid": UUID_ONE, "message": {"role": "user"}}))
    log = runner.events_path.read_text()
    assert session_id not in log and request_id not in log and UUID_ONE not in log


def test_recorded_identity_mismatch_never_signals(tmp_path, monkeypatch, record):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    runner = Runner("s", record)
    runner._atomic_json(runner.process_path, {"pid": 123, "pgid": 123, "identity": "old"})
    monkeypatch.setattr(runner, "_process_identity", lambda _pid: "new")
    signalled = []
    monkeypatch.setattr(runner, "_stop_group", lambda *args: signalled.append(args))
    runner._cleanup_recorded_child()
    assert signalled == [] and not runner.process_path.exists()


def test_stop_reaps_cli_process_group(tmp_path, monkeypatch, record):
    fake = tmp_path / "tree-cli"
    fake.write_text("""#!/usr/bin/env python3
import os, signal, sys
pid=os.fork()
if pid == 0:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.pause()
signal.signal(signal.SIGINT, signal.SIG_IGN)
def stop(*_):
    os.waitpid(pid, 0)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print(pid, flush=True)
signal.pause()
""")
    fake.chmod(0o700)
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    runner = Runner("s", record | {"claude_binary": str(fake)})
    runner.spawn()
    grandchild = int(runner.child.stdout.readline())
    leader = runner.child.pid
    runner.stop_child(timeout=.2)
    assert runner.child.returncode is not None
    for pid in (leader, grandchild):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_run_restarts_forever_and_resets_after_healthy_turn(tmp_path, monkeypatch, record, short_dir):
    fake = tmp_path / "restart-cli"
    count = tmp_path / "count"
    args = tmp_path / "args"
    fake.write_text(f"""#!/usr/bin/env python3
import json, pathlib, sys
count=pathlib.Path({str(count)!r})
n=int(count.read_text()) + 1 if count.exists() else 1
count.write_text(str(n))
with pathlib.Path({str(args)!r}).open('a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')
sid=sys.argv[sys.argv.index('--resume') + 1] if '--resume' in sys.argv else 'stable-session'
print(json.dumps({{'type':'system','subtype':'init','session_id':sid}}), flush=True)
if n == 2: print(json.dumps({{'type':'result','subtype':'success'}}), flush=True)
sys.exit(1)
""")
    fake.chmod(0o700)
    monkeypatch.setenv("CTB_STATE_DIR", short_dir + "/state")
    sleeps = []
    runner = Runner("s", record | {"claude_binary": str(fake)}, sleep=lambda delay: sleeps.append(delay))
    original_spawn = runner.spawn
    def spawn():
        if len(sleeps) == 2:
            runner.stopping = True
            return
        original_spawn()
    runner.spawn = spawn
    assert runner.run() == 0
    invocations = [json.loads(line) for line in args.read_text().splitlines()]
    assert "--resume" not in invocations[0]
    assert invocations[1][invocations[1].index("--resume") + 1] == "stable-session"
    assert sleeps == [2, 2]


SESSION = "11111111-2222-4333-8444-555555555555"
UUID_TWO = "00000000-0000-4000-8000-000000000002"


def test_project_folder_matches_claude_code_naming():
    from ctb.runner import claude_project_folder
    assert claude_project_folder("/work/.hidden/my_app v2") == "-work--hidden-my-app-v2"
    assert claude_project_folder("/work/\u05e9\u05dc\u05d5\u05dd") == "-work-" + "-" * 4
    assert claude_project_folder("/w/\U0001F600") == "-w-" + "--"


@pytest.mark.parametrize("leaf", [".dotted.dir", "under_score", "with space", "\u05e2\u05d1\u05e8\u05d9\u05ea"])
def test_transcript_path_uses_real_folder_naming(tmp_path, monkeypatch, record, isolated_claude_home, leaf):
    from ctb.runner import claude_project_folder
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    cwd = tmp_path / "work" / leaf
    cwd.mkdir(parents=True)
    folder = isolated_claude_home / "projects" / claude_project_folder(os.path.realpath(cwd))
    folder.mkdir(parents=True)
    (folder / f"{SESSION}.jsonl").write_text(json.dumps({"type": "user", "uuid": UUID_ONE}) + "\n")
    runner = Runner("s", record | {"cwd": str(cwd)})
    runner.claude_session = SESSION
    assert runner._transcript_path() == folder / f"{SESSION}.jsonl"
    assert runner._transcript_uuids() == {UUID_ONE}
    assert "/" not in folder.name and "." not in folder.name and "_" not in folder.name


def test_long_or_renamed_project_folder_falls_back_to_search(tmp_path, monkeypatch, record, isolated_claude_home):
    from ctb.runner import PROJECT_NAME_LIMIT, claude_project_folder
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    cwd = tmp_path
    for _ in range(5):
        cwd = cwd / ("segment_" + "x" * 50)
    cwd.mkdir(parents=True)
    name = claude_project_folder(os.path.realpath(cwd))
    assert len(name) > PROJECT_NAME_LIMIT
    hashed = isolated_claude_home / "projects" / (name[:PROJECT_NAME_LIMIT] + "-abc123")
    hashed.mkdir(parents=True)
    (hashed / f"{SESSION}.jsonl").write_text("")
    runner = Runner("s", record | {"cwd": str(cwd)})
    runner.claude_session = SESSION
    assert runner._transcript_path() == hashed / f"{SESSION}.jsonl"

    other = Runner("s", record)
    other.claude_session = UUID_TWO
    renamed = isolated_claude_home / "projects" / "some-other-name"
    renamed.mkdir(parents=True)
    (renamed / f"{UUID_TWO}.jsonl").write_text("")
    assert other._transcript_path() == renamed / f"{UUID_TWO}.jsonl"
    other.claude_session = "../escape"
    assert other._transcript_path() is None


def test_replayed_stdin_uuid_is_the_transcript_record_uuid(tmp_path, monkeypatch, record):
    """Fixture shaped like a CLI transcript (synthetic ids): the stdin uuid is the user record's own uuid."""
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    runner = Runner("s", record)
    runner.claude_session = SESSION
    _transcript(runner,
                {"type": "summary", "summary": "synthetic", "leafUuid": UUID_TWO},
                {"parentUuid": None, "isSidechain": False, "userType": "external", "cwd": "/work/project",
                 "sessionId": SESSION, "version": "0.0.0", "type": "user",
                 "message": {"role": "user", "content": "synthetic"}, "uuid": UUID_ONE,
                 "timestamp": "2000-01-01T00:00:00.000Z"},
                {"parentUuid": UUID_ONE, "type": "assistant", "uuid": UUID_TWO, "sessionId": SESSION,
                 "message": {"role": "assistant", "content": []}})
    runner.child = _Child()
    assert runner.command({"command": "send", "uuid": UUID_ONE, "content": "synthetic"})["state"] == "delivered"
    assert runner.command({"command": "send", "uuid": UUID_TWO, "content": "synthetic"}) == {
        "accepted": True, "uuid": UUID_TWO}
    assert len(runner.child.stdin.lines) == 1


def test_broken_transcript_lines_are_skipped_and_counted(tmp_path, monkeypatch, record):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    runner = Runner("s", record)
    runner.claude_session = SESSION
    path = _transcript(runner)
    path.write_bytes(b'{"broken\n\xff\xfe\n' + json.dumps({"type": "user", "uuid": UUID_ONE}).encode()
                     + b'\n{"type":"user","uu')
    runner.pending[UUID_TWO] = {"type": "user", "uuid": UUID_TWO}
    runner.pending[UUID_ONE] = {"type": "user", "uuid": UUID_ONE}
    runner.child = _Child()
    runner._reconcile_pending(resend=True)
    assert list(runner.pending) == [UUID_TWO]
    assert [line["uuid"] for line in runner.child.stdin.lines] == [UUID_TWO]
    events = [json.loads(line) for line in runner.events_path.read_text().splitlines()]
    assert {"type": "transcript_skipped_lines", "count": 3}.items() <= events[0].items()


def test_pending_before_first_init_is_resent_after_restart(tmp_path, monkeypatch, record):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    first = Runner("s", record)
    first.child = _Child()
    first.command({"command": "send", "uuid": UUID_ONE, "content": "before init"})
    restarted = Runner("s", record)
    restarted._load_delivery()
    assert restarted.claude_session == ""
    restarted.child = _Child()
    restarted._reconcile_pending(resend=True)
    assert [line["uuid"] for line in restarted.child.stdin.lines] == [UUID_ONE]


def test_silence_is_not_a_stall_while_a_tool_or_permission_request_is_open(tmp_path, monkeypatch, record):
    from ctb.runner import STALL_SECONDS
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    runner = Runner("s", record)
    runner.child = _Child()
    runner.turn_open = True
    runner.consume(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tool-a", "name": "Bash", "input": {}}]}}))
    assert not runner.stalled(runner.last_output + 24 * 3600)
    runner.consume(json.dumps({"type": "control_request", "request_id": "req-a",
                               "request": {"subtype": "can_use_tool"}}))
    runner.consume(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tool-a", "content": "x"}]}}))
    assert not runner.stalled(runner.last_output + 24 * 3600)
    runner.command({"command": "answer", "request_id": "req-a", "response": {"behavior": "deny"}})
    assert not runner.stalled(runner.last_output + STALL_SECONDS - 1)
    assert runner.stalled(runner.last_output + STALL_SECONDS)
    runner.consume(json.dumps({"type": "control_request", "request_id": "req-b",
                               "request": {"subtype": "can_use_tool"}}))
    runner.consume(json.dumps({"type": "control_cancel_request", "request_id": "req-b"}))
    assert runner.stalled(runner.last_output + STALL_SECONDS)
    runner.consume(json.dumps({"type": "result", "subtype": "success"}))
    assert not runner.stalled(runner.last_output + 24 * 3600)
    assert "tool-a" not in runner.events_path.read_text() and "req-a" not in runner.events_path.read_text()


def test_lines_written_together_are_all_consumed_from_a_real_pipe(tmp_path, monkeypatch, record, short_dir):
    fake = tmp_path / "burst-cli"
    fake.write_text("""#!/usr/bin/env python3
import json, os, time
line = lambda item: json.dumps(item) + '\\n'
os.write(1, (line({'type':'system','subtype':'init','session_id':'burst-session'})
             + line({'type':'result','subtype':'success'})).encode())
time.sleep(1.5)
os.write(1, (line({'type':'assistant','message':{'content':[]}})
             + json.dumps({'type':'result','subtype':'success'})).encode())
""")
    fake.chmod(0o700)
    monkeypatch.setenv("CTB_STATE_DIR", short_dir + "/state")
    sleeps = []
    runner = Runner("s", record | {"claude_binary": str(fake)}, sleep=sleeps.append)
    original_spawn = runner.spawn
    def spawn():
        if sleeps:
            runner.stopping = True
            return
        original_spawn()
    runner.spawn = spawn
    assert runner.run() == 0
    types = [json.loads(line)["type"] for line in runner.events_path.read_text().splitlines()]
    assert types[:4] == ["system", "result", "assistant", "result"]
    assert sleeps == [2]


def test_failed_child_write_keeps_message_pending_for_restart(tmp_path, monkeypatch, record):
    fake = tmp_path / "gone-cli"
    fake.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdin.close()\n")
    fake.chmod(0o700)
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    runner = Runner("s", record | {"claude_binary": str(fake)})
    runner.spawn()
    runner.child.wait(timeout=5)
    reply = runner.command({"command": "send", "uuid": UUID_ONE, "content": "x" * 200000})
    assert reply == {"accepted": True, "uuid": UUID_ONE, "state": "pending"}
    assert runner.child_broken and UUID_ONE in runner.pending
    with pytest.raises(RunnerError, match="child_unavailable"):
        runner.command({"command": "interrupt"})
    runner.stop_child(timeout=.2)
