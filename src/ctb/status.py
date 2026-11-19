"""`/status` (also `/sessions`): which bridged sessions are alive, and how full their context is."""
from __future__ import annotations

import contextlib
import glob
import html
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import lang as L
from .i18n import t
from .usage import background_of, context_tokens
from .topics import session_state

COMMANDS = ("/status", "/sessions", "סטטוס")
RECENT = 3 * 86400  # sessions untouched for longer are left out
WORKING = 90  # seconds since the transcript changed
WAITING = 3600


def is_command(text: str) -> bool:
    return (text or "").strip().lower().split("@")[0] in COMMANDS


def usage_of(path: str) -> Tuple[int, str]:
    """Context tokens and model of the session (after a compaction: its new, smaller size)."""
    return context_tokens(path)


def projects_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


_ROUND = threading.local()


@contextlib.contextmanager
def transcript_round():
    """Within this block (per thread), transcript_of answers from one listing of each projects folder,
    taken on first use, instead of one glob of every project folder per call. A heartbeat round asks for
    each topic's transcript several times (the session list alone is built for the waiting count, the
    cards and the day view), which made the walk its main cost. A transcript that appears while the block
    runs is found by the next one. Nested blocks share the outer listing."""
    if getattr(_ROUND, "index", None) is not None:
        yield
        return
    _ROUND.index = {}
    try:
        yield
    finally:
        _ROUND.index = None


def _transcript_index(root: str) -> Dict[str, str]:
    """{"<session>.jsonl": path} for root/*/<session>.jsonl, matching glob.glob: folders whose name starts
    with a dot are skipped, folder symlinks are followed, and the first folder in listing order wins."""
    index: Dict[str, str] = {}
    try:
        with os.scandir(root) as found:
            folders = []
            for entry in found:
                try:
                    if not entry.name.startswith(".") and entry.is_dir():
                        folders.append(entry.name)
                except OSError:
                    continue
    except OSError:
        return index
    for folder in folders:
        try:
            with os.scandir(os.path.join(root, folder)) as found:
                for entry in found:
                    if entry.name.endswith(".jsonl"):
                        index.setdefault(entry.name, os.path.join(root, folder, entry.name))
        except OSError:
            continue
    return index


def transcript_of(session: str) -> Optional[str]:
    from .harness import ClaudeHarness, SessionRef
    try:
        session = ClaudeHarness._native(SessionRef.parse(session))
    except ValueError:
        return None
    if not session or "/" in session or ".." in session:
        return None
    root = str(projects_dir())
    rounds = getattr(_ROUND, "index", None)
    if rounds is not None:
        if root not in rounds:
            rounds[root] = _transcript_index(root)
        return rounds[root].get(session + ".jsonl")
    found = glob.glob(os.path.join(glob.escape(root), "*", glob.escape(session) + ".jsonl"))
    return found[0] if found else None


def session_cwd(session: str) -> Optional[str]:
    """The folder a session was started in, from its transcript."""
    path = transcript_of(session)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(60), f):
                try:
                    cwd = json.loads(line).get("cwd")
                except (ValueError, AttributeError):
                    continue
                if isinstance(cwd, str) and os.path.isdir(cwd):
                    return cwd
    except OSError:
        pass
    return None


def workplace(path: Optional[str]) -> Tuple[str, str]:
    """(folder, git branch) a session works in, from the head of its transcript: the folder with the home
    directory written as ~, "" for what the transcript does not say."""
    folder = branch = ""
    if not path:
        return folder, branch
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(60), f):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if not folder and isinstance(row.get("cwd"), str):
                    folder = row["cwd"]
                if not branch and isinstance(row.get("gitBranch"), str):
                    branch = row["gitBranch"]
                if folder and branch:
                    break
    except OSError:
        return "", ""
    home = os.path.expanduser("~")
    if folder == home or folder.startswith(home + os.sep):
        folder = "~" + folder[len(home):]
    clean = lambda v: "".join(ch for ch in v if ch.isprintable())[:200]  # noqa: E731
    return clean(folder), clean(branch)


def rows(topics: Dict[str, dict], only: Optional[List[str]] = None, now: Optional[float] = None,
         find: Callable[[str], Optional[str]] = transcript_of) -> List[Tuple[float, str, int, str]]:
    """(age seconds, display name, context tokens, session id), newest first."""
    now = time.time() if now is None else now
    out = []
    for session, info in topics.items():
        if only is not None and session not in only:
            continue
        path = find(session)
        if not path:
            continue
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        if age > RECENT:
            continue
        try:
            bg = sorted({v for v in background_of(path).values() if v.strip(" 🤖⚙️")})
        except Exception:
            bg = []
        out.append((age, info.get("name") or session[:8], usage_of(path)[0], session, bg,
                    session_state(session).get("state")))
    return sorted(out)


def render(items: List[tuple], lang: str) -> str:
    s = L.strings(lang)
    lines = []
    for item in items:
        age, name, ctx, _session = item[0], item[1], item[2], item[3]
        bg = item[4] if len(item) > 4 else []
        state = s["working"] if age < WORKING else (s["waiting"] if age < WAITING else s["idle"])
        explicit = item[5] if len(item) > 5 else None
        if explicit in ("busy", "waiting", "done"):
            state = t("status_" + explicit, lang=lang)
        ago = s["minutes_ago"].format(n=int(age // 60)) if age < WAITING else s["hours_ago"].format(n=int(age // 3600))
        jobs = "".join(f"\n     {html.escape(j, quote=False)}" for j in bg) if bg else ""
        lines.append(f"{state} · <b>{html.escape(name, quote=False)}</b>\n"
                     f"     \U0001F9E0 {round(ctx / 1000):,}k · {s['updated'].format(ago=ago)}{jobs}")
    text = s["sessions_title"] + "\n\n" + ("\n".join(lines) or s["no_sessions"])
    return (L.RLM if L.is_rtl(lang) else "") + text
