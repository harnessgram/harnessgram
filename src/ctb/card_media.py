"""Private choice-card images. Source names never cross the publishing boundary.

No required dependencies: prefer Pillow when installed, then macOS sips. The
bounded fallback keeps the original raster container, stripping its metadata.
All filesystem operations use a verified directory descriptor and opaque IDs.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import secrets
import shutil
import stat
import struct
import subprocess
import tempfile
import time
import warnings
import zlib
from pathlib import Path

from .config import private_dir, state_dir

ID = re.compile(r"[0-9a-f]{32}")
MAX_SOURCE = 20 * 1024 * 1024  # strictly below 20 MiB
MAX_STORED = 4 * 1024 * 1024
MAX_SIDE = 1600
MAX_PIXELS = 40_000_000
GRACE = 10 * 60
TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}


class MediaError(ValueError):
    pass


@contextlib.contextmanager
def directory(create=False):
    root = state_dir() / "card-media"
    if create:
        # Do not chmod/follow an existing symlink through private_dir.
        if root.is_symlink():
            raise MediaError("private image storage is unavailable")
        private_dir(root)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


def _dimensions(w, h):
    if not w or not h or w * h > MAX_PIXELS:
        raise MediaError("image dimensions are too large or invalid")


def raster(data, dimensions=None):
    """Validate the raster container and discard metadata, including EXIF/XMP.

    Decoding is also checked by Pillow/sips when available. The portable path
    accepts only structurally complete PNG/JPEG/WebP, never extension-based MIME.
    """
    def size(w, h):
        _dimensions(w, h)
        if dimensions is not None:
            dimensions[:] = [w, h]
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            out, p, seen = [data[:8]], 8, set()
            while p + 12 <= len(data):
                n = int.from_bytes(data[p:p+4], "big")
                kind, chunk = data[p+4:p+8], data[p+8:p+8+n]
                if p + 12 + n > len(data) or zlib.crc32(kind + chunk) != int.from_bytes(data[p+8+n:p+12+n], "big"):
                    break
                if not seen and (kind != b"IHDR" or n != 13):
                    break
                if kind == b"IHDR":
                    size(*struct.unpack(">II", chunk[:8]))
                # Keep only pixels, palette and transparency; drop EXIF, text,
                # profiles and animation. No private metadata in the copy.
                if kind in (b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"):
                    out.append(data[p:p+12+n])
                seen.add(kind)
                p += 12 + n
                if kind == b"IEND":
                    if n == 0 and p == len(data) and b"IDAT" in seen:
                        return b"".join(out), "png"
                    break
        elif data.startswith(b"\xff\xd8"):
            out, p, dims = [data[:2]], 2, False
            while p + 4 <= len(data):
                if data[p] != 255:
                    break
                start = p
                while p < len(data) and data[p] == 255:
                    p += 1
                marker = data[p]
                p += 1
                n = int.from_bytes(data[p:p+2], "big")
                if n < 2 or p + n > len(data):
                    break
                if marker in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", data[p+3:p+7])
                    size(w, h)
                    dims = True
                if marker == 0xDA:
                    if dims and data.endswith(b"\xff\xd9"):
                        return b"".join(out) + data[start:], "jpeg"
                    break
                # Keep only what decoding needs: JFIF (APP0), ICC colour (APP2) and Adobe colour transform (APP14).
                # Every other APPn (EXIF/XMP, IPTC, maker notes, vendor blocks) and comments are dropped.
                if not (0xE0 <= marker <= 0xEF or marker == 0xFE) or marker in (0xE0, 0xE2, 0xEE):
                    out.append(data[start:p+n])
                p += n
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP" and int.from_bytes(data[4:8], "little") + 8 == len(data):
            chunks, p, dims = [], 12, False
            while p + 8 <= len(data):
                kind = data[p:p+4]
                n = int.from_bytes(data[p+4:p+8], "little")
                chunk = data[p+8:p+8+n]
                end = p + 8 + n + (n % 2)
                if end > len(data):
                    break
                if kind == b"VP8 " and chunk[3:6] == b"\x9d\x01\x2a":
                    w, h = struct.unpack("<HH", chunk[6:10])
                    size(w & 0x3FFF, h & 0x3FFF)
                    dims = True
                elif kind == b"VP8L" and chunk[0] == 0x2F:
                    bits = int.from_bytes(chunk[1:5], "little")
                    size((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
                    dims = True
                elif kind == b"VP8X":
                    if len(chunk) != 10 or chunk[0] & 2:  # no animated payloads
                        break
                    size(int.from_bytes(chunk[4:7], "little") + 1, int.from_bytes(chunk[7:10], "little") + 1)
                    chunk = bytes([chunk[0] & ~0x2C]) + chunk[1:]
                if kind in (b"VP8 ", b"VP8L", b"VP8X", b"ALPH"):
                    chunks.append(kind + len(chunk).to_bytes(4, "little") + chunk + (b"\0" if len(chunk) % 2 else b""))
                p = end
            if dims and p == len(data):
                payload = b"WEBP" + b"".join(chunks)
                return b"RIFF" + len(payload).to_bytes(4, "little") + payload, "webp"
    except (IndexError, struct.error):
        pass
    raise MediaError("each image must be a valid PNG, JPEG or WebP")


_SIPS_TURN = {2: ["-f", "horizontal"], 3: ["-r", "180"], 4: ["-f", "vertical"], 5: ["-r", "90", "-f", "horizontal"],
              6: ["-r", "90"], 7: ["-r", "270", "-f", "horizontal"], 8: ["-r", "270"]}


def exif_orientation(data):
    """The EXIF Orientation (1-8) of a JPEG, or None. Bounded reads only; any malformed block gives None."""
    try:
        if data[:2] != b"\xff\xd8":
            return None
        p = 2
        while p + 4 <= len(data) and data[p] == 0xFF:
            marker, n = data[p + 1], int.from_bytes(data[p + 2:p + 4], "big")
            if marker == 0xDA or n < 2:
                return None
            body = data[p + 4:p + 2 + n]
            if marker == 0xE1 and body[:6] == b"Exif\0\0":
                tiff = body[6:]
                order = {b"II": "little", b"MM": "big"}.get(tiff[:2])
                if not order:
                    return None
                ifd = int.from_bytes(tiff[4:8], order)
                count = int.from_bytes(tiff[ifd:ifd + 2], order)
                for i in range(min(count, 512)):
                    e = ifd + 2 + 12 * i
                    if int.from_bytes(tiff[e:e + 2], order) == 0x0112:
                        value = int.from_bytes(tiff[e + 8:e + 10], order)
                        return value if 1 <= value <= 8 else None
                return None
            p += 2 + n
    except (IndexError, ValueError):
        return None
    return None


def _pillow(data):
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as src:
            if src.format not in ("PNG", "JPEG", "WEBP"):
                raise MediaError("each image must be a PNG, JPEG or WebP")
            _dimensions(*src.size)
            image = ImageOps.exif_transpose(src)
            image.thumbnail((MAX_SIDE, MAX_SIDE))
            # A fresh image prevents source metadata from propagating.
            clean = Image.new("RGB", image.size, "white")
            if "A" in image.getbands() or "transparency" in image.info:
                rgba = image.convert("RGBA")
                clean.paste(rgba, mask=rgba.getchannel("A"))
            else:
                clean.paste(image.convert("RGB"))
            out = io.BytesIO()
            clean.save(out, format="JPEG", quality=88, optimize=True)
            return out.getvalue()


def prepare(source):
    """Read a bounded regular file once. Never include source paths in errors."""
    try:
        fd = os.open(Path(source).expanduser(), os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size < MAX_SOURCE:
                raise MediaError("each image must be a regular file under 20 MiB")
            data = f.read(MAX_SOURCE)
        if len(data) >= MAX_SOURCE:
            raise MediaError("each image must be under 20 MiB")
        # Check the original before allocating a decoder, but orient from the
        # original EXIF before stripping it during the final encode.
        dimensions = []
        stripped, _ = raster(data, dimensions)
        result = _pillow(data)
        turn = _SIPS_TURN.get(exif_orientation(data), [])
        if result is None:
            sips = shutil.which("sips")
            if sips:
                with tempfile.TemporaryDirectory(prefix="card-image-") as temp:
                    src, dest = Path(temp) / "input", Path(temp) / "output.jpg"
                    src.write_bytes(stripped)
                    resize = ["-Z", str(MAX_SIDE)] if max(dimensions) > MAX_SIDE else []
                    # EXIF was stripped above, so the phone's orientation is applied here explicitly.
                    proc = subprocess.run([sips, *resize, *turn, "-s", "format", "jpeg", str(src), "--out", str(dest)],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                    if proc.returncode or not dest.is_file() or dest.stat().st_size > MAX_STORED:
                        raise MediaError("image conversion failed")
                    result = dest.read_bytes()
            else:
                # Without a decoder the copy can be neither re-encoded clean, downscaled nor turned upright: refuse.
                raise MediaError("this image needs Pillow or sips to be resized, cleaned or turned upright")
        result, _ = raster(result)
        if len(result) > MAX_STORED:
            raise MediaError("image copy exceeds the 4 MiB storage cap")
        return result
    except MediaError:
        raise
    except Exception:
        raise MediaError("image could not be read or converted") from None


def store_many(paths):
    ids = []
    try:
        with directory(create=True) as root:
            for path in paths:
                data = prepare(path)
                ident = secrets.token_hex(16)
                fd = os.open(ident, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
                ids.append(ident)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
        return ids
    except BaseException:
        discard(ids)
        raise


def discard(ids):
    if not ids:
        return
    try:
        with directory() as root:
            for ident in ids:
                if isinstance(ident, str) and ID.fullmatch(ident):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(ident, dir_fd=root)
    except OSError:
        pass


def read(ident):
    if not isinstance(ident, str) or not ID.fullmatch(ident):
        return None
    try:
        with directory() as root:
            fd = os.open(ident, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            with os.fdopen(fd, "rb") as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= MAX_STORED:
                    return None
                data = f.read(MAX_STORED + 1)
        if len(data) > MAX_STORED:
            return None
        _, kind = raster(data)
        return data, TYPES[kind]
    except (OSError, MediaError):
        return None


def cleanup(records, now=None):
    """Called under the cards lock; protect live cards and recently closed copies.

    Unknown files get an orphan grace too (a publisher may be preparing copies
    before taking that lock). Never follow symlinks, recurse, or delete sources.
    """
    now = time.time() if now is None else now
    keep, expired = set(), set()
    for rec in records:
        ids = rec.get("images", [])
        if rec["state"] == "open" or now - rec["closed"] < GRACE:
            keep.update(ids)
        else:
            expired.update(ids)
    try:
        with directory() as root:
            for name in os.listdir(root):
                if not ID.fullmatch(name) or name in keep:
                    continue
                st = os.stat(name, dir_fd=root, follow_symlinks=False)
                if name in expired or now - st.st_mtime >= GRACE:
                    os.unlink(name, dir_fd=root)
    except OSError:
        pass
