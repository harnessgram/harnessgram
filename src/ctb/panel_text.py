"""Hebrew for the control panel: what agents wrote in English (decision questions, board items, session names)
shown in Hebrew, without ever making the page wait.

`Translator.he(text)` answers at once from memory or from translate.py's disk cache (keyed by a hash of the
text, so each text is translated once for the bot and the panel together). A text that is not cached yet is
queued for a small pool of background threads that run the normal translation chain (Gemini first on the
owner's machine) and fill the cache; the page shows the original meanwhile and asks again a moment later.

Only engines that are cheap to run inside the panel's process are used: never the local MLX model (it loads
gigabytes), NLLB only when its model folder is set, and the network engines only when the owner allowed
external translation (translate.external_allowed). With none left the translator is off and the panel shows
the originals, as before.
"""
from __future__ import annotations

import collections
import dataclasses
import hashlib
import re
import threading
import time
from typing import Callable, Dict, Optional

from . import lang as L
from . import translate
from .config import Config

WORKERS = 2
MEMO = 600  # translations kept in memory (also covers translate_cache off)
QUEUE = 200  # texts waiting at most; more are asked again on a later refresh
RETRY_AFTER = 600  # seconds before a text whose translation failed is tried again
MAX_CHARS = 1600  # a decision detail is at most 1500


_WORD = re.compile(r"\b[A-Za-z]{2,}\b")


def _key(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def panel_config(cfg: Config) -> Optional[Config]:
    """The config the panel translates with (target Hebrew, light engines only), or None when it cannot."""
    if getattr(cfg, "ui_language", "") != "he":
        return None
    engines = [e for e in getattr(cfg, "engines", []) if e != "mlx"]
    if not getattr(cfg, "nllb_model_dir", ""):
        engines = [e for e in engines if e != "nllb"]
    if not translate.external_allowed(cfg):
        engines = [e for e in engines if e not in translate.EXTERNAL_ENGINES]
    if not engines:
        return None
    try:
        return dataclasses.replace(cfg, target_language="he", engines=engines)
    except Exception:
        return None


class Translator:
    """he(text): the Hebrew text, "" when no translation is needed (already Hebrew, or nothing to translate),
    or None while it is being made. `translate_fn` replaces the engine chain (tests)."""

    def __init__(self, cfg: Config, translate_fn: Optional[Callable[[str], str]] = None):
        self.cfg = panel_config(cfg)
        self.enabled = self.cfg is not None or translate_fn is not None
        self._fn = translate_fn
        self._memo: "collections.OrderedDict[str, str]" = collections.OrderedDict()
        self._failed: Dict[str, float] = {}
        self._queue: "collections.deque[str]" = collections.deque()
        self._queued: set = set()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._threads = 0

    # --- reading -------------------------------------------------------------------------------

    def he(self, text) -> Optional[str]:
        if not self.enabled or not isinstance(text, str) or not text.strip() or len(text) > MAX_CHARS:
            return ""
        if not L.needs_translation(text, "he") or not _WORD.search(text):
            return ""  # already Hebrew, or only ids and numbers (a commit hash is not English)
        key = _key(text)
        with self._lock:
            if key in self._memo:
                self._memo.move_to_end(key)
                return self._memo[key]
            failed = self._failed.get(key)
            if failed is not None and time.monotonic() - failed < RETRY_AFTER:
                return ""
        hit = None
        if self.cfg is not None and self._fn is None:
            try:
                hit = translate.cached(text, self.cfg)
            except Exception:
                hit = None
        if hit:
            self._remember(key, hit)
            return hit
        self._enqueue(key, text)
        return None

    # --- filling -------------------------------------------------------------------------------

    def _remember(self, key: str, value: str) -> None:
        with self._lock:
            self._memo[key] = value
            self._memo.move_to_end(key)
            while len(self._memo) > MEMO:
                self._memo.popitem(last=False)

    def _enqueue(self, key: str, text: str) -> None:
        with self._lock:
            if key in self._queued or len(self._queue) >= QUEUE:
                return
            self._queued.add(key)
            self._queue.append(text)
            if self._threads < WORKERS:
                self._threads += 1
                threading.Thread(target=self._work, name="panel-translate", daemon=True).start()
            self._wake.notify()

    def _translate(self, text: str) -> str:
        if self._fn is not None:
            return self._fn(text)
        return translate.translate_text(text, self.cfg)

    def _work(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._wake.wait(timeout=60)
                if not self._queue:
                    self._threads -= 1
                    return
                text = self._queue.popleft()
            key = _key(text)
            try:
                out = (self._translate(text) or "").strip()
            except Exception:
                out = ""
            if out and out != text.strip():
                self._remember(key, out)
            else:
                with self._lock:
                    self._failed[key] = time.monotonic()
                    if len(self._failed) > MEMO:
                        self._failed.pop(next(iter(self._failed)))
            with self._lock:
                self._queued.discard(key)

    def pending(self) -> int:
        with self._lock:
            return len(self._queued)
