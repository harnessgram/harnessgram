"""The family board as a standalone A4 page in Hebrew (the "open as a page" fallback for the print view).

The panel serves it at `GET /family/print?auth=<token>` for a short-lived owner token; the page is built here from the
board's own view, every text escaped, with no images. Page 1 holds the week and the daily routine, page 2 the shopping
list and the meal plan."""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from html import escape
from typing import Optional

DAYS = ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "מוצאי שבת"]
TITLE = "הלוח המשפחתי שלנו"
NOTE = "סדר · בית נקי · אוכל מוכן · זמן משפחה"
CSS = """
:root{color-scheme:light}*{box-sizing:border-box}
body{margin:0;padding:12px 16px;background:#fff;color:#000;font:11pt/1.3 system-ui,"Segoe UI",Arial,sans-serif}
@page{size:A4;margin:10mm}
.bar{display:flex;justify-content:flex-end;margin-block-end:8px}
button{min-block-size:44px;padding-inline:18px;border:1px solid #000;border-radius:8px;background:#fff;color:#000;font:inherit;font-weight:700}
header{text-align:center;margin-block-end:6mm}h1{margin:0;font-size:20pt}header p{margin:2mm 0 0;font-size:10pt}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4mm;margin-block-end:4mm}
.low{grid-template-columns:repeat(2,minmax(0,1fr));break-before:page}
.box{border:1px solid #000;border-radius:2mm;padding:3mm;break-inside:avoid;min-inline-size:0}
h2{margin:0 0 2mm;font-size:12pt;border-block-end:1px solid #000;padding-block-end:1mm}
ul{list-style:none;margin:0;padding:0}li{display:flex;gap:2mm;align-items:flex-start;margin-block-end:1.5mm;overflow-wrap:anywhere}
.mark{flex:none;inline-size:3.5mm;block-size:3.5mm;border:1px solid #000;margin-block-start:.5mm}
@media(max-width:600px){.grid,.low{grid-template-columns:minmax(0,1fr)}}
@media print{.bar{display:none}body{padding:0}.grid{grid-template-columns:repeat(3,minmax(0,1fr))}.low{grid-template-columns:repeat(2,minmax(0,1fr))}}
"""


def _box(title: str, rows, mark: bool) -> str:
    items = "".join("<li>%s<span>%s</span></li>" % ('<i class="mark"></i>' if mark else "", escape(r["text"])) for r in rows)
    return '<section class="box"><h2>%s</h2><ul>%s</ul></section>' % (escape(title), items or "<li>—</li>")


def render(view: dict, nonce: str) -> bytes:
    week = "השבוע של " + ".".join(reversed(view["week_start"].split("-")))
    days = "".join(_box(DAYS[c["day"]], c["chores"], True) for c in view["days"])
    page = ('<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>%s</title>'
            '<style nonce="%s">%s</style></head><body>'
            '<div class="bar"><button type="button" id="print">הדפסה</button></div>'
            '<header><h1>%s</h1><p>%s</p><p>%s</p></header>'
            '<div class="grid">%s%s</div><div class="grid low">%s%s</div>'
            '<script nonce="%s">document.getElementById("print").addEventListener("click",function(){window.print();});</script>'
            '</body></html>') % (TITLE, nonce, CSS, TITLE, NOTE, escape(week), days, _box("שגרה יומית", view["daily"], True),
                                 _box("רשימת קניות", view["shop"], True), _box("תפריט שבועי", view["meals"], False), nonce)
    return page.encode("utf-8")


CSP = "default-src 'none'; style-src 'nonce-{n}'; script-src 'nonce-{n}'; frame-ancestors 'none'"

# --- the short-lived owner token for the page --------------------------------------------------------------------------

TTL = 600
_TOKEN = re.compile(r"fam1\.[0-9]{1,20}\.[0-9]{1,12}\.[0-9a-f]{64}")


def _sign(secret: str, owner: int, expires: int) -> str:
    key = hmac.new(secret.encode(), b"cockpit-family-print:v1", hashlib.sha256).digest()
    return hmac.new(key, ("fam1.%d.%d" % (owner, expires)).encode(), hashlib.sha256).hexdigest()


def token(secret: str, owner: int, expires: int) -> str:
    return "fam1.%d.%d.%s" % (owner, expires, _sign(secret, owner, expires))


def valid_token(value, secret: str, owner: int, now: Optional[float] = None) -> bool:
    if not secret or not owner or not isinstance(value, str) or not _TOKEN.fullmatch(value):
        return False
    _, who, expiry, _ = value.split(".")
    now = time.time() if now is None else now
    return int(who) == owner and now < int(expiry) <= now + TTL and hmac.compare_digest(value, token(secret, owner, int(expiry)))
