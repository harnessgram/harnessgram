"""Old, irrelevant forum topics: found by the bridge, closed only when the owner approves.

A topic is "stale" when nothing has happened in it for `topic_cleanup_days` (default 14): no session
activity (transcript or state file) and so no messages, and nothing is waiting on the owner there.
It is never offered when it is the General topic, a scheduled task's shared topic, one that has an open
decision, an open card, a reminder or an unticked checklist item, a pinned link on its board, or one the
owner marked "keep". A topic whose last activity is unknown is not offered either (stale cannot be proven).

Every session on a topic must qualify: one that is still running, working, has a queued owner message,
is mid-move or waits on a limit keeps the whole topic out. Only ended sessions count (never a live idle one).
Known gap: Telegram's own pinned messages cannot be read through the Bot API, so "pinned" means a link
pinned on the topic's bridge board.

The only action is Telegram's closeForumTopic, which is reversible: writing in a closed topic, or the
"reopen" undo here, opens it again. Deleting a topic is not offered. Every close, keep and reopen is
appended to topic-cleanup-log.jsonl (private, session id prefix only; no message text).
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from typing import List, Optional

import contextlib

from . import board, cards, decisions, flood, inbox as inbox_mod, scheduled, sessions_menu
from .config import Config, home_chat, private_dir, state_dir, write_private
from .projects import project_by_chat
from .telegram import TelegramError
from .topics import TopicMap

DEFAULT_DAYS = 14
MAX_LISTED = 20  # one list and one request never go beyond this (the same cap as the left-behind closer)
CLOSE_PACE = 1.0  # seconds between two closes
KEEP_FILE = "topic-cleanup-keep.json"
LOG_FILE = "topic-cleanup-log.jsonl"
UNDO_WINDOW = 7 * 86400  # a reopen is offered for topics this tool closed in the last week


def days(cfg: Config) -> int:
    n = getattr(cfg, "topic_cleanup_days", DEFAULT_DAYS)
    return n if type(n) is int and n > 0 else DEFAULT_DAYS


def _key(chat: int, thread: int) -> str:
    return f"{chat}:{thread}"


@contextlib.contextmanager
def _keep_lock():
    with open(str(private_dir(state_dir()) / KEEP_FILE) + ".lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def kept() -> dict:
    try:
        data = json.loads((state_dir() / KEEP_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _log(action: str, session: str, chat: int, thread: int, result: str, now: float) -> None:
    """One line per action. Only the session prefix, the place and the result are written."""
    try:
        root = private_dir(state_dir())
        line = json.dumps({"at": int(now), "action": action, "session": session[:8], "chat": chat,
                           "thread": thread, "result": result}, ensure_ascii=False)
        path = root / LOG_FILE
        with open(path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(line + "\n")
        os.chmod(path, 0o600)
    except OSError:
        pass


def log_lines() -> List[dict]:
    try:
        rows = [json.loads(x) for x in (state_dir() / LOG_FILE).read_text(encoding="utf-8").splitlines() if x]
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict)]


def _busy_places() -> set:
    """Every (chat, thread) where something still waits on the owner or was pinned for later."""
    places = set()
    for r in decisions.open_decisions():
        places.add((r.get("chat"), r.get("thread")))
    for r in cards.open_cards():
        places.add((r.get("chat"), r.get("thread")))
    for chat, thread, _item in board.open_reminders():
        places.add((chat, thread))
    for chat, thread, _item in board.open_checks():
        places.add((chat, thread))
    return places


def _pinned(chat: int, thread: int) -> bool:
    try:
        return any(i.get("k") == "link" for i in board.items_of(chat, thread))
    except Exception:
        return False


def _session_ok(e, limit: float, busy_sessions: set, box) -> bool:
    """One session on a topic that lets the topic be offered."""
    if e.state != "ended" or e.age is None or e.age < limit or e.session in busy_sessions:
        return False
    try:
        if scheduled.task_of(e.session) or box.pending(e.session) or not inbox_mod._closed(e.session):
            return False
    except Exception:
        return False
    return True


def candidates(cfg: Config, topics: TopicMap, now: Optional[float] = None) -> List[sessions_menu.Entry]:
    """The stale topics, quietest first; each is represented by its newest session."""
    now = time.time() if now is None else now
    limit = days(cfg) * 86400
    owned = decisions.owner_chats(cfg)
    busy_sessions = {r.get("session") for r in decisions.open_decisions()} | \
        {r.get("session") for r in cards.open_cards()}
    busy, keep = _busy_places(), kept()
    box = inbox_mod.Inbox(state_dir() / "inbox")
    by_topic = {}
    for e in sessions_menu.collect(topics, now):
        by_topic.setdefault((sessions_menu._chat(cfg, e), e.thread), []).append(e)
    out = []
    for (chat, thread), group in by_topic.items():
        if (thread <= 1 or chat not in owned or (chat, thread) in busy or _key(chat, thread) in keep
                or _pinned(chat, thread) or any(e.closed for e in group)):
            continue
        if all(_session_ok(e, limit, busy_sessions, box) for e in group):  # one ineligible session rules the topic out
            out.append(min(group, key=lambda e: e.age))
    return sorted(out, key=lambda e: e.age, reverse=True)


def view(cfg: Config, topics: TopicMap, now: Optional[float] = None) -> dict:
    """What the Cockpit card shows. Session ids stay short and chat ids stay on the server."""
    from . import panel_view
    now = time.time() if now is None else now
    found = candidates(cfg, topics, now)
    rows = []
    for e in found[:MAX_LISTED]:
        chat = sessions_menu._chat(cfg, e)
        project = project_by_chat(cfg, chat)
        rows.append({"id": e.session[:8], "name": e.name, "project": project.name if project else "",
                     "age": int(e.age), "url": panel_view.topic_url(chat, e.thread)})
    return {"days": days(cfg), "count": len(rows), "more": max(0, len(found) - len(rows)), "topics": rows,
            "undo": undoable(cfg, topics, now)}


def undoable(cfg: Config, topics: TopicMap, now: float) -> List[str]:
    """Short ids of topics this tool closed in the last week and that are still closed."""
    marks = sessions_menu.closed_marks()
    ids = []
    for r in log_lines():
        if r.get("action") == "close" and r.get("result") == "ok" and now - r.get("at", 0) <= UNDO_WINDOW:
            ids.append(r["session"])
    return [i for i in dict.fromkeys(ids)
            if any(s[:8] == i for s in marks)][-MAX_LISTED:]


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _match(entries, short: str):
    found = [e for e in entries if e.session[:8] == short]
    return found[0] if len(found) == 1 else None


def keep(cfg: Config, topics: TopicMap, shorts: List[str], now: Optional[float] = None) -> dict:
    """Mark topics "keep forever": they are never offered again. Only topics that exist are marked."""
    return _mark(cfg, topics, shorts, True, now)


def unkeep(cfg: Config, topics: TopicMap, shorts: List[str], now: Optional[float] = None) -> dict:
    """The undo of keep."""
    return _mark(cfg, topics, shorts, False, now)


def _mark(cfg, topics, shorts, on: bool, now: Optional[float]) -> dict:
    now = time.time() if now is None else now
    entries = sessions_menu.collect(topics, now)
    n = 0
    with _keep_lock():
        data = kept()
        for short in shorts:
            e = _match(entries, short)
            if e is None or e.thread <= 1:
                continue
            chat = sessions_menu._chat(cfg, e)
            if on:
                data[_key(chat, e.thread)] = {"at": int(now)}
            elif data.pop(_key(chat, e.thread), None) is None:
                continue
            _log("keep" if on else "unkeep", e.session, chat, e.thread, "ok", now)
            n += 1
        if n:
            write_private(state_dir() / KEEP_FILE, json.dumps(data))
    return {"kept" if on else "unkept": n, "skipped": len(shorts) - n}


def close(cfg: Config, bot, topics: TopicMap, shorts: List[str], now: Optional[float] = None) -> dict:
    """Close the named topics, but only those the detector offers at the moment of each close (a stale page
    cannot close a topic that woke up, got a queued message or a decision meanwhile: the closes take up to
    ~20 s, so eligibility is checked again right before every one). The flood gate is read per target chat.
    Each close is logged."""
    closed, skipped, ids = 0, 0, []
    shorts = shorts[:MAX_LISTED]
    for i, short in enumerate(shorts):
        if i:
            _pause(CLOSE_PACE)
        stamp = time.time() if now is None else now
        e = _match(candidates(cfg, topics, stamp), short)  # fresh: anything that changed since the last look
        if e is None:
            skipped += 1
            continue
        chat = sessions_menu._chat(cfg, e)
        if not flood.is_open(chat):  # this chat's group was told to slow down: leave this one for the next tap
            skipped += 1
            continue
        result = sessions_menu._close_topic(cfg, bot, topics, e)
        _log("close", e.session, chat, e.thread, result, stamp)
        if result in ("ok", "gone"):
            closed += 1
            ids.append(short)
        else:
            skipped += 1
            if result == "flood":
                skipped += len(shorts) - i - 1
                break
    return {"closed": closed, "skipped": skipped, "closed_ids": ids}


def _reopen_call(cfg: Config, bot, session: str, mark) -> bool:
    """Ask Telegram to open the topic (twice at most) and forget the close only once it is open. On a failure
    the mark stays, so the topic is still known as closed here and stays in the undo list."""
    if not isinstance(mark, dict) or type(mark.get("thread")) is not int:
        return False
    chat = mark["chat"] if mark.get("chat") else home_chat(cfg)
    for _ in range(2):
        try:
            bot.call("reopenForumTopic", {"chat_id": chat, "message_thread_id": mark["thread"]})
            break
        except TelegramError as exc:
            if sessions_menu._already(exc) or sessions_menu._gone(exc):
                break
    else:
        return False
    sessions_menu._set_mark(session, None)
    return True


def reopen(cfg: Config, bot, topics: TopicMap, shorts: List[str], now: Optional[float] = None) -> dict:
    """The undo: reopen topics this tool closed (never any other closed topic)."""
    now = time.time() if now is None else now
    allowed = set(undoable(cfg, topics, now))
    marks = sessions_menu.closed_marks()
    opened = skipped = 0
    for short in shorts:
        sessions = [s for s in marks if s[:8] == short]
        if short not in allowed or len(sessions) != 1:
            skipped += 1
            continue
        mark = marks[sessions[0]]
        ok = _reopen_call(cfg, bot, sessions[0], mark)
        _log("reopen", sessions[0], (mark.get("chat") or home_chat(cfg)) if isinstance(mark, dict) else 0,
             mark.get("thread", 0) if isinstance(mark, dict) else 0, "ok" if ok else "failed", now)
        if ok:
            opened += 1
        else:
            skipped += 1
    return {"reopened": opened, "skipped": skipped}
