"""Gap-filler controller for the isolated private mixer; no network work on the audio clock."""
from __future__ import annotations

import threading
import time

from . import briefing, radio_music, radio_now, radio_podcast as P
from .config import read_map


class Filler:
    FEED_INTERVAL = 300

    def __init__(self, cfg, mixer, player, *, make=None, clock=time.time, quiet=None):
        self.cfg, self.mixer, self.player = cfg, mixer, player
        self.make, self.clock = make or P.Feed, clock
        self.quiet = quiet or briefing.rest_day
        self.feed = None
        self.selection = None
        self.music_item = None
        self.worker = None
        self.done = threading.Event()
        self.closed = threading.Event()
        self.poll_at = 0.0
        self.music_at = 0.0
        self.mixer.background_allowed = self.allowed

    def allowed(self):
        try:
            from . import radio_programs
            block = radio_programs.current_block(self.clock())
            programme_allows = block is None or block["kind"] == "music"
            if block is not None and block["kind"] == "news":
                channels = radio_programs.channels_for(block, self.cfg.news_edition_channels)
                programme_allows = not self.cfg.news_edition_enabled or not channels
            return (programme_allows and radio_music.enabled() and not self.quiet(self.cfg, self.clock())
                    and not self.closed.is_set())
        except Exception:
            return False

    def _select(self):
        """Fetch only the owner's feeds using the bounded, HTTPS/SSRF-safe existing reader."""
        candidates = []
        try:
            seen = read_map(P.folder() / "played.json")
            for feed in P.feeds(self.cfg):
                if self.closed.is_set():
                    return
                try:
                    candidates.extend(P.episodes(self.cfg, feed))
                except P.PodcastError:
                    continue
            for item in sorted(candidates, key=lambda e: -e.get("published", 0)):
                if self.closed.is_set():
                    return
                if item["id"] in seen:
                    continue
                try:
                    prepared = P.prepare_episode(self.cfg, item)
                except P.PodcastError:
                    continue
                self.selection = prepared
                break
        finally:
            self.done.set()

    def step(self):
        now = self.clock()
        if self.feed is not None and self.music_item is not None and radio_music.skip_requested(self.music_item["id"]):
            self.close_music()
        # A queued episode is downloaded before its decoder is made.  Release
        # the music decoder while that happens so the shared Feed path never
        # has a music and podcast decoder alive at the same time.
        if self.feed is not None and self.player.cur:
            self.close_music()
        # Reuse the briefing's Shabbat and configured quiet-window rules; uncertainty means silence.
        allowed = self.allowed()
        with self.mixer.lock:
            self.mixer.background_enabled = allowed
        if not allowed:
            self.close_music()
            return False
        if self.feed is not None and self.mixer.background is not self.feed:
            self.close_music()
        # No bed selection or episode starts while a broadcast is waiting or playing.
        if self.mixer.pending():
            return True
        if self.worker is not None and self.done.is_set():
            self.worker.join()
            self.worker = None
            if self.selection is not None and not P._read()["items"]:
                try:
                    P._queue_episode(self.selection)  # prepared and checked on the feed worker, never here
                except P.PodcastError:
                    pass
            self.selection = None
        if self.worker is None and now >= self.poll_at and P.feeds(self.cfg):
            self.poll_at = now + self.FEED_INTERVAL
            self.done.clear()
            self.worker = threading.Thread(target=self._select, daemon=True, name="radio-gap-feeds")
            self.worker.start()
        if not self.player.cur and not P._read()["items"] and self.feed is None and now >= self.music_at:
            self.music_at = now + 5  # empty libraries and decode failures never spin
            item = radio_music.pick(now, cfg=self.cfg)
            if item is not None:
                try:
                    feed = self.make(str(radio_music.folder() / (item["id"] + ".audio")))
                except Exception:
                    return True
                # The decoder may take long enough for the checker or its
                # rules to change.  Revalidate the stamped verdict at the last
                # possible point, then destroy a decoder that lost approval.
                if not radio_music.approved_for_attach(item, self.cfg):
                    feed.stop()
                    return True
                self.feed = feed
                with self.mixer.lock:
                    self.mixer.background = self.feed
                self.music_item = item
                radio_music.playback(item)
                radio_now.publish(radio_music.folder().parent, " · ".join(
                    part for part in (item.get("title", ""), item.get("artist", "")) if part))
        elif self.music_item is not None and not self.mixer.pending():
            title = " · ".join(part for part in (self.music_item.get("title", ""),
                                                  self.music_item.get("artist", "")) if part)
            if radio_now.read(radio_music.folder().parent).get("title") != title:
                radio_now.publish(radio_music.folder().parent, title)
        return True

    def close_music(self):
        with self.mixer.lock:
            self.mixer.background = None
        if self.feed is not None:
            self.feed.stop()
            self.feed = None
        self.music_item = None
        radio_music.playback(False)

    def close(self):
        self.closed.set()
        with self.mixer.lock:
            self.mixer.background_enabled = False
        self.close_music()
        # Feed reads have an existing hard deadline. They only return metadata and cannot attach audio.
