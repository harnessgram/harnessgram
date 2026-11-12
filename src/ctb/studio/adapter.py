"""Trusted local adapter loading and the public, bounded Studio data contract.

An adapter is Python code selected by the owner, not an untrusted plug-in sandbox. Its return values are
untrusted data: only documented fields survive. No owner folder or video-engine convention belongs here.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import math
import os
import re
import sys
from pathlib import Path

from .. import config, cockpit_apps
from . import safefs

CONFIG_PATH = None
# None = use config/adapter. An installed wrapper pins this to its manifest's root ("" means no media).
MEDIA_ROOT_OVERRIDE = None
_cached = None
MEMORY_LIMIT = 256 * 1024 * 1024
_memory_limit_failed = False
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}\Z")
MEDIA = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")
REASONS = {
    "config": {"he": "הגדרות הסטודיו אינן זמינות. מוצגת דוגמה.",
               "en": "Studio settings are unavailable. Showing a sample."},
    "load": {"he": "לא ניתן לטעון את חיבור הסטודיו. מוצגת דוגמה.",
             "en": "The Studio adapter could not be loaded. Showing a sample."},
    "data": {"he": "חיבור הסטודיו החזיר נתונים לא תקינים. מוצגת דוגמה.",
             "en": "The Studio adapter returned invalid data. Showing a sample."},
    "call": {"he": "חיבור הסטודיו נכשל בקריאת הסרטונים. מוצגת דוגמה.",
             "en": "The Studio adapter could not read the videos. Showing a sample."},
}


class AdapterError(ValueError):
    pass


def limit_memory():
    """Bound the dedicated API process before loading adapter code; never call this in the panel process.

    macOS may refuse RLIMIT_AS, so try its data limit first. Keep any stricter inherited limit and never
    raise the hard limit. If neither limit works, read() returns the labelled call-failure sample.
    """
    global _memory_limit_failed
    _memory_limit_failed = True
    try:
        import resource
    except ImportError:
        return
    names = ("RLIMIT_DATA", "RLIMIT_AS") if sys.platform == "darwin" else ("RLIMIT_AS", "RLIMIT_DATA")
    for name in names:
        limit = getattr(resource, name, None)
        if limit is None:
            continue
        try:
            soft, hard = resource.getrlimit(limit)
            ceilings = [MEMORY_LIMIT] + [n for n in (soft, hard) if n != resource.RLIM_INFINITY]
            resource.setrlimit(limit, (min(ceilings), hard))
        except (OSError, ValueError):
            continue
        _memory_limit_failed = False
        return
    # macOS refuses both limits (EINVAL): watch the process's own peak memory instead and end it past the cap.
    _start_watchdog()
    _memory_limit_failed = False


def _peak_bytes() -> int:
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # macOS reports bytes, Linux kilobytes


def _watch(limit=None, peak=_peak_bytes, stop=os._exit, every=0.05, rounds=None):
    import time
    limit = MEMORY_LIMIT if limit is None else limit
    n = 0
    while rounds is None or n < rounds:
        n += 1
        try:
            if peak() > limit:
                stop(3)  # the panel sees a failed call and shows the labelled sample
                return
        except Exception:
            pass
        time.sleep(every)


def _start_watchdog():
    import threading
    threading.Thread(target=_watch, name="studio-memory-watchdog", daemon=True).start()


@contextlib.contextmanager
def quiet():
    """Discard adapter diagnostics rather than accumulating them before the answer-size check."""
    with open(os.devnull, "w", encoding="utf-8") as sink:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            yield


def settings():
    """Read config without migrations, credentials commands or writes (unlike config.load)."""
    try:
        path = Path(CONFIG_PATH) if CONFIG_PATH else config.config_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return config.Config()
        if len(raw) > 1024 * 1024:
            raise ValueError
        return config.from_dict(json.loads(raw))
    except Exception:
        raise AdapterError("config") from None


def module(cfg):
    """Reload on file changes, without writing bytecode next to the private source."""
    global _cached
    try:
        path = Path(cfg.studio_adapter)
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
        if _cached and _cached[0] == key:
            return _cached[1]
        if st.st_size > 1024 * 1024:
            raise ValueError
        spec = importlib.util.spec_from_file_location("_ctb_private_studio", path)
        if spec is None:
            raise ValueError
        mod = importlib.util.module_from_spec(spec)
        # Adapter diagnostics must never corrupt the one-JSON-object API protocol.
        previous = sys.modules.get(spec.name)
        sys.modules[spec.name] = mod  # ordinary Python modules (e.g. dataclasses) expect this registration
        try:
            with quiet():
                exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), mod.__dict__)
            if not callable(getattr(mod, "desk", None)) or not callable(getattr(mod, "project", None)):
                raise ValueError
        except BaseException:
            if previous is None:
                sys.modules.pop(spec.name, None)
            else:
                sys.modules[spec.name] = previous
            raise
        _cached = (key, mod)
        return mod
    except MemoryError:
        raise AdapterError("call") from None
    except (Exception, SystemExit):
        raise AdapterError("load") from None


def media_root(cfg, mod):
    """The same read-only root the install puts in the Cockpit manifest. Never expose it as API data."""
    try:
        value = MEDIA_ROOT_OVERRIDE if MEDIA_ROOT_OVERRIDE is not None else (
            cfg.studio_media_root or getattr(mod, "MEDIA_ROOT", ""))
        if not value:
            return None
        root = Path(os.path.expanduser(os.fspath(value)))
        if not root.is_absolute() or root.is_symlink():
            raise ValueError
        real = Path(os.path.realpath(root))
        # Match the host's protected-root policy before allowing any media data through.
        conf, state = cockpit_apps._protected()
        if (not real.is_dir() or real in (Path('/'), Path.home().resolve())
                or cockpit_apps._inside(conf, str(real)) or cockpit_apps._inside(state, str(real))
                or cockpit_apps._inside(str(real), conf)
                or (cockpit_apps._inside(str(real), state)
                    and not cockpit_apps._inside(str(real), os.path.realpath(cockpit_apps.apps_dir())))):
            raise ValueError
        return real
    except Exception:
        raise AdapterError("data") from None


def media(value, root, kind):
    if not isinstance(value, str) or not MEDIA.fullmatch(value) or ".." in value or root is None:
        return None
    ctype = cockpit_apps.MEDIA_TYPES.get(Path(value).suffix.lower(), "")
    if not ctype.startswith(kind + "/"):
        return None
    try:
        fd = safefs.open_readonly(root, value)
        os.close(fd)
        return value
    except (OSError, ValueError):
        return None


def text(v):
    if isinstance(v, str) and v.strip() and len(v) <= 2000 and not cockpit_apps.CONTROL.search(v):
        return v
    if isinstance(v, dict):
        out = {k: v[k] for k in ("he", "en") if isinstance(v.get(k), str) and text(v[k]) is not None}
        return out or None
    return None


def number(v, maximum=36000):
    return (type(v) in (int, float) and 0 <= v <= maximum and math.isfinite(v))


def integer(v, maximum=1000000):
    return type(v) is int and 0 <= v <= maximum


def ident(v):
    return isinstance(v, str) and ID.fullmatch(v) is not None


def rows(raw, key, maximum, indexed=False):
    value = raw.get(key)
    if not isinstance(value, list):
        raise AdapterError("data")
    if indexed:
        return [(pos, x) for pos, x in enumerate(value[:maximum]) if isinstance(x, dict)]
    return [x for x in value[:maximum] if isinstance(x, dict)]


def base(item, scenes):
    if not ident(item.get("id")) or text(item.get("title")) is None or not ident(item.get("series")):
        return None
    return {"id": item["id"], "series": item["series"], "title": text(item["title"]),
            "scene": item.get("scene") if isinstance(item.get("scene"), str) and item["scene"] in scenes else "title"}


def clean_desk(raw):
    from .page import scene_ids
    if not isinstance(raw, dict) or type(raw.get("sample")) is not bool:
        raise AdapterError("data")
    scenes = set(scene_ids())
    out = {"sample": raw["sample"], "series": [], "waiting": [], "working": [], "shelf": [], "templates": []}
    known = set()
    for s in rows(raw, "series", 50):
        if ident(s.get("id")) and s["id"] not in known and text(s.get("name")) is not None:
            out["series"].append({"id": s["id"], "name": text(s["name"])})
            known.add(s["id"])
    seen = set()
    for key in ("waiting", "working", "shelf"):
        for row in rows(raw, key, 50):
            b = base(row, scenes)
            if b is None or b["series"] not in known or b["id"] in seen:
                continue
            if key == "waiting":
                if not (integer(row.get("episode")) and integer(row.get("draft"), 99)
                        and number(row.get("length_s")) and integer(row.get("suggestions"), 200)
                        and number(row.get("est_s"))):
                    continue
                b.update({k: row[k] for k in ("episode", "draft", "length_s", "suggestions", "est_s")})
            elif key == "working":
                if not (row.get("stage") in ("script", "voice", "art", "captions", "checks")
                        and number(row.get("pct"), 1) and number(row.get("eta_s"))):
                    continue
                b.update({k: row[k] for k in ("stage", "pct", "eta_s")})
            else:
                status = row.get("status")
                if status not in ("ready", "writing", "producing", "published"):
                    continue
                b["status"] = status
                if status == "published":
                    if not number(row.get("ago_s"), 100 * 365 * 86400):
                        continue
                    b["ago_s"] = row["ago_s"]
            out[key].append(b)
            seen.add(b["id"])
    seen = set()
    for row in rows(raw, "templates", 50):
        if (ident(row.get("id")) and row["id"] not in seen and text(row.get("name")) is not None
                and number(row.get("length_s")) and row.get("format") in ("wide", "tall")):
            out["templates"].append({"id": row["id"], "name": text(row["name"]), "length_s": row["length_s"],
                                     "format": row["format"], "scene": row.get("scene") if row.get("scene") in list(scenes) else "title"})
            seen.add(row["id"])
    return out


def clean_project(raw, pid, root):
    from .page import scene_ids
    if raw is None:
        return None
    if not isinstance(raw, dict) or type(raw.get("sample")) is not bool or raw.get("id") != pid:
        raise AdapterError("data")
    pictures = set(scene_ids())
    out = base(raw, pictures)
    if out is None or not (integer(raw.get("episode")) and integer(raw.get("draft"), 99)
                            and number(raw.get("length_s"))):
        raise AdapterError("data")
    out.pop("scene")
    out.update({k: raw[k] for k in ("sample", "episode", "draft", "length_s")})
    out.update(scenes=[], sentences=[])
    scene_map = {}
    previous = -1
    for pos, sc in rows(raw, "scenes", 60, indexed=True):
        if not (integer(sc.get("i")) and sc["i"] == pos + 1 and text(sc.get("title")) is not None
                and number(sc.get("start")) and number(sc.get("end"))
                and previous <= sc["start"] < sc["end"] <= out["length_s"]):
            continue
        scene_map[pos] = len(out["scenes"])
        out["scenes"].append({"i": len(out["scenes"]) + 1, "title": text(sc["title"]),
                              "start": sc["start"], "end": sc["end"],
                              "scene": sc.get("scene") if sc.get("scene") in list(pictures) else "title",
                              "frame": media(sc.get("frame"), root, "image")})
        previous = sc["start"]
    previous = -1
    for pos, s in rows(raw, "sentences", 200, indexed=True):
        si = s.get("scene_i")
        if not (integer(s.get("i")) and s["i"] == pos + 1 and integer(si) and si in scene_map
                and number(s.get("start")) and number(s.get("end"))
                and previous <= s["start"] < s["end"] <= out["length_s"] and text(s.get("said")) is not None):
            continue
        si = scene_map[si]
        sc = out["scenes"][si]
        if not sc["start"] <= s["start"] < sc["end"]:
            continue
        suggestion = s.get("suggest") if s.get("suggest") in ("tighten", "remove") else None
        why = text(s.get("why"))
        short = text(s.get("short"))
        short_s = s.get("short_s")
        if not why or (suggestion == "tighten" and not (short and number(short_s) and short_s < s["end"] - s["start"])):
            suggestion = None
        flag = s.get("flag") if s.get("flag") in ("visual", "take", "fix") else None
        out["sentences"].append({"i": s["i"], "scene_i": si, "start": s["start"], "end": s["end"],
                                 "said": text(s["said"]), "scene": s.get("scene") if s.get("scene") in list(pictures) else "title",
                                 "frame": media(s.get("frame"), root, "image"), "audio": media(s.get("audio"), root, "audio"),
                                 "suggest": suggestion, "short": short if suggestion == "tighten" else None,
                                 "short_s": short_s if suggestion == "tighten" else None, "why": why if suggestion else None,
                                 "flag": flag, "note": text(s.get("note")) if flag else None})
        previous = s["start"]
    # A production without captions stays truthful; never invent sentences to make its desk look complete.
    return out


def read(kind, pid=None):
    """(configured, result, safe reason). Errors never carry exception text or private paths to the API."""
    try:
        cfg = settings()
        if not cfg.studio_adapter:
            return False, None, None
        if _memory_limit_failed:
            raise AdapterError("call")
        mod = module(cfg)
        root = media_root(cfg, mod)
        try:
            with quiet():
                raw = mod.desk() if kind == "desk" else mod.project(pid)
        except (Exception, SystemExit):
            raise AdapterError("call") from None
        value = clean_desk(raw) if kind == "desk" else clean_project(raw, pid, root)
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > cockpit_apps.API_OUT - 4096:
            raise AdapterError("data")
        return True, value, None
    except AdapterError as e:
        return True, None, str(e)
    except MemoryError:
        return True, None, "call"
    except Exception:
        return True, None, "data"
