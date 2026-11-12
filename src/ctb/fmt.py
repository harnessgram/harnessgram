"""Markdown -> Telegram HTML, summary extraction and message layout.

Telegram's HTML subset is small: <b>, <i>, <code>, <pre>, <a>, <blockquote>.
Markdown is converted conservatively; anything unexpected is escaped so the
message always parses (and mirror.py falls back to plain text if it still does not).
"""
from __future__ import annotations

import html
import re
from typing import Dict, List, Optional, Tuple

from . import lang as L
from .creds import safe_cut
from .usage import when

LIMIT = 3500  # Telegram allows 4096 characters; leave room for tags and the summary head
FENCE = re.compile(r"(```[\w+-]*[ \t]*\n?.*?```)", re.S)
FENCE_BODY = re.compile(r"```[\w+-]*[ \t]*\n?(.*?)```", re.S)

# "Summary: ...", "**Summary:** ...", "> Summary: ..." (any case, optional bold wrapper).
_SUMMARY_LINE = re.compile(
    r"^[ \t>#-]*(\*\*|__)?[ \t]*summary[ \t]*(\*\*|__)?[ \t]*:[ \t]*(\*\*|__)?[ \t]*(.*)$", re.I)


def parse_summary_line(line: str) -> Optional[str]:
    """The text of a 'Summary:' line, or None if the line is not one."""
    m = _SUMMARY_LINE.match(line)
    if not m:
        return None
    opening, middle, after, rest = m.groups()
    if after and not (opening or middle):
        rest = after + rest  # "Summary: **bold** text": the ** belongs to the text, not the label
    if opening and rest.endswith(opening) and rest.count(opening) % 2 == 1:
        rest = rest[: -len(opening)]  # "**Summary: text**"
    rest = rest.strip()
    return rest or None


def split_summary(text: str) -> Tuple[str, Optional[str]]:
    """(body without the summary line, summary text). Summary is None when there is no label.

    The last labelled line wins, since the convention is to end a reply with it.
    """
    lines = text.strip().split("\n")
    for i in range(len(lines) - 1, -1, -1):
        summary = parse_summary_line(lines[i])
        if summary:
            body = "\n".join(lines[:i] + lines[i + 1:]).strip()
            return body, summary
    return text.strip(), None


def join_summary(text: str) -> str:
    """Put the summary in its own final paragraph and drop the label.

    Makes "the last paragraph is the summary" reliable after translation, even
    when the model wrote the label on the line right under the body.
    """
    body, summary = split_summary(text)
    if summary is None:
        return text.strip()
    return (body + "\n\n" + summary) if body else summary


def _protect_code(text: str) -> Tuple[str, List[str]]:
    codes: List[str] = []

    def stash(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    return re.sub(r"`([^`\n]+)`", stash, text), codes


def inline(text: str) -> str:
    """Inline Markdown (bold, code, links, bullets, headings) -> Telegram HTML."""
    text, codes = _protect_code(text)  # code spans are shielded so ** inside them stays literal
    out = html.escape(text, quote=False)
    out = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
                 lambda m: '<a href="%s">%s</a>' % (m.group(2).replace('"', "%22"), m.group(1)), out)
    out = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", out)
    out = re.sub(r"(?m)^#+\s*(.+)$", lambda m: "<b>" + m.group(1).replace("**", "") + "</b>", out)
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", out)
    return re.sub(r"\x00(\d+)\x00",
                  lambda m: "<code>" + html.escape(codes[int(m.group(1))], quote=False) + "</code>", out)


def _with_tables(text: str) -> str:
    """Runs of 2+ lines starting with '|' are Markdown tables: keep their columns in <pre>."""
    lines, out, run = text.split("\n"), [], []

    def flush():
        if len(run) >= 2:
            out.append("<pre>" + html.escape("\n".join(run), quote=False) + "</pre>")
        else:
            out.extend(inline(r) for r in run)
        run.clear()

    for line in lines:
        if line.lstrip().startswith("|"):
            run.append(line)
        else:
            flush()
            out.append(inline(line))
    flush()
    return "\n".join(out)


def to_html(md: str, lang: str = "") -> str:
    """Markdown -> Telegram HTML. Hebrew points are removed when the language is Hebrew."""
    strip = L.base(lang) == "he"
    if strip:
        parts_unmix = []
        for i, chunk in enumerate(FENCE.split(md)):
            if i % 2:
                parts_unmix.append(chunk)
            else:
                parts_unmix.append(unmix(chunk))
        md = "".join(parts_unmix)
    parts, last = [], 0
    for m in FENCE_BODY.finditer(md):
        before = md[last:m.start()]
        parts.append(_with_tables(L.NIKUD.sub("", before) if strip else before))
        parts.append("<pre>" + html.escape(m.group(1).strip("\n"), quote=False) + "</pre>")
        last = m.end()
    tail = md[last:]
    parts.append(_with_tables(L.NIKUD.sub("", tail) if strip else tail))
    return "".join(parts)


def paragraphs(md: str, limit: int = LIMIT) -> List[str]:
    """Blank-line separated paragraphs; a fenced block stays whole; oversize ones are split."""
    out: List[str] = []
    for i, seg in enumerate(FENCE.split(md.strip())):
        if i % 2:  # a fenced code block
            out.extend(_split_fence(seg, limit))
        else:
            for para in (p.strip() for p in re.split(r"\n\s*\n", seg)):
                if para:
                    out.extend(_split_text(para, limit))
    return out


def _split_text(para: str, limit: int) -> List[str]:
    if len(para) <= limit:
        return [para]
    pieces, cur = [], ""
    for line in para.split("\n"):
        while len(line) > limit:  # a single endless line: hard cut
            if cur:
                pieces.append(cur)
                cur = ""
            cut = safe_cut(line, limit)  # never through a held link's placeholder
            pieces.append(line[:cut])
            line = line[cut:]
        if cur and len(cur) + len(line) + 1 > limit:
            pieces.append(cur)
            cur = ""
        cur = cur + "\n" + line if cur else line
    if cur:
        pieces.append(cur)
    return pieces


def _split_fence(block: str, limit: int) -> List[str]:
    if len(block) <= limit:
        return [block]
    m = FENCE_BODY.match(block)
    body = m.group(1) if m else block
    return ["```\n" + piece + "\n```" for piece in _split_text(body.strip("\n"), limit - 8)]


def chunks(md: str, limit: int = LIMIT) -> List[str]:
    """Pack paragraphs into messages of at most ~limit characters."""
    out, cur = [], ""
    for p in paragraphs(md, limit - 200):
        if cur and len(cur) + len(p) + 2 > limit - 200:
            out.append(cur)
            cur = ""
        cur = cur + "\n\n" + p if cur else p
    if cur:
        out.append(cur)
    return out


def layout(md: str, lang: str = "", pin_last: bool = True) -> List[str]:
    """HTML messages for a reply: the summary (last paragraph) pinned on top, the rest below.

    Plain text under the summary, not a quote: Telegram draws quotes left to right,
    which breaks right-to-left languages.
    """
    paras = paragraphs(md)
    if not paras:
        return []
    head = None
    if pin_last:
        head = "\U0001F4CC " + to_html(paras[-1], lang)
        paras = paras[:-1]
    if not paras:
        return [head] if head else []
    labels: Dict[str, str] = L.strings(lang)
    msgs = []
    for i, part in enumerate(chunks("\n\n".join(paras))):
        if i == 0:
            prefix = (head + "\n\n— — —\n\n") if head else ""
        else:
            prefix = "<i>" + html.escape(labels["continued"], quote=False) + "</i>\n"
        msgs.append(prefix + to_html(part, lang))
    return msgs


# -- mixed scripts and terminal table formatting ----------

HEB = re.compile(r"[\u0590-\u05FF]")
LAT = re.compile(r"[A-Za-z]")
TOKEN = re.compile(r"`[^`\n]*`|\[[^\]\n]*\]\([^)\s]*\)|https?://\S+|\S+")
PHONE_COLS = 24  # a phone code block shows about this many characters before it wraps


def unmix(text: str, arrows: bool = True) -> str:
    """Global rule: English and Hebrew never share a line, the phone scrambles
    them. A mixed line is split where the script changes; numbers and marks stay with the words
    before them. Inline code, links and URLs count as English and are never cut."""
    out = []
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):  # a fenced block is code: never reflowed
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        # The text can pass through here more than once (guard, then the bridge): drop earlier
        # isolates so a name is wrapped once, not three times.
        line = line.replace("\u2068", "").replace("\u2069", "")
        if HEB.search(line) and "`" not in line:
            # "ה-סשן": a prefix hyphen belongs only before a non-Hebrew word; the glossary's Hebrew
            # replacements left it before Hebrew ones.
            line = re.sub(r"(?<![\u0590-\u05FF])([ובלכמשה]{1,3})-(?=[\u0590-\u05FF])", r"\1", line)
        if not (HEB.search(line) and LAT.search(line)):
            out.append(line)
            continue
        groups, kinds, kind = [], [], None
        # "ב-PR" and "ל־PR" (maqaf): the prefix stays Hebrew, the name after it is judged on its own.
        line = re.sub(r"([\u0590-\u05FF][-\u05BE])(?=[`\[A-Za-z])", r"\1 ", line)
        for tok in TOKEN.findall(line):
            shown = tok.split("](")[0] if tok.startswith("[") else tok  # a link reads as its label
            k = "en" if (tok[0] == "`" or tok.startswith("http") or (LAT.search(shown) and not HEB.search(shown))) \
                else "he" if HEB.search(shown) else None
            if k and k != kind and groups and kind is not None:
                groups.append([tok])
                kinds.append(k)
            elif groups:
                groups[-1].append(tok)
            else:
                groups.append([tok])
                kinds.append(k or "he")
            kind = k or kind
        # A line break for every English word is too many. A short English name
        # (up to 3 words, no code or link) stays inside the Hebrew line, wrapped in a bidi isolate
        # (U+2068..U+2069) so the phone keeps the word order; longer English still gets its own line.
        merged, mkinds = [], []
        for g, k in zip(groups, kinds):
            short = k == "en" and sum(1 for t in g if LAT.search(t)) <= 3 and not any(
                t[0] == "`" or t.startswith("http") or t.startswith("[") or "**" in t for t in g)
            if short and "he" in kinds:
                name = " ".join(g)
                # Brackets and punctuation stay outside the isolate: "(Claude Opus," inside it would
                # pair its "(" with a ")" in the Hebrew text, and the phone draws them mirrored.
                head = re.match(r"[(\[\"'«]*", name).group()
                tail = re.search(r"[,.;:!?)\]\"'»]*$", name[len(head):]).group()
                inner = name[len(head):len(name) - len(tail)]
                # a bracket inside the name ("Codex (gpt-6.1-sol," whose ")" is in the Hebrew text) stays outside
                # the isolates too: each English piece is isolated on its own
                parts = re.split(r"(\s*[()\[\]«»]\s*)", inner)
                g = [head + "".join(p if not p or re.fullmatch(r"\s*[()\[\]«»]\s*", p) else "\u2068" + p + "\u2069"
                                    for p in parts) + tail]
                k = "he"
            if merged and k == "he" and mkinds[-1] == "he":
                merged[-1] += g
            else:
                merged.append(list(g))
                mkinds.append(k)
        groups = merged
        lines, bold = [], False
        for g in groups:  # bold cut in two: close it on one line, reopen on the next
            seg = ("**" if bold else "") + " ".join(g)
            bold = seg.count("**") % 2 == 1
            lines.append(re.sub(r"([\u0590-\u05FF][-\u05BE]) \u2068", "\\1\u2068", seg) + ("**" if bold else ""))
        out += lines
    # A Hebrew line reads right to left, so an arrow written for English points backwards.
    # Only after the split: an arrow that ends up on an English line stays as written.
    flip = str.maketrans({"→": "←", "←": "→", "⇒": "⇐", "⇐": "⇒"})
    if not arrows:  # the arrow swap is its own inverse: text that already had it must not get it again
        return "\n".join(out)
    out = [l.translate(flip).replace("->", "←") if HEB.search(l) and not LAT.search(l) and "`" not in l else l
           for l in out]
    return "\n".join(out)


def isolate(name: str) -> Optional[str]:
    """`name` wrapped in a bidi isolate so it can sit inside a Hebrew line, or None when it may not:
    more than 3 words, or code or a link (those get a line of their own)."""
    words = [t for t in name.split() if LAT.search(t)]
    if len(words) > 3 or any(c in name for c in ("`", "http", "](", "\n")):
        return None
    return "\u2068" + name.replace("\u2068", "").replace("\u2069", "") + "\u2069"


def wrap_reset(row: str, c: str) -> List[str]:
    """A table row plus its reset clock; a dated clock that would wrap moves to its own line."""
    if not c:
        return [row]
    if len(row) + 3 + len(c) <= PHONE_COLS:
        return [f"{row} ↻ {c}"]
    return [row, f"{' ' * 5}↻ {c}"]

