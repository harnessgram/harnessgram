"""`ctb pair`: find out which Telegram account is the owner, safely.

Anyone who finds the bot could send `/start`. So pairing prints a one-time code and accepts only
`/start <code>`, only from a message sent after pairing began, and the terminal user still has to
confirm the account (name and numeric id) before anything is saved.
"""
from __future__ import annotations

import hmac
import secrets
import time
from typing import Callable, Optional


def new_code() -> str:
    return secrets.token_hex(4)


def wait_for_start(bot, code: str, started: float, timeout: float = 180, now: Callable[[], float] = time.time,
                   poll: int = 10) -> Optional[dict]:
    """The private-chat message "/start <code>" sent after `started`, or None on timeout.
    Updates are marked as read so the listener does not see them later."""
    offset, deadline, found = None, now() + timeout, None
    while now() < deadline and not found:
        params = {"timeout": poll, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        for u in bot.call("getUpdates", params, timeout=poll + 10):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            parts = (msg.get("text") or "").split()
            if ((msg.get("chat") or {}).get("type") == "private" and not (msg.get("from") or {}).get("is_bot")
                    and msg.get("date", 0) >= started - 2 and len(parts) == 2 and parts[0] == "/start"
                    and hmac.compare_digest(parts[1].encode(), code.encode())):
                found = msg
    if offset is not None:
        bot.call("getUpdates", {"offset": offset, "timeout": 0})
    return found


def wait_for_group_start(bot, bot_username: str, started: float, owner_id: int, timeout: float = 180,
                         now: Callable[[], float] = time.time, poll: int = 10) -> Optional[dict]:
    """The supergroup message "/start" or "/start@<bot>" sent after `started`.
    Only the already paired owner's message counts: anyone else in the group is ignored.
    Updates are marked as read so the listener does not see them later."""
    offset, deadline, found = None, now() + timeout, None
    bot_target = f"/start@{bot_username.lower()}" if bot_username else "/start"
    while now() < deadline and not found:
        params = {"timeout": poll, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        for u in bot.call("getUpdates", params, timeout=poll + 10):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            chat = msg.get("chat") or {}
            sender = msg.get("from") or {}
            if (chat.get("type") in ("group", "supergroup") or chat.get("id", 0) < 0) and not sender.get("is_bot"):
                if msg.get("date", 0) >= started - 2:
                    parts = (msg.get("text") or "").strip().lower().split()
                    if parts and parts[0] in ("/start", bot_target):
                        if owner_id and sender.get("id") == owner_id:
                            found = msg
                            break
    if offset is not None:
        bot.call("getUpdates", {"offset": offset, "timeout": 0})
    return found
