"""`harnessgram setup --web`: the same first run as the terminal wizard, as a guided page in the browser.

A small HTTP server on 127.0.0.1 (a random free port) serves one self-contained page with four steps: create
the bot, paste the token, pair the Telegram account, finish. Every step calls the step functions of setup.py,
so the page validates, stores and installs exactly like `harnessgram setup` does.

Security:
- every request needs the one-time secret generated at start (the first path segment), compared in constant
  time; anything else gets 404. The Host header must be 127.0.0.1:<port> or localhost:<port> (DNS rebinding).
- actions are POST with a JSON body (Content-Type application/json, which a cross-site form cannot send) of at
  most MAX_BODY bytes; no CORS headers, so no other site can read an answer.
- the page has a strict CSP with a per-response nonce, no external resources, no-store and no-referrer.
- the bot token arrives in a POST body, lives in memory, is saved by the same functions as the wizard, and is
  never sent back, logged or put in a URL or a process argument. The page shows fixed sentences only.
- the server stops by itself a few seconds after the setup finished and the clear-clipboard button was answered
  (or CLIP_GRACE after the finish when nobody taps it), or after IDLE_SECONDS without a request.
"""
from __future__ import annotations

import copy
import hmac
import http.server
import json
import os
import secrets
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable, Optional

from . import config as C
from . import lang as L
from . import pairing
from . import setup
from .setup import SetupError
from . import clipboard
from .telegram import Bot, TelegramError, clean_token, find_token

MAX_BODY = 4096
MAX_CONNECTIONS = 8  # connections served at once; more are closed at once
READ_TIMEOUT = 5.0  # seconds a connection may take for each read, the request line and headers included
IDLE_SECONDS = 30 * 60
DONE_GRACE = 3.0  # seconds the server keeps answering after a finished setup, so the last answer arrives
CLIP_GRACE = 5 * 60.0  # seconds after the finish (or the last failed clear) the clear button keeps working
PAIR_SECONDS = 10 * 60
# One table per page language, the same keys in each. A command or a code sample is never inside a sentence:
# the page shows it on its own line (CODES in the script).
from .i18n import SETUP_PAGE_STRINGS as STRINGS

# Error codes the server answers with, and nothing else (never a message from an exception).
ERRORS = ("token_format", "token_rejected", "network", "save_failed", "install_failed", "order", "bad_request",
          "stale", "conflict", "internal")


def _no_prompt(prompt: str) -> str:
    raise SetupError("no prompt on the setup page")


def install_items(report) -> list:
    """The page's lines for the install step, from what install checked (keys only; the page has the words).
    A line with "warn" was not checked or did not work, and names the command that shows why."""
    if report is None:
        return [{"key": "r_install_skipped", "warn": True, "cmd": "install"}]
    items = [{"key": "r_hooks_ok"} if report.hooks else {"key": "r_hooks_failed", "warn": True, "cmd": "install"}]
    if not report.service:
        items.append({"key": "r_service_skipped", "warn": True, "cmd": "install"})
    elif not report.platform:
        items.append({"key": "r_service_unsupported", "warn": True, "cmd": "serve"})
    elif not report.start:
        items.append({"key": "r_service_not_started", "warn": True, "cmd": "install"})
    else:  # the listener's own state, then the scheduled jobs that did not start, each on its own line
        if "listener" in report.running and "listener" not in report.failed:
            items.append({"key": "r_service_running"})
        else:
            items.append({"key": "r_service_failed", "warn": True, "cmd": "install"})
        jobs = [name for name in report.failed if name != "listener"]
        if jobs:
            items.append({"key": "r_jobs_failed", "warn": True, "cmd": "install", "vars": {"jobs": ", ".join(jobs)}})
    return items


def listener_items(report, listener) -> list:
    """Whether the listener reaches Telegram: shown when this finish started it; "not confirmed" unless the
    health check saw the new listener poll."""
    confirmed = setup.listener_confirmed(report, listener)
    if confirmed is None:
        return []
    return [{"key": "r_listener_ok"}] if confirmed else [{"key": "r_listener_unchecked", "warn": True, "cmd": "doctor"}]


class SetupFlow:
    """The page's state machine. Thread-safe; the token lives only in memory until finish() saves it."""

    def __init__(self, *, language: Optional[str] = None, owner_id: Optional[int] = None, install_hooks: bool = True,
                 service: bool = True, start: bool = True, test_message: bool = True, run_doctor: bool = True,
                 settings_file: Optional[Path] = None, env: Optional[dict] = None,
                 bot_factory: Optional[Callable[[str], object]] = None, pair_timeout: float = PAIR_SECONDS,
                 say: Callable[[str], None] = print, read_clipboard: Optional[Callable[[], Optional[str]]] = None,
                 clear_clipboard: Optional[Callable[[], bool]] = None):
        self.lock = threading.RLock()
        self.path = C.config_path()
        self.raw = setup._existing_raw(self.path)
        self.before = copy.deepcopy(self.raw)
        self.env = os.environ if env is None else env
        self.language, self.say = language, say
        self.opts = dict(install_hooks=install_hooks, service=service, start=start, test_message=test_message,
                         run_doctor=run_doctor, settings_file=settings_file)
        self.bot_factory = bot_factory or Bot
        self.pair_timeout = pair_timeout
        self._token: Optional[str] = None
        self.new_token = False
        self.bot, self.me = None, None
        self.chat_id = int(owner_id or self.raw.get("owner_chat_id") or 0)
        self.kept = bool(self.chat_id)  # paired before (or --owner-id): the page keeps it, like the wizard
        self.account: Optional[dict] = None
        self.pair_state, self.pair_code, self.found, self.pair_gen = "idle", None, None, 0
        self.bot_gen = 0  # every change to another bot counts up; a result made for an older bot is dropped
        self.stop = threading.Event()
        self.busy = False
        self.result: Optional[dict] = None
        self.read_clipboard = read_clipboard or clipboard.read
        self.clear_clipboard_fn = clear_clipboard or clipboard.clear
        self.clip: Optional[str] = None  # after the page's clear button: "cleared", "failed" or "clean"
        self.clip_taps = 0  # how often the clear button was answered (the server's watcher keys its grace on it)

    # --- one gate for every network result ------------------------------------------------------------------
    #
    # A network call (getMe, getUpdates, the install and doctor) is always made outside the lock, and its result
    # enters the flow state only through _commit, with the ticket taken before the call. The result is dropped
    # when the bot changed meanwhile (bot_gen counts every bot change) or when the flow settled (the finish is
    # running or done); only the running finish's own results pass a settled flow. Earlier review rounds each
    # found one more stale result written straight into the state; this keeps it to one place.

    def _ticket(self, finish: bool = False) -> tuple:
        with self.lock:
            return (self.bot_gen, finish)

    def _commit(self, ticket: tuple, write: Callable[[], object]) -> bool:
        """Runs `write` under the lock when the result is still current; False when it was dropped (or when
        `write` itself returned False after its own check)."""
        gen, finish = ticket
        with self.lock:
            if gen != self.bot_gen:
                return False
            if finish:
                if not self.busy or self.result is not None:
                    return False
            elif self._settled():
                return False
            return write() is not False

    def _settled(self) -> bool:
        """The finish is running or done: the bot and the paired account can no longer change (call under lock).
        A change applied meanwhile would leave the saved config on one bot and the page on another."""
        return self.busy or self.result is not None

    def _dropped(self) -> str:
        """The answer for a request whose result came too late: "order" once the flow settled, else "stale"."""
        with self.lock:
            return "order" if self._settled() else "stale"

    def _set_bot(self, token: str, bot, me: dict, new_token: bool) -> None:
        """A token accepted (under the lock, from _commit). Another bot is a bot change: a new bot_gen makes every
        result still on its way for the previous bot stale, and its pairing starts over (a kept owner stays,
        like the wizard). The same bot again (its token pasted once more) is no change: results about it stay
        valid and a waiting pairing keeps waiting (a second poll of the same bot would clash with it)."""
        other_bot = not self.me or self.me.get("id") != me.get("id")
        self._token, self.bot, self.me = token, bot, me
        self.new_token = self.new_token or new_token
        if other_bot:
            self.bot_gen += 1
            if not self.kept:
                self._reset_pairing()

    def _reset_pairing(self) -> None:
        self.pair_gen += 1
        self.pair_state, self.pair_code, self.found, self.account = "idle", None, None, None
        if not self.kept:
            self.chat_id = 0

    # --- steps ---------------------------------------------------------------------------------------------

    def load_saved(self) -> None:
        """A token already saved (or in CTB_BOT_TOKEN) that Telegram accepts: the token step starts done."""
        ticket = self._ticket()
        try:
            token = setup._token(self.raw, self.env, False, _no_prompt)
            bot = self.bot_factory(token)
            me = setup.check_bot(bot)
        except (Exception, SystemExit):  # no token, a rejected one, a broken token command: the page asks
            return
        self._commit(ticket, lambda: self._set_bot(token, bot, me, new_token=False))

    def submit_token(self, text) -> Optional[str]:
        with self.lock:
            if self._settled():
                return "order"
            ticket = (self.bot_gen, False)
        if not isinstance(text, str) or not text.strip():
            return "token_format"
        try:  # just the token, or BotFather's whole message pasted as it is
            token = find_token(text) or clean_token(text)
        except TelegramError:
            return "token_format"
        try:
            bot = self.bot_factory(token)
            me = setup.check_bot(bot)
        except SetupError as e:
            return "network" if e.kind == "network" else "token_rejected"
        except (Exception, SystemExit):
            return "token_rejected"
        if not self._commit(ticket, lambda: self._set_bot(token, bot, me, new_token=True)):
            return self._dropped()  # the finish started, or another token won, while Telegram checked this one
        return None

    def clear_clipboard(self) -> Optional[str]:
        """The finish banner's one-tap clear: the browser cannot read the clipboard without asking for permission,
        but this local server can. A clipboard that still holds the token is emptied; any other content is left
        alone. Only after the finish, and only on that tap."""
        with self.lock:
            if self.result is None or not self._token:
                return "order"
            token = self._token
        cleared = setup.clear_if_holds(token, self.read_clipboard, self.clear_clipboard_fn)
        with self.lock:
            self.clip = "clean" if cleared is None else "cleared" if cleared else "failed"
            self.clip_taps += 1
        return None

    def recheck(self) -> Optional[str]:
        with self.lock:
            if self._settled():
                return "order"
            if self.bot is None:
                return "order"
            bot, ticket = self.bot, (self.bot_gen, False)
        try:
            me = setup.check_bot(bot)
        except SetupError as e:
            return "network" if e.kind == "network" else "token_rejected"

        def write():
            self.me = me
        if not self._commit(ticket, write):
            return self._dropped()  # the bot changed or the finish started while this check ran
        return None

    def start_pairing(self) -> Optional[str]:
        with self.lock:
            if self._settled():
                return "order"
            if self.bot is None or not (self.me or {}).get("username"):
                return "order"
            if self.pair_state in ("waiting", "found") or self.account or self.kept:
                return None
            self.pair_gen += 1
            gen, bot, code, ticket = self.pair_gen, self.bot, pairing.new_code(), (self.bot_gen, False)
            self.pair_state, self.pair_code, self.found = "waiting", code, None
        started = time.time()
        threading.Thread(target=self._wait, args=(bot, code, started, gen, ticket), daemon=True,
                         name="setup-page-pairing").start()
        return None

    def _wait(self, bot, code: str, started: float, gen: int, ticket: tuple) -> None:
        def now() -> float:  # a stopped, replaced or outdated wait ends at its next look at the clock
            stale = self.stop.is_set() or gen != self.pair_gen or ticket[0] != self.bot_gen
            return float("inf") if stale else time.time()
        try:
            found = pairing.wait_for_start(bot, code, started, timeout=self.pair_timeout, now=now)
            state = "found" if found else "expired"
        except (Exception, SystemExit):
            found, state = None, "failed"

        def write():
            if gen != self.pair_gen or self.pair_state != "waiting":
                return False  # this wait was replaced (a new pairing, a declined account)
            self.found, self.pair_state = found, state
        self._commit(ticket, write)

    def confirm(self, body: dict) -> Optional[str]:
        """The account the browser showed, approved or declined. The request carries what the browser saw (bot
        and pairing generations, the account id); any difference from now is "stale", so a late or other-tab
        approval can never approve a different account."""
        yes = body.get("yes")
        seen = [body.get(k) for k in ("bot_gen", "pair_gen", "account_id")]
        if not isinstance(yes, bool) or not all(isinstance(v, int) and not isinstance(v, bool) for v in seen):
            return "bad_request"
        bot_gen, pair_gen, account_id = seen
        why = {}

        def write():
            if self.pair_state != "found" or not self.found:
                why["error"] = "order"
                return False
            if pair_gen != self.pair_gen or setup.account_of(self.found)["id"] != account_id:
                why["error"] = "stale"
                return False
            if yes:
                self.account = setup.account_of(self.found)
                self.chat_id = self.account["chat_id"]
                self.pair_state = "done"
            else:
                self._reset_pairing()
        if not self._commit((bot_gen, False), write):  # the bot the browser showed, not the current one
            return why.get("error") or self._dropped()
        return None

    def finish(self, page_lang, prefs=None) -> Optional[str]:
        if prefs is None:
            prefs = {}
        try:
            prefs = setup.check_preferences(prefs)  # the same allowed values the bridge uses; nothing else is saved
        except SetupError:
            return "bad_request"
        with self.lock:
            if self.result is not None:
                return None
            if self.busy:
                return "order"
            if self.bot is None or not self.chat_id:
                return "order"
            self.busy = True  # settles the flow: no bot or pairing change until this finish fails or is done
            ticket = (self.bot_gen, True)
            snap = dict(raw=self.raw, before=self.before, token=self._token, bot=self.bot, chat_id=self.chat_id,
                        me=self.me, new_token=self.new_token, paired_here=self.account is not None)
        try:
            return self._finish(ticket, snap, page_lang, prefs)
        finally:
            with self.lock:
                self.busy = False

    def _finish(self, ticket: tuple, snap: dict, page_lang, prefs: dict) -> Optional[str]:
        o, say = self.opts, self.say
        raw, token, bot, chat_id = snap["raw"], snap["token"], snap["bot"], snap["chat_id"]
        language = prefs.get("target_language") or self.language
        if not language and "target_language" not in raw and page_lang in ("he", "en"):
            language = page_lang
        work = copy.deepcopy(raw)
        if not self.path.exists():
            setup.new_install_defaults(work)
        if snap["new_token"]:
            work.pop("bot_token_command", None)
        try:
            setup.apply_settings(work, chat_id, token, language, prefs)
            # the person at the page confirmed the account; a kept owner is the same owner
            replace = snap["paired_here"] or not setup._existing_raw(self.path).get("owner_chat_id")
            saved = setup.save_settings(snap["before"], work, self.path, replace, guard_identity=True)
        except SetupError as e:
            if e.kind == "conflict":
                say("  The settings were changed elsewhere while the setup page was open; nothing was saved.")
                return "conflict"
            say("  The setup page could not save the settings. Run `harnessgram setup` in a terminal to see why.")
            return "save_failed"
        if not self._still_ours(work):
            say("  The settings were changed elsewhere right after saving; stopped before installing.")
            return "conflict"

        def keep_saved():
            self.raw, self.before = work, copy.deepcopy(work)
        self._commit(ticket, keep_saved)
        say(f"  Saved {saved} (mode 600).")
        if "activity" in prefs:
            C.set_activity_level(prefs["activity"])  # the same switch as the bot's settings menu
        items = [{"key": "r_saved"}]
        try:
            report = setup.install_step(o["install_hooks"], o["service"], o["start"], o["settings_file"], say)
        except (Exception, SystemExit):  # install raises SystemExit for a broken Claude settings file
            say("  Installing failed. Run `harnessgram install` in a terminal to see why.")
            return "install_failed"
        items += install_items(report)
        if o["test_message"]:
            failed = setup.send_connected(bot, chat_id, work.get("ui_language", "en"))
            items.append({"key": "r_test_sent"} if failed is None
                         else {"key": "r_test_failed", "warn": True, "cmd": "doctor"})
        me = snap["me"] or {}
        try:
            me = setup.check_bot(bot)  # topics may have been switched on since the token step
        except SetupError:
            pass

        def keep_me():
            self.me = me
        self._commit(ticket, keep_me)
        topics_on = bool(me.get("has_topics_enabled"))  # this finish's own bot, never a value written meanwhile
        lines, code, listener = [], 0, None
        if o["run_doctor"]:
            try:
                listener, results = setup.health(report, bot, say)  # one listener check for the list and the code
            except (Exception, SystemExit):
                results = [("FAIL", "the health check could not run; run `harnessgram doctor` in a terminal")]
            for level, msg in results:
                text = str(msg).replace(token, "[token]") if token else str(msg)
                lines.append({"level": str(level), "text": text})
            code = 1 if any(level == "FAIL" for level, _ in results) else 0
        items += listener_items(report, listener)
        try:
            from . import buildstamp
            stale = buildstamp.stale()
        except Exception:
            stale = []
        old = [n for n in stale if n != "radio"]  # what install restarts
        if old:
            items.append({"key": "r_stale_code", "warn": True, "cmd": "install", "vars": {"names": ", ".join(old)}})
        if "radio" in stale:  # install leaves the radio alone: it moves over when it is turned off and on
            items.append({"key": "r_stale_radio", "warn": True})
        if report is not None:
            code = code or report.code  # a service that did not start, or hooks not confirmed, is no success
        if setup.listener_confirmed(report, listener) is False:
            code = code or 1  # started but not seen polling (a warn after the wait, or no health check)
        if not topics_on:
            code = code or 1
        result = {"items": items, "doctor": lines, "threaded": topics_on, "code": code}
        if not self._still_ours(work):
            say("  The settings were changed elsewhere while installing; not reporting success.")
            return "conflict"

        def keep_result():
            self.result = result
        if not self._commit(ticket, keep_result):
            return "stale"
        say("  The setup page finished." + (" All set." if code == 0 else " Some checks need attention."))
        return None

    def _still_ours(self, work: dict) -> bool:
        """The saved file still names the bot and owner this flow saved (another setup may write in between)."""
        return setup.same_identity(setup._existing_raw(self.path), work)

    def _saved_prefs(self) -> dict:
        """Choices an earlier setup already saved (or --language gave); the page preselects them and fills the
        rest from the page language."""
        out = {k: self.raw[k] for k in ("target_language",) + setup.CONFIG_PREFERENCES if k in self.raw}
        if "target_language" not in out and self.language in L.LANG_NAMES:
            out["target_language"] = self.language
        if out.get("target_language") not in L.LANG_NAMES:
            out.pop("target_language", None)
        out["activity"] = C.activity_level()
        return out

    def close(self) -> None:
        self.stop.set()

    # --- what the page sees (never the token) --------------------------------------------------------------

    def state(self) -> dict:
        with self.lock:
            me = self.me or {}
            pair = {"state": self.pair_state}
            if self.pair_state == "waiting" and self.pair_code and me.get("username"):
                pair.update(code=self.pair_code, link=setup.pairing_link(me["username"], self.pair_code))
            if self.pair_state == "found" and self.found:
                who = setup.account_of(self.found)
                pair["account"] = {"name": who["name"], "username": who["username"], "id": who["id"]}
            return {
                "bot": {"username": me.get("username"), "name": me.get("first_name")} if self.bot and me else None,
                "threaded": bool(me.get("has_topics_enabled")),
                "pair": pair,
                "paired": {"name": self.account["name"], "username": self.account["username"]} if self.account else None,
                "kept_id": self.chat_id if self.kept and self.chat_id else None,
                "busy": self.busy,
                "finished": copy.deepcopy(self.result),
                "clip": self.clip,
                "prefs": self._saved_prefs(),
                "gens": {"bot": self.bot_gen, "pair": self.pair_gen},
                "languages": [[code, name] for code, name in L.LANG_NAMES.items()],
            }


# --- the server ------------------------------------------------------------------------------------------------

def csp(nonce: str) -> str:
    return ("default-src 'none'; script-src 'nonce-%s'; style-src 'nonce-%s'; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'" % (nonce, nonce))


class SetupServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, flow: SetupFlow, page_lang: str = "", idle_timeout: float = IDLE_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        super().__init__(("127.0.0.1", 0), SetupHandler)
        self.flow, self.page_lang = flow, page_lang if page_lang in ("he", "en") else ""
        self.secret = secrets.token_urlsafe(24)
        self.port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.idle_timeout, self.clock = idle_timeout, clock
        self.last_seen = clock()
        self.done_at: Optional[float] = None
        self.clip_seen = (0, None)  # (flow.clip_taps, when the watcher first saw that count)
        self.reason: Optional[str] = None
        self.on_page: Optional[Callable[[], None]] = None
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        """One thread per connection, at most MAX_CONNECTIONS at once: a local process holding half-sent
        requests open can never use up the threads (each also ends after READ_TIMEOUT without data)."""
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/{self.secret}/"

    def handle_error(self, request, client_address):
        pass  # never a traceback on the terminal (it could carry request details)

    def touch(self) -> None:
        self.last_seen = self.clock()

    def should_stop(self) -> Optional[str]:
        now = self.clock()
        if self.flow.result is not None:
            if self.done_at is None:
                self.done_at = now
            taps = self.flow.clip_taps
            if taps != self.clip_seen[0]:
                self.clip_seen = (taps, now)
            if self.flow.clip in ("cleared", "clean"):  # the clear button is gone: only the last answer is left
                if now - self.clip_seen[1] >= DONE_GRACE:
                    return "done"
            elif now - (self.clip_seen[1] or self.done_at) >= CLIP_GRACE:
                return "done"  # nobody tapped (or the clear failed and nobody retried) for a few minutes
            return None
        if self.flow.busy:  # the last step runs (it may wait for the listener): that is activity, not idleness
            self.last_seen = now
            return None
        if now - self.last_seen >= self.idle_timeout:
            return "idle"
        return None


def watch(server: SetupServer, interval: float = 0.25) -> None:
    """Stops the server after a finished setup (and a short grace) or when nobody used it for a while."""
    while True:
        reason = server.should_stop()
        if reason:
            server.reason = reason
            server.flow.close()
            server.shutdown()
            return
        time.sleep(interval)


class SetupHandler(http.server.BaseHTTPRequestHandler):
    server_version = "harnessgram-setup"
    sys_version = ""
    timeout = READ_TIMEOUT  # set on the socket before the request line is read

    def log_message(self, format, *args):
        pass  # the path holds the secret: nothing is logged

    def _send(self, status: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
              headers: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _gate(self) -> Optional[str]:
        """The path after the secret, or None after an answer was sent (wrong Host: 403, wrong secret: 404)."""
        if self.headers.get("Host", "") not in self.server.hosts:
            self._send(403)
            return None
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        first, _, rest = path.lstrip("/").partition("/")
        if not path.startswith("/") or not hmac.compare_digest(first.encode("utf-8", "replace"),
                                                               self.server.secret.encode()):
            self._send(404)
            return None
        self.server.touch()
        return "/" + rest

    def do_GET(self):
        rest = self._gate()
        if rest is None:
            return
        if rest != "/":
            self._send(404)
            return
        if self.server.on_page:
            self.server.on_page()  # the launcher file holding the link is gone before the page arrives
        nonce = secrets.token_urlsafe(16)
        body = render_page(self.server.page_lang, nonce)
        self._send(200, body, "text/html; charset=utf-8", {"Content-Security-Policy": csp(nonce)})

    def do_POST(self):
        rest = self._gate()
        if rest is None:
            return
        name = rest[len("/api/"):] if rest.startswith("/api/") else ""
        if name not in ACTIONS:
            self._send(404)
            return
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self._send(415)
            return
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            self._send(411)
            return
        if length < 0 or length > MAX_BODY:
            self._send(413)
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict):
            self._json(400, {"error": "bad_request", "state": self.server.flow.state()})
            return
        try:
            error = ACTIONS[name](self.server.flow, body)
        except (Exception, SystemExit):
            error = "internal"
        out = {"state": self.server.flow.state()}
        if error:
            out["error"] = error
        self._json(200 if not error else 409 if error == "order" else 400, out)


ACTIONS = {
    "state": lambda flow, body: None,
    "token": lambda flow, body: flow.submit_token(body.get("token")),
    "recheck": lambda flow, body: flow.recheck(),
    "pair": lambda flow, body: flow.start_pairing(),
    "confirm": lambda flow, body: flow.confirm(body),
    "finish": lambda flow, body: flow.finish(body.get("lang"), body.get("prefs")),
    "clipboard": lambda flow, body: flow.clear_clipboard(),
}


# --- start -----------------------------------------------------------------------------------------------------

def _launcher(url: str) -> Path:
    """A private file that forwards to the page, so the secret is never a browser's command-line argument."""
    fd, name = tempfile.mkstemp(prefix="harnessgram-setup-", suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
                f'<meta http-equiv="refresh" content="0;url={url}"><title>Harnessgram setup</title>'
                f'<a href="{url}">Open the Harnessgram setup page</a>\n')
    os.chmod(name, 0o600)
    return Path(name)


def run_web(*, language: Optional[str] = None, owner_id: Optional[int] = None, install_hooks: bool = True,
            service: bool = True, start: bool = True, test_message: bool = True, run_doctor: bool = True,
            settings_file: Optional[Path] = None, env: Optional[dict] = None, say: Callable[[str], None] = print,
            open_browser: Optional[Callable[[str], bool]] = None, bot_factory=None,
            idle_timeout: float = IDLE_SECONDS, pair_timeout: float = PAIR_SECONDS) -> int:
    env = os.environ if env is None else env
    flow = SetupFlow(language=language, owner_id=owner_id, install_hooks=install_hooks, service=service, start=start,
                     test_message=test_message, run_doctor=run_doctor, settings_file=settings_file, env=env,
                     bot_factory=bot_factory, pair_timeout=pair_timeout, say=say)
    flow.load_saved()
    server = SetupServer(flow, page_lang=language or "", idle_timeout=idle_timeout)
    launcher: Optional[Path] = None

    def forget_launcher() -> None:
        if launcher is not None:
            try:
                launcher.unlink()
            except OSError:
                pass

    server.on_page = forget_launcher
    say("Harnessgram setup page")
    remote = bool(env.get("SSH_CONNECTION") or env.get("SSH_TTY"))
    opened = False
    if not remote:
        try:
            launcher = _launcher(server.url)
            opened = bool((open_browser or webbrowser.open)(launcher.as_uri()))
        except (Exception, SystemExit):
            opened = False
    if opened:
        say("  The setup page is open in your browser. Keep this window open until the page says \"All set\".")
    else:
        say(f"  Open this link in a browser on this computer:\n\n      {server.url}\n")
        if remote:
            say(f"  Connected over SSH? Forward the port first (ssh -L {server.port}:127.0.0.1:{server.port} "
                "<this machine>), then open the link on your own computer.")
    say(f"  After \"All set\" the page stays open up to {int(CLIP_GRACE // 60)} minutes for its clear-clipboard "
        "button; press Ctrl-C to close it sooner.")
    threading.Thread(target=watch, args=(server,), daemon=True, name="setup-page-watch").start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        if flow.result is None:  # after the finish, Ctrl-C only closes the page sooner
            say("\nSetup cancelled; nothing more was changed.")
            return 1
    finally:
        flow.close()
        server.server_close()
        forget_launcher()
    if flow.result is None:
        say("\nThe setup page closed after 30 minutes without activity; nothing more was changed. "
            "Run `harnessgram setup --web` to start again.")
        return 1
    if flow.result["code"] == 0:
        say("\nAll set. Start a new Claude Code session; its first reply creates its topic.")
    elif not flow.result["threaded"]:
        say("\nThreaded Mode is still off: turn it on in BotFather, then run `harnessgram doctor`.")
    else:
        say("\nSome checks need attention: run `harnessgram doctor` to see them again.")
    return flow.result["code"]


# --- the page ----------------------------------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="__LANG__" dir="__DIR__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="referrer" content="no-referrer">
<title>__TITLE__</title>
<style nonce="__NONCE__">
:root{
--bg:#f3f5fa;--card:#ffffff;--text:#151821;--muted:#5d6475;--line:#e1e5ee;--field:#f7f8fb;
--accent:#2481cc;--on-accent:#ffffff;--accent-soft:rgba(36,129,204,.14);
--ok:#16834a;--ok-soft:#e3f5ea;--warn:#9a5b00;--warn-soft:#fff3dc;--err:#b42318;--err-soft:#fdeceb;--code:#eef1f6;
}
@media (prefers-color-scheme:dark){:root{
--bg:#0e1116;--card:#171b22;--text:#eceff4;--muted:#9aa2b1;--line:#29303b;--field:#11151b;
--accent:#4aa3ec;--on-accent:#06121c;--accent-soft:rgba(74,163,236,.18);
--ok:#4cc985;--ok-soft:#14301f;--warn:#f2b552;--warn-soft:#352812;--err:#ff8a80;--err-soft:#3a1715;--code:#222833;
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans Hebrew","Arial Hebrew",sans-serif}
main{max-width:640px;margin-inline:auto;padding-block:28px 56px;padding-inline:16px}
header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
h1{font-size:1.65rem;line-height:1.25;margin:0 0 6px}
.sub{color:var(--muted);margin:0}
.lang{flex:none;min-height:40px;padding-inline:14px;border-radius:999px;border:1px solid var(--line);
background:var(--card);color:var(--text);font:inherit;font-size:.92rem;cursor:pointer}
ol.steps{list-style:none;margin:24px 0 0;padding:0;display:grid;gap:12px}
li.step{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px;
display:grid;grid-template-columns:40px minmax(0,1fr);gap:14px;align-items:start}
li.step.current{border-color:var(--accent);box-shadow:0 0 0 4px var(--accent-soft)}
li.step.todo{opacity:.62}
.mark{width:36px;height:36px;border-radius:50%;display:grid;place-items:center;font-weight:700;
border:2px solid var(--line);color:var(--muted);background:var(--card)}
.current .mark{border-color:var(--accent);color:var(--accent)}
.done .mark{background:var(--ok);border-color:var(--ok);color:#fff}
.kicker{margin:0;color:var(--muted);font-size:.85rem}
h2{font-size:1.12rem;line-height:1.35;margin:2px 0 0}
.status{margin:4px 0 0;color:var(--ok);font-weight:600}
.body{margin-block-start:10px}
.body p{margin:8px 0}
ol.how{margin:6px 0 0;padding-inline-start:22px}
ol.how li{margin-block:8px}
.codeline{margin-block:6px}
code{display:inline-block;max-width:100%;overflow-wrap:anywhere;direction:ltr;unicode-bidi:isolate;
background:var(--code);border-radius:8px;padding:3px 10px;font:.94em/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.row{display:flex;flex-wrap:wrap;gap:10px;margin-block-start:14px;align-items:center}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:48px;padding-inline:20px;
border-radius:12px;border:1px solid transparent;background:var(--accent);color:var(--on-accent);font:inherit;
font-weight:600;text-decoration:none;cursor:pointer}
.btn.soft{background:transparent;color:var(--accent);border-color:var(--line)}
.btn.quiet{background:transparent;color:var(--accent);min-height:40px;padding-inline:6px;font-weight:500}
.btn[disabled]{opacity:.6;cursor:default}
.btn:focus-visible,.lang:focus-visible,input:focus-visible{outline:3px solid var(--accent-soft);outline-offset:2px}
label{display:block;font-weight:600;margin-block:4px 6px}
input{width:100%;min-height:52px;padding-inline:14px;border-radius:12px;border:1px solid var(--line);
background:var(--field);color:var(--text);font:inherit;text-align:start}
.hint{color:var(--muted);font-size:.9rem;margin:6px 0 0}
fieldset.prefs{border:1px solid var(--line);border-radius:14px;padding-block:6px 4px;padding-inline:14px;margin:14px 0 0;
min-inline-size:0}
fieldset.prefs legend{font-weight:700;padding-inline:6px}
.pref{margin-block:14px}
.pref label{margin-block:0 6px}
select{width:100%;min-height:48px;padding-inline:12px;border-radius:12px;border:1px solid var(--line);
background:var(--field);color:var(--text);font:inherit;text-align:start}
.switchtop{display:flex;align-items:center;justify-content:space-between;gap:12px}
.switchtop label{margin:0}
input.switch{appearance:none;-webkit-appearance:none;flex:none;width:52px;min-height:0;height:32px;padding:0;margin:0;
border:0;border-radius:999px;background:var(--line);position:relative;cursor:pointer;transition:background .15s}
input.switch::before{content:"";position:absolute;inset-block-start:3px;inset-inline-start:3px;width:26px;height:26px;
border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.3);transition:inset-inline-start .15s}
input.switch:checked{background:var(--accent)}
input.switch:checked::before{inset-inline-start:23px}
select:focus-visible,input.switch:focus-visible{outline:3px solid var(--accent-soft);outline-offset:2px}
.msg{border-radius:12px;padding:10px 14px;margin-block-start:12px}
.msg p{margin:0}
.msg.err{background:var(--err-soft);color:var(--err)}
.msg.warn{background:var(--warn-soft);color:var(--warn)}
.msg.ok{background:var(--ok-soft);color:var(--ok)}
.wait{display:flex;align-items:center;gap:10px;color:var(--muted);margin-block-start:12px}
.spin{width:18px;height:18px;border-radius:50%;border:2px solid var(--line);border-block-start-color:var(--accent);
animation:spin .9s linear infinite;flex:none}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
.who{border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin-block-start:12px;background:var(--field)}
.who b{display:block;font-size:1.05rem}
.who span{color:var(--muted)}
ul.results{list-style:none;margin:12px 0 0;padding:0;display:grid;gap:8px}
ul.results li{display:grid;grid-template-columns:24px minmax(0,1fr);gap:8px;align-items:start}
.ic{font-weight:700;text-align:center}
.ic.ok{color:var(--ok)}
.ic.warn{color:var(--warn)}
h3{font-size:1rem;margin:18px 0 6px}
ul.doctor{list-style:none;margin:0;padding:0;display:grid;gap:4px;font-size:.88rem}
ul.doctor li{display:flex;gap:8px;align-items:baseline;text-align:start}
.lvl{flex:none;min-width:44px;text-align:center;border-radius:6px;padding-inline:4px;font:600 .78rem/1.6 ui-monospace,Menlo,monospace;
background:var(--code)}
.lvl.FAIL{background:var(--err-soft);color:var(--err)}
.lvl.WARN,.lvl.warn{background:var(--warn-soft);color:var(--warn)}
.lvl.ok{background:var(--ok-soft);color:var(--ok)}
.banner{margin-block-start:18px;border-radius:18px;padding:18px;background:var(--ok-soft);color:var(--text)}
.banner h2{color:var(--ok);font-size:1.3rem}
.banner.warn{background:var(--warn-soft)}
.banner.warn h2{color:var(--warn)}
.banner p{margin:6px 0 0}
@media (max-width:440px){
li.step{grid-template-columns:32px minmax(0,1fr);gap:10px;padding:14px}
.mark{width:30px;height:30px}
.row .btn{flex:1 1 100%}
h1{font-size:1.4rem}
}
</style>
</head>
<body>
<main id="app"></main>
<script nonce="__NONCE__">
(function(){
"use strict";
var ALL=__STRINGS__, FORCED="__FORCED__";
var CODES={newbot:"/newbot",name:"Harnessgram",username:suggestUsername(),token:"123456789:AAE…",threaded:"/mybots → your bot → Bot Settings → Threaded Mode → On",
  install:"harnessgram install",doctor:"harnessgram doctor",serve:"harnessgram serve",setup:"harnessgram setup",again:"harnessgram setup --web"};
var ERR={token_format:"err_format",token_rejected:"err_rejected",network:"err_network",save_failed:"err_save",
  install_failed:"err_install",order:"err_order",gone:"err_gone",bad_request:"err_generic",stale:"err_generic",conflict:"err_conflict",internal:"err_generic"};
var ERR_CMD={save_failed:"setup",install_failed:"install",gone:"again",conflict:"again"};
var BASE=location.pathname.replace(/\/?$/,"/");
var nav=(navigator.language||"").toLowerCase();
var lang=FORCED||((nav.indexOf("he")===0||nav.indexOf("iw")===0)?"he":"en"), S=ALL[lang];
var app=document.getElementById("app"), st=null, err={}, working=null, pollTimer=null;
var seen1=false, changing=false, gone=false, pairAsked=false;
var tokenInput=document.createElement("input");
tokenInput.type="password";tokenInput.id="token";tokenInput.autocomplete="off";tokenInput.spellcheck=false;
tokenInput.setAttribute("autocapitalize","off");tokenInput.setAttribute("autocorrect","off");
function syncDir(el){if(el.value){el.setAttribute("dir","auto");}else{el.removeAttribute("dir");}}
tokenInput.addEventListener("input",function(){syncDir(tokenInput);});
// BotFather's whole message may be pasted: keep only the token (a one-line field would glue the lines together)
tokenInput.addEventListener("paste",function(ev){
  var data=ev.clipboardData||window.clipboardData;if(!data)return;
  ev.preventDefault();var text=String(data.getData("text")||"");
  var found=findTokens(text);tokenInput.value=found.length===1?found[0]:text.replace(/\s+/g," ").trim();
  syncDir(tokenInput);
});

function suggestUsername(){
  var abc="abcdefghijkmnpqrstuvwxyz23456789",out="",r=new Uint32Array(6);
  (window.crypto||window.msCrypto).getRandomValues(r);
  for(var i=0;i<6;i++){out+=abc.charAt(r[i]%abc.length);}
  return "harnessgram_"+out+"_bot";
}
function findTokens(text){
  var re=/(^|[^A-Za-z0-9_:\/-])(\d+:[A-Za-z0-9_-]{30,})(?![A-Za-z0-9_:-])/g,m,seen={},out=[];
  while((m=re.exec(text))!==null){if(!seen[m[2]]){seen[m[2]]=1;out.push(m[2]);}}
  return out;
}
function t(k,v){var s=S[k]||ALL.en[k]||k;if(v){for(var n in v){s=s.split("{"+n+"}").join(String(v[n]));}}return s;}
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;}
function add(p){for(var i=1;i<arguments.length;i++){if(arguments[i])p.appendChild(arguments[i]);}return p;}
function codeLine(key){var d=el("div","codeline"),c=el("code",null,CODES[key]);c.setAttribute("dir","ltr");d.appendChild(c);return d;}
function button(text,cls,fn){var b=el("button","btn"+(cls?" "+cls:""),text);b.type="button";b.addEventListener("click",fn);return b;}
function linkButton(text,href,cls){var a=el("a","btn"+(cls?" "+cls:""),text);a.href=href;a.target="_blank";a.rel="noopener noreferrer";return a;}
function msg(kind,text,cmd){var m=el("div","msg "+kind);m.setAttribute("role",kind==="err"?"alert":"status");add(m,el("p",null,text));if(cmd)m.appendChild(codeLine(cmd));return m;}
function errBox(step){var e=err[step];if(!e)return null;return msg("err",t(ERR[e]||"err_generic"),ERR_CMD[e]);}
function waiting(text){var w=el("div","wait");w.setAttribute("role","status");add(w,el("span","spin"),el("span",null,text));return w;}

function setLang(l){lang=l;S=ALL[l];var h=document.documentElement;h.lang=l;h.dir=(l==="he")?"rtl":"ltr";document.title=t("title");syncDir(tokenInput);render();}

function api(name,body){
  return fetch(BASE+"api/"+name,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body||{}),cache:"no-store",credentials:"same-origin",referrerPolicy:"no-referrer"})
  .then(function(r){return r.json().catch(function(){return {error:"internal"};});},function(){return {error:"gone"};});
}
function apply(step,r){
  if(r.state)st=r.state;
  if(r.error==="gone"){gone=true;}
  if(step){err[step]=r.error||null;}
  render();
}
function act(step,name,body){
  working=step;render();
  return api(name,body).then(function(r){working=null;apply(step,r);return r;});
}

function poll(){
  clearTimeout(pollTimer);pollTimer=null;
  if(!st||gone||st.finished||changing)return;
  if(st.pair&&st.pair.state==="waiting"){
    pollTimer=setTimeout(function(){api("state").then(function(r){if(r.error==="gone"){gone=true;}else if(r.state){st=r.state;}render();});},2000);
  }
}

function step1(cur){
  var b=el("div","body"),how=el("ol","how");
  add(how,add(el("li"),el("span",null,t("s1_a")),codeLine("newbot")),
    add(el("li"),el("span",null,t("s1_b")),codeLine("name"),el("span",null,t("s1_b2")),codeLine("username")),
    add(el("li"),el("span",null,t("s1_c")),codeLine("token")),
    add(el("li"),el("span",null,t("s1_d")),codeLine("threaded")));
  add(b,how,add(el("div","row"),linkButton(t("s1_open"),"https://t.me/BotFather"),
    cur?button(t("s1_next"),"soft",function(){seen1=true;render();tokenInput.focus();}):null));
  return b;
}

function tokenForm(){
  var f=el("form","body");f.noValidate=true;
  var l=el("label",null,t("s2_label"));l.htmlFor="token";
  tokenInput.placeholder=t("s2_ph");syncDir(tokenInput);
  var go=el("button","btn",working===2?t("s2_checking"):t("s2_check"));go.type="submit";go.disabled=working===2;
  add(f,l,tokenInput,el("p","hint",t("s2_hint")),errBox(2),add(el("div","row"),go,
    (changing&&st.bot)?button(t("s2_cancel"),"quiet",function(){changing=false;err[2]=null;tokenInput.value="";render();}):null));
  f.addEventListener("submit",function(ev){
    ev.preventDefault();
    var v=tokenInput.value;tokenInput.value="";syncDir(tokenInput);
    working=2;render();
    api("token",{token:v}).then(function(r){
      working=null;
      if(!r.error){changing=false;pairAsked=false;}  // a new bot starts pairing again (the server reset it)
      apply(2,r);
    });
  });
  return f;
}

function threadedBox(){
  if(!st.bot)return null;
  if(st.threaded)return el("p","hint","✓ "+t("threaded_on"));
  var m=msg("warn",t("threaded_off"),"threaded");
  if(!st.finished&&!st.busy&&working!==4){
    add(m,add(el("div","row"),button(working===21?t("s2_checking"):t("recheck"),"soft",function(){act(21,"recheck");})));
  }
  return m;
}

function step3(){
  var b=el("div","body"),p=st.pair||{state:"idle"};
  add(b,el("p",null,t("s3_body")));
  if(p.state==="waiting"&&p.link){
    add(b,add(el("div","row"),linkButton(t("s3_open"),p.link)),el("p","hint",t("s3_manual")));
    var c=el("code",null,"/start "+p.code);c.setAttribute("dir","ltr");add(b,add(el("div","codeline"),c),waiting(t("s3_waiting")));
  }else if(p.state==="found"&&p.account){
    var who=el("div","who");
    add(who,el("p","kicker",t("s3_found")),el("b",null,p.account.name),el("span",null,"@"+p.account.username+" · "+p.account.id));
    who.querySelector("span").setAttribute("dir","ltr");
    // the confirmation names what this page showed: a late or other-tab answer can never approve another account
    var seen={bot_gen:st.gens.bot,pair_gen:st.gens.pair,account_id:p.account.id};
    add(b,who,add(el("div","row"),
      button(t("s3_yes"),"",function(){act(3,"confirm",{yes:true,bot_gen:seen.bot_gen,pair_gen:seen.pair_gen,account_id:seen.account_id});}),
      button(t("s3_no"),"soft",function(){pairAsked=false;act(3,"confirm",{yes:false,bot_gen:seen.bot_gen,pair_gen:seen.pair_gen,account_id:seen.account_id});})));
  }else if(p.state==="expired"||p.state==="failed"){
    add(b,msg("warn",t(p.state==="expired"?"s3_expired":"s3_failed")),
      add(el("div","row"),button(t("s3_retry"),"",function(){act(3,"pair");})));
  }else{
    add(b,waiting(t("loading")));
    if(!pairAsked&&working!==3){pairAsked=true;setTimeout(function(){act(3,"pair");},0);}
  }
  add(b,errBox(3));
  return b;
}

// Preferences: what the person changed, else what an earlier setup saved, else a default from the page language.
var chosen={}, names=null;
function pref(k){
  if(Object.prototype.hasOwnProperty.call(chosen,k))return chosen[k];
  var d=(st&&st.prefs)||{};
  if(d[k]!=null)return d[k];
  if(k==="target_language")return lang;
  if(k==="translate")return pref("target_language")!=="en";
  if(k==="ui_language")return lang;
  if(k==="context_footer")return true;
  return "normal";
}
function prefsBody(){
  return {target_language:pref("target_language"),translate:!!pref("translate"),ui_language:pref("ui_language"),
    context_footer:!!pref("context_footer"),activity:pref("activity")};
}
function langName(code,fallback){
  try{
    if(window.Intl&&Intl.DisplayNames){
      if(!names||names.l!==lang){names={l:lang,d:new Intl.DisplayNames([lang],{type:"language"})};}
      var n=names.d.of(code);if(n&&n!==code)return n;
    }
  }catch(e){}
  return fallback||code;
}
function choice(id,key,label,hint,options){
  var w=el("div","pref"),l=el("label",null,label),s=el("select");l.htmlFor=id;s.id=id;
  options.forEach(function(o){var op=el("option",null,o[1]);op.value=o[0];s.appendChild(op);});
  s.value=pref(key);
  s.addEventListener("change",function(){chosen[key]=s.value;render();});
  return add(w,l,s,el("p","hint",hint));
}
function toggle(id,key,label,hint){
  var w=el("div","pref"),top=el("div","switchtop"),l=el("label",null,label),c=el("input","switch");
  c.type="checkbox";c.id=id;c.setAttribute("role","switch");c.checked=!!pref(key);l.htmlFor=id;
  c.addEventListener("change",function(){chosen[key]=c.checked;});
  return add(w,add(top,l,c),el("p","hint",hint));
}
function prefsBox(){
  var box=el("fieldset","prefs");
  var langs=(st.languages||[]).map(function(x){return [x[0],langName(x[0],x[1])];});
  add(box,el("legend",null,t("prefs_title")),el("p","hint",t("prefs_hint")),
    choice("p-reply","target_language",t("p_reply"),t("p_reply_hint"),langs),
    toggle("p-translate","translate",t("p_translate"),t("p_translate_hint")),
    choice("p-menu","ui_language",t("p_menu"),t("p_menu_hint"),[["he",langName("he","Hebrew")],["en",langName("en","English")]]),
    toggle("p-footer","context_footer",t("p_footer"),t("p_footer_hint")),
    choice("p-activity","activity",t("p_activity"),t("p_activity_hint"),
      [["quiet",t("act_quiet")],["normal",t("act_normal")],["full",t("act_full")]]));
  return box;
}

function step4(){
  var b=el("div","body");
  add(b,el("p",null,t("s4_body")));
  if(working===4){add(b,waiting(t("s4_working")));}
  else{add(b,prefsBox(),add(el("div","row"),button(t("s4_go"),"",function(){act(4,"finish",{lang:lang,prefs:prefsBody()});})));}
  add(b,errBox(4));
  return b;
}

function results(r){
  var b=el("div","body"),ul=el("ul","results");
  r.items.forEach(function(it){
    var warn=!!it.warn;
    var li=el("li");add(li,el("span","ic "+(warn?"warn":"ok"),warn?"!":"✓"));
    var txt=el("div");add(txt,el("span",null,t(it.key,it.vars)));if(it.cmd&&CODES[it.cmd]){txt.appendChild(codeLine(it.cmd));}
    li.appendChild(txt);ul.appendChild(li);
  });
  b.appendChild(ul);
  if(r.doctor&&r.doctor.length){
    var d=el("ul","doctor");
    r.doctor.forEach(function(line){
      var li=el("li");li.setAttribute("dir","ltr");
      add(li,el("span","lvl "+line.level,line.level),el("span",null,line.text));d.appendChild(li);
    });
    add(b,el("h3",null,t("r_doctor")),d);
  }
  if(!r.threaded){add(b,msg("warn",t("warn_threaded"),"doctor"));}
  else if(r.code!==0){add(b,msg("warn",t("warn_doctor")));}
  return b;
}

function stepItem(n,title,state,status,body){
  var li=el("li","step "+state);if(state==="current")li.setAttribute("aria-current","step");
  var mark=el("span","mark",state==="done"?"✓":String(n));mark.setAttribute("aria-hidden","true");
  var main=el("div");
  add(main,el("p","kicker",state==="done"?t("done"):t("step_n",{n:n})),el("h2",null,title),
    status?el("p","status",status):null,body);
  return add(li,mark,main);
}

function render(){
  var focused=document.activeElement&&document.activeElement.id;
  while(app.firstChild)app.removeChild(app.firstChild);
  var head=el("header"),hx=el("div");
  add(hx,el("h1",null,t("title")),el("p","sub",t("subtitle")));
  var lb=el("button","lang",t("lang_other"));lb.type="button";lb.lang=(lang==="he")?"en":"he";
  lb.addEventListener("click",function(){setLang(lang==="he"?"en":"he");});
  add(head,hx,lb);
  app.appendChild(head);
  if(gone&&!(st&&st.finished)){app.appendChild(msg("err",t("err_gone"),"again"));}
  if(!st){app.appendChild(waiting(t("loading")));return;}
  var hasBot=!!st.bot, paired=!!(st.paired||st.kept_id), fin=!!st.finished;
  var s1=(hasBot||seen1)?"done":"current";
  var s2=(hasBot&&!changing)?"done":(s1==="done"?"current":"todo");
  var s3=paired?"done":(s2==="done"?"current":"todo");
  var s4=fin?"done":(s3==="done"&&s2==="done"?"current":"todo");
  var ol=el("ol","steps");
  ol.appendChild(stepItem(1,t("s1_title"),s1,s1==="done"&&hasBot?t("s1_done"):null,s1==="current"?step1(true):null));
  var b2=null;
  if(s2==="current"){b2=tokenForm();}
  else if(s2==="done"){b2=add(el("div","body"),threadedBox(),errBox(21),(!fin&&!st.busy&&working!==4)?add(el("div","row"),button(t("s2_change"),"quiet",function(){changing=true;render();tokenInput.focus();})):null);}
  ol.appendChild(stepItem(2,t("s2_title"),s2,s2==="done"?t("s2_ok",{bot:"@"+st.bot.username}):null,b2));
  var status3=st.paired?t("s3_done",{name:st.paired.name}):(st.kept_id?t("s3_kept",{id:st.kept_id}):null);
  ol.appendChild(stepItem(3,t("s3_title"),s3,status3,s3==="current"?step3():null));
  ol.appendChild(stepItem(4,t("s4_title"),s4,null,s4==="current"?step4():(fin?results(st.finished):null)));
  app.appendChild(ol);
  if(fin){
    var ok=st.finished.code===0,ban=el("section","banner"+(ok?"":" warn"));ban.setAttribute("role","status");
    add(ban,el("h2",null,ok?t("all_set"):t("almost")),el("p",null,t("next")),el("p",null,t("next_close")));
    var CLIP={cleared:"clip_cleared",clean:"clip_clean",failed:"clip_failed"};
    var clip=gone?msg("warn",t("clip_gone")):(CLIP[st.clip]?msg(st.clip==="failed"?"warn":"ok",t(CLIP[st.clip])):null);
    add(ban,el("p",null,t("clip_hint")),clip,(gone||st.clip==="cleared"||st.clip==="clean")?null:
      add(el("div","row"),button(working===5?t("clip_working"):t("clip_go"),"soft",function(){act(5,"clipboard",{});})));
    app.appendChild(ban);
  }
  if(focused){var again=document.getElementById(focused);if(again)again.focus();}
  poll();
}

setLang(lang);
api("state").then(function(r){apply(null,r);});
})();
</script>
</body>
</html>
"""


def render_page(lang: str, nonce: str) -> bytes:
    """The page in `lang` ("he" or "en"); any other value lets the browser's language choose."""
    forced = lang if lang in ("he", "en") else ""
    code = forced or "en"
    strings = json.dumps(STRINGS, ensure_ascii=False).replace("</", "<\\/")
    return (PAGE.replace("__LANG__", code).replace("__DIR__", "rtl" if code == "he" else "ltr")
            .replace("__TITLE__", STRINGS[code]["title"]).replace("__NONCE__", nonce)
            .replace("__FORCED__", forced).replace("__STRINGS__", strings)).encode("utf-8")
