"""Async Stop hook (asyncRewake): wake an idle Claude Code session from Telegram.

Runs in the background after each turn. Waits for this session's queue, then
exits 2 with the owner's text, which Claude Code hands to the model as a new
turn. Ends quietly (exit 0) as soon as the session moves on by itself (its
transcript grows), when a newer waiter takes over, or after MAX_WAIT.
Telegram text is never approval for sensitive actions (framing.py).
"""
from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable, Optional, Tuple

from . import policy
from .config import Config, private_dir, state_dir, write_private
from . import compact
from .hooks import TAKE_WAIT, delivery_lease, delivery_text, load_config, spawn
from .inbox import Inbox, default_inbox, spawn_delivered
from .sweeper import before_take
from .telegram import Bot, TelegramError
from .topics import place

MAX_WAIT = 12 * 3600
POLL = 2
SETTLE = 3  # let this turn's own transcript writes finish before measuring
GROWTH = 20000  # bytes of new transcript that mean "the session is working again"
COLD_AFTER = 3600  # seconds (1 hour): prompt cache has gone cold by then
COLD_MIN = 150_000  # context tokens worth compacting when the cache is cold anyway


def auto_compact_limit() -> int:
    """Thousands of context tokens that start an automatic compaction; 0 = off."""
    try:
        return max(0, int((state_dir() / "auto-compact").read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return 0


def valid_transcript(path: str) -> bool:
    """A session transcript: a .jsonl under a Claude projects directory (the default home or an account profile)."""
    try:
        from . import accounts
        real = Path(path).resolve()
        roots = [accounts.claude_home() / "projects"]
        prof = accounts.profiles_dir()
        if prof.is_dir():
            roots += [d / "projects" for d in prof.iterdir()]
        return real.suffix == ".jsonl" and any(real.is_relative_to(r.resolve()) for r in roots)
    except (OSError, ValueError):
        return False


SETTLE_COMPACT = 6.0  # seconds to wait for the compaction to reach the transcript


def refresh_status(session: str, transcript: str, cfg: Optional[Config] = None, bot=None,
                   sleep: Callable[[float], None] = time.sleep, source: str = "compact") -> bool:
    """A fresh status message (and the keyboard's live info row) when the conversation changes under
    it. After a compaction (`source` "compact") the SessionStart hook can run before the compaction is
    in the transcript: wait a moment for it, and never show the old pre-compaction number (without the
    new size the button is left out). After /clear the conversation is empty: no number until the first
    reply. A startup or resume shows the transcript's own size."""
    try:
        cfg = cfg or load_config()
        if not cfg.ready:
            return False
        bot = bot or Bot.from_config(cfg)
        found = place(cfg, session)
        if found is None or not transcript:
            return False
        from .usage import context_state
        from .mirror import status_message, get_keyboard
        from .i18n import t
        lang = getattr(cfg, "ui_language", None)
        ctx, _, compacted = context_state(transcript)
        waited = 0.0
        while source == "compact" and not compacted and waited < SETTLE_COMPACT:
            sleep(0.5)
            waited += 0.5
            ctx, _, compacted = context_state(transcript)
        if source == "clear" or (source == "compact" and not compacted):
            ctx = 0
        tip = t("footer_tip", lang=lang) if ctx >= 400_000 else ""
        footer = t("footer_context", lang=lang, k=f"{round(ctx / 1000):,}", tip=tip) if ctx else ""
        status_message(cfg, found.thread, footer, get_keyboard(lang), bot=bot, chat=found.away)
        return True
    except Exception:
        return False


def cold_compact(session: str, transcript: str, idle_since: float, timeout: float = 30.0,
                 poll_interval: float = 0.5, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, spawn_fn: Optional[Callable] = None) -> bool:
    """The owner writes after an hour's break into a big context: the whole context is re-read at full price anyway, so compact first.
    Starts the compaction and returns True; the message stays queued and the waiter started
    after the compaction delivers it. Falls back to delivery if no compaction marker within 30 s."""
    try:
        if clock() - idle_since < COLD_AFTER:
            return False
        from .compact import limit_for
        if not limit_for(session):
            return False  # automatic compaction is off, for everything or for this session
        from .status import usage_of
        ctx = usage_of(transcript)[0]
        if ctx < COLD_MIN:
            return False
        started = clock()
        if spawn_fn is not None:
            spawn_fn(session, ctx)
        else:
            compact.spawn_auto_compact(session, ctx, cold=True)  # a process: wake exits right after
        mark = compact.marker(session)
        while clock() - started < timeout:
            try:
                if mark.exists() and mark.stat().st_mtime >= started - 1:
                    return True
            except OSError:
                pass
            sleep(poll_interval)
        return False
    except Exception:
        return False


LIMIT_RECHECK = 30  # seconds between looks at the transcript while messages wait for a quota to return
_limit_seen: dict = {}


def limit_active(session: str, clock: Callable[[], float] = time.time) -> bool:
    """The session's quota is exhausted and has not reset yet: messages stay queued instead of
    being handed to a turn that would only fail (recover.py delivers them after the reset)."""
    now = clock()
    seen = _limit_seen.get(session)
    if seen and 0 < _forgotten_at(session) and _forgotten_at(session) >= seen[0]:
        _limit_seen.pop(session, None)  # another process moved the session off the account since that look
        seen = None
    if seen and now - seen[0] < LIMIT_RECHECK and seen[1] > now:
        return True
    try:
        from .usage import limit_of
        error = limit_of(session)
    except Exception:
        error = None
    if error and error["reset"] > now:
        _limit_seen[session] = (now, error["reset"])
        return True
    _limit_seen.pop(session, None)
    return False


def _forgotten_path(session: str):
    return state_dir() / "limit-forgotten" / (re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_")


def _forgotten_at(session: str) -> float:
    try:
        return float(_forgotten_path(session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0


def forget_limit(session: str) -> None:
    """The session moved off the exhausted account. The move runs in its own process (`ctb account
    auto-move`), so the listener's cached "limit is active" look is dropped through a file it checks."""
    _limit_seen.pop(session, None)
    try:
        write_private(_forgotten_path(session), repr(time.time()))
    except OSError:
        pass


def _own_chain() -> list:
    """This process and its parents. Empty when it cannot be read, which makes every check below fail closed."""
    from .inbox import process_ancestors
    try:
        return process_ancestors(os.getpid())
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def _is_session_process(session: str, chain: list) -> bool:
    """True when this hook runs inside the session's own Claude process, or when no live process is recorded
    for the id yet (SessionStart can come before the record is written, so that cannot be checked)."""
    from .inbox import claude_pids
    try:
        known = claude_pids(session)
    except (OSError, ValueError):
        return False
    return not known or bool(set(known) & set(chain))


def _pane_is_ours(pane: str, chain: list) -> bool:
    """The pane named by TMUX_PANE is this process's own: its first process is one of our ancestors."""
    from .inbox import _tmux
    try:
        res = _tmux("display", "-p", "-t", pane, "#{pane_pid}")
        out = res.stdout.strip() if res.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return False
    return out.isdigit() and int(out) in chain


def hook_start(stdin_text: str) -> int:
    """Associate a session with its tmux pane without trusting arbitrary pane targets, and a SessionStart after /compact (source == "compact") tells the topic the wait is over:
    the PreCompact notice asked the owner to wait for exactly this message."""
    try:
        data = json.loads(stdin_text)
        if not isinstance(data, dict):
            return 0
        session = data.get("session_id")
        if not isinstance(session, str) or not session:
            return 0
        chain = _own_chain()
        if not _is_session_process(session, chain):
            return 0  # a process that is not inside this session's Claude process changes nothing
        if data.get("source") == "compact" and load_config().ready:
            try:
                if compact.mark_done(session):
                    # Started from Telegram: its progress message says it, so no
                    # notice of our own; only the topic returns to "done".
                    spawn(["session-state", f"--session={session}", "--state=done"])
                else:
                    spawn(["lifecycle", f"--session={session}", "--kind=compact-done"])
            except Exception:
                pass
        source = data.get("source")
        transcript = data.get("transcript_path") or ""
        if source in ("compact", "clear", "startup", "resume") and transcript and load_config().ready:
            # detached: it may wait for the compaction to reach the transcript, never inside the hook
            spawn(["refresh-status", f"--session={session}", f"--transcript={transcript}", f"--source={source}"])
        pane = os.environ.get("TMUX_PANE", "")
        base = private_dir(state_dir())
        with open(base / "tmux-panes.lock", "a") as lock:
            os.chmod(base / "tmux-panes.lock", 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = base / "tmux-panes.json"
            try:
                panes = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(panes, dict):
                    panes = {}
            except (OSError, ValueError):
                panes = {}
            panes.pop(session, None)
            previous = ""
            if re.fullmatch(r"%[0-9]+", pane) and _pane_is_ours(pane, chain):
                # the session this window ran before: a /clear or /resume typed in Claude switched it
                previous = next((s for s, p in reversed(list(panes.items())) if p == pane and s != session), "")
                if previous and source in ("clear", "resume"):
                    panes.pop(previous, None)
                panes[session] = pane
            write_private(path, json.dumps(dict(list(panes.items())[-200:])))
        if source in ("clear", "resume") and load_config().ready:
            # the topic says so, and follows the window when the new conversation has no topic (direct.py)
            spawn(["direct-note", f"--session={session}", "--field=switch", f"--value={source}:{previous}"])
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return 0


def wait_for_message(data: dict, inbox: Optional[Inbox] = None, poll: float = POLL, settle: float = SETTLE,
                     max_wait: float = MAX_WAIT) -> Tuple[int, str]:
    """(exit code, text to print). 2 = deliver the text to the model; 0 = nothing to do."""
    session = data.get("session_id") or ""
    transcript = data.get("transcript_path") or ""
    if not session:
        return 0, ""
    inbox = inbox or default_inbox()
    waiters = private_dir(state_dir() / "wake")
    marker = waiters / (re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_")
    token = f"{os.getpid()}-{time.time()}"
    marker.write_text(token)  # the newest waiter wins; older ones step aside
    time.sleep(settle)
    try:
        baseline = os.path.getsize(transcript)
    except OSError:
        baseline = 0
    idle_since = time.time()
    tried_cold = False
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            if marker.read_text() != token:
                return 0, ""
        except OSError:
            return 0, ""
        # mirroring off (policy.py): this waiter takes nothing and delivers nothing; it keeps waiting
        if inbox.pending(session) and not limit_active(session) and policy.on():
            if not tried_cold and data.get("source") != "compact":
                tried_cold = True
                if cold_compact(session, transcript, idle_since):
                    return 0, ""
            try:  # under a lease: if Claude Code drops this exit-2 output, the sweeper puts the messages back
                until = time.monotonic() + TAKE_WAIT
                try:  # an earlier delivery still in flight goes first: never overtaken (sweeper.before_take)
                    held = before_take(inbox, session, until=until)
                except Exception:
                    held = False
                msgs = inbox.take(session, only=(lambda i: False) if held else None,
                                  lease=delivery_lease(data, "wake"), until=until)
            except OSError:
                msgs = []  # the lock is busy: try again on the next look
            if msgs:
                spawn_delivered(msgs)  # 👌 here too: most messages reach an idle session through this path
                return 2, delivery_text(msgs)
        try:
            if os.path.getsize(transcript) > baseline + GROWTH:
                return 0, ""  # working again; its next Stop starts a new waiter
        except OSError:
            pass
        time.sleep(poll)
    return 0, ""


def _silence_stderr() -> None:
    """Claude shows an async Stop hook's stderr as a "Stop hook blocking error". This hook speaks only
    on stdout, so anything on fd 2 (e.g. macOS libmalloc's "MallocStackLogging: can't turn off ..."
    line, written below Python) is noise: point fd 2 at /dev/null."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
        os.close(devnull)
    except OSError:
        pass


def hook_wake(stdin_text: str) -> int:
    _silence_stderr()
    try:
        data = json.loads(stdin_text)
        if not load_config().ready:
            return 0
        code, text = wait_for_message(data)
    except Exception:
        return 0
    if text:
        print(text)
    return code
