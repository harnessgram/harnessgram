"""`ctb install` / `ctb uninstall`: the listener service and the Claude Code hooks.

* macOS: LaunchAgents: the listener (`ctb serve`, kept running, starts at login) plus three scheduled jobs
  next to it: `dashboard` every 60 s, `heartbeat` kept running, `dashboard --daily` at 08:00.
* Linux: the same four as systemd user units (the jobs are a .service plus a .timer). They are written,
  then daemon-reload, enable, restart and is-active; without a systemd user session (containers, WSL without
  systemd) nothing is started and the exact command is printed.
* ~/.claude/settings.json: the hooks are merged in. Our entries are recognised by
  their command (`... -m ctb hook-...`), so running install again updates them in
  place and never touches other hooks. A timestamped backup is made first.

--dry-run prints exactly what would change and writes nothing.
"""
from __future__ import annotations

import copy
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from . import launch, listener_service
from .config import HOME, config_dir, private_dir, state_dir

LABEL = "com.claude-telegram-bridge.listener"
UNIT = "claude-telegram-bridge.service"  # the listener unit; the jobs are in SERVICES below
# Matches both launch forms: ".../ctb hook-stop" and "python -I -m ctb hook-stop".
HOOK_RE = re.compile(r"\bctb['\"]?\s+hook-(?:stop|prompt|wake|post|notify|start|lifecycle|stop-failure|ask|bash|permission)\b")


def settings_path() -> Path:
    # Claude Code itself honours CLAUDE_CONFIG_DIR, so we do too.
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude") / "settings.json"


def command(sub: str) -> str:
    return " ".join(shlex.quote(a) for a in launch.argv()) + " " + sub


PERMISSION_TIMEOUT = 300  # permission_request.HOOK_TIMEOUT (a test keeps them equal)


def desired_hooks(wake: bool = True) -> dict:
    stop = [{"hooks": [{"type": "command", "command": command("hook-stop"), "timeout": 60}]}]
    if wake:
        # Background hook; exit code 2 wakes an idle session with the Telegram message.
        stop.append({"hooks": [{"type": "command", "command": command("hook-wake"),
                                "async": True, "asyncRewake": True, "timeout": 43200}]})
    return {"SessionStart": [{"hooks": [{"type": "command", "command": command("hook-start"), "timeout": 5}]}],
            **{event: [{"hooks": [{"type": "command", "command": command("hook-lifecycle"), "timeout": 5}]}]
               for event in ("PreCompact", "SessionEnd", "SubagentStart", "SubagentStop", "PostModelSwitch")},
            "Notification": [{"hooks": [{"type": "command", "command": command("hook-notify"), "timeout": 15}]}],
            # Synchronous: record expiring question hashes before the tool can show its dialog.
            # Existing installs acquire this registration by running install again (merge is idempotent).
            "PreToolUse": [{"matcher": "AskUserQuestion",
                            "hooks": [{"type": "command", "command": command("hook-ask"), "timeout": 5}]},
                           {"matcher": "Bash",
                            "hooks": [{"type": "command", "command": command("hook-bash"), "timeout": 5}]}],
            # Claude Code's permission prompt, answered from the phone (permission_request.py): the hook waits for the
            # owner's answer below its timeout and otherwise prints nothing, so the terminal prompt stays.
            "PermissionRequest": [{"matcher": ".*|AskUserQuestion|ExitPlanMode",
                                   "hooks": [{"type": "command", "command": command("hook-permission"),
                                              "timeout": PERMISSION_TIMEOUT}]}],
            "PostToolUse": [{"hooks": [{"type": "command", "command": command("hook-post"), "timeout": 30}]}],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": command("hook-prompt")}]}],
            "Stop": stop,
            "StopFailure": [{"hooks": [{"type": "command", "command": command("hook-stop-failure"), "timeout": 30}]}]}


def is_ours(entry: dict) -> bool:
    return any(HOOK_RE.search(h.get("command") or "") for h in (entry.get("hooks") or []) if isinstance(h, dict))


def is_hook_command(cmd: str, sub: str) -> bool:
    m = HOOK_RE.search(cmd or "")
    return bool(m) and m.group(0).split()[-1] == sub


def _ours_hook(h) -> bool:
    return isinstance(h, dict) and bool(HOOK_RE.search(h.get("command") or ""))


def _without_ours(entries: list) -> list:
    """The event's hook groups with only the bridge's own hooks taken out: another hook the owner put in the same
    group stays, with its group's matcher; a group left empty goes."""
    out = []
    for e in entries:
        if not isinstance(e, dict) or not is_ours(e):
            out.append(e)
            continue
        rest = [h for h in (e.get("hooks") or []) if not _ours_hook(h)]
        if rest:
            out.append(dict(e, hooks=rest))
    return out


def merge_settings(settings: dict, wake: bool = True) -> Tuple[dict, List[str]]:
    """(new settings, human-readable list of changes). Other hooks are left exactly as they are."""
    new = copy.deepcopy(settings)
    hooks = new.setdefault("hooks", {})
    changes = []
    for event, wanted in desired_hooks(wake).items():
        entries = hooks.setdefault(event, [])
        ours = [e for e in entries if is_ours(e)]
        if ours == wanted:
            changes.append(f"{event}: already installed, unchanged")
            continue
        hooks[event] = _without_ours(entries) + wanted
        verb = "updated" if ours else "added"
        for w in wanted:
            changes.append(f"{event}: {verb} `{w['hooks'][0]['command']}`")
    return new, changes


def remove_settings(settings: dict) -> Tuple[dict, List[str]]:
    new = copy.deepcopy(settings)
    changes = []
    for event, entries in list((new.get("hooks") or {}).items()):
        keep = _without_ours(entries)
        if keep != entries:
            changes.append(f"{event}: removed the bridge's hooks from {sum(1 for e in entries if isinstance(e, dict) and is_ours(e))} group(s)")
            if keep:
                new["hooks"][event] = keep
            else:
                del new["hooks"][event]
    if "hooks" in new and not new["hooks"]:
        del new["hooks"]
    if statusline_wired(new):
        del new["statusLine"]
        changes.append("Removed the HarnessGram status line")
    return new, changes


def _read_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise SystemExit(f"{path} is not valid JSON; fix it first (nothing was changed)")
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object (nothing was changed)")
    return data


def _write_settings(path: Path, settings: dict) -> Optional[Path]:
    # Write through a symlink (dotfile managers link settings.json): replacing the link itself would
    # silently turn it into a regular file and leave the real one unchanged.
    path = Path(os.path.realpath(path))
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-ctb-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".ctb-tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)
    return backup


# --- the services: the listener and three scheduled jobs -----------------------

LABEL_PREFIX = "com.claude-telegram-bridge"
UNIT_PREFIX = "claude-telegram-bridge"
DAILY_AT = (8, 0)        # local time of the morning summary
DASHBOARD_EVERY = 60     # seconds between dashboard refreshes


@dataclass(frozen=True)
class Service:
    name: str
    label: str
    unit: str            # systemd unit base name (the .service, and the .timer when there is one)
    args: Tuple[str, ...]
    kind: str            # "keepalive" | "interval" | "calendar"
    description: str

    @property
    def timed(self) -> bool:
        return self.kind != "keepalive"


SERVICES: Tuple[Service, ...] = (
    Service("listener", LABEL, "claude-telegram-bridge", ("serve",), "keepalive", "HarnessGram listener"),
    Service("dashboard", f"{LABEL_PREFIX}.dashboard", f"{UNIT_PREFIX}-dashboard", ("dashboard",), "interval",
            "HarnessGram dashboard refresh and periodic jobs"),
    Service("heartbeat", f"{LABEL_PREFIX}.heartbeat", f"{UNIT_PREFIX}-heartbeat", ("heartbeat",), "keepalive",
            "HarnessGram heartbeat"),
    Service("daily", f"{LABEL_PREFIX}.daily", f"{UNIT_PREFIX}-daily", ("dashboard", "--daily"), "calendar",
            "HarnessGram morning summary"),
)
SCHEDULES = tuple(s for s in SERVICES if s.name != "listener")


def _service(svc) -> Service:
    if isinstance(svc, Service):
        return svc
    return next(s for s in SERVICES if s.name == svc)


def _env() -> dict:
    return {n: os.environ[n] for n in ("CTB_CONFIG_DIR", "CTB_STATE_DIR") if os.environ.get(n)}


ESSENTIAL_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def _tool_dirs(env) -> List[str]:
    """Where installers put command-line tools, in a fixed order: Homebrew (Apple silicon, then Intel), the uv
    and pipx tool folders, and ~/.local/bin (also the native Claude Code installer's folder)."""
    home = str(HOME)
    xdg_data = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin",
            env.get("UV_TOOL_BIN_DIR") or "", env.get("XDG_BIN_HOME") or "",
            os.path.normpath(os.path.join(xdg_data, "..", "bin")),
            env.get("PIPX_BIN_DIR") or "", os.path.join(home, ".local", "bin")]


def service_path(extra=(), env=None) -> str:
    """The PATH a background job gets: launchd starts it with only /usr/bin:/bin:/usr/sbin:/sbin, so tools the
    terminal finds (tmux, ffmpeg, whisper-cli, a credential command such as gcloud) would be missing.
    In order: configured `extra` folders (kept as given), then the PATH of the shell running the install in its
    own order (so the same program wins as in that terminal), then the usual tool folders it lacks, then the
    essentials. Only absolute folders that exist, each once; the essentials always."""
    env = os.environ if env is None else env
    found = [os.path.expanduser(d) for d in extra if d and os.path.isabs(os.path.expanduser(d))]
    for d in (env.get("PATH") or "").split(os.pathsep) + _tool_dirs(env):
        d = os.path.expanduser(d) if d else ""
        if d and os.path.isabs(d) and os.path.isdir(d):
            found.append(os.path.normpath(d))
    return os.pathsep.join(dict.fromkeys(found + list(ESSENTIAL_PATH)))


def plist_bytes(svc="listener") -> bytes:
    svc = _service(svc)
    env = {"PATH": service_path()}  # tmux, ffmpeg, whisper-cli and credential commands, as the terminal finds them
    env.update(_env())
    data = {
        "Label": svc.label,
        "ProgramArguments": launch.argv() + list(svc.args),
        "WorkingDirectory": str(private_dir(state_dir())),
        "EnvironmentVariables": env,
        "StandardErrorPath": str(state_dir() / f"{svc.name}.err"),
    }
    if svc.kind == "keepalive":
        data.update({"RunAtLoad": True, "KeepAlive": True})
    elif svc.kind == "interval":
        data.update({"RunAtLoad": True, "StartInterval": DASHBOARD_EVERY})
    else:
        data["StartCalendarInterval"] = {"Hour": DAILY_AT[0], "Minute": DAILY_AT[1]}
    if svc.name == "listener":
        # Sessions the listener starts (new chats, resumes) must outlive a listener restart.
        data["AbandonProcessGroup"] = True
    return plistlib.dumps(data)


def _systemd_word(value: str, *, exec_arg: bool = False) -> str:
    """Encode a systemd word, not a shell word."""
    value = str(value).replace("%", "%%")
    if exec_arg:
        value = value.replace("$", "$$")
    if re.fullmatch(r"[A-Za-z0-9_./:%+@=-]+", value):
        return value
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return '"' + escaped + '"'


def _systemd_directory(path: str) -> str:
    # WorkingDirectory is a path directive, not a systemd word list.
    if (not os.path.isabs(path) or path != path.rstrip() or path.endswith("\\")
            or any(ord(c) < 32 or ord(c) == 127 for c in path)):
        raise ValueError("WorkingDirectory cannot be represented in a systemd unit")
    return path.replace("%", "%%")


def statusline_wired(settings: dict) -> bool:
    line = settings.get("statusLine")
    if not isinstance(line, dict) or line.get("type") != "command" or not isinstance(line.get("command"), str):
        return False
    try:
        args = shlex.split(line["command"])
        return (args == launch.argv() + ["statusline"] or
                (len(args) == 2 and Path(args[0]).name in ("harnessgram", "lingoline", "ctb")
                 and args[1] == "statusline"))
    except ValueError:
        return False


def statusline_check(settings: dict, path: Optional[Path] = None) -> Tuple[str, str]:
    if statusline_wired(settings):
        return "ok", "Claude Code status line is wired for quota display."
    fix = "harnessgram install --wire-statusline"
    if path is not None and path != settings_path():
        fix += " --settings " + shlex.quote(str(path))
    return "warn", "Claude Code status line is not wired for quota display. To replace it, run: " + fix


def unit_text(svc="listener") -> str:
    svc = _service(svc)
    env = "".join(f"Environment={_systemd_word(n + '=' + v)}\n" for n, v in _env().items())
    exec_start = " ".join(_systemd_word(a, exec_arg=i > 0)
                          for i, a in enumerate(launch.argv() + list(svc.args)))
    directory = _systemd_directory(str(state_dir()))
    if svc.timed:
        return (f"[Unit]\nDescription={svc.description}\n\n[Service]\nType=oneshot\n"
                f"ExecStart={exec_start}\nWorkingDirectory={directory}\n{env}")
    extra = "KillMode=process\n" if svc.name == "listener" else ""
    return (f"[Unit]\nDescription={svc.description}\n\n[Service]\n"
            f"ExecStart={exec_start}\n"
            f"WorkingDirectory={directory}\nRestart=always\nRestartSec=5\n{extra}{env}\n"
            "[Install]\nWantedBy=default.target\n")


def timer_text(svc) -> str:
    svc = _service(svc)
    if svc.kind == "interval":
        when = f"OnBootSec={DASHBOARD_EVERY}\nOnUnitActiveSec={DASHBOARD_EVERY}\n"
    else:
        when = f"OnCalendar=*-*-* {DAILY_AT[0]:02d}:{DAILY_AT[1]:02d}:00\n"
    return (f"[Unit]\nDescription={svc.description}\n\n[Timer]\n{when}\n"
            "[Install]\nWantedBy=timers.target\n")


def service_files(svc="listener", platform: Optional[str] = None) -> List[Path]:
    """Every file the service owns: the plist (macOS), or the .service and, for a scheduled job, the .timer.
    `platform` defaults to this machine's (sys.platform at call time)."""
    svc = _service(svc)
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        return [HOME / "Library" / "LaunchAgents" / f"{svc.label}.plist"]
    if platform.startswith("linux"):
        base = HOME / ".config" / "systemd" / "user"
        return [base / f"{svc.unit}.service"] + ([base / f"{svc.unit}.timer"] if svc.timed else [])
    return []


def service_file(svc="listener") -> Optional[Path]:
    files = service_files(svc)
    return files[0] if files else None


def _contents(svc: Service, path: Path) -> bytes:
    if sys.platform == "darwin":
        return plist_bytes(svc)
    return (timer_text(svc) if path.suffix == ".timer" else unit_text(svc)).encode("utf-8")


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".ctb-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


_sh_output: Optional[str] = None


def _sh(*cmd: str) -> int:
    """The command's exit code; 127 when it cannot be started at all (for example no systemctl)."""
    global _sh_output
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        _sh_output = done.stdout
        return done.returncode
    except (OSError, subprocess.SubprocessError):
        _sh_output = ""
        return 127


def _launchctl(*cmd: str) -> Tuple[int, str]:
    """Run launchctl through the installer's command seam and retain `print` output.

    Tests have historically replaced ``_sh`` with an exit-code-only fake. A successful
    print from that seam therefore keeps its old meaning (loaded/running); the real
    runner always records stdout, allowing the listener's pid check to remain strict.
    """
    global _sh_output
    _sh_output = None
    args = cmd[1:] if cmd and cmd[0] == "launchctl" else cmd
    code = _sh("launchctl", *args)
    output = _sh_output
    # Older embedders supplied their print reader separately from the command
    # seam. Honour that injected reader without ever falling through to the real
    # subprocess reader when only `_sh` has been replaced.
    reader = globals().get("_out")
    real_reader = globals().get("_REAL_OUT")
    legacy_job_read = (args and args[0] == "print"
                       and args[-1].rsplit("/", 1)[-1] in (PANEL_JOB, RADIO_JOB))
    if output is None and legacy_job_read and reader is not None and reader is not real_reader:
        _, output = reader("launchctl", *args)
    if output is None:  # an exit-code-only injected runner
        output = "    pid = 1\n" if code == 0 and args and args[0] == "print" else ""
    return code, output


def _unit_name(svc: Service) -> str:
    """The unit that is enabled and started: the .timer for a scheduled job, else the .service."""
    return f"{svc.unit}.timer" if svc.timed else f"{svc.unit}.service"


def _start_command(services) -> str:
    """The manual command: reload, enable for every login, and restart, which starts a stopped unit and
    replaces a running one with the file just written (`enable --now` would leave a running one as it was)."""
    names = " ".join(_unit_name(s) for s in services)
    return (f"systemctl --user daemon-reload && systemctl --user enable {names} && "
            f"systemctl --user restart {names}")


def systemd_user_available() -> bool:
    """A systemd user manager answers (missing in many containers, and in WSL without systemd)."""
    return _sh("systemctl", "--user", "show-environment") == 0


def _start_linux(services, say, stopped=None) -> List[str]:
    """daemon-reload, enable, stop the listener (when it is among them), call `stopped()`, restart, then
    is-active for each unit: the units checked active after a restart that succeeded. `stopped` runs once the
    old listener has really stopped and before the new one starts (install reads its last run id there). Each step's result and each unit's measured state are reported on their own; for anything
    that did not work the exact command is printed on its own line."""
    command = _start_command(services)
    if not systemd_user_available():
        say("  Not started: no systemd user session answers here (common in containers, and in WSL without "
            "systemd). The unit files are written. Where systemd runs, start them with:")
        say(f"    {command}")
        say("  Or run the listener in a terminal:")
        say("    harnessgram serve")
        return []
    names = [_unit_name(s) for s in services]
    steps = []
    listener = _unit_name(SERVICES[0])
    plan = [("daemon-reload", ("daemon-reload",)), ("enable", ("enable", *names))]
    if listener in names:  # stop it on its own first: nothing it writes after this can pass for the new one
        plan.append(("stop", ("stop", listener)))
    plan.append(("restart", ("restart", *names)))
    for step, cmd in plan:
        code = _sh("systemctl", "--user", *cmd)
        steps.append((step, code))
        say(f"  systemctl --user {step}: " + ("ok" if code == 0 else f"failed (exit {code})"))
        if code != 0:
            break  # a later step would act on the old unit files
        if step == "stop" and stopped:
            stopped()
    replaced = len(steps) == len(plan) and all(code == 0 for _, code in steps)
    active = [n for n in names if _sh("systemctl", "--user", "is-active", "--quiet", n) == 0]
    for n in names:
        if n in active and not replaced:
            say(f"  {n}: active, but not restarted, so it may still run the previous version")
        else:
            say(f"  {n}: " + ("active" if n in active else "NOT active"))
    if replaced and len(active) == len(names):
        say("  They start when you log in. To keep them running after you log out, turn on lingering:")
        say("    loginctl enable-linger")
        return names
    bad = [n for n in names if n not in active] or names
    say("  To see why:")
    say("    systemctl --user status " + " ".join(bad))
    if replaced:
        say("  Then restart them:")
        say("    systemctl --user restart " + " ".join(bad))
    else:
        say("  Then reload, enable and restart them:")
        say(f"    {command}")
    return active if replaced else []


PANEL_JOB = f"{LABEL_PREFIX}.panel"  # set up by the Cockpit docs, not by install (macOS)
RADIO_JOB = f"{LABEL_PREFIX}.radio"  # loaded by `radio on`, removed by `radio off`


def _out(*cmd: str) -> Tuple[int, str]:
    """Exit code and output; 127 when the command cannot be started."""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return done.returncode, done.stdout


_REAL_OUT = _out


def _job_pid(label: str) -> Optional[int]:
    """The pid of a macOS launchd job that is loaded and running now, else None (also off macOS and when its
    plist is not installed: then launchctl is not even asked)."""
    if sys.platform != "darwin" or not (HOME / "Library" / "LaunchAgents" / f"{label}.plist").exists():
        return None
    code, text = _launchctl("print", f"gui/{os.getuid()}/{label}")
    found = re.search(r"^\s*pid = (\d+)\s*$", text or "", re.M) if code == 0 else None
    return int(found.group(1)) if found else None


@dataclass(frozen=True)
class LaunchdStatus:
    loaded: bool
    pid: Optional[int] = None
    spawn_failed: bool = False


def launchd_status(label: str) -> LaunchdStatus:
    """Read one launchd job without changing it. ``spawn failed`` is separate from a normally idle job."""
    code, output = _out("launchctl", "print", f"gui/{os.getuid()}/{label}")
    if code:
        return LaunchdStatus(False)
    match = re.search(r"^\s*pid = (\d+)\s*$", output, re.M)
    failed = bool(re.search(r"(?:state|last exit reason)\s*=\s*spawn failed", output, re.I) or
                  re.search(r"^\s*last exit code = 78\s*$", output, re.I | re.M))
    pid = int(match.group(1)) if match else None
    return LaunchdStatus(True, pid, failed and pid is None)  # an old exit code on a running job is not a failure


def _panel_port(path: Path) -> Optional[int]:
    """Read the panel port from its plist; this deliberately does not load the bridge config."""
    try:
        with path.open("rb") as fh:
            args = plistlib.load(fh).get("ProgramArguments", [])
        pos = args.index("--port")
        port = int(args[pos + 1])
        return port if 0 < port < 65536 else None
    except (OSError, ValueError, TypeError, IndexError, plistlib.InvalidFileException):
        return None


def panel_answers(port: int, timeout: float = 20.0, opener=urllib.request.urlopen,
                  sleep=time.sleep, clock=time.monotonic) -> bool:
    """Whether loopback returned any HTTP response before the deadline (error statuses count as answers)."""
    end = clock() + timeout
    while True:
        try:
            response = opener(f"http://127.0.0.1:{port}/", timeout=min(1.0, max(0.1, end - clock())))
            response.close()
            return True
        except urllib.error.HTTPError:
            return True
        except (OSError, ValueError):
            if clock() >= end:
                return False
            sleep(min(0.25, max(0.0, end - clock())))


def verify_launch_agents(services, say=print, repair: bool = True) -> List[str]:
    """Check installed macOS jobs and repair each unhealthy job once; return names still unhealthy."""
    if sys.platform != "darwin":
        return []
    uid = os.getuid()
    jobs = [(s.name, s.label, service_files(s)[0], s.kind == "keepalive", False) for s in services]
    panel_path = HOME / "Library" / "LaunchAgents" / f"{PANEL_JOB}.plist"
    if panel_path.exists():
        jobs.append(("panel", PANEL_JOB, panel_path, True, True))
    failed = []
    for name, label, path, needs_pid, is_panel in jobs:
        port = _panel_port(path) if is_panel else None

        def healthy() -> Tuple[bool, str]:
            status = launchd_status(label)
            if status.spawn_failed:
                return False, "spawn failed"
            if not status.loaded:
                return False, "not loaded"
            if needs_pid and status.pid is None:
                return False, "no running pid"
            if is_panel and (port is None or not panel_answers(port)):
                return False, "local HTTP did not answer"
            return True, f"running (pid {status.pid})" if status.pid else "loaded"

        ok, detail = healthy()
        repaired = False
        if not ok and repair:
            _sh("launchctl", "bootout", f"gui/{uid}/{label}")
            repaired = _sh("launchctl", "bootstrap", f"gui/{uid}", str(path)) == 0
            ok, detail = healthy()
        suffix = " after one reload" if ok and repaired else ""
        say(f"LaunchAgent {name}: " + (f"ok ({detail}){suffix}" if ok else f"DOWN ({detail})"))
        if not ok:
            failed.append(name)
    return failed


def _restart_panel(say) -> List[str]:
    """Restart the panel's launchd job only when it is loaded and running (kickstart -k stops it and starts its
    program again, the newly installed code). A stopped or absent job is never started."""
    if _job_pid(PANEL_JOB) is None:
        return []
    ok = _launchctl("kickstart", "-k", f"gui/{os.getuid()}/{PANEL_JOB}")[0] == 0
    say("Panel job: " + ("restarted on the new code" if ok else "could not be restarted. Run:"))
    if not ok:
        say(f"    launchctl kickstart -k gui/{os.getuid()}/{PANEL_JOB}")
    return ["panel"] if ok else []


def listener_run() -> Optional[str]:
    """The run id in the listener's status file (each listener start writes a new one), or None."""
    try:
        run = json.loads((state_dir() / "listener.status.json").read_text(encoding="utf-8")).get("run")
    except (OSError, ValueError, AttributeError):
        return None
    return run if isinstance(run, str) and run else None


def service_states(services) -> dict:
    """{name: True/False} whether each service is loaded (launchd) or its unit active (systemd); None where it
    cannot be told (another platform, or no systemd user session). Read-only: no prompt, nothing started."""
    services = [_service(s) for s in services]
    if sys.platform == "darwin":
        uid = os.getuid()
        return {s.name: _launchctl("print", f"gui/{uid}/{s.label}")[0] == 0 for s in services}
    if sys.platform.startswith("linux") and services and systemd_user_available():
        return {s.name: _sh("systemctl", "--user", "is-active", "--quiet", _unit_name(s)) == 0 for s in services}
    return {s.name: None for s in services}


@dataclass
class InstallReport:
    """What install did and, separately, what it checked, so the closing message claims only what was checked."""
    settings: Path
    dry_run: bool = False
    hooks: bool = False                  # read back from the settings file after writing: every hook is there
    service: bool = True                 # a background service was asked for
    platform: bool = True                # this platform has one (macOS LaunchAgents, Linux systemd user units)
    start: bool = True                   # it was asked to start
    running: List[str] = field(default_factory=list)  # loaded by launchd / active in systemd, checked one by one
    failed: List[str] = field(default_factory=list)   # written but not loaded or active
    old_run: Optional[str] = None  # run id in the listener's status before the restart: that report is not ours
    restarted: List[str] = field(default_factory=list)  # long-running services now on the new code
    radio_kept: bool = False  # a radio stream runs: it keeps its code until it is next turned off and on

    @property
    def started(self) -> Optional[bool]:
        """True when every service was checked running, False when one was not, None when none was started."""
        if self.dry_run or not (self.service and self.platform and self.start):
            return None
        return not self.failed and bool(self.running)

    @property
    def code(self) -> int:
        return 0 if self.dry_run or (self.hooks and self.started is not False) else 1

    def checked(self) -> List[str]:
        out = []
        if self.hooks:
            out.append(f"Claude Code hooks are in {self.settings} (read back after writing)")
        if self.running:
            how = "loaded by launchd" if sys.platform == "darwin" else "active in systemd"
            out.append(f"background services {how}: {', '.join(self.running)}")
        if self.restarted:
            out.append(f"restarted on the new code: {', '.join(self.restarted)}")
        return out

    def unchecked(self) -> List[str]:
        out = []
        if not self.hooks:
            out.append(f"the Claude Code hooks could not be confirmed in {self.settings}")
        if not self.service:
            out.append("no background service was installed (asked not to)")
        elif not self.platform:
            out.append("this platform has no background service: run `harnessgram serve` yourself")
        elif not self.start:
            out.append("the background services were written but not started (asked not to)")
        elif self.failed:
            out.append(f"not running: {', '.join(self.failed)} (the fix is printed above)")
        if self.radio_kept:
            out.append("the radio keeps its current code until it is next turned off and on")
        return out


def run_install(dry_run: bool = False, service: bool = True, wake: bool = True, start: bool = True,
                settings_file: Optional[Path] = None, schedules: bool = True, say=print,
                wire_statusline: bool = False) -> InstallReport:
    if not dry_run:
        from .hooks import wait_idle
        try:
            wait = float(os.environ.get("CTB_INSTALL_WAIT", "30"))
        except ValueError:
            wait = 30.0
        if not wait_idle(wait):
            say("WARNING: a Stop worker was still running; continuing anyway.")
    problem = launch.warning()
    if problem:
        say(f"WARNING: {problem}")
    tag = "[dry-run] would " if dry_run else ""
    path = settings_file or settings_path()
    report = InstallReport(settings=path, dry_run=dry_run, service=service, start=start)
    old = _read_settings(path)
    new, changes = merge_settings(old, wake)
    if wire_statusline:
        new["statusLine"] = {"type": "command", "command": command("statusline")}
        if new.get("statusLine") != old.get("statusLine"):
            changes.append("wire the HarnessGram status line")
    say(f"Claude Code settings: {path}")
    for c in changes:
        say(f"  {tag}{c}")
    if new != old and not dry_run:
        backup = _write_settings(path, new)
        say(f"  settings written; backup: {backup}" if backup else "  settings file created")
    if not dry_run:
        try:
            again = _read_settings(path)
            report.hooks = merge_settings(again, wake)[0] == again
        except (SystemExit, OSError):
            report.hooks = False
            again = None
    else:
        again = old
    if again is None:
        say("Could not read settings to check the status line.")
    else:
        say(statusline_check(again, path)[1])
    if service:
        chosen = SERVICES if schedules else SERVICES[:1]
        if service_file() is None:
            report.platform = False
            say("Listener service: unsupported platform; run `harnessgram serve` yourself.")
        for svc in chosen:
            files = service_files(svc)
            if not files:
                continue
            title = svc.name.capitalize()
            say(f"{title} service: {files[0]}" + (f" (+ {files[1].name})" if len(files) > 1 else ""))
            if dry_run:
                say(f"  {tag}write it" + (" and start it" if start else ""))
                continue
            for f in files:
                _write_atomic(f, _contents(svc, f))
            say("  written")
            if start and sys.platform == "darwin":
                uid = str(os.getuid())
                if svc.name == "listener":
                    with listener_service.install_lock(blocking=True):
                        _launchctl("bootout", f"gui/{uid}/{svc.label}")
                        # The last id written after bootout belongs to the old listener.
                        report.old_run = listener_run()
                        ok = listener_service.load_and_verify(files[0], runner=_launchctl)
                else:
                    _launchctl("bootout", f"gui/{uid}/{svc.label}")
                    ok = (_launchctl("bootstrap", f"gui/{uid}", str(files[0]))[0] == 0
                          and _launchctl("print", f"gui/{uid}/{svc.label}")[0] == 0)
                (report.running if ok else report.failed).append(svc.name)
                if ok:
                    say("  loaded by launchd (it starts at every login)")
                else:
                    say("  NOT loaded or running after retries. To load it, run:")
                    say(f"    launchctl bootstrap gui/{uid} {shlex.quote(str(files[0]))}")
        if start and not dry_run and sys.platform.startswith("linux") and service_file() is not None:
            say("Starting the systemd user units")
            active = _start_linux(chosen, say, stopped=lambda: setattr(report, "old_run", listener_run()))
            report.running = [s.name for s in chosen if _unit_name(s) in active]
            report.failed = [s.name for s in chosen if _unit_name(s) not in active]
        if start and not dry_run:
            # install is the only restarter: the long-running services replaced above, then the panel's own
            # job when it is running; scheduled jobs start fresh on each run, and the radio is left alone
            report.restarted = [s.name for s in chosen if s.kind == "keepalive" and s.name in report.running]
            report.restarted += _restart_panel(say)
            if sys.platform == "darwin":
                unhealthy = verify_launch_agents(chosen, say=say)
                chosen_names = [s.name for s in chosen]
                report.failed = unhealthy
                report.running = [name for name in chosen_names if name not in unhealthy]
            report.radio_kept = _job_pid(RADIO_JOB) is not None
            if report.radio_kept:
                say("Radio: it keeps its current code until it is next turned off and on")
    if dry_run:
        say("Next: harnessgram doctor   (nothing was changed)")
        return report
    for title, lines in (("Checked:", report.checked()),
                         ("Not checked:", report.unchecked() + ["that the listener reaches Telegram"])):
        say(title)
        for line in lines:
            say(f"  - {line}")
    say("Next: harnessgram doctor")
    return report


def install(dry_run: bool = False, service: bool = True, wake: bool = True, start: bool = True,
            settings_file: Optional[Path] = None, schedules: bool = True, wire_statusline: bool = False) -> int:
    """The CLI: 0 only when the hooks were confirmed and every service asked to start was checked running."""
    return run_install(dry_run, service, wake, start, settings_file, schedules, wire_statusline=wire_statusline).code


def _uninstall_radios(dry_run: bool) -> bool:
    """Stop only owned radio jobs and streams; never start either edition."""
    if sys.platform != "darwin":
        return True
    from . import radio, radio_youtube
    team_state = radio._read(radio.folder() / "state.json")
    youtube_state = radio_youtube._state()
    team = (radio._job_loaded() is not False or radio.plist_path().exists()
            or radio.owned_call(team_state) or radio.wanted())
    youtube = (radio_youtube._job_on() or radio_youtube.plist_path().exists()
               or youtube_state.get("on") or youtube_state.get("broadcast_id") or youtube_state.get("cleanup"))
    if not (team or youtube):
        return True
    if dry_run:
        print("[dry-run] would stop the active radio editions and their owned streams")
        return True
    try:
        from .config import load
        cfg = load()
        # Each stop has its own cleanup path. Attempt both even if one fails.
        ok = True
        for active, name, stop, expected in ((youtube, "YouTube radio", radio_youtube.stop, "youtube_stopped"),
                                             (team, "team radio", radio.stop, "radio_stopped")):
            if active:
                try:
                    if stop(cfg) != expected:
                        print(f"Could not fully stop {name}; retry its stop command.")
                        ok = False
                except Exception:
                    print(f"Could not fully stop {name}; retry its stop command.")
                    ok = False
        if team and radio._job_loaded() is not False:
            ok = False
        if youtube and radio_youtube._job_on():
            ok = False
        return ok
    except Exception:
        print("Could not stop the radios; check configuration and retry uninstall.")
        return False


def uninstall(dry_run: bool = False, settings_file: Optional[Path] = None) -> int:
    tag = "[dry-run] would " if dry_run else ""
    path = settings_file or settings_path()
    old = _read_settings(path)
    try:
        radios_ok = _uninstall_radios(dry_run)
    except Exception as e:  # a missing config or revoked credentials must not keep the hooks installed
        print(f"Could not stop the radio ({e.__class__.__name__}); retry its stop command.")
        radios_ok = False
    new, changes = remove_settings(old)
    print(f"Claude Code settings: {path}")
    for c in changes or ["no HarnessGram hooks found"]:
        print(f"  {tag}{c}")
    if new != old and not dry_run:
        print(f"  settings written; backup: {_write_settings(path, new)}")
    removed = False
    for svc in SERVICES:
        files = [f for f in service_files(svc) if f.exists()]
        if not files:
            continue
        print(f"{svc.name.capitalize()} service: {tag}remove {', '.join(str(f) for f in files)}")
        if dry_run:
            continue
        if sys.platform == "darwin":
            _launchctl("bootout", f"gui/{os.getuid()}/{svc.label}")
        else:
            _sh("systemctl", "--user", "disable", "--now", f"{svc.unit}.timer" if svc.timed else f"{svc.unit}.service")
        for f in files:
            f.unlink()
        removed = True
    if removed and sys.platform.startswith("linux"):
        _sh("systemctl", "--user", "daemon-reload")
    print(f"Your config ({config_dir()}) and state ({state_dir()}) were left in place.")
    return 0 if radios_ok else 1
