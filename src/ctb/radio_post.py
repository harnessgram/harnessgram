"""The radio's summaries in the alerts channel: every spoken update (the rundown, a short news line, a bulletin of
the sessions' replies) is also posted as ONE silent text message, built from the report the script filled in
(plain lines, never re-parsed from the spoken text). It goes through alerts.alert(kind="radio"), so the channel
checks, the owner's on/off choice, the secret cleaning and the flood and privacy gates all apply. Nothing here
raises: a post is a copy, never the only notice, and never delays the voice."""
from __future__ import annotations

import collections
import hashlib
import html
import threading
import time
from typing import Callable, List, Optional

from . import alerts, radio_script as R

BULLET = "• "
MAX_NEW = 6
MAX_LINES = 8
LINE_CHARS = 280

from .i18n import RADIO_POST_DONE_HE, RADIO_POST_TITLES as _TITLES
_ICONS = {"new": "🔔", "done": "✅", "team": "👥", "quota": "⚠️", "replies": "💬"}


def bulletin_report(records: List[dict]) -> dict:
    """The report of a bulletin of replies: one (name, text) line per session reply."""
    return {"kind": "bulletin", "lines": [(str(r.get("name") or ""), str(r.get("say") or r.get("text") or ""))
                                          for r in records if str(r.get("say") or r.get("text") or "").strip()]}


def _line(text: str, he: bool = False) -> str:
    """One line for the channel: cleaned (the module's own redaction), Latin and number runs isolated so a
    right-to-left line keeps its order, then HTML-escaped."""
    text = " ".join(str(text).split())
    from .creds import for_outside
    text = R.short(for_outside(text), LINE_CHARS)  # the one sanitizer, on the whole text first, then cut
    return html.escape(R.isolate(text) if he else text, quote=False)


def _more(n: int, he: bool) -> str:
    return R._more(n, he)


def _done_lines(rep: dict, he: bool) -> List[str]:
    lines = list(rep.get("done") or [])
    count = int(rep.get("done_count") or 0)
    if not lines and count:
        return [R.count_he(count, *RADIO_POST_DONE_HE[:2]) + (RADIO_POST_DONE_HE[2] if count == 1 else RADIO_POST_DONE_HE[3]) if he else
                f"{count} done."]
    if count > len(lines):
        lines.append((RADIO_POST_DONE_HE[4] + R.number_he(count - len(lines)) + "." if he else f"And {count - len(lines)} more."))
    return lines


def render(rep: dict, lang: str = "he", clock: str = "") -> str:
    """The channel message (Telegram HTML) for one report, or "" when there is nothing to post."""
    he = lang == "he"
    tr = _TITLES["he" if he else "en"]
    kind = rep.get("kind")
    sections: List[tuple] = []  # (icon key, [lines], muted)
    status = ""
    if kind == "bulletin":
        lines = [f"{name}: {text}" if name else text for name, text in (rep.get("lines") or [])][:MAX_LINES]
        if lines:
            sections.append(("replies", lines))
    else:
        if kind == "news":
            status = tr["short"]
        else:
            status = rep.get("status") or ""
        new = list(rep.get("new") or [])[:MAX_NEW]
        if rep.get("more"):
            new.append(_more(int(rep["more"]), he))
        for key, lines in (("new", new), ("done", _done_lines(rep, he)), ("team", list(rep.get("team") or [])),
                           ("quota", list(rep.get("quota") or []))):
            if lines:
                sections.append((key, lines[:MAX_LINES]))
    if not sections and not status:
        return ""
    head = f"<b>📻 {_line(tr['head'], he)} · {_line(clock, he)}</b>" if clock else f"<b>📻 {_line(tr['head'], he)}</b>"
    backlog = f"<i>{_line(rep['backlog'], he)}</i>" if rep.get("backlog") else ""

    def build(secs: List[tuple]) -> str:
        parts = [head] + ([_line(status, he)] if status else [])
        for key, lines in secs:
            parts.append(f"<b>{_ICONS[key]} {_line(tr[key], he)}</b>\n" + "\n".join(BULLET + _line(x, he) for x in lines))
        if backlog:
            parts.append(backlog)
        return "\n\n".join(parts)

    body = build(sections)
    while len(body) > alerts.PART_CHARS and sections:  # one message: drop the last lines until it fits
        key, lines = sections[-1]
        if len(lines) > 1:
            sections[-1] = (key, lines[:-1])
        else:
            sections.pop()
        body = build(sections)
    return body


def fingerprint(body: str) -> str:
    """What makes two posts the same: the body without its header line (the clock changes every time)."""
    return hashlib.sha256(body.split("\n", 1)[-1].encode("utf-8")).hexdigest()[:16]


def post(cfg, rep: dict, lang: str, state: dict, bot=None, clock: Optional[str] = None,
         send: Optional[Callable[..., bool]] = None, log: Callable[[str], None] = lambda line: None) -> bool:
    """Post one update's report to the alerts channel, silently. False when nothing was sent (nothing to say,
    the same body as the last post, no channel, the owner's switch off, or a failure: logged, never raised).
    `state["last"]` keeps the fingerprint of the last body that went out."""
    try:
        body = render(rep, lang, time.strftime("%H:%M") if clock is None else clock)
        if not body:
            return False
        mark = fingerprint(body)
        if state.get("last") == mark:
            return False
        if (send or alerts.alert)(cfg, body, bot=bot, silent=True, kind="radio", html=True):
            state["last"] = mark
            return True
    except Exception as e:  # noqa: BLE001 - a copy to the channel never breaks the radio
        log("channel: could not post (%s)" % e.__class__.__name__)
    return False


OFF_POLL = 2.0  # seconds between two looks at the mirroring switch while it is off and posts wait
QUEUE_MAX = 4  # updates waiting to be posted; when full the oldest is dropped


class Poster:
    """The ONE posting worker: a small bounded queue and one daemon thread that does, in order, the same-body
    check, the send and the state update. Nothing else posts, so two updates can never pass the check together or
    finish out of order. `put` never blocks and never raises."""

    def __init__(self, log: Callable[[str], None] = lambda line: None, send: Optional[Callable[..., bool]] = None):
        self.log, self.send, self.state = log, send, {}
        self.queue: collections.deque = collections.deque()
        self.cond = threading.Condition()
        self.working = False
        self.stopping = False
        self.thread: Optional[threading.Thread] = None

    def put(self, cfg, rep: dict, lang: str, bot=None) -> None:
        try:
            job = (cfg, rep, lang, bot, time.strftime("%H:%M"))
            with self.cond:
                if len(self.queue) >= QUEUE_MAX:
                    self.queue.popleft()
                    self.log("channel: the queue was full, the oldest update was dropped")
                self.queue.append(job)
                if self.thread is None:
                    self.thread = threading.Thread(target=self._run, name="radio-poster", daemon=True)
                    self.thread.start()
                self.cond.notify()
        except Exception as e:  # noqa: BLE001
            self.log("channel: could not queue (%s)" % e.__class__.__name__)

    def _run(self) -> None:
        from . import policy
        while True:
            with self.cond:
                # mirroring off (policy.py): nothing is taken from the queue; it looks again every OFF_POLL
                while not self.stopping and (not self.queue or not policy.on()):
                    self.cond.wait(OFF_POLL if self.queue else None)
                if self.stopping:
                    return
                cfg, rep, lang, bot, clock = self.queue.popleft()
                self.working = True
            try:
                post(cfg, rep, lang, self.state, bot=bot, clock=clock, send=self.send, log=self.log)
            finally:
                with self.cond:
                    self.working = False
                    self.cond.notify_all()

    def stop(self, timeout: float = 5.0) -> None:
        """End the worker thread (what is still queued is dropped): the station's shutdown, and tests."""
        with self.cond:
            self.stopping = True
            self.cond.notify_all()
        if self.thread is not None:
            self.thread.join(timeout)

    def drain(self, timeout: float = 5.0) -> bool:
        """Wait until everything queued is posted (tests, and a clean stop)."""
        end = time.monotonic() + timeout
        with self.cond:
            while self.queue or self.working:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self.cond.wait(left)
        return True
