"""`harnessgram doctor`: check that everything the bridge needs is in place.

Prints one line per check: ok / warn / FAIL; every warn/FAIL names one concrete fix.
Exit code 1 when anything FAILs. The bot token is never printed.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from typing import List, Tuple

from . import accounts
from . import config as C
from .install import SERVICES, is_hook_command, service_files, service_states, settings_path
from .telegram import DEFAULT_API, Bot, TelegramError

Result = Tuple[str, str]  # (level, message)


def check_config() -> Tuple[List[Result], "C.Config | None"]:
    path = C.config_path()
    try:
        cfg = C.load(path)
    except C.ConfigError as e:
        return [("FAIL", str(e))], None
    out: List[Result] = []
    loose = C.loose_mode(path)
    out.append(("warn", f"{path} is readable by others (mode {loose}); run: chmod 600 {path}") if loose
               else ("ok", f"config {path} (mode 600)"))
    if cfg.bot_token:
        out.append(("ok", "token: in config"))
    elif cfg.bot_token_command:
        out.append(("ok", "token: from command"))
    else:
        out.append(("FAIL", "token is missing: run `harnessgram setup`"))
    out.append(("ok", "owner_chat_id set") if cfg.owner_chat_id else
               ("FAIL", "owner_chat_id is missing (run `harnessgram setup`, or `harnessgram pair`)"))
    out.append(("ok", f"target_language={cfg.target_language}, translate={cfg.translate}"))
    from . import policy
    out.append(("ok", "mirroring is on") if policy.on() else
               ("warn", 'mirroring is off ("mirror" is not true): the bridge sends and receives nothing'))
    return out, cfg


def check_bot(cfg: "C.Config", bot=None) -> List[Result]:
    try:
        bot = bot or Bot.from_config(cfg, strict=True)
    except (TelegramError, C.ConfigError) as e:
        return [("FAIL", str(e))]
    try:
        me = bot.call("getMe", timeout=10)
    except TelegramError as e:
        return [("FAIL", f"Telegram getMe failed: {e.description}. Fix: check the token "
                         "(BotFather > /mybots > your bot > API Token) and that this machine can reach api.telegram.org")]
    out = [("ok", f"bot @{me.get('username')} answers")]
    if C.home_chat(cfg) < 0:  # sessions live in group topics; Threaded Mode only covers the private chat
        out.append(("ok", "sessions live in a group, so the private-chat Threaded Mode setting is not needed"))
    elif me.get("has_topics_enabled"):
        out.append(("ok", "Threaded Mode is on (one topic per session works)"))
    else:
        out.append(("FAIL", "Threaded Mode is off. Fix: BotFather > /mybots > your bot > Bot Settings > "
                            "Threaded Mode > turn on. Without it there are no per-session topics."))
    if cfg.team.enabled and not me.get("can_read_all_group_messages"):
        out.append(("warn", "bot privacy mode is on, so the bot only sees commands in the team group unless it is an "
                            "admin there. Fix: make the bot a group admin, or BotFather > Bot Settings > "
                            "Group Privacy > Turn off"))
    else:
        out.append(("ok", "bot privacy mode: " + ("off" if me.get("can_read_all_group_messages") else
                                                   "on (fine: your sessions live in your private chat with the bot)")))
    return out


def check_tmux(run=subprocess.run, which=shutil.which) -> List[Result]:
    path = which("tmux")
    if not path:
        return [("warn", "tmux not found. Sessions started from Telegram and /esc, /compact need it. "
                         "Fix: brew install tmux (macOS) or sudo apt install tmux (Linux)")]
    try:
        version = run([path, "-V"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        version = ""
    m = re.search(r"(\d+)\.(\d+)", version)
    if m and (int(m[1]), int(m[2])) < (2, 6):
        return [("warn", f"{version} is old. Fix: upgrade tmux to 2.6 or newer")]
    return [("ok", version or "tmux found")]


def check_claude(auth=None, binary=None) -> List[Result]:
    """The `claude` executable and its login."""
    exe = (binary or accounts.claude_binary)()
    if exe == "claude" and not shutil.which("claude"):
        return [("warn", "`claude` not found. Fix: install Claude Code (https://claude.com/claude-code) "
                         "and make sure it is on PATH")]
    out: List[Result] = [("ok", f"claude binary: {exe}")]
    status = (auth or accounts.auth_status)("main", 15)
    if status.get("loggedIn") is False:
        out.append(("warn", "Claude Code is not logged in. Fix: run `claude auth login`"))
    elif status.get("loggedIn") or status.get("email"):
        out.append(("ok", "Claude Code is logged in"))
    else:
        out.append(("warn", "could not read `claude auth status`. Fix: run `claude auth login` once in a terminal"))
    return out


def check_team(cfg: "C.Config", bot=None) -> List[Result]:
    t = cfg.team
    if not t.enabled:
        return []
    out: List[Result] = [("ok", f"team group on: {len(t.members)} member(s), {len(t.shared_sessions)} shared "
                                f"session(s), {len(t.block_patterns)} block pattern(s)")]
    if not t.members:
        out.append(("warn", "team group has no members yet (harnessgram team add-member <id> <name>)"))
    try:
        bot = bot or Bot.from_config(cfg, strict=True)
    except (TelegramError, C.ConfigError) as e:
        out.append(("FAIL", f"team group check failed: {e}"))
        return out
    try:
        chat = bot.call("getChat", {"chat_id": t.chat_id}, timeout=10)
        out.append(("ok", "team chat is a supergroup with topics") if chat.get("is_forum") else
                   ("FAIL", "team chat does not have Topics turned on (group settings, Topics)"))
        me = bot.call("getMe", timeout=10)
        mem = bot.call("getChatMember", {"chat_id": t.chat_id, "user_id": me["id"]}, timeout=10)
        ok = mem.get("status") == "creator" or (mem.get("status") == "administrator" and mem.get("can_manage_topics"))
        out.append(("ok", "bot is an admin that can manage topics (it also receives all group messages)") if ok else
                   ("FAIL", "make the bot an admin of the team group with 'Manage topics'"))
    except TelegramError as e:
        out.append(("FAIL", f"team group check failed: {e.description}"))
    return out


def check_projects(cfg: "C.Config", bot=None) -> List[Result]:
    """Each project group: a forum, the bot an admin who can manage topics, pin and delete, and nobody
    else in it (a project group shows everything mirrored)."""
    if not cfg.projects:
        return []
    out: List[Result] = []
    try:
        bot = bot or Bot.from_config(cfg, strict=True)
        me = bot.call("getMe", timeout=10)
    except (TelegramError, C.ConfigError) as e:
        return [("FAIL", f"project groups check failed: {getattr(e, 'description', e)}")]
    for project in cfg.projects:
        label = f"project {project.name}"
        try:
            chat = bot.call("getChat", {"chat_id": project.chat_id}, timeout=10)
            out.append(("ok", f"{label}: a supergroup with topics") if chat.get("is_forum") else
                       ("FAIL", f"{label}: turn on Topics in the group's settings"))
            mem = bot.call("getChatMember", {"chat_id": project.chat_id, "user_id": me["id"]}, timeout=10)
            rights = (("can_manage_topics", "Manage topics"), ("can_pin_messages", "Pin messages"),
                      ("can_delete_messages", "Delete messages"))
            if mem.get("status") == "creator":
                missing = []
            elif mem.get("status") == "administrator":
                missing = [name for key, name in rights if not mem.get(key)]
            else:
                missing = [name for _, name in rights]
            out.append(("ok", f"{label}: the bot is an admin with topics, pin and delete") if not missing else
                       ("FAIL", f"{label}: make the bot an admin of the group with {', '.join(missing)}"))
            count = bot.call("getChatMemberCount", {"chat_id": project.chat_id}, timeout=10)
            if isinstance(count, int) and count > 2:
                out.append(("warn", f"{label}: {count} members; everything mirrored there is visible to all of "
                                    "them. Fix: keep the group to you and the bot"))
        except TelegramError as e:
            out.append(("FAIL", f"{label}: check failed: {e.description}"))
    return out


def check_listener(restarted: bool = False, old_run: "str | None" = None) -> List[Result]:
    """The listener's own state: its lock is held and its status file says it polled Telegram lately. After a
    restart (`restarted`), only a status with a run id other than `old_run` (the one read before the restart)
    counts: each listener start writes its own id, so the previous listener's report never vouches for the
    new one, whenever it was written. No clock or file time is compared."""
    base = C.state_dir()
    lock = base / "updates.lock"
    held = False
    if lock.exists():
        with open(lock, "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
            except BlockingIOError:
                held = True
    if not held:
        return [("FAIL", "listener is not running. Fix: harnessgram install (starts the service), "
                         "or try it in a terminal: harnessgram serve")]
    try:
        status = json.loads((base / "listener.status.json").read_text(encoding="utf-8"))
        age = time.time() - float(status.get("at", 0))
    except (OSError, ValueError):
        return [("warn", "listener is running but has not reported status yet")]
    run = status.get("run")
    if restarted and (not isinstance(run, str) or not run or run == old_run):
        return [("warn", "listener is running but has not polled Telegram since it was started; "
                         "Fix: wait a minute, then run harnessgram doctor")]
    if status.get("state") == "listening" and age < 120:
        return [("ok", "listener is running and polling Telegram")]
    return [("warn", f"listener is running but reports {status.get('state')} (last update {int(age)}s ago); "
                     "Fix: check that this machine can reach api.telegram.org, then restart the service")]


def wait_listener(timeout: float, check=None, sleep=time.sleep, clock=time.monotonic,
                  restarted: bool = False, old_run: "str | None" = None) -> List[Result]:
    """check_listener, again every half second until it is ok or `timeout` seconds have passed (0: once)."""
    check = check or (lambda: check_listener(restarted, old_run))
    end = clock() + timeout
    while True:
        result = check()
        if (result and result[0][0] == "ok") or clock() >= end:
            return result
        sleep(0.5)


def check_hooks() -> List[Result]:
    path = settings_path()
    try:
        hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks") or {}
    except (OSError, ValueError):
        return [("FAIL", f"cannot read {path}; run: harnessgram install")]

    def has(event: str, sub: str) -> bool:
        return any(is_hook_command(h.get("command") or "", sub)
                   for e in hooks.get(event, []) for h in e.get("hooks", []))

    out: List[Result] = []
    for event, sub, level in (("UserPromptSubmit", "hook-prompt", "FAIL"), ("Stop", "hook-stop", "FAIL"),
                              ("Stop", "hook-wake", "warn"),
                              ("PostToolUse", "hook-post", "warn"),
                              ("PreToolUse", "hook-ask", "warn"),
                              ("Notification", "hook-notify", "warn")):
        out.append(("ok", f"{event} -> {sub}") if has(event, sub) else (level, f"{event} -> {sub} is not installed "
                                                                              "(run: harnessgram install)"))
    present = [svc for svc in SERVICES if service_files(svc) and all(f.exists() for f in service_files(svc))]
    states = service_states(present)  # loaded (launchd) / active (systemd); None where it cannot be told
    word = "loaded" if sys.platform == "darwin" else "active"
    for svc in SERVICES:
        files = service_files(svc)
        if not files:
            continue
        missing = [f for f in files if not f.exists()]
        name = "service" if svc.name == "listener" else f"{svc.name} job"
        if missing:
            out.append(("warn", f"{name} file {missing[0]} missing. Fix: harnessgram install"))
        elif states.get(svc.name) is False:
            out.append(("warn", f"{name} file {files[0]} is there, but the {name} is not {word}. "
                                "Fix: harnessgram install"))
        else:
            out.append(("ok", f"{name} file {files[0]}" + (f", {word}" if states.get(svc.name) else "")))
    return out


def check_extras(cfg: "C.Config") -> List[Result]:
    out: List[Result] = []
    if not cfg.translate:
        out.append(("ok", "translation is off (set translate=true and target_language to read replies in your language)"))
    for engine in (cfg.engines if cfg.translate else []):
        if engine == "google":
            out.append(("warn", "engine google uses Google's unofficial web endpoints and sends text to Google; "
                                "remove it from `engines` for offline-only translation"))
        elif engine == "gemini":
            out.append(("ok", "engine gemini (official Gemini API with your key; sends text to Google)"))
        elif engine == "mlx":
            have = importlib.util.find_spec("mlx_lm") is not None
            out.append(("ok", "engine mlx (mlx-lm installed)") if have else
                       ("warn", "engine mlx listed but mlx-lm is not installed (pip install 'harnessgram[mlx]')"))
        elif engine == "nllb":
            have = importlib.util.find_spec("ctranslate2") is not None and bool(cfg.nllb_model_dir)
            out.append(("ok", "engine nllb") if have else
                       ("warn", "engine nllb listed but ctranslate2/sentencepiece or nllb_model_dir is missing "
                        "(pip install 'harnessgram[nllb]' and set nllb_model_dir)"))
    voice = [shutil.which("ffmpeg"), shutil.which("whisper-cli"), cfg.whisper_model]
    out.append(("ok", "voice notes: ffmpeg, whisper-cli and model configured") if all(voice) else
               ("ok", "voice notes are off (optional; to turn on: install ffmpeg and whisper.cpp, then set whisper_model in the config)"))
    gemini = bool((cfg.gemini_vertex_project and cfg.gemini_token_command) or cfg.gemini_key_command)
    if cfg.auto_topic_names:
        out.append(("ok", "topic names: Gemini names and renames topics") if gemini else
                   ("ok", "topic names come from each session's first message (optional: set gemini_key_command, "
                          "or gemini_vertex_project and gemini_token_command, for names that follow the task)"))
    radio = getattr(cfg, "radio", None) or {}
    needed = ("telethon_python", "account_api_command", "account_session_command", "rtmp_url_command",
              "rtmp_key_command")
    missing = [k for k in needed if not radio.get(k)]
    if not radio:
        out.append(("ok", "team radio is off (optional; see docs/radio.md)"))
    elif missing:
        out.append(("warn", "team radio is configured but misses " + ", ".join(missing) + ". Fix: add them to "
                            "the config's radio object (docs/radio.md), or remove the radio object"))
    else:
        out.append(("ok", "team radio is configured" + ("" if shutil.which("ffmpeg") else ", but ffmpeg is missing")))
    return out


def _service_env_path(platform: str) -> "str | None":
    """The PATH the installed listener service runs with (macOS: from its plist), or None when unknown."""
    if platform != "darwin":
        return None
    files = service_files("listener", platform=platform)  # the platform asked about, never the host's
    try:
        with open(files[0], "rb") as fh:
            data = plistlib.load(fh)
    except (OSError, IndexError, ValueError, plistlib.InvalidFileException):
        return None
    path = (data.get("EnvironmentVariables") or {}).get("PATH")
    return path if isinstance(path, str) else None


def check_service_path(cfg: "C.Config", platform: "str | None" = None, which=shutil.which) -> List[Result]:
    """Tools the terminal finds but the background service would not (its PATH is the one in its plist):
    tmux and the configured credential commands. Silent when nothing is missing. `platform` defaults to this
    machine's at call time; every lookup uses the one given."""
    path = _service_env_path(sys.platform if platform is None else platform)
    if path is None:
        return []
    tools = ["tmux"] + [cmd[0] for cmd in (cfg.bot_token_command, cfg.gemini_key_command, cfg.gemini_token_command)
                        if cmd and isinstance(cmd[0], str) and not os.path.isabs(cmd[0])]
    missing = [t for t in dict.fromkeys(tools) if which(t) and not which(t, path=path)]
    if not missing:
        return []
    return [("warn", f"the background service cannot find {', '.join(missing)} (its PATH is shorter than this "
                     "terminal's). Fix: run harnessgram install again from this terminal")]


def check_running_code(stale=None) -> List[Result]:
    """Long-running services that started before the code now installed (each records the code it started
    with). Silent when the installed or the recorded code cannot be told."""
    if stale is None:
        from .buildstamp import stale
    out: List[Result] = []
    for name in stale():
        if name == "radio":
            out.append(("warn", "the radio still runs the previous version; turn it off and on"))
        else:
            out.append(("warn", f"{name} still runs the previous version. Fix: run harnessgram install"))
    return out


PROTECTED = ("Documents", "Desktop", "Downloads", "Library/Mobile Documents")


def check_macos_privacy(cfg: "C.Config", platform: "str | None" = None, python: str = sys.executable) -> List[Result]:
    """macOS keeps Documents, Desktop, Downloads and iCloud Drive from background processes until the
    interpreter that runs the service has Full Disk Access. Without it a session started from Telegram cannot
    open a project there (tmux starts, then the shell gets "Operation not permitted")."""
    platform = sys.platform if platform is None else platform
    if platform != "darwin":
        return []
    real = os.path.realpath(python)
    home = os.path.realpath(os.path.expanduser("~"))
    root = os.path.realpath(os.path.expanduser(cfg.projects_root)) if cfg.projects_root else home
    inside = any(root == os.path.join(home, d) or root.startswith(os.path.join(home, d) + os.sep) for d in PROTECTED)
    text = (f"give Full Disk Access to {real} (System Settings > Privacy & Security > Full Disk Access > +), "
            "then restart the service: harnessgram install")
    if inside:
        return [("warn", f"macOS privacy: your projects folder {root} is protected. Fix: " + text)]
    return [("ok", "macOS privacy: to start sessions from Telegram in Documents, Desktop, Downloads or iCloud "
                   "Drive, " + text)]


def check_group(cfg: "C.Config", bot=None) -> List[Result]:
    """Group mode: the home chat is a forum supergroup where the bot manages topics."""
    if C.home_chat(cfg) == cfg.owner_chat_id:
        return []
    try:
        bot = bot or Bot.from_config(cfg, strict=True)
        chat = bot.call("getChat", {"chat_id": cfg.chat_id}, timeout=10)
        out: List[Result] = [("ok", "group mode: the home chat is a supergroup with topics") if chat.get("is_forum")
                             else ("FAIL", "group mode: turn on Topics in the group's settings")]
        me = bot.call("getMe", timeout=10)
        mem = bot.call("getChatMember", {"chat_id": cfg.chat_id, "user_id": me["id"]}, timeout=10)
        ok = mem.get("status") == "creator" or (mem.get("status") == "administrator" and mem.get("can_manage_topics"))
        out.append(("ok", "group mode: the bot is an admin that can manage topics") if ok else
                   ("FAIL", "group mode: make the bot an admin of the group with 'Manage topics'"))
        return out
    except (TelegramError, C.ConfigError) as e:
        return [("FAIL", f"group mode check failed: {getattr(e, 'description', e)}")]


def check_local_api(cfg: "C.Config") -> List[Result]:
    """Large files (2000 MB) through a self-hosted `telegram-bot-api --local`; silent while it is off."""
    if not cfg.api_local:
        if cfg.api_base != DEFAULT_API:
            return [("warn", f"api_base is {cfg.api_base} but api_local is off: limits stay 20 MB down / 50 MB up. "
                             "Fix: set api_local=true if that server runs with --local")]
        return []
    out: List[Result] = [("ok", f"local Bot API server {cfg.api_base}: files up to 2000 MB both ways")]
    root = cfg.api_files_dir
    if not root:
        out.append(("FAIL", "api_files_dir is not set, so downloads are refused. Fix: set it to the server's working "
                            "directory (mounted at the same absolute path on this machine). See README: Large files"))
    elif not os.path.isdir(root):
        out.append(("FAIL", f"api_files_dir {root} is not a folder on this machine. Fix: mount the server's working "
                            "directory at that same path"))
    else:
        out.append(("ok", f"downloads are read only from {root}"))
    return out


def collect(offline: bool = False, bot=None, listener=None) -> list:
    """Every check as (level, message), without printing; `run` prints them, the setup page shows them.
    `listener`: a check_listener result already taken (setup waits for a just-started listener), used as is so
    the summary and the exit code rest on the same check."""
    results, cfg = check_config()
    has_token = bool(cfg and (cfg.bot_token or cfg.bot_token_command))
    if has_token and not offline:
        results += check_bot(cfg, bot)
    if has_token and not offline:
        results += check_group(cfg, bot) + check_team(cfg, bot) + check_projects(cfg, bot)
    results += check_tmux() + check_claude()
    results += (check_listener() if listener is None else list(listener)) + check_hooks() + check_running_code()
    if cfg is not None:
        results += check_local_api(cfg) + check_extras(cfg) + check_service_path(cfg) + check_macos_privacy(cfg)
    return results


def show(results, say=print) -> int:
    """Print the results one per line; 1 when anything FAILed."""
    for level, msg in results:
        say(f"[{level:>4}] {msg}")
    return 1 if any(level == "FAIL" for level, _ in results) else 0


def run(offline: bool = False, bot=None) -> int:
    return show(collect(offline, bot))
