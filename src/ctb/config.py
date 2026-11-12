"""Configuration file and private state directory.

Config lives in ~/.config/claude-telegram-bridge/config.json (mode 600). The
bot token is read from there only and is never printed, logged or put in a URL
that is shown anywhere. State (topic map, inbox queues, downloaded files) lives
in a private directory (mode 700). Both locations can be moved with the
CTB_CONFIG_DIR / CTB_STATE_DIR environment variables (used by the tests).
"""
from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import json
import logging
import math
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Callable, List, Optional

from .telegram import DEFAULT_API, TOKEN_RE, download_limit, upload_limit, valid_api_base

HOME = Path.home()


class ConfigError(Exception):
    pass


def config_dir() -> Path:
    return Path(os.environ.get("CTB_CONFIG_DIR") or HOME / ".config" / "claude-telegram-bridge")


def state_dir() -> Path:
    return Path(os.environ.get("CTB_STATE_DIR") or HOME / ".local" / "state" / "claude-telegram-bridge")


def config_path() -> Path:
    return config_dir() / "config.json"


def private_dir(path: Path) -> Path:
    """Create a directory only the current user can enter (makedirs honours umask, so chmod too)."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_private(path: Path, text: str) -> None:
    """Atomic write of a file readable only by the current user."""
    private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_map(path: Path) -> dict:
    """A small private JSON object file as a dict ({} when missing or damaged)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@contextlib.contextmanager
def locked_map(path: Path, keep: int = 200):
    """Read-modify-write of a small private JSON object shared between processes: the dict is yielded under an
    exclusive lock (<path>.lock) and written back, trimmed to its newest `keep` keys, only when it changed.
    Callers that run often check read_map(path) first and come here only when there is something to change."""
    private_dir(path.parent)
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read_map(path)
        before = json.dumps(data, sort_keys=True)
        yield data
        if json.dumps(data, sort_keys=True) != before:
            write_private(path, json.dumps(dict(list(data.items())[-keep:]), ensure_ascii=False))


# Noise settings are plain marker files: the file's presence means "on" (loud). Both default to off.
STATUS_REPOST = "status-repost"      # post a new status card after every reply instead of editing the old one
AGENT_NOTICES = "agent-notices"      # announce each finished background agent in its topic


ACTIVITY_LEVEL = "activity-level"    # how much of an agent's work its topic shows while it works
ACTIVITY_LEVELS = ("quiet", "normal", "full")


def activity_level() -> str:
    """quiet: the agent's own words only, no card of its tool steps; normal: the live card, the newest steps
    in full; full: more steps in full and more of each step's output. A missing or damaged file is normal."""
    try:
        value = (state_dir() / ACTIVITY_LEVEL).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return "normal"
    return value if value in ACTIVITY_LEVELS else "normal"


def set_activity_level(value: str) -> None:
    if value not in ACTIVITY_LEVELS:
        raise ValueError(value)
    path = state_dir() / ACTIVITY_LEVEL
    if value == "normal":
        path.unlink(missing_ok=True)
    else:
        write_private(path, value)


def setting_on(name: str) -> bool:
    return (state_dir() / name).exists()


def set_setting(name: str, on: bool) -> None:
    path = state_dir() / name
    if on:
        write_private(path, "")
    else:
        path.unlink(missing_ok=True)


def clean_name(name: str) -> str:
    """A display name that is safe to put into framing text: no brackets, angle brackets,
    control characters or line breaks, at most 40 characters."""
    name = " ".join(str(name).split())  # line breaks and tabs become single spaces
    name = "".join(c for c in name if c.isprintable() and c not in "[]()<>{}\"'`")
    return " ".join(name.split())[:40]


@dataclass
class TeamConfig:
    """Opt-in team group (off while chat_id is 0). Changed only through `ctb team ...` on this machine."""
    chat_id: int = 0  # a supergroup with topics, the bot is an admin there
    members: dict = field(default_factory=dict)  # {telegram user id (int): display name}
    shared_sessions: List[str] = field(default_factory=list)
    block_patterns: List[str] = field(default_factory=list)  # a part matching any of these is withheld

    @property
    def enabled(self) -> bool:
        return bool(self.chat_id)

    def shares(self, session: str) -> bool:
        return self.enabled and session in self.shared_sessions

    def to_raw(self) -> dict:
        return {"chat_id": self.chat_id, "members": {str(k): v for k, v in self.members.items()},
                "shared_sessions": list(self.shared_sessions), "block_patterns": list(self.block_patterns)}


def team_from_raw(raw) -> TeamConfig:
    if not isinstance(raw, dict):
        raise ConfigError("team must be an object")
    team = TeamConfig()
    if "chat_id" in raw:
        try:
            team.chat_id = int(raw["chat_id"])
        except (TypeError, ValueError):
            raise ConfigError("team.chat_id must be a number") from None
    members = raw.get("members", {})
    if not isinstance(members, dict):
        raise ConfigError("team.members must be an object {user id: name}")
    for uid, name in members.items():
        try:
            uid_int = int(uid)
        except (TypeError, ValueError):
            raise ConfigError("team.members keys must be numeric Telegram user ids") from None
        if uid_int <= 0 or not clean_name(name):
            raise ConfigError("team.members needs positive user ids and non-empty names")
        team.members[uid_int] = clean_name(name)
    shared = raw.get("shared_sessions", [])
    patterns = raw.get("block_patterns", [])
    for label, value in (("shared_sessions", shared), ("block_patterns", patterns)):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"team.{label} must be a list of strings")
    for pat in patterns:
        try:
            re.compile(pat)
        except re.error:
            raise ConfigError("team.block_patterns contains an invalid regular expression") from None
    team.shared_sessions, team.block_patterns = list(shared), list(patterns)
    return team


MAX_PROJECTS = 10


@dataclass
class ProjectConfig:
    """One project group: an owner-only forum supergroup that holds the sessions of one project.
    Opt-in; with no projects configured every session lives in the home group."""
    name: str
    chat_id: int  # a supergroup with topics, the bot is an admin there
    folders: List[str] = field(default_factory=list)  # picker defaults for a new session started in this group
    paths: List[str] = field(default_factory=list)  # a session whose folder matches any of these belongs here
    repos: List[str] = field(default_factory=list)  # git origin or common-dir names
    title_prefixes: List[str] = field(default_factory=list)  # text before the first ":" of a session title

    def to_raw(self) -> dict:
        raw = {"name": self.name, "chat_id": self.chat_id}
        if self.folders:
            raw["folders"] = list(self.folders)
        match = {key: list(value) for key, value in (("paths", self.paths), ("repos", self.repos),
                                                     ("title_prefixes", self.title_prefixes)) if value}
        if match:
            raw["match"] = match
        return raw


def _string_list(label: str, value, what: str = "strings") -> List[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{label} must be a list of non-empty {what}")
    return [v.strip() for v in value]


_GLOB = re.compile(r"[*?\[]")


def _literal_prefix(rule: str) -> str:
    found = _GLOB.search(rule)
    return rule[:found.start()] if found else rule


def _under(path: str, folder: str) -> bool:
    folder = folder.rstrip(os.sep)
    return path == folder or path.startswith(folder + os.sep)


def _project_paths(label: str, value, generic: List[str]) -> List[str]:
    """Absolute folder rules. A rule that matches the home folder, the projects root or ~/Documents,
    or any folder above them, would pull every unrelated session into one project, so it is refused."""
    paths = _string_list(label, value, "folder paths")
    for path in paths:
        expanded = os.path.normpath(os.path.expanduser(path))
        if not os.path.isabs(expanded):
            raise ConfigError(f"{label} must be absolute folder paths")
        literal = _GLOB.search(expanded) is None
        real = os.path.realpath(expanded) if literal else expanded
        # A pattern's `*` also spans "/", so `~/Docs/*` reaches every folder below ~/Docs: its fixed
        # leading part may not be a guarded folder or above one.
        fixed = real if literal else (_literal_prefix(expanded).rstrip(os.sep) or os.sep)
        if (expanded == os.sep or any(fnmatch.fnmatchcase(folder, expanded) for folder in generic)
                or any(_under(folder, fixed) for folder in generic)):
            raise ConfigError(f"{label} may not match your home folder, projects_root or ~/Documents")
    return paths


def _rules_overlap(a: str, b: str) -> bool:
    """True when one folder rule can match a folder the other also matches (a rule matches its folder
    and everything below it; `*` and `?` patterns are compared by their fixed leading part)."""
    a, b = os.path.normpath(os.path.expanduser(a)), os.path.normpath(os.path.expanduser(b))
    glob_a, glob_b = _GLOB.search(a) is not None, _GLOB.search(b) is not None
    if not glob_a and not glob_b:
        a, b = os.path.realpath(a), os.path.realpath(b)
        return _under(a, b) or _under(b, a)
    if glob_a and glob_b:
        la, lb = _literal_prefix(a), _literal_prefix(b)
        return la.startswith(lb) or lb.startswith(la)
    literal, pattern = (b, a) if glob_a else (a, b)
    literal = os.path.realpath(literal)
    return (fnmatch.fnmatchcase(literal, pattern) or fnmatch.fnmatchcase(literal, pattern + os.sep + "*")
            or _under(_literal_prefix(pattern), literal))


def projects_from_raw(raw, projects_root: str = "") -> List[ProjectConfig]:
    if not isinstance(raw, list) or not all(isinstance(v, dict) for v in raw):
        raise ConfigError("projects must be a list of objects")
    if len(raw) > MAX_PROJECTS:
        raise ConfigError(f"projects may hold at most {MAX_PROJECTS} groups")
    home = os.path.expanduser("~")
    generic = [os.path.normpath(p) for p in (home, os.path.join(home, "Documents"),
                                             os.path.expanduser(projects_root) if projects_root else "") if p]
    projects: List[ProjectConfig] = []
    for item in raw:
        name = clean_name(item.get("name", "")) if isinstance(item.get("name"), str) else ""
        if not name:
            raise ConfigError("projects[].name must be a non-empty name")
        chat = item.get("chat_id")
        if isinstance(chat, bool) or not isinstance(chat, int) or not re.fullmatch(r"-100\d{9,}", str(chat)):
            raise ConfigError("projects[].chat_id must be a supergroup id (-100 followed by at least 9 digits)")
        match = item.get("match", {})
        if not isinstance(match, dict):
            raise ConfigError("projects[].match must be an object")
        project = ProjectConfig(name=name, chat_id=chat)
        project.folders = _project_paths("projects[].folders", item.get("folders", []), generic)
        project.paths = _project_paths("projects[].match.paths", match.get("paths", []), generic)
        project.repos = _string_list("projects[].match.repos", match.get("repos", []))
        project.title_prefixes = _string_list("projects[].match.title_prefixes", match.get("title_prefixes", []))
        projects.append(project)
    for i, first in enumerate(projects):
        for second in projects[i + 1:]:
            if any(_rules_overlap(a, b) for a in first.folders + first.paths for b in second.folders + second.paths):
                raise ConfigError(f"projects {first.name} and {second.name} have overlapping folder rules")
    for label, key in (("repos", "repos"), ("title prefixes", "title_prefixes")):
        seen = {}
        for project in projects:
            for value in {v.strip().casefold() for v in getattr(project, key)}:
                if value in seen:
                    raise ConfigError(f"projects {seen[value]} and {project.name} share the {label} rule {value!r}")
                seen[value] = project.name
    if len({p.name.casefold() for p in projects}) != len(projects):
        raise ConfigError("projects[].name values must be unique")
    if len({p.chat_id for p in projects}) != len(projects):
        raise ConfigError("projects[].chat_id values must be unique")
    return projects


@dataclass
class Config:
    bot_token: str = ""
    bot_token_command: list = field(default_factory=list)
    # Optional self-hosted Bot API server (`telegram-bot-api --local`): files up to 2000 MB both ways
    # instead of 20 MB down / 50 MB up. api_base is its URL (plain http only on this machine),
    # api_local says it runs with --local, api_files_dir is its working directory: the only place
    # downloads are read from. See the README section "Large files".
    api_base: str = DEFAULT_API
    api_local: bool = False
    api_files_dir: str = ""
    owner_chat_id: int = 0
    widgets_enabled: bool = False
    # Optional supergroup id (negative number). Default = 0 (private chat, uses owner_chat_id).
    chat_id: int = 0
    # UI language for menus, buttons, toasts, and alerts: "en" or "he".
    ui_language: str = ""
    # Language replies are shown/mirrored in (ISO 639-1 code, "he", "ar", "ru", "es", ...).
    target_language: str = "he"
    # Language Claude writes in when translate mode is on.
    source_language: str = "en"
    # "Reply in English + Summary line" mode: Claude answers in source_language, ends with a
    # "Summary:" line, and the Stop hook shows/mirrors a translation to target_language.
    translate: bool = False
    # Mirror every reply to the session's Telegram topic.
    mirror: bool = True
    # Suppress replies to background-only transcript notifications.
    quiet_background_turns: bool = True
    # Block a reply that is not in target_language and ask for a rewrite (translate off, or failed).
    enforce_language: bool = False
    # Replace credential-looking strings with [redacted] before text leaves the machine.
    redact_outgoing: bool = True
    # Order of a translated reply. "edit" (default): the reply goes out at once as Claude wrote it, then that
    # same message is edited in place to the translation with the original folded under it ("edit-off": no
    # fold). "folded-after" ("after" is an alias): wait a few seconds for the translation and send it first
    # with the original folded under it; if it is slow or fails, the original goes out first. "before":
    # always the original first, then the translation. "off": the translation only (the original is sent
    # only when the translation is slow or fails).
    original: str = "edit"
    # A Telegram message that is only one of these words stops the session after the current tool call.
    stop_words: List[str] = field(default_factory=lambda: ["stop", "/stop", "עצור", "תעצור", "די"])
    # Translation engines in order; the first that answers wins. Default: offline only ("mlx" =
    # TranslateGemma on Apple silicon, "nllb" = NLLB-200; both are optional extras). "gemini" uses
    # the official Gemini API with your own key. "google" is an explicit opt-in: it calls Google's
    # UNOFFICIAL, undocumented web translate endpoints (no key, no SLA, may break or be blocked;
    # check Google's terms before using it) and it sends the text to Google.
    engines: List[str] = field(default_factory=lambda: ["mlx", "nllb"])
    gemini_model: str = "gemini-2.5-flash-lite"
    gemini_vertex_project: str = ""
    gemini_vertex_account: str = ""
    gemini_token_command: List[str] = field(default_factory=list)
    gemini_key_command: List[str] = field(default_factory=list)
    gemini_glossary: dict = field(default_factory=dict)
    # Thinking level sent to Gemini 3 models ("low", "medium", "high"; "" leaves the model default).
    gemini_thinking: str = ""
    # A second Gemini model tried only when the first one fails (error, refusal or no answer), within the same
    # time budget; "" = none. A fast model suits it (e.g. "gemini-3.5-flash-lite").
    gemini_fallback_model: str = ""
    # Team radio (`ctb radio`): a narrated live stream in the home group. Off unless configured; see docs/radio.md.
    radio: dict = field(default_factory=dict)
    # The Cockpit's Morning Brief (brief.py, docs/panel.md): where its work lines come from ("ledger_dir" or
    # "ledger_command"; the radio's ledger_dir otherwise), "project_names", and an optional "events_file".
    brief: dict = field(default_factory=dict)
    # Optional trusted local Studio data adapter; empty keeps the built-in sample.
    studio_adapter: str = ""
    # Read-only media folder; empty uses the adapter's MEDIA_ROOT when provided.
    studio_media_root: str = ""
    # A pinned control-panel button in every session topic (docs/panel.md). It only helps once the panel Mini App
    # is set up, so None (default) = on only when the panel service is installed; true or false forces it.
    panel_button: Optional[bool] = None
    # The bridge names each session topic itself, "project: task" in the target language, and keeps it current
    # (topic_names.py). A name set by hand (in Telegram or with `ctb topic-rename`) always wins.
    auto_topic_names: bool = False
    topic_check_reminders: bool = False
    auto_account_switch: bool = False
    quota_auto_continue: bool = False
    session_auto_revive: bool = False
    # Hard daily budget of Gemini requests for topic names (every route attempt counts, failed ones too; shared by
    # the listener and the CLI). 0 = never ask Gemini; names then come from the first request only.
    topic_names_daily_calls: int = 150
    # Folder or repository name prefix -> project label for topic names ({"my-app": "My app"}). Empty = none.
    topic_project_names: dict = field(default_factory=dict)
    # Project label -> one line on what it is about, so a session started in the projects root is placed well.
    topic_project_about: dict = field(default_factory=dict)
    mlx_model_fast: str = "mlx-community/translategemma-4b-it-4bit"
    mlx_model_quality: str = "mlx-community/translategemma-12b-it-4bit"
    nllb_model_dir: str = ""
    # Voice notes: path to a whisper.cpp ggml model; empty disables transcription.
    whisper_model: str = ""
    voice_language: str = ""  # defaults to target_language
    # Telegram messages kept per session while it is busy; beyond this the oldest are dropped (with a note).
    max_queue: int = 50
    # Delete downloaded pictures/files older than this many days (checked by `ctb serve`); 0 keeps them.
    cleanup_days: int = 0
    # Large files (over the upload limit, 45 MB on the cloud server) are never uploaded: with this https base URL set they are placed in a
    # private share directory and only the link is sent. Empty (default) = the feature is off.
    share_base_url: str = ""
    panel_base_url: str = ""  # the Cockpit's own public https origin (the YouTube sign-in returns there)
    # A small "context size" line under the last message of each reply, with a /compact hint from
    # this many tokens up (0 = never hint).
    context_footer: bool = True
    context_hint_tokens: int = 400_000
    # Cache translations on disk (private folder) so the hook and the mirror translate a text once.
    translate_cache: bool = True
    # Live progress: edit one message with recent tool calls while working, delete when done.
    progress: bool = False
    progress_details: bool = False
    # Forum group only: one pinned index message per session topic, edited after each reply with
    # links to the last `pin_limit` replies (the pinned bar jumps between replies; 0 = no index).
    pin_replies: bool = True
    pin_limit: int = 15
    # Team group (opt-in). See the README section "Team group" before turning this on.
    team: TeamConfig = field(default_factory=TeamConfig)
    # Project groups (opt-in, group mode only): one owner-only forum supergroup per project, next to
    # the home group. Empty (default) = every session lives in the home group.
    projects: List[ProjectConfig] = field(default_factory=list)
    # Extra sentence appended to the style reminder injected before every prompt.
    extra_reminder: str = ""
    # Voice summary in car mode (read summary aloud with TTS)
    voice: bool = True
    # Folder where Claude Code projects live. The /new picker lists recent projects under it and new
    # sessions start here when no folder is known. Default: your home directory.
    projects_root: str = ""
    # Extra quota gauges (all off by default). `codex_sessions_dir`: the Codex CLI sessions folder
    # to show its 5-hour and weekly limits. `quota_files`: JSON files any
    # tool can write, [{"label": "My tool", "path": "~/usage.json"}]; the file format is in the README.
    codex_sessions_dir: str = ""
    # Existing persisted sessions only; discovery alone never attaches a session.
    codex_harness_enabled: bool = False
    codex_attached_sessions: List[str] = field(default_factory=list)
    codex_commentary: bool = False
    quota_files: List[dict] = field(default_factory=list)
    # USD per million tokens: input, output, cache write, cache read.
    usage_prices: dict = field(default_factory=dict)
    # Other tools your sessions delegate work to (all off by default). Each entry is
    # {"match": "text in the command line", "label": "Name shown"}: a running process whose command
    # line contains `match` is listed on the dashboard ("Background jobs now"), and a Bash call that
    # contains it is labelled with `label` in the live progress card.
    delegates: List[dict] = field(default_factory=list)
    # Send a full HTML work log of a session to its topic after a finished task with 20+ new actions
    # (checked by `harnessgram dashboard`). Off by default; `harnessgram session-log` always works.
    session_log_auto: bool = False
    # A sentence added to a session's context at most every `periodic_reminder_minutes`, once its
    # transcript is over `periodic_reminder_min_kb` KB (a long session): for example "Save one
    # verified lesson from this stretch". Empty (default) = off.
    periodic_reminder: str = ""
    periodic_reminder_minutes: int = 90
    periodic_reminder_min_kb: int = 1500
    _cached_token: Optional[str] = field(default=None, repr=False, compare=False, init=False)

    def __post_init__(self) -> None:
        if not self.ui_language:
            self.ui_language = "he" if self.target_language == "he" else "en"

    def __repr__(self) -> str:  # the token must never reach logs or tracebacks
        return f"Config(target_language={self.target_language!r}, translate={self.translate!r}, token=<hidden>)"

    def token(self) -> str:
        if self.bot_token:
            return self.bot_token
        if self._cached_token:
            return self._cached_token
        if self.bot_token_command:
            try:
                proc = subprocess.run(
                    self.bot_token_command,
                    capture_output=True,
                    timeout=20,
                    shell=False,
                )
                if proc.returncode != 0:
                    raise RuntimeError
                out = proc.stdout.decode("utf-8") if isinstance(proc.stdout, bytes) else proc.stdout
                val = out.strip()
                if not TOKEN_RE.match(val):
                    raise ValueError
                self._cached_token = val
                return val
            except Exception:
                raise ConfigError("bot token command failed") from None
        raise ConfigError("bot token command failed")

    @property
    def download_limit(self) -> int:
        return download_limit(self.api_local)

    @property
    def upload_limit(self) -> int:
        return upload_limit(self.api_local)

    @property
    def ready(self) -> bool:
        return bool((self.bot_token or self.bot_token_command) and self.owner_chat_id)

    @property
    def projects_dir(self) -> Path:
        """The folder holding your projects (the `projects_root` setting, default: the home directory)."""
        return Path(self.projects_root).expanduser() if self.projects_root else Path.home()

    @property
    def voice_lang(self) -> str:
        return self.voice_language or self.target_language

    @property
    def owner_user_id(self) -> int:
        return self.owner_chat_id

    @property
    def is_group_mode(self) -> bool:
        return bool(self.chat_id and self.chat_id < 0)

    @property
    def home_chat(self) -> int:
        return home_chat(self)


def home_chat(cfg: Config) -> int:
    """The supergroup chat_id when configured (negative int), otherwise owner_chat_id."""
    return cfg.chat_id if (cfg.chat_id and cfg.chat_id < 0) else cfg.owner_chat_id


def from_dict(raw: dict) -> Config:
    known = {f.name: f for f in fields(Config) if f.init}
    kwargs = {}
    for key, value in raw.items():
        if key not in known:
            continue  # ignore unknown keys: newer config, older program
        if key == "bot_token":
            value = str(value).strip()
            if value and not TOKEN_RE.match(value):  # never echo the value: it is a secret
                raise ConfigError("bot_token has an invalid format (expected 123456:ABC...)")
        elif key in ("max_queue", "cleanup_days", "context_hint_tokens", "periodic_reminder_minutes",
                     "periodic_reminder_min_kb", "pin_limit", "topic_names_daily_calls"):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ConfigError(f"{key} must be a whole number, 0 or more")
        elif key == "owner_chat_id":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ConfigError("owner_chat_id must be a number") from None
        elif key == "chat_id":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ConfigError("chat_id must be a number") from None
            if value > 0:
                raise ConfigError("chat_id must be a negative supergroup id")
        elif key == "api_base":
            value = str(value).strip().rstrip("/") or DEFAULT_API
            if not valid_api_base(value):
                raise ConfigError("api_base must be an https:// URL (http:// only for localhost or 127.0.0.1)")
        elif key == "api_files_dir":
            if not isinstance(value, str):
                raise ConfigError("api_files_dir must be a path string")
            value = value.strip()
            if value and (not os.path.isabs(os.path.expanduser(value)) or os.path.normpath(value) == os.sep):
                raise ConfigError("api_files_dir must be an absolute folder path (not /)")
            value = os.path.expanduser(value)
        elif key in ("studio_adapter", "studio_media_root"):
            if not isinstance(value, str):
                raise ConfigError(f"{key} must be a path string")
            value = os.path.expanduser(value.strip())
            if value and (not os.path.isabs(value) or "\x00" in value or os.path.normpath(value) == os.sep):
                raise ConfigError(f"{key} must be an absolute path (not /)")
        elif key == "panel_base_url":
            value = str(value).strip().rstrip("/")
            if value and not re.fullmatch(r"https://[A-Za-z0-9.-]{1,253}(?::\d{1,5})?", value):
                raise ConfigError("panel_base_url must be an https:// origin (scheme, host and port only)")
        elif key == "share_base_url":
            value = str(value).strip()
            if value and not value.startswith("https://"):
                raise ConfigError("share_base_url must be an https:// URL (a share link must be secure)")
        elif key in ("engines", "stop_words", "bot_token_command", "gemini_token_command", "gemini_key_command",
                     "codex_attached_sessions"):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"{key} must be a list of strings")
        elif key in ("radio", "brief"):
            if not isinstance(value, dict):
                raise ConfigError(f"{key} must be an object")
        elif key in ("topic_project_names", "topic_project_about"):
            if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
                raise ConfigError(f"{key} must be an object of strings")
        elif key == "gemini_glossary":
            if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
                raise ConfigError("gemini_glossary must be an object of strings")
        elif key in ("projects_root", "codex_sessions_dir"):
            if not isinstance(value, str):
                raise ConfigError(f"{key} must be a path string")
            value = value.strip()
        elif key == "quota_files":
            if not isinstance(value, list) or not all(isinstance(v, dict) and isinstance(v.get("path"), str) for v in value):
                raise ConfigError('quota_files must be a list of objects like {"label": "...", "path": "..."}')
        elif key == "usage_prices":
            if not isinstance(value, dict) or not all(
                isinstance(k, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", k)
                and isinstance(v, list) and len(v) == 4
                and all(not isinstance(n, bool) and isinstance(n, (int, float))
                        and (isinstance(n, int) or math.isfinite(n)) and 0 <= n <= 1_000_000_000 for n in v)
                for k, v in value.items()
            ):
                raise ConfigError("usage_prices must map model id prefixes to four finite non-negative numbers")
        elif key == "delegates":
            if not isinstance(value, list) or not all(
                    isinstance(v, dict) and isinstance(v.get("match"), str) and v["match"].strip()
                    and isinstance(v.get("label"), str) and v["label"].strip() for v in value):
                raise ConfigError('delegates must be a list of objects like {"match": "...", "label": "..."}')
        elif key == "periodic_reminder":
            if not isinstance(value, str):
                raise ConfigError("periodic_reminder must be a string")
            value = value.strip()
        elif key == "team":
            value = team_from_raw(value)
        elif key == "projects":
            root = raw.get("projects_root")
            value = projects_from_raw(value, root.strip() if isinstance(root, str) else "")
        elif key == "original":
            if value not in ("edit", "edit-off", "before", "after", "folded-after", "off"):
                raise ConfigError('original must be "edit", "edit-off", "folded-after", "before", "after" or "off"')
        elif key == "panel_button":
            if value is not None and not isinstance(value, bool):
                raise ConfigError("panel_button must be true, false or null")
        elif key == "ui_language":
            value = str(value).strip().lower()
            if value not in ("en", "he"):
                raise ConfigError('ui_language must be "en" or "he"')
        elif isinstance(known[key].default, bool) and not isinstance(value, bool):
            raise ConfigError(f"{key} must be true or false")
        kwargs[key] = value
    if "ui_language" not in kwargs:
        tgt = kwargs.get("target_language", raw.get("target_language", "he"))
        if tgt == "he" and ("bot_token" in raw or "owner_chat_id" in raw or "target_language" in raw):
            kwargs["ui_language"] = "he"
        else:
            kwargs["ui_language"] = "en"
    cfg = Config(**kwargs)
    if cfg.api_local and cfg.api_base == DEFAULT_API:
        raise ConfigError("api_local needs api_base to point at your own Bot API server")
    if cfg.chat_id and cfg.team.enabled and cfg.chat_id == cfg.team.chat_id:
        # the team group takes precedence in the listener, so every home message would be dropped
        raise ConfigError("chat_id (home group) and team.chat_id must be different groups")
    if cfg.projects and not cfg.is_group_mode:
        raise ConfigError("projects need group mode (set chat_id to the home supergroup)")
    for project in cfg.projects:
        if project.chat_id == cfg.chat_id or (cfg.team.enabled and project.chat_id == cfg.team.chat_id):
            raise ConfigError("projects[].chat_id must differ from chat_id and team.chat_id")
    return cfg


def load(path: Optional[Path] = None) -> Config:
    path = path or config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"no config at {path}; run `harnessgram setup`") from None
    except (OSError, ValueError):
        raise ConfigError(f"cannot read {path} (invalid JSON?)") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    # Validate before migrating; re-read under the same lock used by settings edits.
    from_dict(raw)
    if "release_defaults_v1" not in raw:
        state = state_dir()
        # The lock may itself be created in the state directory when the two
        # configured directories coincide; distinguish it from prior state.
        lock_path = config_dir() / "config.lock"
        lock_existed = lock_path.exists()
        with config_lock():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if "release_defaults_v1" not in raw:
                try:
                    existing = any(entry != path and (entry != lock_path or lock_existed)
                                   for entry in state.iterdir())
                except FileNotFoundError:
                    existing = False
                raw.setdefault("auto_account_switch", existing and not (state / "account-auto-off").exists())
                for key in ("quota_auto_continue", "session_auto_revive", "progress", "progress_details",
                            "auto_topic_names", "topic_check_reminders"):
                    raw.setdefault(key, existing)
                raw["release_defaults_v1"] = True
                from_dict(raw)
                try:
                    save_raw(raw, path)
                except OSError as exc:
                    logging.getLogger(__name__).warning("Could not save migrated settings: %s", exc)
    cfg = from_dict(raw)
    from .i18n import set_lang
    set_lang(cfg.ui_language)
    return cfg


def loose_mode(path: Path) -> Optional[str]:
    """The file's mode (e.g. '644') when group/others can read it, else None."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    return format(mode, "o") if mode & 0o077 else None


def _is_real_settings(path: Path) -> bool:
    """True for the machine's real config file (the login user's home, whatever HOME says)."""
    import pwd
    real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "claude-telegram-bridge" / "config.json"
    try:
        return path.resolve() == real.resolve()
    except OSError:
        return False


@contextlib.contextmanager
def config_lock():
    """Exclusive lock for a read-change-save of the settings. Not reentrant: take it only around the
    fresh read and the save, never around a wait or a network call, and never call it while holding it."""
    root = private_dir(config_dir())
    with open(root / "config.lock", "a") as lock:
        os.chmod(root / "config.lock", 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save_changes(before: dict, after: dict, path: Optional[Path] = None, replace_owner: bool = False,
                 check: Optional[Callable[[dict], None]] = None) -> Path:
    """Save what `after` changed compared with `before` onto a fresh read of the file, under the lock, so
    an edit made while a slow command waited (a team member added by a tap) is not overwritten. `check` sees
    the fresh file (an empty dict when there is none) under the same lock and may raise to stop the save."""
    path = path or config_path()
    with config_lock():
        try:
            fresh = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            fresh = None
        if check is not None:
            check(fresh if isinstance(fresh, dict) else {})
        if not isinstance(fresh, dict):
            fresh = dict(after)
        else:
            for key in before.keys() - after.keys():
                fresh.pop(key, None)
            for key, value in after.items():
                if key not in before or before[key] != value:
                    fresh[key] = value
        saved = save_raw(fresh, path, replace_owner=replace_owner)
        if "auto_account_switch" in after and before.get("auto_account_switch") != after["auto_account_switch"]:
            set_setting("account-auto-off", not after["auto_account_switch"])
        return saved


def save_raw(raw: dict, path: Optional[Path] = None, replace_owner: bool = False) -> Path:
    """Write the settings. Refuses to replace a config that belongs to another owner (a stray test or
    probe once overwrote the live settings with sample values), and keeps the previous file as
    config.json.bak so a bad write can be undone."""
    path = path or config_path()
    if os.environ.get("PYTEST_CURRENT_TEST") and _is_real_settings(path):
        raise ConfigError("refusing to write the real settings from a test (set CTB_CONFIG_DIR)")
    try:
        old_text = path.read_text(encoding="utf-8")
    except OSError:
        old_text = None
    try:
        old = json.loads(old_text) if old_text is not None else None
    except ValueError:
        old = None  # unreadable: still backed up below, so a bad write can be undone
    if isinstance(old, dict) and not replace_owner:
        was, now = old.get("owner_chat_id"), raw.get("owner_chat_id")
        if was and str(was) != str(now):
            raise ConfigError("refusing to replace the settings of another owner (owner_chat_id differs)")
    if old_text is not None:
        write_private(path.with_name(path.name + ".bak"), old_text)
    write_private(path, json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
    return path
