"""The radio's podcast queue: the owner puts an episode on the air, the radio's own talk waits, then goes on.

Sources: the owner's feed list (config `radio_podcasts`, default empty) and single episode links. A feed is read
safely (https only, a size and a time limit, redirects checked hop by hop, and every address a name resolves to
must be a public one, checked on the very connection that is used, so a name that changes its answer between a
check and a connection cannot reach a private address). The queue is a small private file shared by the Cockpit,
the `/podcast` command and the radio's job; the job (`Player`) downloads the head of the queue into a private
cache (size capped, cleaned), decodes it with ffmpeg and hands it to the radio's mixer as a "show": while a show
is attached the mixer plays it instead of the bed and the radio's queued clips wait (the clip on the air is never
cut); when it ends or is stopped the bed fades in and the waiting clips go on.

Safety:
- every episode passes the radio's child-suitability rules (`radio.room_suitability_file`, the same file the
  brainstorming room uses) on its title and description before it is queued, and an episode marked explicit is
  never queued; a configured rule file that cannot be read refuses every episode (as it closes the rooms);
- the audio is never spoken text of the radio: nothing about an episode reaches the captions, the headline or the
  alerts channel;
- podcast audio goes to the owner's private Telegram radio only. The staff edition on YouTube
  (radio_youtube / radio_staff) is a separate job with its own mixer and a deny-by-default allowlist that has no
  audio section; it never imports this module, so an episode is never mirrored there (a test holds this).
"""
from __future__ import annotations

import collections
import email.utils
import fcntl
import hashlib
import html
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .config import Config, private_dir, write_private

MAX_FEED_BYTES = 2 * 1024 * 1024        # a feed larger than this is refused
FETCH_TIMEOUT = 8.0                     # seconds for one connect or read
FETCH_DEADLINE = 12.0                   # seconds for a whole feed fetch
MAX_REDIRECTS = 3
MAX_EPISODE_BYTES = 150 * 1024 * 1024   # one downloaded episode
DOWNLOAD_DEADLINE = 1200.0
CACHE_MAX_BYTES = 400 * 1024 * 1024     # the whole cache
CACHE_KEEP_DAYS = 2
MAX_QUEUE = 10
MAX_EPISODES = 12                       # episodes listed from one feed
MAX_TITLE, MAX_DESC = 120, 600
MAX_URL = 2000
BUFFER_SECONDS = 8                      # decoded audio kept ahead of the mixer
RATE = 48000
STATUS_FRESH = 15.0                     # a status older than this means the radio is not running
PLAYER_OPS = ("play", "pause", "skip", "stop")
ITEM_ID = re.compile(r"[0-9a-f]{16}")
_AUDIO_EXT = re.compile(r"\.(?:mp3|m4a|aac|ogg|oga|opus|wav|flac)$", re.I)
_TAGS = re.compile(r"<[^>]*>")
_SUIT_NORM = re.compile("[֑-ׇ]")


class PodcastError(Exception):
    """kind: bad | url | fetch | feed | too_big | unknown | full | dup | unsuitable | explicit | notext | rules | decode"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


# --- the network, guarded ------------------------------------------------------------------------------

def resolve(host: str, port: int) -> list:
    """Every address of a name (tests replace this; nothing here touches the network on its own)."""
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_url(url: object) -> urllib.parse.SplitResult:
    """https only, a host, no login in the address, port 443; PodcastError("url") otherwise."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL or any(c.isspace() or ord(c) < 32 for c in url):
        raise PodcastError("url")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise PodcastError("url") from None
    if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
        raise PodcastError("url")
    if port not in (None, 443):
        raise PodcastError("url")
    return parts


class _Connection(http.client.HTTPSConnection):
    """Connects only to a public address, chosen from the same answer that was checked. urllib sets `sock` to None
    once the response owns the connection, so the socket the watchdog must shut down is kept apart in `cut_socket`."""

    cut_socket = None

    def connect(self) -> None:
        infos = resolve(self.host, self.port or 443)
        if not infos or not all(public(i[4][0]) for i in infos):
            raise PodcastError("url")
        family, kind, proto, _name, address = infos[0]
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(self.timeout)
            sock.connect(address)
            self.sock = self.cut_socket = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class _Https(urllib.request.HTTPSHandler):
    def https_open(self, req):
        made: list = []

        def connection(host, **kw):
            made.append(_Connection(host, **kw))
            return made[-1]
        resp = self.do_open(connection, req, context=ssl.create_default_context())
        resp.podcast_conn = made[-1] if made else None  # the watchdog's handle on the socket
        return resp


class _Watchdog:
    """A hard wall-clock limit on a response: when it runs out the socket is shut down, which wakes a read that
    is stuck (a per-read timeout alone lets a server that trickles a byte at a time hold a thread for ever)."""

    def __init__(self, resp, seconds: float):
        self.conn = getattr(resp, "podcast_conn", None)
        self.fired = False
        self.timer = threading.Timer(seconds, self._cut) if self.conn is not None else None
        if self.timer is not None:
            self.timer.daemon = True

    def _cut(self) -> None:
        self.fired = True
        try:
            self.conn.cut_socket.shutdown(socket.SHUT_RDWR)
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        if self.timer is not None:
            self.timer.start()
        return self

    def __exit__(self, *exc):
        if self.timer is not None:
            self.timer.cancel()
        return False


class _Redirect(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(urllib.parse.urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener() -> urllib.request.OpenerDirector:
    """No proxy handler (a proxy would hide the address that is dialled)."""
    o = urllib.request.OpenerDirector()
    for handler in (_Redirect(), _Https(), urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
        o.add_handler(handler)
    return o


def _open(url: str, op):
    check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "harnessgram-radio", "Accept": "*/*"})
    try:
        return (op or opener()).open(req, timeout=FETCH_TIMEOUT)
    except PodcastError:
        raise
    except Exception:
        raise PodcastError("fetch") from None


def _ctype(resp) -> str:
    try:
        return resp.headers.get_content_type() or ""
    except Exception:
        return ""


def fetch(url: str, limit: int = MAX_FEED_BYTES, op=None, clock: Callable[[], float] = time.monotonic,
          audio_stops: bool = False) -> Tuple[str, bytes]:
    """(content type, body) of an https page of at most `limit` bytes within FETCH_DEADLINE seconds. With
    `audio_stops`, an audio answer returns at once with an empty body (the link is an episode itself)."""
    resp = _open(url, op)
    with resp, _Watchdog(resp, FETCH_DEADLINE) as dog:
        ctype = _ctype(resp)
        if audio_stops and ctype.startswith("audio/"):
            return ctype, b""
        size = resp.headers.get("Content-Length", "")
        if size.isdigit() and int(size) > limit:
            raise PodcastError("too_big")
        started, body = clock(), b""
        while True:
            try:
                chunk = resp.read(min(65536, limit + 1 - len(body)))
            except Exception:
                raise PodcastError("fetch") from None
            if dog.fired or clock() - started > FETCH_DEADLINE:
                raise PodcastError("fetch")
            if not chunk:
                return ctype, body
            body += chunk
            if len(body) > limit:
                raise PodcastError("too_big")


def download(url: str, dest: Path, limit: int = MAX_EPISODE_BYTES, op=None,
             clock: Callable[[], float] = time.monotonic, cancel: Optional[threading.Event] = None) -> int:
    """An episode into `dest` (written beside it and renamed when whole). Refuses a page that is not audio, one
    larger than `limit` and one that takes longer than DOWNLOAD_DEADLINE; `cancel` stops it at the next chunk.
    Nothing is left behind on failure."""
    resp = _open(url, op)
    part = dest.with_name(dest.name + ".part")
    with resp, _Watchdog(resp, DOWNLOAD_DEADLINE) as dog:
        ctype = _ctype(resp)
        if not (ctype.startswith("audio/") or ctype in ("application/octet-stream", "video/mp4", "application/ogg")):
            raise PodcastError("fetch")
        size = resp.headers.get("Content-Length", "")
        if size.isdigit() and int(size) > limit:
            raise PodcastError("too_big")
        started, total = clock(), 0
        try:
            with open(part, "wb") as out:
                os.chmod(part, 0o600)
                while True:
                    chunk = resp.read(min(65536, limit + 1 - total))
                    if dog.fired or clock() - started > DOWNLOAD_DEADLINE or (cancel is not None and cancel.is_set()):
                        raise PodcastError("fetch")
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise PodcastError("too_big")
                    out.write(chunk)
            if not total:
                raise PodcastError("fetch")
            os.replace(part, dest)
        except PodcastError:
            part.unlink(missing_ok=True)
            raise
        except Exception:
            part.unlink(missing_ok=True)
            raise PodcastError("fetch") from None
    return total


# --- reading a feed ---------------------------------------------------------------------------------------

def plain(value: object, limit: int) -> str:
    text = html.unescape(_TAGS.sub(" ", value if isinstance(value, str) else ""))
    text = " ".join(text.replace("‏", "").replace("‎", "").split())
    return text[:limit].rstrip()


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(parent, name: str) -> Optional[ET.Element]:
    for c in parent:
        if _local(c.tag) == name:
            return c
    return None


def _text(parent, name: str) -> str:
    c = _child(parent, name)
    return (c.text or "").strip() if c is not None else ""


def _seconds(value: str) -> int:
    try:
        parts = [int(p) for p in value.strip().split(":")]
    except ValueError:
        return 0
    if not 1 <= len(parts) <= 3 or any(p < 0 for p in parts):
        return 0
    total = 0
    for p in parts:
        total = total * 60 + p
    return min(total, 24 * 3600)


def parse_feed(data: bytes, feed_url: str = "", feed_name: str = "") -> List[dict]:
    """The newest episodes of an RSS feed: [{id, title, description, audio_url, published, seconds, explicit,
    feed}], at most MAX_EPISODES, newest first. PodcastError("feed") for anything that is not a plain RSS feed
    (malformed, a DTD or entity declaration, no channel)."""
    if not isinstance(data, (bytes, bytearray)) or b"\x00" in data or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", data, re.I):
        raise PodcastError("feed")
    try:
        root = ET.fromstring(bytes(data))
    except (ET.ParseError, ValueError, RecursionError):
        raise PodcastError("feed") from None
    channel = _child(root, "channel") if _local(root.tag) == "rss" else None
    if channel is None:
        raise PodcastError("feed")
    out: List[dict] = []
    for item in channel:
        if _local(item.tag) != "item":
            continue
        enc = _child(item, "enclosure")
        audio = (enc.get("url") or "").strip() if enc is not None else ""
        kind = (enc.get("type") or "") if enc is not None else ""
        try:
            check_url(audio)
        except PodcastError:
            continue
        if not (kind.startswith("audio/") or _AUDIO_EXT.search(urllib.parse.urlsplit(audio).path)):
            continue
        guid = _text(item, "guid") or audio
        explicit = (_text(item, "explicit") or _text(channel, "explicit")).lower() in ("yes", "true", "explicit")
        when = _text(item, "pubDate")
        try:
            stamp = email.utils.parsedate_to_datetime(when).timestamp() if when else 0.0
        except (TypeError, ValueError, IndexError, OverflowError):
            stamp = 0.0
        desc = _text(item, "description") or _text(item, "summary") or _text(item, "subtitle")
        raw_title = plain(_text(item, "title"), MAX_TITLE)
        out.append({"id": hashlib.sha256(f"{feed_url}\0{guid}".encode()).hexdigest()[:16], "raw_title": raw_title,
                    "title": raw_title or plain(urllib.parse.unquote(
                        urllib.parse.urlsplit(audio).path.rsplit("/", 1)[-1]), MAX_TITLE) or "?",
                    "description": plain(desc, MAX_DESC), "audio_url": audio, "published": stamp,
                    "seconds": _seconds(_text(item, "duration")), "explicit": explicit, "feed": feed_name})
    if all(e["published"] for e in out):
        out.sort(key=lambda e: -e["published"])
    return out[:MAX_EPISODES]


def feeds(cfg: Config) -> List[dict]:
    """The owner's configured feeds: [{name, url}] (malformed entries are left out)."""
    out = []
    for v in getattr(cfg, "radio_podcasts", None) or []:
        if isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"].strip() and isinstance(v.get("url"), str):
            try:
                check_url(v["url"])
            except PodcastError:
                continue
            out.append({"name": plain(v["name"], 60), "url": v["url"]})
    return out[:20]


def find_feed(cfg: Config, ref: str) -> Optional[dict]:
    ref = " ".join(str(ref).split()).casefold()
    return next((f for f in feeds(cfg) if f["name"].casefold() == ref), None)


def episodes(cfg: Config, feed: dict, fetcher: Optional[Callable] = None) -> List[dict]:
    _ctype_, body = (fetcher or fetch)(feed["url"])
    return parse_feed(body, feed["url"], feed["name"])


# --- the suitability gate ----------------------------------------------------------------------------------

def _checker_path(cfg: Config) -> Path:
    from . import radio, radio_suitability
    custom = radio.settings(cfg).get("podcast_checker") or ""
    return Path(os.path.expanduser(custom)) if custom else radio_suitability.SHARED_CHECKER


def _checker_says_ok(path: Path, text: str) -> bool:
    """The shared child-suitability checker on `text` (it reads a temporary file; nothing is printed or sent).
    False when it rejects, times out or cannot run."""
    import sys
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", suffix=".txt") as source:
            source.write(text)
            source.flush()
            result = subprocess.run([sys.executable, "-I", str(path), source.name], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


_LETTERS_OUTSIDE = re.compile("[^\\W\\d_a-zA-Z\u00C0-\u024F\u0590-\u05FF]")


def gate(cfg: Config, title: str, description: str, explicit: bool = False) -> None:
    """An episode is queued, and played, only if its text passes. PodcastError: explicit | notext | rules |
    unsuitable.
    - an explicit episode is refused;
    - there must be text to judge: a title or a description that the feed or the page itself gave (a file name
      or a placeholder does not count);
    - the shared checker (`radio.podcast_checker`, by default ~/.agents/content-suitability/check.py) must exist
      and accept the text: with no checker nothing is queued;
    - the shared checker matches Hebrew, so any text with Latin letters must also pass the portable word list of
      radio_suitability (which has English words), and text in any other script is refused: no rule covers it;
    - the radio's own rule file (`radio.room_suitability_file`) is applied as well when it is configured, and a
      configured file that cannot be read refuses everything."""
    from . import radio, radio_room, radio_suitability
    if explicit:
        raise PodcastError("explicit")
    text = " ".join(f"{title or ''} {description or ''}".split())
    if not text or text == "?":
        raise PodcastError("notext")
    path = _checker_path(cfg)
    if not path.is_file():
        raise PodcastError("rules")
    try:
        rules = radio_room.suitability(radio.settings(cfg).get("room_suitability_file") or "")
    except radio_room.RoomError:
        raise PodcastError("rules") from None
    normalized = _SUIT_NORM.sub("", text.replace("\u05BE", " "))
    if _LETTERS_OUTSIDE.search(text):
        raise PodcastError("unsuitable")  # a script no rule covers
    if not _checker_says_ok(path, text):
        raise PodcastError("unsuitable")
    if re.search("[a-zA-Z]", text) and radio_suitability.FALLBACK.search(normalized):
        raise PodcastError("unsuitable")
    if rules is not None and rules.search(normalized):
        raise PodcastError("unsuitable")


def episode_text(item: dict) -> Tuple[str, str]:
    """(title, description) of a queued item as the feed or the page gave them."""
    return str(item.get("raw_title", item.get("title")) or ""), str(item.get("desc") or "")


# --- the queue file -----------------------------------------------------------------------------------------

def folder() -> Path:
    from . import radio
    return private_dir(radio.folder() / "podcast")


def cache_dir() -> Path:
    return private_dir(folder() / "cache")


def _blank() -> dict:
    return {"items": [], "paused": False, "skip": 0, "stop": 0, "status": {}}


def _read() -> dict:
    try:
        data = json.loads((folder() / "queue.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _blank()
    if not isinstance(data, dict):
        return _blank()
    out = _blank()
    items = data.get("items")
    out["items"] = [i for i in items if isinstance(i, dict) and ITEM_ID.fullmatch(str(i.get("id")))
                    and isinstance(i.get("url"), str)][:MAX_QUEUE] if isinstance(items, list) else []
    out["paused"] = data.get("paused") is True
    for k in ("skip", "stop"):
        out[k] = data[k] if type(data.get(k)) is int else 0
    out["status"] = data["status"] if isinstance(data.get("status"), dict) else {}
    return out


@contextmanager
def _locked():
    with open(folder() / "queue.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def update(fn: Callable[[dict], object]):
    """Read, change and write the queue file as one step, for every process that shares it."""
    with _locked():
        st = _read()
        result = fn(st)
        write_private(folder() / "queue.json", json.dumps(st, ensure_ascii=False))
        return result


def add_episode(cfg: Config, ep: dict, now: Optional[float] = None) -> dict:
    """Queue one parsed episode after the suitability gate. PodcastError: explicit | notext | rules | unsuitable |
    url | dup | full."""
    raw = ep.get("raw_title", ep.get("title", ""))
    gate(cfg, raw, ep.get("description", ""), bool(ep.get("explicit")))
    check_url(ep.get("audio_url"))
    item = {"id": ep["id"], "title": plain(ep.get("title"), MAX_TITLE) or "?", "raw_title": plain(raw, MAX_TITLE),
            "desc": plain(ep.get("description"), MAX_DESC), "feed": plain(ep.get("feed"), 60),
            "url": ep["audio_url"], "seconds": int(ep.get("seconds") or 0), "added": int(now or time.time())}

    def put(st: dict):
        if any(i["id"] == item["id"] for i in st["items"]):
            raise PodcastError("dup")
        if len(st["items"]) >= MAX_QUEUE:
            raise PodcastError("full")
        st["items"].append(item)
    update(put)
    return item


_META = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR = re.compile(r"""([a-zA-Z:_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_HTML_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_AUDIO_TAG = re.compile(r"<(?:audio|source)\b[^>]*>", re.I)


def parse_page(data: bytes, page_url: str) -> Optional[dict]:
    """One episode from an episode page: its title and description (og: or meta tags) and its audio address
    (og:audio, an <audio> or <source> tag). None when the page names no https audio."""
    try:
        text = bytes(data).decode("utf-8", "replace")[:MAX_FEED_BYTES]
    except Exception:
        return None
    meta: Dict[str, str] = {}
    for tag in _META.findall(text):
        attrs = {m.group(1).lower(): html.unescape(m.group(2) if m.group(2) is not None else m.group(3))
                 for m in _ATTR.finditer(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key and "content" in attrs:
            meta.setdefault(key, attrs["content"])
    audio = meta.get("og:audio:secure_url") or meta.get("og:audio:url") or meta.get("og:audio") or ""
    if not audio:
        for tag in _AUDIO_TAG.findall(text):
            attrs = {m.group(1).lower(): html.unescape(m.group(2) if m.group(2) is not None else m.group(3))
                     for m in _ATTR.finditer(tag)}
            if attrs.get("src"):
                audio = attrs["src"]
                break
    audio = urllib.parse.urljoin(page_url, audio.strip()) if audio else ""
    try:
        check_url(audio)
    except PodcastError:
        return None
    page_title = _HTML_TITLE.search(text)
    title = plain(meta.get("og:title") or (page_title.group(1) if page_title else ""), MAX_TITLE)
    desc = plain(meta.get("og:description") or meta.get("description") or "", MAX_DESC)
    if not (title or desc):
        return None
    return {"id": hashlib.sha256(f"{page_url}\0{audio}".encode()).hexdigest()[:16], "raw_title": title, "title": title or "?",
            "description": desc, "audio_url": audio, "seconds": 0, "explicit": False, "feed": ""}


def episode_from_link(link: str, fetcher: Optional[Callable] = None) -> dict:
    """A pasted link: a feed gives its latest episode; an episode page gives its title, description and audio.
    A bare audio file has no text to judge, so it is refused (PodcastError("notext")): paste its feed or page."""
    check_url(link)
    ctype, body = fetch(link, audio_stops=True) if fetcher is None else fetcher(link)
    if ctype.startswith("audio/"):
        raise PodcastError("notext")
    try:
        found = parse_feed(body, link, "")
    except PodcastError:
        found = []
    if found:
        return found[0]
    page = parse_page(body, link) if ctype in ("text/html", "application/xhtml+xml") or b"<html" in body[:2000].lower() else None
    if page is None:
        raise PodcastError("feed")
    return page


def add_link(cfg: Config, link: str, fetcher: Optional[Callable] = None) -> dict:
    return add_episode(cfg, episode_from_link(link, fetcher))


def add_latest(cfg: Config, ref: str, fetcher: Optional[Callable] = None) -> dict:
    """`/podcast <feed name or link>`: the latest episode of a configured feed (by name) or of a link."""
    feed = find_feed(cfg, ref)
    if feed is None:
        return add_link(cfg, ref.strip(), fetcher)
    found = episodes(cfg, feed, fetcher)
    if not found:
        raise PodcastError("feed")
    return add_episode(cfg, found[0])


def add_picked(cfg: Config, feed_name: str, episode_id: str, fetcher: Optional[Callable] = None) -> dict:
    """The Cockpit's pick: an episode id of a configured feed (re-read from the feed, never taken from the page)."""
    feed = find_feed(cfg, feed_name)
    if feed is None or not ITEM_ID.fullmatch(str(episode_id)):
        raise PodcastError("unknown")
    ep = next((e for e in episodes(cfg, feed, fetcher) if e["id"] == episode_id), None)
    if ep is None:
        raise PodcastError("unknown")
    return add_episode(cfg, ep)


def remove(item_id: str) -> bool:
    return bool(update(lambda st: _drop(st, item_id)))


def _drop(st: dict, item_id: str) -> bool:
    before = len(st["items"])
    st["items"] = [i for i in st["items"] if i["id"] != item_id]
    return len(st["items"]) != before


def control(op: str) -> None:
    """play | pause | skip | stop. Stop empties the queue as well; the radio's job reacts within a second."""
    if op not in PLAYER_OPS:
        raise PodcastError("bad")

    def apply(st: dict):
        if op == "play":
            st["paused"] = False
        elif op == "pause":
            st["paused"] = True
        elif op == "skip":
            st["skip"] += 1
        else:
            st["items"], st["paused"] = [], False
            st["stop"] += 1
    update(apply)


def view(cfg: Config, now: Optional[float] = None) -> dict:
    """What the Cockpit shows: the feed names, the queue, the paused flag and the line that is playing."""
    now = time.time() if now is None else now
    st = _read()
    status = st["status"]
    live = isinstance(status.get("at"), (int, float)) and 0 <= now - status["at"] <= STATUS_FRESH
    keep = ("state", "id", "title", "feed", "position", "total", "error")
    now_playing = {k: status[k] for k in keep if k in status} if live and status.get("state") in (
        "playing", "paused", "downloading", "error") else None
    return {"feeds": [f["name"] for f in feeds(cfg)], "paused": st["paused"], "running": live,
            "queue": [{k: i.get(k) for k in ("id", "title", "feed", "seconds")} for i in st["items"]],
            "now": now_playing}


# --- the cache ------------------------------------------------------------------------------------------------

_CACHED = re.compile(r"[0-9a-f]{16}\.audio(?:\.part)?")


def prune_cache(cache: Path, max_bytes: int = CACHE_MAX_BYTES, days: float = CACHE_KEEP_DAYS,
                keep: frozenset = frozenset(), now: Optional[float] = None) -> None:
    """Remove the cache's own files (by exact name, regular files only) that are old, then the oldest until the
    rest fits `max_bytes`. A file named in `keep` stays."""
    now = time.time() if now is None else now
    found = []
    for entry in os.scandir(cache) if cache.is_dir() else []:
        if _CACHED.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
            st = entry.stat(follow_symlinks=False)
            found.append([st.st_mtime, st.st_size, entry.path, entry.name])
    found.sort()
    total = sum(f[1] for f in found)
    for mtime, size, path, name in found:
        if name in keep:
            continue
        if now - mtime > days * 86400 or total > max_bytes:
            try:
                os.unlink(path)
                total -= size
            except OSError:
                pass


# --- decoding and playing ---------------------------------------------------------------------------------------

FORMATS = "mp3,mov,mp4,m4a,ogg,wav,aac,flac,matroska,webm,opus,adts,ac3"   # audio containers only: no playlists


def _spawn(path: str):
    return subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file", "-format_whitelist", FORMATS,
                             "-i", path, "-vn",
                             "-ac", "1", "-ar", str(RATE), "-f", "s16le", "pipe:1"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


class Feed:
    """An episode decoded by a child process, buffered a few seconds ahead. The mixer calls `read` every 20 ms:
    audio when there is some, b"" to hold (paused, or the decoder is behind), None when it is over."""

    def __init__(self, path: str, spawn: Callable = _spawn):
        self.proc = spawn(path)
        self.lock = threading.Lock()
        self.buf: collections.deque = collections.deque()
        self.size = 0
        self.eof = False
        self.paused = False
        self.stopped = False
        self.played = 0
        self.room = threading.Event()
        self.room.set()
        threading.Thread(target=self._pump, daemon=True, name="radio-podcast-decode").start()

    def _pump(self) -> None:
        cap = 2 * RATE * BUFFER_SECONDS
        try:
            while not self.stopped:
                with self.lock:
                    full = self.size >= cap
                if full:
                    self.room.clear()
                    self.room.wait(0.2)
                    continue
                data = self.proc.stdout.read(8192)
                if not data:
                    break
                with self.lock:
                    self.buf.append(data)
                    self.size += len(data)
        except Exception:  # noqa: BLE001 - a broken decoder ends the episode
            pass
        self.eof = True

    def read(self, nbytes: int) -> Optional[bytes]:
        if self.stopped:
            return None
        if self.paused:
            return b""
        with self.lock:
            if not self.size:
                return None if self.eof else b""
            out = b""
            while self.buf and len(out) < nbytes:
                head = self.buf.popleft()
                need = nbytes - len(out)
                if len(head) > need:
                    self.buf.appendleft(head[need:])
                    head = head[:need]
                out += head
            self.size -= len(out)
            self.played += len(out)
        self.room.set()
        return out

    def seconds(self) -> float:
        return self.played / (2.0 * RATE)

    def stop(self) -> None:
        self.stopped = True
        self.room.set()
        try:
            self.proc.kill()
        except Exception:  # noqa: BLE001
            pass


class _Download:
    """One episode being fetched on its own thread, so a slow download never holds up stop, skip or remove."""

    def __init__(self, item: dict, dest: Path, get: Callable):
        self.item, self.dest = item, dest
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.error = ""
        threading.Thread(target=self._run, args=(get,), daemon=True, name="radio-podcast-download").start()

    def _run(self, get: Callable) -> None:
        try:
            if not self.dest.exists() or self.dest.stat().st_size == 0:
                get(self.item["url"], self.dest, cancel=self.cancel)
        except PodcastError as e:
            self.error = e.kind
        except Exception:  # noqa: BLE001
            self.error = "decode"
        finally:
            if self.cancel.is_set():
                self.dest.unlink(missing_ok=True)
            self.done.set()


class Player:
    """The radio job's side: one `step()` a second turns the queue file into sound on the mixer. While an
    episode is audible a marker file says so (radio.private_audio_on), so the YouTube mirror, which reads the
    home radio's audio, holds instead of carrying it."""

    def __init__(self, mixer, lock, blocked: Callable[[], bool] = lambda: False,
                 get: Optional[Callable] = None, make: Callable[[str], Feed] = Feed,
                 clock: Callable[[], float] = time.time, cfg: Optional[Config] = None):
        self.mixer, self.lock, self.blocked, self.make, self.clock = mixer, lock, blocked, make, clock
        self.cfg = cfg  # the suitability rules are applied again with it just before an episode is played
        self.get = get or (lambda url, dest, cancel=None: download(url, dest, cancel=cancel))
        self.cur: Optional[dict] = None
        self.feed: Optional[Feed] = None
        self.dl: Optional[_Download] = None
        self.path: Optional[Path] = None
        st = _read()
        self.seen = (st["skip"], st["stop"])
        prune_cache(cache_dir())
        self._mark(False)
        update(lambda s: s.update(status={}))

    def active(self) -> bool:
        return self.feed is not None

    @staticmethod
    def _mark_path() -> Path:
        from . import radio
        return radio.folder() / radio.PRIVATE_AUDIO

    def _mark(self, on: bool) -> None:
        try:
            if on:
                write_private(self._mark_path(), "")
            else:
                self._mark_path().unlink(missing_ok=True)
        except OSError:
            pass

    def _detach(self) -> None:
        feed = self.feed
        if feed is not None:
            feed.stop()
            with self.lock:
                if self.mixer.show is feed:
                    self.mixer.show = None
                    self.mixer.gap = int(self.mixer.GAP * RATE)
        self.feed = None
        self._mark(False)

    def _finish(self, drop: bool) -> None:
        item = self.cur
        self._detach()
        if self.dl is not None:
            self.dl.cancel.set()
            self.dl = None
        if self.path is not None:
            self.path.unlink(missing_ok=True)
        self.cur, self.path = None, None
        if item is not None and drop:
            update(lambda st: _drop(st, item["id"]))
        prune_cache(cache_dir())

    def stop_all(self) -> None:
        """The radio is going quiet (mirroring off, the job ending): nothing plays; the queue stays."""
        self._finish(drop=False)

    def _publish(self, state: str, error: str = "") -> None:
        item = self.cur or {}
        status = {"state": state, "at": int(self.clock()), "id": item.get("id"), "title": item.get("title"),
                  "feed": item.get("feed"), "total": item.get("seconds") or 0,
                  "position": int(self.feed.seconds()) if self.feed is not None else 0}
        if error:
            status["error"] = error
        update(lambda st: st.update(status=status))

    def _begin(self, item: dict) -> None:
        """Start fetching the head of the queue; `step` goes on when the download is done. The suitability rules
        are applied again first, so a word list changed since the episode was queued still holds."""
        self.cur = item
        if not self._still_suitable(item):
            return
        self.path = cache_dir() / (item["id"] + ".audio")
        self.dl = _Download(item, self.path, self.get)
        self._publish("downloading")

    def _attach(self) -> bool:
        """The download is done: decode it and put it on the mixer. False when it failed."""
        dl, self.dl = self.dl, None
        if dl.error:
            self._failed(dl.error)
            return False
        if self.cur is not None and not self._still_suitable(self.cur):  # the last look, right before it is heard
            return False
        try:
            feed = self.make(str(self.path))
        except PodcastError as e:
            self._failed(e.kind)
            return False
        except Exception:  # noqa: BLE001 - one bad episode never stops the radio
            self._failed("decode")
            return False
        feed.paused = _read()["paused"]
        self._mark(not feed.paused)  # before it can be heard: the mirror must already be holding
        self.feed = feed
        with self.lock:
            self.mixer.show = feed
        return True

    def _still_suitable(self, item: dict) -> bool:
        if self.cfg is None:
            return True
        title, desc = episode_text(item)
        try:
            gate(self.cfg, title, desc)
        except PodcastError as e:
            self._failed(e.kind)
            return False
        return True

    def _failed(self, kind: str) -> None:
        self._finish(drop=True)
        self._publish("error", kind)

    def step(self) -> None:
        st = _read()
        stop, skip = st["stop"] != self.seen[1], st["skip"] != self.seen[0]
        self.seen = (st["skip"], st["stop"])
        if stop:
            self._finish(drop=False)
        elif skip:
            if self.cur is not None:
                self._finish(drop=True)
            elif st["items"]:
                update(lambda s: _drop(s, s["items"][0]["id"]) if s["items"] else None)
        if stop or skip:
            st = _read()
        if self.cur is not None and not any(i["id"] == self.cur["id"] for i in st["items"]):
            self._finish(drop=False)  # taken out of the queue while it loaded or played
        if self.dl is not None:
            if not self.dl.done.is_set():
                self._publish("downloading")
                return
            if not self._attach():
                return
        if self.feed is not None:
            self.feed.paused = st["paused"]  # a paused episode leaves the mix: the radio's talk goes on
            self._mark(not st["paused"])
            if self.mixer.show is not self.feed:  # the mixer let it go: it played to the end
                bad = self.feed.played == 0
                self._finish(drop=True)
                st = _read()
                if bad:
                    self._publish("error", "decode")
                    return
            else:
                self._publish("paused" if st["paused"] else "playing")
                return
        if self.cur is None and st["items"] and not st["paused"] and not self.blocked():
            self._begin(st["items"][0])
            return
        if self.cur is None:
            prev = (_read()["status"] or {})
            if prev.get("state") != "error" or self.clock() - prev.get("at", 0) > 8:
                self._publish("idle")


# --- the /podcast command -------------------------------------------------------------------------------------

_WORDS = {"stop": "stop", "עצור": "stop", "עצירה": "stop", "skip": "skip", "next": "skip", "דלג": "skip",
          "pause": "pause", "השהה": "pause", "play": "play", "resume": "play", "המשך": "play"}


def telegram_command(cfg: Config, arg: str, reply: Callable[[str], None], fetcher: Optional[Callable] = None) -> str:
    """/podcast [<feed name or link> | play | pause | skip | stop]: the owner's command in the home group. With a
    feed name or a link it queues the latest episode; with nothing it says what is playing and what waits."""
    from . import radio
    from .i18n import t
    lang = radio.settings(cfg)["lang"]
    word = " ".join(arg.split())

    def work():
        try:
            if not word:
                v = view(cfg)
                now = v["now"]
                line = (t("podcast_now", lang=lang, title=now["title"] or "?") if now and now.get("state") in (
                    "playing", "paused", "downloading") else t("podcast_idle", lang=lang))
                reply(line + (("\n" + t("podcast_waiting", lang=lang, count=len(v["queue"]))) if v["queue"] else ""))
                return
            op = _WORDS.get(word.lower())
            if op:
                control(op)
                reply(t("podcast_" + op, lang=lang))
                return
            item = add_latest(cfg, word, fetcher)
            reply(t("podcast_queued", lang=lang, title=item["title"])
                  + ("" if radio.job_pid() else "\n" + t("podcast_radio_off", lang=lang)))
        except PodcastError as e:
            reply(t("podcast_err_" + e.kind if ("podcast_err_" + e.kind) in _error_keys() else "podcast_err_fetch", lang=lang))
        except Exception:  # noqa: BLE001
            reply(t("podcast_err_fetch", lang=lang))
    threading.Thread(target=work, daemon=True, name="radio-podcast-command").start()
    return "podcast"


def _error_keys() -> set:
    from .i18n import EN
    return {k for k in EN if k.startswith("podcast_err_")}
