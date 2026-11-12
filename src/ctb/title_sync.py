"""The session's Claude Code title follows its Telegram topic name.

When the bridge renames a topic (the automatic namer in topic_names.py, or `ctb topic-rename`), the same name is
wanted as the Claude Code session title. Claude Code 2.1.322 supports this without typing anything: a
UserPromptSubmit hook may answer `hookSpecificOutput.sessionTitle` ("Set the session title"), and its input
carries the current `session_title`. So the name is only recorded here, and the next prompt of that session (the
owner's own, or a Telegram message delivered to it) sets it. Nothing is ever typed into a terminal, so a busy
session, a half-written draft or the wrong pane cannot be hit.

A title set this way is written to the transcript as the app's custom title, which topic_names.py otherwise
reads as "the owner renamed it in the app: stop naming this topic". `ours()` tells our own titles apart, so the
automatic namer keeps working while a real /rename by the owner still wins."""
from __future__ import annotations

import contextlib
import fcntl
import json
from typing import Callable, Dict, Optional

from .config import private_dir, state_dir, write_private

TITLE_LIMIT = 200  # characters Claude Code accepts for a title
KEPT = 300  # sessions remembered


def _path():
    return state_dir() / "claude-titles.json"


@contextlib.contextmanager
def _locked():
    private_dir(state_dir())
    with open(str(_path()) + ".lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _load() -> Dict[str, dict]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _clean(name) -> str:
    return " ".join(str(name or "").split())[:TITLE_LIMIT]


def want(session: str, name: str) -> None:
    """The topic of `session` was renamed to `name`: its Claude Code title should follow."""
    name = _clean(name)
    if not session or not name:
        return
    try:
        with _locked():
            data = _load()
            rec = data.pop(session, {})
            rec["want"] = name
            data[session] = rec
            write_private(_path(), json.dumps(dict(list(data.items())[-KEPT:]), ensure_ascii=False))
    except OSError:
        pass


def due(session: str, current: Optional[str], app_title: Callable[[], str] = lambda: "") -> str:
    """The title to set now, or "". `current` is the hook's `session_title` (None when the hook did not say):
    nothing is sent when the session already carries the name. `app_title` reads the newest title set in the app
    (/rename): one that is not ours is the owner's choice and is never overwritten."""
    if not session:
        return ""
    try:
        with _locked():
            data = _load()
            rec = data.get(session)
            name = _clean((rec or {}).get("want"))
            if not name or (current if current is not None else rec.get("set")) == name:
                return ""
            mine = (_clean(rec.get("want")), _clean(rec.get("set")))
            renamed = _clean(app_title())
            if renamed and renamed not in mine:
                return ""  # the owner renamed the session in the app: theirs wins
            rec["set"] = name
            write_private(_path(), json.dumps(data, ensure_ascii=False))
            return name
    except OSError:
        return ""


def ours(session: str, title: str) -> bool:
    """Whether `title` is a name this bridge set (or asked for), not the owner's own /rename."""
    rec = _load().get(session) or {}
    title = _clean(title)
    return bool(title) and title in (_clean(rec.get("want")), _clean(rec.get("set")))


def with_title(out: Optional[dict], data: dict) -> Optional[dict]:
    """The UserPromptSubmit hook answer, with the wanted title added when one is due."""
    session = data.get("session_id")
    current = data.get("session_title")
    transcript = data.get("transcript_path")

    def app_title() -> str:
        from .topic_names import custom_title
        return custom_title(transcript) if isinstance(transcript, str) and transcript else ""

    name = due(session, current if isinstance(current, str) else None, app_title) if isinstance(session, str) else ""
    if not name:
        return out
    out = dict(out or {})
    spec = dict(out.get("hookSpecificOutput") or {"hookEventName": "UserPromptSubmit"})
    spec["sessionTitle"] = name
    out["hookSpecificOutput"] = spec
    return out
