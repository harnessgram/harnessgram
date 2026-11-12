"""Builds the Studio's one HTML page (the panel serves one file per app): the brand kit's CSS and icon sprite, the
strings table and the script, all inline. Nothing here is dynamic: the page asks the panel for everything else."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import strings

HERE = Path(__file__).resolve().parent

# The empty-state picture: a clapperboard on a desk. A named slot: final art replaces this drawing (see docs/studio.md).
ART_CLAPPER = (
    '<svg class="art" viewBox="0 0 180 120" aria-hidden="true"><ellipse cx="90" cy="108" rx="70" ry="8" fill="var(--soft)"/>'
    '<rect x="40" y="48" width="100" height="56" rx="12" fill="var(--st-slate)"/>'
    '<g transform="rotate(-12 40 48)"><rect x="40" y="30" width="100" height="18" rx="6" fill="#eef3fb" stroke="var(--st-slate)" stroke-width="3"/>'
    '<path d="M56 30l-10 18M78 30l-10 18M100 30l-10 18M122 30l-10 18" stroke="var(--st-slate)" stroke-width="9"/></g>'
    '<rect x="54" y="62" width="44" height="6" rx="3" fill="#9fb2dc"/><rect x="54" y="76" width="70" height="6" rx="3" fill="#5c6f99"/>'
    '<rect x="54" y="88" width="56" height="6" rx="3" fill="#5c6f99"/><circle cx="150" cy="30" r="14" fill="var(--accent)" opacity=".18"/>'
    '<circle cx="26" cy="40" r="8" fill="var(--warn)" opacity=".25"/></svg>'
)


def _read(name: str) -> str:
    return (HERE / name).read_text(encoding="utf-8")


def scene_ids() -> list:
    """The placeholder pictures the sprite holds (`s-<id>`): what a desk item may name as its `scene`."""
    return re.findall(r'<symbol id="s-([a-z0-9-]+)"', _read("sprite.svg"))


def _json(value) -> str:
    """JSON that is safe inside a <script> element."""
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_page() -> str:
    script = _read("app.js").replace("__STRINGS__", _json(strings.table())).replace("__SCENES__", _json(scene_ids()))
    return (
        '<!doctype html>\n<html lang="he" dir="rtl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="color-scheme" content="light dark">'
        "<title>" + strings.COMMON["he"]["title"] + "</title><style>" + _read("kit.css") + "</style></head>\n"
        "<body>" + _read("sprite.svg") + '<template id="art-clapper">' + ART_CLAPPER + "</template>\n"
        '<div class="shell"><main class="appframe page nodock" id="app"></main></div>\n'
        "<script>" + script + "</script></body></html>\n"
    )
