"""Keep a Claude Code session name aligned with its Telegram topic title.

Topic changes are persisted as a small queue.  Delivery uses Claude Code's own ``/rename`` command through the
same guarded idle-prompt path as other bridge-owned terminal input.  Busy sessions, drafts, dialogs, unknown
panes, and failed sends leave the title queued for the next idle or session-start opportunity.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
from typing import Callable, Dict

from .config import private_dir, state_dir, write_private

TITLE_LIMIT = 200
KEPT = 300


def _path():
    return state_dir() / "claude-titles.json"


@contextlib.contextmanager
def _locked():
    private_dir(state_dir())
    with open(str(_path()) + ".lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _load() -> Dict[str, dict]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _clean(name) -> str:
    return " ".join(str(name or "").replace("\u2068", "").replace("\u2069", "").split())[:TITLE_LIMIT]


def want(session: str, name: str) -> None:
    """Queue the topic title, retaining a successful send for duplicate suppression."""
    name = _clean(name)
    if not session or not name:
        return
    try:
        with _locked():
            data = _load()
            rec = data.pop(session, {})
            rec["want"] = name
            data[session] = rec
            write_private(_path(), json.dumps(dict(list(data.items())[-KEPT:]), ensure_ascii=False))
    except OSError:
        pass


def due(session: str) -> str:
    """Return a queued title unless it was sent or Claude has a different custom title."""
    rec = _load().get(session) or {}
    name = _clean(rec.get("want"))
    if not name or _clean(rec.get("set")) == name:
        return ""
    from .status import transcript_of
    from .topic_names import custom_title
    path = transcript_of(session)
    current = _clean(custom_title(path)) if path else ""
    mine = (_clean(rec.get("want")), _clean(rec.get("set")))
    return "" if current and current not in mine else name


def flush(session: str, send: Callable[[str, str], bool] | None = None) -> bool:
    """Try the queued ``/rename`` at an idle prompt; keep it queued on every refusal or failure."""
    name = due(session)
    if not name:
        return False
    if send is None:
        from .inbox import send_idle_text
        send = send_idle_text
    if not send(session, f"/rename {name}"):
        return False
    try:
        with _locked():
            data = _load()
            rec = data.get(session) or {}
            # A concurrent topic rename wins and remains queued.
            if _clean(rec.get("want")) != name:
                return True
            rec["set"] = name
            data[session] = rec
            write_private(_path(), json.dumps(data, ensure_ascii=False))
    except OSError:
        return True
    return True


def ours(session: str, title: str) -> bool:
    """Whether a transcript title is one the bridge requested or successfully sent."""
    rec = _load().get(session) or {}
    title = _clean(title)
    return bool(title) and title in (_clean(rec.get("want")), _clean(rec.get("set")))
