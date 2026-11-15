"""What the radio is talking about right now: one short title for the iPhone lock screen.

When a spoken clip goes on the air, the station (radio.py, caption_loop) writes its title to `now.json` in the
radio's private folder. The listen page's token reads it at `GET /listen/<token>/now.json` (listen_link.py, the same
token check as the stream), which answers {"title", "updated"}. While the radio is off, or before anything was said,
the title is the station's name.

A title is the clip's opening line after the station's greeting (the card's headline). Invisible and direction
characters go first, then it is credential-filtered like everything the radio says (creds.for_outside), cut to at most
TITLE_CHARS characters, filtered again, and that final text must pass the child-suitability check
(radio_suitability.py, which fails closed); a title that does not is never shown.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Optional

from .config import write_private

FILE = "now.json"
TITLE_CHARS = 80


def _visible(text: str) -> str:
    """Every invisible character gone (format characters such as a soft hyphen U+00AD, zero-width and direction
    marks), controls and line breaks as spaces, whitespace collapsed. Runs before the secret filter: a key split by an
    invisible character must be whole when the filter looks at it, never put back together after it."""
    from .radio_script import strip_invisible
    text = "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c
                   for c in strip_invisible(str(text or "")) if unicodedata.category(c) != "Cf")
    return " ".join(text.split())


def title_for(text: str) -> str:
    """The lock-screen title of a spoken text, or "" when there is none fit to show. Order matters: invisible
    characters first, then the secret filter, then the cut, then the secret filter and the suitability check on the
    exact text that would be shown."""
    from . import radio_suitability
    from .creds import for_outside
    from .radio_script import headline
    try:
        line = _visible(headline(for_outside(_visible(text))))
        if len(line) > TITLE_CHARS:
            line = line[:TITLE_CHARS - 1].rstrip() + "…"
        line = _visible(for_outside(line))
    except Exception:
        return ""
    if not line or not re.search(r"\w", line) or len(line) > TITLE_CHARS:
        return ""
    try:
        return line if radio_suitability.suitable(line) else ""
    except Exception:
        return ""  # the check could not run: nothing is shown (fail closed)


def publish(folder: Path, title: str, now: Optional[float] = None) -> None:
    """Record the title of the clip that just went on the air; never raises."""
    if not title:
        return
    try:
        write_private(Path(folder) / FILE, json.dumps({"title": title[:TITLE_CHARS],
                                                       "updated": int(time.time() if now is None else now)},
                                                      ensure_ascii=False))
    except Exception:
        pass


def clear(folder: Path) -> None:
    try:
        (Path(folder) / FILE).unlink()
    except OSError:
        pass


def read(folder: Path) -> dict:
    """{"title", "updated"} as last published, or {} when there is none."""
    try:
        data = json.loads((Path(folder) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if (not isinstance(data, dict) or not isinstance(data.get("title"), str) or not data["title"]
            or type(data.get("updated")) is not int):
        return {}
    return {"title": data["title"][:TITLE_CHARS], "updated": data["updated"]}
