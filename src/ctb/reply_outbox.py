"""Private, durable outbox for reply parts and delivery notices.

Session locks serialize send/enqueue and send/remove without holding the queue
lock during network calls. Never log payloads. Telegram has no idempotency key, so delivery
is at-least-once: there is a narrow duplicate window if the process crashes after Telegram
accepts a message but before the local removal is committed.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import time

from .config import private_dir, state_dir, write_private
from .moves import _file_lock

QUEUE_FILE = "reply-outbox.json"
FAILED_FILE = "reply-outbox-failed.json"
KEEP = 50


def locked():
    return _file_lock(QUEUE_FILE)


@contextlib.contextmanager
def session_locked(session, blocking=True):
    """A heartbeat skips busy sessions; a crashed sender releases its lock automatically."""
    name = hashlib.sha256(session.encode()).hexdigest() + ".lock"
    path = private_dir(state_dir() / "reply-locks") / name
    with open(path, "a") as lock:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def save(path, entries) -> None:
    write_private(path, json.dumps(entries, ensure_ascii=False))


def load(path, now=None) -> list:
    from .mirror import log
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except ValueError:
        log("reply outbox unreadable; started empty")
        save(path, [])
        return []
    if not isinstance(entries, list):
        log("reply outbox unreadable; started empty")
        save(path, [])
        return []
    valid = [e for e in entries if isinstance(e, dict) and isinstance(e.get("created"), (int, float))
             and isinstance(e.get("session"), str) and isinstance(e.get("text"), str)
             and isinstance(e.get("options"), dict) and "chat" in e and "thread" in e and "title" in e]
    invalid = len(entries) - len(valid)
    if invalid:
        log(f"reply outbox pruned (invalid {invalid})")
        save(path, valid)
    return valid


def append(path, entries, entry) -> bool:
    entries.append(entry)
    save(path, entries)
    return True


def retry_failed() -> int:
    """Put every permanently failed part back at the end of the pending queue."""
    with locked() as path:
        failed_path = path.with_name(FAILED_FILE)
        failed = load(failed_path)
        if not failed:
            return 0
        entries = load(path)
        for entry in failed:
            entry.pop("failures", None)
            entry.pop("auth_reported", None)
        save(path, entries + failed)
        save(failed_path, [])
        return len(failed)


def drain(cfg, bot, now=None) -> int:
    """Retry each session in order, skipping busy or blocked sessions, while enabled."""
    from . import mirror, policy
    if not cfg.mirror or not policy.on():
        return 0
    delivered = 0
    with locked() as path:
        if not policy.on():
            return 0
        sessions = list(dict.fromkeys(e["session"] for e in load(path, now)))
    if not sessions:
        return 0
    sender = mirror.build(cfg, bot=bot)
    for session in sessions:
        with session_locked(session, blocking=False) as acquired:
            if not acquired:
                continue
            delivered += _drain_session(sender, session, now)
    return delivered


def _drain_session(sender, session, now) -> int:
    from . import flood, lang, mirror, policy
    delivered = 0
    while policy.on():
        with locked() as path:
            entry = next((e for e in load(path, now) if e["session"] == session), None)
        if entry is None:
            break
        try:
            found = sender.topics.place_of(entry["session"])
            chat = found.chat if found else entry["chat"]
            if flood.remaining(chat) > 0:
                break
            # Retain the saved destination when the topic map has no entry.
            if not found and entry["thread"]:
                sender.topics.ensure(entry["session"], entry["title"],
                                     thread=entry["thread"], chat=entry["chat"])
            options = dict(entry["options"])
            if entry.get("answer_to") and entry["answer_to"]["chat"] != chat:
                options.pop("reply_parameters", None)  # message ids belong to their original chat
            marker = lang.strings(entry.get("language") or "en")["delayed"]
            sender.first_reply_id = None
            sender.last_error = None
            sender.last_error_plain = False
            ok = sender._post_now(chat, sender.topics, entry["session"], entry["title"],
                                  marker + "\n" + entry["text"], **options)
        except (mirror.TelegramError, OSError) as exc:
            sender.last_error = exc
            ok = False
        with locked() as path:
            entries = load(path, now)
            if entry not in entries:
                break
            index = entries.index(entry)
            if ok:
                entries.pop(index)
            else:
                entry["failures"] = entry.get("failures", 0) + 1
                code = getattr(sender.last_error, "code", None)
                permanent = code == 400 and getattr(sender, "last_error_plain", False)
                if permanent:
                    failed_path = path.with_name(FAILED_FILE)
                    failed = load(failed_path, now)
                    save(failed_path, failed + [entry])
                    entries.pop(index)
                    mirror.log(f"reply outbox part moved aside (code {code}, failures {entry['failures']})")
                else:
                    if code in (401, 403) and not entry.get("auth_reported"):
                        mirror.record_outcome(False)
                        mirror.log(f"reply outbox authentication failed (code {code}); part remains pending")
                        entry["auth_reported"] = True
                    entries[index] = entry
                    save(path, entries)
                    break  # only this session waits for a later round
            save(path, entries)
        if ok:
            delivered += 1
            if entry.get("index"):
                try:
                    sender._index_reply(entry["session"], sender.first_reply_id,
                                        entry["index"]["text"], entry["index"]["has_summary"])
                except Exception:
                    mirror.log("delayed reply index update failed")
    return delivered
