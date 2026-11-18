"""Assistant sessions: the owner's personal-assistant sessions may prepare outward actions, never perform them.

A session is an assistant session when its id is listed in the config (`"assistant": {"sessions": ["<id>"]}`;
a full id, or its first 8 or more characters), or when it was forked from one (`mark`: one file per id in the
private state folder DERIVED). `"tools"` in the same section may add exact MCP tool names to the allow-list. Every
launch of such a session (start, resume, reload, fork, account move: all go through accounts.shell_command) runs
`ctb assistant-exec` (launch_command) instead of claude. At the moment of launch, in the session's own folder, it
checks everything (preflight), then becomes claude (exec_main) with launch_env() (an allow-list: paths, terminal
and locale settings, the bridge's folder variables, Claude's own sign-in, CLAUDE_CODE_TMPDIR=tmp_root(); no token
or key of the owner's shell) and launch_flags(). The boundary is the operating system's,
through Claude Code's own sandbox (verified on this macOS with probes; docs/panel.md):

- `--setting-sources ""`: no user, project or local settings file is read, so no allow rule, permission mode,
  plugin or hook of the owner's own applies; `--settings` carries everything (settings_dict): the bridge's own
  hooks, the guard hook, the permission rules and the sandbox;
- `--permission-mode dontAsk` (and `defaultMode`, `disableBypassPermissionsMode`): a tool runs only when an allow
  rule names it: the built-ins of assistant_guard.TOOLS, the exact MCP reads and draft steps, the owner's extra
  tools; everything else is refused with no prompt. `--tools` leaves only those built-ins (no Write, Edit, WebFetch,
  Agent, Skill ...). Deny rules name the known sending tools (DENIED) and every secret path (Read);
- the sandbox: every Bash command runs inside it (`allowUnsandboxedCommands: false`: no way out; an exclusion
  never applies without a prompt), with no network at all (`allowedDomains: []`, `strictAllowlist`, no unix
  sockets, no local binding, no Apple Events), writes only in its private temporary folder (tmp_root(): the
  working directory and Claude's shared /tmp/claude-<uid> are denied, no_write), and no reading of secret_paths(): the
  bridge's config and state, Claude's folders and accounts, the Keychain, ~/.ssh and similar. `failIfUnavailable`:
  Claude exits when the sandbox cannot start;
- `--no-chrome` and `--disallowedTools` (DENIED) as before; `--mcp-config` with only the owner's MCP servers the
  allowed tool names belong to (the WhatsApp hub), from a private file, since user MCP servers are not loaded.

The launch is refused (it prints why and exits 1, nothing starts) when any part cannot be set up: no interpreter or guard
file, no sandbox on this system, a temporary folder that is not private, managed settings that could loosen it,
or Claude Code itself not reporting the sandbox on and strict with exactly these settings (`claude sandbox status`:
a settings value Claude does not accept would otherwise be dropped silently).

Fail closed: a config file that exists but cannot be read, an invalid `assistant` section, or a DERIVED record
that cannot be checked makes the launch an assistant launch. No config file at all (the bridge is not set up) adds
nothing.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional

from .assistant_guard import BUILTIN, MCP_TOOLS, TOOLS

DENIED = (
    "mcp__claude_ai_Gmail__send_message",
    "mcp__claude_ai_Gmail__reply",
    "mcp__claude_ai_Gmail__forward",
    "mcp__whatsapp__wa_execute",
    "mcp__claude_ai_Google_Calendar__create_event",
    "mcp__claude_ai_Google_Calendar__update_event",
    "mcp__claude_ai_Google_Calendar__delete_event",
    "mcp__claude_ai_Google_Calendar__respond_to_event",
    "mcp__claude_ai_Google_Drive__share_file",
    "mcp__claude-in-chrome",
    "WebFetch",
    "WebSearch",
)
SESSION_RE = re.compile(r"[0-9a-fA-F][0-9a-fA-F-]{7,35}")
DERIVED = "assistant-sessions"  # a private folder: one empty file per forked session id (no shared list to race on)
MCP_FILE = "assistant-mcp-{account}.json"
GUARD = Path(__file__).with_name("assistant_guard.py")
MANAGED = (Path("/Library/Application Support/ClaudeCode"), Path("/etc/claude-code"))
CLEARED_ENV = ("CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE", "CLAUDE_TMPDIR")  # never passed (not on ENV_KEEP)
# claude's environment is built from this list only: a token or key the owner's shell exports (GH_TOKEN, a bot
# token, cloud keys ...) would otherwise reach every sandboxed command and, printed, the model. Paths and terminal
# settings only, plus the bridge's own folder variables (not CTB_BOT_TOKEN) and TMUX_PANE for its hooks.
ENV_KEEP = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "COLORTERM", "LANG",
    "LANGUAGE", "TZ", "TMUX", "TMUX_PANE", "NO_COLOR", "FORCE_COLOR", "EDITOR", "VISUAL", "PAGER",
    "__CF_USER_TEXT_ENCODING", "CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL", "CTB_CONFIG_DIR", "CTB_STATE_DIR",
    "CTB_PROFILES_DIR", "CTB_CLAUDE_HOME", "CTB_CLAUDE_JSON", "CTB_ASSISTANT_TMPDIR",
})
ENV_KEEP_PREFIXES = ("LC_",)
# Claude Code's own sign-in, when it comes from the environment: kept for claude itself and unset for every
# sandboxed command (sandbox.credentials.envVars, mode deny).
AUTH_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
            "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_AUTH_TOKEN", "ANTHROPIC_AWS_API_KEY",
            "AWS_BEARER_TOKEN_BEDROCK")


def tmp_root() -> Path:
    """The assistant sessions' private CLAUDE_CODE_TMPDIR: the only folder their commands may write in (as
    <root>/claude-<uid>). Short on purpose: Claude Code keeps child processes on it only while
    <root>/claude-<uid> stays short enough for a unix socket path (about 30 bytes); a longer one falls back to the
    shared /tmp/claude-<uid>, which the sandbox denies."""
    return Path(os.environ.get("CTB_ASSISTANT_TMPDIR") or f"/tmp/hga-{os.getuid()}")


def secret_paths(folder: Optional[Path] = None) -> List[str]:
    """What an assistant session may not read, in the sandbox nor with Read, Glob or Grep."""
    from . import accounts
    from .config import config_dir, state_dir
    home = Path.home()
    paths = [config_dir(), state_dir(), accounts.claude_home(), accounts.profiles_dir(), accounts.claude_json(),
             home / "Library" / "Keychains", home / "Library" / "Application Support", home / "Library" / "Cookies",
             home / "Library" / "Messages", home / "Library" / "Mail", home / ".ssh", home / ".gnupg", home / ".aws",
             home / ".netrc", home / ".config", home / ".local" / "state", GUARD.parent,
             # Codex's sign-in, settings, transcripts and state; not its worktrees, where a session may work
             *(home / ".codex" / name for name in ("auth.json", "config.toml", "history.jsonl", "sessions", "state",
                                                   "log", "memories"))]
    if folder is not None:
        paths.append(folder)
    return sorted({os.path.abspath(str(p)) for p in paths})


def hook_command(python: Optional[str] = None, guard: Optional[Path] = None, extra: Iterable[str] = (),
                 ctb: Optional[List[str]] = None) -> str:
    """The hook line: the guard by absolute paths, isolated (-I), told how to start ctb (for card commands) and
    given the owner's extra exact tool names. Only its exit 0 allows; anything else (a refusal, a crash, a missing
    interpreter or file) ends in exit 2."""
    from . import launch
    python = python or sys.executable
    guard = guard or GUARD
    ctb = launch.argv() if ctb is None else ctb
    args = [f"--ctb={json.dumps(ctb, separators=(',', ':'))}", *extra]
    return f"{shlex.quote(str(python))} -I {shlex.quote(str(guard))} {' '.join(shlex.quote(a) for a in args)}" \
           " && exit 0; exit 2"


def _wake_on(folder: Optional[Path]) -> bool:
    """Whether the owner's install has the wake hook (install.desired_hooks); yes when that cannot be told."""
    from . import accounts, install
    path = (folder or accounts.claude_home()) / "settings.json"
    try:
        hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks") or {}
        return any(install.is_hook_command(h.get("command", ""), "hook-wake")
                   for entry in hooks.get("Stop", []) for h in entry.get("hooks", []))
    except (OSError, ValueError, AttributeError, TypeError):
        return True


def no_write(cwd: Optional[str]) -> List[str]:
    """Folders the sandbox's own defaults would let a command write that it may not: the session's working folder
    (by its absolute and real paths; a "." in the settings is not the working folder) and Claude Code's shared
    /tmp/claude-<uid>, where other sessions keep files they run."""
    uid = os.getuid()
    out = [f"/tmp/claude-{uid}", f"/private/tmp/claude-{uid}"]
    if cwd:
        out += [os.path.abspath(cwd), os.path.realpath(cwd)]
    return list(dict.fromkeys(out))


def settings_dict(python: Optional[str] = None, guard: Optional[Path] = None, extra: Iterable[str] = (),
                  folder: Optional[Path] = None, bridge_hooks: bool = True, ctb: Optional[List[str]] = None,
                  cwd: Optional[str] = None) -> dict:
    from . import install
    extra = list(extra)
    secrets = secret_paths(folder)
    hooks = install.desired_hooks(_wake_on(folder)) if bridge_hooks else {}
    hooks.pop("PermissionRequest", None)  # never: a hook allow would override this session's dontAsk refusals
    guard_entry = {"matcher": "*", "hooks": [{"type": "command", "command": hook_command(python, guard, extra, ctb)}]}
    hooks["PreToolUse"] = [guard_entry, *hooks.get("PreToolUse", [])]
    reads = [rule for p in secrets for rule in (f"Read(/{p})", f"Read(/{p}/**)")]
    return {
        "disableAllHooks": False,
        "permissions": {"defaultMode": "dontAsk", "disableBypassPermissionsMode": "disable",
                        "disableAutoMode": "disable",  # auto mode would let a classifier approve unlisted tools
                        # no bare "Bash": a command runs only as one the sandbox auto-allows (sandboxed)
                        "allow": sorted(BUILTIN) + sorted(MCP_TOOLS) + [t for t in extra if t not in MCP_TOOLS],
                        "deny": list(DENIED) + reads},
        "sandbox": {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
                    "autoAllowBashIfSandboxed": True, "excludedCommands": [], "allowAppleEvents": False,
                    "network": {"allowedDomains": [], "strictAllowlist": True, "allowUnixSockets": [],
                                "allowAllUnixSockets": False, "allowLocalBinding": False},
                    "filesystem": {"allowWrite": [], "denyRead": secrets, "denyWrite": no_write(cwd)},
                    "credentials": {"envVars": [{"name": name, "mode": "deny"} for name in AUTH_ENV]}},
        "hooks": hooks,
    }


def settings_json(python: Optional[str] = None, guard: Optional[Path] = None, extra: Iterable[str] = (),
                  folder: Optional[Path] = None, bridge_hooks: bool = True, ctb: Optional[List[str]] = None,
                  cwd: Optional[str] = None) -> str:
    return json.dumps(settings_dict(python, guard, extra, folder, bridge_hooks, ctb, cwd), separators=(",", ":"))


def mcp_servers(extra: Iterable[str] = (), folder: Optional[Path] = None) -> dict:
    """The owner's own MCP servers (user scope) that an allowed tool name belongs to: --setting-sources "" does not
    load user MCP servers, so these are passed again by --mcp-config. Claude.ai connectors load on their own."""
    from . import accounts
    wanted = {name.split("__")[1] for name in [*MCP_TOOLS, *extra]}
    path = (folder / ".claude.json") if folder is not None else accounts.claude_json()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    servers = data.get("mcpServers") or {} if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise ValueError("mcpServers is not an object")
    return {name: servers[name] for name in sorted(wanted) if isinstance(servers.get(name), dict)}


def _mcp_file(extra: Iterable[str], folder: Optional[Path], account: str) -> Optional[Path]:
    """The private --mcp-config file (it may hold a server's environment, so never on the command line)."""
    from .config import private_dir, state_dir, write_private
    servers = mcp_servers(extra, folder)
    if not servers:
        return None
    path = private_dir(state_dir()) / MCP_FILE.format(account=account)
    write_private(path, json.dumps({"mcpServers": servers}, indent=2))
    return path


def launch_flags(python: Optional[str] = None, guard: Optional[Path] = None, extra: Iterable[str] = (),
                 folder: Optional[Path] = None, mcp_file: Optional[Path] = None,
                 cwd: Optional[str] = None) -> List[str]:
    """The claude arguments an assistant launch gets (as an argv list). `--mcp-config` takes several values, so it
    is never last: the launch's own arguments follow it."""
    extra = list(extra)
    flags = ["--no-chrome", "--setting-sources", "", "--permission-mode", "dontAsk"]
    if mcp_file is not None:
        flags += ["--mcp-config", str(mcp_file)]
    return flags + ["--tools", ",".join(TOOLS), "--disallowedTools", ",".join(DENIED),
                    "--settings", settings_json(python, guard, extra, folder, cwd=cwd)]


def launch_env(environ: Optional[dict] = None) -> dict:
    """claude's environment, from an allow-list (ENV_KEEP, LC_*, Claude's own sign-in AUTH_ENV) with the private
    temporary folder. Nothing else of the owner's environment: no token or key, no CLAUDE_CODE_SAFE_MODE or
    CLAUDE_CODE_SIMPLE, and no CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0. (The scrub is not switched on either: Claude Code
    2.1.322 then forces the permission mode to "default", which would replace dontAsk with prompts; the
    allow-list does its job here.)"""
    source = os.environ if environ is None else environ
    env = {k: v for k, v in source.items()
           if (k in ENV_KEEP or k.startswith(ENV_KEEP_PREFIXES) or k in AUTH_ENV) and k not in CLEARED_ENV}
    env["CLAUDE_CODE_TMPDIR"] = env["TMPDIR"] = str(tmp_root())
    return env


def launch_command() -> str:
    """What accounts.shell_command runs for an assistant session: ctb's own launcher (`ctb assistant-exec`), which
    sets everything up in the session's own folder at the moment of launch and then becomes claude (exec_main)."""
    from . import launch
    return " ".join(shlex.quote(w) for w in [*launch.argv(), "assistant-exec"])


def exec_argv(rest: Iterable[str] = (), cwd: Optional[str] = None, folder: Optional[Path] = None,
              claude: Optional[str] = None) -> List[str]:
    """The full claude argv of an assistant launch in `cwd`; OSError or ValueError when it cannot be set up."""
    claude = claude or shutil.which("claude")
    if not claude:
        raise OSError("Claude Code is not on the PATH")
    extra = extra_tools()
    account = "main" if folder is None else re.sub(r"[^A-Za-z0-9_.-]", "_", folder.name) or "main"
    mcp_file = _mcp_file(extra, folder, account)
    return [claude, *launch_flags(extra=extra, folder=folder, mcp_file=mcp_file, cwd=cwd), *rest]


def exec_main(rest: List[str]) -> int:
    """`ctb assistant-exec [claude arguments]`: check and set up the boundary here, in the session's own folder,
    then become claude. Prints why and returns 1 (nothing starts) when any part fails."""
    def refuse(why: str) -> int:
        sys.stderr.write(f"HarnessGram: assistant session not started: {why}\n")
        return 1
    try:
        cwd = os.getcwd()
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        folder = Path(configured) if configured else None
        why = preflight(folder=folder, cwd=cwd, full=True)
        if why:
            return refuse(why)
        argv = exec_argv(rest, cwd=cwd, folder=folder)
        env = launch_env()
    except (OSError, ValueError) as exc:
        return refuse(f"its launch could not be prepared ({type(exc).__name__})")
    try:
        os.execve(argv[0], argv, env)
    except OSError as exc:
        return refuse(f"claude could not be started ({type(exc).__name__})")
    return 1  # not reached


def extra_tools(raw: Optional[dict] = None) -> List[str]:
    """The owner's added exact MCP tool names (`assistant.tools`); none when the config cannot be read."""
    if raw is None:
        raw = _read_config()
    section = raw.get("assistant") if isinstance(raw, dict) else None
    if not valid_section(section):
        return []
    return list(section.get("tools", []))


def _managed_loosens(data: dict) -> Optional[str]:
    if data.get("disableAllHooks") is True or data.get("allowManagedHooksOnly") is True:
        return "managed settings switch hooks off"
    if "sandbox" in data:
        return "managed settings change the sandbox"
    perms = data.get("permissions")
    if perms is not None and (not isinstance(perms, dict) or perms.get("allow") or perms.get("ask")
                              or "defaultMode" in perms or perms.get("additionalDirectories")):
        return "managed settings allow tools"
    if data.get("allowManagedPermissionRulesOnly") is True:
        return "managed settings drop the assistant's permission rules"
    return None


def _managed_blocks_hooks() -> Optional[str]:
    for folder in MANAGED:
        files = [folder / "managed-settings.json"]
        drop_in = folder / "managed-settings.d"
        if drop_in.is_dir():
            files += sorted(drop_in.glob("*.json"))
        for f in files:
            if not f.exists():
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                return "managed settings cannot be read"
            if not isinstance(data, dict):
                return "managed settings cannot be read"
            why = _managed_loosens(data)
            if why:
                return why
    return None


def _sandbox_missing() -> Optional[str]:
    if sys.platform == "darwin":
        return None if os.access("/usr/bin/sandbox-exec", os.X_OK) else "no macOS sandbox (sandbox-exec)"
    if sys.platform.startswith("linux"):
        return None if shutil.which("bwrap") and shutil.which("socat") else "no Linux sandbox (bubblewrap, socat)"
    return "no sandbox on this system"


def prepare_tmp(root: Optional[Path] = None) -> Optional[str]:
    """Make the private temporary folder (0700, ours, not a link); None when it is ready, else why not."""
    root = root or tmp_root()
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        pass
    except OSError:
        return "its private temporary folder cannot be made"
    try:
        st = os.lstat(root)
    except OSError:
        return "its private temporary folder cannot be checked"
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        return "its temporary folder is not private to this user"
    return None


def posture(folder: Optional[Path] = None, claude: Optional[str] = None, cwd: Optional[str] = None) -> Optional[str]:
    """Ask Claude Code itself whether the sandbox is on and strict with exactly these settings: None when it is."""
    claude = claude or shutil.which("claude")
    if not claude:
        return "Claude Code is not on the PATH"
    env = launch_env()
    if folder is not None:
        env["CLAUDE_CONFIG_DIR"] = str(folder)
    try:
        out = subprocess.run([claude, "--setting-sources", "", "--settings", settings_json(folder=folder, cwd=cwd),
                              "sandbox", "status"], env=env, cwd=str(tmp_root()), stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=30)
        line = [x for x in out.stdout.splitlines() if x.strip().startswith("{")][-1]
        state = json.loads(line)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return "Claude Code did not report its sandbox"
    if not isinstance(state, dict) or state.get("supported") is not True or state.get("enabled") is not True \
            or state.get("strictMode") is not True or state.get("unavailableReason") is not None:
        return "Claude Code's sandbox is not on and strict with these settings"
    return None


def preflight(python: Optional[str] = None, guard: Optional[Path] = None, folder: Optional[Path] = None,
              cwd: Optional[str] = None, full: bool = False) -> Optional[str]:
    """None when the boundary can be set up, else why an assistant launch must not start. `full` (at the launch
    itself, exec_main) also asks Claude Code whether the sandbox is on with exactly these settings."""
    python = python or sys.executable
    guard = guard or GUARD
    if not python or not os.path.isabs(python) or not os.access(python, os.X_OK):
        return "no interpreter for the guard"
    if not Path(guard).is_file():
        return "the guard file is missing"
    why = _sandbox_missing() or _managed_blocks_hooks() or prepare_tmp()
    return why or (posture(folder, cwd=cwd) if full else None)


def refusal(why: str) -> str:
    """A launch line that starts nothing: it says why and exits 1 (arguments appended after it are inert)."""
    return f"echo {shlex.quote('HarnessGram: assistant session not started: ' + why)} >&2; exit 1; :"


def valid_section(value) -> bool:
    """{"sessions": [ids], "tools": [exact MCP names]}; a tool with a write or network word is refused."""
    from .assistant_guard import extra_ok
    if not isinstance(value, dict):
        return False
    sessions, tools = value.get("sessions", []), value.get("tools", [])
    return (isinstance(sessions, list) and all(isinstance(s, str) and SESSION_RE.fullmatch(s) for s in sessions)
            and isinstance(tools, list) and all(extra_ok(t) for t in tools))


def listed(session: str, sessions: Iterable[str]) -> bool:
    session = (session or "").lower()
    return bool(session) and any(session == s.lower() or (len(s) >= 8 and session.startswith(s.lower()))
                                 for s in sessions)


def _derived_dir() -> Path:
    from .config import state_dir
    return state_dir() / DERIVED


def derived(session: Optional[str]) -> bool:
    """True when this session was forked from an assistant session, or when that cannot be told (fail closed)."""
    if not isinstance(session, str) or not SESSION_RE.fullmatch(session):
        return False  # not a full id: nothing was ever recorded under it
    try:
        os.stat(_derived_dir() / session.lower())
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def mark(session: str) -> None:
    """Record a session (a fork's new id) as an assistant session before it is launched: one file per id, created
    atomically, so concurrent forks never overwrite each other. Raises OSError when it cannot be recorded: the
    caller must then not launch it."""
    from .config import private_dir
    if not isinstance(session, str) or not SESSION_RE.fullmatch(session):
        raise OSError("not a session id")
    folder = private_dir(_derived_dir())
    try:
        fd = os.open(folder / session.lower(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    os.close(fd)


def _read_config():
    """The config file's object; None when there is none; ... (Ellipsis) when it exists but cannot be read."""
    from .config import config_path
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError):
        return ...


def is_assistant(session: Optional[str], raw: Optional[dict] = None) -> bool:
    """True when this session must launch as an assistant session. `raw`: the config file's object (read here
    when None). Fails closed on an unreadable file or an invalid section."""
    if derived(session):
        return True
    if raw is None:
        raw = _read_config()
        if raw is None:
            return False
    if not isinstance(raw, dict):
        return True
    if "assistant" not in raw:
        return False
    section = raw["assistant"]
    if not valid_section(section):
        return True
    return listed(session or "", section.get("sessions", []))
