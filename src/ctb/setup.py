"""`harnessgram setup`: one guided first run.

BotFather steps (with a suggested username), the token read from the clipboard or a paste (just the token or
BotFather's whole message), token check (format, then getMe), Threaded Mode, pairing (the account that sends
the one-time /start code), config (mode 600), install, a "connected" test message, and doctor.

Everything that talks to the user or the network is injected, so the wizard is tested without a
terminal. The step functions below the wizard's helpers (check_bot, apply_settings, save_settings,
install_step, send_connected) are shared with the guided setup page (`setup --web`, setup_page.py), so both
paths validate, store and install exactly the same way. Non-interactive use (CI, scripts): pass `--non-interactive`, put the token in the
CTB_BOT_TOKEN environment variable (never on the command line) and give `--owner-id`.
The token is never printed.
"""
from __future__ import annotations

import copy
import getpass
import json
import os
import secrets
import time
from pathlib import Path
from typing import Callable, List, Optional

from . import config as C
from . import lang as L
from . import pairing
from .i18n import t
from . import clipboard
from .telegram import Bot, TelegramError, clean_token, find_token, holds_token

BOTFATHER_STEPS = """\
Step 1 of 4: create your bot (about 1 minute, in the Telegram app)
  1. Open @BotFather (https://t.me/BotFather) and send /newbot.
     When it asks, send a name, for example: Harnessgram
     Then a username that ends in "bot", for example: {username}
  2. BotFather answers with a message that holds the token (it looks like 123456789:AAE...).
     Tap the token to copy it, or copy the whole message. Setup reads it from the clipboard.
  3. Still in BotFather: /mybots > your bot > Bot Settings > Threaded Mode > turn ON.
     (This gives the private chat with your bot Topics: one topic per Claude Code session.
     Setup checks it for you.)
"""
USERNAME_CHARS = "abcdefghijkmnpqrstuvwxyz23456789"  # no look-alikes (l/1, o/0) to retype


def suggested_username(choice: Callable[[str], str] = secrets.choice) -> str:
    """A username BotFather is likely to accept on the first try (random, so it is rarely taken already)."""
    return "harnessgram_" + "".join(choice(USERNAME_CHARS) for _ in range(6)) + "_bot"


def botfather_steps(username: Optional[str] = None) -> str:
    return BOTFATHER_STEPS.format(username=username or suggested_username())


CONNECTED_TEXT = t("setup_connected", lang="en")
LISTENER_WAIT = 45.0  # seconds a just-started listener gets to report a poll (one long poll is 25 s)
THREADED_MODE_HOW = "In BotFather: /mybots > your bot > Bot Settings > Threaded Mode > turn on."


class SetupError(Exception):
    """A step failed; the message says what to do next. `kind` is a fixed code the setup page shows a fixed
    sentence for (it never shows the message itself): network, rejected, save or other."""

    def __init__(self, message: str, kind: str = "other"):
        super().__init__(message)
        self.kind = kind


def _existing_raw(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


TOKEN_PROMPT = "Press Enter to read the token from the clipboard, or paste it here (hidden): "
TOKEN_TRIES = 3


PASTE_QUIET = 0.25  # seconds without more input that end one paste (pasted lines arrive together)
PASTE_LIMIT = 64 * 1024


def read_hidden(prompt: str, tty: str = "/dev/tty", quiet: float = PASTE_QUIET) -> str:
    """A hidden prompt that keeps a multi-line paste together. getpass reads one line and then discards the rest
    of the paste (it restores the terminal with TCSAFLUSH), so the token on a later line of BotFather's message was
    lost; and a terminal in line mode cuts a line past its own limit (1024 bytes on macOS) before anyone can check
    its length. Here echo and line mode go off, bytes are read as they come (Backspace deletes, Ctrl-U clears the
    line, Ctrl-D on nothing is the end of input, Ctrl-C still interrupts), and after an Enter every byte that
    follows within `quiet` seconds belongs to the same paste. More than PASTE_LIMIT bytes is refused, never cut.
    The terminal is restored on every exit. Without a terminal it is getpass."""
    try:
        import select
        import termios
        fd = os.open(tty, os.O_RDWR | os.O_NOCTTY)
    except (ImportError, OSError):
        return getpass.getpass(prompt)
    try:
        try:
            old = termios.tcgetattr(fd)
        except termios.error:
            return getpass.getpass(prompt)
        new = termios.tcgetattr(fd)
        new[3] &= ~(termios.ECHO | termios.ICANON)
        new[6][termios.VMIN], new[6][termios.VTIME] = 1, 0
        buf, too_long, entered = bytearray(), False, False
        try:
            os.write(fd, prompt.encode("utf-8", "replace"))
            termios.tcsetattr(fd, termios.TCSAFLUSH, new)
            while True:
                if entered and not select.select([fd], [], [], quiet)[0]:
                    break  # Enter, then nothing more: the paste (or the typing) is over
                data = os.read(fd, 4096)
                if not data:
                    break
                for byte in data:
                    entered = entered or byte in (0x0A, 0x0D)  # also counted once the paste is too long to keep
                    if byte in (0x7F, 0x08):
                        del buf[-1:]
                    elif byte == 0x15:
                        del buf[buf.rfind(b"\n") + 1:]
                    elif byte == 0x04 and not buf:
                        raise EOFError
                    elif not too_long:
                        buf.append(byte)
                        too_long = len(buf) > PASTE_LIMIT
        finally:
            termios.tcsetattr(fd, termios.TCSAFLUSH, old)
            os.write(fd, b"\n")
        if too_long:
            raise TelegramError("the paste is too long to hold just a bot token; copy only the token")
        return buf.decode("utf-8", "replace")
    finally:
        os.close(fd)


def clear_if_holds(token: Optional[str], read_clipboard: Callable[[], Optional[str]],
                   clear_clipboard: Callable[[], bool]) -> Optional[bool]:
    """Empty a clipboard that holds `token` (or, with token None, anything shaped like a token). None when it does
    not (nothing is touched), else whether clearing worked. The clipboard is read again right before clearing and
    left alone when its content changed; the moment between that read and the clear remains (no clipboard tool
    offers compare-and-clear), so content copied in that instant can still be emptied."""
    def held(text) -> bool:
        return bool(text) and (token in text if token else holds_token(text))
    first = read_clipboard()
    if not held(first):
        return None
    if read_clipboard() != first:
        return None
    return clear_clipboard()


def _clear_after(clear_clipboard: Callable[[], bool], say: Callable[[str], None], what: str) -> bool:
    """Empty a clipboard that held a token, and say plainly whether that worked."""
    if clear_clipboard():
        say(f"  {what} Cleared the clipboard, so the token cannot be pasted somewhere by mistake.")
        return True
    say(f"  {what} Could not clear the clipboard: it still holds the token. Copy something else to replace it.")
    return False


def ask_token(getpass_fn: Callable[[str], str], say: Callable[[str], None] = print,
              read_clipboard: Callable[[], Optional[str]] = clipboard.read,
              clear_clipboard: Callable[[], bool] = clipboard.clear) -> str:
    """The token from a hidden prompt: Enter alone reads the clipboard (the token, or BotFather's whole message,
    copied in Telegram), anything typed or pasted is searched the same way. A clipboard that held a token is
    cleared whether the token was taken or refused. Nothing read here is ever printed."""
    for _ in range(TOKEN_TRIES):
        try:
            typed = getpass_fn(TOKEN_PROMPT)
        except TelegramError as e:  # read_hidden refused a paste too long to check
            say(f"  {e.description}.")
            continue
        from_clipboard = not typed.strip()
        text = read_clipboard() if from_clipboard else typed
        if from_clipboard and not text:
            say("  The clipboard is empty or cannot be read here. Copy the token in Telegram, or paste it.")
            continue
        try:
            token = find_token(text)
            if token is None and not from_clipboard and text.strip()[:1].isdigit():
                clean_token(text)  # a typed attempt at a bare token: say what is wrong with its shape
        except TelegramError as e:
            if from_clipboard and holds_token(text):
                _clear_after(clear_clipboard, say, f"{e.description}.")
            else:
                say(f"  {e.description}.")
            continue
        if token is None:
            where = "The clipboard holds" if from_clipboard else "That text holds"
            say(f"  {where} no bot token. Copy the token from BotFather's message (it looks like 123456789:AAE...).")
            continue
        if from_clipboard:
            _clear_after(clear_clipboard, say, "Read the token from the clipboard.")
        return token
    raise SetupError("no bot token was given; copy it from BotFather and run `harnessgram setup` again")


def forget_pasted_token(token: Optional[str], say: Callable[[str], None], read_clipboard: Callable[[], Optional[str]],
                        clear_clipboard: Callable[[], bool]) -> Optional[bool]:
    """However setup ended after the prompt was shown: a clipboard that still holds the token (or, when no token
    was accepted, anything shaped like one) is emptied. None when it does not (nothing to do), else whether
    clearing worked (said plainly either way)."""
    cleared = clear_if_holds(token, read_clipboard, clear_clipboard)
    if cleared is None:
        return None
    return _clear_after(lambda: cleared, say, "The clipboard still held the token.")


def _token(raw: dict, env: dict, interactive: bool, getpass_fn: Callable[[str], str],
           assume_yes: bool = False, input_fn: Callable[[str], str] = input,
           say: Callable[[str], None] = print, read_clipboard: Optional[Callable[[], Optional[str]]] = None,
           clear_clipboard: Optional[Callable[[], bool]] = None) -> str:
    """Existing config first, then CTB_BOT_TOKEN, then a hidden prompt (or the clipboard, see ask_token)."""
    has_saved = bool(raw.get("bot_token") or raw.get("bot_token_command"))
    saved_token = None
    if has_saved:
        try:
            saved_token = C.from_dict(raw).token()
        except C.ConfigError as e:
            raise SetupError(f"the saved token does not work: {e}") from None

    given = env.get("CTB_BOT_TOKEN")
    if given:
        new_token = clean_token(given)
        if has_saved and new_token != saved_token:
            replace = False
            if assume_yes:
                replace = True
            elif interactive:
                answer = input_fn("CTB_BOT_TOKEN is set to a different bot token. Replace the saved token? [y/N] ")
                if answer.strip().lower() in ("y", "yes"):
                    replace = True
            if replace:
                say("Replacing the saved bot token.")
                raw["bot_token"] = new_token
                raw.pop("bot_token_command", None)
                return new_token
            return saved_token
        if not has_saved:
            return new_token

    if has_saved:
        return saved_token

    if not interactive:
        raise SetupError("no bot token: set CTB_BOT_TOKEN in the environment (not on the command line)")
    return ask_token(getpass_fn, say, read_clipboard or clipboard.read, clear_clipboard or clipboard.clear)


def _threaded_mode(bot, me: dict, interactive: bool, input_fn: Callable[[str], str], say: Callable[[str], None]) -> bool:
    """True when Threaded Mode is on. Interactive runs wait for the user to switch it on."""
    for attempt in range(3):
        if me.get("has_topics_enabled"):
            say("  Threaded Mode is on.")
            return True
        say(f"  Threaded Mode is OFF. {THREADED_MODE_HOW}")
        if not interactive:
            return False
        input_fn("  Press Enter when it is on (I will check again): ")
        me = bot.call("getMe", timeout=10)
    return bool(me.get("has_topics_enabled"))


def check_bot(bot) -> dict:
    """getMe: the bot's profile, or SetupError (kind "network" or "rejected")."""
    try:
        return bot.call("getMe", timeout=10)
    except TelegramError as e:
        if e.description in ("network error", "timeout") or e.code is None:
            raise SetupError(f"Could not reach Telegram ({e.description}). Check the internet connection and try again.",
                             kind="network") from None
        raise SetupError(f"Telegram rejected the token ({e.description}). Copy it again from BotFather "
                         "(/mybots > your bot > API Token).", kind="rejected") from None


def pairing_link(username: str, code: str) -> str:
    """The link whose Start button sends exactly "/start <code>" to the bot."""
    return f"https://t.me/{username}?start={code}"


def account_of(found: dict) -> dict:
    """Name, username and id of the account whose /start arrived."""
    who = found.get("from") or {}
    name = " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x) or "?"
    return {"name": name, "username": who.get("username", "-"), "id": who.get("id"),
            "chat_id": int(found["chat"]["id"])}


# First-run choices the setup page offers, each with the only values the bridge accepts for it. The config keys
# go through apply_settings; "activity" is the live-activity level the bot's settings menu also sets (a state
# file, config.set_activity_level).
PREFERENCES = {
    "target_language": tuple(L.LANG_NAMES),  # the languages the bridge knows names and scripts for
    "translate": (False, True),
    "ui_language": ("en", "he"),  # the bot's menus exist in these two languages (config.from_dict)
    "context_footer": (False, True),
    "activity": C.ACTIVITY_LEVELS,
}
CONFIG_PREFERENCES = ("translate", "ui_language", "context_footer")


def check_preferences(prefs) -> dict:
    """The chosen preferences, or SetupError (kind "bad_request") for an unknown key or a value outside the
    allowed set (booleans must be real booleans: 1 or "true" are refused)."""
    if not isinstance(prefs, dict) or set(prefs) - set(PREFERENCES):
        raise SetupError("unknown preference", kind="bad_request")
    for key, value in prefs.items():
        allowed = PREFERENCES[key]
        if isinstance(allowed[0], bool):
            ok = isinstance(value, bool)
        else:
            ok = isinstance(value, str) and value in allowed
        if not ok:
            raise SetupError(f"{key} has a value the bridge does not accept", kind="bad_request")
    return dict(prefs)


def new_install_defaults(raw: dict) -> None:
    """Mark a newly created configuration so legacy migration never enables its defaults."""
    raw.update({"release_defaults_v1": True, "auto_account_switch": False,
                "quota_auto_continue": False, "session_auto_revive": False,
                "progress": False, "progress_details": False, "auto_topic_names": False,
                "topic_check_reminders": False})


def apply_settings(raw: dict, chat_id: int, token: Optional[str], language: Optional[str],
                   prefs: Optional[dict] = None) -> None:
    """Owner, token, languages, chosen preferences (already checked) and defaults into `raw`; SetupError when
    the result is not a valid config. The terminal wizard passes no preferences."""
    raw.update(owner_chat_id=chat_id)
    if token and not raw.get("bot_token_command"):
        raw["bot_token"] = token
    if language:
        raw["target_language"] = language
    for key in CONFIG_PREFERENCES:
        if prefs and key in prefs:
            raw[key] = prefs[key]
    raw.setdefault("target_language", "en")
    # Hebrew readers get Hebrew menus unless they chose otherwise (same rule as config.from_dict)
    raw.setdefault("ui_language", "he" if raw.get("target_language") == "he" else "en")
    raw.setdefault("translate", False)
    try:
        C.from_dict(raw)
    except C.ConfigError as e:
        raise SetupError(f"the settings are not valid: {e}", kind="save") from None


IDENTITY = ("bot_token", "bot_token_command", "owner_chat_id")  # which bot, and whose


def same_identity(a: dict, b: dict) -> bool:
    return all(a.get(k) == b.get(k) for k in IDENTITY)


def save_settings(before: dict, raw: dict, path: Path, replace_owner: bool, guard_identity: bool = False) -> Path:
    """Only what changed, onto a fresh read of the file, mode 600 (config.save_changes). With guard_identity
    (the setup page), a file whose bot or owner changed since `before` was read stops the save inside the
    config lock (SetupError kind "conflict"): merging only our changes would keep the other bot or owner."""
    def check(fresh: dict) -> None:
        if not same_identity(fresh, before):
            raise SetupError("the settings were changed elsewhere while setup ran", kind="conflict")
    try:
        return C.save_changes(before, raw, path, replace_owner=replace_owner,
                              check=check if guard_identity else None)
    except C.ConfigError as e:
        raise SetupError(f"{e} (run setup interactively, or `harnessgram pair`, to change the owner)",
                         kind="save") from None


def install_step(install_hooks: bool, service: bool, start: bool, settings_file: Optional[Path],
                 say: Callable[[str], None]):
    """The listener service and the Claude Code hooks: the install report (what was done and what was checked),
    or None when skipped (--no-install)."""
    if not install_hooks:
        say("  Skipped (--no-install). Run `harnessgram install` yourself.")
        return None
    from .install import run_install
    return run_install(dry_run=False, service=service, wake=True, start=start, settings_file=settings_file)


def health(report, bot, say: Callable[[str], None] = print, wait: Optional[float] = None):
    """(listener, results): one check of the listener this install started (waited for, and only counting a
    poll it reported after its start), and every doctor check built on that same result. Shared with the setup
    page so both show what was checked the same way."""
    from . import doctor
    started = report is not None and "listener" in report.running
    wait = LISTENER_WAIT if wait is None else wait
    if started:
        say(f"  Waiting up to {int(wait)} seconds for the new listener to poll Telegram")
    listener = (doctor.wait_listener(wait, restarted=True, old_run=report.old_run) if started
                else doctor.wait_listener(0))
    return listener, doctor.collect(bot=bot, listener=listener)


def listener_confirmed(report, listener) -> Optional[bool]:
    """None when this install started no listener; else whether the health check saw that new listener poll
    Telegram. A warn after the wait, or no health check at all, is not a confirmation."""
    if report is None or "listener" not in report.running:
        return None
    return bool(listener) and listener[0][0] == "ok"


def summary(saved: Optional[Path], report, test_sent: Optional[bool], listener: Optional[list]) -> List[str]:
    """The closing lines: what setup checked, then plainly what it did not. `test_sent` and `listener` are None
    when not tried; `listener` is doctor.check_listener's result."""
    checked, unchecked = [], []
    if saved is not None:
        loose = C.loose_mode(saved)
        (unchecked if loose else checked).append(f"config saved to {saved}" + (
            f", but readable by others (mode {loose})" if loose else " (mode 600)"))
    if report is None:
        unchecked.append("the hooks and the background service were not installed (--no-install)")
    else:
        checked += report.checked()
        unchecked += report.unchecked()
    if test_sent:
        checked.append("a test message was sent to your chat")
    else:
        unchecked.append("the test message " + ("was not sent (see above)" if test_sent is False else
                                                "was skipped"))
    if listener and listener[0][0] == "ok":
        checked.append("the health check: " + listener[0][1])
    else:
        unchecked.append("that the listener reaches Telegram" + (
            f" ({listener[0][1]})" if listener else ": run harnessgram doctor"))
    unchecked.append("that a Claude Code session reaches your chat (setup starts none)")
    lines = ["\nChecked:"] + [f"  - {c}" for c in checked] + ["Not checked:"] + [f"  - {u}" for u in unchecked]
    return lines


def send_connected(bot, chat_id: int, ui_lang: str) -> Optional[str]:
    """The "connected" test message in the bot's UI language: None when sent, else Telegram's reason."""
    try:
        bot.call("sendMessage", {"chat_id": chat_id, "text": t("setup_connected", lang=ui_lang)})
        return None
    except TelegramError as e:
        return e.description


def run(*, owner_id: Optional[int] = None, language: Optional[str] = None, assume_yes: bool = False,
        interactive: bool = True, install_hooks: bool = True, service: bool = True, start: bool = True,
        test_message: bool = True, run_doctor: bool = True, settings_file: Optional[Path] = None,
        bot=None, env: Optional[dict] = None, input_fn: Callable[[str], str] = input,
        getpass_fn: Callable[[str], str] = read_hidden, say: Callable[[str], None] = print,
        pair_timeout: float = 180, read_clipboard: Optional[Callable[[], Optional[str]]] = None,
        clear_clipboard: Optional[Callable[[], bool]] = None) -> int:
    env = os.environ if env is None else env
    path = C.config_path()
    raw = _existing_raw(path)
    before = copy.deepcopy(raw)
    if not path.exists():
        new_install_defaults(raw)
    say("Harnessgram setup\n")
    say(botfather_steps())
    prompted, token = [], None

    def prompt(text: str) -> str:
        prompted.append(text)
        return getpass_fn(text)
    try:
        token = _token(raw, env, interactive, prompt, assume_yes=assume_yes, input_fn=input_fn, say=say,
                       read_clipboard=read_clipboard, clear_clipboard=clear_clipboard)
        bot = bot or Bot(token)
        say("Step 2 of 4: checking the bot with Telegram")
        me = check_bot(bot)
        say(f"  Bot @{me.get('username')} answers.")
        topics_on = _threaded_mode(bot, me, interactive, input_fn, say)

        say("\nStep 3 of 4: pair your Telegram account")
        chat_id = owner_id or 0
        if not chat_id and raw.get("owner_chat_id"):
            chat_id = int(raw["owner_chat_id"])
            say(f"  Already paired (chat id {chat_id}); keeping it. Delete owner_chat_id from {path} to pair again.")
        if not chat_id:
            if not interactive:
                raise SetupError("pass --owner-id <your numeric Telegram id> for a non-interactive setup")
            code, started = pairing.new_code(), time.time()
            say(f"  Open @{me.get('username')} in Telegram and send exactly this message "
                f"(waiting up to {int(pair_timeout // 60) or 1} minutes):\n\n      /start {code}\n")
            found = pairing.wait_for_start(bot, code, started, timeout=pair_timeout)
            if not found:
                raise SetupError("no matching /start arrived; nothing was saved. Run `harnessgram setup` again.")
            who = account_of(found)
            if not assume_yes:
                answer = input_fn(f"  Pair with {who['name']} (@{who['username']}, id {who['id']})? "
                                  "Only this account will be able to control your sessions. [y/N] ")
                if answer.strip().lower() not in ("y", "yes"):
                    raise SetupError("not paired.")
            chat_id = who["chat_id"]
        elif owner_id:
            say(f"  Using the Telegram id you gave ({chat_id}).")

        upgrade = "target_language" in raw  # a config from before the bot spoke English
        if language is None and interactive and "target_language" not in raw:
            language = (input_fn("\n  Language you want to read replies in, ISO code like en, he, ar, ru, es [en]: ")
                        .strip().lower() or "en")
        if interactive and "ui_language" not in raw and not upgrade:
            # asked only on a first setup; an existing config keeps its menus below
            guess = "he" if (language or "") == "he" else "en"
            ui_lang = (input_fn(f"  Bot UI language (menus and status), en or he [{guess}]: ") or "").strip().lower() or guess
            raw["ui_language"] = ui_lang if ui_lang in ("en", "he") else guess
        apply_settings(raw, chat_id, token, language)
        # a new owner only when someone is answering the prompts (or none was paired yet)
        replace = (interactive and not assume_yes) or not _existing_raw(path).get("owner_chat_id")
        saved = save_settings(before, raw, path, replace)
        say(f"  Saved {saved} (mode 600).")

        say("\nStep 4 of 4: install the listener service and the Claude Code hooks")
        report = install_step(install_hooks, service, start, settings_file, say)

        test_sent = None
        if test_message:
            failed = send_connected(bot, chat_id, raw.get("ui_language", "en"))
            test_sent = failed is None
            if failed is None:
                say("  Sent a test message to your chat with the bot.")
            else:
                say(f"  Could not send the test message ({failed}). Open the bot and press Start, then retry.")
    except SetupError as e:
        say(f"\nSetup stopped: {e}")
        return 1
    except TelegramError as e:
        say(f"\nSetup stopped: {e.description}")
        return 1
    except (EOFError, KeyboardInterrupt):
        say("\nSetup cancelled; nothing more was changed.")
        return 1
    finally:
        if prompted:  # a token that came through the prompt must not stay pasteable, however setup ended
            forget_pasted_token(token, say, read_clipboard or clipboard.read, clear_clipboard or clipboard.clear)

    code = 0 if report is None else report.code
    listener = None
    if run_doctor:
        from . import doctor
        say("\nChecking everything (harnessgram doctor)")
        listener, results = health(report, bot, say)
        code = doctor.show(results, say=say) or code
    if listener_confirmed(report, listener) is False:
        code = code or 1  # a listener that had to start and was not seen working is no full success
    for line in summary(saved, report, test_sent, listener):
        say(line)
    if not topics_on:
        say("\nThreaded Mode is still off: turn it on in BotFather, then run `harnessgram doctor`.")
        code = code or 1
    elif code == 0:
        say("\nSetup finished. Start a new Claude Code session; its first reply creates its topic.")
    return code
