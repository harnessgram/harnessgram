"""The owner's Mac maintenance tools in the Cockpit: a cleanup preview, the real cleanup and the weekly routine.

Everything is configured, nothing is built in. `mac_tools` in the bridge config is an object of argv lists
(empty or missing = the feature is hidden everywhere). Example, documented only and never written into a real config:

    "mac_tools": {
      "clean_dry_run": ["/opt/homebrew/bin/mo", "clean", "--dry-run"],
      "clean":         ["/opt/homebrew/bin/mo", "clean"],
      "maintenance":   ["/path/to/mac-maintenance", "--quiet"],
      "maintenance_log": "~/.local/state/mac-maintenance.log"
    }

Rules the code keeps:
- Commands run with shell=False, no stdin, a minimal environment, a timeout and in their own process group.
- Only the real cleanup gets `OWNER_APPROVED_CLEAN=1`, in the child's environment only, and only after a preview
  that finished in the last ten minutes and that the owner confirmed by its id (the second tap names the size).
- One job at a time. The job runs in a thread; the page polls its state.
- Raw output never leaves this module and is never logged: only sizes, counts and a few fixed words are kept.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import mac_resources
from .config import state_dir, write_private

log = logging.getLogger("ctb.mac_tools")

FRESH_SECONDS = 600                       # the preview must be at most this old for the real cleanup
TIMEOUTS = {"dry-run": 900, "clean": 1800, "maintenance": 3600}
KILL_GRACE = 5                            # seconds between SIGTERM and SIGKILL of a timed-out job
OUT_HEAD = 512 * 1024                     # the most of a command's output that is read: its start and its end
OUT_TAIL = 16 * 1024
LOG_BYTES = 64 * 1024                     # the most of the maintenance log read after a run
TOP_CATEGORIES = 3
STORE_FILE = "mac-tools.json"
JOB_FILE = "mac-tools-job.json"           # the running job (pid, process group), so a bridge restart does not lose it
JOB_SLACK = 60                            # seconds past its timeout after which a recorded job is no longer believed

KINDS = ("dry-run", "clean", "maintenance")
CONFIG_KEYS = {"dry-run": "clean_dry_run", "clean": "clean", "maintenance": "maintenance"}
SECTION_KEYS = frozenset(CONFIG_KEYS.values()) | {"maintenance_log"}
STATES = ("ok", "failed", "timeout", "error", "unreadable", "skipped")
SKIP_WORDS = ("busy", "memory", "setup", "running")


# --- configuration -----------------------------------------------------------------------------------------------

def _argv_ok(value) -> bool:
    return (isinstance(value, list) and bool(value) and all(isinstance(p, str) and p and "\x00" not in p for p in value)
            and value[0].startswith(("/", "~/")))


def valid_section(value) -> bool:
    """The `mac_tools` config object: argv lists whose program is an absolute path, and an optional log path."""
    if not isinstance(value, dict) or set(value) - SECTION_KEYS:
        return False
    for key in CONFIG_KEYS.values():
        if key in value and not _argv_ok(value[key]):
            return False
    log_path = value.get("maintenance_log", "")
    return isinstance(log_path, str) and "\x00" not in log_path and (not log_path or log_path.startswith(("/", "~/")))


def command(cfg, kind: str) -> Optional[List[str]]:
    section = getattr(cfg, "mac_tools", None)
    argv = section.get(CONFIG_KEYS[kind]) if isinstance(section, dict) else None
    if not _argv_ok(argv):
        return None
    return [os.path.expanduser(argv[0])] + list(argv[1:])


def actions(cfg) -> Dict[str, bool]:
    """Which buttons exist. The real cleanup needs the preview as well: it cannot be offered without it."""
    on = _on(cfg)
    dry = on and command(cfg, "dry-run") is not None
    return {"dry_run": dry, "clean": dry and command(cfg, "clean") is not None,
            "maintenance": on and command(cfg, "maintenance") is not None}


def _on(cfg) -> bool:
    """The `mac_tools` add-on (addons.py): off hides the tile and refuses every route."""
    try:
        from . import addons
        return addons.enabled(cfg, "mac_tools")
    except Exception:
        return True


def configured(cfg) -> bool:
    return any(actions(cfg).values())


def available(cfg, kind: str) -> bool:
    return actions(cfg)[{"dry-run": "dry_run", "clean": "clean", "maintenance": "maintenance"}[kind]]


def _log_path(cfg) -> Optional[Path]:
    section = getattr(cfg, "mac_tools", None)
    raw = section.get("maintenance_log") if isinstance(section, dict) else ""
    return Path(os.path.expanduser(raw)) if isinstance(raw, str) and raw else None


# --- parsing (defensive: any text, never raises) ---------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
_SIZE = r"(\d{1,7}(?:\.\d{1,3})?)\s*([KMGT]?B)"
_TOTAL = re.compile(r"Potential space:\s*" + _SIZE + r"(?:\s*·\s*(\d[\d,]{0,8})\s+items)?", re.I)
_CATEGORY = re.compile(r"^\s*\S\s+(?P<name>[^·\n]{1,80}?)\s+·\s+(?:[^·\n]*?,\s*)?" + _SIZE + r"\s+dry\s*$", re.M)
_FREED = re.compile(r"(?:space\s+freed|freed(?:\s+up)?|reclaimed)\D{0,24}" + _SIZE, re.I)
_PATHISH = re.compile(r"(?:^|\s)(?:~|/)\S*|\S*://\S*")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


def plain(text) -> str:
    """Output text without colour codes, control characters or carriage-return spinners."""
    if not isinstance(text, str):
        return ""
    return _CONTROL.sub("", _ANSI.sub("", text.replace("\r\n", "\n").replace("\r", "\n")))


def to_bytes(number: str, unit: str) -> Optional[int]:
    try:
        value = float(number) * _UNITS[unit.upper()]
    except (KeyError, ValueError):
        return None
    return int(value) if 0 <= value < 10 ** 15 else None


def mask(name: str, limit: int = 48) -> str:
    """A category name that is safe to show: paths, links and secret-looking tokens are dropped."""
    name = _LONG_TOKEN.sub("", _PATHISH.sub("", plain(name)))
    return re.sub(r"\s+", " ", name).strip(" ·-")[:limit]


def parse_dry_run(text) -> dict:
    """{"bytes", "items", "categories": [{"name","bytes"}] (the biggest three)}; None where the text does not say."""
    text = plain(text)
    total = _TOTAL.search(text)
    rows = []
    for m in _CATEGORY.finditer(text):
        size, name = to_bytes(m.group(2), m.group(3)), mask(m.group("name"))
        if size is not None and name:
            rows.append({"name": name, "bytes": size})
    rows.sort(key=lambda r: r["bytes"], reverse=True)
    size = to_bytes(total.group(1), total.group(2)) if total else None
    if size is None and rows:
        size = sum(r["bytes"] for r in rows)  # no total line: the categories we could read, at least
    items = None
    if total and total.group(3):
        try:
            items = int(total.group(3).replace(",", ""))
        except ValueError:
            items = None
    return {"bytes": size, "items": items, "categories": rows[:TOP_CATEGORIES]}


def parse_freed(text) -> Optional[int]:
    m = _FREED.search(plain(text))
    return to_bytes(m.group(1), m.group(2)) if m else None


def parse_maintenance_log(text) -> dict:
    """{"skipped": one of SKIP_WORDS or "", "free_before", "free_after" (GiB integers or None)} from the new log lines."""
    text = plain(text)
    skipped = ""
    for line in text.splitlines():
        low = line.lower()
        if "skip:" in low:
            skipped = ("busy" if "heavy job" in low else "memory" if "memory" in low else
                       "setup" if "whitelist" in low else "busy")
        elif "already running" in low:
            skipped = "running"
    done = None
    for done in re.finditer(r"done free\s+(\d{1,6})G\s*(?:->|→|←)\s*(\d{1,6})G", text):
        pass
    return {"skipped": skipped,
            "free_before": int(done.group(1)) * 1024 ** 3 if done else None,
            "free_after": int(done.group(2)) * 1024 ** 3 if done else None}


# --- running a command -------------------------------------------------------------------------------------------

def child_env(extra: Optional[dict] = None) -> dict:
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
           "LC_ALL": "en_US.UTF-8", "TERM": "dumb", "NO_COLOR": "1"}
    for key in ("HOME", "USER", "LOGNAME", "TMPDIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env.update(extra or {})
    return env


def _read_out(handle) -> str:
    size = handle.seek(0, os.SEEK_END)
    handle.seek(0)
    if size <= OUT_HEAD + OUT_TAIL:
        data = handle.read()
    else:
        data = handle.read(OUT_HEAD) + b"\n"
        handle.seek(size - OUT_TAIL)
        data += handle.read()
    return data.decode("utf-8", "replace")


def spawn(argv: List[str], env: dict, timeout: float, on_start: Optional[Callable[[int], None]] = None) -> Tuple[int, str]:
    """Run once and return (exit code, output). Raises subprocess.TimeoutExpired (after killing the whole process
    group) or OSError when the program cannot start. Output goes to a temporary file, so it is bounded in memory.
    `on_start(pid)` is called once the process exists (the process is its own group leader)."""
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                env=env, start_new_session=True, close_fds=True)
        if on_start is not None:
            try:
                on_start(proc.pid)
            except Exception:
                log.warning("mac tools job record could not be written")
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            for sig, grace in ((signal.SIGTERM, KILL_GRACE), (signal.SIGKILL, 3)):
                try:
                    os.killpg(proc.pid, sig)
                except OSError:
                    proc.kill()
                try:
                    proc.wait(timeout=grace)
                    break
                except subprocess.TimeoutExpired:
                    continue
            raise
        return proc.returncode, _read_out(out)


def group_alive(pgid) -> bool:
    """True while a process group with this id exists (and is ours to signal)."""
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
    except OSError:  # no such group, or another user's after the id was reused
        return False
    return True


def disk_free() -> Optional[int]:
    got = mac_resources.parse_df(mac_resources._run("/bin/df", "-k", "/"))
    return got["free_bytes"] if got else None


# --- the job manager ---------------------------------------------------------------------------------------------------

def _num(value, hi=10 ** 15):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= hi else None


def _categories(value) -> list:
    out = []
    for row in value if isinstance(value, list) else []:
        if isinstance(row, dict) and isinstance(row.get("name"), str) and _num(row.get("bytes")) is not None:
            out.append({"name": mask(row["name"]), "bytes": row["bytes"]})
    return out[:TOP_CATEGORIES]


def _clean_report(raw) -> Optional[dict]:
    """A stored report, re-checked field by field (the file is ours but may be old, edited or damaged)."""
    if not isinstance(raw, dict) or raw.get("kind") not in KINDS or raw.get("state") not in STATES:
        return None
    ident = raw.get("id")
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{12}", ident):
        return None
    report = {"id": ident, "kind": raw["kind"], "state": raw["state"],
              "started_at": _num(raw.get("started_at")), "finished_at": _num(raw.get("finished_at")),
              "duration": _num(raw.get("duration"), 10 ** 7), "exit": raw.get("exit") if isinstance(raw.get("exit"), int) else None,
              "bytes": _num(raw.get("bytes")), "items": _num(raw.get("items")),
              "categories": _categories(raw.get("categories")),
              "free_before": _num(raw.get("free_before")), "free_after": _num(raw.get("free_after")),
              "skipped": raw.get("skipped") if raw.get("skipped") in SKIP_WORDS else ""}
    return report if report["finished_at"] is not None else None


class Manager:
    """Runs one job at a time in a thread and remembers the last report and the last preview."""

    def __init__(self, clock: Callable[[], float] = time.time, runner: Optional[Callable] = None,
                 disk: Optional[Callable[[], Optional[int]]] = None):
        self.clock, self.runner, self.disk = clock, runner, disk
        self._lock = threading.Lock()
        self._job: Optional[dict] = None
        self._thread: Optional[threading.Thread] = None
        self._loaded = False
        self._last: Optional[dict] = None
        self._dry: Optional[dict] = None

    # state kept on disk: {"last": report, "dry": preview}
    def _path(self) -> Path:
        return state_dir() / STORE_FILE

    def _job_path(self) -> Path:
        return state_dir() / JOB_FILE

    def _record(self, job: dict, pid: int) -> None:
        write_private(self._job_path(), json.dumps({"id": job["id"], "kind": job["kind"], "started_at": int(job["started"]),
                                                    "pid": pid, "pgid": pid}))

    def _forget(self) -> None:
        try:
            self._job_path().unlink()
        except OSError:
            pass

    def _external(self, now: float) -> Optional[dict]:
        """A job recorded by an earlier bridge process whose process group is still alive, else None. A record of a
        job that has ended (or that is too old to be believed: its id may belong to something else now) is deleted."""
        try:
            raw = json.loads(self._job_path().read_text(encoding="utf-8"))
        except OSError:
            return None
        except (ValueError, UnicodeError):
            raw = None
        ok = (isinstance(raw, dict) and raw.get("kind") in KINDS and isinstance(raw.get("id"), str)
              and re.fullmatch(r"[0-9a-f]{12}", raw["id"]) and _num(raw.get("started_at")) is not None
              and raw.get("pgid") == raw.get("pid"))
        if ok:
            ok = 0 <= now - raw["started_at"] <= TIMEOUTS[raw["kind"]] + JOB_SLACK and group_alive(raw["pgid"])
        if not ok:
            self._forget()
            return None
        return raw

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return
        if not isinstance(raw, dict):
            return
        self._last = _clean_report(raw.get("last"))
        dry = _clean_report(raw.get("dry"))
        self._dry = dry if dry and dry["kind"] == "dry-run" and dry["state"] == "ok" and dry["bytes"] is not None else None

    def _save(self) -> None:
        try:
            write_private(self._path(), json.dumps({"last": self._last, "dry": self._dry}, ensure_ascii=False))
        except OSError:
            log.warning("mac tools report could not be saved")

    def _fresh(self, now: float) -> bool:
        return bool(self._dry) and 0 <= now - self._dry["finished_at"] <= FRESH_SECONDS

    def status(self, cfg) -> dict:
        now = self.clock()
        with self._lock:
            self._load()
            job = None
            if self._job:
                job = {"id": self._job["id"], "kind": self._job["kind"], "elapsed": max(0, int(now - self._job["started"]))}
            else:
                lost = self._external(now)  # started before a bridge restart and still running
                if lost:
                    job = {"id": lost["id"], "kind": lost["kind"], "elapsed": max(0, int(now - lost["started_at"]))}
            dry = None
            if self._dry:
                dry = {"id": self._dry["id"], "bytes": self._dry["bytes"], "items": self._dry["items"],
                       "categories": self._dry["categories"], "finished_at": self._dry["finished_at"],
                       "age": max(0, int(now - self._dry["finished_at"])), "fresh": self._fresh(now)}
            last = dict(self._last) if self._last else None
        return {"ok": True, "now": int(now), "actions": actions(cfg), "fresh_seconds": FRESH_SECONDS,
                "job": job, "dry": dry, "last": last, "mac": mac_resources.snapshot()}

    def start(self, cfg, kind: str, confirm=None) -> dict:
        """{"ok": True, "id", "kind"} or {"ok": False, "error": "unconfigured"|"busy"|"stale"|"confirm"}."""
        if kind not in KINDS or not available(cfg, kind):
            return {"ok": False, "error": "unconfigured"}
        argv = command(cfg, kind)
        now = self.clock()
        with self._lock:
            self._load()
            if self._job:
                return {"ok": False, "error": "busy", "kind": self._job["kind"]}
            lost = self._external(now)
            if lost:
                return {"ok": False, "error": "busy", "kind": lost["kind"]}
            extra = None
            if kind == "clean":
                if not self._fresh(now):
                    return {"ok": False, "error": "stale"}
                if confirm != self._dry["id"]:
                    return {"ok": False, "error": "confirm"}
                extra = {"OWNER_APPROVED_CLEAN": "1"}  # the owner's second tap on the named size; this child only
            elif kind == "maintenance" and confirm is not True:
                return {"ok": False, "error": "confirm"}
            if kind != "dry-run":
                self._dry = None  # a preview is spent by the work that follows it; the next one starts from a new preview
            job = {"id": secrets.token_hex(6), "kind": kind, "started": now}
            self._job = job
            thread = threading.Thread(target=self._work, args=(cfg, job, argv, extra), daemon=True, name="mac-tools")
            self._thread = thread
        try:
            thread.start()
        except Exception:
            with self._lock:
                self._job = None
            return {"ok": False, "error": "busy"}
        return {"ok": True, "id": job["id"], "kind": kind}

    def join(self, timeout: float = 5.0) -> None:
        """For tests: wait for the running job."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _work(self, cfg, job: dict, argv: List[str], extra: Optional[dict]) -> None:
        kind = job["kind"]
        disk = self.disk or disk_free
        runner = self.runner or spawn
        state, code, text, skipped = "error", None, "", ""
        log_file = _log_path(cfg) if kind == "maintenance" else None
        offset = 0
        if log_file is not None:
            try:
                offset = log_file.stat().st_size
            except OSError:
                offset = 0
        before = _num(self._safe_disk(disk))
        try:
            if self.runner is None:
                code, text = runner(argv, child_env(extra), TIMEOUTS[kind], on_start=lambda pid: self._record(job, pid))
            else:
                code, text = runner(argv, child_env(extra), TIMEOUTS[kind])
            state = "ok" if code == 0 else "failed"
        except subprocess.TimeoutExpired:
            state = "timeout"
        except (OSError, ValueError):
            state = "error"
        except Exception:
            log.exception("mac tools job crashed")
            state = "error"
        after = _num(self._safe_disk(disk))
        report = {"id": job["id"], "kind": kind, "state": state, "started_at": int(job["started"]),
                  "exit": code if isinstance(code, int) else None, "bytes": None, "items": None, "categories": [],
                  "free_before": before, "free_after": after, "skipped": ""}
        if state == "ok" and kind == "dry-run":
            parsed = parse_dry_run(text)
            report.update(bytes=parsed["bytes"], items=parsed["items"], categories=parsed["categories"])
            if parsed["bytes"] is None:
                report["state"] = "unreadable"
        elif state == "ok" and kind == "clean":
            freed = parse_freed(text)
            if freed is None and before is not None and after is not None:
                freed = max(0, after - before)
            report["bytes"] = freed
        elif kind == "maintenance" and log_file is not None and state in ("ok", "failed"):
            fresh = self._read_log(log_file, offset)
            parsed = parse_maintenance_log(fresh)
            if parsed["skipped"]:
                report.update(state="skipped", skipped=parsed["skipped"])
            if report["free_before"] is None:
                report["free_before"] = parsed["free_before"]
            if report["free_after"] is None:
                report["free_after"] = parsed["free_after"]
        finished = self.clock()
        report["finished_at"] = int(finished)
        report["duration"] = max(0, int(finished - job["started"]))
        # Only counts, sizes and fixed words are logged: never a line of what the command printed.
        log.info("mac tools job kind=%s state=%s exit=%s seconds=%s", kind, report["state"], code, report["duration"])
        with self._lock:
            self._last = _clean_report(report)
            if kind == "dry-run" and report["state"] == "ok":
                self._dry = dict(self._last) if self._last else None
            self._job = None
            self._forget()
            self._save()

    @staticmethod
    def _safe_disk(disk):
        try:
            return disk()
        except Exception:
            return None

    @staticmethod
    def _read_log(path: Path, offset: int) -> str:
        try:
            with open(path, "rb") as f:
                size = f.seek(0, os.SEEK_END)
                f.seek(offset if 0 <= offset <= size else 0)
                return f.read(LOG_BYTES).decode("utf-8", "replace")
        except OSError:
            return ""


MANAGER = Manager()
