import json

import pytest

from ctb import hooks


@pytest.mark.parametrize("command", [
    "while true; do sleep 5; done",
    "until ready; do sleep 1; done",
    "tail -f app.log",
    "watch make status",
    "python -m http.server 8000",
    "npm run dev",
    "npm run start",
    "uvicorn app:app",
    "pytest tests",
    "uv run pytest -q tests",
    "codex exec 'work'",
])
def test_long_running_pattern_table(command):
    assert hooks.long_running_bash({"command": command})


@pytest.mark.parametrize("tool_input", [
    {"command": "pytest tests", "run_in_background": True},
    {"command": "python -m http.server 8000 &"},
    {"command": "pytest tests/test_hooks.py"},
    {"command": "echo tail -f docs"},
    {"command": "sleep 2"},
])
def test_long_running_pattern_exclusions(tool_input):
    assert not hooks.long_running_bash(tool_input)


def test_timeout_over_five_minutes_warns():
    assert hooks.long_running_bash({"command": "build", "timeout": 300_001})
    assert not hooks.long_running_bash({"command": "build", "timeout": 300_000})


def test_hook_adds_context_without_blocking(capsys):
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": "watch date"}}
    assert hooks.hook_bash(json.dumps(payload)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                              "additionalContext": hooks.LONG_COMMAND_WARNING}}
