"""Once-per-block opening bulletins. Called only by the existing PRIVATE station.

No transmitter, TTS provider, Calendar credentials or real configuration is opened
by this module. The station supplies its existing queue. A durable claim precedes
speech: a crash can lose an announcement, but cannot replay it on restart.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

from . import schedule, schedule_hours
from .config import private_dir, state_dir, write_private

CLAIMS = "schedule-bulletins.json"


def claim(day, ident):
    with schedule._locked():
        path = private_dir(state_dir()) / CLAIMS
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError):
            return False
        key = day + ":" + ident
        if key in data:
            return False
        data = {k: v for k, v in data.items() if k.startswith(day + ":")}
        data[key] = True
        write_private(path, json.dumps(data))
        return True


def words(block, following):
    # Reuse the Cockpit dictionary so Hebrew/English UI and spoken copy stay reviewable together.
    from .panel_page import V2_STRINGS
    s = V2_STRINGS["he"]["schedule"]
    text = s["bulletinStart"].replace("{topic}", block["title"])
    if block.get("summary"):
        text += " " + s["bulletinPlan"].replace("{plan}", block["summary"])
    if following:
        text += " " + s["bulletinNext"].replace("{topic}", following["title"])
    return text


class Conductor:
    def __init__(self, cfg, speak, allowed, clock=time.time, get_view=None, take=claim):
        # speak must queue exclusively on the private mixer, never the shared speech queue.
        self.cfg, self.speak, self.allowed = cfg, speak, allowed
        self.clock, self.get_view, self.take = clock, get_view or schedule.view, take
        self.active = None
        self.next_read = 0
        self.blocks = []
        self.day = ""
        self.deadline = 0

    def step(self):
        now = self.clock()
        local = datetime.fromtimestamp(now, schedule.zone(self.cfg))
        quiet, quiet_start = schedule_hours.quiet_until(self.cfg, local)
        if quiet or not self.allowed():
            self.active = None
            return
        if now >= self.next_read:
            self.next_read = now + 5
            self.blocks = []  # a failed refresh must not leave an old proposal eligible for speech
            data = self.get_view(self.cfg, now=local)
            plan = data["timeline"]["today"]
            self.blocks = plan["blocks"] if plan["available"] else []
            self.day = data["today"]
        point = local.hour * 60 + local.minute + local.second / 60
        current = next((b for b in self.blocks if b["start"] <= point < b["end"]), None)
        if not current or now >= self.deadline or (self.active and current["id"] != self.active):
            self.active = None
        if self.active:
            return
        # No catch-up after a missed hour or a late restart. A delayed queued clip also expires.
        if not current or not 0 <= point - current["start"] < 1:
            return
        if not self.allowed() or not self.take(self.day, current["id"]):
            return
        start_of_day = local.replace(hour=0, minute=0, second=0, microsecond=0)
        self.deadline = min((start_of_day + timedelta(minutes=current["end"])).timestamp(),
                            (start_of_day + timedelta(minutes=current["start"] + 1)).timestamp(), quiet_start)
        self.active = current["id"]
        following = next((b for b in self.blocks if b["start"] >= current["end"]), None)
        from . import radio_privacy, radio_suitability
        text = radio_privacy.scrub(words(current, following), identifiers=radio_privacy.configured_identifiers(self.cfg))
        if not radio_suitability.suitable(text):
            self.active = None
            return
        ident, deadline = self.active, self.deadline
        def gate():
            return gate.playing() and self.allowed()
        gate.playing = lambda: self.active == ident and self.clock() < deadline
        if self.speak(text, gate) is not True:
            self.active = None
