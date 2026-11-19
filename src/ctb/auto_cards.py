"""Deterministic local progress inference, with bounded offline title translation."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
import hashlib
import json
import re
import threading
import time
from typing import Callable, Iterable, Optional

from . import cards
from .config import set_setting, setting_on

WATCH_EVERY = 30.0
MODEL_RETRY_AFTER = 600.0  # a failed helper-agent title call is not repeated sooner than this
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


def english_steps(snapshot: dict) -> list:
    """The remaining task subjects of a snapshot that are not Hebrew: what the model-made names would replace."""
    _valid, remaining, body = cards._todo_body(snapshot["tasks"])
    return [s for s in body["steps"] if remaining and isinstance(s, str) and not _HEBREW.search(s)]


def fallback(row: dict, snapshot: dict, title: Optional[str] = None, step_names: Optional[dict] = None) -> dict:
    """Task status is evidence; tool-only progress is explicitly an estimate, never completion. `step_names`
    maps a task subject to the Hebrew name a helper agent made for it; a subject without one is grouped as before."""
    title = title or hebrew_line(row.get("title"), title_fallback(row.get("title")), cards.TITLE_CHARS)
    _valid, remaining, body = cards._todo_body(snapshot["tasks"])
    if remaining:
        names = step_names or {}
        steps = [hebrew_line(names.get(s, s), _group(s)) for s in body["steps"]]
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
        self.model_names: OrderedDict = OrderedDict()  # (title, steps) -> (when, names or None)

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

    def _model_names(self, title: str, steps: list, cfg) -> Optional[dict]:
        """Hebrew names from the card-titles helper agent, cached by content. A failure is remembered for
        MODEL_RETRY_AFTER seconds, so a broken helper is not asked again on every scan."""
        key = (title, tuple(steps))
        now = time.monotonic()
        with self.lock:
            hit = self.model_names.get(key)
            if hit is not None and (hit[1] is not None or 0 <= now - hit[0] < MODEL_RETRY_AFTER):
                return hit[1]
        try:
            from .helper_jobs import card_names
            names = card_names(cfg, title, steps)
        except Exception:
            names = None
        with self.lock:
            self.model_names[key] = (now, names)
            while len(self.model_names) > 128:
                self.model_names.popitem(last=False)
        return names

    def sync(self, rows: Iterable[dict], now: float, initial_only: bool = False, cfg=None) -> None:
        from .auto_progress import activity_snapshot
        from .helper_agents import enabled as helper_on
        model_on = helper_on(cfg, "card_titles")
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
                        sources = english_steps(snapshot) if model_on else []
                        names = {k: v for k, v in (state.get("step_names") or {}).items() if k in sources}
                        state.update(fingerprint=fingerprint, snapshot=snapshot, source_title=row.get("title"),
                                     summary=fallback(row, snapshot, title, names), step_names=names,
                                     step_sources=sources, steps_ready=all(k in names for k in sources),
                                     title_ready=not needs_translation or title is not None)
                    state["row"] = dict(row)
                    rec = cards.sync_auto(session, row["chat"], row["thread"], row["title"], [],
                                          bg=row.get("bg", 0), now=now, summary=state["summary"], refresh=changed,
                                          activity_fingerprint=fingerprint)
                    if (rec is not None and not initial_only and isinstance(row.get("title"), str)
                            and (not state.get("title_ready", False)
                                 or (model_on and not state.get("steps_ready", True)))):
                        pending.append((session, row["title"], fingerprint, rec["id"], rec["updated"]))
                except Exception:
                    continue  # optional progress must never hold up the bridge

        # Model work can take the full translation budget. Never hold the watcher lock while it runs:
        # Cockpit state refreshes use this same watcher through initial_only=True.
        for session, title, fingerprint, expected_id, expected_updated in pending:
            with self.lock:
                held = self.sessions.get(session) or {}
                sources = list(held.get("step_sources") or [])
                title_ready, steps_ready = held.get("title_ready", False), held.get("steps_ready", True)
            names = None
            if model_on and (not title_ready or not steps_ready):
                names = self._model_names(title, sources, cfg)
            translated = (names or {}).get("title")
            if translated is None and not title_ready:
                translated = self._cached_translation(title, cfg)  # the offline engine stays the fallback
            if translated is None and not names:
                continue
            with self.lock:
                state = self.sessions.get(session)
                if (state is None or state.get("fingerprint") != fingerprint
                        or state.get("source_title") != title):
                    continue
                row, snapshot = state["row"], state["snapshot"]
                step_names = dict(state.get("step_names") or {})
                step_names.update((names or {}).get("steps") or {})
                kept = translated or state.get("translated_title")
                summary = fallback(row, snapshot, kept, step_names)
                rec = cards.sync_auto(session, row["chat"], row["thread"], row["title"], [],
                                      bg=row.get("bg", 0), now=now, summary=summary, refresh=True,
                                      expected_id=expected_id, expected_updated=expected_updated,
                                      activity_fingerprint=fingerprint)
                if rec is not None:
                    state.update(summary=summary, translated_title=kept, step_names=step_names,
                                 title_ready=state.get("title_ready", False) or translated is not None,
                                 steps_ready=names is not None or state.get("steps_ready", True))


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
        from .status import transcript_round
        try:
            with transcript_round():
                _scan(cfg, now)
        except Exception:
            pass
        finally:
            _scan_guard.release()

    try:
        threading.Thread(target=scan, name="auto-cards-scan", daemon=True).start()
    except Exception:
        _scan_guard.release()


def _scan(cfg, now: float) -> None:
    """One auto-cards scan (in its own thread): the sessions, their transcripts and background agents."""
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
