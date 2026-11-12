"""Writes that never follow a link: the Studio's install and its local draft marks both go through here.

A folder is opened component by component with `O_NOFOLLOW` (a link where a folder should be is refused), a file is
written to a fresh temp file created exclusively and then atomically renamed over the name (a link planted at the name
is replaced, its target never written), and a file is read with `O_NOFOLLOW` (a planted link is refused).
"""
from __future__ import annotations

import os
import secrets
import stat


class Refused(OSError):
    """A link, or something that is not a plain folder or file, is in the way."""


def open_dir(parent_fd: int, name: str, mode: int = 0o755) -> int:
    """Open (creating it when missing) the folder `name` inside `parent_fd`, never following a link."""
    try:
        os.mkdir(name, mode, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as e:
        raise Refused(f"{name!r} is a link or not a folder: {e.strerror}; remove it and run the install again") from None


def write_file(dir_fd: int, name: str, text: str, mode: int = 0o644) -> None:
    """Write `name` inside the folder: a fresh temp file (new, never a link), then an atomic replace. If `name`
    is a link it is replaced by the file; the link's target is never written."""
    tmp = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise


def read_file(dir_fd: int, name: str, limit: int):
    """The text of the plain file `name` inside the folder (None when it does not exist); a link is refused, and a
    file over `limit` bytes reads as None too (it is never trusted half-read)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise Refused(f"{name!r} is a link or cannot be read: {e.strerror}") from None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise Refused(f"{name!r} is not a plain file")
        raw = f.read(limit + 1)
    if len(raw) > limit:
        return None
    return raw.decode("utf-8", "replace")


def open_readonly(root, relative: str) -> int:
    """Open an existing regular file below root, without following any link or creating anything.

    The root is an administrator-selected directory. All components below it, including the file, are
    opened relative to directory descriptors, so a concurrent link replacement cannot redirect the read.
    The caller owns the returned descriptor.
    """
    parts = relative.split("/")
    if (not relative or "\\" in relative or "\x00" in relative
            or any(not p or p.startswith(".") for p in parts)):
        raise Refused("not a plain relative file path")
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise Refused("not a regular media file")
        return fd
    finally:
        os.close(parent)
