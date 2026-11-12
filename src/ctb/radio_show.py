"""The radio's project show on the air: plays radio_board's slides as a moving picture for radio.Pipeline.

The pipeline asks `frame()` FPS times a second and gets JPEG bytes, and `strip()` for the news ticker that the
encoder scrolls along the bottom (radio.encoder_args crops a moving window out of it, so the ticker moves
smoothly without any drawing here). Between changes the picture is the same cached JPEG: no drawing at all.
A background thread (`run`) does the rest, off the stream's clock:
- rotation: the overview, then each project's wall of notes, then the owner's desk; each stays for its dwell
  time and the next one slides in (radio_paint.transition). Transitions are cached per picture pair;
- data: `update(projects)` hands over the latest radio_board.collect output; a slide is drawn again only when its
  content changed (its digest); the ticker strip only when its entries changed;
- motion units (radio_motion, prepared once): the station ident at the start and every IDENT_EVERY seconds, and the
  "landed" sting when something new landed, followed by that wall with the fresh note's check stamping on.
  Without prepared units the slides still move in.

Durations use a steady clock (`mono`); the wall clock only feeds what the slides show. Any drawing failure sets
`failed`; the station then goes back to its plain card (radio.py).
"""
from __future__ import annotations

import collections
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple

from . import radio_board, radio_motion, radio_paint

FPS = radio_motion.FPS          # the stream's frame rate while the show is on
CODEC = "mjpeg"
MOVE_FRAMES = 6                 # a slide moving in (0.6 s)
STAMP_FRAMES = 5                # a check stamping on
DWELL = {"overview": 12.0, "wall": 12.0, "waiting": 10.0}
IDENT_EVERY = 1800.0            # seconds between two station idents
CACHE = 24                      # transitions kept in memory
KEEP_PICTURES = 24              # slide pictures kept on disk (the oldest go first)
SEEN_FOR = 2 * 3600.0           # a landing is remembered this long after it was last listed (it lists for an hour)
TICKER_SPEED = 6                # pixels per frame (60 px a second at 10 fps)


# The only files a show writes, by name: clearing a folder removes these and nothing else.
OWN_FILES = re.compile(r"slide-[0-9a-f]{16}\.jpg|ticker-[01]\.jpg|unit-(?:ident|landed)-end\.jpg|"
                       r"\d{2}\.jpg|stamp-\d{2}\.jpg|\.paint-\d+\.(?:jpg|png)")
OWN_DIRS = ("move", "stamp")


def _plain_dir(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)


def safe_folder(root: Path, parts: List[str]) -> Optional[Path]:
    """root/parts..., each step a plain folder (created when missing, never through a link), or None. Only the
    parts below `root` (the bridge's own state folder) are checked and created."""
    path = Path(root)
    if not _plain_dir(path):
        return None
    for part in parts:
        path = path / part
        if not os.path.lexists(path):
            try:
                os.mkdir(path, 0o700)
            except OSError:
                return None
        if not _plain_dir(path):
            return None
    return path


def clear_own(folder: Path) -> None:
    """Remove the files a show writes (by their exact name pattern, regular files only) from `folder` and its
    two working subfolders. Nothing else is touched, and no link is followed."""
    for where in [folder] + [folder / d for d in OWN_DIRS]:
        if not _plain_dir(where):
            continue
        with os.scandir(where) as found:
            for entry in found:
                if OWN_FILES.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                    try:
                        os.unlink(entry.path)
                    except OSError:
                        pass


def _content(slide: dict) -> str:
    """A slide's digest without its clock: a new minute alone swaps the picture without moving it in."""
    return radio_board.digest({k: v for k, v in slide.items() if k not in ("clock", "key")})


class BoardShow:
    fps, codec = FPS, CODEC
    strip_size = (radio_paint.STRIP_W, radio_paint.TICK_H)
    strip_window = (radio_paint.TICK_W, radio_paint.TICK_Y)   # the ticker's visible width and its row
    strip_speed = TICKER_SPEED

    def __init__(self, folder: Path, bin_folder: Path, lang: str = "he", motion_dir: Optional[Path] = None,
                 clock: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic):
        """`clock`: the wall clock, only for what the slides show; `mono`: a steady clock for every duration, so
        a change of the system time never freezes or rushes the rotation."""
        self.dir = Path(folder)
        if not _plain_dir(self.dir):
            raise RuntimeError("the show folder is not a plain folder")
        clear_own(self.dir)  # an earlier show's pictures: nothing here uses them
        self.bin = bin_folder
        self.lang = lang
        self.clock, self.mono = clock, mono
        self.lock = threading.Lock()
        self.queue: Deque[bytes] = collections.deque()
        self.current = b""
        self.strips: Dict[Tuple[int, str], bytes] = {}   # (scroll cycle, entries key) -> that cycle's strip
        self.strip_last = b""                 # the newest strip handed out (used while the next one is drawn)
        self.strip_n = 0                      # strip frames handed to the encoder (its frame number n)
        self.entries_key = ""
        self.projects: List[dict] = []
        self.landed_seen: Dict[str, float] = {}   # landing key -> when it was last listed (steady clock)
        self.seen_any = False
        self.sting_due = False
        self.slides: List[dict] = []
        self.index = -1
        self.until = 0.0
        self.on_air: Optional[dict] = None        # the slide on screen
        self.images: Dict[str, Path] = {}         # slide key -> its drawn picture
        self.moves: "collections.OrderedDict[Tuple[str, str], List[bytes]]" = collections.OrderedDict()
        self.units = radio_motion.load(motion_dir, FPS)
        self.unit_ends: Dict[str, Path] = {}      # each unit's last frame: the next slide moves in over it
        for name, frames in self.units.items():
            end = self.dir / f"unit-{name}-end.jpg"
            end.write_bytes(frames[-1])
            self.unit_ends[name] = end
        self.ident_at: Optional[float] = None
        self.failed = ""
        self.drawn = 0                            # slides drawn (for the tests and the status)

    # --- the pipeline's side (FPS times a second) -------------------------------------------------------
    def frame(self) -> bytes:
        with self.lock:
            if self.queue:
                return self.queue.popleft()
            return self.current

    def cycle(self) -> int:
        """The encoder's scroll cycle: its window wraps every HALF pixels. A plain counter of frames handed out,
        never reset by a change of content, so the windows keep walking through the whole list."""
        return (self.strip_n * TICKER_SPEED) // radio_paint.HALF

    def strip(self) -> bytes:
        """The ticker strip for the encoder's next frame: the strip of the current cycle (window k flowing into
        window k+1), drawn ahead by the show's thread. Never waits: if that one is not ready, the last one."""
        with self.lock:
            data = self.strips.get((self.cycle(), self.entries_key))
            if data is None and not self.strip_last:
                return b""
            self.strip_n += 1
            if data is not None:
                self.strip_last = data
            return self.strip_last

    # --- data ---------------------------------------------------------------------------------------------
    def update(self, projects: List[dict]) -> None:
        keys = set(radio_board.landed_keys(projects))
        now = self.mono()
        with self.lock:
            if self.seen_any and keys - set(self.landed_seen):
                self.sting_due = True
            self.seen_any = True
            for k in keys:
                self.landed_seen[k] = now
            for k in [k for k, at in self.landed_seen.items() if now - at > SEEN_FOR]:
                del self.landed_seen[k]
            self.projects = projects

    # --- drawing ------------------------------------------------------------------------------------------
    def _paint(self, frames: List[radio_paint.Frame], what: str) -> None:
        if not radio_paint.paint(self.bin, frames) or not all(Path(f.out).exists() for f in frames):
            raise RuntimeError(f"the painter did not draw the {what}")

    def _draw(self, slide: dict) -> Path:
        path = self.images.get(slide["key"])
        if path is not None and path.exists():
            return path
        path = self.dir / f"slide-{slide['key']}.jpg"
        self._paint([radio_paint.layout(slide, str(path))], "slide")
        self.drawn += 1
        self.images[slide["key"]] = path
        self._prune(keep={path} | ({self.images.get(self.on_air["key"])} if self.on_air else set()))
        return path

    def _prune(self, keep: set) -> None:
        """At most KEEP_PICTURES slide pictures in the folder, counted on disk (the oldest go first)."""
        files = sorted(self.dir.glob("slide-*.jpg"), key=lambda f: f.stat().st_mtime if f.exists() else 0)
        for old in files[:max(0, len(files) - KEEP_PICTURES)]:
            if old in keep:
                continue
            try:
                old.unlink()
            except OSError:
                pass
            for key in [k for k, v in self.images.items() if v == old]:
                del self.images[key]

    def _ticker(self, wall: float) -> None:
        """Keep the strips of the current and the next scroll cycle drawn for the current entries. A change of
        content redraws them; the cycle counter goes on (no window is skipped or repeated because of it)."""
        with self.lock:
            projects = self.projects
            now_cycle = self.cycle()
        entries = radio_board.ticker(projects, wall, self.lang)
        key = radio_board.digest({"t": entries})
        want = [(c, key) for c in (now_cycle, now_cycle + 1)]
        missing = [w for w in want if w not in self.strips]
        if not missing:
            return
        frames = [radio_paint.ticker_strip(entries, str(self.dir / f"ticker-{c % 2}.jpg"), self.lang, c)
                  for c, _ in missing]
        self._paint(frames, "ticker")
        data = {w: Path(f.out).read_bytes() for w, f in zip(missing, frames)}
        with self.lock:
            self.strips = {w: b for w, b in {**self.strips, **data}.items() if w in want}
            self.entries_key = key

    def _move(self, old: Path, new: Path) -> List[bytes]:
        pair = (old.name, new.name)
        if pair in self.moves:
            self.moves.move_to_end(pair)
            return self.moves[pair]
        work = self._work("move")
        frames = radio_paint.transition(str(old), str(new), work, MOVE_FRAMES)
        self._paint(frames, "transition")
        data = [Path(f.out).read_bytes() for f in frames]
        self.moves[pair] = data
        while len(self.moves) > CACHE:
            self.moves.popitem(last=False)
        return data

    def _stamp(self, slide: dict) -> List[bytes]:
        work = self._work("stamp")
        frames = radio_paint.stamp_frames(slide, work, STAMP_FRAMES)
        self._paint(frames, "stamp")
        return [Path(f.out).read_bytes() for f in frames]

    def _work(self, name: str) -> Path:
        """A working subfolder of this show's folder: a plain folder, emptied of this show's own files."""
        work = safe_folder(self.dir, [name])
        if work is None:
            raise RuntimeError("the show's working folder is not a plain folder")
        clear_own(work)
        return work

    def _show(self, slide: dict, move: bool, before: List[bytes] = (), after: str = "", stamp: bool = False) -> None:
        """Put `slide` on screen: optional unit frames first, then the slide moving in over what was on screen
        (or over the last frame of the unit `after`), then, with `stamp`, the fresh note's check stamping on."""
        new = self._draw(slide)
        frames = list(before)
        old = self.unit_ends.get(after) or (self.images.get(self.on_air["key"]) if self.on_air else None)
        if move and old is not None and old.exists() and old != new:
            frames += self._move(old, new)
        if stamp and slide["kind"] == "wall" and slide.get("fresh"):
            frames += self._stamp(slide)
        picture = new.read_bytes()
        with self.lock:
            self.queue.extend(frames)
            self.current = picture
        self.on_air = slide

    # --- the rotation (the show's own thread) -------------------------------------------------------------
    def _next_slides(self, wall: float) -> List[dict]:
        with self.lock:
            projects = self.projects
        return radio_board.slides(projects, wall, 0, self.lang)

    def step(self, now: Optional[float] = None) -> None:
        """One look: start, advance the rotation when the dwell is over, or refresh the slide on screen."""
        wall = self.clock()
        now = self.mono() if now is None else now
        self._ticker(wall)
        with self.lock:
            busy = bool(self.queue)
            sting, self.sting_due = self.sting_due, False
        if busy:
            with self.lock:
                self.sting_due = self.sting_due or sting
            return
        ident, after = [], ""
        if "ident" in self.units and (self.ident_at is None or now - self.ident_at >= IDENT_EVERY):
            ident, self.ident_at, after = self.units["ident"], now, "ident"
        if sting or now >= self.until or self.on_air is None:
            self.slides = self._next_slides(wall)
            if sting:  # what just landed: the sting, then its wall with the fresh check stamping on
                target = next((s for s in self.slides if s["kind"] == "wall" and s.get("fresh")), None)
                if target is not None:
                    self.index = self.slides.index(target)
                    units = self.units.get("landed", [])
                    self._show(target, True, ident + units, "landed" if units else after, stamp=True)
                    self.until = now + DWELL["wall"] + (len(ident) + len(units) + MOVE_FRAMES + STAMP_FRAMES) / FPS
                    return
            self.index = (self.index + 1) % len(self.slides)
            slide = self.slides[self.index]
            self._show(slide, True, ident, after)
            self.until = now + DWELL.get(slide["kind"], 10.0) + (len(ident) + MOVE_FRAMES) / FPS
            return
        # the slide on screen: drawn again only when what it shows changed
        fresh = [s for s in self._next_slides(wall) if s["kind"] == self.on_air["kind"]
                 and s.get("page") == self.on_air.get("page") and s.get("name") == self.on_air.get("name")]
        if fresh and fresh[0]["key"] != self.on_air["key"]:
            self._show(fresh[0], _content(fresh[0]) != _content(self.on_air))

    def run(self, quit: threading.Event, every: float = 0.25) -> None:
        while not quit.is_set():
            try:
                self.step()
            except Exception as e:  # noqa: BLE001 - the station falls back to its card
                self.failed = e.__class__.__name__
                return
            quit.wait(every)
