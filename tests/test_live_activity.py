import time

import pytest

from ctb import cards, live_activity as A, panel, panel_page, panel_text


@pytest.mark.parametrize("tool,inputs,expected", [
    ("Bash", {"description": "Check the tests", "command": "cat private"}, "Check the tests"),
    ("Bash", {"description": "cat private", "command": "cat private"}, "Bash"),
    ("Agent", {"description": "Review changes"}, "Review changes"),
    ("Task", {"description": "Inspect changes"}, "Inspect changes"),
    ("Edit", {"file_path": "/secret/dir/main.py"}, "editing main.py"),
    ("Write", {"file_path": "/secret/dir/note.txt", "content": "private"}, "editing note.txt"),
    ("WebSearch", {"query": "private"}, "Web Search"),
])
def test_description(tool, inputs, expected):
    assert A.describe({"tool_name": tool, "tool_input": inputs}) == expected


def test_secret_and_path_are_scrubbed():
    text = A.describe({"tool_name": "Bash", "tool_input": {"description": "Check /private/secret/file and api_key=sk-abcdefghijklmnopqrstuvwxyz123456"}})
    assert "/private/secret" not in text and "abcdefghijklmnopqrstuvwxyz" not in text


def test_throttle_per_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    data = {"tool_name": "Bash", "tool_input": {"description": "Checking"}}
    assert A.record("one", data, 100)
    assert not A.record("one", data, 109.99)
    assert A.record("two", data, 101)
    assert A.record("one", data, 110)
    assert A.read("one")["at"] == 110


def test_freshness_and_busy_timestamp(monkeypatch):
    rec = {"session": "abc", "kind": "progress", "state": "open", "updated": 100,
           "live": "Summary", "live_at": 700, "steps": [], "at": 0}
    monkeypatch.setattr(A, "read", lambda s: {"text": "Checking", "at": 990})
    monkeypatch.setattr(panel.auto_progress, "task_list", lambda p: [])
    working = {"abc": "working"}
    out = panel._card_activity(rec, {}, {}, working, 999)
    assert "activity" not in out and out["age"] == 9
    out = panel._card_activity(rec, {}, {}, working, 1000)
    assert out["activity"] == "Checking" and out["activity_age"] == 10
    assert not panel._card_stale(rec, working, 1000)
    assert panel._card_stale(rec, {"abc": "idle"}, 1600)
    assert not panel._card_stale(rec, {"abc": "idle"}, 1599)
    assert not panel._fresh_activity(rec, working, 1890)


def test_activity_translation_uses_existing_cache():
    calls = []
    tr = panel_text.Translator(None, translate_fn=lambda text: calls.append(text) or "בודק")
    assert tr.he("Checking") is None
    for _ in range(100):
        result = tr.he("Checking")
        if result:
            break
        time.sleep(0.01)
    assert result == "בודק" and tr.he("Checking") == "בודק" and calls == ["Checking"]


def test_panel_renders_both_labels_in_hebrew():
    assert panel_page.STRINGS["he"]["cardWorking"] == "עובדים עכשיו"
    assert panel_page.STRINGS["he"]["cardNow"] == "עכשיו"
    assert 'if(c.activity)' in panel_page.PAGE and 'else if(c.live)' in panel_page.PAGE
    assert 'he(c,"activity")' in panel_page.PAGE and 'cardActivityUpdated' in panel_page.PAGE


def test_description_with_a_credential_is_never_kept():
    from ctb import live_activity as L
    for text in ("Check with Cookie: sessionid=amberfox", "Call API with Authorization: Bearer abcdef123456",
                 "Login with password: hunter22"):
        assert L.describe({"tool_name": "Bash", "tool_input": {"description": text, "command": "true"}}) == "Bash"


def test_cookie_header_is_redacted_everywhere():
    from ctb.creds import redact_channel
    assert "amberfox" not in redact_channel("Cookie: sessionid=amberfox; csrftoken=x")
    assert "amberfox" not in redact_channel("set-cookie='sid=amberfox'")
