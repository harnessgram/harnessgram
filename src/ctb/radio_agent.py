"""Guard, approve, and queue agent-created audio or video for the private radio.

Items enter only after their title and complete transcript pass the radio suitability
check, or after an owner presses the button.  File guards and a SHA-256 fingerprint
are checked again immediately before playback.  Playback uses the podcast player's
private mixer, so it can never enter ``public-hls``.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from .config import locked_map, state_dir

MAX_BYTES = 500 * 1024 * 1024
MAX_TEXT = 20_000
MAX_TEXT_FILE = 1024 * 1024
KEEP = 200
STORE = "radio-agent.json"
CALLBACK = re.compile(r"rq:[0-9a-f]{10}")
AV_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac", ".mp4", ".mov", ".webm",
          ".mkv", ".m4v", ".avi", ".mpg", ".mpeg", ".3gp", ".wma", ".aiff", ".aif", ".caf", ".ogv", ".flv"}
DIRECT_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac", ".mp4", ".mov", ".webm", ".mkv"}
CONVERT_SECONDS = 900
TOKEN_SECONDS = 24 * 60 * 60


class AgentMediaError(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def enabled(cfg) -> bool:
    from . import addons
    try:
        return addons.enabled(cfg, "radio_agent_media")
    except Exception:  # noqa: BLE001 - an unreadable switch fails closed
        return False


def check_file(cfg, raw: object) -> Path:
    """Resolve and validate an ordinary media file below the user's home."""
    from . import mirror
    if not isinstance(raw, (str, os.PathLike)) or not str(raw).strip() or "\0" in str(raw):
        raise AgentMediaError("notfile")
    home = os.path.realpath(os.path.expanduser("~"))
    real = os.path.realpath(os.path.abspath(os.path.expanduser(str(raw))))
    if real == home or not real.startswith(home.rstrip(os.sep) + os.sep):
        raise AgentMediaError("outside")
    try:
        st = os.stat(real)
    except OSError:
        raise AgentMediaError("notfile") from None
    if not stat.S_ISREG(st.st_mode):
        raise AgentMediaError("notfile")
    if Path(real).suffix.lower() not in AV_EXT:
        raise AgentMediaError("type")
    if st.st_size <= 0 or st.st_size > MAX_BYTES:
        raise AgentMediaError("size")
    if mirror.channel_may_copy(cfg, real) is None:
        raise AgentMediaError("denied")
    return Path(real)


def read_text(raw: Optional[str]) -> str:
    if not raw:
        return ""
    try:
        path = Path(os.path.expanduser(raw))
        if not path.is_file() or path.stat().st_size > MAX_TEXT_FILE:
            raise AgentMediaError("notfile")
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise AgentMediaError("notfile") from None
    if len(text) > MAX_TEXT:
        raise AgentMediaError("textsize")
    return text


def fingerprint(real: Path) -> dict:
    digest = hashlib.sha256()
    try:
        with real.open("rb") as source:
            before = os.fstat(source.fileno())
            for block in iter(lambda: source.read(1 << 20), b""):
                digest.update(block)
            after = os.fstat(source.fileno())
    except OSError:
        raise AgentMediaError("notfile") from None
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise AgentMediaError("changed")
    return {"size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": digest.hexdigest()}


def unchanged(item: dict) -> bool:
    want = item.get("fingerprint")
    if not isinstance(want, dict) or not item.get("path"):
        return False
    try:
        st = os.stat(item["path"])
        if (st.st_size, st.st_mtime_ns) != (want.get("size"), want.get("mtime_ns")):
            return False
        return fingerprint(Path(item["path"]))["sha256"] == want.get("sha256")
    except (OSError, AgentMediaError):
        return False


def _private_copy(src: Path, item_id: str) -> None:
    from . import radio_podcast
    dest, part = radio_podcast.cache_dir() / f"{item_id}.audio", radio_podcast.cache_dir() / f"{item_id}.audio.part"
    try:
        if dest.is_file() and dest.stat().st_size:
            return
        from .radio_podcast import FORMATS
        done = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y",
                               "-protocol_whitelist", "file", "-format_whitelist", FORMATS,
                               "-i", str(src), "-vn", "-ac", "1",
                               "-c:a", "aac", "-b:a", "96k", "-f", "mp4", str(part)], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=CONVERT_SECONDS)
        if done.returncode or not part.is_file() or not part.stat().st_size:
            raise AgentMediaError("convert")
        os.chmod(part, 0o600)
        os.replace(part, dest)
    except (OSError, subprocess.SubprocessError):
        raise AgentMediaError("convert") from None
    finally:
        part.unlink(missing_ok=True)


def clean_title(title: str, real: Path) -> str:
    return " ".join((title or "").split())[:120] or re.sub(r"[._-]+", " ", real.stem).strip() or "audio"


def judged(title: str, text: str) -> bool:
    from . import radio_suitability
    return bool((text or "").strip()) and radio_suitability.suitable(f"{title}\n{text}")


def project_of(cfg, session: str) -> str:
    try:
        from . import recall
        return str(recall.project_for(cfg, session) or "")
    except Exception:  # noqa: BLE001
        return ""


def announcement(cfg, title: str, project: str) -> str:
    from . import radio
    from .i18n import t
    key = "radio_agent_next" if project else "radio_agent_next_plain"
    return t(key, lang=radio.settings(cfg)["lang"], title=title, project=project)


def queue_file(cfg, raw: object, title: str, session: str = "", text: str = "", approved: bool = False,
               project: Optional[str] = None) -> dict:
    from . import radio_podcast
    if not enabled(cfg):
        raise AgentMediaError("off")
    real = check_file(cfg, raw)
    title = clean_title(title, real)
    if len(text or "") > MAX_TEXT:
        raise AgentMediaError("textsize")
    if not approved:
        if not (text or "").strip():
            raise AgentMediaError("notext")
        if not judged(title, text):
            raise AgentMediaError("unsuitable")
    before = fingerprint(real)
    item_id = hashlib.sha256(f"{real}\0{before['sha256']}".encode()).hexdigest()[:16]
    project = project if project is not None else project_of(cfg, session)
    item = {"id": item_id, "title": title, "text": text or "", "project": project, "approved": approved,
            "announce": announcement(cfg, title, project)}
    if real.suffix.lower() in DIRECT_EXT:
        item.update(path=str(real), fingerprint=before)
    else:
        if any(queued.get("id") == item_id for queued in radio_podcast._read()["items"]):
            raise AgentMediaError("dup")
        _private_copy(real, item_id)
        if fingerprint(real) != before:
            (radio_podcast.cache_dir() / f"{item_id}.audio").unlink(missing_ok=True)
            raise AgentMediaError("changed")
    try:
        return radio_podcast.add_local(cfg, item)
    except radio_podcast.PodcastError as exc:
        if "path" not in item and exc.kind != "dup":
            (radio_podcast.cache_dir() / f"{item_id}.audio").unlink(missing_ok=True)
        raise AgentMediaError(exc.kind) from None


def listening_now(cfg) -> bool:
    from . import radio, radio_listeners
    from .inbox import car_mode_active
    if car_mode_active():
        return True
    try:
        return bool(radio.job_pid()) and bool(radio_listeners.snapshot(radio.folder(), radio.settings(cfg))["listening"])
    except Exception:  # noqa: BLE001
        return False


def auto_queue(cfg, raw: object, title: str, session: str, text: str) -> Optional[dict]:
    if not enabled(cfg) or not (text or "").strip() or not listening_now(cfg):
        return None
    try:
        return queue_file(cfg, raw, title, session, text)
    except AgentMediaError:
        return None


def remember(session: str, raw: object, title: str, text: str = "") -> str:
    real = Path(os.path.realpath(os.path.abspath(os.path.expanduser(str(raw)))))
    stamp = fingerprint(real)
    token = secrets.token_hex(5)
    with locked_map(state_dir() / STORE, keep=KEEP) as data:
        data[token] = {"session": session, "path": str(real), "title": title, "text": text,
                       "fingerprint": stamp, "at": int(time.time())}
    return token


def media_button(cfg, session: str, raw: object, title: str, text: str = "", lang: Optional[str] = None) -> Optional[dict]:
    from . import menus
    from .i18n import t
    if not enabled(cfg):
        return None
    try:
        check_file(cfg, raw)
        token = remember(session, raw, title, text)
    except (AgentMediaError, OSError):
        return None
    lang = lang or getattr(cfg, "ui_language", None)
    row = [{"text": t("btn_radio", lang=lang), "callback_data": "rq:" + token}]
    return {"inline_keyboard": [row, menus.unbuttons_row(lang, later=True)]}


def ask_owner(cfg, session: str, raw: object, title: str, bot=None) -> bool:
    from . import menus, mirror
    from .i18n import t
    lang = getattr(cfg, "ui_language", None)
    try:
        real = check_file(cfg, raw)
        token = remember(session, real, title)
    except (AgentMediaError, OSError):
        return False
    row = [{"text": t("btn_radio_yes", lang=lang), "callback_data": "rq:" + token}]
    fields = {"caption": t("radio_agent_card", lang=lang, title=title),
              "reply_markup": {"inline_keyboard": [row, menus.unbuttons_row(lang, later=True)]}}
    try:
        return mirror.send_essential_media(cfg, bot or mirror.Bot.from_config(cfg), session, fields, real) is not None
    except Exception:  # noqa: BLE001
        return False


def press(q: dict, cfg, bot, session: Optional[str], mine: bool) -> str:
    from .i18n import get_lang, t
    lang = getattr(cfg, "ui_language", None) or get_lang()
    def toast(key: str) -> None:
        try:
            bot.call("answerCallbackQuery", {"callback_query_id": q.get("id") or "", "text": t(key, lang=lang)})
        except Exception:  # noqa: BLE001
            pass
    data = q.get("data") or ""
    if not mine:
        toast("toast_radio_owner_only")
        return "ignored"
    rec = None
    if CALLBACK.fullmatch(data):
        with locked_map(state_dir() / STORE, keep=KEEP) as records:
            candidate = records.get(data[3:])
            if (isinstance(candidate, dict) and session and candidate.get("session") == session
                    and time.time() - float(candidate.get("at") or 0) <= TOKEN_SECONDS):
                rec = records.pop(data[3:])
            elif data[3:] in records:
                records.pop(data[3:])
    if not isinstance(rec, dict):
        toast("toast_radio_expired")
        return "ignored"
    def work() -> None:
        try:
            if fingerprint(Path(rec["path"])) != rec.get("fingerprint"):
                raise AgentMediaError("changed")
            text = str(rec.get("text") or "")
            queue_file(cfg, rec.get("path"), str(rec.get("title") or ""), session, text,
                       approved=not text.strip())
        except AgentMediaError as exc:
            toast({"off": "toast_radio_off", "unsuitable": "toast_radio_blocked", "full": "toast_radio_full",
                   "dup": "toast_radio_dup"}.get(exc.kind, "toast_radio_file"))
            return
        toast("toast_radio_queued")
        message = q.get("message") or {}
        if ((message.get("text") or message.get("caption") or "").startswith("🎧")
                and (message.get("chat") or {}).get("id") is not None):
            try:
                bot.call("editMessageReplyMarkup", {"chat_id": message["chat"]["id"],
                                                    "message_id": message.get("message_id"),
                                                    "reply_markup": {"inline_keyboard": []}})
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=work, daemon=True, name="radio-agent-queue").start()
    return "radio-queued"
