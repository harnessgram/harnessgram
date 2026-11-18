"""Sessions come back after a power cut or reboot.

The heartbeat records which session windows ("tg-<id>") are open. When it starts shortly after the
Mac booted and that record was written before the boot, every session in it is reopened, one by one,
the same way writing in its topic would reopen it. Windows closed on purpose before the cut are not in
the record, so they stay closed.

The tmux server can also die on its own while the Mac stays up (five sessions gone, and the
next snapshot forgot them). The record keeps the server's start time; when the server is gone or new
and two or more recorded sessions vanished with it, they are reopened the same way.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from typing import Callable, Iterable, List, Optional

from .config import state_dir, write_private
from .topics import TopicMap

RECORD = "live-sessions.json"
BOOT_WINDOW = 2 * 3600  # only a heartbeat started this soon after boot revives sessions (a slow login)
FRESH = 15 * 60  # the record must have been written this soon before the boot (else the heartbeat was off)
CRASH_MIN = 2  # a tmux crash closes them all at once; closing by hand goes one at a time
CRASH_EVERY = 30 * 60  # at most one crash reopen this often: sessions that keep dying are not reopened forever
RETRY_EVERY = 2 * 60  # failed reopens wait at least this long before another attempt
RETRY_MAX = 3  # per recovery; a later boot gets a new budget, even during CRASH_EVERY
CLOSED = "closed-sessions.json"  # sessions the owner closed (/exit, logout): never reopened as crash losses
# Handed to each reopened session as the first prompt of the process the bridge starts (`claude --resume <id>
# "<prompt>"`): reopening alone left ten sessions sitting at an empty prompt after the 15.11 reboot. Only that
# process receives it, so nothing is typed into a pane and no other instance of the session can pick it up.
WAKE_TEXT = ("[HarnessGram: The Mac restarted (or the terminal server crashed) and the bridge reopened this "
             "session. If you were in the middle of a task, continue it now from where it stopped: check your "
             "handoff file, the owner ledger and any agents or background jobs that died with the restart, and "
             "restart what is needed. Do not redo finished steps. If nothing was in progress, do nothing and do "
             "not reply.]")

# Set once this boot's revive has finished (or was not needed): from then on an empty list is recorded
# too, so sessions closed on purpose do not come back at the next boot.
revived = threading.Event()
reviving = threading.Event()  # a reopen after a tmux crash is running


def boot_time() -> Optional[float]:
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"sec\s*=\s*(\d+)", out)
    return float(m.group(1)) if m else None


def open_windows() -> Optional[List[str]]:
    """Names of the open tmux sessions; None when tmux can't be asked."""
    try:
        from .inbox import _tmux
        result = _tmux("ls", "-F", "#{session_name}")
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return []  # no server: nothing open
    return [n.strip() for n in result.stdout.splitlines() if n.strip()]


def closed_on_purpose(session: str, now: Optional[float] = None) -> None:
    """Called from the SessionEnd hook when the owner closed the session himself."""
    path = state_dir() / CLOSED
    now = now or time.time()
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    if not isinstance(old, dict):
        old = {}
    keep = {k: v for k, v in old.items() if isinstance(v, (int, float)) and now - v < FRESH}
    keep[session] = now
    write_private(path, json.dumps(keep))


def _closed_recently(now: float) -> set:
    try:
        old = json.loads((state_dir() / CLOSED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(old, dict):
        return set()
    return {k for k, v in old.items() if isinstance(v, (int, float)) and now - v < FRESH}


def recorded_sessions() -> set:
    """The sessions in the open-windows record: the ones a revive may still bring back."""
    try:
        rec = json.loads((state_dir() / RECORD).read_text(encoding="utf-8"))
        return {s for s in rec.get("sessions") or [] if isinstance(s, str)}
    except (OSError, ValueError, AttributeError):
        return set()


def server_start() -> Optional[str]:
    """When the running tmux server started; "" when there is none, None when tmux can't be asked."""
    try:
        from .inbox import _tmux
        result = _tmux("display-message", "-p", "#{start_time}")
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else ""


def sessions_of(windows: Iterable[str], known: Iterable[str]) -> List[str]:
    prefixes = {w[3:] for w in windows if w.startswith("tg-") and len(w) > 3}
    return sorted(s for s in known if s[:8] in prefixes)


def _known() -> List[str]:
    return list(TopicMap(state_dir() / "topics.json", None, 0).all())


def _pending(rec: dict, live: List[str], closed: set) -> dict:
    retry = rec.get("retry")
    if not isinstance(retry, dict):
        return {}
    return {s: attempt for s, attempt in retry.items()
            if s in (rec.get("sessions") or []) and s not in live and s not in closed
            and isinstance(attempt, dict) and isinstance(attempt.get("attempts"), int)
            and isinstance(attempt.get("t"), (int, float))}


def snapshot(now: Optional[float] = None, resume: Optional[Callable[[str], bool]] = None,
             background: bool = True) -> None:
    """Record live and pending sessions; preserve an empty pre-boot view until revived is set."""
    windows = open_windows()
    if windows is None or reviving.is_set():
        return
    live = sessions_of(windows, _known()) if windows else []
    if not live and not revived.is_set():
        return
    server = server_start()
    if server is None:
        return
    path = state_dir() / RECORD
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    if not isinstance(old, dict):
        old = {}
    t = now or time.time()
    closed = _closed_recently(t)
    live = [s for s in live if s not in closed]
    retry = _pending(old, live, closed)
    lost = [s for s in old.get("sessions") or []
            if isinstance(s, str) and s not in live and s not in closed and s not in retry]
    crash_t = old.get("crash_t", 0) if isinstance(old.get("crash_t"), (int, float)) else 0
    if old.get("server") and server != old["server"] and len(lost) >= CRASH_MIN \
            and t - old.get("t", 0) < FRESH:
        if not crash_t or t - crash_t >= CRASH_EVERY:
            _revive_crash(old, lost, resume, background, t, record_time=now)
            return
        # Throttling delays a new crash recovery; it must not erase the sessions it protects.
        retry.update({s: {"attempts": 0, "t": t, "after": crash_t + CRASH_EVERY} for s in lost})
    due = [s for s, attempt in retry.items()
           if attempt["attempts"] < RETRY_MAX and t - attempt["t"] >= RETRY_EVERY
           and t >= attempt.get("after", 0)]
    if due:
        # Use the same exclusion as crash recovery: snapshots cannot overwrite an in-flight retry.
        _revive_crash({**old, "sessions": sorted(set(live) | set(retry)), "retry": retry}, due, resume,
                      background, t, crash=any("after" in retry[s] for s in due), record_time=now)
        return
    sessions = sorted(set(live) | set(retry))
    if old.get("sessions") == sessions and old.get("server") == server \
            and old.get("retry", {}) == retry and t - old.get("t", 0) < 300:
        return
    rec = {"t": t, "sessions": sessions, "server": server}
    if retry:
        rec["retry"] = retry
    if crash_t:
        rec["crash_t"] = crash_t
    write_private(path, json.dumps(rec))


def _recover(rec: dict, todo: List[str], resume: Optional[Callable[[str], bool]],
             now: Optional[float] = None, gap: float = 5.0,
             sleep_fn: Optional[Callable[[float], None]] = None) -> List[str]:
    """One attempt, then bind the entire recovery record to the current server, even on failure."""
    closed = set()
    try:
        return reopen(todo, resume, gap, sleep_fn, rounds=1, closed=closed)
    finally:
        t = time.time() if now is None else now
        closed.update(_closed_recently(t))
        windows = open_windows()
        live = sessions_of(windows, _known()) if windows else []
        sessions = sorted((set(s for s in rec.get("sessions") or [] if isinstance(s, str))
                           | set(live)) - closed)
        retry = _pending(rec, live, closed)
        for sid in sessions:
            if sid not in live and (sid in todo or sid not in retry):
                attempt = retry.get(sid, {"attempts": 0, "t": t})
                retry[sid] = {"attempts": attempt["attempts"] + (sid in todo),
                              "t": t if sid in todo else attempt["t"]}
        # An unavailable server must not leave the pre-boot identity behind. The empty identity
        # also covers an all-failed reopen with no tmux server yet.
        record = {"t": t, "sessions": sessions, "server": server_start() or ""}
        if retry:
            record["retry"] = retry
        if rec.get("crash_t"):
            record["crash_t"] = rec["crash_t"]
        write_private(state_dir() / RECORD, json.dumps(record))


def _revive_crash(rec: dict, lost: List[str], resume: Optional[Callable[[str], bool]], background: bool,
                  now: float, crash: bool = True, record_time: Optional[float] = None) -> None:
    """Recover crash losses or retry pending sessions without discarding failed reopens."""
    reviving.set()
    if crash:
        rec = {**rec, "crash_t": now}

    def run():
        try:
            _recover(rec, lost, resume, record_time)
        except Exception:
            pass
        finally:
            reviving.clear()  # only after the write: a snapshot in between would see the old server
    if background:
        threading.Thread(target=run, daemon=True).start()
    else:
        run()


def revive_after_boot(now: Optional[float] = None, resume: Optional[Callable[[str], bool]] = None,
                      boot: Optional[float] = None, gap: float = 5.0,
                      sleep_fn: Callable[[float], None] = time.sleep) -> List[str]:
    """Reopen the sessions that were open before the boot. Returns those reopened."""
    t = time.time() if now is None else now
    boot = boot_time() if boot is None else boot
    if not boot or t - boot > BOOT_WINDOW:
        return []
    try:
        rec = json.loads((state_dir() / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(rec, dict) or not boot - FRESH < rec.get("t", 0) < boot:
        return []
    closed = _closed_recently(t)
    todo = [s for s in rec.get("sessions") or [] if isinstance(s, str) and s not in closed]
    # Boot recovery is independent of crash throttling and resets exhausted retry budgets.
    rec.pop("retry", None)
    reviving.set()
    try:
        return _recover(rec, todo, resume, now, gap, sleep_fn)
    finally:
        reviving.clear()


def reopen(todo: List[str], resume: Optional[Callable[[str], bool]] = None, gap: float = 5.0,
           sleep_fn: Optional[Callable[[float], None]] = None, *, rounds: int = 3,
           closed: Optional[set] = None) -> List[str]:
    """Reopen these sessions one by one, skipping open ones; returns those reopened. The real reopen starts
    each session with WAKE_TEXT as its first prompt; an injected `resume` (tests) gets none."""
    sleep_fn = sleep_fn or time.sleep
    closed = closed if closed is not None else set()
    if resume is None:
        from .inbox import resume_session

        def resume(sid: str) -> bool:
            return resume_session(sid, first_prompt=WAKE_TEXT)  # the carry-on prompt starts with the process
    done: List[str] = []
    for round_ in range(rounds):
        windows = set(open_windows() or [])
        todo = [s for s in todo if "tg-" + s[:8] not in windows]
        failed = []
        for sid in todo:
            # Keep every observed close for this recovery, even after its FRESH marker expires.
            closed.update(_closed_recently(time.time()))
            if sid in closed:
                continue
            from . import credit_mode
            if credit_mode.blocked(sid):
                continue  # credit recovery belongs to the heartbeat, never an automatic paid prompt
            try:
                ok = resume(sid)
            except Exception:
                ok = False
            (done if ok else failed).append(sid)
            sleep_fn(gap)
        todo = failed
        if not todo or round_ == rounds - 1:
            break
        sleep_fn(RETRY_EVERY)
    return done


def revive_in_background() -> threading.Thread:
    """revive_after_boot off the heartbeat's loop; `revived` is set when it is done."""
    def run():
        try:
            revive_after_boot()
        except Exception:
            pass
        finally:
            revived.set()
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th
