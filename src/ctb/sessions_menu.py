"""The session manager: one message, edited in place, to look over the bridge's session topics and
tidy the old ones from the phone.

Every session topic is listed newest activity first, 8 to a page, with a state icon (working, idle but
the process is alive, ended) and the time since its last activity. A tap opens the session: open the
topic, end the session (process and topic), close only the topic, reopen it, or delete it for good
(ended sessions only, two confirmations). One button closes every ended session that has been quiet for
two days. Closed topics leave the list and wait behind "show closed".

Callback data is `sm:<op>[:<session id prefix>]:<page><o|c>`: short, owner only, and every action
resolves the prefix again and re-checks the session's state, so an old page never acts on the wrong
session or on one that came back to life. Closing a topic is remembered in closed-topics.json (the
Bot API cannot list closed topics).

The manager's own replies answer the owner's taps, so they are essential traffic like the other menus;
the bulk tidy paces its calls and stops at the first 429.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import subprocess
import time
from typing import List, NamedTuple, Optional, Tuple

from . import accounts, flood, identity, lang as L, status
from .config import Config, home_chat, private_dir, state_dir, write_private
from .i18n import get_lang, t
from .projects import owner_chats
from .telegram import TelegramError
from .topics import TopicMap, session_state_path

PAGE = 8
TIDY_AFTER = 2 * 86400  # an ended session must have been quiet this long to be tidied
TIDY_PACE = 0.4  # seconds between the closes of one tidy
CLOSED_FILE = "closed-topics.json"
AUTO_DELETE_AFTER = 7 * 86400  # a topic closed this long, still ended and untouched, is deleted
DELETE_PER_ROUND = 3  # at most this many automatic deletions in one heartbeat round
CLOSE_WAIT_TRIES = 10  # the ending process may take a few seconds to leave (one try per second)
REOPENED_GRACE = 30  # a transcript written this long after the close means the topic was used again
ICONS = {"working": "🟢", "idle": "💤", "ended": "⚫"}
_SID = re.compile(r"[A-Za-z0-9_-]{1,12}")
_PF = re.compile(r"(\d{1,3})([oc])")


class Entry(NamedTuple):
    session: str
    name: str
    thread: int
    age: Optional[float]  # seconds since the last activity; None when no trace of it is left
    state: str  # working | idle | ended
    closed: bool
    chat: Optional[int] = None  # the project group the topic is in; None = the home chat


def _chat(cfg: Config, e: Entry) -> int:
    return home_chat(cfg) if e.chat is None else e.chat


# --- what is known about each session ------------------------------------------------------

def _closed_path():
    return private_dir(state_dir()) / CLOSED_FILE


def closed_marks() -> dict:
    try:
        data = json.loads(_closed_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def _marks_lock():
    with open(str(_closed_path()) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write_marks(data: dict) -> None:
    write_private(_closed_path(), json.dumps(dict(list(data.items())[-500:]), ensure_ascii=False))


def _set_mark(session: str, thread: Optional[int], auto: bool = False, chat: Optional[int] = None) -> None:
    """Remember a closed topic (thread given) or forget the mark (thread None). Only a close made by the
    bridge itself when the session ended is `auto`: the one kind that is later deleted."""
    with _marks_lock():
        data = closed_marks()
        if thread is None:
            if data.pop(session, None) is None:
                return
        else:
            data[session] = {"thread": thread, "at": time.time(), "auto": auto, "chat": chat}
        _write_marks(data)


def _valid_at(mark: dict) -> Optional[float]:
    at = mark.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at <= 0:
        return None
    return float(at)


def _last_activity(session: str) -> Optional[float]:
    """The newest of the transcript's and the session state file's modification times."""
    stamps = []
    path = status.transcript_of(session)
    for p in (path, str(session_state_path(session))):
        try:
            if p:
                stamps.append(os.path.getmtime(p))
        except OSError:
            continue
    return max(stamps) if stamps else None


def collect(topics: TopicMap, now: Optional[float] = None) -> List[Entry]:
    """Every session topic, newest activity first (sessions with no known activity last)."""
    from . import inbox
    now = time.time() if now is None else now
    live = {r.get("sessionId") for r in accounts.live_session_records()}
    marks = closed_marks()
    out = []
    for session, info in topics.all_places().items():
        thread = info.get("thread")
        if not isinstance(thread, int) or not _SID.fullmatch(session[:12] or "-"):
            continue
        stamp = _last_activity(session)
        age = max(0.0, now - stamp) if stamp is not None else None
        mark = marks.get(session)
        closed = bool(isinstance(mark, dict) and mark.get("thread") == thread
                      and (stamp is None or stamp <= (_valid_at(mark) or 0.0) + REOPENED_GRACE))
        if session in live or inbox.recently_moving(session):
            busy = session_state(session) == "busy"
            state = "working" if age is not None and (age < status.WORKING or (busy and age < status.WAITING)) else "idle"
        else:
            state = "ended"
        name = " ".join(str(info.get("name") or session[:8]).split())
        out.append(Entry(session, name, thread, age, state, closed, None if info.get("home", True) else info["chat"]))
    return sorted(out, key=lambda e: (e.age is None, e.age or 0))


def session_state(session: str) -> str:
    from .topics import session_state as _state
    return _state(session).get("state") or ""


def _find(entries: List[Entry], prefix: str) -> Optional[Entry]:
    """The one session this id prefix names now; None when it is gone or the prefix is ambiguous. An id
    of 12 characters or fewer is never a prefix of another: only an exact match counts."""
    hits = [e for e in entries if e.session == prefix or (len(prefix) >= 12 and e.session.startswith(prefix))]
    return hits[0] if len(hits) == 1 else None


def tidy_candidates(entries: List[Entry]) -> List[Entry]:
    """Ended, quiet for 2+ days and still open; the strict check (no process on any account, no move
    running, no limit wait) is asked again for each."""
    from . import inbox
    return [e for e in entries if not e.closed and e.state == "ended" and e.age is not None
            and e.age >= TIDY_AFTER and inbox._closed(e.session)]


# --- the views -----------------------------------------------------------------------------

def _lang(cfg: Config) -> str:
    return getattr(cfg, "ui_language", None) or get_lang()


def _mark(lang: str) -> str:
    return L.RLM if lang == "he" else ""


def _name(e: Entry, lang: str, limit: int = 120) -> str:
    return identity.isolate(e.name[:limit], lang)


def short_age(age: Optional[float], lang: str) -> str:
    if age is None:
        return t("sm_age_unknown", lang=lang)
    if age < 3600:
        return t("sm_age_m", lang=lang, n=max(1, int(age // 60)))
    if age < 86400:
        return t("sm_age_h", lang=lang, n=int(age // 3600))
    return t("sm_age_d", lang=lang, n=int(age // 86400))


def long_age(age: Optional[float], lang: str) -> str:
    if age is None:
        return t("sm_age_unknown", lang=lang)
    if age < 3600:
        return t("minutes_ago", lang=lang, n=max(1, int(age // 60)))
    if age < 86400:
        return t("hours_ago", lang=lang, n=int(age // 3600))
    return t("sm_days_ago", lang=lang, n=int(age // 86400))


def _pf(page: int, flt: str) -> str:
    return f"{page}{flt}"


def _cb(op: str, *parts) -> str:
    return ":".join(["sm", op, *map(str, parts)])


def _dismiss(lang: str) -> dict:
    from . import inbox
    return inbox.dismiss_button(lang)


def list_view(cfg: Config, topics: TopicMap, page: int = 0, flt: str = "o", note: str = "",
              entries: Optional[List[Entry]] = None) -> Tuple[str, dict]:
    lang, mark = _lang(cfg), _mark(_lang(cfg))
    entries = collect(topics) if entries is None else entries
    flt = "c" if flt == "c" else "o"
    open_, closed = [e for e in entries if not e.closed], [e for e in entries if e.closed]
    shown = closed if flt == "c" else open_
    pages = max(1, -(-len(shown) // PAGE))
    page = min(max(page, 0), pages - 1)
    lines = []
    if note:
        lines += [mark + note, ""]
    if flt == "c":
        lines.append(mark + t("sm_title_closed", lang=lang, n=len(closed)))
    else:
        count = lambda s: sum(1 for e in open_ if e.state == s)
        lines += [mark + t("sm_title", lang=lang),
                  mark + t("sm_counts", lang=lang, working=count("working"), idle=count("idle"), ended=count("ended"))]
    if not shown:
        lines.append(mark + t("sm_empty", lang=lang))
    elif pages > 1:
        lines.append(mark + t("sm_page", lang=lang, page=page + 1, pages=pages))
    rows = []
    for e in shown[page * PAGE:(page + 1) * PAGE]:
        label = f"{ICONS[e.state]} {_name(e, lang, 30)} · {short_age(e.age, lang)}"
        row = [{"text": label, "callback_data": _cb("d", e.session[:12], _pf(page, flt))}]
        if flt == "c":
            row.append({"text": "♻️", "callback_data": _cb("o", e.session[:12], _pf(page, flt))})
        rows.append(row)
    nav = []
    if page > 0:
        nav.append({"text": "◀", "callback_data": _cb("l", _pf(page - 1, flt))})
    if page < pages - 1:
        nav.append({"text": "▶", "callback_data": _cb("l", _pf(page + 1, flt))})
    if nav:
        rows.append(nav)
    if flt == "o":
        n = len(tidy_candidates(open_))
        if n:
            rows.append([{"text": t("sm_btn_tidy", lang=lang, n=n), "callback_data": _cb("t", _pf(page, flt))}])
        if closed:
            rows.append([{"text": t("sm_btn_closed", lang=lang, n=len(closed)), "callback_data": _cb("l", "0c")}])
    else:
        rows.append([{"text": t("sm_btn_open", lang=lang), "callback_data": _cb("l", "0o")}])
    rows.append([_dismiss(lang)])
    return "\n".join(lines), {"inline_keyboard": rows}


def detail_view(cfg: Config, e: Entry, pf: str, note: str = "") -> Tuple[str, dict]:
    lang, mark = _lang(cfg), _mark(_lang(cfg))
    sid = e.session[:12]
    lines = [mark + note, ""] if note else []
    lines += [mark + f"{ICONS[e.state]} {_name(e, lang)}",
              mark + t("sm_state_" + e.state, lang=lang),
              mark + t("sm_last_activity", lang=lang, ago=long_age(e.age, lang))]
    if e.closed:
        lines.append(mark + t("sm_topic_closed", lang=lang))
    chat = _chat(cfg, e)
    rows = []
    if str(chat).startswith("-100"):
        rows.append([{"text": t("sm_btn_topic", lang=lang),
                      "url": f"https://t.me/c/{str(chat).removeprefix('-100')}/{e.thread}"}])
    if e.state != "ended":
        rows.append([{"text": t("sm_btn_end", lang=lang), "callback_data": _cb("e", sid, pf)}])
    if e.closed:
        rows.append([{"text": t("sm_btn_reopen", lang=lang), "callback_data": _cb("o", sid, pf)}])
    else:
        rows.append([{"text": t("sm_btn_close", lang=lang), "callback_data": _cb("c", sid, pf)}])
    if e.state == "ended":
        rows.append([{"text": t("sm_btn_delete", lang=lang), "callback_data": _cb("x1", sid, pf)}])
    rows.append([{"text": t("btn_back", lang=lang), "callback_data": _cb("l", pf)}])
    return "\n".join(lines), {"inline_keyboard": rows}


def _confirm(cfg: Config, text: str, yes: str, yes_cb: str, back_cb: str) -> Tuple[str, dict]:
    lang = _lang(cfg)
    return _mark(lang) + text, {"inline_keyboard": [[{"text": yes, "callback_data": yes_cb},
                                                      {"text": t("close_btn_cancel", lang=lang), "callback_data": back_cb}], [_dismiss(lang)]]}


# --- the entry points ----------------------------------------------------------------------

def manage_button(lang: Optional[str] = None) -> dict:
    return {"text": t("sm_btn_manage", lang=lang), "callback_data": _cb("l", "0o")}


def _show(bot, edit: dict, view: Tuple[str, dict]) -> None:
    try:
        bot.call("editMessageText", dict(edit, text=view[0] or "…", reply_markup=view[1]))
    except TelegramError:
        pass  # not modified, or the message is gone


def _toast(bot, q: dict, text: str) -> None:
    from . import inbox
    inbox._answer_button(bot, q, text)


def _gone(exc: TelegramError) -> bool:
    text = str(exc).lower()
    return "topic_id_invalid" in text or "thread not found" in text or "topic not found" in text


def _already(exc: TelegramError) -> bool:
    return "not_modified" in str(exc).lower().replace(" ", "_")


def _close_topic(cfg: Config, bot, topics: TopicMap, e: Entry, auto: bool = False) -> str:
    """Close one topic and remember it. 'ok', 'gone' (the topic no longer exists), 'flood' or 'failed'."""
    try:
        bot.call("closeForumTopic", {"chat_id": _chat(cfg, e), "message_thread_id": e.thread})
    except TelegramError as exc:
        if _gone(exc):
            topics.forget(e.session, chat=e.chat)
            _set_mark(e.session, None)
            return "gone"
        if exc.code == 429:
            return "flood"
        if not _already(exc):
            return "failed"
    _set_mark(e.session, e.thread, auto=auto, chat=_chat(cfg, e))
    return "ok"


def _delete_call(cfg: Config, bot, e: Entry) -> str:
    """'ok' (a topic already gone counts), 'flood' or 'failed'."""
    try:
        bot.call("deleteForumTopic", {"chat_id": _chat(cfg, e), "message_thread_id": e.thread})
    except TelegramError as exc:
        if exc.code == 429:
            return "flood"
        if not _gone(exc):
            return "failed"
    return "ok"


def _delete_topic(cfg: Config, bot, topics: TopicMap, e: Entry, at: Optional[float] = None) -> str:
    """Delete one topic and forget it: 'ok', 'flood', 'failed', or 'changed' (only with `at`, the close
    time the caller saw: the record is read again under the lock and nothing is deleted when the thread
    or the time differs, because the topic was reopened or closed again meanwhile)."""
    if at is None:
        result = _delete_call(cfg, bot, e)
        if result == "ok":
            topics.forget(e.session, chat=e.chat)
            _set_mark(e.session, None)
        return result
    with _marks_lock():
        data = closed_marks()
        mark = data.get(e.session)
        if not (isinstance(mark, dict) and mark.get("thread") == e.thread and mark.get("at") == at):
            return "changed"
        result = _delete_call(cfg, bot, e)
        if result == "ok":
            data.pop(e.session, None)
            _write_marks(data)
    if result == "ok":
        topics.forget(e.session, chat=e.chat)
    return result


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _flooded(cfg: Config) -> bool:
    return not flood.is_open(home_chat(cfg))


def _is_claude(pid: int) -> bool:
    """The strict test the terminal moves use: Claude's own program and command line, not any process
    with "claude" in its name."""
    from . import inbox
    try:
        return inbox._looks_like_claude(pid, inbox._ps_field("command=", pid))
    except (OSError, subprocess.SubprocessError):
        return False


def _end_session(cfg: Config, bot, topics: TopicMap, e: Entry, edit: dict, pf: str) -> None:
    """Off the poll loop: stop the process, wait for it to go, then close the topic."""
    from . import inbox
    lang = _lang(cfg)
    _show(bot, edit, (_mark(lang) + t("sm_ending_text", lang=lang, name=_name(e, lang, 60)), {"inline_keyboard": []}))
    pane, pid = inbox.stop_pane(e.session), inbox.session_pid(e.session)  # a stale %N id is not a pane
    if pane:
        if not inbox._exit_pane(e.session, pane):  # a draft or an unreadable box: /exit was not typed
            return _show(bot, edit, detail_view(cfg, e, pf, t("sm_end_failed", lang=lang, name=_name(e, lang, 60))))
    elif pid and _is_claude(pid):
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    else:
        note = t("sm_end_no_terminal", lang=lang, name=_name(e, lang, 60))
        return _show(bot, edit, detail_view(cfg, e, pf, note))
    for _ in range(40):
        if inbox.session_pid(e.session) is None:
            break
        _pause(0.5)
    page, flt = _split_pf(pf)
    if inbox.session_pid(e.session) is not None:
        note = t("sm_end_failed", lang=lang, name=_name(e, lang, 60))
        return _show(bot, edit, detail_view(cfg, e, pf, note))
    done = _close_topic(cfg, bot, topics, e) in ("ok", "gone")
    note = t("sm_end_done" if done else "sm_end_closed_failed", lang=lang, name=_name(e, lang, 60))
    _show(bot, edit, list_view(cfg, topics, page, flt, note))


def _tidy(cfg: Config, bot, topics: TopicMap, edit: dict, pf: str) -> None:
    lang = _lang(cfg)
    page, flt = _split_pf(pf)
    cands = tidy_candidates(collect(topics))
    done = 0
    for i, e in enumerate(cands):
        if i:
            _pause(TIDY_PACE)
        result = _close_topic(cfg, bot, topics, e)
        if result == "flood":
            break
        done += result in ("ok", "gone")
    note = (t("sm_tidy_done", lang=lang, n=done) if done == len(cands)
            else t("sm_tidy_partial", lang=lang, n=done, total=len(cands)))
    _show(bot, edit, list_view(cfg, topics, 0, "o", note))


# --- automatic close and delete -------------------------------------------------------------

def _entry(topics: TopicMap, session: str) -> Optional[Entry]:
    return next((e for e in collect(topics) if e.session == session and e.thread > 1), None)


def close_after_end(cfg: Config, bot, topics: TopicMap, session: str) -> bool:
    """A session that really ended: close its topic. A session still running (or about to be revived or
    moved) is left open; the process may need a few seconds to leave, so the check is repeated."""
    from . import inbox, scheduled
    if scheduled.task_of(session):
        return False  # a scheduled task's topic is shared by all its runs: the bridge never closes it
    for _ in range(CLOSE_WAIT_TRIES):
        e = _entry(topics, session)
        if e is None or e.closed:
            return False
        if e.state == "ended" and inbox._closed(session):
            return _close_topic(cfg, bot, topics, e, auto=True) == "ok"
        _pause(1.0)
    return False


def reopen_for_message(cfg: Config, bot, session: str) -> bool:
    """The owner did something in a closed session's topic: forget the close (the seven days start over,
    whatever Telegram answers) and open the topic again, trying twice. True when it is open."""
    mark = closed_marks().get(session)
    if not isinstance(mark, dict) or not isinstance(mark.get("thread"), int):
        return False
    _set_mark(session, None)
    for _ in range(2):
        try:
            bot.call("reopenForumTopic", {"chat_id": mark["chat"] if mark.get("chat") else home_chat(cfg),
                                                  "message_thread_id": mark["thread"]})
            return True
        except TelegramError as exc:
            if _already(exc) or _gone(exc):
                return True
    return False


def note_input(cfg: Config, bot, update: dict) -> None:
    """Any input of the owner in a closed topic (message, edit, command, sticker, button tap) reopens it
    and clears the close; Telegram's own "topic reopened" notice only clears it."""
    try:
        q = update.get("callback_query")
        m = (q or {}).get("message") if q else (update.get("message") or update.get("edited_message"))
        m = m or {}
        sender = (q or m).get("from") or {}
        thread = m.get("message_thread_id")
        chat = (m.get("chat") or {}).get("id")
        if chat not in owner_chats(cfg) or not isinstance(thread, int):
            return
        reopened = "forum_topic_reopened" in m
        if not reopened and (sender.get("id") != cfg.owner_chat_id or sender.get("is_bot")):
            return
        for session, mark in closed_marks().items():
            # a mark without a chat is the home group's
            if (isinstance(mark, dict) and mark.get("thread") == thread
                    and (mark.get("chat") or home_chat(cfg)) == chat):
                if reopened:
                    _set_mark(session, None)
                else:
                    reopen_for_message(cfg, bot, session)
    except Exception:
        pass


def auto_tidy(cfg: Config, bot, topics: TopicMap, now: Optional[float] = None, skip=()) -> int:
    """One heartbeat round. A closed topic whose session is running again is opened again; an auto-closed
    one (closed when its session ended) that has been closed for AUTO_DELETE_AFTER, whose session is still
    ended and untouched, is deleted. At most DELETE_PER_ROUND attempts per round, and the round stops at
    the first failure. `skip` are sessions that must not be touched (still to be revived). Returns the
    number of topics deleted."""
    from . import inbox
    now = time.time() if now is None else now
    marks = closed_marks()
    if not marks or _flooded(cfg):
        return 0
    entries = {e.session: e for e in collect(topics, now)}
    deleted = attempts = 0
    for session, mark in list(marks.items()):
        try:
            e = entries.get(session)
            at = _valid_at(mark) if isinstance(mark, dict) else None
            if (at is None or e is None or session in skip or e.thread != mark.get("thread") or e.thread <= 1
                    or mark.get("chat") != home_chat(cfg)):
                continue
            if e.state != "ended":
                stamp = _last_activity(session)
                if stamp is not None and stamp > at + REOPENED_GRACE:  # it came back by itself
                    reopen_for_message(cfg, bot, session)
                continue
            if mark.get("auto") is not True or not e.closed or now - at < AUTO_DELETE_AFTER \
                    or not inbox._closed(session):
                continue
            if attempts >= DELETE_PER_ROUND:
                break
            if attempts:
                _pause(TIDY_PACE)
            attempts += 1
            result = _delete_topic(cfg, bot, topics, e, at=mark.get("at"))
            if result == "ok":
                deleted += 1
            elif result != "changed":
                break
        except Exception:
            continue
    return deleted


def _split_pf(pf: str) -> Tuple[int, str]:
    m = _PF.fullmatch(pf or "")
    return (int(m.group(1)), m.group(2)) if m else (0, "o")


def button(q: dict, cfg: Config, bot, topics: TopicMap) -> str:
    """Every `sm:` press. The caller has already checked that the owner pressed it in the home chat."""
    from . import inbox
    lang = _lang(cfg)
    m = q.get("message") or {}
    edit = {"chat_id": home_chat(cfg), "message_id": m.get("message_id")}
    parts = (q.get("data") or "").split(":")
    op = parts[1] if len(parts) > 1 else ""
    pf = parts[-1] if len(parts) > 2 else ""
    sid = parts[2] if op not in ("l", "t", "ty") and len(parts) == 4 else ""
    n_shown = parts[2] if op == "ty" and len(parts) == 4 else ""
    shape = {"l": 3, "t": 3, "ty": 4}.get(op, 4)
    if (len(parts) != shape or op not in ("l", "d", "e", "ey", "c", "o", "x1", "x2", "xy", "t", "ty")
            or not _PF.fullmatch(pf) or (op not in ("l", "t", "ty") and not _SID.fullmatch(sid))
            or (op == "ty" and not n_shown.isdigit())):
        _toast(bot, q, t("toast_not_available", lang=lang))
        return "ignored"
    page, flt = _split_pf(pf)
    entries = collect(topics)

    if op == "l":
        _toast(bot, q, "")
        _show(bot, edit, list_view(cfg, topics, page, flt, entries=entries))
        return "sessions"

    if op in ("t", "ty"):
        cands = tidy_candidates(entries)
        if op == "ty" and _flooded(cfg):
            _toast(bot, q, t("sm_flood", lang=lang))
            return "busy"
        if not cands:
            _toast(bot, q, t("sm_tidy_none", lang=lang))
            _show(bot, edit, list_view(cfg, topics, page, flt, entries=entries))
            return "sessions"
        if op == "t" or int(n_shown) != len(cands):  # a stale count never closes a different set
            _toast(bot, q, t("sm_tidy_changed", lang=lang) if op == "ty" else "")
            names = "\n".join("• " + _name(e, lang, 40) for e in cands[:10])
            more = t("sm_tidy_more", lang=lang, n=len(cands) - 10) if len(cands) > 10 else ""
            _show(bot, edit, _confirm(cfg, t("sm_tidy_ask", lang=lang, n=len(cands)) + "\n" + names + more,
                                      t("sm_tidy_yes", lang=lang, n=len(cands)), _cb("ty", len(cands), pf), _cb("l", pf)))
            return "sessions"
        if not inbox._terminal_background("__sessions__", lambda: _tidy(cfg, bot, topics, edit, pf)):
            _toast(bot, q, t("toast_session_busy_retry", lang=lang))
            return "busy"
        _toast(bot, q, t("sm_tidy_working", lang=lang))
        return "sessions"

    e = _find(entries, sid)
    if e is None:  # the page is older than the list
        _toast(bot, q, t("sm_stale", lang=lang))
        _show(bot, edit, list_view(cfg, topics, page, flt, entries=entries))
        return "stale"
    nm = _name(e, lang, 60)

    if op == "d":
        _toast(bot, q, "")
        _show(bot, edit, detail_view(cfg, e, pf))
        return "sessions"

    if op in ("e", "ey"):
        if e.state == "ended":
            _toast(bot, q, t("sm_already_ended", lang=lang))
            _show(bot, edit, detail_view(cfg, e, pf))
            return "sessions"
        if op == "e":
            _toast(bot, q, "")
            _show(bot, edit, _confirm(cfg, t("sm_end_ask", lang=lang, name=nm), t("sm_end_yes", lang=lang),
                                      _cb("ey", sid, pf), _cb("d", sid, pf)))
            return "sessions"
        if not inbox._terminal_background(e.session, lambda: _end_session(cfg, bot, topics, e, edit, pf)):
            _toast(bot, q, t("toast_session_busy_retry", lang=lang))
            return "busy"
        _toast(bot, q, t("sm_ending", lang=lang))
        return "sessions"

    if op == "c":
        result = _close_topic(cfg, bot, topics, e)
        _toast(bot, q, "")
        key = {"ok": "sm_close_done", "gone": "sm_gone"}.get(result, "sm_close_failed")
        _show(bot, edit, list_view(cfg, topics, page, flt, t(key, lang=lang, name=nm)))
        return "sessions"

    if op == "o":
        try:
            bot.call("reopenForumTopic", {"chat_id": _chat(cfg, e), "message_thread_id": e.thread})
            ok = True
        except TelegramError as exc:
            ok = _already(exc)
            if _gone(exc):
                topics.forget(e.session, chat=e.chat)
        if ok:
            _set_mark(e.session, None)
        _toast(bot, q, "")
        _show(bot, edit, list_view(cfg, topics, page, flt, t("sm_reopen_done" if ok else "sm_reopen_failed",
                                                              lang=lang, name=nm)))
        return "sessions"

    # delete: only an ended session, asked twice, and the state is read again at every step
    if not inbox._closed(e.session) or e.state != "ended":
        _toast(bot, q, t("sm_delete_live", lang=lang))
        _show(bot, edit, detail_view(cfg, e, pf))
        return "sessions"
    if op == "x1":
        _toast(bot, q, "")
        _show(bot, edit, _confirm(cfg, t("sm_delete_ask1", lang=lang, name=nm), t("sm_delete_continue", lang=lang),
                                  _cb("x2", sid, pf), _cb("d", sid, pf)))
        return "sessions"
    if op == "x2":
        _toast(bot, q, "")
        _show(bot, edit, _confirm(cfg, t("sm_delete_ask2", lang=lang, name=nm), t("sm_delete_yes", lang=lang),
                                  _cb("xy", sid, pf), _cb("d", sid, pf)))
        return "sessions"
    ok = _delete_topic(cfg, bot, topics, e) == "ok"
    _toast(bot, q, "")
    _show(bot, edit, list_view(cfg, topics, page, flt, t("sm_delete_done" if ok else "sm_delete_failed",
                                                          lang=lang, name=nm)))
    return "sessions"
