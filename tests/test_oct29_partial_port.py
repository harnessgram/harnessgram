import json
import time

from ctb import hooks, inbox, progress, translate
from ctb.config import Config


def test_gemini_skipped_in_fast_path_and_falls_back(monkeypatch):
    monkeypatch.setattr(translate, "gemini", lambda *a: (_ for _ in ()).throw(AssertionError("slow")))
    monkeypatch.setattr(translate, "google", lambda *a: "תורגם")  # an answer still in English is rejected now
    assert translate.translate_text("a", Config(engines=["gemini", "google"], translate_cache=False), fast=True) == "תורגם"


def test_cache_remembers_translation_engine(monkeypatch):
    monkeypatch.setattr(translate, "gemini", lambda *a: "תורגם")
    cfg = Config(engines=["gemini"])
    translate.reset_engines()
    assert translate.translate_text("new cache entry", cfg) == "תורגם"
    translate.reset_engines()
    assert translate.translate_text("new cache entry", cfg) == "תורגם"
    assert translate.used_engines() == ("gemini",)


def test_progress_shows_engine(monkeypatch):
    monkeypatch.setattr(translate, "google", lambda *a: "טקסט")
    cfg = Config(translate=True, engines=["google"], translate_cache=False)
    assert progress.interim_text("Checking progress now", cfg).endswith("Translated by google")


def test_background_notification_waits_for_completion(tmp_path, monkeypatch):
    transcript = tmp_path / "session.jsonl"
    started = {"message": {"content": [{"type": "tool_use", "name": "Bash", "id": "abc", "input": {"run_in_background": True}}]}}
    transcript.write_text(json.dumps(started) + "\n")
    sent = []
    monkeypatch.setattr(hooks, "load_config", lambda: Config(bot_token="test", owner_chat_id=1))
    monkeypatch.setattr(hooks, "spawn", lambda *a, **k: sent.append(a))
    data = {"session_id": "s", "notification_type": "idle_prompt", "transcript_path": str(transcript)}
    hooks.hook_notify(json.dumps(data))
    assert not sent
    with transcript.open("a") as f:
        f.write(json.dumps({"message": {"content": "<tool-use-id>abc</tool-use-id>"}}) + "\n")
    assert hooks.background_running(str(transcript)) == 0
    hooks.hook_notify(json.dumps(data))
    assert sent


def test_bridge_typed_prompt_not_echoed(monkeypatch):
    sent = []
    monkeypatch.setattr(hooks, "load_config", lambda: Config(bot_token="test", owner_chat_id=1))
    monkeypatch.setattr(hooks, "spawn", lambda args, text=None: sent.append((args, text)))
    inbox.typed_mark("/compact")
    hooks.hook_prompt(json.dumps({"session_id": "s", "prompt": "/compact"}))
    assert not any(args[0] == "prompt-mirror" for args, _ in sent)
    hooks.hook_prompt(json.dumps({"session_id": "s", "prompt": "direct question"}))
    assert any(args[0] == "prompt-mirror" and text == "direct question" for args, text in sent)


def test_denied_or_foreground_agent_does_not_hold_the_idle_ring(tmp_path):
    transcript = tmp_path / "session.jsonl"
    rows = [{"message": {"content": [{"type": "tool_use", "name": "Agent", "id": "denied", "input": {}}]}},
            {"message": {"content": [{"type": "tool_result", "tool_use_id": "denied", "is_error": True,
                                      "content": "Owner rule: set model and effort"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Agent", "id": "fg", "input": {}}]}},
            {"message": {"content": [{"type": "tool_result", "tool_use_id": "fg", "content": "Review done."}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Agent", "id": "bg",
                                      "input": {"run_in_background": True}}]}},
            {"message": {"content": [{"type": "tool_result", "tool_use_id": "bg",
                                      "content": "Async agent launched successfully."}]}}]
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert hooks.background_running(str(transcript)) == 1
