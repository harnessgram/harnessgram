"""`harnessgram bot-api logout|close`: move the bot between Telegram's cloud server and your own local server.

The token comes from the saved config (or its token command) and stays inside this process: it is never a
command-line argument, an environment variable you type, or part of your shell history."""
from __future__ import annotations

import sys
import re
from urllib.parse import unquote
from typing import Callable

from . import config as C

ACTIONS = ("logout", "close")
# A bot token as it appears bare or in a URL: digits, a colon (also written %3A in any case), then 20 or more token
# characters. The lookbehind makes a match start only at the start of a digit run, so a long run of digits cannot
# make the search quadratic; nothing here nests a repeat inside a repeat, and nothing is built from the token.
_BARE_TOKEN = re.compile(r"(?<!\d)\d+(?::|%3a)[A-Za-z0-9_-]{20,}", re.I)
_BOT_URL = re.compile(r"bot\d+(?::|%3a)[A-Za-z0-9_-]{20,}", re.I)
MAX_TEXT = 4000        # a server message is judged up to this many characters; the rest is cut with a marker
MAX_DECODES = 3        # further percent-decodings looked at; text that still changes after them is too deep to judge
CUT = " [cut]"
HIDDEN = "<server message hidden: it contained the bot token>"
TOO_DEEP = "<server message hidden: it was encoded too deeply to check>"


def safe(text: str, token: str) -> str:
    """Text from a server for printing. A server (or a proxy in front of it) may echo the request URL in its error
    description, and the URL holds the token. The text is cut at MAX_TEXT, then percent-decoded up to MAX_DECODES
    more times (stopping when it no longer changes); if the token is in the text at ANY of those levels (compared
    as written and case-folded), or the text still changes after the last decoding, the whole message is replaced
    by a fixed line. Otherwise the generic token and bot-URL patterns are applied. No pattern is built from the
    token, so the work is linear in the text whatever the token's length."""
    text = str(text)
    cut = len(text) > MAX_TEXT
    if cut:
        if token and token in text:  # the token beyond the cut point: nothing of this message is shown
            return HIDDEN
        text = text[:MAX_TEXT]
    levels = [text]
    for _ in range(MAX_DECODES):
        nxt = unquote(levels[-1])
        if nxt == levels[-1]:
            break
        levels.append(nxt)
    else:
        if unquote(levels[-1]) != levels[-1]:
            return TOO_DEEP
    if token:
        folded = token.casefold()
        if any(token in level or folded in level.casefold() for level in levels):
            return HIDDEN

    def word(m: "re.Match") -> str:
        w = m.group()
        return "<url hidden>" if "/" in w and _BOT_URL.search(w) else w
    out = _BARE_TOKEN.sub("<hidden>", re.sub(r"\S+", word, text))
    return out + CUT if cut else out


CONSEQUENCE = {
    "logout": "You can start your own server right away, but the bot cannot log in to the cloud server again "
              "for about 10 minutes.",
    "close": "It is meant for moving the bot to another local server, and Telegram refuses it in the first "
             "10 minutes after the bot's server instance started.",
}
AFTER = {
    "logout": "Start your local Bot API server now; going back to the cloud server works after about 10 minutes.",
    "close": "The bot's instance on that server is closed.",
}


def run(action: str, assume_yes: bool = False, ask: Callable[[str], str] = input) -> int:
    """logout: log the bot out of Telegram's cloud server (do this once, before the first start of your own
    server; you can start it right away, but the bot cannot log in to the cloud again for about 10 minutes).
    close: close the bot on your own local server, for moving it to another local server (Telegram refuses it
    in the first 10 minutes after the bot's server instance started)."""
    from .telegram import Bot, TelegramError
    if action not in ACTIONS:
        print(f"Harnessgram: unknown action {action!r}; use one of: {', '.join(ACTIONS)}", file=sys.stderr)
        return 2
    try:
        cfg = C.load()
        token = cfg.token()
    except C.ConfigError as e:
        print(f"Harnessgram: {e}", file=sys.stderr)
        return 1
    if action == "close" and not cfg.api_local:
        print("Harnessgram: `close` talks to your own local Bot API server; set api_base and api_local first.",
              file=sys.stderr)
        return 1
    where = "Telegram's cloud server" if action == "logout" else "your local Bot API server"
    if not assume_yes:
        try:
            answer = ask(f"Send {action} for your bot to {where}? {CONSEQUENCE[action]} [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing sent.")
            return 1
    bot = Bot(token) if action == "logout" else Bot(token, cfg.api_base, cfg.api_local, cfg.api_files_dir)
    try:
        bot.call("logOut" if action == "logout" else "close")
    except TelegramError as e:
        print(f"Harnessgram: {safe(e.description, token)}", file=sys.stderr)
        return 1
    print(f"Done: {action} sent to {where}. {AFTER[action]}")
    return 0
