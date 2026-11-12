"""Team radio: a Telegram live stream in the home group that sounds like a radio.

Short narrated updates on what moved forward, what waits for the owner and what needs his action; a quiet
background between them; the picture is a live summary card. macOS only (launchd, the card renderer, `say`).

    ctb radio start | stop | status [--live] | say "<text>"

`start` opens the group's RTMP live stream (through the owner's account, see radio_call.py; it refuses when
someone else's stream or voice chat is already live, and only ever ends the stream it opened itself), then
loads a launchd job (com.claude-telegram-bridge.radio) that runs `ctb radio run`: one ffmpeg process encodes the
card and a continuous audio feed to FLV, and radio_rtmp.Publisher sends it to Telegram. The stream URL and key
are read from the configured commands into this process's memory only: never on a command line, in a file or a
log. `stop` unloads the job, stops ffmpeg and ends the live stream. Nothing keeps running while the radio is off.

Configuration: the "radio" object in config.json (see docs/radio.md).
"""
from __future__ import annotations

import array
import collections
import fcntl
import hashlib
import json
import os
import plistlib
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import launch, radio_card, radio_post, radio_script, radio_talk, radio_voice
from .config import Config, HOME, home_chat, private_dir, set_setting, setting_on, state_dir, write_private
from .i18n import t

LABEL = "com.claude-telegram-bridge.radio"
CALL_BUSY = "the group already has another live stream or voice chat"  # radio_call.BUSY
RATE = radio_voice.RATE
FRAME = 960                 # samples per 20 ms
FPS = 2                     # card frames per second
OUT_FPS = 15                # frames per second the stream carries: Telegram players stutter on very low rates
LEAD = 0.3                  # seconds of audio kept ahead of real time
POLL = 30                   # seconds between looks at the team's state
QUOTA_EVERY = 300           # seconds between runs of the quota command
MARKER = "ctb-radio"        # in ffmpeg's metadata, so a leftover encoder is recognisable
MAX_SAY = 600
MAX_FAILURES = 5            # pipeline failures within FAILURE_WINDOW before the radio gives up
FAILURE_WINDOW = 600
WANTED = "wanted.json"      # the owner's intent: the home radio should be on (see wanted(), resume_after_boot)
RESUME_DELAY = 20           # seconds after the listener started before the radio is resumed (network and logins are up)
RESUME_BACKOFF = (0, 60, 180, 600)  # waits before each resume attempt; one attempt per entry
MAX_RESUME = len(RESUME_BACKOFF)    # failed resume attempts within RESUME_WINDOW before the radio gives up
RESUME_WINDOW = 3600
LATE_AFTER = 0.25           # seconds the audio feed may fall behind real time before the pipeline logs it
DEFAULTS = {
    "every_minutes": 20, "news_minutes": 3, "tts_daily_minutes": 25, "tts_model": "gemini-2.5-flash-tts", "voice": "Sadaltager",
    "style": "", "say_voice": "", "bed": "off", "bed_db": -30, "video_encoder": "", "title": "",
    "telethon_python": "", "account_api_command": [], "account_session_command": [],
    "rtmp_url_command": [], "rtmp_key_command": [], "ledger_dir": "", "quota_command": [],
    "rewrite_command": [], "project_names": {}, "path": [], "motion_modules": "", "chrome_path": "",
    # the brainstorming room (radio_room.py): its own paid-voice minutes per day (at most
    # radio_room.ROOM_TTS_CEILING_MINUTES, counted apart from tts_daily_minutes) and an optional suitability rule file
    "room_tts_daily_minutes": 15, "room_suitability_file": "",
}


BOARD_ON = "radio-board-on"  # the flight board instead of the card (a runtime switch: the control panel's radio settings)
BOARD_RETRY = 600            # seconds before the board is tried again after it failed (the card is shown meanwhile)
MODE_CHANGED = "the picture changed"
BOARD_BITRATE = ("600k", "900k", "1800k")  # the moving board needs more than the still card's 250k


def board_on() -> bool:
    """The flight board is the picture (radio_board / radio_show); off (the default): the summary card."""
    return setting_on(BOARD_ON)


def set_board(on: bool) -> None:
    set_setting(BOARD_ON, on)


TRANSLATE_RETRY = 600       # seconds before an item whose translation failed is tried again
PHRASE_TIMEOUT = 12.0       # seconds for the whole localize step (phrasing and any translation fallback together)
POOL_SIZE = 4               # background calls in flight at once, for the whole station
from .i18n import RADIO_PHRASE_PROMPT as PHRASE_PROMPT
SAY_MAX = 120
_URL = re.compile(r"https?://|www\.|\w+\.\w{2,4}/", re.I)


class Workers:
    """The station's bounded background pool for phrasing and translation calls: POOL_SIZE daemon threads, started
    once. `submit` never creates a thread: it returns None when every worker is taken (earlier calls may still be
    hung), and the caller skips that work for the update."""

    def __init__(self, size: int = POOL_SIZE):
        self.size, self.busy, self.started = size, 0, False
        self.lock = threading.Lock()
        self.jobs: "queue.SimpleQueue" = queue.SimpleQueue()

    def _run(self) -> None:
        while True:
            fn, args, fut = self.jobs.get()
            try:
                fut.set_result(fn(*args))
            except BaseException as e:  # noqa: BLE001 - the caller sees it as a failed call
                fut.set_exception(e)
            finally:
                with self.lock:
                    self.busy -= 1

    def submit(self, fn: Callable, *args) -> Optional[Future]:
        with self.lock:
            if self.busy >= self.size:
                return None
            self.busy += 1
            if not self.started:
                self.started = True
                for n in range(self.size):
                    threading.Thread(target=self._run, name=f"radio-worker-{n}", daemon=True).start()
        fut: Future = Future()
        self.jobs.put((fn, args, fut))
        return fut

    def free(self) -> int:
        with self.lock:
            return self.size - self.busy


WORKERS = Workers()


def valid_say(text: object) -> str:
    """A phrased line the voice may read: Hebrew enough (85 %), short, whole (no ellipsis), no link; else ""."""
    if not isinstance(text, str):
        return ""
    out = radio_script.spoken(text)
    if (not out or len(out) > SAY_MAX or "…" in text or "..." in out or _URL.search(text)
            or not radio_script.hebrew_enough(out, 0.85)):
        return ""
    return out


def phrase_one(row: dict, ask: Callable[[str], str]) -> str:
    """The Hebrew line for ONE item ({id, project, text}), in its own prompt: the note sits between markers as
    data (markers inside it are removed). Runs on a pool worker; "" for any failure or an invalid answer."""
    from .creds import for_outside  # the prompt goes to an outside model: every field through the one sanitizer
    clean = [{"id": "0", "project": for_outside(row.get("project") or ""),
              "text": for_outside(str(row["text"]).replace("=====", " "))[:400]}]
    out = ask(PHRASE_PROMPT.replace("{items}", json.dumps(clean, ensure_ascii=False))) or ""
    start, end = out.find("["), out.rfind("]")
    try:
        data = json.loads(out[start:end + 1]) if isinstance(out, str) and 0 <= start < end else []
    except ValueError:
        return ""
    for entry in data if isinstance(data, list) else []:
        if isinstance(entry, dict) and entry.get("id") == "0":
            return valid_say(entry.get("say"))
    return ""


def translate_one(text: str, translate: Callable[[str], str]) -> str:
    """The literal translation of a plain one-clause note, if Hebrew enough ("" otherwise: a telegraphic note is
    never translated word for word, see radio_script.plain_title). Runs on a pool worker."""
    if not radio_script.plain_title(text):
        return ""
    out = radio_script.spoken(str(translate(text) or ""))
    return radio_script.say_short(out, 160) if radio_script.hebrew_enough(out) else ""


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _wait(futs: List[Tuple[dict, str, Optional[Future]]], deadline: float) -> List[Tuple[dict, str, Optional[str]]]:
    """[(item, text, answer)] for each submitted call: its answer, "" when it failed or gave nothing, None when
    it was not finished by `deadline` (or never started)."""
    out = []
    for item, text, fut in futs:
        if fut is None:
            out.append((item, text, None))
            continue
        try:
            out.append((item, text, fut.result(timeout=max(0.0, deadline - time.monotonic()))))
        except FutureTimeout:
            out.append((item, text, None))
        except Exception as e:  # noqa: BLE001
            log("localize: a call failed (%s)" % e.__class__.__name__)
            out.append((item, text, ""))
    return out


def localize(snap: dict, last: dict, kind: str, translate: Optional[Callable[[str], str]], cache: dict,
             now: float, ask: Optional[Callable[[str], str]] = None, names: Optional[dict] = None,
             budget: float = PHRASE_TIMEOUT, workers: Optional[Workers] = None) -> int:
    """Give the items this update may read aloud, and whose text is not Hebrew, a Hebrew `say` line. Each item is
    phrased in its OWN call (`ask`, the Gemini text route), in parallel on the station's bounded pool; an item
    whose answer is missing or invalid then gets the literal translation (`translate`) the same way. ONE
    deadline (`budget` seconds) covers the whole step. Whatever is not done by then, or finds no free worker,
    stays as it is: counted by the script, never read. Returns how many items got a line."""
    pool = workers or WORKERS
    deadline = time.monotonic() + budget
    pending, got = [], 0
    for item in radio_script.to_read(snap, last, kind):
        text = radio_script.spoken(item.get("text") or "")
        if not text or item.get("say"):
            continue
        if radio_script.hebrew_enough(text) and radio_script.radio_ready(item.get("text") or "", text):
            continue  # short, clean Hebrew is read as it is: no call
        hit = cache.get(_key(text))
        if hit and (hit[0] or now - hit[1] < TRANSLATE_RETRY):  # an earlier update's answer, or a recent failure
            if hit[0]:
                item["say"], got = hit[0], got + 1
        else:
            pending.append((item, text))
    if ask is not None and pending:
        calls = [(item, text, pool.submit(phrase_one, {"text": text, "project": radio_script.project_label(item, names or {})},
                                          ask)) for item, text in pending]
        pending = []
        for item, text, say in _wait(calls, deadline):
            if say:
                item["say"], got = say, got + 1
                cache[_key(text)] = (say, now)
            elif say is not None and radio_script.hebrew_enough(text):  # Hebrew already: its own text is read, as before
                cache[_key(text)] = ("", now)
            elif say is not None:  # the call finished without a usable line: the literal translation next
                pending.append((item, text))
    if translate is not None and pending and time.monotonic() < deadline:
        calls = [(item, text, pool.submit(translate_one, text, translate)) for item, text in pending]
        for item, text, say in _wait(calls, deadline):
            if say:
                item["say"], got = say, got + 1
            if say is not None:
                cache[_key(text)] = (say, now)
    return got


class RadioError(Exception):
    """A user-facing failure (an i18n key in .key); never carries a secret."""

    def __init__(self, key: str, **kw):
        super().__init__(key)
        self.key, self.kw = key, kw


# --- settings and files ------------------------------------------------------------------------

def settings(cfg: Config) -> dict:
    raw = getattr(cfg, "radio", None) or {}
    s = dict(DEFAULTS)
    for k, v in raw.items():
        if k not in DEFAULTS:
            continue
        want = type(DEFAULTS[k])
        if want is int and isinstance(v, (int, float)) and not isinstance(v, bool):
            v = int(v)
        if isinstance(v, want) and (want is not list or all(isinstance(a, str) for a in v)):
            s[k] = v
    s["lang"] = "he" if (cfg.ui_language or "").startswith("he") else "en"
    if not s["say_voice"] and s["lang"] == "he":
        s["say_voice"] = "Carmit"
    if not s["title"]:
        s["title"] = t("radio_title", lang=s["lang"])
    s["chat"] = home_chat(cfg)
    return s


def folder() -> Path:
    return private_dir(state_dir() / "radio")


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    write_private(path, json.dumps(data, ensure_ascii=False))


def log(line: str) -> None:
    """One short line in radio.log (kept under 200 KB). Callers never pass a URL, a key or a token."""
    path = folder() / "radio.log"
    try:
        if path.exists() and path.stat().st_size > 200_000:
            path.replace(path.with_suffix(".log.1"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
        os.chmod(path, 0o600)
    except OSError:
        pass


@contextmanager
def _locked():
    fd = os.open(str(folder() / "control.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _secret(command: List[str]) -> str:
    """Run a configured credential command; its output stays in memory."""
    if not command:
        raise RadioError("radio_not_configured")
    try:
        r = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise RadioError("radio_no_stream_key") from None
    value = r.stdout.decode("utf-8", "replace").strip()
    if r.returncode or not value:
        raise RadioError("radio_no_stream_key")
    return value


# --- the group's live stream (the owner's account) -------------------------------------------

def call_helper(s: dict, action: str, call: Optional[dict] = None, runner: Callable = subprocess.run) -> dict:
    """Run radio_call.py under the Telethon Python. The request names only the home group."""
    if not s["telethon_python"] or not s["account_api_command"] or not s["account_session_command"]:
        raise RadioError("radio_not_configured")
    chat = s["chat"]
    if not isinstance(chat, int) or chat >= 0:
        raise RadioError("radio_needs_group")
    req = {"action": action, "chat": chat, "allowed": [chat], "title": s["title"], "call": call or {},
           "api_command": s["account_api_command"], "session_command": s["account_session_command"]}
    try:
        r = runner([s["telethon_python"], "-I", str(Path(__file__).with_name("radio_call.py"))],
                   input=json.dumps(req).encode(), capture_output=True, timeout=120)
        out = json.loads((r.stdout or b"{}").decode("utf-8", "replace").strip().splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return {"ok": False, "error": "helper failed"}
    return out if isinstance(out, dict) else {"ok": False, "error": "helper failed"}


# --- launchd -----------------------------------------------------------------------------------

def plist_path() -> Path:
    return HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def job_path(extra: List[str]) -> str:
    """The job's PATH: the configured `path` entries, the usual tool folders, then the starting shell's PATH
    (so a credential command such as `gcloud` is found as it is in the terminal). Built where the core
    services' PATH is (install.service_path)."""
    from .install import service_path
    return service_path(extra)


def plist_bytes(extra_path: Optional[List[str]] = None) -> bytes:
    env = {"PATH": job_path(extra_path or [])}
    env.update({n: os.environ[n] for n in ("CTB_CONFIG_DIR", "CTB_STATE_DIR", "CTB_RADIO_DIAG_SECONDS", "CTB_RADIO_TAP_SECONDS", "CTB_RADIO_LEAD_SECONDS",
                 "CTB_RADIO_OUT_FPS") if os.environ.get(n)})
    return plistlib.dumps({
        "Label": LABEL, "ProgramArguments": launch.argv() + ["radio", "run"],
        "WorkingDirectory": str(folder()), "EnvironmentVariables": env,
        "StandardErrorPath": str(state_dir() / "radio.err"),
        "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 10,
        "ProcessType": "Interactive",  # real-time audio: no timer coalescing
    })


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)


def job_pid() -> Optional[int]:
    """The radio job's pid when launchd runs it, else None."""
    if sys.platform != "darwin":
        return None
    try:
        r = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode:
        return None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            try:
                return int(line[6:])
            except ValueError:
                return None
    return None


def _job_loaded() -> Optional[bool]:
    """True when launchd has the radio job, False when it says there is none, None when it could not be asked
    (a timeout or a failing launchctl). Callers that act on "absent" (the resume) must not act on None."""
    try:
        r = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode == 0:
        return True
    return False if r.returncode == 113 or "could not find service" in (r.stderr or "").lower() else None


def _unload() -> None:
    if _job_loaded() is not False:  # loaded, or launchd could not say: boot it out anyway (stop must stop)
        _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
        for _ in range(40):
            if not _job_loaded():
                break
            time.sleep(0.25)
    try:
        plist_path().unlink()
    except OSError:
        pass


def _kill_leftover_encoder() -> None:
    pid = _read(folder() / "status.json").get("ffmpeg_pid")
    if not isinstance(pid, int) or pid <= 1:
        return
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    except OSError:
        return
    if "ffmpeg" in cmd and MARKER in cmd:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def note_failure(now: Optional[float] = None) -> int:
    """Record one worker failure; returns how many happened within FAILURE_WINDOW. Kept in a file, so failures
    count across launchd's restarts of the job (a crash that recurs on every start still ends in giving up)."""
    now = time.time() if now is None else now
    path = folder() / "failures.json"
    at = [float(x) for x in _read(path).get("at", []) if isinstance(x, (int, float)) and 0 <= now - x < FAILURE_WINDOW]
    at.append(now)
    try:
        _write(path, {"at": at[-50:]})
    except OSError:
        pass
    return len(at)


def _wanted_path() -> Path:
    return state_dir() / "radio" / WANTED  # read-only callers never create the folder


def _read_wanted() -> dict:
    try:
        data = json.loads(_wanted_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def wanted() -> bool:
    """Should the home radio be on? The durable intent, kept apart from the job's runtime state: only an explicit
    stop (or the radio giving up) clears it, never a shutdown, a reboot or a crash. Only a real boolean in
    wanted.json counts; a missing, corrupt or odd file (a radio started by an older version, a half-written file)
    falls back to state.json's "on", which only a stop or giving up ever turns off too. Every decision that acts
    on this is taken under the control lock (_locked); the bare read is for status and the cheap early outs."""
    value = _read_wanted().get("wanted")
    if isinstance(value, bool):
        return value
    return _read(state_dir() / "radio" / "state.json").get("on") is True


def set_wanted(on: bool, resume_at: Optional[List[float]] = None, strict: bool = False) -> bool:
    """Record the intent (callers hold the control lock). Setting it on starts a clean resume budget; `resume_at`
    keeps the failed resume attempts. Returns whether it was saved; `strict` raises OSError instead of logging."""
    rec = {"wanted": bool(on), "at": int(time.time())}
    if on and resume_at:
        rec["resume_at"] = resume_at[-MAX_RESUME * 2:]
    try:
        _write(folder() / WANTED, rec)
        return True
    except OSError:
        if strict:
            raise
        log("could not save whether the radio is wanted")
        return False


def _resume_attempts(now: float) -> List[float]:
    seen = _read_wanted().get("resume_at")
    return [float(x) for x in (seen if isinstance(seen, list) else [])
            if isinstance(x, (int, float)) and not isinstance(x, bool) and 0 <= now - x < RESUME_WINDOW]


def _resume_once(cfg: Config, clock: Callable[[], float] = time.time) -> str:
    """One resume decision and attempt, wholly under the control lock (the one start and stop take), each fact
    re-checked there: wanted, whether the job is loaded (an unknown answer does nothing this round, and nothing
    here ever boots a job out), and the failure budget. Returns off, running, resumed, gave_up, unsupported,
    unknown or failed."""
    try:
        s = _ready(cfg)
    except RadioError:
        return "unsupported"
    with _locked():
        if not wanted():
            return "off"
        loaded = _job_loaded()
        if loaded is True or job_pid():
            return "running"
        if loaded is None:
            return "unknown"
        attempts = _resume_attempts(clock())
        if len(attempts) >= MAX_RESUME:
            return _resume_gave_up()
        try:
            set_wanted(True, attempts + [clock()], strict=True)  # counted before the attempt: a crash still uses it up
        except OSError:
            log("resume: could not record the attempt")
            return "failed"
        try:
            key = _start_locked(cfg, s)
        except RadioError as e:
            if e.key in ("radio_macos_only", "radio_not_configured"):
                return "unsupported"
            log("resume: failed (%s)" % e.key)
            return "failed"
        except Exception as e:
            log("resume: failed (%s)" % e.__class__.__name__)
            return "failed"
        log("resume: on again after the restart")
        return "running" if key == "radio_already_on" else "resumed"


def resume_after_boot(cfg: Config, sleep: Callable[[float], None] = time.sleep,
                      clock: Callable[[], float] = time.time) -> str:
    """Bring the home radio back after the Mac (or the listener's host) went down while it was on. Runs once per
    listener start. Starts it only when it is wanted and its job is absent; never touches a running radio, and
    never the YouTube staff radio (going live there is the owner's own action, that edition keeps its own state).
    Failures back off (RESUME_BACKOFF) and, past MAX_RESUME within RESUME_WINDOW, end in gave_up like the job's own
    failures do. Returns what happened: off, running, resumed, gave_up, unsupported, unknown or failed."""
    if sys.platform != "darwin":
        return "unsupported"
    if not wanted():
        return "off"
    sleep(RESUME_DELAY)
    from . import policy
    last = "failed"
    for wait in RESUME_BACKOFF:
        if wait:
            sleep(wait)
        while wanted() and not policy.on():  # mirroring is off: nothing starts; look again later
            sleep(60)
        last = _resume_once(cfg, clock)
        if last not in ("failed", "unknown"):
            return last
    while wanted() and not policy.on():
        sleep(60)
    last = _resume_once(cfg, clock)  # the budget is used up: the verdict is taken under the lock like any other
    return last


def _resume_gave_up() -> str:
    """The budget is used up (the control lock is held, and the radio was just seen absent and wanted)."""
    set_wanted(False)
    try:
        st = _read(folder() / "status.json")
        st.update(pipeline="failed", gave_up=True)
        _write(folder() / "status.json", st)
    except Exception:
        pass
    log("resume: giving up after repeated failures")
    return "gave_up"


def resume_in_background(cfg: Config) -> None:
    """The listener's one call at startup: a daemon thread, so nothing waits for it. Does nothing at all (not
    even a thread) while the radio is not wanted."""
    if sys.platform != "darwin" or not wanted():
        return

    def work():
        try:
            resume_after_boot(cfg)
        except Exception as e:
            log("resume: stopped (%s)" % e.__class__.__name__)
    threading.Thread(target=work, daemon=True, name="radio-resume").start()


def give_up(s: dict) -> int:
    """Too many failures: end the radio's live stream and unload the job, so nothing is left running."""
    try:
        with _locked():  # after a start that is still bootstrapping this job has written its intent, never before it
            set_wanted(False)  # it will not be resumed either: a radio that keeps failing must not restart in a loop
    except OSError:
        log("run: could not clear the wish to resume")
    _end_room("radio_off")
    try:
        st = _read(folder() / "status.json")
        st.update(pipeline="failed", gave_up=True, ffmpeg_pid=None)
        _write(folder() / "status.json", st)
    except Exception:
        pass
    log("run: giving up; ending the live stream")
    try:
        _end_call(s)
    except Exception:
        pass
    try:
        plist_path().unlink()
    except OSError:
        pass
    _bootout_self()
    return 0


# --- control (start / stop / status / say) -----------------------------------------------------

def start(cfg: Config) -> str:
    """Start the radio; returns an i18n key for the result. Raises RadioError."""
    s = _ready(cfg)
    with _locked():
        return _start_locked(cfg, s)


def _ready(cfg: Config) -> dict:
    if sys.platform != "darwin":
        raise RadioError("radio_macos_only")
    s = settings(cfg)
    if not s["rtmp_url_command"] or not s["rtmp_key_command"]:
        raise RadioError("radio_not_configured")
    return s


def _start_locked(cfg: Config, s: dict) -> str:
    """The start itself; the caller holds the control lock (start, or the resume's one locked decision)."""
    if job_pid():
        return "radio_already_on"
    _end_room("radio_off")  # a fresh start never carries on a room of an earlier run
    res = call_helper(s, "ensure", call=owned_call(_read(folder() / "state.json")))
    if not res.get("ok"):
        log("start: the live stream could not be opened (%s)" % str(res.get("error", ""))[:60])
        raise RadioError("radio_call_busy" if res.get("error") == CALL_BUSY else "radio_call_failed")
    call = res.get("call") or {}
    call = {"id": call.get("id"), "access_hash": call.get("access_hash")}
    owned = bool(res.get("owned"))
    try:
        _write(folder() / "state.json", {"on": True, "since": int(time.time()), "owned": owned, "call": call})
        for name in ("last.json", "failures.json"):  # a fresh start says everything once, with a clean slate
            try:
                (folder() / name).unlink()
            except OSError:
                pass
        path = plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".ctb-tmp")
        tmp.write_bytes(plist_bytes(s["path"]))
        os.replace(tmp, path)
        _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
        if _launchctl("bootstrap", f"gui/{os.getuid()}", str(path)).returncode:
            raise RadioError("radio_job_failed")
        if _job_loaded() is False:  # already gone again (it gave up at once): not on, and not wanted
            raise RadioError("radio_job_failed")
        set_wanted(True)  # under the lock, so neither a stop nor the job's giving up can be overwritten
    except BaseException as e:
        # Any failure once the stream exists (a state or job file that cannot be written, launchctl
        # missing or timing out, an interrupt): nothing may be left running.
        _abort_start(s, call if owned else None)
        if isinstance(e, RadioError) or not isinstance(e, Exception):
            raise
        log("start: the job could not be set up (%s)" % e.__class__.__name__)
        raise RadioError("radio_job_failed") from None
    log("start: on (stream %s)" % ("created" if res.get("created") else "taken back"))
    return "radio_started"


def _abort_start(s: dict, call: Optional[dict]) -> None:
    """Undo a start that failed half-way: unload the job, then end the stream it opened (and only that)."""
    try:
        _unload()
    except Exception:
        pass
    try:
        if call:
            state = _read(folder() / "state.json")
            state.update(on=False, owned=True, call=call)
            _write(folder() / "state.json", state)
    except Exception:
        pass
    try:
        _end_call(s, call)
    except Exception:
        log("start: the live stream may still be open")


def owned_call(state: dict) -> Optional[dict]:
    """The live stream the radio itself created (or took back after a restart), else None. Only this one is
    ever ended or taken over; a stream someone else opened in the group is never touched. An older state file
    without "owned" counts only when it says the radio created the call."""
    call = state.get("call") or {}
    if not isinstance(call, dict) or call.get("id") is None:
        return None
    return call if state.get("owned", state.get("created")) is True else None


def _end_call(s: dict, call: Optional[dict] = None) -> bool:
    """End the radio's own stream (`call`, else the owned call in state.json). The call is forgotten only once
    Telegram confirms it ended or is no longer the group's active call; until then it stays in state.json with
    cleanup_pending, so the next stop (or a start, which takes it back) still knows which stream is the radio's."""
    state = _read(folder() / "state.json")
    call = call or owned_call(state)
    if call:
        try:
            res = call_helper(s, "end", call=call)
        except RadioError:
            res = {"ok": False}
    else:
        res = {"ok": True}
    state.update(on=False, stopped=int(time.time()))
    if res.get("ok"):
        state.update(call={}, owned=False)
        state.pop("cleanup_pending", None)
    else:
        state.update(call=call, owned=True, cleanup_pending=True)
    _write(folder() / "state.json", state)
    return bool(res.get("ok"))


def _end_room(why: str) -> None:
    try:
        from . import radio_room
        radio_room.stop_live(why)
    except Exception:
        pass


def stop(cfg: Config) -> str:
    s = settings(cfg)
    _end_room("radio_off")  # the brainstorming room ends with the radio, whatever happens to the job below
    with _locked():
        # The only thing besides giving up that clears the intent (a shutdown never gets here). Done under the lock,
        # so a start still in progress has finished writing its own intent first, and nothing writes it after this.
        try:
            set_wanted(False, strict=True)
            saved = True
        except OSError:
            saved = False  # the radio is still stopped below, but the owner is told the stop did not stick
        _unload()
        _kill_leftover_encoder()
        st = _read(folder() / "status.json")
        if st:
            st.update(pipeline="stopped", ffmpeg_pid=None)
            _write(folder() / "status.json", st)
        ended = _end_call(s)
        log("stop: off" + ("" if ended else " (the live stream may still be open)") + ("" if saved else " (not saved)"))
        if not saved:
            raise RadioError("radio_stop_not_saved")
        return "radio_stopped" if ended else "radio_stopped_call_left"


def status(cfg: Config, live: bool = False) -> dict:
    s = settings(cfg)
    state, st = _read(folder() / "state.json"), _read(folder() / "status.json")
    usage = radio_voice.Usage(folder() / "tts-usage.json")
    out = {"on": bool(job_pid()), "since": state.get("since"), "pipeline": st.get("pipeline", ""),
           "last_update": st.get("last_update"), "route": st.get("route", ""),
           "tts_minutes_today": round(usage.today() / 60, 1), "tts_daily_minutes": s["tts_daily_minutes"],
           "failures": st.get("failures", 0), "gave_up": bool(st.get("gave_up")), "wanted": wanted(),
           "cleanup_pending": bool(state.get("cleanup_pending"))}
    if live:
        res = call_helper(s, "status")
        out["call"] = {k: v for k, v in (res.get("call") or {}).items() if k in ("rtmp", "participants", "live")}
        out["call"]["active"] = bool(res.get("active"))
    return out


def minutes(cfg: Config) -> dict:
    """Today's paid speech and the daily cap, in minutes (the panel's radio card)."""
    usage = radio_voice.Usage(folder() / "tts-usage.json")
    return {"tts_minutes_today": round(usage.today() / 60, 1), "tts_daily_minutes": settings(cfg)["tts_daily_minutes"]}


def say(text: str) -> str:
    text = radio_script.whole(text, MAX_SAY)
    if not text:
        raise RadioError("radio_say_empty")
    if not job_pid():
        raise RadioError("radio_is_off")
    spool = private_dir(folder() / "say")
    write_private(spool / f"{time.time():.6f}.txt", text)
    return "radio_said"


OP_TIMEOUT = 300  # a start or stop still unfinished after this long no longer counts as in progress


def _op(**fields) -> None:
    try:
        _write(folder() / "op.json", fields)
    except OSError:
        pass


def switch(cfg: Config, action: str) -> str:
    """Start or stop, recording the operation in op.json (in progress, then its result) so the panel can keep
    its button disabled until the switch really finished. Returns the i18n key; raises RadioError."""
    prev = _read(folder() / "op.json")  # a switch the panel asked for (spawn) keeps the time it was asked
    asked = (int(prev.get("asked") or 0) if prev.get("action") == action and not prev.get("done") else 0) or int(time.time())
    _op(action=action, at=int(time.time()), asked=asked, done=False)
    key, ok = "radio_call_failed" if action == "start" else "radio_stopped_call_left", False
    try:
        key = start(cfg) if action == "start" else stop(cfg)
        ok = key in ("radio_started", "radio_already_on", "radio_stopped")
        return key
    except RadioError as e:
        key = e.key
        raise
    finally:
        _op(action=action, at=int(time.time()), asked=asked, done=True, ok=ok, result=key)


def operation(now: Optional[float] = None) -> dict:
    """The panel's view of the last switch: {"pending": "start"|"stop"|"", "failed": bool, "op_at": when it
    was asked for (seconds)}."""
    now = time.time() if now is None else now
    op = _read(folder() / "op.json")
    age = now - float(op.get("at") or 0)
    pending = op.get("action") if not op.get("done") and 0 <= age < OP_TIMEOUT else ""
    return {"pending": pending if pending in ("start", "stop") else "",
            "failed": bool(op.get("done") and op.get("ok") is False and 0 <= age < OP_TIMEOUT),
            "op_at": int(op.get("asked") or op.get("at") or 0)}


def spawn(action: str) -> None:
    """Run `ctb radio <action>` detached (the panel answers at once; the job may take seconds). The operation
    counts as in progress from now on, before the detached process even starts."""
    if action not in ("start", "stop"):
        return
    now = int(time.time())
    _op(action=action, at=now, asked=now, done=False)
    subprocess.Popen(launch.argv() + ["radio", action], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


# --- audio ------------------------------------------------------------------------------------

def make_bed(kind: str, db: int) -> bytes:
    """40 s of raw 48 kHz mono s16le: true digital silence unless `kind` is "pad" (an opt-in soft chord drone of pure
    sines, no noise source; the default is "off", so nothing sounds between updates)."""
    if kind != "pad":
        return bytes(2 * RATE * 2)
    # carriers and swells complete whole cycles in 40 s, so the loop has no click
    voices = [(110, 0.05), (130.8, 0.075), (164.8, 0.1), (220, 0.025)]
    expr = "+".join(f"0.22*sin(2*PI*{f}*t)*(0.55+0.45*sin(2*PI*{m}*t+{i}))" for i, (f, m) in enumerate(voices))
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                            f"aevalsrc={expr}:s={RATE}:d=40", "-af", f"lowpass=f=900,volume={db}dB",
                            "-ac", "1", "-f", "s16le", "pipe:1"], capture_output=True, timeout=60)
        if r.returncode == 0 and r.stdout:
            return r.stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    return bytes(2 * RATE * 2)


_SPEAK_LOCK = threading.Lock()  # radio_voice reports its route through module globals, and two loops speak


class Mixer:
    """The continuous audio: the bed, ducked to silence under each spoken clip, with short fades."""

    FADE_OUT, FADE_IN, GAP = 0.6, 2.0, 0.35

    def __init__(self, bed: bytes):
        self.bed = array.array("h", bed[: len(bed) // 2 * 2] or bytes(4))
        self.pos = 0
        self.gain = 1.0
        self.clips = collections.deque()
        self.front_n = 0  # how many clips at the queue's head came in with front=True
        self.voice: Optional[memoryview] = None
        self.gap = 0
        self.lock = threading.Lock()
        self.speaking = False
        self.voice_tag: Optional[int] = None
        self.voice_total = 0
        self.veto: Optional[Callable[[int], bool]] = None  # a tag whose clip must not start (a cancelled room line)

    def add(self, pcm: bytes, tag: Optional[int] = None, front: bool = False) -> None:
        """Queue a clip; `front`: ahead of the clips still waiting (a session's answer), never cutting one off."""
        with self.lock:  # answers keep their own order (two close together were said newest first; review of #153)
            if front:
                self.clips.insert(self.front_n, (pcm, tag))
                self.front_n += 1
            else:
                self.clips.append((pcm, tag))

    def drop(self, tags) -> int:
        """Remove the queued clips carrying one of `tags` (a brainstorming room's lines on a pause or an end). The
        clip on the air is never cut; returns how many were removed."""
        with self.lock:
            kept, removed, front = collections.deque(), 0, self.front_n
            for n, (pcm, tag) in enumerate(self.clips):
                if tag is not None and tag in tags:
                    removed += 1
                    if n < self.front_n:
                        front -= 1
                else:
                    kept.append((pcm, tag))
            self.clips, self.front_n = kept, max(0, front)
            return removed

    def waiting(self, tags) -> bool:
        """A clip carrying one of `tags` is queued and not yet on the air."""
        with self.lock:
            return any(tag is not None and tag in tags for _pcm, tag in self.clips)

    def progress(self) -> Tuple[Optional[int], int]:
        """(tag of the clip on the air or None, bytes of it already played): the card's captions follow it."""
        with self.lock:
            if self.voice is None:
                return None, 0
            return self.voice_tag, self.voice_total - len(self.voice)

    def pending(self) -> int:
        with self.lock:
            return len(self.clips) + (1 if self.voice is not None else 0)

    def _bed(self, n: int) -> array.array:
        out = array.array("h")
        while len(out) < n:
            take = min(n - len(out), len(self.bed) - self.pos)
            out.extend(self.bed[self.pos:self.pos + take])
            self.pos = (self.pos + take) % len(self.bed)
        return out

    def next(self, n: int = FRAME) -> bytes:
        with self.lock:
            bed = self._bed(n)
            while self.voice is None and self.clips and self.gain <= 0.0 and self.gap <= 0:  # a breath between lines
                pcm, tag = self.clips.popleft()
                self.front_n = max(0, self.front_n - 1)
                if tag is not None and self.veto is not None and self.veto(tag):
                    continue  # cancelled after it was queued: never started
                self.voice_tag = tag
                self.voice, self.voice_total = memoryview(pcm), len(pcm)
            if self.voice is not None:
                chunk = bytes(self.voice[:2 * n])
                self.voice = self.voice[2 * n:] if len(self.voice) > 2 * n else None
                if self.voice is None:
                    self.gap = int(self.GAP * RATE)
                self.speaking = True
                return chunk + bytes(2 * n - len(chunk))
            self.speaking = False
            target = 0.0 if self.clips else (1.0 if self.gap <= 0 else 0.0)
            self.gap = max(0, self.gap - n)
            if target == self.gain:
                if self.gain >= 1.0:
                    return bed.tobytes()
                return bytes(2 * n)
            step = n / (self.FADE_OUT * RATE) if target < self.gain else n / (self.FADE_IN * RATE)
            g0 = self.gain
            g1 = max(target, g0 - step) if target < g0 else min(target, g0 + step)
            self.gain = g1
            out = array.array("h", (int(v * (g0 + (g1 - g0) * i / n)) for i, v in enumerate(bed)))
            return out.tobytes()


# --- the encoder pipeline ------------------------------------------------------------------------

def out_fps() -> int:
    """The stream's constant frame rate (CTB_RADIO_OUT_FPS, 2..30, default OUT_FPS). The picture still changes at
    its own rate; the encoder repeats frames, which costs almost nothing for a still card."""
    try:
        return min(30, max(2, int(os.environ.get("CTB_RADIO_OUT_FPS", OUT_FPS))))
    except ValueError:
        return OUT_FPS


def encoder_args(encoder: str, video_fd: int = 3, marker: str = MARKER, fps: int = FPS, codec: str = "png",
                 bitrate: Tuple[str, str, str] = ("250k", "300k", "600k"), strip_fd: Optional[int] = None,
                 strip: Optional[Tuple[int, int, int]] = None) -> List[str]:
    """ffmpeg: raw audio on stdin + the picture as image frames on `video_fd` (both paced by our clock: the card
    as PNG at 2 fps, the project show as JPEG at its own rate) -> FLV on stdout. No URL or key in here.
    With `strip_fd` and `strip` = (visible width, row, pixels per frame): a ticker strip on that fd (same rate),
    whose window the encoder moves along the strip every frame and lays over the picture at that row."""
    video = (["-c:v", "h264_videotoolbox", "-realtime", "1", "-allow_sw", "1", "-profile:v", "main", "-bf", "0"]
             if encoder == "h264_videotoolbox" else
             ["-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage,zerolatency", "-bf", "0"])
    out = out_fps()
    picture = ["-map", "1:v", "-map", "0:a", "-vf", f"fps={out},format=yuv420p"]
    ticker: List[str] = []
    if strip_fd is not None and strip:
        width, row, speed = strip
        ticker = ["-analyzeduration", "100000", "-f", "image2pipe", "-c:v", codec, "-framerate", str(fps),
                  "-thread_queue_size", "8", "-i", f"pipe:{strip_fd}"]
        # the strip holds the same text twice: the window runs right to left through one copy and loops seamlessly
        picture = ["-filter_complex",
                   f"[2:v]crop=w={width}:h=ih:x='iw-{width}-mod(n*{speed},iw/2)':y=0[t];"
                   f"[1:v][t]overlay=x=0:y={row}:eof_action=repeat,fps={out},format=yuv420p[v]",
                   "-map", "[v]", "-map", "0:a"]
    return ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error",
            "-probesize", "32", "-analyzeduration", "100000",  # raw audio: nothing to probe (0 means 5 s)
            "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-thread_queue_size", "64", "-i", "pipe:0",
            "-analyzeduration", "100000", "-f", "image2pipe", "-c:v", codec, "-framerate", str(fps),
            "-thread_queue_size", "8", "-i", f"pipe:{video_fd}", *ticker,
            *picture, "-g", str(2 * out), "-keyint_min", str(2 * out), *video,
            "-b:v", bitrate[0], "-maxrate", bitrate[1], "-bufsize", bitrate[2],
            "-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "2",
            "-metadata", f"comment={marker}", "-flush_packets", "1", "-max_interleave_delta", "100000", "-f", "flv", "-flvflags", "no_duration_filesize",
            "pipe:1"]


def feed_behind(start: float, sent: int, now: float) -> float:
    """Seconds by which the audio written so far (`sent` samples since `start`) is behind real time: a live
    listener's player has run dry for this long. Zero while the feed is ahead, as it normally is (LEAD)."""
    return max(0.0, (now - start) - sent / RATE)


def pick_encoder(wanted: str) -> str:
    if wanted:
        return wanted
    if sys.platform != "darwin":
        return "libx264"
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=s=320x240:r=2",
                            "-frames:v", "2", "-c:v", "h264_videotoolbox", "-f", "null", "-"],
                           capture_output=True, timeout=30)
        return "h264_videotoolbox" if r.returncode == 0 else "libx264"
    except (OSError, subprocess.TimeoutExpired):
        return "libx264"


MIRROR_OFF = "mirroring is off"
OFF_POLL = 2.0  # seconds between two looks at the mirroring switch while it is off


class Pipeline:
    """ffmpeg + the RTMP publisher + the paced audio writer. `run()` returns when any part fails."""

    def __init__(self, url: str, key: str, card: Path, encoder: str, mixer: Mixer, on_pid: Callable[[int], None],
                 quit: Optional[threading.Event] = None, err_path: Optional[Path] = None, marker: str = MARKER,
                 source=None, keep: Optional[Callable[[], bool]] = None):
        """`err_path` and `marker`: a second, independent pipeline (the staff edition, radio_youtube) keeps its
        own encoder log and its own recognisable encoder. `source` (radio_show.BoardShow): the moving picture
        instead of the card file, with its own frame rate and codec; `keep`: checked each second, False ends the
        run with MODE_CHANGED (the station then starts the other picture)."""
        self.source, self.keep = source, keep
        self.strip_out = None
        self._url, self._key = url, key
        self.err_path, self.marker = err_path, marker
        self.card, self.encoder, self.mixer, self.on_pid = card, encoder, mixer, on_pid
        self.stop = threading.Event()
        self.quit = quit or threading.Event()
        self.proc: Optional[subprocess.Popen] = None
        self.stalls: "collections.deque[float]" = collections.deque(maxlen=20)  # seconds, appended by _writer
        self.stalled = threading.Event()  # set by the writer (never blocks); the log thread wakes on it
        self.diag = None

    def report_stalls(self) -> None:
        """Write the stalls the audio writer noted (on the log thread only)."""
        while self.stalls:
            log(f"audio feed {self.stalls.popleft():.1f} s behind real time (the encoder or the connection stalled)")

    def _stall_logger(self) -> None:
        """The small daemon thread that turns noted stalls into log lines, so neither the real-time writer nor
        run()'s supervising loop ever does file work for them."""
        while not self.stop.is_set():
            self.stalled.wait(1.0)
            self.stalled.clear()
            self.report_stalls()
        self.report_stalls()

    def _writer(self, failed: threading.Event) -> None:
        """Audio frames at real-time pace (LEAD seconds ahead)."""
        start, sent, late = time.monotonic(), 0, False
        try:
            while not self.stop.is_set() and not failed.is_set():
                now = time.monotonic()
                behind = feed_behind(start, sent, now)
                if self.diag:
                    self.diag.call("lag", "audio", behind, now)
                if behind > LATE_AFTER and not late:  # the listener's player has run dry: noted once per episode
                    late = True
                    self.stalls.append(behind)  # no file work here: the stall-log thread writes the line
                    self.stalled.set()
                elif behind <= 0:
                    late = False
                due = (time.monotonic() - start + LEAD) * RATE
                while sent < due:
                    self.proc.stdin.write(self.mixer.next(FRAME))
                    sent += FRAME
                self.proc.stdin.flush()
                time.sleep(0.02)
        except (OSError, ValueError):
            pass
        failed.set()

    def _pictures(self, out, failed: threading.Event) -> None:
        """The card as PNG frames at FPS on the same clock; a new card file is picked up within a second. With a
        `source`, its frames at its own rate instead."""
        if self.source is not None:
            return self._moving(out, failed)
        start, sent, png, stamp = time.monotonic(), 0, b"", None
        try:
            while not self.stop.is_set() and not failed.is_set():
                if self.diag:
                    now = time.monotonic()
                    self.diag.call("lag", "video", max(0.0, now - start - sent / FPS), now)
                try:
                    st = self.card.stat()
                    if (st.st_mtime_ns, st.st_size) != stamp:
                        png, stamp = self.card.read_bytes(), (st.st_mtime_ns, st.st_size)
                except OSError:
                    pass
                due = int((time.monotonic() - start + LEAD) * FPS) + 1
                while png and sent < due:
                    out.write(png)
                    sent += 1
                out.flush()
                time.sleep(0.1)
        except (OSError, ValueError):
            pass
        failed.set()

    def _moving(self, out, failed: threading.Event, get: Optional[Callable[[], bytes]] = None) -> None:
        """Frames from the source at its rate on our clock. The picture and the ticker strip each have their own
        writer thread: the encoder reads its inputs in turn while it starts, so one writer for both would block."""
        if get is None and self.strip_out is not None:
            threading.Thread(target=self._moving, args=(self.strip_out, failed, self.source.strip),
                             daemon=True, name="radio-ticker").start()
        get = get or self.source.frame
        writer = "ticker" if get == getattr(self.source, "strip", None) else "video"
        start, sent, fps = time.monotonic(), 0, self.source.fps
        try:
            while not self.stop.is_set() and not failed.is_set():
                if self.diag:
                    now = time.monotonic()
                    self.diag.call("lag", writer, max(0.0, now - start - sent / fps), now)
                due = int((time.monotonic() - start + LEAD) * fps) + 1
                while sent < due:
                    frame = get()
                    if not frame:
                        break
                    out.write(frame)
                    sent += 1
                out.flush()
                time.sleep(0.5 / fps)
        except (OSError, ValueError):
            pass
        failed.set()

    def run(self) -> str:
        """Stream until a part fails or `quit` is set; returns why it stopped ("" on quit). Never raises: a bad
        stream URL, a missing ffmpeg or any other error comes back as the reason, after everything is closed."""
        from . import policy
        from .radio_rtmp import Publisher, lead_seconds
        pub = err = video = None
        # the one gate before any connection: whatever was prepared before this point (a first picture, a show)
        # is dropped when a stop arrived or the bridge was switched off meanwhile
        if self.quit.is_set():
            return ""
        if not policy.on():
            return MIRROR_OFF
        try:
            try:
                pub = Publisher(self._url, self._key)
                pub.lead = lead_seconds()  # jitter buffer: steady sends even when the encoder stalls
                pub.connect()
            except Exception as e:
                return "publish failed: " + (str(e) if e.__class__.__name__ == "RtmpError" else e.__class__.__name__)
            try:
                from .radio_diag import Capture, Guard, configured_seconds
                seconds = configured_seconds()
                if seconds:
                    self.diag = pub.diag = Guard(Capture(folder(), seconds, log))
            except Exception as e:
                try:
                    log("radio diagnostic disabled: " + e.__class__.__name__)
                except Exception:
                    pass
            err = open(self.err_path or folder() / "ffmpeg.err", "ab")
            rfd, wfd = os.pipe()
            sfd = swfd = None
            try:
                args = encoder_args(self.encoder, rfd) if self.marker == MARKER else \
                    encoder_args(self.encoder, rfd, self.marker)  # the staff edition's own encoder marker
                if self.source is not None:
                    ticker = getattr(self.source, "strip", None) is not None
                    if ticker:
                        sfd, swfd = os.pipe()
                    args = encoder_args(self.encoder, rfd, self.marker, fps=self.source.fps, codec=self.source.codec,
                                        bitrate=BOARD_BITRATE, strip_fd=sfd,
                                        strip=(*self.source.strip_window, self.source.strip_speed) if ticker else None)
                self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                             pass_fds=(rfd,) if sfd is None else (rfd, sfd))
            except BaseException:
                os.close(wfd)
                if swfd is not None:
                    os.close(swfd)
                raise
            finally:
                os.close(rfd)
                if sfd is not None:
                    os.close(sfd)
            video = os.fdopen(wfd, "wb")
            self.strip_out = os.fdopen(swfd, "wb") if swfd is not None else None
            self.on_pid(self.proc.pid)
            failed = threading.Event()
            reason = {"why": ""}

            def forward():
                try:
                    pub.send_flv(self.proc.stdout, self.stop)
                    reason["why"] = "the encoder stopped"
                except Exception as e:
                    reason["why"] = str(e) if e.__class__.__name__ == "RtmpError" else e.__class__.__name__
                failed.set()
            for target, args in ((forward, ()), (self._writer, (failed,)), (self._pictures, (video, failed)),
                                 (self._stall_logger, ())):
                threading.Thread(target=target, args=args, daemon=True).start()
            from . import policy
            while not failed.wait(1.0):
                if self.diag:
                    self.diag.log_error(log)
                if self.quit.is_set():
                    break
                if not policy.on():
                    return MIRROR_OFF  # switched off: the stream stops here (within about a second)
                if self.keep is not None and not self.keep():
                    return MODE_CHANGED  # the picture switch moved: the station reconnects with the other one
            return "" if self.quit.is_set() else (reason["why"] or "the audio feed stopped")
        except Exception as e:
            return "pipeline error: " + e.__class__.__name__
        finally:
            if self.diag:
                self.diag.log_error(log)
            self.close(pub, err)
            for pipe_out in (video, self.strip_out):
                if pipe_out is not None:
                    try:
                        pipe_out.close()
                    except OSError:
                        pass

    def close(self, pub=None, err=None) -> None:
        """Stop the encoder first (a writer blocked on a full pipe then fails at once), then the rest."""
        self.stop.set()
        self.stalled.set()  # wakes the stall-log thread for its last write
        p = self.proc
        if p is not None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                p.kill()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            for f in (p.stdin, p.stdout):
                try:
                    f.close()
                except (OSError, ValueError):
                    pass
        if pub is not None:
            pub.close()
        if err is not None:
            err.close()


# --- the daemon ------------------------------------------------------------------------------------

class Station:
    """`ctb radio run`: the launchd job. Keeps the stream going and speaks updates."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.s = settings(cfg)
        self.dir = folder()
        self.card = self.dir / "card.png"
        self.mixer = Mixer(make_bed(self.s["bed"], self.s["bed_db"]))
        self.usage = radio_voice.Usage(self.dir / "tts-usage.json")
        self.cache = private_dir(self.dir / "tts-cache")
        self.quit = threading.Event()
        self.pipe: Optional[Pipeline] = None
        self.status = {"pid": os.getpid(), "started": int(time.time()), "pipeline": "starting", "failures": 0}
        self.quota_rows: List[dict] = []
        self.quota_at = 0.0
        self.snap: Optional[dict] = None
        self.draw_lock = threading.Lock()
        self.caption_lock = threading.Lock()
        self.captions: dict = {}
        self.tag = 0
        self.headline: Tuple[str, str] = ("", "")
        self.talks: dict = {}
        self._switch_day = ""
        self.translations: dict = {}
        self.asking: List[str] = []
        self.show = None                      # radio_show.BoardShow while the flight board is on the air
        self.show_quit = threading.Event()
        self.board_failed_at: Optional[float] = None   # steady clock (time.monotonic)
        self.show_threads: dict = {}          # show folder -> the thread of the last show that drew there
        self.show_slot: Optional[str] = None
        self.room_tags: set = set()           # the mixer tags of the brainstorming room's lines (radio_room.py)

    channel_bot = None   # tests inject a fake bot; None = the bridge's own

    def asker(self) -> Optional[Callable[[str], str]]:
        """The Gemini text route the spoken replies use (radio_talk.brief_line), only for the Hebrew voice."""
        if self.s["lang"] != "he":
            return None
        from .translate import ask
        return lambda prompt: ask(prompt, self.cfg, budget=PHRASE_TIMEOUT)

    def translator(self) -> Optional[Callable[[str], str]]:
        """The bridge's own fast translator (the one the spoken replies use), only for the Hebrew voice."""
        if self.s["lang"] != "he":
            return None
        from .translate import translate_text
        return lambda text: translate_text(text, self.cfg, fast=True)

    def _status(self, **kw) -> None:
        self.status.update(kw)
        _write(self.dir / "status.json", self.status)

    # content
    def gather(self, now: float) -> dict:
        from .panel import build_state
        state = build_state(self.cfg, now=now, light=False)
        from . import asks
        asking = []
        ask_dir = state_dir() / "ask"
        for f in ask_dir.glob("*.json") if ask_dir.is_dir() else []:
            rec = asks.load(f.stem, now=now)
            if rec:
                asking.append(f.stem[:8])
        self.asking = asking
        ledger = radio_script.ledger_items(Path(os.path.expanduser(self.s["ledger_dir"])) if self.s["ledger_dir"]
                                           else None, now)
        if now - self.quota_at >= QUOTA_EVERY:  # an outside command: not on every look
            self.quota_rows, self.quota_at = radio_script.team_quota(self.s["quota_command"]), now
        return radio_script.snapshot(state, asking, ledger, self.quota_rows, now, agents_done=agents_done(now))

    def draw(self, snap: dict, top: Optional[dict] = None) -> None:
        """The card: what is being said (or the last headline) on top, then the team's state."""
        names = self.s["project_names"]
        when = time.localtime(snap["at"])
        if top is None:
            top = self._headline_section()
        room = self.room_card()
        if room is not None:  # a live brainstorming room: the table card (stage 1: text only) instead of the state
            sections = ([top] if top else []) + room
        else:
            sections = ([top] if top else []) + radio_script.card_sections(snap, names, self.s["lang"])
        with self.draw_lock:
            radio_card.render(self.dir / "bin", self.card, "📻 " + self.s["title"],
                              time.strftime("%H:%M", when), sections,
                              radio_script.card_foot(snap, self.quota_rows), rtl=self.s["lang"] == "he")

    def _headline_section(self) -> Optional[dict]:
        with self.caption_lock:
            line, at = self.headline
        if not line:
            return None
        he = self.s["lang"] == "he"
        return {"head": (f"🎙 העדכון האחרון · {at}" if he else f"🎙 Last update · {at}"),
                "lines": radio_script.card_lines(line, 2)}

    def caption_loop(self) -> None:
        """While a clip is on the air, the card shows the sentence being said, following the audio."""
        shown = None
        while not self.quit.wait(0.5):
            tag, played = self.mixer.progress()
            with self.caption_lock:
                entry = self.captions.get(tag) if tag is not None else None
                talk = self.talks.pop(tag, None) if tag is not None else None
            if talk is not None:  # sessions' answers just went on the air: each one's round trip
                for one in talk if isinstance(talk, list) else [talk]:
                    log(radio_talk.round_trip(one, time.time()))
            if entry is None:
                if shown is not None and self.snap is not None:
                    shown = None
                    self._safe_draw(None)  # back to the headline
                continue
            intro_len, voice_len, chunks = entry
            if not chunks:
                continue
            frac = max(0.0, (played - intro_len) / max(1, voice_len))
            index = radio_script.chunk_at(chunks, frac)
            if (tag, index) != shown and self.snap is not None:
                shown = (tag, index)
                he = self.s["lang"] == "he"
                self._safe_draw({"head": "🎙 בשידור עכשיו" if he else "🎙 On the air", "tone": "warn",
                                 "lines": radio_script.card_lines(chunks[index], 3)})

    def _safe_draw(self, top: Optional[dict]) -> None:
        try:
            self.draw(self.snap, top)
        except Exception as e:
            log("card: could not draw (%s)" % e.__class__.__name__)

    def rewrite(self, script: str, snap: dict) -> str:
        """Optional: an external rewriter (configured command; brief on stdin) polishes the template."""
        if not self.s["rewrite_command"]:
            return script
        from .creds import for_outside
        clean = getattr(self.cfg, "redact_outgoing", True)  # the editor is an outside program: every fact is cleaned
        facts = _outside(radio_script.fresh_facts(snap), clean)  # fresh only: no old backlog
        brief = (t("radio_rewrite_brief", lang=self.s["lang"]) + "\n\nTEMPLATE:\n" + for_outside(script, clean)
                 + "\n\nFACTS:\n" + json.dumps(facts, ensure_ascii=False)[:6000])
        try:
            out = subprocess.run(self.s["rewrite_command"], input=brief, capture_output=True, text=True,
                                 timeout=240).stdout
        except (OSError, subprocess.TimeoutExpired):
            return script
        start, end = out.rfind("<<<"), out.rfind(">>>")
        text = " ".join(out[start + 3:end].split()) if 0 <= start < end else ""
        ok = 150 <= len(text) <= radio_script.MAX_SCRIPT and (self.s["lang"] != "he" or
                                                              radio_script.hebrew_enough(text, 0.85))
        return text if ok else script

    def post_channel(self, report: Optional[dict]) -> None:
        """A copy of the update in the alerts channel (radio_post): handed, after the clip is queued, to the ONE
        posting worker, so a slow or failing channel never delays or breaks the voice."""
        if not report:
            return
        poster = self.__dict__.setdefault("poster", radio_post.Poster(log))
        poster.put(self.cfg, report, self.s["lang"], bot=self.channel_bot)

    def speak(self, text: str, front: bool = False, talk: Optional[dict] = None,
              report: Optional[dict] = None, voice: Optional[dict] = None, usage=None, chime: bool = True,
              gate: Optional[Callable[[], bool]] = None, room: bool = False) -> Optional[bool]:
        """Speak one update: the radio's own intro chime, then the voice; the card shows the words as they
        are said (captions) and keeps the headline afterwards. `front`/`talk`: a session's answer to the
        owner (radio_talk.py), queued ahead of routine updates, its round trip logged once it is on the air.
        A brainstorming room's line (speak_room) brings its own `voice` settings and `usage` (its own cap), may
        skip the chime, is queued only while `gate()` still holds, and is tagged `room` so a pause can drop it.
        Returns True (queued), False (dropped: switched off, or the gate closed meanwhile), None (no voice)."""
        usage = usage or self.usage
        days = self.__dict__.setdefault("_switch_days", {})  # the free-voice note: once a day per cap
        with _SPEAK_LOCK:  # the news and talk loops: one synthesis reads the route globals at a time (review of #153)
            pcm, route = radio_voice.speak(text, self.cfg, voice or self.s, self.cache, usage)
            why = radio_voice.last_reason if route in ("say", "") else ""
            today = time.strftime("%Y-%m-%d")
            cap_out = route == "say" and radio_voice.last_cap_out and days.get(str(usage.path)) != today
            if pcm and cap_out:
                days[str(usage.path)] = today
        if not pcm:
            log("speak: no voice available" + (f" (paid voice: {why})" if why else ""))
            return None
        if cap_out:
            # the day's paid minutes are used up: keep talking in the free local voice, and say so once
            note = radio_voice.system_voice(t("radio_free_voice", lang=self.s["lang"]), self.s.get("say_voice", ""))
            if note:
                pcm = note + bytes(2 * int(0.4 * RATE)) + pcm
                log("speak: the paid minutes are used up; the free voice goes on")
        intro = radio_voice.intro() if chime else b""
        with self.caption_lock:
            self.tag += 1
            self.captions[self.tag] = (len(intro), len(pcm), radio_script.caption_chunks(text))
            for old in [k for k in self.captions if k < self.tag - 5]:
                self.captions.pop(old, None)
            self.headline = (radio_script.headline(text), time.strftime("%H:%M"))
            if talk is not None:
                self.talks[self.tag] = talk
            tag = self.tag
        from . import policy
        with self.queue_lock():  # the checks and the add are one step: a silence or a room drop never falls between
            if not policy.on():
                return False  # switched off while the voice was being made: it is dropped, never queued
            if gate is not None and not gate():
                return False  # a room's pause or end came while the voice was being made: dropped, never queued
            if room:
                self.room_gates()[tag] = gate or (lambda: True)  # checked again when the clip would start
                self.room_tags.add(tag)
                for old in [k for k in self.room_tags if k < tag - 200]:
                    self.room_tags.discard(old)
                    self.room_gates().pop(old, None)
            self.mixer.add(intro + pcm, tag, front=front)
        self.post_channel(report)  # a room line has no report: it never reaches the alerts channel
        self._status(route=route, tts_today=round(self.usage.today(), 1), voice_problem=why or None)
        log(f"speak: {radio_voice.seconds_of(pcm):.0f} s via {route}" + (f" (paid voice: {why})" if why else ""))
        radio_voice.spoken_log(self.dir / "spoken.log", route, text)
        return True

    def talk_loop(self) -> None:
        """Sessions' replies (radio_talk.on_reply spools them), said ahead of routine updates. Replies that come
        close together go on the air as one bulletin; a voice note's answer goes at once."""
        spool = self.dir / "replies"
        batch: List[dict] = []
        arrived: List[float] = []
        from . import policy
        while not self.quit.wait(0.5):
            if not policy.on():
                batch, arrived = [], []  # nothing is said while off; spooled replies wait in their files
                continue
            for f in sorted(spool.glob("*.json")) if spool.is_dir() else []:
                try:
                    record = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    record = None
                try:
                    f.unlink()
                except OSError:
                    pass
                if isinstance(record, dict) and str(record.get("text") or "").strip():
                    batch.append(record)
                    arrived.append(time.time())
            if radio_talk.ready(batch, arrived, time.time()) and not self.room_live():
                text = radio_script.whole(radio_talk.bulletin(batch, (self.snap or {}).get("private_accounts") or []),
                                          MAX_SAY * 3)
                self.speak(text, front=True, talk=list(batch), report=radio_post.bulletin_report(batch))
                batch, arrived = [], []

    def news_gap(self) -> Optional[float]:
        """Seconds between short news updates, stretched as the day's paid minutes run low, so the paid voice
        lasts the day (25 min/day by default): none once 85 % is used (urgent and periodic updates go on)."""
        cap = self.s["tts_daily_minutes"] * 60
        if self.s["news_minutes"] <= 0 or cap <= 0:
            return None
        used = self.usage.today() / cap
        base = self.s["news_minutes"] * 60
        # stretched while the paid minutes run low; once they are used up the free voice speaks at the base pace
        return base if used < 0.6 or used >= 0.98 else 3 * base

    def update(self, force: bool = False) -> None:
        now = time.time()
        try:
            snap = self.gather(now)
        except Exception as e:
            log("update: could not read the state (%s)" % e.__class__.__name__)
            return
        last = _read(self.dir / "last.json")
        if radio_talk.every_reply_on():  # each reply is said itself: no "session X finished" news beside it
            snap["replies_spoken"] = True
        kind = "start" if force else radio_script.decide(snap, last, now, self.s["every_minutes"] * 60,
                                                         self.news_gap())
        self.snap = snap
        if self.mixer.progress()[0] is None:  # while a clip is on the air the captions own the top of the card
            self.draw(snap)
        self.feed_board(now)
        if kind is None or self.room_live():
            return  # while a brainstorming room is live the routine news waits (nothing is marked told)
        told = {} if kind == "start" else last
        try:
            localize(snap, told, "news" if kind == "news" else "script", self.translator(), self.translations, now,
                     ask=self.asker(), names=self.s["project_names"])
        except Exception as e:  # noqa: BLE001 - the update goes on without translations
            log("translate: skipped (%s)" % e.__class__.__name__)
        report: dict = {}
        if kind == "news":
            script = radio_script.build_news(snap, last, self.s["project_names"], self.s["lang"], report=report)
        else:
            script = radio_script.build_script(snap, told, self.s["project_names"], self.s["lang"], report=report)
        if kind == "periodic":
            script = self.rewrite(script, snap)
        script = radio_script.private_speech(radio_script.whole(script, radio_script.MAX_SCRIPT),
                                            snap.get("private_accounts") or [])
        if self.speak(script, report=report):
            _write(self.dir / "last.json", radio_script.remember(snap, now, last))
            self._status(last_update=int(now), last_kind=kind)

    # the brainstorming room (radio_room.py)
    def queue_lock(self):
        """One lock around every check-then-queue (speak) and every emptying of the queue (silence, a room's drop)."""
        return self.__dict__.setdefault("_queue_lock", threading.RLock())

    def room_gates(self) -> dict:
        return self.__dict__.setdefault("_room_gates", {})

    def room_veto(self, tag: int) -> bool:
        """A queued room line whose gate no longer holds (paused, ended, switched off) never starts playing."""
        gate = self.room_gates().get(tag)
        if gate is None:
            return False
        try:
            return not gate()
        except Exception:
            return True

    def room_drop(self) -> int:
        with self.queue_lock():
            return self.mixer.drop(self.room_tags)

    def room_live(self) -> bool:
        try:
            from . import radio_room
            return radio_room.live()
        except Exception:
            return False

    def room_card(self) -> Optional[List[dict]]:
        try:
            from . import radio_room
            room = radio_room.load()
            return radio_room.card_sections(room) if radio_room.live(room) else None
        except Exception:
            return None

    def speak_room(self, text: str, seat, gate: Callable[[], bool], chime: bool) -> Optional[bool]:
        """One room line in the seat's own voice (`seat`: a seat record or "moderator"), under the room's own daily
        paid-voice cap (counted apart from the radio's), then the free local voice; ahead of routine updates."""
        from . import radio_room
        kind = seat if isinstance(seat, str) else seat.get("kind", "moderator")
        name = (None if isinstance(seat, str) else seat.get("voice")) or radio_room.VOICES.get(kind)
        voice = dict(self.s, voice=name if isinstance(name, str) else self.s["voice"], tts_hard_cap=True,
                     style=radio_room.STYLES.get(kind, ""), tts_daily_minutes=radio_room.tts_cap(self.s))
        usage = self.__dict__.get("room_usage")
        if usage is None:
            usage = self.room_usage = radio_voice.Usage(radio_room.usage_path())
        return self.speak(text, front=True, voice=voice, usage=usage, chime=chime, gate=gate, room=True)

    def room_digest(self, key: str) -> str:
        """A short, plain digest of one project's state for its delegate: done, in work, next, waiting on the owner
        (radio_board's per-project record; every item already through radio_board.paintable)."""
        from . import radio_board
        now = time.time()
        sources = radio_board.gather(self.cfg, self.s, now, self.asking)
        want = radio_board.project_key(key)
        for p in radio_board.collect(sources, self.s["project_names"], now):
            if radio_board.project_key(p.get("key")) == want or radio_board.project_key(p.get("name")) == want:
                he = self.s["lang"] == "he"
                parts = []
                for field, label in (("done", "הושלם" if he else "Done"), ("doing", "בעבודה" if he else "In work"),
                                     ("todo", "הבא בתור" if he else "Next"),
                                     ("owner", "מחכה לבעלים" if he else "Waiting on the owner")):
                    items = [i.get("text", "") for i in (p.get(field) or [])[:3] if i.get("text")]
                    if items:
                        parts.append(f"{label}: " + "; ".join(items))
                return ". ".join(parts)
        return ""

    def room_loop(self) -> None:
        """The room's conductor: one step a second while a room is live (nothing at all otherwise)."""
        from . import radio_room
        from .translate import ask
        try:
            rules = radio_room.suitability(self.s.get("room_suitability_file") or "")
        except radio_room.RoomError:
            rules = re.compile(".")  # a configured rule file that cannot be read: nothing generated is said
            log("room: the suitability rules cannot be read; the room says nothing generated")
        conductor = radio_room.Conductor(
            ask=lambda prompt: ask(prompt, self.cfg, budget=radio_room.LLM_BUDGET),
            speak=self.speak_room,
            waiting=lambda: self.mixer.waiting(self.room_tags),
            drop=self.room_drop,
            digest=self.room_digest, redraw=lambda: self.snap is not None and self._safe_draw(None),
            rules=rules, log=log, run_id="%d-%d" % (os.getpid(), self.status.get("started") or 0))
        self.mixer.veto = self.room_veto
        while not self.quit.wait(1.0):
            try:
                conductor.step()
            except Exception as e:  # noqa: BLE001 - the radio goes on; the room tries again next second
                log("room: a step failed (%s)" % e.__class__.__name__)

    # the flight board
    def board_wanted(self) -> bool:
        """The board is the picture: switched on, and not within BOARD_RETRY of a failure (the card meanwhile)."""
        failed = self.board_failed_at
        return board_on() and not self.room_live() and (failed is None or time.monotonic() - failed >= BOARD_RETRY)

    def feed_board(self, now: float) -> None:
        """The latest per-project state to the show (only while the board is on the air)."""
        show = self.__dict__.get("show")
        if show is None:
            return
        from . import radio_board
        try:
            sources = radio_board.gather(self.cfg, self.s, now, self.asking)
            cache = self.translations

            def hebrew(text: str) -> str:
                hit = cache.get(_key(radio_script.spoken(text)))
                return hit[0] if hit and hit[0] else text
            show.update(radio_board.collect(sources, self.s["project_names"], now, hebrew, lang=self.s["lang"]))
        except Exception as e:  # noqa: BLE001 - the board keeps what it had
            log("board: could not read the state (%s)" % e.__class__.__name__)

    def start_board(self):
        """A fresh show for a new board pipeline, or None (no painter: the card stays)."""
        from . import radio_paint, radio_show
        self.stop_board()
        try:  # everything that can fail on the way in leads back to the card, never to a station failure
            if not radio_paint.available(self.dir / "bin"):
                self.board_failed("no painter available")
                return None
            # Two fixed folders, used in turn: a show draws only into a folder whose last writer has finished, so
            # a previous show still finishing a drawing can never put its pictures or ticker into this one.
            slot = self.free_slot()
            if slot is None:
                self.board_failed("an earlier show is still drawing")
                return None
            where = radio_show.safe_folder(state_dir(), ["radio", "board", slot])
            if where is None:  # a link or a file somewhere on the way: nothing is touched, the card stays
                self.board_failed("the show folder is not a plain folder")
                return None
            self.show_quit = threading.Event()
            show = radio_show.BoardShow(where, self.dir / "bin", self.s["lang"], self.dir / "motion")
            self.show = show
            self.feed_board(time.time())
            show.step()  # the first picture before the encoder starts
        except Exception as e:  # noqa: BLE001
            self.board_failed(e.__class__.__name__)
            return None
        thread = threading.Thread(target=show.run, args=(self.show_quit,), daemon=True, name="radio-board")
        self.show_threads[slot] = thread
        self.show_slot = slot
        thread.start()
        return show

    SLOTS = ("show-a", "show-b")

    def free_slot(self, wait: float = 2.0):
        """The show folder to use next: the one the last show did not use, else the same one, whichever's writer
        has finished (each is given `wait` seconds); None when both are still busy."""
        last = self.__dict__.get("show_slot")
        order = [s for s in self.SLOTS if s != last] + ([last] if last in self.SLOTS else [])
        threads = self.__dict__.setdefault("show_threads", {})
        for slot in order:
            th = threads.get(slot)
            if th is not None and th.is_alive():
                th.join(timeout=wait)
            if th is None or not th.is_alive():
                return slot
        return None

    def stop_board(self) -> None:
        quit = self.__dict__.get("show_quit")  # a station made without __init__ (tests) has no show
        if quit is not None:
            quit.set()
        self.show = None

    def board_failed(self, why: str) -> None:
        log(f"board: {why}; back to the card for a while")
        self.board_failed_at = time.monotonic()
        self.stop_board()

    def spoken_requests(self) -> None:
        spool = self.dir / "say"
        for f in sorted(spool.glob("*.txt")) if spool.is_dir() else []:
            try:
                text = radio_script.whole(f.read_text(encoding="utf-8"), MAX_SAY)
                f.unlink()
            except OSError:
                continue
            if text.strip():
                self.speak(text)

    def content_loop(self) -> None:
        if self.cfg.gemini_vertex_project and self.s["tts_daily_minutes"] > 0:
            radio_voice.access_token(self.cfg)  # first, so the opening update is in the paid voice, not `say`
        from . import policy
        if policy.on():
            self.update(force=True)
        next_look = time.time() + POLL
        radio_voice.prune(self.cache)
        from . import policy
        while not self.quit.wait(1.0):
            if not policy.on():
                continue  # nothing new is prepared while off
            self.spoken_requests()
            if time.time() >= next_look:
                next_look = time.time() + POLL
                self.update()

    # the stream
    def run(self) -> int:
        """The job's life. Any unexpected error counts as a failure (across launchd restarts, see note_failure);
        too many of them end the stream and unload the job instead of restarting forever."""
        signal.signal(signal.SIGTERM, lambda *_: self.quit.set())
        signal.signal(signal.SIGINT, lambda *_: self.quit.set())
        try:
            return self._run()
        except Exception as e:
            log("run: unexpected failure (%s)" % e.__class__.__name__)
            self.quit.set()
            if self.pipe is not None:
                self.pipe.close()
            if note_failure() >= MAX_FAILURES:
                return self.give_up()
            return 1  # launchd starts the job again after its throttle
        finally:
            try:  # a room never outlives the station that ran it
                from . import radio_room
                radio_room.stop_live("radio_off")
            except Exception:
                pass
            poster = self.__dict__.get("poster")
            if poster is not None:
                poster.stop(timeout=2.0)  # its worker never outlives the station

    def _run(self) -> int:
        try:
            url, key = _secret(self.s["rtmp_url_command"]), _secret(self.s["rtmp_key_command"])
        except RadioError:
            log("run: the stream key is not available")
            return self.give_up()
        try:
            self.draw(self.gather(time.time()))
        except Exception:
            radio_card.plain(self.card)
        encoder = pick_encoder(self.s["video_encoder"])
        threading.Thread(target=self.content_loop, daemon=True, name="radio-content").start()
        threading.Thread(target=self.caption_loop, daemon=True, name="radio-captions").start()
        threading.Thread(target=self.talk_loop, daemon=True, name="radio-talk").start()
        threading.Thread(target=self.room_loop, daemon=True, name="radio-room").start()
        from . import policy
        while not self.quit.is_set():
            if not policy.on():  # mirroring is off: no stream is started; whatever was prepared is dropped
                self.silence()
                self.quit.wait(OFF_POLL)
                continue
            show = self.start_board() if self.board_wanted() else None
            if show is None:
                self.stop_board()

            def keep(show=show) -> bool:  # the picture in use is still the wanted one
                if show is not None and show.failed:
                    self.board_failed(show.failed)
                    return False
                return (show is not None) == self.board_wanted()
            self.pipe = Pipeline(url, key, self.card, encoder, self.mixer, lambda pid: self._status(ffmpeg_pid=pid),
                                 quit=self.quit, source=show, keep=keep)
            self._status(pipeline="live", picture="board" if show is not None else "card")
            why = self.pipe.run()
            if self.quit.is_set():
                break
            if why in (MIRROR_OFF, MODE_CHANGED):
                continue  # not a failure: the loop above waits until it is on again, or starts the other picture
            count = note_failure()
            log(f"pipeline: {why}; restart {count}/{MAX_FAILURES}")
            self._status(pipeline="restarting", failures=count)
            if count >= MAX_FAILURES:
                return self.give_up()
            self.quit.wait(min(30, 2 ** count))
        self.stop_board()
        self._status(pipeline="stopped", ffmpeg_pid=None)
        return 0

    def silence(self) -> None:
        """Mirroring is off: every clip waiting to be said, the captions and the headline are dropped, and the
        plain card (no words) replaces the last picture."""
        with self.queue_lock():  # the room is ended first, then the queue emptied: no line of it can slip in between
            try:
                from . import radio_room
                radio_room.stop_live("off")
            except Exception:
                pass
            with self.mixer.lock:
                self.mixer.clips.clear()
                self.mixer.front_n = 0
                self.mixer.voice = None
        with self.caption_lock:
            self.captions.clear()
            self.headline = ("", "")
        self.stop_board()  # the board's pictures are dropped too
        try:
            radio_card.plain(self.card)
        except Exception:
            pass
        self._status(pipeline="off (mirroring is off)")

    def give_up(self) -> int:
        self.status.update(pipeline="failed", gave_up=True, ffmpeg_pid=None)
        return give_up(self.s)


def _outside(value, clean: bool):
    """Every string inside `value` through the outside-model sanitizer (creds.for_outside)."""
    from .creds import for_outside
    if isinstance(value, str):
        return for_outside(value, clean)
    if isinstance(value, list):
        return [_outside(v, clean) for v in value]
    if isinstance(value, dict):
        return {k: _outside(v, clean) for k, v in value.items()}
    return value


def agents_done(now: float, window: float = 3600.0) -> List[dict]:
    """Background agents that ended in the last hour, from the agents topics' own records
    (<state>/agents-topics/<session>.json, agents_topic.agent_finished): a cheap read, no transcript."""
    out = []
    root = state_dir() / "agents-topics"
    for f in root.glob("*.json") if root.is_dir() else []:
        try:
            done = json.loads(f.read_text(encoding="utf-8")).get("finished") or {}
        except (OSError, ValueError, AttributeError):
            continue
        for agent, at in done.items() if isinstance(done, dict) else []:
            if isinstance(at, (int, float)) and 0 <= now - at < window:
                out.append({"key": "a:" + str(agent), "session": f.stem[:8], "at": float(at)})
    return out


def _bootout_self() -> None:
    """Unload this job from a detached launchctl (it ends this process too, after its cleanup)."""
    subprocess.Popen(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def run_station(cfg: Config) -> int:
    try:
        station = Station(cfg)
    except Exception as e:  # counted like any other failure, so a broken setup cannot restart forever
        log("run: could not start (%s)" % e.__class__.__name__)
        return give_up(settings(cfg)) if note_failure() >= MAX_FAILURES else 1
    from . import buildstamp
    buildstamp.record_running("radio")  # doctor tells when it still plays on older code
    try:
        return station.run()
    finally:
        buildstamp.clear_running("radio")


# --- the bot command ------------------------------------------------------------------------------

def telegram_command(cfg: Config, arg: str, reply: Callable[[str], None]) -> str:
    """/radio [on|off|status]: the owner's command in the home group. Start and stop run in a thread."""
    lang = settings(cfg)["lang"]
    word = arg.strip().lower()
    action = {"on": "start", "start": "start", "הפעל": "start", "off": "stop", "stop": "stop", "כבה": "stop",
              "עצור": "stop"}.get(word, "status")

    def work():
        try:
            reply(t(switch(cfg, action), lang=lang))
        except RadioError as e:
            reply(t(e.key, lang=lang, **e.kw))
        except Exception:
            reply(t("radio_call_failed" if action == "start" else "radio_stopped_call_left", lang=lang))
    if action == "status":
        st = status(cfg)
        reply(t("radio_status_on", lang=lang, minutes=st["tts_minutes_today"], cap=st["tts_daily_minutes"])
              if st["on"] else t("radio_status_off", lang=lang))
        return "radio"
    reply(t("radio_starting" if action == "start" else "radio_stopping", lang=lang))
    threading.Thread(target=work, daemon=True, name="radio-" + action).start()
    return "radio"


# --- the CLI -------------------------------------------------------------------------------------

def prepare_motion(cfg: Config, modules: str = "", chrome: str = "", node: str = "node") -> int:
    """`radio prepare-motion`: render the board's motion units once into the radio's folder (radio_motion)."""
    from . import radio_motion
    s = settings(cfg)
    why = radio_motion.prepare(folder() / "motion", os.path.expanduser(modules or s["motion_modules"]),
                               chrome or s["chrome_path"] or radio_motion.default_chrome(), node)
    print(why or "the flight board's motion units are ready")
    return 1 if why else 0


def cli(cfg: Config, action: str, text: str = "", live: bool = False) -> int:
    lang = settings(cfg)["lang"]
    try:
        if action == "run":
            return run_station(cfg)
        if action in ("start", "stop"):
            print(t(switch(cfg, action), lang=lang))
        elif action == "say":
            print(t(say(text), lang=lang))
        elif action == "status":
            print(json.dumps(status(cfg, live=live), ensure_ascii=False, indent=1))
        else:
            return 2
        return 0
    except RadioError as e:
        print(t(e.key, lang=lang, **e.kw), file=sys.stderr)
        return 1
