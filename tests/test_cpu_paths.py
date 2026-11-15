"""The CPU savings keep the answers they replaced: single-flight state cache, redaction fast paths, the transcript
tail reader, the hooks' argument shortcut and the per-session memo of a state build."""
import json
import random
import re
import threading
import time

import pytest

from ctb import cli, creds, hooks, panel, state_cache
from ctb.config import inbox_dir, safe_name, state_dir


# --- StateCache ---------------------------------------------------------------------------------------------

def test_requests_arriving_together_share_one_build():
    cache = state_cache.StateCache()
    started, release = threading.Event(), threading.Event()
    calls = []

    def build():
        calls.append(1)
        started.set()
        release.wait(5)
        return {"n": len(calls)}

    answers = []
    threads = [threading.Thread(target=lambda: answers.append(cache.get("k", build))) for _ in range(6)]
    threads[0].start()
    assert started.wait(5)
    for t in threads[1:]:
        t.start()
    time.sleep(0.1)  # the others are now waiting on the first build
    release.set()
    for t in threads:
        t.join(5)
    assert len(calls) == 1 and answers == [{"n": 1}] * 6


def test_an_answer_is_reused_for_the_ttl_and_then_built_again():
    now = [100.0]
    cache = state_cache.StateCache(ttl=3, clock=lambda: now[0])
    calls = []
    build = lambda: calls.append(1) or len(calls)
    assert cache.get("k", build) == 1 and cache.get("k", build) == 1
    assert cache.get("other", build) == 2  # another key is another answer
    now[0] += 3.5
    assert cache.get("k", build) == 3


def test_clear_forgets_answers_and_a_build_that_started_before_it_is_not_kept():
    cache = state_cache.StateCache(ttl=60)
    cache.get("k", lambda: "old")
    cache.clear()
    assert cache.get("k", lambda: "new") == "new"
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return "before the change"

    out = []
    t = threading.Thread(target=lambda: out.append(cache.get("s", slow)))
    t.start()
    assert started.wait(5)
    cache.clear()  # a change was made while the build ran
    release.set()
    t.join(5)
    assert out == ["before the change"]  # its own caller still gets it, but nobody after
    assert cache.get("s", lambda: "after the change") == "after the change"


def test_a_failed_build_reaches_every_waiter_and_is_not_kept():
    cache = state_cache.StateCache()
    started, release = threading.Event(), threading.Event()

    def broken():
        started.set()
        release.wait(5)
        raise KeyError("boom")

    errors = []

    def ask():
        try:
            cache.get("k", broken)
        except KeyError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=ask) for _ in range(3)]
    threads[0].start()
    assert started.wait(5)
    for t in threads[1:]:
        t.start()
    time.sleep(0.1)
    release.set()
    for t in threads:
        t.join(5)
    assert len(errors) == 3
    assert cache.get("k", lambda: "fine") == "fine"


def test_an_answer_the_caller_does_not_want_kept_is_built_again():
    cache = state_cache.StateCache()
    calls = []
    for _ in range(2):
        cache.get("k", lambda: calls.append(1) or {"pending": 2}, keep=lambda v: not v["pending"])
    assert len(calls) == 2


# --- the panel uses it ---------------------------------------------------------------------------------------

def test_the_state_route_reuses_a_build_and_an_action_forgets_it(monkeypatch):
    monkeypatch.setattr(state_cache, "TTL", 3.0)
    from conftest import GOOD_TOKEN
    from test_panel import OWNER, call, signed
    from ctb.config import Config
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    srv = panel.PanelServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        real, builds = panel.build_state, []
        monkeypatch.setattr(panel, "build_state", lambda *a, **k: builds.append(1) or real(*a, **k))
        assert call(srv, init=signed())[0] == 200 and call(srv, init=signed())[0] == 200
        assert len(builds) == 1
        call(srv, path="/api/client-error", init=signed(), body=b"{}")  # reads nothing of the state: still kept
        call(srv, init=signed())
        assert len(builds) == 1
        call(srv, path="/api/decide", init=signed(), body=b"{}")  # an action: whatever it answers, it may change things
        call(srv, init=signed())
        assert len(builds) == 2
        # another topic's view is another answer
        call(srv, init=signed(extra={"start_param": "panel"}))
        call(srv, init=signed(extra={"start_param": "panel"}))
        assert len(builds) == 2
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_state_still_waiting_for_translations_is_not_kept(monkeypatch):
    cache = state_cache.StateCache()
    calls = []
    for _ in range(2):
        cache.get("k", lambda: calls.append(1) or {"pending": 3}, keep=lambda built: not built.get("pending"))
    assert len(calls) == 2


# --- redaction fast paths -------------------------------------------------------------------------------------

def _reference_redact_channel(text: str) -> str:
    """The alerts-channel cleaning written out in full, with none of the shortcuts."""
    def swap(hides):
        def pair(m):
            if m.groupdict().get("qval") is not None:
                if not hides(m.group("word"), m.group("qval")):
                    return m.group()
                return m.string[m.start():m.start("qval")] + "[redacted]" + m.string[m.end("qval"):m.end()]
            if not hides(m.group("word"), m.group("val")):
                return m.group()
            return m.string[m.start():m.start("val")] + "[redacted]" + creds._tail(m.group("val"))
        return pair
    text = creds.USERINFO.sub(lambda m: m.group(1) + ":[redacted]@", text)
    text = creds.FLAG_VALUE.sub(lambda m: m.group() if not creds._label_hides("password", m.group("val"))
                                else m.group("flag") + "[redacted]", text)
    for pattern in (creds.FLAG_P, creds.CURL_USER, creds.BASIC, creds.COOKIE):
        text = pattern.sub(lambda m: m.group("pre") + "[redacted]", text)
    for pattern in (creds.LABEL_VALUE, creds.LABEL_NEXT, creds.LABEL_IS, creds.LABEL_HE):
        text = pattern.sub(swap(creds._label_hides), text)
    return creds._redact(creds.LABEL_SPACE.sub(swap(creds._spaced_hides), text))


_PIECES = ["password", "Password", "pwd", "pass", "pw", "token", "secret", "api_key", "api-key", "code", "your code",
           "otp", "pin", "pinned", "key", "ſecret", "Key", ": ", "=", ":", "\n", " ", "\t", " is ", "hunter2",
           "482913", "A1b2" * 9, "0123456789abcdef" * 2, "ghp_", "sk-", "AKIA", "xox", "eyJ", "Bearer ", "--password",
           "-p", "mysql", "curl -u", "user:pass@", "https://", "://", "@", "סיסמה", "קוד אימות", "אסימון", "סוד",
           "מפתח גישה", "Cookie: ", "authorization: basic ", "%6f", "%3a", "&#58;", "-----BEGIN PRIVATE KEY",
           "1234567:", "the", "a", "fix", "docs", "/path/to/x", "$VAR", "<value>", "'", '"', "Done.", "commit abcdef1234567"]


def test_redact_channel_matches_the_unshortcut_cleaning():
    rnd = random.Random(7)
    texts = ["".join(rnd.choice(_PIECES) for _ in range(rnd.randint(1, 9))) for _ in range(6000)]
    texts += ["password: hunter2", "your code is 482913", "DB_PASSWORD=abc123XYZ", "curl -u me:s3cret https://example.com",
              "Reviewed the retry path and wrote the notes.", "", "token\n  abc123def456"]
    for text in texts:
        creds._redact_channel_memo.cache_clear()
        assert creds.redact_channel(text) == creds._decoded_aware(text, _reference_redact_channel), repr(text)


def test_a_remembered_answer_is_the_same_as_a_fresh_one():
    text = "token: hunter2xyz9 and more"
    first = creds.redact_channel(text)
    assert first == creds.redact_channel(text) and "hunter2xyz9" not in first
    long_text = "word " * 600 + "password: hunter2xyz9"  # past the memo limit: cleaned every time, same answer
    assert "hunter2xyz9" not in creds.redact_channel(long_text)


# --- the transcript tail reader ---------------------------------------------------------------------------------

def _event(role, text, uuid=None, **extra):
    ev = {"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}}
    if uuid:
        ev["uuid"] = uuid
    ev.update(extra)
    return json.dumps(ev)


def _tool_result(n):
    return json.dumps({"type": "user", "uuid": f"r{n}", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"t{n}", "content": "ok " * 40}]}})


def _write(tmp_path, lines):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _same_as_whole_file(path):
    from pathlib import Path
    return hooks.read_blocks(Path(path).read_text(encoding="utf-8").splitlines())


@pytest.mark.parametrize("with_uuid", [True, False])
@pytest.mark.parametrize("tail", [4096, 20000, 10 ** 9])
def test_the_tail_reader_gives_the_whole_file_answer(tmp_path, monkeypatch, with_uuid, tail):
    monkeypatch.setattr(hooks, "TAIL_BYTES", tail if tail < 10 ** 8 else 1 << 20)
    lines = []
    for turn in range(40):
        lines.append(_event("user", f"question {turn}", f"u{turn}" if with_uuid else None))
        for step in range(6):
            lines.append(_tool_result(turn * 10 + step))
            lines.append(_event("assistant", f"reply {turn}.{step} " + "x" * 300, f"a{turn}-{step}" if with_uuid else None))
        lines.append("{not json")
    path = _write(tmp_path, lines)
    assert hooks.read_blocks_of(path) == _same_as_whole_file(path)


def test_the_tail_reader_finds_a_prompt_far_from_the_end(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "TAIL_BYTES", 2048)
    lines = [_event("user", "the only prompt", "u0")]
    lines += [_event("assistant", f"step {n} " + "y" * 200, f"a{n}") for n in range(300)]
    path = _write(tmp_path, lines)
    got = hooks.read_blocks_of(path)
    assert got == _same_as_whole_file(path) and len(got) == 300


def test_the_tail_reader_without_any_prompt_and_for_short_or_missing_files(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "TAIL_BYTES", 1024)
    path = _write(tmp_path, [_event("assistant", f"s{n} " + "z" * 100, f"a{n}") for n in range(50)])
    assert hooks.read_blocks_of(path) == _same_as_whole_file(path)
    short = _write(tmp_path, [_event("user", "hi", "u"), _event("assistant", "hello", "a")])
    assert hooks.read_blocks_of(short) == [("a:0", "hello")]
    with pytest.raises(OSError):
        hooks.read_blocks_of(str(tmp_path / "missing.jsonl"))
    with pytest.raises(OSError):
        hooks.read_blocks_of("")


def test_the_tail_reader_keeps_a_line_number_key_whole(tmp_path, monkeypatch):
    # a block with no id of its own is keyed by its line number, which only the whole file knows
    monkeypatch.setattr(hooks, "TAIL_BYTES", 1024)
    lines = [_event("user", "p", "u")] + [_tool_result(n) for n in range(80)] + [_event("assistant", "answer")]
    path = _write(tmp_path, lines)
    assert hooks.read_blocks_of(path) == _same_as_whole_file(path) == [("81:0", "answer")]


# --- the hooks' shortcuts ----------------------------------------------------------------------------------------

def test_the_shortcut_arguments_are_what_argparse_gives():
    parser = cli._parser()
    for argv in (["hook-post"], ["statusline"], ["typing", "--session=abc-123"]):
        quick, full = cli._quick_args(argv), parser.parse_args(argv)
        assert quick is not None and vars(quick) == {k: v for k, v in vars(full).items() if k in vars(quick)}
        assert quick.cmd == full.cmd
    for argv in (["typing"], ["typing", "--session="], ["typing", "--session", "x"], ["hook-post", "-h"], ["status"], []):
        assert cli._quick_args(argv) is None


def test_the_inbox_path_the_post_hook_checks_is_the_inboxs_own():
    from ctb.inbox import default_inbox
    for session in ("abc", "s/../x", "ünï-1", ""):
        assert default_inbox()._paths(session)[0] == inbox_dir() / (safe_name(session) + ".jsonl")
        assert inbox_dir() == state_dir() / "inbox"


# --- one read per session in a state build -------------------------------------------------------------------------

def test_a_build_reads_a_sessions_task_list_and_activity_once(monkeypatch):
    reads = {"tasks": 0, "activity": 0}
    monkeypatch.setattr(panel.auto_progress, "task_list",
                        lambda path: reads.__setitem__("tasks", reads["tasks"] + 1) or [
                            {"content": "Write the thing", "status": "in_progress"}])
    monkeypatch.setattr(panel.live_activity, "read",
                        lambda s: reads.__setitem__("activity", reads["activity"] + 1) or {"text": "Checking", "at": 990})
    memo = {}
    states = {"abc": "working"}
    for n in range(5):
        rec = {"id": f"{n}" * 16, "session": "abc", "kind": "progress", "state": "open", "updated": 100,
               "steps": [], "at": 0}
        out = panel._card_activity(rec, {"abc": "/t.jsonl"}, {"abc": 2}, states, 1000, memo)
        assert out["task"] == "Write the thing" and out["bg"] == 2
        panel._card_stale(rec, states, 1000, memo)
    assert reads == {"tasks": 1, "activity": 1}
