"""The owner's own picture for a project (Cockpit v2 tiles).

Pictures live in a private folder under the bridge state directory, one file per project. Every file is
validated on the way in (type by magic bytes, size, and a square 512 px re-encode when Pillow is available) and
read back through the same guards as the radio's HLS files: a strict file name, no symlinks, regular files only.
A picture is decoration, never a project identity or an authorization.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from . import card_media, work_lines
from .config import state_dir, write_private_bytes

ART_FOLDER = "project-art"
SUFFIX = ".webp"
MAX_BYTES = 1_000_000  # at most 1 MB, before and after the re-encode
SIDE = 512
MAX_PIXELS = 40_000_000  # refuse decompression bombs before decoding
# A readable line key is its own file name; any other key (Hebrew, spaces, slashes) is hashed. The hashed form
# starts with "_", which a readable key cannot, so the two never collide.
READABLE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
HASHED = re.compile(r"_[0-9a-f]{24}")
NAME = re.compile(r"(?:%s|%s)" % (READABLE.pattern, HASHED.pattern))


class ArtError(Exception):
    """code: bad (the request), type, size, decode, needs_pillow."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def folder():
    return state_dir() / ART_FOLDER


def name_for(project: object) -> str:
    """The file stem of a project's picture ("" when the project has no usable name)."""
    key = work_lines.line_key(project) if isinstance(project, str) else ""
    if not key:
        return ""
    if READABLE.fullmatch(key):
        return key
    return "_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def kind_of(data: bytes) -> str:
    """"webp", "png" or "jpeg" by magic bytes; "" for anything else (the file name never counts)."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    return ""


def _pillow():
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    return Image, ImageOps


def validate(data: bytes) -> bytes:
    """The bytes to store.

    With Pillow: a square 512 px webp. Without it: macOS sips (the resize path of card_media.py) turns a png or jpeg
    into a square 512 px jpeg; a webp of at most 1 MB is stored as it is; anything else is refused (needs_pillow).
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ArtError("bad")
    data = bytes(data)
    if len(data) > MAX_BYTES:
        raise ArtError("size")
    kind = kind_of(data)
    if not kind:
        raise ArtError("type")
    pil = _pillow()
    if pil is None:
        return data if kind == "webp" else _with_sips(data)
    Image, ImageOps = pil
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format not in ("WEBP", "PNG", "JPEG") or img.width * img.height > MAX_PIXELS:
                raise ArtError("decode")
            img.load()
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
            img = ImageOps.fit(img, (SIDE, SIDE), Image.LANCZOS)
            out = io.BytesIO()
            img.save(out, "WEBP", quality=85, method=4)
    except ArtError:
        raise
    except Exception:  # a damaged or hostile image: Pillow raises many kinds
        raise ArtError("decode")
    if len(out.getvalue()) > MAX_BYTES:
        raise ArtError("size")
    return out.getvalue()


def _with_sips(data: bytes) -> bytes:
    """A png or jpeg as a square 512 px jpeg through sips, as card_media.prepare does for card images."""
    sips = shutil.which("sips")
    if not sips:
        raise ArtError("needs_pillow")
    dims = []
    try:
        clean, _ = card_media.raster(data, dims)  # structure and dimensions first, then metadata is dropped
    except card_media.MediaError:
        raise ArtError("decode")
    turn = card_media._SIPS_TURN.get(card_media.exif_orientation(data), [])
    w, h = dims
    if turn and turn[0] == "-r" and turn[1] in ("90", "270"):
        w, h = h, w
    scale = SIDE / min(w, h)
    try:
        with tempfile.TemporaryDirectory(prefix="project-art-") as temp:
            src, dest = Path(temp) / "input", Path(temp) / "output.jpg"
            src.write_bytes(clean)
            proc = subprocess.run([sips, *turn, "--resampleHeightWidth", str(max(SIDE, round(h * scale))),
                                   str(max(SIDE, round(w * scale))), "-c", str(SIDE), str(SIDE), "-s", "format", "jpeg",
                                   "-s", "formatOptions", "85", str(src), "--out", str(dest)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            out = dest.read_bytes() if not proc.returncode and dest.is_file() else b""
    except (OSError, subprocess.SubprocessError):
        raise ArtError("decode")
    if kind_of(out) != "jpeg":
        raise ArtError("decode")
    if len(out) > MAX_BYTES:
        raise ArtError("size")
    return out


def _open_folder() -> int:
    return os.open(folder(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def read(name: str) -> bytes:
    """Only a regular file of the strict name beneath the private folder; never a link, never a path."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise OSError("not a picture")
    root_fd = _open_folder()
    try:
        fd = os.open(name + SUFFIX, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    finally:
        os.close(root_fd)
    with os.fdopen(fd, "rb") as stream:
        st = os.fstat(stream.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            raise OSError("not a picture")
        return stream.read(MAX_BYTES + 1)


def stamp(project: object) -> Optional[dict]:
    """{"name", "v"} when the project has a valid custom picture (v is a string that changes with the file), else None."""
    name = name_for(project)
    if not name:
        return None
    try:
        root_fd = _open_folder()
    except OSError:
        return None
    try:
        st = os.stat(name + SUFFIX, dir_fd=root_fd, follow_symlinks=False)
    except OSError:
        return None
    finally:
        os.close(root_fd)
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
        return None
    return {"name": name, "v": "%d-%d" % (st.st_mtime_ns, st.st_size)}


def store(project: object, data: bytes) -> dict:
    """Validate and save; returns the stamp. Raises ArtError."""
    name = name_for(project)
    if not name:
        raise ArtError("bad")
    body = validate(data)
    write_private_bytes(folder() / (name + SUFFIX), body)
    return stamp(project) or {"name": name, "v": "0"}


def clear(project: object) -> bool:
    """Remove the picture (a link is removed, never followed); True when something was removed."""
    name = name_for(project)
    if not name:
        return False
    try:
        root_fd = _open_folder()
    except OSError:
        return False
    try:
        os.unlink(name + SUFFIX, dir_fd=root_fd)
    except OSError:
        return False
    finally:
        os.close(root_fd)
    return True
