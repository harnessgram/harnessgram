"""A module that is imported when it is first used.

A hook starts a new process for every tool call, and most of what it imports is never reached on a given call. A
module bound with `lazy_module(".fmt")` in place of `from . import fmt` costs nothing until an attribute is read, and
is then the real module: the same object every other importer gets, so patching an attribute of it (as tests do)
works as it always did.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from typing import Optional


def lazy_module(name: str, package: Optional[str] = "ctb"):
    """The module `name` (relative to `package`, e.g. ".fmt"), executed when one of its attributes is first used."""
    full = importlib.util.resolve_name(name, package) if name.startswith(".") else name
    module = sys.modules.get(full)
    if module is not None:
        return module
    spec = importlib.util.find_spec(full)
    if spec is None or spec.loader is None:
        return importlib.import_module(full)
    spec.loader = importlib.util.LazyLoader(spec.loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    parent, _, child = full.rpartition(".")
    if parent and parent in sys.modules:
        setattr(sys.modules[parent], child, module)  # `import ctb.fmt; ctb.fmt.x` finds it as it would after a plain import
    return module
