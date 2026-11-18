"""Deterministic local progress inference, with bounded offline title translation."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
import hashlib
import json
import re
import threading
from typing import Callable, Iterable, Optional

from . import cards
from .config import set_setting, setting_on

WATCH_EVERY = 30.0
TITLE_TRANSLATION_TIMEOUT = 15.0  # the existing translate.TRANSLATE_BUDGET
_HEBREW = re.compile(r"[א-ת]")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]*")
_CONTROLS = re.compile(r"[\x00-\x1f\x7f\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def enabled() -> bool:
    return not setting_on("auto_cards_off")


def set_enabled(on: bool) -> None:
    set_setting("auto_cards_off", not on)


def hebrew_line(value, fallback: str, limit: int = cards.STEP_CHARS) -> str:
    """Hebrew text, with at most two short isolated product names; never an English sentence."""
    if not isinstance(value, str) or len(value) > 512:
        return fallback
    text = cards.scrub_subject(_CONTROLS.sub("", value))
    text = re.sub(r"\[(?:path|link|email|redacted)\]", "פרט מוסתר", text)
    names = _LATIN.findall(text)
    if not _HEBREW.search(text) or len(names) > 2 or any(len(n) > 18 for n in names):
        return fallback
    # Keep the auto-generated wording suitable for a family display.
    if any(w in text.casefold() for w in ("fuck", "shit", "זין", "כוס", "חרא")):
        return fallback
    text = _LATIN.sub(lambda m: "\u2068" + m.group() + "\u2069", text)
    return text if len(text) <= limit else fallback  # never cut an isolation pair in half


def title_fallback(value) -> str:
    """A short Hebrew title retaining a likely product/project name."""
    if not isinstance(value, str):
        return "התקדמות העבודה"
    scrubbed = cards.scrub_subject(_CONTROLS.sub("", value))
    scrubbed = re.sub(r"\[(?:path|link|email|redacted)\]", "", scrubbed, flags=re.IGNORECASE)
    names = [n for n in _LATIN.findall(scrubbed) if len(n) <= 30]
    if not names:
        return "התקדמות העבודה"
    common = {"add", "build", "check", "create", "edit", "email", "fix", "implement", "improve",
              "link", "make", "path", "plan", "project", "remove", "run", "test", "update", "work"}
    name = next((n for n in names if n.casefold() not in common), names[0])
    out = "עבודה על \u2068" + name + "\u2069"
    return out if len(out) <= cards.TITLE_CHARS else "התקדמות העבודה"


_translation_lock = threading.Lock()


def _translated_title(value, cfg, translate_fn: Optional[Callable] = None,
                      late_result: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """Return a valid translation from an offline engine, or None when one was not produced."""
    if not isinstance(value, str) or _HEBREW.search(value) or cfg is None:
        return None
    # NLLB only: it loads from a local folder and never contacts the network. MLX may reach the
    # Hugging Face Hub while loading, which a background title must never do.
    local = [engine for engine in getattr(cfg, "engines", []) if engine == "nllb"]
    if not local:
        return None
    local_cfg = replace(cfg, engines=local, source_language="en", target_language="he")
    box = {}

    def work():
        if not _translation_lock.acquire(blocking=False):
            return
        try:
            if translate_fn is None:
                from .translate import translate_text
                text = translate_text(value, local_cfg, fast=True)
            else:
                text = translate_fn(value, local_cfg, fast=True)
            translated = hebrew_line(text, "", cards.TITLE_CHARS) or None
            box["text"] = translated
            if translated is not None and late_result is not None:
                late_result(translated)
        except Exception:
            pass
        finally:
            _translation_lock.release()

    worker = threading.Thread(target=work, name="auto-card-title-translate", daemon=True)
    worker.start()
    worker.join(TITLE_TRANSLATION_TIMEOUT)
    return box.get("text")


def translated_title(value, cfg, translate_fn: Optional[Callable] = None) -> str:
    """Use only configured offline engines and return by the normal translation deadline."""
    fallback_title = title_fallback(value)
    if not isinstance(value, str) or _HEBREW.search(value):
        return hebrew_line(value, fallback_title, cards.TITLE_CHARS)
    return _translated_title(value, cfg, translate_fn) or fallback_title


def _group(text: str) -> str:
    text = text.casefold()
    if text.isdecimal() and len(text) <= 6:
        return "שלב " + text
    for words, label in (
        (("test", "pytest", "check", "בדיק", "בדוק"), "בדיקת התוצאה"),
        (("doc", "readme", "תיעוד"), "עדכון ההסבר"),
        (("edit", "write", "patch", "fix", "implement", "תיקון", "שינוי", "כתיב"), "הכנת השינוי"),
        (("read", "search", "inspect", "glob", "grep", "find", "סקיר", "קריא", "חיפוש"), "סקירת העבודה"),
        (("task", "todo", "plan", "תכנון"), "סידור השלבים"),
    ):
        if any(w in text for w in words):
            return label
    return "המשך העבודה"


def fallback(row: dict, snapshot: dict, title: Optional[str] = None) -> dict:
    """Task status is evidence; tool-only progress is explicitly an estimate, never completion."""
    title = title or hebrew_line(row.get("title"), title_fallback(row.get("title")), cards.TITLE_CHARS)
    _valid, remaining, body = cards._todo_body(snapshot["tasks"])
    if remaining:
        steps = [hebrew_line(s, _group(s)) for s in body["steps"]]
        at, percent = body["at"], body["percent"]
    else:
        groups = []
        for activity in snapshot["activities"]:
            label = _group(activity["text"])
            if label in groups:
                groups.remove(label)
            groups.append(label)
        steps = ["הכנת העבודה"] + groups[-4:]
        at = len(steps) - 1 if groups else 0
        percent = min(90, round(100 * at / max(3, len(steps) + 1)))
    for label in ("בדיקת התוצאה", "סיכום העבודה", "המשך העבודה"):
        if len(steps) >= 3:
            break
        if label not in steps:
            steps.append(label)
    return {"title": title, "steps": steps[:cards.MAX_STEPS], "at": at, "percent": percent}


def validate_summary(raw) -> Optional[dict]:
    """Validate the deterministic summary before it enters shared card storage."""
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list) or not 3 <= len(raw["steps"]) <= 6:
        return None
    title = hebrew_line(raw.get("title"), "", cards.TITLE_CHARS)
    steps = [hebrew_line(step, "") for step in raw["steps"]]
    at, percent = raw.get("at"), raw.get("percent")
    if (not title or not all(steps) or type(at) is not int or not 0 <= at < len(steps)
            or type(percent) is not int or not 0 <= percent <= 99):
        return None
    return {"title": title, "steps": steps, "at": at, "percent": percent}



class Watcher:
    """Publish deterministic cards. Storage guards ensure agent cards win and closed cards stay closed."""

    def __init__(self, translate_fn: Optional[Callable] = None):
        self.translate_fn = translate_fn
        self.lock = threading.RLock()
        self.sessions: dict = {}
        self.translations: OrderedDict[str, str] = OrderedDict()

    def _cached_translation(self, title: str, cfg) -> Optional[str]:
        with self.lock:
            cached = self.translations.get(title)
            if cached is not None:
                self.translations.move_to_end(title)
                return cached
        def cache(translated: str) -> None:
            with self.lock:
                self.translations[title] = translated
                self.translations.move_to_end(title)
                while len(self.translations) > 128:
                    self.translations.popitem(last=False)

        translated = _translated_title(title, cfg, self.translate_fn, cache)
        return translated

    def sync(self, rows: Iterable[dict], now: float, initial_only: bool = False, cfg=None) -> None:
        from .auto_progress import activity_snapshot
        pending = []
        with self.lock:
            rows = list(rows)
            on = enabled()
            live = {r["session"] for r in rows if on and (r["state"] == "working"
                    or (r["state"] == "idle" and r.get("bg", 0) > 0))}
            quiet = {r["session"] for r in rows} - live
            quiet.update(set(self.sessions) - live)
            quiet.update(c["session"] for c in cards.open_cards() if c.get("automatic") and c["session"] not in live)
            cards.close_auto(quiet, now)
            for session in quiet:
                self.sessions.pop(session, None)
            for row in rows:
                session = row["session"]
                if session not in live:
                    continue
                try:
                    if initial_only and any(c["session"] == session and c.get("automatic") for c in cards.open_cards()):
                        continue  # HTTP refresh cannot replace a heartbeat update
                    snapshot = activity_snapshot(row.get("path"))
                    fingerprint = hashlib.sha256(json.dumps(
                        [row.get("path"), row.get("title"), snapshot], sort_keys=True).encode()).hexdigest()
                    existing = next((c for c in cards.open_cards()
                                     if c["session"] == session and c.get("automatic")), None)
                    initial = {}
                    if existing and existing.get("activity_fingerprint") == fingerprint:
                        initial.update(fingerprint=fingerprint, summary={
                            "title": existing["title"], "steps": existing["steps"],
                            "at": existing["at"], "percent": existing["percent"]}, title_ready=False)
                    state = self.sessions.setdefault(session, initial)
                    changed = state.get("fingerprint") != fingerprint
                    if changed:
                        needs_translation = isinstance(row.get("title"), str) and not _HEBREW.search(row["title"])
                        same_title = state.get("source_title") == row.get("title")
                        title = state.get("translated_title") if same_title else None
                        state.update(fingerprint=fingerprint, snapshot=snapshot, source_title=row.get("title"),
                                     summary=fallback(row, snapshot, title),
                                     title_ready=not needs_translation or title is not None)
                    state["row"] = dict(row)
                    rec = cards.sync_auto(session, row["chat"], row["thread"], row["title"], [],
                                          bg=row.get("bg", 0), now=now, summary=state["summary"], refresh=changed,
                                          activity_fingerprint=fingerprint)
                    if (rec is not None and not initial_only and not state.get("title_ready", False)
                            and isinstance(row.get("title"), str)):
                        pending.append((session, row["title"], fingerprint, rec["id"], rec["updated"]))
                except Exception:
                    continue  # optional progress must never hold up the bridge

        # Model work can take the full translation budget. Never hold the watcher lock while it runs:
        # Cockpit state refreshes use this same watcher through initial_only=True.
        for session, title, fingerprint, expected_id, expected_updated in pending:
            translated = self._cached_translation(title, cfg)
            if translated is None:
                continue
            with self.lock:
                state = self.sessions.get(session)
                if (state is None or state.get("fingerprint") != fingerprint
                        or state.get("source_title") != title):
                    continue
                row, snapshot = state["row"], state["snapshot"]
                summary = fallback(row, snapshot, translated)
                rec = cards.sync_auto(session, row["chat"], row["thread"], row["title"], [],
                                      bg=row.get("bg", 0), now=now, summary=summary, refresh=True,
                                      expected_id=expected_id, expected_updated=expected_updated,
                                      activity_fingerprint=fingerprint)
                if rec is not None:
                    state.update(summary=summary, translated_title=translated, title_ready=True)


watcher = Watcher()
_scan_guard = threading.Lock()
_scan_at: Optional[float] = None


def start(cfg, now: float) -> None:
    """Schedule a bounded heartbeat scan; never scan transcripts on the heartbeat caller."""
    global _scan_at
    if _scan_at is not None and 0 <= now - _scan_at < WATCH_EVERY:
        return
    if not _scan_guard.acquire(blocking=False):
        return
    _scan_at = now

    def scan():
        try:
            if not enabled():
                watcher.sync([], now)
                return
            from .config import home_chat, state_dir
            from .sessions_menu import collect
            from .topics import TopicMap
            from .status import transcript_of
            from .auto_progress import cached, forget
            from .usage import background_of
            entries = collect(TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg), now=now)
            rows = []
            for entry in entries:
                path = transcript_of(entry.session)
                bg = cached(path, "auto-cards-bg", lambda p: len(background_of(p, now=now)), 0,
                            now=now, max_age=600) if entry.state != "ended" else 0
                rows.append({"session": entry.session, "chat": home_chat(cfg) if entry.chat is None else entry.chat,
                             "thread": entry.thread, "title": entry.name or "התקדמות העבודה",
                             "state": entry.state, "path": path, "bg": bg})
            watcher.sync(rows, now, cfg=cfg)
            forget(r["path"] for r in rows if r["path"])
        except Exception:
            pass
        finally:
            _scan_guard.release()

    try:
        threading.Thread(target=scan, name="auto-cards-scan", daemon=True).start()
    except Exception:
        _scan_guard.release()
