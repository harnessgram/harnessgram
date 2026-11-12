"""The shell that runs the command lines the bridge builds (new sessions, resumes, account logins).

Those lines are POSIX shell (`VAR=value cmd`, `unset`, `&&`, `( ... ) &`) started as `<shell> -lic "<line>"`,
so the shell must be a POSIX-family shell that accepts -l -i -c. The user's own login shell is preferred (its
profile sets PATH, nvm and the like); fish, tcsh, nushell and unknown shells fall back to /bin/sh rather than
failing on that syntax. A service such as launchd may start the bridge without $SHELL, so the account's login
shell from the user database is the second choice.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from typing import Iterable, List, Mapping, Optional

FALLBACK = "/bin/sh"
GUARD_END = "exit 127; }; "  # where the builder's preamble ends and the caller's own line begins
MISSING_PAUSE = 60  # seconds the "not found" line stays on screen before the terminal closes
POSIX_SHELLS = ("zsh", "bash", "sh")  # the same names inbox.SHELLS recognises as "the pane is back at a prompt"


def _usable(path: Optional[str]) -> bool:
    return (bool(path) and os.path.isabs(path) and os.path.basename(path) in POSIX_SHELLS
            and os.path.isfile(path) and os.access(path, os.X_OK))


def _account_shell() -> Optional[str]:
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_shell or None
    except (ImportError, KeyError, OSError):
        return None


def candidates(env: Optional[Mapping[str, str]] = None) -> Iterable[Optional[str]]:
    env = os.environ if env is None else env
    yield env.get("SHELL")
    yield _account_shell()


def user_shell(env: Optional[Mapping[str, str]] = None) -> str:
    """$SHELL when it is an existing absolute executable of a POSIX-family shell, else the account's login
    shell under the same rule, else /bin/sh."""
    for path in candidates(env):
        if _usable(path):
            return path
    return FALLBACK


class ClaudeNotFound(OSError):
    """The chosen shell cannot find Claude Code. Raised before any window exists; every caller already treats an
    OSError from starting a terminal as "could not open" and says so in Telegram."""


PROBE_TIMEOUT = 5.0  # seconds; a shell profile slower than this does not block a launch
NOT_FOUND = 97       # the probe's own exit code for "not found", told apart from a profile that fails


def _suffix(binary: Optional[str]) -> str:
    """Append the folder of the executable the service found (accounts.claude_binary) to PATH: it only adds a
    place to look, so the profile's own choice still wins."""
    if not (binary and os.path.isabs(binary) and os.path.isfile(binary) and os.access(binary, os.X_OK)):
        return ""
    return f'PATH="$PATH":{shlex.quote(os.path.dirname(binary))}; export PATH; '


def probe(shell: str, flag: str, prefix: str, program: str, timeout: Optional[float] = None) -> Optional[bool]:
    """Ask the same shell, with the same flags, environment and PATH suffix as the launch line, whether it finds
    `program` (on PATH, or as a function or alias the profile defines). True: found. False: the shell ran and
    did not find it. None: the shell could not be asked (timeout, or it failed for another reason)."""
    try:
        done = subprocess.run([shell, flag, f"{prefix}command -v {program} >/dev/null 2>&1 && exit 0; "
                                            f"exit {NOT_FOUND}"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=PROBE_TIMEOUT if timeout is None else timeout, start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return True if done.returncode == 0 else False if done.returncode == NOT_FOUND else None


def shell_argv(line: str, interactive: bool = True, program: str = "claude", binary: Optional[str] = None,
               shell: Optional[str] = None) -> List[str]:
    """The argv tail for `tmux new-session ...`: [shell, -lic or -lc, command]. Every session start, resume and
    account login goes through here, so the shell may differ from the one the user set up (/bin/sh does not read
    ~/.zshrc). Before any window is created, the same shell is asked whether it finds `program` (probe): when
    it answers "not found", ClaudeNotFound is raised and the caller reports the failure in Telegram; when it
    cannot answer, the launch goes ahead as before. In the window, a missing program is still said plainly
    (exit 127) as a backstop. `binary`: the executable whose folder is appended to PATH (default: the one the
    service finds; "" for none)."""
    if binary is None:
        from .accounts import claude_binary
        binary = claude_binary()
    shell = shell or user_shell()
    flag = "-lic" if interactive else "-lc"
    prefix = _suffix(binary)
    if probe(shell, flag, prefix, program) is False:
        raise ClaudeNotFound(f"{program} was not found by {shell}")
    guard = (f"command -v {program} >/dev/null 2>&1 || {{ echo \"Harnessgram: {program} was not found on PATH "
             f"in this shell ($0).\" >&2; sleep {int(MISSING_PAUSE)}; " + GUARD_END)
    return [shell, flag, prefix + guard + line]
