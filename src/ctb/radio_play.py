"""Start the radio when someone presses Play in the private listen page.

Before: the owner switched the radio on (Cockpit, `/radio on`, `ctb radio start`) before the Safari player worked.
Now: pressing Play is the detection. An authenticated playlist request of the private listen link (listen_link.py)
that finds no fresh audio starts the radio in private audio-only mode, the same code path as `ctb radio start`
(radio.start with play=True), and the playlist answer is a live playlist of short silent segments (radio_quiet.py,
no speech, nothing paid) that slides with the clock until the first real segments exist. The first real playlists are
then rewritten with an EXT-X-DISCONTINUITY so AVPlayer and Safari switch over cleanly.

What this path never does:
- open, end or touch a group live stream (the account helper is not run; a start here is always private-only);
- start or stop the YouTube mirror edition (no home-sync; a station the mirror depends on is never stopped here);
- start when mirroring is off, the daily paid-speech cap is spent, the add-on or the player is off, or a start was
  already tried in the last RATE seconds;
- log or store the token or the URL (the markers hold only timestamps).

A station Play started carries the marker `play-started.json`; presence is the last authenticated media fetch
(radio_listeners.player_fetch). With no fetch for `play_idle_minutes` (default 10) the station stops itself. A station
started any other way has no marker and is never stopped here; starting the radio by hand while a Play-started one runs
removes the marker (the owner has taken it over).
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from .config import locked_map, read_map, write_private

STARTED = "play-started.json"   # {"at": when Play started the station}; present only for a Play-started station
ATTEMPT = "play-attempt.json"   # {"at": when a start was last attempted}, for the rate limit
QUIET = "play-quiet.json"       # {"at": when silent segments were last served, "top": the highest number served}
INTENT = "play-intent.json"     # {"at": when a Play request asked for a start}; the start checks it again
STOPPED = "play-stopped.json"   # {"at": when the radio was last stopped, by anyone}
INTENT_FRESH = 180.0            # a queued start older than this is dropped
BRIDGE = "play-bridge.json"     # {"first", "mine"}: the first real segment after the silence (radio_quiet.bridge)
BRIDGE_WINDOW = 120.0           # real playlists get the discontinuity if silence was served this recently
RATE = 60.0                     # seconds between start attempts
WARM = 120.0                    # seconds after a Play start during which the "starting" answer is given
CHECK_EVERY = 15.0              # seconds between the station's idle checks
IDLE_MIN, IDLE_MAX = 1, 240     # allowed range of `play_idle_minutes`

OFF, STARTING = "off", "starting"


def _folder() -> Path:
    from . import radio
    return radio.folder()


def _num(value) -> Optional[float]:
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def started_at() -> Optional[float]:
    """When Play started the running station, else None. Read-only; creates no folder."""
    from .config import state_dir
    return _num(read_map(state_dir() / "radio" / STARTED).get("at"))


def started() -> bool:
    return started_at() is not None


def mark(now: Optional[float] = None) -> None:
    write_private(_folder() / STARTED, json.dumps({"at": time.time() if now is None else now}))


def forget(quiet: bool = True) -> None:
    """The station is no longer a Play-started one (stopped, given up, taken over by an owner start). A new run starts
    its audio timeline afresh, so the bridge to the old one is forgotten too. The note that silence was served
    (QUIET) belongs to the listener who is waiting for the run that is just starting: a start keeps it
    (`quiet=False`), only the end of a run clears it."""
    for name in (STARTED, BRIDGE) + ((QUIET,) if quiet else ()):
        try:
            (_folder() / name).unlink()
        except OSError:
            pass


def release() -> None:
    """The owner took over a running Play-started station: drop only the Play mark (no lock: the caller holds the
    radio control lock, and the idle stop re-checks this mark under that same lock)."""
    try:
        (_folder() / STARTED).unlink()
    except OSError:
        pass


def adopt() -> None:
    """Something else now depends on the running station (the YouTube mirror was started): it is not Play's to stop."""
    from . import radio_listeners
    try:
        with radio_listeners.presence_lock(_folder()):
            (_folder() / STARTED).unlink()
    except OSError:
        pass


def note_stop(now: Optional[float] = None) -> None:
    """The radio was stopped (by anyone): a Play start that was queued before this is not carried out."""
    try:
        write_private(_folder() / STOPPED, json.dumps({"at": time.time() if now is None else now}))
    except OSError:
        pass


def clear_intent() -> None:
    try:
        (_folder() / INTENT).unlink()
    except OSError:
        pass


def still_wanted(cfg, s: dict, now: Optional[float] = None) -> bool:
    """The queued Play start, checked again under the radio's control lock just before it acts: the Play request is
    recent, nothing stopped the radio since, the add-on is still on with a live listening link, the switch and the
    mirroring switch are on, and today's cap is not spent."""
    from . import addons, listen_link, policy
    now = time.time() if now is None else now
    asked = _num(read_map(_folder() / INTENT).get("at"))
    if asked is None or not 0 <= now - asked <= INTENT_FRESH:
        return False
    stopped = _num(read_map(_folder() / STOPPED).get("at"))
    if stopped is not None and stopped >= asked:
        return False
    try:
        return bool(s.get("play_start") and cfg.radio and policy.on() and addons.enabled(cfg, "listen_link")
                    and listen_link.active() > 0 and not caps_spent(s, now))
    except Exception:
        return False


def quiet_playlist(now: Optional[float] = None) -> bytes:
    """The answer while the radio is starting: a live playlist of silent segments. Remembers that it was served, so
    the first real playlists that follow can be bridged."""
    from . import radio_quiet
    now = time.time() if now is None else now
    body = radio_quiet.playlist(now)
    top = int(now // radio_quiet.SEGMENT_SECONDS)
    old = read_map(_folder() / QUIET)
    seen = old.get("top") if type(old.get("top")) is int else 0
    try:
        write_private(_folder() / QUIET, json.dumps({"at": now, "top": max(top, seen)}))
    except OSError:
        pass
    return body


def bridge_playlist(text: str, now: Optional[float] = None) -> str:
    """A real playlist for the listen link. If silence was served just before (or the bridge is already running for
    this run), the first real segment gets an EXT-X-DISCONTINUITY; otherwise the playlist is returned unchanged."""
    from . import radio_quiet
    now = time.time() if now is None else now
    record = read_map(_folder() / BRIDGE)
    quiet = _num(read_map(_folder() / QUIET).get("at"))
    if not record.get("first") and not (quiet is not None and 0 <= now - quiet <= BRIDGE_WINDOW):
        return text
    top = read_map(_folder() / QUIET).get("top")
    # Choosing the offset is read-compute-write: two first requests at once must agree on one offset, so the choice
    # is made under the bridge record's own lock and the second request sees the first one's record.
    try:
        with locked_map(_folder() / BRIDGE) as data:
            out, new = radio_quiet.bridge(text, dict(data) or None, top if type(top) is int else None)
            if new and new != data:
                data.clear()
                data.update(new)
    except OSError:
        out, _ = radio_quiet.bridge(text, record or None, top if type(top) is int else None)
    return out


def idle_minutes(s: dict) -> int:
    value = s.get("play_idle_minutes")
    value = value if type(value) is int else 10
    return max(IDLE_MIN, min(IDLE_MAX, value))


def mirror_in_use(cfg) -> bool:
    """The YouTube mirror edition follows the home radio's audio under a standing owner start. While that holds the
    station is not Play's to stop. Any doubt answers yes."""
    try:
        from . import radio_youtube
        return radio_youtube.settings(cfg)["edition"] == "mirror" and radio_youtube.mirror_started()
    except Exception:
        return True


def idle_due(cfg, s: dict, now: Optional[float] = None) -> bool:
    """A Play-started station with no authenticated fetch for `play_idle_minutes`. False for every other station."""
    now = time.time() if now is None else now
    at = started_at()
    if at is None:
        return False
    last = at
    heard = _num(read_map(_folder() / "player-listener.json").get("at"))
    if heard is not None and heard > last:
        last = heard
    return now - last >= idle_minutes(s) * 60 and not mirror_in_use(cfg)


def caps_spent(s: dict, now: Optional[float] = None) -> bool:
    """The daily paid-speech cap is used up (the cap that applies once someone listens). A cap of 0 means paid speech
    is switched off, which costs nothing, so it never blocks. An unreadable ledger counts as spent."""
    from . import radio_listeners, radio_voice
    cap = radio_listeners.voice_settings(s, {"listening_detection": True, "listening": True})["tts_daily_minutes"]
    if cap <= 0:
        return False
    return radio_voice.paid_usage(_folder()).today(now) >= cap * 60


def _claim_attempt(now: float) -> bool:
    """At most one start attempt per RATE seconds, across processes."""
    with locked_map(_folder() / ATTEMPT) as rec:
        last = _num(rec.get("at"))
        if last is not None and 0 <= now - last < RATE:
            return False
        rec["at"] = now
        return True


def _warming(now: float) -> bool:
    at = started_at()
    return at is not None and 0 <= now - at < WARM


def _spawn_start() -> None:
    from . import radio
    radio.spawn("start", play=True)


def request(cfg, now: Optional[float] = None, spawn: Optional[Callable[[], None]] = None) -> str:
    """The listen link asked for the playlist and there is no fresh audio. Returns STARTING (answer with the silent
    live playlist, a start is under way or was just made) or OFF (the existing answer: not found). The caller has
    already checked the token, the add-on and that the radio player is allowed."""
    from . import policy, radio
    now = time.time() if now is None else now
    try:
        s = radio.settings(cfg)
        if sys.platform != "darwin" or not s["play_start"] or not policy.on():
            return OFF
        if radio.job_pid():   # a station is up but has no fresh audio yet: only a Play-started one is warming up
            return STARTING if _warming(now) else OFF
        pending = radio.operation(now)["pending"]
        if pending:           # a start or stop is already in progress (a button, a command or an earlier Play)
            return STARTING if pending == "start" else OFF
        if radio.wanted() or caps_spent(s, now):
            return OFF        # the owner's radio is wanted (it resumes by itself), or no paid speech is left today
        if not _claim_attempt(now):
            return STARTING   # tried within the last RATE seconds: it is on its way, do not try again
        write_private(_folder() / INTENT, json.dumps({"at": now}))   # the queued start checks this again
        (spawn or _spawn_start)()
        radio.log("play: start requested")
        return STARTING
    except Exception:
        return OFF            # whatever goes wrong the answer is the old one; nothing here may break playback


def watch(cfg, quit_event, stop: Optional[Callable[[], None]] = None, clock: Callable[[], float] = time.time) -> None:
    """The station's idle loop (private mode only): while the station is a Play-started one and nobody has fetched
    audio for `play_idle_minutes`, ask for a Play stop. The stop itself re-checks under the radio's control lock."""
    from . import radio
    ask = stop or (lambda: radio.spawn("stop", play=True))
    asked = 0.0
    while not quit_event.wait(CHECK_EVERY):
        if not started():
            return
        now = clock()
        try:
            due = idle_due(cfg, radio.settings(cfg), now)
        except Exception:
            continue
        if due and now - asked >= 120.0:   # a stop that did not happen is asked for again, not in a tight loop
            asked = now
            ask()
