"""Keep the working placeholder moving while a session really works.

Telegram does not replay a lone emoji's animation on an edit, so every EVERY seconds each busy
session whose transcript changed within the last ACTIVE seconds gets a fresh placeholder (the
"bottom" transition of topics.set_state: send a new one, delete the old, under the session's
lock). A session that stalls stops being refreshed, so a still placeholder means no real activity.
Run it as `harnessgram heartbeat` under a service manager (launchd KeepAlive, systemd Restart=always).
"""
from __future__ import annotations

import os
import re
import time
from typing import Callable, Optional

from . import agents_card, flood, revive, status
from .i18n import t
from .config import Config, home_chat, state_dir
from .telegram import Bot
from .topics import TopicMap, session_state, set_state

EVERY = 30   # seconds between rounds; all topics share one chat's rate limit (a round costs 2-3 calls)
ACTIVE = 45  # the transcript must have changed this recently
FIRST_CARD_IDLE = 30 * 60  # a session quiet for longer gets no first card (a deploy must not flood every topic)

SPINNER = re.compile(r"^\S\s+[A-Za-z]{1,30}…\s*\(.+\)$")
WORDS: dict[str, str] = {}  # spinner verb -> Hebrew, per process

_DURATION_RE = re.compile(r"^(?:(\d+h)(?:\s+(\d+m))?(?:\s+(\d+s))?|(\d+m)(?:\s+(\d+s))?|(\d+s))$")
_TOKENS_RE = re.compile(r"^[↓↑]\s+\d+(?:\.\d+)?[kM]?\s+tokens$")
_THOUGHT_RE = re.compile(r"^thought for (?:(\d+h)(?:\s+(\d+m))?(?:\s+(\d+s))?|(\d+m)(?:\s+(\d+s))?|(\d+s))$")
_ESC_TOKEN = "esc to interrupt"


def _valid_token(tok: str) -> bool:
    tok = tok.strip()
    if not tok:
        return False
    if tok == _ESC_TOKEN:
        return True
    if _TOKENS_RE.match(tok):
        return True
    if _DURATION_RE.match(tok):
        return True
    if _THOUGHT_RE.match(tok):
        return True
    return False


def _is_valid_status_line(line: str) -> bool:
    m = re.fullmatch(r"(\S)\s+([A-Za-z]{1,30})…\s*\((.+)\)", line.strip())
    if not m:
        return False
    inside = m.group(3).strip()
    tokens = inside.split(" · ")
    return bool(tokens and all(_valid_token(t) for t in tokens))


def status_line(session: str) -> str:
    """Claude Code's working line from the pane, e.g. "✶ Ebbing… (16s · ↓ 1.1k tokens)"."""
    try:
        from .inbox import pane_for_session, _tmux
        pane = pane_for_session(session)
        if not pane:
            return ""
        out = _tmux("capture-pane", "-p", "-t", pane, "-S", "-15")
        if out.returncode:
            return ""
        non_empty = [l for l in out.stdout.splitlines() if l.strip()]
        tail = non_empty[-6:]
        found = [l.strip() for l in tail if _is_valid_status_line(l.strip())]
        return found[-1] if found else ""
    except Exception:
        return ""


def hebrew_status(line: str, cfg: Optional[Config] = None) -> str:
    """"✶ Ebbing… (16s · ↓ 1.1k tokens)" -> "✶ חושב… (16 שנ׳ · ↓ 1.1k טוקנים)"."""
    if not line or not _is_valid_status_line(line):
        return ""
    m = re.fullmatch(r"(\S)\s+([A-Za-z]{1,30})…\s*\((.+)\)", line.strip())
    if not m:
        return ""
    glyph, verb, rest = m.groups()
    if cfg and cfg.ui_language != "he":
        from .creds import redact
        return redact(line.strip())
    if verb not in WORDS:
        from . import translate
        can_gemini = bool(cfg and getattr(cfg, "translate", False)
                          and translate.external_allowed(cfg)
                          and (getattr(cfg, "engines", None) or [""])[0] == "gemini")  # only as the first engine
        if can_gemini:
            try:
                from .creds import for_outside
                verb_clean = for_outside(verb)
                prompt = for_outside(
                    "Translate this one English verb, shown while an AI assistant works, into one Hebrew "
                    "present-tense masculine verb. Output only the word: " + verb_clean
                )
                he = translate.ask(prompt, cfg).strip()
                if he and len(he) < 25 and not re.search(r"[A-Za-z\u0600-\u06ff]", he):
                    WORDS[verb] = he
            except Exception:
                pass
        if verb not in WORDS:
            WORDS[verb] = t("spinner_working", lang="he")
    rest = re.sub(r"(\d+)s\b", lambda m: m[1] + t("spinner_seconds", lang="he"), rest)
    rest = re.sub(r"(\d+)m\b", lambda m: m[1] + t("spinner_minutes", lang="he"), rest)
    rest = re.sub(r"(\d+)h\b", lambda m: m[1] + t("spinner_hours", lang="he"), rest)
    rest = rest.replace("tokens", t("spinner_tokens", lang="he")).replace("thought for", t("spinner_thought", lang="he"))
    rest = rest.replace(_ESC_TOKEN, t("spinner_interrupt", lang="he"))
    from .creds import redact
    return redact(f"{glyph} {WORDS[verb]}… ({rest})")


def pane_spinner(session: str) -> Optional[bool]:
    """True/False when the session's tmux pane shows (or lacks) Claude Code's working line;
    None when there is no live pane to look at."""
    try:
        from .inbox import pane_for_session, _tmux
        pane = pane_for_session(session)
        if not pane:
            return None
        out = _tmux("capture-pane", "-p", "-t", pane, "-S", "-15")
        if out.returncode:
            return None
        non_empty = [l for l in out.stdout.splitlines() if l.strip()]
        tail = non_empty[-6:]
        return any(_is_valid_status_line(l.strip()) for l in tail)
    except Exception:
        return None


def working(session: str, now: Optional[float] = None,
            find: Callable[[str], Optional[str]] = status.transcript_of) -> bool:
    """If a pane has a matching spinner, it is working; otherwise fall back to transcript activity checks."""
    if pane_spinner(session):
        return True
    now = time.time() if now is None else now
    path = find(session)
    return bool(path and now - last_activity(path) <= ACTIVE)


_TURN = [0]
TIDY_EVERY = 300.0  # seconds between the automatic reopen/delete checks of closed topics
_TIDIED = [0.0]


def last_activity(path: str) -> float:
    """Newest write to the session's transcript or any of its background agents' transcripts
    (the placeholder froze while only an agent was working)."""
    stamps = [os.path.getmtime(path)]
    agents = os.path.join(os.path.splitext(path)[0], "subagents")
    try:
        stamps += [os.path.getmtime(os.path.join(agents, f)) for f in os.listdir(agents) if f.endswith(".jsonl")]
    except OSError:
        pass
    return max(stamps)


def refresh_cards(cfg: Config, bot, now: Optional[float] = None,
                  find: Optional[Callable[[str], Optional[str]]] = None) -> int:
    """The status cards of the live sessions: redraw ONE whose info buttons (account, quota) are out of
    date, or post the first card of a live session that has none yet (a new topic) and was active within
    FIRST_CARD_IDLE. At most one attempt per round, whether it worked or not, so a batch of changes
    spreads out; the sends are optional traffic (flood.py) and a skipped one is retried next round.
    A topic whose first card failed, or whose card was drawn lately, is skipped (mirror.CARD_BACKOFF). Returns the number
    of cards drawn."""
    from . import accounts, mirror
    now = time.time() if now is None else now
    find = find or status.transcript_of
    topics = TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg)
    try:
        live = {str(r.get("sessionId") or "") for r in accounts.live_session_records()}
    except Exception:
        return 0
    base = state_dir()
    have = mirror._read_map(base / "status-footer.json")
    seen = mirror._read_map(base / mirror.CARD_DATA_FILE)
    for session in sorted(topics.all_places()):
        if session not in live:
            continue
        found = topics.place_of(session)
        if found is None or not flood.is_open(found.chat, now):
            continue  # a flooded group waits; the other groups' cards go on
        data_key = mirror.card_data_key(session)
        stale = (found.key not in have) or seen.get(found.key) != data_key
        if not stale:
            continue
        # The context size alone moved (mid-turn): a shorter, still throttled wait than other redraws.
        backoff = mirror.CONTEXT_BACKOFF if mirror._only_context_moved(seen.get(found.key), data_key) else mirror.CARD_BACKOFF
        if mirror.card_waiting(found.thread, found.key in have, now, chat=found.away, backoff=backoff):
            continue
        if found.key not in have:
            try:
                path = find(session)
                if not path or now - last_activity(path) > FIRST_CARD_IDLE:
                    continue
            except Exception:
                continue
        return 1 if mirror.refresh_card(cfg, bot, session, topics=topics, create=True, now=now) else 0
    return 0


def escaped_in_claude(cfg: Config, bot, session: str,
                      find: Callable[[str], Optional[str]] = status.transcript_of) -> bool:
    """A busy topic whose turn was stopped with Escape in Claude itself (no Stop hook runs then): the
    transcript ends with Claude Code's interrupt entry and the window shows no working line. The topic is
    settled like after the Stop button (done, queued messages handed over) and says so once."""
    from . import direct
    try:
        uuid = direct.interrupted(find(session))
        if not uuid or pane_spinner(session):
            return False
        if not direct.seen_interrupt(session, uuid):
            return False  # handled before (or stopped from Telegram, which marks it itself)
        from .inbox import _interrupted_idle
        _interrupted_idle(cfg, bot, session)
        from .i18n import t
        direct.post(cfg, session, t("direct_interrupted", lang=getattr(cfg, "ui_language", None)), bot)
        return True
    except Exception:
        return False


def run_once(cfg: Config, bot=None, now: Optional[float] = None,
             find: Callable[[str], Optional[str]] = status.transcript_of) -> int:
    """One round: refresh the placeholder of one busy, recently active session, in turn. Returns 0 or 1."""
    if not cfg.ready:
        return 0
    now = time.time() if now is None else now
    chats = {home_chat(cfg), *(p.chat_id for p in cfg.projects)}
    if not any(flood.is_open(chat, now) for chat in chats):
        return 0  # every chat is flooded: the placeholder can wait, replies cannot
    bot = bot or Bot.from_config(cfg)
    try:
        refresh_cards(cfg, bot, now, find=find)
    except Exception:
        pass  # the cards are a nicety: never hold up the placeholder
    try:
        agents_card.retry_dirty(cfg, bot, now)
    except Exception:
        pass
    if now - _TIDIED[0] >= TIDY_EVERY and revive.revived.is_set() and not revive.reviving.is_set():
        _TIDIED[0] = now
        try:
            from . import sessions_menu
            sessions_menu.auto_tidy(cfg, bot, TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg), now,
                                    skip=revive.recorded_sessions())
        except Exception:
            pass  # housekeeping: never hold up the placeholder
    due = []
    topics = TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg)
    for session in sorted(topics.all_places()):
        try:
            if not flood.is_open(topics.place_of(session).chat, now):
                continue  # that session's group is flooded; the others go on
            cur = session_state(session)
            if cur.get("state") != "busy":
                continue
            if escaped_in_claude(cfg, bot, session, find):
                continue
            if type(cur.get("placeholder")) is not int:
                continue
            if working(session, now=now, find=find):
                due.append(session)
        except Exception:
            continue  # a half-written state file
    if not due:
        return 0
    # One refresh per round in total, taking turns: N busy sessions renew every N x EVERY seconds and
    # the chat stays near 6 calls a minute, far below Telegram's group limit, so replies never queue
    # behind the heartbeat.
    _TURN[0] += 1
    sid = due[_TURN[0] % len(due)]
    line = status_line(sid)
    he_line = hebrew_status(line, cfg) if line else ""
    status_text = ("⏳ " + he_line) if he_line else None
    try:
        set_state(cfg, sid, "bottom", bot=bot, status=status_text, optional=True)
    except Exception:
        return 0  # message already deleted or rate limited
    return 1


def run(cfg: Config, every: float = EVERY, once: bool = False,
        sleep_fn: Callable[[float], None] = time.sleep) -> int:
    if not cfg.ready:
        return 1
    bot = Bot.from_config(cfg)
    from . import policy
    if once:
        if policy.on():  # mirroring off: the round does nothing
            run_once(cfg, bot=bot)
        return 0
    if cfg.session_auto_revive:
        revive.revive_in_background()
    else:
        revive.revived.set()
    from . import buildstamp
    buildstamp.record_running("heartbeat")  # doctor tells when an install left this copy on older code
    try:
        while True:
            if policy.on():  # mirroring off: this round does nothing (it looks again next round)
                run_once(cfg, bot=bot)
            if revive.revived.is_set():  # not before the revive finished: it must read the old record
                try:
                    from .config import load
                    if load().session_auto_revive:
                        revive.snapshot()
                except Exception:
                    pass
            sleep_fn(every)
    finally:
        buildstamp.clear_running("heartbeat")
