"""Every non-Python file under src/ctb ships in the installed package.

A Cockpit script missing from package-data made the installed panel fail on every poll, so owner messages stopped
reaching sessions. This compares the package-data patterns with the files on disk, the same way setuptools expands them.
"""
from __future__ import annotations

import glob
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _covered(package: str, base: Path) -> set:
    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"][package]
    return {str(Path(f).relative_to(base)) for p in patterns for f in glob.glob(str(base / p)) if os.path.isfile(f)}


def _assets(base: Path, skip: tuple = ()) -> set:
    out = set()
    for f in base.rglob("*"):
        rel = f.relative_to(base)
        if f.is_file() and f.suffix not in (".py", ".pyc") and "__pycache__" not in rel.parts and rel.parts[0] not in skip:
            out.add(str(rel))
    return out


def test_every_ctb_asset_is_in_package_data():
    base = ROOT / "src/ctb"
    missing = _assets(base, skip=("studio",)) - _covered("ctb", base)
    assert not missing, f"add these to [tool.setuptools.package-data] ctb: {sorted(missing)}"


def test_every_studio_asset_is_in_package_data():
    base = ROOT / "src/ctb/studio"
    missing = _assets(base) - _covered("ctb.studio", base)
    assert not missing, f"add these to [tool.setuptools.package-data] ctb.studio: {sorted(missing)}"
