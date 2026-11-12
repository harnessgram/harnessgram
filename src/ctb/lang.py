"""Language helpers: names, direction, scripts, and the few UI strings we send.

Mixed-language handling needs to know whether a paragraph is already in the
target language. That is decidable for languages with their own script (Hebrew,
Arabic, Cyrillic, ...). For Latin-script targets (Spanish, French, ...) it is not,
so those paragraphs are always sent to the translator.
"""
from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

LANG_NAMES = {
    "en": "English", "he": "Hebrew", "ar": "Arabic", "fa": "Persian", "ur": "Urdu", "yi": "Yiddish",
    "ru": "Russian", "uk": "Ukrainian", "bg": "Bulgarian", "el": "Greek", "hi": "Hindi",
    "th": "Thai", "ja": "Japanese", "zh": "Chinese", "ko": "Korean", "es": "Spanish",
    "fr": "French", "de": "German", "pt": "Portuguese", "it": "Italian", "tr": "Turkish",
    "pl": "Polish", "nl": "Dutch",
}
RTL_LANGS = {"he", "ar", "fa", "ur", "yi"}
RLM = "‏"  # right-to-left mark: makes Telegram lay the message out right to left

# Regex character classes of each script's letters.
SCRIPTS = {
    "he": "֐-׿", "yi": "֐-׿",
    "ar": "؀-ۿݐ-ݿ", "fa": "؀-ۿݐ-ݿ", "ur": "؀-ۿݐ-ݿ",
    "ru": "Ѐ-ӿ", "uk": "Ѐ-ӿ", "bg": "Ѐ-ӿ",
    "el": "Ͱ-Ͽ", "hi": "ऀ-ॿ", "th": "฀-๿",
    "ja": "぀-ヿ一-鿿", "zh": "一-鿿", "ko": "가-힯ᄀ-ᇿ",
}
# Cantillation and vowel points Google sometimes adds to Hebrew; unreadable in chat.
NIKUD = re.compile("[֑-ׇ]")

# Things that are not prose and must not influence language detection.
_NOISE = [
    re.compile(r"```.*?```", re.S),
    re.compile(r"`[^`\n]*`"),
    re.compile(r"\]\([^)]*\)"),
    re.compile(r"https?://\S+"),
    re.compile(r"(?:~|\.{0,2})?/[\w.@~/-]+"),
]

from .i18n import STRINGS

# Translation glossary: fixed renderings the engine gets wrong (it turns
# "session" into meeting/activation). Regex -> replacement, applied to the text before it reaches
# an engine (translate.apply_glossary). One entry per language; Hebrew first.
GLOSSARY: Dict[str, Dict[str, str]] = {
    "he": {r"\b[Ss]essions\b": "סשנים", r"\b[Ss]ession\b": "סשן"},
}


def base(lang: str) -> str:
    return (lang or "").replace("_", "-").split("-")[0].lower()


def lang_name(lang: str) -> str:
    return LANG_NAMES.get(base(lang), lang)


def is_rtl(lang: str) -> bool:
    return base(lang) in RTL_LANGS


def strings(lang: str) -> Dict[str, str]:
    merged = dict(STRINGS["en"])
    merged.update(STRINGS.get(base(lang), {}))
    return merged


def script_class(lang: str) -> Optional[str]:
    return SCRIPTS.get(base(lang))


def strip_noise(text: str) -> str:
    for rx in _NOISE:
        text = rx.sub(" ", text)
    return text


def letter_counts(text: str, lang: str) -> Tuple[int, int]:
    """(letters in the target script, Latin letters) after dropping code, links and paths."""
    text = strip_noise(text)
    cls = script_class(lang)
    target = len(re.findall("[" + cls + "]", text)) if cls else 0
    return target, len(re.findall(r"[A-Za-z]", text))


def needs_translation(paragraph: str, lang: str) -> bool:
    """True when a paragraph should go to the translator."""
    target, latin = letter_counts(paragraph, lang)
    if script_class(lang):
        return latin > target
    return latin >= 2  # Latin-script target: cannot tell, translate any prose
