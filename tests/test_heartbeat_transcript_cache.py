"""Fake transcripts prove heartbeat readers do not reopen unchanged large files."""
import builtins
import json

from ctb import usage


def _large_transcript(path, rows):
    with path.open("wb") as fh:
        fh.write(b'{"type":"noise"}\n' * 400_000)
        for row in rows:
            fh.write(json.dumps(row).encode() + b"\n")
    return str(path)


def _count_reads(monkeypatch, target):
    opened = []
    original = builtins.open

    def tracked(file, *args, **kwargs):
        if str(file) == target:
            opened.append(str(file))
        return original(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracked)
    return opened


def test_unchanged_context_transcript_is_not_reread(tmp_path, monkeypatch):
    row = {"type": "assistant", "message": {"model": "fake-model", "usage": {
        "input_tokens": 123, "cache_read_input_tokens": 456}}}
    path = _large_transcript(tmp_path / "context.jsonl", [row])
    usage._context_cache.clear()
    opened = _count_reads(monkeypatch, path)

    assert usage.context_tokens(path) == (579, "fake-model")
    assert usage.context_tokens(path) == (579, "fake-model")
    assert opened == [path]


def test_unchanged_background_transcript_is_not_reread_and_time_result_matches(tmp_path, monkeypatch):
    launched = {"type": "assistant", "timestamp": "2026-11-19T10:00:00+00:00", "message": {"content": [{
        "type": "tool_use", "name": "Agent", "id": "tool-1",
        "input": {"run_in_background": True, "description": "Fake worker"}}]}}
    confirmed = {"type": "user", "message": {"content": [{
        "type": "tool_result", "tool_use_id": "tool-1", "content": "agentId: fake123"}]}}
    path = _large_transcript(tmp_path / "background.jsonl", [launched, confirmed])
    usage._background_cache.clear()
    opened = _count_reads(monkeypatch, path)

    expected = {"fake123": "🤖 Fake worker"}
    assert usage.background_of(path, now=1_763_547_000) == expected
    assert usage.background_of(path, now=1_763_547_030) == expected
    assert opened == [path]
