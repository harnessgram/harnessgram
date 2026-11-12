"""The owner's draft marks: keep, tighten or remove, per sentence of a video. The only thing the Studio writes.

Marks are a local draft: nothing is rendered, sent or published because of them. They live in one small file,
`<app folder>/state/marks.json`, written the way the install writes (safefs.py: no write ever follows a link, a temp
file created exclusively and then renamed over the name), under a lock so two quick taps never lose one another.

    {"<project id>": {"<sentence i>": {"mark": "keep"|"tighten"|"remove", "text"?: "<the owner's shorter wording>"}}}

`set_mark` validates everything against the provider's own project (a known video, a sentence it has, one of the
three marks, a short plain text only with `tighten`) and refuses anything else with a plain code.
"""
from __future__ import annotations

import fcntl
import json
import os
import re

from . import provider, safefs

STATE = "state"
FILE = "marks.json"
LOCK = ".lock"
MARKS = ("keep", "tighten", "remove")
MAX_FILE = 64 * 1024
MAX_PROJECTS = 40
MAX_TEXT = 240
PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


class MarkError(ValueError):
    """A request the store refuses; `code` is what the API answers."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _open_state(folder: str, create: bool):
    """The state folder's descriptor (None when it does not exist yet and `create` is false). Neither the app folder
    nor the state folder may be a link."""
    try:
        app_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise MarkError("unavailable") from None
    try:
        if not create:
            try:
                return os.open(STATE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=app_fd)
            except FileNotFoundError:
                return None
            except OSError:
                raise MarkError("unavailable") from None
        try:
            return safefs.open_dir(app_fd, STATE, 0o700)
        except OSError:
            raise MarkError("unavailable") from None
    finally:
        os.close(app_fd)


def _load(state_fd: int) -> dict:
    try:
        text = safefs.read_file(state_fd, FILE, MAX_FILE)
    except OSError:
        raise MarkError("unavailable") from None
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}  # a damaged draft file starts over rather than blocking the owner
    return data if isinstance(data, dict) else {}


def _clean_project(raw, sentence_ids) -> dict:
    out = {}
    if not isinstance(raw, dict):
        return out
    sentence_ids = set(sentence_ids)
    for k, v in raw.items():
        if not (isinstance(k, str) and k.isdigit() and int(k) in sentence_ids and isinstance(v, dict)):
            continue
        mark = v.get("mark")
        if mark not in MARKS:
            continue
        item = {"mark": mark}
        text = v.get("text")
        if mark == "tighten" and isinstance(text, str) and text and len(text) <= MAX_TEXT and not CONTROL.search(text):
            item["text"] = text
        out[k] = item
    return out


def _open_lock(state_fd: int) -> int:
    """The lock file's descriptor (never through a link). Two first taps at once can make macOS answer ENOENT to a
    plain O_CREAT while the other creates the file, so that one answer is tried again a few times."""
    for _ in range(20):
        try:
            return os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=state_fd)
        except FileNotFoundError:
            continue
        except OSError:
            break
    raise MarkError("unavailable")


def get(folder, pid: str) -> dict:
    """The marks of one video ({} when there are none, or when the API runs without its app folder). Read-only."""
    proj = provider.project(pid)
    if not folder or proj is None or proj.get("notice"):
        return {}
    fd = _open_state(folder, create=False)
    if fd is None:
        return {}
    try:
        return _clean_project(_load(fd).get(pid), (s["i"] for s in proj["sentences"]))
    finally:
        os.close(fd)


def _text(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise MarkError("bad_request")
    text = " ".join(value.split())
    if not text or len(text) > MAX_TEXT or CONTROL.search(text):
        raise MarkError("bad_request")
    return text


def set_mark(folder, pid, i, mark, text=None) -> dict:
    """Set (or with `mark="clear"` drop) the mark of sentence `i` of video `pid`; returns that video's marks."""
    if not folder:
        raise MarkError("unavailable")
    if not isinstance(pid, str) or not PROJECT_ID.match(pid):
        raise MarkError("bad_request")
    if isinstance(i, bool) or not isinstance(i, int) or mark not in MARKS + ("clear",):
        raise MarkError("bad_request")
    if text is not None and mark != "tighten":
        raise MarkError("bad_request")
    text = _text(text)
    proj = provider.project(pid)
    if proj is None:
        raise MarkError("not_found")
    if proj.get("notice"):
        raise MarkError("unavailable")  # never attach invented sample sentences to a real production
    sentence_ids = {s["i"] for s in proj["sentences"]}
    if i not in sentence_ids:
        raise MarkError("bad_request")
    fd = _open_state(folder, create=True)
    try:
        lock = _open_lock(fd)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = _load(fd)
            mine = _clean_project(data.get(pid), sentence_ids)
            if mark == "clear":
                mine.pop(str(i), None)
            else:
                mine[str(i)] = {"mark": mark, **({"text": text} if text else {})}
            if mine:
                data[pid] = mine
            else:
                data.pop(pid, None)
            data = {k: v for k, v in data.items() if isinstance(k, str) and PROJECT_ID.match(k) and isinstance(v, dict)}
            if len(data) > MAX_PROJECTS:  # the oldest drafts go first (insertion order), the current one stays
                for k in [k for k in data if k != pid][:len(data) - MAX_PROJECTS]:
                    del data[k]
            blob = json.dumps(data, ensure_ascii=False, sort_keys=False)
            if len(blob.encode("utf-8")) > MAX_FILE:
                raise MarkError("too_large")
            try:
                safefs.write_file(fd, FILE, blob + "\n", 0o600)
            except OSError:
                raise MarkError("unavailable") from None
            return mine
        finally:
            os.close(lock)  # closing the descriptor drops the lock
    finally:
        os.close(fd)
