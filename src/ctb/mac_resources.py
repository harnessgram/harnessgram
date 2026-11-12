"""Cheap, read-only Mac resource snapshot. Process text never leaves this module."""
import os
import re
import shlex
import subprocess
import sys
import threading
import time

CACHE_SECONDS = 20
COMMAND_TIMEOUT = 1.5
MEMORY_WARN_PERCENT = 20
MEMORY_CRITICAL_PERCENT = 10
LOAD_WARN_PER_CORE = 0.8
LOAD_CRITICAL_PER_CORE = 1.5
DISK_WARN_PERCENT = 15
DISK_CRITICAL_PERCENT = 5
BATTERY_WARN_PERCENT = 20
BATTERY_CRITICAL_PERCENT = 10

_lock = threading.Lock()
_snapshot = {"available": False, "status": "unavailable"}
_checked = 0.0
_running = False


def _run(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=COMMAND_TIMEOUT,
                              check=True, env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def parse_vm_stat(text):
    page = re.search(r'page size of (\d+) bytes', text)
    if not page:
        return None
    fields = dict((k.strip(), int(v)) for k, v in re.findall(r'^([^:\n]+):\s*(\d+)\.', text, re.M))
    if "Pages free" not in fields:
        return None
    return sum(fields.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages speculative")) * int(page.group(1))


def parse_pressure(text):
    match = re.search(r"System-wide memory free percentage:\s*(\d+)%", text)
    if not match or int(match.group(1)) > 100:
        return None
    return int(match.group(1))


def parse_df(text):
    lines = text.strip().splitlines()
    if len(lines) < 2:
        return None
    parts = lines[-1].split()
    try:
        total, free = int(parts[1]) * 1024, int(parts[3]) * 1024
        return {"free_bytes": free, "total_bytes": total} if total > 0 and free >= 0 else None
    except (ValueError, IndexError):
        return None


def process_kind(command):
    # Only executable basenames are inspected, and only a fixed vocabulary leaves this module.
    try:
        words = shlex.split(command)
    except ValueError:
        return "other"
    if not words:
        return "other"
    name = os.path.basename(words[0]).lower()
    names = [name]
    if re.fullmatch(r'(?:python(?:\d+(?:\.\d+)*)?|node|nodejs)', name) and len(words) > 1:
        names.insert(0, os.path.basename(words[1]).lower())
    for name in names:
        for kind, pattern in (("test", r'(?:pytest(?:-\d+(?:\.\d+)*)?|py\.test|unittest|xctest|vitest(?:\.mjs)?|jest(?:\.js)?)'),
                              ("encoder", r'(?:ffmpeg|handbrake(?:cli)?|videotoolbox)'),
                              ("agent", r'(?:claude|codex|antigravity|agy)(?:\.js)?'),
                              ("browser", r'(?:google chrome|chrome|safari|firefox|webkit|chromium)'),
                              ("build", r'(?:xcodebuild|swift(?:-frontend)?|docker|qemu(?:-system-[a-z0-9_]+)?|node|nodejs|python(?:\d+(?:\.\d+)*)?)')):
            if re.fullmatch(pattern, name):
                return kind
    return "other"


def parse_ps(text):
    rows = []
    for line in text.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            cpu, rss = float(parts[0]), int(parts[1])
            if not (0 <= cpu < 100000 and 0 <= rss < 10**15):
                continue
        except ValueError:
            continue
        rows.append({"kind": process_kind(parts[2]), "cpu_percent": cpu, "memory_bytes": rss * 1024})
    return {"cpu": sorted(rows, key=lambda r: r["cpu_percent"], reverse=True)[:3],
            "memory": sorted(rows, key=lambda r: r["memory_bytes"], reverse=True)[:3]}


def low_status(percent, warn, critical):
    if percent is None:
        return "unavailable"
    return "critical" if percent <= critical else "warn" if percent <= warn else "ok"


def load_status(ratio):
    if ratio is None:
        return "unavailable"
    return "critical" if ratio >= LOAD_CRITICAL_PER_CORE else "warn" if ratio >= LOAD_WARN_PER_CORE else "ok"


def parse_battery(text):
    if not text:
        return None
    match = re.search(r'(\d+)%', text)
    if not match:
        return {"present": False}
    percent = int(match.group(1))
    if percent > 100:
        return None
    plugged = "AC Power" in text
    return {"present": True, "percent": percent, "plugged_in": plugged,
            "status": "ok" if plugged else low_status(percent, BATTERY_WARN_PERCENT, BATTERY_CRITICAL_PERCENT)}


def collect():
    if sys.platform != "darwin":
        return {"available": False, "status": "unavailable"}
    mem_free = parse_vm_stat(_run("/usr/bin/vm_stat"))
    try:
        total = int(_run("/usr/sbin/sysctl", "-n", "hw.memsize").strip())
        if total <= 0:
            total = None
    except ValueError:
        total = None
    pct = parse_pressure(_run("/usr/bin/memory_pressure", "-Q"))
    memory = {"available_bytes": mem_free, "total_bytes": total,
              "pressure_free_percent": pct, "status": low_status(pct, MEMORY_WARN_PERCENT, MEMORY_CRITICAL_PERCENT)}
    try:
        loads = list(os.getloadavg())
        cores = os.cpu_count() or 1
        cpu = {"load": loads, "cores": cores, "status": load_status(loads[1] / cores)}
    except OSError:
        cpu = {"load": None, "cores": os.cpu_count(), "status": "unavailable"}
    disk = parse_df(_run("/bin/df", "-k", "/"))
    if disk:
        disk["status"] = low_status(100 * disk["free_bytes"] / disk["total_bytes"], DISK_WARN_PERCENT, DISK_CRITICAL_PERCENT)
    boot = re.search(r'\bsec\s*=\s*(\d+)', _run("/usr/sbin/sysctl", "-n", "kern.boottime"))
    uptime = max(0, int(time.time()) - int(boot.group(1))) if boot else None
    processes_text = _run("/bin/ps", "-A", "-o", "%cpu=", "-o", "rss=", "-o", "args=")
    statuses = [x["status"] for x in (memory, cpu, disk or {}) if "status" in x]
    battery = parse_battery(_run("/usr/bin/pmset", "-g", "batt"))
    if battery and battery.get("present"):
        statuses.append(battery["status"])
    status = next((s for s in ("critical", "warn", "ok") if s in statuses), "unavailable")
    return {"available": True, "status": status, "memory": memory, "cpu": cpu, "disk": disk,
            "battery": battery, "uptime_seconds": uptime,
            "processes": parse_ps(processes_text) if processes_text else None, "sampled_at": int(time.time())}


def _refresh():
    global _snapshot, _checked, _running
    try:
        try:
            result = collect()
        except Exception:
            result = {"available": False, "status": "unavailable"}
        with _lock:
            _snapshot, _checked = result, time.monotonic()
    finally:
        with _lock:
            _running = False


def snapshot():
    global _running
    if sys.platform != "darwin":
        return {"available": False, "status": "unavailable"}
    with _lock:
        start = not _running and (not _checked or time.monotonic() - _checked >= CACHE_SECONDS)
        if start:
            _running = True
        current = _snapshot
    if start:  # outside the lock: a refresh that runs at once (or a test's inline thread) takes it itself
        try:
            threading.Thread(target=_refresh, daemon=True, name="mac-resources").start()
        except Exception:  # no thread (out of resources): the next poll tries again
            with _lock:
                _running = False
    return current
