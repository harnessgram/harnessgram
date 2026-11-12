"""Dispatcher: routes messages from unknown chats/threads to a designated session."""
from __future__ import annotations

from pathlib import Path

from .config import state_dir, write_private


def dispatcher_path() -> Path:
    return state_dir() / "dispatcher"


def get_dispatcher() -> str:
    path = dispatcher_path()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def set_dispatcher(session: str) -> Path:
    session = (session or "").strip()
    path = dispatcher_path()
    write_private(path, session + "\n")
    return path


def clear_dispatcher() -> bool:
    path = dispatcher_path()
    try:
        path.unlink()
        return True
    except OSError:
        return False
