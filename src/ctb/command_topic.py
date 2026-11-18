"""Private bindings from forum topics to owner-configured local commands."""
from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import selectors
import signal
from pathlib import Path
import subprocess
import sys
import threading
import tempfile
import time

from . import config as C, flood
from .telegram import TelegramError
from .topics import create_forum_topic

LOG = logging.getLogger(__name__)
REFUSED = "כאן מתקבל רק טקסט שהקלדת"
BUSY = "רגע, עוד עונה על ההודעה הקודמת"
ERROR = "לא הצלחתי לקבל תשובה. אפשר לנסות שוב."
MEDIA = ("voice", "audio", "photo", "document", "video", "video_note", "animation", "sticker", "contact", "location", "poll", "dice", "venue", "game")


def _path():
    return C.state_dir() / "command-topics.json"


def bindings():
    return C.read_map(_path())


def cli(args):
    action = args.command_topic_cmd
    if action == "list":
        print(json.dumps(bindings(), ensure_ascii=False, indent=2))
        return 0
    if action == "remove":
        with C.locked_map(_path()) as data:
            if args.name not in data:
                print("Binding not found.", file=sys.stderr)
                return 1
            del data[args.name]
        return 0
    argv = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
    cwd = os.path.abspath(os.path.expanduser(args.cwd))
    if (not args.name.strip() or len(args.name) > 128 or not argv or not all(argv)
            or not math.isfinite(args.timeout_s) or args.timeout_s <= 0 or not os.path.isdir(cwd)):
        print("Invalid name, directory, timeout or command.", file=sys.stderr)
        return 2
    from .telegram import Bot, TelegramError
    try:
        cfg = C.load()
        chat = C.home_chat(cfg)
        if chat >= 0 or cfg.owner_chat_id <= 0:
            raise ValueError("A paired owner and home forum group are required")
        with C.locked_map(_path()) as data:
            if args.name in data:
                raise ValueError("Binding already exists")
            thread = create_forum_topic(Bot.from_config(cfg), chat, args.name)
            data[args.name] = {"chat": chat, "thread": thread, "name": args.name,
                               "argv": argv, "cwd": cwd, "timeout_s": args.timeout_s}
    except (C.ConfigError, TelegramError, ValueError, OSError, KeyError):
        print("Could not create the command topic.", file=sys.stderr)
        return 1
    return 0


CHUNK_SIZE = 4000
MAX_CHUNKS = 4
CUT = "…התשובה קוצרה"
OUTPUT_LIMITS = (256 * 1024, 16 * 1024)


def _notice(bot, binding, text):
    try:
        bot.call("sendMessage", {"chat_id": binding["chat"], "message_thread_id": binding["thread"], "text": text})
    except TelegramError as exc:
        LOG.warning("command topic send failed: %s", exc.code)


def _send(bot, binding, text):
    # Keep the cut notice inside the last chunk and send literal text.
    limit = CHUNK_SIZE * MAX_CHUNKS
    if len(text) > limit:
        text = text[:limit - len(CUT) - 1] + "\n" + CUT
    for i in range(0, len(text), CHUNK_SIZE):
        if i:
            time.sleep(3)
        params = {"chat_id": binding["chat"], "message_thread_id": binding["thread"],
                  "text": text[i:i + CHUNK_SIZE]}
        for attempt in range(3):
            delay = flood.remaining(binding["chat"])
            if delay:
                time.sleep(delay)
            try:
                flood.optional_call(bot, "sendMessage", params, budget=False)
                break
            except TelegramError as exc:
                if exc.code != 429 or attempt == 2:
                    LOG.warning("command topic send failed: %s", exc.code)
                    return
                delay = exc.retry_after
                if not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay <= 0:
                    delay = flood.DEFAULT_PAUSE
                time.sleep(max(delay, flood.remaining(binding["chat"])))


def _execute(binding, text):
    # A file for stdin prevents a child that never reads from blocking the timeout.
    with tempfile.TemporaryFile() as stdin:
        stdin.write(json.dumps({"message": text}, ensure_ascii=False).encode("utf-8"))
        stdin.seek(0)
        proc = subprocess.Popen(binding["argv"], cwd=os.path.expanduser(binding["cwd"]),
                                stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        output = [bytearray(), bytearray()]
        deadline = time.monotonic() + binding["timeout_s"]
        try:
            with selectors.DefaultSelector() as selector:
                for index, pipe in enumerate((proc.stdout, proc.stderr)):
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, index)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(binding["argv"], binding["timeout_s"])
                    for key, _ in selector.select(remaining):
                        data = os.read(key.fileobj.fileno(), 8192)
                        if not data:
                            selector.unregister(key.fileobj)
                        else:
                            output[key.data].extend(data)
                            if len(output[key.data]) > OUTPUT_LIMITS[key.data]:
                                raise ValueError("output limit exceeded")
                proc.wait(timeout=max(0, deadline - time.monotonic()))
            LOG.info("command topic exit code: %d", proc.returncode)
            if proc.returncode:
                raise ValueError("command failed")
            return output[0].decode("utf-8")
        finally:
            # Also stop descendants that outlive a successful direct child.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()


def _run(binding, text, bot, lock):
    try:
        try:
            obj = json.loads(_execute(binding, text))
            if not isinstance(obj, dict) or not isinstance(obj.get("reply"), str) or not obj["reply"]:
                raise ValueError("invalid reply")
            reply = obj["reply"]
        except (subprocess.TimeoutExpired, OSError, ValueError, TypeError, KeyError, UnicodeError):
            reply = ERROR
        _send(bot, binding, reply)
    finally:
        lock.close()


def handle(update, cfg, bot, started, from_owner):
    q = update.get("callback_query") or {}
    m = update.get("message") or update.get("edited_message") or q.get("message") or {}
    chat = (m.get("chat") or {}).get("id")
    thread = m.get("message_thread_id")
    if not thread:
        return None
    binding = next((v for v in bindings().values() if isinstance(v, dict)
                    and v.get("chat") == chat and v.get("thread") == thread), None)
    if binding is None:
        return None
    valid = (bool(update.get("message")) and from_owner(m, cfg, started)
             and isinstance(m.get("text"), str) and bool(m["text"])
             and not any(m.get(k) for k in MEDIA)
             and not any(m.get(k) for k in ("forward_from", "forward_from_chat", "forward_date", "forward_sender_name")))
    if not valid:
        # Service messages (topic created/renamed, pins), edits, button taps and bots are dropped silently; only a
        # person's real message that cannot go in gets the one-line explanation.
        human = bool(update.get("message")) and not (m.get("from") or {}).get("is_bot") and (
            isinstance(m.get("text"), str) or any(m.get(k) for k in MEDIA))
        if human:
            _notice(bot, binding, REFUSED)
        return "command-topic-refused"
    try:
        C.private_dir(C.state_dir())
        lock = open(C.state_dir() / ("command-topic-%s-%s.lock" % (chat, thread)), "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            _notice(bot, binding, BUSY)
            return "command-topic-busy"
        try:
            threading.Thread(target=_run, args=(binding, m["text"], bot, lock), daemon=True).start()
        except BaseException:
            lock.close()
            raise
    except (OSError, RuntimeError):
        _notice(bot, binding, ERROR)
        return "command-topic-error"
    return "command-topic"
