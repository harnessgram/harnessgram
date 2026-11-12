"""One Telegram topic per Claude Code session.

The bot's private chat must have Threaded Mode on (BotFather > your bot > Bot
Settings > Threaded Mode), otherwise createForumTopic is refused and everything
falls back to the main chat (outbound only: inbound messages are routed by topic).
The map session id -> {thread, name} is kept in a private JSON file; a topic is
renamed whenever the session's title changes. Sessions placed in a project group live in a second
file, topics-projects.json, because thread ids are only unique inside one chat.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Optional

from . import scheduled
from .config import home_chat, private_dir, state_dir, write_private
from .projects import Place, project_by_chat, resolve
from .telegram import TelegramError

MAX_ENTRIES = 300
MAX_MOVES = 500  # old topics remembered after a move (topic-moves.json)
NAME_LIMIT = 120  # Telegram allows 1-128 characters
STATE_ICONS = {"busy": "⚙️", "waiting": "❓", "done": "✅"}
PLACEHOLDER = "🧑🏼‍💻"  # the "working" placeholder message; a lone emoji is shown large and animated


def session_name(session: str) -> str:
    """A locally saved name for sessions that have no app title."""
    try:
        names = json.loads((state_dir() / "topic-names.json").read_text(encoding="utf-8"))
        name = names.get(session) if isinstance(names, dict) else None
        return name[:NAME_LIMIT] if isinstance(name, str) else ""
    except (OSError, ValueError):
        return ""


def session_override_name(session: str) -> str:
    """A name set explicitly via topic-rename that overrides the app title."""
    try:
        path = state_dir() / "topic-renames.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        val = data.get(session) if isinstance(data, dict) else None
        return val[:NAME_LIMIT] if isinstance(val, str) else ""
    except (OSError, ValueError):
        return ""


def save_topic_rename(session: str, name: str) -> None:
    """Store the explicit rename so it wins over the app title and persists across restarts."""
    if not (isinstance(session, str) and session) or not (name and str(name).strip()):
        return
    clean = str(name).strip()[:NAME_LIMIT]
    save_session_name(session, clean)
    try:
        path = state_dir() / "topic-renames.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[session] = clean
        write_private(path, json.dumps(dict(list(data.items())[-MAX_ENTRIES:]), ensure_ascii=False))
    except OSError:
        pass


def save_session_name(session: str, name: str) -> None:
    """Persist a task or Telegram name as a fallback when there is no app title."""
    try:
        path = state_dir() / "topic-names.json"
        try:
            names = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            names = {}
        if not isinstance(names, dict):
            names = {}
        if not (isinstance(session, str) and session) or not (name and str(name).strip()):
            return
        names[session] = str(name).strip()[:NAME_LIMIT]
        write_private(path, json.dumps(dict(list(names.items())[-MAX_ENTRIES:]), ensure_ascii=False))
    except OSError:
        pass


def topic_rename(cfg, name: str, session: Optional[str] = None, bot=None) -> int:
    """Rename this session's Telegram topic and persist the name so it wins over the app title."""
    from .telegram import Bot
    clean = " ".join(str(name or "").split())[:NAME_LIMIT]
    if not clean:
        print("Harnessgram: topic-rename requires a non-empty name", file=sys.stderr)
        return 1
    sid = session or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not sid:
        from .inbox import last_session
        tmap = TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg)
        sid = last_session(tmap) or ""
    if not sid:
        print("Harnessgram: no session found to rename (set CLAUDE_CODE_SESSION_ID or pass session)", file=sys.stderr)
        return 1
    bot = bot or (Bot.from_config(cfg) if cfg.ready else None)
    topics = TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg)
    found = topics.place_of(sid)
    if found is None:
        print(f"Harnessgram: session {sid} has no Telegram topic", file=sys.stderr)
        return 1
    chat, thread = found.chat, found.thread
    save_topic_rename(sid, clean)
    from . import title_sync
    title_sync.want(sid, clean)  # the Claude Code session title follows the topic name
    state = session_state(sid).get("state", "")
    visible = (STATE_ICONS.get(state, "") + " " + clean).strip()[:128]
    if bot:
        try:
            bot.call("editForumTopic", {"chat_id": chat,
                                        "message_thread_id": thread,
                                        "name": visible})
        except TelegramError:
            pass
    with topics._locked():
        load, save = (topics._load, topics._save) if found.home else (topics._load_projects, topics._save_projects)
        data = load()
        entry = data.get(sid) or {}
        entry["thread"] = thread
        entry["name"] = clean
        entry["visible_name"] = visible
        data[sid] = entry
        save(data)
    print(f"renamed to: {clean}")
    return 0


def record_reply(chat: int, thread: int, message_id: int) -> None:
    """Remember the newest delivered reply part for each chat and topic."""
    # A group's chat id is negative: only topic and message ids are always positive.
    if type(chat) is not int or chat == 0 or any(type(x) is not int or x <= 0 for x in (thread, message_id)):
        return
    root = private_dir(state_dir())
    with open(root / "latest-replies.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "latest-replies.json"
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(entries, dict):
                entries = {}
        except (OSError, ValueError):
            entries = {}
        key = f"{chat}:{thread}"
        old = entries.get(key)
        entries[key] = max(old, message_id) if type(old) is int else message_id
        write_private(path, json.dumps(entries))


def latest_reply(chat: int, thread: int) -> Optional[int]:
    try:
        entries = json.loads((state_dir() / "latest-replies.json").read_text(encoding="utf-8"))
        value = entries.get(f"{chat}:{thread}") if isinstance(entries, dict) else None
        return value if type(value) is int else None
    except (OSError, ValueError):
        return None


def name_for_task(folder: str, task: str, label: str = "") -> str:
    """A new session's topic name: the project label (or folder) and the first five words of its task, capped at
    60 characters. A worktree that Claude made under <repo>/.claude/worktrees is named after <repo>, and markup
    tags in the task (a remote session's first prompt starts with one) are dropped."""
    folder = str(folder or "").split(os.sep + ".claude" + os.sep + "worktrees" + os.sep)[0]
    folder = label or (os.path.basename(folder.rstrip("/")) if folder else "")
    task = re.sub(r"</?[A-Za-z][^<>]{0,300}>", " ", str(task or ""))
    words = " ".join(task.split()).split()[:5]
    return f"{folder}: {' '.join(words)}"[:60].rstrip(": ").strip()


def session_state_path(session: str) -> Path:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_"
    return state_dir() / "session-state" / (name + ".json")


def _task_of(session: str) -> str:
    try:
        return scheduled.task_of(session)
    except Exception:
        return ""  # never worth a topic


def _newest(chat, thread, found: list) -> Optional[str]:
    """One session per topic, except a scheduled task's topic that several runs share: the newest run."""
    if len(found) > 1:
        newest = scheduled.newest_run(chat, thread)
        if newest in found:
            return newest
    return found[0] if found else None


def session_state(session: str) -> dict:
    try:
        data = json.loads(session_state_path(session).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _drop(bot, chat_id: int, message_id: int) -> None:
    try:
        bot.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    except TelegramError:
        pass


def _sweep(bot, chat, ids, keep) -> list:
    """Delete these placeholder messages except `keep`; returns what must stay tracked (keep, and
    the ones whose deletion failed for a reason other than "already gone")."""
    left = []
    for mid in dict.fromkeys(ids):
        if mid == keep:
            left.append(mid)
            continue
        try:
            bot.call("deleteMessage", {"chat_id": chat, "message_id": mid})
        except TelegramError as exc:
            if "not found" not in str(exc).lower():
                left.append(mid)
    return left


def main_turn_active(session: str) -> None:
    """The main conversation took a step: it is not idle, whatever background agents did before."""
    if not session or not session_state(session).get("main_idle"):
        return
    path = session_state_path(session)
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cur = session_state(session)
        cur.pop("main_idle", None)
        write_private(path, json.dumps(cur))


def set_state(cfg, session: str, state: str, bot=None, status: Optional[str] = None,
              optional: bool = False) -> int:
    """Track this session's own placeholder IDs; sweep only those IDs under one lock.

    ``bottom`` replaces the busy placeholder after another message was sent. Failed
    deletions remain tracked for the next transition, including same-state retries.

    ``optional`` marks background traffic (progress, heartbeat): a refresh is skipped without any
    network call while the shared flood gate is closed or the minute's budget is spent (flood.py).
    """
    if not cfg.ready or not session or state not in {*STATE_ICONS, "bottom"}:
        return 0
    from . import flood
    from .telegram import Bot
    # A muted task's status card and state icon are held back: every Telegram call below fails like a
    # refused send, so the local state (state, main_idle, ids still to delete) is written as always.
    held = scheduled.suppressed(session, "status")
    bot = scheduled.SilentBot() if held else (bot or Bot.from_config(cfg))
    topics = TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg)
    found = topics.place_of(session)
    chat = found.chat if found else home_chat(cfg)
    if optional and state == "bottom" and not held and not flood.allow_optional(chat, units=2):
        return 0  # a refresh is a delete plus a send; both are paid for here, or neither happens
    if found is None and state == "busy" and not held:
        topics.ensure(session, "")
        found = topics.place_of(session)
        chat = found.chat if found else chat
    thread = found.thread if found else None
    if not isinstance(thread, int) or isinstance(thread, bool):
        return _held_state(session, state, optional) if held else 0
    send = (flood.optional_call if optional and not held else (lambda b, m, p: b.call(m, p)))
    # the refresh's two units were reserved above, so its own send is not charged again
    placeholder = ((lambda b, m, p: flood.optional_call(b, m, p, budget=False)) if optional and not held
                   else (lambda b, m, p: b.call(m, p)))
    path = session_state_path(session)
    private_dir(path.parent)
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # A move may have switched the session to another group while this waited for the lock: the
        # place read above is only good for the flood check; every call below uses this one.
        found = topics.place_of(session)
        if found is None or type(found.thread) is not int:
            return 0
        chat, thread = found.chat, found.thread
        cur = session_state(session)
        if cur.get("chat") is not None and cur.get("chat") != chat:
            # the tracked messages belong to another chat (the session moved): never address them here
            for key in ("all", "placeholder", "status_id", "status_text"):
                cur.pop(key, None)
        cur["chat"] = chat
        if state == "bottom" and cur.get("state") != "busy":
            return 0
        target = "busy" if state == "bottom" else state
        drop_status = None
        if target != "busy":
            if cur.get("status_id"):
                drop_status = cur.pop("status_id")
                cur.pop("status_text", None)
        elif state == "bottom":
            if status is None and cur.get("status_id"):
                drop_status = cur.pop("status_id")
                cur.pop("status_text", None)
            elif status:
                try:
                    if cur.get("status_id"):
                        if cur.get("status_text") != status:
                            send(bot, "editMessageText", {"chat_id": chat,
                                                      "message_id": cur["status_id"],
                                                      "text": status})
                            cur["status_text"] = status
                    else:
                        sent = send(bot, "sendMessage", {"chat_id": chat,
                                                         "message_thread_id": thread,
                                                         "text": status,
                                                         "disable_notification": True})
                        if isinstance(sent, dict) and "message_id" in sent:
                            cur["status_id"] = sent["message_id"]
                            cur["status_text"] = status
                except TelegramError:
                    pass
        tracked = [mid for mid in cur.get("all", []) if type(mid) is int and mid > 0]
        old = cur.get("placeholder")
        if type(old) is int and old > 0 and old not in tracked:
            tracked.append(old)
        keep = old if target == "busy" else None
        if target == "busy" and (not keep or state == "bottom"):
            # The old placeholder goes first: sending the new one first showed two
            # for a moment. If the new one cannot be sent, the topic simply has none until the next round.
            tracked = _sweep(bot, chat, tracked, None)
            keep = None
            try:
                mid = placeholder(bot, "sendMessage", {"chat_id": chat,
                                  "message_thread_id": thread, "text": PLACEHOLDER, "disable_notification": True}).get("message_id")
                if type(mid) is int and mid > 0:
                    keep = mid
                    tracked.append(mid)
            except TelegramError:
                pass
        left = _sweep(bot, chat, tracked, keep)
        if drop_status:
            try:
                bot.call("deleteMessage", {"chat_id": chat, "message_id": drop_status})
            except TelegramError as exc:
                if "not found" not in str(exc).lower():
                    left.append(drop_status)
        if cur.get("state") != target:
            cur.pop("stuck_alert", None)
        cur.update(state=target, all=left)
        if target == "done":  # stays set while background agents' tool calls refresh the topic to busy
            cur["main_idle"] = True
        elif state == "busy" and not optional:  # only a new prompt asks for busy this way
            cur.pop("main_idle", None)
        if keep:
            cur["placeholder"] = keep
        else:
            cur.pop("placeholder", None)
        write_private(path, json.dumps(cur))
        if not held:
            topics.refresh(session)
    return 0


def _held_state(session: str, state: str, optional: bool) -> int:
    """A muted task's run that has no topic yet: its local state (state, main_idle) is written under the
    same lock as set_state's, the same way, with no topic made and no Telegram call."""
    if state == "bottom":
        return 0  # a placeholder refresh changes nothing locally without a topic
    path = session_state_path(session)
    private_dir(path.parent)
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cur = session_state(session)
        if cur.get("state") != state:
            cur.pop("stuck_alert", None)
        cur["state"] = state
        if state == "done":
            cur["main_idle"] = True
        elif state == "busy" and not optional:
            cur.pop("main_idle", None)
        write_private(path, json.dumps(cur))
    return 0


def place(cfg, session: str) -> Optional[Place]:
    """Where this session's topic is, or None when it has none."""
    return TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg).place_of(session)


def chat_of(cfg, session: str) -> int:
    """The chat this session's topic is in (the home chat when it has none)."""
    found = place(cfg, session)
    return found.chat if found else home_chat(cfg)


class TopicMap:
    def __init__(self, path: Path, bot, chat_id: int, cfg=None):
        self.path, self.bot, self.chat_id = Path(path), bot, chat_id
        # Project groups exist only beside the home map and only for a config that lists them; a map
        # built without a config (the team map, older callers) sees the home topics alone.
        self.cfg = cfg
        self._ppath = self.path.with_name("topics-projects.json") if self.path.name == "topics.json" else None
        self._pchats = frozenset(p.chat_id for p in cfg.projects) if cfg is not None and self._ppath else frozenset()
        self._mpath = self.path.with_name("topic-moves.json") if self._ppath is not None else None

    def _load(self) -> Dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: Dict[str, dict]) -> None:
        write_private(self.path, json.dumps(dict(list(data.items())[-MAX_ENTRIES:]), ensure_ascii=False))

    def _load_projects(self) -> Dict[str, dict]:
        if self._ppath is None:
            return {}
        try:
            data = json.loads(self._ppath.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_projects(self, data: Dict[str, dict]) -> None:
        write_private(self._ppath, json.dumps(dict(list(data.items())[-MAX_ENTRIES:]), ensure_ascii=False))

    @contextlib.contextmanager
    def _locked(self):
        """Hooks of several sessions (and the listener) share this map; the lock keeps two of them
        from creating two topics for one session or overwriting each other's entries. One lock
        covers both files, so a session is never half in each."""
        private_dir(self.path.parent)
        with open(str(self.path) + ".lock", "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            yield

    def ensure(self, session: str, title: str, thread: Optional[int] = None, chat: Optional[int] = None) -> Optional[int]:
        with self._locked():
            found = self._ensure(session, title, thread=thread, chat=chat)
        self._suggest(session, title, found)
        return found

    def refresh(self, session: str, fallback: str = "Claude") -> Optional[int]:
        """Show the topic's current state icon, keeping the name the topic has NOW. The name is read under the
        same lock as the update: a name read before the lock could be stale (auto-naming may have saved a new
        one meanwhile) and would be written back over it."""
        with self._locked():
            title = (self.all_places().get(session) or {}).get("name") or fallback
            found = self._ensure(session, title)
        self._suggest(session, title, found)
        return found

    def _suggest(self, session: str, title: str, found: Optional[int]) -> None:
        if found is not None and self.bot is not None and self.cfg is not None and self.cfg.projects and title:
            try:
                from . import moves
                moves.suggest(self.cfg, self.bot, self, session, session_override_name(session) or title)
            except Exception as exc:  # an offer is never worth breaking the topic's own work
                print(f"Harnessgram: move suggestion failed: {type(exc).__name__}", file=sys.stderr)

    def _ensure(self, session: str, title: str, thread: Optional[int] = None, chat: Optional[int] = None) -> Optional[int]:
        """The topic's thread id for this session: created on first use, renamed when the
        title changed. None means 'post in the main chat' (Threaded Mode off, API down).
        `chat` picks the group for a NEW topic (a configured project group, else the home chat);
        a session that already has a topic keeps it."""
        data = self._load()
        pdata = self._load_projects()
        override = session_override_name(session)
        task = _task_of(session) if self.path.name == "topics.json" else ""
        name = (override or task or title or session_name(session) or "Claude")[:NAME_LIMIT]
        state = session_state(session).get("state") if self.path.name == "topics.json" else ""
        visible = (STATE_ICONS.get(state, "") + " " + name).strip()[:128]
        entry = pdata.get(session)
        if entry is not None and self.cfg is None:
            return None  # a project session seen without the settings: leave it (and its file) alone
        if entry is not None and entry.get("chat") not in self._pchats:
            pdata.pop(session)  # its group left the settings: the session goes home again
            self._save_projects(pdata)
            entry = None
        if entry is not None:
            where, store, save = entry["chat"], pdata, self._save_projects
        else:
            entry, where, store, save = data.get(session), self.chat_id, data, self._save
        why = "explicit"
        joined = False  # this run maps to its task's topic
        if entry is None:
            if chat is None and thread is None and self.cfg is not None and self._pchats:
                found, why = resolve(self.cfg, session, title)
                chat = found.chat_id if found else None
            if chat in self._pchats:
                where, store, save = chat, pdata, self._save_projects
            if task and thread is None:  # the destination chat is known first: a topic is shared only inside it
                shared = scheduled.topic_of(where, task)
                if shared is not None and self._open(where, shared):
                    thread, joined = shared, True
        elif task and thread is None and entry.get("task") != task:
            # proven scheduled after it got an ordinary topic: remap it once; the earlier topic is left as it is
            entry["task"] = task
            shared = scheduled.topic_of(where, task)
            if shared is not None and shared != entry["thread"] and self._open(where, shared):
                entry = dict(entry, thread=shared)
            store.pop(session, None)
            store[session] = entry
            save(store)
            scheduled.remember_topic(task, where, entry["thread"], session)
            if entry["thread"] == shared:
                return shared
        if thread is not None:
            if entry is None or entry.get("thread") != thread:
                entry = self._new_entry(where, thread, name, visible, why)
                if joined:
                    entry["task"] = task
                store.pop(session, None)  # re-insert last, so an updated entry counts as recent
                store[session] = entry
                save(store)
                if joined:  # the newest run: typed text in the task's topic goes to it
                    scheduled.remember_topic(task, where, thread, session)
            return thread
        try:
            if entry is None:
                topic = self.bot.call("createForumTopic", {"chat_id": where, "name": visible})
                entry = self._new_entry(where, topic["message_thread_id"], name, visible, why)
                if task:
                    entry["task"] = task
                    scheduled.remember_topic(task, where, entry["thread"], session)
            elif entry.get("name") != name or entry.get("visible_name", entry.get("name")) != visible:
                self.bot.call("editForumTopic", {"chat_id": where,
                                                 "message_thread_id": entry["thread"], "name": visible})
                entry["name"] = name
                entry["visible_name"] = visible
            else:
                return entry["thread"]
        except TelegramError:
            return entry["thread"] if entry else None  # keep working with what we have
        store.pop(session, None)  # re-insert last, so the newest sessions survive the cap
        store[session] = entry
        save(store)
        return entry["thread"]

    def _open(self, chat: int, thread: int) -> bool:
        """The task's topic can take this run: open, or closed by the bridge (an earlier run ended) and
        opened again now. False when it cannot be opened: the run gets a new topic."""
        from . import sessions_menu
        marked = [s for s, mk in sessions_menu.closed_marks().items() if isinstance(mk, dict)
                  and mk.get("thread") == thread and (mk.get("chat") or self.chat_id) == chat]
        try:
            return all(sessions_menu.reopen_for_message(self.cfg, self.bot, s) for s in marked)
        except Exception:
            return False

    def claim(self, session: str, thread: int, name: str, chat: Optional[int] = None) -> bool:
        """Give a brand new session the topic the owner just opened (the project picker): False when some
        session already has that topic. `chat` is its group (None = the home chat)."""
        with self._locked():
            if chat is None or chat == self.chat_id:
                data = self._load()
                if any(e.get("thread") == thread for e in data.values()):
                    return False
                data[session] = {"thread": thread, "name": name}
                self._save(data)
                return True
            if chat not in self._pchats:
                return False
            pdata = self._load_projects()
            if any(e.get("chat") == chat and e.get("thread") == thread for e in pdata.values()):
                return False
            pdata[session] = self._new_entry(chat, thread, name, name)
            self._save_projects(pdata)
            return True

    def _new_entry(self, chat: int, thread: int, name: str, visible: str, why: str = "explicit") -> dict:
        entry = {"thread": thread, "name": name, "visible_name": visible}
        if chat != self.chat_id:
            project = project_by_chat(self.cfg, chat)
            entry = {"chat": chat, **entry, "project": project.name if project else "", "why": why}
        return entry

    def moved_session(self, chat: int, thread: Optional[int]) -> Optional[str]:
        """The session that used to have this topic and moved to another group (None for a topic that
        was never moved). Input in such a topic is never delivered."""
        if self._mpath is None or type(thread) is not int:
            return None
        try:
            data = json.loads(self._mpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        value = data.get(f"{chat}:{thread}") if isinstance(data, dict) else None
        if not isinstance(value, str) or not value:
            return None
        here = self.place_of(value)
        if here is not None and here.chat == chat and here.thread == thread:
            return None  # the session is still in this topic (a move that did not finish): not moved
        return value

    def relocate(self, session: str, chat: int, thread: int, name: str, visible: str) -> Optional[Place]:
        """Point a session at its new topic `thread` in `chat` (the home chat or a configured project group)
        and remember its old topic as moved, so input there is answered with a link and never delivered.
        Returns the old place; None (nothing changed) when the session has no topic or `chat` is not one of ours.
        A failure part-way puts the place files back as they were and raises."""
        if self._mpath is None or type(thread) is not int or (chat != self.chat_id and chat not in self._pchats):
            return None
        with self._locked():
            old = self.place_of(session)
            if old is None or old.chat == chat:
                return None
            data, pdata = self._load(), self._load_projects()
            before = copy.deepcopy((data, pdata))
            entry = self._new_entry(chat, thread, name, visible, "move")
            try:
                if chat == self.chat_id:  # to the home group: the project file first, so a crash leaves a fresh topic, not two
                    pdata.pop(session, None)
                    self._save_projects(pdata)
                    data.pop(session, None)
                    data[session] = entry
                    self._save(data)
                else:  # to a project group: the new place first, so a crash leaves it winning over the old one
                    pdata.pop(session, None)
                    pdata[session] = entry
                    self._save_projects(pdata)
                    if session in data:
                        del data[session]
                        self._save(data)
                # The old topic refuses input only once the session has surely left it: written last,
                # and inside the same rollback (a failed atomic write leaves the file as it was).
                moves = self._load_moves()
                moves.pop(f"{old.chat}:{old.thread}", None)
                moves[f"{old.chat}:{old.thread}"] = session
                write_private(self._mpath, json.dumps(dict(list(moves.items())[-MAX_MOVES:]), ensure_ascii=False))
            except BaseException:
                for save, original in ((self._save, before[0]), (self._save_projects, before[1])):
                    try:
                        save(original)
                    except Exception:
                        pass
                raise
            return old

    def _load_moves(self) -> Dict[str, str]:
        try:
            data = json.loads(self._mpath.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def forget(self, session: str, chat: Optional[int] = None) -> None:
        """Drop a mapping (the user deleted the topic); the next ensure() creates a new one.
        `chat` is the chat that reported the topic gone (default: the home chat): an error from
        another chat says nothing about this session's own topic, so it is kept."""
        chat = self.chat_id if chat is None else chat
        with self._locked():
            pdata = self._load_projects() if self.cfg is not None else {}  # no settings: never touch the project file
            if session in pdata:
                if pdata[session].get("chat") == chat:
                    gone = pdata.pop(session)
                    self._save_projects(pdata)
                    scheduled.forget_topic(chat, gone.get("thread"))
                return
            data = self._load()
            gone = data.pop(session, None) if chat == self.chat_id else None
            if gone is not None:
                self._save(data)
                scheduled.forget_topic(chat, gone.get("thread"))

    def follow(self, old: str, new: str) -> bool:
        """The window of session `old` now runs session `new` (a /clear, or a /resume of a conversation that
        has no topic yet): `new` takes over `old`'s topic, with its name and place. False (nothing changed)
        when `old` has no topic or `new` already has one."""
        if not old or not new or old == new:
            return False
        with self._locked():
            data = self._load()
            pdata = self._load_projects() if self.cfg is not None else {}
            if new in data or new in pdata:
                return False
            if old in data:
                data[new] = data.pop(old)
                self._save(data)
                return True
            if old in pdata:
                pdata[new] = pdata.pop(old)
                self._save_projects(pdata)
                return True
        return False

    def all(self) -> Dict[str, dict]:
        return self._load()

    def all_places(self) -> Dict[str, dict]:
        """Every session's entry from both files, each with its "chat" and "home" added. Sessions
        whose group is no longer in the settings are left out."""
        found = {s: dict(e, chat=self.chat_id, home=True) for s, e in self._load().items()}
        for session, entry in self._load_projects().items():
            if entry.get("chat") in self._pchats:
                found[session] = dict(entry, home=False)
        return found

    def thread_of(self, session: str) -> Optional[int]:
        entry = self._load().get(session)
        return entry.get("thread") if entry else None

    def place_of(self, session: str) -> Optional[Place]:
        """The session's chat and thread; None when it has no topic, or its group is no longer configured."""
        entry = self._load_projects().get(session)
        if entry is not None and entry.get("chat") in self._pchats and type(entry.get("thread")) is int:
            return Place(entry["chat"], entry["thread"], home=False)
        entry = self._load().get(session)
        if entry is not None and type(entry.get("thread")) is int:
            return Place(self.chat_id, entry["thread"], home=True)
        return None

    def session_at(self, chat: int, thread: Optional[int]) -> Optional[str]:
        """The session behind a topic. The only safe inbound lookup: a thread id means nothing without its chat."""
        if type(thread) is not int:
            return None
        if chat == self.chat_id:
            return self.session_for_thread(thread)
        if chat in self._pchats:
            return _newest(chat, thread, [s for s, e in self._load_projects().items()
                                          if e.get("chat") == chat and e.get("thread") == thread])
        return None

    def session_for_thread(self, thread: Optional[int]) -> Optional[str]:
        """The session behind a topic of the home chat only."""
        if thread is None:
            return None
        return _newest(self.chat_id, thread, [s for s, e in self._load().items() if e.get("thread") == thread])
