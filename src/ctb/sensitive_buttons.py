"""Sensitive-button markers for the HarnessGram iPhone app (defence in depth, never the only check).

The app asks for Face ID or the passcode before it acts on a button the bridge marked as sensitive:

* a callback button's `callback_data` starts with the ASCII bytes ``hg!`` (3 of Telegram's 64 bytes);
  the bridge strips the prefix when it reads the callback (`strip`), so every handler sees the same data;
* a button without callback data (a URL or web app button) has a title starting with "🔐 " (U+1F510 and a space).

Sensitive means money, deletion, publication, production promotion, store submission and accounts: the owner
rules' list, the same areas `dialogs._SENSITIVE` turns a card red for. Only the button that acts is marked,
never its "No" or "Cancel" (a marked button always asks, whatever its title).

Every outgoing Bot API call passes through `mark` (telegram.Bot.call), so a keyboard built anywhere is covered by
the table below; a button whose action is decided by its words (an agent card's or a dialog's option) is marked
when its label names a sensitive area and does not read as a refusal. The handlers keep every check of their own.
"""
from __future__ import annotations

import re
import sys
import unicodedata
from typing import Optional

# Invisible characters: every format character (Cf), the combining grapheme joiner, variation selectors and the Hangul
# fillers that render as nothing.
# Letters (category L) that render as nothing: the Hangul fillers and the Khmer inherent vowels.
_HIDDEN_LOOKALIKES = frozenset("\u115f\u1160\u3164\uffa0\u17b4\u17b5\u2800")  # + Braille blank
CALLBACK_MARK = "hg!"
TITLE_MARK = "\U0001F510 "
CALLBACK_LIMIT = 64  # Telegram's limit for callback_data, in bytes

# The callback data (as the handlers read it) of every button that performs a sensitive action by itself. A button
# that only opens the next step (a picker, a confirmation) is not listed: its confirmation is.
SENSITIVE_CALLBACKS = tuple(re.compile(p) for p in (
    r"cr:[0-9a-f]{16}:yes",                              # explicit paid-credit activation
    r"more:/account add(?: [a-z0-9][a-z0-9-]{0,19})?",    # starts a sign-in to a new Claude account (accounts)
    r"more:/account remove [a-z0-9][a-z0-9-]{0,19} yes",  # removes an account (accounts, deletion)
    r"more:/account use [a-z0-9][a-z0-9-]{0,19}",         # switches the default account of new sessions (accounts)
    r"more:/account move [a-z0-9][a-z0-9-]{0,19}(?: now)?",  # moves a session to another account (accounts)
    r"mb:a:\d{1,16}:[0-9a-f]{16}",                        # adds a person to the team group (accounts, access)
    r"mb:r:\d{1,16}",                                     # removes a team member and their queued messages (deletion)
    r"sm:xy:\S{1,50}",                                    # deletes a session's topic forever (deletion)
    r"alerts:use:-?\d{1,20}",                             # makes a channel the bridge's alerts channel (publication)
    r"al:r:full",                                         # posts full replies to the alerts channel (publication)
    r"al:f:on",                                           # posts files to the alerts channel (publication)
))
# Buttons whose action is the option written on them: marked when the label itself is sensitive.
LABELLED_CALLBACKS = re.compile(r"(?:agr:\d{1,9}:\d{1,2}|dg:x:\d{1,2}:\S{1,40})")


def text_sensitive(text: str) -> bool:
    """Whether `text` names a sensitive area: the dialog cards' own red-card rule (dialogs._SENSITIVE, plus a
    credential path). Over-matching only costs one more Face ID."""
    from .dialogs import _SECRET_PATH, _SENSITIVE  # lazy: dialogs imports telegram, which imports this module
    # Hidden characters can hide a word from the word rule in more ways than any list of them covers (zero-width
    # joiners, bidi marks, variation selectors of every script, fillers). So this is an allow-list of what plainly
    # shows: letters, digits, punctuation, symbols, the plain space and newline. Anything else (a mark, a format or
    # control character, an unusual space) fails closed: the text counts as sensitive, which costs one more Face ID.
    raw = str(text or "")
    if any(not _plain(c) and not _emoji_style(raw, i) for i, c in enumerate(raw)):
        return True
    raw = unicodedata.normalize("NFKC", raw)  # fullwidth letters
    return bool(_SENSITIVE.search(raw) or _SECRET_PATH.search(raw))


def _emoji_style(text: str, i: int) -> bool:
    """U+FE0F right after a symbol is the ordinary emoji style ("⚠️", "✔️"); anywhere else it is hidden."""
    return text[i] == "\ufe0f" and i > 0 and unicodedata.category(text[i - 1]) == "So"


def _plain(c: str) -> bool:
    if c in " \n":
        return True
    if c in _HIDDEN_LOOKALIKES:
        return False
    return unicodedata.category(c)[0] in "LNPS"



def label_sensitive(label: str) -> bool:
    """An option label that names a sensitive area. A refusal is not exempt: "No, delete the backups" acts, and a
    plain "No" or "Cancel" names nothing sensitive anyway (a marked refusal only costs one more Face ID)."""
    return text_sensitive(label)


def decision_sensitive(rec: dict) -> bool:
    """Whether a decision (decisions.py) may approve a sensitive action. An allow-list: only a decision its asker
    marked routine (`ctb decide ask --routine`) can be answered outside the app's Face ID, and even then not when
    its title, detail or either label names a sensitive area. Old, unmarked or damaged records are sensitive."""
    if not isinstance(rec, dict) or rec.get("routine") is not True:
        return True
    parts = [rec.get(k) for k in ("title", "detail", "yes", "no")]
    if not all(isinstance(p, str) for p in parts):
        return True
    return text_sensitive("\n".join(parts))


def callback_sensitive(data: str, label: str = "") -> bool:
    if not isinstance(data, str) or not data:
        return False
    if any(p.fullmatch(data) for p in SENSITIVE_CALLBACKS):
        return True
    return bool(LABELLED_CALLBACKS.fullmatch(data)) and label_sensitive(label)


def mark_title(text: str) -> str:
    """A URL or web app button's title with the lock-with-key marker (once)."""
    text = str(text or "")
    return text if text.startswith(TITLE_MARK) else TITLE_MARK + text


def strip(data):
    """The callback data as the handlers know it: one leading marker removed."""
    if isinstance(data, str) and data.startswith(CALLBACK_MARK):
        return data[len(CALLBACK_MARK):]
    return data


def _button(button):
    """(button to send or None to leave it out, changed)."""
    if not isinstance(button, dict):
        return button, False
    data = button.get("callback_data")
    if not isinstance(data, str) or data.startswith(CALLBACK_MARK):
        return button, False
    if not callback_sensitive(data, str(button.get("text") or "")):
        return button, False
    marked = CALLBACK_MARK + data
    if len(marked.encode("utf-8")) > CALLBACK_LIMIT:
        # Never send a sensitive action unmarked: the button is left out (a guard test keeps every listed shape
        # within the limit, so this only catches a future change).
        try:
            sys.stderr.write("sensitive_buttons: a sensitive button was left out (callback data too long)\n")
        except Exception:
            pass
        return None, True
    return dict(button, callback_data=marked), True


def mark_markup(markup):
    """An inline keyboard with every sensitive acting button marked; anything else is returned unchanged."""
    rows = markup.get("inline_keyboard") if isinstance(markup, dict) else None
    if not isinstance(rows, list):
        return markup
    out, changed = [], False
    for row in rows:
        if not isinstance(row, list):
            out.append(row)
            continue
        kept = []
        for button in row:
            new, touched = _button(button)
            changed |= touched
            if new is not None:
                kept.append(new)
        if kept or not row:
            out.append(kept)
        else:
            changed = True
    return dict(markup, inline_keyboard=out) if changed else markup


def mark(params: Optional[dict]) -> Optional[dict]:
    """A Bot API call's parameters with its reply_markup marked (telegram.Bot.call runs every call through it)."""
    if not isinstance(params, dict) or not isinstance(params.get("reply_markup"), dict):
        return params
    markup = mark_markup(params["reply_markup"])
    return params if markup is params["reply_markup"] else dict(params, reply_markup=markup)
