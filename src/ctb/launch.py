"""How hooks and the service start ctb, safely.

A hook runs with Claude Code's current directory, i.e. inside whatever project is open.
`python -m ctb` puts that directory first on sys.path, so a `json.py` or a `ctb/` folder in an
untrusted project would be imported instead of the real module, with the bot token readable.
Therefore ctb is always started in one of these ways:

1. its console script by absolute path (the script directory, not the cwd, goes on sys.path);
2. `python -I -m ctb` (isolated mode ignores the cwd and PYTHON* variables), but only when ctb is
   importable under -I, which a `pip install --user` copy is not (-I also disables user site).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple


def _is_exe(path: Optional[str]) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def argv() -> List[str]:
    """The command (as an argv prefix) that starts ctb without trusting the current directory."""
    # Next to the running interpreter: pipx and venv installs. abspath, not realpath, so a venv's
    # python keeps pointing into the venv.
    beside = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "ctb")
    if _is_exe(beside):
        return [beside]
    found = shutil.which("ctb")  # pip --user installs
    if _is_exe(found):
        return [os.path.abspath(found)]
    return [sys.executable, "-I", "-m", "ctb"]


def importable_isolated() -> bool:
    """True when `python -I -c 'import ctb'` works (False for a not-installed source checkout or
    a --user install)."""
    try:
        return subprocess.run([sys.executable, "-I", "-c", "import ctb"], cwd="/", capture_output=True,
                              timeout=30).returncode == 0
    except Exception:
        return False


def warning() -> Optional[str]:
    """A message for `ctb install` when the safe launcher would not work."""
    cmd = argv()
    if len(cmd) == 1 or importable_isolated():
        return None
    return ("The ctb Python package for Harnessgram is not importable in isolated mode (`python -I`), so the hooks would fail. Install it with "
            "pipx or into a venv (pip install .), or make sure the `ctb` script is on PATH.")
