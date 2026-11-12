"""One private pinned dashboard, plus an explicitly requested morning summary."""
from __future__ import annotations

import fcntl
import html
import json
import time
from pathlib import Path
from typing import List, Optional

from . import accounts, flood, identity, lang as L, menus, status
from .config import home_chat, private_dir, state_dir, write_private
from .i18n import t
from .telegram import Bot, TelegramError
from .topics import TopicMap, session_state, session_state_path, set_state
from .usage import ended_on_limit, quota_line, quota_rows

GEMINI_IN, GEMINI_OUT = 0.30, 2.50  # USD per 1M tokens (gemini-3.5-flash-lite list price)
TTS_IN, TTS_OUT = 0.50, 10.00  # gemini-2.5-flash-tts text in / audio out


def gemini_cost_line(now: Optional[float] = None, log_path: Optional[Path] = None, lang: Optional[str] = None) -> str:
    path = log_path or (state_dir() / "gemini-usage.log")
    if not path.exists():
        return ""
    now_ts = now if now is not None else time.time()
    try:
        day = month = 0.0
        with open(path, encoding="utf-8") as f:
            for row in f:
                f_parts = row.split()
                if len(f_parts) < 3:
                    continue
                ts, i, o = int(f_parts[0]), int(f_parts[1]), int(f_parts[2])
                tts = f_parts[3:4] == ["tts"]
                usd = i * (TTS_IN if tts else GEMINI_IN) / 1e6 + o * (TTS_OUT if tts else GEMINI_OUT) / 1e6
                if now_ts - ts <= 30 * 86400:
                    month += usd
                if now_ts - ts <= 86400:
                    day += usd
        return t("dashboard_gemini_cost", lang=lang, day=day, month=month)
    except (OSError, ValueError):
        return ""


def health(now: Optional[float] = None, lang: Optional[str] = None) -> List[str]:
    """The morning health check: were the last 24 hours of final replies delivered, which engines
    translated, and is the listener polling."""
    from . import mirror
    now = time.time() if now is None else now
    lines = [t("health_title", lang=lang), ""]
    ok = fail = 0
    try:
        rows = (state_dir() / mirror.OUTCOMES).read_text(encoding="utf-8").splitlines()
    except OSError:
        rows = []
    for row in rows:
        at, _, outcome = row.partition(" ")
        try:
            recent = now - float(at) <= 86400
        except ValueError:
            continue
        if recent:
            ok += outcome == "ok"
            fail += outcome == "fail"
    if fail:
        lines.append(t("health_replies_failed", lang=lang, failed=fail, total=ok + fail))
    elif ok:
        lines.append(t("health_replies_ok", lang=lang, n=ok))
    engines: dict = {}
    root = state_dir() / "translate-cache"
    try:
        entries = list(root.iterdir()) if root.is_dir() else []
    except OSError:
        entries = []
    for entry in entries:
        try:
            if now - entry.stat().st_mtime > 86400:
                continue
            name = str(json.loads(entry.read_text(encoding="utf-8")).get("engine") or "")
        except (OSError, ValueError, AttributeError):
            continue
        if name:
            engines[name] = engines.get(name, 0) + 1
    total = sum(engines.values())
    if total:
        shares = " · ".join(f"{name} {n * 100 // total}%" for name, n in sorted(engines.items(), key=lambda x: -x[1]))
        lines.append(t("health_translation", lang=lang, shares=html.escape(shares, quote=False), total=total))
    try:
        status_row = json.loads((state_dir() / "listener.status.json").read_text(encoding="utf-8"))
        alive = status_row.get("state") == "listening" and now - float(status_row.get("at", 0)) < 120
    except (OSError, ValueError, AttributeError):
        alive = False
    lines.append(t("health_listener_ok" if alive else "health_listener_down", lang=lang))
    return lines


def deliberately_waiting(transcript: str) -> bool:
    """A session that armed a ScheduleWakeup after the last real user message, or that still has
    background jobs, is waiting on purpose, not stuck."""
    try:
        with open(transcript, "rb") as fh:
            fh.seek(0, 2)
            start = max(0, fh.tell() - 3_000_000)
            fh.seek(start)
            raw = fh.read()
        if start:
            raw = raw.partition(b"\n")[2]
        wake_at = user_at = -1
        for i, line in enumerate(raw.decode("utf-8", "replace").splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            msg = event.get("message") if isinstance(event, dict) else None
            content = msg.get("content") if isinstance(msg, dict) else None
            blocks = content if isinstance(content, list) else []
            if any(isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "ScheduleWakeup"
                   for b in blocks):
                wake_at = i
            real = isinstance(content, str) or any(isinstance(b, dict) and b.get("type") == "text" for b in blocks)
            if event.get("type") == "user" and not event.get("isMeta") and real:
                user_at = i
        if wake_at > user_at:
            return True
        from .hooks import background_running
        return bool(background_running(transcript))
    except Exception:
        return False


def stuck_check(cfg, topics, bot, now=None, find=status.transcript_of):
    """One warning per busy period after ten minutes without transcript activity, in the session's own group
    (`topics` from TopicMap.all_places(); an entry without a "chat" is a home topic)."""
    now = time.time() if now is None else now
    for session, entry in topics.items():
        thread, path = entry.get("thread"), find(session)
        if type(thread) is not int or not path:
            continue
        try:
            from pathlib import Path
            idle = now - Path(path).stat().st_mtime
        except OSError:
            continue
        if idle < 600 or deliberately_waiting(path):
            continue
        if ended_on_limit(path):  # the turn is over: clear the stale mark instead of calling it stuck
            if session_state(session).get("state") == "busy":
                try:
                    set_state(cfg, session, "done", bot=bot)
                except Exception:
                    pass
            continue
        state_path = session_state_path(session)
        private_dir(state_path.parent)
        with open(str(state_path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            cur = session_state(session)
            if cur.get("state") != "busy" or cur.get("stuck_alert"):
                continue
            ui_lang = getattr(cfg, "ui_language", "en")
            text = t("dashboard_stuck", lang=ui_lang, mins=int(idle // 60))
            chat = entry["chat"] if entry.get("home") is False else home_chat(cfg)
            try:
                sent = bot.call("sendMessage", {"chat_id": chat, "message_thread_id": thread,
                         "text": (L.RLM if ui_lang == "he" else "") + text,
                         "reply_markup": {"inline_keyboard": [[
                             {"text": t("btn_continue", lang=ui_lang), "callback_data": "say:continue"},
                             {"text": t("btn_stop", lang=ui_lang), "callback_data": "say:stop"},
                             {"text": t("btn_status", lang=ui_lang), "callback_data": "say:status"}],
                             menus.not_now_row(ui_lang)]}})
            except TelegramError:
                continue
            mid = sent.get("message_id") if isinstance(sent, dict) else None
            cur["stuck_alert"] = mid if type(mid) is int else True  # the notice's own id: its buttons act only on it
            write_private(state_path, json.dumps(cur))


# -- aligned monospace tables for team quotas ------------

from .i18n import DASHBOARD_EN_TEAM as EN_TEAM

PHONE_COLS = 24  # a phone code block shows about this many characters before it wraps


def clock(ts, now: Optional[float] = None) -> str:
    """Reset time for a monospace table: digits only, no Hebrew ("מחר" flips inside a code block)."""
    try:
        if isinstance(ts, str):
            import calendar
            ts = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        ts = float(ts)
    except Exception:
        return ""
    now_ts = now if now is not None else time.time()
    if ts < now_ts:
        return ""
    t = time.localtime(ts)
    now_t = time.localtime(now_ts)
    return time.strftime("%H:%M" if time.strftime("%d.%m", t) == time.strftime("%d.%m", now_t) else "%d.%m %H:%M", t)


def wrap_reset(row: str, c: str) -> List[str]:
    """A table row plus its reset clock; a dated clock that would wrap moves to its own line."""
    if not c:
        return [row]
    if len(row) + 3 + len(c) <= PHONE_COLS:
        return [f"{row} ↻ {c}"]
    return [row, f"{' ' * 5}↻ {c}"]


def quotas(accounts_flag: bool = True, full: bool = True, session: Optional[str] = None,
           account: Optional[str] = None, now: Optional[float] = None,
           cfg=None, lang: Optional[str] = None) -> List[str]:
    """Team quotas as aligned monospace tables per window (liked the code block);
    Hebrew headers in bold outside the block, English and digits only inside it."""
    from .i18n import get_lang
    ui_lang = lang or (getattr(cfg, "ui_language", None) if cfg else None) or get_lang()
    from .usage import dot, quota_rows
    rows = quota_rows(full=full, session=session, account=account, now=now, lang=ui_lang, cfg=cfg)
    mark = L.RLM if ui_lang == "he" else ""
    lines: List[str] = []
    window_keys = (("5h", "quota_5h_title"), ("week", "quota_week_title"), ("month", "quota_month_title"))
    for window, key in window_keys:
        group = [(team, p, r) for w, team, p, r in rows if w == window]
        if not group:
            continue
        body, named = [], []
        for team, p, r in group:
            name = EN_TEAM.get(team) or (team.replace("קלוד", "Claude") if team.startswith("קלוד") else team)
            named.append((name, p, "" if r == "rolling" else clock(r, now=now)))
        width = max(len(n) for n, _, _ in named) + 1
        for name, p, c in named:
            row = f"{dot(p)} {name:<{width}}{p:>4.0f}%"
            body += wrap_reset(row, c)
        title = t(key, lang=ui_lang)
        lines += ["", mark + "<b>" + title + "</b>", "<pre>" + html.escape("\n".join(body)) + "</pre>"]
    while lines and not lines[0]:
        lines.pop(0)
    if accounts_flag:
        acct_lines = accounts.usage_lines(now=now, lang=ui_lang)
        if acct_lines:
            lines += [mark + line if line and not line.startswith(mark) else line for line in acct_lines]
    return lines



def _minutes(etime: str) -> int:
    """Whole minutes of a `ps` elapsed time ([[dd-]hh:]mm:ss)."""
    days, _, clock = etime.rpartition("-")
    parts = [int(x) for x in clock.split(":")]
    secs = sum(v * f for v, f in zip(reversed(parts), (1, 60, 3600)))
    return (secs + (int(days) * 86400 if days else 0)) // 60


def team_jobs(cfg, ps_output: Optional[str] = None, lang: Optional[str] = None) -> List[str]:
    """Rows for helper processes running right now, one per match from the `delegates` setting
    (empty when none is configured, so nothing is scanned by default)."""
    specs = [d for d in getattr(cfg, "delegates", None) or [] if isinstance(d, dict) and d.get("match")]
    if not specs:
        return []
    if ps_output is None:
        import subprocess
        try:
            ps_output = subprocess.run(["ps", "-axo", "etime=,command="], capture_output=True, text=True,
                                       timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return []
    lang = lang or getattr(cfg, "ui_language", "en")
    rows = []
    for line in ps_output.splitlines():
        etime, _, cmd = line.strip().partition(" ")
        if cmd.lstrip().startswith(("grep ", "pgrep ", "ps ")) or " grep " in cmd:
            continue
        who = next((d.get("label") or d["match"] for d in specs if d["match"] in cmd), None)
        if who is None:
            continue
        try:
            mins = _minutes(etime)
        except ValueError:
            continue
        name = html.escape(str(who), quote=False)
        if lang == "he":
            name = "\u2068" + name + "\u2069"  # a short foreign name stays inside the Hebrew line (bidi isolate)
        rows.append(t("dashboard_job_row", lang=lang, who=name, mins=mins))
    return rows[:8]


def jobs_block(cfg, lang: str, ps_output: Optional[str] = None) -> List[str]:
    """The "background jobs now" section, or [] when no delegate is configured."""
    if not getattr(cfg, "delegates", None):
        return []
    rows = team_jobs(cfg, ps_output=ps_output, lang=lang) or [t("dashboard_jobs_none", lang=lang)]
    return [t("dashboard_jobs_title", lang=lang), ""] + rows


def text(cfg, topics, daily=False, now=None, find=status.transcript_of):
    now = time.time() if now is None else now
    active = (16 if daily else 2) * 3600
    items = [row for row in status.rows(topics, now=now, find=find) if row[0] <= active][:8]
    lang = getattr(cfg, "ui_language", "en")
    quota = quota_line(lang, now=now).strip()
    body = status.render(items, lang)
    mark = L.RLM if lang == "he" else ""
    # One quota line per Claude account, once there is more than one.
    per_account = "\n".join(mark + line if line else line for line in accounts.usage_lines(now=now, lang=lang))
    if getattr(cfg, "quota_files", None) or getattr(cfg, "codex_sessions_dir", None):
        # Other teams are configured: the full per-window table (with the Claude accounts under it).
        table = "\n".join(quotas(accounts_flag=True, full=False, now=now, cfg=cfg, lang=lang))
        if table.strip():
            body = table + "\n\n" + body
    elif quota or per_account:
        head = html.escape(quota, quote=False) + (per_account if quota else per_account.lstrip("\n"))
        body = head + "\n\n" + body
    jobs = jobs_block(cfg, lang)
    if jobs:
        body = "\n".join(jobs) + "\n\n" + body
    if daily:
        cost = gemini_cost_line(now=now, lang=lang)
        if cost:
            body = body + "\n\n" + mark + cost
    title = t("dashboard_morning_title", lang=lang)
    if daily:
        body = "\n".join(health(now=now, lang=lang)) + "\n\n" + body
    return title + "\n\n" + body if daily else body


def run(cfg, daily=False, print_only=False, bot=None, now=None, find=status.transcript_of):
    if not cfg.ready:
        return 1
    bot = bot or Bot.from_config(cfg)
    topics = TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg).all_places()
    body = text(cfg, topics, daily=daily, now=now, find=find)
    if print_only:
        print(body)
        return 0  # preview is read-only: no alerts, pins or state writes
    stuck_check(cfg, topics, bot, now=now, find=find)
    if not daily:
        from . import autopilot, recover, sessionlog
        recover.limit_check(cfg, bot=bot, now=now)  # announce limits, deliver saved messages after the reset
        sessionlog.auto(cfg, bot=bot, find=find)  # a full log file after a task with many actions
        try:
            autopilot.run_autopilot(cfg, bot=bot, now=now)
        except Exception:
            pass
        try:
            identity.check(cfg, bot, now=now)  # at most one `claude auth status` per profile per 10 minutes
        except Exception:
            pass
        try:
            accounts.refresh_usage(now=now)  # idle accounts: quota from the token, at most every 5 min each
        except Exception:
            pass
        try:
            accounts.check_default_account(now=now)
            accounts.rebalance(cfg, bot=bot, now=now)
        except Exception:
            pass
    if daily:
        bot.call("sendMessage", {"chat_id": home_chat(cfg), "text": body, "parse_mode": "HTML"})
        return 0
    path = state_dir() / "dashboard.json"
    private_dir(path.parent)
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("id") and state.get("text") == body and state.get("pinned"):
            return 0
        if state.get("id"):
            try:
                flood.optional_call(bot, "editMessageText", {"chat_id": home_chat(cfg), "message_id": state["id"],
                                    "text": body, "parse_mode": "HTML"})
            except TelegramError as exc:
                if "not modified" not in str(exc).lower():
                    return 1  # ambiguous failure must not create a duplicate pin
        else:
            msg = flood.optional_call(bot, "sendMessage", {"chat_id": home_chat(cfg), "text": body,
                                      "parse_mode": "HTML", "disable_notification": True})
            state["id"] = msg["message_id"]
            state["text"] = body
            write_private(path, json.dumps(state))  # retain ID even if pinning fails
        if not state.get("pinned"):
            bot.call("pinChatMessage", {"chat_id": home_chat(cfg), "message_id": state["id"],
                                       "disable_notification": True})
            state["pinned"] = True
        state["text"] = body
        write_private(path, json.dumps(state))
    return 0
