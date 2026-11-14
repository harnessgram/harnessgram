"""Opt-in, narrowly scoped health check for the Cockpit's Tailscale Funnel."""
import json
import shutil
import subprocess


def status(port):
    """Return (public, reason); None means the status could not be determined."""
    if not shutil.which("tailscale"):
        return None, "tailscale unavailable"
    try:
        result = subprocess.run(["tailscale", "serve", "status", "--json"], capture_output=True, text=True, timeout=20)
        if result.returncode:
            return None, "status command failed"
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or not isinstance(data.get("AllowFunnel", {}), dict):
            return None, "invalid status"
        allowed = data.get("AllowFunnel", {})
        return any(isinstance(key, str) and key.rpartition(":")[0] and
                   key.rpartition(":")[2] == str(port) and value is True
                   for key, value in allowed.items()), ""
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, "status unavailable"


def restore(port, panel_port):
    try:
        result = subprocess.run(["tailscale", "funnel", "--bg", f"--https={port}",
                                 f"http://127.0.0.1:{panel_port}"],
                                capture_output=True, text=True, timeout=20)
        if result.returncode:
            return False, "command failed"
        public, reason = status(port)
        return (True, "") if public else (False, reason or "still off")
    except (OSError, subprocess.SubprocessError):
        return False, "command unavailable"
