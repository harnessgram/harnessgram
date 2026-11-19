"""Conservative detection and one-shot handling of questions addressed to the owner."""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
import time
from typing import Optional

from .config import locked_map, private_dir, read_map, state_dir, write_private


_ADDRESS = re.compile(
    r"(?i)\b(?:should (?:I|we)|shall (?:I|we)|want me to|do you want|which do you|would you (?:like|prefer)|"
    r"can you (?:choose|confirm)|please (?:choose|confirm|approve))\b|"
    r"(?:לאשר|מה אתה מעדיף|האם תרצה|האם אתה רוצה|רוצה שאמשיך)"
)
_SECOND_PERSON = re.compile(r"(?i)\b(?:you|your)\b|(?:אתה|שלך)")
_RHETORICAL = re.compile(r"(?i)^(?:why (?:not|bother)|who (?:knows|cares)|what could go wrong|isn't it obvious|"
                         r"needless to say|did you know|מי יודע|למה לא|מה כבר יכול להשתבש)\b")
_CARD_ID = re.compile(r"(?i)\b(?:decision|card)\s+(?:id\s*[:#]?\s*)?[0-9a-f]{16}\b")
_COMMAND = re.compile(r"\bctb\s+(?:decide\s+ask|card\s+(?:ask|choice))\b")
_LEDGER = "auto-owner-questions.json"


def _visible_lines(text: str) -> list[str]:
    out, fenced = [], False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("```") or line.startswith("~~~"):
            fenced = not fenced
            continue
        if fenced or not line or line.startswith((">", "#", "|")):
            continue
        # Whole quoted lines are examples/peer text, not the assistant addressing its reader.
        if len(line) >= 2 and line[0] in "\"'“‘«" and line[-1] in "\"'”’»":
            continue
        # ASCII apostrophes inside words are contractions, not quote delimiters.
        line = re.sub(r'"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|«[^»\n]*»|(?<!\w)\'[^\'\n]*\'(?!\w)', "", line).strip()
        if not line:
            continue
        out.append(line)
    return out


def question(text: str) -> Optional[str]:
    """Return one conservatively identified owner question, otherwise ``None``."""
    lines = [part.strip() for line in _visible_lines(text)
             for part in re.split(r"(?<=[.!?؟])\s+", line) if part.strip()]
    for i, line in enumerate(lines):
        if not line.endswith(("?", "؟")) or _RHETORICAL.search(line):
            continue
        if not (_ADDRESS.search(line) or _SECOND_PERSON.search(line)):
            continue
        # An immediate answer means the question was rhetorical. Status summaries,
        # continuation promises and option lists instead follow genuine owner questions.
        if i + 1 < len(lines) and not lines[i + 1].endswith(("?", "؟")) and not re.match(
                r"(?i)^(?:\*{0,2}(?:summary|סיכום)\s*:|meanwhile\b.*\b(?:I(?:'|’)ll|I will)\s+keep working\b|"
                r"I will continue with the recommended option meanwhile\.|"
                r"(?:[-*•]\s+|\d+[.)](?:\s+|$)))", lines[i + 1]):
            continue
        return line[:1000]
    return None


def references_card(text: str) -> bool:
    return bool(_CARD_ID.search(text or ""))


def has_real_prompt(lines: list[str]) -> bool:
    """Whether the transcript slice contains an owner prompt rather than generated hook context."""
    for raw in lines:
        try:
            row = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if row.get("type") != "user" or row.get("isMeta"):
            continue
        content = (row.get("message") or {}).get("content")
        items = content if isinstance(content, list) else [{"type": "text", "text": content}]
        texts = [str(x.get("text") or "") for x in items
                 if isinstance(x, dict) and x.get("type") == "text"]
        if texts and not any(text.lstrip().startswith("<task-notification>") for text in texts):
            return True
    return False


def turn_used_explicit_card(lines: list[str]) -> bool:
    """Look only after the current turn's last genuine user prompt for an explicit card command."""
    current: list[dict] = []
    for raw in lines:
        try:
            row = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if row.get("type") == "user" and not row.get("isMeta"):
            content = (row.get("message") or {}).get("content")
            items = content if isinstance(content, list) else [{"type": "text", "text": content}]
            if any(isinstance(x, dict) and x.get("type") == "text" for x in items):
                current = []
                continue
        current.append(row)
    for row in current:
        if row.get("type") != "assistant":
            continue
        content = (row.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" \
                    and _COMMAND.search(json.dumps(block.get("input"), ensure_ascii=False)):
                return True
    return False


def turn_key(session: str, lines: list[str], _prompt: str) -> str:
    """Identity shared by Stop and mirror: the last genuine user prompt, not generated context."""
    last = ""
    for raw in lines:
        try:
            row = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if row.get("type") != "user" or row.get("isMeta"):
            continue
        content = (row.get("message") or {}).get("content")
        items = content if isinstance(content, list) else [{"type": "text", "text": content}]
        texts = [str(x.get("text") or "") for x in items
                 if isinstance(x, dict) and x.get("type") == "text"]
        if texts and not any(text.lstrip().startswith("<task-notification>") for text in texts):
            last = str(row.get("uuid") or row.get("id") or json.dumps(content, sort_keys=True, ensure_ascii=False))
    material = session + "\0" + last
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()


def claim_once(session: str, key: str, kind: str, *, timeout: Optional[float] = None) -> bool:
    """Durably claim a per-turn action; false when this action already ran."""
    path = state_dir() / _LEDGER
    token = f"{kind}:{session}:{key}"
    if token in read_map(path):
        return False
    if timeout is None:
        with locked_map(path, keep=400) as data:
            if token in data:
                return False
            data[token] = True
    else:
        private_dir(path.parent)
        deadline = time.monotonic() + timeout
        with open(str(path) + ".lock", "a") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(0.01)
            data = read_map(path)
            if token in data:
                return False
            data[token] = True
            write_private(path, json.dumps(dict(list(data.items())[-400:]), ensure_ascii=False))
    return True


def claimed(session: str, key: str, kind: str) -> bool:
    return f"{kind}:{session}:{key}" in read_map(state_dir() / _LEDGER)

