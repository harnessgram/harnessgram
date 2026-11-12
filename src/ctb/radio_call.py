"""The radio's group live stream, through the owner's own Telegram account (Telethon).

A bot cannot start or end a group's live stream; only a group admin's user account can. This file runs as a
standalone script under a Python that has Telethon (`radio.telethon_python` in the config), never inside the
bridge process, so the bridge itself needs no Telethon and never holds the account session.

Input is one JSON object on stdin: {"action": "ensure" | "end" | "status", "chat": <supergroup id>,
"allowed": [<supergroup id>, ...], "title": "...", "call": {"id", "access_hash"},
"api_command": [...], "session_command": [...]}. The two commands print the account's API id:hash and its
session string (for example from the macOS Keychain); their output stays in this process's memory and is never
printed or written anywhere. Output is one JSON line without any secret.

Scope: only the group in "allowed" (the caller passes its home group only), and only these calls:
read the group's active call, create an RTMP live stream, read its state, and end it. "ensure" takes over
only the radio's own earlier stream (the "call" it is given) and refuses when anything else is live; "end"
ends only that same call.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

BUSY = "the group already has another live stream or voice chat"


def _secret(command) -> str:
    if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
        raise SystemExit(_out({"ok": False, "error": "missing credential command"}))
    r = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    value = r.stdout.decode("utf-8", "replace").strip()
    if r.returncode or not value:
        raise SystemExit(_out({"ok": False, "error": "credential unavailable"}))
    return value


def _out(obj: dict) -> int:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
    return 0 if obj.get("ok") else 1


def _channel_id(chat: int) -> int:
    """-100123 -> 123 (Telethon's PeerChannel id)."""
    text = str(chat)
    if not text.startswith("-100") or not text[4:].isdigit():
        raise SystemExit(_out({"ok": False, "error": "not a supergroup"}))
    return int(text[4:])


async def _run(req: dict) -> dict:
    from telethon import TelegramClient, functions
    from telethon.sessions import StringSession
    from telethon.tl.types import InputGroupCall, PeerChannel

    action, chat = req.get("action"), req.get("chat")
    if action not in ("ensure", "end", "status"):
        return {"ok": False, "error": "unknown action"}
    if type(chat) is not int or chat not in (req.get("allowed") or []):
        return {"ok": False, "error": "chat not allowed"}
    creds = _secret(req.get("api_command"))
    api_id, _, api_hash = creds.partition(":") if ":" in creds else creds.partition(" ")
    if not api_id.strip().isdigit() or not api_hash.strip():
        return {"ok": False, "error": "credential unavailable"}
    client = TelegramClient(StringSession(_secret(req.get("session_command"))), int(api_id), api_hash.strip(),
                            receive_updates=False)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return {"ok": False, "error": "account not authorized"}
        peer = await client.get_input_entity(PeerChannel(_channel_id(chat)))
        full = await client(functions.channels.GetFullChannelRequest(peer))
        active = full.full_chat.call

        async def describe(call) -> dict:
            got = await client(functions.phone.GetGroupCallRequest(call=call, limit=10))
            gc = got.call
            info = {"id": gc.id, "access_hash": gc.access_hash, "rtmp": bool(getattr(gc, "rtmp_stream", False)),
                    "participants": int(getattr(gc, "participants_count", 0) or 0),
                    "title": getattr(gc, "title", None) or "", "live": None}
            dc = getattr(gc, "stream_dc_id", None)
            info["stream_dc"] = bool(dc)
            try:
                req = functions.phone.GetGroupCallStreamChannelsRequest(call=call)
                if dc:  # the stream's media lives on its own data center
                    sender = await client._borrow_exported_sender(dc)
                    try:
                        ch = await client._call(sender, req)
                    finally:
                        await client._return_exported_sender(sender)
                else:
                    ch = await client(req)
                channels = getattr(ch, "channels", None) or []
                info["live"] = bool(channels)
                info["stream_ms"] = max((int(getattr(c, "last_timestamp_ms", 0) or 0) for c in channels), default=0)
            except Exception as e:  # e.g. the stream has no media yet
                info["live_error"] = e.__class__.__name__
            return info

        if action == "status":
            if active is None:
                return {"ok": True, "active": False}
            return {"ok": True, "active": True, "call": await describe(active)}
        want = req.get("call") or {}
        if action == "ensure":
            if active is not None:
                # Only the radio's own earlier stream (its id in "call") is taken over; any other live stream
                # or voice chat belongs to someone else: never publish into it, never end it.
                if want.get("id") is None or want.get("id") != active.id:
                    return {"ok": False, "error": BUSY}
                return {"ok": True, "active": True, "created": False, "owned": True, "call": await describe(active)}
            title = str(req.get("title") or "")[:64] or None
            await client(functions.phone.CreateGroupCallRequest(
                peer=peer, rtmp_stream=True, random_id=int.from_bytes(os.urandom(4), "big") >> 1,
                title=title))
            full = await client(functions.channels.GetFullChannelRequest(peer))
            if full.full_chat.call is None:
                return {"ok": False, "error": "the stream was not created"}
            return {"ok": True, "active": True, "created": True, "owned": True,
                    "call": await describe(full.full_chat.call)}
        # end: only the call we were given, and only when it is still the group's active call. Another call
        # running now means ours is already over: nothing to end, and the other one is left alone.
        if active is None:
            return {"ok": True, "active": False, "ended": False}
        if want.get("id") is None or want.get("id") != active.id:
            return {"ok": True, "active": True, "ended": False, "ours": False}
        await client(functions.phone.DiscardGroupCallRequest(call=InputGroupCall(active.id, active.access_hash)))
        return {"ok": True, "active": False, "ended": True}
    finally:
        await client.disconnect()


def main() -> int:
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return _out({"ok": False, "error": "bad request"})
    if not isinstance(req, dict):
        return _out({"ok": False, "error": "bad request"})
    try:
        return _out(asyncio.run(_run(req)))
    except SystemExit as e:
        return int(e.code or 1)
    except Exception as e:  # never echo details that could hold account data
        return _out({"ok": False, "error": e.__class__.__name__})


if __name__ == "__main__":
    sys.exit(main())
