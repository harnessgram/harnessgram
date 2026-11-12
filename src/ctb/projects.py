"""Project groups: one owner-only forum supergroup per project, next to the home group.

A session lives in exactly one place: a topic in the home group (the default) or in a project
group. Thread ids are only unique inside one chat, so a place is always a chat plus a thread.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from . import config as C
from .config import _GLOB, Config, ProjectConfig, _under, home_chat

GIT_TIMEOUT = 2.0  # seconds for all the git questions about one new session; slower means "home"


@dataclass(frozen=True)
class Place:
    """Where a session's topic is: its chat and thread. `home` is the home group (or private chat)."""
    chat: int
    thread: int
    home: bool = True

    @property
    def key(self) -> str:
        """The key for state that is kept per topic: the bare thread id at home (the old keys keep
        working, no migration), "chat:thread" in a project group."""
        return str(self.thread) if self.home else f"{self.chat}:{self.thread}"

    @property
    def away(self) -> Optional[int]:
        """The value for a `chat=` argument: None at home (the default), the project chat otherwise."""
        return None if self.home else self.chat


def thread_key(thread, chat: Optional[int] = None) -> str:
    """`Place.key` for a topic known only by its thread and `chat=` argument (None = the home chat)."""
    return str(thread) if chat is None else f"{chat}:{thread}"


def owner_chats(cfg: Config) -> frozenset:
    """Every chat the owner is obeyed in: the home chat, plus the project groups in group mode."""
    if not cfg.is_group_mode:
        return frozenset({home_chat(cfg)})
    return frozenset({home_chat(cfg), *(p.chat_id for p in cfg.projects)})


def project_by_chat(cfg: Config, chat: int) -> Optional[ProjectConfig]:
    for project in cfg.projects:
        if project.chat_id == chat:
            return project
    return None


def _matches_rule(rule: str, *paths: str) -> bool:
    """True when one of the folders is the rule's folder or below it. A rule with `*`, `?` or `[` is a pattern."""
    expanded = os.path.normpath(os.path.expanduser(rule))
    if _GLOB.search(expanded):
        return any(fnmatch.fnmatchcase(p, expanded) or fnmatch.fnmatchcase(p, expanded + os.sep + "*") for p in paths)
    real = os.path.realpath(expanded)
    return any(_under(p, expanded) or _under(p, real) for p in paths)


def _folder_project(cfg: Config, *paths: str) -> Optional[ProjectConfig]:
    for project in cfg.projects:
        if any(_matches_rule(rule, *paths) for rule in project.paths):
            return project
    return None


def _git(cwd: str, deadline: float, *args: str) -> str:
    """Output of `git -C cwd ...`; "" when git fails. Raises TimeoutError past the shared deadline."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("git")
    try:
        done = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=left,
                              stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        raise TimeoutError("git") from None
    return done.stdout.strip() if done.returncode == 0 else ""


def _git_repo(cwd: str) -> Optional[Tuple[str, str]]:
    """(the main repository's folder, the origin's repository name) for a folder inside a git repository or
    one of its worktrees; None when it is not one, or git fails or is slow."""
    deadline = time.monotonic() + GIT_TIMEOUT
    try:
        lines = _git(cwd, deadline, "rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir").splitlines()
        if len(lines) != 3:
            return None
        top, git_dir, common = (os.path.realpath(os.path.join(cwd, line)) for line in lines)
        main = top
        # A worktree counts as its main repository only when that repository lists this worktree back:
        # a folder's own `.git` file could otherwise point anywhere and claim another project's folder.
        if git_dir != common and os.path.basename(common) == ".git" and _under(git_dir, os.path.join(common, "worktrees")):
            with open(os.path.join(git_dir, "gitdir"), encoding="utf-8") as back:
                if os.path.realpath(os.path.dirname(back.read().strip())) == top:
                    main = os.path.dirname(common)
        origin = _git(cwd, deadline, "config", "--get", "remote.origin.url")
    except (OSError, TimeoutError, ValueError):
        return None
    name = re.split(r"[/:\\]", origin.rstrip("/"))[-1] if origin else ""
    return main, name[:-4] if name.endswith(".git") else name


def _repo_project(cfg: Config, *names: str) -> Optional[ProjectConfig]:
    names = tuple(n.casefold() for n in names if n)
    for project in cfg.projects:
        if any(repo.casefold() in names for repo in project.repos):
            return project
    return None


def title_project(cfg: Config, title: str) -> Optional[ProjectConfig]:
    """The project whose title prefix is the text before the title's first ":" (casefolded), if any."""
    prefix = (title or "").split(":", 1)[0].strip().casefold() if ":" in (title or "") else ""
    if prefix:
        for project in cfg.projects:
            if any(prefix == p.strip().casefold() for p in project.title_prefixes):
                return project
    return None


def resolve(cfg: Config, session: str, title: str) -> Tuple[Optional[ProjectConfig], str]:
    """The project group a new session belongs in, and why ("path", "repo" or "title"); (None, "") means
    the home group. Signals in order: the folder the session runs in (and, for a git worktree, its main
    repository's folder, folder name and origin name), then the title's prefix before the first ":".
    The caller keeps the answer for good: a session never moves because its title changed."""
    if not cfg.projects:
        return None, ""
    from .status import session_cwd
    cwd = session_cwd(session)
    if cwd:
        here = (os.path.realpath(cwd), os.path.abspath(cwd))
        found = _folder_project(cfg, *here)
        if found:
            return found, "path"
        if any(p.paths or p.repos for p in cfg.projects):
            repo = _git_repo(cwd)
            if repo:
                main, origin = repo
                found = _folder_project(cfg, main)
                if found:
                    return found, "path"
                found = _repo_project(cfg, os.path.basename(main), origin)
                if found:
                    return found, "repo"
    found = title_project(cfg, title)
    return (found, "title") if found else (None, "")


# --- editing the settings (local CLI only) ------------------------------------------------------

def _load_raw() -> dict:
    path = C.config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise C.ConfigError(f"cannot read {path}; run `harnessgram pair` first") from None
    if not isinstance(raw, dict):
        raise C.ConfigError(f"{path} must contain a JSON object")
    return raw


def _edit(change: Callable[[List[dict]], str]) -> str:
    """Apply `change` to the raw `projects` list, validate the whole settings by reading them back, save
    (mode 600). One lock is held from the read to the save."""
    with C.config_lock():
        raw = _load_raw()
        projects = raw.get("projects", [])
        if not isinstance(projects, list):
            raise C.ConfigError("projects must be a list of objects")
        projects = [dict(p) if isinstance(p, dict) else p for p in projects]
        message = change(projects)
        raw["projects"] = projects
        C.from_dict(raw)  # never write something we could not read back
        C.save_raw(raw)
        return message


def _find(projects: List[dict], name: str) -> Optional[int]:
    wanted = C.clean_name(name).casefold()
    for i, item in enumerate(projects):
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].casefold() == wanted:
            return i
    return None


def add(name: str, chat_id: int, folders=(), paths=(), repos=(), title_prefixes=(), replace: bool = False) -> str:
    """Add a project group, or with `replace` point an existing one at another group (and change its rules
    when any are given). Group mode, the id, the names, the folder rules and the limit of
    MAX_PROJECTS groups are checked by the settings reader."""
    def change(projects: List[dict]) -> str:
        found = _find(projects, name)
        if found is not None and not replace:
            raise C.ConfigError(f"project {projects[found]['name']} already exists; remove it first")
        if found is None and len(projects) >= C.MAX_PROJECTS:
            raise C.ConfigError(f"at most {C.MAX_PROJECTS} project groups; remove one first")
        item = dict(projects[found]) if found is not None else {"name": C.clean_name(name)}
        item["chat_id"] = chat_id
        if folders:
            item["folders"] = list(folders)
        match = dict(item.get("match") or {})
        for key, value in (("paths", paths), ("repos", repos), ("title_prefixes", title_prefixes)):
            if value:
                match[key] = list(value)
        if match:
            item["match"] = match
        if found is None:
            projects.append(item)
        else:
            projects[found] = item
        return f"Project {item['name']}: group {chat_id} saved."
    return _edit(change)


def remove(name: str) -> str:
    def change(projects: List[dict]) -> str:
        found = _find(projects, name)
        if found is None:
            raise C.ConfigError(f"unknown project: {name}")
        gone = projects.pop(found)
        return (f"Project {gone['name']} removed. Its sessions move to the home group the next time they are "
                "active; the group itself is left alone.")
    return _edit(change)


def describe(cfg: Config) -> str:
    if not cfg.projects:
        return "No project groups: every session lives in the home group."
    lines = [f"{len(cfg.projects)} of {C.MAX_PROJECTS} project groups:"]
    for p in cfg.projects:
        lines.append(f"{p.name}: group {p.chat_id}")
        for label, values in (("folders", p.folders), ("paths", p.paths), ("repos", p.repos),
                              ("title prefixes", p.title_prefixes)):
            if values:
                lines.append(f"  {label}: {', '.join(values)}")
    return "\n".join(lines)


def pair(cfg: Config, bot, name: str, folders=(), paths=(), repos=(), title_prefixes=(),
         timeout: float = 180, now: Callable[[], float] = time.time) -> str:
    """Learn a project group by having the paired owner send /start@<bot> in it, check that it is a forum
    where the bot can manage topics, and save it as the project `name` (a new one, or the existing one
    pointed at this group). Only the owner's message counts."""
    from . import pairing
    from .telegram import TelegramError
    if not cfg.is_group_mode:
        raise C.ConfigError("project groups need group mode: run `harnessgram pair --group` first")
    if not cfg.owner_chat_id:
        raise C.ConfigError("pair the private chat first (harnessgram pair)")
    try:
        me = bot.call("getMe")
        print(f"In the {name} group, with your own account, send (waiting up to 3 minutes):\n\n"
              f"    /start@{me.get('username')}\n")
        found = pairing.wait_for_group_start(bot, me.get("username") or "", now(), cfg.owner_chat_id,
                                             timeout=timeout, now=now)
        if not found:
            raise C.ConfigError("no /start from you in a group; nothing saved")
        group = found["chat"]["id"]
        chat = bot.call("getChat", {"chat_id": group})
        mem = bot.call("getChatMember", {"chat_id": group, "user_id": me["id"]})
    except TelegramError as e:
        raise C.ConfigError(e.description) from None
    if not chat.get("is_forum"):
        raise C.ConfigError("turn on Topics in the group's settings, then run this again")
    if not (mem.get("status") == "creator" or (mem.get("status") == "administrator" and mem.get("can_manage_topics"))):
        raise C.ConfigError("make the bot an admin of the group with 'Manage topics', then run this again")
    exists = any(p.name.casefold() == C.clean_name(name).casefold() for p in cfg.projects)
    return add(name, group, folders, paths, repos, title_prefixes, replace=exists)
