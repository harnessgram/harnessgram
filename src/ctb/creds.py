"""Credential-looking text: detection (refuse incoming), redaction (outgoing), and relay access."""
from __future__ import annotations

import html
import json
import os
import re
import socket
from pathlib import Path
from typing import Optional

# Things that look like credentials. Deliberately broad: a false refusal costs a retype,
# a leaked key costs a rotation.
SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY|gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{25,}"
    r"|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|(?<![0-9])[0-9]{7,}:[A-Za-z0-9_-]{30,}"
    r"|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|(?i:password|passwd|secret|token|api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*\S{12,}")

# Match the live progress-card redaction, without hiding normal paths and URLs.
LONG = re.compile(r"[A-Za-z0-9_+-]{32,}={0,2}")
BEARER = re.compile(r"(?i:bearer)\s+\S+")
EXTRA = BEARER  # backwards compatibility
FLAG = re.compile(r"(?i)((?:--?|\b)(?:token|secret|password|passwd|api[_-]?key|auth|key)[ =:]+)(\S{8,})")
HEX = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])")
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
URL_HOST = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]{0,63}://(?:[^/?#@\s]*@)?([^/?#@\s]+)")


def _git_sha(m: re.Match) -> bool:
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", m.group()):
        return False
    before = m.string[:m.start()].rsplit("\n", 1)[-1]
    after = m.string[m.end():].split("\n", 1)[0]
    return bool(
        re.search(r"(?i)\b(?:commit|checkout|sha|rev|git\s+(?:show|diff|reset|merge|cherry-pick|revert))"
                  r"\s*[:=]?\s*$", before)
        or re.search(r"/(?:commit|commits|tree|blob)/$", before)
        # git log --oneline (optionally with --graph) and git commit output.
        or (re.fullmatch(r"[ |*]*", before) and re.match(r"\s+\S", after))
        or (re.fullmatch(r"\[[^\]\n]+ ", before) and after.startswith("] ")))


# A labelled content digest ("sha256:<64 hex>", "@sha256:..." of an image, "md5 ...") is an identifier, not a
# credential: it stays. An unlabelled long hex run is still redacted (a token cannot be told from a hash).
_DIGEST_BEFORE = re.compile(r"(?i)(?:\bsha-?(?:1|224|256|384|512)|\bblake2[bs]?|\bmd5)\s*[:=]?\s*$")


def _digest(m: re.Match) -> bool:
    return bool(_DIGEST_BEFORE.search(m.string[max(0, m.start() - 16):m.start()]))


def looks_random(s: str) -> bool:
    return any(len(p) >= 32 and re.search(r"[a-z]", p) and re.search(r"[A-Z]", p)
               and re.search(r"[0-9]", p) for p in s.split("/"))


def _hide_long(m: re.Match) -> str:
    value = m.group()
    if UUID.fullmatch(value) or _git_sha(m):
        return value
    return "[redacted]" if looks_random(value) else value


def is_secret(text: str) -> bool:
    return bool(SECRET.search(text))


# Links that are for the owner only. A one-time secure-handoff form link (secure-secret-handoff) is
# Tailscale-only and short-lived; a Claude sign-in link is the owner's own login. Neither is a credential
# the bridge may keep: the owner's own copy has them whole, a team copy and the progress card never do.
# The YouTube sign-in page for the phone (yt-auth-phone) is the same kind of one-time Tailscale-only link.
HANDOFF = re.compile(r"https://[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.ts\.net(?::\d+)?/__(?:secure-handoff|yt-auth)/"
                     r"[0-9a-f]{48}/?(?![0-9A-Za-z_-])")  # the token is always 48 lowercase hex characters
# Claude's OAuth authorize URL, exact hosts and path only (never a general URL allowlist). Its query
# (state, code_challenge...) is long random text that redaction would otherwise cut up.
# Two patterns, two jobs. OAUTH_STRICT decides only what the OWNER gets whole: a query of key=value pairs
# joined by "&" (or "&amp;"), ending at a space, comma, closing mark, "#", "@" or "**", with a left boundary
# so a link nested in another link's query is not held. OAUTH_BROAD decides everything that must NEVER show
# the link (team copy, progress card, translation engines): no left boundary, so "Visit:https://...",
# "url=https://...", "_https://..._" and a literal "\\n" before the link are all caught, and it takes
# everything up to the next space (client_id, redirect_uri and login_hint hold the owner's data).
_END = r"\s,<>\"'`()\[\]{}|\\^.;:!?#@*"
_PAIR = r"[\w.~%+-]+=[^\s,&<>\"'`()\[\]{}|\\^#@*]*"
_HOSTS = r"https://(?:claude\.com|claude\.ai|platform\.claude\.com)(?:/cai)?/oauth/authorize"
OAUTH_STRICT = re.compile(r"(?<![\w/?=&%.:-])" + _HOSTS + r"\?" + _PAIR + r"(?:&(?:amp;)?" + _PAIR + r")*"
                          r"(?<![.;:!?])(?=$|[" + _END + "])")
OAUTH = OAUTH_STRICT  # the name earlier code and tests use
# Matched on the link's parts: any case, an optional login part before the host, and any path (dot segments
# included) that leads to /oauth/authorize.
OAUTH_BROAD = re.compile(r"(?i)https?://(?:[^\s/@]*@)?(?:claude\.com|claude\.ai|platform\.claude\.com)(?::\d+)?"
                         r"(?:/[^\s]*?)?/oauth/authorize\S*")
# Any form of a one-time Tailscale form link, also one whose token was already redacted or cut, or written
# with an upper-case scheme or host: the host (the owner's tailnet name) and the path must not show either.
HANDOFF_BROAD = re.compile(r"(?i)https?://[^\s/]*\.ts\.net(?::\d+)?/[^\s]*?__(?:secure-handoff|yt-auth)\S*")
OWNER_LINKS = (HANDOFF, OAUTH_STRICT)  # held whole for the owner
NEVER_SHOWN = (HANDOFF_BROAD, OAUTH_BROAD)  # replaced by "[redacted]" wherever the link must not show
MAX_HELD = 2000  # a longer match is not a real link: normal redaction handles it

# What stands in for a held link while a reply is translated, split and formatted. It looks like a URL
# (fmt.unmix keeps a URL on its own line) and is made of safe characters, so nothing alters it.
PLACEHOLDER = re.compile(r"https://hgl\.invalid/HGL[0-9a-f]{6}N\d+X")
_FRAGMENT = re.compile(r"(?i)https://hgl\.invalid/hgl[0-9a-f]{6}n\d+x|(?:https?://)?hgl\.invalid\S*"
                       r"|(?<![A-Za-z0-9])HGL[0-9a-z]{3,}\S*")
SIGNIN_NOTICE = "(sign-in link sent separately)"


def hold_links(text: str):
    """(text with each owner-only link replaced by a one-off placeholder, [(placeholder, link)])."""
    import secrets
    nonce = secrets.token_hex(3)
    links: list = []
    def keep(m):
        if SECRET.search(m.group()) or len(m.group()) > MAX_HELD:
            return m.group()  # anything that also looks like a credential is left to redaction
        links.append((f"https://hgl.invalid/HGL{nonce}N{len(links)}X", m.group()))
        return links[-1][0]
    text = text or ""
    for pattern in OWNER_LINKS:
        text = pattern.sub(keep, text)
    return text, links


def restore_links(text: str, links: list, escape: bool = False) -> str:
    """Put the held links back; `escape` for text that is already Telegram HTML (a query holds `&`)."""
    for placeholder, link in links:
        text = text.replace(placeholder, html.escape(link, quote=False) if escape else link)
    return text


def hide_links(text: str, links: list) -> str:
    """The text without any held link (and without leftovers of a placeholder): "[redacted]" instead."""
    if not links:
        return text
    for placeholder, _ in links:
        text = text.replace(placeholder, "[redacted]")
    return _FRAGMENT.sub("[redacted]", text)  # whatever of a placeholder is left, none is kept


def scrub_placeholders(text: str, links: list) -> str:
    """A translation can alter a placeholder: whatever of one is left, and is not a held placeholder
    exactly, becomes "[redacted]"."""
    if not links:
        return text  # nothing was held, so nothing here is a placeholder
    known = {p for p, _ in links}
    return _FRAGMENT.sub(lambda m: m.group() if m.group() in known else "[redacted]", text)


def safe_cut(text: str, n: int) -> int:
    """`n`, or where the placeholder it would cut through starts: a split never separates one."""
    for m in PLACEHOLDER.finditer(text, max(0, n - 64)):
        if m.start() >= n:
            break
        if m.end() > n:
            return m.start() or n
    return n


# The host part of each never-shown link kind, and the path markers that make it one. Matching goes token by
# token (a link never contains whitespace) and only where a marker occurs, so the cost stays linear in the text
# (the plain regular expressions above rescan the rest of the text from every "https://": quadratic).
_OAUTH_HOST = re.compile(r"(?i)https?://(?:[^\s/@]*@)?(?:claude\.com|claude\.ai|platform\.claude\.com)(?::\d+)?")
_HANDOFF_HOST = re.compile(r"(?i)https?://[^\s/]*\.ts\.net(?::\d+)?/")
_LINK_KINDS = ((_OAUTH_HOST, ("/oauth/authorize",)), (_HANDOFF_HOST, ("__secure-handoff", "__yt-auth")))
_TOKEN = re.compile(r"\S+")
# A-Z to a-z and nothing else: str.lower() can change a string's length ("İ" becomes two characters), and the
# positions found in the lowered copy would then point at the wrong place in the original.
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _link_start(token: str, lower: str, host: re.Pattern, markers) -> int:
    """Where in `token` the earliest never-shown link of one kind starts (it runs to the end of the token, like
    `\\S*` in the broad patterns), or -1."""
    last = max(lower.rfind(m) for m in markers)
    if last < 0:
        return -1
    at = lower.find("http", 0, last)
    while at >= 0:
        m = host.match(token, at)
        if m and m.end() <= last + (0 if host is _HANDOFF_HOST else 1):
            return at
        at = lower.find("http", at + 1, last)
    return -1


def owner_link_spans(text: str, kinds=_LINK_KINDS) -> list:
    """(start, end) of every never-shown owner link in `text` (the same matches as NEVER_SHOWN), in one pass."""
    spans = []
    for tok in _TOKEN.finditer(text or ""):
        token = tok.group()
        lower = token.translate(_ASCII_LOWER)  # same length as the token: its offsets index the token itself
        if "http" not in lower:
            continue
        starts = [s for s in (_link_start(token, lower, host, markers) for host, markers in kinds) if s >= 0]
        if starts:
            spans.append((tok.start() + min(starts), tok.end()))
    return spans


def _hide_links_once(text: str) -> str:
    out, last = [], 0
    for start, end in owner_link_spans(text):
        out.append(text[last:start])
        out.append("[redacted]")
        last = end
    out.append(text[last:])
    return "".join(out)


def hide_owner_links(text: str) -> str:
    """"[redacted]" for every owner-only link in any form: the pass before text leaves for the team or an engine.
    A link that only shows once decoded (normalized) is caught too (see _decoded_aware)."""
    return _decoded_aware(text or "", _hide_links_once)


_PERCENT = re.compile(r"%[0-9A-Fa-f]{2}")
_UNICODE = re.compile(r"\\u([0-9A-Fa-f]{4})")
_ENTITY = re.compile(r"&#(?:[xX]([0-9A-Fa-f]{1,6})|([0-9]{1,7}));?")
DECODE_ROUNDS = 6


def _printable(code: int, original: str) -> str:
    """The decoded character when it is printable (a plain space included: an encoded space separates words
    as a real one does), else the encoded form as it was. No control character is ever produced."""
    try:
        ch = chr(code)
    except (ValueError, OverflowError):
        return original
    return ch if ch.isprintable() else original


def _unescape_slashes(text: str) -> str:
    """Every run of backslashes right before "/" becomes "/" (a JSON-escaped slash), in one linear pass: a long
    run of backslashes with no slash after it costs no more than its length."""
    if "\\" not in text:
        return text
    out, i, n = [], 0, len(text)
    while i < n:
        k = text.find("\\", i)
        if k < 0:
            out.append(text[i:])
            break
        out.append(text[i:k])
        j = k
        while j < n and text[j] == "\\":
            j += 1
        if j < n and text[j] == "/":
            out.append("/")
            i = j + 1
        else:
            out.append(text[k:j])
            i = j
    return "".join(out)


def _decode_once(text: str) -> str:
    text = _unescape_slashes(text)
    text = _PERCENT.sub(lambda m: _printable(int(m.group()[1:], 16), m.group()), text)
    text = _UNICODE.sub(lambda m: _printable(int(m.group(1), 16), m.group()), text)
    return _ENTITY.sub(lambda m: _printable(int(m.group(1), 16) if m.group(1) else int(m.group(2)), m.group()), text)


def normalize(text: str):
    """(the text as a link or a token in it reads once decoded, whether the decoding finished). Escaped slashes,
    %XX, \\uXXXX and numeric HTML entities are decoded, round after round until nothing changes (at most
    DECODE_ROUNDS). Only printable characters are decoded, so no control character can appear. Unfinished
    means something was still encoded after the last round: callers refuse such a text."""
    text = str(text or "")
    for _ in range(DECODE_ROUNDS):
        nxt = _decode_once(text)
        if nxt == text:
            return text, True
        text = nxt
    return text, _decode_once(text) == text


def normalized(text: str) -> str:
    return normalize(text)[0]


def for_outside(text: str, redact_credentials: bool = True) -> str:
    """The one sanitizer for text sent to an outside model or voice service (translation, topic naming and
    icons, voice, the radio's editor): owner-only links (Claude sign-in, one-time secure handoff) are always
    removed, credentials are redacted unless the owner turned `redact_outgoing` off. Every field of a request
    goes through it, names, notes and styles too. The text is normalized first (escaped slashes, %XX), so a
    link or token written another way is still recognised; links go before redaction and again after it."""
    if redact_credentials:  # the same checks as the alerts channel and the panel: one core (_clean)
        return _decoded_aware(str(text or ""), lambda t: _hide_links_once(_redact_channel(_hide_links_once(t))))
    return _decoded_aware(str(text or ""), _hide_links_once)


def _decoded_aware(text: str, clean) -> str:
    """clean(text), checked on the decoded copy too. The original stays as written (an encoded link keeps its
    %20 and %26) unless decoding shows something clean() missed in it; only then is the decoded copy cleaned and
    sent. A text still encoded after the decoding limit is refused."""
    if not text:
        return ""
    decoded, complete = normalize(text)
    if not complete:
        return "[redacted]"
    out = clean(text)
    if decoded == text:
        return out
    seen, done = normalize(out)
    if done and clean(seen) == seen:
        return out  # decoding the cleaned original shows nothing more to hide
    return clean(decoded)


_URL_END = re.compile(r"[\\\"'<>`)\]}|^*]")  # a backslash (a literal "\\n" of JSON), a quote, a closing mark


def _signin_url(m: re.Match) -> str:
    """The sign-in link a broad match holds: the strict match when there is one, else the match cut at the
    first character that cannot belong to a URL, minus trailing punctuation and an italics "_"."""
    strict = OAUTH_STRICT.match(m.string, m.start())
    if strict:
        return strict.group()
    cut = _URL_END.search(m.group())
    url = (m.group()[:cut.start()] if cut else m.group()).rstrip(".,;:!?")
    if url.endswith("_") and m.string[:m.start()].endswith("_"):
        url = url[:-1]
    return url if "?" in url else ""


def _real_signin(url: str) -> bool:
    """Whether a sign-in link has the shape of a real one: `state` and `code_challenge` are each 43+ base64url
    characters and the method, when given, is S256. A sample in tool output (a test, a doc) has neither, and
    the owner must not be taught to tap such a link."""
    from urllib.parse import parse_qs, urlsplit
    query = parse_qs(urlsplit(url.replace("&amp;", "&")).query)
    def one(key: str) -> str:
        return query[key][0] if len(query.get(key, ())) == 1 else ""
    return (all(re.fullmatch(r"[A-Za-z0-9_-]{43,}", one(k)) for k in ("state", "code_challenge"))
            and one("code_challenge_method") in ("", "S256"))


def extract_signin(text: str, notice: str = SIGNIN_NOTICE):
    """(text with every Claude sign-in link, in any form, replaced by `notice`; the links to send whole to the
    owner, each once). Every link is hidden; only one with the shape of a real one is returned."""
    found: list = []
    def swap(m):
        url = _signin_url(m)
        if url and not SECRET.search(url) and len(url) <= MAX_HELD and url not in found and _real_signin(url):
            found.append(url)
        return notice + re.search(r"[.,;:!?]*$", m.group()).group()  # sentence punctuation stays
    text = text or ""
    out, last = [], 0
    for start, end in owner_link_spans(text, _LINK_KINDS[:1]):
        m = OAUTH_BROAD.match(text, start)  # one anchored match per link found by the linear scan
        if m is None:
            continue
        out.append(text[last:start])
        out.append(swap(m))
        last = m.end()
    out.append(text[last:])
    return "".join(out), found


def redact(text: str, keep_handoff: bool = False) -> str:
    """Hide credentials while preserving URL/path segments and contextual git SHAs. `keep_handoff`
    leaves one-time secure-handoff form links whole (the owner's own chat only)."""
    if not text:
        return ""
    if keep_handoff:
        held, links = hold_links(text)
        return restore_links(redact(held), links)
    return _decoded_aware(text, _redact)


def _redact(text: str) -> str:
    text = SECRET.sub("[redacted]", text)
    text = BEARER.sub("[redacted]", text)
    text = FLAG.sub(lambda m: m.group(1) + "[redacted]", text)
    def hide_values(part: str) -> str:
        part = HEX.sub(lambda m: m.group() if _git_sha(m) or _digest(m) else "[redacted]", part)
        return LONG.sub(_hide_long, part)

    # Keep URL hosts intact; credentials in userinfo, paths and queries still pass
    # through redaction. Do not replace hosts with placeholders that could collide.
    parts = []
    start = 0
    for host in URL_HOST.finditer(text):
        parts.extend((hide_values(text[start:host.start(1)]), host.group(1)))
        start = host.end(1)
    parts.append(hide_values(text[start:]))
    return "".join(parts)


# --- the alerts channel: stricter, label-based cleaning --------------------------------------------------------
# A copy for the channel has readers the session does not, so it also hides short secrets that are named by a
# label ("password: hunter2", "DB_PASSWORD=...", "your code is 482913"). That is wrong for a topic reply, where
# code like `def f(password: str)` is normal, so redact() never does this: only redact_channel() does, and it
# hides a value only when it looks like a secret (not a type, a call, a placeholder, a path or a common word).
_SEG = r"[A-Za-z0-9]{1,20}"
_STRONG_IS = r"(?:[A-Za-z]{0,12}(?:password|passwd|pwd)|passphrase|passcode)"  # prose uses these words too
_STRONG_WORDS = r"(?:" + _STRONG_IS + r"|pass|pw)"  # but before a ":" or "=" "pass" and "pw" are labels
_WEAK_WORDS = r"(?:secret|token|api[_-]?key|access[_-]?key|private[_-]?key|otp|pin|credential|code)"
_PREFIX = (r"(?:your|the|my|one[- ]time|verification|security|login|auth(?:entication)?|2fa|mfa|sms|access|"
           r"confirmation|activation|reset|pairing|telegram|whatsapp|google|apple|signal)")
_KEY_BEFORE = r"(?i)(?<![A-Za-z0-9])(?:" + _SEG + r"[_.-]){0,3}"
_KEY_AFTER = r"(?:[_.-]" + _SEG + r"){0,3}(?![A-Za-z0-9])"
LABEL_VALUE = re.compile(
    _KEY_BEFORE + r"(?P<word>" + _STRONG_WORDS + "|" + _WEAK_WORDS + r"|" + _PREFIX + r"[ _-]?code)" + _KEY_AFTER
    + r"""(?P<sep>["']?[ \t]*[:=][ \t]*)(?:(?P<q>["'])(?P<qval>[^\n]{1,200}?)(?P=q)|["']?(?P<val>[^\s"']{1,200}))""")
# YAML: the value on the next line.
LABEL_NEXT = re.compile(
    _KEY_BEFORE + r"(?P<word>" + _STRONG_WORDS + "|" + _WEAK_WORDS + r")" + _KEY_AFTER
    + r"""(?P<sep>["']?[ \t]*:[ \t]*\r?\n[ \t]+)(?P<val>[^\s"']{1,200})""")
LABEL_IS = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?P<word>(?:" + _PREFIX + r"[ _-]?(?:code|pin|password)|" + _STRONG_IS + r"|otp|token|secret"
    r"|api[ _-]?key|code))(?P<sep>[ \t]+is[ \t]*[:=]?[ \t]*[\"']?)(?P<val>[^\s\"']{1,200})")
# "password hunter2", "OTP 482913", "pin 4821": the label and the value, nothing between.
LABEL_SPACE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?P<word>" + _STRONG_IS + r"|otp|pin)(?P<sep>[ \t]+)(?P<val>[^\s\"']{1,200})")
LABEL_HE = re.compile(
    r"(?<![\u0590-\u05FF])(?P<word>ה?(?:סיסמה|סיסמא|סיסמת\s+\S+|אסימון|טוקן|סוד|מפתח\s+גישה|"
    r"קוד\s+(?:אימות|חד[- ]פעמי|כניסה|גישה)))(?P<sep>\s*(?:[:=]|\s(?:היא|הוא)\s*[:=]?)\s*)(?P<val>\S{1,200})")
FLAG_VALUE = re.compile(
    r"(?i)(?<![\w-])(?P<flag>--?(?:password|passwd|pwd|pass|token|secret|api[-_]?key|apikey|access[-_]?key"
    r"|auth[-_]?token|access[-_]?token|key)(?:[ \t]+|=))(?P<val>(?!-)\S{1,200})")
# Command lines with a password in a short flag: `mysql -phunter2`, `mysql -p hunter2`, `curl -u user:pass`.
FLAG_P = re.compile(r"(?i)(?P<pre>\b(?:mysql\w*|mariadb\w*|sshpass)\b[^\n]{0,300}?[ \t](?-i:-p)[ \t]*)(?P<val>(?!-)[^\s\"']{1,200})")
CURL_USER = re.compile(r"(?i)(?P<pre>\bcurl\b[^\n]{0,300}?[ \t](?-i:-u|-U|--user|--proxy-user)(?:[ \t]+|=)[\"']?[^\s:\"']+:)"
                       r"(?P<val>[^\s\"']{1,200})")
BASIC = re.compile(r"(?i)(?P<pre>authorization[\"']?[ \t]*[:=][ \t]*[\"']?basic[ \t]+)(?P<val>[^\s\"']{4,})")
# user:password@ in a URL: the password goes, the user and the host stay.
USERINFO = re.compile(r"((?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{0,63}://[^/?#@\s:]*):[^/?#@\s]*@")
# Values that are not secrets even after a label: types, switches, "see the docs"-style words, a count.
_NOT_SECRET = frozenset("""
    str string int integer bool boolean float bytes list dict any object none null nil undefined true false yes no on off
    enabled disabled required optional missing empty set unset hidden redacted default defaults expired invalid valid
    incorrect correct wrong ok see the a an your my this that it here there not stored saved sent given needed
    unknown n/a na tbd todo xxx is are was be to for in of or and with from
""".split())
_WORDLIKE = re.compile(r"[\u0590-\u05FF'\"\u05F3\u05F4-]+")
_COUNT = re.compile(r"\d{1,3}(?:,\d{3})+")


# What a placeholder or a path really looks like ($VAR in capitals, ${VAR}, <value>, ***, a path of two or more
# parts); a leading symbol alone ("!Abcd1234", "$Ecret99", "/Abcd1234") is no sign of one.
_PLACEHOLDER_VALUE = re.compile(r"\$[A-Z_][A-Z0-9_]*|<[A-Za-z_][\w\-]*>?|<[^<>\s]+>|\*{3,}|%[sd]|"
                                r"(?:~|\.\.?)?/[\w.\-]+(?:/[\w.\-]+)+/?|(?:~|\.\.?)/[\w.\-]+/?")


def _label_hides(word: str, val: str) -> bool:
    """Whether `val`, written after the label `word`, looks like the secret it names."""
    val = val.strip("\"'`").rstrip(",;.)}]*_")
    strong = bool(re.search(r"(?i)pass|pwd|סיסמ|^pw$", word))
    if (not val or val.lower() in _NOT_SECRET or _COUNT.fullmatch(val)
            or (_WORDLIKE.fullmatch(val) and not re.search("סיסמ", word))):
        return False
    if re.fullmatch(r"(?i)code", word.strip()):  # a bare "code" is a one-time code only when it is one
        return bool(re.fullmatch(r"\d{4,8}", val))
    if _PLACEHOLDER_VALUE.fullmatch(val) or any(c in val for c in "()[]{}") or "://" in val:
        return False  # a placeholder ($VAR, ${VAR}, <value>, ***), a call, a type, a path, a link
    if strong:
        return True
    return bool(re.search(r"\d", val) or (re.search(r"(?i)code|otp|קוד", word) and re.fullmatch(r"[A-Z]{4,}", val)))


def _spaced_hides(word: str, val: str) -> bool:
    """The same for a value that follows its label with nothing in between: prose uses these words, so a
    password must have a digit and a code or PIN must be one."""
    val = val.strip("\"'`").rstrip(",;.)}]*_")
    if re.fullmatch(r"(?i)otp|pin", word):
        return bool(re.fullmatch(r"\d{4,8}", val))
    return _label_hides(word, val) and bool(re.search(r"\d", val))


def redact_channel(text: str) -> str:
    """redact() plus the label-based cleaning for the alerts channel (see above). Cleaning catches known
    shapes, not everything. The text is decoded once first (normalize): a short labelled secret written
    encoded ("%6ftp: 482913") is caught like a plain one; the decoded text is what goes out only then."""
    return _decoded_aware(text or "", _redact_channel)


def _redact_channel(text: str) -> str:
    def swap(hides):
        def pair(m: re.Match) -> str:
            if m.groupdict().get("qval") is not None:
                if not hides(m.group("word"), m.group("qval")):
                    return m.group()
                return m.string[m.start():m.start("qval")] + "[redacted]" + m.string[m.end("qval"):m.end()]
            if not hides(m.group("word"), m.group("val")):
                return m.group()
            return m.string[m.start():m.start("val")] + "[redacted]" + _tail(m.group("val"))
        return pair
    def flag(m: re.Match) -> str:
        return m.group() if not _label_hides("password", m.group("val")) else m.group("flag") + "[redacted]"
    def after(m: re.Match) -> str:
        return m.group("pre") + "[redacted]"
    text = USERINFO.sub(lambda m: m.group(1) + ":[redacted]@", text)
    text = FLAG_VALUE.sub(flag, text)
    for pattern in (FLAG_P, CURL_USER, BASIC):
        text = pattern.sub(after, text)
    for pattern in (LABEL_VALUE, LABEL_NEXT, LABEL_IS, LABEL_HE):
        text = pattern.sub(swap(_label_hides), text)
    # The label passes go first: redact() would cut a quoted value at its first space ("'correct horse'").
    return _redact(LABEL_SPACE.sub(swap(_spaced_hides), text))


def _tail(val: str) -> str:
    """The closing punctuation that followed a value, so `password: x,` keeps its shape."""
    return val[len(val.rstrip(",;.)}]*_\"'`")):]


def redact_pair(label: str, value: str) -> tuple:
    """A label and its value shown together (a card's figure): cleaned one by one and as the pair they read as,
    so "password" = "hunter2" is caught like "password: hunter2"."""
    label, value = str(label or ""), str(value or "")
    joined = f"{label}: {value}"
    if redact_channel(joined) != joined and redact_channel(label) == label:
        value = "[redacted]"
    return redact_channel(label), redact_channel(value)


def redact_detail(text: str) -> str:
    """redact() plus bearer values, for showing a command/path in the progress card."""
    if not text:
        return ""
    text = redact(text)
    return BEARER.sub("[redacted]", text)


def relay_socket_path() -> Path:
    from .config import state_dir
    return state_dir() / "relay.sock"


def relay_call(request: dict, timeout: float = 15, sock_path: Optional[Path] = None):
    """Send an API request via the inbox relay socket when Keychain credentials are unavailable."""
    from .telegram import TelegramError
    path = sock_path or relay_socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout + 10)
            conn.connect(str(path))
            conn.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8"))
            conn.shutdown(socket.SHUT_WR)
            data = b""
            while chunk := conn.recv(65536):
                data += chunk
                if len(data) > 60 * 1024 * 1024:
                    raise TelegramError("request_too_large")
        if not data:
            raise TelegramError("empty_relay_response")
        reply = json.loads(data.decode("utf-8"))
    except (OSError, ValueError):
        raise TelegramError("credential_unavailable") from None
    if "error" in reply:
        raise TelegramError(str(reply["error"]), reply.get("code"), reply.get("retry_after"),
                            api_rejected=reply.get("api_rejected") is True)
    return reply.get("result")


def get_token(cfg) -> Optional[str]:
    """Retrieve bot token from config, or None if credentials cannot be accessed."""
    try:
        return cfg.token()
    except Exception:
        return None
