"""Cockpit apps: private tools that add a tab to the control panel without the public code knowing them.

An app is a folder `<state_dir>/cockpit-apps/<name>/` (name: `[a-z0-9-]{1,24}`) holding a `manifest.json`:

    {"title": "Sample", "title_he": "...", "icon": "box", "page": "index.html",
     "media_root": "/absolute/folder", "command": ["/absolute/executable", "--flag"]}

- `title` (and the optional `title_he`): the tab's label, 1-24 characters, no control characters.
- `icon`: one of the page's own icon names (panel_page.ICON_NAMES).
- `page`: the relative path of one `.html` file inside the app folder; the panel shows it in a sandboxed frame.
- `media_root` (optional): an absolute folder the app may serve files from (images, video, audio, JSON, text).
- `command` (optional): an argv list; `argv[0]` is an absolute executable, or a path inside the app folder. The
  server runs it for each of the app's API calls with the JSON request on stdin, never through a shell.

No other key is accepted. A folder whose manifest breaks a rule is ignored with one line in the log; the server
never fails because of an app. The routes (panel.py) are `GET /app/<name>/` (the page), `GET /app/<name>/media/<path>`
(a file under `media_root`, with HTTP Range) and `POST /app/<name>/api` (the command); every one needs the owner's
signed initData, like the panel's own routes.

The frame talks to the panel page with `postMessage` (the frame has an opaque origin, so it cannot call the server
with a header itself, and the server never answers cross-origin requests):
- `{"cockpit": "hello"}` -> `{"cockpit": "hello", "initData", "lang", "dir", "theme"}` (initData builds media URLs:
  `media/<path>?auth=<initData, URL-encoded>`);
- `{"cockpit": "api", "id": <string|number>, "body": {...}}` -> `{"cockpit": "api", "id", "status", "data"}`: the
  page posts the body to the app's API with the owner's initData and hands the answer back.
Further messages ask the page for chrome and layout (`focus`, `back`, `badge`, `open_chat`; the page answers `hello`
with the layout and sends `layout` when it changes): see docs/panel.md.
Long work (renders and the like) is the app's own business: its command starts it detached (its own session, stdout
and stderr redirected, or the call waits for them until the timeout kills the whole process group) and answers at
once; one heavy job at a time is the app's rule to keep, not this module's.

What the frame is trusted with: its URL and the hello answer carry the owner's initData (media elements need it in
the URL), and replies to an opaque origin can only be posted to "*". So an app's page is trusted code from the
owner's own folder; the sandbox keeps it away from the panel's DOM, storage and cookies, not from that login.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Union

from .config import config_dir, state_dir

NAME = re.compile(r"[a-z0-9-]{1,24}")
APPS_DIR = "cockpit-apps"
MAX_APPS = 8
MAX_MANIFEST = 16 * 1024
MAX_PAGE = 2 * 1024 * 1024
MAX_TITLE = 24
MAX_ARGV = 32
MAX_ARG = 1024
MAX_MEDIA_PATH = 1024
KEYS = {"title", "title_he", "icon", "page", "media_root", "command"}
API_BODY = 16 * 1024  # the largest request an app's API takes
API_TIMEOUT = 15  # seconds the command may run before it is killed with its process group
API_OUT = 1024 * 1024  # the largest answer it may print
MEDIA_CHUNK = 8 * 1024 * 1024  # an open-ended range ("bytes=N-") is answered with at most this much
READ_BLOCK = 256 * 1024
MEDIA_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
    ".json": "application/json", ".txt": "text/plain; charset=utf-8", ".vtt": "text/vtt; charset=utf-8",
    ".srt": "text/plain; charset=utf-8",
}  # never HTML, SVG or scripts: nothing served from media can run code
# What a command inherits from the server's environment: enough to run, nothing the bridge itself holds.
ENV_KEYS = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


class ManifestError(ValueError):
    """Why one app folder is ignored (written to the log once, without any secret)."""


@dataclass(frozen=True)
class App:
    name: str
    folder: Path  # the app folder's real path
    title: str
    title_he: str
    icon: str
    page: Path  # real path of the page, inside `folder`
    media_root: Optional[Path]  # real path, or None
    command: Tuple[str, ...]  # argv with argv[0] resolved; () when the app has no API

    def listing(self) -> dict:
        return {"name": self.name, "title": self.title, "title_he": self.title_he, "icon": self.icon}


def apps_dir() -> Path:
    return state_dir() / APPS_DIR


_warned: set = set()
_warn_lock = threading.Lock()


def _warn(name: str, reason: str) -> None:
    key = (name, reason)
    with _warn_lock:
        if key in _warned:
            return
        _warned.add(key)
    print(f"Harnessgram: cockpit app {name!r} ignored: {reason}", file=sys.stderr)


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _clean_parts(rel: str) -> Optional[list]:
    """The segments of a relative path when every one is a plain name (no "", ".", "..", dotfile, backslash, NUL)."""
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        return None
    parts = rel.split("/")
    if any(not p or p.startswith(".") for p in parts):
        return None
    return parts


def _title(value, field: str, required: bool) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TITLE or CONTROL.search(value):
        raise ManifestError(f"{field} must be 1-{MAX_TITLE} characters of plain text")
    return " ".join(value.split())


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _protected() -> list:
    """The bridge's own config (its token) and state: a media root may not hold them or lie inside them (an app's
    own folder under the state folder excepted)."""
    return [os.path.realpath(p) for p in (config_dir(), state_dir())]


def load_app(folder: Path, icons) -> App:
    """Read and check one app folder; raises ManifestError with the first rule it breaks."""
    name = folder.name
    if not NAME.fullmatch(name):
        raise ManifestError("the folder name must match [a-z0-9-]{1,24}")
    real = os.path.realpath(folder)
    if not os.path.isdir(real):
        raise ManifestError("not a folder")
    manifest = os.path.join(real, "manifest.json")
    try:
        with open(manifest, "rb") as f:
            raw = f.read(MAX_MANIFEST + 1)
    except OSError:
        raise ManifestError("no readable manifest.json") from None
    if len(raw) > MAX_MANIFEST:
        raise ManifestError("manifest.json is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ManifestError("manifest.json is not valid JSON") from None
    if not isinstance(data, dict):
        raise ManifestError("manifest.json must hold one object")
    unknown = set(data) - KEYS
    if unknown:
        raise ManifestError("unknown keys: " + ", ".join(sorted(repr(k)[:40] for k in unknown)))
    title = _title(data.get("title"), "title", True)
    title_he = _title(data.get("title_he"), "title_he", False)
    icon = data.get("icon")
    if not isinstance(icon, str) or icon not in icons:
        raise ManifestError("icon must be one of the page's icon names")

    page = data.get("page")
    parts = _clean_parts(page) if isinstance(page, str) and len(page) <= MAX_MEDIA_PATH else None
    if parts is None or not page.endswith(".html"):
        raise ManifestError("page must be a relative path of an .html file inside the app folder")
    page_real = os.path.realpath(os.path.join(real, *parts))
    if not _inside(page_real, real) or page_real == real or not os.path.isfile(page_real):
        raise ManifestError("page must be a file inside the app folder")
    if os.path.getsize(page_real) > MAX_PAGE:
        raise ManifestError("page is too large")

    media_root = None
    if "media_root" in data:
        root = data["media_root"]
        if not isinstance(root, str) or "\x00" in root or not os.path.isabs(root):
            raise ManifestError("media_root must be an absolute folder")
        root_real = os.path.realpath(root)
        if not os.path.isdir(root_real):
            raise ManifestError("media_root must be an existing folder")
        conf, state = _protected()
        if root_real == os.path.realpath(os.sep) or root_real == os.path.realpath(Path.home()) \
                or _inside(conf, root_real) or _inside(state, root_real) or _inside(root_real, conf) \
                or (_inside(root_real, state) and not _inside(root_real, os.path.realpath(apps_dir()))):
            raise ManifestError("media_root may not be the whole disk, the home folder or hold the bridge's own files")
        media_root = Path(root_real)

    command: Tuple[str, ...] = ()
    if "command" in data:
        argv = data["command"]
        if not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGV or not all(
                isinstance(a, str) and a and len(a) <= MAX_ARG and "\x00" not in a for a in argv):
            raise ManifestError(f"command must be a list of 1-{MAX_ARGV} non-empty strings")
        exe = argv[0]
        if os.path.isabs(exe):
            exe_real = exe
        else:
            exe_parts = _clean_parts(exe[2:] if exe.startswith("./") else exe)
            exe_real = os.path.realpath(os.path.join(real, *exe_parts)) if exe_parts else ""
            if not exe_real or not _inside(exe_real, real):
                raise ManifestError("command[0] must be absolute or a path inside the app folder")
        if not _executable(exe_real):
            raise ManifestError("command[0] is not an executable file")
        command = (exe_real, *argv[1:])
    return App(name, Path(real), title, title_he, icon, Path(page_real), media_root, command)


def _icons(icons):
    if icons is not None:
        return icons
    from .panel_page import ICON_NAMES
    return ICON_NAMES


def load_apps(icons=None) -> list:
    """Every valid app, by name (at most MAX_APPS); invalid ones are skipped with one log line each."""
    base = apps_dir()
    try:
        folders = sorted(p for p in base.iterdir() if not p.name.startswith("."))
    except OSError:
        return []
    icons = _icons(icons)
    out = []
    for folder in folders:
        try:
            out.append(load_app(folder, icons))
        except ManifestError as e:
            _warn(folder.name if NAME.fullmatch(folder.name) else "(invalid folder name)", str(e))
        except Exception as e:  # an app must never break the panel
            _warn(folder.name if NAME.fullmatch(folder.name) else "(invalid folder name)", type(e).__name__)
        if len(out) == MAX_APPS:
            break
    return out


def find(name: str, icons=None) -> Optional[App]:
    """The one valid app called `name`, or None."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        return None
    return next((a for a in load_apps(icons) if a.name == name), None)


def listing() -> list:
    """What /api/state carries: [{"name", "title", "title_he", "icon"}]."""
    try:
        return [a.listing() for a in load_apps()]
    except Exception:
        return []


# --- media -------------------------------------------------------------------------------------

def media_file(app: App, rel: str) -> Optional[Tuple[str, str]]:
    """(real path, content type) of the media file `rel` (already URL-decoded) under the app's media root, or
    None. Refused: no media root, a control character, a segment that is empty, ".", ".." or a dotfile, a type outside MEDIA_TYPES,
    a path whose real location (after every symlink) leaves the root or passes through a dot name, a non-file."""
    if app.media_root is None or not isinstance(rel, str) or len(rel) > MAX_MEDIA_PATH or CONTROL.search(rel):
        return None
    parts = _clean_parts(rel)
    if parts is None:
        return None
    root = str(app.media_root)
    real = os.path.realpath(os.path.join(root, *parts))
    if not _inside(real, root) or real == root:
        return None
    if _clean_parts(os.path.relpath(real, root).replace(os.sep, "/")) is None:
        return None  # a symlink inside the root that points at a dotfile or dot folder
    ctype = MEDIA_TYPES.get(os.path.splitext(real)[1].lower())
    if ctype is None or not os.path.isfile(real):
        return None
    return real, ctype


RANGE = re.compile(r"bytes=(\d{0,18})-(\d{0,18})")
UNSATISFIABLE = "unsatisfiable"


def parse_range(header: Optional[str], size: int) -> Union[None, str, Tuple[int, int]]:
    """One byte range (start, end inclusive), None to send the whole file (no Range, or one this server ignores,
    such as several ranges), or UNSATISFIABLE. An open-ended range is cut to MEDIA_CHUNK bytes; players ask on."""
    if header is None:
        return None
    m = RANGE.fullmatch(header.strip())
    if not m or (not m.group(1) and not m.group(2)):
        return None
    first, last = m.group(1), m.group(2)
    if not first:  # the last N bytes
        n = int(last)
        if n == 0 or size == 0:
            return UNSATISFIABLE
        return max(0, size - n), size - 1
    start = int(first)
    if start >= size:
        return UNSATISFIABLE
    if last:
        end = int(last)
        if end < start:
            return None
        return start, min(end, size - 1)
    return start, min(size - 1, start + MEDIA_CHUNK - 1)


# --- the API command ---------------------------------------------------------------------------

def _env(app: App) -> dict:
    env = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env["COCKPIT_APP"] = app.name
    return env


def run_command(app: App, body: bytes, timeout: Optional[float] = None) -> Tuple[int, dict]:
    """Run the app's command with `body` (a JSON object) on stdin; (HTTP status, JSON object to send back).
    The command runs from the app folder with a small environment, in its own process group (killed whole on
    timeout); its stderr is dropped, never logged; its stdout must be one JSON object of at most API_OUT bytes."""
    if not app.command:
        return 404, {"error": "not found"}
    timeout = API_TIMEOUT if timeout is None else timeout
    try:
        proc = subprocess.Popen(list(app.command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, cwd=str(app.folder), env=_env(app), shell=False,
                                close_fds=True, start_new_session=True)
    except OSError:
        return 502, {"error": "app failed"}
    try:
        out, _ = proc.communicate(body, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        return 504, {"error": "timeout"}
    if proc.returncode != 0:
        return 502, {"error": "app failed"}
    if len(out) > API_OUT:
        return 502, {"error": "too large"}
    try:
        data = json.loads(out.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return 502, {"error": "app failed"}
    if not isinstance(data, dict):
        return 502, {"error": "app failed"}
    return 200, data
