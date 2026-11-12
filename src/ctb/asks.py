"""Proof that a numbered dialog on the pane is a real AskUserQuestion.

Pane text can never prove anything. The PreToolUse hook (`ctb hook-ask`, matcher AskUserQuestion) records the
call's structured tool_input before the question is shown: its questions, option labels and multiSelect flags.
A tap-to-answer card is built only when the options on the pane match one recorded question exactly and in
order. The record is cleared on any PostToolUse (the question was answered, or another tool ran) and at Stop,
and it is read only for the session that wrote it.
"""
from __future__ import annotations

import json
import re
import time
from typing import List, Optional

from .config import private_dir, state_dir, write_private

MAX_AGE = 6 * 3600       # a record older than this is not proof
MAX_TEXT = 400


def _path(session: str):
    return private_dir(state_dir() / "ask") / ((re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_") + ".json")


def _norm(text: str) -> str:
    return " ".join(str(text).replace("’", "'").replace("‏", "").split())


def questions_of(tool_input) -> Optional[List[dict]]:
    """The questions of an AskUserQuestion tool_input in the shape we keep, or None when it is not one."""
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("questions"), list) or not tool_input["questions"]:
        return None
    out = []
    for q in tool_input["questions"][:8]:
        if not isinstance(q, dict) or not isinstance(q.get("question"), str) or not isinstance(q.get("options"), list):
            return None
        labels = [o.get("label") for o in q["options"] if isinstance(o, dict)]
        if len(labels) != len(q["options"]) or len(labels) < 2 or not all(isinstance(x, str) and x.strip() for x in labels):
            return None
        out.append({"question": _norm(q["question"])[:MAX_TEXT], "multiSelect": bool(q.get("multiSelect")),
                    "options": [_norm(x)[:MAX_TEXT] for x in labels]})
    return out


def record(session: str, tool_input, now: Optional[float] = None) -> bool:
    questions = questions_of(tool_input)
    if not session or questions is None:
        return False
    write_private(_path(session), json.dumps({"session": session, "at": time.time() if now is None else now,
                                              "questions": questions}, ensure_ascii=False))
    return True


def clear(session: str) -> None:
    if not session:
        return
    try:
        _path(session).unlink()
    except OSError:
        pass


def load(session: str, now: Optional[float] = None) -> Optional[dict]:
    try:
        data = json.loads(_path(session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("session") != session or not isinstance(data.get("questions"), list):
        return None
    at = data.get("at")
    if not isinstance(at, (int, float)) or (time.time() if now is None else now) - at > MAX_AGE:
        return None
    return data
