"""One count of items awaiting the owner, shared by Cockpit entry points."""
from __future__ import annotations

import time

from . import board, decisions, owner_requests


def waiting_items(cfg, *, now=None, requests=None):
    """Return the independent sources, including old but still open items: decisions (not ended or replaced),
    open owner reminders, requests waiting on the owner, and open ask/choice cards of sessions not ended."""
    from . import cards, panel_view, sessions_menu
    from .config import home_chat, state_dir
    from .topics import TopicMap
    now = time.time() if now is None else now
    records = decisions.open_decisions()
    asks = [c for c in cards.open_cards() if c.get("kind") in ("ask", "choice")]
    states = {}
    if records or asks:
        topics = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg)
        states = {e.session: e.state for e in sessions_menu.collect(topics, now=now)}
    if records:
        reasons = panel_view.stale_reasons(records, states, now)
        records = [r for r in records if reasons.get(r["id"]) not in ("ended", "replaced")]
    asks = [c for c in asks if states.get(c.get("session")) != "ended"]
    if requests is None:
        requests = owner_requests.read(now, owner_requests.ledger_dir(cfg), dormant_after=owner_requests.dormant_seconds(cfg))
    return (records, board.open_reminders(),
            [r for r in requests if r['status'] == 'open' and r['wait'] == 'owner' and not r.get('older')], asks)


def waiting_count(cfg, *, now=None, requests=None):
    return sum(map(len, waiting_items(cfg, now=now, requests=requests)))
