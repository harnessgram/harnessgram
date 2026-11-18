"""Bounded, read-only access to the owner's YouTube playlists for the Cockpit."""
from __future__ import annotations
import json, subprocess, threading, time, urllib.parse, urllib.request

from .config import read_map

CACHE_SECONDS, MAX_PLAYLISTS, MAX_LIKED = 1800, 50, 50
API = "https://www.googleapis.com/youtube/v3/"
_lock, _cached = threading.Lock(), None

def clear_cache():
    global _cached
    with _lock: _cached = None

def _token(cfg, runner=subprocess.run):
    command = (getattr(cfg, "youtube", None) or {}).get("account_token_command") or []
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command): raise ValueError("not_configured")
    result = runner(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False)
    token = (result.stdout or "").strip()
    if result.returncode or not token or len(token) > 4096 or any(c.isspace() for c in token): raise ValueError("token_unavailable")
    return token

def _get(token, resource, params, opener=urllib.request.urlopen):
    request = urllib.request.Request(API + resource + "?" + urllib.parse.urlencode(params), headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    with opener(request, timeout=10) as response: body = response.read(512 * 1024 + 1)
    if len(body) > 512 * 1024: raise ValueError("response_too_large")
    value = json.loads(body)
    if not isinstance(value, dict): raise ValueError("bad_response")
    return value

def _thumbnail(snippet):
    for key in ("medium", "default", "high"):
        url = (((snippet.get("thumbnails") or {}).get(key) or {}).get("url", ""))
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == "https" and parsed.hostname == "i.ytimg.com": return url[:500]
    return ""

def _playlist(row):
    pid, snippet, details = row.get("id"), row.get("snippet"), row.get("contentDetails")
    if not isinstance(pid, str) or not pid or len(pid) > 128 or not isinstance(snippet, dict): return None
    count = (details or {}).get("itemCount", 0) if isinstance(details, dict) else 0
    return {"title": " ".join(str(snippet.get("title") or "").split())[:160], "id": pid,
            "item_count": max(0, min(int(count), 1000000)), "thumbnail_url": _thumbnail(snippet)}

def load(cfg, *, now=None, runner=subprocess.run, opener=urllib.request.urlopen):
    global _cached
    stamp = time.monotonic() if now is None else now
    with _lock:
        if _cached and stamp - _cached[0] < CACHE_SECONDS: return _cached[1]
        try:
            token = _token(cfg, runner)
            raw = _get(token, "playlists", {"part":"snippet,contentDetails", "mine":"true", "maxResults":MAX_PLAYLISTS}, opener)
            playlists = [p for p in (_playlist(x) for x in raw.get("items", [])[:MAX_PLAYLISTS]) if p]
            liked = []
            for list_id in ("LM", "LL"):
                try: rows = _get(token, "playlistItems", {"part":"snippet,contentDetails", "playlistId":list_id, "maxResults":MAX_LIKED}, opener).get("items", [])
                except Exception: continue
                for row in rows[:MAX_LIKED]:
                    snippet, video_id = row.get("snippet") or {}, (row.get("contentDetails") or {}).get("videoId") or ""
                    if isinstance(video_id, str) and video_id and len(video_id) <= 32:
                        liked.append({"title":" ".join(str(snippet.get("title") or "").split())[:160], "id":video_id, "thumbnail_url":_thumbnail(snippet)})
                if liked: break
            answer = {"ok":True, "playlists":playlists, "liked_music":liked}
        except Exception as exc:
            return {"ok":False, "reason":(str(exc) if isinstance(exc, ValueError) else "unavailable")[:40]}
        _cached = (stamp, answer)
        return answer
