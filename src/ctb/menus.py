"""One way out of every menu (owner rule: a Close, Back or Cancel button in every relevant menu).

A menu the owner opened gets `close_row`; an offer or a one-button notice the bridge sent on its own
gets `not_now_row`. Both carry the same callback: the listener deletes the message (or shrinks it to
a short note when Telegram refuses to delete an old one). The row is always the last one.
tests/test_way_out.py fails when a new keyboard has no way out.
"""
from __future__ import annotations

from typing import Optional

from .i18n import t

CLOSE = "more:dismiss"


def close_button(lang: Optional[str] = None) -> dict:
    return {"text": t("menu_btn_dismiss", lang=lang), "callback_data": CLOSE}


def close_row(lang: Optional[str] = None) -> list:
    """The last row of a menu the owner opened."""
    return [close_button(lang)]


def not_now_row(lang: Optional[str] = None) -> list:
    """The last row of an offer or a notice the bridge sent by itself."""
    return [{"text": t("menu_btn_not_now", lang=lang), "callback_data": CLOSE}]


def unbuttons_row(lang: Optional[str] = None, *, later: bool = False) -> list:
    """Remove controls while keeping the message text."""
    key = "menu_btn_not_now" if later else "menu_btn_dismiss"
    return [{"text": t(key, lang=lang), "callback_data": "more:unbuttons"}]
