"""`ctb typing`: the "typing..." indicator in a session's topic.

The hooks start this helper (detached) while a session works, so it keeps its imports small: the Bot client,
the flood gate and the topic map, not the whole mirror.
"""
from __future__ import annotations

from .config import Config


def run_typing(cfg: Config, session: str, bot_class=None, find_place=None, gate=None) -> int:
    """'typing...' in the session's topic (sendChatAction). Quiet when the session has no topic.
    The three optional arguments are the Bot class, the topic lookup and the flood gate to use; mirror.run_typing
    passes its own names, which tests replace."""
    try:
        if gate is None:
            from . import flood as gate
        if bot_class is None:
            from .telegram import Bot as bot_class
        if find_place is None:
            from .topics import place as find_place
        bot = bot_class.from_config(cfg)
        found = find_place(cfg, session)
        if found:
            gate.optional_call(bot, "sendChatAction", {"chat_id": found.chat, "message_thread_id": found.thread,
                                                        "action": "typing"}, budget=False)  # gate only
    except Exception:
        return 1
    return 0
