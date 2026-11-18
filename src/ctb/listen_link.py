"""The private "Listen in Safari" link: the team radio on a plain web page that keeps playing in the background.

The Cockpit Mini App stops its audio when it closes or Telegram goes to the background. Safari keeps native HLS
audio running (and shows lock-screen controls), so the owner opens a minimal listening page there.

That page cannot use Telegram's initData, so it carries its own capability: an unguessable listening token in the
URL path (`/listen/<token>`); `/listen/<token>/now.json` names what the radio is saying (radio_now.py). The bridge keeps only the SHA-256 of each token in the private `listen-link.json` of
its state directory, so a stolen state file does not yield a working link. A token reaches exactly four things:
the page, the radio playlist, its segments and the current title. It never reaches an API. The owner revokes every token in one tap
from Settings, and switching the `listen_link` add-on off revokes them too.

Pressing Play is also the signal to start the radio when it is off (radio_play.py): a playlist request with a valid
token and no fresh audio starts the station in private mode and is answered with a live playlist of silent
segments (`quiet-<n>.ts`, radio_quiet.py) until the first real segments exist.

Each tap on the Cockpit button mints a fresh token (the bridge cannot show an old one again, it only has the hash),
and at most MAX_TOKENS stay valid at once; the oldest is dropped first. A token expires TTL seconds after it was made.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
import time

from .config import locked_map, read_map, state_dir

STORE = "listen-link.json"
MAX_TOKENS = 5
TTL = 30 * 86400  # a token expires after 30 days, and can be revoked sooner
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")  # secrets.token_urlsafe(32)
PATH = re.compile(r"/listen/([^/?#]{1,128})(?:/(live\.m3u8|(?:segment|quiet)-[0-9]{1,20}\.ts|now\.json))?")
PAGE_CSP = ("default-src 'none'; script-src 'nonce-{n}'; style-src 'nonce-{n}'; img-src data:; media-src 'self'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _store():
    return state_dir() / STORE


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _live(row: object, now: float) -> bool:
    return (isinstance(row, dict) and isinstance(row.get("h"), str) and isinstance(row.get("at"), int)
            and 0 <= now - row["at"] < TTL)


def mint() -> str:
    """A new listening token. Only its hash is stored; the token itself is returned once and never kept."""
    token = secrets.token_urlsafe(32)
    with locked_map(_store()) as data:
        now = time.time()
        rows = [r for r in data.get("tokens", []) if _live(r, now)]
        rows.append({"h": _digest(token), "at": int(now)})
        data["tokens"] = rows[-MAX_TOKENS:]
    return token


def valid(token: object) -> bool:
    """Whether this token is one of the active ones. Every stored hash is compared in constant time."""
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        return False
    want = _digest(token)
    ok, now = False, time.time()
    for row in read_map(_store()).get("tokens", []):
        if _live(row, now):
            ok |= hmac.compare_digest(row["h"], want)
    return ok


def active() -> int:
    now = time.time()
    return len([r for r in read_map(_store()).get("tokens", []) if _live(r, now)])


def revoke() -> int:
    """Revoke every token at once; returns how many were active."""
    if not read_map(_store()).get("tokens"):
        return 0
    with locked_map(_store()) as data:
        count = sum(1 for r in data.get("tokens", []) if _live(r, time.time()))
        data["tokens"] = []
    return count


def parse(path: str):
    """(token, file) of a /listen/ request path, or None when the path has any other shape. `file` is "" for the
    page itself. The query string is ignored; nothing else (dots, extra segments, encodings) matches."""
    path = path.split("?", 1)[0]
    m = PATH.fullmatch(path)
    return (m.group(1), m.group(2) or "") if m else None


# --- the page -------------------------------------------------------------------------------------------------

TEXT = {
    "he": {"station": "שידורי הקוקפיט", "play": "האזנה", "pause": "השהיה", "idle": "מוכן להאזנה",
           "loading": "ממתין לשמע…", "error": "ההאזנה נעצרה. אפשר לנסות שוב", "live": "בשידור חי", "wait": "הרדיו כבוי, ממתין לשידור…", "again": "מתחבר מחדש…",
           "revoked": "הקישור בוטל. אפשר לבקש קישור חדש מהקוקפיט"},
    "en": {"station": "Team radio", "play": "Listen", "pause": "Pause", "idle": "Ready to listen",
           "loading": "Waiting for audio…", "error": "Playback stopped. Try again", "live": "Live", "wait": "The radio is off, waiting for it…", "again": "Reconnecting…",
           "revoked": "This link was revoked. Ask the Cockpit for a new one"},
}
ARTWORK = ("data:image/svg+xml," + "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'%3E"
           "%3Crect width='512' height='512' rx='96' fill='%231f3a5f'/%3E%3Ccircle cx='256' cy='256' r='64' "
           "fill='%23f2f6fb'/%3E%3Cpath d='M130 256a126 126 0 0 1 252 0M86 256a170 170 0 0 1 340 0' fill='none' "
           "stroke='%23f2f6fb' stroke-width='28' stroke-linecap='round'/%3E%3C/svg%3E")

PAGE = """<!doctype html>
<html lang="{lang}" dir="{dir}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<meta name="color-scheme" content="light dark"><title>{station}</title>
<style nonce="{nonce}">
:root{{color-scheme:light dark;--bg:#f2f6fb;--fg:#12263f;--card:#fff;--mute:#5b6b80;--acc:#1f3a5f}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0e1621;--fg:#e8eef6;--card:#17212e;--mute:#93a3b8;--acc:#5b8fd6}}}}
html,body{{margin:0;height:100%;background:var(--bg);color:var(--fg);font:17px/1.4 -apple-system,system-ui,sans-serif}}
main{{min-height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;
padding:24px 16px;box-sizing:border-box;text-align:center}}
img{{width:min(60vw,240px);height:auto;border-radius:24px}}
h1{{margin:0;font-size:24px}}
#st{{color:var(--mute);min-height:1.4em}}
button{{font:inherit;font-weight:600;min-width:176px;min-height:56px;border:0;border-radius:28px;background:var(--acc);
color:#fff;padding:0 28px}}
</style></head><body><main>
<img id="art" alt="" src="{art}" width="240" height="240"><h1>{station}</h1>
<div id="st" role="status" aria-live="polite">{idle}</div>
<button id="btn" type="button">{play}</button>
<audio id="au" preload="none" playsinline></audio>
</main>
<script nonce="{nonce}">
(function(){{
var T={text},a=document.getElementById("au"),b=document.getElementById("btn"),s=document.getElementById("st");
var base=location.pathname.replace(/\\/+$/,""),url=base+"/live.m3u8",want=false,dead=false,tries=0,timer=0,stall=0,epoch=0,serial=0,state="paused";
var ms=("mediaSession" in navigator)?navigator.mediaSession:null,position=a.currentTime;
function active(){{return !a.paused&&!a.ended&&!a.error;}}
function paint(){{var running=active(),playing=running&&state==="playing";
 b.textContent=running?T.pause:T.play;b.setAttribute("aria-pressed",String(playing));
 s.textContent=dead?T.revoked:playing?T.live:state==="error"?T.error:running?T.loading:T.idle;
 if(ms)ms.playbackState=playing?"playing":running?"none":"paused";}}
function cancel(){{epoch++;clearTimeout(timer);clearTimeout(stall);}}
function load(){{a.src=url+"?r="+Date.now()+"-"+(++serial);a.load();position=a.currentTime;}}
function start(ticket){{if(ticket!==epoch||!want||dead)return;state="waiting";load();
 var p=a.play();if(p&&p.catch)p.catch(function(){{if(ticket!==epoch||!want)return;state="error";paint();recover();}});paint();}}
function play(){{if(dead)return;cancel();want=true;tries=0;start(epoch);}}
function pause(){{want=false;cancel();a.pause();state="paused";paint();}}
// Only failures/waiting while listening may retry. A Pause invalidates timers AND in-flight fetches/play promises.
function recover(){{
 if(!want||dead)return;clearTimeout(timer);var ticket=epoch;
 timer=setTimeout(function(){{
  if(ticket!==epoch||!want||dead)return;
  fetch(url+"?r="+Date.now(),{{cache:"no-store"}}).then(function(r){{
   if(ticket!==epoch||!want||dead)return;
   if(r.status===403){{dead=true;pause();return;}}
   if(r.status!==200){{tries++;recover();return;}}
   start(ticket);
  }}).catch(function(){{if(ticket===epoch&&want){{tries++;recover();}}}});
 }},Math.min(15000,1000*Math.pow(2,Math.min(tries,4))));}}
b.addEventListener("click",function(){{if(active())pause();else play();}});
a.addEventListener("play",function(){{state="waiting";paint();}});
a.addEventListener("error",function(){{state="error";paint();recover();}});
a.addEventListener("ended",function(){{want=false;cancel();state="paused";paint();}});
function progressing(){{cancel();tries=0;state="playing";paint();}}
a.addEventListener("playing",function(){{position=a.currentTime;if(active())progressing();}});
a.addEventListener("timeupdate",function(){{var next=a.currentTime;if(next>position&&active())progressing();position=next;}});
a.addEventListener("waiting",function(){{state="waiting";paint();position=a.currentTime;
 clearTimeout(stall);if(want)stall=setTimeout(recover,10000);}});
a.addEventListener("pause",function(){{if(a.paused){{want=false;cancel();state=a.error?"error":"paused";paint();}}}});
if(ms){{try{{ms.metadata=new MediaMetadata({{title:T.station,artist:"HarnessGram",
 artwork:[{{src:{art_js},sizes:"512x512",type:"image/svg+xml"}}]}});
 ms.setActionHandler("play",play);ms.setActionHandler("pause",pause);ms.setActionHandler("stop",pause);}}catch(e){{}}}}
paint();
}})();
</script></body></html>
"""


def render_page(lang: str, nonce: str) -> str:
    code = "he" if str(lang).startswith("he") else "en"
    t = TEXT[code]
    return PAGE.format(
        lang=code, dir="rtl" if code == "he" else "ltr", nonce=nonce, station=html.escape(t["station"]),
        idle=html.escape(t["idle"]), play=html.escape(t["play"]), art=html.escape(ARTWORK, quote=True),
        art_js=json.dumps(ARTWORK), text=json.dumps(t, ensure_ascii=False).replace("</", "<\\/"))


def csp(nonce: str) -> str:
    return PAGE_CSP.format(n=nonce)
