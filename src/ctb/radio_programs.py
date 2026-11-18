"""Small, private model for the owner's radio programme clock."""
from __future__ import annotations

import json
import time
from copy import deepcopy

from .config import private_dir, read_map, state_dir, write_private

KINDS = ("music", "news")
MAX_BLOCKS = 24
MAX_MINUTES = 240
MAX_SOURCES = 20
MAX_SOURCE_CHARS = 80
PRESETS = {
    "default": [
        {"kind": "music", "minutes": 45, "sources": []},
        {"kind": "news", "minutes": 15, "sources": []},
    ],
    "drive": [
        {"kind": "music", "minutes": 60, "sources": []},
        {"kind": "news", "minutes": 60, "sources": []},
        {"kind": "music", "minutes": 30, "sources": []},
    ],
}


def path():
    return private_dir(state_dir() / "radio") / "program.json"


def validate_blocks(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_BLOCKS:
        raise ValueError("blocks")
    result = []
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {"kind", "minutes", "sources"}:
            raise ValueError("block")
        kind, minutes, sources = raw["kind"], raw["minutes"], raw["sources"]
        if kind not in KINDS or type(minutes) is not int or not 1 <= minutes <= MAX_MINUTES:
            raise ValueError("block")
        if (not isinstance(sources, list) or len(sources) > MAX_SOURCES or
                any(not isinstance(item, str) or not item.strip() or len(item) > MAX_SOURCE_CHARS
                    for item in sources)):
            raise ValueError("sources")
        result.append({"kind": kind, "minutes": minutes,
                       "sources": list(dict.fromkeys(item.strip() for item in sources))})
    return result


def load():
    raw = read_map(path())
    try:
        blocks = validate_blocks(raw.get("blocks"))
    except (TypeError, ValueError):
        blocks = deepcopy(PRESETS["default"])
    preset = raw.get("preset") if raw.get("preset") in PRESETS else "default"
    started = raw.get("started_at")
    return {"preset": preset, "blocks": blocks, "active": raw.get("active") is True,
            "started_at": float(started) if isinstance(started, (int, float)) else None}


def save(blocks, preset="default", *, active=False, started_at=None):
    if preset not in PRESETS:
        raise ValueError("preset")
    data = {"preset": preset, "blocks": validate_blocks(blocks), "active": active is True,
            "started_at": float(started_at) if active and isinstance(started_at, (int, float)) else None}
    write_private(path(), json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return data


def select_preset(name):
    if name not in PRESETS:
        raise ValueError("preset")
    return save(deepcopy(PRESETS[name]), name)


def start(blocks=None, preset=None, *, now=None):
    current = load()
    return save(current["blocks"] if blocks is None else blocks, preset or current["preset"],
                active=True, started_at=time.time() if now is None else now)


def current_block(now=None, state=None):
    state = load() if state is None else state
    if not state.get("active") or state.get("started_at") is None:
        return None
    blocks = validate_blocks(state.get("blocks"))
    total = sum(item["minutes"] * 60 for item in blocks)
    elapsed = max(0.0, (time.time() if now is None else now) - float(state["started_at"])) % total
    for index, block in enumerate(blocks):
        span = block["minutes"] * 60
        if elapsed < span:
            return dict(block, index=index, remaining_seconds=max(0, int(span - elapsed)))
        elapsed -= span
    return None


def view(now=None):
    state = load()
    return dict(state, presets={name: deepcopy(blocks) for name, blocks in PRESETS.items()},
                current=current_block(now, state))


def channels_for(block, channels):
    """Resolve a news block against the configured selection; genre tags never add channels."""
    chosen = [str(item) for item in channels if isinstance(item, str) and item]
    explicit = block.get("sources") or []
    if block.get("kind") == "news":
        return [item for item in chosen if not explicit or item in explicit]
    return []
