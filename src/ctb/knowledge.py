"""What the system knows: one private index of the agents' rules, memories, skills, tools and docs.

The owner sees every item in plain words, switches items off and on, and undoes any change. Nothing here
rewrites a source file:

- the index, the on/off map and the append-only change log live in the bridge's private state folder (0600);
- what is switched off is rendered into one overlay file (`overlay`, default ~/.agents/knowledge/off.md)
  that the rules files can import, so a switch takes effect from the agents' next session;
- nothing is ever written under the Claude home folder (settings, hooks, skills, CLAUDE.md);
- locked safety rules can be read but never switched off;
- agents only suggest changes (a ledger item); only the owner switches.

The bridge itself never sends index text anywhere: no translation, no model, no network. Credential-looking text is
masked when the index is built (creds.redact_channel). Plain-words notes (Hebrew and English titles, a one-sentence
summary and an advice) are written by the owner's own agents from a masked export and imported into a private notes
file keyed by content hash; see "Plain-words notes" below.

Configuration: the optional `knowledge` object of the bridge config (see docs/knowledge.md). Every key has a
generic default, so a new install indexes the common Claude Code / Codex / Gemini locations.
"""
from __future__ import annotations

import fcntl
import glob
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .config import private_dir, state_dir, write_private
from .creds import redact_channel

KINDS = ("rule", "memory", "skill", "tool", "doc")
AGENTS = ("claude", "codex", "agy")
DEFAULTS = {
    # Each agent's root instruction file; `@path` (and Gemini's `@[title](path)`) imports are followed.
    "agents": {"claude": "~/.claude/CLAUDE.md", "codex": "~/.codex/AGENTS.md", "agy": "~/.gemini/GEMINI.md"},
    "rules": [],          # extra rules files every agent reads
    "memory": ["~/.claude/projects/*/memory"],
    # An imported file with one of these names is a memory index, not rules.
    "memory_index": ["MEMORY.md", "SHARED_MEMORY.md"],
    "skills": {"~/.claude/skills": ["claude"], "~/.codex/skills": ["codex"], "~/.agents/skills": list(AGENTS)},
    "tools": {"claude": "~/.claude.json", "codex": "~/.codex/config.toml", "agy": "~/.gemini/settings.json"},
    "docs": [],
    "overlay": "~/.agents/knowledge/off.md",
    # Plain Hebrew and English titles, summaries and advice an agent wrote once, keyed by content hash.
    "notes": "~/.agents/knowledge/notes.json",
    "locked": [],         # extra lock patterns (regular expressions over a rule's text)
    "locked_ids": [],
    "ledger_project": "env",
}
# Safety rules that are read-only everywhere: secrets, messaging people, permissions and sandbox, paid services,
# publication and deletion approvals, production servers, child-suitable content.
LOCKED = (
    r"\bsecrets?\b[^.;]{0,40}\bnever\b|\bnever\b[^.;]{0,40}\bsecrets?\b",
    r"\b(?:never|no)\b[^.;]{0,30}\bsend\w*\b[^.;]{0,30}\bmessages?\b|\bmessages?\b[^.;]{0,30}\bother people\b",
    r"\bweaken\w*\b[^.;]{0,60}\b(?:permissions?|auth|sandbox|protection)\b",
    r"\bpaid (?:services?|fallbacks?|apis?)\b",
    r"\bstop for the owner\b|\b(?:publication|publish\w*|delet\w*)\b[^.;]{0,40}\b(?:approv\w*|confirm\w*)",
    r"\bproduction server\b",
    r"\bchild-suitable\b",
)
# Patterns are matched against a rule's title and first sentence: what the rule is, not every word it mentions.
MAX_FILE = 4 * 1024 * 1024
MAX_JSON = 32 * 1024 * 1024  # ~/.claude.json grows with history; only the server names are read
TEXT_CHARS = 1500
SUMMARY_CHARS = 240
TITLE_CHARS = 80
HISTORY_DAYS = 30
LOG_ID = re.compile(r"[0-9a-f]{12}")
ITEM_ID = re.compile(r"(?:rule|memory|skill|tool|doc):[\w.:/@+-]{1,200}")
_IMPORT = re.compile(r"^@\[[^\]]*\]\(([^)\s]+)\)\s*$|^@((?:~|/)\S+)\s*$")
_BULLET = re.compile(r"^(?:[-*+]|\d{1,3}[.)])\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_DATE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_LINK = re.compile(r"\]\(([^)\s]+\.md)\)")
OVERLAY_BEGIN = "<!-- harnessgram-knowledge-overlay:begin -->"
OVERLAY_END = "<!-- harnessgram-knowledge-overlay:end -->"


class KnowledgeError(ValueError):
    code = "bad"


class Unknown(KnowledgeError):
    code = "unknown"


class Locked(KnowledgeError):
    code = "locked"


class Changed(KnowledgeError):
    code = "changed"


class Forbidden(KnowledgeError):
    code = "forbidden"


# --- configuration -------------------------------------------------------------------------------------------

def valid_section(value) -> bool:
    if not isinstance(value, dict):
        return False
    for key, v in value.items():
        if key not in DEFAULTS:
            continue  # newer config, older program
        want = DEFAULTS[key]
        if isinstance(want, str) and not isinstance(v, str):
            return False
        if isinstance(want, list) and not (isinstance(v, list) and all(isinstance(x, (str, dict)) for x in v)):
            return False
        if isinstance(want, dict) and not isinstance(v, dict):
            return False
    return True


def settings(cfg) -> dict:
    raw = getattr(cfg, "knowledge", None) or {}
    out = {k: (v.copy() if isinstance(v, (dict, list)) else v) for k, v in DEFAULTS.items()}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in DEFAULTS and type(v) is type(DEFAULTS[k]):
                out[k] = v
    return out


def _path(p: str) -> Path:
    return Path(os.path.expanduser(p))


def _shown(p) -> str:
    """A path as the owner reads it: the home folder as ~."""
    s, home = str(p), str(Path.home())
    return "~" + s[len(home):] if s == home or s.startswith(home + os.sep) else s


def claude_home() -> Path:
    return Path(os.environ.get("CTB_CLAUDE_HOME") or Path.home() / ".claude")


def folder() -> Path:
    return state_dir() / "knowledge"


# --- reading sources -----------------------------------------------------------------------------------------

def _read(path: Path, limit: int = MAX_FILE) -> Optional[str]:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


_URL = re.compile(r"\b[a-z][a-z0-9+.-]{1,15}://\S+", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# Any run of 9 to 15 digits joined by spaces, dots, dashes, slashes or brackets, with an optional "+", that does not
# continue a word: phone numbers in every grouping, after ":", "=", "(" or a quote. Dates and versions are kept.
_PHONE = re.compile(r"(?<![A-Za-z0-9])\+?\(?\d(?:[\s./()-]{0,3}\d){8,14}\)?(?![A-Za-z0-9])")
_KEEP = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)|(?<![\d.])\d{1,3}(?:\.\d{1,3}){2}(?![\d.])")
# A long run of letters, digits, "_" and "-" that holds both letters and digits is treated as a token.
_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")


def _token(m: re.Match) -> str:
    v = m.group()
    return "[redacted]" if re.search(r"[A-Za-z]", v) and re.search(r"\d", v) else v


def safe_text(raw) -> str:
    """The one privacy boundary for everything the index keeps or shows, applied to the raw text before any
    formatting is removed: credentials (creds.redact_channel), links, email addresses, long tokens and phone numbers."""
    text = redact_channel(raw if isinstance(raw, str) else "")
    text = _URL.sub("[link]", text)
    text = _EMAIL.sub("[email]", text)
    text = _TOKEN.sub(_token, text)
    kept: list = []

    def hold(m):
        # A version or date has at most 8 digits and no "+": a longer dotted run is a phone number.
        if len(re.sub(r"\D", "", m.group())) > 8 or m.string[max(0, m.start() - 2):m.start()].strip().endswith("+"):
            return m.group()
        kept.append(m.group())
        return "\ue000" + chr(0xE100 + len(kept) - 1) + "\ue001"
    text = _KEEP.sub(hold, text)
    text = _PHONE.sub("[phone]", text)
    return re.sub("\ue000(.)\ue001", lambda m: kept[ord(m.group(1)) - 0xE100], text)


mask = safe_text  # the earlier name


def _hid(kind: str, *parts) -> str:
    """An opaque item id: stable for the same source place, never containing any of its text."""
    return "%s:%s" % (kind, hashlib.sha256("\0".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16])


PRIVATE = "\u0001private"  # a label that cannot be shown safely; the Cockpit says "private folder"


def _safe_name(name: str, pattern: str = r"[a-z0-9-]{1,40}") -> str:
    """A folder or file name shown only as it is, when it is plainly safe; never rewritten into something else."""
    return name if re.fullmatch(pattern, name or "") and safe_text(name) == name else PRIVATE


_NAME_OK = re.compile(r"[\w .@-]{1,40}")
_ENC = re.compile(r"[^A-Za-z0-9]")


def _project_label(encoded: str, home=None) -> str:
    """A display-only label for a Claude memory folder named after the working folder it belongs to (every
    character that is not a letter or digit written as '-'). The encoded form of the real home folder is stripped as
    an exact prefix, so a user name (dashes included) is never shown; a folder outside home is private. The rest is
    matched against the real folders under home, longest name first, so dashes inside real names are kept; the
    label is the last two resolved names. '' means general (home itself, or only Documents); PRIVATE means nothing
    safe to show."""
    home = str(home if home is not None else Path.home())
    prefix = _ENC.sub("-", home)
    if len(home) < 2 or not (encoded or "").startswith("-") or not (encoded == prefix or encoded.startswith(prefix + "-")):
        return PRIVATE
    rest = encoded[len(prefix) + 1:]
    names, here = [], home
    while rest:
        try:
            with os.scandir(here) as it:
                found = sorted((e.name for e in it if e.is_dir(follow_symlinks=False)), key=lambda n: (-len(n), n))
        except OSError:
            break
        hit = next((n for n in found if rest == _ENC.sub("-", n) or rest.startswith(_ENC.sub("-", n) + "-")), None)
        if hit is None:
            break
        names.append(hit)
        rest = rest[len(hit) + 1:]
        here = os.path.join(here, hit)
    if not names:
        return "" if not encoded[len(prefix) + 1:] else PRIVATE
    if names == ["Documents"]:
        return ""
    names = names[-2:]
    if not all(_NAME_OK.fullmatch(n) and safe_text(n) == n for n in names):
        return PRIVATE
    return " \u00b7 ".join(n.replace("-", " ").replace("_", " ") for n in names)


def _spath(p) -> str:
    """A path shown part by part: every part that is not plainly safe is withheld."""
    parts = _shown(p).split("/")
    return "/".join(x if (x in ("", "~") or _safe_name(x, r"[A-Za-z0-9._@-]{1,60}") != PRIVATE) else "…" for x in parts)


def _clean(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", safe_text(text)).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _plain(text: str) -> str:
    """Markdown decoration removed: links keep their words, emphasis and code marks go. The raw text passes
    safe_text first, so stripping a mark (an underscore in a token) can never hide anything from it."""
    text = safe_text(text or "")
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"[*_`]{1,3}", "", text)


def _sentence(text: str) -> str:
    text = _plain(text).strip()
    m = re.search(r"(?<=[.!?])\s+(?=\S)", text)
    return _clean(text[:m.start()] if m else text, SUMMARY_CHARS)


def _title(text: str) -> str:
    bold = re.match(r"\s*\*\*(.+?)\*\*", text)
    if bold:
        return _clean(bold.group(1).rstrip(":. "), TITLE_CHARS)
    text = _plain(text).strip()
    cut = re.search(r"[:;(]|(?<=[.!?])\s|\s[-–—]\s", text)
    head = text[:cut.start()] if cut and cut.start() >= 12 else text
    return _clean(head.rstrip(".,: "), TITLE_CHARS)


def _slug(text: str, words: int = 6) -> str:
    parts = re.findall(r"\w+", _plain(text).lower().replace("'", ""))
    return "-".join(parts[:words])[:60] or "item"


def _date_in(text: str) -> Optional[float]:
    m = _DATE.search(text)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(0), "%Y-%m-%d"))
    except (ValueError, OverflowError):
        return None


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _blame(path: Path) -> dict:
    """{line: author time} from git, when the file is tracked; {} otherwise (never raises)."""
    try:
        out = subprocess.run(["git", "-C", str(path.parent), "blame", "--line-porcelain", "--", path.name],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode:
        return {}
    times, current = {}, None
    for line in out.stdout.splitlines():
        head = re.match(r"^[0-9a-f]{40} \d+ (\d+)", line)
        if head:
            current = int(head.group(1))
        elif line.startswith("author-time ") and current is not None:
            times[current] = float(line.split()[1])
    return times


def _imports(text: str, base: Path) -> list:
    out = []
    for line in text.splitlines():
        m = _IMPORT.match(line.strip())
        if m:
            target = m.group(1) or m.group(2)
            p = _path(target)
            out.append(p if p.is_absolute() else (base / p))
    return out


def _bullets(path: Path, text: str):
    """(section, line number, bullet text) for every top-level bullet; continuation lines and sub-bullets
    belong to their bullet. Tables, code blocks, paragraphs and marker lines are not rules."""
    section, current, fence, generated = "", None, False, False
    for number, raw in enumerate(text.splitlines(), 1):
        # The generated overlay lists switched-off items: never items itself.
        if raw.strip() in (OVERLAY_BEGIN, OVERLAY_END):
            generated = raw.strip() == OVERLAY_BEGIN
            if current:
                yield current
                current = None
            continue
        if generated:
            continue
        if raw.lstrip().startswith("```"):
            fence = not fence
        if fence:
            continue
        heading = _HEADING.match(raw)
        if heading:
            if current:
                yield current
                current = None
            section = re.sub(r"^\d+[a-z]?[.)]\s*", "", heading.group(2)).strip()
            continue
        bullet = _BULLET.match(raw) if raw[:1] not in (" ", "\t") else None
        if bullet:
            if current:
                yield current
            current = [section, number, bullet.group(1)]
        elif current and raw.strip() and raw[:1] in (" ", "\t"):
            current[2] += " " + raw.strip()
        elif current and not raw.strip():
            continue
        elif current:
            yield current
            current = None
    if current:
        yield current


def _frontmatter(text: str) -> tuple:
    meta, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            key = None
            for line in text[3:end].splitlines():
                m = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
                if m:
                    key, value = m.group(1), m.group(2).strip()
                    # A YAML block scalar (">-", "|") continues on the indented lines below it.
                    meta[key] = "" if re.fullmatch(r"[>|][-+]?", value) else value.strip("\"'")
                elif key and line[:1] in (" ", "\t") and line.strip():
                    meta[key] = (meta[key] + " " + line.strip()).strip()
                else:
                    key = None
            body = text[end + 4:]
    return meta, body


def _first_paragraph(body: str) -> str:
    for block in re.split(r"\n\s*\n", body):
        lines = [l for l in block.splitlines() if l.strip() and not _HEADING.match(l) and not l.startswith(("|", "<!--", "```"))]
        if lines:
            return " ".join(l.strip().lstrip("-*> ") for l in lines)
    return ""


def _h1(body: str) -> str:
    m = re.search(r"^#\s+(.+)$", body, re.M)
    return m.group(1).strip() if m else ""


class _Index:
    def __init__(self, cfg_settings: dict, now: float):
        self.s, self.now = cfg_settings, now
        self.items: dict = {}
        self.locks = [re.compile(p, re.I) for p in list(LOCKED) + [p for p in self.s["locked"] if isinstance(p, str)]
                      if _compiles(p)]
        self.memory_names = {n.lower() for n in self.s["memory_index"] if isinstance(n, str)}
        self.memory_agents: dict = {}
        self.sources: set = set()  # every file the scan read (hashes of real paths), items or not: never a write target

    def read(self, path: Path, limit: int = MAX_FILE) -> Optional[str]:
        """A source's text, recorded as a source. The overlay this module generated (known by its first line, never
        by its path) is neither a source nor items."""
        text = _read(path, limit)
        if text is None or is_generated(text):
            return None
        self.sources.add(_source_hash(path))
        return text

    def add(self, item: dict, merge: bool = True):
        ident = item["id"]
        old = self.items.get(ident)
        if old and merge and old["source"]["path"] != item["source"]["path"]:
            old["agents"] = sorted(set(old["agents"]) | set(item["agents"]), key=_agent_order)
            if old["text"] != item["text"]:
                old["drift"] = True
            return
        n = 2
        while ident in self.items:
            ident = "%s-%d" % (item["id"], n)
            n += 1
        item["id"] = ident
        self.items[ident] = item

    # rules: every agent's root file and what it imports
    def rules(self):
        docs = {str(_resolve(p)) for p in self._doc_files()}
        seen: dict = {}  # resolved path -> set of agents
        order: list = []

        def walk(path: Path, agent: str, depth: int):
            key = str(_resolve(path))
            if key in docs or depth > 4:
                return
            if path.name.lower() in self.memory_names:
                self._memory_index(path, agent)
                return
            first = key not in seen
            seen.setdefault(key, set()).add(agent)
            if first:
                order.append(path)
            text = self.read(path)
            if text is None:
                return
            for child in _imports(text, path.parent):
                walk(child, agent, depth + 1)

        for agent, root in self.s["agents"].items():
            if isinstance(root, str) and agent:
                walk(_path(root), agent, 0)
        for extra in self.s["rules"]:
            if isinstance(extra, str):
                for agent in self.s["agents"]:
                    walk(_path(extra), agent, 1)
        for path in order:
            text = self.read(path)
            if text is None:
                continue
            agents = sorted(seen[str(_resolve(path))], key=_agent_order)
            times = _blame(path)
            mtime = _mtime(path)
            for section, line, body in _bullets(path, text):
                if len(_plain(body).strip()) < 3:
                    continue
                title = _title(body)
                full = _clean(_plain(body), TEXT_CHARS)
                ident = _hid("rule", _slug(section, 4) if section else "general", _slug(title))
                self.add({"id": ident, "kind": "rule", "title": title, "summary": _sentence(body), "text": full,
                          "group": _clean(section, TITLE_CHARS) or "", "agents": list(agents),
                          "source": {"path": _spath(path), "line": line, "section": _clean(section, TITLE_CHARS)},
                          "date": _date_in(body) or times.get(line) or mtime,
                          "locked": any(p.search(title + " " + _sentence(body)) for p in self.locks)})

    def _memory_index(self, path: Path, agent: str):
        text = self.read(path) or ""
        for target in _LINK.findall(text):
            p = _path(target) if target.startswith(("/", "~")) else path.parent / target
            self.memory_agents.setdefault(str(_resolve(p)), set()).add(agent)

    def memories(self):
        for pattern in self.s["memory"]:
            if not isinstance(pattern, str):
                continue
            for d in sorted(glob.glob(os.path.expanduser(pattern))):
                base = Path(d)
                project = base.parent.name if base.name == "memory" else base.name
                key = _safe_name(project)  # the id keeps hashing this (stable); the label below is display only
                group = _project_label(project)
                for f in sorted(base.glob("*.md")):
                    if f.name.lower() in self.memory_names:
                        continue
                    text = self.read(f)
                    if text is None:
                        continue
                    meta, body = _frontmatter(text)
                    title = meta.get("name") or _h1(body) or f.stem
                    about = meta.get("description") or _first_paragraph(body)
                    agents = {"claude"} | self.memory_agents.get(str(_resolve(f)), set())
                    self.add({"id": _hid("memory", key, f.stem), "kind": "memory",
                              "title": _name(title),
                              "summary": _sentence(about), "text": _clean(_plain(_first_paragraph(body) or about), TEXT_CHARS),
                              "group": group, "agents": sorted(agents, key=_agent_order),
                              "source": {"path": _spath(f).replace(project, "…") if project.startswith("-") else _spath(f), "line": 1, "section": ""},
                              "date": _date_in(about) or _mtime(f), "locked": False}, merge=False)

    def skills(self):
        by_name: dict = {}
        for d, agents in self.s["skills"].items():
            if not isinstance(d, str) or not isinstance(agents, list):
                continue
            base = _path(d)
            try:
                entries = sorted(base.iterdir())
            except OSError:
                continue
            for entry in entries:
                f = entry / "SKILL.md"
                text = self.read(f, 1024 * 1024)
                if text is None:
                    continue
                meta, body = _frontmatter(text)
                name = (meta.get("name") or entry.name)[:80]
                about = meta.get("description") or _first_paragraph(body)
                key = re.sub(r"[^\w.:@+-]", "-", name)
                if key in by_name:
                    item = by_name[key]
                    item["agents"] = sorted(set(item["agents"]) | {a for a in agents if isinstance(a, str)}, key=_agent_order)
                    continue
                item = {"id": _hid("skill", key), "kind": "skill", "title": _clean(name, TITLE_CHARS),
                        "summary": _sentence(about), "text": _clean(_plain(about), TEXT_CHARS),
                        "group": _spath(base), "agents": sorted({a for a in agents if isinstance(a, str)}, key=_agent_order),
                        "source": {"path": _spath(f), "line": 1, "section": ""}, "date": _mtime(f),
                        "locked": False, "internal": True}
                by_name[key] = item
        for item in by_name.values():
            self.add(item, merge=False)

    def tools(self):
        """Server names only: a tool's settings (commands, environment, keys) are never read into the index."""
        by_name: dict = {}
        for agent, p in self.s["tools"].items():
            if not isinstance(p, str):
                continue
            path = _path(p)
            names = _toml_servers(path) if path.suffix == ".toml" else _json_servers(path)
            if path.is_file():
                self.sources.add(_source_hash(path))
            for name, native_on in names:
                item = by_name.get(name)
                if item is None:
                    item = by_name[name] = {"id": _hid("tool", name), "kind": "tool", "title": _clean(name, TITLE_CHARS),
                                            "summary": "", "text": "", "group": "", "agents": [],
                                            "source": {"path": _spath(path), "line": 1, "section": ""},
                                            "date": _mtime(path), "locked": False, "native_off": []}
                item["agents"] = sorted(set(item["agents"]) | {agent}, key=_agent_order)
                if not native_on:
                    item["native_off"].append(agent)
        for item in by_name.values():
            item["group"] = ", ".join(item["agents"])
            self.add(item, merge=False)

    def _doc_files(self) -> list:
        out = []
        for pattern in self.s["docs"]:
            if not isinstance(pattern, str):
                continue
            for hit in sorted(glob.glob(os.path.expanduser(pattern))):
                p = Path(hit)
                out += sorted(p.glob("*.md")) if p.is_dir() else ([p] if p.suffix == ".md" else [])
        return out

    def docs(self):
        for f in self._doc_files():
            text = self.read(f)
            if text is None:
                continue
            meta, body = _frontmatter(text)
            title = meta.get("name") or _h1(body) or f.stem
            about = meta.get("description") or _first_paragraph(body)
            self.add({"id": _hid("doc", _resolve(f)), "kind": "doc",
                      "title": _clean(_plain(title), TITLE_CHARS), "summary": _sentence(about),
                      "text": _clean(_plain(about), TEXT_CHARS), "group": _spath(f.parent), "agents": list(AGENTS),
                      "source": {"path": _spath(f), "line": 1, "section": ""}, "date": _mtime(f), "locked": False},
                     merge=False)


def _name(text: str) -> str:
    """A file-style name ("always-commit-and-push") read as words."""
    text = _plain(text).strip()
    if " " not in text and re.search(r"[-_]", text):
        text = re.sub(r"[-_]+", " ", text)
        text = text[:1].upper() + text[1:]
    return _clean(text, TITLE_CHARS)


def _safe_item(item: dict) -> dict:
    """Every text field of one item through safe_text: when the item is built and again when it is shown."""
    for key in ("title", "summary", "text"):
        item[key] = safe_text(item.get(key) or "")
    if item.get("group") != PRIVATE:
        item["group"] = safe_text(item.get("group") or "")
    item["source"] = dict(item.get("source") or {})
    for key in ("path", "section"):
        item["source"][key] = safe_text(item["source"].get(key) or "")
    return item


def _compiles(p) -> bool:
    try:
        re.compile(p)
        return True
    except (re.error, TypeError):
        return False


def _real(p) -> str:
    """The real path (symlinks followed, also for a file that does not exist yet)."""
    return os.path.realpath(os.path.expanduser(str(p)))


def _canonical(p) -> str:
    """The real path spelled the way the disk stores it. On a disk that ignores case (the Mac default) ~/.CLAUDE and
    ~/.claude are one folder: every existing part is replaced by the entry that is the same file, so two spellings of
    one file always compare equal. A part that does not exist yet is kept as written."""
    out = os.sep
    for name in _real(p).split(os.sep)[1:]:
        if not name:
            continue
        cand = os.path.join(out, name)
        if os.path.lexists(cand):
            try:
                if name not in os.listdir(out):
                    folded = name.casefold()
                    name = next((e for e in os.listdir(out)
                                 if e.casefold() == folded and os.path.samefile(os.path.join(out, e), cand)), name)
            except OSError:
                pass
        out = os.path.join(out, name)
    return out


def _same(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def is_generated(text: str) -> bool:
    return (text or "").lstrip("\ufeff").startswith(OVERLAY_BEGIN)


def _resolve(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p


def _agent_order(a: str) -> tuple:
    return (AGENTS.index(a) if a in AGENTS else len(AGENTS), a)


def _json_servers(path: Path) -> list:
    text = _read(path, MAX_JSON)
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        return []
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []
    return [(str(k)[:80], not (isinstance(v, dict) and v.get("disabled") is True)) for k, v in servers.items()]


def _toml_servers(path: Path) -> list:
    """`[mcp_servers.<name>]` tables and their `enabled` flag; nothing else in the file is kept."""
    text = _read(path) or ""
    out, current = {}, None
    for line in text.splitlines():
        head = re.match(r'^\s*\[\s*mcp_servers\.(?:"([^"]+)"|([\w-]+))\s*\]\s*$', line)
        if head:
            current = (head.group(1) or head.group(2))[:80]
            out.setdefault(current, True)
            continue
        if line.lstrip().startswith("["):
            current = None
        elif current:
            m = re.match(r"^\s*enabled\s*=\s*(true|false)\b", line)
            if m:
                out[current] = m.group(1) == "true"
    return list(out.items())


# --- the index -----------------------------------------------------------------------------------------------

def scan(cfg, now: Optional[float] = None) -> dict:
    """Read every configured source and save the private index."""
    now = time.time() if now is None else now
    s = settings(cfg)
    ix = _Index(s, now)
    ix.rules()
    ix.memories()
    ix.skills()
    ix.tools()
    ix.docs()
    locked_ids = {i for i in s["locked_ids"] if isinstance(i, str)}
    for item in ix.items.values():
        _safe_item(item)
        if item["id"] in locked_ids:
            item["locked"] = True
    index = {"scanned": now, "items": list(ix.items.values()), "sources": sorted(ix.sources)}
    write_private(folder() / "index.json", json.dumps(index, ensure_ascii=False))
    return index


def load_index() -> Optional[dict]:
    try:
        data = json.loads((folder() / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None


def fresh_index(cfg, max_age: float = 600, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    index = load_index()
    if index is None or not isinstance(index.get("scanned"), (int, float)) or now - index["scanned"] > max_age:
        index = scan(cfg, now)
    return index


def _find(index: dict, ident: str) -> dict:
    for item in index["items"]:
        if item.get("id") == ident:
            return item
    raise Unknown("unknown item")


# --- switches, log and undo ----------------------------------------------------------------------------------

@contextmanager
def _locked():
    private_dir(folder())
    fd = os.open(str(folder() / ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_state() -> dict:
    try:
        data = json.loads((folder() / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(data, dict) else {}


def is_on(state: dict, ident: str) -> bool:
    return state.get(ident, {}).get("on", True) is not False


def read_log() -> list:
    try:
        lines = (folder() / "log.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            out.append(entry)
    return out


def _append(entry: dict) -> None:
    private_dir(folder())
    fd = os.open(str(folder() / "log.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _set(state: dict, ident: str, on: bool, now: float, actor: str) -> None:
    if on:
        state.pop(ident, None)  # a missing id is on: the map holds only what is off
    else:
        state[ident] = {"on": False, "at": now, "by": actor}
    write_private(folder() / "state.json", json.dumps(state, ensure_ascii=False, sort_keys=True))


def _entry(action: str, item: dict, now: float, actor: str, origin: str, **extra) -> dict:
    return dict({"id": secrets.token_hex(6), "at": now, "action": action, "item": item["id"], "kind": item["kind"],
                 "title": item["title"], "actor": actor, "origin": origin}, **extra)


def locked_now(cfg, item: dict) -> bool:
    """Locked by the index, by the current `locked_ids`, or by the current lock patterns (re-checked every time)."""
    s = settings(cfg)
    if item.get("locked") or item.get("id") in {i for i in s["locked_ids"] if isinstance(i, str)}:
        return True
    if item.get("kind") != "rule":
        return False
    pats = [re.compile(p, re.I) for p in list(LOCKED) + [p for p in s["locked"] if isinstance(p, str)] if _compiles(p)]
    words = (item.get("title") or "") + " " + (item.get("summary") or "")
    return any(p.search(words) for p in pats)


def _switch(cfg, ident: str, on: bool, entry_for, now: float, actor: str, index: Optional[dict]) -> dict:
    """Change one switch, render, then log: if the overlay cannot be written, the switch is put back and
    nothing is logged, so the state, the history and the overlay never disagree."""
    state = read_state()
    previous = dict(state)
    _set(state, ident, on, now, actor)
    try:
        render(cfg, index=index)
    except BaseException:
        write_private(folder() / "state.json", json.dumps(previous, ensure_ascii=False, sort_keys=True))
        raise
    entry = entry_for()
    _append(entry)
    return entry


def toggle(cfg, ident: str, on: bool, actor: str = "owner", origin: str = "cli", now: Optional[float] = None,
           index: Optional[dict] = None) -> dict:
    """Switch one item on or off (owner only). A locked safety rule is never switched."""
    if not isinstance(on, bool) or not isinstance(ident, str) or not ITEM_ID.fullmatch(ident):
        raise KnowledgeError("bad request")
    now = time.time() if now is None else now
    index = index or fresh_index(cfg, now=now)
    item = _find(index, ident)
    if locked_now(cfg, item):
        raise Locked("locked safety rule")
    check_targets(cfg, index)
    with _locked():
        before = is_on(read_state(), ident)
        if before == on:
            return {"id": "", "item": ident, "after": on, "unchanged": True}
        return _switch(cfg, ident, on, lambda: _entry("toggle", item, now, actor, origin, before=before, after=on),
                       now, actor, index)


def undo(cfg, log_id: str, actor: str = "owner", origin: str = "cli", now: Optional[float] = None,
         index: Optional[dict] = None) -> dict:
    """Apply the inverse of one switch (itself logged, so an undo can be undone too).

    The lock is checked against the current settings and a current index (rebuilt when missing). An item that is no
    longer in the index can only be switched back on, the one direction that can never weaken a safety rule."""
    if not isinstance(log_id, str) or not LOG_ID.fullmatch(log_id):
        raise KnowledgeError("bad request")
    now = time.time() if now is None else now
    index = index or fresh_index(cfg, now=now)
    with _locked():
        target = next((e for e in read_log() if e["id"] == log_id), None)
        if target is None or target.get("action") not in ("toggle", "undo") or not isinstance(target.get("item"), str):
            raise Unknown("unknown change")
        ident, restore = target["item"], bool(target.get("before"))
        item = next((i for i in index["items"] if i.get("id") == ident), None)
        if item is None:
            if not restore or ident in {i for i in settings(cfg)["locked_ids"] if isinstance(i, str)}:
                raise Locked("the lock of a missing item cannot be verified")
            item = {"id": ident, "kind": target.get("kind", ""), "title": target.get("title", "")}
        elif locked_now(cfg, item):
            raise Locked("locked safety rule")
        check_targets(cfg, index)
        if is_on(read_state(), ident) != target.get("after"):
            raise Changed("the item changed since")
        return _switch(cfg, ident, restore, lambda: _entry("undo", item, now, actor, origin, before=target.get("after"),
                                                             after=restore, of=log_id), now, actor, index)


def history(now: Optional[float] = None, days: int = HISTORY_DAYS) -> list:
    """Switches and suggestions, newest first, each with `undone` (a later undo reversed it)."""
    now = time.time() if now is None else now
    log = read_log()
    undone, by_id = {}, {e["id"]: e for e in log}
    for e in log:
        if e.get("action") == "undo" and e.get("of") in by_id:
            undone[e["of"]] = e["id"]
            inner = by_id[e["of"]]
            if inner.get("action") == "undo" and inner.get("of"):
                undone.pop(inner["of"], None)
    rows = []
    for e in reversed(log):
        if e.get("action") not in ("toggle", "suggest") or not isinstance(e.get("at"), (int, float)):
            continue
        if now - e["at"] > days * 86400:
            break
        rows.append(dict(e, undone=e["id"] in undone, undo_id=undone.get(e["id"], "")))
    return rows


def suggest(cfg, ident: str, text: str, actor: str = "owner", origin: str = "cli", now: Optional[float] = None,
            index: Optional[dict] = None) -> dict:
    """The owner's change request for an agent: a ledger item. Nothing is edited here."""
    if not isinstance(text, str) or not text.strip() or len(text) > 1000 or not isinstance(ident, str):
        raise KnowledgeError("bad request")
    now = time.time() if now is None else now
    item = _find(index or fresh_index(cfg, now=now), ident)
    if locked_now(cfg, item):
        raise Locked("locked safety rule")
    note = " ".join(safe_text(text).split())
    line = "Knowledge change request for %s (%s, %s:%s): %s" % (
        item["id"], item["title"], item["source"]["path"], item["source"]["line"], note)
    _ledger_add(cfg, settings(cfg)["ledger_project"], line)
    entry = _entry("suggest", item, now, actor, origin, text=note)
    with _locked():
        _append(entry)
    return entry


def _ledger_add(cfg, project: str, text: str) -> None:
    tool = ((getattr(cfg, "brief", None) or {}).get("ledger_tool")
            or (getattr(cfg, "radio", None) or {}).get("ledger_tool"))
    if isinstance(tool, str):
        tool = [tool]
    if (not isinstance(tool, list) or not tool or not all(isinstance(s, str) and s for s in tool)
            or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", project or "")):
        raise OSError("no ledger tool")
    try:
        subprocess.run([*tool, project, "add", text], check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError):
        raise OSError("ledger tool unavailable") from None


# --- the overlay -----------------------------------------------------------------------------------------------

def _forbidden_roots() -> list:
    """The Claude home folder (settings, hooks, skills, CLAUDE.md) and its config file, as real paths."""
    roots = [_canonical(claude_home()), _canonical(Path.home() / ".claude")]
    files = [_canonical(Path.home() / ".claude.json"),
             _canonical(os.environ.get("CTB_CLAUDE_JSON") or Path.home() / ".claude.json")]
    return roots + files


def _source_hash(p) -> str:
    """A source as the index keeps it: the hash of its real path, so no file or folder name is stored."""
    return hashlib.sha256(_canonical(p).encode("utf-8", "surrogateescape")).hexdigest()


def _configured_sources(s: dict) -> set:
    """Files the settings name as sources, whether or not they exist yet (hashes of real paths)."""
    out = {_source_hash(_path(p)) for p in list(s["agents"].values()) + list(s["rules"]) + list(s["tools"].values())
           if isinstance(p, str)}
    return out


def writable(path, sources=()) -> bool:
    """Never under the Claude home folder, never ~/.claude.json, never any file a scan reads (symlinks followed)."""
    real = _canonical(path)
    roots = _forbidden_roots()
    for root in roots:
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            return False
    # By file identity too: the target and every existing folder above it against the Claude home and its config.
    probe = real
    while True:
        if os.path.lexists(probe) and any(_same(probe, root) for root in roots):
            return False
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if os.path.isdir(real):
        return False
    return _source_hash(real) not in set(sources)


def _sources(cfg, index: Optional[dict]) -> set:
    return set((index or {}).get("sources") or []) | _configured_sources(settings(cfg))


def _write_plain(path: Path, text: str, mode: Optional[int] = None) -> None:
    """Atomic write that keeps an existing file's mode (or sets `mode`) and never changes a parent folder's mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = 0o600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def off_items(index: Optional[dict] = None) -> list:
    index = index or load_index() or {"items": []}
    state = read_state()
    known = {i["id"]: i for i in index["items"]}
    out = []
    for ident, row in sorted(state.items()):
        if row.get("on") is False:
            item = known.get(ident)
            out.append(item or {"id": ident, "kind": ident.split(":", 1)[0], "title": ident, "text": "",
                                "source": {"path": "", "line": 0}, "missing": True})
    return out


SECTION_TITLES = {"rule": "Rules (do not apply)", "memory": "Memories (do not rely on)", "skill": "Skills (do not use)",
                  "tool": "Tools (do not call)", "doc": "Guides (do not follow)"}


def overlay_text(index: Optional[dict] = None, paused: bool = False) -> str:
    """The overlay. Its first line is the marker: a scan never reads it as a source, and render overwrites no other file.
    `paused` (the owner switched the `knowledge_overlay` add-on off): the list is empty, so every item is in force."""
    items = [] if paused else [i for i in off_items(index) if not i.get("locked")]
    lines = [OVERLAY_BEGIN, "# Switched off by the owner", "",
             "<!-- Generated by `harnessgram knowledge render`. Do not edit: switch items in the Cockpit. -->", ""]
    if not items:
        return "\n".join(lines + ["Nothing is switched off.", OVERLAY_END, ""])
    lines += ["The owner switched off the items below. Do not apply, rely on, use or call them until they are "
              "switched on again. Everything else in your instructions stays in force.", ""]
    for kind in KINDS:
        group = [i for i in items if i.get("kind") == kind]
        if group:
            lines.append("## " + SECTION_TITLES[kind])
            for i in group:
                where = i["source"].get("path") or ""
                where += (":%s" % i["source"]["line"]) if where and i["source"].get("line") else ""
                text = i.get("text") or i.get("title") or ""
                lines.append("- `%s`%s%s" % (i["id"], (" (" + where + ")") if where else "", (": " + text) if text else ""))
            lines.append("")
    return "\n".join(lines + [OVERLAY_END, ""])


def _index_for_render(cfg, index: Optional[dict]) -> dict:
    index = index or load_index()
    hashes = index.get("sources") if isinstance(index, dict) else None
    if not isinstance(hashes, list) or not all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes):
        index = scan(cfg)  # the guard needs the list of every file a scan reads
    return index


def check_targets(cfg, index: Optional[dict] = None) -> dict:
    """Refuse before anything changes. The overlay may be written only when it does not exist yet, or when it is a
    file this module generated (its first line is the overlay marker); never under the Claude home folder, never
    ~/.claude.json and never a file a scan read (real paths, so a symlink to a source is refused too)."""
    index = _index_for_render(cfg, index)
    target = _path(settings(cfg)["overlay"])
    if not writable(target, _sources(cfg, index)):
        raise Forbidden("the overlay path is not allowed")
    real = Path(_real(target))
    if os.path.lexists(str(target)) or real.exists():
        try:
            head = real.read_text(encoding="utf-8", errors="replace")[:len(OVERLAY_BEGIN) + 8]
        except OSError:
            raise Forbidden("the overlay path is not allowed") from None
        if not is_generated(head):
            raise Forbidden("the overlay path holds a file this tool did not write")
    return index


def _overlay_on(cfg) -> bool:
    try:
        from . import addons
        return addons.enabled(cfg, "knowledge_overlay")
    except Exception:
        return True


def render(cfg, index: Optional[dict] = None) -> list:
    """Write the overlay. Returns the paths written."""
    index = check_targets(cfg, index)
    overlay = _path(settings(cfg)["overlay"])
    text = overlay_text(index, paused=not _overlay_on(cfg))
    try:
        current = overlay.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current == text:
        return []
    _write_plain(overlay, text)
    return [overlay]


# --- plain-words notes ---------------------------------------------------------------------------------------------
# Each item may have a note an agent wrote once from the masked export: a Hebrew and an English title, one plain
# sentence each, and an advice with one short reason. A note is keyed by the item's content hash, so when the text
# changes the old note simply stops matching. Section headers get a Hebrew and an English title the same way.

ADVICE = ("keep", "optional", "review")
NOTE_LIMITS = {"title_he": 60, "summary_he": 160, "title_en": 60, "summary_en": 160, "why_he": 120, "why_en": 120}
SECTION_LIMITS = {"title_he": 60, "title_en": 60}
SECTION_KINDS = ("rule", "memory")  # skill, doc and tool groups are folder paths or agent names, not headings
MAX_NOTES_FILE = 4 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{16}")
# Control characters and the bidi embeddings and overrides (LRE, RLE, PDF, LRO, RLO, ALM): never part of a note.
# Directional isolates (U+2066-U+2069) are kept only when they pair up, the way a short English name sits in Hebrew.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u202a-\u202e\u061c\ufeff\u200b]")
_ISOLATE = re.compile("[\u2066-\u2069]")
_HEBREW = re.compile("[\u05d0-\u05ea]")
_FSI_SPAN = re.compile("\u2068[^\u2066-\u2069]*\u2069")
# A locked safety rule's reason is fixed: an agent's words never explain away a safety rule.
LOCKED_WHY = {"why_he": "כלל בטיחות, תמיד פעיל", "why_en": "Safety rule, always on"}
_HOME_PATH = re.compile(r"(?<![\w~])/(?:Users|home)/[^/\s]+")


def content_hash(item: dict) -> str:
    """The note key of one item: its kind and the masked words the index keeps (changes whenever the text does)."""
    parts = [item.get("kind") or ""] + [safe_text(item.get(k) or "") for k in ("title", "summary", "text")]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:16]


def section_hash(group: str) -> str:
    return hashlib.sha256(("section\0" + safe_text(group or "")).encode("utf-8")).hexdigest()[:16]


def _isolates_pair(text: str) -> bool:
    depth = 0
    for ch in _ISOLATE.findall(text):
        depth += -1 if ch == "\u2069" else 1
        if depth < 0:
            return False
    return depth == 0


def _note_text(value, limit: int) -> Optional[str]:
    """One note field cleaned and masked, or None when it is missing, empty or longer than its limit."""
    if not isinstance(value, str):
        return None
    text = _CONTROL.sub(" ", value)
    if not _isolates_pair(text):
        text = _ISOLATE.sub("", text)
    text = re.sub(r"\s+", " ", safe_text(text)).strip()
    return text if 0 < len(text) <= limit else None


def clean_note(raw, section: bool = False, locked: bool = False) -> Optional[dict]:
    """A valid note, or None. Every field is required; a locked safety rule's advice is always `keep`."""
    if not isinstance(raw, dict):
        return None
    out = {}
    for key, limit in (SECTION_LIMITS if section else NOTE_LIMITS).items():
        text = _note_text(raw.get(key), limit)
        if text is None:
            return None
        # Hebrew fields are Hebrew: at least one Hebrew letter, and Latin words only inside a U+2068...U+2069 isolate.
        if key.endswith("_he") and (not _HEBREW.search(text) or re.search(r"[A-Za-z]", _FSI_SPAN.sub("", text))):
            return None
        out[key] = text
    if not section:
        advice = raw.get("advice")
        if advice not in ADVICE:
            return None
        out["advice"] = "keep" if locked else advice
        if locked:
            out.update(LOCKED_WHY)
    return out


def notes_path(cfg) -> Path:
    return _path(settings(cfg)["notes"])


def _read_notes_file(path: Path) -> dict:
    text = _read(path, MAX_NOTES_FILE)
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def load_notes(cfg) -> dict:
    """{"items": {hash: note}, "sections": {hash: note}}: only valid entries; a broken file is simply no notes."""
    data = _read_notes_file(notes_path(cfg))
    out = {"items": {}, "sections": {}}
    for part, section in (("items", False), ("sections", True)):
        rows = data.get(part)
        if not isinstance(rows, dict):
            continue
        for key, raw in rows.items():
            if isinstance(key, str) and _HASH.fullmatch(key):
                note = clean_note(raw, section=section)
                if note:
                    out[part][key] = note
    return out


def _section_rows(index: dict) -> dict:
    """{hash: group} for every heading the Cockpit shows that a note can name."""
    out = {}
    for item in index["items"]:
        group = item.get("group") or ""
        if item.get("kind") in SECTION_KINDS and group and group != PRIVATE:
            out.setdefault(section_hash(group), group)
    return out


def missing_notes(cfg, index: Optional[dict] = None) -> list:
    """Every item and section heading with no valid note, with the masked words an agent needs to write one.
    No source path, no line number: only the id, the hash, the kind, the heading and the masked text."""
    index = index or fresh_index(cfg)
    notes = load_notes(cfg)
    out = []
    for key, group in _section_rows(index).items():
        if key not in notes["sections"]:
            out.append({"id": "section:" + key, "hash": key, "kind": "section", "section": _export_text(group)})
    for item in index["items"]:
        key = content_hash(item)
        if key in notes["items"]:
            continue
        group = item.get("group") or ""
        if group == PRIVATE:
            group = ""
        elif item["kind"] in ("skill", "doc"):
            group = os.path.basename(group.rstrip("/"))  # a folder path: only its last name is a label
        out.append({"id": item["id"], "hash": key, "kind": item["kind"], "section": _export_text(group),
                    "title": _export_text(item.get("title")), "summary": _export_text(item.get("summary")),
                    "text": _export_text(item.get("text")), "locked": bool(locked_now(cfg, item))})
    return out


def _export_text(text) -> str:
    """Masked words with every home folder shown as ~ (a source may quote /Users/<name>/... in its text)."""
    text = text or ""
    home = str(Path.home())
    if len(home) > 1:
        text = text.replace(home, "~")
    return _HOME_PATH.sub("~", safe_text(text))


def _import_rows(data) -> list:
    """(part, hash, raw note) from either an export-shaped list or a notes-shaped {"items": {}, "sections": {}}."""
    rows = []
    if isinstance(data, list):
        for raw in data:
            if isinstance(raw, dict):
                rows.append(("sections" if raw.get("kind") == "section" else "items", raw.get("hash"), raw))
            else:
                rows.append(("items", None, raw))
    elif isinstance(data, dict):
        for part in ("items", "sections"):
            block = data.get(part)
            if isinstance(block, dict):
                rows += [(part, key, raw) for key, raw in block.items()]
    else:
        raise KnowledgeError("the notes file must be a list or an object")
    return rows


def _own_export(data) -> bool:
    return isinstance(data, list) and all(isinstance(r, dict) and "id" in r and "hash" in r for r in data)


def _own_notes(data) -> bool:
    return isinstance(data, dict) and data.get("version") == 1 and set(data) <= {"version", "items", "sections"}


def _json_target(cfg, path: Path, index: Optional[dict], shape) -> None:
    """A notes file or an export may be written only as a new `.json` file, or over a JSON file of the same shape (one
    this tool wrote); never under the Claude home folder, never ~/.claude.json and never a file a scan reads."""
    if path.suffix != ".json" or not writable(path, _sources(cfg, _index_for_render(cfg, index))):
        raise Forbidden("the path is not allowed")
    real = Path(_real(path))
    if os.path.lexists(str(path)) or real.exists():
        text = _read(real, MAX_NOTES_FILE)
        try:
            ok = text is not None and (not text.strip() or shape(json.loads(text)))
        except ValueError:
            ok = False
        if not ok:
            raise Forbidden("the path holds a file this tool did not write")


def import_notes(cfg, data, index: Optional[dict] = None) -> dict:
    """Validate and merge notes. Unknown hashes (text that changed, or never existed) and bad entries are counted and
    skipped; a locked safety rule's advice is stored as `keep`. Returns the counts."""
    index = index or fresh_index(cfg)
    path = notes_path(cfg)
    _json_target(cfg, path, index, _own_notes)
    items = {content_hash(i): i for i in index["items"]}
    sections = _section_rows(index)
    counts = {"added": 0, "updated": 0, "unchanged": 0, "bad": 0, "unknown": 0, "locked_keep": 0, "pruned": 0}
    with _locked():
        notes = load_notes(cfg)
        for part, key, raw in _import_rows(data):
            if not isinstance(key, str) or not _HASH.fullmatch(key):
                counts["bad"] += 1
                continue
            known = items if part == "items" else sections
            if key not in known:
                counts["unknown"] += 1
                continue
            locked = part == "items" and bool(locked_now(cfg, known[key]))
            note = clean_note(raw, section=part == "sections", locked=locked)
            if note is None:
                counts["bad"] += 1
                continue
            if locked and raw.get("advice") != "keep":
                counts["locked_keep"] += 1
            old = notes[part].get(key)
            counts["unchanged" if old == note else "updated" if old else "added"] += 1
            notes[part][key] = note
        # Notes for text that changed or is gone are dropped, so the file never outgrows its read limit.
        before = len(notes["items"]) + len(notes["sections"])
        notes["items"] = {k: v for k, v in notes["items"].items() if k in items}
        notes["sections"] = {k: v for k, v in notes["sections"].items() if k in sections}
        counts["pruned"] = before - len(notes["items"]) - len(notes["sections"])
        _write_plain(path, json.dumps({"version": 1, "items": notes["items"], "sections": notes["sections"]},
                                      ensure_ascii=False, sort_keys=True, indent=1) + "\n", mode=0o600)
    return counts


def write_export(cfg, rows: list, out: str, index: Optional[dict] = None) -> Path:
    """The missing-notes export written 0600, never onto a source or under the Claude home folder."""
    path = _path(out)
    _json_target(cfg, path, index, _own_export)
    _write_plain(path, json.dumps(rows, ensure_ascii=False, indent=1) + "\n", mode=0o600)
    return path


# --- what the Cockpit shows ------------------------------------------------------------------------------------

def view(cfg, now: Optional[float] = None, rescan: bool = False) -> dict:
    now = time.time() if now is None else now
    index = scan(cfg, now) if rescan else fresh_index(cfg, now=now)
    state = read_state()
    notes = load_notes(cfg)
    items, counts = [], {k: {"total": 0, "on": 0} for k in KINDS}
    for raw in index["items"]:
        item = dict(raw, on=is_on(state, raw["id"]))
        note = notes["items"].get(content_hash(raw))
        if note:
            item["note"] = dict(note, advice="keep", **LOCKED_WHY) if locked_now(cfg, raw) else dict(note)
        if item["kind"] == "tool" and item.get("native_off") and set(item["native_off"]) >= set(item.get("agents") or []):
            item["connected"] = False
        else:
            item["connected"] = item["on"]
        _safe_item(item)
        if item["text"] in (item["summary"], item["title"]):
            item["text"] = ""  # the phone shows the summary; the same words are not sent twice
        items.append(item)
        if item["kind"] in counts:
            counts[item["kind"]]["total"] += 1
            counts[item["kind"]]["on"] += 1 if (item["connected"] if item["kind"] == "tool" else item["on"]) else 0
    rows = history(now)
    for row in rows:
        row["title"] = safe_text(row.get("title") or "")
        row.pop("text", None)  # a suggestion's own words stay in the ledger
    week = sum(1 for r in rows if now - r["at"] <= 7 * 86400 and not r["undone"])
    sections = {group: notes["sections"][key] for key, group in _section_rows(index).items() if key in notes["sections"]}
    return {"ok": True, "now": int(now), "scanned": int(index.get("scanned") or now), "items": items, "counts": counts,
            "sections": sections,
            "locked": sum(1 for i in items if i.get("locked")), "history": rows, "week": week,
            "overlay": _spath(_path(settings(cfg)["overlay"]))}

