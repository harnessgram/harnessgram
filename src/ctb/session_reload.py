"""Reload hooks by resuming stale, idle bridge sessions in their existing panes.

Busy/draft/dialog/unknown state is skipped, never interrupted. Run again after the
session becomes idle. No account change, force kill or automatic work nudge; a pane that
closed with Claude on /exit is reopened as the bridge opens sessions.
"""
from __future__ import annotations

import fcntl
import re
import threading
import time

from . import accounts, inbox, install
from .config import home_chat, private_dir, state_dir
from .topics import TopicMap

_WORKER = threading.Lock()


def process_start(pid):
    """The actual process start (not transcript/session creation), and PID reuse identity."""
    identity = inbox._proc_start(pid)
    if not identity:
        return None
    try:
        return time.mktime(time.strptime(identity, "%a %b %d %H:%M:%S %Y")), identity
    except (ValueError, OverflowError):
        return None


def _idle(session, pane, pid, identity):
    if (not accounts.restart_is_safe(session)
            or inbox.session_pid(session) != pid or inbox._proc_start(pid) != identity
            or not inbox.pane_runs_session(pane, session, known_pid=pid)
            or inbox._pane_claude_targets(pane) != [(pid, identity)]):
        return False
    try:
        body = inbox.pane_raw(pane)
        if inbox._dialog_open(body, session) or inbox._footer_shows_work(body):
            return False
        box, ready = inbox.input_box(pane)
        return box == "" and ready
    except (OSError, ValueError):
        return False


def _reopen(session, folder, command):
    """Open the closed session without typing into any pane: its own tmux session, or a new window
    in that session when it still exists (another pane or window kept it); returns the pane id or None."""
    name = "tg-" + session[:8]
    tail = ["-c", folder, "-P", "-F", "#{pane_id}", *inbox.shell_argv(command)]
    for argv in (["new-session", "-d", "-s", name, *tail], ["new-window", "-d", "-a", "-t", name + ":", *tail]):
        try:
            opened = inbox._tmux(*argv)
        except OSError:
            return None
        pane = (opened.stdout or "").strip()
        if opened.returncode == 0 and re.fullmatch(r"%[0-9]+", pane):
            return pane
    return None


def _restart(session, pane, pid, identity, cutoff):
    """Caller owns the move lock and terminal claim. Recheck immediately before /exit."""
    if not _idle(session, pane, pid, identity):
        return "busy"
    # Capture the exact account command/folder before exit hooks remove the old records.
    pane_command = f"{accounts.shell_command(session)} --remote-control --resume {session}"
    folder = inbox.session_folder(session)
    if not folder:
        return "unknown"
    inbox.mark_planned_restart(session)
    exited = False
    try:
        if not _idle(session, pane, pid, identity):
            return "busy"
        if inbox.type_command(session, pane, "/exit", verify_session=True) != "sent":
            return "busy"
        for _ in range(45):
            time.sleep(1)
            running = inbox._pane_command(pane)
            # Bridge panes run Claude directly, so /exit closes the pane; a manual pane falls back to its shell.
            if running is None or running in inbox.SHELLS:
                if inbox.session_pid(session) == pid and inbox._proc_start(pid) == identity:
                    continue  # the old process is still ending
                exited = True
                break
            if inbox._exit_dialog_pending(pane):
                # Never answer "stop background tasks": cancel (= Stay) so the session keeps working.
                inbox._tmux("send-keys", "-t", pane, "Escape")
                return "background"
        if not exited:
            return "failed"
        if inbox.session_pid(session) is None:
            # Nobody else brought it back: reopen it. Never type into a shell, where the owner may be typing.
            pane = _reopen(session, folder, pane_command)
            if not pane:
                return "failed"
            time.sleep(3)
            if not inbox.pass_trust(pane, folder) or inbox._resume_dialog_pending(pane):
                return "dialog"
        else:
            pane = inbox.pane_for_session(session) or pane  # restarted by someone else meanwhile: verify, never a second copy
        # A typed command or accepted resume dialog alone is not proof of a reload.
        for _ in range(20):
            new_pid = inbox.session_pid(session)
            begun = process_start(new_pid) if new_pid else None
            if (begun and begun[0] >= cutoff and (new_pid, begun[1]) != (pid, identity)
                    and inbox.pane_runs_session(pane, session, known_pid=new_pid)):
                return "reloaded"
            time.sleep(1)
        return "failed"
    finally:
        if not exited:
            inbox.clear_planned_restart(session)


def reload_sessions(cfg, session=None, dry_run=False):
    if session is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", session):
        raise ValueError("invalid_session")
    result = {"reloaded": [], "would_reload": [], "skipped": []}
    try:
        cutoff = install.settings_path().stat().st_mtime
    except OSError:
        raise ValueError("settings_unreadable") from None
    # Bridge-owned topics only; unrelated Claude sessions are never targets.
    places = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg).all_places()
    base = private_dir(state_dir() / "moving")
    # Serialize CLI/panel batches; also share each account-move lock across processes.
    with open(base / "hook-reload.lock", "a") as batch:
        try:
            fcntl.flock(batch, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("reload_running") from None
        seen = set()
        for rec in accounts.live_session_records():
            sid = rec.get("sessionId")
            if (not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", sid)
                    or sid in seen or sid not in places or (session is not None and sid != session)):
                continue
            seen.add(sid)
            def skip(reason):
                result["skipped"].append({"session": sid, "reason": reason})
            try:
                pid = int(rec.get("pid") or 0)
                started = process_start(pid) if pid else None
                if not started:
                    skip("unknown")
                    continue
                if started[0] >= cutoff:
                    skip("current")
                    continue
                pane = inbox.pane_for_session(sid)
                if not pane:
                    skip("no_pane")
                    continue
                with open(base / (inbox.safe_name(sid) + ".lock"), "a") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        skip("busy")
                        continue
                    if not inbox._claim_terminal(sid):
                        skip("busy")
                        continue
                    try:
                        with accounts.ops_lock():
                            if not _idle(sid, pane, pid, started[1]):
                                skip("busy")
                            elif dry_run:
                                result["would_reload"].append(sid)
                            else:
                                outcome = _restart(sid, pane, pid, started[1], cutoff)
                                if outcome == "reloaded":
                                    result["reloaded"].append(sid)
                                else:
                                    skip(outcome)
                    finally:
                        inbox._release_terminal(sid)
            except (OSError, ValueError):
                skip("unknown")
        if session is not None and session not in seen:
            result["skipped"].append({"session": session, "reason": "not_live"})
    return result


def start(cfg):
    """Bounded background batch; HTTP returns immediately and state polls show the result."""
    from . import hook_health
    if not _WORKER.acquire(blocking=False):
        return
    try:
        with hook_health.locked() as data:
            data["reload"] = {"running": True, "at": time.time()}
    except Exception:
        _WORKER.release()
        raise
    def work():
        try:
            try:
                result = reload_sessions(cfg)
            except Exception:
                result = {"error": "reload_failed"}  # no settings, pane or exception content in logs
            with hook_health.locked() as data:
                data["reload"] = dict(result, running=False, at=time.time())
        finally:
            _WORKER.release()
    try:
        threading.Thread(target=work, daemon=True).start()
    except Exception:
        _WORKER.release()
        with hook_health.locked() as data:
            data["reload"] = {"running": False, "error": "reload_failed", "at": time.time()}
        raise
