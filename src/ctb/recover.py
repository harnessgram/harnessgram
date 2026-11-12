"""Quota recovery: announce a session's exhausted quota once, hand the session to a freed account, and
deliver what was written while it waited as soon as the quota returns.

Run from the dashboard tick (`harnessgram dashboard`). Messages written to a limited session stay
queued (see wake.py and the inbox nudge, which both hold while the limit lasts). When the window resets, an
idle session with an empty input box gets the same short "Telegram" nudge as any session without a
waiter, and the UserPromptSubmit hook then hands over the framed messages; nothing a person typed
in Telegram is ever typed into the terminal as the owner's own words, and no text here approves anything.
Never raises: a failed tick is simply retried by the next one.
"""
from __future__ import annotations

import fcntl
import json
import re
import time
from typing import Callable, Optional

from . import accounts, alerts, usage
from .config import home_chat, private_dir, state_dir, write_private
from .i18n import t
from .telegram import Bot, TelegramError
from .topics import TopicMap, chat_of

STATE = "limit-state.json"
GRACE = 60  # seconds after the reset before the queue is delivered
NUDGE_RETRY = 300  # seconds: a nudge that did not get the messages delivered is repeated at most this often
GIVE_UP = 1800  # seconds after the reset: a later queue is an ordinary busy queue, not "quota returned"
NUDGE_TEXT = "📩 Telegram"
RESUME_TEXT = "▶️ Harnessgram: the quota is back. Continue the work the limit interrupted."


def _load(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, state: dict) -> None:
    newest = sorted(state.items(), key=lambda kv: (kv[1] or {}).get("updated", 0), reverse=True)[:500]
    write_private(path, json.dumps(dict(newest), ensure_ascii=False))


def _cleared(session: str, error_id: str) -> bool:
    """The session was moved off the account that hit this limit (the move wrote this marker)."""
    try:
        return (state_dir() / "limit-cleared" / session).read_text(encoding="utf-8").strip() == error_id
    except OSError:
        return False


def _say(cfg, bot, thread: int, text: str, session: str, alert: bool = False) -> None:
    try:
        bot.call("sendMessage", {"chat_id": chat_of(cfg, session), "message_thread_id": thread, "text": text})
    except TelegramError:
        return
    if alert:
        alerts.alert(cfg, text, session, bot=bot, kind="system")


def _deliver(session: str, queued: bool, line: str = NUDGE_TEXT) -> str:
    """Wake the session for its saved messages: "nudged", "no-pane" or "later". The conditions are those
    of the inbox's own nudge: no live waiter (it delivers by itself), not busy, an idle prompt, empty."""
    from .autopilot import at_prompt, type_into
    from . import compact
    from .inbox import _empty_prompt, _nudge_guard, _waiter_alive, clear_nudge_typed, main_working, mark_nudge_typed, pane_for_session
    pane = pane_for_session(session)
    if not pane:
        return "no-pane"
    # A live waiter delivers queued messages by itself; with nothing queued it never wakes, so it must not
    # hold back the line that tells an interrupted session to go on.
    if (queued and _waiter_alive(session)) or main_working(session) or compact.command_pending(session):
        return "later"
    try:
        with _nudge_guard(session):  # the lock every nudge and slash line takes: one writer into the input box
            if not (at_prompt(pane) and _empty_prompt(pane)) or compact.command_pending(session):
                return "later"
            if line == NUDGE_TEXT:
                mark_nudge_typed(session)
            type_into(pane, line)
            if line == NUDGE_TEXT:
                clear_nudge_typed(session)
    except Exception:
        return "later"
    return "nudged"


def limit_check(cfg, bot=None, now: Optional[float] = None, spawn_fn: Optional[Callable] = None,
                inbox=None) -> None:
    try:
        _limit_check(cfg, bot, now, spawn_fn, inbox)
    except Exception:
        pass


def _limit_check(cfg, bot, now, spawn_fn, inbox) -> None:
    from .inbox import default_inbox
    now_ts = time.time() if now is None else now
    base = private_dir(state_dir())
    with open(base / (STATE + ".lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # another tick is already on it
        bot = bot or Bot.from_config(cfg)
        inbox = inbox or default_inbox()
        ui_lang = getattr(cfg, "ui_language", "en")
        topics = TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg).all_places()
        path = base / STATE
        state = _load(path)
        for session, entry in topics.items():
            thread = entry.get("thread") if isinstance(entry, dict) else None
            if type(thread) is not int:
                continue
            try:
                _one(cfg, bot, inbox, session, thread, state, path, now_ts, spawn_fn, ui_lang)
            except Exception:
                continue


def _just_moved(session: str) -> bool:
    """The limit notice has just moved this session (or is moving it): a second, "freed quota"
    move at the same moment raced the first and both failed."""
    from .inbox import recently_moving
    mark = state_dir() / "limit-offered" / (re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_")
    try:
        offered = time.time() - mark.stat().st_mtime < 300
    except OSError:
        offered = False
    return offered or recently_moving(session, 300)


def _same_hung_process(session: str, wait: dict, busy_ok: bool = False) -> Optional[bool]:
    """Whether the window still runs the very process that ignored /exit when the move was deferred: True,
    False (it is not: drop the retry), None (cannot be told yet). The saved pid and start time must be the
    one and only Claude leading the window's terminal, and no live record may name that pid: a record
    means a live session, and after /clear or /resume in the same process it names a different one.
    `busy_ok`: an overdue move; the process is not hung but busy, so a record naming this very session is fine."""
    from .inbox import _live_records_strict, _pane_claude_targets, pane_for_session, pane_runs_session
    try:
        pid, started = int(wait.get("pid") or 0), wait.get("started")
        pane = pane_for_session(session)
        if not pid or not started or not pane or not pane_runs_session(pane, session, known_pid=pid):
            return False
        if _pane_claude_targets(pane) != [(pid, started)]:
            return False
        records = _live_records_strict(pid)
        if records is None:
            return None
        return not any(int(r.get("pid") or 0) == pid and not (busy_ok and r.get("sessionId") == session)
                       for r in records)
    except Exception:
        return None


def _retry_deferred(cfg, bot, session, thread, now_ts, spawn_fn, old, error) -> bool:
    """A limit move that waited for background work: once the session is quiet (idle, no agent writing,
    no shell job) try it again, at most once a minute and without another message (the owner was told
    once). True while such a move is pending. It is dropped when the limit it belongs to is gone, or the
    session no longer runs on the account that hit it (no ping-pong between accounts)."""
    wait = accounts.deferred_move(session, now_ts)
    if not wait:
        return False
    if (error and wait.get("limit_id") and wait["limit_id"] != error["id"]) or (
            wait.get("origin") and accounts.origin_account(session) != wait["origin"]):
        accounts.clear_deferred(session)
        return False
    if old.get("moved") and float(old.get("moved_at") or 0) > float(wait.get("tried") or 0):
        return True  # a move started from here is still on its way
    if _just_moved(session):
        return True
    if now_ts - float(wait.get("tried") or 0) < accounts.DEFER_RETRY:
        return True
    from .inbox import BG_WAIT_MAX
    overdue = now_ts - float(wait.get("since") or now_ts) >= BG_WAIT_MAX
    if not accounts.restart_is_safe(session, now_ts):
        # A job that runs for hours keeps the session "not quiet" for good: once the wait is over the move
        # goes ahead and stops it (see _account_move). No record at all (its exit hooks removed it, the process stayed) used to stall here for good.
        # Try again only while agents are quiet AND the window still runs the same process that ignored
        # /exit. Anything else (a /clear, a new process) drops the retry for good: typing /exit then would
        # close the owner's current conversation. The move proves the process is hung before ending it.
        if not overdue and not accounts.restart_is_safe_or_gone(session, now_ts):
            return True
        same = _same_hung_process(session, wait, busy_ok=overdue)
        if same is None:
            return True
        if not same:
            from .inbox import poll_log
            poll_log(f"deferred move of {session[:8]} dropped: its window no longer runs the process that ignored /exit")
            accounts.clear_deferred(session)
            old["moved"], old["moved_at"] = "dropped", now_ts  # no "account freed up" move in its place
            return False
    fresh = accounts.fallback_plan(session, now_ts)["fresh"]
    target = wait["target"] if wait["target"] in fresh else (fresh[0] if fresh else None)
    if target is None:
        return True
    if spawn_fn is None:
        from .hooks import spawn as spawn_fn
    accounts.defer_move(session, target, now_ts)  # stamps this attempt
    spawn_fn(["account", "auto-move", f"--session={session}", f"--thread={thread}", f"--target={target}"])
    return True


def _finish_resume(session: str) -> None:
    """A limit move whose process died or timed out before the resumed session was told to carry on: one look per tick, at most once overall (the marker and a lock guard it)."""
    from .inbox import resume_after_move, resume_pending
    if resume_pending(session):
        resume_after_move(session, wait=0)


def _one(cfg, bot, inbox, session, thread, state, path, now_ts, spawn_fn, ui_lang) -> None:
    try:
        _finish_resume(session)
    except Exception:
        pass
    error = usage.limit_of(session)
    old = state.get(session) or {}
    if old and not error and now_ts < old.get("reset", 0) and _cleared(session, old.get("id", "")):
        # Moved to an account with room: the session is served there (its waiter no longer sees the
        # error), so there is nothing left to announce or deliver for the old limit.
        state.pop(session, None)
        _save(path, state)
        return
    if error and old.get("id") != error["id"] and not _just_moved(session):
        # While the limit notice moves the session, "quota ran out, messages saved" is untrue by the time
        # it is read; a move that fails leaves the limit, and the next tick announces it.
        _say(cfg, bot, thread, t("limit_announce", lang=ui_lang, clock=error["clock"]), session)
        old = dict(error, updated=now_ts)
        state[session] = old
        _save(path, state)
    # Before any move to another account: the owner spent the reset to go on with this one.
    if (old and not error and now_ts < old.get("reset", 0) and not old.get("moved")
            and usage.reset_early(session, old.get("updated", now_ts))):
        old["reset"] = now_ts - GRACE  # the owner spent a saved reset: the quota is back now, not in days
        old["updated"] = now_ts
        state[session] = old
        _save(path, state)
    if old and now_ts < old.get("reset", 0) and accounts.auto_enabled() and _retry_deferred(
            cfg, bot, session, thread, now_ts, spawn_fn, old, error):
        return
    if old.get("moved") == "dropped":
        _save(path, state)
    if (old and not old.get("moved") and now_ts < old.get("reset", 0) and accounts.auto_enabled()
            and not _just_moved(session)):
        fresh = accounts.fallback_plan(session, now_ts)["fresh"]  # another account freed up while it waits
        if fresh:
            if spawn_fn is None:
                from .hooks import spawn as spawn_fn
            spawn_fn(["account", "auto-move", f"--session={session}", f"--thread={thread}", f"--target={fresh[0]}"])
            _say(cfg, bot, thread, t("limit_freed_account", lang=ui_lang, target=accounts.label(fresh[0])), session)
            old["moved"] = fresh[0]
            old["moved_at"] = now_ts
            _save(path, state)
            return
    if not old or now_ts < old.get("reset", float("inf")) + GRACE:
        return
    queued = inbox.pending(session)
    if not queued and not old.get("resumed") and now_ts <= old.get("reset", 0) + GIVE_UP \
            and usage.interrupted_by_limit(session):
        # Nothing was written meanwhile, but the limit stopped the session in the middle: it goes on by itself.
        outcome = _deliver(session, False, RESUME_TEXT)
        if outcome == "later":
            return
        if outcome == "nudged":
            old["resumed"] = True
            state[session] = old
            _save(path, state)
            _say(cfg, bot, thread, t("limit_back_resumed", lang=ui_lang), session, alert=True)
            return
    if not queued or now_ts > old.get("reset", 0) + GIVE_UP:
        # Done with this limit: nothing waits, or a later queue is an ordinary busy queue, not "quota returned".
        state.pop(session, None)
        _save(path, state)
        return
    if now_ts - old.get("nudged_at", 0) < NUDGE_RETRY:
        return
    outcome = _deliver(session, queued)
    if outcome == "later":
        return  # not at an idle prompt yet: the next tick tries again
    if outcome == "nudged":
        old["nudged_at"] = now_ts
        state[session] = old
        _save(path, state)
    key, flag = ("limit_back_no_pane", "no_pane") if outcome == "no-pane" else ("limit_back_delivered", "back")
    if old.get(flag):
        return
    old[flag] = True
    old["updated"] = now_ts
    _say(cfg, bot, thread, t(key, lang=ui_lang), session, alert=True)
    _save(path, state)
