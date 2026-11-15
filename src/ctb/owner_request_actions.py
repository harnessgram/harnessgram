"""Owner actions delegate every write to the configured ledger tool.

The CLI owns locking and conditional completion. There is no panel ledger writer,
action history, delayed action or session send.
"""
from __future__ import annotations

import re
import subprocess
import threading

from . import idle_lines, owner_requests


class AlreadyClosed(ValueError):
    """The CLI atomically rejected completion of an item that is no longer open."""


def locate(cfg, project, ident):
    folder = owner_requests.ledger_dir(cfg)
    if folder is None or not isinstance(project, str) or not project or type(ident) is not int or ident < 1:
        raise ValueError("bad request")
    # Use the reader's project spelling, but pass only the actual safe file stem to the CLI.
    paths = [p for p in folder.glob("*.json")
             if owner_requests._text(owner_requests.project_name(p.stem)) == project]
    if len(paths) != 1 or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", paths[0].stem):
        raise ValueError("unknown request")
    data = owner_requests._read(paths[0])
    items = data.get("items") if isinstance(data, dict) else None
    hits = [i for i in (items[:owner_requests.MAX_ITEMS] if isinstance(items, list) else [])
            if isinstance(i, dict) and type(i.get("id")) is int and i["id"] == ident]
    if len(hits) != 1 or hits[0].get("status") not in ("open", "done"):
        raise ValueError("unknown request")
    return paths[0].stem


def command(cfg, project, ident, verb, note):
    tool = ((getattr(cfg, "brief", None) or {}).get("ledger_tool")
            or (getattr(cfg, "radio", None) or {}).get("ledger_tool"))
    if isinstance(tool, str):
        tool = [tool]
    if not isinstance(tool, list) or not tool or not all(isinstance(s, str) and s for s in tool):
        raise ValueError("invalid ledger tool")
    args = [*tool, project, verb, str(ident)]
    if verb == "done":
        args.append("--if-open")
    try:
        subprocess.run([*args, note], check=True, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as exc:
        if verb == "done" and exc.returncode == 3:
            raise AlreadyClosed("already closed") from None
        raise OSError("ledger tool unavailable") from None
    except (subprocess.SubprocessError, OSError):
        raise OSError("ledger tool unavailable") from None


def act(cfg, project, ident, action):
    if action not in ("done", "drop", "push"):
        raise ValueError("bad action")
    stem = locate(cfg, project, ident)
    note = {"done": "done by owner", "drop": "dropped by owner", "push": "pushed by owner"}[action]
    command(cfg, stem, ident, "note" if action == "push" else "done", note)


def schedule(cfg):
    """Check the existing guarded idle-line loop immediately; the heartbeat also sees urgent notes."""
    worker = threading.Thread(target=idle_lines.tick, args=(cfg,), daemon=True)
    worker.start()
    return worker
