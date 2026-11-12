"""Easier first run: the token is found in BotFather's whole message, read from the clipboard on Enter (then
cleared), and BotFather gets a ready username to copy. Fakes only: no network, never the real clipboard."""
import json
import os
import re
import select
import signal
import stat
import termios
import threading
import time

import pytest

from conftest import GOOD_TOKEN
from test_setup_doctor import SetupBot
from test_setup_page import api, make  # noqa: F401  (make is a fixture)
from ctb import clipboard, config as C, setup, setup_page
from ctb.i18n import SETUP_PAGE_STRINGS
from ctb.telegram import MAX_PASTE, TelegramError, find_token, holds_token

REAL_COMMANDS = clipboard._commands  # taken before the autouse fixture swaps it for "no clipboard here"
OTHER_TOKEN = "987654:" + "B" * 35

BOTFATHER_MESSAGE = (
    "Done! Congratulations on your new bot. You will find it at t.me/harnessgram_sample_bot. You can now add a "
    "description, about section and profile picture for your bot, see /help for a list of commands.\n\n"
    "Use this token to access the HTTP API:\n" + GOOD_TOKEN + "\n"
    "Keep your token secure and store it safely, it can be used by anyone to control your bot.")


# --- finding the token -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    GOOD_TOKEN, f"  {GOOD_TOKEN}\n", BOTFATHER_MESSAGE, BOTFATHER_MESSAGE.replace("\n", " "),
    f"{BOTFATHER_MESSAGE}\n\n{BOTFATHER_MESSAGE}",  # the same token twice is still one token
])
def test_the_token_is_found_alone_or_in_botfathers_whole_message(text):
    assert find_token(text) == GOOD_TOKEN


@pytest.mark.parametrize("text", [
    "", "hello", None, 12345, "123456:short",
    "https://api.telegram.org/bot" + GOOD_TOKEN + "/getMe",  # part of a URL, not a token on its own
    GOOD_TOKEN + "XYZ:1",  # glued to more token characters: not a clean word
])
def test_no_token_is_none(text):
    assert find_token(text) is None


def test_text_over_the_limit_is_refused_not_cut():
    """One token before the cut and another after it must not pass as one token."""
    text = GOOD_TOKEN + " " + "x" * MAX_PASTE + " " + OTHER_TOKEN
    with pytest.raises(TelegramError):
        find_token(text)
    with pytest.raises(TelegramError):
        find_token(GOOD_TOKEN + " " + "x" * MAX_PASTE)
    assert holds_token(text) and not holds_token("x" * 100)


def test_two_different_tokens_are_refused_without_echoing_them():
    with pytest.raises(TelegramError) as e:
        find_token(f"{GOOD_TOKEN}\n{OTHER_TOKEN}")
    assert GOOD_TOKEN not in str(e.value) and OTHER_TOKEN not in str(e.value)


# --- the hidden prompt and the clipboard -----------------------------------------------------------------------

class Clip:
    def __init__(self, text):
        self.text, self.cleared = text, 0

    def read(self):
        return self.text

    def clear(self):
        self.cleared += 1
        self.text = None
        return True


def ask(answers, clip):
    lines, it = [], iter(answers)
    token = setup.ask_token(lambda prompt: next(it), lines.append, clip.read, clip.clear)
    return token, "\n".join(lines)


def test_enter_reads_botfathers_message_from_the_clipboard_and_clears_it():
    clip = Clip(BOTFATHER_MESSAGE)
    token, out = ask([""], clip)
    assert token == GOOD_TOKEN and clip.cleared == 1
    assert "Cleared the clipboard" in out and GOOD_TOKEN not in out


def test_a_pasted_token_is_used_and_the_clipboard_is_left_alone():
    clip = Clip("something else")
    token, out = ask([GOOD_TOKEN], clip)
    assert token == GOOD_TOKEN and clip.cleared == 0 and GOOD_TOKEN not in out


def test_an_empty_clipboard_or_a_wrong_text_asks_again_then_stops():
    token, out = ask(["", "12345:nope", GOOD_TOKEN], Clip(None))
    assert token == GOOD_TOKEN and "clipboard is empty" in out and "invalid format" in out
    token, out = ask(["Done! Congratulations on your new bot.", GOOD_TOKEN], Clip(None))
    assert token == GOOD_TOKEN and "no bot token" in out  # only the first line of a pasted message arrived
    assert "12345:nope" not in out and "Congratulations" not in out
    with pytest.raises(setup.SetupError):
        ask(["", "", ""], Clip("no token in here"))


def test_two_tokens_in_the_clipboard_are_not_guessed_but_still_cleared():
    clip = Clip(f"{GOOD_TOKEN} {OTHER_TOKEN}")
    with pytest.raises(setup.SetupError) as e:
        ask(["", "", ""], clip)
    assert clip.cleared == 1  # refused, yet a token must not stay on the clipboard
    assert GOOD_TOKEN not in str(e.value) and OTHER_TOKEN not in str(e.value)


class StuckClip(Clip):
    def clear(self):
        self.cleared += 1
        return False


def test_a_clipboard_that_cannot_be_cleared_is_said_plainly():
    clip = StuckClip(BOTFATHER_MESSAGE)
    token, out = ask([""], clip)
    assert token == GOOD_TOKEN and clip.cleared == 1
    assert "Could not clear the clipboard: it still holds the token" in out and "Cleared" not in out
    _, out = ask(["", GOOD_TOKEN], StuckClip(f"{GOOD_TOKEN} {OTHER_TOKEN}"))
    assert "Could not clear the clipboard" in out


def test_a_typed_paste_is_cleared_from_the_clipboard_once_saved(tmp_path):
    clip = Clip(BOTFATHER_MESSAGE)  # copied in Telegram, then pasted at the prompt by hand
    lines = []
    code = setup.run(bot=SetupBot(), env={}, owner_id=4242, language="en", interactive=True, assume_yes=True,
                     install_hooks=False, test_message=False, run_doctor=False, say=lines.append,
                     getpass_fn=lambda prompt: GOOD_TOKEN, input_fn=lambda prompt: "",
                     read_clipboard=clip.read, clear_clipboard=clip.clear)
    out = "\n".join(lines)
    assert code == 0, out
    assert clip.cleared == 1 and "The clipboard still held the token. Cleared the clipboard" in out
    assert GOOD_TOKEN not in out


def test_a_token_from_the_environment_never_reads_the_clipboard(tmp_path):
    clip = Clip(BOTFATHER_MESSAGE)
    reads = []
    code = setup.run(bot=SetupBot(), env={"CTB_BOT_TOKEN": GOOD_TOKEN}, owner_id=4242, language="en",
                     interactive=False, install_hooks=False, test_message=False, run_doctor=False, say=print,
                     read_clipboard=lambda: reads.append(1) or clip.read(), clear_clipboard=clip.clear)
    assert code == 0 and not reads and clip.cleared == 0


def _through_a_terminal(data: bytes, read=None, **kw):
    """read_hidden on a real pseudo-terminal: the other end shows what the prompt writes (a terminal's output must
    drain) and pastes `data` once the prompt is up. Answers (text or the exception, the terminal's flags after)."""
    master, slave = os.openpty()
    name, done, began = os.ttyname(slave), threading.Event(), time.monotonic()

    def terminal():
        pasted = False
        while not done.is_set():
            if select.select([master], [], [], 0.05)[0]:
                os.read(master, 65536)
            if not pasted and time.monotonic() - began > 0.3:
                for i in range(0, len(data), 512):  # a terminal drops input past a full queue: pace it
                    os.write(master, data[i:i + 512])
                    while select.select([master], [], [], 0)[0]:
                        os.read(master, 65536)
                    time.sleep(0.002)
                pasted = True
    worker = threading.Thread(target=terminal)
    worker.start()
    signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(TimeoutError("the prompt never returned")))
    signal.alarm(10)
    try:
        try:
            out = (read or setup.read_hidden)("token: ", tty=name, quiet=0.5, **kw)
        except BaseException as e:  # noqa: B902 - the caller checks which
            out = e
        lflag = termios.tcgetattr(slave)[3]
    finally:
        signal.alarm(0)
        done.set()
        worker.join()
        os.close(slave)
        os.close(master)
    return out, lflag


def test_the_default_prompt_keeps_a_multi_line_paste_together():
    assert setup.run.__kwdefaults__["getpass_fn"] is setup.read_hidden
    text, lflag = _through_a_terminal(BOTFATHER_MESSAGE.encode() + b"\n")
    assert find_token(text) == GOOD_TOKEN  # getpass kept only the first line and threw the token away
    assert lflag & termios.ECHO and lflag & termios.ICANON  # the terminal is back as it was


def test_a_line_longer_than_the_terminal_line_limit_is_read_whole():
    """In line mode a macOS terminal cuts a line past 1024 bytes before the program sees it."""
    text, _ = _through_a_terminal(b"x" * 3000 + b" " + GOOD_TOKEN.encode() + b"\n")
    assert find_token(text) == GOOD_TOKEN


def test_backspace_and_ctrl_u_edit_what_was_typed():
    text, _ = _through_a_terminal(b"junk\x15" + GOOD_TOKEN.encode() + b"Z\x7f\n")
    assert text.strip() == GOOD_TOKEN


def test_a_paste_over_the_limit_is_refused_not_cut():
    out, lflag = _through_a_terminal(GOOD_TOKEN.encode() + b" " + b"x" * (setup.PASTE_LIMIT + 10) + b"\n")
    assert isinstance(out, TelegramError) and "too long" in out.description
    assert lflag & termios.ECHO
    _, said = ask_raising(TelegramError("the paste is too long to hold just a bot token; copy only the token"))
    assert "too long" in said


def ask_raising(error):
    calls, lines = [], []

    def prompt(text):
        calls.append(text)
        if len(calls) == 1:
            raise error
        return GOOD_TOKEN
    return setup.ask_token(prompt, lines.append, Clip(None).read, Clip(None).clear), "\n".join(lines)


def test_echo_comes_back_when_interrupted_right_after_it_went_off(monkeypatch):
    real, calls = termios.tcsetattr, []

    def interrupted(fd, when, attrs):
        real(fd, when, attrs)
        calls.append(attrs)
        if len(calls) == 1:
            raise KeyboardInterrupt  # Ctrl-C the moment echo went off
    monkeypatch.setattr(termios, "tcsetattr", interrupted)
    out, lflag = _through_a_terminal(b"")
    assert isinstance(out, KeyboardInterrupt) and len(calls) == 2
    assert lflag & termios.ECHO and lflag & termios.ICANON


def test_the_wizard_takes_the_token_from_the_clipboard_and_never_prints_it(tmp_path):
    clip = Clip(BOTFATHER_MESSAGE)
    lines = []
    code = setup.run(bot=SetupBot(), env={}, owner_id=4242, language="en", interactive=True, assume_yes=True,
                     install_hooks=False, test_message=False, run_doctor=False, say=lines.append,
                     getpass_fn=lambda prompt: "", input_fn=lambda prompt: "",
                     read_clipboard=clip.read, clear_clipboard=clip.clear)
    out = "\n".join(lines)
    assert code == 0, out
    assert json.loads(C.config_path().read_text())["bot_token"] == GOOD_TOKEN
    assert clip.cleared == 1 and GOOD_TOKEN not in out


def test_tests_never_touch_the_real_clipboard():
    assert clipboard.read() is None and clipboard.clear() is False


# --- the clipboard helpers, run against stand-in programs ------------------------------------------------------

def _program(folder, name, body):
    path = folder / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def tools(tmp_path, monkeypatch):
    folder = tmp_path / "bin"
    folder.mkdir()
    monkeypatch.setenv("PATH", str(folder))
    monkeypatch.setattr(clipboard, "_commands", REAL_COMMANDS)
    monkeypatch.setattr(clipboard, "_last_reader", None)
    return folder


def _linux_tools(folder, tmp_path, read_ok=("wl-paste", "xclip")):
    """Stand-ins for both Linux clipboards; each clear tool leaves a marker file named after itself."""
    _program(folder, "wl-paste", f"printf '%s' '{GOOD_TOKEN}'" if "wl-paste" in read_ok else "exit 1")
    _program(folder, "wl-copy", f"/usr/bin/touch '{tmp_path / 'cleared-wl-copy'}'")
    _program(folder, "xclip", 'case "$*" in *-o*) ' + (f"printf '%s' '{GOOD_TOKEN}'" if "xclip" in read_ok
             else "exit 1") + f";; *) /usr/bin/touch '{tmp_path / 'cleared-xclip'}';; esac")


def test_linux_clears_the_clipboard_that_was_read(tools, tmp_path):
    _linux_tools(tools, tmp_path)
    assert clipboard.read("linux") == GOOD_TOKEN and clipboard.clear("linux") is True
    assert (tmp_path / "cleared-wl-copy").exists() and not (tmp_path / "cleared-xclip").exists()


def test_linux_x11_read_is_cleared_with_the_x11_tool(tools, tmp_path):
    _linux_tools(tools, tmp_path, read_ok=("xclip",))
    assert clipboard.read("linux") == GOOD_TOKEN and clipboard.clear("linux") is True
    assert (tmp_path / "cleared-xclip").exists() and not (tmp_path / "cleared-wl-copy").exists()


def test_linux_unsure_clears_every_clipboard(tools, tmp_path):
    _linux_tools(tools, tmp_path)
    assert clipboard.clear("linux") is True  # nothing was read: both clipboards are emptied
    assert (tmp_path / "cleared-xclip").exists() and (tmp_path / "cleared-wl-copy").exists()


def test_linux_read_without_its_clear_tool_is_no_success(tools, tmp_path):
    _program(tools, "wl-paste", f"printf '%s' '{GOOD_TOKEN}'")
    _program(tools, "xsel", "exit 0")  # an X11 tool cannot empty the Wayland clipboard that was read
    assert clipboard.read("linux") == GOOD_TOKEN and clipboard.clear("linux") is False


def test_macos_reads_with_pbpaste_and_clears_with_pbcopy(tools, tmp_path):
    _program(tools, "pbpaste", f"printf '%s' '{GOOD_TOKEN}'")
    _program(tools, "pbcopy", f"/bin/cat > '{tmp_path / 'copied'}'")
    assert clipboard.read("darwin") == GOOD_TOKEN
    assert clipboard.clear("darwin") is True and (tmp_path / "copied").read_text() == ""


def test_linux_falls_back_to_the_next_tool_and_nothing_found_is_none(tools):
    assert clipboard.read("linux") is None and clipboard.clear("linux") is False  # no tool at all
    _program(tools, "wl-paste", "exit 1")
    _program(tools, "xclip", f"printf '%s' '{GOOD_TOKEN}'")
    assert clipboard.read("linux") == GOOD_TOKEN


def test_a_blank_clipboard_is_none_and_an_oversized_one_comes_back_whole(tools, tmp_path):
    _program(tools, "pbpaste", "printf '   '")
    assert clipboard.read("darwin") is None
    big = tmp_path / "big.txt"
    big.write_text("x" * (MAX_PASTE + 10) + " " + GOOD_TOKEN)
    _program(tools, "pbpaste", f"/bin/cat '{big}'")
    text = clipboard.read("darwin")
    assert text.endswith(GOOD_TOKEN)  # whole, so the token in it is seen and cleared (find_token still refuses it)


def test_an_oversized_clipboard_with_a_token_is_refused_but_cleared():
    clip = Clip("x" * (MAX_PASTE + 10) + " " + GOOD_TOKEN)
    with pytest.raises(setup.SetupError):
        ask(["", "", ""], clip)
    assert clip.cleared == 1


def test_the_clipboard_is_cleared_however_setup_ends(tmp_path):
    """A token pasted at the prompt must not stay on the clipboard when setup stops early."""
    bot = SetupBot()
    bot.fail["getMe"] = "network error"
    clip, lines = Clip(BOTFATHER_MESSAGE), []
    code = setup.run(bot=bot, env={}, owner_id=4242, interactive=True, install_hooks=False, test_message=False,
                     run_doctor=False, say=lines.append, getpass_fn=lambda p: GOOD_TOKEN,
                     read_clipboard=clip.read, clear_clipboard=clip.clear)
    assert code == 1 and clip.cleared == 1 and "Could not reach Telegram" in "\n".join(lines)
    stuck, lines = StuckClip(GOOD_TOKEN), []  # pairing that never arrives, and a clipboard that will not clear
    code = setup.run(bot=SetupBot(), env={}, interactive=True, install_hooks=False, test_message=False,
                     run_doctor=False, say=lines.append, getpass_fn=lambda p: GOOD_TOKEN, pair_timeout=0.05,
                     read_clipboard=stuck.read, clear_clipboard=stuck.clear)
    out = "\n".join(lines)
    assert code == 1 and "no matching /start" in out and "Could not clear the clipboard" in out
    assert not C.config_path().exists() and GOOD_TOKEN not in out


def test_a_clipboard_that_changed_since_the_check_is_left_alone():
    reads = iter([GOOD_TOKEN, "something new"])
    cleared = []
    assert setup.clear_if_holds(GOOD_TOKEN, lambda: next(reads), lambda: cleared.append(1) or True) is None
    assert not cleared


# --- BotFather gets a ready name and username ------------------------------------------------------------------

def test_the_suggested_username_is_one_botfather_accepts():
    names = {setup.suggested_username() for _ in range(20)}
    assert len(names) > 1  # random, so a taken one is unlikely
    for name in names:
        assert re.fullmatch(r"[a-z][a-z0-9_]{4,31}", name) and name.endswith("_bot")
    steps = setup.botfather_steps("harnessgram_sample_bot")
    assert "harnessgram_sample_bot" in steps and "Harnessgram" in steps and "clipboard" in steps


# --- the setup page --------------------------------------------------------------------------------------------

def test_the_page_accepts_botfathers_whole_message(make):
    srv = make()
    status, body = api(srv, "token", {"token": BOTFATHER_MESSAGE})
    assert status == 200 and body["state"]["bot"] is not None, body
    assert srv.flow._token == GOOD_TOKEN and GOOD_TOKEN not in json.dumps(body)
    status, body = api(srv, "token", {"token": f"{GOOD_TOKEN} {OTHER_TOKEN}"})
    assert status == 400 and body["error"] == "token_format"


def test_the_page_keeps_only_the_token_on_paste_and_suggests_a_username():
    page = setup_page.PAGE
    assert 'tokenInput.addEventListener("paste"' in page and "function findTokens(text)" in page
    assert "username:suggestUsername()" in page and 'codeLine("username")' in page
    assert "getRandomValues" in page
    for lang in ("en", "he"):
        assert SETUP_PAGE_STRINGS[lang]["s1_b2"]
    assert set(SETUP_PAGE_STRINGS["en"]) == set(SETUP_PAGE_STRINGS["he"])


def test_hebrew_setup_lines_isolate_their_english_words():
    for key in ("s1_b", "s1_b2", "s1_c", "s2_hint"):
        text = SETUP_PAGE_STRINGS["he"][key]
        bare = re.sub("⁨[^⁩]*⁩", "", text)
        assert not re.search("[A-Za-z]", bare), key


def test_the_wizard_runs_no_clipboard_tool_by_itself(monkeypatch):
    """A saved or given token never opens the clipboard: it is read only after Enter at the prompt."""
    called = []
    monkeypatch.setattr(clipboard, "read", lambda *a: called.append("read"))
    raw = {}
    assert setup._token(raw, {"CTB_BOT_TOKEN": GOOD_TOKEN}, True, lambda p: "") == GOOD_TOKEN
    assert not called and os.environ.get("CTB_BOT_TOKEN") != GOOD_TOKEN


def _finished(srv, token=GOOD_TOKEN):
    with srv.flow.lock:
        srv.flow._token, srv.flow.result = token, {"items": [], "doctor": [], "threaded": True, "code": 0}


def test_the_page_offers_a_one_tap_clear_after_the_finish(make):
    clip = Clip(BOTFATHER_MESSAGE)
    srv = make(read_clipboard=clip.read, clear_clipboard=clip.clear)
    status, body = api(srv, "clipboard")
    assert status == 409 and body["error"] == "order" and clip.cleared == 0  # not before the finish
    _finished(srv)
    status, body = api(srv, "clipboard")
    assert status == 200 and body["state"]["clip"] == "cleared" and clip.cleared == 1
    assert GOOD_TOKEN not in json.dumps(body)


def test_the_page_leaves_other_clipboard_content_alone_and_says_when_clearing_failed(make):
    clip = Clip("a shopping list")
    srv = make(read_clipboard=clip.read, clear_clipboard=clip.clear)
    _finished(srv)
    assert api(srv, "clipboard")[1]["state"]["clip"] == "clean" and clip.cleared == 0
    stuck = StuckClip(GOOD_TOKEN)
    srv = make(read_clipboard=stuck.read, clear_clipboard=stuck.clear)
    _finished(srv)
    assert api(srv, "clipboard")[1]["state"]["clip"] == "failed"


def test_the_finish_banner_has_the_clear_button_and_hint():
    page = setup_page.PAGE
    assert 'act(5,"clipboard",{})' in page and 't("clip_hint")' in page
    for lang in ("en", "he"):
        for key in ("clip_hint", "clip_go", "clip_working", "clip_cleared", "clip_clean", "clip_failed"):
            assert SETUP_PAGE_STRINGS[lang][key]


def test_the_watcher_keeps_the_page_up_for_the_clear_button_then_stops(make):
    clip = Clip(GOOD_TOKEN)
    srv = make(read_clipboard=clip.read, clear_clipboard=clip.clear)
    now = [1000.0]
    srv.clock = lambda: now[0]
    _finished(srv)
    watcher = threading.Thread(target=setup_page.watch, args=(srv, 0.01))
    watcher.start()
    try:
        time.sleep(0.1)
        now[0] += setup_page.DONE_GRACE + 30  # well past the old grace: the button must still work
        time.sleep(0.1)
        assert watcher.is_alive()
        assert api(srv, "clipboard")[1]["state"]["clip"] == "cleared" and clip.cleared == 1
        time.sleep(0.1)
        now[0] += setup_page.DONE_GRACE
        watcher.join(timeout=5)
        assert not watcher.is_alive() and srv.reason == "done"
    finally:
        srv.flow.close()
        if watcher.is_alive():
            now[0] += setup_page.CLIP_GRACE
            watcher.join(timeout=5)


def test_an_untapped_clear_button_stops_after_its_grace(make):
    srv = make()
    now = [1000.0]
    srv.clock = lambda: now[0]
    _finished(srv)
    assert srv.should_stop() is None
    now[0] += setup_page.CLIP_GRACE - 1
    assert srv.should_stop() is None
    now[0] += 1
    assert srv.should_stop() == "done"


def test_a_closed_page_says_so_instead_of_a_dead_button():
    page = setup_page.PAGE
    assert 'gone?msg("warn",t("clip_gone"))' in page
    assert SETUP_PAGE_STRINGS["en"]["clip_gone"] and SETUP_PAGE_STRINGS["he"]["clip_gone"]
