"""Unconditional privacy boundary for durable radio records and speech."""
from __future__ import annotations

import re

from . import creds, radio_script as R

ACCOUNT = re.compile(r"(?i)(?<![\w.-])(?:account(?:[ _-]?id)?|(?:user|chat|session)[ _-]?id|חשבון(?=[ \t]*[:=#]))"
                     r'''["']?(?:[ \t]*[:=#][ \t]*|[ \t]+(?:is[ \t]+)?)["']?([^\s"',;<>]+)''')
TOKEN = re.compile(r"(?i)(?<![\w.-])(?:[A-Za-z0-9]+[_-])*(?:token|secret|password|passwd|api[ _-]?key|אסימון|סיסמה)"
                   r'''["']?(?:[ \t]*[:=][ \t]*|[ \t]+(?:is[ \t]+)?)["']?([^\s"',;<>]+)''')
NUMERIC_ID = re.compile(r"(?<!\w)-?\d{6,}(?!\w)")
PRIVATE_FIELD = re.compile(r"(?i)^(?:(?:account|user|chat|owner)(?:[ _-]?id)?|session[ _-]?id|email|"
                           r"(?:[A-Za-z0-9]+[_-])*(?:token|secret|password|passwd|api[ _-]?key))$")


def configured_identifiers(cfg):
    """Already available values only: never run credential commands to scrub radio text."""
    return tuple(str(v) for v in (cfg.bot_token, cfg._cached_token, cfg.gemini_vertex_account,
                                  cfg.owner_chat_id, cfg.chat_id) if v)


def scrub(value, *sources, identifiers=()):
    """Scrub every string, including nested metadata and keys, using source identifiers too.

    Sources are inspected only in memory, before formatting or truncation. Structured
    private fields teach their values even when an unlabelled copy appears elsewhere.
    Unknown, unlabelled opaque values cannot reliably be identified as credentials.
    """
    def strings(obj):
        if isinstance(obj, str):
            yield obj
        elif type(obj) in (int, float):
            yield str(obj)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                yield from strings(k)
                yield from strings(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                yield from strings(v)

    sensitive = set(creds.normalized(str(v)) for v in identifiers if v)

    def learn(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if PRIVATE_FIELD.fullmatch(creds.normalized(str(key))):
                    sensitive.update(creds.normalized(str(v)) for v in strings(value))
                learn(value)
        elif isinstance(obj, (list, tuple)):
            for value in obj:
                learn(value)

    learn((value, *sources))
    for text in strings((value, *sources)):
        text = creds.normalize(text)[0]
        for address in R._EMAIL.findall(text):
            sensitive.update((address, address.split("@", 1)[0]))
        for pattern in (ACCOUNT, TOKEN):
            sensitive.update(v.rstrip(".!?)}]") for v in pattern.findall(text))
    sensitive.discard("")

    def clean(obj):
        if isinstance(obj, str):
            text = creds.for_outside(creds.normalized(obj), True)
            for value in sorted(sensitive, key=len, reverse=True):
                # Small configured IDs must not corrupt other numbers or ordinary words.
                pattern = re.escape(value) if len(value) >= 6 else r"(?<!\w)" + re.escape(value) + r"(?!\w)"
                text = re.sub(pattern, "חשבון", text, flags=re.I)
            text = R.private_speech(text)
            text = creds.UUID.sub("חשבון", text)
            text = NUMERIC_ID.sub("חשבון", text)
            return text
        if isinstance(obj, dict):
            return {clean(k): "חשבון" if PRIVATE_FIELD.fullmatch(creds.normalized(str(k))) else clean(v)
                    for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [clean(v) for v in obj]
        return obj

    return clean(value)
