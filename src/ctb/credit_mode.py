"""One-tap emergency billing through the existing account move/resume path.

Credentials pass through memory only during key renewal. The ledger contains
activation intervals, decisions and normalized dollar totals only. Prices live
in session_usage and are estimates.
Credential renewal is gated by the shared daily spend check and safety margin.
"""
from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import accounts, credit_usage, decisions, status, usage
from .config import locked_map, read_map, state_dir

YES = "המשך בקרדיט"
NO = "לא עכשיו"
BACK = "המכסה התחדשה. השיחה חזרה למנוי."
UNKNOWN = "המשך בקרדיט נעצר עד שאפשר יהיה לבדוק את ההוצאה היומית."
UNAVAILABLE = "המשך בקרדיט מושבת עד שבדיקת התקציב תהיה זמינה."


def _path():
    return state_dir() / "emergency-credit.json"


def _day(now):
    return datetime.fromtimestamp(now).astimezone().date().isoformat()


def cap_line(now=None):
    local = datetime.fromtimestamp(time.time() if now is None else now).astimezone()
    tomorrow = local.date() + timedelta(days=1)
    return f"תקרת הקרדיט היומית נוצלה. מתחדש ב־{tomorrow:%d.%m} בחצות."


def _open(session):
    """Unclosed billing interval, including revoked/expired ones needing recovery."""
    data = read_map(_path())
    records = data.get("intervals", {})
    if not isinstance(records, dict):
        return None
    return next((r for r in reversed(list(records.values())) if isinstance(r, dict)
                 if r.get("session") == session and r.get("end") is None), None)


def active(session, now=None):
    """An unexpired approval for this exact conversation; not a billing interval."""
    rec = _open(session)
    now = time.time() if now is None else now
    reset = rec.get("reset") if rec else None
    return rec if (rec and not rec.get("revoked") and type(reset) in (int, float)
                   and math.isfinite(reset) and now < reset) else None


def session_changed(previous, session, transcript, now=None):
    """Quarantine a conversation switch inside a paid process; never transfer consent.

    Retain both transcript intervals and move recovery to the actual conversation.
    The process marker also blocks delivery if the pane mapping/state is missing.
    """
    if not previous or previous == session:
        return
    now = time.time() if now is None else now
    with locked_map(_path()) as data:
        records = data.setdefault("intervals", {})
        old = next((r for r in reversed(list(records.values())) if isinstance(r, dict)
                    and r.get("session") == previous and r.get("end") is None), None)
        if old is None:
            return
        # A deferred move has not put a key in this process yet.
        if old.get("billing_start", old["started_at"]) is None:
            return
        old["end"] = now
        records["switch:" + session + ":" + str(now)] = {
            **old, "session": session, "transcript": transcript,
            "switched_at": now, "billing_start": now, "end": None,
            "revoked": True, "ready": True}
    accounts.record(session, accounts.CREDIT)


def _provider():
    """Optional local credit tool. Never retain, print or log its raw output.

    Recognize explicit USD daily-spend / remaining-balance fields, not a bare
    dollar amount (which could mean a balance, lifetime usage, or a credit grant).
    An installed but unreadable meter fails closed.
    """
    helper = Path.home() / ".codex/bin/anthropic-credit"
    if not helper.is_file():
        return {}
    try:
        result = subprocess.run([str(helper)], capture_output=True, text=True, timeout=10, check=False)
        if result.returncode:
            return None
        try:
            raw = json.loads(result.stdout)
        except ValueError:
            raw = {}
        out = {}
        for kind, keys in (("spent", ("spent_today_usd", "daily_spend_usd", "today_spend_usd", "spent_today")),
                           ("balance", ("balance_usd", "remaining_usd", "balance"))):
            value = next((raw[k] for k in keys if isinstance(raw, dict) and k in raw), None)
            if value is None:
                label = r"(?:spent today|today spent|daily spend|הוצאה היום)" if kind == "spent" else r"(?:balance|remaining|יתרה)"
                match = re.search(label + r"\s*[:=]?\s*\$?([\d,]+(?:\.\d+)?)", result.stdout, re.I)
                value = match[1].replace(",", "") if match else None
            if value is not None:
                amount = float(value)
                if not math.isfinite(amount) or amount < 0:
                    return None
                out[kind] = amount
        return out or None
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None


def spend(now=None, *, provider=False):
    try:
        return _spend(now, provider=provider)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None  # missing/corrupt accounting never authorizes a new paid turn


def _spend(now=None, *, provider=False):
    now = time.time() if now is None else now
    try:
        if _path().exists() and not isinstance(json.loads(_path().read_text()), dict):
            return None
    except (OSError, ValueError):
        return None
    data = read_map(_path())
    intervals = []
    for rec in data.get("intervals", {}).values():
        start = rec.get("billing_start", rec["started_at"])
        if start is None:
            continue  # approved/deferred, but no billable process has started
        # Old closed days need no transcript access and cannot poison today's gate.
        if rec.get("end") is not None and _day(rec["end"]) < _day(now):
            continue
        root = Path(rec["transcript"])
        paths = [root]
        agents = root.parent / rec["session"] / "subagents"
        try:
            agents.stat()
        except FileNotFoundError:
            pass  # no subagent directory yet
        else:
            # Every scan error, including disappearance after stat, is unknown spend.
            with os.scandir(agents) as entries:
                paths.extend(Path(entry.path) for entry in entries if entry.name.endswith(".jsonl"))
        intervals.append((paths, start, rec.get("end")))
    estimated = credit_usage.estimate(intervals, now)
    if estimated is None:
        return None
    meter = _provider() if provider else {}
    if meter is None:
        with locked_map(_path()) as saved:
            saved["meter_unknown"] = True
        return None
    with locked_map(_path()) as saved:
        if provider:
            saved["meter_unknown"] = False
        elif saved.get("meter_unknown"):
            return None
        day = saved.setdefault("days", {}).setdefault(_day(now), {})
        if any(not _valid_amount(day[field]) for field in ("spent", "balance_last", "balance_spent")
               if field in day):
            return None
        if "balance" in meter:
            previous = day.get("balance_last", meter["balance"])
            day["balance_spent"] = day.get("balance_spent", 0) + max(0, previous - meter["balance"])
            day["balance_last"] = meter["balance"]
            # Count decreases after a top-up too; increases never erase spend.
            estimated = max(estimated, day["balance_spent"])
        cost = max(estimated, meter.get("spent", 0), day.get("spent", 0))
        if not _valid_amount(cost):
            return None
        day["spent"] = cost
        return cost


def _valid_amount(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def blocked(session, now=None):
    process_session = os.environ.get("CTB_CREDIT_SESSION")
    if process_session and process_session != session:
        return True  # /clear and /resume cannot inherit another conversation's consent
    rec = _open(session)
    if not rec:
        if process_session:
            return True  # corrupt/missing ledger in a known paid process
        return bool(session and accounts._read(accounts._sessions_path(), {}).get(session) == accounts.CREDIT)
    if not credit_usage.spend_gate_available():
        return True
    now = time.time() if now is None else now
    if not active(session, now) or _subscription_ready(rec, now):
        return True  # recovery may return to subscription, never relaunch expired credit
    cost = spend(now)
    stop = not credit_usage.may_start(cost)
    if stop:
        with locked_map(_path()) as data:
            for rec in data.get("intervals", {}).values():
                if rec["session"] == session and rec.get("end") is None:
                    rec["stop_day"] = _day(now)
    return stop


def _key_ready(session):
    now = time.time()
    rec = active(session, now) if session else None
    start = rec.get("billing_start", rec["started_at"]) if rec else None
    return (rec is not None and _valid_amount(start) and start <= now
            and not blocked(session, now))


def key_main():
    """Release credentials only after rechecking consent, billing window and spend.

    Capture helper bytes in memory only; never log them or inherit helper stdout.
    Keep this command independent of the real bridge configuration and silent
    on refusal, including unreadable accounting and failed meter checks.
    """
    session = os.environ.get("CTB_CREDIT_SESSION")
    try:
        if not _key_ready(session):
            return 1
        if not credit_usage.may_start(spend(provider=True)):
            return 1
        # The bounded provider query may outlast approval or subscription reset.
        if not _key_ready(session):
            return 1
        result = subprocess.run([str(Path.home() / ".codex/bin/anthropic-key-helper")],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=10, check=False)
        if result.returncode or not result.stdout or not _key_ready(session):
            return 1
        if not credit_usage.may_start(spend(provider=True)) or not _key_ready(session):
            return 1
        sys.stdout.buffer.write(result.stdout)
        sys.stdout.buffer.flush()
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError,
            subprocess.SubprocessError):
        return 1


def status_line(session, now=None):
    rec = _open(session)
    if not rec or rec.get("billing_start", rec["started_at"]) is None:
        return ""
    cost = spend(now)
    return UNKNOWN if cost is None else f"בקרדיט · ${cost:.2f} מתוך $10 היום"


def stop_line(now=None):
    if not credit_usage.spend_gate_available():
        return UNAVAILABLE
    return UNKNOWN if spend(now) is None else cap_line(now)


def offer(cfg, bot, session, text, found):
    """One persisted decision for this exhaustion, shared by topic and Cockpit."""
    if not cfg.emergency_credit or _open(session) or accounts.account_for(session) == accounts.CREDIT:
        return False
    now = time.time()
    error = usage.limit_of(session)
    if not error:
        # Pane-only / StopFailure notices can precede transcript flushing.
        error = {"id": text.strip(), "reset": usage.reset_time(text, datetime.fromtimestamp(now).astimezone()).timestamp()}
    origin = accounts.origin_account(session)
    # Pane text can arrive before the transcript error id. Both observations of
    # the same account/reset must still refer to exactly one exhaustion.
    token = f"{origin}:{int(error['reset'])}"
    with locked_map(_path()) as data:
        old = data.setdefault("offers", {}).get(session, {})
        if old.get("token") == token:
            return False
    cost = spend(now, provider=True)
    if not credit_usage.may_start(cost):
        return False
    # No cross-store lock inversion: reserve, release, then create the decision.
    with locked_map(_path()) as data:
        old = data.setdefault("offers", {}).get(session, {})
        if old.get("token") == token:
            return False
        data["offers"][session] = {"token": token, "reset": error["reset"], "at": now,
                                   "origin": origin, "limit_id": error["id"]}
    try:
        rec = decisions.create(session, found.chat, found.thread,
                               f"המכסה נגמרה. להמשיך את השיחה בקרדיט? נשאר ${10 - cost:.2f} היום מתוך $10",
                               detail="הסכום המוצג הוא אומדן. השימוש נעצר באומדן של $8.50 כדי להשאיר מרווח ביטחון; בקשות פעילות עלולות להוסיף חיוב.",
                               yes=YES, no=NO, lang="he", routine=False, action="emergency_credit")
    except Exception:
        with locked_map(_path()) as data:
            data.get("offers", {}).pop(session, None)
        raise
    with locked_map(_path()) as data:
        data["offers"][session]["decision"] = rec["id"]
    from . import cockpit_notice
    cockpit_notice.update(cfg, rec, True, bot)
    return True


def _offer_current(session, offer, now):
    """Recheck the actual account, API exhaustion and all subscription alternatives."""
    error = usage.limit_of(session)
    return (accounts.account_for(session) == offer.get("origin")
            and isinstance(error, dict) and error.get("id") == offer.get("limit_id")
            and error.get("reset") == offer.get("reset") and now < offer.get("reset", 0)
            and accounts.spent(offer["origin"], now)
            and not accounts.fallback_plan(session, now=now).get("fresh"))


def check_answer(cfg, rec, now):
    offer = read_map(_path()).get("offers", {}).get(rec["session"], {})
    if (not cfg.emergency_credit or offer.get("decision") != rec["id"]
            or now >= offer.get("reset", 0) or _open(rec["session"])
            or not _offer_current(rec["session"], offer, now)
            or not credit_usage.may_start(spend(now))):
        raise decisions.DecisionError("credit offer is no longer available", "closed", "expired")


def _options(session):
    """Capture the live permission mode and exact model; unknown means no restart."""
    from . import direct, inbox, session_usage
    pane = inbox.pane_for_session(session)
    mode = (inbox.pane_mode(pane) if pane else "") or direct.current(session).get("mode", "")
    model, _ = usage.session_model(session)
    if not model.startswith("claude-"):
        # session_model returns display names (including older models).
        model = "claude-" + model.lower().replace(" ", "-").replace(".", "-")
    base = model.removesuffix("[1m]")
    if not pane or mode not in direct.MODES or session_usage.price(base, {}) is None:
        return None
    return model, mode


def resume_flags(session):
    import shlex
    rec = _open(session)  # expired/revoked credit still needs its options for the move back
    return (" --model " + shlex.quote(rec["model"]) + " --permission-mode " + shlex.quote(rec["mode"])) if rec else ""


def starting(session, now=None):
    """Persist the billing boundary after old-process exit, before the new command."""
    with locked_map(_path()) as data:
        for rec in data.get("intervals", {}).values():
            if rec["session"] == session and rec.get("end") is None and rec.get("billing_start") is None:
                rec["billing_start"] = time.time() if now is None else now


def leaving(session, now=None):
    """Called after the credit process exits, BEFORE subscription resume can write tokens."""
    with locked_map(_path()) as data:
        for rec in data.get("intervals", {}).values():
            if rec["session"] == session and rec.get("end") is None:
                rec["end"] = time.time() if now is None else now


def _subscription_ready(rec, now):
    origin = rec["origin"]
    if rec["reset"] <= now:
        return True  # the reset parsed from the actual limit notice outranks stale usage
    reading = accounts._read(accounts.usage_path(origin), {})
    return accounts.iso_epoch(reading.get("at")) > rec["started_at"] and not accounts.spent(origin, now)


def _has_live_process(session):
    """Tri-state process check: unreadable records never prove a process has exited."""
    try:
        files = accounts._session_record_files_strict()
        if files is None:
            return None
        for path in files:
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            if not isinstance(rec, dict):
                return None
            if not isinstance(rec.get("sessionId"), str) or not rec["sessionId"]:
                return None
            if rec["sessionId"] != session:
                continue
            pid = rec.get("pid")
            if type(pid) is not int or pid <= 0:
                return None
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass  # a process we cannot signal still counts as live
            return True
        return False
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return None


def _switch_back(cfg, bot, rec, now, inbox, say, *, interrupt=False):
    session = rec["session"]
    live = _has_live_process(session)
    if live is False:
        # No restart, prompt or paid launch is needed after a crash.
        if accounts.account_for(session) == accounts.CREDIT:
            accounts.record(session, rec["origin"])
        accounts.clear_deferred(session)
        leaving(session, now)
        return True
    if live is None:
        return False
    if interrupt or accounts.restart_is_safe(session, now=now, min_idle=0):
        if inbox.account_move(cfg, bot, session, rec["origin"], say, idle_only=not interrupt):
            leaving(session, now)
            return True
    return False


def _stop_credit_processes(sessions, inbox):
    """End only the captured paid processes, even if a client retains its cached key.

    As in the bridge's restart path, send TERM then KILL after a short grace.
    All sessions get TERM before waiting. Recheck PID/start identity, executable,
    credit binding and strict session records before every signal; never signal
    a process group, another conversation, or a replacement process.
    """
    def same(session, pid, started):
        try:
            if accounts.account_for(session) != accounts.CREDIT:
                return False
            records = inbox._live_records_strict(pid)
            if records is None or any(int(r.get("pid") or 0) == pid and r.get("sessionId") != session
                                      for r in records):
                return False
            return (inbox._looks_like_claude(pid, inbox._ps_field("command=", pid))
                    and inbox._proc_start(pid) == started)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            return False

    targets = []
    for session in dict.fromkeys(sessions):
        pid = inbox.session_pid(session)
        if type(pid) is not int or pid <= 1:
            continue
        started = inbox._proc_start(pid)
        if started and same(session, pid, started):
            try:
                os.kill(pid, signal.SIGTERM)
                targets.append((session, pid, started))
            except OSError:
                pass
    for _ in range(6):  # one shared three-second grace, not one delay per session
        if not targets:
            return
        time.sleep(0.5)
        targets = [target for target in targets if same(*target)]
    for session, pid, started in targets:
        if same(session, pid, started):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def tick(cfg, bot, now=None):
    """Heartbeat owner. Slow moves use the existing serialized move and replay machinery."""
    from . import inbox, mirror
    now = time.time() if now is None else now
    # Cross-process nonblocking gate; never launch two moves for the same answer.
    gate = state_dir() / "emergency-credit-tick"
    from .config import private_dir
    import fcntl
    private_dir(gate.parent)
    with gate.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        _tick(cfg, bot, now, inbox, mirror)


def _tick(cfg, bot, now, inbox, mirror):
    records = decisions._load(state_dir() / decisions.STATE_FILE)
    from . import cockpit_notice
    for rec in records.values():
        if rec.get("action") == "emergency_credit" and rec["state"] == "open" and not rec.get("notice_message"):
            cockpit_notice.update(cfg, rec, True, bot)  # retry a failed first topic delivery
    ledger = read_map(_path())
    pending = any(r.get("action") == "emergency_credit" and r["state"] == "answered" and r["choice"] == "yes"
                  and r["id"] not in ledger.get("intervals", {}) and r["id"] not in ledger.get("consumed", {})
                  for r in records.values())
    if not pending and not any(r.get("end") is None for r in ledger.get("intervals", {}).values()):
        return
    cost = spend(now, provider=True)
    stop = not credit_usage.may_start(cost)
    if stop:
        # Revoke every approval and interrupt every paid turn before any slow move.
        # Cached keys must not keep sibling sessions running while one move waits.
        with locked_map(_path()) as data:
            for row in data.get("intervals", {}).values():
                if row.get("end") is None:
                    row.update(revoked=True, stop_day=_day(now))
    stopped = [row["session"] for row in read_map(_path()).get("intervals", {}).values()
               if row.get("end") is None and (row.get("revoked") or row.get("stop_day"))
               and row.get("billing_start", row["started_at"]) is not None]
    for session in dict.fromkeys(stopped):
        try:
            pane = inbox.pane_for_session(session)
            if pane and inbox.pane_runs_session(pane, session):
                inbox._tmux("send-keys", "-t", pane, "Escape")
        except (OSError, subprocess.SubprocessError):
            pass  # lack of a usable pane must not leave a cached key working
    _stop_credit_processes(stopped, inbox)
    for rec in records.values():
        if rec.get("action") != "emergency_credit" or rec["state"] != "answered" or rec["choice"] != "yes":
            continue
        data = read_map(_path())
        if rec["id"] in data.get("intervals", {}) or rec["id"] in data.get("consumed", {}):
            continue
        offer = data.get("offers", {}).get(rec["session"], {})
        if (not cfg.emergency_credit or offer.get("decision") != rec["id"] or now >= offer.get("reset", 0)
                or not _offer_current(rec["session"], offer, now)
                or not credit_usage.may_start(cost)):
            with locked_map(_path()) as data:
                data.setdefault("consumed", {})[rec["id"]] = True
            continue
        options, transcript = _options(rec["session"]), status.transcript_of(rec["session"])
        if not options or not transcript:
            continue  # retain the approval, but never change unknown model/permissions
        with locked_map(_path()) as data:
            data.setdefault("intervals", {})[rec["id"]] = {
                "session": rec["session"], "started_at": now, "day": _day(now), "end": None,
                "origin": offer["origin"], "reset": offer["reset"], "limit_id": offer["limit_id"],
                "transcript": str(transcript), "model": options[0], "mode": options[1],
                "billing_start": None, "ready": False}
    for ident, rec in read_map(_path()).get("intervals", {}).items():
        if rec.get("end") is not None:
            continue
        session = rec["session"]
        say = lambda text, **kw: mirror.send_essential(cfg, bot, session, {"text": text, **kw})
        parked = bool(rec.get("stop_day"))
        if parked:
            notify = rec.get("cap_notice") != _day(now)
            line = (UNAVAILABLE if not credit_usage.spend_gate_available()
                    else cap_line(now) if cost is not None else UNKNOWN)
            if notify and say(line):
                with locked_map(_path()) as data:
                    data["intervals"][ident]["cap_notice"] = _day(now)
        revoked = bool(rec.get("revoked"))
        if parked or revoked or _subscription_ready(rec, now):
            if not rec.get("ready") and accounts.account_for(session) != accounts.CREDIT:
                leaving(session, now)
                continue  # quota recovered before a credit process was started
            if _switch_back(cfg, bot, rec, now, inbox, say, interrupt=parked or revoked):
                if not parked:
                    say(BACK)
            continue
        if not rec.get("ready"):
            if accounts.account_for(session) != accounts.CREDIT:
                offer = read_map(_path()).get("offers", {}).get(session, {})
                if not _offer_current(session, offer, now):
                    leaving(session, now)
                    continue  # deferred approval cannot survive a later account/quota change
            moved = accounts.account_for(session) == accounts.CREDIT and inbox.resume_pending(session)
            if not moved:
                moved = inbox.account_move(cfg, bot, session, accounts.CREDIT, say,
                                           defer=True, limit={"id": rec["limit_id"], "reset": rec["reset"]})
            if moved is None:
                continue  # resumed on the previous account; activation closed, original limit still applies
            if moved:
                with locked_map(_path()) as data:
                    data["intervals"][ident]["ready"] = True
                inbox._limit_cleared_by_move(session, {"id": rec["limit_id"]})
                replayed = inbox._queue_replay(cfg, bot, session)
                inbox.mark_resume_pending(session, replayed, accounts.CREDIT)
                inbox.resume_after_move(session, wait=0)
                say(status_line(session, now))
        elif inbox.resume_pending(session):
            inbox.resume_after_move(session, wait=0)
