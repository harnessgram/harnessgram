"""Registers the Studio as a Cockpit app: writes `<state dir>/cockpit-apps/studio/` (manifest, page, command wrapper).

Run `harnessgram studio install` again after an update: the page is a copy written here (the panel serves a file
inside the app folder only). Nothing else is touched, and nothing is started. The command reads the desk and the
projects, and keeps one thing of its own: the owner's draft marks, in `state/` inside the app folder (api.py, marks.py).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ..config import config_path
from ..cockpit_apps import apps_dir
from . import adapter, page, safefs, strings

NAME = "studio"
WRAPPER = '''"""Runs the Studio's API (written by `harnessgram studio install`). The panel runs it from the app folder;
the folder is handed on so the draft marks stay in its own `state/` folder."""
import os
import sys

sys.path.insert(0, {root!r})
from ctb.studio import adapter
adapter.CONFIG_PATH = {config!r}
adapter.MEDIA_ROOT_OVERRIDE = {media_root!r}
adapter.limit_memory()
from ctb.studio.api import main

sys.exit(main(folder=os.path.dirname(os.path.abspath(__file__))))
'''


def manifest() -> dict:
    out = {"title": strings.COMMON["en"]["title"].replace("The ", ""), "title_he": strings.STUDIO_MANIFEST_TITLE_HE, "icon": "film",
           "page": "index.html", "command": [sys.executable, "api.py"]}
    try:
        cfg = adapter.settings()
        if cfg.studio_adapter:
            root = adapter.media_root(cfg, adapter.module(cfg))
            if root is not None:
                out["media_root"] = str(root)
    except adapter.AdapterError:
        pass  # the API will explain the fallback sample, without breaking installation
    return out


InstallError = safefs.Refused  # the install refused to write: a link, or not a plain folder or file, is in the way


def install(base: Path = None) -> Path:
    """Write the app folder (replacing its three files) and return it. Nothing is written through a link: the apps
    folder and the app folder are opened component by component without following links (a link there is refused),
    and each file goes through a unique temp file created exclusively."""
    root = Path(os.path.abspath(base or apps_dir()))
    folder = root / NAME
    root.parent.mkdir(parents=True, exist_ok=True)
    parent = os.open(str(root.parent), os.O_RDONLY | os.O_DIRECTORY)  # the folder above is the user's own choice
    try:
        root_fd = safefs.open_dir(parent, root.name)
    finally:
        os.close(parent)
    try:
        folder_fd = safefs.open_dir(root_fd, NAME)
    finally:
        os.close(root_fd)
    try:
        code_root = str(Path(__file__).resolve().parent.parent.parent)  # the folder that holds the `ctb` package
        data = manifest()
        files = {"index.html": page.render_page(), "api.py": WRAPPER.format(
                    root=code_root, config=str(adapter.CONFIG_PATH or config_path()), media_root=data.get("media_root", "")),
                 "manifest.json": json.dumps(data, ensure_ascii=False, indent=1) + "\n"}
        for name, text in files.items():
            safefs.write_file(folder_fd, name, text)
    finally:
        os.close(folder_fd)
    real = os.path.realpath(folder)
    if os.path.dirname(real) != os.path.realpath(root):  # belt and braces: the folder really is inside the apps folder
        raise InstallError("the app folder is not inside the apps folder")
    return folder
