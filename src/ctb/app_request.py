"""The owner's "request an app" action: one App Store link, id or app name goes to a local approval command.

The command is `app_request_command` in the bridge config (a list of strings; empty = the feature is hidden
everywhere). Example (documented only, never written into a real config):

    "app_request_command": ["/path/to/.venv/bin/python", "/path/to/scripts/app_request.py"]

It is called as `command + [<one App Store link, id or app name>]` with shell=False, no stdin, a minimal environment and a
60 second limit. It prints exactly one JSON line {"status","app","bundle","reason","until"} and exits with:
0 approved (App Store open until "until"), 2 denied, 3 review (the committee answers later), 4 approved but remote
management is not connected yet, 1 bad link or name, or error. A name is searched by the command (installs are
locked, so the owner often cannot open the App Store to copy a link). Nothing the owner typed is logged.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime
from typing import Callable, List, Optional

log = logging.getLogger("ctb.app_request")

MAX_CHARS = 500          # the longest text taken from the owner
TIMEOUT = 60             # seconds the command may run
MIN_GAP = 10             # seconds between two requests
OUT_BYTES = 8192         # the most the command's answer may be
TRANSLATE_WAIT = 4.0     # seconds the Hebrew version of the reason may take before the English one is shown

# exit code -> state; anything else (including a signal) is an error
STATES = {0: "approved", 2: "denied", 3: "review", 4: "pending", 1: "error"}

_LINK = re.compile(r"https://apps\.apple\.com/[^\s<>\"'`]{1,300}")
_LINK_ID = re.compile(r"/id([0-9]{6,12})(?![0-9A-Za-z])")
_ID = re.compile(r"(?<![0-9A-Za-z/._=?&-])id[0-9]{6,12}(?![0-9A-Za-z])")
_BARE = re.compile(r"(?:id)?[0-9]{6,12}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TRAILING = ".,;:!?)]}>"
NAME_CHARS = (2, 60)     # an app name the owner typed; the command validates and searches it
_URLISH = re.compile(r"(?i)https?:|www\.|://")
# The same characters the command accepts (letters of any script, digits, space and . & ' + : ! -), at least one
# letter; its own subcommands are never sent as a name (they would run instead of a search and exit 0).
_NAME = re.compile(r"[\w .&'+:!-]+")
_LETTER = re.compile(r"[^\W\d_]")
RESERVED = frozenset({"tick", "status"})


class BadInput(ValueError):
    """The text has no App Store link, id or app name (or is not text at all)."""


def configured(cfg) -> bool:
    """True when the command is set and the `app_request` add-on is on; the panel and the bot hide it otherwise."""
    return command_of(cfg) is not None and _on(cfg)


def _on(cfg) -> bool:
    try:
        from . import addons
        return addons.enabled(cfg, "app_request")
    except Exception:
        return True


def command_of(cfg) -> Optional[List[str]]:
    cmd = getattr(cfg, "app_request_command", None)
    if isinstance(cmd, list) and cmd and all(isinstance(p, str) and p and "\x00" not in p for p in cmd):
        return list(cmd)
    return None


def extract(text) -> str:
    """The one App Store link or id the owner pasted (the share text around it is dropped), else the app name
    the owner typed. BadInput otherwise (another site's link, a bare number, a name that is too short or long)."""
    if not isinstance(text, str):
        raise BadInput("text")
    text = text.strip()
    if not text or len(text) > MAX_CHARS or _CONTROL.search(text):
        raise BadInput("text")
    link = _LINK.search(text)
    if link:
        url = link.group(0).rstrip(_TRAILING)
        if _LINK_ID.search(url):
            return url
        raise BadInput("link")
    if _BARE.fullmatch(text):
        return text
    found = _ID.search(text)
    if found:
        return found.group(0)
    return _name(text)


def _name(text: str) -> str:
    """The app's name as the owner typed it (one line, spaces collapsed), passed on as one argument."""
    name = " ".join(text.split())
    if (_URLISH.search(name) or name.startswith("-") or name.lower() in RESERVED or not _NAME.fullmatch(name)
            or not _LETTER.search(name) or not NAME_CHARS[0] <= len(name) <= NAME_CHARS[1]):
        raise BadInput("link")
    return name


class Gate:
    """One request at a time and at most one every MIN_GAP seconds."""

    def __init__(self, gap: float = MIN_GAP, clock: Callable[[], float] = time.monotonic):
        self.gap, self.clock = gap, clock
        self._lock = threading.Lock()
        self._last: Optional[float] = None
        self._busy = False

    def enter(self) -> int:
        """0 = go ahead (call leave() after); otherwise the seconds to wait."""
        with self._lock:
            now = self.clock()
            if self._busy:
                return max(1, int(self.gap))
            if self._last is not None and now - self._last < self.gap:
                return max(1, int(self.gap - (now - self._last) + 0.999))
            self._busy, self._last = True, now
            return 0

    def leave(self) -> None:
        with self._lock:
            self._busy = False

    def reset(self) -> None:
        with self._lock:
            self._last, self._busy = None, False


GATE = Gate()


def _env() -> dict:
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
    for key in ("HOME", "USER", "LOGNAME", "TMPDIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _clean(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _CONTROL.sub("", value).replace("\r", " ").replace("\n", " ").strip()[:limit]


def _until(value) -> str:
    """The approval window's end as the command printed it, when it is a real ISO time; else ''."""
    if not isinstance(value, str) or len(value) > 40:
        return ""
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return value


def parse_line(out: str) -> dict:
    """The last JSON object line of the command's output; {} when there is none. Never raises."""
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            return data if isinstance(data, dict) else {}
    return {}


def interpret(code: Optional[int], out: str) -> dict:
    """The exit code decides the state; the JSON line only adds the app's name, the reason and the end time."""
    data = parse_line(out)
    state = STATES.get(code, "error") if code is not None else "error"
    result = {"state": state, "app": _clean(data.get("app"), 120), "bundle": _clean(data.get("bundle"), 160),
              "reason": _clean(data.get("reason"), 400), "until": _until(data.get("until"))}
    if state not in ("approved",):
        result["until"] = ""
    return result


def run(cfg, token: str, runner: Optional[Callable] = None) -> dict:
    """Run the command once for an already extracted `token`. Returns the interpreted result; a command that
    cannot start, times out or prints nothing is an error result (with "error" saying which)."""
    cmd = command_of(cfg)
    if cmd is None:
        return {"state": "error", "app": "", "bundle": "", "reason": "", "until": "", "error": "unconfigured"}
    argv = cmd + [token]
    try:
        if runner is not None:
            code, out = runner(argv)
        else:
            code, out = _spawn(argv)
    except subprocess.TimeoutExpired:
        log.warning("app request timed out")
        return {"state": "error", "app": "", "bundle": "", "reason": "", "until": "", "error": "timeout"}
    except (OSError, ValueError):
        log.warning("app request command could not start")
        return {"state": "error", "app": "", "bundle": "", "reason": "", "until": "", "error": "start"}
    result = interpret(code, out)
    log.info("app request state=%s exit=%s", result["state"], code)
    return result


def _spawn(argv: List[str]):
    proc = subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=_env(), start_new_session=True, close_fds=True)
    try:
        out, _ = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            proc.communicate(timeout=3)
        except Exception:
            pass
        raise
    return proc.returncode, out[:OUT_BYTES].decode("utf-8", "replace")


def hebrew_reason(translator, reason: str, wait: float = TRANSLATE_WAIT) -> Optional[str]:
    """The reason in Hebrew when the panel's translator makes it within `wait` seconds, else None (the English
    text is shown on its own line). Text that is already Hebrew needs nothing: None."""
    if not reason or translator is None:
        return None
    deadline = time.monotonic() + wait
    while True:
        try:
            got = translator.he(reason)
        except Exception:
            return None
        if got:
            return got
        if got is not None or time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def request(cfg, text, translator=None, gate: Optional[Gate] = None, runner: Optional[Callable] = None) -> dict:
    """The whole action: validate, rate-limit, run, translate. Returns {"ok": True, "result": {...}} or
    {"ok": False, "error": "unconfigured" | "bad" | "rate", "why"/"retry": ...} without running anything."""
    if not configured(cfg):
        return {"ok": False, "error": "unconfigured"}
    try:
        token = extract(text)
    except BadInput as e:
        return {"ok": False, "error": "bad", "why": str(e)}
    gate = gate or GATE
    wait = gate.enter()
    if wait:
        return {"ok": False, "error": "rate", "retry": wait}
    try:
        result = run(cfg, token, runner)
    finally:
        gate.leave()
    result["reason_he"] = hebrew_reason(translator, result["reason"]) if result["reason"] else None
    return {"ok": True, "result": result}


def telegram_lines(result: dict, lang: str, tz=None) -> List[str]:
    """The owner's answer for the `/app` bot command: short lines, any English (app name, reason) on its own line."""
    from .i18n import t
    state = result.get("state")
    lines: List[str] = []
    if state == "approved":
        stamp = ""
        if result.get("until"):
            try:
                when = datetime.fromisoformat(result["until"].replace("Z", "+00:00"))
                stamp = when.astimezone(tz).strftime("%H:%M") if tz else when.astimezone().strftime("%H:%M")
            except ValueError:
                stamp = ""
        lines.append(t("app_request_approved_until" if stamp else "app_request_approved", lang=lang, time=stamp))
    else:
        lines.append(t("app_request_" + (state if state in ("denied", "review", "pending") else "error"), lang=lang))
    if result.get("app"):
        lines.append(result["app"])
    reason = result.get("reason_he") or result.get("reason")
    if reason:
        lines.append(reason)
    return lines


def telegram_command(cfg, arg: str, reply: Callable[[str], None], translator=None) -> str:
    """`/app <link>` in the owner's private chat. The command runs in a thread: polling never waits for it."""
    from .i18n import t
    lang = getattr(cfg, "ui_language", "") or "he"
    if not arg.strip():
        reply(t("app_request_usage", lang=lang))
        return "app-request"

    def work():
        try:
            answer = request(cfg, arg, translator=translator)
        except Exception:
            reply(t("app_request_error", lang=lang))
            return
        if answer["ok"]:
            reply("\n".join(telegram_lines(answer["result"], lang)))
        elif answer["error"] == "rate":
            reply(t("app_request_rate", lang=lang, n=answer["retry"]))
        else:
            reply(t("app_request_bad", lang=lang))
    threading.Thread(target=work, daemon=True, name="app-request").start()
    return "app-request"
