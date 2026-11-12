"""Context size and background jobs of a session, read from its transcript (no model call, no tokens)."""
from __future__ import annotations

import json
import hashlib
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import accounts
from . import models
from . import lang as L
from .i18n import t as translate_ui
from .config import Config, state_dir

TAIL = 6_000_000  # bytes of the transcript tail to search
PROJECTS = Path.home() / ".claude" / "projects"


def reset_time(body: str, stamp: datetime) -> datetime:
    """Interpret the reset clock in the system time zone, with a five-hour fallback. Understands
    "resets 12:50pm", "resets 8pm" and, for the weekly limit, "resets Nov 1 at 8pm" (the weekly form
    used to fall back to +5 hours and promised a reset days too early)."""
    try:
        local = stamp.astimezone().replace(tzinfo=None)
        match = re.search(r"resets\s+(?:([A-Z][a-z]{2})\s+(\d{1,2}),?\s+(?:at\s+)?)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
                          body, re.I)
        if not match:
            raise ValueError("no reset clock")
        day = None
        if match[1]:
            day = local.replace(month=("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec").index(match[1].lower()) + 1, day=int(match[2]))
            if day.date() < local.date():
                day = day.replace(year=day.year + 1)
        hour, minute = int(match[3]), int(match[4] or 0)
        period = (match[5] or "").lower()
        if minute > 59 or (period and not 1 <= hour <= 12) or (not period and hour > 23):
            raise ValueError("invalid reset clock")
        if period:
            hour = hour % 12 + (12 if period == "pm" else 0)
        reset = (day or local).replace(hour=hour, minute=minute, second=0, microsecond=0)
        seen = stamp.astimezone()
        # When clocks go back an hour repeats: try its first pass, then its second, then the next day.
        tries = [reset, reset.replace(fold=1)] + ([] if day else [reset + timedelta(days=1)])
        for candidate in tries:
            aware = candidate.astimezone()
            if aware >= seen.replace(second=0, microsecond=0):
                return aware
        return tries[-1].astimezone()
    except (ValueError, TypeError, AttributeError):
        return (stamp + timedelta(hours=5)).astimezone()


def _restarted_with_room(session: str, hit_at: float) -> bool:
    """The session was started again after the limit, on an account that is not spent (the owner
    moved it by hand, so no limit-cleared mark exists): the old error no longer holds its messages."""
    try:
        from . import accounts
        for rec in accounts.live_session_records():
            if rec.get("sessionId") == session and float(rec.get("startedAt") or 0) / 1000.0 > hit_at:
                return not accounts.spent(accounts.account_for(session))
    except Exception:
        pass
    return False


def reset_early(session: str, hit_at: float) -> bool:
    """The quota came back before its time (the owner spent a saved reset): a reading of the session's
    account taken after the limit was hit shows the account is no longer spent. Without this the old error
    held the session and its messages until the original reset time, days later."""
    try:
        from . import accounts
        name = accounts.origin_account(session)  # never account_for: its fallback is the default for NEW sessions
        reading = accounts._read(accounts.usage_path(name), {})
        return accounts.iso_epoch(reading.get("at")) > hit_at and not accounts.spent(name)
    except Exception:
        return False


def interrupted_by_limit(session: str) -> bool:
    """The session's last reply is still the quota error: the limit stopped it in the middle and nothing has
    been asked since. Tool results and background notices written after it are not prompts (ended_on_limit)."""
    try:
        path = transcript_of(session)
        return path is not None and ended_on_limit(str(path))
    except Exception:
        return False


def transcript_of(session: str) -> Optional[Path]:
    """The newest transcript file of this session across the account folders, or None."""
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session or ""):
            return None
        paths = (p / (session + ".jsonl") for p in PROJECTS.iterdir())
        return max((p for p in paths if p.is_file()), key=lambda p: p.stat().st_mtime, default=None)
    except OSError:
        return None


def prompt_since(session: str, since: float, needle: str = "") -> Optional[bool]:
    """Whether a real user prompt (containing `needle`) reached the session's transcript at or after
    `since` (epoch seconds). None when the transcript cannot be read: the caller cannot tell."""
    path = transcript_of(session)
    if path is None:
        return None
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            tail = f.read()
    except OSError:
        return None
    for raw in reversed(tail.splitlines()):
        try:
            row = json.loads(raw)
            if not isinstance(row, dict) or row.get("type") != "user" or row.get("isMeta"):
                continue
            if row.get("isCompactSummary") or row.get("isVisibleInTranscriptOnly"):
                continue  # "Resume from summary" or auto-compaction writes this, not the user
            stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            if stamp.timestamp() < since - 1:
                return False  # rows are in time order: everything older came before the prompt was typed
            content = (row.get("message") or {}).get("content")
            if isinstance(content, list):
                if not any(isinstance(b, dict) and b.get("type") == "text" for b in content):
                    continue  # a tool result, not a prompt
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            # "<command-name>", "<task-notification>" and the like are machine rows (the rule hooks.py uses)
            if isinstance(content, str) and not content.lstrip().startswith("<") and needle in content:
                return True
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return False


def limit_of(session: str) -> Optional[dict]:
    """Return the latest assistant row's usage limit, if any."""
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session):
            return None
        paths = (p / (session + ".jsonl") for p in PROJECTS.iterdir())
        path = max((p for p in paths if p.is_file()), key=lambda p: p.stat().st_mtime, default=None)
        if path is None:
            return None
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            tail = f.read()
        if size > 200_000:
            tail = tail.partition(b"\n")[2]
        for raw in reversed(tail.splitlines()):
            try:
                row = json.loads(raw)
                if row.get("type") != "assistant":
                    continue
                if row.get("isApiErrorMessage") is not True or row.get("apiError") != "usage_limit_reached":
                    return None
                stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    return None
                content = row.get("message", {}).get("content", [])
                body = content[0].get("text", "") if content and isinstance(content[0], dict) else ""
                reset = reset_time(body, stamp)
                if reset.timestamp() <= time.time():
                    return None  # the window already reset; the next reply will clear the error
                today = reset.date() == datetime.fromtimestamp(time.time()).date()
                rid = hashlib.sha256(raw).hexdigest()
                try:  # the session already moved off the account that hit this limit
                    if (state_dir() / "limit-cleared" / session).read_text(encoding="utf-8").strip() == rid:
                        return None
                except OSError:
                    pass
                if _restarted_with_room(session, stamp.timestamp()) or reset_early(session, stamp.timestamp()):
                    return None
                return {"id": rid, "reset": reset.timestamp(),
                        "clock": reset.strftime("%H:%M") if today else reset.strftime("%d.%m %H:%M")}
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                continue
    except OSError:
        pass
    return None


def ended_on_limit(path: str) -> bool:
    """The transcript's last turn ended on Claude's plan-limit reply, and nothing has been asked since
    (a later user prompt means a new turn is running). Such a session is not working: whatever marks
    it busy is stale (a stall notice ten minutes after a limit reply)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            tail = f.read()
        if size > 200_000:
            tail = tail.partition(b"\n")[2]
    except OSError:
        return False
    for raw in reversed(tail.splitlines()):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("type") not in ("assistant", "user"):
            continue
        if row.get("type") == "user":
            content = (row.get("message") or {}).get("content")
            if row.get("isMeta"):
                continue
            if isinstance(content, list) and not any(isinstance(b, dict) and b.get("type") == "text" for b in content):
                continue  # a tool result, not a prompt
            return False
        content = (row.get("message") or {}).get("content")
        text = " ".join(b.get("text", "") for b in content if isinstance(b, dict)) if isinstance(content, list) else ""
        return bool(row.get("apiError") == "usage_limit_reached" or accounts.QUOTA_HIT.search(text))
    return False


def _is_boundary(row: dict) -> bool:
    return row.get("subtype") == "compact_boundary" or row.get("compact_boundary") is True


def _summary_tokens(row: dict) -> int:
    """Rough size of the summary a compaction left (about 4 characters per token)."""
    content = (row.get("message") or {}).get("content")
    if isinstance(content, list):
        content = "".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return len(content) // 4 if isinstance(content, str) else 0


def context_state(path: str) -> Tuple[int, str, bool]:
    """(tokens, model, compacted): the newest assistant record with usage: input + cache read + cache
    creation, i.e. what the last request held in context. When a compaction is newer than every such
    record the old number is stale: tokens is compactMetadata.postTokens (or the summary's size) and
    compacted is True. (0, "", False) when unknown."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return 0, "", False
    summary = 0
    for line in reversed(lines):
        if '"compact_boundary"' in line:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and _is_boundary(row):
                post = (row.get("compactMetadata") or {}).get("postTokens")
                return (int(post) if isinstance(post, (int, float)) and post else summary), "", True
        if '"isCompactSummary"' in line and not summary:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("isCompactSummary"):
                summary = _summary_tokens(row)
            continue
        if '"usage"' not in line or '"assistant"' not in line:
            continue
        try:
            msg = json.loads(line).get("message") or {}
        except (ValueError, AttributeError):
            continue
        u = msg.get("usage") or {}
        ctx = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                 "cache_creation_input_tokens"))
        if ctx:
            return ctx, str(msg.get("model") or ""), False
    return 0, "", False


def context_tokens(path: str) -> Tuple[int, str]:
    """(tokens, model) of what the last request held in context, never the size from before a newer
    compaction. (0, "") when unknown."""
    tokens, model, _ = context_state(path)
    return tokens, model


def _parse_time(val) -> Optional[float]:
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        try:
            return float(val)
        except ValueError:
            pass
        try:
            import calendar
            return float(calendar.timegm(time.strptime(val, "%Y-%m-%dT%H:%M:%SZ")))
        except Exception:
            pass
        try:
            from datetime import datetime
            return datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    return None


def has_five_hour(u: dict) -> bool:
    """False for an account with only a weekly window: its usage file names the 5-hour reset and leaves it
    empty. A file without that key (older writers) is taken to have a 5-hour window."""
    return not ("five_hour_resets" in u and not u["five_hour_resets"])


def big_plan(account: str) -> bool:
    """The Ultra account: it has no 5-hour window, so no 5-hour number is shown for it."""
    try:
        return accounts.is_big_plan(account)
    except Exception:
        return False


def window_5h(u: dict, account: str) -> bool:
    """Whether this account's usage shows a 5-hour number: never. Claude's and Codex's plans here have
    only a weekly limit (plus a bank of resets); a 5-hour number misled every reader. The
    recorded five_hour fields stay for the account logic; they are just not shown."""
    return False


def banked_resets(u: dict) -> Optional[int]:
    """The number of banked resets a usage record carries (`banked_resets`), or None when it does not
    say: no number is ever guessed."""
    value = u.get("banked_resets") if isinstance(u, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value)


def when(reset, now: Optional[float] = None, lang: Optional[str] = None) -> str:
    """Reset mark: same day -> '↻ HH:MM', within 24 h -> '↻ מחר HH:MM', else '↻ dd.mm HH:MM'."""
    if reset == "rolling":
        return "↻ " + translate_ui("reset_rolling", lang=lang)
    try:
        if isinstance(reset, str):
            import calendar
            reset = calendar.timegm(time.strptime(reset[:19].replace(" ", "T"), "%Y-%m-%dT%H:%M:%S"))
        elif hasattr(reset, "timestamp"):
            reset = reset.timestamp()
        t = time.localtime(float(reset))
    except Exception:
        return ""
    now_ts = now if now is not None else time.time()
    now_t = time.localtime(now_ts)
    if time.strftime("%d.%m", t) == time.strftime("%d.%m", now_t):
        return "↻ " + time.strftime("%H:%M", t)
    if time.mktime(t) - now_ts < 86400:
        return "↻ " + translate_ui("reset_tomorrow", lang=lang) + " " + time.strftime("%H:%M", t)
    return "↻ " + time.strftime("%d.%m %H:%M", t)


def quota_line(lang: str, state_path: Optional[Path] = None, now: Optional[float] = None,
               session: Optional[str] = None, account: Optional[str] = None) -> str:
    """Claude plan usage ({five_hour, weekly, at}; hidden when older than 12 h; ⚠️ at >= 80)."""
    path = state_path or claude_usage_file(session=session, account=account)
    if not path.exists():
        return ""
    try:
        u = json.loads(path.read_text(encoding="utf-8"))
        at_ts = _parse_time(u.get("at"))
        if at_ts is None:
            return ""
        now_ts = now if now is not None else time.time()
        age = now_ts - at_ts
    except Exception:
        return ""
    if age > 12 * 3600 or age < 0:
        return ""
    s = L.strings(lang)
    ago = s["minutes_ago"].format(n=int(age // 60)) if age < 3600 else s["hours_ago"].format(n=int(age // 3600))
    weekly = int(u.get("weekly", 0))
    warn = " ⚠️" if weekly >= 80 else ""
    # Weekly only, with its reset time and the banked resets when the record has them (no 5-hour window).
    reset_time_str = when(u.get("weekly_resets"), now=now_ts, lang=lang)
    if reset_time_str and L.base(lang) == "he":  # "07.11 15:59" would read as "15:59 07.11" inside RTL text
        reset_time_str = re.sub(r"\d{1,2}[.:]\d{2}(?: \d{1,2}[.:]\d{2})?", "\u2066\\g<0>\u2069", reset_time_str)
    reset_part = f" · {reset_time_str}" if reset_time_str else ""
    bank = banked_resets(u)
    banked = s["quota_banked"].format(n=bank) if bank is not None else ""
    return s["quota_line_weekly"].format(weekly=weekly, warn=warn, reset=reset_part, banked=banked, ago=ago)


# -- per-account Claude quota and team limits -------------

def claude_account(session: Optional[str] = None, env: Optional[dict] = None, transcript_path: str = "",
                   use_record: bool = True) -> str:
    """The Claude account this process runs on: the session's profile, else the default for new
    sessions (two topics showed each other's quota from one shared file).
    use_record=False is for whoever WRITES usage (the status line): the process's own CLAUDE_CONFIG_DIR,
    else the profile in its transcript path, never session-accounts.json. That file names the account a
    session is meant to be on, and a limited session whose move had not happened yet wrote its numbers
    into the target account's file (two accounts showed identical usage)."""
    cfg = ((env if env is not None else os.environ).get("CLAUDE_CONFIG_DIR") or "").rstrip("/")
    if "/.claude-profiles/" in cfg:
        return os.path.basename(cfg)
    try:
        if cfg and Path(cfg).parent == accounts.profiles_dir():
            return Path(cfg).name
    except Exception:
        pass
    if not cfg and transcript_path:
        marker = "/.claude-profiles/"
        if marker in transcript_path:
            return transcript_path.split(marker, 1)[1].split("/", 1)[0]
        try:
            if Path(transcript_path).is_relative_to(accounts.profiles_dir()):
                return Path(transcript_path).relative_to(accounts.profiles_dir()).parts[0]
        except Exception:
            pass
    if session and use_record:
        acct = accounts.account_for(session)
        if acct:
            return acct
    # No profile and no record: the main account. Never the default for NEW sessions
    # (`/account use`), or main's usage lands in another account's file (review of 3b3c59a).
    return accounts.name_from_env(env)


def claude_usage_file(session: Optional[str] = None, account: Optional[str] = None) -> Path:
    """That account's own quota file from the status line; the shared last-writer file only as
    a fallback."""
    acct = account or claude_account(session)
    p = state_dir() / f"claude-usage-{acct}.json"
    if p.exists():
        return p
    fallback = state_dir() / "claude-usage.json"
    if fallback.exists():
        return fallback
    return fallback


def claude_five_hour(session: Optional[str] = None, account: Optional[str] = None,
                     now: Optional[float] = None) -> Optional[float]:
    """Claude's 5h quota % off the status line; None when missing or over 12 hours old
    (same file and freshness rule as quota_lines)."""
    try:
        import calendar
        path = claude_usage_file(session=session, account=account)
        u = json.loads(Path(path).read_text(encoding="utf-8"))
        at_ts = _parse_time(u.get("at"))
        now_ts = now if now is not None else time.time()
        if at_ts is not None and now_ts - at_ts <= 12 * 3600:
            if not window_5h(u, account or claude_account(session)):
                return None
            return float(u["five_hour"])
    except Exception:
        pass
    return None


def codex_window(minutes: int) -> str:
    """The window a Codex rate limit covers, from its own length: "week" for a week or more, "5h" for a
    few hours, else "". The primary window is not always the short one (on the Pro plan it is the week)."""
    if minutes >= 6 * 24 * 60:
        return "week"
    if 0 < minutes <= 6 * 60:
        return "5h"
    return ""


def codex_limits(sessions_dir: Optional[Path] = None) -> Optional[Dict[str, Tuple[float, int]]]:
    """Codex CLI windows from the newest rollout's rate_limits event, named by their real length
    (`window_minutes`): {"week": (pct, reset)} and, only if a short window exists, "5h". None when
    there is no reading. Opt-in: only reads the folder named by the `codex_sessions_dir` setting."""
    if not sessions_dir:
        return None
    folder = Path(sessions_dir).expanduser()
    try:
        if not folder.exists():
            return None
        files = sorted(folder.glob("*/*/*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    except OSError:
        return None
    for f in files:
        try:
            with open(f, "rb") as fh:
                fh.seek(max(0, os.path.getsize(f) - 400_000))
                tail = fh.read().decode("utf-8", "ignore")
            hits = re.findall(r'"rate_limits":(\{"limit_id":"codex".*?"credits")', tail)
            if hits:
                out = {}
                for key in ("primary", "secondary"):
                    m = re.search(r'"%s":\{"used_percent":([\d.]+),"window_minutes":(\d+),"resets_at":(\d+)' % key, hits[-1])
                    name = codex_window(int(m.group(2))) if m else ""
                    if name and name not in out:
                        out[name] = (float(m.group(1)), int(m.group(3)))
                if out:
                    return out
        except OSError:
            continue
    return None


def file_limits(spec: dict, now: Optional[float] = None, lang: str = "he") -> list:
    """Quota rows from one user-supplied JSON file (the `quota_files` setting). Any tool can write it:

        {"at": "2026-01-01T12:00:00Z",
         "rows": [{"window": "5h", "percent": 40, "resets": 1767268800, "label": "Tool"},
                  {"window": "week", "percent": 12}]}

    window is "5h", "week" or "month"; resets is an epoch, an ISO time or "rolling" (optional); the row
    label falls back to the setting's `label`. A file older than `max_age_hours` (default 12) is ignored."""
    now_ts = now if now is not None else time.time()
    try:
        d = json.loads(Path(str(spec["path"])).expanduser().read_text(encoding="utf-8"))
        at_ts = _parse_time(d.get("at"))
        max_age = float(spec.get("max_age_hours") or 12) * 3600
        if at_ts is None or now_ts - at_ts > max_age:
            return []
        out = []
        for row in d.get("rows") or []:
            if not isinstance(row, dict) or row.get("window") not in ("5h", "week", "month"):
                continue
            pct = row.get("percent")
            if isinstance(pct, bool) or not isinstance(pct, (int, float)):
                continue
            name = str(spec.get("label") or row.get("label") or "").strip()
            if row.get("label") and spec.get("label"):
                name = f"{spec['label']} {row['label']}"
            if name:
                out.append((row["window"], name[:40], float(pct), row.get("resets", "rolling")))
        return out
    except Exception:
        return []


def dot(pct: float) -> str:
    return "🔴" if pct >= 80 else "🟡" if pct >= 50 else "🟢"


def quota_rows(full: bool = False, session: Optional[str] = None, account: Optional[str] = None,
               now: Optional[float] = None, lang: str = "he",
               cfg: Optional[Config] = None) -> List[Tuple[str, str, float, object]]:
    """Quota rows per window: (window, team, % used, reset).
    Reads the session's account file (claude-usage-<acct>.json), label "Claude <acct>" when not main.
    Other tools show up only when the config opts in (`codex_sessions_dir`, `quota_files`)."""
    now_ts = now if now is not None else time.time()
    rows: List[Tuple[str, str, float, object]] = []
    acct = account or claude_account(session)
    usage_file = claude_usage_file(session=session, account=acct)
    is_he = (lang == "he" or L.base(lang) == "he")
    try:
        u = json.loads(Path(usage_file).read_text(encoding="utf-8"))
        at_ts = _parse_time(u.get("at"))
        if at_ts is not None and now_ts - at_ts <= 12 * 3600:
            base_label = translate_ui("quota_claude", lang=lang)
            who = base_label + ("" if acct == "main" else " " + acct)
            weekly = float(u.get("weekly", 0))
            rows.append(("week", who, weekly, u.get("weekly_resets")))  # weekly only: no 5-hour window
            if full:
                for x in u.get("extra") or []:
                    if isinstance(x, dict) and isinstance(x.get("percent"), (int, float)):
                        kind = str(x.get("kind") or "")
                        label = next((m for m in ("Opus", "Sonnet", "Haiku", "Fable") if m.lower() in kind.lower()), kind)
                        if "five" in kind or "session" in kind:
                            continue  # a 5-hour row: not a limit of this plan
                        rows.append(("week", f"{base_label} {label}", float(x["percent"]), x.get("resets_at")))
    except Exception:
        pass
    try:
        if cfg is None:
            try:
                from . import config as _config
                cfg = _config.load()
            except Exception:
                cfg = Config()
        c = codex_limits(cfg.codex_sessions_dir) or {}
        codex_name = translate_ui("quota_codex", lang=lang)
        if "week" in c:  # Codex: the weekly limit only (AGY and OpenCode files keep their short windows)
            rows.append(("week", codex_name, float(c["week"][0]), c["week"][1]))
        for spec in cfg.quota_files:
            rows += file_limits(spec, now=now_ts, lang=lang)
    except Exception:
        pass
    return rows


def quota_lines(full: bool = False, table: bool = False, session: Optional[str] = None,
                account: Optional[str] = None, now: Optional[float] = None,
                lang: str = "he", cfg: Optional[Config] = None) -> list:
    rows = quota_rows(full=full, session=session, account=account, now=now, lang=lang, cfg=cfg)
    if table:
        return rows
    lines = []
    is_he = (lang == "he" or L.base(lang) == "he")
    titles = (("5h", translate_ui("quota_five_hours", lang=lang)),
              ("week", translate_ui("quota_week", lang=lang)),
              ("month", translate_ui("quota_month", lang=lang)))
    for window, title in titles:
        group = [(team, p, r) for w, team, p, r in rows if w == window]
        if group:
            lines += ["", title] + [f"{dot(p)} {team} \u2066{p:.0f}%\u2069" for team, p, _ in group]
            resets = [f"{team} {when(r, now=now, lang=lang)[2:]}" for team, _, r in group if r != "rolling" and when(r, now=now, lang=lang)]
            if resets:
                lines.append("↻ " + " · ".join(resets))
    return lines



def background_commands(path: str) -> list:
    """The full commands of background Bash jobs still running in a session, oldest first: tool_use
    with run_in_background whose launch was confirmed and that has no final task-notification.
    Same reading as background_of, but it keeps the command text instead of a label. [] when unreadable."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    launched, running = {}, {}
    for line in lines:
        if not any(k in line for k in ("run_in_background", "tool_result", "task-id")):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        content = (ev.get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == "Bash" and (b.get("input") or {}).get("run_in_background"):
                launched[b.get("id")] = str(b["input"].get("command") or "")
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in launched:
                text = json.dumps(b.get("content"), ensure_ascii=False)
                for task in re.findall(r"with ID: ([A-Za-z0-9]+)", text):
                    running[task] = launched[b["tool_use_id"]]
        for task, status in re.findall(r"<task-id>([A-Za-z0-9]+)</task-id>.{0,600}?<status>(\w+)</status>", line):
            if status in ("completed", "killed", "failed", "stopped"):
                running.pop(task, None)
    return [c for c in running.values() if c.strip()]


def background_of(path: str, now: Optional[float] = None) -> Dict[str, str]:
    """Background agents and commands still running in a session (transparency),
    from its transcript: started (Agent/Bash in background, a resumed agent) minus finished
    (task-notification with a final status). {task_id: label}."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return {}
    running, labels, names, started = {}, {}, {}, {}
    for line in lines:
        if not any(k in line for k in ("run_in_background", "tool_result", "task-id")):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        content = (ev.get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and (b.get("input") or {}).get("run_in_background"):
                inp = b["input"]
                tier = " · ".join(x for x in (inp.get("model"), inp.get("effort")) if x)
                if b.get("name") != "Agent":
                    cmd = inp.get("command") or ""
                    tier = "codex exec" if "codex exec" in cmd else ""
                labels[b.get("id")] = ("🤖 " if b.get("name") == "Agent" else "⚙️ ") + (
                    inp.get("description") or "")[:60] + (f" ({tier})" if tier else "")
            elif b.get("type") == "tool_result":
                text = json.dumps(b.get("content"), ensure_ascii=False)
                # Only the result of a background launch counts; any other output that merely
                # quotes "with ID: x" (e.g. a grep of the transcript) must not re-register a job.
                for task in (re.findall(r"(?:agentId: |with ID: )([A-Za-z0-9]+)", text)
                             if b.get("tool_use_id") in labels else []):
                    running[task] = names[task] = labels.get(b.get("tool_use_id"), "⚙️")
                    started[task] = ev.get("timestamp") or ""
                for task in re.findall(r'resumedAgentId\\?":\s*\\?"([A-Za-z0-9]+)', text):
                    running[task] = names.get(task, "🤖")
        # Notifications are stored as queue-operation events with a top-level "content" string,
        # so match the raw line (finished jobs stayed listed as running).
        for task, status in re.findall(r"<task-id>([A-Za-z0-9]+)</task-id>.{0,600}?<status>(\w+)</status>", line):
            if status in ("completed", "killed", "failed", "stopped"):
                running.pop(task, None)
    # A job whose finish notice fell outside the read window or never came (e.g. lost across a
    # compaction) must not show forever: drop anything started over 3 hours ago.
    import datetime
    if now is not None:
        cutoff_dt = datetime.datetime.fromtimestamp(now, datetime.timezone.utc) - datetime.timedelta(hours=3)
    else:
        cutoff_dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=3)
    cutoff = cutoff_dt.isoformat()
    return {t: l for t, l in running.items() if not started.get(t) or started[t] >= cutoff[:19]}


TASK_CUT = 48  # a background task label is cut here so a bullet stays one phone line


def _cut(label: str, limit: int = TASK_CUT) -> str:
    return label if len(label) <= limit else label[:limit - 1].rstrip() + "…"


def background_lines(transcript: str, lang: str, bullets: bool = True) -> List[str]:
    """'⚙️ Background (N):' and one bullet per running task (at most 6). In Hebrew a task that still
    mixes scripts is split like any Hebrew text (fmt.unmix): its English part gets its own line."""
    try:
        bg = background_of(transcript)
    except Exception:
        bg = {}
    names = sorted({v for v in bg.values() if v.strip(" 🤖⚙️")})
    if not names:
        return []
    from .translate import job_hebrew, mostly_english_label
    he = L.base(lang) == "he"
    formatted = []
    for n in names:
        icon, _, label = n.partition(" ")
        if he and mostly_english_label(label):
            try:
                label = job_hebrew(label)
            except Exception:
                pass
        formatted.append(f"{icon} {_cut(label)}".strip())
    s = L.strings(lang)
    header = s["bg_header"].format(n=len(formatted))
    if not bullets:
        return [header.rstrip(":")]
    out = [header]
    for n in formatted[:6]:
        line = "• " + n
        if he:
            from .fmt import unmix
            first, *rest = unmix(line).split("\n")
            out += [first] + ["   " + r for r in rest]
        else:
            out.append(line)
    if len(formatted) > 6:
        out.append(s["bg_more"].format(n=len(formatted) - 6))
    return out


def background_line(transcript: str, lang: str) -> str:
    """The background task list as text (see background_lines)."""
    return "\n".join(background_lines(transcript, lang))


def changed_files(path: str) -> list:
    files = []
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                message = event.get("message") or {}
                content = message.get("content")
                if event.get("type") == "user" and not event.get("isMeta") and (
                    isinstance(content, str) or isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "text" for b in content)):
                    files = []
                if event.get("type") == "assistant" and isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in ("Edit", "Write", "NotebookEdit"):
                            inp = block.get("input") or {}
                            name = os.path.basename(inp.get("file_path") or inp.get("notebook_path") or "")
                            if name and name not in files:
                                files.append(name)
    except (OSError, TypeError, AttributeError):
        pass
    return files


def changed_lines(transcript: str, lang: str = "he") -> List[str]:
    """'✏️ Files changed: N' and, on the next line, the first three names (file names are English)."""
    names = changed_files(transcript)
    if not names:
        return []
    more = translate_ui("footer_shown4", lang=lang).replace("4", "3") if len(names) > 3 else ""
    return [translate_ui("footer_changed", lang=lang, n=len(names), more=more), ", ".join(names[:3])]


def changed_line(transcript: str, lang: str = "he") -> str:
    return "\n".join(changed_lines(transcript, lang))


def compact_footer(cfg: Config, transcript: str) -> str:
    """The footer of a short reply: the same lines without the task list and changed files."""
    return "\n".join(footer_lines(cfg, transcript, compact=True))


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


REPLY_CHUNK = 256 * 1024  # bytes read from the transcript's end first: the newest reply is almost always there
_reply_models: dict = {}  # path -> (size, mtime_ns, result): the panel asks for every session every few seconds


def _tail_lines(path: str, size: int, span: int) -> List[str]:
    with open(path, "rb") as f:
        f.seek(max(0, size - span))
        return f.read().decode("utf-8", "replace").splitlines()


def last_reply_model(path: Optional[str]) -> Tuple[str, str]:
    """(model display name, thinking level) of the session's newest own reply in its transcript; "" for
    what is unknown. A session without a terminal status line (started from the app, a remote-control
    server) never saves them, so its topic's buttons read them here. A /effort answer newer than that
    reply wins for the level. Cheap when called often: a small tail first, and one read per file change."""
    model, level, switched, _ = _reply_facts(path)
    return model, ((switched or level) if model else "")


def effort_switch(path: Optional[str]) -> Tuple[str, Optional[float]]:
    """(level, when) a /effort command set after the session's newest reply; ("", None) when none did. `when` is
    the transcript row's time, None when it has none."""
    return _reply_facts(path)[2:]


def _reply_facts(path: Optional[str]) -> Tuple[str, str, str, Optional[float]]:
    """(model, the reply's own level, a later /effort level) of the newest own reply; read once per file change."""
    if not path:
        return "", "", "", None
    try:
        st = os.stat(path)
    except OSError:
        return "", "", "", None
    # the file itself, not only its path: a transcript swapped for another keeps neither inode nor change time
    stamp = (st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns)
    known = _reply_models.get(path)
    if known and known[0] == stamp:
        return known[1]
    out = ("", "", "", None)
    for span in (REPLY_CHUNK, TAIL):
        try:
            lines = _tail_lines(path, st.st_size, span)
        except OSError:
            return "", "", "", None
        out = _reply_scan(lines)
        if out[0] or span >= st.st_size:
            break
    if len(_reply_models) > 300:
        _reply_models.clear()
    _reply_models[path] = (stamp, out)
    return out


def _reply_model_in(lines: List[str]) -> Tuple[str, str]:
    model, level, switched, _ = _reply_scan(lines)
    return model, (switched or level) if model else ""


def _reply_scan(lines: List[str]) -> Tuple[str, str, str, Optional[float]]:
    """(model, the reply's own level, the level a later /effort set, that row's time) of the newest reply."""
    switched, switched_at = "", None  # the level a /effort command set after the newest reply
    for line in reversed(lines):
        if "Set effort level to" in line and not switched:
            try:
                text = models._row_text(json.loads(line))  # None for a background agent's row
            except ValueError:
                text = None
            m = re.match(r"\s*<local-command-stdout>\s*Set effort level to (\w+)", text or "")
            if m and m.group(1) in EFFORT_LEVELS:
                switched = m.group(1)
                try:
                    switched_at = _parse_time(json.loads(line).get("timestamp"))
                except (ValueError, AttributeError):
                    switched_at = None
            continue
        if '"assistant"' not in line or '"model"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("type") != "assistant" or row.get("isSidechain"):
            continue  # a background agent's reply says nothing about the session's own model
        msg = row.get("message") if isinstance(row.get("message"), dict) else {}
        model = str(msg.get("model") or "")
        if not model or model.startswith("<"):  # "<synthetic>": an error line, not a model
            continue
        level = row.get("effort")
        return models.display_name(model), (level if level in EFFORT_LEVELS else ""), switched, switched_at
    return "", "", switched, switched_at


def _model_time(session: str) -> Optional[float]:
    """When the status line last reported a changed model or level for the session (statusline.save_model)."""
    try:
        value = json.loads((state_dir() / "session-models-at.json").read_text(encoding="utf-8")).get(session)
    except (OSError, ValueError, AttributeError):
        return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def session_model(session: str, transcript: Optional[str] = None) -> Tuple[str, str]:
    """(model display name, thinking level) of a session ("" when unknown): what the status line saved
    (else what the newest reply in the transcript used: last_reply_model),
    unless a /model answer in the transcript is newer than the last reply (the card kept
    showing Fable 5.1 after "Set model to Opus 5.5", until the next turn redrew the status line)."""
    try:
        saved = json.loads((state_dir() / "session-models.json").read_text(encoding="utf-8")).get(session, "")
    except (OSError, ValueError, AttributeError):
        saved = ""
    model, _, level = (saved if isinstance(saved, str) else "").partition("|")
    try:
        if transcript is None and session:
            from .status import transcript_of
            transcript = transcript_of(session)
        switched = models.latest_switch(transcript)
    except Exception:
        switched = ""
    if not model or not level:  # no status line saved them: the newest reply knows both
        try:
            seen_model, seen_level = last_reply_model(str(transcript) if transcript else "")
        except Exception:
            seen_model, seen_level = "", ""
        borrowed = ""
        if not model:
            model, borrowed = seen_model, seen_level
        elif seen_model == model:
            borrowed = seen_level
        if not level and borrowed and not (switched and switched != model):
            level = borrowed  # after a /model switch the old model's level says nothing about the new one
    if not (switched and switched != model):  # a /effort typed after the newest reply, unless the status line
        try:  # reported a level after it (each is compared by when it was written)
            typed, typed_at = effort_switch(str(transcript) if transcript else "")
            reported_at = _model_time(session) if level else None
            if typed and (typed_at is None or reported_at is None or typed_at >= reported_at):
                level = typed
        except Exception:
            pass
    return switched or model, level


def context_window(session: Optional[str], model: str = "") -> int:
    """The size of the session's context window in tokens: what its status line reported, else 1,000,000 for a
    model named with a 1M window; 0 when unknown (no guess)."""
    if session:
        try:
            size = json.loads((state_dir() / "session-windows.json").read_text(encoding="utf-8")).get(session)
            if type(size) is int and size > 0:
                return size
        except (OSError, ValueError, AttributeError):
            pass
    return 1_000_000 if re.search(r"\b1M\b|\[1m\]", model or "") else 0


def stamp_parts(session: str, fallback_model: str, lang: str) -> Tuple[str, str, str]:
    """(model, thinking level in `lang`, Claude account name) for the footer; "" for what is unknown.
    The model comes from the status line, else from the transcript; the account only once there is
    more than one."""
    model, level = session_model(session)
    s = L.strings(lang)
    return (model or fallback_model, s.get("effort_" + level, level) if level else "",
            accounts.label(accounts.name_from_env()) if accounts.multiple() else "")


def stamp(session: str, fallback_model: str, lang: str) -> str:
    """"Opus 5.5 · 🎚️ גבוהה · 👤 name": which model, thinking level and (once there is more than one)
    Claude account answered."""
    model, level, account = stamp_parts(session, fallback_model, lang)
    parts = [model]
    if level:
        parts.append("\U0001F39A\uFE0F " + level)
    if account:
        parts.append("\U0001F464 " + account)
    return " \u00b7 ".join(p for p in parts if p)


def footer_lines(cfg: Config, transcript: str, compact: bool = False) -> List[str]:
    """The status footer, one fact per line and no line mixing Hebrew with English (the phone
    scrambles them). English: '🧠 184k · Opus 5.5 · medium', '👤 name', quota, background tasks.
    Hebrew: the context, the model and thinking level, the account and the quota each on their own
    line; a short Latin name is isolated, a long one gets its own line.
    `compact` (a short reply): no task list and no changed files."""
    if not cfg.context_footer or not transcript:
        return []
    from .fmt import isolate
    tokens, model = context_tokens(transcript)
    lang = cfg.target_language
    changed = [] if compact else changed_lines(transcript, lang)
    if not tokens:
        return changed
    s = L.strings(lang)
    name, level, account = stamp_parts(Path(transcript).stem, models.display_name(model), lang)
    context = s["context"].format(k=f"{round(tokens / 1000):,}")
    if L.base(lang) == "he":
        lines = [context]
        parts = []
        if name and isolate(name) is None:  # a long model name: its own line
            lines.append("\U0001F916 " + name)
        elif name:
            parts.append("\U0001F916 " + isolate(name))
        if level:
            parts.append("\U0001F39A\uFE0F " + level)
        if parts:
            lines.append(" · ".join(parts))
        if account:
            lines.append("\U0001F464 " + (isolate(account) or account))
    else:
        lines = [" · ".join(p for p in (context, name, level) if p)]
        if account:
            lines.append("\U0001F464 " + account)
    try:  # the permission mode, active agent and output style, also when changed in Claude itself (direct.py)
        from . import direct
        lines += direct.card_lines(Path(transcript).stem, lang)
    except Exception:
        pass
    if cfg.context_hint_tokens and tokens >= cfg.context_hint_tokens:
        lines.append(s["compact_hint"])
    lines += [l for l in quota_line(lang, session=Path(transcript).stem).split("\n") if l]
    lines += background_lines(transcript, lang, bullets=not compact)
    return lines + changed


def footer(cfg: Config, transcript: str) -> str:
    """The status footer as text, one fact per line (see footer_lines)."""
    return "\n".join(footer_lines(cfg, transcript))


def context_footer(transcript: str, cfg: Optional[Config] = None, session: Optional[str] = None) -> str:
    """Full context footer under a Telegram reply."""
    if not transcript:
        return ""
    import html
    sid = session or Path(transcript).stem
    tokens, _ = context_tokens(transcript)
    if not tokens:
        return ""
    lang = getattr(cfg, "ui_language", None) if cfg else None
    tip = translate_ui("footer_tip", lang=lang) if tokens >= 400_000 else ""
    bg = background_of(transcript)
    names = sorted({v for v in bg.values() if v.strip(" 🤖⚙️")})
    lines = [translate_ui("footer_context", lang=lang, k=f"{round(tokens / 1000):,}", tip=tip)]
    lines = [html.escape(l, quote=False) for l in lines]
    try:
        from .dashboard import quotas
        q_lines = quotas(accounts_flag=False, full=False, session=sid, cfg=cfg)
        if q_lines:
            lines += [""] + ["\0" + l for l in q_lines]
    except Exception:
        ql = quota_lines(session=sid)
        if ql:
            lines += [html.escape(l, quote=False) for l in ql]
    changed = changed_files(transcript)
    if changed:
        more = translate_ui("footer_shown4", lang=lang) if len(changed) > 4 else ""
        lines += ["", translate_ui("footer_changed", lang=lang, n=len(changed), more=more)]
        lines += ["\0<code>" + html.escape(", ".join(changed[:4]), quote=False) + "</code>"]
    if names:
        lines += ["", translate_ui("footer_background", lang=lang, n=len(names))]
        from .translate import job_hebrew, mostly_english_label
        for n in names[:5]:
            icon, _, label = n.partition(" ")
            if mostly_english_label(label):
                try:
                    label = job_hebrew(label)
                except Exception:
                    pass
            lines.append(html.escape(f"   {icon} {label}", quote=False))
    return "\n".join(l[1:] if l.startswith("\0") else L.RLM + l if l else "" for l in lines)

