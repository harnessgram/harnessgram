"""Helper agents: small, cheap Haiku jobs that look after the bridge. Every job has its own switch and all are off.

One runner serves every job (`ask`). Route order:
1. "subscription": a headless `claude -p --model haiku` on the owner's own subscription, with no tools of any kind
   (the same lock-down the isolated reader uses), the prompt on stdin, a short timeout.
2. "api": only when `api_fallback` is on. The Anthropic API with a key the existing key helper releases. The key is
   held in memory for one request: never logged, never in the config, never echoed, never in an exception.

The api route is bounded three ways and never pay-as-you-go: a hard daily USD cap (`daily_usd_cap`, with every call
recorded in a private usage file), an `api_until` date after which the route is refused until the owner changes it
(an empty date refuses it too), and a clean stop for the rest of the day when the cap is reached, the key is missing,
the key is refused, or the API answers that credit is exhausted or billing failed. There are no retries.

Every failure returns None, and each caller then does what the bridge did before: nothing model-made is ever needed.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

from .config import private_dir, state_dir, write_private

JOBS = ("card_titles", "patrol", "ledger_capture")
ROUTES = ("subscription", "api")
DEFAULTS = {"card_titles": False, "patrol": False, "ledger_capture": False, "route": "subscription",
            "api_fallback": False, "daily_usd_cap": 0.5, "api_until": ""}
MAX_CAP = 100.0
SUBSCRIPTION_MODEL = "haiku"
API_MODEL = "claude-haiku-5-5"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "-".join(("2023", "06", "01"))  # the Messages API version, a protocol name
SUBSCRIPTION_TIMEOUT = 45.0
API_TIMEOUT = 30.0
KEY_TIMEOUT = 10.0
KEY_RETRY_AFTER = 30 * 60.0   # a missing or unreadable key is not asked for again for this long
MAX_SUBSCRIPTION_CALLS = 400  # a day's ceiling on subscription calls, so a loop can never drain the quota
MAX_PROMPT_CHARS = 6000
USAGE_FILE = "helper-agents-usage.json"
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_KEY = re.compile(r"[\x21-\x7e]{20,400}")
_BILLING = re.compile(r"credit balance|billing|insufficient|payment|purchase credits|usage limit", re.I)
log = logging.getLogger(__name__)
_key_failed_at: Dict[str, float] = {}


# --- the settings ---------------------------------------------------------------------------------

def valid_section(value) -> bool:
    """The `helper_agents` config object: only the known keys, each of the right kind."""
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        return False
    for key in (*JOBS, "api_fallback"):
        if key in value and type(value[key]) is not bool:
            return False
    if "route" in value and value["route"] not in ROUTES:
        return False
    cap = value.get("daily_usd_cap", DEFAULTS["daily_usd_cap"])
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or not 0 <= cap <= MAX_CAP:
        return False
    until = value.get("api_until", "")
    if not isinstance(until, str):
        return False
    if until:
        if not _ISO_DATE.fullmatch(until):
            return False
        try:
            date.fromisoformat(until)
        except ValueError:
            return False
    return True


def settings(cfg) -> dict:
    """The settings with every default filled in. An invalid section reads as all defaults: everything off."""
    raw = getattr(cfg, "helper_agents", None)
    out = dict(DEFAULTS)
    if valid_section(raw):
        out.update(raw)
    return out


def enabled(cfg, job: str) -> bool:
    return job in JOBS and settings(cfg)[job] is True


def _today(now: Optional[float]) -> str:
    from .session_usage import ZONE
    return datetime.fromtimestamp(time.time() if now is None else now, ZONE).date().isoformat()


def api_allowed(cfg, now: Optional[float] = None) -> bool:
    """The api route is open only with the fallback on and an `api_until` date that has not passed."""
    s = settings(cfg)
    return s["api_fallback"] is True and bool(s["api_until"]) and _today(now) <= s["api_until"]


# --- the usage record -----------------------------------------------------------------------------

@contextlib.contextmanager
def _usage_locked():
    root = private_dir(state_dir())
    with open(root / (USAGE_FILE + ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield root / USAGE_FILE


def _load(path: Path, day: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or data.get("day") != day:
        return {"day": day, "usd": 0.0, "calls": 0, "sub_calls": 0, "jobs": {}, "stop": ""}
    usd = data.get("usd")
    data["usd"] = float(usd) if isinstance(usd, (int, float)) and not isinstance(usd, bool) and math.isfinite(usd) \
        and usd >= 0 else MAX_CAP  # an unreadable total reads as spent
    for key in ("calls", "sub_calls"):
        data[key] = data[key] if type(data.get(key)) is int and data[key] >= 0 else 0
    data["jobs"] = data["jobs"] if isinstance(data.get("jobs"), dict) else {}
    data["stop"] = data["stop"] if data.get("stop") in ("", "credit", "auth", "key") else ""
    return data


def usage(now: Optional[float] = None) -> dict:
    """Today's api spend and call counts; never any prompt, answer or key."""
    day = _today(now)
    with _usage_locked() as path:
        return _load(path, day)


def _record(now: Optional[float], job: str, usd: float = 0.0, api: bool = False, stop: str = "") -> None:
    day = _today(now)
    with _usage_locked() as path:
        data = _load(path, day)
        if api:
            data["calls"] += 1
            data["usd"] = round(data["usd"] + max(0.0, usd), 6)
            data["jobs"][job] = round(float(data["jobs"].get(job, 0.0)) + max(0.0, usd), 6)
        else:
            data["sub_calls"] += 1
        if stop:
            data["stop"] = stop
        write_private(path, json.dumps(data))


def _reserve(now: Optional[float], job: str, amount: float, cap: float) -> bool:
    """Book the worst case of a call BEFORE it is made, in one locked step; False (call refused) when it would cross
    the cap, a stop is set, or the record cannot be written."""
    try:
        with _usage_locked() as path:
            data = _load(path, _today(now))
            if data["stop"] or data["usd"] + amount > cap:
                return False
            data["calls"] += 1
            data["usd"] = round(data["usd"] + amount, 6)
            data["jobs"][job] = round(float(data["jobs"].get(job, 0.0)) + amount, 6)
            write_private(path, json.dumps(data))
        return True
    except Exception:  # noqa: BLE001 - no usable record, no spend
        return False


def _settle(now: Optional[float], job: str, booked: float, actual: float) -> None:
    """Replace the booked worst case by the real cost."""
    diff = max(0.0, actual) - booked
    if diff == 0:
        return
    with contextlib.suppress(Exception):
        with _usage_locked() as path:
            data = _load(path, _today(now))
            data["usd"] = round(max(0.0, data["usd"] + diff), 6)
            data["jobs"][job] = round(max(0.0, float(data["jobs"].get(job, 0.0)) + diff), 6)
            write_private(path, json.dumps(data))


def _cost(input_tokens: int, output_tokens: int) -> Optional[float]:
    from .session_usage import price
    return price(API_MODEL, {"input_tokens": input_tokens, "output_tokens": output_tokens})


# --- the subscription route -----------------------------------------------------------------------

Runner = Callable[[Sequence[str], str, Dict[str, str], str, float], str]


def _argv(binary: str, system: str) -> list:
    """The lock-down of the isolated reader (untrusted_reader.argv) without its schema: no tools, servers, hooks,
    skills or saved session, and anything that would ask for a permission is refused."""
    return [binary, "-p", "--model", SUBSCRIPTION_MODEL, "--tools", "",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--safe-mode", "--restricted", "--disable-slash-commands", "--no-chrome",
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--no-session-persistence", "--output-format", "json", "--system-prompt", system]


def _run_claude(args, stdin, env, cwd, timeout) -> str:
    from .untrusted_reader import _run
    return _run(args, stdin, env, cwd, timeout)


def _claude_binary() -> str:
    from .accounts import claude_binary
    return claude_binary()


def _parse_cli(stdout) -> Optional[str]:
    try:
        out = json.loads(stdout)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(out, dict) or out.get("is_error") is not False or out.get("subtype") != "success":
        return None
    if out.get("permission_denials") or not isinstance(out.get("result"), str):
        return None
    return out["result"].strip() or None


def _subscription(job: str, system: str, prompt: str, now: Optional[float]) -> Optional[str]:
    from .untrusted_reader import environment
    try:
        if usage(now)["sub_calls"] >= MAX_SUBSCRIPTION_CALLS:
            return None
        _record(now, job)
        with tempfile.TemporaryDirectory(prefix="ctb-helper-") as cwd:
            os.chmod(cwd, 0o700)
            stdout = _run_claude(_argv(_claude_binary(), system), prompt, environment(), cwd, SUBSCRIPTION_TIMEOUT)
    except Exception:  # noqa: BLE001 - a timeout, a missing CLI, a full disk: the caller's non-model path
        return None
    return _parse_cli(stdout)


# --- the api route --------------------------------------------------------------------------------

def _read_key() -> Optional[str]:
    """The key from the existing key helper, in memory only."""
    helper = Path.home() / ".codex/bin/anthropic-key-helper"
    try:
        done = subprocess.run([str(helper)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=KEY_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    key = done.stdout.decode("utf-8", "ignore").strip() if done.returncode == 0 else ""
    return key if _KEY.fullmatch(key) else None


def _post(url: str, headers: dict, body: bytes, timeout: float) -> Tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:  # noqa: S310 - a fixed https URL
            return reply.status, reply.read(1_000_000)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(100_000)
        except OSError:
            return exc.code, b""


def _classify(status: int, body: bytes) -> str:
    """"ok", "credit" (billing or credit trouble), "auth" (the key is refused) or "error" (anything else)."""
    if status == 200:
        return "ok"
    text = ""
    try:
        err = json.loads(body).get("error")
        text = " ".join(str(err.get(k, "")) for k in ("type", "message")) if isinstance(err, dict) else ""
    except (ValueError, AttributeError, TypeError):
        pass
    if status == 402 or _BILLING.search(text):
        return "credit"
    if status in (401, 403):
        return "auth"
    return "error"


def _api(cfg, job: str, system: str, prompt: str, max_tokens: int, now: Optional[float]) -> Optional[str]:
    if not api_allowed(cfg, now):
        return None
    cap = float(settings(cfg)["daily_usd_cap"])
    state = usage(now)
    if state["stop"] or state["usd"] >= cap:
        return None
    worst = _cost(math.ceil((len(system) + len(prompt)) / 1.5) + 16, max_tokens)
    if worst is None:
        return None
    mono = time.monotonic()
    if mono - _key_failed_at.get("key", float("-inf")) < KEY_RETRY_AFTER:
        return None
    key = _read_key()
    if key is None:
        _key_failed_at["key"] = mono
        _record(now, job, stop="key")
        log.warning("Helper agents: no API key available; the API route is stopped for today")
        return None
    if not _reserve(now, job, worst, cap):
        return None  # the call could cross the cap, or the spend cannot be recorded: it is not made
    body = json.dumps({"model": API_MODEL, "max_tokens": max_tokens, "system": system,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    headers = {"x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"}
    try:
        status, reply = _post(API_URL, headers, body, API_TIMEOUT)
    except Exception:  # noqa: BLE001 - a timeout may still have been billed: the booked worst case stays
        return None
    finally:
        key = headers = None  # nothing keeps the key past the request
    kind = _classify(status, reply)
    if kind != "ok":
        _settle(now, job, worst, 0.0)  # a refused request costs nothing
        if kind in ("credit", "auth"):
            with contextlib.suppress(Exception):
                _record(now, job, stop=kind)
            log.warning("Helper agents: the API refused (%s); the API route is stopped for today", kind)
        return None
    try:
        data = json.loads(reply)
        used = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        counts = [used.get(k) for k in ("input_tokens", "output_tokens")]
        spent = _cost(*counts) if all(type(c) is int and c >= 0 for c in counts) else None
        _settle(now, job, worst, worst if spent is None else spent)
        text = next((b["text"] for b in data["content"] if isinstance(b, dict) and b.get("type") == "text"
                     and isinstance(b.get("text"), str)), "")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return text.strip() or None


# --- the runner -----------------------------------------------------------------------------------

def ask(cfg, job: str, system: str, prompt: str, max_tokens: int = 400, now: Optional[float] = None) -> Optional[str]:
    """One short model answer for a job, or None. Inert (nothing runs, nothing is read) while the job's switch is
    off; every failure is None. The prompt goes to the model on stdin or in the request body only."""
    try:
        if not enabled(cfg, job) or not isinstance(system, str) or not isinstance(prompt, str):
            return None
        s = settings(cfg)
        prompt = prompt[:MAX_PROMPT_CHARS]
        if s["route"] == "subscription":
            answer = _subscription(job, system, prompt, now)
            if answer is not None:
                return answer
        if s["api_fallback"]:
            return _api(cfg, job, system, prompt, max(1, min(int(max_tokens), 1000)), now)
    except Exception:  # noqa: BLE001 - a helper never breaks the bridge
        return None
    return None
