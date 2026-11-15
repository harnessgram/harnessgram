"""Owner-confirmed Claude forks. All terminal operations target a fresh run only."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import time
import uuid

from . import accounts
from .config import private_dir, state_dir, write_private
from .i18n import t
from .projects import owner_chats
from .telegram import TelegramError
from .topics import NAME_LIMIT, save_topic_rename
from .usershell import ClaudeNotFound

CONFIRM_TTL = 120
COOLDOWN = 30


class ForkError(Exception):
    def __init__(self, key):
        self.key = key
        super().__init__(key)


@contextmanager
def _locked():
    root = private_dir(state_dir())
    with open(root / "session-forks.lock", "a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "session-forks.json"
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            data = {"offers": {}, "started": {}}
        # Malformed state fails closed: do not discard a possibly active cooldown.
        yield data
        write_private(path, json.dumps(data))


def source(cfg, topics, session):
    try:
        uuid.UUID(session)
    except (ValueError, TypeError, AttributeError):
        raise ForkError("fork_unavailable")
    entry = topics.all_places().get(session)
    if (not entry or entry.get("chat") not in owner_chats(cfg)
            or type(entry.get("thread")) is not int):
        raise ForkError("fork_unavailable")
    return entry


def arm(cfg, topics, session, scope, now=None):
    source(cfg, topics, session)
    now = time.time() if now is None else now
    token = secrets.token_hex(8)
    with _locked() as data:
        data["offers"] = {k: v for k, v in data["offers"].items() if v["at"] > now - CONFIRM_TTL}
        data["offers"][token] = {"session": session, "scope": scope, "at": now}
    return token


def title(name, lang):
    suffix = " · " + t("fork_label", lang=lang)
    return (name or "Claude")[:NAME_LIMIT - len(suffix)] + suffix


def start(cfg, bot, topics, session, scope, token, now=None):
    """Consume a scoped, single-use confirmation before scheduling any terminal work."""
    from . import inbox
    entry = source(cfg, topics, session)
    live_clock = now is None
    now = time.time() if now is None else now
    try:
        with _locked() as data:
            offer = data["offers"].get(token) if isinstance(token, str) else None
            if (not offer or offer["session"] != session or offer["scope"] != scope
                    or not 0 <= now - offer["at"] < CONFIRM_TTL):
                raise ForkError("fork_confirm_again")
            del data["offers"][token]
            last = data["started"].get(session)
            if last is not None and now - last < COOLDOWN:
                error = "fork_rate_limited"
            else:
                error = None
        if error:
            raise ForkError(error)
        folder = inbox.session_folder(session)
        if not folder or not Path(folder).is_dir():
            raise ForkError("fork_no_folder")
        if not shutil.which("tmux"):
            raise ForkError("fork_no_tmux")
        sid = str(uuid.uuid4())
        from . import assistant
        if assistant.is_assistant(session):
            assistant.mark(sid)  # the fork keeps the assistant role on every later launch (OSError: no fork)
        # Resume history belongs to the source account, even if the default changed.
        account = accounts.account_for(session)
        argv = inbox.shell_argv(accounts.shell_command(session)
                                + f" --remote-control --resume {shlex.quote(session)} --fork-session --session-id {sid}")
        started_at = time.time() if live_clock else now
        with _locked() as data:
            last = data["started"].get(session)
            if last is not None and started_at - last < COOLDOWN:
                raise ForkError("fork_rate_limited")
            accounts.record(sid, account)
            data["started"] = {k: v for k, v in data["started"].items() if started_at - v < COOLDOWN}
            data["started"][session] = started_at
        name = title(entry.get("name"), cfg.ui_language)
        run = lambda: _run(cfg, bot, topics, entry, sid, folder, name, argv)
        if not inbox._terminal_background(sid, run):
            with _locked() as data:
                if data["started"].get(session) == started_at:
                    data["started"].pop(session)
            raise ForkError("fork_busy")
        return sid
    except ForkError:
        raise
    except OSError as exc:
        # shell_argv probes the actual login shell, including the service's PATH fallback.
        raise ForkError("fork_no_claude" if isinstance(exc, ClaudeNotFound)
                        else "fork_failed")


def _say(cfg, bot, entry, key, **kw):
    try:
        bot.call("sendMessage", {"chat_id": entry["chat"], "message_thread_id": entry["thread"],
                                "text": t(key, lang=cfg.ui_language, **kw), "disable_notification": True})
    except TelegramError:
        pass


def _run(cfg, bot, topics, old, sid, folder, name, argv):
    from . import inbox
    target = "tg-" + sid[:8]
    launched = False
    try:
        # Reserve the topic before a SessionStart hook can route this run elsewhere.
        save_topic_rename(sid, name)
        thread = topics.ensure(sid, name, chat=old["chat"])
        found = topics.place_of(sid)
        if thread is None or found is None:
            raise ForkError("fork_topic_failed")
        if inbox._tmux("new-session", "-d", "-s", target, "-c", folder, *argv).returncode:
            raise ForkError("fork_failed")
        launched = True
        if not inbox.pass_trust(target, folder):
            raise ForkError("fork_trust_needed")
        if inbox._resume_dialog_pending(target):
            raise ForkError("fork_dialog_needed")
        # pass_trust can time out; an exited launch must never be called a successful fork.
        if not inbox._is_claude_process(inbox._pane_command(target)):
            raise ForkError("fork_failed")
        link = f"https://t.me/c/{str(found.chat).removeprefix('-100')}/{found.thread}"
        _say(cfg, bot, old, "fork_created", link=link)
    except (ForkError, OSError, subprocess.SubprocessError) as exc:
        # Cleanup is restricted to the fresh tmux name. The source pane is never looked up.
        stopped = not launched
        if launched:
            try:
                stopped = inbox._tmux("kill-session", "-t", target).returncode == 0
            except (OSError, subprocess.SubprocessError):
                pass
        found = topics.place_of(sid)
        if found and stopped:
            try:
                bot.call("deleteForumTopic", {"chat_id": found.chat, "message_thread_id": found.thread})
            except TelegramError:
                pass  # Keep its mapping if Telegram could not remove the failed fork's topic.
            else:
                topics.forget(sid, chat=found.chat)
        _say(cfg, bot, old, exc.key if isinstance(exc, ForkError) else "fork_failed")


def button(q, cfg, bot, topics, session):
    """Called only after the inbox's owner/chat/harness checks."""
    from . import inbox, menus
    m = q["message"]
    scope = f"topic:{m['chat']['id']}:{m.get('message_thread_id')}:{m['message_id']}"
    data = q["data"]
    edit = {"chat_id": m["chat"]["id"], "message_id": m["message_id"]}
    inbox._answer_button(bot, q, "")
    try:
        if data == "fork:ask":
            token = arm(cfg, topics, session, scope)
            bot.call("editMessageText", dict(edit, text=t("fork_confirm", lang=cfg.ui_language),
                reply_markup={"inline_keyboard": [[{"text": t("fork_action", lang=cfg.ui_language),
                                                    "callback_data": f"fork:go:{token}"}],
                                                   menus.close_row(cfg.ui_language)]}))
            return "fork-confirm"
        if not data.startswith("fork:go:"):
            return "ignored"
        start(cfg, bot, topics, session, scope, data[8:])
        key = "fork_starting"
    except ForkError as exc:
        key = exc.key
    except (OSError, ValueError, KeyError, TypeError):
        key = "fork_failed"
    try:
        bot.call("editMessageText", dict(edit, text=t(key, lang=cfg.ui_language),
                                        reply_markup={"inline_keyboard": []}))
    except TelegramError:
        pass
    return "fork"
