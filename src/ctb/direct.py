"""What the owner changes or does directly in Claude Code (not through Telegram), so the topic shows it.

Claude Code 2.1.322 reports it in two places the bridge already listens to:
* every hook's input carries `permission_mode` (default, plan, acceptEdits, auto, bypassPermissions, dontAsk)
  and, in a session started with an agent, `agent_type`;
* the status-line JSON carries `model`, `effort.level`, `output_style.name` and `agent.name`.
`record()` keeps the latest of each per session (a small private state file) and returns what changed, so
the caller can post one short line in the topic (detached: `ctb direct-note`). The status card shows the
mode, the agent and the output style (card_lines), and its data key includes them, so a change made in
Claude reaches the card within the mid-turn refresh pace (mirror.CONTEXT_BACKOFF) or at the turn's end.

An Escape typed in Claude ends a turn without a Stop hook: interrupted() finds Claude Code's own
"[Request interrupted by user" entry at the end of the transcript, for the heartbeat to settle the topic.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from .config import private_dir, state_dir, write_private

STATE_FILE = "session-direct.json"
KEEP = 300
MODES = ("default", "plan", "acceptEdits", "auto", "bypassPermissions", "dontAsk")
FIELDS = ("mode", "agent", "style")
_NAME = re.compile(r"[\w .:/@+-]{1,60}")
_SID = re.compile(r"[A-Za-z0-9_-]{8,80}")
INTERRUPTED = "[Request interrupted by user"
TAIL = 64 * 1024


@contextlib.contextmanager
def _locked():
    base = private_dir(state_dir())
    with open(base / (STATE_FILE + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield base / STATE_FILE


def _load(path) -> Dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _clean(field: str, value) -> Optional[str]:
    """A value fit to store and show; None for anything else (it is then left as it was)."""
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if field == "mode":
        return value if value in MODES else None
    if field == "style":
        value = "" if value.lower() == "default" else value
    return value if value == "" or _NAME.fullmatch(value) else None


def record(session: str, now: Optional[float] = None, **values) -> List[Tuple[str, str, str]]:
    """Save the session's mode/agent/style given as keyword arguments (None = not reported, left as is).
    Returns [(field, old, new)] for each value that changed from a known earlier value; a first sighting
    is saved quietly (nothing changed that the owner did not see)."""
    if not isinstance(session, str) or not _SID.fullmatch(session):
        return []
    clean = {f: _clean(f, values.get(f)) for f in FIELDS}
    clean = {f: v for f, v in clean.items() if v is not None}
    if not clean:
        return []
    changes = []
    with _locked() as path:
        data = _load(path)
        cur = dict(data.get(session) or {})
        dirty = False
        for field, value in clean.items():
            if cur.get(field) == value:
                continue
            if field in cur:
                changes.append((field, cur[field], value))
            cur[field] = value
            dirty = True
        if dirty:
            cur["at"] = time.time() if now is None else now
            data.pop(session, None)
            data[session] = cur
            write_private(path, json.dumps(dict(list(data.items())[-KEEP:]), ensure_ascii=False))
    return changes


def current(session: str) -> dict:
    """{"mode", "agent", "style"} as last reported for the session (a field is missing while unknown)."""
    try:
        cur = _load(state_dir() / STATE_FILE).get(session) or {}
    except Exception:
        cur = {}
    return {f: cur[f] for f in FIELDS if isinstance(cur.get(f), str)}


def from_hook(data: dict) -> List[Tuple[str, str, str]]:
    """Record what a hook's input says about the main session (a subagent's own hooks are skipped)."""
    if not isinstance(data, dict) or data.get("agent_id"):
        return []
    values = {"mode": data.get("permission_mode")}
    if "agent_type" in data:
        values["agent"] = data.get("agent_type") or ""
    return record(str(data.get("session_id") or ""), **values)


def from_status(data: dict) -> List[Tuple[str, str, str]]:
    """Record what the status-line JSON says: output style and agent (and the mode, when it is given)."""
    if not isinstance(data, dict):
        return []
    style = (data.get("output_style") or {}).get("name") if isinstance(data.get("output_style"), dict) else None
    agent = (data.get("agent") or {}).get("name") if isinstance(data.get("agent"), dict) else ""
    return record(str(data.get("session_id") or ""), mode=data.get("permission_mode"), agent=agent or "",
                  style=style)


def mode_label(mode: str, lang: Optional[str]) -> str:
    from .i18n import t
    names = {"plan": "plan", "default": "default", "acceptEdits": "accept_edits", "auto": "auto",
             "bypassPermissions": "bypass"}  # the /mode menu's own labels (inbox.MODE_NAMES)
    return t("mode_" + names[mode], lang=lang) if mode in names else mode


CARD_PREFIXES = ("🔐 ", "🧩 ", "🎨 ")  # the first characters of card_lines' lines (restamp replaces them)


def card_lines(session: str, lang: Optional[str]) -> List[str]:
    """The status card's lines for the mode, the active agent and a non-default output style."""
    from .fmt import isolate
    from .i18n import t
    cur = current(session)
    he = (lang or "").startswith("he")
    out = []
    if cur.get("mode"):
        out.append(t("card_mode", lang=lang, mode=mode_label(cur["mode"], lang)))
    for field, key in (("agent", "card_agent"), ("style", "card_style")):
        value = cur.get(field) or ""
        if value:
            shown = (isolate(value) or value) if he else value
            out.append(t(key, lang=lang, **{field: shown}))
    return out


def note_text(field: str, value: str, lang: Optional[str]) -> str:
    """The short topic line for one change made in Claude."""
    from .i18n import t
    if field == "mode":
        return t("direct_mode_changed", lang=lang, mode=mode_label(value, lang))
    if field == "agent":
        return t("direct_agent_changed", lang=lang, agent=value) if value else t("direct_agent_off", lang=lang)
    return ""


def interrupted(path: Optional[str], since: float = 0.0) -> str:
    """The uuid of an Escape (Claude Code's "[Request interrupted by user" entry) that ends the transcript,
    written after `since`; "" when the last entry is anything else."""
    if not path:
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type") not in ("user", "assistant"):
            continue
        if ev.get("type") != "user" or ev.get("isMeta"):
            return ""
        content = (ev.get("message") or {}).get("content")
        texts = [content] if isinstance(content, str) else [
            b.get("text") or "" for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
        if not any(str(x).startswith(INTERRUPTED) for x in texts):
            return ""
        stamp = _stamp(ev.get("timestamp"))
        return str(ev.get("uuid") or "") if stamp is None or stamp >= since else ""
    return ""


def _stamp(value) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def seen_interrupt(session: str, uuid: str) -> bool:
    """True the first time this Escape is handled for the session (so its line is posted once)."""
    if not uuid:
        return False
    with _locked() as path:
        data = _load(path)
        cur = dict(data.get(session) or {})
        if cur.get("esc") == uuid:
            return False
        cur["esc"] = uuid
        data[session] = cur
        write_private(path, json.dumps(dict(list(data.items())[-KEEP:]), ensure_ascii=False))
    return True


def post(cfg, session: str, text: str, bot=None) -> bool:
    """One silent line in the session's topic. False when it has no topic or the send failed."""
    if not text:
        return False
    try:
        from .topics import place
        found = place(cfg, session)
        if found is None:
            return False
        from .telegram import Bot
        bot = bot or Bot.from_config(cfg)
        bot.call("sendMessage", {"chat_id": found.chat, "message_thread_id": found.thread, "text": text,
                                 "disable_notification": True})
        return True
    except Exception:
        return False


# --- the detached side: `ctb direct-note` ------------------------------------------------------------

NOTE_FIELDS = ("mode", "agent", "model", "switch")


def notify(session: str, changes: List[Tuple[str, str, str]]) -> None:
    """Start one detached `ctb direct-note` per change the topic should hear about (mode and agent)."""
    for field, _old, new in changes:
        if field in ("mode", "agent"):
            spawn_note(session, field, new)


def spawn_note(session: str, field: str, value: str) -> None:
    try:
        from .hooks import spawn
        spawn(["direct-note", f"--session={session}", f"--field={field}", f"--value={value}"])
    except Exception:
        pass


def run_note(cfg, session: str, field: str, value: str, bot=None) -> int:
    """Show in the topic what was done in Claude: a short line (mode, agent, /clear, /resume) and a fresh
    status card. `switch` values are "clear:<previous session>" or "resume:<previous session>": the window
    that ran the previous session now runs this one."""
    if field not in NOTE_FIELDS or not getattr(cfg, "ready", False) or not _SID.fullmatch(session or ""):
        return 0
    from .i18n import t
    from .telegram import Bot
    bot = bot or Bot.from_config(cfg)
    lang = getattr(cfg, "ui_language", None)
    if field in ("mode", "agent"):
        post(cfg, session, note_text(field, value, lang), bot)
    elif field == "switch":
        source, _, old = value.partition(":")
        if source not in ("clear", "resume") or (old and not _SID.fullmatch(old)):
            return 0
        from .config import home_chat
        from .topics import TopicMap
        topics = TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg)
        if topics.place_of(session) is not None:  # the conversation already has its own topic
            if source == "resume":
                post(cfg, session, t("direct_resume_here", lang=lang), bot)
                if old and old != session and topics.place_of(old) is not None:
                    post(cfg, old, t("direct_resume_left", lang=lang), bot)
            else:
                post(cfg, session, t("direct_clear", lang=lang), bot)
        elif old and topics.follow(old, session):
            post(cfg, session, t("direct_clear" if source == "clear" else "direct_resume_follow", lang=lang), bot)
        else:
            return 0
    try:
        from .mirror import refresh_card
        refresh_card(cfg, bot, session, force=True, turn_end=True)
    except Exception:
        pass
    return 0


def restamp(text: str, session: str, lang: Optional[str]) -> str:
    """A saved status text with today's mode, agent and style lines: the old ones dropped, the current ones
    put right after the model line (the line with the 🤖 model, or the English "🧠 …" line), else at the end
    of the first paragraph."""
    lines = [l for l in (text or "").split("\n") if not l.lstrip("\u200f").startswith(CARD_PREFIXES)]
    fresh = card_lines(session, lang)
    if not fresh:
        return "\n".join(lines)
    bare = [l.lstrip("\u200f") for l in lines]
    at = next((i + 1 for i, l in enumerate(bare) if l.startswith("\U0001F916")), None)  # Hebrew: the model line
    if at is None:  # English: "🧠 184k · Opus 5.5 · high"
        at = next((i + 1 for i, l in enumerate(bare) if l.startswith("\U0001F9E0") and "·" in l), None)
    if at is None:
        at = next((i for i, l in enumerate(lines) if not l.strip()), len(lines))
    return "\n".join(lines[:at] + fresh + lines[at:])


def card_key(session: str) -> str:
    """The part of the status card's data key that a change made in Claude moves."""
    cur = current(session)
    try:
        from .usage import session_model
        model, level = session_model(session)
    except Exception:
        model, level = "", ""
    return "~".join((model, level, cur.get("mode", ""), cur.get("agent", ""), cur.get("style", "")))
