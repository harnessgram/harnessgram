"""The reset bank: how many saved quota resets the owner holds per provider (Claude, Codex, ...).

No provider reports this number to a program, so the owner keeps it: from the control panel (plus and
minus) or with `harnessgram bank set <provider> <n>`. Each provider's saved resets may carry an expiry date
the owner sets (`harnessgram bank expire <provider> <YYYY-MM-DD|none>` or the panel's date field), since a
reset that is not spent by then is lost. Agents are told the counts and dates in the session hint, so they
can recommend when a reset is worth spending and use the quota fully before one expires. Spending one is
always the owner's decision.
"""
from __future__ import annotations

import fcntl
import json
import re
import sys
import time
from datetime import date
from typing import Dict, List, Optional

from .config import private_dir, state_dir, write_private

STATE_FILE = "reset-bank.json"
LOCK_WAIT = 2.0  # seconds a write waits for another writer before giving up
NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,19}")
KNOWN = ("claude", "codex")  # always offered in the panel, even before the owner sets a number
MAX_NAMES = 6
MAX_N = 99
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
MAX_AHEAD_DAYS = 400  # an expiry further ahead than this is a typo, not a real reset
SOON_DAYS = 7  # a saved reset this close to its expiry makes agents use the quota fully


def allowed(name, n) -> bool:
    return isinstance(name, str) and bool(NAME_RE.fullmatch(name)) and type(n) is int and 0 <= n <= MAX_N


def _date(value) -> Optional[date]:
    """A strict local calendar date "YYYY-MM-DD", or None when the value is not one."""
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def valid_expiry(value, today: date = None) -> bool:
    """A date the owner may set: a real "YYYY-MM-DD" no more than MAX_AHEAD_DAYS ahead (a past date is allowed)."""
    d = _date(value)
    return d is not None and (d - (today or date.today())).days <= MAX_AHEAD_DAYS


def _rows() -> Dict[str, dict]:
    """{provider: {"n", "at"?, "expires"?}}, only well-formed rows and fields. Read without the lock: writes are atomic."""
    try:
        data = json.loads((state_dir() / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(data, dict):
        return {}
    rows = {}
    for k, v in data.items():
        if not isinstance(v, dict) or not allowed(k, v.get("n")):
            continue
        row = {"n": v["n"]}
        if isinstance(v.get("at"), (int, float)) and not isinstance(v.get("at"), bool):
            row["at"] = v["at"]
        if v["n"] > 0 and _date(v.get("expires")):
            row["expires"] = v["expires"]
        rows[k] = row
    return rows


def read() -> Dict[str, int]:
    """{provider: saved resets}, only well-formed entries."""
    return {k: v["n"] for k, v in _rows().items()}


def expiries() -> Dict[str, str]:
    """{provider: "YYYY-MM-DD"} for the providers whose saved resets carry an expiry."""
    return {k: v["expires"] for k, v in _rows().items() if "expires" in v}


def _save(base, rows: Dict[str, dict]) -> None:
    write_private(base / STATE_FILE, json.dumps({k: rows[k] for k in sorted(rows)}))


def _lock(lock) -> None:
    until = time.monotonic() + LOCK_WAIT
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= until:
                raise TimeoutError("the reset bank is busy") from None
            time.sleep(0.05)


def write(name: str, n: int, now: float = None) -> Dict[str, int]:
    """Set one provider's count (0 keeps the row at zero and clears its expiry). Other rows stay as they are.
    Raises ValueError on a bad name, number or a 7th name."""
    if not allowed(name, n):
        raise ValueError("a provider name (a-z, digits, dashes) and a number from 0 to 99")
    base = private_dir(state_dir())
    with open(base / (STATE_FILE + ".lock"), "a") as lock:
        _lock(lock)
        rows = _rows()
        if name not in rows and len(rows) >= MAX_NAMES:
            raise ValueError(f"at most {MAX_NAMES} providers")
        row = {"n": n, "at": time.time() if now is None else now}
        if n > 0 and "expires" in rows.get(name, {}):
            row["expires"] = rows[name]["expires"]
        rows[name] = row
        _save(base, rows)
    return {k: v["n"] for k, v in rows.items()}


def set_expiry(name: str, expires: Optional[str], today: date = None) -> Dict[str, str]:
    """Set (or with None, clear) the expiry of one provider's saved resets; its count and time stay.
    Raises ValueError on a bad date or when the provider holds no saved reset."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError("a provider name (a-z, digits, dashes)")
    if expires is not None and not valid_expiry(expires, today):
        raise ValueError(f"a date as YYYY-MM-DD, at most {MAX_AHEAD_DAYS} days ahead, or none")
    base = private_dir(state_dir())
    with open(base / (STATE_FILE + ".lock"), "a") as lock:
        _lock(lock)
        rows = _rows()
        if name not in rows or (expires is not None and rows[name]["n"] == 0):
            raise ValueError(f"no saved resets for {name}; set a count first (bank set {name} <n>)")
        if expires is None:
            rows[name].pop("expires", None)
        else:
            rows[name]["expires"] = expires
        _save(base, rows)
    return {k: v["expires"] for k, v in rows.items() if "expires" in v}


def forget(name: str) -> bool:
    base = private_dir(state_dir())
    with open(base / (STATE_FILE + ".lock"), "a") as lock:
        _lock(lock)
        rows = _rows()
        if name not in rows:
            return False
        del rows[name]
        _save(base, rows)
    return True


def _days(expires: Optional[str], today: date = None) -> Optional[int]:
    d = _date(expires)
    return None if d is None else (d - (today or date.today())).days


def view(today: date = None) -> List[dict]:
    """For the panel: the known providers first, then any other, each with its count (None = never set), its
    expiry (None = not set) and the whole days left until it (0 = today, negative = passed)."""
    rows = _rows()
    names = list(KNOWN) + sorted(k for k in rows if k not in KNOWN)
    out = []
    for k in names:
        row = rows.get(k, {})
        exp = row.get("expires")
        out.append({"name": k, "n": row.get("n"), "expires": exp, "days": _days(exp, today)})
    return out


def label(name: str) -> str:
    return name[:1].upper() + name[1:]


def when(expires: Optional[str], days: Optional[int]) -> str:
    """The date part of a row, such as " (expires 2030-01-20, in 9 days)" or " (expired 2030-01-01)"; "" when unset."""
    if expires is None or days is None:
        return ""
    if days < 0:
        return f" (expired {expires})"
    left = "today" if days == 0 else f"in {days} day" + ("" if days == 1 else "s")
    return f" (expires {expires}, {left})"


def hint(today: date = None) -> str:
    """A short paragraph for a session's hint, or "" when no reset is saved anywhere."""
    saved = [r for r in view(today) if r["n"]]
    if not saved:
        return ""
    have = [f"{label(r['name'])} {r['n']}{when(r['expires'], r['days'])}" for r in saved]
    text = ("Reset bank (saved quota resets the owner can spend): " + ", ".join(have) + ". Spending one is the "
            "owner's decision, never yours. When a quota limit blocks important work, say so and recommend it "
            'with `ctb decide ask "<question>" --detail "<why now>"`; until the owner says yes, plan as if '
            "there were none.")
    if any(r["days"] is not None and 0 <= r["days"] <= SOON_DAYS for r in saved):
        text += (" A reset that expires unused is lost, so while one is about to expire use the current quota "
                 "fully rather than save it (run queued work, more parallel teams, stronger models where they "
                 "help), and once the quota is nearly used, recommend with `ctb decide ask` spending the reset "
                 "before its date.")
    if any(r["days"] is not None and r["days"] < 0 for r in saved):
        text += " A saved reset is past its date: tell the owner to update the reset bank."
    return text


def run(action: str, name: str = "", n: int = 0, expires: str = "") -> int:
    """`harnessgram bank` (list), `bank set <provider> <n>`, `bank expire <provider> <YYYY-MM-DD|none>`,
    `bank forget <provider>`."""
    try:
        if action == "set":
            write(name.lower(), n)
        elif action == "expire":
            set_expiry(name.lower(), None if expires.lower() == "none" else expires)
        elif action == "forget" and not forget(name.lower()):
            print(f"Harnessgram: no saved count for {name}", file=sys.stderr)
            return 1
    except (ValueError, TimeoutError) as e:
        print(f"Harnessgram: {e}", file=sys.stderr)
        return 1
    rows = [f"{label(r['name'])}: {'not set' if r['n'] is None else r['n']}{when(r['expires'], r['days'])}"
            for r in view()]
    print("Reset bank\n" + "\n".join(rows))
    return 0
