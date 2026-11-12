"""Proof that a numbered dialog on the pane is a real AskUserQuestion.

Pane text can never prove anything. The PreToolUse hook (`ctb hook-ask`, matcher AskUserQuestion) records the
call's structured tool_input before the question is shown: its questions, option labels and multiSelect flags.
A tap-to-answer card requires the complete question and ordered labels to match their recorded hash.
Proof expires after thirty minutes, binds to the first matching screen's identity and stamp, and is consumed
atomically before a tap sends keys. The record is cleared on any PostToolUse (the question was answered,
or another tool ran) and at Stop, and it is read only for the session that wrote it.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import time
from typing import List, Optional

from .config import private_dir, state_dir, write_private

MAX_AGE = 1800           # thirty minutes, like the typed-answer window; older questions stay typed-only
MAX_TEXT = 400


@contextlib.contextmanager
def _locked():
    # Keep the lock outside ask/: records can be replaced or removed while another process waits.
    with open(private_dir(state_dir()) / "ask.lock", "a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _path(session: str):
    return private_dir(state_dir() / "ask") / ((re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_") + ".json")


def _norm(text: str) -> str:
    return " ".join(text.replace("’", "'").replace("‘", "'").replace("ʼ", "'").replace("‏", "").split())


def fingerprint(question: str, options: List[str], multi: bool) -> str:
    """Hash the complete text and ordered labels, preserving punctuation and case."""
    raw = json.dumps([_norm(question), [_norm(x) for x in options], multi], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def questions_of(tool_input) -> Optional[List[dict]]:
    """The questions of an AskUserQuestion tool_input in the shape we keep, or None when it is not one."""
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("questions"), list) or not tool_input["questions"]:
        return None
    if len(tool_input["questions"]) > 8:
        return None
    out = []
    for q in tool_input["questions"]:
        if not isinstance(q, dict) or not isinstance(q.get("question"), str) or not isinstance(q.get("options"), list):
            return None
        labels = [o.get("label") for o in q["options"] if isinstance(o, dict)]
        if (len(labels) != len(q["options"]) or not 2 <= len(labels) <= 8
                or not all(isinstance(x, str) and x.strip() for x in labels)):
            return None
        text, labels = _norm(q["question"]), [_norm(x) for x in labels]
        multi = q.get("multiSelect", False)
        if (not text or len(text) > MAX_TEXT or any(len(x) > MAX_TEXT for x in labels)
                or not isinstance(multi, bool)):
            return None
        out.append({"question": text, "multiSelect": multi, "options": labels,
                    "hash": fingerprint(text, labels, multi)})
    return out


def record(session: str, tool_input, now: Optional[float] = None) -> bool:
    questions = questions_of(tool_input)
    if not session:
        return False
    with _locked():
        if questions is None:
            _path(session).unlink(missing_ok=True)
            return False
        write_private(_path(session), json.dumps({"session": session, "at": time.time() if now is None else now,
                                                  "id": secrets.token_hex(16), "questions": questions,
                                                  "bindings": {}}, ensure_ascii=False))
    return True


def clear(session: str) -> None:
    if not session:
        return
    try:
        with _locked():
            _path(session).unlink(missing_ok=True)
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
    if (type(at) not in (int, float) or not math.isfinite(at)
            or not 0 <= (time.time() if now is None else now) - at <= MAX_AGE
            or not isinstance(data.get("id"), str) or not data["id"]
            or not isinstance(data.get("bindings"), dict)):
        return None
    for q in data["questions"]:
        if (not isinstance(q, dict) or not isinstance(q.get("question"), str)
                or not q["question"].strip() or not isinstance(q.get("multiSelect"), bool)
                or not isinstance(q.get("options"), list) or not 2 <= len(q["options"]) <= 8
                or not all(isinstance(x, str) and x.strip() for x in q["options"])
                or q.get("hash") != fingerprint(q["question"], q["options"], q["multiSelect"])):
            return None
    return data


def bind(session: str, question: dict, identity: str, stamp: str) -> Optional[dict]:
    """Bind a fresh hook question to its first exact screen; never follow a changed screen implicitly."""
    try:
        digest = fingerprint(question["question"], question["options"], question["multiSelect"])
        with _locked():
            rec = load(session)
            if not rec or question.get("hash") != digest or question not in rec["questions"]:
                return None
            binding = rec["bindings"].get(digest)
            expected = {"identity": identity, "stamp": stamp, "used": False}
            if binding is None:
                rec["bindings"][digest] = expected
                write_private(_path(session), json.dumps(rec, ensure_ascii=False))
            elif binding != expected:
                return None
            return dict(question, _record=rec["id"])
    except (OSError, TypeError, KeyError, ValueError):
        return None


def claim(session: str, proof: dict, identity: str, stamp: str) -> bool:
    """Consume before sending any key. Competing callbacks/processes can claim a screen only once."""
    try:
        with _locked():
            rec = load(session)
            if not rec or rec["id"] != proof.get("_record"):
                return False
            binding = rec["bindings"].get(proof["hash"])
            if binding != {"identity": identity, "stamp": stamp, "used": False}:
                return False
            binding["used"] = True
            write_private(_path(session), json.dumps(rec, ensure_ascii=False))
            return True
    except (OSError, TypeError, KeyError, ValueError):
        return False


def advance(session: str, proof: dict, identity: str, previous: str, stamp: str) -> None:
    """Only a verified multi-select toggle can authorize the resulting new screen."""
    if stamp == previous:
        return
    with _locked():
        rec = load(session)
        if not rec or rec["id"] != proof.get("_record"):
            return
        binding = rec["bindings"].get(proof["hash"])
        if binding == {"identity": identity, "stamp": previous, "used": True}:
            rec["bindings"][proof["hash"]] = {"identity": identity, "stamp": stamp, "used": False}
            write_private(_path(session), json.dumps(rec, ensure_ascii=False))
