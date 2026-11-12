"""Where each line of work stands, for the Cockpit's Morning Brief (brief.py), from a pluggable source.

A provider gives plain work items, one row per tracked item:
    {"line": key, "name": shown name, "id": str, "text": str, "status": "open" | "done",
     "wait": "owner" | "", "updated": epoch seconds, "note": the item's latest note or ""}
`line` is a project key (radio_board.project_key spelling: case and spacing folded), so a ledger file stem, a
project group's name and a session title prefix meet on the same line.

Providers here:
- LedgerProvider: the owner ledger, read-only. Either a folder of `<project>.json` files ({"items": [...]}, the
  format the staff radio's `ledger_dir` already reads) or a command that prints the same as JSON
  ({"<project>": {"items": [...]}}). The path or command comes from the configuration, never from code.
- SampleProvider: invented rows for tests, screenshots and a first look on an install without a ledger.

Every reader is bounded (files, items, characters, time) and returns [] when its source is missing or odd, so a
broken source greys its facts in the brief instead of breaking the Cockpit.
"""
from __future__ import annotations

import json
import math
import os
import re
import selectors
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

MAX_PROJECTS = 40
MAX_ITEMS = 300  # per project
TEXT_CHARS = 300
PROJECT_CHARS = 60
FILE_BYTES = 1024 * 1024
COMMAND_TIMEOUT = 5.0
COMMAND_CACHE = 30.0  # seconds a command's answer is reused (the page asks every 15 seconds)
_CONTROLS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x84\x86-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_SEPARATORS = re.compile(r"[^\S\n]*[^\S \n][^\S\n]*")


def clean_text(text: str) -> str:
    """Keep line breaks, normalize other separators, and remove dangerous controls."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROLS.sub("", _SEPARATORS.sub(" ", text))  # separators first: they never glue words


def project_name(name: object) -> str:
    text = " ".join(clean_text(str(name or "")).split())
    return text if len(text) <= PROJECT_CHARS and len(text.casefold()) <= PROJECT_CHARS else ""


def line_key(name: object) -> str:
    """One spelling per line (the same folding as radio_board.project_key)."""
    return project_name(name).casefold()


def _num(value) -> Optional[float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _text(value) -> str:
    text = " ".join(clean_text(str(value)).split())
    return text if len(text) <= TEXT_CHARS else text[:TEXT_CHARS - 1] + "…"


def rows_of(project: str, data) -> List[dict]:
    """The checked rows of one project's ledger data ({"items": [...]}); anything odd is left out."""
    project = project_name(project)
    if not project:
        return []
    items = data.get("items") if isinstance(data, dict) else None
    out = []
    for it in (items if isinstance(items, list) else [])[:MAX_ITEMS]:
        if not isinstance(it, dict) or not isinstance(it.get("text"), str) or not it["text"].strip():
            continue
        if it.get("status") not in ("open", "done"):
            continue
        updated, created = _num(it.get("updated")), _num(it.get("created"))
        if updated is None or created is None:
            continue
        updated = updated or created
        notes = it.get("notes") if isinstance(it.get("notes"), list) else []
        last = next((n for n in reversed(notes) if isinstance(n, str) and n.strip()), "")
        out.append({"line": line_key(project), "name": project, "id": str(it.get("id", ""))[:20],
                    "text": _text(it["text"]), "status": it["status"],
                    "wait": "owner" if it.get("wait") == "owner" else "", "updated": updated,
                    "note": _text(last) if last else ""})
    return out


class Provider:
    """The interface: `items(now)` returns work item rows (see the module docstring); `name` says where they
    came from, and `ok` is False after a read that failed (the brief shows the source as unavailable)."""
    name = ""
    ok = True

    def items(self, now: float) -> List[dict]:
        raise NotImplementedError


class LedgerProvider(Provider):
    name = "ledger"

    def __init__(self, folder: Optional[Path] = None, command: Sequence[str] = ()):
        self.folder = folder
        self.command = [str(a) for a in command]
        self._lock = threading.Lock()
        self._cached: tuple = (0.0, [])

    def items(self, now: float) -> List[dict]:
        if self.command:
            return self._from_command(now)
        return self._from_folder()

    def _from_folder(self) -> List[dict]:
        folder = self.folder
        if not folder or not folder.is_dir():
            self.ok = False
            return []
        self.ok = True
        out: List[dict] = []
        for path in sorted(folder.glob("*.json"))[:MAX_PROJECTS]:
            try:
                if path.stat().st_size > FILE_BYTES:
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                continue
            out.extend(rows_of(path.stem, data))
        return out

    def _from_command(self, now: float) -> List[dict]:
        with self._lock:
            at, rows = self._cached
            if at and 0 <= time.monotonic() - at < COMMAND_CACHE:
                return list(rows)
            try:
                raw = self._command_output()
                data = json.loads(raw.decode("utf-8")) if raw is not None else None
            except (OSError, subprocess.SubprocessError, ValueError, RecursionError):
                data = None
            rows = []
            if isinstance(data, dict):
                for project, body in list(data.items())[:MAX_PROJECTS]:
                    if isinstance(project, str) and project.strip():
                        rows.extend(rows_of(project.strip(), body))
            self.ok = data is not None
            self._cached = (time.monotonic(), rows)
            return list(rows)

    def _command_output(self) -> Optional[bytes]:
        """Drain both pipes within independent byte budgets and one wall-clock deadline."""
        deadline = time.monotonic() + COMMAND_TIMEOUT
        with subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=dict(os.environ, NO_COLOR="1")) as proc:
            try:
                raw = bytearray()
                with selectors.DefaultSelector() as selector:
                    sizes = {proc.stdout: 0, proc.stderr: 0}
                    for pipe in sizes:
                        selector.register(pipe, selectors.EVENT_READ)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return None
                        for key, _ in selector.select(remaining):
                            pipe = key.fileobj
                            chunk = os.read(pipe.fileno(), min(65536, FILE_BYTES - sizes[pipe] + 1))
                            if not chunk:
                                selector.unregister(pipe)
                                continue
                            sizes[pipe] += len(chunk)
                            if sizes[pipe] > FILE_BYTES:
                                return None
                            if pipe is proc.stdout:
                                raw.extend(chunk)
                remaining = deadline - time.monotonic()
                if remaining <= 0 or proc.wait(timeout=remaining) != 0:
                    return None
                return bytes(raw)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()


class SampleProvider(Provider):
    """Invented work for tests and screenshots: no real project, person or id."""
    name = "sample"

    ROWS = (  # (project, text, status, wait, minutes ago[, latest note])
        ("studio", "Three of eight screens are built. Next: the filters board.", "open", "", 12),
        ("studio", "Pick a caption style", "open", "owner", 30),
        ("radio", "Found the source of the background noise; the fix is in review.", "open", "", 40),
        ("release", "Everything is ready except your approval.", "open", "owner", 60),
        ("bug fixes", "Card looks almost finished; nobody touched it since last night.", "open", "", 11 * 60),
        ("studio", "Filters board drafted", "done", "", 70),
        ("radio", "Old intro removed", "done", "", 100),
    )

    def __init__(self, rows: Optional[Sequence[tuple]] = None):
        self.rows = tuple(rows) if rows is not None else self.ROWS

    def items(self, now: float) -> List[dict]:
        return [{"line": line_key(r[0]), "name": r[0], "id": str(n), "text": r[1], "status": r[2], "wait": r[3],
                 "updated": now - r[4] * 60, "note": r[5] if len(r) > 5 else ""} for n, r in enumerate(self.rows)]


def from_config(cfg) -> Optional[Provider]:
    """The configured source: `brief.lines` = "sample" for the sample rows; otherwise the ledger from
    `brief.ledger_command` (argv) or `brief.ledger_dir`, falling back to the staff radio's `ledger_dir`.
    None when nothing is configured (the brief then builds lines from the sessions alone)."""
    brief = getattr(cfg, "brief", None) or {}
    if brief.get("lines") == "sample":
        return SampleProvider()
    command = brief.get("ledger_command")
    if isinstance(command, list) and command and all(isinstance(a, str) and a for a in command):
        return _shared(("cmd",) + tuple(command), lambda: LedgerProvider(command=command))
    folder = brief.get("ledger_dir") or (getattr(cfg, "radio", None) or {}).get("ledger_dir")
    if isinstance(folder, str) and folder.strip():
        path = Path(os.path.expanduser(folder.strip()))
        return _shared(("dir", str(path)), lambda: LedgerProvider(folder=path))
    return None


_providers: Dict[tuple, Provider] = {}


def _shared(key: tuple, make) -> Provider:
    """One provider per source for the panel's lifetime, so a command's short cache is shared by every request."""
    found = _providers.get(key)
    if found is None:
        found = _providers[key] = make()
    return found
