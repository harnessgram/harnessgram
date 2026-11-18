"""Adopt installed radio code at a quiet boundary, once per installed build.

The heartbeat owns this repair; a live YouTube job/broadcast or a private item always waits.
The persisted claim precedes launchctl, so failed starts and heartbeat restarts cannot form a loop.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Callable, Optional

from . import buildstamp, radio, radio_podcast, radio_room, radio_youtube
from .config import Config, locked_map

COOLDOWN = 15 * 60.0
ATTEMPT = "upgrade-restart.json"


def _private_item(cfg: Config, now: float) -> bool:
    # A paused/downloading episode still owns its place; an expired audible marker alone is insufficient.
    return (radio.private_audio_on(now) or radio_room.live() or
            (radio_podcast.view(cfg, now=now).get("now") or {}).get("state") in
            ("playing", "paused", "downloading"))


def _claim(stamp: str, now: float) -> bool:
    with locked_map(radio.folder() / ATTEMPT) as saved:
        if saved.get("stamp") == stamp:
            return False  # one attempt for this installed build, including a failed attempt
        at = saved.get("at")
        if type(at) in (int, float) and now - at < COOLDOWN:
            return False
        saved.clear()
        saved.update(stamp=stamp, at=now)
    return True


def check(cfg: Config, now: Optional[float] = None, runner: Optional[Callable] = None) -> bool:
    """True only after a successful kickstart. Unknown state fails closed; never starts an absent radio.
    `runner` is the subprocess.run-shaped seam used by offline tests (no shell, bounded runtime).
    """
    if sys.platform != "darwin" or not cfg.radio:
        return False
    try:
        with radio_youtube.try_control_lock() as held:
            if not held:
                return False  # a mirror start/stop is in progress: wait for a later round
            stamp = buildstamp.read()
            if not stamp or "radio" not in buildstamp.stale(current=stamp):
                return False
            mirror = radio_youtube.status(cfg)  # exactly the offline `radio youtube status` flags
            # Read the clock only after every slow check: a marker written meanwhile must still count as audible.
            at = time.time() if now is None else now
            if mirror.get("on") is not False or mirror.get("broadcast") is not False or _private_item(cfg, at):
                return False
            if not _claim(stamp, at):
                return False
            # Still under the start/stop lock, so the mirror cannot go live before launchd acts.
            try:
                done = (runner or subprocess.run)(
                    ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{radio.LABEL}"],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
                ok = done.returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
    except Exception:
        return False  # an optional repair cannot interrupt replies or take a doubtful broadcast down
    radio.log("upgrade: restarted radio on installed code" if ok else
              "upgrade: radio restart failed; run doctor")
    return ok
