"""The bridge settings the control panel can show and change: the same ones as the bot's global settings
menu (inbox.global_markup), read and written through the same helpers, so a tap in either place means the
same thing. Each setting is a closed choice: a switch, or one of a fixed list of values."""
from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

from .compact import AUTO_COMPACT_STEPS, auto_compact_limit, set_auto_compact_limit
from .config import (ACTIVITY_LEVELS, AGENT_NOTICES, STATUS_REPOST, activity_level, set_activity_level,
                     set_setting, setting_on)


def _general_off() -> str:
    from .inbox import GENERAL_OFFER_OFF  # the marker lives with the routing code
    return GENERAL_OFFER_OFF


def _agents_topic() -> str:
    from .agents_topic import AGENTS_TOPIC
    return AGENTS_TOPIC


def _every_reply_get() -> bool:
    from .radio_talk import every_reply_on
    return every_reply_on()


def _every_reply_set(on: bool) -> None:
    from .radio_talk import set_every_reply
    set_every_reply(on)


def _board_get() -> bool:
    from .radio import board_on
    return board_on()


def _board_set(on: bool) -> None:
    from .radio import set_board
    set_board(on)


def _room_get() -> bool:
    from .radio_room import enabled
    return enabled()


def _room_set(on: bool) -> None:
    from .radio_room import set_enabled
    set_enabled(on)


def _voice_get() -> bool:
    from . import voice
    return voice.confirm_on()


def _voice_set(on: bool) -> None:
    from . import voice
    voice.set_confirm(on)


def _delivery_get() -> bool:
    from . import delivery
    return delivery.default_mode() == "end"


def _delivery_set(end: bool) -> None:
    from . import delivery
    delivery.set_default_mode("end" if end else "now")


def _away_get() -> bool:
    from . import autopilot
    return autopilot.away_active()


def _away_set(on: bool) -> None:
    from . import autopilot
    autopilot.set_away(on)


def _config_get(name: str) -> bool:
    from .config import load
    return getattr(load(), name)


def _config_set(name: str, value: bool) -> None:
    from .config import load, save_changes
    load()  # migrate before changing a value
    save_changes({}, {name: value})


def _brief_get() -> bool:
    from . import brief
    return brief.on()


def _brief_set(on: bool) -> None:
    from . import brief
    brief.set_on(on)


# name: (reader, writer, allowed values); a bool setting allows (False, True)
SETTINGS: Dict[str, Tuple[Callable[[], Any], Callable[[Any], None], tuple]] = {
    **{name: (lambda n=name: _config_get(n), lambda value, n=name: _config_set(n, value), (False, True))
       for name in ("auto_account_switch", "quota_auto_continue", "session_auto_revive", "progress", "progress_details", "auto_topic_names")},
    "voice_confirm": (_voice_get, _voice_set, (False, True)),
    "delivery_end": (_delivery_get, _delivery_set, (False, True)),
    "auto_compact": (auto_compact_limit, set_auto_compact_limit, tuple(AUTO_COMPACT_STEPS)),
    "away": (_away_get, _away_set, (False, True)),
    # how much of an agent's work its topic shows while it works (config.activity_level)
    "activity": (activity_level, set_activity_level, ACTIVITY_LEVELS),
    "status_repost": (lambda: setting_on(STATUS_REPOST), lambda on: set_setting(STATUS_REPOST, on), (False, True)),
    "agent_notices": (lambda: setting_on(AGENT_NOTICES), lambda on: set_setting(AGENT_NOTICES, on), (False, True)),
    "agents_topic": (lambda: setting_on(_agents_topic()), lambda on: set_setting(_agents_topic(), on), (False, True)),
    "general_offer": (lambda: not setting_on(_general_off()), lambda on: set_setting(_general_off(), not on), (False, True)),
    # the radio says every session's reply; off: only answers to voice notes (radio_talk.py)
    "radio_every_reply": (_every_reply_get, _every_reply_set, (False, True)),
    # the radio's picture: the moving flight board instead of the summary card (radio_show.py); read each second
    "radio_board": (_board_get, _board_set, (False, True)),
    # the brainstorming room on the radio (radio_room.py); off by default, and off ends a live room
    "radio_room": (_room_get, _room_set, (False, True)),
    # the Cockpit's home becomes the Morning Brief (brief.py); off (the default): the classic home, untouched
    "brief_home": (_brief_get, _brief_set, (False, True)),
}


def read() -> Dict[str, Any]:
    """Every setting's current value; one that cannot be read is left out."""
    out = {}
    for name, (get, _set, _allowed) in SETTINGS.items():
        try:
            out[name] = get()
        except Exception:
            continue
    return out


def allowed(name, value) -> bool:
    if name not in SETTINGS:
        return False
    choices = SETTINGS[name][2]
    if choices == (False, True):
        return isinstance(value, bool)
    return type(value) in (int, str) and value in choices


def write(name: str, value) -> None:
    """Change one setting; ValueError for an unknown name or a value it does not allow."""
    if not allowed(name, value):
        raise ValueError("unknown setting or value")
    SETTINGS[name][1](value)
