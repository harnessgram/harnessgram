"""Claude Code -> Telegram: send a reply (optionally translated) or a file to the session's topic.

Layout of a reply: when it was translated, the translation goes out first (the translator gets a short budget,
TRANSLATE_BUDGET) with the summary (when the reply ended with a "Summary:" line) pinned on top and a small
context-size footer, then the original folded in one expandable message. If the translation is slow or fails the
original goes out first, unfolded, as before (config `original`). Messages are silent. Runs in its own detached process, started by the Stop hook, so it survives
the hook being cancelled when the next prompt arrives.

A bad part never drops the rest: HTML Telegram cannot parse is retried as plain text, and if that fails
too only that part is dropped. Failures go to <state>/mirror.log WITHOUT message content.

Shared sessions (see team.py) are also posted to the team group: always redacted, and any part matching
`team.block_patterns` is replaced by "(part withheld)".
"""
from __future__ import annotations

import fcntl
import copy
import html
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from . import accounts, agent_task, agents_card, agents_topic, alerts, flood, fmt, identity, pins
from . import lang as L
from . import team as T
from . import usage
from .config import (STATUS_REPOST, Config, home_chat, locked_map, private_dir, read_map, setting_on, state_dir,
                     write_private)
from .creds import (PLACEHOLDER, for_outside, hide_links, hide_owner_links, hold_links, redact, redact_channel, restore_links,
                    scrub_placeholders)
from . import menus
from .i18n import t
from .telegram import CLOUD_UPLOAD_LIMIT, LOCAL_LIMIT, Bot, TelegramError, read_regular_file
from .projects import thread_key
from .topics import TopicMap, place
from .translate import TranslateError, external_allowed, needs_translation, speak, translate_reply, translate_text

PHOTO_LIMIT = 10 * 1024 * 1024
DOCUMENT_LIMIT = CLOUD_UPLOAD_LIMIT  # largest file sent as multipart bytes (read into memory)
UPLOAD_LIMIT = 45 * 1024 * 1024  # above this a file is never uploaded (see send_file)


def _media_info(ext: str, stream) -> tuple[str, str, Optional[int]]:
    """Recognize complete MP3 frames or bounded ISO media structure in the file head."""
    document = ("sendDocument", "document", None)
    stream.seek(0, os.SEEK_END)
    end = stream.tell()
    stream.seek(0)
    data = stream.read(min(end, 1024 * 1024))
    if ext == ".mp3":
        pos = 0
        if data.startswith(b"ID3"):
            if (len(data) < 10 or data[3] not in (2, 3, 4) or data[4] == 255
                    or any(b & 128 for b in data[6:10])):
                return document
            pos = 10 + sum(b << shift for b, shift in zip(data[6:10], (21, 14, 7, 0)))
            if data[3] == 4 and data[5] & 0x10:
                pos += 10
        # Validate complete consecutive frames throughout the available head.
        frames = 0
        while pos < len(data):
            if end == len(data) and len(data) - pos == 128 and data[pos:pos + 3] == b"TAG":
                pos = len(data)
                break
            if pos + 4 > len(data):
                return ("sendAudio", "audio", None) if end > len(data) and frames >= 2 else document
            h = int.from_bytes(data[pos:pos + 4], "big")
            version, layer = (h >> 19) & 3, (h >> 17) & 3
            bitrate, rate = (h >> 12) & 15, (h >> 10) & 3
            if h >> 21 != 0x7ff or version == 1 or layer != 1 or bitrate in (0, 15) or rate == 3:
                return document
            rates = (44100, 48000, 32000)
            hz = rates[rate] // (1 if version == 3 else 2 if version == 2 else 4)
            table = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320) if version == 3 else (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
            length = (144 if version == 3 else 72) * table[bitrate] * 1000 // hz + ((h >> 9) & 1)
            if pos + length > len(data):
                return ("sendAudio", "audio", None) if end > len(data) and frames >= 2 else document
            pos += length
            frames += 1
        return ("sendAudio", "audio", None) if frames >= 2 else document
    if ext not in (".m4a", ".mp4"):
        return document

    def boxes(start, stop):
        for _ in range(4096):
            if start + 8 > min(stop, len(data)):
                return
            n = int.from_bytes(data[start:start + 4], "big")
            kind = data[start + 4:start + 8]
            header = 8
            if n == 1:
                if start + 16 > len(data):
                    return
                n, header = int.from_bytes(data[start + 8:start + 16], "big"), 16
            elif n == 0:
                n = stop - start
            if n < header or n > stop - start:
                return
            yield kind, start + header, start + n
            start += n

    top = list(boxes(0, end))
    if not top or top[0][0] != b"ftyp":
        return document
    _, begin, finish = top[0]
    if finish > len(data) or finish - begin < 8 or (finish - begin) % 4:
        return document
    if end <= len(data) and top[-1][2] != end:
        return document  # a whole file whose boxes do not add up to its size is cut short
    # Brands vary widely between encoders (streamed files carry "dash", "iso6"), so the track type decides.
    wanted = b"soun" if ext == ".m4a" else b"vide"
    found, duration, seen = False, None, False
    for kind, start, stop in top:
        if kind != b"moov" or stop > len(data):
            continue
        seen = True
        for inner, begin, finish in boxes(start, stop):
            if inner == b"mvhd" and begin < finish:
                version = data[begin]
                offset, width = (20, 8) if version == 1 else (12, 4)
                if version in (0, 1) and begin + offset + 4 + width <= finish:
                    scale = int.from_bytes(data[begin + offset:begin + offset + 4], "big")
                    ticks = int.from_bytes(data[begin + offset + 4:begin + offset + 4 + width], "big")
                    if scale and ticks != 2 ** (width * 8) - 1:
                        duration = ticks // scale
            if inner == b"trak":
                for typ, a, b in boxes(begin, finish):
                    if typ == b"mdia":
                        for sub, x, y in boxes(a, b):
                            if sub == b"hdlr" and y - x >= 12 and data[x + 8:x + 12] == wanted:
                                found = True
    # A track list that is not in the head (it sits after the media data) cannot be judged here: Telegram
    # decides, and a clear refusal falls back to a document.
    if seen and not found:
        return document
    if not seen and (end <= len(data) or ext == ".mp4"):
        return document  # an unknown track type is never sent as video; an .m4a can only be audio
    return ("sendAudio", "audio", duration) if ext == ".m4a" else ("sendVideo", "video", duration)


def upload_limit(cfg: Config) -> int:
    """Largest file sent to Telegram: 45 MB on the cloud server (below its 50 MB), 2000 MB on a local one."""
    return LOCAL_LIMIT if cfg.api_local else UPLOAD_LIMIT
TELEGRAM_LIMIT = 4096  # a message's hard character limit
RETRY_AFTER_CAP = 30.0  # seconds: never wait longer for a 429, whatever Telegram asks
QUESTION_TAIL = 300  # a "?" this close to the reply's end gets yes/no buttons
TRANSLATE_BUDGET = 12.0  # seconds a reply waits for its translation before the original goes out first
TRANSLATE_TIMEOUT = 120.0  # seconds an instant reply waits for its translation before giving up on the edit
EDIT_FLOOD_WAIT = 90.0  # seconds an instant reply's edits may wait for flood control in all, as Telegram asks
SHORT_REPLY = 450  # a shorter reply gets a one-line footer instead of the full one
SHARE_LIMIT = 2 * 1024 * 1024 * 1024
SHARE_DAYS = 7  # a shared file's lifetime; older entries are pruned


def _plain(html_text: str) -> str:
    """HTML message -> readable plain text, for when Telegram refuses the markup."""
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


def _is_thread_error(e: TelegramError) -> bool:
    return "thread" in e.description.lower()


def _is_parse_error(e: TelegramError) -> bool:
    """Only Telegram's own "can't parse entities" (bad HTML): a plain-text retry for any other
    error would repeat a send that may already have gone through."""
    return "parse entities" in e.description.lower()


def reason_str(exc: BaseException, lang: str = "en") -> str:
    """One short reason for a send failure, appended to the owner's alert."""
    code = getattr(exc, "code", None)
    text = str(getattr(exc, "description", None) or str(exc)).lower()
    if code == 429 or "too many requests" in text or "retry after" in text:
        return t("reason_rate_limit", lang=lang)
    if "thread" in text:  # e.g. "message thread not found": the topic was deleted
        return t("reason_thread_deleted", lang=lang)
    if isinstance(exc, TimeoutError) or "timeout" in text or "timed out" in text:
        return t("reason_timeout", lang=lang)
    if isinstance(exc, ConnectionError) or "network" in text or "connection" in text or "unreachable" in text:
        return t("reason_network", lang=lang)
    return t("reason_unknown", lang=lang)


def reason_he(exc: BaseException) -> str:
    """One short Hebrew reason for a send failure, appended to the owner's alert ("why did it not arrive?")."""
    return reason_str(exc, lang="he")


def reply_keyboard(cfg: Config) -> dict:
    """The fixed one-time keyboard under a topic (also re-sent by /keyboard), rows listed so an
    RTL chat lays them out left to right."""
    ui_lang = getattr(cfg, "ui_language", "en")
    s = L.strings(ui_lang)
    row1 = [{"text": s["btn_continue"]}, {"text": s["btn_stop"]}, {"text": s["btn_escape"]}]
    row2 = [{"text": s["btn_status"]}, {"text": s["btn_screen"]}, {"text": s["btn_compact"]}]
    row3 = [{"text": s["btn_permissions"]}, {"text": s["btn_context"]}, {"text": s["btn_model"]}]
    if ui_lang == "he":
        row1, row2, row3 = list(reversed(row1)), list(reversed(row2)), list(reversed(row3))
    return {
        "keyboard": [row1, row2, row3],
        "is_persistent": False,
        "one_time_keyboard": True,
        "resize_keyboard": True,
    }


def fixed_keyboard(lang: str) -> dict:
    """The fixed keyboard under the typing box, phone first. Above it, live_keyboard adds the info row
    (context, quota, account). Its first row is the session's settings, each button showing its current
    value once known (model, thinking level, permission mode); the bottom row, closest to the thumb, holds
    the actions with the shortest labels (stop, compact, the More menu, the Cockpit)."""
    def b(key: str) -> dict:
        return {"text": t(key, lang=lang)}
    return {
        "keyboard": [
            [b("fixed_btn_model"), b("fixed_btn_thinking"), b("fixed_btn_permissions")],
            [b("fixed_btn_stop"), b("fixed_btn_compact"), b("fixed_btn_more"), b("fixed_btn_panel")],
        ],
        "is_persistent": False,
        "one_time_keyboard": True,
        "resize_keyboard": True,
    }


KEYBOARD = fixed_keyboard("he")
KEYBOARD_EN = fixed_keyboard("en")


QUOTA_LINE = re.compile(r"\n?⏳[^\n]*")


def strip_quota(text: str) -> str:
    """The saved status text without its quota line (see usage.quota_line), which is old by now."""
    return QUOTA_LINE.sub("", text or "")


CONTEXT_STEP = 10_000  # tokens: the context cell redraws the card when the size moved this much
_CONTEXT_NUMBER = re.compile(r"(הקשר )[\d,]+( אלף)|(Context )[\d,]+(k)|(\U0001F9E0 )[\d,]+(k)")


def session_context(session: Optional[str]) -> int:
    """The session's context size now, in tokens, from its transcript (a compaction newer than the last
    reply counts: its post-compaction size), or 0 when unknown."""
    return context_now(session)[0]


def context_now(session: Optional[str]) -> tuple:
    """(tokens, compacted): the session's context size now, and whether a compaction is newer than every reply.
    (0, True) is "compacted, the new size not measured yet": the size from before it must not be shown."""
    if not session:
        return 0, False
    try:
        from .status import transcript_of
        path = transcript_of(session)
        if not path:
            return 0, False
        tokens, _, compacted = usage.context_state(path)
        return int(tokens), bool(compacted)
    except Exception:
        return 0, False


def with_context(footer: str, session: Optional[str], lang: str) -> str:
    """The status text with the session's CURRENT context size: the number of a context line already in
    it is replaced, and a text without one gets the context line on top. The saved card texts were
    drawn once and kept their old size, or had no context line at all, so the keyboard's context cell
    stayed stale or missing."""
    ctx = session_context(session)
    if not ctx:
        return footer
    k = f"{round(ctx / 1000):,}"
    for code in ("he", "en"):  # the compaction tip follows the fresh number, not the saved one
        footer = footer.replace(t("footer_tip", lang=code), "")
    tip = t("footer_tip", lang=lang) if ctx >= 400_000 else ""
    inline = []  # "🧠 450k · Opus 5.5 · high": the tip goes at the line's end, never between the number and
    # the model, where restamp_footer took it for the model and repeated the name on every redraw (review of #150)

    def stamp(m):
        inline.append(bool(m.group(5)))
        return (next(g for g in (m.group(1), m.group(3), m.group(5)) if g) + k
                + next(g for g in (m.group(2), m.group(4), m.group(6)) if g) + ("" if m.group(5) else tip))
    fresh, n = _CONTEXT_NUMBER.subn(stamp, footer, count=1)
    if n:
        if tip and inline and inline[0]:
            fresh = re.sub(r"(\U0001F9E0 [\d,]+k[^\n]*)", lambda m: m.group(1) + tip, fresh, count=1)
        return fresh
    line = t("footer_context", lang=lang, k=k, tip=tip)
    return line + ("\n" + footer.lstrip("\n") if footer.strip() else "")


def restamp_footer(footer: str, session: Optional[str], lang: str) -> str:
    """A saved status text with today's quota line in place of the old one (right after its first line)
    and the session's current context size."""
    base = strip_quota(footer)
    if not session:
        return base
    base = with_context(base, session, lang)
    try:  # the model line of an older reply: the model the session is on now (a /model since then)
        model = usage.session_model(session)[0]
    except Exception:
        model = ""
    if model:
        base = re.sub(r"(?m)^(\U0001F916 \u2068?)[^\u2069\n·]+?(\u2069?)(?=\s*(?:·|$))",
                      lambda m: m.group(1) + model + m.group(2), base, count=1)
        base = re.sub(r"(?m)^(\U0001F9E0 [\d,]+k · )[^·\n]+?(?=\s*(?:·|$))",  # English: "🧠 184k · Opus 5.5 · high"
                      lambda m: m.group(1) + model, base, count=1)
    try:  # the mode, agent and style lines as they are now (a change made in Claude since the reply)
        from . import direct
        base = direct.restamp(base, session, lang)
    except Exception:
        pass
    try:
        line = usage.quota_line(lang or "en", session=session)
    except Exception:
        line = ""
    if not line:
        return base
    head, nl, rest = base.partition("\n")
    return head + line + nl + rest


def get_keyboard(lang: str = "en") -> dict:
    return KEYBOARD if (lang or "").startswith("he") else KEYBOARD_EN


def context_numbers(session: Optional[str], footer: str = "") -> tuple:
    """(tokens, window) for the keyboard's context cell: the session's size now from its transcript, else the
    number in the status text; the window when known, else 0."""
    ctx, compacted = context_now(session)
    if compacted and not ctx:  # the old text's number is from before the compaction
        return 0, 0
    if not ctx:
        found = re.search(r"הקשר ([\d,]+) אלף", footer or "") or re.search(r"(?:Context|🧠|📏)\s*([\d,]+)\s?k", footer or "")
        ctx = int(found.group(1).replace(",", "")) * 1000 if found else 0
    window = 0
    if ctx and session:
        try:
            window = usage.context_window(session, usage.session_model(session)[0])
        except Exception:
            window = 0
    return ctx, window


def context_label(tokens: int, window: int, lang: str) -> str:
    """"📏 182k · 18%" ("📏 182 אלף · 18%"): the size, and how full the window is when its size is known."""
    k = f"{round(tokens / 1000):,}"
    size = t("context_size", lang=lang, k=k)
    pct = f" · {min(100, round(tokens * 100 / window))}%" if window else ""
    return f"📏 {size}{pct}"


def _short_model(model: str) -> str:
    """"Claude Opus 5.5 (1M context)" -> "Opus 5.5 1M": short enough for a third of a phone's width."""
    name = re.sub(r"^Claude\s+", "", (model or "").strip())
    name = re.sub(r"\s*\((\d+[KkMm])(?:\s+context)?\)", lambda m: " " + m.group(1).upper(), name)
    return name[:20].strip()


def state_labels(session: Optional[str], lang: str) -> dict:
    """The live labels of the settings row: {"model"|"effort"|"mode": label} for each value known now.
    The model and level come from the status line, else the transcript (usage.session_model); the mode
    from the terminal's own status line, else what Claude's hooks last reported (direct.current)."""
    out = {}
    if not session:
        return out
    try:
        model, level = usage.session_model(session)  # a newer /model answer wins over the status line
    except Exception:
        model, level = "", ""
    if model and _short_model(model):
        out["model"] = "🧠 " + _short_model(model)
    if level in ("low", "medium", "high", "xhigh", "max"):
        out["effort"] = "🎚️ " + t("effort_" + level, lang=lang)
    current = ""
    try:  # the permission mode as the terminal's status line shows it
        from .inbox import pane_for_session, pane_mode
        pane = pane_for_session(session)
        current = pane_mode(pane) if pane else ""
    except Exception:
        current = ""
    if not current:  # no terminal, or a question on screen hides the line: the mode Claude last reported
        try:
            from . import direct
            current = direct.current(session).get("mode", "")
        except Exception:
            current = ""
    if current and t("mode_short_" + current, lang="en") != "mode_short_" + current:
        out["mode"] = "🔐 " + t("mode_short_" + current, lang=lang)
    return out


# The settings buttons of the fixed keyboard, by their icon (a bidi mark or a dropped U+FE0F does not count).
_STATE_ICONS = (("🧠", "model"), ("🎚", "effort"), ("🔐", "mode"))
BIDI_MARKS = re.compile("[\u200e\u200f\u061c\u202a-\u202e\u2066-\u2069\ufe0f]")


def bare_label(text: str) -> str:
    """A button label without bidi marks and emoji variation selectors, for matching a tap to its button."""
    return BIDI_MARKS.sub("", text or "").strip()


def state_button(text: str) -> str:
    """"model", "effort" or "mode" for a settings button of the fixed keyboard (by its icon); else "".
    Only for the keyboard this module builds: a tap that arrives as text is matched by sent_labels."""
    bare = bare_label(text)
    return next((kind for icon, kind in _STATE_ICONS if bare.startswith(icon + " ")), "")


def account_cell(label: str) -> str:
    """The info row's account cell for an account label (its e-mail's local part, else its name)."""
    return "👤 " + (label[:9] + "…" if len(label) > 10 else label)


def _model_labels(name: str) -> set:
    """Every label a model's button had: today's short one and the older unshortened ones."""
    name = (name or "").strip()
    if not name:
        return set()
    plain = name.replace("Claude ", "").strip()
    return {"🧠 " + _short_model(name), "🧠 " + plain[:40]}


SENT_LABELS_FILE = "keyboard-labels.json"  # {session: {bare label: kind}}: the live labels its keyboards showed
SENT_LABELS_KEEP = 24  # per session: the last few keyboards' values (a keyboard stays on the phone for a while)


def remember_labels(session: Optional[str], labels: dict) -> None:
    """Keep the live labels a keyboard of this session showed ({bare label: kind}), so a tap on a keyboard
    still on the phone is known after the value changed. Written only when a label is new."""
    if not session or not labels:
        return
    path = state_dir() / SENT_LABELS_FILE
    seen = read_map(path).get(session)
    if isinstance(seen, dict) and all(seen.get(k) == v for k, v in labels.items()):
        return
    try:
        with locked_map(path) as data:
            mine = data.pop(session, None)
            mine = dict(mine) if isinstance(mine, dict) else {}
            for label, kind in labels.items():
                mine.pop(label, None)
                mine[label] = kind
            data[session] = dict(list(mine.items())[-SENT_LABELS_KEEP:])
    except OSError:
        pass


def sent_labels(session: Optional[str] = None) -> dict:
    """{bare label: "model" | "effort" | "mode" | "account"} for every settings or account button a keyboard
    could have shown, in both languages: the labels this session's keyboards really showed (remember_labels),
    the plain wordings (today's and older ones still on phones), every model, level and mode from the tables
    that build the keyboard, and the account labels. A text that is none of these is the owner's message,
    whatever its icon."""
    from . import models
    out = {}

    def add(label: str, kind: str) -> None:
        out[bare_label(label)] = kind
    for lang in ("he", "en"):
        for key, kind in (("fixed_btn_model", "model"), ("btn_model", "model"), ("fixed_btn_thinking", "effort"),
                          ("fixed_btn_permissions", "mode"), ("btn_permissions", "mode")):
            add(t(key, lang=lang), kind)
        for level in ("low", "medium", "high", "xhigh", "max"):
            add("🎚️ " + t("effort_" + level, lang=lang), "effort")
        for mode in ("default", "plan", "acceptEdits", "auto", "bypassPermissions", "dontAsk"):
            add("🔐 " + t("mode_short_" + mode, lang=lang), "mode")
    names = [name for _, _, name in models.MODELS]
    names += [n + suffix for n in list(names) for suffix in (" (1M)", " (1M context)")]
    if session:
        try:
            names.append(usage.session_model(session)[0])
        except Exception:
            pass
    for name in names:
        for label in _model_labels(name):
            add(label, "model")
    try:
        for name in accounts.names():
            for label in {accounts.label(name), name}:
                if label:
                    add(account_cell(label), "account")
    except Exception:
        pass
    mine = read_map(state_dir() / SENT_LABELS_FILE).get(session) if session else None
    if isinstance(mine, dict):
        out.update({k: v for k, v in mine.items() if v in ("model", "effort", "mode", "account")})
    return out


def live_keyboard(footer: str, base_kb: Optional[dict] = None, lang: str = "he",
                  thread: Optional[int] = None, fresh: bool = False, session: Optional[str] = None,
                  chat: Optional[int] = None) -> dict:
    """The fixed keyboard with a live info row on top ("context and quota always in view"): the context size
    (and how full the window is), Claude's weekly use and, when there is more than one account, the account
    of this topic. The settings buttons show their current values (state_labels).
    `fresh`: the footer is an OLD saved text (a card re-posted later, a restored keyboard): its quota numbers
    are ignored and read again from the saved usage of the topic's account.
    `session`: the topic's session when the caller knows it (a thread id means nothing without its chat).
    `chat`: the topic's chat when it is not the home chat; the home-only thread lookup is then skipped."""
    plain = re.sub(r"<[^>]+>", "", footer or "")
    ctx_text = plain  # the saved text's context size is only a fallback (context_numbers); its quota is not, when `fresh`
    if thread and session is None and chat is None:
        try:
            tmap = TopicMap(state_dir() / "topics.json", None, 0)
            session = tmap.session_for_thread(thread)
        except Exception:
            session = None
    if fresh:
        plain = strip_quota(plain)
        if session:  # tables of an old text may also hold quota numbers: only today's saved usage counts
            plain = ""
    if not re.search(r"(?:Claude(?: [A-Za-z0-9_-]+)?|⏱️|weekly|שבועית|5\s*(?:h|hours|שעות))\s+\u2066?\d+%", plain):
        # A footer without quota numbers (the status card's terminal line) still gets the quota
        # cell, from the saved usage of this topic's own account, so every keyboard looks the same.
        try:
            from .usage import quota_line
            plain += quota_line(lang or "en", session=session) if session else quota_line(lang or "en")
        except Exception:
            pass
    info = []
    tokens, window = context_numbers(session, ctx_text)
    if tokens:
        info.append({"text": context_label(tokens, window, lang)})
    # regex accepts "Claude <account> NN%"
    claude = re.findall(r"Claude(?: [A-Za-z0-9_-]+)?\s+\u2066?(\d+)%", plain)
    short = re.search(r"⏱️ \u2066?(\d+)%", plain) if not claude else None
    reset_re = r"↻\s*\u2066?(?:(\d{1,2}\.\d{1,2}) [\d:]+|([\d:]+))"

    def reset_of(match) -> str:  # a later day by its date only, today's reset by its time: short enough
        return f" · ↻ {match.group(1) or match.group(2)}" if match else ""
    if len(claude) > 1:
        info.append({"text": "⏱️ " + claude[0] + "%" + f" · 📅 {claude[1]}%"})
    elif claude:
        is_weekly = bool(re.search(r"(?:weekly|שבועית|week)", plain, re.I))
        icon = "📅" if is_weekly else "⏱️"
        reset = re.search(r"Claude(?: [A-Za-z0-9_-]+)?\s+\d+%\s*" + reset_re, plain) or re.search(reset_re, plain)
        info.append({"text": f"{icon} {claude[0]}%" + (reset_of(reset) if is_weekly else "")})
    elif short:
        info.append({"text": f"⏱️ {short.group(1)}%"})
    else:
        wk = re.search(r"(?:weekly|שבועית)\s+(\d+)%", plain)
        if wk:  # weekly only: an old footer's 5-hour number is never shown
            info.append({"text": "📅 " + wk.group(1) + "%" + reset_of(re.search(reset_re, plain))})

    if session:
        try:  # which account answers matters only when there is more than one (the account menu is also under More)
            acct = accounts.account_for(session) if accounts.multiple() else ""
            if acct:
                mail = accounts.email(acct)
                info.append({"text": account_cell(mail.split("@")[0] if mail else acct)})
        except Exception:
            pass

    base = base_kb or (KEYBOARD if (lang or "").startswith("he") else KEYBOARD_EN)
    rows = [list(r) for r in base.get("keyboard", [])]
    live = state_labels(session, lang)
    if live:
        rows = [[dict(b, text=live[state_button(b.get("text", ""))]) if state_button(b.get("text", "")) in live else b
                 for b in r] for r in rows]

    kb = dict(base, keyboard=([info] if info else []) + rows)
    shown = {bare_label(text): kind for kind, text in live.items()}
    shown.update({bare_label(b["text"]): "account" for b in info if b["text"].startswith("👤 ")})
    remember_labels(session, shown)
    hint = " · ".join(b["text"] for b in info)
    if hint:
        kb["input_field_placeholder"] = hint[:64]
    return kb


# Context and quota numbers on the live info row change with almost every reply; they are not labels.
_NUMBER_BUTTONS = ("📏", "⏱️", "📅")


def keyboard_signature(markup: Optional[dict]) -> str:
    """What decides whether the status card must be posted again: the labels of the keyboard (account,
    mode, model, thinking level) and the context and quota numbers in coarse steps."""
    rows = []
    for row in (markup or {}).get("keyboard", []):
        kept = []
        for b in row:
            text = b.get("text", "")
            if text.startswith(_NUMBER_BUTTONS):
                # The numbers count only in coarse steps (context per CONTEXT_STEP, quota per 10 points), so the
                # card is posted again when they really moved, not after every reply. Reset times don't count.
                nums = [int(n.replace(",", "")) for n in re.findall(r"(\d[\d,]*)\s*(?:%|k|K|אלף)", text)]
                step = CONTEXT_STEP // 1000 if text.startswith("📏") else 10
                text = text[:2] + ":" + ",".join(str(n // step) for n in nums)
            kept.append(text)
        if kept:
            rows.append(kept)
    return json.dumps(rows, ensure_ascii=False)


STATUS_TEXT_FILE = "telegram-status-text.json"


def _read_map(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def session_stamp(session: Optional[str], lang: Optional[str] = None) -> str:
    """Which model, Claude account and thinking level answer in this session, one fact per line (a
    phone scrambles a line that mixes English and Hebrew). The account line shows only when there
    is more than one account to tell apart. "" when nothing is known."""
    if not session:
        return ""
    lines = []
    try:
        model, level = usage.session_model(session)  # a newer /model answer wins over the status line
    except Exception:
        model = level = ""
    if model:
        lines.append("🧠 " + model.strip())
    try:
        if accounts.multiple():
            lines.append("👤 " + accounts.label(accounts.account_for(session)))
    except Exception:
        pass
    if level in ("low", "medium", "high", "xhigh", "max"):
        lines.append(t("stamp_effort", lang=lang, level=t("effort_" + level, lang=lang)))
    return "\n".join(lines)


def status_message(cfg: Config, thread: int, footer: str, keyboard: Optional[dict] = None, asked: bool = False, bot=None,
                   repost: bool = False, optional: bool = False, chat: Optional[int] = None) -> Optional[dict]:
    """Context and team quota in their own silent message after the reply.

    Quiet by default: the previous card is edited in place. A new card is posted (and the old one
    deleted afterwards, so the keyboard never drops) only when the reply asked a yes/no question
    (the question hides the keyboard), when there is no previous card, when the keyboard labels
    differ from the last posted card (an edit cannot change a reply keyboard, so a moved session
    would keep showing its old account), or when the "new card after every reply" setting is on.
    An unchanged text is not edited: Telegram rejects an edit to the same text.
    `repost` forces a new card at the bottom (bringing the keyboard back under a newer message).
    `optional` is for background callers: every send and edit goes through the flood gate and budget, and
    a real Telegram error is raised to the caller (a flooded chat still returns None quietly).
    `chat`: the project group the thread belongs to (None = the home chat)."""
    if chat == home_chat(cfg):
        chat = None
    base = private_dir(state_dir())
    path = base / "status-footer.json"
    lock_path = base / "status-footer.lock"
    tp = base / STATUS_TEXT_FILE  # also read by the inbox to bring the keyboard back
    sigs_path = base / "status-labels.json"
    with open(lock_path, "a") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
        except OSError:
            pass
        ids = _read_map(path)
        key = thread_key(thread or 0, chat)
        previous = ids.get(key)
        ui_lang = getattr(cfg, "ui_language", "en")
        header = f"{t('status_card_header', lang=ui_lang)}\n"
        try:
            session = _session_in(cfg, thread, chat)
            who = session_stamp(session, ui_lang)
        except Exception:
            session, who = None, ""
        if who:
            header += html.escape(who, quote=False) + "\n"
        text = (header + footer) if footer else header.strip()
        where = home_chat(cfg) if chat is None else chat
        params = {
            "chat_id": where,
            "disable_notification": True,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if thread:
            params["message_thread_id"] = thread
        if keyboard is not None:
            params["reply_markup"] = live_keyboard(footer, base_kb=keyboard, lang=ui_lang, thread=thread, fresh=True,
                                                  session=session, chat=chat)
        sig = keyboard_signature(params.get("reply_markup"))
        bot = bot or Bot.from_config(cfg)

        def remember(texts_key_value: str) -> None:
            texts = _read_map(tp)
            texts[key] = texts_key_value
            write_private(tp, json.dumps(dict(list(texts.items())[-200:]), ensure_ascii=False))

        sigs = _read_map(sigs_path)
        if previous and not asked and not repost and sigs.get(key) == sig and not setting_on(STATUS_REPOST):
            if _read_map(tp).get(key) == footer:
                return {"message_id": previous}
            try:
                flood.optional_call(bot, "editMessageText", {
                    "chat_id": where,
                    "message_id": previous,
                    "text": params["text"],
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                })
                remember(footer)
                return {"message_id": previous}
            except TelegramError as exc:
                if getattr(exc, "skipped", False):
                    return {"message_id": previous, "skipped": True}  # flooded chat: keep the old card, refresh next time
                pass  # deleted or too old to edit: post a new one
        msg = None
        try:
            # A fresh card with nothing to replace, or one that brings a question's keyboard back, is
            # wanted now; re-posting an existing card is background traffic. Note: a re-post (e.g. one that
            # brings the keyboard back) skipped during a flood is NOT retried; the old card stays until the
            # next reply refreshes it.
            if (previous and not asked) or optional:
                msg = flood.optional_call(bot, "sendMessage", params)
            else:
                msg = bot.call("sendMessage", params)
        except TelegramError as exc:
            if optional and not getattr(exc, "skipped", False):
                raise
        if msg and isinstance(msg, dict) and msg.get("message_id"):
            ids[key] = msg["message_id"]
            write_private(path, json.dumps(ids))
            sigs[key] = sig
            write_private(sigs_path, json.dumps(dict(list(sigs.items())[-200:]), ensure_ascii=False))
            try:  # kept so the inbox can bring the keyboard back after a button message
                remember(footer)
            except Exception:
                pass
            if previous:
                try:
                    bot.call("deleteMessage", {"chat_id": where, "message_id": previous})
                except TelegramError:
                    pass
        return msg


def _session_in(cfg: Config, thread, chat: Optional[int] = None) -> Optional[str]:
    """The session behind a topic of the home chat (`chat` None) or of a project group."""
    tmap = TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg)
    return tmap.session_at(tmap.chat_id if chat is None else chat, thread)


CARD_DATA_FILE = "status-data.json"


def card_data_key(session: str) -> str:
    """What the status card's info buttons show for a session, as a short string: which account answers
    and its saved quota in the keyboard's 10-point steps. When it differs from the last one the card was
    drawn for, the card is stale."""
    acct = ""
    try:
        acct = accounts.account_for(session) or ""
    except Exception:
        pass
    label = ""
    try:
        label = accounts.label(acct) if acct else ""
    except Exception:
        pass
    quota = "-"
    try:
        path = usage.claude_usage_file(session=session)
        u = json.loads(Path(path).read_text(encoding="utf-8"))
        at = usage._parse_time(u.get("at"))
        if at is not None and 0 <= time.time() - at <= 12 * 3600:
            quota = f"{int(u.get('five_hour', 0)) // 10}:{int(u.get('weekly', 0)) // 10}"
    except Exception:
        pass
    live = ""
    try:  # model, thinking level, mode, agent, style: a change made in Claude redraws the card at the
        from . import direct  # context pace (CONTEXT_BACKOFF mid-turn, at once at the turn's end)
        live = "~" + direct.card_key(session)
    except Exception:
        pass
    try:  # the window size (the context cell's "· 18%") and whether the account cell shows at all
        live += f"~{usage.context_window(session)}~{int(accounts.multiple())}"
    except Exception:
        pass
    return f"{acct}|{label}|{quota}|{session_context(session) // CONTEXT_STEP}{live}"


def _only_context_moved(old: Optional[str], new: str) -> bool:
    """Two card data keys that differ only in their context part."""
    return bool(old) and old.rsplit("|", 1)[0] == new.rsplit("|", 1)[0] and old != new


CARD_PACE_FILE = "status-card-pace.json"
CARD_BACKOFF = 15 * 60  # after a failed first card, and between two background redraws of one topic
CONTEXT_BACKOFF = 3 * 60  # between two mid-turn redraws of a topic whose context size alone moved


def _topic_gone(exc: BaseException) -> bool:
    text = str(getattr(exc, "description", None) or exc).lower()
    # A closed topic is left alone: the owner may reopen it, and forgetting it would open a new topic.
    return any(k in text for k in ("thread not found", "topic_deleted", "topic_id_invalid"))


def card_waiting(thread, has_card: bool, now: Optional[float] = None, chat: Optional[int] = None,
                 backoff: float = CARD_BACKOFF) -> bool:
    """Not this topic's turn yet: a first card failed less than CARD_BACKOFF ago (a send that timed out
    may still have gone through, so it is not repeated at once), or an existing card was drawn less than
    CARD_BACKOFF ago (a card is not redrawn at the bottom more than once a quarter hour)."""
    now = time.time() if now is None else now
    entry = _read_map(state_dir() / CARD_PACE_FILE).get(thread_key(thread, chat))
    if not isinstance(entry, dict):
        return False
    try:
        if has_card:
            return now - float(entry.get("drawn") or 0) < backoff
        return float(entry.get("retry") or 0) > now
    except (TypeError, ValueError):
        return False


def _pace(thread, chat: Optional[int] = None, **fields) -> None:
    path = private_dir(state_dir()) / CARD_PACE_FILE
    data = _read_map(path)
    key = thread_key(thread, chat)
    entry = data.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry.update(fields)
    data.pop(key, None)
    data[key] = entry
    write_private(path, json.dumps(dict(list(data.items())[-200:])))


def refresh_card(cfg: Config, bot, session: str, topics: Optional[TopicMap] = None, create: bool = False,
                 force: bool = False, now: Optional[float] = None, turn_end: bool = False) -> bool:
    """Bring a session topic's status card (and its quota/account buttons) up to date when what they show
    changed: the quota moved a step, the session moved to another account, or its account has a reading now.
    `create`: also post a first card for a topic that has none (a new topic or session).
    Background traffic: a flooded chat or a spent optional budget skips it quietly (the data key is then
    not saved, so the next round retries); a card is drawn for a topic at most once per CARD_BACKOFF unless
    `force`; a failed first card waits CARD_BACKOFF before the next try, and a deleted or closed topic is
    forgotten. A change of the context size alone (a step of CONTEXT_STEP) waits only CONTEXT_BACKOFF, and
    `turn_end` (the reply was just delivered) does not wait at all: the context cell is current after
    every turn, and refreshed mid-turn at a throttled pace. Returns True when a card was drawn or edited."""
    now = time.time() if now is None else now
    try:
        topics = topics or TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg)
        found = topics.place_of(session)
        if found is None:
            return False
        thread, chat = found.thread, found.away
        base = private_dir(state_dir())
        key = found.key
        has_card = bool(_read_map(base / "status-footer.json").get(key))
        if not has_card and not create:
            return False
        data_key = card_data_key(session)
        stored = _read_map(base / CARD_DATA_FILE)
        if has_card and not force and stored.get(key) == data_key:
            return False
        backoff = 0.0 if (turn_end and has_card) else \
            CONTEXT_BACKOFF if _only_context_moved(stored.get(key), data_key) else CARD_BACKOFF
        if not (force and has_card) and card_waiting(thread, has_card, now, chat=chat, backoff=backoff):
            return False
        if not flood.is_open(home_chat(cfg) if chat is None else chat):
            return False
        ui_lang = getattr(cfg, "ui_language", None) or "en"
        footer = restamp_footer(_read_map(base / STATUS_TEXT_FILE).get(key) or "", session, ui_lang)
        try:
            msg = status_message(cfg, thread, footer, get_keyboard(ui_lang), bot=bot, optional=True, chat=chat)
        except Exception as exc:
            # Any failure waits CARD_BACKOFF, so one failing topic does not take every round's turn.
            _pace(thread, chat=chat, **({"drawn": now} if has_card else {"retry": now + CARD_BACKOFF}))
            if isinstance(exc, TelegramError) and _topic_gone(exc):
                topics.forget(session, chat=found.chat)
            raise
        if not msg or msg.get("skipped"):
            return False
        stored[key] = data_key
        write_private(base / CARD_DATA_FILE, json.dumps(dict(list(stored.items())[-200:]), ensure_ascii=False))
        _pace(thread, chat=chat, drawn=now)
        return True
    except Exception as exc:
        log(f"refresh_card failed: {type(exc).__name__}")
        return False


def keyboard_once(thread: int, chat: Optional[int] = None) -> bool:
    """The fixed keyboard is sent once per topic and then stays behind the keyboard icon
    (open only when tapped). `chat`: the project group of the topic (None = the home chat)."""
    if not thread:
        return False
    try:
        thread = int(thread)
    except (ValueError, TypeError):
        pass
    mark = thread if chat is None else thread_key(thread, chat)
    base = private_dir(state_dir())
    path = base / "keyboard-topics.json"
    lock_path = base / "keyboard-topics.lock"
    with open(lock_path, "a") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
        except OSError:
            pass
        try:
            done = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(done, list):
                done = []
        except Exception:
            done = []
        if mark in done:
            return False
        done.append(mark)
        write_private(path, json.dumps(done))
        return True


def drop_topic_state(found) -> Optional[int]:
    """Forget everything kept for the status card of the topic `found` (a topics.Place) once its session
    has left it, and return the card's message id so the caller can delete the message."""
    base = private_dir(state_dir())
    card = None
    with open(base / "status-footer.lock", "a") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
        except OSError:
            pass
        for name in ("status-footer.json", STATUS_TEXT_FILE, "status-labels.json", CARD_DATA_FILE, CARD_PACE_FILE):
            data = _read_map(base / name)
            if found.key in data:
                value = data.pop(found.key)
                if name == "status-footer.json" and type(value) is int:
                    card = value
                write_private(base / name, json.dumps(data, ensure_ascii=False))
    mark = found.thread if found.home else found.key
    with open(base / "keyboard-topics.lock", "a") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
        except OSError:
            pass
        try:
            done = json.loads((base / "keyboard-topics.json").read_text(encoding="utf-8"))
        except Exception:
            done = []
        if isinstance(done, list) and mark in done:
            write_private(base / "keyboard-topics.json", json.dumps([x for x in done if x != mark]))
    return card


def log(line: str) -> None:
    """Failures only, never message content."""
    try:
        path = private_dir(state_dir()) / "mirror.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
        os.chmod(path, 0o600)
    except OSError:
        pass


OUTCOMES = "delivery-outcomes.log"
OUTCOMES_KEEP = 2000


def record_outcome(ok: bool, now: Optional[float] = None) -> None:
    """One line per final reply, "<time> ok|fail", for the morning health report (never content)."""
    try:
        path = private_dir(state_dir()) / OUTCOMES
        line = f"{int(time.time() if now is None else now)} {'ok' if ok else 'fail'}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        os.chmod(path, 0o600)
        if path.stat().st_size > OUTCOMES_KEEP * 20:
            rows = path.read_text(encoding="utf-8").splitlines()[-OUTCOMES_KEEP:]
            write_private(path, "\n".join(rows) + "\n")
    except OSError:
        pass


class Mirror:
    def __init__(self, cfg: Config, bot, topics: TopicMap, team_topics: Optional[TopicMap] = None):
        self.cfg, self.bot, self.topics, self.team_topics = cfg, bot, topics, team_topics
        self.rtl = L.is_rtl(cfg.target_language)
        self.dropped = 0  # parts the OWNER did not receive: a reply that failed to arrive
        self.team_dropped = 0  # parts the team group did not receive (the owner's copy arrived)
        self._held: list = []  # (placeholder, owner-only link) of the reply being sent: see send_reply
        self.last_error: Optional[TelegramError] = None  # the last error that dropped a part
        self.first_reply_id: Optional[int] = None  # the first reply message delivered since it was reset
        # While a list: every message _post delivers is added as {chat, id, text, html, team}, so an
        # instant reply can later be edited in place to its translation (see _send_instant).
        self._track: Optional[list] = None
        self._team_also = ""  # while an instant reply is edited: its original, for the team filter

    # -- low level ---------------------------------------------------------

    def _chat(self, session: str) -> int:
        """The chat this session's topic is in: its project group, else the home chat."""
        found = self.topics.place_of(session) if session else None
        return found.chat if found and not found.home else home_chat(self.cfg)

    def _reply_keyboard(self, footer: str = "", session: str = "") -> dict:
        # The same keyboard as the status card (account, mode, thinking, model labels): two layouts
        # side by side made topics look different from each other.
        found = self.topics.place_of(session) if session else None
        return live_keyboard(footer, base_kb=get_keyboard(self.cfg.ui_language), lang=self.cfg.ui_language,
                             thread=found.thread if found else None, session=session or None,
                             chat=found.chat if found and not found.home else None)

    def _yes_no_keyboard(self) -> dict:
        """Yes/no answer buttons for a reply that ends in a question."""
        s = L.strings(self.cfg.target_language)
        return {"inline_keyboard": [[{"text": s["btn_no"], "callback_data": "say:no"},
                                    {"text": s["btn_yes"], "callback_data": "say:yes"}],
                                   menus.unbuttons_row(getattr(self.cfg, "ui_language", None), later=True)]}

    def _post(self, chat_id: int, topics: TopicMap, session: str, title: str, html_text: str,
              main_chat_fallback: bool = True, reply_markup: Optional[dict] = None,
              reply_parameters: Optional[dict] = None, footer: str = "", record_reply: bool = False) -> bool:
        """One silent message into the session's topic. On HTTP 429 Telegram's retry_after is
        honoured once (capped); a plain-text retry happens ONLY for an HTML parse error, so
        nothing is ever sent twice; a deleted topic is re-created and the send retried once.
        A session placed in a project group is sent there, whatever `chat_id` says.
        Room for the footer is reserved, so the final part never crosses the 4096 limit and
        cuts it. Never raises for a Telegram problem."""
        waited = False  # one 429 wait per part
        attempt = 0
        while attempt < 2:
            thread = topics.ensure(session, title)
            if not thread and not main_chat_fallback:
                return False
            found = topics.place_of(session) if thread else None
            where = found.chat if found and not found.home else chat_id
            body = html_text if thread else f"\U0001F916 <b>{html.escape(title, quote=False)}</b>\n\n" + html_text
            mark = L.RLM if self.rtl else ""
            text = mark + body + footer
            params = {"chat_id": where, "disable_notification": True, "text": text,
                      "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
            if reply_parameters:
                params["reply_parameters"] = reply_parameters
            if thread:
                params["message_thread_id"] = thread
                if reply_markup:
                    if "keyboard" in reply_markup:
                        if keyboard_once(thread, chat=found.away if found else None):
                            params["reply_markup"] = reply_markup
                    else:
                        params["reply_markup"] = reply_markup
            try:
                result = self.bot.call("sendMessage", params)
                if record_reply and thread:
                    from .topics import record_reply as remember_reply
                    remember_reply(where, thread, result.get("message_id"))
                    if self.first_reply_id is None:
                        self.first_reply_id = result.get("message_id")
                self._note_sent(where, result, params, topics)
                return True
            except TelegramError as e:
                if _is_thread_error(e) and thread and attempt == 0:
                    topics.forget(session, chat=found.chat if found else None)  # the topic was deleted: make a new one and retry
                    attempt += 1
                    continue
                if e.code == 429 and not waited:
                    waited = True
                    delay = e.retry_after if isinstance(e.retry_after, (int, float)) and e.retry_after > 0 else 1.0
                    delay = max(delay, flood.remaining(where))  # the shared gate may know a longer pause
                    time.sleep(min(delay, RETRY_AFTER_CAP))
                    continue  # the send is retried once; the thread retry stays available
                if not _is_parse_error(e):
                    log(f"send failed (code {e.code}); part dropped")
                    self.last_error = e
                    break
                log(f"html send failed (code {e.code}); retrying as plain text")
                params.pop("parse_mode", None)
                params["text"] = _plain(text)[:TELEGRAM_LIMIT]
                try:
                    result = self.bot.call("sendMessage", params)
                    if record_reply and thread:
                        from .topics import record_reply as remember_reply
                        remember_reply(where, thread, result.get("message_id"))
                        if self.first_reply_id is None:
                            self.first_reply_id = result.get("message_id")
                    self._note_sent(where, result, params, topics)
                    return True
                except TelegramError as e2:
                    log(f"plain send failed too (code {e2.code}); part dropped")
                    self.last_error = e2
                    break
        self.dropped += 1
        return False

    def _team_text(self, html_text: str) -> str:
        """The team group's copy of one part: owner-only links hidden, and withheld when a block pattern
        matches it OR the original it translates (`self._team_also`): a translation must never reveal
        what the pattern blocked in English."""
        shared = hide_owner_links(hide_links(html_text, self._held))
        plain = _plain(shared)
        if self._team_also:
            plain += "\n" + self._team_also
        return T.filter_part(shared, plain, self.cfg.team, self.cfg.target_language)

    def _note_sent(self, chat, result, params: dict, topics: TopicMap) -> None:
        """Remember a delivered message for a later in-place edit (only while self._track is a list)."""
        if self._track is None or not isinstance(result, dict) or type(result.get("message_id")) is not int:
            return
        self._track.append({"chat": chat, "id": result["message_id"], "text": params.get("text", ""),
                            "html": params.get("parse_mode") == "HTML", "team": topics is not self.topics})

    def _emit(self, session: str, title: str, html_text: str, footer_html: str = "",
              reply_markup: Optional[dict] = None, reply_parameters: Optional[dict] = None,
              record_reply: bool = True, to_owner: bool = True, to_team: bool = True) -> int:
        """Send one part to the owner and, for a shared session, to the team group (no footer).
        A team copy that fails must not fail the owner's delivery (a review finding): the reply
        arrived, so it is never re-sent and the Stop worker still records it as delivered.
        `to_owner` / `to_team` limit the send to one side (an instant reply's follow-up goes only
        where the in-place edit did not land)."""
        sent = 0
        if to_owner:
            sent = int(self._post(home_chat(self.cfg), self.topics, session, title,
                                  restore_links(html_text, self._held, escape=True),
                                  reply_markup=reply_markup, reply_parameters=reply_parameters,
                                  footer=footer_html, record_reply=record_reply))
        team = self.cfg.team
        if to_team and team.shares(session) and self.team_topics is not None:
            # An owner-only link (secure handoff, Claude sign-in) is for the owner's copy; the team gets
            # "[redacted]" (an earlier version also cut it from the owner's own copy of a shared session). The
            # pattern pass is a second net for a link that was never held.
            out = self._team_text(html_text)
            before, self.dropped = self.dropped, 0
            try:
                try:
                    if not self._post(team.chat_id, self.team_topics, session, title, out, main_chat_fallback=False):
                        self.team_dropped += 1
                except (TelegramError, OSError):
                    self.team_dropped += 1
            finally:
                failed, self.dropped = self.dropped, before
            if failed:
                log("team copy failed; the owner's reply was delivered")
        return sent

    @staticmethod
    def _footer(text: str) -> str:
        """The status lines under a reply, behind a rule. English sits in a <pre> block (monospace, every
        line kept as written). Hebrew cannot: Telegram draws <pre> and quotes left to right, which
        scrambles Hebrew and its numbers, so each line is italic text that starts right to left."""
        if not text:
            return ""
        if fmt.HEB.search(text):
            body = "\n".join(("\u200f" + html.escape(l, quote=False)) if l else "" for l in text.split("\n"))
            return f"\n\n— — —\n<i>{body}</i>"
        return f"\n\n— — —\n<pre>{html.escape(text, quote=False)}</pre>"

    def _fit(self, parts: list, budget: int) -> list:
        """Parts that fit `budget` once their held links are back (a link is far longer than its
        placeholder). An oversized part is re-split as escaped plain text; a placeholder is one unit
        that is never cut, so the owner keeps the link whole and the team never sees a piece of it."""
        held = dict(self._held)
        fitted = []
        for part in parts:
            if len(restore_links(part, self._held, escape=True)) <= budget:
                fitted.append(part)
                continue
            current, size = "", 0
            for i, piece in enumerate(re.split("(" + PLACEHOLDER.pattern + ")", _plain(part))):
                if i % 2:  # a placeholder: one unit, as long as the link that will replace it
                    units = [(piece, len(html.escape(held.get(piece, piece), quote=False)))]
                else:
                    units = [(e, len(e)) for e in (html.escape(c, quote=False) for c in piece)]
                for text, cost in units:
                    if current and size + cost > budget:
                        fitted.append(current)
                        current, size = "", 0
                    current, size = current + text, size + cost
            if current:
                fitted.append(current)
        return fitted

    def _send_layout(self, session: str, title: str, md: str, pin: bool, footer: str,
                     reply_keyboard: bool = True, quick_buttons: Optional[bool] = None,
                     reply_parameters: Optional[dict] = None, slots: Optional[list] = None) -> int:
        """Send a laid-out reply. With `slots` (a list), one entry per part is appended: the messages that
        part became (owner and team copies), its inline keyboard and whether it carries the footer."""
        if quick_buttons is not None:
            reply_keyboard = quick_buttons
        footer = footer[:1000]
        parts = fmt.layout(md, self.cfg.target_language, pin_last=pin)
        # Escape expansion and a large footer can exceed Telegram's limit. Re-split
        # oversized HTML as escaped plain text so no reply text is silently discarded.
        parts = self._fit(parts, max(1, TELEGRAM_LIMIT - 200 - len(self._footer(footer))))
        sent = 0
        for i, msg in enumerate(parts):
            is_last = (i == len(parts) - 1)
            kb = None
            if reply_keyboard and is_last:
                # A question in the reply's last characters gets yes/no answer buttons;
                # any other reply ends with the fixed one-time keyboard.
                kb = (self._yes_no_keyboard() if "?" in md[-QUESTION_TAIL:] else self._reply_keyboard(footer, session))
            rp = reply_parameters if i == 0 else None
            asked = bool(kb and "inline_keyboard" in kb)
            if slots is not None:
                self._track = []
            try:
                sent_part = self._emit(session, title, msg, self._footer(footer) if is_last and not asked else "",
                                       reply_markup=kb, reply_parameters=rp)
            finally:
                if slots is not None:
                    slots.append({"msgs": self._track or [], "kb": kb if asked else None,
                                  "footer": is_last and not asked, "asked": asked})
                self._track = None
            sent += sent_part
            if asked and footer and sent_part:
                found = self.topics.place_of(session)
                if found:
                    try:
                        self.bot.call("sendMessage", {"chat_id": self._chat(session),
                                      "message_thread_id": found.thread, "text": self._footer(footer).strip(),
                                      "parse_mode": "HTML", "disable_notification": True,
                                      "reply_markup": self._reply_keyboard(footer, session)})
                    except TelegramError:
                        pass
        return sent

    def _send_original(self, session: str, title: str, prepared: str, folded: bool,
                       reply_keyboard: bool = False, quick_buttons: Optional[bool] = None,
                       reply_parameters: Optional[dict] = None) -> int:
        if quick_buttons is not None:
            reply_keyboard = quick_buttons
        label = html.escape(L.strings(self.cfg.target_language)["original"], quote=False)
        sent = 0
        msgs = []
        for part in fmt.chunks(prepared):
            body = fmt.to_html(part)
            if folded:
                msgs.append(f"\U0001F310 <b>{label}</b>\n<blockquote expandable>{body}</blockquote>")
            else:
                msgs.append(f"\U0001F310 <b>{label}</b>\n\n{body}")
        msgs = self._fit(msgs, TELEGRAM_LIMIT - 200)
        for i, msg in enumerate(msgs):
            is_last = (i == len(msgs) - 1)
            kb = self._reply_keyboard(session=session) if (reply_keyboard and is_last) else None
            rp = reply_parameters if i == 0 else None
            # The folded original under the translation is not the newest reply: the translation's
            # yes/no buttons above it must still count as the last question.
            sent += self._emit(session, title, msg, reply_markup=kb, reply_parameters=rp, record_reply=not folded)
        return sent

    # -- replies -----------------------------------------------------------

    def _index_reply(self, session: str, message_id: Optional[int], text: str, has_summary: bool) -> None:
        """Add the reply just delivered (its first message `message_id`) to the topic's pinned replies
        index. Runs last, after the voice summary: it never fails or delays the reply."""
        if message_id is None:
            return
        paras = [p for p in text.split("\n\n") if p.strip()]
        line = (paras[-1] if has_summary else paras[0]) if paras else ""
        found = self.topics.place_of(session)
        chat = self._chat(session)
        pins.update_index(self.cfg, self.bot, found.thread if found else None, message_id, line,
                          chat=found.away if found else None)
        full = alerts.allowed("full", self.cfg, self.bot)  # the owner's saved choice, on a trusted channel
        if full or (has_summary and line.strip()):  # the alerts channel gets every reply, silently
            thread = found.thread if found else None
            url = pins.link(chat, thread, message_id) if thread and str(chat).startswith("-100") else ""
            if full:
                # `text` is what the owner got with every owner-only link already hidden (see send_reply),
                # never the expanded one; send_reply() redacts it again. Replaces the summary copy.
                alerts.send_reply(self.cfg, text, session, bot=self.bot, url=url)
                return
            # Plain text, and always redacted: the channel may have readers the session does not.
            plain = fmt.unmix(redact(pins.strip_markup(line).strip()))
            alerts.alert(self.cfg, plain, session, bot=self.bot, silent=True, url=url, kind="summary")

    def send_reply(self, title: str, session: str, original: str,
                   translate_fn: Optional[Callable[[str], str]] = None, footer: str = "",
                   answer_to: Optional[dict] = None) -> int:
        """Send a reply. With translate_fn the translation goes first when it is ready within TRANSLATE_BUDGET, with
        the original folded under it; otherwise the original goes out first and the translation follows (config
        `original`: "folded-after" default, "before", "off"). Returns the number of messages delivered to the owner."""
        original = original.strip()
        if not original:
            return 0
        # Owner-only links (secure handoff, Claude sign-in) travel as placeholders until _emit: redaction
        # leaves them whole, no split can cut one in two (review of #47), a translation engine never sees
        # them, and the team copy gets "[redacted]" in their place. The owner's copy gets them back.
        original, self._held = hold_links(original)
        held = self._held
        # A shared session is always redacted, whatever redact_outgoing says.
        if self.cfg.redact_outgoing or self.cfg.team.shares(session):
            original = hide_owner_links(redact(original))  # a link held whole is a placeholder: untouched
        reply_params = None
        if (answer_to and answer_to.get("chat") == self._chat(session)
                and type(answer_to.get("id")) is int and answer_to["id"] > 0):
            reply_params = {"message_id": answer_to["id"], "allow_sending_without_reply": True}
        summary = fmt.split_summary(original)[1]
        prepared = fmt.join_summary(original)
        lang = self.cfg.target_language
        pin = summary is not None
        self.first_reply_id = None
        if not (translate_fn and needs_translation(prepared, lang)):
            sent = self._send_layout(session, title, prepared, pin, footer, reply_keyboard=True,
                                     reply_parameters=reply_params)
            first = self.first_reply_id
            if sent and L.base(lang) == "he":
                voice_summary(self.cfg, self.bot, session, hide_links(prepared, held), topics=self.topics)
            self._index_reply(session, first, hide_links(prepared, held), pin)
            return sent
        if self.cfg.original in ("edit", "edit-off"):
            return self._send_instant(title, session, prepared, translate_fn, footer, reply_params, held, pin)
        return self._send_translated(title, session, prepared, translate_fn, footer, reply_params, held, pin)

    # -- instant reply, edited in place to its translation ------------------

    def _translated_footer(self, footer: str, engines) -> str:
        """The footer of a translated reply: "Translated by <engines>" above the status lines."""
        lang = self.cfg.target_language
        if not engines:
            return footer
        names = ", ".join(engines)
        inline = fmt.isolate(names) if L.base(lang) == "he" else names
        # Too long to sit inside a Hebrew line: the names get a line of their own.
        by = t("translated_by", lang=lang, engines=inline) if inline else \
            t("translated_by", lang=lang, engines="").rstrip() + "\n" + names
        return "\n".join(filter(None, (by, footer)))

    def _edit_call(self, params: dict) -> str:
        """One editMessageText through the shared flood gate (no budget: this is the owner's reply).
        A closed gate or a 429 is waited out for as long as Telegram asks, up to EDIT_FLOOD_WAIT in
        all. Returns "ok" (also for "message is not modified"), "parse" (Telegram could not parse the
        HTML), "flood" (still limited: the caller must not send around the gate) or "failed"."""
        waited = 0.0
        while True:
            try:
                flood.optional_call(self.bot, "editMessageText", params, budget=False)
                return "ok"
            except TelegramError as e:
                if "not modified" in str(e).lower():
                    return "ok"
                if e.code == 429:
                    delay = e.retry_after if isinstance(e.retry_after, (int, float)) and e.retry_after > 0 else 1.0
                    delay = max(delay, flood.remaining(params.get("chat_id")))
                    if waited + delay > EDIT_FLOOD_WAIT:
                        log(f"edit postponed by flood control ({delay:.0f}s asked)")
                        return "flood"
                    time.sleep(delay)
                    waited += delay
                    continue
                if _is_parse_error(e):
                    return "parse"
                log(f"edit failed (code {e.code})")
                return "failed"
            except OSError:
                log("edit failed (network)")
                return "failed"

    def _edit_message(self, msg: dict, text: str, markup: Optional[dict] = None, html_mode: bool = True) -> str:
        """Replace the text of one delivered message (`msg` from the tracker) with `markup` as its inline
        keyboard (an edit without one removes it). A parse error is retried as plain text. Returns
        "ok", "flood" or "failed" (see _edit_call)."""
        params = {"chat_id": msg["chat"], "message_id": msg["id"], "text": text,
                  "link_preview_options": {"is_disabled": True}}
        if html_mode:
            params["parse_mode"] = "HTML"
        if markup and "inline_keyboard" in markup:
            params["reply_markup"] = markup
        if len(text) > TELEGRAM_LIMIT:
            return "failed"
        outcome = self._edit_call(params)
        if outcome == "parse" and html_mode:
            params.pop("parse_mode", None)
            params["text"] = _plain(text)[:TELEGRAM_LIMIT]
            outcome = self._edit_call(params)
        return "failed" if outcome == "parse" else outcome

    def _owner_text(self, html_text: str, footer_html: str = "") -> str:
        """The owner's copy of one part, as _post builds it: links restored, right-to-left mark, footer."""
        return (L.RLM if self.rtl else "") + restore_links(html_text, self._held, escape=True) + footer_html

    def _note_unavailable(self, slots: list) -> None:
        """The translation failed or timed out: the original stays, and its last message says so."""
        lang = self.cfg.target_language
        for slot in reversed(slots):
            owner = next((m for m in slot["msgs"] if not m["team"]), None)
            if owner is None:
                continue
            note = t("translation_unavailable", lang=lang)
            if owner["html"]:
                note = "<i>" + html.escape(note, quote=False) + "</i>"
            text = owner["text"] + "\n\n" + note
            if len(text) <= TELEGRAM_LIMIT:
                self._edit_message(owner, text, slot.get("kb"), html_mode=owner["html"])
            return

    def _instant_targets(self, shown: str, prepared: str, pin: bool, footer: str, fold: bool,
                         at_least: int = 0) -> list:
        """The translated reply as message bodies, in order: the translation's parts, the last one with
        the original folded under it when that fits (else the folded original follows on its own) and
        the footer at the very end of the last translated part. Each item is (html, footer_html)."""
        foot = self._footer(footer[:1000])
        parts = fmt.layout(shown, self.cfg.target_language, pin_last=pin)
        parts = self._fit(parts, max(1, TELEGRAM_LIMIT - 200 - len(foot)))
        if not parts:
            return []
        folds: list = []
        if fold:
            label = html.escape(L.strings(self.cfg.target_language)["original"], quote=False)
            folds = self._fit([f"\U0001F310 <b>{label}</b>\n<blockquote expandable>{fmt.to_html(c)}</blockquote>"
                               for c in fmt.chunks(prepared)], TELEGRAM_LIMIT - 200)
        out = [(p, "") for p in parts]
        last = parts[-1]
        size = lambda body: len(restore_links(body, self._held, escape=True)) + len(foot) + 2  # noqa: E731
        # Merged only when that still fills every message already sent: a spare would have to be deleted.
        if folds and size(last + "\n\n" + folds[0]) <= TELEGRAM_LIMIT - 100 and len(parts) + len(folds) > at_least:
            last = last + "\n\n" + folds.pop(0)
        out[-1] = (last, foot)
        return out + [(f, "") for f in folds]

    def _send_instant(self, title: str, session: str, prepared: str, translate_fn: Callable[[str], str],
                      footer: str, reply_params: Optional[dict], held, pin: bool) -> int:
        """The reply goes out at once exactly as Claude wrote it; the translation runs meanwhile and then
        replaces it IN PLACE (each part edited), with the original folded underneath (config `original`:
        "edit"; "edit-off" drops the fold). Parts the edit cannot hold go out as follow-ups; a failed or
        timed-out translation leaves the original with a short "translation unavailable" note. Edits are
        silent, so the owner is notified once, by the first send."""
        lang = self.cfg.target_language
        box = self._translate_in_thread(prepared, translate_fn, lang)
        slots: list = []
        self.first_reply_id = None
        sent = self._send_layout(session, title, prepared, pin, footer, reply_keyboard=True,
                                 reply_parameters=reply_params, slots=slots)
        first = self.first_reply_id
        shown_at = time.monotonic() - box["t0"]
        th = box["thread"]
        th.join(TRANSLATE_TIMEOUT)
        took = time.monotonic() - box["t0"]
        shown: Optional[str] = None if th.is_alive() else box["shown"]
        failed = th.is_alive() or box["error"] is not None
        if shown is not None:  # an engine can alter a placeholder: what is left of one becomes "[redacted]"
            shown = scrub_placeholders(shown, held)
        if failed or shown is None or shown.strip() == prepared.strip():
            log(f"reply shown in {shown_at:.1f}s; " + (f"translation failed after {took:.1f}s; original kept"
                                                        if failed else "translation unchanged; original kept"))
            if failed and first is not None:
                self._note_unavailable(slots)
            self._index_reply(session, first, hide_links(prepared, held), pin)
            return sent
        footer = self._translated_footer(footer, box["engines"])
        asked = any(slot.get("asked") for slot in slots)
        targets = self._instant_targets(shown, prepared, pin, "" if asked else footer,
                                        fold=self.cfg.original != "edit-off", at_least=len(slots))
        self._team_also = _plain(hide_owner_links(hide_links(prepared, held)))
        try:
            edited, followups, first_shown, complete = self._apply_targets(session, title, slots, targets)
        finally:
            self._team_also = ""
        log(f"reply shown in {shown_at:.1f}s; translated in {took:.1f}s; edited {edited} part(s)"
            + (f", {followups} follow-up(s)" if followups else "") + ("" if complete else "; incomplete"))
        if L.base(lang) == "he" and (edited or followups):
            voice_summary(self.cfg, self.bot, session, hide_links(shown, held), topics=self.topics)
        # The index points at what the owner really sees: the translated first part where it arrived
        # (edited in place, or the follow-up that replaced a refused edit), otherwise the original.
        if first_shown is not None:
            self._index_reply(session, first_shown, hide_links(shown, held), pin)
        else:
            self._index_reply(session, first, hide_links(prepared, held), pin)
        return sent + followups

    def _apply_targets(self, session: str, title: str, slots: list, targets: list) -> tuple:
        """Show the translated `targets` in place of the instant reply's `slots`, for the owner and the
        team group separately. Each side edits its messages in order; from the first edit a side cannot
        make (message gone, never delivered) it gets the rest as new messages, so the translation keeps
        its order and a side that already shows a part never gets it twice. A flood limit stops all
        further changes (the originals stay; nothing is sent around the gate). The answer buttons go on
        the final part; spare originals are removed only when the whole translation arrived.
        Returns (edited, followups, id of the owner message now showing the first part, complete)."""
        owner_chat = next((m["chat"] for slot in slots for m in slot["msgs"] if not m["team"]), self._chat(session))
        team_on = self.cfg.team.shares(session) and self.team_topics is not None
        kb = next((slot["kb"] for slot in slots if slot.get("kb")), None)
        owner_broken = team_broken = False
        edited = followups = 0
        first_shown: Optional[int] = None
        complete = True
        dropped_before = self.dropped
        for i, (body, foot) in enumerate(targets):
            last = i == len(targets) - 1
            markup = kb if last else None
            slot = slots[i] if i < len(slots) else {"msgs": []}
            owner = next((m for m in slot["msgs"] if not m["team"]), None)
            team = next((m for m in slot["msgs"] if m["team"]), None)
            need_owner = owner_broken or owner is None
            if not need_owner:
                outcome = self._edit_message(owner, self._owner_text(body, foot), markup)
                if outcome == "flood":
                    complete = False
                    break
                if outcome == "ok":
                    edited += 1
                    if i == 0:
                        first_shown = owner["id"]
                else:
                    need_owner = owner_broken = True
            need_team = team_on and (team_broken or team is None)
            if team_on and not need_team:
                outcome = self._edit_message(team, self._team_text(body))
                if outcome == "flood":
                    complete = False
                    break
                if outcome != "ok":
                    need_team = team_broken = True
            if need_owner or need_team:
                if need_owner and not flood.is_open(owner_chat):
                    complete = False  # the gate is closed: the essential send would only deepen it
                    break
                self._track = []
                try:
                    got = self._emit(session, title, body, foot if need_owner else "",
                                     reply_markup=markup if need_owner else None, record_reply=bool(markup),
                                     to_owner=need_owner, to_team=need_team)
                finally:
                    track, self._track = self._track or [], None
                if need_owner:
                    followups += got
                    mine = next((m for m in track if not m["team"]), None)
                    if mine is None:
                        complete = False
                    elif i == 0:
                        first_shown = mine["id"]
        if self.dropped > dropped_before:  # the reply itself arrived: a lost follow-up is not a lost reply
            log(f"{self.dropped - dropped_before} translated follow-up(s) not delivered; the original stays")
            self.dropped = dropped_before
        if complete and not owner_broken:
            for slot in slots[len(targets):]:  # the translation needs fewer messages: the spares go away
                for m in slot["msgs"]:
                    try:
                        flood.optional_call(self.bot, "deleteMessage", {"chat_id": m["chat"], "message_id": m["id"]},
                                            budget=False)
                    except (TelegramError, OSError):
                        pass
        return edited, followups, first_shown, complete

    def _translate_in_thread(self, prepared: str, translate_fn: Callable[[str], str], lang: str) -> dict:
        """Start the translation in a worker thread; the dict gets `thread`, then `shown` / `engines` / `error`."""
        import contextvars
        import threading
        from .translate import reset_engines, used_engines
        box: dict = {"shown": None, "engines": (), "error": None}

        def work() -> None:
            try:
                reset_engines()
                box["shown"] = translate_reply(prepared, translate_fn, lang)
                box["engines"] = used_engines()
            except Exception as e:  # noqa: BLE001 - any engine failure means "original only"
                box["error"] = e

        t0 = time.monotonic()
        box["t0"] = t0
        th = threading.Thread(target=contextvars.copy_context().run, args=(work,), daemon=True)
        box["thread"] = th
        th.start()
        return box

    def _send_translated(self, title: str, session: str, prepared: str, translate_fn: Callable[[str], str],
                         footer: str, reply_params: Optional[dict], held, pin: bool) -> int:
        """Translated reply. Default: wait up to TRANSLATE_BUDGET for the translation and send the Hebrew first with the
        original folded under it. Over budget or failed: the original first (never held up), then the translation."""
        lang = self.cfg.target_language
        mode = self.cfg.original
        sent = 0
        original_id: Optional[int] = None
        hebrew_first = False
        if mode == "before":  # the original is out before the translator even starts
            sent = self._send_original(session, title, prepared, folded=False, reply_keyboard=False,
                                       reply_parameters=reply_params)
            original_id, self.first_reply_id = self.first_reply_id, None
            reply_params = None
            box = self._translate_in_thread(prepared, translate_fn, lang)
            box["thread"].join()
        else:
            box = self._translate_in_thread(prepared, translate_fn, lang)
            th = box["thread"]
            th.join(TRANSLATE_BUDGET)
            hebrew_first = not th.is_alive()
            if not hebrew_first:
                sent = self._send_original(session, title, prepared, folded=False, reply_keyboard=False,
                                           reply_parameters=reply_params)
                original_id, self.first_reply_id = self.first_reply_id, None
                reply_params = None
                th.join()
        took = time.monotonic() - box["t0"]
        shown: Optional[str] = box["shown"]
        if box["error"] is not None:
            log(f"translation failed after {took:.1f}s; original only")
            shown = None
        if shown is not None:  # an engine can alter a placeholder: what is left of one becomes "[redacted]"
            shown = scrub_placeholders(shown, held)
        unchanged = shown is None or shown.strip() == prepared.strip()
        if unchanged:
            if hebrew_first:  # nothing usable to show: the original goes out now, plain
                sent = self._send_original(session, title, prepared, folded=False, reply_keyboard=False,
                                           reply_parameters=reply_params)
                original_id, self.first_reply_id = self.first_reply_id, None
            self._index_reply(session, original_id, hide_links(prepared, held), pin)
            return sent
        log(f"reply translated in {took:.1f}s; " + ("hebrew first" if hebrew_first else
                                                       "original first (before)" if mode == "before" else
                                                       "original first (over budget)"))
        engines = box["engines"]
        if engines:
            names = ", ".join(engines)
            he = L.base(lang) == "he"
            inline = fmt.isolate(names) if he else names
            # Too long to sit inside a Hebrew line: the names get a line of their own.
            by = t("translated_by", lang=lang, engines=inline) if inline else \
                t("translated_by", lang=lang, engines="").rstrip() + "\n" + names
            footer = "\n".join(filter(None, (by, footer)))
        self.first_reply_id = None
        dropped_before = self.dropped
        sent_he = self._send_layout(session, title, shown, pin, footer, reply_keyboard=True,
                                    reply_parameters=reply_params)
        first = self.first_reply_id
        he_failed = hebrew_first and (first is None or self.dropped > dropped_before)
        if he_failed:  # the Hebrew did not (fully) arrive: the original goes out whole, never folded
            self.first_reply_id = None
            sent += self._send_original(session, title, prepared, folded=False, reply_keyboard=False,
                                        reply_parameters=reply_params if first is None else None)
            first, shown = self.first_reply_id, prepared
        elif hebrew_first and mode != "off":  # the original, folded, right under the translation
            self.first_reply_id = None
            sent += self._send_original(session, title, prepared, folded=True, reply_keyboard=False)
            self.first_reply_id = first
        if sent_he and not he_failed and L.base(lang) == "he":
            voice_summary(self.cfg, self.bot, session, hide_links(shown, held), topics=self.topics)
        if first is None:  # the translated part did not arrive: the index points to the original
            first, shown = original_id, prepared
        self._index_reply(session, first, hide_links(shown, held), pin)
        return sent + sent_he


    # -- files (owner only: files may hold anything) -------------------------

    def send_file(self, title: str, session: str, path: str, caption: str = "",
                  audio_title: Optional[str] = None, performer: Optional[str] = None) -> None:
        # a file never renames its topic: `ctb mirror --file` carries the default title "Claude", and
        # ensure() would rename a named topic to it
        title = (self.topics.all_places().get(session) or {}).get("name") or title
        path = os.path.abspath(os.path.expanduser(path))
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size > upload_limit(self.cfg):
            self._send_large_file(title, session, path)  # never uploaded: too big for a chat
            return
        if size > DOCUMENT_LIMIT and self.cfg.api_local:
            if self.bot.direct:
                self._send_file_by_path(title, session, path, caption, audio_title, performer)  # the server reads it from disk
            else:
                self._send_large_file(title, session, path, DOCUMENT_LIMIT)  # the relay carries bytes only
            return
        ext = os.path.splitext(path)[1].lower()
        copy = channel_may_copy(self.cfg, path)  # decided from the path alone, before the file is read
        opened: list = []
        payload = read_regular_file(path, DOCUMENT_LIMIT, opened)
        # The file read is the file that was checked, not one swapped in between (and still not hard-linked).
        copy = bool(copy) and bool(opened) and same_file(copy, opened[0])
        if len(payload) > upload_limit(self.cfg):
            self._send_large_file(title, session, path)
            return
        is_photo = ext in (".png", ".jpg", ".jpeg") and len(payload) <= PHOTO_LIMIT and (
            payload.startswith(b"\x89PNG\r\n\x1a\n") or payload.startswith(b"\xff\xd8\xff"))
        method, field, duration = (("sendPhoto", "photo", None) if is_photo else
                                   _media_info(ext, io.BytesIO(payload)))
        thread = self.topics.ensure(session, title)
        fields = {"chat_id": self._chat(session), "disable_notification": "true",
                  "caption": ("\U0001F4CE " + self._meta(caption or os.path.basename(path)))[:1000]}
        if thread:
            fields["message_thread_id"] = thread
        self._media_fields(fields, method, duration, path, audio_title, performer)
        filename = self._meta(os.path.basename(path))
        try:
            self.bot.upload(method, fields, field, filename, payload)
        except TelegramError as exc:
            if not self._media_rejected(method, exc):
                raise
            method, field = "sendDocument", "document"
            fields = self._document_fields(fields)
            self.bot.upload(method, fields, field, filename, payload)
        if copy:
            self._copy_file_to_channel(title, session, path, method, field, payload, caption, fields, audio_title)

    def _media_fields(self, fields, method, duration, path, audio_title, performer):
        if method == "sendAudio":
            raw_title = self._meta(audio_title if audio_title is not None else Path(path).stem)
            fields["title"] = self._safe_track_meta(raw_title if audio_title is not None else
                               re.sub(r"[._-]+", " ", raw_title).strip() or "audio")
            if performer:
                fields["performer"] = self._safe_track_meta(self._meta(performer))
        elif method == "sendVideo":
            fields["supports_streaming"] = "true"
        if method in ("sendAudio", "sendVideo") and duration is not None:
            fields["duration"] = duration

    @staticmethod
    def _safe_track_meta(value: str) -> str:
        value = "".join(c for c in value if not unicodedata.category(c).startswith("C"))
        value = re.sub(r"^(\s*)file:", r"\1file -", value, flags=re.IGNORECASE)
        return value[:1000]

    @staticmethod
    def _media_rejected(method, exc):
        return (method in ("sendAudio", "sendVideo") and exc.code == 400
                and exc.api_rejected)

    @staticmethod
    def _document_fields(fields):
        return {k: v for k, v in fields.items()
                if k not in ("title", "performer", "duration", "supports_streaming")}

    def _meta(self, text: str) -> str:
        """A file's caption or name as it leaves: the file itself cannot be redacted, its description can."""
        text = hide_owner_links(text or "")
        return redact(text) if self.cfg.redact_outgoing else text

    def _copy_file_to_channel(self, title: str, session: str, path: str, method: str, field: str,
                              payload: bytes, caption: str, fields: dict, audio_title: Optional[str]) -> None:
        """After the topic has the file: a silent copy in the alerts channel when the owner turned files
        on. A file cannot be redacted, so nothing that looks like a credential goes (see channel_may_copy),
        nor a file whose bytes hold a secret pattern. This runs in the detached mirror process, never on
        the poll loop, and a failure never touches the topic."""
        try:
            # The cheap checks first: a scan of the bytes costs time and is pointless when nothing is sent.
            if not alerts.allowed("files", self.cfg, self.bot) or looks_secret(payload):
                return
            media_fields = {k: fields[k] for k in ("title", "performer", "duration", "supports_streaming")
                            if k in fields}
            if method == "sendAudio" and audio_title is None:
                media_fields["title"] = self._safe_track_meta(re.sub(r"[._-]+", " ", alerts._clean(Path(path).stem)).strip() or "audio")
            alerts.send_file(self.cfg, session, title, method, field, os.path.basename(path), payload, caption,
                             bot=self.bot, media_fields=media_fields)
        except Exception:
            log("alerts channel file copy failed")

    def _send_file_by_path(self, title: str, session: str, path: str, caption: str,
                           audio_title: Optional[str], performer: Optional[str]) -> None:
        """Local Bot API server, file over 50 MB: send a file:// URI, never the bytes (2 GB stays on disk).
        The server re-opens the file by name, so it gets a private staged copy that is deleted afterwards."""
        thread = self.topics.ensure(session, title)
        fields = {"chat_id": self._chat(session), "disable_notification": "true",
                  "caption": ("\U0001F4CE " + self._meta(caption or os.path.basename(path)))[:1000]}
        if thread:
            fields["message_thread_id"] = thread
        job, staged = stage_upload(self.cfg, path, upload_limit(self.cfg))
        try:
            with open(staged, "rb") as stream:
                method, field, duration = _media_info(Path(path).suffix.lower(), stream)
            self._media_fields(fields, method, duration, path, audio_title, performer)
            try:
                self.bot.upload_path(method, fields, field, staged)
            except TelegramError as exc:
                if not self._media_rejected(method, exc):
                    raise
                self.bot.upload_path("sendDocument", self._document_fields(fields), "document", staged)
        finally:
            shutil.rmtree(job, ignore_errors=True)

    def _send_large_file(self, title: str, session: str, path: str, limit: Optional[int] = None) -> None:
        """A file over the upload limit is not sent to Telegram. With a configured share base URL
        it is placed in the private share directory (share_publish) and only its link is sent;
        without one, a short 'too large' note (no silent missing files)."""
        name = self._meta(os.path.basename(path))
        thread = self.topics.ensure(session, title)
        s = L.strings(self.cfg.target_language)
        mark = L.RLM if self.rtl else ""
        base = (self.cfg.share_base_url or "").strip()
        if self.cfg.api_local or limit is not None:  # cloud mode keeps the original fixed wording
            note = s["file_too_large_n"].format(mb=(limit or upload_limit(self.cfg)) >> 20)
        else:
            note = s["file_too_large"]
        text = f"{mark}\U0001F4CE {html.escape(name, quote=False)} · {note}"
        if base:
            try:
                url = share_publish(path, base)
                text = f"{mark}\U0001F4CE {html.escape(name, quote=False)}\n{url}"
            except (OSError, ValueError):
                pass
        params = {"chat_id": self._chat(session), "text": text, "disable_notification": True,
                  "link_preview_options": {"is_disabled": True}}
        if thread:
            params["message_thread_id"] = thread
        try:
            self.bot.call("sendMessage", params)
        except TelegramError:
            log("large-file note failed")


STAGING_DIR = "ctb-upload-staging"
STAGING_MAX_AGE = 24 * 3600  # leftovers of a crashed send are pruned after a day


def _ident(st) -> tuple:
    return (st.st_dev, st.st_ino)


def inside_bridge_folders(real: str) -> bool:
    """Whether the (real) path is in the bridge's own config or state folder (compared by inode, so letter
    case and hard links do not matter). A folder that cannot be examined raises OSError."""
    from .config import config_dir
    private = set()
    for folder in (config_dir(), state_dir()):
        try:
            private.add(_ident(os.stat(folder)))
        except OSError:
            pass
    here = os.path.dirname(real)
    while True:
        if _ident(os.stat(here)) in private:
            return True
        parent = os.path.dirname(here)
        if parent == here:
            return False
        here = parent


# Files the alerts channel never gets: its readers are not the owner. Names are compared in lower case.
DENIED_DIRS = {".ssh", ".aws", ".gnupg", ".config", ".kube", ".docker", ".azure", ".claude", ".codex", ".gemini",
               ".agents", ".password-store", "keychains"}
DENIED_NAMES = {".netrc", "_netrc", ".npmrc", ".pypirc", ".pgpass", ".git-credentials", "credentials",
                "credentials.json", "secrets.json", "authorized_keys", "known_hosts", ".htpasswd", "pass.txt",
                "kubeconfig", "cookies.sqlite", "login data"}
DENIED_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".keychain", ".keychain-db", ".gpg",
                   ".asc", ".ppk")
_KEY_NAME = re.compile(r"id_(?:rsa|dsa|ecdsa|ed25519)\S*")
DENIED_NAME_PARTS = ("secret", "credential", "password", "api-key", "api_key", ".env.")  # secrets.yaml, a.env.prod
DENIED_NAME_STARTS = (".env", "env.", "creds", "service-account", "service_account")  # .env.local, env.prod, creds.txt
_DENIED_PAIR = (".local", "share")  # ~/.local/share holds keyrings, browser data and tool state


def _denied_name(name: str) -> bool:
    return (name in DENIED_NAMES or name.startswith(DENIED_NAME_STARTS) or name.endswith((".env", "_history"))
            or name.endswith(DENIED_SUFFIXES) or any(part in name for part in DENIED_NAME_PARTS)
            or ("token" in name and name.endswith(".json")) or bool(_KEY_NAME.fullmatch(name)))


def channel_may_copy(cfg: Config, path: str) -> Optional[tuple]:
    """Whether a file the session sent may also go to the alerts channel, decided from its real path
    before anything is read or uploaded: not a credential path or name, not Claude's home or profiles, not
    the bridge's own folders or the local server's file directory, not hard-linked. Any doubt is a no (None);
    a yes is the file's (device, inode), to compare with the file that is then opened (see same_file)."""
    try:
        from . import accounts
        real = os.path.realpath(os.path.expanduser(path))
        parts = [x.lower() for x in real.split(os.sep) if x]
        name = parts[-1] if parts else ""
        folders = parts[:-1]
        if (set(folders) & DENIED_DIRS or _denied_name(name)
                or any(folders[i:i + 2] == list(_DENIED_PAIR) for i in range(len(folders)))):
            return None
        checked = os.stat(real)
        if checked.st_nlink > 1:  # a hard link can hide a credential file behind an innocent name
            return None
        roots = [accounts.claude_home(), accounts.profiles_dir(), accounts.claude_json()]
        if getattr(cfg, "api_files_dir", None):
            roots.append(cfg.api_files_dir)
        for root in roots:
            top = os.path.realpath(os.path.expanduser(str(root)))
            if real == top or real.startswith(top.rstrip(os.sep) + os.sep):
                return None
        return None if inside_bridge_folders(real) else (checked.st_dev, checked.st_ino)
    except Exception:
        return None


def same_file(ident, opened) -> bool:
    """Whether `opened` (the fstat of the file that was read) is the file channel_may_copy checked, and the
    only link to it."""
    return (isinstance(ident, tuple) and (opened.st_dev, opened.st_ino) == ident and opened.st_nlink == 1)


SCAN_LIMIT = 4 << 20  # bytes of a file the channel copy will scan; a bigger one is not copied
SCAN_SLICE = 32 << 10  # small, so the time limit is looked at often
SCAN_SECONDS = 5.0


def looks_secret(payload: bytes) -> bool:
    """Anything the channel's text cleaning would change, anywhere in the bytes: the same patterns, so a
    file that would not pass as a message does not pass as a file. Any doubt is a yes: a file over
    SCAN_LIMIT, or a scan that runs past SCAN_SECONDS, counts as one (overlapping slices keep memory
    and the time of each pattern pass bounded)."""
    if len(payload) > SCAN_LIMIT:
        return True
    deadline = time.monotonic() + SCAN_SECONDS
    overlap = 1024
    for start in range(0, max(len(payload), 1), SCAN_SLICE):
        if time.monotonic() > deadline:
            return True
        part = payload[start:start + SCAN_SLICE + overlap].decode("utf-8", "ignore")
        if redact_channel(part) != part:
            return True
    return time.monotonic() > deadline


def stage_upload(cfg: Config, path: str, limit: int):
    """Copy a file for a local Bot API server, which re-opens uploads by name (so the original could be
    swapped under it). The source is opened without following a symlink and must be a regular file
    with one link, outside the bridge's own config and state folders (compared by inode, so letter case
    and hard links do not matter). The copy goes in chunks into a private folder (0700) under
    api_files_dir (what the server can see), else the state dir. Returns (job folder, staged path);
    the caller deletes the job folder after the send."""
    from .telegram import copy_limited, open_regular_file
    src, size = open_regular_file(path, limit)
    with src:
        st = os.fstat(src.fileno())
        if st.st_nlink != 1:
            raise TelegramError("file has several hard links; refused")
        real = os.path.realpath(path)
        if _ident(os.stat(real)) != _ident(st):
            raise TelegramError("file changed while it was opened")
        if inside_bridge_folders(real):
            raise TelegramError("file is inside the bridge's own folders; refused")
        files_dir = cfg.api_files_dir
        root = private_dir((Path(files_dir) if files_dir and os.path.isdir(files_dir) else state_dir()) / STAGING_DIR)
        now = time.time()
        for old in root.iterdir():
            try:
                if now - old.stat().st_mtime > STAGING_MAX_AGE:
                    shutil.rmtree(old, ignore_errors=True)
            except OSError:
                pass
        job = private_dir(root / secrets.token_hex(16))
        staged = job / opaque_name(path)  # the server sees an opaque name, never the original one
        try:
            fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                copy_limited(src, out, limit)
        except BaseException:
            shutil.rmtree(job, ignore_errors=True)
            raise
    return job, str(staged)


def build(cfg: Config) -> Mirror:
    bot = Bot.from_config(cfg)
    team = TopicMap(state_dir() / "topics-team.json", bot, cfg.team.chat_id) if cfg.team.enabled else None
    return Mirror(cfg, bot, TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg), team)


def opaque_name(path: str) -> str:
    """A file name that tells nothing: "file" plus the original's short extension when it is plain."""
    ext = os.path.splitext(os.path.basename(path or ""))[1].lower()
    return "file" + (ext if 1 < len(ext) <= 8 and ext[1:].isalnum() else "")


def share_publish(path: str, base_url: str, root: Optional[Path] = None) -> str:
    """Link a verified file into the private share directory, or copy if linking fails,
    under a fresh unguessable token. Only the link leaves the machine.
    The directory is never listed; each file is reached only through its token in the URL. Old entries are pruned on every publish."""
    root = Path(root) if root else state_dir() / "share"
    token = secrets.token_urlsafe(12)
    safe = opaque_name(path)  # the link never carries the original name (it may hold a secret)
    # Verify a hard link against the opened file; copy from the descriptor if linking fails.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("share source is not a regular file")
        if opened.st_size > SHARE_LIMIT:
            raise ValueError("share source is too large")
        target = private_dir(root / token) / safe
        try:
            try:
                os.link(path, target, follow_symlinks=False)
                linked = target.stat(follow_symlinks=False)
                if (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino):
                    target.unlink()
                    raise OSError("share source changed")
            except OSError:
                with target.open("xb") as dest:
                    shutil.copyfileobj(source, dest)
        except BaseException:
            shutil.rmtree(target.parent, ignore_errors=True)
            raise
    share_prune(SHARE_DAYS, root)
    return f"{base_url.rstrip('/')}/{quote(token)}/{quote(safe)}"


def share_prune(days: int = SHARE_DAYS, root: Optional[Path] = None, now: Optional[float] = None) -> int:
    """Delete share entries older than `days` days. Returns how many were removed."""
    root = Path(root) if root else state_dir() / "share"
    if days <= 0 or not root.is_dir():
        return 0
    cutoff = (now or time.time()) - days * 86400
    removed = 0
    for entry in root.iterdir():
        try:
            if entry.is_dir() and not entry.is_symlink() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
                removed += 1
        except OSError:
            pass
    return removed


LIFECYCLE_KINDS = ("precompact", "compact-done", "session-end", "subagent-start", "subagent-stop")


def _jsonl_rows(path: str, tail: int = 4_000_000) -> list:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - tail))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError, ValueError):
        return []
    rows = []
    for raw in lines:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _row_text(row: dict) -> str:
    msg = row.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def subagent_info(agent_transcript: str, transcript: str = "") -> dict:
    """Which agent finished: its task description, model, effort and result text. Claude Code writes
    the Agent call's description, model and effort to <agent transcript>.meta.json; the parent
    transcript's Agent call and the agent's own assistant rows are the fallbacks."""
    rows = _jsonl_rows(agent_transcript) if agent_transcript else []
    assistants = [r for r in rows if r.get("type") == "assistant"]
    prompt = next((_row_text(r) for r in rows if r.get("type") == "user"), "")
    result = next((t for t in (_row_text(r) for r in reversed(assistants)) if t.strip()), "")
    model = next((str((r.get("message") or {}).get("model") or "") for r in reversed(assistants)
                  if isinstance(r.get("message"), dict)), "")
    desc = ""
    for row in reversed(_jsonl_rows(transcript)) if (transcript and prompt) else []:
        msg = row.get("message")
        for block in (msg.get("content") if isinstance(msg, dict) else None) or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in ("Agent", "Task"):
                inp = block.get("input") or {}
                if isinstance(inp, dict) and str(inp.get("prompt", "")).strip()[:200] == prompt.strip()[:200]:
                    desc = str(inp.get("description") or "")
                    break
        if desc:
            break
    meta = {}
    if agent_transcript.endswith(".jsonl"):
        try:
            with open(agent_transcript[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as fh:
                loaded = json.load(fh)
            meta = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            pass
    found = re.match(r"claude-([a-z]+)-(\d+)-(\d+)", model)
    pretty = f"{found.group(1).title()} {found.group(2)}.{found.group(3)}" if found \
        else str(meta.get("model") or model).title()
    if pretty and meta.get("effort"):
        pretty += ", " + str(meta["effort"])
    return {"description": str(meta.get("description") or desc), "model": pretty, "result": result, "meta": meta}


def _summary_tail(result: str) -> str:
    """The agent's closing "Summary:" line when it wrote one, else its whole result."""
    summary = str(result or "").strip()
    for marker in ("Summary:", "סיכום:"):
        if marker in summary:
            summary = summary.rsplit(marker, 1)[1]
    return summary


def _cached_translation(text: str, cfg: Config) -> str:
    """The translation of `text` when it is already cached (no engine call); "" otherwise."""
    if not text or not cfg.translate or not L.needs_translation(text, cfg.target_language):
        return ""
    try:
        from .translate import cached
        return (cached(text, cfg) or "").strip()
    except Exception:
        return ""


def subagent_report(cfg: Config, agent_type: str, description: str, agent_transcript: str = "",
                    transcript: str = "", translate: bool = True) -> dict:
    """What the agents card shows for a finished agent: name, model (with effort), a one-line result translated
    for the owner, the full result and whether it failed. Detached child only (it may translate)."""
    try:
        info = subagent_info(agent_transcript, transcript)
    except Exception:  # a malformed transcript must never cost the card its update
        info = {"description": "", "model": "", "result": "", "meta": {}}
    name = " ".join(str(info.get("description") or description or agent_type or "").split())
    summary = " ".join(_summary_tail(info["result"]).split())[:400]
    if translate and summary and cfg.translate and L.needs_translation(summary, cfg.target_language):
        try:
            summary = translate_text(summary, cfg, fast=False).strip() or summary
        except Exception:
            pass
    return {"name": name, "model": info["model"], "summary": summary, "full": info["result"],
            "failed": agents_card.is_failure(info["result"], info.get("meta"))}


def subagent_text(cfg: Config, agent_type: str, description: str, agent_transcript: str = "",
                  transcript: str = "") -> str:
    """The SubagentStop notice: which agent, its task, model and effort, and a short summary of its
    result translated for the owner. Detached child only (reading transcripts and translating can
    take seconds). Everything is redacted."""
    s = L.strings(cfg.target_language)
    try:
        info = subagent_info(agent_transcript, transcript)
    except Exception:  # a malformed transcript must never cost the notice itself
        info = {"description": "", "model": "", "result": "", "meta": {}}
    desc = info.get("description") or str(description or "").strip()
    if not info.get("meta") and not desc:
        return ""
    who = redact(desc).strip()
    who = " ".join(x for x in (str(agent_type or ""), who) if x).strip()
    text = f"{s['lifecycle_subagent']} · {who}" if who else s["lifecycle_subagent"]
    if info["model"]:
        text += f" ({info['model']})"
    summary = redact(" ".join(_summary_tail(info["result"]).split())[:400])
    if summary:
        if cfg.translate and L.needs_translation(summary, cfg.target_language):
            try:
                summary = translate_text(summary, cfg, fast=False).strip() or summary
            except Exception:
                pass
        text += "\n" + summary
    return text[:1500] if (info["model"] or summary) else text[:80]


def run_lifecycle(cfg: Config, session: str, kind: str, agent_type: str = "", description: str = "",
                  bot=None, agent_transcript: str = "", transcript: str = "", agent_id: str = "") -> int:
    """One lifecycle notice in the session's topic (started detached by the hooks): compaction
    starting/finished, the session closed (rings, marks the topic done), or a background subagent
    started/finished (with the agents topic on, those go to the session's agents topic). Quiet on any failure."""
    try:
        if kind not in LIFECYCLE_KINDS or not session:
            return 0
        bot = bot or Bot.from_config(cfg)
        found = place(cfg, session)
        if found is None:
            return 0
        thread = found.thread
        s = L.strings(cfg.target_language)
        mark = L.RLM if L.is_rtl(cfg.target_language) else ""
        silent = True
        if kind == "precompact":
            text = s["lifecycle_precompact"]
        elif kind == "compact-done":
            text = s["lifecycle_compact_done"]
        elif kind == "session-end":
            from .inbox import consume_planned_restart
            if consume_planned_restart(session):
                return 0
            agents_topic.session_ended(cfg, session, bot)
            text, silent = s["lifecycle_session_end"], False  # the owner should notice at once
            agents_card.session_ended(session, cfg, bot)
        elif kind == "subagent-start":
            task = agent_task.lookup(session, agent_id, transcript)  # one verified link or {}; never raises
            if agents_topic.enabled():
                agents_topic.agent_started(cfg, session, agent_id, agent_type, bot, found=task)
            else:
                agents_card.agent_started(cfg, session, thread, agent_id, agent_type, bot, chat=found.chat, found=task)
            return 0
        elif agents_topic.enabled():  # the explicit separate topic is the place for these notices
            text = subagent_text(cfg, agent_type, description, agent_transcript, transcript)
            agents_topic.agent_finished(cfg, session, agent_id, (mark + text) if text else "", bot, thread)
            return 0
        else:  # the live agents card replaces one notice per agent
            # The card shows the finished agent at once, with its result as written (or as already translated),
            # and is edited to the translation afterwards: a translation never holds back the card.
            report = subagent_report(cfg, agent_type, description, agent_transcript, transcript, translate=False)
            original = report.get("summary") or ""
            known = _cached_translation(original, cfg)
            if known:
                report["summary"] = known
            agents_card.agent_finished(cfg, session, thread, agent_id, report, bot, chat=found.chat)
            if original and not known and cfg.translate and L.needs_translation(original, cfg.target_language):
                try:
                    he = translate_text(original, cfg, fast=False).strip()
                except Exception:
                    he = ""
                if he and he != original:
                    agents_card.agent_summary(cfg, session, thread, agent_id, he, bot)
            return 0
        if not text:
            if kind == "session-end":
                from .topics import set_state
                set_state(cfg, session, "done", bot=bot)
            return 0
        params = {"chat_id": found.chat, "message_thread_id": thread, "text": mark + text}
        if silent:
            params["disable_notification"] = True
        try:
            sent = bot.call("sendMessage", params)
            if kind == "session-end":  # a copy in the alerts channel, when there is one
                alerts.alert(cfg, t("alert_session_closed", lang=getattr(cfg, "ui_language", "en")), session, bot=bot, kind="system")
        finally:
            if kind == "session-end":
                from .topics import set_state
                set_state(cfg, session, "done", bot=bot)
        if kind == "session-end":
            _close_with_warning(cfg, bot, session, thread, s, mark, sent)
    except Exception:
        return 0
    return 0


def _close_with_warning(cfg: Config, bot, session: str, thread: int, s: dict, mark: str, sent) -> None:
    """Close the ended session's topic, then turn the notice just posted into the one that warns of the
    deletion. A topic is never left to be deleted without that visible warning: when the edit fails the
    topic is opened again and the record is dropped."""
    from . import sessions_menu
    if not sessions_menu.close_after_end(cfg, bot, TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg),
                                         session):
        return
    chat = (sessions_menu.closed_marks().get(session) or {}).get("chat") or home_chat(cfg)  # where the notice went
    try:
        bot.call("editMessageText", {"chat_id": chat, "message_id": (sent or {})["message_id"],
                                     "text": mark + s["lifecycle_session_end_closed"]})
    except Exception:
        sessions_menu.reopen_for_message(cfg, bot, session)


_last_failure: Optional[BaseException] = None  # the failure of the last run_reply; never content


def last_reply_failure() -> Optional[BaseException]:
    """The error that made the last run_reply fail (None when it did not), for the alert's reason."""
    return _last_failure


def run_reply(cfg: Config, title: str, session: str, text: str, translate: bool, transcript: str = "",
              answer_to: Optional[dict] = None) -> int:
    """Entry for `ctb mirror`: slow, high-quality offline models are allowed here (fast=False).
    A short reply gets the one-line footer. On failure the reason is remembered (last_reply_failure)
    for the Stop worker's alert."""
    global _last_failure
    from .scheduled import suppressed
    if suppressed(session, "reply", text):
        return 0  # an ordinary reply of a muted scheduled task; a failure reply still goes (scheduled.py)
    keep = True  # handoff links stay whole in the owner's reply; _emit drops them from the team copy
    fn = (lambda t: translate_text(t, cfg, fast=False, keep_links=keep)) if translate else None
    # A short reply would be outweighed by the full footer card: one line only.
    footer = (usage.compact_footer(cfg, transcript) if len(text.strip()) < SHORT_REPLY
              else usage.footer(cfg, transcript))
    try:
        m = build(cfg)
        m.send_reply(title, session, text, fn, footer, answer_to=answer_to)
    except (TelegramError, TranslateError, OSError) as e:
        _last_failure = e
        record_outcome(False)
        return 1
    _last_failure = m.last_error if m.dropped else None
    record_outcome(not m.dropped)
    try:  # the turn is over: the card and its context cell show the size now (quiet when nothing moved)
        refresh_card(cfg, m.bot, session, topics=m.topics, turn_end=True)
    except Exception:
        log("card refresh after the reply failed")
    if not m.dropped:
        try:
            limit_offer(cfg, m.bot, session, text)
        except Exception:
            log("limit offer failed")
    return 1 if m.dropped else 0


def limit_offer(cfg: Config, bot, session: str, text: str, spawn_fn: Optional[Callable] = None) -> bool:
    """When a session hits Claude's plan limit, offer to move it to another account, or (on by default)
    move it at once to one that still has room ("offer it automatically when the quota
    runs out"). Returns whether a notice was sent."""
    if len(text) > 600 or not accounts.QUOTA_HIT.search(text):
        return False
    # Stop and StopFailure can both report the same limit: offer the move once (300 s dedupe)
    mark_dir = private_dir(state_dir() / "limit-offered")
    mark = mark_dir / (re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_")
    try:
        if time.time() - mark.stat().st_mtime < 300:
            return False
    except OSError:
        pass
    mark.touch()
    found = _essential_place(cfg, bot, session)
    if found is None:
        return False
    thread = found.thread
    # Note this limit first: the fallback skips an account that ran out for five hours, and groups accounts
    # whose limits were recorded together.
    accounts.note_limit(accounts.limited_account(session))
    try:  # who each profile is signed in as may have changed since the last look; owner notices if so
        identity.check(cfg, bot, force=True, timeout=accounts.IDENTITY_QUICK_TIMEOUT)
    except Exception:
        pass
    plan = accounts.fallback_plan(session)
    ui_lang = getattr(cfg, "ui_language", "en")
    mark_str = L.RLM if ui_lang == "he" else ""
    params = {}
    if plan["auto"] and plan["fresh"]:
        target = plan["fresh"][0]
        if spawn_fn is None:
            from .hooks import spawn as default_spawn
            spawn_fn = default_spawn
        spawn_fn(["account", "auto-move", f"--session={session}", f"--thread={thread}", f"--target={target}"])
        params["text"] = mark_str + t("limit_offer_auto", lang=ui_lang, here=plan['here'], target=target)
    else:
        rows = [[{"text": t("limit_offer_move_btn", lang=ui_lang, name=n), "callback_data": f"more:/account move {n}"}] for n in plan["others"][:3]]
        rows.append([{"text": t("account_btn_add", lang=ui_lang), "callback_data": "more:/account add"}])
        rows.append(menus.not_now_row(ui_lang))
        params["text"] = mark_str + t("limit_offer_ask", lang=ui_lang, here=plan['here'])
        params["reply_markup"] = {"inline_keyboard": rows}
    send_essential(cfg, bot, session, params)
    if not (plan["auto"] and plan["fresh"]):  # it waits for a choice: ring the alerts channel too
        alerts.alert(cfg, t("alert_quota_out", lang=ui_lang), session, bot=bot, kind="system")
    return True


FILE_FLOOD_WAIT = 30.0  # the longest flood pause `mirror --file` sits out before giving up


def run_file(cfg: Config, title: str, session: str, path: str, caption: str,
             audio_title: Optional[str] = None, performer: Optional[str] = None) -> int:
    """Send one file to the session's topic. A short flood wait is sat out once; a failure says why on stderr
    (it used to return 1 in silence, so a lost file looked sent)."""
    from .scheduled import suppressed
    if suppressed(session, "reply"):
        return 0  # a file sent with an ordinary reply of a muted scheduled task
    for attempt in (0, 1):
        try:
            build(cfg).send_file(title, session, path, caption, audio_title, performer)
            return 0
        except TelegramError as e:
            wait = getattr(e, "retry_after", None)
            if attempt == 0 and e.code == 429 and wait and wait <= FILE_FLOOD_WAIT:
                time.sleep(float(wait) + 0.5)
                continue
            print(f"Harnessgram: the file was not sent ({e.description})", file=sys.stderr)
            return 1
        except OSError as e:
            print(f"Harnessgram: the file was not sent ({type(e).__name__})", file=sys.stderr)
            return 1
    return 1


def run_typing(cfg: Config, session: str) -> int:
    """'typing...' in the session's topic (sendChatAction). Quiet when the session has no topic."""
    try:
        bot = Bot.from_config(cfg)
        found = place(cfg, session)
        if found:
            flood.optional_call(bot, "sendChatAction", {"chat_id": found.chat, "message_thread_id": found.thread,
                                                        "action": "typing"}, budget=False)  # gate only
    except Exception:
        return 1
    return 0


def _essential_place(cfg: Config, bot, session: str):
    """The session's topic for a notice that needs a person (permission request, question, limit, error).
    A scheduled run may have none yet, since a muted task holds back the ordinary sends that make it: its
    task topic is made or joined here. Any other session without a topic gets no notice (as before)."""
    found = place(cfg, session)
    if found is None:
        from .scheduled import task_of
        if task_of(session):
            TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg).ensure(session, "")
            found = place(cfg, session)
    return found


def send_essential(cfg: Config, bot, session: str, fields: dict) -> Optional[dict]:
    """Send one notice that needs a person into the session's topic, for every session. A topic Telegram
    reports closed is opened again, and a deleted one is made again (as Mirror._post does for replies);
    either way the send is retried once. None when the session has no topic."""
    topics = TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg)
    for attempt in (0, 1):
        found = _essential_place(cfg, bot, session)
        if found is None:
            return None
        try:
            return bot.call("sendMessage", dict(fields, chat_id=found.chat, message_thread_id=found.thread))
        except TelegramError as e:
            if attempt:
                raise
            if "topic_closed" in e.description.lower():
                from . import sessions_menu
                if not sessions_menu.reopen_for_message(cfg, bot, session):
                    bot.call("reopenForumTopic", {"chat_id": found.chat, "message_thread_id": found.thread})
            elif _is_thread_error(e) or _topic_gone(e):
                topics.forget(session, chat=found.chat)
                topics.ensure(session, "")
            else:
                raise
    return None


def alert(cfg: Config, session: str, text: str, exc: Optional[BaseException] = None, bot=None) -> None:
    """Report a failed delivery in the existing owner topic, without transcript content. The
    Hebrew reason for the failure (reason_he) is appended to the alert."""
    try:
        bot = bot or Bot.from_config(cfg)
        ui_lang = getattr(cfg, "ui_language", "en")
        if text.startswith("Reply delivery failed"):
            body = t("alert_delivery_failed", lang=ui_lang) + " · " + reason_str(exc, lang=ui_lang)
        elif text.startswith("The turn ended"):
            body = t("alert_no_reply", lang=ui_lang)
        else:
            body = text
        mark = L.RLM if ui_lang == "he" else ""
        send_essential(cfg, bot, session, {"text": mark + "⚠️ " + body, "disable_notification": True})
    except Exception:
        log("delivery alert failed")


def voice_summary(cfg: Config, bot, session: str, he_body: str, topics: Optional[TopicMap] = None) -> None:
    """The reply's summary read aloud in Hebrew, as a Telegram voice note.
    Active ONLY in car mode, when voice is enabled and text contains Hebrew.
    Gemini TTS (gemini-2.5-flash-tts, Kore, 24kHz s16le PCM) when gemini is first configured engine
    and external calls are allowed; otherwise macOS say -v Carmit when available.
    Converts with ffmpeg to OGG/Opus and sends via sendVoice after a notifying '🚗 ' text line.
    Skips silently if ffmpeg is missing or anything fails."""
    try:
        if not (state_dir() / "car-mode").exists():
            return
        if not getattr(cfg, "voice", True):
            return
        voice_switch = state_dir() / "voice"
        if voice_switch.exists() and voice_switch.read_text(encoding="utf-8").strip() == "off":
            return
        paras = [p.strip() for p in (he_body or "").split("\n\n") if p.strip()]
        he = paras[-1] if paras else ""
        if not he or not re.search(r"[\u0590-\u05ff]", he):
            return
        ffmpeg = shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else None)
        if not ffmpeg:
            return
        pcm = b""
        if cfg.engines and cfg.engines[0] == "gemini" and external_allowed(cfg):
            try:
                pcm = speak(for_outside(he), cfg, voice="Kore")
            except Exception:
                pcm = b""

        import tempfile
        with tempfile.TemporaryDirectory() as d:
            aiff = os.path.join(d, "v.aiff")
            ogg = os.path.join(d, "v.ogg")
            src = []
            inp = None
            if pcm:
                src = ["-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0"]
                inp = pcm
            else:
                say = shutil.which("say") or ("/usr/bin/say" if os.path.exists("/usr/bin/say") else None)
                if not say:
                    return
                res = subprocess.run([say, "-v", "Carmit", "-r", "185", "-o", aiff, "--", for_outside(he)],
                                     capture_output=True, timeout=60)
                if res.returncode != 0 or not os.path.exists(aiff) or os.path.getsize(aiff) == 0:
                    return
                src = ["-i", aiff]

            cmd = [ffmpeg, "-loglevel", "error", "-y", *src, "-c:a", "libopus", "-b:a", "32k", "-ac", "1", ogg]
            proc = subprocess.run(cmd, input=inp, capture_output=True, timeout=60)
            if proc.returncode != 0 or not os.path.exists(ogg):
                return
            payload = Path(ogg).read_bytes()
            if not payload:
                return

        found = None
        if topics:
            found = topics.place_of(session)
        elif session:
            try:
                found = place(cfg, session)
            except Exception:
                pass
        thread = found.thread if found else None
        chat = found.chat if found and not found.home else home_chat(cfg)

        # Text with notification so Siri / CarPlay reads it aloud
        text_params = {"chat_id": chat, "text": "\u200f🚗 " + for_outside(he)[:900],
                       "disable_notification": False}
        if thread:
            text_params["message_thread_id"] = thread
        bot.call("sendMessage", text_params)

        fields = {"chat_id": str(chat), "disable_notification": "true"}
        if thread:
            fields["message_thread_id"] = str(thread)
        bot.upload("sendVoice", fields, "voice", "summary.ogg", payload)
    except Exception:
        pass


def is_loop_prompt(prompt: str) -> bool:
    """Filter out timer prompts and loop ticks."""
    if not isinstance(prompt, str):
        return False
    p = prompt.strip()
    return (
        p.startswith("# Autonomous loop")
        or "<<autonomous-loop" in prompt
        or "Autonomous loop tick" in prompt[:300]
    )


def run_prompt(cfg: Config, session: str, prompt: str, bot=None) -> int:
    """Mirror a direct prompt only into an already mapped private session topic."""
    if is_loop_prompt(prompt):
        return 0
    try:
        bot = bot or Bot.from_config(cfg)
        found = place(cfg, session)
        if found:
            text = redact(prompt) if cfg.redact_outgoing else prompt
            label = t("prompt_mirror_label", lang=cfg.ui_language)
            bot.call("sendMessage", {"chat_id": found.chat, "message_thread_id": found.thread,
                                     "text": f"💻 {label}:\n{text[:3500]}", "disable_notification": True,
                                     "link_preview_options": {"is_disabled": True}})
        return 0
    except Exception:
        return 1


def translate_card(cfg: Config, text: str, body: str, markup: dict):
    """A question card in the owner's language (detached child only: Gemini can take seconds).

    Adds a translated version of the screen under the English one and translates the answer
    buttons' labels; every callback_data stays exactly as it was, so the keys sent are unchanged.
    Returns (text, markup); both unchanged when translation is off, not needed or fails."""
    if not cfg.translate or not L.needs_translation(body, cfg.target_language):
        return text, markup
    try:
        translated = translate_text(redact(body[-2500:]), cfg, fast=False).strip()
    except Exception:
        translated = ""
    if translated:
        head = "\n\n" + L.RLM + "<b>" + html.escape(L.strings(cfg.target_language)["card_translated"]) + "</b>\n"
        room = 4000 - len(text) - len(head)
        if room > 200:
            while translated and len(html.escape(translated, quote=False)) > room:
                translated = translated[:int(len(translated) * 0.9)]
            text += head + html.escape(translated, quote=False)
    markup = copy.deepcopy(markup) if markup else markup  # the caller compares old and new (review of #154)
    keys = [btn for row in (markup or {}).get("inline_keyboard", []) for btn in row
            if (str(btn.get("callback_data", "")).startswith("k:")
                and str(btn["callback_data"]).split(":")[1:2] and str(btn["callback_data"]).split(":")[1].isdigit())
            or str(btn.get("callback_data", "")).startswith(("dg:o:", "dg:x:"))]
    if keys:
        try:
            lines = [x.strip() for x in translate_text("\n".join(b["text"] for b in keys), cfg, fast=False).splitlines()
                     if x.strip()]
        except Exception:
            lines = []
        def number(t):
            m = re.match(r"\s*(\d+)[.):]", str(t))
            return m.group(1) if m else None
        # each translated label must keep its own option number, or the line order changed
        if len(lines) == len(keys) and all(number(b["text"]) == number(l) for b, l in zip(keys, lines)):
            for btn, label in zip(keys, lines):
                btn["text"] = label[:60]  # callback_data is left alone
    return text, markup


ALERT_EVERY = 600  # seconds: at most one alerts-channel ring per session per 10 minutes


def _notify_alert(cfg: Config, session: str, bot) -> None:
    """The alerts channel hears about a session that waits for you, once per ALERT_EVERY."""
    try:
        if not alerts.allowed("waiting", cfg, bot):
            return  # off, or no channel: nothing is marked, so turning it on later rings at once
        mark = private_dir(state_dir() / "notify") / ((re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_") + ".alert")
        if time.time() - mark.stat().st_mtime < ALERT_EVERY:
            return
    except FileNotFoundError:
        pass
    except OSError:
        return
    if alerts.alert(cfg, t("alert_waiting", lang=getattr(cfg, "ui_language", "en")), session, bot=bot, kind="waiting"):
        mark.touch()  # only a ring that went out silences the session; a failed one is tried again


# Claude Code's own error rows (the "⎿" lines under a prompt) that mean the turn did not run, so
# "finished" would mislead. Claude's message lines ("●") and older turns are never read.
SCREEN_ERRORS = re.compile(r"not logged in\b|please run /login|invalid api key|authentication_error"
                           r"|api error:|credit balance is too low|limit reached\s*[∙·]\s*resets", re.I)


def screen_error(session: str) -> str:
    """The error row Claude Code printed after the owner's last prompt on the terminal, or ""."""
    try:
        from .inbox import pane_for_session, pane_text
        pane = pane_for_session(session)
        text = pane_text(pane, lines=30) if pane else ""
    except Exception:
        return ""
    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if re.match(r"\s*❯\s*\S", line):
            start = i + 1  # the last prompt with text: only what came after it counts
    for line in reversed(lines[start:]):
        row = line.strip(" │")
        if row.startswith("●"):
            return ""  # Claude wrote after it: the turn went on, an earlier row was a tool's output
        if row.startswith("⎿") and SCREEN_ERRORS.search(row):
            return " ".join(row.strip(" ⎿").split())[:200]  # plain text: the notice has no parse_mode
    return ""


def _notify_log(what: str) -> None:
    """A question that cannot be shown must leave a trace (an earlier version left none)."""
    try:
        from .inbox import poll_log
        poll_log("notify " + what)
    except Exception:
        pass


def run_notify(cfg: Config, session: str, ntype: str, bot=None) -> int:
    """A sound-making message in the session's topic: the session needs the owner."""
    try:
        from .scheduled import suppressed
        if ntype == "idle_prompt" and suppressed(session, "idle"):
            return 0  # a plain "finished, waiting" of a muted task; one that shows an error still goes
        bot = bot or Bot.from_config(cfg)
        found = _essential_place(cfg, bot, session)
        if found is None:
            return 0
        thread = found.thread
        from .topics import set_state
        try:
            set_state(cfg, session, "done" if ntype == "idle_prompt" else "waiting", bot=bot)
        except Exception as exc:  # the card below must not depend on the placeholder refresh
            _notify_log(f"state refresh failed: {type(exc).__name__}")
        if ntype != "idle_prompt":
            from . import dialogs
            time.sleep(1.0)  # The child is detached; allow the terminal dialog to draw.
            _notify_alert(cfg, session, bot)
            try:
                if dialogs.send_card(cfg, bot, session, thread, translate=True, chat=found.away):
                    return 0
            except Exception as exc:
                _notify_log(f"dialog card failed: {type(exc).__name__}")
        s = L.strings(cfg.target_language)
        what = s.get("notify_" + ntype) or s["notify_default"]
        if ntype == "idle_prompt":
            err = screen_error(session)
            if err:  # "finished and waiting" would hide that it never ran (Not logged in)
                what = s["notify_error"] + "\n" + err
        send_essential(cfg, bot, session, {"text": (L.RLM if L.is_rtl(cfg.target_language) else "") + "\U0001F514 " + what})
    except Exception as exc:
        _notify_log(f"failed: {type(exc).__name__}")
        return 1
    return 0
