"""Catch a final reply whose Stop worker never delivered it.

The Stop hook starts a detached `ctb finish-stop` worker. While the package is being reinstalled the hook
or the worker can fail before ctb even imports (the console script is missing for a few seconds, or a
module is half written), so nothing is logged, nothing is retried and the turn's last answer never
reaches the topic (seen twice, both during an install).

The heartbeat calls `sweep` about once a minute. It redelivers only a turn that never started at all:
its turn-end record (`stop_hook_summary`) is older than SETTLE and newer than WINDOW, its last reply block
is not in the delivery ledger, and no sender ever claimed it (hooks.take_claim, written under the session's
stop lock before any send). The sweep takes that claim itself, under the same lock, and starts the Stop
worker with the claim's token; a live worker that starts later finds the claim and hands off. A turn that
was claimed but never completed is never sent again (a part may be out): it is logged and gets one short
"the last answer may be incomplete" notice. Only sessions the bridge has mirrored before (a ledger file
exists) are looked at. Logs never carry reply text.
"""
from __future__ import annotations

import glob
import json
import os
import time
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from .config import state_dir

SETTLE = 120.0  # seconds after the turn ended (or was claimed): its worker had its chance
NOTICE_TRIES = 3   # sweeps that may try the one 'answer may be incomplete' notice
WINDOW = 3 * 3600.0  # older turns are left alone
EVERY = 60.0  # seconds between sweeps
TAIL = 2_000_000  # bytes of each transcript read for the check
LAST_TURNS = 3  # the newest turn ends looked at per session
INCOMPLETE = "The last answer may be incomplete; the full reply is in the app."
_LAST = [0.0]


def _when(stamp) -> Optional[float]:
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _tail_rows(path: str) -> List[Tuple[int, str]]:
    """(absolute byte offset, raw line) of the transcript's complete lines in its last TAIL bytes."""
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        start = max(0, size - TAIL)
        f.seek(start)
        data = f.read(size - start)
    pos = start
    if start:
        cut = data.find(b"\n")
        if cut < 0:
            return []
        pos += cut + 1
        data = data[cut + 1:]
    rows = []
    for raw in data.split(b"\n")[:-1]:  # the last piece is empty or a line still being written
        if raw:
            rows.append((pos, raw.decode("utf-8", errors="replace") + "\n"))
        pos += len(raw) + 1
    return rows


def _turn_ends(rows: List[Tuple[int, str]]) -> List[Tuple[int, int, float]]:
    """(row index, byte offset, end time) of the turn-end records, newest first."""
    ends = []
    for i, (offset, raw) in enumerate(rows):
        if '"stop_hook_summary"' not in raw:
            continue
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "system" and ev.get("subtype") == "stop_hook_summary":
            at = _when(ev.get("timestamp"))
            if at is not None:
                ends.append((i, offset, at))
    return ends[::-1][:LAST_TURNS]


def find_missed(path: str, session: str, now: float) -> List[dict]:
    """The Stop worker input for each recent turn of this transcript whose final reply is undelivered."""
    from . import hooks
    seen = hooks._seen(session)
    rows = _tail_rows(path)
    lines = [raw for _, raw in rows]
    found = []
    for i, offset, at in _turn_ends(rows):
        if not SETTLE <= now - at <= WINDOW:
            continue
        blocks = hooks.read_blocks(lines[:i])
        if not blocks or blocks[-1][0] in seen or blocks[-1][0].split(":")[0].isdigit():
            continue  # delivered, or a row without a uuid (its key would differ in the worker's view)
        data = {"session_id": session, "transcript_path": path, "_offset": offset, "_at": at,
                "_answer_to": None, "_radio_mark": None, "_swept": True}
        try:
            cwd = json.loads(lines[i]).get("cwd")
            if isinstance(cwd, str):
                data["cwd"] = cwd
        except (ValueError, AttributeError):
            pass
        found.append((blocks[-1][0], data))
    return [dict(d, _key=k) for k, d in found]


def _on(cfg) -> bool:
    """The `missed_replies` add-on (addons.py); on when the registry cannot be read."""
    try:
        from . import addons
        return addons.enabled(cfg, "missed_replies")
    except Exception:
        return True


def sweep(cfg, now: Optional[float] = None, spawn_fn: Optional[Callable] = None,
          root: Optional[str] = None, force: bool = False, notify: Optional[Callable] = None) -> int:
    """Start the Stop worker for each recent turn that no sender ever claimed. Returns how many."""
    from . import hooks, mirror
    from .status import projects_dir
    now = time.time() if now is None else now
    if not force and now - _LAST[0] < EVERY:
        return 0
    _LAST[0] = now
    if not cfg.ready or not cfg.mirror or not _on(cfg):
        return 0
    spawn_fn = spawn_fn or hooks.spawn
    notify = notify or (lambda session: mirror.alert(cfg, session, INCOMPLETE))
    ledgers = state_dir() / "mirrored"
    started = 0
    pattern = os.path.join(glob.escape(str(root or projects_dir())), "*", "*.jsonl")
    for path in glob.glob(pattern):
        try:
            if now - os.path.getmtime(path) > WINDOW:
                continue
            session = os.path.basename(path)[:-len(".jsonl")]
            if not (ledgers / hooks._safe(session)).exists():
                continue  # never mirrored by this bridge
            for data in find_missed(path, session, now):
                turn = data.pop("_key")
                launch, tell = None, False
                with hooks.stop_lock(session, blocking=False) as held:
                    if not held or turn in hooks._seen(session):
                        continue  # a worker is on it, or it was just delivered
                    claim = hooks.read_claim(session, turn)
                    if claim is None:
                        data["_claim"] = hooks.take_claim(session, turn, now=now)
                        launch = data
                    elif (not claim.get("done") and not claim.get("noticed")
                          and int(claim.get("notice_tries") or 0) < NOTICE_TRIES
                          and now - float(claim.get("at") or 0) >= SETTLE):
                        # Counted before the attempt (never a flood); marked noticed only once it was sent, so a
                        # notice that failed mid-install is tried again by a later sweep.
                        hooks.update_claim(session, turn, notice_tries=int(claim.get("notice_tries") or 0) + 1)
                        tell = True
                if launch is not None:
                    mirror.log(f"missed final reply found {int(now - launch['_at'])}s after the turn ended; "
                               "delivering it")
                    try:
                        spawn_fn(["finish-stop"], text=json.dumps(launch, ensure_ascii=False))
                    except Exception as exc:
                        # Nothing was sent: give the claim back so a later sweep tries again.
                        with hooks.stop_lock(session):
                            claim = hooks.read_claim(session, turn) or {}
                            if (claim.get("token") == launch.get("_claim") and not claim.get("accepted")
                                    and not claim.get("done")):
                                hooks.drop_claim(session, turn)
                        mirror.log(f"missed reply worker could not start ({type(exc).__name__}); will retry")
                        continue
                    started += 1
                elif tell:
                    if notify(session) is False:  # neither sent nor queued: a later sweep tries again
                        mirror.log("incomplete-answer notice not sent; will retry")
                        continue
                    with hooks.stop_lock(session):
                        hooks.update_claim(session, turn, noticed=True)
                    mirror.log("final reply was claimed but never completed; not resent, owner notified")
        except Exception as exc:  # one damaged transcript never stops the sweep
            mirror.log(f"missed reply check failed ({type(exc).__name__})")
    return started
