"""Optional agents topic: the background agents of a session report in a topic of their own
("<session topic> · agents") instead of the session's main topic.

Off by default (a settings toggle). One topic per session, created when the first background agent
starts or reports. While agents are running it is open; when the last one finishes it is closed and the
main topic gets one line that links to it. A new agent reopens it. Whatever Telegram refuses (no topic
rights, the topic was deleted) falls back to the main topic: a notice is never lost.

State lives in <state>/agents-topics/<session>.json, apart from topics.json so a message written in the
agents topic is never taken for input to the session."""
from __future__ import annotations

import contextlib
import fcntl
import json
import re
import time
from pathlib import Path
from typing import Optional

from . import agent_task
from . import lang as L
from .config import home_chat, private_dir, setting_on, state_dir, write_private
from .creds import redact
from .telegram import TelegramError
from .topics import NAME_LIMIT, TopicMap, chat_of, session_name

AGENTS_TOPIC = "agents-topic"  # settings marker file: present means the separate topic is on
STALE_AFTER = 10800  # seconds: an agent that never reported its end stops holding the topic open
TYPE_LIMIT = 48  # characters of an agent type shown
FINISHED_KEPT = 100  # ids of finished agents remembered, so a late start for one of them is ignored
MAX_WAIT = 5  # seconds: a 429 shorter than this is waited out once
RETRY_AFTER = 600  # seconds: after Telegram refused to create or reopen the topic, use the main topic
SUPERGROUP_PREFIX = "-100"


def enabled() -> bool:
    return setting_on(AGENTS_TOPIC)


def _path(session: str) -> Path:
    return state_dir() / "agents-topics" / ((re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_") + ".json")


@contextlib.contextmanager
def _locked(session: str):
    """Several detached hook children of one session start and stop agents at the same time."""
    path = _path(session)
    private_dir(path.parent)
    with open(str(path) + ".lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield path


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, st: dict) -> None:
    write_private(path, json.dumps(st, ensure_ascii=False))


def _gone(e: TelegramError) -> bool:
    text = e.description.lower()
    return "not found" in text or "topic_id_invalid" in text or "topic_deleted" in text


def _unchanged(e: TelegramError) -> bool:
    """Closing a closed topic (or opening an open one) is refused as 'not modified': that is the goal reached."""
    return "not_modified" in e.description.lower() or "not modified" in e.description.lower()


def topic_name(cfg, session: str) -> str:
    entry = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg).all_places().get(session) or {}
    base = entry.get("name") or session_name(session) or "Claude"
    suffix = L.strings(cfg.target_language)["agents_topic_suffix"]
    return f"{base[:NAME_LIMIT - len(suffix) - 3]} · {suffix}"[:NAME_LIMIT]


def _forget(st: dict) -> None:
    st.pop("thread", None)
    st.pop("chat", None)
    st["open"] = False
    st["posted"] = False
    st.pop("told", None)


def _saved_chat(cfg, st: dict) -> int:
    """The chat the saved topic lives in: the project group noted in the state, else the home chat."""
    return st["chat"] if isinstance(st.get("chat"), int) else home_chat(cfg)


def _open_topic(cfg, bot, session: str, st: dict, now: float) -> Optional[int]:
    """The agents topic's thread id, created or reopened when needed. None: Telegram refuses, use the main topic.
    It lives in the session's own group; a project group is noted in the state as "chat"."""
    chat = chat_of(cfg, session)
    if isinstance(st.get("thread"), int) and _saved_chat(cfg, st) != chat:
        _forget(st)  # the session moved to another group: a thread id means nothing there
    thread = st.get("thread")
    if isinstance(thread, int) and st.get("open"):
        return thread
    if now < st.get("retry_at", 0):
        return None
    if isinstance(thread, int):
        try:
            bot.call("reopenForumTopic", {"chat_id": chat, "message_thread_id": thread})
        except TelegramError as e:
            if _gone(e):
                _forget(st)  # deleted by hand: a new one is created below
            elif not _unchanged(e):
                st["retry_at"] = now + RETRY_AFTER
                return None
        if isinstance(st.get("thread"), int):
            st["open"] = True
            return st["thread"]
    try:
        topic = bot.call("createForumTopic", {"chat_id": chat, "name": topic_name(cfg, session)})
        st.update(thread=int(topic["message_thread_id"]), open=True, posted=False, told=False)
        if chat != home_chat(cfg):
            st["chat"] = chat
    except (TelegramError, KeyError, TypeError, ValueError):
        st["retry_at"] = now + RETRY_AFTER  # e.g. the bot lacks the manage-topics right
        return None
    return st["thread"]


def _definite(e: TelegramError) -> bool:
    """Telegram answered (an HTTP error code), so the message was not delivered. A timeout or a network error
    may have gone through already: sending it again to the main topic could duplicate it."""
    return isinstance(e.code, int)


def _post(cfg, bot, session: str, st: dict, text: str, main_thread, fallback: bool, now: float) -> None:
    """Send to the agents topic. When Telegram definitely refuses, the text goes to the main topic instead (fallback)."""
    chat = chat_of(cfg, session)
    waited = False
    for attempt in range(3):
        thread = _open_topic(cfg, bot, session, st, now)
        if thread is None:
            break
        try:
            bot.call("sendMessage", {"chat_id": chat, "message_thread_id": thread, "text": text,
                                     "disable_notification": True})
            st["posted"] = True
            return
        except TelegramError as e:
            if not _definite(e):
                return  # unknown whether it arrived: no second copy
            if e.code == 429 and not waited and e.retry_after and e.retry_after <= MAX_WAIT:
                waited = True
                time.sleep(e.retry_after)
            elif attempt >= 1 and (_gone(e) or "topic_closed" in e.description.lower()):
                break
            elif _gone(e):
                _forget(st)
            elif "topic_closed" in e.description.lower():
                st["open"] = False  # closed by hand since: reopen it
            else:
                break
    if fallback and isinstance(main_thread, int):
        bot.call("sendMessage", {"chat_id": chat, "message_thread_id": main_thread, "text": text,
                                 "disable_notification": True})


def _link(chat: int, thread: int) -> str:
    chat = str(chat)
    return f"https://t.me/c/{chat[len(SUPERGROUP_PREFIX):]}/{thread}" if chat.startswith(SUPERGROUP_PREFIX) else ""


def _close(cfg, bot, st: dict, main_thread, chat: Optional[int] = None) -> None:
    """All agents are done: close the topic and say so once in the main topic, with a link.
    `chat` is the main topic's chat; the agents topic is closed where it was saved."""
    chat, thread = home_chat(cfg) if chat is None else chat, st.get("thread")
    if not (isinstance(thread, int) and st.get("open")):
        return
    topic_chat = _saved_chat(cfg, st)
    try:
        bot.call("closeForumTopic", {"chat_id": topic_chat, "message_thread_id": thread})
    except TelegramError as e:
        if _gone(e):
            _forget(st)
        elif not _unchanged(e):
            return  # still open: the next finished agent tries again
    st["open"] = False
    if st.get("posted") and isinstance(main_thread, int) and isinstance(st.get("thread"), int):
        s = L.strings(cfg.target_language)
        mark = L.RLM if L.is_rtl(cfg.target_language) else ""
        link = _link(topic_chat, thread)
        try:
            bot.call("sendMessage", {"chat_id": chat, "message_thread_id": main_thread,
                                     "text": mark + s["agents_topic_done"] + (f"\n{link}" if link else ""),
                                     "disable_notification": True})
            st["posted"] = False
        except TelegramError:
            pass


def _prune(st: dict, now: float) -> dict:
    running = st.get("running")
    st["running"] = {k: at for k, at in (running if isinstance(running, dict) else {}).items()
                     if isinstance(at, (int, float)) and 0 <= now - at < STALE_AFTER}
    done = st.get("finished")
    st["finished"] = {k: at for k, at in sorted((done if isinstance(done, dict) else {}).items(), key=lambda kv: kv[1])[-FINISHED_KEPT:]
                      if isinstance(at, (int, float)) and 0 <= now - at < STALE_AFTER}
    runs = st.get("runs")  # agent id -> the verified call of its last run (None: its end was seen first)
    st["runs"] = {k: v for k, v in (runs if isinstance(runs, dict) else {}).items()
                  if k in st["running"] or k in st["finished"]}
    return st["running"]


def agent_started(cfg, session: str, agent_id: str, agent_type: str, bot, now: Optional[float] = None,
                  found: Optional[dict] = None) -> None:
    """A background agent started: open (create or reopen) the topic and note the agent as running.
    `found`: the verified task and model of the spawning call (agent_task.lookup); the first message names them.
    A repeated start of a running agent posts nothing; a start for an agent that ended earlier is a new run."""
    now = time.time() if now is None else now
    with _locked(session) as path:
        st = _load(path)
        running = _prune(st, now)
        if agent_id and agent_id in running:
            return  # a repeated start
        run = str((found or {}).get("id") or "")  # the verified call that started this run
        runs = st["runs"]
        if agent_id and agent_id in st["finished"]:
            if agent_id in runs and runs[agent_id] is None:  # its end came first: this is the late start hook
                runs[agent_id] = run
                return
            if not run or run == runs.get(agent_id):
                return  # the late start hook of the run that ended (or one that cannot be told apart)
        st["finished"].pop(agent_id, None)
        runs[agent_id] = run
        st["tracked"] = True  # a SubagentStart hook is installed: the end of the last agent is known
        running[agent_id or f"?{now}"] = now
        try:
            if _open_topic(cfg, bot, session, st, now) is not None:
                _post(cfg, bot, session, st, _started_text(cfg, agent_type, found or {}), None, False, now)
        finally:
            _save(path, st)


def _started_text(cfg, agent_type: str, spec: dict) -> str:
    """"Background agent started · type · model" and, on its own line, "↳ task". Hebrew lines follow the
    card's rules: an RLM at the start, a short English name isolated, longer English on a line of its own."""
    from .agents_card import _inline, _label, _long_latin
    s = L.strings(cfg.target_language)
    rtl = L.is_rtl(cfg.target_language)
    mark = L.RLM if rtl else ""
    label = agent_task.clean_label(agent_type, TYPE_LIMIT)
    model = spec.get("model") or ""
    lines = [mark + s["agents_topic_started"]]
    if label and _long_latin(label, rtl):  # longer English never shares a line with Hebrew
        lines += [mark + label] + ([mark + _inline(model, rtl)] if model else [])
    else:
        lines[0] += "".join(" · " + _inline(x, rtl) for x in (label, model) if x)
    task = spec.get("task") or ""
    if task:
        lines.append(mark + "↳ " + _label(task, rtl, _long_latin(task, rtl)))
    return "\n".join(lines)


def agent_finished(cfg, session: str, agent_id: str, text: str, bot, main_thread,
                   now: Optional[float] = None) -> None:
    """A background agent finished: its notice (if any) goes to the agents topic, and the last one closes it."""
    now = time.time() if now is None else now
    with _locked(session) as path:
        st = _load(path)
        running = _prune(st, now)
        if agent_id:
            running.pop(agent_id, None)  # an id never seen removes nothing: it is not another agent's end
            st["finished"][agent_id] = now
            st["runs"].setdefault(agent_id, None)  # None: the end of an agent whose start was not seen (yet)
        elif running:  # no id at all: the oldest running agent is the best guess
            del running[min(running, key=running.get)]
        try:
            if text:
                _post(cfg, bot, session, st, text, main_thread, True, now)
            if not running and st.get("tracked"):  # without start tracking "all finished" cannot be known
                _close(cfg, bot, st, main_thread, chat=chat_of(cfg, session))
        finally:
            _save(path, st)


def session_ended(cfg, session: str, bot) -> None:
    """The session was closed: do not leave its agents topic open. Quiet on any failure."""
    if not _path(session).exists():
        return
    with _locked(session) as path:
        st = _load(path)
        thread = st.get("thread")
        if isinstance(thread, int) and st.get("open"):
            try:
                bot.call("closeForumTopic", {"chat_id": _saved_chat(cfg, st), "message_thread_id": thread})
            except TelegramError:
                pass
        st.update(open=False, running={})
        _save(path, st)


def input_ignored(thread, chat: Optional[int] = None) -> Optional[bool]:
    """None: not an agents topic. Otherwise whether this is the first message written there (say once that it takes no input).
    `chat`: the project group the message came from (None = the home chat); a thread id means nothing without it."""
    root = state_dir() / "agents-topics"
    try:
        paths = sorted(root.glob("*.json"))
    except OSError:
        return None
    for path in paths:
        if _load(path).get("thread") != thread or _load(path).get("chat") != chat:
            continue
        with _locked(path.stem) as locked:
            st = _load(locked)
            if st.get("thread") != thread or st.get("chat") != chat:
                return None
            first = not st.get("told")
            if first:
                st["told"] = True
                _save(locked, st)
            return first
    return None
