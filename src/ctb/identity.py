"""Who each Claude profile is really signed in as, and the two owner notices that follow from it.

accounts.py keeps the addresses fresh (refresh_emails) and knows which profiles are one real account.
This module is the part that talks: when an address changes it posts one notice in General, and when two
profiles turn out to share one login it posts one notice suggesting what to do. It never logs anything in
or out and never touches a profile: account actions are the owner's.

Called from the dashboard job (throttled to once per accounts.IDENTITY_EVERY), when a login finishes,
before a limit move is planned and before any move runs (forced, with the short timeout). Never from
the menu, which only reads the cache.

Notices are recorded before they are posted and removed once they arrived, so a failed post is retried
by the next call and nothing is posted twice. The lock only guards that bookkeeping: it is never held
during a Telegram call.
"""
from __future__ import annotations

import json
import time
from typing import List, Optional, Tuple

from . import accounts
from .config import Config, home_chat, state_dir, write_private
from .i18n import t

FSI, PDI = "⁨", "⁩"
CLAIM_TTL = 60.0  # seconds a notice counts as being posted by another process


def _notices_path():
    return state_dir() / "account-notices.json"


def isolate(value: str, lang: str) -> str:
    """A short Latin name inside a Hebrew line, wrapped in a bidi isolate."""
    return f"{FSI}{value}{PDI}" if lang == "he" else value


_name = isolate


def local_part(addr: str) -> str:
    return addr.split("@")[0] if addr else addr


_who = local_part


def _post(cfg: Config, bot, text: str) -> None:
    bot.call("sendMessage", {"chat_id": home_chat(cfg), "text": text, "disable_notification": False})


def duplicate_signature(group: List[str]) -> str:
    """Identifies one duplicate: the address and the profiles that share it."""
    return accounts.shared_address(group) + "|" + ",".join(sorted(group))


def _state() -> dict:
    state = accounts._read(_notices_path(), {})
    for key, kind in (("duplicates", list), ("pending", list), ("claims", dict), ("stale", list), ("stale_logged", list)):
        if not isinstance(state.get(key), kind):
            state[key] = kind()
    return state


def _save(state: dict) -> None:
    write_private(_notices_path(), json.dumps(state))


def check(cfg: Config, bot, force: bool = False, now: Optional[float] = None,
          timeout: float = accounts.IDENTITY_TIMEOUT) -> Tuple[list, list]:
    """Refresh the addresses (throttled unless `force`), then post what is new or still unposted.
    Returns (changes, announced): the address changes seen and the duplicate groups announced. Every
    failure is swallowed: this runs inside jobs that must carry on."""
    lang = getattr(cfg, "ui_language", None) or "en"
    now_ts = time.time() if now is None else now
    try:
        changes = accounts.refresh_emails(force=force, now=now_ts, timeout=timeout)
    except Exception:
        changes = []
    announced: list = []
    try:
        was = _post_changes(cfg, bot, lang, changes, now_ts)
        _announce_stale(cfg, bot, lang, now_ts)
        announced = _announce_duplicates(cfg, bot, lang, was, now_ts)
    except Exception:
        pass
    return changes, announced


def _post_changes(cfg: Config, bot, lang: str, changes: list, now_ts: float) -> dict:
    """Record the new changes as pending, then post every pending one nobody else is posting. Returns
    {profile: previous address} for the changes handled here."""
    with accounts._lock(_notices_path()):
        state = _state()
        for name, old, new in changes:
            state["pending"].append({"name": name, "old": old, "new": new})
        mine = []
        for item in state["pending"]:
            key = "chg:" + item["name"] + ":" + item["new"]
            if now_ts - float(state["claims"].get(key, 0) or 0) < CLAIM_TTL:
                continue  # another process is posting it
            state["claims"][key] = now_ts
            mine.append((key, item))
        _save(state)
    was = {}
    for key, item in mine:
        ok = False
        try:
            _post(cfg, bot, t("account_identity_changed", lang=lang, name=isolate(item["name"], lang),
                              old=isolate(local_part(item["old"]), lang), new=isolate(local_part(item["new"]), lang)))
            ok = True
        except Exception:
            pass  # stays pending: the next call posts it again
        with accounts._lock(_notices_path()):
            state = _state()
            state["claims"].pop(key, None)
            if ok:
                state["pending"] = [p for p in state["pending"] if p != item]
            _save(state)
        if ok:
            was[item["name"]] = item["old"]
    return was


def _announce_duplicates(cfg: Config, bot, lang: str, was: dict, now_ts: float) -> list:
    """One notice per duplicate, not repeated while it lasts (and again if it goes away and returns). A
    group is recorded as announced only once its post arrived; a failed one is retried."""
    # A group with a stale older login has its own notice (_announce_stale), which says what is used.
    groups = [g for g in accounts.duplicate_groups() if not accounts.stale_twins(g)]
    current = [duplicate_signature(g) for g in groups]
    todo = []
    with accounts._lock(_notices_path()):
        state = _state()
        state["duplicates"] = [sig for sig in state["duplicates"] if sig in current]  # resolved ones may return
        for group, sig in zip(groups, current):
            key = "dup:" + sig
            if sig in state["duplicates"] or now_ts - float(state["claims"].get(key, 0) or 0) < CLAIM_TTL:
                continue
            state["claims"][key] = now_ts
            todo.append((group, sig, key))
        _save(state)
    done = []
    for group, sig, key in todo:
        changed = next((n for n in group if n in was), None)
        hint = (t("account_dup_hint_resign", lang=lang, name=isolate(changed, lang), old=isolate(local_part(was[changed]), lang))
                if changed else t("account_dup_hint_generic", lang=lang))
        ok = False
        try:
            _post(cfg, bot, t("account_dup_notice", lang=lang, names=" + ".join(isolate(n, lang) for n in group),
                              who=isolate(local_part(accounts.shared_address(group)), lang), hint=hint))
            ok = True
        except Exception:
            pass
        with accounts._lock(_notices_path()):
            state = _state()
            state["claims"].pop(key, None)
            if ok and sig not in state["duplicates"]:
                state["duplicates"].append(sig)
            _save(state)
        if ok:
            done.append(group)
    return done


def stale_signature(stale: str, best: str) -> str:
    return f"{stale}>{best}"


def _announce_stale(cfg: Config, bot, lang: str, now_ts: float) -> list:
    """One notice (and one poll.log line) per stale older login of an account, not repeated while it
    lasts and again if it goes away and returns. Plan names only: no address, no token."""
    pairs = [(n, b) for g in accounts.duplicate_groups() for n, b in accounts.stale_twins(g)]
    current = [stale_signature(n, b) for n, b in pairs]
    todo = []
    with accounts._lock(_notices_path()):
        state = _state()
        state["stale"] = [sig for sig in state["stale"] if sig in current]
        state["stale_logged"] = [sig for sig in state["stale_logged"] if sig in current]
        for (n, b), sig in zip(pairs, current):
            key = "stale:" + sig
            if sig in state["stale"] or now_ts - float(state["claims"].get(key, 0) or 0) < CLAIM_TTL:
                continue
            state["claims"][key] = now_ts
            first = sig not in state["stale_logged"]
            if first:
                state["stale_logged"].append(sig)
            todo.append((n, b, sig, key, first))
        _save(state)
    done = []
    for n, b, sig, key, first in todo:
        if first:
            try:
                from .inbox import poll_log
                poll_log(f"stale twin login: {n} reports plan {accounts.cached_plan(n) or '?'}, "
                         f"{b} reports {accounts.cached_plan(b) or '?'}; using {b}")
            except Exception:
                pass
        ok = False
        try:
            _post(cfg, bot, t("account_stale_twin", lang=lang, stale=isolate(n, lang), best=isolate(b, lang)))
            ok = True
        except Exception:
            pass
        with accounts._lock(_notices_path()):
            state = _state()
            state["claims"].pop(key, None)
            if ok and sig not in state["stale"]:
                state["stale"].append(sig)
            _save(state)
        if ok:
            done.append((n, b))
    return done
