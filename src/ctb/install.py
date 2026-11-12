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
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from . import launch
from .config import HOME, config_dir, private_dir, state_dir

LABEL = "com.claude-telegram-bridge.listener"
UNIT = "claude-telegram-bridge.service"  # the listener unit; the jobs are in SERVICES below
# Matches both launch forms: ".../ctb hook-stop" and "python -I -m ctb hook-stop".
HOOK_RE = re.compile(r"\bctb['\"]?\s+hook-(?:stop|prompt|wake|post|notify|start|lifecycle|stop-failure|ask)\b")


def settings_path() -> Path:
    # Claude Code itself honours CLAUDE_CONFIG_DIR, so we do too.
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude") / "settings.json"


def command(sub: str) -> str:
    return " ".join(shlex.quote(a) for a in launch.argv()) + " " + sub


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
            # Only AskUserQuestion pays the startup cost: it records the question as proof for answer buttons.
            "PreToolUse": [{"matcher": "AskUserQuestion",
                            "hooks": [{"type": "command", "command": command("hook-ask"), "timeout": 5}]}],
            "PostToolUse": [{"hooks": [{"type": "command", "command": command("hook-post"), "timeout": 30}]}],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": command("hook-prompt")}]}],
            "Stop": stop,
            "StopFailure": [{"hooks": [{"type": "command", "command": command("hook-stop-failure"), "timeout": 30}]}]}


def is_ours(entry: dict) -> bool:
    return any(HOOK_RE.search(h.get("command") or "") for h in (entry.get("hooks") or []) if isinstance(h, dict))


def is_hook_command(cmd: str, sub: str) -> bool:
    m = HOOK_RE.search(cmd or "")
    return bool(m) and m.group(0).split()[-1] == sub


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
        hooks[event] = [e for e in entries if not is_ours(e)] + wanted
        verb = "updated" if ours else "added"
        for w in wanted:
            changes.append(f"{event}: {verb} `{w['hooks'][0]['command']}`")
    return new, changes


def remove_settings(settings: dict) -> Tuple[dict, List[str]]:
    new = copy.deepcopy(settings)
    changes = []
    for event, entries in list((new.get("hooks") or {}).items()):
        keep = [e for e in entries if not is_ours(e)]
        if len(keep) != len(entries):
            changes.append(f"{event}: removed {len(entries) - len(keep)} hook group(s)")
            if keep:
                new["hooks"][event] = keep
            else:
                del new["hooks"][event]
    if "hooks" in new and not new["hooks"]:
        del new["hooks"]
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
    Service("listener", LABEL, "claude-telegram-bridge", ("serve",), "keepalive", "Harnessgram listener"),
    Service("dashboard", f"{LABEL_PREFIX}.dashboard", f"{UNIT_PREFIX}-dashboard", ("dashboard",), "interval",
            "Harnessgram dashboard refresh and periodic jobs"),
    Service("heartbeat", f"{LABEL_PREFIX}.heartbeat", f"{UNIT_PREFIX}-heartbeat", ("heartbeat",), "keepalive",
            "Harnessgram heartbeat"),
    Service("daily", f"{LABEL_PREFIX}.daily", f"{UNIT_PREFIX}-daily", ("dashboard", "--daily"), "calendar",
            "Harnessgram morning summary"),
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


def unit_text(svc="listener") -> str:
    svc = _service(svc)
    env = "".join(f"Environment={n}={v}\n" for n, v in _env().items())
    exec_start = " ".join(shlex.quote(a) for a in launch.argv() + list(svc.args))
    if svc.timed:
        return (f"[Unit]\nDescription={svc.description}\n\n[Service]\nType=oneshot\n"
                f"ExecStart={exec_start}\nWorkingDirectory={state_dir()}\n{env}")
    extra = "KillMode=process\n" if svc.name == "listener" else ""
    return (f"[Unit]\nDescription={svc.description}\n\n[Service]\n"
            f"ExecStart={exec_start}\n"
            f"WorkingDirectory={state_dir()}\nRestart=always\nRestartSec=5\n{extra}{env}\n"
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


def _sh(*cmd: str) -> int:
    """The command's exit code; 127 when it cannot be started at all (for example no systemctl)."""
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60).returncode
    except (OSError, subprocess.SubprocessError):
        return 127


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


def _job_pid(label: str) -> Optional[int]:
    """The pid of a macOS launchd job that is loaded and running now, else None (also off macOS and when its
    plist is not installed: then launchctl is not even asked)."""
    if sys.platform != "darwin" or not (HOME / "Library" / "LaunchAgents" / f"{label}.plist").exists():
        return None
    code, text = _out("launchctl", "print", f"gui/{os.getuid()}/{label}")
    found = re.search(r"^\s*pid = (\d+)\s*$", text or "", re.M) if code == 0 else None
    return int(found.group(1)) if found else None


def _restart_panel(say) -> List[str]:
    """Restart the panel's launchd job only when it is loaded and running (kickstart -k stops it and starts its
    program again, the newly installed code). A stopped or absent job is never started."""
    if _job_pid(PANEL_JOB) is None:
        return []
    ok = _sh("launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{PANEL_JOB}") == 0
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
        return {s.name: _sh("launchctl", "print", f"gui/{uid}/{s.label}") == 0 for s in services}
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
                settings_file: Optional[Path] = None, schedules: bool = True, say=print) -> InstallReport:
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
        except SystemExit:
            report.hooks = False
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
                _sh("launchctl", "bootout", f"gui/{uid}/{svc.label}")  # a re-install replaces a running one
                if svc.name == "listener":
                    # bootout returns once the old listener has exited, and the new one starts only below:
                    # the last id written now is the old listener's, even one it wrote while stopping
                    report.old_run = listener_run()
                # bootstrap's own exit code, then launchd itself asked whether it now has the job
                ok = (_sh("launchctl", "bootstrap", f"gui/{uid}", str(files[0])) == 0
                      and _sh("launchctl", "print", f"gui/{uid}/{svc.label}") == 0)
                (report.running if ok else report.failed).append(svc.name)
                if ok:
                    say("  loaded by launchd (it starts at every login)")
                else:
                    say("  NOT loaded. To load it, run:")
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
            settings_file: Optional[Path] = None, schedules: bool = True) -> int:
    """The CLI: 0 only when the hooks were confirmed and every service asked to start was checked running."""
    return run_install(dry_run, service, wake, start, settings_file, schedules).code


def uninstall(dry_run: bool = False, settings_file: Optional[Path] = None) -> int:
    tag = "[dry-run] would " if dry_run else ""
    path = settings_file or settings_path()
    old = _read_settings(path)
    new, changes = remove_settings(old)
    print(f"Claude Code settings: {path}")
    for c in changes or ["no Harnessgram hooks found"]:
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
            _sh("launchctl", "bootout", f"gui/{os.getuid()}/{svc.label}")
        else:
            _sh("systemctl", "--user", "disable", "--now", f"{svc.unit}.timer" if svc.timed else f"{svc.unit}.service")
        for f in files:
            f.unlink()
        removed = True
    if removed and sys.platform.startswith("linux"):
        _sh("systemctl", "--user", "daemon-reload")
    print(f"Your config ({config_dir()}) and state ({state_dir()}) were left in place.")
    return 0
