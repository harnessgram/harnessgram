"""The live agents card: the background agents of a session share ONE message in the session's own topic,
edited in place, instead of one notice per agent.

Each agent is a line (type, model, status, elapsed time) with its task under it (agent_task.py finds both when
the agent starts); a finished agent gets a one-line result and an owner-only "Full result" button that sends the
full text as a reply when tapped. When every agent of the batch is done the card collapses to a header plus one
short line per agent (at most COLLAPSED_LINES, then "+N more"), re-sent once, so that single message may notify.
A new agent after a collapse, a card older than CARD_MAX_AGE or one with a newer reply below it starts a fresh
card. Edits are throttled (one per THROTTLE seconds per card, later updates coalesce into the pending one) and
go through the flood gate, so they never compete with the owner's replies.

State lives in <state>/agents-cards/<session>.json; several detached hook children update it at the same time,
hence the file lock. The optional separate agents topic (agents_topic.py) is a different, explicit setting."""
from __future__ import annotations

import contextlib
import fcntl
import json
import re
import time
from pathlib import Path
from typing import Callable, Optional

from . import agent_task, flood, menus
from . import lang as L
from .config import home_chat, private_dir, state_dir, write_private
from .creds import redact
from .telegram import TelegramError
from .topics import chat_of, latest_reply

THROTTLE = 3.0  # seconds between two edits of one card
CARD_MAX_AGE = 1800  # seconds: an older card is not edited any more
STALE_AFTER = 10800  # seconds: an agent that never reported its end stops holding the card open
CARDS_KEPT = 6  # cards remembered (their Full result buttons keep working)
FULL_LIMIT = 6000  # characters of a result kept for the button
SUMMARY_LIMIT = 110  # characters of the one-line result on the card
NAME_LIMIT = 48
BUTTONS = 8  # at most this many Full result buttons per card
COLLAPSED_LINES = 6  # agents listed under the collapsed header; the rest is "+N more"
TEXT_LIMIT = 3900  # a card never grows past this (Telegram refuses 4096)
RETRY_DELAYS = (1.0, 2.0, 4.0)  # seconds between the tries to deliver a card update before the last process exits
CHUNK = 3800  # characters per message of a full result
CALLBACK = re.compile(r"agr:(\d{1,6}):(\d{1,3})")
FAIL_MARKERS = ("API Error", "Error:", "Request failed", "Agent failed")


def _path(session: str) -> Path:
    return state_dir() / "agents-cards" / ((re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_") + ".json")


@contextlib.contextmanager
def _locked(session: str):
    path = _path(session)
    private_dir(path.parent)
    with open(str(path) + ".lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield path


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("cards"), dict):
        data["cards"] = {}
    if not isinstance(data.get("retired"), list):
        data["retired"] = []
    return data


def _save(path: Path, st: dict) -> None:
    write_private(path, json.dumps(st, ensure_ascii=False))


def is_failure(result: str, meta: Optional[dict] = None) -> bool:
    """A finished agent that errored: its meta says so, or its result is only an error message."""
    meta = meta if isinstance(meta, dict) else {}
    if str(meta.get("status") or "").lower() in ("failed", "error") or meta.get("error"):
        return True
    return str(result or "").strip().startswith(FAIL_MARKERS)


# -- rendering -------------------------------------------------------------------------------------------

def _elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def _latin(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text)) and not re.search(r"[֐-׿؀-ۿ]", text)


def _inline(text: str, rtl: bool) -> str:
    """A short English name inside a right-to-left line is wrapped in a bidi isolate."""
    return "⁨" + text + "⁩" if (rtl and _latin(text)) else text


def _label(text: str, rtl: bool, own: bool) -> str:
    return text if own else _inline(text, rtl)


def _long_latin(text: str, rtl: bool) -> bool:
    """Longer English never shares a line with Hebrew."""
    return rtl and _latin(text) and len(text.split()) > 3


def _agent_lines(card_agent: dict, now: float, rtl: bool) -> str:
    icon = {"running": "⏳", "done": "✅", "failed": "❌"}.get(card_agent.get("status"), "⏳")
    name = str(card_agent.get("type") or card_agent.get("name") or "")
    task = str(card_agent.get("task") or "")
    model = str(card_agent.get("model") or "")
    running = card_agent.get("status") == "running"
    end = now if running else card_agent.get("ended")
    elapsed = _elapsed((end or now) - card_agent.get("started", now))
    own_line = _long_latin(name, rtl)
    parts = [] if own_line else [_inline(name, rtl)]
    if model:
        parts.append(_inline(model, rtl))
    parts.append(elapsed)
    mark = L.RLM if rtl else ""
    head = f"{icon} " + " · ".join(p for p in parts if p)
    lines = [mark + name, mark + head] if own_line else [mark + head]
    if task and task != name:  # the task: its own line, English inside a Hebrew line is isolated
        lines.append(mark + "↳ " + _label(task, rtl, _long_latin(task, rtl)))
    summary = str(card_agent.get("summary") or "")
    if summary and not running:
        lines.append(mark + ("💬 " if task and task != name else "↳ ") + summary)
    return "\n".join(lines)


def _compact_line(card_agent: dict, now: float, rtl: bool) -> str:
    """One agent under the collapsed header: icon, task (else type), model, elapsed."""
    icon = {"running": "⏳", "done": "✅", "failed": "❌"}.get(card_agent.get("status"), "❌")
    label = str(card_agent.get("task") or card_agent.get("type") or card_agent.get("name") or "")
    model = str(card_agent.get("model") or "")
    elapsed = _elapsed((card_agent.get("ended") or now) - card_agent.get("started", now))
    mark = L.RLM if rtl else ""
    rest = [_inline(model, rtl)] if model else []
    rest.append(elapsed)
    if _long_latin(label, rtl):
        return mark + f"{icon} {label}" + "\n" + mark + " · ".join(rest)
    return mark + f"{icon} " + " · ".join([_inline(label, rtl)] + rest)


def _numbered(entry: str, n: int, mark: str) -> str:
    """The agent's number at the start of its first line (after the direction mark)."""
    return mark + f"{n}. " + entry[len(mark):]


def _fit(head: str, entries: list, more: str, cap: int) -> tuple:
    """(text, how many entries are listed): the header and at most `cap` entries, short enough for one message;
    the rest is counted in "+N more"."""
    shown = entries[:cap]
    while True:
        hidden = len(entries) - len(shown)
        text = head + "\n" + "\n".join(shown) + (("\n" + more.format(n=hidden)) if hidden else "")
        if len(text) <= TEXT_LIMIT or len(shown) <= 1:
            return text[:TEXT_LIMIT], len(shown)
        shown.pop()


def render(cfg, card: dict, now: float) -> tuple:
    """The card's text and inline keyboard (None when there is no button). With two or more agents every line
    is numbered and a Full result button carries the number of the line it belongs to; only listed agents have
    a button."""
    s = L.strings(cfg.target_language)
    rtl = L.is_rtl(cfg.target_language)
    mark = L.RLM if rtl else ""
    every = card.get("agents", [])
    visible = [i for i, a in enumerate(every) if not a.get("moved")]
    agents = [every[i] for i in visible]
    many = len(agents) > 1
    shown = len(agents)
    if card.get("collapsed"):
        if not agents:
            text = mark + s["agents_card_moved"]
        else:
            bad = sum(1 for a in agents if a.get("status") == "failed")
            ok = len(agents) - bad
            head = s["agents_card_finished_one"] if len(agents) == 1 else s["agents_card_finished_many"].format(n=len(agents))
            head = mark + head + f" · {ok} ✅" + (f" {bad} ❌" if bad else "")
            lines = [_compact_line(a, now, rtl) for a in agents]
            text, shown = _fit(head, [_numbered(x, n, mark) if many else x for n, x in enumerate(lines, 1)],
                               mark + s["agents_card_more"], COLLAPSED_LINES)
    else:
        lines = [_agent_lines(a, now, rtl) for a in agents]
        text, shown = _fit(mark + s["agents_card_title"],
                           [_numbered(x, n, mark) if many else x for n, x in enumerate(lines, 1)],
                           mark + s["agents_card_more"], len(agents))
    buttons = []
    for n, i in enumerate(visible[:shown], 1):
        if every[i].get("full") and len(buttons) < BUTTONS:
            label = s["agents_card_btn_n"].format(n=n) if many else s["agents_card_btn"]
            buttons.append({"text": label, "callback_data": f"agr:{card['seq']}:{i}"})
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    if rows:
        rows.append([{"text": menus.close_button(getattr(cfg, "ui_language", None))["text"],
                      "callback_data": f"agclose:{card['seq']}"}])
    return text, ({"inline_keyboard": rows} if rows else None)


# -- Telegram --------------------------------------------------------------------------------------------

def _gone(e: TelegramError) -> bool:
    """The card message itself is deleted: a new card is posted."""
    text = e.description.lower()
    return any(x in text for x in ("message to edit not found", "message_id_invalid", "can't be edited",
                                   "message to delete not found", "message can't be"))


def _thread_gone(e: TelegramError) -> bool:
    """The topic is deleted or closed: not a deleted card, and posting again would only fail again."""
    text = e.description.lower()
    return any(x in text for x in ("thread not found", "topic_id_invalid", "topic_deleted", "topic_closed"))


def _chat(cfg, card: dict):
    """The chat the card lives in, fixed when the card is created."""
    return card.get("chat") if card.get("chat") is not None else home_chat(cfg)


def _not_modified(e: TelegramError) -> bool:
    return "not modified" in e.description.lower() or "not_modified" in e.description.lower()


def _send(cfg, bot, card: dict, text: str, markup, thread, notify: bool, now: float) -> bool:
    params = {"chat_id": _chat(cfg, card), "text": text}
    if isinstance(thread, int):
        params["message_thread_id"] = thread
    if not notify:
        params["disable_notification"] = True
    if markup:
        params["reply_markup"] = markup
    try:
        msg = flood.optional_call(bot, "sendMessage", params)
        card["message_id"] = int(msg["message_id"])
    except TelegramError as e:
        if _thread_gone(e):
            card["inactive"] = True
        return False
    except (KeyError, TypeError, ValueError):
        return False
    card.update(last_edit=now, shown=text + json.dumps(markup), dirty=False)
    return True


def _edit(cfg, bot, card: dict, text: str, markup, now: float) -> str:
    """'ok', 'gone' (the message is deleted: post a new card) or 'failed' (try again at the next update)."""
    shown = text + json.dumps(markup)
    if card.get("shown") == shown:
        return "ok"
    params = {"chat_id": _chat(cfg, card), "message_id": card["message_id"], "text": text,
              "reply_markup": markup or {"inline_keyboard": []}}
    try:
        flood.optional_call(bot, "editMessageText", params)
    except TelegramError as e:
        if _not_modified(e):
            pass
        elif _thread_gone(e):
            card["inactive"] = True
            return "failed"
        elif _gone(e):
            return "gone"
        else:
            return "failed"
    card.update(last_edit=now, shown=shown, dirty=False)
    return "ok"


def _sync(cfg, bot, card: dict, now: float) -> None:
    """Bring the Telegram message in line with the card. The first post and the collapse may notify;
    everything else is silent. Whatever fails stays marked dirty for the next update."""
    if card.get("inactive") or card.get("dismissed"):
        card["dirty"] = False
        return
    card["last_try"] = now
    text, markup = render(cfg, card, now)
    thread = card.get("thread")
    old = card.get("message_id")
    announce = bool(card.get("collapsed")) and not card.get("announced")
    if old and not announce:
        result = _edit(cfg, bot, card, text, markup, now)
        if result == "ok":
            return
        if result == "failed":
            card["dirty"] = True
            return
        card.pop("message_id", None)  # deleted by hand: a new card below
        card.pop("shown", None)
        old = None
    fresh = dict(card)
    fresh.pop("message_id", None)
    if _send(cfg, bot, fresh, text, markup, thread, announce, now):
        card.update(fresh)
        if announce:
            card["announced"] = True
            if old:  # the quiet card is replaced by the collapsed one
                with contextlib.suppress(TelegramError):
                    flood.optional_call(bot, "deleteMessage", {"chat_id": _chat(cfg, card), "message_id": old})
        return
    if old:  # could not re-send: collapse the existing card in place instead
        if _edit(cfg, bot, card, text, markup, now) == "ok":
            card["announced"] = True
            return
    if not card.get("inactive"):
        card["dirty"] = True


def _deliver(cfg, bot, card: dict, now: float, sleep: Callable) -> float:
    """Sync, and when that fails (flood, network) try again a few times with a growing pause: the last process
    of a batch must not exit with the card stuck on running. Returns the time after the pauses. A card that is
    still dirty afterwards is retried by the next update or the heartbeat (`retry_dirty`)."""
    _sync(cfg, bot, card, now)
    for delay in RETRY_DELAYS:
        if not card.get("dirty") or card.get("inactive"):
            break
        sleep(delay)
        now += delay
        _sync(cfg, bot, card, now)
    return now


# -- state -----------------------------------------------------------------------------------------------

def _prune(st: dict, now: float) -> None:
    for card in st["cards"].values():
        for a in card.get("agents", []):
            if a.get("status") == "running" and not 0 <= now - a.get("started", now) < STALE_AFTER:
                a["moved"] = True  # never reported its end: dropped quietly
    for seq in sorted(st["cards"], key=int)[:-CARDS_KEPT]:
        del st["cards"][seq]


def _usable(st: dict, cfg, chat, thread, now: float) -> Optional[dict]:
    """The card new activity may be added to: open, young, in this topic and still at the bottom."""
    card = st["cards"].get(str(st.get("current")))
    if not card or card.get("collapsed") or card.get("inactive") or card.get("thread") != thread \
            or _chat(cfg, card) != chat or now - card.get("created", 0) >= CARD_MAX_AGE:
        return None
    mid, newest = card.get("message_id"), latest_reply(_chat(cfg, card), thread) if isinstance(thread, int) else None
    if mid and newest and newest > mid:
        return None
    return card


def _new_card(st: dict, chat, thread, now: float) -> dict:
    old = st["cards"].get(str(st.get("current")))
    st["seq"] = int(st.get("seq", 0)) + 1
    card = {"seq": st["seq"], "chat": chat, "thread": thread, "created": now, "agents": []}
    if old and not old.get("collapsed"):  # the running agents follow the new card; the old one is closed off
        for a in old["agents"]:
            if a.get("status") == "running" and not a.get("moved"):
                card["agents"].append(dict(a))
                a["moved"] = True
        old.update(collapsed=True, announced=True, dirty=True)
        if old.get("message_id"):
            st["retired"].append(old["seq"])
    st["cards"][str(card["seq"])] = card
    st["current"] = card["seq"]
    return card


def _card_for(st: dict, cfg, chat, thread, now: float) -> dict:
    return _usable(st, cfg, chat, thread, now) or _new_card(st, chat, thread, now)


def _running(card: dict) -> list:
    return [a for a in card["agents"] if a.get("status") == "running" and not a.get("moved")]


def _collapse_if_done(card: dict) -> None:
    if card["agents"] and not _running(card) and any(not a.get("moved") for a in card["agents"]):
        card["collapsed"] = True


def _clean(text, limit: int) -> str:
    return redact(" ".join(str(text or "").split()))[:limit]


# A value written after a strong secret label ("password: hunter2", "api key = x1"): redact() misses short ones.
# Free text keeps ordinary labelled numbers ("exit code: 1234"), so only these labels hide what follows.
_STRONG_LABEL = re.compile(r"(?i)\b(pass(?:word|phrase)?|passwd|pwd|pw|secret|token|api[ _-]?key|סיסמה|סיסמא)"
                           r"(\s*[:=]\s*|\s+is\s+)([\"'`]?)([^\s\"'`,;]+)")


def _hide_labelled(text: str) -> str:
    def hide(m: re.Match) -> str:
        if "=" not in m.group(2) and ":" not in m.group(2) and not re.search(r"\d", m.group(4)):
            return m.group()  # prose: "the token is expired"
        return m.group(1) + m.group(2) + m.group(3) + "[redacted]"
    return _STRONG_LABEL.sub(hide, redact(text))


def _clean_summary(text) -> str:
    """The one-line result: whole sentences that fit, else a clause or word cut marked with "…", never a
    sentence that just stops in the middle."""
    from .radio_script import short
    return short(_hide_labelled(" ".join(str(text or "").split())), SUMMARY_LIMIT)


def _update(cfg, session: str, thread, bot, mutate: Callable, now: Optional[float], sleep: Callable) -> None:
    """Apply `mutate(st, now)` (returns the card to show) under the lock, then show it, at most once per THROTTLE
    (the first post of a card included, so parallel starts make exactly one card)."""
    given = now
    now = time.time() if given is None else given
    wait = 0.0
    with _locked(session) as path:
        st = _load(path)
        _prune(st, now)
        if st.get("dead_thread") == thread:
            return  # the topic is gone: no card is recreated for it
        card = mutate(st, now)
        try:
            if card is not None:
                if now - card.get("last_try", -THROTTLE) < THROTTLE:
                    card["dirty"] = True
                    if card.get("flush_at", 0) <= now:  # nobody is waiting yet: this process flushes after the pause
                        wait = card["last_try"] + THROTTLE - now
                        card["flush_at"] = now + wait + 5
                else:
                    now = _deliver(cfg, bot, card, now, sleep)
            _flush_retired(cfg, bot, st, now)
            _retry_others(cfg, bot, st, card, now)
            if card is not None and card.get("inactive"):
                st["dead_thread"] = card.get("thread")
        finally:
            _save(path, st)
        seq = card["seq"] if card is not None else None
    if wait <= 0 or seq is None:
        return
    sleep(wait)
    now = time.time() if given is None else given + wait
    with _locked(session) as path:
        st = _load(path)
        card = st["cards"].get(str(seq))
        try:
            if card is not None:
                card["flush_at"] = 0
                if card.get("dirty") or card.get("collapsed"):
                    _deliver(cfg, bot, card, now, sleep)
                if card.get("inactive"):
                    st["dead_thread"] = card.get("thread")
        finally:
            _save(path, st)


def _retry_others(cfg, bot, st: dict, shown, now: float) -> None:
    """Cards left dirty by an earlier failed delivery are tried again with the next update."""
    for card in st["cards"].values():
        if card is not shown and card.get("dirty") and not card.get("inactive") \
                and now - card.get("last_try", -THROTTLE) >= THROTTLE:
            _sync(cfg, bot, card, now)


def retry_dirty(cfg, bot, now: Optional[float] = None) -> None:
    """Heartbeat: deliver card updates that failed earlier, so a collapse is always delivered eventually."""
    now = time.time() if now is None else now
    root = state_dir() / "agents-cards"
    try:
        paths = sorted(root.glob("*.json"))
    except OSError:
        return
    for path in paths:
        try:
            if not any(c.get("dirty") for c in _load(path)["cards"].values()):
                continue
            with _locked(path.stem) as locked:
                st = _load(locked)
                try:
                    _retry_others(cfg, bot, st, None, now)
                finally:
                    _save(locked, st)
        except Exception:
            continue


def _flush_retired(cfg, bot, st: dict, now: float) -> None:
    for seq in st["retired"]:
        card = st["cards"].get(str(seq))
        if card and card.get("message_id"):
            _sync(cfg, bot, card, now)
    st["retired"] = []


def agent_started(cfg, session: str, thread, agent_id: str, agent_type: str, bot, now: Optional[float] = None,
                  sleep: Callable = time.sleep, chat=None, found: Optional[dict] = None) -> None:
    """A background agent started: add its running line to the card (a new card when there is none to extend).
    `chat` is the session's own chat (its project group, or the home chat). `found`: the verified task and model
    of the spawning call (agent_task.lookup), {} when there is none. A repeated start of a running agent is
    ignored; a start for an agent that ended earlier is a new run with its own line and task."""
    chat = home_chat(cfg) if chat is None else chat
    name = agent_task.clean_label(agent_type, NAME_LIMIT) or L.strings(cfg.target_language)["agents_card_agent"]
    spec = found if isinstance(found, dict) else {}
    run = str(spec.get("id") or "")  # the verified call that started this run: what tells two runs apart

    def mutate(st, at):
        card = _card_for(st, cfg, chat, thread, at)
        same = [a for c in st["cards"].values() for a in c["agents"]
                if agent_id and a.get("id") == agent_id and not a.get("moved")]  # a moved line is a stale copy
        if any(a.get("status") == "running" for a in same):
            return card  # a repeated start
        if same:
            last = max(same, key=lambda a: a.get("ended", 0))
            if last.get("unseen") and not last.get("run"):  # its end came first: this is the late start hook
                last.update(name=name, type=name, model=last.get("model") or spec.get("model", ""),
                            task=last.get("task") or spec.get("task", ""), run=run, unseen=False)
                return card
            if not run or run == last.get("run"):
                return None  # the late start hook of the run that ended (or one that cannot be told apart)
        card["agents"].append({"id": agent_id, "name": name, "type": name, "model": spec.get("model", ""),
                               "task": spec.get("task", ""), "run": run, "status": "running", "started": at})
        return card

    _update(cfg, session, thread, bot, mutate, now, sleep)


def agent_finished(cfg, session: str, thread, agent_id: str, report: dict, bot, now: Optional[float] = None,
                   sleep: Callable = time.sleep, chat=None) -> None:
    """A background agent finished: its line gets the model, a one-line result and a Full result button; the
    last one of the batch collapses the card. `report`: name, model, summary, full, failed (all optional)."""
    s = L.strings(cfg.target_language)
    chat = home_chat(cfg) if chat is None else chat

    def mutate(st, at):
        card = _card_for(st, cfg, chat, thread, at)
        same = [a for a in card["agents"] if agent_id and a.get("id") == agent_id and not a.get("moved")]
        mine = next((a for a in same if a.get("status") == "running"), None) or (same[-1] if same else None)  # a resumed agent has two lines
        if mine is None and not agent_id:  # no id at all: the oldest running agent is the best guess
            running = _running(card)
            mine = min(running, key=lambda a: a.get("started", 0)) if running else None
        if mine is None:  # its start was never seen
            mine = {"id": agent_id, "name": "", "model": "", "started": at, "unseen": True}
            card["agents"].append(mine)
        mine.update(status="failed" if report.get("failed") else "done", ended=at,
                    model=agent_task.clean_label(report.get("model"), 40) or mine.get("model", ""),
                    summary=_clean_summary(report.get("summary")),
                    full=_hide_labelled(str(report.get("full") or "").strip())[:FULL_LIMIT])
        label = _clean(report.get("name"), NAME_LIMIT)
        if mine.get("type"):  # the line keeps the agent type; what the report calls it is the task
            if not mine.get("task") and label and label != mine["type"]:
                mine["task"] = agent_task.clean_task(report.get("name"))
        else:
            mine["name"] = label or mine.get("name") or s["agents_card_agent"]
        _collapse_if_done(card)
        return card

    _update(cfg, session, thread, bot, mutate, now, sleep)


def agent_summary(cfg, session: str, thread, agent_id: str, summary: str, bot, now: Optional[float] = None,
                  sleep: Callable = time.sleep) -> bool:
    """Replace one finished agent's one-line result (its translation, made after the card was shown with the
    original): the card is edited in place, nothing else changes. False when that agent is not on a card."""
    found = []

    def mutate(st, at):
        done = [(card, a) for card in st["cards"].values() for a in card["agents"] if a.get("status") != "running"
                and (a.get("id") == agent_id if agent_id else True)]
        if not done:
            return None
        card, mine = max(done, key=lambda p: p[1].get("ended", 0))  # no id: the agent that finished last
        mine["summary"] = _clean_summary(summary)
        found.append(card)
        return card

    _update(cfg, session, thread, bot, mutate, now, sleep)
    return bool(found)


def agent_task_text(cfg, session: str, thread, agent_id: str, task: str, bot, now: Optional[float] = None,
                    sleep: Callable = time.sleep, source: Optional[str] = None) -> bool:
    """Replace one agent's task line with its translation (made after the card was shown with the original):
    the newest line of that agent, running or finished, is edited in place. With `source` (the text that was
    translated) only a line still showing exactly that text changes, so a slow translation never overwrites a
    later run's task or a better task the line already has. False when nothing changed."""
    found = []
    text = agent_task.clean_task(task)
    if not agent_id or not text:
        return False
    original = agent_task.clean_task(source) if source is not None else None

    def mutate(st, at):
        mine = [(card, a) for card in st["cards"].values() for a in card["agents"]
                if a.get("id") == agent_id and not a.get("moved")]
        if not mine:
            return None
        card, line = max(mine, key=lambda p: p[1].get("started", 0))
        if line.get("task") == text:
            return None
        if original is not None and agent_task.clean_task(line.get("task") or "") != original:
            return None
        line["task"] = text
        found.append(card)
        return card

    _update(cfg, session, thread, bot, mutate, now, sleep)
    return bool(found)


def session_ended(session: str, cfg=None, bot=None, now: Optional[float] = None) -> None:
    """The session was closed: agents still shown as running will never report, so they count as failed and the
    card collapses. It is delivered at once when a bot is given; otherwise (or if that fails) it stays marked
    dirty and the heartbeat or the next update delivers it."""
    if not _path(session).exists():
        return
    now = time.time() if now is None else now
    with _locked(session) as path:
        st = _load(path)
        try:
            for card in st["cards"].values():
                running = _running(card)
                if not running:
                    continue
                for a in running:
                    a.update(status="failed", ended=now)
                card.update(collapsed=True, dirty=True)
                if cfg is not None and bot is not None and not card.get("inactive"):
                    _sync(cfg, bot, card, now)
            st["current"] = None
        finally:
            _save(path, st)


def press(cfg, bot, q: dict, session: str) -> str:
    """The owner tapped a Full result button: send that agent's full result as a reply to the card.
    The caller has already checked that the sender is the owner and the topic is this session's."""
    s = L.strings(cfg.target_language)
    msg = q.get("message") or {}
    found = CALLBACK.fullmatch(q.get("data") or "")
    full = ""
    if found:
        with _locked(session) as path:
            card = _load(path)["cards"].get(found.group(1)) or {}
            agents = card.get("agents") or []
            idx = int(found.group(2))
            full = str(agents[idx].get("full") or "") if idx < len(agents) else ""
    try:
        bot.call("answerCallbackQuery", {"callback_query_id": q.get("id") or "", "text": "" if full else s["agents_card_none"]})
    except TelegramError:
        pass
    if not full:
        return "none"
    rtl = L.is_rtl(cfg.target_language)
    parts = [full[i:i + CHUNK] for i in range(0, len(full), CHUNK)]
    for n, part in enumerate(parts):
        chat = (msg.get("chat") or {}).get("id")
        params = {"chat_id": chat if chat is not None else chat_of(cfg, session), "text": (L.RLM if rtl else "") + part,
                  "disable_notification": True}
        if msg.get("message_thread_id"):
            params["message_thread_id"] = msg["message_thread_id"]
        if n == 0 and msg.get("message_id"):
            params["reply_to_message_id"] = msg["message_id"]
            params["allow_sending_without_reply"] = True
        try:
            bot.call("sendMessage", params)
        except TelegramError:
            break
    return "sent"


def dismiss(cfg, bot, q: dict, session: str) -> str:
    """Dismiss this exact card and suppress later updates to it."""
    found = re.fullmatch(r"agclose:(\d{1,6})", q.get("data") or "")
    msg = q.get("message") or {}
    with _locked(session) as path:
        st = _load(path)
        card = st["cards"].get(found.group(1)) if found else None
        if (not card or card.get("message_id") != msg.get("message_id")
                or _chat(cfg, card) != (msg.get("chat") or {}).get("id")
                or card.get("thread") != msg.get("message_thread_id")):
            return "ignored"
        card.update(dismissed=True, dirty=False)
        _save(path, st)
    try:
        bot.call("answerCallbackQuery", {"callback_query_id": q.get("id") or "", "text": ""})
        bot.call("editMessageReplyMarkup", {"chat_id": _chat(cfg, card), "message_id": card["message_id"],
                                             "reply_markup": {"inline_keyboard": []}})
    except TelegramError:
        pass
    return "dismissed"
