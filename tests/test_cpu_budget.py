"""Benchmark-style budgets for the paths that ran hot on a busy Mac: the panel's state build and the three
commands a session's hooks start on every tool call. The bounds are generous (about twice what a slow machine
measures) so they fail on a real regression and not on noise.

Set CTB_SKIP_BENCH=1 to skip them on a slow or shared CI machine.
"""
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import perf_fixture
from conftest import signed_in
from ctb import creds, hooks, panel

pytestmark = pytest.mark.skipif(os.environ.get("CTB_SKIP_BENCH") == "1", reason="CTB_SKIP_BENCH=1")

STATE_BUDGET = 1.5      # seconds, 300 cards and 300 ledger items, nothing remembered from an earlier build
HOOK_BUDGET = 0.150     # seconds of wall time for one hook process, start to exit
SRC = str(Path(__file__).resolve().parent.parent / "src")


def _cold():
    creds._redact_channel_memo.cache_clear()
    panel._clean_memo.cache_clear()


def test_a_state_build_with_300_cards_and_300_ledger_items_stays_fast(tmp_path, monkeypatch):
    signed_in("main")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    cfg, now, _ = perf_fixture.build(tmp_path, cards=300, items=300)
    _cold()
    start = time.perf_counter()
    state = panel.build_state(cfg, now=now)
    elapsed = time.perf_counter() - start
    assert len(state["requests"]) == 300 and state["cards"] and state["sessions"]
    assert elapsed < STATE_BUDGET, f"build_state took {elapsed:.2f}s (budget {STATE_BUDGET}s)"
    # the secrets in the ledger notes never reach the answer
    assert "hunter2xyz9" not in json.dumps(state)
    # a second build, with what is remembered, is much cheaper
    start = time.perf_counter()
    panel.build_state(cfg, now=now)
    assert time.perf_counter() - start < STATE_BUDGET


def test_the_state_reply_is_not_rebuilt_for_every_request(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    cfg, now, _ = perf_fixture.build(tmp_path, cards=300, items=300)
    cache = panel.state_cache.StateCache(ttl=3)
    key = (7, "")
    first = time.perf_counter()
    cache.get(key, lambda: panel.build_state(cfg))
    built = time.perf_counter() - first
    start = time.perf_counter()
    for _ in range(20):
        cache.get(key, lambda: panel.build_state(cfg))
    assert time.perf_counter() - start < max(built, 0.05)  # twenty more requests cost less than one build


# --- the hooks ----------------------------------------------------------------------------------------------------

class _BotApi(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps({"ok": True, "result": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def hook_env(tmp_path):
    """A sandboxed home with a fixture state, a config pointing at a local Bot API, and a session with a transcript."""
    signed_in("main")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _BotApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude")
    try:
        cfg, now, projects = perf_fixture.build(tmp_path, cards=300, items=300)
        session = perf_fixture._session_id(0)
        transcript = projects / "proj" / f"{session}.jsonl"
        # a long conversation: the end of it is all a tool call has to read
        with open(transcript, "a", encoding="utf-8") as f:
            for turn in range(400):
                f.write(json.dumps({"type": "user", "uuid": f"u{turn}", "message": {"role": "user", "content": [
                    {"type": "text", "text": f"question {turn}"}]}}) + "\n")
                f.write(json.dumps({"type": "assistant", "uuid": f"a{turn}", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "answer " * 120}]}}) + "\n")
        config = {"bot_token": "123456:" + "A" * 35, "owner_chat_id": 7, "home_chat_id": -1001234567890,
                  "target_language": "he", "api_base": f"http://127.0.0.1:{server.server_address[1]}",
                  "progress": False}
        config_dir = Path(os.environ["CTB_CONFIG_DIR"])
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="", PATH=os.environ.get("PATH", ""))
        post = {"session_id": session, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                "tool_input": {"command": "ls", "description": "List files"}, "tool_response": {"stdout": "x"},
                "transcript_path": str(transcript), "cwd": str(tmp_path)}
        status = {"session_id": session, "model": {"id": "claude-opus-5", "display_name": "Opus 5"},
                  "context_window": {"used_percentage": 12, "context_window_size": 200000},
                  "rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": "x"},
                                  "seven_day": {"used_percentage": 20, "resets_at": "y"}},
                  "workspace": {"current_dir": str(tmp_path)}, "cwd": str(tmp_path)}
        yield env, session, json.dumps(post), json.dumps(status)
    finally:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        server.shutdown()
        server.server_close()


def _run(env, args, stdin=""):
    """Wall time of one `ctb` command in a fresh interpreter, best of three (the first also warms the disk cache)."""
    code = "import sys; from ctb.cli import main; sys.exit(main(sys.argv[1:]))"
    best = None
    for _ in range(3):
        start = time.perf_counter()
        done = subprocess.run([sys.executable, "-c", code, *args], input=stdin, text=True, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        elapsed = time.perf_counter() - start
        assert done.returncode == 0, done.stderr
        best = elapsed if best is None else min(best, elapsed)
    return best


def test_hook_post_stays_under_its_budget(hook_env):
    env, session, post, _ = hook_env
    elapsed = _run(env, ["hook-post"], post)
    assert elapsed < HOOK_BUDGET, f"hook-post took {elapsed * 1000:.0f} ms (budget {HOOK_BUDGET * 1000:.0f} ms)"


def test_statusline_stays_under_its_budget(hook_env):
    env, session, _, status = hook_env
    elapsed = _run(env, ["statusline"], status)
    assert elapsed < HOOK_BUDGET, f"statusline took {elapsed * 1000:.0f} ms (budget {HOOK_BUDGET * 1000:.0f} ms)"


def test_typing_stays_under_its_budget(hook_env):
    env, session, _, _ = hook_env
    topics = Path(os.environ["CTB_STATE_DIR"]) / "topics.json"
    assert session in json.loads(topics.read_text())  # the fixture's session has a topic: the call really is sent
    elapsed = _run(env, ["typing", f"--session={session}"])
    assert elapsed < HOOK_BUDGET, f"typing took {elapsed * 1000:.0f} ms (budget {HOOK_BUDGET * 1000:.0f} ms)"


def test_hook_post_does_not_import_what_it_never_uses(hook_env):
    """The heavy modules load only when a path needs them: a quiet tool call must not pull in the inbox, the
    dialogs, the translator or the HTTP client."""
    env, session, post, _ = hook_env
    code = ("import sys; from ctb.cli import main; main(['hook-post']); "
            "print(sorted(m for m in ('ctb.inbox', 'ctb.dialogs', 'ctb.translate', 'ctb.mirror', 'http.client', 'urllib.request', "
            "'argparse') if m in sys.modules))")
    done = subprocess.run([sys.executable, "-c", code], input=post, text=True, env=env, capture_output=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "[]"


def test_a_tool_call_reads_only_the_end_of_a_long_transcript(hook_env):
    """append_progress used to parse the whole conversation on every call."""
    env, session, post, _ = hook_env
    path = json.loads(post)["transcript_path"]
    assert os.path.getsize(path) > hooks.TAIL_BYTES
    start = time.perf_counter()
    blocks = hooks.read_blocks_of(path)
    elapsed = time.perf_counter() - start
    assert [k for k, _ in blocks] == ["a399:0"]
    assert elapsed < 0.1, f"{elapsed * 1000:.0f} ms"
