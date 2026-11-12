"""The radio's voice: text to 48 kHz mono 16-bit PCM.

Route 1, Gemini TTS on Vertex (the config's gemini_vertex_project and gemini_token_command, the same
credentials the translator uses): billed speech, so every clip is cached on disk by its exact text, model, voice
and style, and a daily cap (radio.tts_daily_minutes) stops it for the rest of the day.

The cap is hard: before each request a conservative amount of speech is reserved (and the request asks the model
for no more audio than that, maxOutputTokens), and the request is sent only when the reservation fits. Afterwards
the reservation is settled to what was actually billed (the response's audio token count, or the audio length,
whichever is more); a request whose outcome is unknown (a timeout, a broken response) keeps its whole
reservation. A provider-side budget on the Google Cloud project is still the ceiling for money (docs/radio.md).
Route 2, the system voice (macOS `say`, a Hebrew voice such as Carmit): local and free; used when Vertex is not
configured, fails, or the cap is reached.
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
from pathlib import Path
from typing import Optional, Tuple

from .config import Config, write_private

RATE = 48000
SECONDS_PER_CHAR = 0.075  # Gemini Hebrew speech, measured on the narration voice
# Audio tokens per second of Gemini speech. The lower of the published rates (25; input audio is 32), so a token
# count turns into at least as many seconds as were spoken, and a token ceiling into no more seconds than reserved.
AUDIO_TOKENS_PER_SECOND = 25
RESERVE_FACTOR, RESERVE_EXTRA = 2.0, 5.0   # seconds reserved per request: estimate * factor + extra ...
MIN_RESERVE_FACTOR = 1.3                   # ... or what is left today, when that still covers 1.3 x the estimate
DEFAULT_STYLE = ("A warm, calm adult male radio host giving a short team status update: clear, friendly, a natural "
                 "conversational pace, short pauses between sentences, natural intonation, never shouting.")


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
    """Billed (or reserved) seconds per local day, in a small private JSON file ({"days": {date: seconds}}, the last
    KEEP_DAYS dates). Reserve and settle hold a file lock, so two speakers never both fit into the same remaining
    minutes. Each retained date keeps its own total. Reservations are refused if the clock moves before the
    latest recorded date, so turning the clock back cannot renew the budget."""

    KEEP_DAYS = 14

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @staticmethod
    def _day(now: Optional[float]) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(now))

    def _days(self) -> dict:
        data = self._read()
        days = {}
        raw = data.get("days")
        for day, seconds in (raw.items() if isinstance(raw, dict) else []):
            if isinstance(day, str) and isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
                days[day] = max(0.0, float(seconds))
        if isinstance(data.get("date"), str) and isinstance(data.get("seconds"), (int, float)):  # the older one-day file
            days[data["date"]] = max(days.get(data["date"], 0.0), float(data["seconds"]))
        return days

    @staticmethod
    def _used(days: dict, day: str) -> float:
        own = days.get(day, 0.0)
        later = [d for d in days if d > day]
        return max([own] + [days[max(later)]]) if later else own  # the clock went back: never a fresh budget

    def today(self, now: Optional[float] = None) -> float:
        return self._used(self._days(), self._day(now))

    def _add(self, seconds: float, now: Optional[float]) -> None:
        days, day = self._days(), self._day(now)
        days[day] = round(max(0.0, days.get(day, 0.0) + seconds), 2)
        keep = dict(sorted(days.items())[-self.KEEP_DAYS:])
        write_private(self.path, json.dumps({"days": keep}))

    def add(self, seconds: float, now: Optional[float] = None) -> None:
        with self._locked():
            self._add(seconds, now)

    def allows(self, seconds: float, cap_minutes: float, now: Optional[float] = None) -> bool:
        return cap_minutes > 0 and self.today(now) + seconds <= cap_minutes * 60

    def reserve(self, estimate: float, cap_minutes: float, now: Optional[float] = None) -> Optional[Tuple[float, str]]:
        """Reserve the seconds one request may cost at most: (seconds, day), or None when they do not fit."""
        if cap_minutes <= 0:
            return None
        now = time.time() if now is None else now
        with self._locked():
            days, day = self._days(), self._day(now)
            if days and day < max(days):
                return None
            left = cap_minutes * 60 - self._used(days, day)
            want = min(estimate * RESERVE_FACTOR + RESERVE_EXTRA, left)
            if want < estimate * MIN_RESERVE_FACTOR or want <= 0:
                return None
            self._add(want, now)
            return want, day

    def settle(self, reserved: Tuple[float, str], billed: Optional[float], now: Optional[float] = None) -> None:
        """Replace a reservation by what was billed (None: unknown, so the whole reservation stays counted)."""
        seconds, day = reserved
        if billed is None:
            return
        with self._locked():
            if day == self._day(now):
                self._add(billed - seconds, now)
            elif billed > seconds:  # reserved yesterday: only what went over the reservation is new today
                self._add(billed - seconds, now)


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


# Models that refuse maxOutputTokens: for them the day's cap holds by the reservation (an estimate x 2 + 5 s of
# the plain text, read verbatim) and is settled to the billed audio afterwards, so one clip can pass the cap by
# what it overran its reservation; the next request then no longer fits.
NO_CEILING: set = set()


def vertex(text: str, cfg: Config, model: str, voice: str, style: str, max_seconds: float = 0.0,
           timeout: float = 120.0, why: Optional[list] = None, hard: bool = False) -> Tuple[bytes, Optional[float]]:
    """Gemini TTS on Vertex -> (PCM 48 kHz mono or b'', billed seconds). Billed is 0.0 when no request was sent,
    None when one was sent and its cost is unknown (keep the whole reservation), else the seconds billed.
    `max_seconds` caps the audio the model may produce (maxOutputTokens). With `hard` (the brainstorming room's own
    cap) a request is never sent without that ceiling: a model that refuses it gets nothing (the free voice speaks).
    A failure's reason goes to `why`."""
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
    if max_seconds > 0 and model not in NO_CEILING:
        generation["maxOutputTokens"] = max(1, int(math.floor(max_seconds * AUDIO_TOKENS_PER_SECOND)))
    body = {"contents": [{"role": "user", "parts": [part]}], "generationConfig": generation}
    url = (f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/publishers/google/models/"
           f"{urllib.parse.quote(model, safe='-._')}:generateContent")
    while True:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            try:
                detail = str((json.loads(e.read() or b"{}").get("error") or {}).get("message") or "")
            except Exception:
                detail = ""
            e.close()
            if e.code == 400 and "maxOutputTokens" in generation and hard:
                NO_CEILING.add(model)  # refused, so not billed; never asked again without the ceiling
                _why(why, "the voice model refuses a length ceiling; a hard cap sends nothing")
                return b"", 0.0
            if e.code == 400 and "maxOutputTokens" in generation:
                # gemini-3.8-flash-tts refuses maxOutputTokens with a bare "invalid argument" (every
                # update went out in `say`). Refused requests are not billed; ask again without the ceiling.
                NO_CEILING.add(model)
                generation.pop("maxOutputTokens")
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


def spoken_log(path: Path, route: str, text: str, now: Optional[float] = None) -> None:
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
            f.write(f"{stamp} [{route or '-'}] {len(line)} chars: {line}\n")
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001 - a log line must never stop the radio
        pass


def speak(text: str, cfg: Config, settings: dict, cache: Path, usage: Usage,
          now: Optional[float] = None) -> Tuple[bytes, str]:
    """(PCM, route) with route "cache", "vertex", "say" or "" when nothing could speak. Why the paid voice was
    not used is kept in `last_reason` (for the log; never a secret)."""
    global last_reason, last_cap_out
    last_reason, last_cap_out = "", False
    why: list = []
    text = clean_for_voice(text)  # the voice reads the text verbatim: no symbols or unsayable Latin tokens reach it
    model, voice = settings["tts_model"], settings["voice"]
    style = settings.get("style") or DEFAULT_STYLE
    key = hashlib.sha256(json.dumps([model, voice, style, text]).encode()).hexdigest()
    path = cache / f"{key}.pcm"
    try:
        pcm = path.read_bytes()
        if pcm:
            return pcm, "cache"
    except OSError:
        pass
    estimate = len(text) * SECONDS_PER_CHAR
    hard = bool(settings.get("tts_hard_cap"))
    if hard and model in NO_CEILING:  # a hard cap and a model that cannot be held to a length: the free voice
        why.append("the voice model refuses a length ceiling; a hard cap uses the free voice")
        last_reason = "; ".join(why)
        pcm = system_voice(text, settings.get("say_voice", ""))
        return (pcm, "say") if pcm else (b"", "")
    reserved = usage.reserve(estimate, settings["tts_daily_minutes"], now) if settings["tts_daily_minutes"] > 0 else None
    if not reserved:
        why.append("the paid voice is off (tts_daily_minutes is 0)" if settings["tts_daily_minutes"] <= 0 else
                   f"today's {settings['tts_daily_minutes']} paid minutes are used up")
        last_cap_out = settings["tts_daily_minutes"] > 0
    if reserved:
        try:
            pcm, billed = vertex(text, cfg, model, voice, style, max_seconds=reserved[0], why=why,
                                 **({"hard": True} if hard else {}))
        except Exception as e:
            pcm, billed = b"", None
            why.append(f"Vertex voice crashed ({e.__class__.__name__})")
        usage.settle(reserved, billed, now)
        if pcm:
            try:
                cache.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(pcm)
                tmp.replace(path)
            except OSError:
                pass
            return pcm, "vertex"
    last_reason = "; ".join(why) or "the paid voice gave no audio"
    pcm = system_voice(text, settings.get("say_voice", ""))
    return (pcm, "say") if pcm else (b"", "")


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
