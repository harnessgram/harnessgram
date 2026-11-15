"""Offline child-suitability gate for reply narration and voice digests.

Use the owner's shared checker when installed. The portable fallback is a
conservative word-list filter, not a semantic guarantee about arbitrary prose.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

SHARED_CHECKER = Path.home() / ".agents" / "content-suitability" / "check.py"
FALLBACK = re.compile(
    r"(?<![א-ת])[ובהלמש]?(?:רצח|רוצח|נרצח|נהרג|חנק|סקילה|נסקל|עירום|ערום|ערו?וה|עריות|זנו?ת|זונה|אנס|נאנס)"
    r"|משכב|תשמיש|אשת איש|יפת תואר|יומת|מעשה טבח|מצחק את"
    r"|(?:שכב|שוכב|שכבה|שכבו)\s+(?:את|עם|עמה|עמו|עמי|עמך|אצל|אית)"
    r"|יבו?א אליה(?![א-ת])"
    r"|\b(?:porn\w*|rape|raped|rapist|naked|nudity|murder\w*|massacre\w*|sex|sexual\w*)\b", re.I)


def suitable(text: str) -> bool:
    """Never print rejected text or send it to a service. Checker failures fail closed."""
    if not text or not text.strip():
        return False
    if SHARED_CHECKER.is_file():
        try:
            with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", suffix=".txt") as source:
                source.write(text)
                source.flush()
                result = subprocess.run([sys.executable, "-I", str(SHARED_CHECKER), source.name],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            return result.returncode == 0
        except OSError:
            pass  # not callable on this host: use the portable local word list
        except subprocess.TimeoutExpired:
            return False
    normalized = re.sub(r"[\u0591-\u05C7]", "", text.replace("\u05BE", " "))
    return not FALLBACK.search(normalized)
