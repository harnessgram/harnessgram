"""The radio's voice: text to 48 kHz mono 16-bit PCM.

The default route is the configured offline engine, with the system voice as its fallback. Cloud mode is an
explicit opt-in. Its first route is Gemini TTS on Vertex (the config's gemini_vertex_project and token command, the same
credentials the translator uses): billed speech, so every clip is cached on disk by its exact text, model, voice
and style, and a daily cap (radio.tts_daily_minutes) stops it for the rest of the day.

The local cap is defensive: before each request a conservative amount of speech is reserved and the request is sent
only when the reservation fits. The request first asks the model for no more audio than that (maxOutputTokens); some
models refuse that field, and then the length is held by the text instead: speech seconds are estimated from the
characters (SECONDS_PER_CHAR, deliberately slow), text longer than one request may carry is split into several short
requests, and text that does not fit what is left of the day goes to the free voice. Afterwards
the reservation is settled to what was actually billed (the response's audio token count, or the audio length,
whichever is more); a request whose outcome is unknown (a timeout, a broken response) keeps its whole
reservation and blocks further paid requests that day. Only a provider-enforced billing limit can provide an absolute ceiling (docs/radio.md).
The next cloud routes are Google Cloud TTS Chirp3-HD and WaveNet, with deliberately lower hard monthly character
caps in a separate locked ledger. A failed or capped cloud request falls back to the offline engine and then the
system voice (macOS `say`, a Hebrew voice such as Carmit).
Credentials go only into a request header; nothing secret is logged.
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Optional, Tuple

from .config import Config, write_private

RATE = 48000
# Estimated seconds of Gemini Hebrew speech per character of text. The narration voice was measured at 0.075 s per
# character (about 13 characters a second); 0.09 (about 11) deliberately overestimates, because the estimate is the only
# length control for a model that refuses maxOutputTokens: too long an estimate costs a little reserved budget, too
# short a one could let a request run past its reservation (which blocks paid speech for the day).
SECONDS_PER_CHAR = 0.09
MAX_REQUEST_SECONDS = 40.0   # estimated speech per paid request; longer text is split into several requests
# Audio tokens per second of Gemini speech. The lower of the published rates (25; input audio is 32), so a token
# count turns into at least as many seconds as were spoken, and a token ceiling into no more seconds than reserved.
AUDIO_TOKENS_PER_SECOND = 25
RESERVE_FACTOR, RESERVE_EXTRA = 2.0, 5.0   # seconds reserved per request: estimate * factor + extra ...
MIN_RESERVE_FACTOR = 1.3                   # ... or what is left today, when that still covers 1.3 x the estimate
DEFAULT_STYLE = ("A warm, calm adult male radio host giving a short team status update: clear, friendly, a natural "
                 "conversational pace, short pauses between sentences, natural intonation, never shouting.")
_local_busy = threading.Lock()


def seconds_of(pcm: bytes) -> float:
    return len(pcm) / (2.0 * RATE)


def to_pcm(data: bytes, input_args) -> bytes:
    """Any audio ffmpeg reads -> raw 48 kHz mono s16le (b'' on failure)."""
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", *input_args, "-i", "pipe:0", "-ac", "1",
                            "-ar", str(RATE), "-f", "s16le", "pipe:1"], input=data, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return b""
    return r.stdout if r.returncode == 0 else b""


class Usage:
    """One locked daily ledger for all paid speech, with a room sub-total inside it.

    Reservations and settlements change both counters in one atomic write. In-flight reservations
    survive a restart, and settlement always belongs to the date on which the request was reserved.
    """

    KEEP_DAYS = 14

    def __init__(self, path: Path, *, room: bool = False, legacy_room: Optional[Path] = None):
        self.path, self.room, self.legacy_room = path, room, legacy_room

    def for_room(self) -> Usage:
        return Usage(self.path, room=True, legacy_room=self.legacy_room)

    def _read(self) -> dict:
        """Missing is fresh; any malformed existing ledger fails closed and is never overwritten."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"days": {}}
        except (OSError, ValueError, RecursionError):
            return {"unreadable": True}
        def valid_day(day):
            if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                return False
            try:
                from datetime import date
                date.fromisoformat(day)
                return True
            except ValueError:
                return False
        def valid_number(value):
            return type(value) in (int, float) and math.isfinite(value) and value >= 0
        def valid_days(value):
            return isinstance(value, dict) and all(valid_day(k) and valid_number(v) for k, v in value.items())
        try:
            if not isinstance(data, dict) or set(data) - {"days", "room_days", "shared_room", "date", "seconds", "blocked", "pending"}:
                raise ValueError
            if not ("days" in data or {"date", "seconds"} <= data.keys()):
                raise ValueError
            if any(not valid_days(data[k]) for k in ("days", "room_days") if k in data):
                raise ValueError
            if ("date" in data) != ("seconds" in data) or ("date" in data and
                    (not valid_day(data["date"]) or not valid_number(data["seconds"]))):
                raise ValueError
            if any(type(data[k]) is not bool for k in ("shared_room", "blocked") if k in data):
                raise ValueError
            pending = data.get("pending", {})
            if not isinstance(pending, dict):
                raise ValueError
            for key, item in pending.items():
                if (not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key) or
                    not isinstance(item, dict) or set(item) != {"day", "seconds", "room"} or
                    not valid_day(item["day"]) or not valid_number(item["seconds"]) or
                    item["seconds"] <= 0 or type(item["room"]) is not bool):
                    raise ValueError
        except (ValueError, OverflowError, TypeError):
            return {"unreadable": True}
        return data

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @staticmethod
    def _day(now: Optional[float]) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(now))

    def _ledger(self) -> dict:
        data = self._read()
        if data.get("unreadable"):
            return {"unreadable": True}
        days = dict(data.get("days", {}))
        if "date" in data:
            days[data["date"]] = max(days.get(data["date"], 0.0), data["seconds"])
        room_days = dict(data.get("room_days", {}))
        for item in data.get("pending", {}).values():
            day, seconds = item["day"], item["seconds"]
            if days.get(day, 0) < seconds or (item["room"] and room_days.get(day, 0) < seconds):
                return {"unreadable": True}
        if not data.get("shared_room") and self.legacy_room is not None:
            old = Usage(self.legacy_room)._ledger()
            if old.get("unreadable"):
                return {"unreadable": True}
            for day, seconds in old["days"].items():
                days[day] = days.get(day, 0.0) + seconds
                room_days[day] = seconds
        return {"days": days, "room_days": room_days,
                "shared_room": bool(data.get("shared_room") or self.legacy_room is not None),
                "blocked": data.get("blocked", False), "pending": data.get("pending", {})}

    def _write(self, data: dict) -> None:
        for key in ("days", "room_days"):
            data[key] = dict(sorted(data[key].items())[-self.KEEP_DAYS:])
        write_private(self.path, json.dumps(data))

    @staticmethod
    def _used(days: dict, day: str) -> float:
        own = days.get(day, 0.0)
        later = [d for d in days if d > day]
        return max([own] + [days[max(later)]]) if later else own

    def readable_today(self, now: Optional[float] = None) -> Optional[float]:
        data = self._ledger()
        if data.get("unreadable"):
            return None
        return self._used(data["room_days" if self.room else "days"], self._day(now))

    def today(self, now: Optional[float] = None) -> float:
        data = self._ledger()
        if data.get("unreadable"):
            return 24 * 3600.0  # an unreadable ledger counts as a fully used day
        return self._used(data["room_days" if self.room else "days"], self._day(now))

    def _change(self, data: dict, seconds: float, day: str) -> None:
        for key in (("days", "room_days") if self.room else ("days",)):
            data[key][day] = max(0.0, data[key].get(day, 0.0) + seconds)

    def add(self, seconds: float, now: Optional[float] = None) -> None:
        with self._locked():
            data = self._ledger()
            if data.get("unreadable"):
                return  # never overwrite a ledger we could not read
            self._change(data, seconds, self._day(now))
            self._write(data)

    def allows(self, seconds: float, cap_minutes: float, now: Optional[float] = None) -> bool:
        return cap_minutes > 0 and self.today(now) + seconds <= cap_minutes * 60

    def _left(self, data: dict, day: str, cap_minutes: float, room_cap_minutes: Optional[float]) -> Optional[float]:
        """Seconds that may still be reserved now, or None when paid speech is blocked (same rules as reserve).

        ``pending`` is deliberately not scoped to ``day``: midnight resets settled daily usage, but cannot make
        an earlier request with an unknown billing outcome safe to overlap with a new paid request.
        """
        if cap_minutes <= 0 or (self.room and (room_cap_minutes is None or room_cap_minutes <= 0)):
            return None
        if (data.get("unreadable") or data["blocked"] or
                data["pending"]):
            return None
        days = data["days"]
        if days and day < max(days):
            return None
        left = cap_minutes * 60 - self._used(days, day)
        if self.room:
            left = min(left, room_cap_minutes * 60 - self._used(data["room_days"], day))
        return left

    def available(self, cap_minutes: float, now: Optional[float] = None,
                  room_cap_minutes: Optional[float] = None) -> Optional[float]:
        """Seconds a new reservation could take right now (None: blocked or unreadable). A read, not a reservation."""
        with self._locked():
            now = time.time() if now is None else now
            return self._left(self._ledger(), self._day(now), cap_minutes, room_cap_minutes)

    def reserve(self, estimate: float, cap_minutes: float, now: Optional[float] = None,
                room_cap_minutes: Optional[float] = None, *,
                max_seconds: Optional[float] = None) -> Optional[Tuple[float, str, str]]:
        """Reserve against the shared cap AND, for room speech, its sub-cap, under one lock."""
        if cap_minutes <= 0 or (self.room and (room_cap_minutes is None or room_cap_minutes <= 0)):
            return None
        with self._locked():
            now = time.time() if now is None else now
            data, day = self._ledger(), self._day(now)
            left = self._left(data, day, cap_minutes, room_cap_minutes)
            if left is None:
                return None
            want = min(estimate * RESERVE_FACTOR + RESERVE_EXTRA, left)
            if max_seconds is not None:
                want = min(want, max_seconds)
            # Only reserve whole audio tokens, rounding down so the request fits BOTH remaining totals.
            want = math.floor(want * AUDIO_TOKENS_PER_SECOND) / AUDIO_TOKENS_PER_SECOND
            if want < estimate * MIN_RESERVE_FACTOR or want <= 0:
                return None
            self._change(data, want, day)
            nonce = uuid.uuid4().hex
            data["pending"][nonce] = {"day": day, "seconds": want, "room": self.room}
            self._write(data)
            return want, day, nonce

    def settle(self, reserved: Tuple[float, str, str], billed: Optional[float], now: Optional[float] = None,
               *, overrun: bool = False) -> None:
        """Settle and block overruns atomically; unknown results remain pending for their day."""
        seconds, day, nonce = reserved
        if billed is None or not math.isfinite(billed) or billed < 0:
            return
        with self._locked():
            data = self._ledger()
            if data.get("unreadable"):
                return
            item = data["pending"].get(nonce)
            if item != {"day": day, "seconds": seconds, "room": self.room} or day not in data["days"]:
                return
            self._change(data, billed - seconds, day)
            del data["pending"][nonce]
            data["blocked"] = data["blocked"] or overrun or billed > seconds + 1e-6
            self._write(data)


def paid_usage(directory: Path) -> Usage:
    """The shared ledger, including usage recorded by the room before the shared-budget upgrade."""
    return Usage(directory / "tts-usage.json", legacy_room=directory / "room" / "tts-usage.json")


class NewsUsage:
    """Reserve news speech against both the station total and its own small ledger."""
    room = True

    def __init__(self, shared: Usage, news: Usage):
        self.shared, self.news, self.path = shared, news, shared.path
        self._reservations = {}

    def today(self, now=None):
        return self.news.today(now)

    def available(self, cap_minutes, now=None, room_cap_minutes=None):
        total = self.shared.available(cap_minutes, now)
        news = self.news.available(room_cap_minutes or 0, now)
        return None if total is None or news is None else min(total, news)

    def reserve(self, estimate, cap_minutes, now=None, room_cap_minutes=None):
        news = self.news.reserve(estimate, room_cap_minutes or 0, now)
        if news is None:
            return None
        shared = None
        keep = False
        try:
            shared = self.shared.reserve(estimate, cap_minutes, now, max_seconds=news[0])
            if shared is None:
                return None
            nonce = uuid.uuid4().hex
            seconds = min(shared[0], news[0])
            self._reservations[nonce] = (shared, news)
            keep = True
            return seconds, shared[1], nonce
        finally:
            if not keep:
                if shared is not None:
                    self.shared.settle(shared, 0, now)
                self.news.settle(news, 0, now)

    def settle(self, reserved, billed, now=None, *, overrun=False):
        pair = self._reservations.pop(reserved[2], None)
        if pair is None:
            return
        shared, news = pair
        try:
            self.shared.settle(shared, billed, now, overrun=overrun)
        finally:
            self.news.settle(news, billed, now, overrun=overrun)


FREE_TTS_CAPS = {"chirp": 900_000, "wavenet": 3_800_000}


class FreeUsage:
    """Locked UTC-month character ledger for the two Cloud TTS free tiers.

    Characters are reserved permanently before a request. This is intentionally conservative: a timeout cannot
    make a later request cross the hard local ceiling. Any malformed existing ledger is left untouched and makes
    both tiers unavailable.
    """

    KEEP_MONTHS = 14

    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def _month(now: Optional[float]) -> str:
        return time.strftime("%Y-%m", time.gmtime(now))

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _read(self) -> Optional[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"months": {}}
        except (OSError, ValueError, RecursionError):
            return None
        try:
            months = data["months"]
            if set(data) != {"months"} or not isinstance(months, dict):
                raise ValueError
            for month, used in months.items():
                if (not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month) or
                        not isinstance(used, dict) or set(used) - set(FREE_TTS_CAPS)):
                    raise ValueError
                if any(type(value) is not int or value < 0 or value > FREE_TTS_CAPS[tier]
                       for tier, value in used.items()):
                    raise ValueError
        except (KeyError, TypeError, ValueError):
            return None
        return data

    def reserve(self, tier: str, characters: int, now: Optional[float] = None) -> bool:
        if tier not in FREE_TTS_CAPS or type(characters) is not int or characters <= 0:
            return False
        with self._locked():
            data = self._read()
            if data is None:
                return False
            month = self._month(time.time() if now is None else now)
            months = data["months"]
            if months and month < max(months):
                return False
            used = months.setdefault(month, {})
            if used.get(tier, 0) + characters > FREE_TTS_CAPS[tier]:
                return False
            used[tier] = used.get(tier, 0) + characters
            data["months"] = dict(sorted(months.items())[-self.KEEP_MONTHS:])
            write_private(self.path, json.dumps(data))
            return True


def free_usage(directory: Path) -> FreeUsage:
    """The free-tier ledger lives beside, but never inside, the paid-speech ledger."""
    return FreeUsage(directory / "tts-free-usage.json")


TOKEN_TIMEOUT = 90.0     # gcloud can take 20-40 s to print a token on a loaded Mac (17 s measured at load 36)
TOKEN_REUSE = 45 * 60.0  # an access token lives an hour: reuse it, never run gcloud per clip
_tokens: dict = {}
_tokens_lock = threading.Lock()
_SECRETISH = re.compile(r"ya29\.\S+|\b[A-Za-z0-9_\-]{40,}\b")


def _why(why: Optional[list], reason: str) -> None:
    if why is not None:
        why.append(_SECRETISH.sub("<hidden>", reason)[:160])


def access_token(cfg: Config, why: Optional[list] = None) -> str:
    """The Vertex access token from the configured command, kept for TOKEN_REUSE seconds. '' when unavailable,
    with the reason (never the token or the command's output) appended to `why`."""
    command = tuple(cfg.gemini_token_command or ())
    if not command:
        _why(why, "no gemini_token_command configured")
        return ""
    with _tokens_lock:
        hit = _tokens.get(command)
        if hit and time.monotonic() - hit[1] < TOKEN_REUSE:
            return hit[0]
    started = time.monotonic()
    try:
        r = subprocess.run(list(command), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=TOKEN_TIMEOUT)
    except FileNotFoundError:
        _why(why, f"token command {Path(command[0]).name!r} not found on the job's PATH")
        return ""
    except subprocess.TimeoutExpired:
        _why(why, f"token command timed out after {TOKEN_TIMEOUT:.0f} s")
        return ""
    except OSError as e:
        _why(why, f"token command could not start ({e.__class__.__name__})")
        return ""
    token = r.stdout.strip()
    if r.returncode or not token:
        first = next((line.strip() for line in (r.stderr or "").splitlines() if line.strip()), "")
        _why(why, f"token command exited {r.returncode} after {time.monotonic() - started:.0f} s"
                  + (f": {first}" if first else "") if r.returncode else "token command printed no token")
        return ""
    with _tokens_lock:
        _tokens[command] = (token, time.monotonic())
    return token


def forget_token(cfg: Config) -> None:
    with _tokens_lock:
        _tokens.pop(tuple(cfg.gemini_token_command or ()), None)


def cloud_tts(text: str, cfg: Config, voice: str, timeout: float = 120.0,
              why: Optional[list] = None) -> bytes:
    """Google Cloud Text-to-Speech v1 -> 48 kHz mono PCM, using the Vertex project's credentials."""
    from .creds import for_outside
    project = cfg.gemini_vertex_project or ""
    if not project:
        _why(why, "no gemini_vertex_project configured")
        return b""
    token = access_token(cfg, why)
    if not token:
        return b""
    body = {"input": {"text": for_outside(text, getattr(cfg, "redact_outgoing", True))},
            "voice": {"languageCode": "he-IL", "name": voice},
            "audioConfig": {"audioEncoding": "LINEAR16"}}
    req = urllib.request.Request(
        "https://texttospeech.googleapis.com/v1/text:synthesize",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token,
                 "x-goog-user-project": project},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.load(resp)
        raw = base64.b64decode(result["audioContent"], validate=True)
    except urllib.error.HTTPError as e:
        _why(why, f"Cloud TTS {voice} returned HTTP {e.code}")
        if e.code == 401:
            forget_token(cfg)
        e.close()
        return b""
    except Exception as e:
        _why(why, f"Cloud TTS {voice} failed ({e.__class__.__name__})")
        return b""
    pcm = to_pcm(raw, [])
    if not pcm:
        _why(why, f"Cloud TTS {voice} audio could not be converted")
    return pcm


# Models that refused maxOutputTokens (remembered while the process runs): they are asked without it, and the
# length is held by the text estimate instead (see `fits_without_ceiling`).
NO_CEILING: set = set()


def estimate_seconds(text: str) -> float:
    """Estimated seconds of speech for the text (deliberately slow, see SECONDS_PER_CHAR)."""
    return len(text) * SECONDS_PER_CHAR


def fits_without_ceiling(text: str, max_seconds: float) -> bool:
    """A request with no maxOutputTokens may be sent only when the text's estimated speech is a short request and
    fits inside the reservation with the same margin the reservation itself demands."""
    estimate = estimate_seconds(text)
    return estimate <= MAX_REQUEST_SECONDS and estimate * MIN_RESERVE_FACTOR <= max_seconds + 1e-9


def vertex(text: str, cfg: Config, model: str, voice: str, style: str, max_seconds: float = 0.0,
           timeout: float = 120.0, why: Optional[list] = None, hard: bool = True) -> Tuple[bytes, Optional[float]]:
    """Gemini TTS on Vertex -> (PCM 48 kHz mono or b'', billed seconds). Billed is 0.0 when no request was sent,
    None when one was sent and its cost is unknown (keep the whole reservation), else the seconds billed.
    The request asks for no more audio than `max_seconds` (maxOutputTokens). A model that refuses that field (HTTP
    400) is asked once more without it, and from then on without it; such a request is sent only when the text's
    estimated speech fits `max_seconds` (and one request), otherwise nothing is sent and the caller uses the free
    voice. `hard` is retained for call compatibility. A failure's reason goes to `why`."""
    if max_seconds <= 0 or not math.isfinite(max_seconds):
        _why(why, "no usable reserved length; using the free voice")
        return b"", 0.0
    from .creds import for_outside
    clean = getattr(cfg, "redact_outgoing", True)
    text, style = for_outside(text, clean), for_outside(style, clean)  # every field of the request
    project = urllib.parse.quote(cfg.gemini_vertex_project or "", safe="-._")
    if not project:
        _why(why, "no gemini_vertex_project configured")
        return b"", 0.0
    token = access_token(cfg, why)
    if not token:
        return b"", 0.0
    part = {"text": text}
    if not model.startswith("gemini-2"):
        part["speechMetadata"] = {"style": style}  # newer TTS models read the text verbatim; style goes here
    generation = {"responseModalities": ["AUDIO"],
                  "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}
    token_ceiling = int(math.floor(max_seconds * AUDIO_TOKENS_PER_SECOND))
    if token_ceiling < 1:
        _why(why, "the reserved length is below one audio token")
        return b"", 0.0
    if model not in NO_CEILING:
        generation["maxOutputTokens"] = token_ceiling
    elif not fits_without_ceiling(text, max_seconds):
        _why(why, "the text is too long for the reserved length; using the free voice")
        return b"", 0.0
    refused_ceiling = False
    body = {"contents": [{"role": "user", "parts": [part]}], "generationConfig": generation}
    url = (f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/publishers/google/models/"
           f"{urllib.parse.quote(model, safe='-._')}:generateContent")
    while True:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.load(resp)
            if refused_ceiling:
                NO_CEILING.add(model)  # it answered once the ceiling was left out: the ceiling was the problem
            break
        except urllib.error.HTTPError as e:
            try:
                detail = str((json.loads(e.read() or b"{}").get("error") or {}).get("message") or "")
            except Exception:
                detail = ""
            e.close()
            if e.code == 400 and "maxOutputTokens" in generation:
                # Refused, so not billed. Ask again without the ceiling, but only when the text itself keeps the
                # request inside the reservation.
                if not fits_without_ceiling(text, max_seconds):
                    _why(why, "the voice model refuses a length ceiling and the text is too long to send without one")
                    return b"", 0.0
                del generation["maxOutputTokens"]
                refused_ceiling = True
                continue
            if e.code in (401, 403):
                forget_token(cfg)  # an expired or revoked token: fetch a fresh one next time
            _why(why, f"Vertex answered HTTP {e.code}" + (f": {detail}" if detail else ""))
            return b"", (0.0 if 400 <= e.code < 500 else None)  # a refused request is not billed; a 5xx may be
        except Exception as e:
            _why(why, f"Vertex request failed ({e.__class__.__name__})")
            return b"", None  # timeout, dropped connection: it may have been billed
    tokens = None
    try:
        u = result.get("usageMetadata") or {}
        if "candidatesTokenCount" in u:
            tokens = int(u["candidatesTokenCount"])
    except (AttributeError, TypeError, ValueError):
        tokens = None
    billed_by_tokens = tokens / AUDIO_TOKENS_PER_SECOND if tokens is not None else None
    try:
        cand = result["candidates"][0]
        inline = next(p["inlineData"] for p in cand["content"]["parts"] if p.get("inlineData"))
        raw = base64.b64decode(inline["data"])
    except Exception:
        _why(why, f"Vertex answered without audio (finish {str((result.get('candidates') or [{}])[0].get('finishReason') or '?') if isinstance(result, dict) else '?'})")
        return b"", billed_by_tokens
    mime = str(inline.get("mimeType") or "")
    if re.search(r"wav", mime, re.I):
        pcm = to_pcm(raw, [])
        raw_seconds = None
    else:
        rate = re.search(r"rate=(\d+)", mime)
        raw_seconds = len(raw) / (2.0 * int(rate.group(1) if rate else 24000))
        pcm = to_pcm(raw, ["-f", "s16le", "-ar", rate.group(1) if rate else "24000", "-ac", "1"])
    known = [x for x in (billed_by_tokens, raw_seconds, seconds_of(pcm) if pcm else None) if x is not None]
    billed = max(known) if known else None
    if str(cand.get("finishReason") or "").upper() == "MAX_TOKENS":
        _why(why, "Vertex audio was cut at the reserved length")
        return b"", billed  # cut off at the ceiling: billed, but never put a half sentence on the air
    if not pcm:
        _why(why, "the Vertex audio could not be converted")
    return pcm, billed


def system_voice(text: str, voice: str) -> bytes:
    """macOS `say` -> PCM 48 kHz mono, or b'' (not macOS, or the voice is missing)."""
    say = shutil.which("say")
    if not say:
        return b""
    text = clean_for_voice(text)  # the staff edition and the free fallback read the same clean text as the paid voice
    with tempfile.TemporaryDirectory(prefix="ctb-radio-say-") as d:
        src, out = Path(d) / "text.txt", Path(d) / "out.wav"
        src.write_text(text, encoding="utf-8")
        args = [say, "-f", str(src), "-o", str(out), "--file-format=WAVE", f"--data-format=LEI16@{RATE}"]
        if voice:
            args[1:1] = ["-v", voice]
        try:
            subprocess.run(args, capture_output=True, timeout=180, check=True)
            return to_pcm(out.read_bytes(), [])
        except (OSError, subprocess.SubprocessError):
            return b""


SPOKEN_LOG_MAX = 100_000   # bytes; at the limit the file moves to ".1" (one older copy is kept)


def clean_for_voice(text: str) -> str:
    """What the voice may be given. Invisible and bidi characters go FIRST (one inside a label such as "api_key=" would
    hide it from the redaction); then credentials and owner-only links are redacted on the text as written (the
    label is still there to recognise its value), then the text is normalised for speech, then redacted once more
    in case normalising exposed a shape."""
    from .creds import for_outside
    from .radio_script import for_voice, strip_invisible
    return for_outside(for_voice(for_outside(strip_invisible(text))))


def spoken_log(path: Path, route: str, text: str, now: Optional[float] = None,
               length: Optional[int] = None) -> None:
    """One private line per spoken text, exactly as the voice was given it (after `for_voice`, through the one
    outbound sanitizer, so no credential or owner-only link): the next "it cut a word" report can be matched
    against what was really fed in. Rotated and size-capped; never raises."""
    try:
        line = " ".join(clean_for_voice(text).split())
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > SPOKEN_LOG_MAX:
            path.replace(path.with_name(path.name + ".1"))
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            if length is None:
                f.write(f"{stamp} [{route or '-'}] {len(line)} chars: {line}\n")
            else:
                f.write(f"{stamp} [{route or '-'}] {length} chars\n")
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001 - a log line must never stop the radio
        pass


def split_text(text: str, max_seconds: float = MAX_REQUEST_SECONDS) -> list:
    """The text as pieces whose estimated speech is at most `max_seconds` each, cut at sentence ends (or, for one
    overlong sentence, at spaces, or at the length when there are no spaces). Joining the pieces with a space gives
    the original words back. A text that already fits is returned whole."""
    limit = max(1, int(max_seconds / SECONDS_PER_CHAR))
    if len(text) <= limit:
        return [text]
    units = []
    for sentence in re.split(r"(?<=[.!?…:;])\s+", text):
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit + 1)
            cut = cut if cut > 0 else limit
            units.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            units.append(sentence)
    pieces, current = [], ""
    for unit in units:
        if current and len(current) + 1 + len(unit) > limit:
            pieces.append(current)
            current = unit
        else:
            current = f"{current} {unit}" if current else unit
    if current:
        pieces.append(current)
    return pieces


def _cache_key(model: str, voice: str, style: str, text: str) -> str:
    return hashlib.sha256(json.dumps([model, voice, style, text]).encode()).hexdigest()


def _cache_put(path: Path, pcm: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(pcm)
        tmp.replace(path)
    except OSError:
        pass


def _local(text: str, cfg: Config, cache: Path) -> bytes:
    """Run the configured offline engine sentence-by-sentence and return radio PCM.

    The shared engine runner suppresses all child output, owns the process group, enforces its timeout, and accepts
    only OggS output. The lock keeps this CPU-heavy engine to one synthesis at a time across radio callers.
    """
    from .listen import run_engine
    engine = list(getattr(cfg, "tts_command", None) or [])
    voice = getattr(cfg, "tts_voice", "") or ""
    if not engine:
        return b""
    key = json.dumps(engine, ensure_ascii=False, separators=(",", ":"))
    whole = cache / f"{_cache_key('local', key, voice, text)}.pcm"
    try:
        hit = whole.read_bytes()
        if hit:
            return hit
    except OSError:
        pass
    with _local_busy:
        # Another caller may have filled it while this caller waited.
        try:
            hit = whole.read_bytes()
            if hit:
                return hit
        except OSError:
            pass
        parts = []
        for piece in split_text(text):
            path = cache / f"{_cache_key('local', key, voice, piece)}.pcm"
            try:
                pcm = path.read_bytes()
            except OSError:
                pcm = b""
            if not pcm:
                ogg = run_engine(cfg, piece)
                pcm = to_pcm(ogg, []) if ogg else b""
                if not pcm:
                    return b""
                _cache_put(path, pcm)
            parts.append(pcm)
        pcm = b"".join(parts)
        if pcm:
            _cache_put(whole, pcm)
        return pcm


def _local_or_system(text: str, cfg: Config, settings: dict, cache: Path) -> Tuple[bytes, str]:
    pcm = _local(text, cfg, cache)
    if pcm:
        return pcm, "local"
    pcm = system_voice(text, settings.get("say_voice", ""))
    return (pcm, "say") if pcm else (b"", "")


def _paid_piece(piece: str, cfg: Config, settings: dict, cache: Path, usage: Usage, now: Optional[float],
                why: list) -> bytes:
    """One paid request: reserve, send, settle. PCM, or b"" with the reason in `why` (the caller falls back to the
    free voice for the whole text). A piece already paid for is read from the cache."""
    global last_cap_out
    model, voice = settings["tts_model"], settings["voice"]
    style = settings.get("style") or DEFAULT_STYLE
    path = cache / f"{_cache_key(model, voice, style, piece)}.pcm"
    try:
        hit = path.read_bytes()
        if hit:
            return hit
    except OSError:
        pass
    try:
        reserved = usage.reserve(estimate_seconds(piece), settings["tts_daily_minutes"], now,
                                 room_cap_minutes=settings.get("tts_room_daily_minutes"))
    except OSError:
        reserved = None
        why.append("the paid voice budget is unavailable")
    if not reserved:
        cap = min(settings["tts_daily_minutes"], settings.get("tts_room_daily_minutes", 0)) if usage.room else settings["tts_daily_minutes"]
        why.append("the paid voice is off (daily cap is 0)" if cap <= 0 else
                   f"today's {cap} paid minutes are used up")
        last_cap_out = cap > 0
        return b""
    try:
        pcm, billed = vertex(piece, cfg, model, voice, style, max_seconds=reserved[0], why=why, hard=True)
    except Exception as e:
        pcm, billed = b"", None
        why.append(f"Vertex voice crashed ({e.__class__.__name__})")
    # Whatever the provider did, record the full observed cost (never hide an overrun). A response longer than
    # its reservation is discarded without caching, and the ledger blocks further paid speech that day.
    observed = max(seconds_of(pcm), billed or 0.0)
    overrun = not math.isfinite(observed) or observed > reserved[0] + 1e-6
    if overrun:
        why.append("the voice model exceeded its length ceiling; using the free voice")
        pcm = b""
    try:
        usage.settle(reserved, observed if billed is not None or overrun else None, now, overrun=overrun)
    except OSError:
        # The persisted pending reservation still prevents another request today.
        pcm = b""
        why.append("the paid voice budget could not be saved")
    if pcm:
        _cache_put(path, pcm)
    return pcm


def speak(text: str, cfg: Config, settings: dict, cache: Path, usage: Usage,
          now: Optional[float] = None) -> Tuple[bytes, str]:
    """(PCM, route) with route "local", "cache", "vertex", "chirp", "wavenet", "say" or "" when nothing could speak. Why the paid voice was
    not used is kept in `last_reason` (for the log; never a secret).

    The paid voice is length-controlled by the text: it is split into requests of at most MAX_REQUEST_SECONDS of
    estimated speech, and is used only when ALL of them fit what is left of today's paid minutes (otherwise the whole
    text goes to the free voice, never half and half). Each request reserves before it is sent and is settled to
    the billed seconds before the next one starts."""
    global last_reason, last_cap_out
    last_reason, last_cap_out = "", False
    why: list = []
    text = clean_for_voice(text)  # the voice reads the text verbatim: no symbols or unsayable Latin tokens reach it
    if settings.get("voice_route", "local") != "cloud":
        return _local_or_system(text, cfg, settings, cache)
    if settings.get("tts_force_system"):
        last_reason = "no listener detected; using a local voice"
        return _local_or_system(text, cfg, settings, cache)
    model, voice = settings["tts_model"], settings["voice"]
    style = settings.get("style") or DEFAULT_STYLE
    path = cache / f"{_cache_key(model, voice, style, text)}.pcm"
    try:
        pcm = path.read_bytes()
        if pcm:
            return pcm, "cache"
    except OSError:
        pass
    pieces = split_text(text)
    paid = b""
    try:
        left = usage.available(settings["tts_daily_minutes"], now, room_cap_minutes=settings.get("tts_room_daily_minutes"))
    except OSError:
        left = None
        why.append("the paid voice budget is unavailable")
    need = sum(estimate_seconds(p) * MIN_RESERVE_FACTOR for p in pieces)
    if left is not None and need <= left:
        parts = []
        for piece in pieces:
            part = _paid_piece(piece, cfg, settings, cache, usage, now, why)
            if not part:
                parts = []
                break
            parts.append(part)
        paid = b"".join(parts)
    elif left is not None:
        cap = min(settings["tts_daily_minutes"], settings.get("tts_room_daily_minutes", 0)) if usage.room else settings["tts_daily_minutes"]
        why.append(f"the text needs about {need:.0f} s of paid speech but only {max(left, 0):.0f} s are left today"
                   if cap > 0 else "the paid voice is off (daily cap is 0)")
        last_cap_out = cap > 0
    elif not why:
        cap = min(settings["tts_daily_minutes"], settings.get("tts_room_daily_minutes", 0)) if usage.room else settings["tts_daily_minutes"]
        why.append("the paid voice is off (daily cap is 0)" if cap <= 0 else
                   f"today's {cap} paid minutes are used up")
        last_cap_out = cap > 0
    if paid:
        if len(pieces) > 1:
            _cache_put(path, paid)
        return paid, "vertex"
    last_reason = "; ".join(why) or "the paid voice gave no audio"
    ledger = free_usage(usage.path.parent)
    radio_voice = settings.get("voice") or "Sadaltager"
    free_routes = (("chirp", f"he-IL-Chirp3-HD-{radio_voice}"), ("wavenet", "he-IL-Wavenet-B"))
    # Do not consume the local allowance when credentials are known to be unavailable.
    for tier, cloud_voice in free_routes if cfg.gemini_vertex_project and cfg.gemini_token_command else ():
        try:
            allowed = ledger.reserve(tier, len(text), now)
        except OSError:
            allowed = False
        if not allowed:
            continue
        pcm = cloud_tts(text, cfg, cloud_voice, why=why)
        if pcm:
            return pcm, tier
    return _local_or_system(text, cfg, settings, cache)


last_reason = ""
last_cap_out = False  # the last speak() found the day's paid minutes used up


def prune(cache: Path, days: float = 7.0, now: Optional[float] = None) -> None:
    cutoff = (now or time.time()) - days * 86400
    for f in cache.glob("*.pcm"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


# --- the intro ---------------------------------------------------------------------------------

INTRO_SECONDS = 1.6
INTRO_GAP = 0.35       # silence between the chime and the voice
INTRO_PEAK = 0.16      # of full scale, about -16 dBFS: well under the voice, never startling
_intro: Optional[bytes] = None


def intro() -> bytes:
    """The radio's own short, soft chime before every update, so a listener is never
    startled: two bell tones a fourth apart (G5, C6), each faded in over 40 ms and decaying
    slowly, the whole chime rising from silence, then a short pause. Synthesised here, the same every time;
    no recorded or third-party audio. 48 kHz mono s16le."""
    global _intro
    if _intro is not None:
        return _intro
    import array
    n = int(INTRO_SECONDS * RATE)
    mix = [0.0] * n
    for start, freq in ((0.0, 783.99), (0.32, 1046.50)):
        first = int(start * RATE)
        for i in range(first, n):
            t = (i - first) / RATE
            env = min(1.0, t / 0.04) * math.exp(-t / 0.42)
            if env < 1e-4 and t > 0.1:
                break
            w = 2 * math.pi * freq * t
            mix[i] += env * (math.sin(w) + 0.28 * math.sin(2 * w) + 0.12 * math.sin(2.76 * w))
    peak = max(abs(v) for v in mix) or 1.0
    rise, fall = int(0.25 * RATE), int(0.3 * RATE)
    out = array.array("h")
    for i, v in enumerate(mix):
        g = min(1.0, i / rise) * min(1.0, (n - i) / fall)
        out.append(int(32767 * INTRO_PEAK * g * v / peak))
    _intro = out.tobytes() + bytes(2 * int(INTRO_GAP * RATE))
    return _intro
