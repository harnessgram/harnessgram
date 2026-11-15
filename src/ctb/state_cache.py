"""One build shared between requests that arrive together, and its answer kept for a few seconds.

The panel's state takes a while to build on a busy machine, and the page, a second open tab and the radio all ask
for it at about the same time. `StateCache.get` lets the first caller of a key build it; callers of the same key that
arrive meanwhile wait for that build and get its answer (single flight). A finished answer is reused for `ttl`
seconds. `clear` forgets everything (a change made through the panel must show on the next read), and a build that
started before a clear is never stored.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Hashable, Optional, Tuple

TTL = 3.0


class _Flight:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.value = None
        self.error: Optional[BaseException] = None


class StateCache:
    def __init__(self, ttl: Optional[float] = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = TTL if ttl is None else ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._flights: Dict[Hashable, _Flight] = {}
        self._kept: Dict[Hashable, Tuple[float, object]] = {}
        self._generation = 0

    def clear(self) -> None:
        with self._lock:
            self._generation += 1
            self._kept.clear()
            self._flights.clear()  # a request after this builds afresh, never joins an older build

    def get(self, key: Hashable, build: Callable[[], object], keep: Callable[[object], bool] = lambda value: True):
        """The answer for `key`: a fresh kept one, the one being built by another thread, or `build()`.
        `keep(value)` says whether a finished answer may be reused later (it is shared with the callers waiting
        either way)."""
        with self._lock:
            kept = self._kept.get(key)
            if kept and self._clock() - kept[0] < self.ttl:
                return kept[1]
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = self._flights[key] = _Flight()
            generation = self._generation
        if not leader:
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            return flight.value
        try:
            flight.value = build()
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            with self._lock:
                if self._flights.get(key) is flight:
                    del self._flights[key]
                if flight.error is None and generation == self._generation and keep(flight.value):
                    self._kept[key] = (self._clock(), flight.value)
            flight.done.set()
        return flight.value
