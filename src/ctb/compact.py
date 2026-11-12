"""/compact from Telegram: one self-updating progress message instead of a screen dump.

Claude Code shows no real percentage while it compacts, so the bar is an estimate from how long the
previous compactions took; it eases toward 95% and shows 100% only when the compaction really ended.
The message is edited every few seconds until the SessionStart hook (wake.hook_start) writes
<session>.done, or the pane stops saying "Compacting". Because the bar is the notice, the PreCompact
and "compaction finished" messages are skipped for a compaction started here.
"""
from __future__ import annotations

import fcntl
import contextlib
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional

from .config import Config, home_chat, private_dir, state_dir, write_private
from .i18n import t
from .telegram import TelegramError

POLL = 3.0  # seconds between edits
MAX_RUN = 900.0  # a compaction that takes longer is not followed any more
FRESH = 120.0  # a marker younger than this means "started from Telegram"
CAP = 95  # the bar never shows more than this before the end
DEFAULT_EXPECT = 90.0
KEEP_DURATIONS = 10
AUTO_COMPACT_STEPS = [0, 200, 250, 300, 400, 600]
COLD_AFTER = 3600  # owner writes after >1h idle
COLD_MIN = 150_000  # context >=150k tokens worth compacting when cache is cold


def auto_compact_limit() -> int:
    """Thousands of context tokens that start an automatic compaction; 0 = off."""
    try:
        return max(0, int((state_dir() / "auto-compact").read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return 0


def set_auto_compact_limit(limit: int) -> None:
    """Change the auto-compaction threshold (thousands of tokens)."""
    write_private(state_dir() / "auto-compact", str(limit))


SESSION_LIMITS = "auto-compact-sessions.json"  # {session id: thousands of tokens; 0 = never for this session}
ASK_KEPT = 3600.0  # an agent's request to compact waits this long for its turn to end
ASK_CLAIM = 900.0  # one run per request within this window (a compaction just typed is refused again anyway)
ASK_NOTE = 300     # characters of "what to keep" passed to /compact


def session_limits() -> dict:
    try:
        data = json.loads((state_dir() / SESSION_LIMITS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if type(v) is int and v >= 0} if isinstance(data, dict) else {}


def session_limit(session: str) -> Optional[int]:
    """This session's own threshold (thousands of tokens, 0 = never), or None when it follows the global one."""
    return session_limits().get(session)


def set_session_limit(session: str, limit: Optional[int]) -> None:
    """Give one session its own threshold; None puts it back on the global one."""
    if limit is not None and limit not in AUTO_COMPACT_STEPS:
        raise ValueError("not a compaction step")
    path = state_dir() / SESSION_LIMITS
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = session_limits()
        if limit is None:
            data.pop(session, None)
        else:
            data[session] = limit
        write_private(path, json.dumps(dict(list(data.items())[-500:])))


def limit_for(session: str) -> int:
    """The threshold that applies to this session: its own, else the global one; 0 = off."""
    own = session_limit(session)
    return auto_compact_limit() if own is None else own


def _ask_path(session: str) -> Path:
    return directory() / ("asked-" + re.sub(r"[^A-Za-z0-9_-]", "_", session)[:80])


def clean_note(note: str) -> str:
    """What the agent wants kept, as one safe line for the /compact command."""
    return " ".join("".join(c if c.isprintable() else " " for c in str(note or "")).split())[:ASK_NOTE]


@contextlib.contextmanager
def _ask_lock(session: str):
    """One writer at a time per session: asking, taking back and finishing never cross each other."""
    path = _ask_path(session)
    private_dir(path.parent)
    with open(str(path) + ".lock", "a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield path


def _read_ask(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def ask(session: str, note: str = "") -> None:
    """The agent asks for a compaction of its own session: it runs once this turn ends and the session is idle."""
    with _ask_lock(session) as path:
        write_private(path, json.dumps({"note": clean_note(note), "at": time.time()}))


def cancel_ask(session: str) -> bool:
    with _ask_lock(session) as path:
        try:
            path.unlink()
            return True
        except OSError:
            return False


def peek_ask(session: str, now: Optional[float] = None) -> Optional[dict]:
    """The session's waiting request, left in place: {"note", "at"}. It stays until a compaction of the
    session really ended after it was made (whoever started that one: the need is met), so a refusal for
    now, or a /compact that was typed but never ran, is tried again at the next turn's end. A request
    older than ASK_KEPT is dropped."""
    now = time.time() if now is None else now
    with _ask_lock(session) as path:
        data = _read_ask(path)
        if data is None:
            return None
        try:
            at = float(data.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        if now - at > ASK_KEPT or ended_since(session, at):
            try:
                path.unlink()
            except OSError:
                pass
            return None
        return {"note": clean_note(data.get("note", "")), "at": at}


def claim_ask(session: str, now: Optional[float] = None) -> Optional[dict]:
    """Take the waiting request for one run: None when there is none (taken back, met, too old) or another
    run claimed it within ASK_CLAIM. Read and claim are one step under the lock, so two runs never type
    /compact for the same request."""
    now = time.time() if now is None else now
    found = peek_ask(session, now)
    if found is None:
        return None
    with _ask_lock(session) as path:
        data = _read_ask(path)
        if data is None or data.get("at") != found["at"]:
            return None  # taken back or replaced meanwhile
        try:
            claimed = float(data.get("claimed") or 0)
        except (TypeError, ValueError):
            claimed = 0.0
        if 0 <= now - claimed < ASK_CLAIM:
            return None
        write_private(path, json.dumps(dict(data, claimed=now)))
    return found


def release_ask(session: str, at: float) -> None:
    """The claimed run did not start a compaction: the request may be tried again."""
    with _ask_lock(session) as path:
        data = _read_ask(path)
        if data is not None and data.get("at") == at and "claimed" in data:
            data.pop("claimed")
            write_private(path, json.dumps(data))


ASK_TRIES = 3     # a detached run tries this often ...
ASK_GAP = 60.0    # ... this far apart, when /compact could not be typed (an open question, a busy channel)


def run_asked(session: str, note: str, run_fn: Callable[[str], bool], wait_fn: Callable[[], None],
              sleep_fn: Callable[[float], None] = time.sleep, tries: Optional[int] = None,
              gap: Optional[float] = None) -> None:
    """The detached run behind an agent's request (`ctb compact`). `run_fn(note)` tries to type /compact and
    `wait_fn()` returns when its helpers are done. The request counts as met only when a compaction really
    ended or still runs: `run_fn` saying yes is not enough, the keys may have been refused afterwards. Otherwise
    the claim is given back and tried again here, since no later turn may come to retry it."""
    tries = max(1, ASK_TRIES if tries is None else tries)
    gap = ASK_GAP if gap is None else gap
    for attempt in range(tries):
        req = claim_ask(session)
        if req is None:
            if note and attempt == 0:  # no request file (an older caller): one plain run with the given note
                run_fn(note)
                wait_fn()
            return
        run_fn(req["note"])
        wait_fn()
        if ended_since(session, req["at"]) or in_progress(session):
            return
        release_ask(session, req["at"])
        if attempt + 1 < tries:
            sleep_fn(gap)


def claimed(session: str, now: Optional[float] = None) -> bool:
    """A run took the request a moment ago: its /compact may already be typed."""
    now = time.time() if now is None else now
    with _ask_lock(session) as path:
        data = _read_ask(path) or {}
    try:
        return 0 <= now - float(data.get("claimed") or 0) < ASK_CLAIM
    except (TypeError, ValueError):
        return False


TYPING_WINDOW = 120.0  # a claimed request keeps nudges away this long: its /compact is about to be typed


def command_pending(session: str, now: Optional[float] = None) -> bool:
    """A /compact of this session is being typed or runs: a claimed request younger than TYPING_WINDOW whose
    compaction has not ended yet, or a compaction in progress. The bridge's wake nudge ("📩 Telegram") is
    never typed meanwhile (live bug: nudge text and /compact joined into one prompt)."""
    if not session:
        return False
    now = time.time() if now is None else now
    if in_progress(session, now):
        return True
    try:
        with _ask_lock(session) as path:
            data = _read_ask(path) or {}
        claimed_at = float(data.get("claimed") or 0)
    except (OSError, TypeError, ValueError):
        return False
    return 0 <= now - claimed_at < min(ASK_CLAIM, TYPING_WINDOW) and not ended_since(session, claimed_at)


def finish_ask(session: str, at: float) -> bool:
    """Remove the request written at `at`, and only that one (a newer request written meanwhile stays)."""
    with _ask_lock(session) as path:
        data = _read_ask(path)
        if data is None or data.get("at") != at:
            return False
        try:
            path.unlink()
            return True
        except OSError:
            return False


def take_ask(session: str, now: Optional[float] = None) -> Optional[dict]:
    """The session's waiting request, removed; None when there is none or it is too old."""
    found = peek_ask(session, now)
    if found is None or not finish_ask(session, found["at"]):
        return None
    return {"note": found["note"]}


def next_auto_compact_limit(cur: Optional[int] = None) -> int:
    """Cycle steps [0, 200, 250, 300, 400, 600]."""
    steps = AUTO_COMPACT_STEPS
    c = auto_compact_limit() if cur is None else cur
    return steps[(steps.index(c) + 1) % len(steps)] if c in steps else 400


def cold_compact(session: str, transcript: str, idle_since: float,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 auto_compact_fn: Optional[Callable] = None) -> bool:
    """The owner writes after >1h idle with context >=150k:
    the whole context is re-read at full price anyway, so compact first.
    Starts the compaction and returns True; the message stays queued and the waiter started
    after the compaction delivers it. Falls back to delivery if no compaction marker within 30 s."""
    try:
        if clock() - idle_since < COLD_AFTER:
            return False
        if not limit_for(session):
            return False  # automatic compaction is off, for everything or for this session
        from .usage import context_tokens
        ctx, _ = context_tokens(transcript)
        if ctx < COLD_MIN:
            return False
        started = clock()
        if auto_compact_fn is not None:
            auto_compact_fn(session, ctx, cold=True)
        else:
            try:
                from .inbox import auto_compact as inbox_auto_compact
                import threading
                threading.Thread(target=lambda: inbox_auto_compact(session, ctx, cold=True), daemon=True).start()
            except Exception:
                pass
        mark = marker(session)
        while clock() - started < 30:
            try:
                if mark.exists() and mark.stat().st_mtime >= started - 1:
                    return True
            except OSError:
                pass
            sleep(1)
        return False
    except Exception:
        return False


def auto_compact_after_reply(session: str, transcript: str,
                             auto_compact_fn: Optional[Callable] = None) -> bool:
    """Hook side auto_compact(session, transcript) after a reply:
    when context reaches the threshold, trigger auto compaction."""
    try:
        from .usage import context_tokens
        asked = peek_ask(session)
        if asked is not None:  # the agent's own call, at a break it chose: no threshold applies
            ctx, _ = context_tokens(transcript)
            if in_progress(session):
                return False  # it compacts right now: the request is met when that one ends
            if auto_compact_fn is not None:
                took = claim_ask(session)
                if took is None:
                    return False
                if auto_compact_fn(session, ctx or 0, cold=False, note=took["note"], asked=True) is False:
                    release_ask(session, took["at"])
                return True
            # the detached run claims the request and reads its note; the request goes once a compaction ended
            return spawn_auto_compact(session, ctx or 0, asked=True)
        limit = limit_for(session) * 1000
        if not limit:
            return False
        ctx, _ = context_tokens(transcript)
        if not ctx or ctx < limit:
            return False
        if auto_compact_fn is not None:
            auto_compact_fn(session, ctx, cold=False)
            return True
        return spawn_auto_compact(session, ctx)
    except Exception:
        return False


def spawn_auto_compact(session: str, ctx: int | float, cold: bool = False, note: str = "", asked: bool = False) -> bool:
    """Run the auto-compaction in its own detached process: a hook exits right away, and a thread
    of it would die with it, freezing the progress message (review of 3b3c59a)."""
    from .launch import argv
    # one argument per option: a note that starts with a dash must never read as an option
    extra = (["--asked"] if asked else []) + (["--note=" + clean_note(note)] if note else [])
    try:
        subprocess.Popen([*argv(), "auto-compact", session, str(int(ctx)), *(["cold"] if cold else []), *extra],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False




def directory() -> Path:
    return state_dir() / "compact"


def marker(session: str) -> Path:
    from .inbox import safe_name
    return directory() / safe_name(session)


def done_marker(session: str) -> Path:
    return marker(session).with_name(marker(session).name + ".done")


def started_from_telegram(session: str, now: Optional[float] = None) -> bool:
    """True while a progress message of this session is running (its marker is fresh)."""
    try:
        age = (time.time() if now is None else now) - marker(session).stat().st_mtime
    except OSError:
        return False
    return 0 <= age < FRESH


PRE_TTL = 300.0  # a PreCompact note older than this is stale: the compaction failed and SessionStart never came
STRICT_WINDOW = 30.0  # the spinner is not on the pane yet: for this long only a parsed dialog counts as open


def precompact_marker(session: str) -> Path:
    return marker(session).with_name(marker(session).name + ".pre")


def hook_marker(session: str) -> Path:
    return marker(session).with_name(marker(session).name + ".hook")


def note_compacting(session: str, hook: bool = False) -> None:
    """A compaction of this session starts now. Without `hook` this is only a provisional mark (the bridge typed
    a /compact: no question card meanwhile); the progress follower clears it when the compaction never shows.
    `hook`: Claude Code's own PreCompact hook said so, which is evidence that it really started."""
    try:
        private_dir(directory())
        write_private(precompact_marker(session), "")
        if hook:
            write_private(hook_marker(session), "")
    except OSError:
        pass


def _age(path: Path, now: float) -> Optional[float]:
    try:
        age = now - path.stat().st_mtime
    except OSError:
        return None
    return age if age >= 0 else None


def _running_age(session: str, now: Optional[float] = None) -> Optional[float]:
    """Seconds since the freshest sign that this session compacts: the PreCompact note, or the bridge's own
    progress marker while its .done is absent. None when there is none."""
    now = time.time() if now is None else now
    ages = [_age(precompact_marker(session), now)]
    if not done_marker(session).exists():
        ages.append(_age(marker(session), now))
    ages = [a for a in ages if a is not None]
    return min(ages) if ages else None


def in_progress(session: str, now: Optional[float] = None) -> bool:
    """True while this session probably compacts (a stale note, from a compaction that failed and never sent
    SessionStart, expires by itself)."""
    if not session:
        return False
    age = _running_age(session, now)
    return age is not None and age < PRE_TTL


def just_started(session: str, now: Optional[float] = None) -> bool:
    """A compaction began a moment ago, so its spinner may not be drawn yet. After STRICT_WINDOW the screen
    itself says whether it still compacts (dialogs.compacting_screen), which also covers an Esc or a failed
    compaction that never sends SessionStart."""
    if not session:
        return False
    age = _running_age(session, now)
    return age is not None and age < STRICT_WINDOW


def ended_marker(session: str) -> Path:
    return marker(session).with_name(marker(session).name + ".ended")


def ended_since(session: str, at: float) -> bool:
    """A compaction of this session ended after `at` (whoever started it)."""
    try:
        return ended_marker(session).stat().st_mtime >= at
    except OSError:
        return False


def mark_done(session: str) -> bool:
    """Called when the compaction ended: tells the progress message. False when none is running."""
    precompact_marker(session).unlink(missing_ok=True)
    try:
        private_dir(directory())
        write_private(ended_marker(session), "")
    except OSError:
        pass
    if not marker(session).exists():
        return False
    write_private(done_marker(session), "")
    return True


def _history() -> Path:
    return directory() / "durations.json"


def past_durations() -> List[float]:
    try:
        data = json.loads(_history().read_text(encoding="utf-8"))
        return [float(x) for x in data if isinstance(x, (int, float))] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def expected_seconds(past: List[float]) -> float:
    recent = past[-5:]
    return max(20.0, sum(recent) / len(recent)) if recent else DEFAULT_EXPECT


def percent(elapsed: float, expect: float) -> int:
    return min(CAP, int(100 * (1 - math.exp(-1.6 * elapsed / expect))))


def render(pct: int, elapsed: float, extra: str = "", final: bool = False, lang: Optional[str] = None) -> str:
    n = int(pct / 10)
    head = t("compact_done_head", lang=lang) if final else t("compact_running_head", lang=lang)
    elapsed_line = t("compact_elapsed_line", lang=lang, elapsed=int(elapsed), extra=extra)
    return f"{head}\n{'▰' * n}{'▱' * (10 - n)} {pct}%\n{elapsed_line}"


def run(cfg: Config, bot, thread: Optional[int], session: str, read_pane: Callable[[], str],
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
        chat: Optional[int] = None) -> bool:
    """Send the progress message and keep editing it. `read_pane` returns the session's screen text
    ("" when unknown). Returns whether the compaction was seen to run. `chat`: the project group of
    the topic (None = the home chat)."""
    chat = home_chat(cfg) if chat is None else chat
    lang = getattr(cfg, "ui_language", "en")
    private_dir(directory())
    write_private(marker(session), str(clock()))
    try:
        done_marker(session).unlink()
    except OSError:
        pass
    expect = expected_seconds(past_durations())
    start = clock()
    params = {"chat_id": chat, "text": render(0, 0, lang=lang), "disable_notification": True}
    if thread:
        params["message_thread_id"] = thread
    try:
        mid = bot.call("sendMessage", params)["message_id"]  # the API client returns the result object itself
    except (TelegramError, KeyError, TypeError):
        marker(session).unlink(missing_ok=True)
        return False
    seen, last = False, ""
    while clock() - start < MAX_RUN:
        sleep(POLL)
        try:
            body = read_pane() or ""
        except Exception:
            body = ""
        busy = "Compacting" in body
        seen = seen or busy
        if done_marker(session).exists() or (seen and not busy) or (not seen and clock() - start > 30):
            break
        elapsed = clock() - start
        # The spinner's own "↓ N tokens" counter is not shown: it is about twice the context being compacted
        # (664.0k for a 327k context, seen live), so "tokens written" read as a figure of the summary.
        msg = render(percent(elapsed, expect), elapsed, lang=lang)
        if msg != last:
            last = msg
            try:
                bot.call("editMessageText", {"chat_id": chat, "message_id": mid, "text": msg})
            except TelegramError:
                pass
    took = clock() - start
    try:  # the PreCompact hook of Claude Code after this run began: it started, whatever the pane showed
        hooked = hook_marker(session).stat().st_mtime >= time.time() - took - 5
    except OSError:
        hooked = False
    ok = done_marker(session).exists() or seen or hooked
    finish_extra = t("compact_can_continue", lang=lang)
    not_started = t("compact_not_started", lang=lang)
    try:
        bot.call("editMessageText", {"chat_id": chat, "message_id": mid,
                                     "text": render(100, took, finish_extra, True, lang=lang) if ok else not_started})
    except TelegramError:
        pass
    if ok:
        try:
            write_private(_history(), json.dumps((past_durations() + [round(took, 1)])[-KEEP_DURATIONS:]))
        except OSError:
            pass
        try:  # seen on the screen to run and to end, also when no SessionStart came to say so: a request is met
            write_private(ended_marker(session), "")
        except OSError:
            pass
    for f in (marker(session), done_marker(session), precompact_marker(session), hook_marker(session)):
        f.unlink(missing_ok=True)  # also the provisional mark of a compaction that never showed: nothing stays blocked
    return ok
