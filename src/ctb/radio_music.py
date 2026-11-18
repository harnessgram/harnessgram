"""Private radio music library. Playback must use an isolated private audio path."""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import radio_podcast, radio_suitability
from .config import Config, locked_map, private_dir, state_dir

MAX_BYTES = 60 * 1024 * 1024
CHECKER_VERSION = 1
_RECHECKING = set()
_RECHECK_LOCK = threading.Lock()


def enabled():
    from .config import setting_on
    # This is an add-on, not part of the station's baseline behaviour.  An
    # installation that has never chosen it must therefore remain silent.
    return setting_on("radio_music")


def set_enabled(on):
    from .config import set_setting
    set_setting("radio_music", bool(on))
    return {"enabled": enabled()}


def folder():
    return private_dir(state_dir() / "radio" / "music")


def _verdict_revision(cfg):
    """Hash every input to the music verdict, not merely the checker's path or mtime."""
    from . import radio
    checker = radio_podcast._checker_path(cfg)
    try:
        if not checker.is_file():
            return None
        digest = hashlib.sha256(f"radio-music:{CHECKER_VERSION}\0".encode())
        # The installed checker keeps its version and rule/list data in its own
        # content-suitability tree. Tests may use a lone temporary checker.
        checker_files = ([checker] if checker.parent.name != "content-suitability" else
                         sorted(p for p in checker.parent.rglob("*") if p.is_file()))
        for path in checker_files:
            digest.update(str(path.relative_to(checker.parent)).encode("utf-8", "surrogateescape"))
            digest.update(b"\0" + path.read_bytes() + b"\0")
        shared = radio_suitability.SHARED_CHECKER
        if shared != checker:
            if not shared.is_file():
                return None
            digest.update(b"shared-checker\0" + shared.read_bytes() + b"\0")
        custom = radio.settings(cfg).get("room_suitability_file") or ""
        if custom:
            digest.update(b"room-rules\0" + Path(os.path.expanduser(custom)).read_bytes() + b"\0")
        digest.update(radio_suitability.FALLBACK.pattern.encode("utf-8"))
        digest.update(str(radio_suitability.FALLBACK.flags).encode())
        return f"{CHECKER_VERSION}:{digest.hexdigest()}"
    except OSError:
        return None


def _check(cfg, item):
    """Return (approved, reason, revision); unavailable or changing rules fail closed."""
    revision = _verdict_revision(cfg)
    if revision is None:
        return False, "בודק ההתאמה אינו זמין", None
    title, artist = str(item.get("title", "")), str(item.get("artist", ""))
    metadata = " ".join(str(v) for v in (item.get("metadata") or {}).values())
    description = " ".join((artist, str(item.get("description", "")), metadata))
    text = " ".join((title, description))
    try:
        radio_podcast.gate(cfg, title, description)
        checker = radio_podcast._checker_path(cfg)
        if checker != radio_suitability.SHARED_CHECKER and not radio_podcast._checker_says_ok(
                radio_suitability.SHARED_CHECKER, text):
            raise radio_podcast.PodcastError("unsuitable")
        normalized = radio_podcast._SUIT_NORM.sub("", text.replace("\u05be", " "))
        if radio_suitability.FALLBACK.search(normalized):
            raise radio_podcast.PodcastError("unsuitable")
    except radio_podcast.PodcastError as exc:
        return False, ("בודק ההתאמה אינו זמין" if exc.kind == "rules" else
                       "פרטי היצירה לא עברו את בדיקת ההתאמה"), revision
    if revision != _verdict_revision(cfg):
        return False, "בודק ההתאמה אינו זמין", None
    return True, "", revision


def _probe(path):
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-show_entries",
                            "stream=codec_type:stream_tags:format_tags", "-of", "json", str(path)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=15, check=True)
        data = json.loads(p.stdout)
        streams = data.get("streams", [])
        if not streams or not any(s.get("codec_type") == "audio" for s in streams) or any(
                s.get("codec_type") != "audio" for s in streams):
            raise ValueError("הקובץ אינו קובץ שמע בלבד")
        tags = dict(data.get("format", {}).get("tags") or {})
        for n, stream in enumerate(streams):
            for key, value in (stream.get("tags") or {}).items():
                tags[f"stream_{n}_{key}"] = value
        return {str(k).lower(): str(v) for k, v in tags.items()}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        raise ValueError("לא ניתן לוודא שזה קובץ שמע ללא וידאו") from None


def add(path_or_bytes, title="", artist="", *, cfg=None, description="", now=None):
    """Check all available textual tags and retain even rejected entries as ineligible."""
    dest_dir = folder()
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix="upload-")
    try:
        with os.fdopen(fd, "wb") as out:
            if isinstance(path_or_bytes, (bytes, bytearray)):
                data = bytes(path_or_bytes)
                if len(data) > MAX_BYTES:
                    raise ValueError("הקובץ גדול מ־60 מגה־בייט")
                out.write(data)
            else:
                with open(path_or_bytes, "rb") as source:
                    total = 0
                    while chunk := source.read(65536):
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise ValueError("הקובץ גדול מ־60 מגה־בייט")
                        out.write(chunk)
        tags = _probe(tmp)
        title = str(title or tags.get("title") or "").strip()
        artist = str(artist or tags.get("artist") or "").strip()
        metadata = " ".join([str(description)] + list(tags.values()))
        reason = ""
        revision = None
        if not title or not artist:
            reason = "חסרים שם היצירה או שם המבצע לבדיקה"
        else:
            _approved, reason, revision = _check(cfg or Config(), {"title": title, "artist": artist,
                                                                   "description": description, "metadata": tags})
        ident = uuid.uuid4().hex[:16]
        item = {"id": ident, "title": title, "artist": artist, "description": str(description),
                "metadata": tags, "added": time.time() if now is None else now,
                "source": "upload", "checked_ok": not bool(reason), "reason": reason,
                "verdict_revision": revision,
                "likes": 0, "skips": 0, "plays": 0, "like_times": [], "skip_times": [], "play_times": []}
        if not reason:
            os.chmod(tmp, 0o600)
            os.replace(tmp, dest_dir / (ident + ".audio"))
        with locked_map(dest_dir / "index.json", keep=100000) as index:
            index[ident] = item
        return item
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def listing():
    from .config import read_map
    return list(read_map(folder() / "index.json").values())


def _vote(ident, field, now=None):
    if not re.fullmatch(r"[0-9a-f]{16}", str(ident)):
        raise ValueError("יצירה לא נמצאה")
    with locked_map(folder() / "index.json", keep=100000) as index:
        item = index.get(ident)
        if not isinstance(item, dict):
            raise ValueError("יצירה לא נמצאה")
        item[field] = item.get(field, 0) + 1
        item[field[:-1] + "_times"].append(time.time() if now is None else now)
        return dict(item)


def like(ident, now=None):
    return _vote(ident, "likes", now)


def skip(ident, now=None):
    return _vote(ident, "skips", now)


def _recheck(ident, cfg):
    try:
        with locked_map(folder() / "index.json", keep=100000) as index:
            item = dict(index.get(ident) or {})
        approved, reason, revision = _check(cfg, item)
        with locked_map(folder() / "index.json", keep=100000) as index:
            if isinstance(index.get(ident), dict):
                index[ident].update(checked_ok=approved, reason=reason, verdict_revision=revision)
    finally:
        with _RECHECK_LOCK:
            _RECHECKING.discard(ident)


def _queue_recheck(item, cfg):
    ident = str(item.get("id", ""))
    with _RECHECK_LOCK:
        if ident in _RECHECKING:
            return
        _RECHECKING.add(ident)
    threading.Thread(target=_recheck, args=(ident, cfg), daemon=True, name="radio-music-suitability").start()


def approved_for_attach(item, cfg):
    """Last look before audio attachment; stale verdicts are rechecked off-thread."""
    revision = _verdict_revision(cfg)
    if revision is None or item.get("checked_ok") is not True or item.get("verdict_revision") != revision:
        _queue_recheck(item, cfg)
        return False
    return True


def pick(now=None, *, rng=random, cfg=None):
    now = time.time() if now is None else now
    cfg = cfg or Config()
    revision = _verdict_revision(cfg)
    with locked_map(folder() / "index.json", keep=100000) as index:
        items = [v for v in index.values() if isinstance(v, dict)]
        stale = [i for i in items if i.get("checked_ok") is True and i.get("verdict_revision") != revision]
        played_today = {i["id"] for i in items for t in i.get("play_times", []) if 0 <= now - t < 86400}
        eligible = [i for i in items if revision is not None and i.get("checked_ok") is True
                    and i.get("verdict_revision") == revision and i.get("id") not in played_today
                    and i.get("skips", 0) <= i.get("likes", 0)
                    and (folder() / (str(i.get("id")) + ".audio")).is_file()]
        if not eligible:
            item = None
        else:
            weights = [(1 + max(0, i.get("likes", 0))) / (1 + max(0, i.get("skips", 0)) +
                       sum(1 for t in i.get("play_times", []) if 0 <= now - t < 86400)) for i in eligible]
            item = rng.choices(eligible, weights=weights, k=1)[0]
            item["plays"] += 1
            item["play_times"].append(now)
            item = dict(item)
    for candidate in stale:
        _queue_recheck(candidate, cfg)
    return item


def playback(item=None):
    """Publish/read the private player's small, non-secret now-playing record."""
    from .config import read_map, write_private
    path = folder() / "playing.json"
    if item is None:
        data = read_map(path)
        return {k: data[k] for k in ("id", "title", "artist") if isinstance(data.get(k), str)}
    if item is False:
        path.unlink(missing_ok=True)
        (folder() / "skip.json").unlink(missing_ok=True)
        return {}
    data = {k: str(item.get(k, "")) for k in ("id", "title", "artist")}
    if playback().get("id") != data["id"]:
        (folder() / "skip.json").unlink(missing_ok=True)
    write_private(path, json.dumps(data, ensure_ascii=False))
    return data


def request_skip(ident):
    """Ask the daemon to stop this exact item; a stale request cannot skip a later track."""
    from .config import write_private
    if not re.fullmatch(r"[0-9a-f]{16}", str(ident)):
        raise ValueError("יצירה לא נמצאה")
    playing = playback().get("id")
    if playing != ident:
        raise ValueError("יצירה לא נמצאה")
    skip(playing)
    write_private(folder() / "skip.json", json.dumps({"id": playing}))


def skip_requested(ident):
    from .config import read_map
    path = folder() / "skip.json"
    requested = read_map(path).get("id") == ident
    if requested:
        path.unlink(missing_ok=True)
    return requested
