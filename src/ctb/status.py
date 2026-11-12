"""`/status` (also `/sessions`): which bridged sessions are alive, and how full their context is."""
from __future__ import annotations

import glob
import html
import json
import os
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


def transcript_of(session: str) -> Optional[str]:
    from .harness import ClaudeHarness, SessionRef
    try:
        session = ClaudeHarness._native(SessionRef.parse(session))
    except ValueError:
        return None
    if not session or "/" in session or ".." in session:
        return None
    found = glob.glob(os.path.join(glob.escape(str(projects_dir())), "*", glob.escape(session) + ".jsonl"))
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
