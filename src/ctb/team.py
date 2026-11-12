"""Team group (opt-in): share chosen sessions with a few people in a Telegram supergroup.

Security model, in short:
* The group and the shared sessions are changed ONLY by commands run on this machine (`ctb team ...`).
  Members are added the same way, or by the owner's own tap on a request in their home chat
  (members.py); nothing a Telegram user sends can add a member or share a session: granting access
  is the owner's decision.
* A member is identified by numeric Telegram user id, never by name.
* A message from a member is queued as a SUGGESTION (see framing.py); it is never the owner.
* Everything posted to the group is redacted and passes `block_patterns` first.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, List, Optional

from . import config as C
from . import lang as L
from .creds import redact
from .inbox import Inbox

_PLAIN = re.compile(r"<[^>]+>")


def part_blocked(plain: str, patterns: List[str]) -> bool:
    return any(re.search(p, plain, re.I | re.S) for p in patterns)


def filter_part(html_text: str, plain: str, team: C.TeamConfig, lang: str) -> str:
    """The HTML to post to the group for one message part: the part itself, or '(part withheld)'
    when any block pattern matches its text. The text has been redacted by the caller."""
    if part_blocked(plain, team.block_patterns):
        return L.strings(lang)["withheld"]
    return html_text


# --- editing the config (local CLI only) ------------------------------------------------------

def _load_raw() -> dict:
    path = C.config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise C.ConfigError(f"cannot read {path}; run `harnessgram pair` first") from None
    if not isinstance(raw, dict):
        raise C.ConfigError(f"{path} must contain a JSON object")
    return raw


def _edit(change: Callable[[C.TeamConfig], str]) -> str:
    """Load, apply `change` to the team section, validate by re-parsing, save (mode 600). One lock is held
    from the read to the save, so a tap in Telegram and a command here cannot undo each other."""
    with C.config_lock():
        raw = _load_raw()
        team = C.team_from_raw(raw.get("team", {}))
        message = change(team)
        raw["team"] = team.to_raw()
        C.team_from_raw(raw["team"])  # never write something we could not read back
        C.from_dict(raw)  # nor a team group that clashes with the home or a project group
        C.save_raw(raw)
        return message


def resolve_session(ref: str) -> str:
    """A full session id, or a unique prefix of one the bridge already mirrored."""
    topics_file = C.state_dir() / "topics.json"
    try:
        known = list(json.loads(topics_file.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        known = []
    if ref in known:
        return ref
    matches = [s for s in known if s.startswith(ref)] if len(ref) >= 6 else []
    if len(matches) == 1:
        return matches[0]
    raise C.ConfigError("unknown session: it needs at least one mirrored reply, and a prefix must be "
                        "6+ characters and unique (see the topic list)")


def set_chat(chat_id: int) -> str:
    if chat_id >= 0:
        raise C.ConfigError("a Telegram group id is a negative number (like -1001234567890)")

    def change(t):
        t.chat_id = chat_id
        return f"team group set; the bot must be an admin there with 'Manage topics'"
    return _edit(change)


def add_member(user_id: int, name: str) -> str:
    raw = _load_raw()
    if user_id <= 0:
        raise C.ConfigError("a Telegram user id is a positive number")
    if user_id == int(raw.get("owner_chat_id") or 0):
        raise C.ConfigError("that is the owner's own id; the owner needs no entry")
    clean = C.clean_name(name)
    if not clean:
        raise C.ConfigError("name is empty after removing brackets and control characters")

    def change(t):
        t.members[user_id] = clean
        return f"member {user_id} ({clean}) can now send SUGGESTIONS to shared sessions"
    return _edit(change)


def refresh(cfg: C.Config) -> None:
    """Re-read the member list into the running listener's config, in place, so a change made through
    add_member/remove_member applies at once (the listener loads its config only when it starts)."""
    cfg.team.members = C.team_from_raw(_load_raw().get("team", {})).members


def remove_member(user_id: int, cfg: Optional[C.Config] = None) -> str:
    """Remove a member. With `cfg` (the running listener's), its member list is refreshed straight after
    the save, before the queued messages are discarded, so a failure there cannot leave memory behind."""
    def change(t):
        if user_id not in t.members:
            raise C.ConfigError("no such member")
        del t.members[user_id]
        return f"member {user_id} removed"
    msg = _edit(change)
    if cfg is not None:
        refresh(cfg)
    discard_queued(user_id)
    return msg + " (their queued messages were discarded)"


def discard_queued(user_id: int) -> int:
    return purge(lambda item: item.get("source") == "team" and item.get("from") == user_id)


def share(ref: str) -> str:
    session = resolve_session(ref)

    def change(t):
        if session not in t.shared_sessions:
            t.shared_sessions.append(session)
        return f"session {session} is shared with the team group (its replies are redacted first)"
    return _edit(change)


def unshare(ref: str) -> str:
    session = resolve_session(ref) if ref not in _shared_ids() else ref

    def change(t):
        if session not in t.shared_sessions:
            raise C.ConfigError("that session is not shared")
        t.shared_sessions.remove(session)
        return f"session {session} is no longer shared"
    msg = _edit(change)
    purge(lambda item: item.get("source") == "team", only_session=session)
    return msg + " (queued team messages for it were discarded)"


def _shared_ids() -> List[str]:
    try:
        return list(C.team_from_raw(_load_raw().get("team", {})).shared_sessions)
    except C.ConfigError:
        return []


def purge(predicate: Callable[[dict], bool], only_session: Optional[str] = None) -> int:
    """Drop queued messages matching `predicate` (all sessions, or one)."""
    root = C.state_dir() / "inbox"
    removed = 0
    if not root.is_dir():
        return 0
    for f in root.glob("*.jsonl"):
        session = f.stem
        if only_session and session != re.sub(r"[^A-Za-z0-9._-]", "_", only_session):
            continue
        removed += Inbox(root).filter(session, lambda item: not predicate(item))
    return removed


def describe(cfg: C.Config) -> str:
    t = cfg.team
    lines = [f"team group: {'on, chat ' + str(t.chat_id) if t.enabled else 'off (harnessgram team set-chat <id>)'}",
             "members: " + (", ".join(f"{n} ({u})" for u, n in sorted(t.members.items())) or "none"),
             "shared sessions: " + (", ".join(t.shared_sessions) or "none"),
             f"block patterns: {len(t.block_patterns)}"]
    return "\n".join(lines)
