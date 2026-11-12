"""The status card brought back under newer messages shows the current context, not a pre-compaction one."""
from ctb import inbox, status


def test_fresh_context_replaces_stale_number(monkeypatch):
    monkeypatch.setattr(inbox.TopicMap, "session_for_thread", lambda self, th: "s1")
    monkeypatch.setattr(status, "transcript_of", lambda s: "/x.jsonl")
    monkeypatch.setattr(status, "usage_of", lambda p: (107_840, "m"))
    assert inbox.fresh_context("📏 הקשר 253 אלף", 21) == "📏 הקשר 108 אלף"
    assert inbox.fresh_context("📏 Context 1,253k · x", 21) == "📏 Context 108k · x"


def test_fresh_context_keeps_text_when_unknown(monkeypatch):
    monkeypatch.setattr(inbox.TopicMap, "session_for_thread", lambda self, th: None)
    assert inbox.fresh_context("📏 הקשר 253 אלף", 21) == "📏 הקשר 253 אלף"


def test_fresh_context_drops_or_adds_the_compaction_tip(monkeypatch):
    monkeypatch.setattr(inbox.TopicMap, "session_for_thread", lambda self, th: "s1")
    monkeypatch.setattr(status, "transcript_of", lambda s: "/x.jsonl")
    monkeypatch.setattr(status, "usage_of", lambda p: (107_840, "m"))
    assert inbox.fresh_context("📏 הקשר 512 אלף · כדאי לדחוס", 21) == "📏 הקשר 108 אלף"
    monkeypatch.setattr(status, "usage_of", lambda p: (450_000, "m"))
    assert inbox.fresh_context("📏 Context 300k", 21) == "📏 Context 450k · worth compacting"
