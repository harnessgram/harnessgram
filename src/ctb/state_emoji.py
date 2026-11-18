"""Choose and update the lone emoji shown while a session is working."""
from __future__ import annotations

import fcntl
import json
import re
import time
from typing import Optional

from .config import home_chat, private_dir, state_dir, write_private
from .telegram import TelegramError

DEFAULT = "🧑🏼‍💻"
EDIT_EVERY = 20

_READ = frozenset({"Read", "Glob", "Grep", "WebSearch", "WebFetch", "Search", "Fetch"})
_WRITE = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch"})
_DELEGATE = frozenset({"Agent", "Task", "SendMessage", "TeamCreate"})
_WAIT = frozenset({"AskUserQuestion", "ExitPlanMode"})
_BUILD = re.compile(r"\b(build|bundle|deploy|publish|release|upload|docker\s+(?:build|push)|git\s+push)\b", re.I)


def for_activity(session_state: str = "busy", tool_name: str = "", tool_input=None) -> str:
    """Return one standard activity emoji from the state and latest tool call."""
    if session_state == "waiting" or tool_name in _WAIT:
        return "✋"
    if session_state == "done":
        return "✅"
    if tool_name in _DELEGATE or tool_name.startswith("mcp__collaboration__"):
        return "🤝"
    if tool_name in _READ or any(word in tool_name.lower() for word in ("search", "read", "fetch")):
        return "🔎"
    if tool_name in _WRITE:
        return DEFAULT
    if tool_name in {"Bash", "Shell", "exec_command"}:
        inp = tool_input if isinstance(tool_input, dict) else {}
        command = str(inp.get("command") or inp.get("cmd") or inp.get("description") or "")
        if _BUILD.search(command):
            return "🚀"
        return "🧪"
    if any(word in tool_name.lower() for word in ("upload", "deploy", "build", "publish")):
        return "🚀"
    return "🤔"


def enabled(cfg) -> bool:
    return getattr(cfg, "state_emoji", True) is not False


def update(cfg, session: str, activity: Optional[dict] = None, bot=None, now: Optional[float] = None) -> bool:
    """Edit an existing working placeholder when its activity changes; never raises."""
    if not getattr(cfg, "ready", False) or not session:
        return False
    from .topics import TopicMap, session_state, session_state_path
    from .telegram import Bot

    activity = activity if isinstance(activity, dict) else {}
    path = session_state_path(session)
    private_dir(path.parent)
    now = time.time() if now is None else now
    try:
        with open(str(path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            cur = session_state(session)
            mid = cur.get("placeholder")
            if cur.get("state") != "busy" or type(mid) is not int or mid <= 0:
                return False
            target = (for_activity(cur.get("state", ""), str(activity.get("tool_name") or ""),
                                   activity.get("tool_input")) if enabled(cfg) else DEFAULT)
            shown = str(cur.get("placeholder_emoji") or DEFAULT)
            if target == shown or now - float(cur.get("placeholder_emoji_at") or 0) < EDIT_EVERY:
                return False
            # Only now read the topic map: most tool calls stop above without touching it.
            found = TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg).place_of(session)
            if found is None:
                return False
            bot = bot or Bot.from_config(cfg)
            # The limit covers attempts too: an unavailable Telegram API must not be hammered. This runs in
            # the after-tool hook, so the edit gets a short timeout.
            cur["placeholder_emoji_at"] = now
            write_private(path, json.dumps(cur))
            try:
                bot.call("editMessageText", {"chat_id": found.chat, "message_id": mid, "text": target}, timeout=3)
            except TelegramError:
                return False
            cur["placeholder_emoji"] = target
            write_private(path, json.dumps(cur))
            return True
    except (OSError, ValueError, TypeError):
        return False
