"""Several Claude Code accounts side by side.

Each extra account has its own Claude Code config folder under ~/.claude-profiles/<name>, selected
with CLAUDE_CONFIG_DIR (its login is kept there and in the Keychain by Claude Code), while settings,
hooks, skills, sessions and transcripts are shared through symlinks to ~/.claude, so the bridge sees
every session the same way. "main" is the normal ~/.claude account.

This module is the Python equivalent of a small `claude-as` shell wrapper plus the bookkeeping the
bridge needs: the account each session runs on, the account used for new sessions, the quota-hit
fallback and the per-account quota lines of the dashboard. Nothing here talks to Telegram or tmux;
inbox.py and mirror.py do that and call into these helpers.
"""
from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .config import private_dir, state_dir, write_private
from .i18n import t

MAIN = "main"
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,19}")
# Everything else in ~/.claude is shared. `sessions` IS shared on purpose: it is how the bridge
# sees live sessions of every account.
UNSHARED = (".credentials.json", ".claude.json", "daemon*", "session-env", "statsig", "*.bak*", "backups")
# What a new account's .claude.json starts with: past the first-run screens, with the main
# account's MCP servers and trusted folders, but none of its login.
SEED_KEYS = ("hasCompletedOnboarding", "lastOnboardingVersion", "mcpServers", "projects", "installMethod",
             "autoUpdates", "theme", "hasSeenAutoModeEntryWarning", "hasSeenAutoDefaultNotice")
LIMIT_COOLDOWN = 5 * 3600  # an account that hit its limit is skipped by the automatic fallback this long
LOGIN_WINDOW = 600  # seconds a started login waits for its code
# A reply that says the plan limit is reached. Deliberately not called LIMIT: mirror and fmt keep a
# chunk-size constant of that name, and shadowing it once broke every long reply.
QUOTA_HIT = re.compile(r"\A\s*(?:API Error: )?(?:You've hit your (?:[\w-]+ )?limit|Claude (?:AI )?usage limit reached|"
                       r"Credit balance is too low|You're out of (?:extra )?usage)", re.I)


def profiles_dir() -> Path:
    return Path(os.environ.get("CTB_PROFILES_DIR") or Path.home() / ".claude-profiles")


def claude_home() -> Path:
    return Path(os.environ.get("CTB_CLAUDE_HOME") or Path.home() / ".claude")


def claude_json() -> Path:
    return Path(os.environ.get("CTB_CLAUDE_JSON") or Path.home() / ".claude.json")


def valid_name(name: object) -> bool:
    return isinstance(name, str) and name != MAIN and bool(NAME.fullmatch(name))


def _read(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def names() -> List[str]:
    """"main" first, then every profile folder."""
    try:
        extra = sorted(p.name for p in profiles_dir().iterdir() if p.is_dir() and NAME.fullmatch(p.name))
    except OSError:
        extra = []
    return [MAIN] + [n for n in extra if n != MAIN]


def multiple() -> bool:
    return len(names()) > 1


def config_dir(name: str) -> Optional[Path]:
    """The CLAUDE_CONFIG_DIR of an account (None for an unknown one)."""
    if name == MAIN:
        return claude_home()
    return profiles_dir() / name if valid_name(name) and (profiles_dir() / name).is_dir() else None


# -- the account new sessions use ----------------------------------------------------------------

def _current_path() -> Path:
    return state_dir() / "account-current"


def current() -> str:
    try:
        name = _current_path().read_text(encoding="utf-8").strip()
    except OSError:
        name = MAIN
    name = name if name == MAIN or config_dir(name) else MAIN
    try:
        better = better_twins(name)
    except Exception:
        better = []
    return better[0] if better else name  # the account new sessions start on is never a stale login


def use(name: str) -> None:
    if name != MAIN and not config_dir(name):
        raise ValueError("no such account")
    write_private(_current_path(), name)


def name_taken(name: str) -> bool:
    """A name whose folder is gone but whose Keychain item (the login) or removed-profiles entry remains:
    a new profile there would be the same path, so it would look signed in as the removed account."""
    if (profiles_dir() / name).exists():
        return False
    service = keychain_service(name)
    if service and _keychain_read(service):
        return True
    try:
        return any(p.name.startswith(name + "-") and re.fullmatch(re.escape(name) + r"-\d{8}-\d{6}(?:-\d+)?", p.name)
                   for p in removed_dir().iterdir())
    except OSError:
        return False


def next_free_name(listed: List[str]) -> str:
    return next(f"account{i}" for i in range(2, 1000) if f"account{i}" not in listed and not name_taken(f"account{i}"))


def add(name: str) -> Path:
    """Create a profile folder that shares everything but the login with ~/.claude."""
    if not valid_name(name):
        raise ValueError("bad account name")
    if name_taken(name):
        raise ValueError("name was used by a removed account")
    target = private_dir(profiles_dir() / name)
    home = claude_home()
    try:
        entries = sorted(home.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if any(fnmatch.fnmatch(entry.name, pattern) for pattern in UNSHARED):
            continue
        link = target / entry.name
        if not link.exists() and not link.is_symlink():
            link.symlink_to(entry)
    seed = target / ".claude.json"
    if not seed.exists():
        source = _read(claude_json(), {})
        fd = os.open(seed, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({k: source[k] for k in SEED_KEYS if k in source}, f)
    return target


# -- e-mail of each account (cached: menus stay fast) --------------------------------------------

def _emails_path() -> Path:
    return state_dir() / "account-emails.json"


def cached_email(name: str) -> str:
    """The address the profile was last read as; '' while its login names none (an older address kept in
    the file is not who it is now, so it pairs it with no twin and labels nothing)."""
    if login_state(name) == "noemail":
        return ""
    value = _read(_emails_path(), {}).get(name, "")
    return value if isinstance(value, str) else ""


def env_for(name: str) -> Dict[str, str]:
    env = dict(os.environ)
    folder = config_dir(name)
    if name == MAIN or folder is None:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(folder)
    return env


def claude_binary() -> str:
    """The claude executable. The listener service has a short PATH without ~/.local/bin (where the
    native installer puts it), so look there too; fall back to the bare name."""
    import shutil
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "claude", Path("/opt/homebrew/bin/claude"),
                      Path("/usr/local/bin/claude"), Path.home() / ".claude" / "local" / "claude"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return "claude"


def auth_status(name: str, timeout: float = 30) -> dict:
    """`claude auth status` of an account as a dict ({} when it cannot be read)."""
    try:
        out = subprocess.run([claude_binary(), "auth", "status"], env=env_for(name), capture_output=True, text=True,
                             timeout=timeout, check=False).stdout
        data = json.loads(out)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def _lock(path: Path):
    """An exclusive lock on <path>.lock, so a read-modify-write of a small state file is atomic across
    the listener, the dashboard job and the auto-move processes. Use as `with _lock(path):`."""
    private_dir(path.parent)
    return _Locked(str(path) + ".lock")


class _Locked:
    def __init__(self, path: str):
        self.path = path
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, "a")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


def _store_email(name: str, new: str) -> str:
    """Cache `new` as the e-mail of `name`. Returns the address it REPLACED, or '' when nothing was
    cached before or the address did not change. Atomic: when several processes race, exactly one sees
    the change."""
    with _lock(_emails_path()):
        cache = _read(_emails_path(), {})
        old = cache.get(name, "")
        old = old if isinstance(old, str) else ""
        if old != new:
            cache[name] = new
            write_private(_emails_path(), json.dumps(cache))
        return old if norm_email(old) != norm_email(new) else ""


def email(name: str, refresh: bool = False) -> str:
    got = "" if refresh else cached_email(name)
    if got:
        return got
    if not refresh and login_state(name) == "noemail":
        return ""  # the login names no address: asking `claude auth status` again on every mirrored reply won't change it
    got = str(auth_status(name).get("email") or "")
    if got:
        _store_email(name, got)
    return got


# -- the login each profile REALLY has: its own OAuth token ---------------------------------------------
# `claude auth status` and .claude.json kept naming one address while the profile's token
# belonged to another account (a stale record), so three profiles were grouped as twins of an account
# whose quota was full and the real, roomy account looked exhausted. The token is the truth: the
# Keychain item Claude Code keeps per config folder is asked who it belongs to.

OAUTH_API = "https://api.anthropic.com"
OAUTH_BETA = "oauth-2025-04-20"
KEYCHAIN_SERVICE = "Claude Code-credentials"
PROFILE_HTTP_TIMEOUT = 8.0


def keychain_service(name: str) -> Optional[str]:
    """The Keychain service Claude Code stores the login of an account under: a plain name for the main
    account, and one named after a hash of the config folder for a profile."""
    if name == MAIN:
        return KEYCHAIN_SERVICE
    if not valid_name(name):
        return None
    folder = config_dir(name) or (profiles_dir() / name)  # also for a name with no folder (yet / any more)
    return KEYCHAIN_SERVICE + "-" + hashlib.sha256(str(folder).encode("utf-8")).hexdigest()[:8]


def _keychain_read(service: str, timeout: float = 5.0) -> Optional[str]:
    """The secret stored under `service` (the login JSON), None when there is none. Never logged."""
    try:
        res = subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True,
                             text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (res.stdout or "").strip()
    return out if res.returncode == 0 and out else None


def _oauth_fields(name: str) -> dict:
    service = keychain_service(name)
    raw = _keychain_read(service) if service else None
    if not raw:
        return {}
    try:
        oauth = json.loads(raw).get("claudeAiOauth") or {}
    except (ValueError, AttributeError):
        return {}
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    return oauth if isinstance(token, str) and token else {}


def oauth_login(name: str) -> Tuple[Optional[str], Optional[float]]:
    """(access token, expiresAt in epoch seconds) of an account, read from its Keychain item. Either is None
    when unavailable (expiresAt is also None when the item does not carry one). Never logged."""
    oauth = _oauth_fields(name)
    if not oauth:
        return None, None
    try:
        expires = float(oauth["expiresAt"]) / 1000.0 if oauth.get("expiresAt") else None
    except (TypeError, ValueError):
        expires = None
    return oauth["accessToken"], expires


def login_fingerprint(oauth: dict, account_id: object = None) -> str:
    """A hash that stands for the account a sign-in belongs to, never the tokens themselves. The account
    id the profile endpoint returns is stable across token refreshes, so a refresh plus a corrected label is
    not mistaken for a re-login; without one, the tokens are hashed (they rotate on refresh, so the hash
    only ever counts together with a changed address)."""
    if isinstance(account_id, str) and account_id.strip():
        return hashlib.sha256(("account|" + account_id.strip()).encode("utf-8")).hexdigest()
    blob = str(oauth.get("accessToken") or "") + "|" + str(oauth.get("refreshToken") or "")
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def access_token(name: str) -> Optional[str]:
    """The OAuth access token of an account (None when unavailable)."""
    return oauth_login(name)[0]


def valid_token(name: str, now: Optional[float] = None) -> Optional[str]:
    """The access token only while its expiresAt is still in the future. An expired (or undated) token is
    not proof of anything: Claude Code refreshes it on its next use, so callers treat it as unavailable."""
    token, expires = oauth_login(name)
    now_ts = time.time() if now is None else now
    return token if token and expires is not None and expires > now_ts else None


def _http_json(path: str, token: str, timeout: float = PROFILE_HTTP_TIMEOUT) -> Tuple[int, dict, float]:
    """GET an Anthropic OAuth endpoint. Returns (status, body, retry_after seconds); status 0 when the call
    itself failed (offline, timeout, bad answer). The token is only ever sent in the header."""
    req = urllib.request.Request(OAUTH_API + path, headers={
        "Authorization": "Bearer " + token, "anthropic-beta": OAUTH_BETA, "Accept": "application/json",
        "User-Agent": "harnessgram"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, (body if isinstance(body, dict) else {}), 0.0
    except urllib.error.HTTPError as exc:
        try:
            retry = float(exc.headers.get("Retry-After") or 0)
        except (TypeError, ValueError, AttributeError):
            retry = 0.0
        return exc.code, {}, retry
    except Exception:
        return 0, {}, 0.0


def real_profile(name: str, timeout: float = PROFILE_HTTP_TIMEOUT) -> Optional[dict]:
    """Who the profile's token belongs to: {email, plan, tier}. {"loggedOut": True} when the server rejects
    the token (401: signed out, revoked or expired; `claude auth status` still shows the stale record then).
    The fields are returned when the token has not expired. None when that cannot be told (no token, expired token, 429 rate limited, offline): the caller
    then keeps its previous answer and falls back to `claude auth status`."""
    oauth = _oauth_fields(name)
    token = oauth.get("accessToken")
    try:
        expires = float(oauth["expiresAt"]) / 1000.0 if oauth.get("expiresAt") else None
    except (TypeError, ValueError):
        expires = None
    if not token:
        return None
    status, body, _ = _http_json("/api/oauth/profile", token, min(timeout, PROFILE_HTTP_TIMEOUT))
    if status == 401:
        # Only a token that has not expired yet and is still refused means signed out; an expired (or undated)
        # one is just waiting for Claude Code to refresh it, so nothing can be said about the login.
        return {"loggedOut": True} if expires is not None and expires > time.time() else None
    if status != 200:
        return None
    account = body.get("account") if isinstance(body.get("account"), dict) else {}
    org = body.get("organization") if isinstance(body.get("organization"), dict) else {}
    mail = account.get("email") or account.get("email_address")
    if not isinstance(mail, str) or "@" not in mail:
        return None
    return {"email": mail, "plan": str(org.get("organization_type") or ""),
            "tier": str(org.get("rate_limit_tier") or ""), "fp": login_fingerprint(oauth, account.get("uuid"))}


# -- the plan each login reports ----------------------------------------------------------------------------
# One record per profile in account-plans.json: {"email", "plan", "tier", "source"}. "source" is "token" (the
# profile's own OAuth token answered: the source of truth) or "status" (`claude auth status` answered while
# the token could not). Plan names have one form, the organization type the token reports ("claude_max");
# the status word "max" is stored as "claude_max". "tier" (e.g. default_claude_max_20x) only comes from the
# token and tells Max 5x from 20x. Older files may hold a plain string per profile (a status plan, no
# address): it is read as a status record. Only plan names are kept here, never a token.

_PLAN_WORDS = ("free", "pro", "max", "team", "enterprise", "ultra")


def _plans_path() -> Path:
    return state_dir() / "account-plans.json"


def norm_plan(value: object) -> str:
    """A plan name in the one form used everywhere: lower case, "claude_" prefixed for the bare status words."""
    plan = value.strip().lower()[:40] if isinstance(value, str) else ""
    return "claude_" + plan if plan in _PLAN_WORDS else plan


def _plan_entry(value: object) -> Optional[dict]:
    """A stored entry in the one record form, None when it holds no plan. A legacy plain string is a status
    plan with no address; a legacy record without "source" was written by the token reader."""
    if isinstance(value, str):
        value = {"email": "", "plan": value, "tier": "", "source": "status"}
    if not isinstance(value, dict):
        return None
    plan = norm_plan(value.get("plan"))
    if not plan:
        return None
    mail = value.get("email") if isinstance(value.get("email"), str) else ""
    tier = value.get("tier") if isinstance(value.get("tier"), str) else ""
    source = value.get("source") if value.get("source") in ("token", "status") else "token"
    return {"email": mail, "plan": plan, "tier": tier, "source": source}


def _store_plans(found: Dict[str, Optional[dict]]) -> None:
    """Remember the plan each freshly read profile reported (None forgets it: signed out). A status plan
    never replaces a token plan of the same address: the token is the truth, the status may be stale."""
    if not found:
        return
    with _lock(_plans_path()):
        cur = _read(_plans_path(), {})
        for n, rec in found.items():
            new = _plan_entry(rec)
            if new is None:
                cur.pop(n, None)
                continue
            old = _plan_entry(cur.get(n))
            if (new["source"] == "status" and old and old["source"] == "token"
                    and norm_email(old["email"]) == norm_email(new["email"])):
                continue
            cur[n] = new
        write_private(_plans_path(), json.dumps(cur))


def _store_plan(name: str, mail: str, plan: str, tier: str, source: str = "token") -> None:
    _store_plans({name: {"email": mail, "plan": plan, "tier": tier, "source": source}})


def plan_record(name: str) -> Optional[dict]:
    """The profile's cached plan record (see above), only while it belongs to the address the profile is
    cached as now (a record with no address, legacy or from a login that names none, always counts)."""
    rec = _plan_entry(_read(_plans_path(), {}).get(name))
    if rec and rec["email"] and norm_email(rec["email"]) != norm_email(cached_email(name)):
        return None
    return rec


def known_plan(name: str) -> Optional[dict]:
    """The plan read from the profile's own token, only while it still matches the cached address."""
    rec = plan_record(name)
    if rec and rec["source"] == "token" and norm_email(rec["email"]) == norm_email(cached_email(name)):
        return rec
    return None


def cached_plan(name: str) -> str:
    """The plan name the profile's login last reported, in the one form ("claude_pro"); '' when unknown."""
    rec = plan_record(name)
    return rec["plan"] if rec else ""


def _fingerprints_path() -> Path:
    return state_dir() / "account-fingerprints.json"


def forget_account_data(name: str, now: Optional[float] = None) -> None:
    """Drop what was recorded for the account a profile USED to be: its usage reading and its limit hit
    belong to the previous login and would make the new one look full (or empty). Only called when the
    login really changed (see refresh_emails). A limit hit newer than LIMIT_COOLDOWN is never dropped: it may
    be the one just recorded, and forgetting it could send a session to the account that just ran out."""
    now_ts = time.time() if now is None else now
    try:
        usage_path(name).unlink()
    except OSError:
        pass
    with _lock(_limits_path()):
        hits = _read(_limits_path(), {})
        try:
            recent = 0 <= now_ts - float(hits.get(name, 0) or 0) < LIMIT_COOLDOWN
        except (TypeError, ValueError):
            recent = False
        if not recent and hits.pop(name, None) is not None:
            write_private(_limits_path(), json.dumps(hits))


def _read_identity(name: str, timeout: float) -> dict:
    """The profile's login as `claude auth status` would show it, but from its token first."""
    got = real_profile(name, timeout)
    if got and got.get("loggedOut"):
        return {"loggedIn": False}
    if got:
        return {"email": got["email"], "loggedIn": True, "_plan": got["plan"], "_tier": got["tier"], "_fp": got["fp"]}
    # The token could not answer this time (429, offline, expired). When it already told us who this profile
    # is, keep that: `claude auth status` may still show a stale record and flip the address back (main).
    # Unless .claude.json now names another address than the one the token confirmed: that may be a real
    # re-login the token could not confirm yet, so `claude auth status` gets to look.
    plan = known_plan(name)
    if plan and norm_email(claimed_email(name)) in ("", norm_email(plan.get("email"))):
        return {"email": cached_email(name), "loggedIn": True}
    return auth_status(name, timeout)


# -- who each profile is really signed in as ----------------------------------------------------
# A profile can be signed in as another account in a terminal; a cached address then goes stale, so the menu
# showed two rows for one real login (the exhausted one) and an auto-move "moved" a session onto the account
# it had just left. The address is therefore re-read
# periodically and right before a move is planned, and profiles that share an address are one account.

IDENTITY_EVERY = 600.0  # seconds between two reads of the same profile's login
IDENTITY_HINT_EVERY = 120.0  # ...when .claude.json already names a different address than the cache
IDENTITY_FORCE_GAP = 30.0  # a forced read never repeats sooner than this
IDENTITY_OUT_EVERY = 60.0  # a never-signed-in profile is re-read this often: the sign-in may have just finished
IDENTITY_TIMEOUT = 10.0  # one `claude auth status`
IDENTITY_QUICK_TIMEOUT = 5.0  # ...on the paths a person waits on (limit offer, a move)


def _checked_path() -> Path:
    return state_dir() / "account-emails-checked.json"


def norm_email(value: object) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def claimed_email(name: str) -> str:
    """The address the profile's own .claude.json names (a free local read, no process). A hint only: a
    copied profile can carry another account's record, so the token (see real_profile) has the last word."""
    try:
        path = claude_json() if name == MAIN else (profiles_dir() / name / ".claude.json")
        return str((_read(path, {}).get("oauthAccount") or {}).get("emailAddress") or "")
    except Exception:
        return ""


def _status_path() -> Path:
    return state_dir() / "account-status.json"


def login_state(name: str) -> str:
    """What the last read of the profile's login showed: 'out' (the status said it is not logged in),
    'unreadable' (no usable answer: timeout, crash), 'noemail' (logged in, but the login names no address)
    or '' (logged in, or never read)."""
    value = _read(_status_path(), {}).get(name, "")
    return value if value in ("out", "unreadable", "noemail") else ""


def sign_in_unfinished(name: str) -> bool:
    """A profile whose sign-in was never completed: the login reads as signed out and the bridge has never
    known an address for it (new account, the link was not finished). Different from an account that was
    signed in once and has signed out since. A login the owner is in the middle of counts too."""
    pending = login_pending()
    if pending and pending.get("name") == name:
        return True
    return login_state(name) == "out" and not cached_email(name)


def logged_out(name: str) -> bool:
    """Not signed in: the last read said so, or the sign-in is not finished. Never a target for a move."""
    return login_state(name) == "out" or sign_in_unfinished(name)


def _set_login_states(states: Dict[str, str]) -> None:
    with _lock(_status_path()):
        cur = _read(_status_path(), {})
        for n, v in states.items():
            if v:
                cur[n] = v
            else:
                cur.pop(n, None)
        write_private(_status_path(), json.dumps(cur))


def forget_check(name: str) -> None:
    """Make the next read of this profile due at once (it was just signed in)."""
    with _lock(_checked_path()):
        cur = _read(_checked_path(), {})
        if cur.pop(name, None) is not None:
            write_private(_checked_path(), json.dumps(cur))


def _claims_seen_path() -> Path:
    return state_dir() / "account-claims-seen.json"


NEW_PROFILE_AGE = 3600.0  # a profile folder this young may still be waiting for its first sign-in


def _profile_age(name: str, now_ts: float) -> float:
    """Seconds since the profile folder was created (its birth time, else its last change)."""
    try:
        st = (profiles_dir() / name).stat()
        return now_ts - float(getattr(st, "st_birthtime", None) or st.st_mtime)
    except OSError:
        return float("inf")


def _sign_in_may_finish(name: str, now_ts: float) -> bool:
    """A sign-in is under way (within LOGIN_WINDOW of its start) or the profile was only just created."""
    pending = login_pending()
    if pending and pending.get("name") == name:
        return True
    return _profile_age(name, now_ts) < NEW_PROFILE_AGE


def refresh_emails(force: bool = False, now: Optional[float] = None, timeout: float = IDENTITY_TIMEOUT,
                   only: Optional[List[str]] = None) -> List[Tuple[str, str, str]]:
    """Re-read the login of each profile (from its own OAuth token, else `claude auth status`; all in
    parallel, bounded by `timeout`) and update the cache. Throttled to one read per profile per IDENTITY_EVERY (sooner when .claude.json names
    another address than the cache); `force` skips that but still honours IDENTITY_FORCE_GAP.
    Returns [(profile, old, new)] for every address that CHANGED (a first fill is not a change).
    A status that says "not logged in" marks the profile logged out (it is then no twin and no move
    target, whatever address it used to have); one that cannot be read at all keeps the cached address
    but is remembered as unreadable, so the address `.claude.json` names counts too for twin checks."""
    from concurrent.futures import ThreadPoolExecutor, wait
    now_ts = time.time() if now is None else now
    stamps = _read(_checked_path(), {})
    seen = _read(_claims_seen_path(), {})
    due = []
    for name in (only if only is not None else names()):
        try:
            gap = now_ts - float(stamps.get(name, 0) or 0)
        except (TypeError, ValueError):
            gap = IDENTITY_EVERY
        if gap < 0:
            gap = IDENTITY_EVERY  # a clock that went backwards must not freeze the check
        cached, hint = norm_email(cached_email(name)), norm_email(claimed_email(name))
        # A .claude.json that disagrees with the cache brings the next read forward, but only once per value:
        # a record that stays stale next to a good token must not make every read repeat every two minutes.
        hinted = bool(hint and hint != cached and hint != norm_email(seen.get(name)))
        need = IDENTITY_FORCE_GAP if force else IDENTITY_HINT_EVERY if hinted else IDENTITY_EVERY
        if login_state(name) == "out" and not cached and _sign_in_may_finish(name, now_ts):
            need = min(need, IDENTITY_OUT_EVERY)  # a new account whose sign-in may have just finished: do not wait 10 min
        if gap >= need:
            due.append(name)
    if not due:
        return []
    pool = ThreadPoolExecutor(max_workers=len(due))
    changes: List[Tuple[str, str, str]] = []
    states: Dict[str, str] = {}
    claims_ok: Dict[str, str] = {}
    fingerprints = _read(_fingerprints_path(), {})
    new_fps: Dict[str, str] = {}
    relogin: Dict[str, bool] = {}
    plans: Dict[str, Optional[dict]] = {}
    try:
        futures = {n: pool.submit(_read_identity, n, timeout) for n in due}
        wait(list(futures.values()), timeout=timeout + 1)
        for n, fut in futures.items():
            data: dict = {}
            if fut.done():
                try:
                    data = fut.result() or {}
                except Exception:
                    data = {}
            got = str(data.get("email") or "")
            status_plan = str(data.get("subscriptionType") or "")
            # Only an answer that does not say "not logged in" is a login; a status without a plan keeps
            # the cached one.
            if got and data.get("loggedIn") is not False:
                states[n] = ""
                old = _store_email(n, got)
                fp = str(data.get("_fp") or "")
                if fp:
                    prev_fp = fingerprints.get(n)
                    new_fps[n] = fp
                    relogin[n] = bool(prev_fp and prev_fp != fp)
                if data.get("_plan"):
                    plans[n] = {"email": got, "plan": str(data["_plan"]), "tier": str(data.get("_tier") or ""),
                                "source": "token"}
                elif status_plan:
                    plans[n] = {"email": got, "plan": status_plan, "tier": "", "source": "status"}
                claims_ok[n] = claimed_email(n)
                if old:
                    changes.append((n, old, got))
                    if relogin.get(n):  # a corrected label alone (same sign-in) keeps its readings and limit hit
                        forget_account_data(n, now_ts)
            elif data.get("loggedIn") is False:
                states[n] = "out"
                plans[n] = None
            elif data.get("loggedIn") is True:
                # Signed in, but the status names no address (main). When the token already
                # confirmed who the profile is (its token could not answer this time), that address stands,
                # exactly as on a failed read: otherwise it would drop out of its twin group and come back on
                # the next good token read, re-posting the duplicate notice every cycle and allowing moves
                # within one account. Only with no token-confirmed address is the profile its own account
                # (no twin, no "sign in again as" advice) until it names one.
                if not known_plan(n):
                    states[n] = "noemail"
                    if status_plan:
                        plans[n] = {"email": "", "plan": status_plan, "tier": "", "source": "status"}
                claims_ok[n] = claimed_email(n)  # this claim was looked at: do not re-read early for it again
            elif login_state(n) not in ("out", "noemail") and not known_plan(n):
                states[n] = "unreadable"
            # else: a timed-out or failed read never overwrites what is known (signed out, no address, or an
            # address the token confirmed); the identity and its notices do not flip on a hiccup.
    finally:
        pool.shutdown(wait=False)
    _set_login_states(states)
    _store_plans(plans)
    if new_fps:
        with _lock(_fingerprints_path()):
            merged_fps = _read(_fingerprints_path(), {})
            merged_fps.update(new_fps)
            write_private(_fingerprints_path(), json.dumps(merged_fps))
    if claims_ok:
        with _lock(_claims_seen_path()):
            merged_claims = _read(_claims_seen_path(), {})
            merged_claims.update({n: norm_email(c) for n, c in claims_ok.items()})
            write_private(_claims_seen_path(), json.dumps(merged_claims))
    with _lock(_checked_path()):
        merged = _read(_checked_path(), {})
        merged.update({n: now_ts for n in due})
        write_private(_checked_path(), json.dumps(merged))
    return changes


# -- twins with different plans ---------------------------------------------------------------------
# A profile can hold a login from before the account's plan upgrade. `claude auth status`
# on it said subscriptionType "pro" while the same address in another profile said "max", and the old
# login's own 5-hour limit kept stopping sessions although the account had plenty of room. Profiles signed
# in as one address are twins, but the twin whose login reports the bigger plan is the one to use; the
# lower one is "stale". The plans come from the records above (token first, else the status).

def plan_rank(name: str) -> Optional[int]:
    """How big the plan the profile's own login reports is (free 0, pro 1, max/team/enterprise/ultra 2),
    None when no plan is known. Only the login's word counts: .claude.json keeps the plan
    of the sign-in it was copied from and does not change when the plan does."""
    plan = cached_plan(name)
    if not plan:
        return None
    if any(w in plan for w in ("max", "ultra", "team", "enterprise")):
        return 2
    if "pro" in plan:
        return 1
    return 0 if "free" in plan else None


def confirmed_twins(name: str) -> List[str]:
    """The twins whose cached address (what the token, or a logged-in status, said) is `name`'s own: not
    those grouped with it only through the .claude.json hint of a profile whose last read failed. A
    profile whose login names no address has none."""
    mine = norm_email(cached_email(name))
    if not mine:
        return []
    return [n for n in twins(name) if norm_email(cached_email(n)) == mine]


def better_twins(name: str) -> List[str]:
    """Logged-in confirmed twins whose login reports a strictly bigger plan than `name`'s (best first).
    Non-empty means `name` is a stale login of the account."""
    mine = plan_rank(name)
    if mine is None:
        return []
    rank = lambda n: -1 if plan_rank(n) is None else plan_rank(n)  # noqa: E731
    better = [n for n in confirmed_twins(name) if not logged_out(n) and rank(n) > mine]
    return sorted(better, key=lambda n: (-(plan_rank(n) or 0), names().index(n)))


def stale_twins(group: List[str]) -> List[Tuple[str, str]]:
    """[(stale profile, the twin used instead)] of a group of profiles signed in as one address."""
    out = []
    for n in group:
        if logged_out(n):
            continue
        best = better_twins(n)
        if best:
            out.append((n, best[0]))
    return out


def quota_twins(name: str) -> List[str]:
    """The twins that share `name`'s quota: those whose login reports the same plan (or no plan). A
    stale login has its own, smaller limit; neither its readings nor its limit hits count for the better
    twin, and the better twin's room does not rescue the stale one."""
    mine = plan_rank(name)
    out = []
    for n in twins(name):
        theirs = plan_rank(n)
        if mine is None or theirs is None or mine == theirs:
            out.append(n)
    return out


def login_addresses(name: str) -> set:
    """Every address the profile may be signed in as, for twin checks: none when it is logged out; its
    cached address; plus the one `.claude.json` names when the last read of its login failed (the
    incident: the profile was re-signed, `claude auth status` could not be read, the cache was stale)."""
    state = login_state(name)
    if state == "out":
        return set()
    out = {norm_email(cached_email(name))} - {""}
    if state == "unreadable":
        out |= {norm_email(claimed_email(name))} - {""}
    return out


def real_accounts() -> List[List[str]]:
    """Profiles grouped by the account they are signed in as, in menu order. A profile that is logged
    out or whose address is unknown is its own group."""
    order = names()
    groups: List[Tuple[List[str], set]] = []
    for n in order:
        addrs = login_addresses(n)
        hit = [g for g in groups if addrs & g[1]]
        if not hit:
            groups.append(([n], set(addrs)))
            continue
        first = hit[0]
        first[0].append(n)
        first[1].update(addrs)
        for other in hit[1:]:
            first[0].extend(other[0])
            first[1].update(other[1])
            groups.remove(other)
    out = [sorted(g[0], key=order.index) for g in groups]
    return sorted(out, key=lambda g: order.index(g[0]))


def twins(name: str) -> List[str]:
    """The other profiles signed in as the same account (one real account, one shared quota)."""
    for group in real_accounts():
        if name in group:
            return [n for n in group if n != name]
    return []


def same_login(a: str, b: str) -> bool:
    return a == b or b in twins(a)


def usable_in(group: List[str]) -> Optional[str]:
    """The profile of a group a session can be moved to: the logged-in one whose login reports the
    biggest plan (never a stale older login), else the first logged in."""
    live = [n for n in group if not logged_out(n)]
    if not live:
        return None
    return max(live, key=lambda n: (plan_rank(n) if plan_rank(n) is not None else -1, -live.index(n)))


def shared_address(group: List[str]) -> str:
    """The address the profiles of a group share (any one of them when it is only a possible match)."""
    counts: Dict[str, int] = {}
    for n in group:
        for a in login_addresses(n):
            counts[a] = counts.get(a, 0) + 1
    best = max(counts, key=lambda a: counts[a]) if counts else ""
    return best or norm_email(cached_email(group[0]))


def duplicate_groups() -> List[List[str]]:
    return [g for g in real_accounts() if len(g) > 1]


def label(name: str) -> str:
    """Short, safe label of an account: the part of its e-mail before the @, else its name."""
    mail = cached_email(name)
    return mail.split("@")[0] if mail else name


def account_from_env(env: Optional[dict] = None) -> Optional[str]:
    """The known account CLAUDE_CONFIG_DIR points to, or 'main' for ~/.claude; None when unset or unknown."""
    folder = ((env if env is not None else os.environ).get("CLAUDE_CONFIG_DIR") or "").rstrip("/")
    if folder:
        try:
            p = Path(folder).expanduser()
            if p.parent == profiles_dir():
                cand = p.name
                if config_dir(cand):
                    return cand
            if p == claude_home().expanduser():
                return MAIN
        except (OSError, ValueError):
            pass
    return None


def name_from_env(env: Optional[dict] = None) -> str:
    """The account a hook runs on: Claude Code gives hooks the session's own CLAUDE_CONFIG_DIR."""
    return account_from_env(env) or MAIN


# -- removing an account ------------------------------------------------------------------------------------

class _OpsLock:
    def __init__(self, exclusive: bool, blocking: bool):
        self.exclusive, self.blocking, self.fh = exclusive, blocking, None

    def __enter__(self):
        private_dir(state_dir())
        self.fh = open(state_dir() / "account-ops.lock", "a")
        mode = (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | (0 if self.blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(self.fh, mode)
        except OSError:
            self.fh.close()
            raise
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


def ops_lock(exclusive: bool = False, blocking: bool = True) -> _OpsLock:
    """The lock a removal (exclusive, never waits) and a move or rebalance (shared) hold, so a profile is
    never taken away while a move onto it is being planned or run. Raises OSError when an exclusive
    non-blocking request cannot be had."""
    return _OpsLock(exclusive, blocking)


def login_terminal_running(name: str) -> bool:
    """A sign-in terminal for this profile is still alive: its tmux session, or any process running
    `auth login` with the profile's CLAUDE_CONFIG_DIR."""
    try:
        if subprocess.run(["tmux", "has-session", "-t", "acct-login-" + name], capture_output=True, timeout=5,
                          check=False).returncode == 0:
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    folder = config_dir(name)
    if folder is None:
        return False
    try:
        out = subprocess.run(["ps", "eww", "-A", "-o", "command="], capture_output=True, text=True, timeout=5,
                             check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    pat = re.compile(r"CLAUDE_CONFIG_DIR=" + re.escape(str(folder)) + r"/?(?:\s|$)")
    return any("auth login" in line and pat.search(line) for line in out.splitlines())


def removed_dir() -> Path:
    return state_dir() / "removed-profiles"


def _pid_uses_folder(pid: int, folder: Path) -> bool:
    """Whether the process runs with CLAUDE_CONFIG_DIR pointing at `folder` (read from its environment)."""
    try:
        out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return re.search(r"CLAUDE_CONFIG_DIR=" + re.escape(str(folder)) + r"/?(?:\s|$)", out) is not None


def sessions_using(name: str) -> List[str]:
    """Ids of the running sessions that use this account: by the bridge's record of the session, or by the
    config folder in the process environment."""
    folder = config_dir(name)
    out: List[str] = []
    for rec in live_session_records():
        sid = str(rec.get("sessionId") or "")
        try:
            pid = int(rec.get("pid", 0))
        except (TypeError, ValueError):
            pid = 0
        if sid and sid in out:
            continue
        mine = bool(sid) and _read(_sessions_path(), {}).get(sid) == name
        if not mine and folder is not None and pid:
            mine = _pid_uses_folder(pid, folder)
        if mine:
            out.append(sid or str(pid))
    return out


def removal_blocker(name: str) -> Tuple[str, List[str]]:
    """Why an account cannot be removed now: ('', []) when it can; else ('unknown'|'main'|'login'|'sessions',
    the sessions using it)."""
    if name == MAIN:
        return "main", []
    if config_dir(name) is None:
        return "unknown", []
    pending = login_pending()
    if (pending and pending.get("name") == name) or login_terminal_running(name):
        return "login", []
    live = sessions_using(name)
    return ("sessions", live) if live else ("", [])


def remove(name: str, now: Optional[float] = None) -> Path:
    """Take an account out of the bridge WITHOUT deleting anything: its profile folder moves to
    <state>/removed-profiles/<name>-<timestamp> (move it back to restore it), and everything the bridge
    cached about it is dropped. Raises ValueError when it must not be removed (see removal_blocker)."""
    try:
        with ops_lock(exclusive=True, blocking=False):
            return _remove_locked(name, now)
    except BlockingIOError:
        raise ValueError("moving")


def _remove_locked(name: str, now: Optional[float]) -> Path:
    why, _ = removal_blocker(name)
    if why:
        raise ValueError(why)
    folder = config_dir(name)
    was_default = _current_path_file() == name
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() if now is None else now))
    target = private_dir(removed_dir()) / f"{name}-{stamp}"
    n = 1
    while target.exists() or target.is_symlink():
        n += 1
        target = removed_dir() / f"{name}-{stamp}-{n}"
    shutil.move(str(folder), str(target))
    if was_default:
        use(MAIN)
    for path in (_emails_path(), _checked_path(), _status_path(), _limits_path(), _plans_path(), _claims_seen_path(),
                 _usage_state_path(), _sessions_path(), _fingerprints_path()):
        with _lock(path):
            cache = _read(path, {})
            if path == _sessions_path():
                changed = any(v == name for v in cache.values())
                cache = {k: v for k, v in cache.items() if v != name}
            else:
                changed = cache.pop(name, None) is not None
            if changed:
                write_private(path, json.dumps(cache))
    try:
        usage_path(name).unlink()
    except OSError:
        pass
    return target


def _current_path_file() -> str:
    try:
        return _current_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# -- the account of each session ---------------------------------------------------------------------

def _sessions_path() -> Path:
    return state_dir() / "session-accounts.json"


def account_for(session: Optional[str] = None) -> str:
    """The account a session runs on. A known session keeps the account it started on; a new one
    takes the current choice."""
    if session:
        known = _read(_sessions_path(), {}).get(session)
        if isinstance(known, str) and (known == MAIN or config_dir(known)):
            return known
    return current()


def record(session: str, name: str) -> None:
    known = _read(_sessions_path(), {})
    known[session] = name
    write_private(_sessions_path(), json.dumps(dict(list(known.items())[-300:])))


CHROME_HOST = "com.anthropic.claude_code_browser_extension.json"


def chrome_available() -> bool:
    """True when the Claude in Chrome extension's native host is installed for Claude Code. A session
    only gets the browser tools when started with --chrome, so every session the bridge starts or
    resumes asks for them; without the extension the flag is left out."""
    hosts = Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts"
    try:
        return (hosts / CHROME_HOST).is_file()
    except OSError:
        return False


def shell_command(session: Optional[str] = None, remember: bool = False, replace: bool = True) -> str:
    """The shell command that starts Claude Code on the session's account. With replace the shell
    is replaced by Claude (exec)."""
    name = account_for(session)
    if remember and session:
        record(session, name)
    folder = config_dir(name)
    run = ("exec claude" if replace else "claude") + (" --chrome" if chrome_available() else "")
    if name == MAIN or folder is None:
        # Without extra accounts nothing is changed (a CLAUDE_CONFIG_DIR of the user's own stays);
        # with them, "main" must clear an account folder inherited from another session.
        return ("unset CLAUDE_CONFIG_DIR; " + run) if multiple() else run
    return f"CLAUDE_CONFIG_DIR={shlex.quote(str(folder))} {run}"


# -- automatic fallback on a quota limit ------------------------------------------------------------------

def _auto_off_path() -> Path:
    return state_dir() / "account-auto-off"


def auto_enabled() -> bool:
    from .config import load, ConfigError
    try:
        return load().auto_account_switch
    except ConfigError:
        return False


def toggle_auto() -> bool:
    from .config import load, config_path, save_changes
    value = not load().auto_account_switch
    save_changes({}, {"auto_account_switch": value}, config_path())
    return value


def _limits_path() -> Path:
    return state_dir() / "account-limits.json"


def note_limit(name: str, now: Optional[float] = None) -> None:
    hit = _read(_limits_path(), {})
    hit[name] = time.time() if now is None else now
    write_private(_limits_path(), json.dumps(hit))


# A limit move that had to wait for background work: retried by the limit loop, announced once.
DEFER_TTL = 6 * 3600
DEFER_RETRY = 60.0


def _deferred_path(session: str) -> Path:
    return private_dir(state_dir() / "move-deferred") / (re.sub(r"[^A-Za-z0-9._-]", "_", session)[:120] or "_")


def defer_move(session: str, target: str, now: Optional[float] = None, origin: Optional[str] = None,
               limit: Optional[dict] = None, proc: Optional[Tuple[int, str]] = None) -> bool:
    """Remember that this session's move waits for its background work. True the first time for a limit
    only, so the owner hears it once however often the move is tried again; a new limit (another id)
    starts a fresh record. `origin` is the account that hit the limit. `proc` is (pid, start time) of the
    process that ignored /exit: a later retry may type into the window only while that same process is
    still the one running there (the owner may since have run /clear or /resume in it)."""
    now_ts = time.time() if now is None else now
    old = deferred_move(session, now_ts)
    lid = (limit or {}).get("id") or (old or {}).get("limit_id")
    if old and lid != old.get("limit_id"):
        old = None
    write_private(_deferred_path(session), json.dumps({
        "target": target, "since": (old or {}).get("since", now_ts), "tried": now_ts,
        "origin": origin or (old or {}).get("origin"), "limit_id": lid,
        "reset": (limit or {}).get("reset") or (old or {}).get("reset"),
        "pid": proc[0] if proc else (old or {}).get("pid"),
        "started": proc[1] if proc else (old or {}).get("started")}))
    return old is None


def deferred_move(session: str, now: Optional[float] = None) -> Optional[dict]:
    """The waiting move, None once its limit has reset or it is older than DEFER_TTL."""
    now_ts = time.time() if now is None else now
    data = _read(_deferred_path(session), {})
    if not isinstance(data, dict) or not data.get("target") or now_ts - float(data.get("since") or 0) > DEFER_TTL:
        return None
    try:
        if data.get("reset") and now_ts >= float(data["reset"]):
            return None
    except (TypeError, ValueError):
        pass
    return data


def clear_deferred(session: str) -> None:
    try:
        _deferred_path(session).unlink()
    except OSError:
        pass


def is_big_plan(name: str) -> bool:
    """True for the Max 20x / Ultra account. The plan read from the profile's own token wins when there is
    one (Max, and not the 5x tier). Otherwise a stale older login of it (its own status reports a smaller
    plan) is not big, whatever its .claude.json still says; twins that report the same plan count as one."""
    plan = known_plan(name)
    if plan:
        return _token_big(plan)
    try:
        return any(_profile_big(n) for n in [name] + quota_twins(name))
    except Exception:
        return False


def _token_big(plan: dict) -> bool:
    return plan.get("plan") == "claude_max" and "5x" not in str(plan.get("tier") or "").replace("20x", "")


def _profile_big(name: str) -> bool:
    """One profile's word: its token plan when known; else its .claude.json names the 20x tier (trusted only
    when its emailAddress matches the cached email: a copied profile can carry another account's record)
    and its login's status does not report a smaller plan."""
    plan = known_plan(name)
    if plan:
        return _token_big(plan)
    rank = plan_rank(name)
    if rank is not None and rank < 2:
        return False
    try:
        path = claude_json() if name == MAIN else (profiles_dir() / name / ".claude.json")
        data = _read(path, {})
        oauth = data.get("oauthAccount") or {}
        mail = cached_email(name)
        tier = str(oauth.get("organizationRateLimitTier") or "")
        addr = str(oauth.get("emailAddress") or "")
        return bool(mail and addr == mail and "20x" in tier)
    except Exception:
        return False


big_account = is_big_plan


def used(name: str, now: Optional[float] = None) -> float:
    """The fullest window of the real account behind `name` (0-100). Profiles signed in as the same
    address share one quota, so the fullest reading of any of them counts for all."""
    return max([_used_one(n, now) for n in [name] + quota_twins(name)])


def _used_one(name: str, now: Optional[float] = None) -> float:
    """The profile's own fullest window still running (0-100).
    A limit hit makes the account count as full (100) for 5 hours, EXCEPT when its
    5-hour window reset after the hit, or a usage reading newer than the hit shows it below 95%."""
    now_ts = time.time() if now is None else now
    u = _read(usage_path(name), {})
    hit = _read(_limits_path(), {})
    t_hit = float(hit.get(name, 0) or 0)
    running = [float(u.get(k) or 0) for k in ("five_hour", "weekly") if float(u.get(k + "_resets") or 0) > now_ts]
    level = max(running) if running else (0.0 if u else 50.0)
    try:
        five_now, five_reset = float(u.get("five_hour") or 0), u.get("five_hour_resets")
    except (TypeError, ValueError):
        five_now, five_reset = 0.0, None
    if five_now > 0 and not five_reset:
        # A 5-hour reading with no reset time cannot be placed in time: it counted as 0% and made a spent
        # account look free. Unknown ranks behind every account with known room.
        level = max(level, 50.0, five_now)
    if 0 <= now_ts - t_hit < LIMIT_COOLDOWN:
        at = 0.0
        try:
            at_raw = u.get("at")
            if at_raw:
                at = datetime.fromisoformat(str(at_raw).replace("Z", "+00:00")).timestamp()
        except Exception:
            at = 0.0
        reset = float(u.get("five_hour_resets") or 0)
        if not ((t_hit < reset < now_ts) or (at > t_hit + 60 and level < 95)):
            return 100.0
    return level


def spent(name: str, now: Optional[float] = None) -> bool:
    """The real account behind `name` is out: any profile signed in as the same address shows it."""
    now_ts = time.time() if now is None else now
    return any(_spent_one(n, now_ts) for n in [name] + quota_twins(name))


def _spent_one(name: str, now: Optional[float] = None) -> bool:
    """The profile's own last reading shows a full 5-hour or weekly window that has not reset yet.
    A weekly-spent account was picked once by the fallback and stopped at once."""
    now_ts = time.time() if now is None else now
    u = _read(usage_path(name), {})
    for key in ("five_hour", "weekly"):
        try:
            if float(u.get(key) or 0) >= 100 and float(u.get(key + "_resets") or 0) > now_ts:
                return True
        except (TypeError, ValueError):
            continue
    return _used_one(name, now_ts) >= 100


def limited_account(session: str, env: Optional[dict] = None) -> str:
    """The account a limit notice came from: the process's own, else the session's record, else main."""
    here = account_from_env(env)
    if here is None:
        known = _read(_sessions_path(), {}).get(session) if session else None
        here = known if isinstance(known, str) and (known == MAIN or config_dir(known)) else MAIN
    return here


def origin_account(session: str) -> str:
    """The account a session's process really runs on: its record, else the process account, else main.
    Never `account_for`, whose fallback is the default for NEW sessions."""
    known = _read(_sessions_path(), {}).get(session) if session else None
    if isinstance(known, str) and (known == MAIN or config_dir(known)):
        return known
    return limited_account(session)


GROUP_WINDOW = 10.0  # seconds: limits recorded this close with the same reset are one exhausted group


def _five_reset(name: str) -> float:
    try:
        return float(_read(usage_path(name), {}).get("five_hour_resets") or 0)
    except (TypeError, ValueError):
        return 0.0


def _exhausted_group(here: str, others: List[str], now_ts: float) -> set:
    """Accounts that ran out together with `here`: their limits were recorded within a few seconds of each
    other and show the same 5-hour reset. Moving to one of them lands on a limit that is already hit
    (two sessions moved to the second account of the same exhausted pair)."""
    hits = _read(_limits_path(), {})
    mine, reset = float(hits.get(here, 0) or 0) or now_ts, _five_reset(here)
    if reset <= now_ts:  # a shared reset that has passed means nothing is exhausted any more
        return set()
    out = set()
    for n in others:
        theirs = float(hits.get(n, 0) or 0)
        if theirs and abs(theirs - mine) <= GROUP_WINDOW and abs(_five_reset(n) - reset) <= 1:
            out.add(n)
    return out


def distinct_others(here: str) -> List[str]:
    """The profiles a session on `here` can really move to: never one signed in as the same account as
    `here` (the account it is leaving) unless `here` is a stale older login and it is the twin with the
    bigger plan, never a logged-out one, and one profile per other real account (its twin with the
    biggest plan that is logged in)."""
    out: List[str] = []
    for group in real_accounts():
        if here in group:
            continue
        target = usable_in(group)
        if target:
            out.append(target)
    # A session on a stale older login may move to the twin whose login reports the bigger plan (that
    # is the same account with the right plan); the reverse never happens.
    for better in better_twins(here)[:1]:
        if better not in out:
            out.append(better)
    return out


def fallback_plan(session: str, now: Optional[float] = None, env: Optional[dict] = None) -> dict:
    """Which account ran out and which are left: {here, others, fresh, auto}. `fresh` are the others
    that did not hit their own limit within the last five hours, sorted by (is_big_plan, used%).
    Profiles signed in as the same account are one account: `others` never holds a twin of `here`.
    Callers about to plan a move re-read the logins first (identity.check)."""
    now_ts = time.time() if now is None else now
    here = limited_account(session, env)
    others = distinct_others(here)
    group = _exhausted_group(here, others, now_ts)
    fresh = [n for n in others if n not in group and not spent(n, now_ts)]
    fresh.sort(key=lambda n: (is_big_plan(n), used(n, now_ts)))
    return {"here": here, "others": others, "fresh": fresh, "auto": auto_enabled()}


def pick_account(cur: str, small_first: bool = False, now: Optional[float] = None) -> Optional[str]:
    """The account with the most room when cur is at 95% or hit a limit in the last 5 hours.
    small_first: also leave the big plan for a smaller one with room (<80%), so Pro quota that would
    otherwise reset unused is spent first."""
    now_ts = time.time() if now is None else now
    all_names = distinct_others(cur)
    if small_first and is_big_plan(cur):
        small = sorted((n for n in all_names if not is_big_plan(n) and used(n, now_ts) < 80),
                       key=lambda n: used(n, now_ts))
        if small:
            return small[0]
    if used(cur, now_ts) < 95:
        return None
    room = sorted((n for n in all_names if used(n, now_ts) < 90),
                  key=lambda n: (is_big_plan(n), used(n, now_ts)))
    return room[0] if room else None


def roomier_account(cur: str, now: Optional[float] = None) -> Optional[str]:
    """Another account to use when cur is (nearly) out of quota, smaller plans first; None when cur is fine."""
    return pick_account(cur, small_first=True, now=now)


def check_default_account(now: Optional[float] = None) -> Optional[str]:
    """New sessions start on the default account; when it has no room (>=95%), or when on the
    big plan while a smaller plan has room (<80%), switch default to the roomiest account, smaller plans first.
    Does nothing when automatic account switching is off."""
    if not auto_enabled():
        return None
    cur = current()
    target = roomier_account(cur, now=now)
    if target and target != cur:
        use(target)
        return target
    return None


def _subagent_active(sid: str, now_ts: float, quiet: float = 600.0) -> bool:
    """True when a transcript of this session's background agents changed within the last `quiet` seconds."""
    for base in [claude_home(), *profiles_dir().glob("*")]:
        try:
            for f in (base / "projects").glob(f"*/{sid}/subagents/*.jsonl"):
                if now_ts - f.stat().st_mtime < quiet:
                    return True
        except OSError:
            continue
    return False


def _session_record_files() -> List[Path]:
    """Claude Code's per-process session records (<pid>.json) of every account's folder."""
    candidates = [claude_home() / "sessions", *profiles_dir().glob("*/sessions")]
    try:
        from .status import projects_dir
        candidates.append(projects_dir().parent / "sessions")
    except Exception:
        pass
    files: List[Path] = []
    seen = set()
    for d in candidates:
        try:
            r = d.resolve()
            if r not in seen and d.is_dir():
                seen.add(r)
                files.extend(d.glob("*.json"))
        except OSError:
            continue
    return files


def _session_record_files_strict() -> Optional[List[Path]]:
    """Like _session_record_files, but None when a records folder exists and cannot be listed: its
    records are then unknown (the lenient reader shows an unreadable folder as "no records")."""
    candidates = [claude_home() / "sessions", *profiles_dir().glob("*/sessions")]
    try:
        from .status import projects_dir
        candidates.append(projects_dir().parent / "sessions")
    except Exception:
        pass
    files: List[Path] = []
    seen = set()
    for d in candidates:
        try:
            r = d.resolve()
            if r in seen or not d.is_dir():
                continue
            seen.add(r)
            files.extend(d / name for name in os.listdir(d) if name.endswith(".json"))
        except (OSError, RuntimeError):
            return None
    return files


def live_session_records() -> List[dict]:
    """The records whose process is still running."""
    out = []
    for path in _session_record_files():
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            pid = int(rec.get("pid", 0))
            if not pid:
                continue
            os.kill(pid, 0)
        except Exception:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def restart_is_safe_or_gone(session: str, now: Optional[float] = None) -> bool:
    """For retrying a deferred move: the same as restart_is_safe while a record names the session; when
    none does (the exit hooks removed it but the process stayed) it is safe to try again
    once no background agent wrote lately. False whenever the records cannot be read. The move itself
    still proves the process is hung before it ends anything."""
    now_ts = time.time() if now is None else now
    files = _session_record_files_strict()
    if files is None:
        return False
    for path in files:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return False  # half written, or unreadable: could be this session's
        if isinstance(rec, dict) and rec.get("sessionId") == session:
            return restart_is_safe(session, now_ts)  # a record exists: the ordinary rule
    return not _subagent_active(session, now_ts)


def shell_children(pid: int) -> Optional[bool]:
    """Whether the process has shell children (background jobs a restart would kill). None when
    that cannot be told: callers must then treat the session as busy."""
    try:
        res = subprocess.run(["pgrep", "-lP", str(pid)], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if res.returncode not in (0, 1):  # 1 only means "no children"
        return None
    return any(w in (res.stdout or "") for w in (" zsh", " bash", " sh"))


def idle_for(rec: dict, now_ts: float) -> Optional[float]:
    """Seconds the session has been idle, None when it is not idle."""
    if rec.get("status") != "idle":
        return None
    return max(0.0, now_ts - float(rec.get("statusUpdatedAt") or 0) / 1000.0)


def restart_is_safe(session: str, now: Optional[float] = None, min_idle: float = 0.0) -> bool:
    """True only while the session is idle (for at least min_idle seconds), none of its background agents
    wrote lately and it has no shell children: a restart would not lose any work."""
    now_ts = time.time() if now is None else now
    for rec in live_session_records():
        if rec.get("sessionId") != session:
            continue
        idle = idle_for(rec, now_ts)
        if idle is None or idle < min_idle or _subagent_active(session, now_ts):
            return False
        return shell_children(int(rec["pid"])) is False
    return False


def session_is_idle(session: str, now: Optional[float] = None) -> bool:
    """The session's own record says idle (a turn is not running, no question is waiting)."""
    now_ts = time.time() if now is None else now
    return any(r.get("sessionId") == session and idle_for(r, now_ts) is not None for r in live_session_records())


def rebalance(cfg: Optional[object] = None, bot=None, now: Optional[float] = None,
              spawn_fn: Optional[Callable] = None) -> Optional[Tuple[str, str]]:
    """Move one terminal session that has sat idle on the big plan for 5+ minutes to a smaller
    account with room. Idle only: no running shell jobs; 3-hour cooldown per session.
    Opt-out via auto_enabled(). One per round."""
    if not auto_enabled():
        return None
    now_ts = time.time() if now is None else now
    seen_path = state_dir() / "rebalanced.json"
    done = _read(seen_path, {})
    from .config import home_chat
    from .topics import TopicMap
    try:  # with the config, the sessions of project groups count too
        topics = TopicMap(state_dir() / "topics.json", None, home_chat(cfg) if cfg else 0, cfg=cfg).all_places()
    except Exception:
        topics = {}
    for rec in live_session_records():
        pid = int(rec.get("pid", 0))
        sid = str(rec.get("sessionId") or "")
        if not sid:
            continue
        acct = account_for(sid)
        thread = (topics.get(sid) or {}).get("thread")
        pane_exists = False
        try:
            panes_file = state_dir() / "tmux-panes.json"
            if panes_file.exists():
                panes = json.loads(panes_file.read_text(encoding="utf-8"))
                pane_exists = sid in panes
        except Exception:
            pass
        if not (acct and thread and (rec.get("tmux") or pane_exists) and is_big_plan(acct)):
            continue
        updated_at = float(rec.get("statusUpdatedAt") or 0) / 1000.0
        if rec.get("status") != "idle" or (now_ts - updated_at < 300):
            continue
        if _subagent_active(sid, now_ts):
            continue  # a background agent still writing would die with the restart
        if now_ts - float(done.get(sid, 0) or 0) < 3 * 3600:
            continue
        if shell_children(pid) is not False:
            continue  # background shell jobs, or no way to tell: the session stays put
        with ops_lock():  # a removal in progress finishes first; the target is picked from what is left
            target = pick_account(acct, small_first=True, now=now_ts)
            if not target or is_big_plan(target) or config_dir(target) is None:
                return None
            done[sid] = now_ts
            write_private(seen_path, json.dumps(done))
            if spawn_fn is None:
                from .hooks import spawn as default_spawn
                spawn_fn = default_spawn
            spawn_fn(["account", "auto-move", f"--session={sid}", f"--thread={thread}", f"--target={target}", "--idle"])
        return sid, target
    return None


def login_shell(name: str) -> str:
    """The shell line that starts `claude auth login` for an account in a headless terminal: no
    browser is opened; the owner opens the printed link on their phone and pastes the code back."""
    folder = config_dir(name)
    if folder is None:
        raise ValueError("no such account")
    # A link nobody finishes must not stay open for hours: past LOGIN_WINDOW its terminal closes by itself,
    # or it would keep blocking the account's removal. The pane id is never reused, so a later sign-in is safe;
    # an empty id would make tmux kill some other pane.
    watchdog = f'(sleep {LOGIN_WINDOW + 120}; [ -n "$TMUX_PANE" ] && tmux kill-pane -t "$TMUX_PANE") >/dev/null 2>&1 &'
    return (f"{watchdog} CLAUDE_CONFIG_DIR={shlex.quote(str(folder))} BROWSER=/usr/bin/true claude auth login;"
            " sleep 30")


# -- starting a login from Telegram ---------------------------------------------------------------------

def _login_path() -> Path:
    return state_dir() / "account-login.json"


def login_started(name: str, tmux_session: str) -> None:
    write_private(_login_path(), json.dumps({"name": name, "tmux": tmux_session, "at": time.time()}))


def login_pending() -> Optional[dict]:
    pending = _read(_login_path(), {})
    if pending.get("name") and time.time() - float(pending.get("at", 0) or 0) < LOGIN_WINDOW:
        return pending
    return None


def _finished_path() -> Path:
    return state_dir() / "account-login-finished.json"


def login_done() -> None:
    pending = _read(_login_path(), {})
    if pending.get("name"):  # remembered, so the account menu can fetch its first quota reading at once
        with _lock(_finished_path()):
            cur = _read(_finished_path(), {})
            cur[str(pending["name"])] = time.time()
            write_private(_finished_path(), json.dumps(dict(list(cur.items())[-20:])))
    try:
        _login_path().unlink()
    except OSError:
        pass


CODE_SHAPE = re.compile(r"[A-Za-z0-9_#-]{30,300}")  # a login code pasted without any command


# -- per-account quota lines of the dashboard ------------------------------------------------------------

def usage_path(name: str) -> Path:
    return state_dir() / f"claude-usage-{name}.json"


# -- quota of idle accounts: read from the account's token, not only from a running status line -------------
# The status line of a running session is the only thing that writes claude-usage-<name>.json, so an
# account with no session open showed an old reading, or none. The dashboard job refreshes it from the
# usage endpoint, at most every USAGE_EVERY per profile and backing off when the server says 429.

STALE_READING = 3600.0  # a dashboard reading older than this shows its age
USAGE_EVERY = 300.0  # seconds between two fetches for one profile
USAGE_BACKOFF = 900.0  # first wait after a 429; doubles per repeat, never above USAGE_BACKOFF_MAX
USAGE_BACKOFF_MAX = 7200.0


def _usage_state_path() -> Path:
    return state_dir() / "account-usage-fetch.json"


def iso_epoch(value: object) -> float:
    """An ISO 8601 time (with Z or an offset, any number of fraction digits) as epoch seconds; 0 when it
    cannot be read. Python 3.9's fromisoformat cannot parse most of what the server sends."""
    import calendar
    if isinstance(value, (int, float)):
        return float(value)
    m = re.fullmatch(r"\s*(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?\s*", str(value or ""))
    if not m:
        return 0.0
    try:
        base = float(calendar.timegm(time.strptime(m.group(1) + "T" + m.group(2), "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return 0.0
    off = m.group(3)
    if off and off != "Z":
        secs = int(off[1:3]) * 3600 + int(off[-2:]) * 60
        base -= secs if off[0] == "+" else -secs
    return base


def _window(body: dict, key: str) -> Tuple[Optional[float], float]:
    win = body.get(key)
    if not isinstance(win, dict) or win.get("utilization") is None:
        return None, 0.0
    try:
        return float(win["utilization"]), iso_epoch(win.get("resets_at"))
    except (TypeError, ValueError):
        return None, 0.0


def usage_record_from_oauth(body: dict, now_ts: float) -> Optional[dict]:
    """The same record the status line saves (see statusline.usage_record), from the usage endpoint."""
    five, five_r = _window(body, "five_hour")
    week, week_r = _window(body, "seven_day")
    if five is None and week is None:
        return None
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)), "five_hour": round(five or 0),
            "weekly": round(week or 0), "five_hour_resets": int(five_r) if five_r else "",
            "weekly_resets": int(week_r) if week_r else "", "source": "oauth", "extra": []}


USAGE_FORCE_GAP = 20.0  # a forced fetch never repeats for one profile sooner than this


def _usage_wait(rec: object, now_ts: float, force: bool) -> bool:
    """True while a profile's saved fetch record says to wait: backing off, or fetched too lately."""
    if not isinstance(rec, dict):
        return False
    try:
        gap = USAGE_FORCE_GAP if force else USAGE_EVERY
        return now_ts < float(rec.get("until") or 0) or 0 <= now_ts - float(rec.get("at") or 0) < gap
    except (TypeError, ValueError):
        return False


def refresh_usage(now: Optional[float] = None, only: Optional[List[str]] = None, timeout: float = PROFILE_HTTP_TIMEOUT,
                  fetch: Optional[Callable] = None, force: bool = False) -> List[str]:
    """Refresh the usage file of each profile that is due. Returns the profiles written. Skips a profile
    whose status line wrote lately (a running session keeps it fresh), one with no usable token (signed
    out or never signed in), one still backing off after a 429/401, and never overwrites a status line
    reading newer than the fetch. `force` (a sign-in just finished, the account menu was opened) ignores
    the 5-minute throttle, keeping only USAGE_FORCE_GAP and the back-off.
    A "signed out" state does not block a profile the bridge never knew an address for (a new account: the
    state may be older than the sign-in); a token that is really revoked answers 401, which backs off."""
    now_ts = time.time() if now is None else now
    get = fetch or _http_json
    written: List[str] = []
    for name in (only if only is not None else names()):
        if login_state(name) == "out" and cached_email(name):
            continue  # signed out after being signed in: nothing to fetch until the login is read again
        if _usage_wait(_read(_usage_state_path(), {}).get(name), now_ts, force):
            continue  # a cheap look first; the claim below decides under the lock
        current_file = _read(usage_path(name), {})
        if current_file.get("source") == "statusline" and 0 <= now_ts - iso_epoch(current_file.get("at")) < USAGE_EVERY:
            continue  # a running session's status line is fresher than a fetch would be
        token = valid_token(name, now_ts)  # an expired token cannot be fetched with: skip until it is refreshed
        if not token:
            continue
        # Claim the fetch slot under the lock, before the request: a parallel call (the menu, the dashboard
        # job) then sees the claim and keeps the gap, and sees any back-off this call saves for its profile.
        with _lock(_usage_state_path()):
            state = _read(_usage_state_path(), {})
            rec = state.get(name) if isinstance(state.get(name), dict) else {}
            if _usage_wait(rec, now_ts, force):
                continue
            state[name] = dict(rec, at=now_ts)
            write_private(_usage_state_path(), json.dumps(state))
        status, body, retry = get("/api/oauth/usage", token, timeout)
        entry = {"at": now_ts}
        if status == 401:
            entry.update({"until": now_ts + USAGE_BACKOFF})  # revoked or signed out: stop asking for a while
        elif status == 429:
            fails = int(rec.get("fails") or 0) + 1
            wait = max(retry, min(USAGE_BACKOFF * (2 ** (fails - 1)), USAGE_BACKOFF_MAX))
            entry.update({"fails": fails, "until": now_ts + wait})
        elif status == 200:
            out = usage_record_from_oauth(body, now_ts)
            latest = _read(usage_path(name), {})  # re-read: a status line may have written during the fetch
            newer = latest.get("source") == "statusline" and iso_epoch(latest.get("at")) >= int(now_ts)
            if out and not newer:
                write_private(usage_path(name), json.dumps(out))
                written.append(name)
        with _lock(_usage_state_path()):  # only this profile's entry goes back, onto the file as it is now
            merged = _read(_usage_state_path(), {})
            merged[name] = entry
            write_private(_usage_state_path(), json.dumps(merged))
    return written


def reading_age(name: str, now: Optional[float] = None) -> Optional[float]:
    """Seconds since the profile's usage reading was taken; None when there is none."""
    rec = _read(usage_path(name), {})
    at = iso_epoch(rec.get("at"))
    return None if not at else (time.time() if now is None else now) - at


FRESH_SIGN_IN = 600.0  # a sign-in finished this recently counts as new


def recently_signed_in(name: str, now: Optional[float] = None) -> bool:
    """The profile's sign-in was finished from Telegram within FRESH_SIGN_IN."""
    now_ts = time.time() if now is None else now
    try:
        return 0 <= now_ts - float(_read(_finished_path(), {}).get(name) or 0) < FRESH_SIGN_IN
    except (TypeError, ValueError):
        return False


def ensure_usage(now: Optional[float] = None, max_age: float = 2 * USAGE_EVERY, timeout: float = 4.0,
                 fetch: Optional[Callable] = None, recent_only: bool = False) -> List[str]:
    """Fetch now the reading of every account that has none, or whose reading is older than `max_age`
    (the account menu was opened, or a sign-in just finished). Accounts whose sign-in is unfinished are
    left alone. Quick: a short timeout, and the per-profile gap keeps repeated taps from hammering.
    `recent_only`: just the accounts whose sign-in finished within FRESH_SIGN_IN."""
    now_ts = time.time() if now is None else now
    due = []
    for name in names():
        if sign_in_unfinished(name) or (recent_only and not recently_signed_in(name, now_ts)):
            continue
        age = reading_age(name, now_ts)
        if age is None or age > max_age:
            due.append(name)
    return refresh_usage(now=now_ts, only=due, timeout=timeout, fetch=fetch, force=True) if due else []


def usage_lines(now: Optional[float] = None, lang: Optional[str] = None) -> List[str]:
    """One quota line per account, from the per-account files the status line writes; only once there
    is more than one account. Reads cached e-mails only: the dashboard must stay quick."""
    import calendar
    import html
    if not multiple():
        return []
    now = time.time() if now is None else now
    out = ["", t("accounts_header", lang=lang)]
    for name in names():
        who = html.escape(label(name), quote=False)
        if sign_in_unfinished(name):
            out.append(t("accounts_unfinished", lang=lang, who=who))
            continue
        u = _read(usage_path(name), {})
        try:
            week = int(u.get("weekly", 0))
            at = calendar.timegm(time.strptime(u["at"], "%Y-%m-%dT%H:%M:%SZ"))
            top = week  # weekly only: the plans here have no 5-hour window
            dot = "🔴" if top >= 90 else "🟡" if top >= 70 else "🟢"
            age = now - at
            old = t("accounts_old_age", lang=lang, hours=int(age // 3600)) if age > STALE_READING else ""
            out.append(t("accounts_week", lang=lang, dot=dot, who=who, week=week, old=old))
        except (KeyError, ValueError, TypeError):
            out.append(t("accounts_no_data", lang=lang, who=who))
    return out
