"""Opt-in, bounded capture of the first seconds of a radio connection."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

MAX_ROWS = 100_000
KEEP_FILES = 5     # newest captures kept; older ones are removed
CHUNK = 500        # rows serialised between pauses, so the stream threads keep running


def configured_seconds() -> int:
    try:
        return min(120, max(0, int(os.environ.get("CTB_RADIO_DIAG_SECONDS", "0"))))
    except ValueError:
        return 0


class Capture:
    """Bounded metadata only; contention drops a sample rather than delaying media."""
    def __init__(self, directory: Path, seconds: int, log=None):
        self.directory = directory
        self.log = log
        self.deadline = time.monotonic() + seconds
        self.lock = threading.Lock()
        self.rows = []
        self.rows_dropped = 0
        self.busy_dropped = 0  # samples skipped because the lock was busy (approximate: no lock taken)
        self.done = threading.Event()
        self.name = "radio-diag-" + uuid.uuid4().hex + ".json"
        self.thread = threading.Thread(target=self._finish, args=(seconds,), daemon=True, name="radio-diag")
        self.thread.start()

    def tag(self, kind, timestamp, read_time, size):
        if not self.lock.acquire(blocking=False):
            self.busy_dropped += 1
            return None
        try:
            if read_time > self.deadline or self.done.is_set():
                return None
            if len(self.rows) >= MAX_ROWS:
                self.rows_dropped += 1
                return None
            row = {"type": kind, "flv_timestamp_ms": timestamp, "read_monotonic": read_time,
                   "size_bytes": size, "send_start_monotonic": None, "send_end_monotonic": None}
            self.rows.append(row)
            return row
        finally:
            self.lock.release()

    def sent(self, row, field, now):
        if row is None or not self.lock.acquire(blocking=False):
            return
        try:
            if not self.done.is_set():
                row[field] = now
        finally:
            self.lock.release()

    def lag(self, writer, lag, now):
        if not self.lock.acquire(blocking=False):
            self.busy_dropped += 1
            return
        try:
            if now > self.deadline or self.done.is_set():
                return
            if len(self.rows) >= MAX_ROWS:
                self.rows_dropped += 1
                return
            self.rows.append({"type": "writer", "writer": writer, "monotonic": now, "lag_seconds": lag})
        finally:
            self.lock.release()

    def _finish(self, seconds):
        try:
            time.sleep(seconds)
            with self.lock:
                self.done.set()
                rows, self.rows = self.rows, []
            parts = []
            for i in range(0, len(rows), CHUNK):
                parts.append(",".join(json.dumps(r) for r in rows[i:i + CHUNK]))
                time.sleep(0.001)  # hand the interpreter back to the stream between chunks
            head = json.dumps({"version": 1, "rows_dropped": self.rows_dropped, "rows_busy_dropped": self.busy_dropped})
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._write(self.name, (head[:-1] + ', "rows": [' + ",".join(parts) + "]}").encode("utf-8"))
            self._prune()
        except Exception as exc:
            self.done.set()
            if self.log:
                try:
                    self.log("radio diagnostic disabled: " + exc.__class__.__name__)
                except Exception:
                    pass

    def _prune(self):
        files = sorted(self.directory.glob("radio-diag-*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        for old in files[KEEP_FILES:]:
            try:
                old.unlink()
            except OSError:
                pass

    def _write(self, name, data):
        fd, path = tempfile.mkstemp(prefix=".radio-diag-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            os.replace(path, self.directory / name)
        finally:
            if os.path.exists(path):
                os.unlink(path)


class Guard:
    """Disable all paths after the first error; logging is drained off media threads."""
    def __init__(self, capture):
        self.capture = capture
        self.error = None

    def __bool__(self):
        # False once the window is over or after an error: the stream threads stop calling in at all.
        capture = self.capture
        done = getattr(capture, "done", None)
        return capture is not None and not (done is not None and done.is_set())

    def call(self, method, *args):
        capture = self.capture
        if capture is None:
            return None
        try:
            return getattr(capture, method)(*args)
        except Exception as exc:
            self.capture = None
            if self.error is None:
                self.error = exc.__class__.__name__
            return None

    def log_error(self, log):
        if self.error:
            error, self.error = self.error, None
            try:
                log("radio diagnostic disabled: " + error)
            except Exception:
                pass


def report(path):
    with open(path, encoding="utf-8") as source:
        doc = json.load(source)
    rows = doc["rows"]
    audio = [r for r in rows if r.get("type") == "audio"]
    gaps = []
    read_gaps = []
    for a, b in zip(audio, audio[1:]):
        flv_gap = b["flv_timestamp_ms"] - a["flv_timestamp_ms"]
        read_gap = (b["read_monotonic"] - a["read_monotonic"]) * 1000
        if flv_gap > 30:
            gaps.append((b["flv_timestamp_ms"], flv_gap))
        read_gaps.append((read_gap, flv_gap))
    print(f"Audio timestamp gaps > 30 ms: {len(gaps)}")
    for timestamp, gap in gaps:
        print(f"  at {timestamp} ms: {gap} ms")
    if read_gaps:
        read_gap, flv_gap = max(read_gaps)
        print(f"Max audio read gap: {read_gap:.1f} ms (FLV gap {flv_gap} ms)")
        burst = min(read_gaps, key=lambda pair: pair[0] - pair[1])
        print(f"Largest read burst: read gap {burst[0]:.1f} ms (FLV gap {burst[1]} ms)")
    else:
        print("Audio read gaps: insufficient data")
    slow = [r for r in rows if r.get("send_start_monotonic") is not None
            and r.get("send_end_monotonic") is not None
            and r["send_end_monotonic"] - r["send_start_monotonic"] > 0.1]
    print(f"Send durations > 100 ms: {len(slow)}")
    for r in slow:
        print(f"  {r['type']} at {r['flv_timestamp_ms']} ms: "
              f"{(r['send_end_monotonic'] - r['send_start_monotonic']) * 1000:.1f} ms")
    writers = {r["writer"] for r in rows if r.get("type") == "writer"}
    for writer in sorted(writers):
        maximum = max(r["lag_seconds"] for r in rows if r.get("writer") == writer)
        print(f"Writer lag max ({writer}): {maximum * 1000:.1f} ms")
    if doc.get("raw_truncated") or doc.get("rows_dropped"):
        print(f"Capture truncated: raw={bool(doc.get('raw_truncated'))}, rows dropped={doc.get('rows_dropped', 0)}")
    if doc.get("rows_busy_dropped"):
        print(f"Samples skipped while busy: {doc['rows_busy_dropped']} (gaps above may be partly missing samples)")
