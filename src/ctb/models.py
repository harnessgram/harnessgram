"""The Claude models the bridge offers, how it names them, and what /model really did.

One row per model: a new model is one line in MODELS. The bridge types the exact id (an alias such as
"opus" can resolve to an older model through a gateway) and shows the display name, never a bare alias.
What /model did is read from Claude Code's own answer in the transcript ("Set model to `Opus 5.5`" or
"Kept model as `Fable 5.1`" when a confirm dialog was cancelled), never assumed.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

# (button key, exact id typed into /model, display name)
MODELS = (
    ("fable", "claude-fable-5-2", "Fable 5.2"),
    ("opus", "claude-opus-5-5", "Opus 5.5"),
    ("sonnet", "claude-sonnet-5-5", "Sonnet 5.5"),
    ("haiku", "claude-haiku-5-5", "Haiku 5.5"),
)
CHOICES = tuple(key for key, _, _ in MODELS)
IDS = {key: model_id for key, model_id, _ in MODELS}
LABELS = {key: name for key, _, name in MODELS}
_BY_ID = {model_id: name for _, model_id, name in MODELS}

TAIL = 256 * 1024  # bytes of the transcript's end read for the newest /model result

_STDOUT = re.compile(r"<local-command-std(out|err)>(.*?)</local-command-std(?:out|err)>", re.S)
_COMMAND = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>(?:.*?<command-args>(.*?)</command-args>)?", re.S)
_RESULT = re.compile(r"^\s*(Set model to|Kept model as)\s+`?([^`\n]+?)`?(?:\s+and saved\b.*)?\s*$", re.S)


def display_name(model: str) -> str:
    """"claude-opus-5-5" -> "Opus 5.5"; a name that already reads well is kept; "" stays ""."""
    model = (model or "").strip()
    if not model:
        return ""
    if model in _BY_ID:
        return _BY_ID[model]
    if model.lower() in IDS:
        return LABELS[model.lower()]
    m = re.fullmatch(r"(?:claude-)?([a-z]+)((?:-\d{1,2})+)(?:-\d{8})?(\[1m\])?", model)
    if not m:
        return model
    version = ".".join(m.group(2).strip("-").split("-"))
    return f"{m.group(1).title()} {version}" + (" (1M)" if m.group(3) else "")


def parse_result(stdout: str) -> Optional[Tuple[bool, str]]:
    """Claude Code's answer to /model as (changed, display name), or None when it is something else."""
    m = _RESULT.match(stdout or "")
    if not m:
        return None
    return m.group(1) == "Set model to", display_name(m.group(2).strip())


def _row_text(row: dict) -> Optional[str]:
    if not isinstance(row, dict) or row.get("isSidechain"):
        return None
    content = row.get("content") if row.get("type") == "system" else (row.get("message") or {}).get("content")
    return content if isinstance(content, str) else None


def _command_output(row: dict) -> Optional[Tuple[str, str]]:
    """("out"|"err", text) of a local command's output record (a system or a user row), else None."""
    content = _row_text(row)
    m = _STDOUT.search(content) if content else None
    return (m.group(1), m.group(2).strip()) if m else None


def _command_typed(row: dict) -> Optional[Tuple[str, str]]:
    """(command name without the slash, its arguments) of a local command's own record, else None."""
    content = _row_text(row)
    m = _COMMAND.search(content) if content else None
    return (m.group(1).lower(), (m.group(2) or "").strip()) if m else None


def _is_reply(row: dict) -> bool:
    msg = row.get("message") or {}
    return (row.get("type") == "assistant" and not row.get("isSidechain") and isinstance(msg, dict)
            and bool(msg.get("model")) and msg.get("model") != "<synthetic>")


def _rows(path: str, offset: Optional[int] = None) -> List[dict]:
    """Complete JSON rows of the transcript from `offset` (None: the last TAIL bytes)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            start = max(0, size - TAIL) if offset is None else min(max(0, offset), size)
            f.seek(start)
            data = f.read()
    except (OSError, TypeError):
        return []
    text = data.decode("utf-8", "replace")
    lines = text.split("\n")
    if offset is None and start:
        lines = lines[1:]  # the first line was cut by the seek
    if not text.endswith("\n"):
        lines = lines[:-1]  # still being written
    out = []
    for line in lines:
        if "local-command-std" not in line and '"assistant"' not in line and "command-name" not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def model_answer_after(path: Optional[str], offset: int, args: str = "") -> Optional[Tuple[str, str]]:
    """Claude Code's answer to the /model typed after byte `offset`, as ("out"|"err", text), or None while
    it has not answered. Only the output that follows a /model command record counts (with these `args`
    when given); another command's output in between (/effort, /status) never does (the watcher once ended on any command output). An output with no command record before it
    counts only when it reads as a /model answer."""
    if not path:
        return None
    current = None  # the command record the next output belongs to
    for row in _rows(path, offset):
        typed, out = _command_typed(row), _command_output(row)
        if typed:
            current = typed
        if not out:
            continue
        owner, current = current, None
        if owner is None:
            if out[0] == "out" and parse_result(out[1]):
                return out
        elif owner[0] == "model" and (not args or owner[1] == args.strip()):
            return out
    return None


def latest_switch(path: Optional[str]) -> str:
    """The model a /model command left the session on, when that answer is newer than the last reply
    (the reply's own model and the status line are behind it until the next turn). "" otherwise."""
    if not path:
        return ""
    for row in reversed(_rows(path)):
        if _is_reply(row):
            return ""
        out = _command_output(row)
        if out and out[0] == "out":
            parsed = parse_result(out[1])
            if parsed:
                return parsed[1]
    return ""


# -- the default model for new sessions (Claude Code's own "model" setting) ------------------------------

def settings_file() -> Path:
    """Claude Code's user settings, where its default model lives (`model`)."""
    return Path(os.environ.get("CTB_CLAUDE_HOME") or Path.home() / ".claude") / "settings.json"


def _read_settings(path: Path) -> dict:
    """The settings as a dict ({} when the file is missing). Raises ValueError when it is not a JSON
    object, so a broken file is never overwritten."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    return data


def default_model() -> str:
    """The model new sessions start on (Claude Code's `model` setting), or "" for Claude's own default."""
    try:
        value = _read_settings(settings_file()).get("model")
    except (OSError, ValueError):
        return ""
    return value.strip() if isinstance(value, str) else ""


def default_label() -> str:
    """The display name of the default model ("Opus 5.5"), or "" when none is set."""
    return display_name(default_model())


def set_default(key: str) -> str:
    """Make model `key` (one of CHOICES) the default for new sessions: writes the exact id into the
    `model` key of Claude Code's settings.json, atomically, keeping every other key as it was (a
    symlinked file is written through its link). Returns the model's display name. Raises ValueError
    for an unknown key or a settings file that is not a JSON object, OSError when it cannot be written."""
    if key not in IDS:
        raise ValueError(f"unknown model {key!r}")
    path = Path(os.path.realpath(settings_file()))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".ctb-lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read_settings(path)
        data["model"] = IDS[key]
        tmp = path.with_name(f".{path.name}.ctb-{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            if path.exists():
                os.chmod(tmp, path.stat().st_mode & 0o7777)
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
    return LABELS[key]


def launch_flag(choice: Optional[str]) -> str:
    """The `--model` argument that starts a new session on `choice` ("" for the default)."""
    return f" --model {IDS[choice]}" if choice in IDS else ""
