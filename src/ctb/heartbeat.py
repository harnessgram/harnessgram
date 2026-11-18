"""Keep the working placeholder moving while a session really works.

Telegram does not replay a lone emoji's animation on an edit, so every EVERY seconds each busy
session whose transcript changed within the last ACTIVE seconds gets a fresh placeholder (the
"bottom" transition of topics.set_state: send a new one, delete the old, under the session's
lock). A session that stalls stops being refreshed, so a still placeholder means no real activity.
Run it as `harnessgram heartbeat` under a service manager (launchd KeepAlive, systemd Restart=always).
"""
from __future__ import annotations

import os
import logging
import re
import sys
import time
from typing import Callable, Optional

from . import agents_card, flood, revive, status
from .i18n import t
from .config import Config, home_chat, state_dir
from .telegram import Bot
from .topics import TopicMap, session_state, set_state

OWNER_VIEWS_EVERY = 60.0  # seconds between the work map and morning briefing rounds
_owner_views = {"at": 0.0}
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
    count = None
    for session in sorted(topics.all_places()):
        if session not in live:
            continue
        found = topics.place_of(session)
        if found is None or not flood.is_open(found.chat, now):
            continue  # a flooded group waits; the other groups' cards go on
        if count is None:
            from . import waiting
            count = waiting.waiting_count(cfg)  # once per round, not once per session
        data_key = mirror.card_refresh_key(cfg, session, count)
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


_FUNNEL_LAST = None
_FUNNEL_STATE = None


def _check_funnel(cfg, now):
    global _FUNNEL_LAST, _FUNNEL_STATE
    port = cfg.panel_funnel_port
    if port is None:
        return
    if _FUNNEL_LAST is not None and now - _FUNNEL_LAST < 300:
        return
    _FUNNEL_LAST = now
    from . import panel_funnel
    public, reason = panel_funnel.status(port)
    if reason == "tailscale unavailable":
        return
    if public is True:
        _FUNNEL_STATE = "on"
        return
    if public is False:
        public, reason = panel_funnel.restore(port, cfg.panel_port)
    state = "restored" if public else "failed"
    if public or state != _FUNNEL_STATE:
        from .mirror import log
        log("cockpit funnel was off; restored" if public else f"cockpit funnel restore failed ({reason})")
    _FUNNEL_STATE = state


def _drain_live_activity(cfg: Config, now: float) -> None:
    """Keep optional push failures visible without interrupting the heartbeat."""
    try:
        from . import apns
        apns.drain(cfg, now)
    except Exception as exc:
        print(f"live activity drain failed: {exc}", file=sys.stderr)


def run_once(cfg: Config, bot=None, now: Optional[float] = None,
             find: Callable[[str], Optional[str]] = status.transcript_of,
             listener_check: Optional[Callable[[], object]] = None) -> int:
    """One round: refresh the placeholder of one busy, recently active session, in turn. Returns 0 or 1."""
    from . import reply_outbox, policy
    now = time.time() if now is None else now
    if listener_check is not None:
        try:
            listener_check()
        except Exception:
            logging.getLogger(__name__).warning("Listener LaunchAgent check could not finish")
    _drain_live_activity(cfg, now)
    try:
        from . import auto_cards
        auto_cards.start(cfg, now)  # local progress does not depend on Telegram or an open Cockpit
    except Exception:
        pass
    try:
        _check_funnel(cfg, now)
    except Exception:
        pass  # an optional repair never interrupts a heartbeat
    from . import radio_upgrade
    radio_upgrade.check(cfg)  # its own clock: the round's `now` predates the funnel check
    if cfg.ready:
        # Paid spend is enforced even while mirroring or the policy is off: a credit process
        # keeps a cached key, so the cap and expiry must always reach it (review #423f).
        bot = bot or Bot.from_config(cfg)
        try:
            from . import credit_mode
            credit_mode.tick(cfg, bot, now)
        except Exception:
            logging.getLogger(__name__).warning("Emergency credit check could not finish")
    if not cfg.ready or not cfg.mirror or not policy.on():
        return 0
    try:
        from . import request_sweep
        request_sweep.start(cfg, now)
    except Exception as exc:
        logging.getLogger(__name__).warning("Request sweep could not start (%s)", type(exc).__name__)
    try:
        from . import board
        board.daily_sweep(cfg, bot, now)
        board.refresh_dirty(cfg, bot)
    except Exception:
        pass  # optional housekeeping must never interrupt replies
    try:
        from . import briefing, work_map
        from .sessions_menu import collect
        from .topics import TopicMap
        from .config import home_chat, state_dir
        if (work_map.enabled(cfg) or briefing.enabled(cfg)) and now - _owner_views["at"] >= OWNER_VIEWS_EVERY:
            _owner_views["at"] = now
            seen = collect(TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg), now=now)
            work_map.tick(cfg, bot, now, entries=seen)  # the pinned work map of each project, edited in place
            briefing.tick(cfg, bot, now, entries=seen)  # the morning briefing, once a day at the wake time
    except Exception:
        pass  # optional: never holds up replies
    try:
        from . import day_assistant
        day_assistant.heartbeat_tick(cfg, bot, now)
    except Exception:
        pass  # optional owner help must never interrupt replies
    try:
        from . import hook_health
        hook_health.check(cfg, bot, now)
    except Exception:
        pass  # settings/Telegram failure must never hold up replies or the heartbeat
    try:
        reply_outbox.drain(cfg, bot, now)
    except Exception as exc:  # a damaged outbox never stops the heartbeat
        from .mirror import log
        log(f"reply outbox drain failed ({type(exc).__name__})")
    try:
        from . import missed_replies
        missed_replies.sweep(cfg, now)  # a final reply whose Stop worker died (e.g. mid-install) goes out now
    except Exception as exc:
        from .mirror import log
        log(f"missed reply sweep failed ({type(exc).__name__})")
    try:
        from . import idle_lines
        idle_lines.tick(cfg, now)  # idle team lines go to their lead session (never to Telegram)
    except Exception as exc:  # optional: never holds up replies or the placeholder
        from .mirror import log
        log(f"idle lines check failed ({type(exc).__name__})")
    try:
        from . import voice_digest
        voice_digest.start(cfg, bot, now)
    except Exception:
        logging.getLogger(__name__).warning("Voice digest could not start")
    chats ={home_chat(cfg), *(p.chat_id for p in cfg.projects)}
    if not any(flood.is_open(chat, now) for chat in chats):
        return 0  # every chat is flooded: the placeholder can wait, replies cannot
    if cfg.codex_harness_enabled is True:
        try:
            from . import codex_mirror
            codex_mirror.poll(cfg, bot)
        except Exception:
            logging.getLogger(__name__).warning("Codex mirror poll failed")
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
            if cfg.codex_harness_enabled is True and session.startswith("codex:"):
                continue  # Codex lifecycle events own this topic's working/idle state
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
        sleep_fn: Callable[[float], None] = time.sleep,
        listener_check: Optional[Callable[[], object]] = None) -> int:
    if not cfg.ready:
        return 1
    bot = Bot.from_config(cfg)
    from . import policy
    if once:
        if listener_check is not None:
            try:
                listener_check()
            except Exception:
                logging.getLogger(__name__).warning("Listener LaunchAgent check could not finish")
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
            # run_once gates on mirroring/policy itself, after the paid-spend watchdog (review #423g).
            if listener_check is not None:
                try:
                    listener_check()
                except Exception:
                    logging.getLogger(__name__).warning("Listener LaunchAgent check could not finish")
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
