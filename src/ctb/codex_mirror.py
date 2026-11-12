"""Opt-in Codex events consumed by the existing heartbeat and Telegram mirror."""
from __future__ import annotations

import fcntl
from itertools import islice
import json
import logging
import uuid

from .config import home_chat, private_dir, read_map, state_dir, write_private
from .harness import AssistantText, SessionRef, TurnEnded, TurnStarted, registry
from .mirror import Mirror
from .topics import TopicMap, name_for_task, set_state

EVENTS_PER_SESSION = 50
LOG = logging.getLogger(__name__)


def poll(cfg, bot) -> int:
    """Handle at most 50 events per attached session; checkpoint each handled event.

    First attachment tails the current end, including when it has no events.
    Each session lock spans delivery and checkpointing, preventing concurrent
    heartbeat rounds from replaying the same cursor. Telegram delivery and the
    local atomic write cannot form one transaction; a crash between them may
    repeat an outward message.
    """
    if cfg.codex_harness_enabled is not True or not cfg.mirror or not cfg.ready:
        return 0
    topics = TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg)
    adapters = registry(cfg=cfg)
    handled = 0
    for native_id in dict.fromkeys(cfg.codex_attached_sessions):
        try:
            ref = SessionRef("codex", "default", native_id)
            adapter, ref = adapters.resolve(ref)
            # Validate the full native ID before using it in a local filename.
            if str(uuid.UUID(native_id)) != native_id:
                raise ValueError("A complete canonical session UUID is required")
            path = private_dir(state_dir() / "codex-mirror") / (native_id + ".json")
            with open(str(path) + ".lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                saved = read_map(path)
                if not saved.get("cursor"):
                    saved["cursor"] = adapter.initial_cursor(ref)
                    write_private(path, json.dumps(saved))
                title = name_for_task("Codex", native_id[:8])
                if topics.ensure(ref.key, title) is None:
                    continue  # never spill an attached session into General
                events = iter(adapter.events(ref, saved["cursor"]))
                try:
                    for event in islice(events, EVENTS_PER_SESSION):
                        if isinstance(event, AssistantText):
                            if event.phase == "final" or (event.phase == "commentary" and cfg.codex_commentary):
                                mirror = Mirror(cfg, bot, topics)
                                sent = mirror.send_reply(title, ref.key, "Codex\n\n" + event.text)
                                if not sent or mirror.dropped:
                                    break  # retry from the last persisted event next tick
                        elif isinstance(event, TurnStarted):
                            set_state(cfg, ref.key, "busy", bot=bot)
                        elif isinstance(event, TurnEnded):
                            set_state(cfg, ref.key, "done", bot=bot)
                        # Tools and usage intentionally have no chat output.
                        saved["cursor"] = event.cursor
                        write_private(path, json.dumps(saved))
                        handled += 1
                finally:
                    close = getattr(events, "close", None)
                    if close is not None:
                        close()
        except Exception:
            # Never log event bodies, native paths, or exception diagnostics.
            LOG.warning("Codex mirror session poll failed")
    return handled
