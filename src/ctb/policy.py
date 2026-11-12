"""The mirroring switch: is the bridge on?

`"mirror"` in the config file: true or missing means on (the default, like every other option), and so does a
config file that does not exist yet (a fresh install, which has no token to send with anyway). false, a value
that is not a boolean, or a config file that exists but cannot be read means off. The file is read again at every call (it is small; nothing is cached), so a change applies at the
next entry point or loop iteration of every process, in both directions, without a restart.

The switch is a configuration setting, not an emergency stop: it is checked where work starts (each command,
each hook, each round of the long-running services, each request to the panel and the share server, each
chunk of a shared file), and an operation already in progress completes. `harnessgram uninstall` stops
everything at once. One backstop sits in the Telegram client: a request that carries content is refused
while off.

Messages (and edits) the owner sends while off are not delivered later either. The listener persists the end
of an off period (the config file's time when it was switched on, `listener.resume.json`) before it polls
again, and a message dated up to that second is dropped (inbox.serve, inbox.effective_start). Known limits,
documented rather than solved: a message sent in the same second as switching on is not delivered, and an
off-then-on flick shorter than one long poll may not be seen as an off period at all, and another change saved
to the config file after switching on, before the listener notices, moves the boundary to that later save.
"""
from __future__ import annotations

import json


def on() -> bool:
    """Whether the bridge is on right now (read from the config file)."""
    from .config import config_path
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True  # a fresh, unpaired install: nothing can be sent anyway (no token), as before
    except (OSError, ValueError, RecursionError):
        return False  # there, but not readable as settings: off
    if not isinstance(raw, dict):
        return False
    value = raw.get("mirror", True)
    return value if isinstance(value, bool) else False


def explicitly_off() -> bool:
    """The config file is there and does not say on (false, not a boolean, or not readable): unlike a fresh
    install, whose config is missing or has no "mirror" yet."""
    from .config import config_path
    return config_path().exists() and not on()


_on_from_file = on  # the real reader (tests replace `on` and restore it)

OFF_NOTICE = 'Harnessgram: mirroring is off ("mirror": false in the config); nothing was done.'
PAIR_OFF_NOTICE = 'Harnessgram: the bridge is off; turn mirror on first ("mirror": true in the config).'
