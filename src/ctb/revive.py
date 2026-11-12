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
CLOSED = "closed-sessions.json"  # sessions the owner closed (/exit, logout): never reopened as crash losses

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


def snapshot(now: Optional[float] = None, resume: Optional[Callable[[str], bool]] = None,
             background: bool = True) -> None:
    """Record the open session windows. An empty list is never recorded: right after a boot the
    record must still hold what was open before it."""
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
    lost = [s for s in old.get("sessions") or [] if isinstance(s, str) and s not in live and s not in closed]
    crash_t = old.get("crash_t", 0) if isinstance(old.get("crash_t"), (int, float)) else 0
    if old.get("server") and server != old["server"] and len(lost) >= CRASH_MIN \
            and t - old.get("t", 0) < FRESH and (not crash_t or t - crash_t >= CRASH_EVERY):
        _revive_crash(lost, resume, background, t)
        return
    if old.get("sessions") == live and old.get("server") == server and t - old.get("t", 0) < 300:
        return
    rec = {"t": t, "sessions": live, "server": server}
    if crash_t:
        rec["crash_t"] = crash_t
    write_private(path, json.dumps(rec))


def _revive_crash(lost: List[str], resume: Optional[Callable[[str], bool]], background: bool,
                  crash_t: float) -> None:
    """Reopen sessions lost with the tmux server, then record what is open under the new one."""
    reviving.set()

    def run():
        try:
            reopen(lost, resume)
        except Exception:
            pass
        finally:
            try:
                server = server_start()
                windows = open_windows()
                if server is not None and windows is not None:
                    write_private(state_dir() / RECORD, json.dumps(
                        {"t": time.time(), "sessions": sessions_of(windows, _known()), "server": server,
                         "crash_t": crash_t}))
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
    now = time.time() if now is None else now
    boot = boot_time() if boot is None else boot
    if not boot or now - boot > BOOT_WINDOW:
        return []
    try:
        rec = json.loads((state_dir() / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(rec, dict) or not boot - FRESH < rec.get("t", 0) < boot:
        return []
    return reopen([s for s in rec.get("sessions") or [] if isinstance(s, str)], resume, gap, sleep_fn)


def reopen(todo: List[str], resume: Optional[Callable[[str], bool]] = None, gap: float = 5.0,
           sleep_fn: Optional[Callable[[float], None]] = None) -> List[str]:
    """Reopen these sessions one by one, skipping open ones; returns those reopened."""
    sleep_fn = sleep_fn or time.sleep
    if resume is None:
        from .inbox import resume_session as resume
    done: List[str] = []
    for round_ in range(3):  # right after boot tmux or the Keychain may not be ready yet
        windows = set(open_windows() or [])
        todo = [s for s in todo if "tg-" + s[:8] not in windows]
        failed = []
        for sid in todo:
            try:
                ok = resume(sid)
            except Exception:
                ok = False
            (done if ok else failed).append(sid)
            sleep_fn(gap)
        todo = failed
        if not todo:
            break
        sleep_fn(60)
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
