"""Autopilot: keep idle sessions working while you are away or quota returns.

  1. Quota back: last assistant turn paused for Claude quota (regex QUOTA_WAIT),
     and account usage (5-hour or weekly) < 85% or reset passed -> type continue prompt into pane.
  2. Away mode (state file owner-away.json in state_dir()):
     session idle at prompt -> continue with its own recommendation.
     Stops if last turn has AUTOPILOT-IDLE, max 25 nudges per away period.
Never the same reply uuid twice, only when the pane is at the prompt (❯).
"""
from __future__ import annotations

import fcntl
import glob
import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

from . import accounts, status
from .config import Config, home_chat, private_dir, state_dir, write_private
from .i18n import t
from .telegram import Bot, TelegramError
from .topics import TopicMap

IDLE_MIN = 180          # seconds at the prompt before a nudge
QUOTA_ROOM = 85         # % of the fuller window under which work may continue
MAX_AWAY_NUDGES = 25    # per session per away period
MAX_QUOTA_NUDGES = 3    # per session per 5 hours: a session that keeps pausing is left alone
QUOTA_NUDGE_ROOM = 70   # % under which a quota nudge is sent
AWAY_MAX_HOURS = 48     # away mode ends by itself (owner back but never wrote in Telegram)
AWAY_RECENT_HOURS = 6   # away mode continues only sessions that worked this recently before it was turned on
# A menu waiting on the owner (permission, plan, question) also shows "❯", on "❯ 1. Yes": typing there
# and pressing Enter would approve it (review of PR #12). inbox._SENSITIVE_SCREEN is not
# reused: words like "proceed" or "confirm" are common in an ordinary reply above the input box and
# would keep autopilot from ever firing.
MENU = re.compile(r"❯\s*\d+\.|Do you want|Would you like|\b1\. Yes\b|Enter to (select|confirm)|esc to cancel", re.I)

QUOTA_WAIT = re.compile(
    r"(wait(?:ing)?|paus\w*|hold(?:ing)?|not starting|stop(?:ping)?|before I continue|until)"
    r"[^.\n]{0,90}(quota|usage limit|5-hour|five-hour|window|limit resets?|reset)"
    r"|(quota|5-hour|usage limit)[^.\n]{0,60}(above|at|over) ?\d{2}%[^.\n]{0,60}(not starting|pause|wait)", re.I)

AWAY_PROMPT = ("[Autopilot: the owner is away (for example on a trip) and asked that work continue until they return.] "
               "Continue this session's open work on your own: for every decision you asked about, take your own "
               "recommended option and proceed. Do NOT do anything that needs the owner: money, legal, production "
               "promotion, deleting production data, accounts or passwords, public publication, store submission, "
               "or messages to other people. Park those in a short list for the owner's return. Follow every owner "
               "rule as usual (draft PRs, reviews, quota-aware routing). If nothing safe is left to do, reply with "
               "one line that starts with AUTOPILOT-IDLE and stop.")


def away_path() -> Path:
    return state_dir() / "owner-away.json"


def away_active() -> bool:
    return away_path().exists()


def in_away_scope(last_activity: float, since: float) -> bool:
    """Away mode continues only the sessions that were really in use: one whose last activity is older than
    AWAY_RECENT_HOURS before the owner left is old work left behind, and is never woken."""
    return last_activity >= since - AWAY_RECENT_HOURS * 3600


def set_away(on: bool, now: Optional[float] = None) -> None:
    path = away_path()
    if on:
        write_private(path, json.dumps({"since": time.time() if now is None else now}))
    else:
        try:
            path.unlink()
        except OSError:
            pass


def memory_path() -> Path:
    return state_dir() / "autopilot.json"


def load_memory() -> dict:
    try:
        data = json.loads(memory_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_memory(mem: dict) -> None:
    try:
        write_private(memory_path(), json.dumps(mem, ensure_ascii=False))
    except OSError:
        pass


def last_turn(path: str) -> Tuple[Optional[str], str]:
    """(uuid, text) of the last assistant reply, and whether a newer user prompt follows it."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 2_000_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None, ""
    uuid, text = None, ""
    for raw in lines:
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if e.get("type") == "assistant":
            parts = [b.get("text", "") for b in (e.get("message") or {}).get("content") or []
                     if isinstance(b, dict) and b.get("type") == "text"]
            if any(p.strip() for p in parts):
                uuid, text = e.get("uuid"), "\n".join(parts)
        elif e.get("type") == "user" and not e.get("isMeta") and uuid:
            content = (e.get("message") or {}).get("content")
            if isinstance(content, str) or any(isinstance(b, dict) and b.get("type") == "text" for b in content or []):
                uuid, text = None, ""  # a prompt after the reply: the session is on a newer turn
    return uuid, text


def _parse_reset(val) -> Optional[float]:
    if not val:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            pass
        try:
            import calendar
            return float(calendar.timegm(time.strptime(val[:19], "%Y-%m-%dT%H:%M:%S")))
        except Exception:
            pass
        try:
            from datetime import datetime
            return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    return None


def account_room(sid: str, now: Optional[float] = None) -> Optional[float]:
    """The session's account % used: the fuller of the 5-hour and weekly windows (a passed reset
    counts as 0; Ultra has no 5-hour window), or None if unknown."""
    now_ts = time.time() if now is None else now
    acct = accounts.account_for(sid)
    paths = [accounts.usage_path(acct), state_dir() / "claude-usage.json"]
    u = None
    for p in paths:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    u = data
                    break
            except Exception:
                pass
    if not u:
        return None
    used = 0.0
    known = False
    for key in ("five_hour", "weekly"):
        reset_ts = _parse_reset(u.get(key + "_resets"))
        if reset_ts is not None and reset_ts < now_ts:
            known = True
            continue  # that window has reset: it counts as 0
        try:
            value = u.get(key)
            if value is not None and not isinstance(value, bool):
                used = max(used, float(value))
                known = True
        except (TypeError, ValueError):
            pass
    return used if known else None


def session_pid(sid: str) -> Optional[int]:
    from .inbox import session_pid as _session_pid  # lazy: inbox imports this module
    return _session_pid(sid)


def pane_for_session(sid: str) -> Optional[str]:
    from .inbox import pane_for_session as _pane_for_session
    return _pane_for_session(sid)


def at_prompt(pane: str) -> bool:
    """True only for an idle input box: not busy, and not a permission/plan/question menu."""
    from .inbox import _tmux
    try:
        out = _tmux("capture-pane", "-p", "-t", pane)
        if out.returncode != 0:
            return False
        tail = out.stdout.splitlines()[-12:]
        if any(MENU.search(l) for l in tail):
            return False
        return (any(l.lstrip("│ ").startswith("❯") for l in tail)
                and not any("esc to interrupt" in l for l in tail))
    except Exception:
        return False


def type_into(pane: str, text: str) -> None:
    """Type the text and press Enter; raises when tmux refuses or the line was not sent, so no nudge is
    recorded. A tip notice is closed first, and text that Enter did not send is taken out of the input box
    again (only the typed text: four "📩 Telegram" once sat unsent in one box)."""
    from .inbox import _tmux, dismiss_tip, input_box, submitted, take_back, typed_mark
    clean = " ".join(text.split())
    dismiss_tip(pane)
    before, _ = input_box(pane)
    if before:  # unsent text is never typed after (the owner's draft would be sent along with the line)
        raise OSError("input box holds unsent text")
    typed_mark(clean)
    from .dialogs import tmux_text
    first = _tmux("send-keys", "-t", pane, "-l", "--", tmux_text(clean))
    if first.returncode:
        raise OSError("tmux send-keys failed")
    second = _tmux("send-keys", "-t", pane, "Enter")
    if second.returncode:
        try:
            take_back(pane, clean)  # do not leave half a nudge in the input box
        except Exception:
            pass
        raise OSError("tmux send-keys failed")
    if not submitted(pane, clean, before):
        try:
            take_back(pane, clean)
        except Exception:
            pass
        raise OSError("typed text was not sent")


def _transcript_globs() -> list:
    return [str(accounts.claude_home() / "projects" / "*" / "*.jsonl"),
            str(accounts.profiles_dir() / "*" / "projects" / "*" / "*.jsonl"),
            str(Path.home() / ".claude" / "projects" / "*" / "*.jsonl")]


def owner_back(since: float) -> bool:
    """A prompt typed by hand in any session after away mode began: the owner is working again."""
    seen = set()
    for pattern in _transcript_globs():
        for path in glob.glob(pattern):
            if path in seen:
                continue
            seen.add(path)
            try:
                if os.path.getmtime(path) < since:
                    continue
                with open(path, "rb") as f:
                    f.seek(max(0, os.path.getsize(path) - 300_000))
                    lines = f.read().decode("utf-8", "replace").splitlines()
            except OSError:
                continue
            for raw in lines:
                if '"type":"user"' not in raw.replace(" ", ""):
                    continue
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                content = (e.get("message") or {}).get("content")
                if e.get("isMeta") or not isinstance(content, str) or content.startswith(("[Autopilot", "<")):
                    continue
                when = _parse_reset(e.get("timestamp"))
                if when is not None and when > since + 60:
                    return True
    return False


def find_transcript(sid: str) -> Optional[str]:
    import glob
    found = status.transcript_of(sid)
    if found and os.path.exists(found):
        return found
    candidates = []
    pats = [
        str(accounts.claude_home() / "projects" / "*" / f"{sid}.jsonl"),
        str(accounts.profiles_dir() / "*" / "projects" / "*" / f"{sid}.jsonl"),
        str(Path.home() / ".claude" / "projects" / "*" / f"{sid}.jsonl"),
        str(state_dir() / "transcripts" / f"{sid}.jsonl"),
    ]
    for p in pats:
        candidates.extend(glob.glob(p))
    if candidates:
        return max(candidates, key=os.path.getmtime)
    return None


def run_autopilot(cfg: Config, bot=None, dry_run: bool = False,
                  now: Optional[float] = None, idle_min: int = IDLE_MIN) -> int:
    """One pass of autopilot across all sessions. Passes never overlap: the whole run, including the
    state save, holds a lock, and a pass that finds it taken does nothing."""
    private_dir(state_dir())
    with open(state_dir() / "autopilot.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        return _run_locked(cfg, bot, dry_run, now, idle_min)


def _run_locked(cfg: Config, bot, dry_run: bool, now: Optional[float], idle_min: int) -> int:
    now_ts = time.time() if now is None else now
    away = away_active()
    since = 0.0
    if away:
        try:
            since = float(json.loads(away_path().read_text(encoding="utf-8")).get("since", 0))
        except (OSError, ValueError, AttributeError, TypeError):
            since = 0.0
        if now_ts - since > AWAY_MAX_HOURS * 3600 or owner_back(since):
            if not dry_run:
                set_away(False)
            away = False
    memory = load_memory()
    if not away:
        # Clear away_nudges counter when away mode is off
        memory = {k: {kk: vv for kk, vv in v.items() if kk != "away_nudges"} for k, v in memory.items()}

    bot = bot or (Bot.from_config(cfg) if cfg.ready else None)
    topics = TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg).all_places()

    for sid, entry in topics.items():
        thread = entry.get("thread")
        if not isinstance(thread, int):
            continue
        pane = pane_for_session(sid)
        if not pane:
            continue
        path = find_transcript(sid)
        if not path:
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if now_ts - mtime < idle_min:
            continue

        uuid, text = last_turn(path)
        mem = memory.setdefault(sid, {})
        if not uuid or mem.get("last") == uuid:
            continue

        room = account_room(sid, now=now_ts)
        if room is not None and room >= QUOTA_ROOM:
            continue  # wait for quota reset

        recent = [t for t in mem.get("quota_nudges", []) if isinstance(t, (int, float)) and now_ts - t < 5 * 3600]
        if (cfg.quota_auto_continue and QUOTA_WAIT.search(text[-1500:]) and len(recent) < MAX_QUOTA_NUDGES
                and (room is None or room < QUOTA_NUDGE_ROOM)):
            room_str = f"{int(room)}%" if room is not None else "?%"
            prompt = (f"[Autopilot] The Claude quota is back (window at {room_str}). "
                      "Continue the work you paused, with your own recommendations.")
            why = "quota"
        elif (away and in_away_scope(mtime, since) and "AUTOPILOT-IDLE" not in text
              and mem.get("away_nudges", 0) < MAX_AWAY_NUDGES):
            prompt = AWAY_PROMPT
            why = "away"
        else:
            continue

        try:
            from . import compact
            from .inbox import _nudge_guard
            with _nudge_guard(sid):  # the lock every nudge takes: one process types into the input box
                if compact.command_pending(sid) or not at_prompt(pane):
                    continue
                if dry_run:
                    print(sid[:8], why)
                    continue
                type_into(pane, prompt)
        except Exception:
            continue  # nothing typed (or tmux failed): no nudge recorded, no note sent

        mem["last"] = uuid
        if why == "quota":
            mem["quota_nudges"] = recent + [now_ts]
        if why == "away":
            mem["away_nudges"] = mem.get("away_nudges", 0) + 1

        note = t("autopilot_quota" if why == "quota" else "autopilot_away",
                 lang=getattr(cfg, "ui_language", "en"))
        if bot:
            try:
                bot.call("sendMessage", {"chat_id": entry["chat"], "message_thread_id": thread,
                                         "text": note, "disable_notification": True})
            except TelegramError:
                pass

    if not dry_run:
        save_memory(memory)
    return 0
