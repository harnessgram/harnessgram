"""`ctb statusline`: a Claude Code statusLine command (stdin: the status JSON Claude Code passes).

It prints a short line and saves what the bridge needs from it:

* the subscription rate limits, to <state>/claude-usage.json (the footer's quota line) and, since
  several accounts can run side by side, to <state>/claude-usage-<account>.json too;
* the model and thinking level of each session, to <state>/session-models.json, which the stamp
  under every Telegram reply reads ("Opus 5.5 · thinking: high");
* the size of each session's context window, to <state>/session-windows.json (the keyboard's "· 18%").
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Optional, Tuple

from . import accounts
from .config import locked_map, private_dir, read_map, state_dir, write_private
from .usage import claude_account

SESSION_ID = re.compile(r"[0-9a-f-]{36}")
KEEP_SESSIONS = 200
MODEL_TIMES = "session-models-at.json"  # {session id: epoch when its "model|level" last changed}


def percent(rl: dict, *names: str) -> Tuple[Optional[float], Optional[str]]:
    """Old shape {five_hour: {used_percentage}} or new {limits: [{kind, percent}]}."""
    for n in names:
        v = rl.get(n)
        if isinstance(v, dict):
            return v.get("used_percentage", v.get("percent")), v.get("resets_at")
    for row in rl.get("limits") or []:
        if isinstance(row, dict) and row.get("kind") in names:
            return row.get("percent"), row.get("resets_at")
    return None, None


def usage_record(rl: dict) -> Optional[dict]:
    five, five_r = percent(rl, "five_hour", "session")
    week, week_r = percent(rl, "seven_day", "weekly_all")
    if five is None and week is None:
        return None
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "five_hour": round(five or 0),
            "weekly": round(week or 0), "five_hour_resets": five_r or "", "weekly_resets": week_r or "",
            "source": "statusline",
            # every other limit Claude Code reports (e.g. per-model weekly caps)
            "extra": [{"kind": r.get("kind"), "percent": r.get("percent"), "resets_at": r.get("resets_at")}
                      for r in rl.get("limits") or [] if isinstance(r, dict)
                      and r.get("kind") not in ("five_hour", "session", "seven_day", "weekly_all")]
            + [{"kind": k, "percent": v.get("used_percentage", v.get("percent")), "resets_at": v.get("resets_at")}
               for k, v in rl.items() if isinstance(v, dict) and k not in ("five_hour", "seven_day")]}


def save_usage(rl: dict, account: str) -> bool:
    out = usage_record(rl)
    if out is None:
        return False
    for name in ("claude-usage.json", f"claude-usage-{account}.json"):
        write_private(state_dir() / name, json.dumps(out))  # one file per account: they no longer overwrite each other
    return True


REPORT_REFRESH = 10.0  # seconds: an unchanged report renews its time at most this often, and only after the
# transcript changed (a /effort typed there could be newer than the last report)


def _transcript_time(transcript_path: str) -> float:
    """When the transcript last changed (0 when unknown). Read before any lock: the file may sit on a slow disk."""
    try:
        return os.path.getmtime(transcript_path) if transcript_path else 0.0
    except OSError:
        return 0.0


def _report_is_old(at, changed: float, now: float) -> bool:
    """The saved report time may be older than a /effort written to the transcript since (`changed`)."""
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return True
    return now - at >= REPORT_REFRESH and changed > at


def save_model(session: str, model: str, level: str, transcript_path: str = "") -> None:
    """Remember "model|level" per session, and when the status line reported it (MODEL_TIMES), so a /effort in
    the transcript is compared with the newer of the two (usage.session_model). A changed value is written at
    once; an unchanged one only renews its time, and only when the transcript changed since (at most every
    REPORT_REFRESH seconds). Value and time are updated together under one lock."""
    if not model or not SESSION_ID.fullmatch(session or ""):
        return
    value = model + ("|" + level if level else "")
    base = private_dir(state_dir())
    path, times_path = base / "session-models.json", base / MODEL_TIMES
    now = time.time()
    at = read_map(times_path).get(session)
    same = read_map(path).get(session) == value
    if same and isinstance(at, (int, float)) and not isinstance(at, bool) and now - at < REPORT_REFRESH:
        return  # the common case, every status line: no lock, no write, no look at the transcript
    changed = _transcript_time(transcript_path)  # outside the locks every session shares
    if same and not _report_is_old(at, changed, now):
        return
    with locked_map(path, KEEP_SESSIONS) as known, locked_map(times_path, KEEP_SESSIONS) as times:
        if known.get(session) != value or _report_is_old(times.get(session), changed, now):
            known[session] = value
            times.pop(session, None)
            times[session] = now


WINDOWS_FILE = "session-windows.json"  # {session id: context window size in tokens}, read by usage.context_window


def save_window(session: str, size) -> None:
    """Remember the size of the session's context window (only when it changed), so the keyboard's context
    cell can show how full it is."""
    if not SESSION_ID.fullmatch(session or "") or type(size) is not int or not 10_000 <= size <= 100_000_000:
        return
    path = private_dir(state_dir()) / WINDOWS_FILE
    if read_map(path).get(session) == size:  # the common case, every status line: no lock, no write
        return
    with locked_map(path, KEEP_SESSIONS) as known:  # status lines of several sessions write at once
        known.pop(session, None)
        known[session] = size


def run(stdin_text: str, env: Optional[dict] = None) -> str:
    """Process one status JSON; returns the line to print. Never raises."""
    try:
        d = json.loads(stdin_text)
        if not isinstance(d, dict):
            return ""
    except ValueError:
        return ""
    rl = d.get("rate_limits")
    rl = rl if isinstance(rl, dict) else {}
    model = (d.get("model") or {}).get("display_name", "")
    try:
        private_dir(state_dir())
        acct = claude_account(d.get("session_id") or "", env=env, transcript_path=str(d.get("transcript_path") or ""),
                              use_record=False)
        save_usage(rl, acct)
        save_model(d.get("session_id") or "", model, (d.get("effort") or {}).get("level") or "",
                   str(d.get("transcript_path") or ""))
        window = d.get("context_window")
        if isinstance(window, dict):
            save_window(d.get("session_id") or "", window.get("context_window_size"))
        from . import direct  # output style, agent (and mode): a change made in Claude reaches the topic
        direct.notify(d.get("session_id") or "", direct.from_status(d))
    except Exception:
        pass  # a status line must never fail
    week, _ = percent(rl, "seven_day", "weekly_all")
    bits = [model] + ([f"week {round(week)}%"] if week is not None else [])  # weekly only: no 5-hour window
    return " · ".join(b for b in bits if b)
