import json

from conftest import GOOD_TOKEN
from ctb import progress
from ctb.config import Config, state_dir


def _cfg(**values):
    defaults = dict(bot_token=GOOD_TOKEN, owner_chat_id=123, progress=True,
                    progress_details=True, target_language="he", ui_language="he", plain_step_text=True)
    defaults.update(values)
    return Config(**defaults)


def _row(tool, **tool_input):
    spec = progress._plain_source({"tool_name": tool, "tool_input": tool_input})
    return {"l": f"⚙️ {tool}", "tool": tool, "d": tool_input.get("command", "/tmp/example"),
            "plain": spec}


def test_rule_based_tool_and_command_descriptions_are_plain_hebrew():
    assert progress.plain_step_fallback(_row("Read", file_path="/tmp/a.py")["plain"]) == "קורא קובץ"
    assert progress.plain_step_fallback(_row("Bash", command="git status")["plain"]) == \
        "בודק או מעדכן את היסטוריית השינויים"
    assert progress.plain_step_fallback(_row("Bash", command="pytest -q")["plain"]) == \
        "מריץ את בדיקות התוכנה"
    assert progress.plain_step_fallback(_row("WebSearch", query="news")["plain"]) == \
        "מחפש מידע באינטרנט"


def test_card_is_immediate_and_raw_command_is_folded(monkeypatch):
    monkeypatch.setattr(progress, "_plain_model_available", lambda cfg: True)
    monkeypatch.setattr("ctb.translate.ask", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("render must not call a model")))
    pending = []
    shown = progress.card_text({"row": _row("Bash", command="git status")}, _cfg(translate=True), pending)
    assert "בודק או מעדכן את היסטוריית השינויים" in shown
    assert "<blockquote expandable>git status</blockquote>" in shown
    assert pending and pending[0].startswith(progress.PLAIN_JOB_PREFIX)


def test_short_command_is_kept_as_folded_raw_detail():
    data = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    assert progress.detail(data) == ""
    assert progress.detail(data, include_same=True) == "git status"


def test_model_input_is_redacted_and_cache_hit_avoids_second_call(monkeypatch):
    cfg = _cfg(translate=True, engines=["gemini"])
    spec = progress._plain_source({"tool_name": "Bash", "tool_input": {
        "command": "TOKEN=topsecret curl 'https://example.test/run?key=secret'",
        "api_key": "sk-abcdefghijklmnopqrstuvwxyz123456",
    }})
    assert "topsecret" not in json.dumps(spec)
    assert "?key=secret" not in json.dumps(spec)
    assert "abcdefghijklmnopqrstuvwxyz" not in json.dumps(spec)
    prompts = []
    monkeypatch.setattr(progress, "_plain_model_available", lambda cfg: True)
    monkeypatch.setattr("ctb.translate.ask", lambda prompt, cfg: prompts.append(prompt) or "בודק את מצב המאגר")
    assert progress._plain_model_text(spec, cfg) == "בודק את מצב המאגר"
    assert all(secret not in prompts[0] for secret in ("topsecret", "?key=secret", "abcdefghijklmnopqrstuvwxyz"))
    monkeypatch.setattr("ctb.translate.ask", lambda *a: (_ for _ in ()).throw(AssertionError("cache miss")))
    assert progress._plain_model_text(spec, cfg) == "בודק את מצב המאגר"
    assert (state_dir() / progress.PLAIN_CACHE_FILE).exists()


def test_accepts_hebrew_line_and_only_isolated_short_english():
    assert progress._plain_acceptable("מריץ את הבדיקות")
    assert progress._plain_acceptable("בודק בעזרת \u2068GitHub CLI\u2069")
    assert not progress._plain_acceptable("בודק GitHub כעת")
    assert not progress._plain_acceptable("בודק בעזרת \u2068one two three four\u2069")
    assert not progress._plain_acceptable("בודק\nעכשיו")


def test_switch_off_preserves_existing_technical_heading():
    shown = progress.card_text({"row": _row("Bash", command="git status")},
                               _cfg(plain_step_text=False))
    assert "Bash" in shown
    assert "בודק או מעדכן" not in shown


def test_plain_step_text_is_off_by_default():
    assert Config(bot_token=GOOD_TOKEN, owner_chat_id=123).plain_step_text is False
