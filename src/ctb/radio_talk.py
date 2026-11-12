"""Talking with the team through the radio, for example while driving.

While the radio is on, every session's reply is said on the air in a few spoken Hebrew sentences (Hebrew only
for now), and the
owner's voice notes are its way back in:

* a voice note in a session's topic goes to that session the usual way (transcribed, the transcript-approval
  setting respected, framed as a voice note and never as approval: framing.VOICE_MARK);
* Inbox.take notes, per session, that the owner's own messages were handed over (note_take): whether one was a
  voice note, and when they were sent and queued, for the round trip;
* the Stop worker, after the reply reached Telegram, calls on_reply: unless the reply is trivial (an
  acknowledgement, "still working"), brief_line asks the configured rewrite command or Gemini for 2-4 spoken
  sentences (what was done, the result, what comes next, what waits for the owner), prefixed with the session's
  name, and spools it (radio/replies/*.json). With the "radio speaks every reply" switch off, only answers to
  voice notes are said;
* the station (radio.Station.talk_loop) says spooled replies ahead of routine updates, after the intro chime,
  merging replies that come close together into one bulletin (a voice note's answer goes at once), within the
  day's paid minutes and then in the free local voice, and logs each round trip: voice note -> queued
  (transcript) -> taken by the session -> reply delivered -> line made -> on the air.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable, List, Optional

from .config import Config, private_dir, set_setting, setting_on, state_dir, write_private

EVERY_REPLY_OFF = "radio-every-reply-off"  # setting file: only voice notes' answers are spoken (default: every reply)
FRESH = 1800.0      # a turn answers the owner's message only if it took it within this long
MAX_SENTENCES = 3
MAX_CHARS = 320     # about 25 seconds of Hebrew speech
SAFE = re.compile(r"[^A-Za-z0-9._-]")


def every_reply_on() -> bool:
    """Every session's reply is said on the radio (more to hear while driving).
    Off (the panel switch): only answers to the owner's voice notes."""
    return not setting_on(EVERY_REPLY_OFF)


def set_every_reply(on: bool) -> None:
    set_setting(EVERY_REPLY_OFF, not on)


def _mark_path(session: str) -> Path:
    return private_dir(state_dir() / "radio-talk") / ((SAFE.sub("_", session) or "_") + ".json")


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def note_take(session: str, items: List[dict], now: Optional[float] = None) -> None:
    """A take handed the session these items: remember the owner's own ones for this turn's reply."""
    owner = [i for i in items if isinstance(i, dict) and i.get("source") == "owner" and not i.get("forwarded")]
    if not session or not owner:
        return
    now = time.time() if now is None else now
    path = _mark_path(session)
    old = _read(path)
    if old and now - float(old.get("taken_at") or 0) > FRESH:
        old = {}

    def first(key: str) -> Optional[float]:
        found = [float(i[key]) for i in owner if isinstance(i.get(key), (int, float))]
        if isinstance(old.get(key), (int, float)):
            found.append(float(old[key]))
        return min(found) if found else None
    mark = {"voice": bool(old.get("voice")) or any(i.get("voice") for i in owner), "taken_at": now,
            "sent_at": first("at"), "queued_at": first("queued_at")}
    write_private(path, json.dumps(mark))


def take_mark(session: str, now: Optional[float] = None) -> dict:
    """This turn's mark, removed (one reply answers it); {} when none or too old."""
    path = _mark_path(session)
    mark = _read(path)
    path.unlink(missing_ok=True)
    now = time.time() if now is None else now
    return mark if mark and now - float(mark.get("taken_at") or 0) <= FRESH else {}


def _sentences(text: str) -> List[str]:
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if p.strip()]


def spoken_reply(title: str, reply: str, translate: Callable[[str], str], names: Optional[dict] = None) -> str:
    """The short Hebrew line for the radio: the session's name, then the reply's summary (or its first
    sentences), in Hebrew, at most MAX_SENTENCES sentences and MAX_CHARS characters."""
    from . import fmt, radio_script as R
    body, summary = fmt.split_summary(reply)
    base = R.spoken(summary or "") or " ".join(_sentences(R.spoken(body or reply))[:MAX_SENTENCES])
    if base and not R.hebrew_enough(base, 0.5):
        try:
            base = R.spoken(translate(base))
        except Exception:
            base = ""
    words = _sentences(base)[:MAX_SENTENCES]
    while len(words) > 1 and len(" ".join(words)) > MAX_CHARS:  # whole sentences only: never cut one mid-way
        words.pop()
    text = " ".join(words)
    if len(text) > MAX_CHARS:  # one sentence longer than the limit: cut where the voice can pause
        text = R.say_short(text, MAX_CHARS)
    name = _name(title, names or {})
    if not text:
        from .i18n import RADIO_TALK_FALLBACK
        return RADIO_TALK_FALLBACK[0].format(name=name) if name else RADIO_TALK_FALLBACK[1]
    return f"{name}: {text}" if name else text


def _name(title: str, names: dict) -> str:
    from . import radio_script as R
    from .i18n import RADIO_SESSION_HE
    project, _, task = (title or "").partition(":")
    project = names.get(project.strip().lower(), project.strip())
    label = R.spoken(f"{project}: {task.strip()}" if task.strip() else project)
    label = R.short(label, 50) if label else ""
    return label if label and R.hebrew_enough(label, 0.5) else RADIO_SESSION_HE


TRIVIAL = re.compile(r"^(?:ok(?:ay)?|done|got it|thanks?|sure|on it|working on it|still working|continuing|"
                     r"will do|noted|understood|קיבלתי|בסדר|תודה|ממשיך|עובד על זה|עדיין עובד)\W*$", re.I)


def trivial(reply: str) -> bool:
    """A reply with nothing worth saying aloud: an acknowledgement, or "still working" with no news."""
    from . import fmt
    body, summary = fmt.split_summary(reply)
    text = " ".join((summary or body or "").split())
    return len(" ".join(reply.split())) < 40 or bool(TRIVIAL.match(text))


BRIEF = ("You write one item of a spoken Hebrew radio bulletin for the owner of a software team, who listens while "
         "driving. From a session's reply below, write 2 to 4 short, natural spoken Hebrew sentences: what was done, "
         "the result, what comes next, and anything that waits for the owner (say clearly if he must decide or act). "
         "Plain words, no code, no file names, no paths, no links, no English words unless a product name has no "
         "Hebrew form; numbers as words. Do not start with the session's name or a greeting. If the reply has no real "
         "news (an acknowledgement, or only says work goes on), answer exactly SKIP. Answer only between <<< and >>>.")


def _key_points(reply: str, limit: int = 2500) -> str:
    """The reply's Summary line first, then its body, trimmed (code blocks dropped)."""
    from . import fmt
    body, summary = fmt.split_summary(reply)
    body = re.sub(r"```.*?```", " ", body or "", flags=re.S)
    text = (f"Summary: {summary}\n\n" if summary else "") + body.strip()
    return text[:limit]


def brief_line(cfg: Config, title: str, reply: str, names: Optional[dict] = None,
               ask: Optional[Callable[[str], str]] = None, rewrite: Optional[List[str]] = None,
               translate: Optional[Callable[[str], str]] = None) -> str:
    """The spoken item for one reply: the session's name, then 2-4 Hebrew sentences from the configured rewrite
    command or Gemini text; '' when there is nothing worth saying. Without either (or when they fail), the
    Summary line or first sentences, translated (spoken_reply)."""
    from . import radio_script as R
    from .creds import for_outside
    # the editor, Gemini and the voice are outside services: the reply (and so the plain fallback) is clean first
    reply = for_outside(reply, getattr(cfg, "redact_outgoing", True))
    title = for_outside(title, getattr(cfg, "redact_outgoing", True))
    if trivial(reply):
        return ""
    prompt = BRIEF + "\n\nREPLY:\n" + _key_points(reply)
    out = ""
    if rewrite:
        import subprocess
        try:
            out = subprocess.run(rewrite, input=prompt, capture_output=True, text=True, timeout=120).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
    if not out:
        if ask is None:
            from .translate import ask as gemini_ask

            def ask(text: str) -> str:
                return gemini_ask(text, cfg)
        try:
            out = ask(prompt) or ""
        except Exception:
            out = ""
    start, end = out.rfind("<<<"), out.rfind(">>>")
    text = " ".join(out[start + 3:end].split()) if 0 <= start < end else " ".join(out.split())
    if text.strip(" .").upper() == "SKIP":
        return ""
    text = R.spoken(text)
    sentences = _sentences(text)[:4]
    text = " ".join(sentences)
    if not text or not R.hebrew_enough(text, 0.7) or len(text) > 2 * MAX_CHARS:
        return spoken_reply(title, reply, translate or (lambda t: t), names)  # the plain fallback
    return f"{_name(title, names or {})}: {text}"


def on_reply(cfg: Config, session: str, title: str, reply: str, now: Optional[float] = None,
             radio_on: Optional[Callable[[], bool]] = None,
             translate: Optional[Callable[[str], str]] = None,
             ask: Optional[Callable[[str], str]] = None, mark: Optional[dict] = None) -> Optional[dict]:
    """After a reply reached Telegram: while the radio is on, spool its spoken item for the station: every
    session's reply (default), or only the answer to the owner's voice note (EVERY_REPLY off). The spooled
    record, or None when nothing is to be said (radio off, a trivial reply, the setting)."""
    now = time.time() if now is None else now
    if mark is None:  # the Stop hook freezes it (hooks.hook_stop): a later take would rewrite it for the next turn
        mark = take_mark(session, now)
    if not getattr(cfg, "radio", None) or not (mark.get("voice") or every_reply_on()):
        return None
    from . import radio
    if not (radio_on or (lambda: bool(radio.job_pid())))():
        return None
    if translate is None:
        from .translate import translate_text

        def translate(text: str) -> str:
            return translate_text(text, cfg, fast=True)
    started = time.time()
    s = radio.settings(cfg)
    line = brief_line(cfg, title, reply, s["project_names"], ask=ask, rewrite=s["rewrite_command"] or None,
                      translate=translate)
    if not line:
        return None
    name = _name(title, s["project_names"])
    say = line[len(name) + 2:] if line.startswith(name + ": ") else line
    record = {"text": line, "name": name, "say": say, "session": session[:8], "voice": bool(mark.get("voice")),
              "sent_at": mark.get("sent_at"), "queued_at": mark.get("queued_at"), "taken_at": mark.get("taken_at"), "replied_at": now,
              "line_secs": round(time.time() - started, 2)}
    spool = private_dir(radio.folder() / "replies")
    write_private(spool / f"{time.time():.6f}-{SAFE.sub('_', session[:8])}.json", json.dumps(record, ensure_ascii=False))
    return record


MERGE_QUIET = 8.0    # seconds without another reply before a bulletin goes on the air
MERGE_MAX = 25.0     # the longest the first reply of a bulletin waits for others
BULLETIN_INTRO = "עדכונים מהצוות."


def bulletin(records: List[dict]) -> str:
    """Several replies that came close together as one flowing segment."""
    lines = [" ".join(str(r.get("text") or "").split()) for r in records]
    lines = [line if line.endswith((".", "!", "?")) else line + "." for line in lines if line]
    return lines[0] if len(lines) == 1 else " ".join([BULLETIN_INTRO] + lines)


def ready(records: List[dict], arrived: List[float], now: float) -> bool:
    """A batch goes on the air at once for a voice note's answer, else after MERGE_QUIET without a new reply
    or MERGE_MAX after the first."""
    if not records:
        return False
    return (any(r.get("voice") for r in records) or now - max(arrived) >= MERGE_QUIET
            or now - min(arrived) >= MERGE_MAX)


def round_trip(record: dict, on_air: float) -> str:
    """One log line: voice note -> queued (transcript, approval) -> taken -> reply delivered -> on the air."""
    def gap(a, b) -> str:
        return f"{b - a:.0f}s" if isinstance(a, (int, float)) and isinstance(b, (int, float)) else "?"
    total = gap(record.get("sent_at"), on_air)
    return (f"talk {record.get('session', '?')}: {total} from {'voice note' if record.get('voice') else 'message'} "
            f"to air (queued {gap(record.get('sent_at'), record.get('queued_at'))}, "
            f"taken {gap(record.get('queued_at'), record.get('taken_at'))}, "
            f"reply {gap(record.get('taken_at'), record.get('replied_at'))}, "
            f"line {record.get('line_secs', '?')}s, to air {gap(record.get('replied_at'), on_air)})")
