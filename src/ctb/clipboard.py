"""Read and clear the system clipboard for the setup wizard, so a bot token copied in Telegram needs no paste.

Only setup uses this, and only after the user asked for it (Enter at the token prompt, or the page's clear
button). The text is returned to the caller and never printed or logged. Each helper tries the platform's own
tool (pbpaste/pbcopy on macOS; wl-paste/wl-copy, xclip or xsel on Linux) with a short timeout, and answers None
or False when none works, so setup falls back to a typed paste.

Clearing uses the tool of the clipboard that was read (Wayland and X11 keep separate clipboards); when no read
said which one, every available clipboard is cleared, and success means all of them were.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from typing import List, Optional

TIMEOUT = 3.0

READERS = {
    "darwin": [["pbpaste"]],
    "linux": [["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"],
              ["xsel", "--clipboard", "--output"]],
}
CLEARERS = {
    "darwin": [["pbcopy"]],
    "linux": [["wl-copy", "--clear"], ["xclip", "-selection", "clipboard", "-i"], ["xsel", "--clipboard", "--clear"]],
}
# the clear command for the clipboard each reader reads
CLEAR_FOR = {"pbpaste": ["pbcopy"], "wl-paste": ["wl-copy", "--clear"],
             "xclip": ["xclip", "-selection", "clipboard", "-i"], "xsel": ["xsel", "--clipboard", "--clear"]}

_last_reader: Optional[str] = None  # the tool whose read last answered (text or empty), for clear()


def _key(platform: Optional[str]) -> str:
    platform = platform or sys.platform
    return "linux" if platform.startswith("linux") else platform


def _commands(table: dict, platform: Optional[str]) -> List[List[str]]:
    return [cmd for cmd in table.get(_key(platform), []) if shutil.which(cmd[0])]


def read(platform: Optional[str] = None) -> Optional[str]:
    """The clipboard's text, or None when it is empty, too big or cannot be read here."""
    global _last_reader
    _last_reader = None
    for cmd in _commands(READERS, platform):
        try:
            done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode != 0:
            continue
        _last_reader = cmd[0]
        # all of it, however long: find_token refuses text too long to check, and holds_token still sees a token
        # in it, so an oversized clipboard holding the token is cleared instead of skipped
        text = done.stdout.decode("utf-8", errors="replace")
        return text if text.strip() else None
    return None


def _run_clear(cmd: List[str]) -> bool:
    try:
        return subprocess.run(cmd, input=b"", capture_output=True, timeout=TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def clear(platform: Optional[str] = None) -> bool:
    """Empty the clipboard (so a token does not linger there). True only when it was surely emptied: the one that
    was last read, or, when that is unknown, every clipboard a tool here can reach."""
    available = _commands(CLEARERS, platform)
    if _last_reader:  # another clipboard's tool would leave this one full: only the matching one counts
        matching = CLEAR_FOR.get(_last_reader)
        return matching in available and _run_clear(matching)
    results = [_run_clear(cmd) for cmd in available]
    return bool(results) and all(results)
