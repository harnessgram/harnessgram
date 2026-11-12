"""Claude Code dialogs (questions, forms, permission and plan prompts, menus) as Telegram cards.

Design rule: text on the terminal pane can never approve anything. A card shows
the dialog and its options as text, with Cancel and Full screen as its only buttons; the owner types
the option number or name in the session's topic. Only that typed text, from the paired owner, in that
session's topic, while the same dialog is still open (identity and stamp unchanged), selects the option.

Tap-to-answer is deliberately absent. It would need positive proof that the numbered dialog is an
AskUserQuestion, recorded by a PreToolUse hook before the question is shown; the installed hooks only
include PostToolUse, which fires after the answer, so there is no proof and everything is typed-only.

The screen picture and keypad (inbox.menu_buttons) stay only as a last resort behind a small button.
Everything shown is cleaned with creds.redact and progress.hide_signin first.
"""
from __future__ import annotations

import fcntl
import hashlib
import html
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import asks
from .config import Config, home_chat, private_dir, state_dir, write_private
from .creds import is_secret, redact
from .i18n import get_lang, t
from .telegram import TelegramError
from .topics import place

REMIND_EVERY = 600.0     # seconds between re-shown cards for a dialog that is still open
SEEN_GRACE = 20.0        # a dialog must be open this long before the watcher shows it (the hook is faster)
TYPED_TTL = 1800.0       # a typed-answer window closes by itself after 30 minutes
CARD_LIMIT = 3900        # Telegram refuses a message over 4096 characters, counted after HTML escaping
MAX_ANSWER = 2000

_OPT = re.compile(r"^\s*(?P<cur>[❯›])?\s*(?P<num>\d{1,2})[.)]\s+(?P<rest>\S.*)$")
_SEP = re.compile(r"^[\s─━═_\-╌┄]+$")
_CHECKED = re.compile(r"^(?:\[\s*[xX✔✓*]\s*\]|[☑☒✔✓◉✅])\s*")
_UNCHECKED = re.compile(r"^(?:\[\s*\]|[☐◯○⬜□])\s*")
_OTHER = re.compile(r"(?i)^(?:type something|other\b|something else|type your own|custom answer|enter (?:a )?custom)")
# Only a row without a checkbox can be the confirm row, and only when it says so in full ("Next.js" is an option).
_CONFIRM_ROW = re.compile(r"(?i)^(?:submit(?: answers)?|done|confirm|next)\.?$")
# The dialog's own key-hint row: key names joined by dots, nothing else ("Press Enter to confirm the change."
# in a reply is prose, not a hint).
_KEY = r"(?:enter|esc|space|tab|shift\+tab|ctrl\+[a-z](?:\s+ctrl\+[a-z])*(?:\s+twice)?|arrow keys|[a-z]|[↑↓←→]+(?:/[↑↓←→]+)?)"
_SEGMENT = rf"{_KEY}(?:\s*/\s*{_KEY})*\s+to\s+[a-z][a-z ]{{1,24}}"
_REAL_HINT = re.compile(rf"(?i)^{_SEGMENT}(?:\s*[·•|]\s*{_SEGMENT})*$")
# Remote Control's connected screen advertises an optional QR display, not a blocking prompt.
_REMOTE_QR_HINT = re.compile(r"(?i)^space to (?:show|hide) QR code$")
_REMOTE_CONNECTED = re.compile(r"(?:^|[·•])\s*Connected\s*(?:[·•]|$)", re.I)
_REMOTE_CAPACITY = re.compile(r"^\s*Capacity:\s*\d+/\d+\b", re.I)
# What may sit under the hint row: the status line, never an input box or text.
_STATUS_ROW = re.compile(r"(?i)^[⏵⏸⏯]|\d+\s*%|\btokens\b|\bcontext\b|\b(?:opus|sonnet|haiku)\b|plan mode|shift\+tab|\? for shortcuts|esc to interrupt|\b\d+s\b|\b\d+m\b")
_TABS = re.compile(r"←.*→")
# An option that grants a lasting permission, trusts something, spends money or switches a mode: the card
# says so. (Curly apostrophes are normalised first; wrapped rows are joined before matching.)
_STANDING = re.compile(r"(?i)don'?t ask again|do not ask again|always|allow all|all future|auto-?accept|accept edits|"
                       r"bypass|skip permissions|from now on|every time|for all (?:projects|sessions)|"
                       r"for this project|this session|permanently|forever|remember|\btrust|disable|turn off|"
                       r"buy usage credits|usage credits|\bi accept\b|use auto mode|auto mode|set auto mode|"
                       r"default permission mode|keep allowing|allow access to")
_TRUST = re.compile(r"(?i)\btrust")
_PLAN = re.compile(r"(?i)ready to code|claude'?s plan|exit plan mode")
_SUBMIT_ROW = re.compile(r"(?i)^\s*(?P<cur>[❯›])?\s+(?:Submit|Next)\s*$")
_EXTRA_ROW = re.compile(r"(?i)^(?:[❯›]\s*)?(?:chat about this|type something\.?|skip|other)$")
_RULE = "────────"
_DASHED = "╌╌╌╌╌╌╌╌"
_PERM = re.compile(r"(?i)do you want to (?:proceed|make this edit|create|allow|run|overwrite|apply|use)|"
                   r"\brun command\b|exit plan mode|wants to exit plan|wants to fetch|"
                   r"allow (?:this|claude)|permission to|wants to (?:use|run|edit|write|fetch)|\bbash command\b|"
                   r"\bbash\(|\bedit file\b|\bwrite file\b|\bcreate file\b|\bmcp\b|\bweb ?fetch\b|\bweb ?search\b|"
                   r"\btool use\b|\bread file\b")
_HEADER = re.compile(r"(?i)^(?:bash(?: command)?|run command|exit plan mode\??|read(?: file)?|grep|glob|edit(?: file)?|write(?: file)?|create file|"
                     r"update|fetch|web ?fetch|web ?search|notebook ?edit|mcp\b.*|task|agent|skill)(?:\(.*\))?$")
_SECRET_PATH = re.compile(r"(?i)\.ssh|\.env(?:rc)?\b|\.aws|\.config|\.gnupg|\.netrc|\.npmrc|\.kube|auth\.json|"
                          r"id_rsa|id_ed25519|keychain|credential|\.pem\b")
# Sensitive areas (owner rules): the card is red. Over-matching only costs a louder card.
_SENSITIVE = re.compile(
    r"(?i)\b(?:pay|payment|purchase|billing|charge[sd]?|invoice|subscri\w+|credit card|refund|extra usage|usage credits|"
    r"spend|prod|production|promot\w+|go[- ]?live|rollout|roll out|release|deploy\w*|canary|"
    r"delet\w+|remove|drop (?:table|database|schema)|rm -r\w*|truncate|destroy|wipe|erase|"
    r"force[- ]?push|reset --hard|--force|"
    r"login|log in|sign[- ]?in|password|passwd|credential\w*|secret|api[- ]?key|token|oauth|2fa|account\w*|"
    r"publish\w*|public(?:ly)?|announce\w*|send (?:an? )?(?:e-?mail|message)|whatsapp|"
    r"app store|testflight|play store|play console|store submission|submit for review|app review)\b|₪|\$\s?\d"
    r"|הרשא|סיסמ|תשלום|מחיק|פרסו|פרודקשן|ייצור")
_ANSWER_LIKE = re.compile(r"^\d{1,2}(?:\s*[,\s]\s*\d{1,2})*$")


@dataclass
class Option:
    num: int
    label: str
    checked: Optional[bool] = None
    cursor: bool = False
    other: bool = False
    typed_only: bool = False
    confirm: bool = False


@dataclass
class Dialog:
    question: str
    options: List[Option]
    multi: bool = False
    sensitive: bool = False
    lasting: bool = False
    trust: bool = False
    plan: bool = False
    perm: bool = False
    preview: bool = False        # side-box layout: a digit only moves the selector, Enter answers
    cut: bool = False            # a permission prompt whose top (rule and header) is not on the screen
    submit: Optional[int] = None  # row index of the unnumbered Submit row of a multi-select
    nav: List = field(default_factory=list)  # selectable rows in screen order: ("o", num) or ("s",)
    cursor_at: Optional[int] = None          # index into nav of the row that carries the selector
    hint: str = ""
    tabs: str = ""
    body: str = ""
    stamp: str = ""
    identity: str = ""
    rows: List[str] = field(default_factory=list)


# --- reading the screen ---------------------------------------------------------------------

def clean(body: str) -> str:
    """Everything that leaves the machine: credentials redacted, sign-in links replaced by a note."""
    from .progress import hide_signin
    return hide_signin(redact(body or ""))


def _norm_text(text: str) -> str:
    return text.replace("\u2019", "'").replace("\u2018", "'").replace("\u02bc", "'").replace("\u200f", "")


def _norm_row(row: str) -> str:
    row = _norm_text(row).rstrip()
    return re.sub(r"^[│┃]\s?|\s?[│┃]$", "", row).rstrip()


def hint_row_index(rows: List[str]) -> Optional[int]:
    """Index of the dialog's key-hint row when it is the bottom of the screen: at most three status rows
    (never an input box, a prompt or other text) may follow it."""
    i, below = len(rows) - 1, 0
    while i >= 0:
        text = rows[i].strip()
        if not text:
            i -= 1
            continue
        if _REAL_HINT.match(text):
            if (_REMOTE_QR_HINT.fullmatch(text)
                    and any(_REMOTE_CONNECTED.search(r) for r in rows[:i])
                    and any(_REMOTE_CAPACITY.match(r) for r in rows[:i])):
                return None
            return i
        if below >= 3 or text.startswith(("❯", "╭", "╰", "┌", "└")) or _SEP.match(text) or not _STATUS_ROW.search(text):
            return None
        below += 1
        i -= 1
    return None


def bottom_hint(body: str) -> bool:
    return hint_row_index([_norm_row(r) for r in (body or "").splitlines()]) is not None


def _unparsed_rows(body: str) -> List[str]:
    """The last twelve text rows up to and including the hint row (a status line below it ticks)."""
    rows = [_norm_row(r) for r in (body or "").splitlines()]
    end = hint_row_index(rows)
    rows = rows[:end + 1] if end is not None else rows
    return [r.strip() for r in rows if r.strip()][-12:]


def stamp_of(body: str) -> str:
    """For a screen that is not a parsed dialog."""
    return hashlib.sha256("\n".join(_unparsed_rows(body)).encode("utf-8")).hexdigest()[:12]


_TRAILING_OK = re.compile(r"^\s")


def _trailing_ok(rows: List[str], last_row: int) -> Optional[int]:
    """With no key-hint row: the rows under the options may only be blank, a rule, an indented
    description, or a status line. Returns the end of the dialog (exclusive) or None when text or an input
    box follows (an ordinary reply, an idle prompt)."""
    end, status = last_row + 1, 0
    for k in range(last_row + 1, len(rows)):
        text = rows[k].strip()
        if not text or _SEP.match(text):
            continue
        if text.startswith(("❯", "╭", "╰", "┌", "└", ">")):
            return None
        if _STATUS_ROW.search(text) and not _TRAILING_OK.match(rows[k]):
            status += 1
            if status > 3:
                return None
            continue
        if _TRAILING_OK.match(rows[k]) and status == 0 and k - last_row <= 8:
            end = k + 1
            continue
        return None
    return end


def _label_of(rest: str) -> str:
    rest = re.split(r"\s{2,}", rest.strip())[0]  # a side preview box starts after two or more spaces
    return " ".join(rest.split())


def parse(body: str) -> Optional[Dialog]:
    """A numbered-choice dialog at the bottom of the screen, or None. Fails closed. It needs either the
    dialog's own key-hint row at the bottom of the screen, or a selector mark on an option with nothing
    under the options but descriptions and a status line (no input box, no text): an ordinary reply, a
    quoted dialog and the owner's own prompt are not dialogs."""
    rows = [_norm_row(r) for r in (body or "").splitlines()]
    cands = []
    for i, row in enumerate(rows):
        m = _OPT.match(row)
        if m:
            cands.append((i, int(m["num"]), bool(m["cur"]), m["rest"].strip()))
    if len(cands) < 2:
        return None
    group = [cands[-1]]
    for cand in reversed(cands[:-1]):
        if cand[1] == group[0][1] - 1 and group[0][0] - cand[0] <= 14:
            group.insert(0, cand)
        elif cand[1] >= group[0][1]:
            break
    if group[0][1] != 1 or len(group) < 2:
        return None
    first_row, last_row = group[0][0], group[-1][0]
    if [c[1] for c in group] != list(range(1, len(group) + 1)):
        return None
    hint_at = hint_row_index(rows)
    if hint_at is not None and hint_at > last_row:
        if any(rows[k].strip() and not _SEP.match(rows[k].strip()) and not rows[k].startswith("    ")
               and not _EXTRA_ROW.match(rows[k].strip()) for k in range(last_row + 1, hint_at)) \
                or hint_at - last_row > 12:
            return None
        hint, end = " ".join(rows[hint_at].split())[:120], hint_at + 1
    else:
        if not any(c[2] for c in group):
            return None
        end = _trailing_ok(rows, last_row)
        if end is None:
            return None
        hint = ""
    group_rows = [c[0] for c in group] + [end]
    options, multi, submit = [], False, None
    preview = "add notes" in hint.lower() or any(re.search(r"\s{2,}[┌│└╭╰]", rows[c[0]]) for c in group)
    for gi, (row_i, num, cur, rest) in enumerate(group):
        cont_rows = []
        for k in range(row_i + 1, group_rows[gi + 1]):
            sm = _SUBMIT_ROW.match(rows[k])
            if sm and submit is None:
                submit = (k, bool(sm["cur"]))
            elif rows[k].strip() and not _SEP.match(rows[k].strip()):
                cont_rows.append(rows[k].strip())
        cont = " ".join(cont_rows)
        checked = None
        if _CHECKED.match(rest):
            checked, rest, multi = True, _CHECKED.sub("", rest, count=1), True
        elif _UNCHECKED.match(rest):
            checked, rest, multi = False, _UNCHECKED.sub("", rest, count=1), True
        label = _label_of(rest)
        options.append(Option(num=num, label=label[:80], checked=checked, cursor=cur,
                              other=bool(_OTHER.match(label)), typed_only=bool(_STANDING.search(label + " " + cont)),
                              confirm=checked is None and bool(_CONFIRM_ROW.match(label))))
    if re.search(r"(?i)\bspace\b.*\b(?:select|toggle)", hint) or submit is not None:
        multi = True
    # selectable rows in screen order, and where the selector stands
    entries = [(c[0], ("o", c[1]), c[2]) for c in group]
    if submit is not None:
        entries.append((submit[0], ("s",), submit[1]))
    entries.sort(key=lambda e: e[0])
    nav = [e[1] for e in entries]
    cursor_at = next((i for i, e in enumerate(entries) if e[2]), None)
    question_rows, blank, qstart, top = [], 0, first_row, 0
    for i in range(first_row - 1, -1, -1):
        text = rows[i].strip()
        if not text:
            blank += 1
            if blank >= 2 and question_rows:
                top = i
                break
            continue
        if _SEP.match(text):
            if question_rows:
                top = i
                break
            continue
        if text.startswith("❯") or (_TABS.search(text) and question_rows):
            top = i
            break
        blank = 0
        question_rows.append(text)
        qstart = i
        if len(question_rows) >= 6:
            top = i
            break
    question = "\n".join(reversed(question_rows))
    tabs = ""
    for row in rows[max(0, first_row - 10):first_row]:
        if _TABS.search(row):
            tabs = " ".join(row.split())[:120]
    # A tool prompt: its own solid rule and header must be on the screen above the options. Everything
    # from that rule down is the dialog (and is hashed); without them the top is cut off.
    rule = next((i for i in range(first_row - 1, -1, -1) if rows[i].strip() == _RULE), None)
    header = None
    if rule is not None:
        header = next((i for i in range(rule + 1, first_row) if rows[i].strip()), None)
    header_ok = header is not None and bool(_HEADER.match(rows[header].strip()))
    dashed = sum(1 for k in range((rule or 0) + 1, first_row) if rows[k].strip() == _DASHED) if rule is not None else 0
    solid_inside = rule is not None and any(rows[k].strip() == _RULE for k in range(rule + 1, first_row))
    region_start = min(top, qstart)
    perm_text = "\n".join(rows[region_start:end])
    perm = bool(_PERM.search(perm_text)) or header_ok
    cut = False
    if perm:
        if header_ok and dashed in (0, 2) and not solid_inside:
            region_start = rule
        else:
            cut, region_start = True, 0   # the top is not visible: the whole screen above counts
    region_rows = rows[region_start:end]
    region = "\n".join(region_rows)
    sensitive = bool(_SENSITIVE.search(region)) or bool(_SECRET_PATH.search(region))
    trust = bool(_TRUST.search(question))
    lasting = any(o.typed_only for o in options) or bool(_STANDING.search(question))
    # The stamp and the identity cover the nearest paragraph above the options (for a tool prompt: every
    # row from its rule), the options and the hint row. A banner or status line elsewhere cannot make a
    # card stale, and the same options under another command can never share one.
    near = first_row
    while near > 0 and not rows[near - 1].strip():
        near -= 1
    while near > 0 and rows[near - 1].strip() and not _SEP.match(rows[near - 1].strip()):
        near -= 1
    if perm:
        near = region_start
    stamp_src = "\n".join(r.strip() for r in rows[near:end])
    shown = "\n".join(r.strip() for r in rows[near:first_row]) if perm else ""
    para = " ".join(r.strip() for r in rows[near:first_row])
    labels = "|".join(re.sub(r"[^a-z0-9]+", " ", o.label.lower()).strip() for o in options if not o.other)
    ident_src = (f"{re.sub(r'[^a-z0-9]+', ' ', para.lower()).strip()}|{len(options)}|{labels}|"
                 f"{hashlib.sha256(shown.encode()).hexdigest()}")
    return Dialog(question=question, options=options, multi=multi, sensitive=sensitive, lasting=lasting, trust=trust,
                  plan=bool(_PLAN.search(perm_text)), perm=perm, cut=cut, preview=preview, submit=submit[0] if submit else None,
                  nav=nav, cursor_at=cursor_at, hint=hint, tabs=tabs, body=_norm_text(body or ""),
                  stamp=hashlib.sha256(stamp_src.encode("utf-8")).hexdigest()[:12],
                  identity=hashlib.sha256(ident_src.encode()).hexdigest()[:12], rows=rows)


# The spinner row of a running /compact: "✻ Compacting conversation… (23s · ↓ 1.2k tokens · esc to interrupt)".
# Only its own glyph and words: a command, a description or an option that says "Compacting..." is not it.
_COMPACTING = re.compile(r"(?i)^[·✢✳✶✻✽*]\s*Compacting conversation\b[^()]*(?:…|\.\.\.)")


def compacting_screen(body: str) -> bool:
    """The bottom of the screen is the compaction spinner. The reply above it may hold numbered lists and
    the spinner's own "esc to interrupt" reads like a key hint, but nothing here is a question."""
    rows = [_norm_row(r).strip() for r in (body or "").splitlines()]
    return any(_COMPACTING.match(r) and len(r) < 160 for r in [r for r in rows if r][-8:])


_EXIT_BG_TITLE = re.compile(r"(?i)^background work is running$")
_EXIT_BG_OPTIONS = ("exit and stop tasks", "move to background and exit", "stay")
_NUMBERED_ROW = re.compile(r"^\s*(?:[❯›]\s*)?(\d{1,2})[.)]\s+(\S.*)$")
_TASK_ROW = re.compile(r"^(?:shell|agent|monitor|task|workflow|local agent|remote agent)s? · \S", re.I)
FOOTER_MARK = re.compile(r"⏵⏵|⏸|\? for shortcuts|\bmode on\b")


def exit_background_screen(body: str) -> bool:
    """Claude's answer to /exit while shells or agents still run: "Background work is running ... 1. Exit and
    stop tasks  2. Move to background and exit  3. Stay". Options 1 and 2 lose or detach the owner's work,
    so only Escape is ever sent to it. A live dialog is the bottom of the screen: nothing under its title
    may be a prompt box or the footer, and the last numbered row on the screen is one of its three options,
    alone on its row. A reply that merely quotes the dialog fails at least one of these."""
    rows = [_norm_row(r) for r in (body or "").splitlines()]
    rows = [r for r in rows if r.strip()]
    title = next((i for i in range(len(rows) - 1, -1, -1) if _EXIT_BG_TITLE.match(rows[i].strip())), None)
    if title is None:
        return False
    below = rows[title + 1:]
    fixed = [r for r in below if not _TASK_ROW.match(r.strip())]  # the task list may be any length
    if len(fixed) > 12 or any(FOOTER_MARK.search(r) for r in below):
        return False
    numbered, last_numbered = [], 0
    for k, r in enumerate(below):
        text = r.strip()
        if text.startswith("❯") and not _NUMBERED_ROW.match(r):
            return False  # an input box under the title: the dialog is long gone
        m = _NUMBERED_ROW.match(r)
        if m:
            if re.search(r"\s{2,}\d{1,2}[.)]\s", m.group(2)):
                return False  # several options on one row is a quotation
            last_numbered = k
            numbered.append(re.split(r"\s{2,}", m.group(2).strip())[0].strip().lower())
    if len(below) - 1 - last_numbered > 3:
        return False  # text under the options: only the hint row (and a status row) may follow
    return bool(numbered) and numbered[-1] in _EXIT_BG_OPTIONS and any(
        n in _EXIT_BG_OPTIONS[:2] for n in numbered)


def exit_background_tasks(body: str) -> list:
    """The rows the live exit dialog lists under "The following will stop when you exit:" ("shell · sleep 600"),
    without the kind and the "·". Claude may cut a long command with "…". Empty when the screen is not
    that dialog or lists nothing."""
    if not exit_background_screen(body):
        return []
    rows = [_norm_row(r).strip() for r in (body or "").splitlines()]
    rows = [r for r in rows if r]
    title = max(i for i, r in enumerate(rows) if _EXIT_BG_TITLE.match(r))
    out = []
    for r in rows[title + 1:]:
        if _NUMBERED_ROW.match(r) or r.startswith("❯"):
            break
        if "·" in r and not r.lower().startswith("enter to"):
            out.append(r.split("·", 1)[1].strip())
    return [o for o in out if o]


def is_exit_background_dialog(body: str) -> bool:
    """The parsed dialog is exactly Claude's three-option exit question and nothing else."""
    d = parse(body)
    return bool(d and exit_background_screen(body) and not d.perm
                and [re.sub(r"\s+", " ", o.label.lower()).strip() for o in d.options] == list(_EXIT_BG_OPTIONS))


def open_screen(session: str):
    """(pane, body, dialog or None, open) for the session's terminal; pane None when there is none.
    `open` is strict: a parsed dialog, or a screen whose bottom is a key-hint row. A parsed dialog is always
    open. Otherwise nothing is while the compaction spinner is shown, or in the first moments of a compaction
    (before the spinner is drawn)."""
    from . import compact, inbox as I
    pane = I.pane_for_session(session) if session else None
    if not pane:
        return None, "", None, False
    try:
        body = I.pane_text(pane)
    except Exception:
        return pane, "", None, False
    if is_exit_background_dialog(body) and I.recently_moving(session):
        # A bridge move typed /exit and owns this dialog: it presses Escape and waits for the work. Showing
        # it as a question would let the owner answer something the move had already cancelled.
        return pane, body, None, False
    d = parse(body)
    if d:
        return pane, body, d, True
    if compacting_screen(body) or compact.just_started(session):
        return pane, body, None, False
    return pane, body, None, bottom_hint(body)


def _identity_of(d: Optional[Dialog], body: str) -> str:
    if d:
        return d.identity
    return "u" + hashlib.sha256("\n".join(_unparsed_rows(body)).encode()).hexdigest()[:11]  # digits count


# --- state: one record per session --------------------------------------------------------------

def _path():
    return private_dir(state_dir()) / "dialogs.json"


def _update(fn):
    root = private_dir(state_dir())
    with open(root / "dialogs.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        out = fn(data)
        write_private(_path(), json.dumps(data, ensure_ascii=False))
        return out


def rec_get(session: str) -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        rec = data.get(session) if isinstance(data, dict) else None
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def rec_set(session: str, rec: dict) -> None:
    _update(lambda data: data.__setitem__(session, rec))


def rec_del(session: str) -> None:
    _update(lambda data: data.pop(session, None))


def _log(what: str) -> None:
    from .inbox import poll_log
    poll_log("dialog " + what)


# --- the card -----------------------------------------------------------------------------------

def fit(head: str, body: str, tail: str = "", limit: int = CARD_LIMIT, tag: str = "pre") -> str:
    """head + <pre>body</pre> + tail, with the body cut from the top until the HTML-escaped message fits.
    Escaping turns each quote into six characters, so an unbounded screen could pass 4096 and Telegram
    refused it (the question card never arrived)."""
    lines = body.splitlines()
    while True:
        text = "\n".join(lines)
        if len(html.escape(text)) + len(head) + len(tail) + 40 <= limit or not lines:
            break
        if len(lines) == 1:
            lines = [lines[0][int(len(lines[0]) * 0.8):]]
            continue
        lines = lines[1:]
    opening, closing = ("<blockquote expandable>", "</blockquote>") if tag == "quote" else ("<pre>", "</pre>")
    return f"{head}{opening}{html.escape(text)}{closing}{tail}" if text else f"{head}{tail}"


def _lang(cfg) -> str:
    return getattr(cfg, "ui_language", None) or get_lang()


def _label(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _utility_row(stamp: str, lang: str) -> list:
    """Cancel (red) and Full screen (no style): on every card."""
    return [{"text": t("dlg_btn_cancel", lang=lang), "callback_data": f"dg:e:{stamp}", "style": "danger"},
            {"text": t("dlg_btn_screen", lang=lang), "callback_data": "dg:s"}]


def _norm_label(text: str) -> str:
    return " ".join(_norm_text(text).split())


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9\u0590-\u05ff]+", "", _norm_text(text).lower())


_BUILTIN_ROW = re.compile(r"(?i)^(?:chat about this|type something|skip)")


def proof_for(session: str, d: Optional[Dialog]) -> Optional[dict]:
    """The recorded AskUserQuestion question this dialog is, or None (typed-only).

    Positive proof only: the PreToolUse hook recorded this session's call, the pane's options equal one
    recorded question's options exactly and in order (only the built-in rows may follow them), the recorded
    question text is on the screen, and multiSelect agrees with the pane. Pane text alone proves nothing.
    Rule C wins: a sensitive, lasting-grant, trust, plan or permission dialog is never answered by a tap, and
    neither is a recorded question or option that matches those words."""
    if d is None or not session or d.sensitive or d.lasting or d.trust or d.plan or d.perm:
        return None
    rec = asks.load(session)
    if not rec:
        return None
    shown = _alnum("\n".join(d.rows))
    for q in rec["questions"]:
        try:
            labels = [_norm_label(x)[:80] for x in q["options"]]
            text, multi = q["question"], bool(q["multiSelect"])
        except (KeyError, TypeError):
            continue
        k = len(labels)
        if (not isinstance(text, str) or k < 2 or k > len(d.options) or multi != d.multi
                or [_norm_label(o.label) for o in d.options[:k]] != labels
                or [o.num for o in d.options[:k]] != list(range(1, k + 1))
                or any(o.other or o.confirm for o in d.options[:k])):
            continue
        if not all(o.other or o.confirm or _BUILTIN_ROW.match(o.label) for o in d.options[k:]):
            continue
        if _alnum(text) not in shown:
            continue
        blob = text + "\n" + "\n".join(q["options"])
        if _SENSITIVE.search(blob) or _STANDING.search(_norm_text(blob)) or _SECRET_PATH.search(blob):
            continue
        return q
    return None


_OPTION_ROW = re.compile(r"(?i)^\s*[❯›]?\s*(?:\d{1,2}[.)]\s+\S|(?:yes|no)\b)")


def has_option_rows(body: str) -> bool:
    """Any numbered row, any Yes/No row, or any selector row that is not the idle input box."""
    rows = [_norm_row(r) for r in (body or "").splitlines()][-30:]
    for i, row in enumerate(rows):
        if _OPTION_ROW.match(row):
            return True
        if row.lstrip().startswith(("❯", "›")) and row.strip() not in ("❯", "›"):
            boxed = i > 0 and rows[i - 1].strip() == _RULE and i + 1 < len(rows) and rows[i + 1].strip() == _RULE
            if not boxed:
                return True
    return False


def selector_row(body: str) -> bool:
    """A "❯ 1. text" row in the last 25 rows that is not the idle input box (the owner's own prompt
    "1. please fix" sits between two solid rules)."""
    rows = [_norm_row(r) for r in (body or "").splitlines()][-25:]
    for i, row in enumerate(rows):
        if re.match(r"^\s*❯\s*\d+[.)]\s", row):
            boxed = i > 0 and rows[i - 1].strip() == _RULE and i + 1 < len(rows) and rows[i + 1].strip() == _RULE
            below = rows[i + 1:]
            tidy = all(not r.strip() or _SEP.match(r.strip()) or r.startswith(" ") or _OPT.match(r)
                       or _REAL_HINT.match(r.strip()) or _STATUS_ROW.search(r) for r in below)
            if not boxed and tidy:
                return True
    return False


def keys_ok(body: str) -> bool:
    """Arrow and Enter buttons only for a screen with no option rows at all (nothing there can be an
    approval) that does not look like a permission, trust, login or payment screen either. A screen with
    options is answered by typing."""
    from . import inbox as I
    tail = "\n".join((body or "").splitlines()[-25:])
    return not (has_option_rows(body) or I._SENSITIVE_SCREEN.search(tail) or _PERM.search(tail)
                or _SENSITIVE.search(tail) or _SECRET_PATH.search(tail))


KEY_BUTTONS = (("up", "⬆️"), ("down", "⬇️"), ("enter", "⏎"))


def markup(d: Optional[Dialog], lang: str, proof: Optional[dict] = None, body: str = "", stamp: str = "") -> dict:
    stamp = d.stamp if d else stamp
    rows = []
    if d is not None and proof is not None:
        k = len(proof["options"])
        for o in d.options[:k]:
            label = _label(o.label)
            style = "primary" if re.search(r"(?i)\(recommended\)", proof["options"][o.num - 1]) else "success"
            if d.multi:
                text, data = f"{'☑' if o.checked else '☐'} {o.num}. {label}", f"dg:t:{o.num}:{stamp}"
            else:
                text, data = f"{o.num}. {label}", f"dg:o:{o.num}:{stamp}"
            rows.append([{"text": text, "callback_data": data, "style": style}])
        if not d.multi:
            for o in d.options[k:]:
                if o.other:  # "Other" only for a recorded question
                    rows.append([{"text": "✍️ " + _label(o.label), "callback_data": f"dg:x:{o.num}:{stamp}",
                                  "style": "success"}])
        else:
            rows.append([{"text": t("dlg_btn_done", lang=lang), "callback_data": f"dg:d:{stamp}", "style": "success"}])
        rows = rows[:9]
    elif d is None and keys_ok(body):
        rows.append([{"text": icon, "callback_data": f"dg:k:{name}:{stamp}", "style": "success"}
                     for name, icon in KEY_BUTTONS])
    rows.append(_utility_row(stamp, lang))
    return {"inline_keyboard": rows}


def unknown_markup(stamp: str, lang: str, body: str = "") -> dict:
    return markup(None, lang, body=body, stamp=stamp)


def kind_of(d: Optional[Dialog], proof: Optional[dict] = None, keys: bool = False) -> str:
    if d is None:
        return "keys" if keys else "unknown"
    if d.sensitive:
        return "sensitive"
    if d.cut and d.perm:
        return "cut"
    if d.trust:
        return "trust"
    if d.plan:
        return "plan"
    if d.lasting:
        return "lasting"
    if d.perm:
        return "perm"
    if proof is not None:
        return "tapmulti" if d.multi else "tap"
    if d.tabs:
        return "quiz"
    return "multi" if d.multi else "typed"


_ICON = {"cut": "🛡️", "unknown": "🧩", "keys": "🧩", "sensitive": "🔒", "trust": "🤝", "plan": "📝", "lasting": "🛡️", "perm": "🛡️",
         "quiz": "📚", "multi": "☑️", "typed": "❓", "tap": "❓", "tapmulti": "☑️"}
_GRADE = {"cut": "perm", "unknown": "unknown", "keys": "unknown", "sensitive": "sensitive", "trust": "lasting", "plan": "lasting",
          "lasting": "lasting", "perm": "perm", "quiz": "typed", "multi": "typed", "typed": "typed", "tap": "safe",
          "tapmulti": "safe"}
_TITLE = {"tap": "dlg_title", "typed": "dlg_title", "tapmulti": "dlg_t_multi", "keys": "dlg_t_unknown"}


def option_lines(d: Dialog, lang: str) -> str:
    """The options as text, always (a tap card shows them too)."""
    out = []
    for o in d.options:
        if o.confirm and d.multi:  # the confirm row of a multi-select is a button, a review screen's "Submit answers" is an option
            continue
        mark = ("☑ " if o.checked else "☐ ") if o.checked is not None else ""
        star = " ⭐" if re.search(r"(?i)\(recommended\)", o.label) else ""  # only what the session itself recommends
        out.append(f"{o.num} · {mark}{o.label}{star}")
        if o.other:
            out.append("   ↳ " + t("dlg_opt_other", lang=lang))
        elif o.typed_only:
            out.append("   ↳ " + t("dlg_opt_lasting", lang=lang))
    return "\n".join(out)


def type_line(d: Dialog, lang: str, proof: Optional[dict] = None) -> str:
    """What to type when the card has no answer buttons: the real option numbers (and, in English, labels).
    The Hebrew line carries digits only so English never shares a line with Hebrew."""
    if proof is not None or d.multi or (d.cut and d.perm):  # a cut permission prompt refuses typed answers
        return ""
    opts = [o for o in d.options if not o.other]
    if not opts:
        return ""
    if lang.startswith("he"):
        n = " / ".join(str(o.num) for o in opts)
    else:
        n = " or ".join(f"{o.num} for {o.label}" for o in opts)
    return t("dlg_type_line", lang=lang, n=n)


def card_text(d: Optional[Dialog], body: str, lang: str, title_key: str = "dlg_title",
              proof: Optional[dict] = None) -> str:
    kind = kind_of(d, proof, keys=d is None and keys_ok(body))
    title = t(_TITLE.get(kind) or "dlg_t_" + kind, lang=lang)
    if title_key == "dlg_title_again":
        title = t("dlg_title_again", lang=lang)
    head = f"{_ICON[kind]} <b>{html.escape(title)}</b>\n{time.strftime('%H:%M:%S')}\n"
    head += html.escape(t("dlg_grade_" + _GRADE[kind], lang=lang)) + "\n"
    head += f"<i>{html.escape(t('dlg_note_' + kind, lang=lang))}</i>\n"
    if d is None:
        if kind != "keys":  # with the arrow buttons under the card the key words are only noise
            head += html.escape(t("dlg_keys_line", lang=lang)) + "\n"
        # The part that waits is the bottom of the screen: it is shown open and in the terminal's own
        # columns, not folded behind the first lines of an old reply (the owner could not tell what waited).
        return fit(head, waiting_part(clean(body)))
    else:
        listing = option_lines(d, lang)
        if listing:
            head += html.escape(listing) + "\n"
        line = type_line(d, lang, proof)
        if line:
            head += html.escape(line) + "\n"
    return fit(head, clean(body)[-3000:], tag="quote")


SHOWN_ROWS = 16  # rows of an unread screen a card shows; the Full screen button has the rest


def waiting_part(body: str) -> str:
    """The bottom of the screen without blank rows, the input box's rules and the status rows under it."""
    rows = [r.rstrip() for r in body.replace("\u200f", "").splitlines()]
    rows = [r for r in rows if r.strip() and not re.fullmatch(r"\s*[─━╌]{8,}.*", r) and not re.fullmatch(r"\s*❯\s*", r)]
    return "\n".join(rows[-SHOWN_ROWS:])


KEPT_SCREENS = 20


def keep_unread(stamp: str, body: str) -> None:
    """Keep the (redacted) screen the reader could not turn into buttons, newest KEPT_SCREENS only, in the
    private state folder: each one is a screen the reader should learn."""
    try:
        folder = private_dir(state_dir() / "unread-screens")
        write_private(folder / f"{stamp}.txt", clean(body))
        for old in sorted(folder.glob("*.txt"), key=lambda p: p.stat().st_mtime)[:-KEPT_SCREENS]:
            old.unlink()
    except OSError:
        pass


def _send(bot, cfg, thread, text: str, markup_: dict, silent: bool = False, chat: Optional[int] = None) -> Optional[int]:
    where = home_chat(cfg) if chat is None else chat
    params = {"chat_id": where, "text": text, "parse_mode": "HTML", "reply_markup": markup_,
              "disable_notification": silent}
    if thread:
        params["message_thread_id"] = thread
    for attempt in range(2):  # a permission prompt may block itself within a minute: one quick retry
        try:
            return bot.call("sendMessage", params).get("message_id")
        except (TelegramError, AttributeError) as exc:
            _log(f"card send failed: {type(exc).__name__} {getattr(exc, 'code', '')} "
                 f"{str(getattr(exc, 'description', ''))[:120]}")
            code, desc = getattr(exc, "code", None), str(getattr(exc, "description", "") or exc)
            transient = (code == 429 or (code or 0) >= 500 or code is None) and "Bad Request" not in desc
            if attempt == 0 and transient:
                time.sleep(2)
                continue
            break
    # A card that cannot be formatted still reaches the owner: plain text, no screen, same buttons.
    plain = {"chat_id": where, "disable_notification": silent, "reply_markup": markup_,
             "text": html.unescape(re.sub(r"<[^>]+>", "", text.split("<pre>")[0])).strip()[:1000]
             + "\n" + t("dlg_plain_fallback", lang=_lang(cfg))}
    if thread:
        plain["message_thread_id"] = thread
    try:
        return bot.call("sendMessage", plain).get("message_id")
    except (TelegramError, AttributeError) as exc:
        _log(f"plain card send failed: {type(exc).__name__} {getattr(exc, 'code', '')} "
             f"{str(getattr(exc, 'description', ''))[:120]}")
        return None


def send_card(cfg: Config, bot, session: str, thread, translate: bool = False, silent: bool = False,
              title_key: str = "dlg_title", chat: Optional[int] = None) -> bool:
    """Show the session's open dialog in its topic and remember it. False when nothing is open.
    `chat`: the project group of the topic (None = the home chat)."""
    from . import inbox as I
    pane, body, d, is_open = open_screen(session)
    loose = False
    if not is_open:
        # A menu the strict reader does not know (no hint row): shown as text with Cancel, never typed into.
        loose = bool(pane and body and I._dialog_open(body, session))
        if not loose:
            return False
    lang = _lang(cfg)
    stamp = d.stamp if d else stamp_of(body)
    proof = proof_for(session, d)
    text = card_text(d, body, lang, title_key, proof)
    kb = markup(d, lang, proof) if d else unknown_markup(stamp, lang, body)
    if d is None:
        _log("unparsed screen shown as text, stamp " + stamp)
        keep_unread(stamp, body)
    old = rec_get(session)
    ident_now = _identity_of(d, body)
    mid = _send(bot, cfg, thread, text, kb, silent=silent, chat=chat)
    if mid is None:
        return False
    _drop_keyboard(bot, cfg, old, keep=mid)
    keep_text = bool(old.get("text_mode") and old.get("identity") == ident_now)  # "Other" stays open
    rec_set(session, {"identity": ident_now, "stamp": stamp, "message_id": mid, "thread": thread,
                      "sent_at": time.time(), "typed": True, "sensitive": bool(d and d.sensitive),
                      "unparsed": d is None, "multi": bool(d and d.multi), "loose": loose,
                      "options": [{"num": o.num, "label": o.label, "confirm": o.confirm, "other": o.other}
                                  for o in (d.options if d else [])],
                      "text_mode": keep_text, "text_at": old.get("text_at", 0) if keep_text else 0,
                      **({} if chat is None else {"chat": chat})})  # no "chat" = the home chat
    if translate:
        # The question goes out at once as Claude Code shows it; its translation is edited in afterwards
        # (the card waited seconds for the translation), unless it was answered meanwhile.
        try:
            from .mirror import translate_card
            done, done_kb = translate_card(cfg, text, clean(body), kb)
            now = rec_get(session)
            if (done, done_kb) != (text, kb) and len(done) <= 4000 \
                    and now.get("message_id") == mid and now.get("identity") == ident_now:
                _edit(bot, cfg, mid, done, done_kb, chat=chat)
        except Exception:
            pass
    return True


def _drop_keyboard(bot, cfg, rec: dict, keep=None) -> None:
    mid = rec.get("message_id")
    if isinstance(mid, int) and mid != keep:
        try:
            bot.call("editMessageReplyMarkup", {"chat_id": rec.get("chat") or home_chat(cfg), "message_id": mid,
                                                "reply_markup": {"inline_keyboard": []}})
        except TelegramError:
            pass


def _edit(bot, cfg, mid, text: str, markup_: Optional[dict] = None, chat: Optional[int] = None) -> None:
    params = {"chat_id": home_chat(cfg) if chat is None else chat, "message_id": mid, "text": text, "parse_mode": "HTML"}
    params["reply_markup"] = markup_ if markup_ is not None else {"inline_keyboard": []}
    try:
        bot.call("editMessageText", params)
    except TelegramError:
        pass


def _say(bot, cfg, thread, text: str, chat: Optional[int] = None, **extra) -> None:
    params = {"chat_id": home_chat(cfg) if chat is None else chat, "text": text, "disable_notification": True, **extra}
    if thread:
        params["message_thread_id"] = thread
    try:
        bot.call("sendMessage", params)
    except TelegramError:
        pass


# --- pressing keys ------------------------------------------------------------------------------

def _keys(pane: str, *keys: str) -> bool:
    from . import inbox as I
    return I._tmux("send-keys", "-t", pane, *keys).returncode == 0


def tmux_text(text: str) -> str:
    """tmux reads a trailing ";" as a command separator and drops it: "\\;" arrives as ";"."""
    return text[:-1] + "\\;" if text.endswith(";") else text


def _literal(pane: str, text: str) -> bool:
    from . import inbox as I
    # "--" ends the options: text such as "-R" must be typed, never read as a tmux flag
    return (I._tmux("send-keys", "-t", pane, "-l", "--", tmux_text(text)).returncode == 0
            and I._tmux("send-keys", "-t", pane, "Enter").returncode == 0)


def _select(pane: str, d: Dialog, num: int) -> bool:
    """A single-choice answer: the option's own digit key (the CLI selects on it)."""
    return num <= 9 and not d.multi and not d.preview and _keys(pane, str(num))


def _answer(session: str, pane: str, d: Dialog, num: int) -> bool:
    """Answer a single-choice option. In the side-box (preview) layout a digit only moves the selector, so
    the selector is walked there with verified arrow keys and Enter answers."""
    if d.multi:
        return False
    if not d.preview:
        return _select(pane, d, num)
    return _move_to(session, pane, d, ("o", num)) is not None and _keys(pane, "Enter")


def _step(session: str, pane: str, d: Dialog, key: str, expect: int) -> Optional[Dialog]:
    """One arrow key, then read the screen again: it must be the same dialog with the selector exactly one
    row further. Anything else stops the whole sequence."""
    if not _keys(pane, key):
        return None
    time.sleep(0.25)
    _p, _b, d2, _o = open_screen(session)
    if d2 is None or d2.identity != d.identity or d2.nav != d.nav or d2.cursor_at != expect:
        return None
    return d2


def _move_to(session: str, pane: str, d: Dialog, target: tuple) -> Optional[Dialog]:
    """Move the selector to `target` ((\"o\", number) or (\"s\",)) one verified key at a time."""
    if target not in d.nav or d.cursor_at is None:
        return None
    want, cur = d.nav.index(target), d
    while cur.cursor_at != want:
        step = 1 if want > cur.cursor_at else -1
        cur = _step(session, pane, cur, "Down" if step > 0 else "Up", cur.cursor_at + step)
        if cur is None:
            return None
    return cur


def _toggle(session: str, pane: str, d: Dialog, num: int) -> Optional[Dialog]:
    """Tick or untick one checkbox row; the screen must show the flipped box afterwards."""
    opt = next((o for o in d.options if o.num == num), None)
    if opt is None or opt.checked is None or opt.other or opt.confirm:
        return None
    at = _move_to(session, pane, d, ("o", num))
    if at is None or not _keys(pane, "Space"):
        return None
    time.sleep(0.25)
    _p, _b, d2, _o = open_screen(session)
    now = next((o for o in (d2.options if d2 else []) if o.num == num), None)
    if d2 is None or d2.identity != d.identity or now is None or now.checked != (not opt.checked):
        return None
    return d2


def _confirm_multi(session: str, pane: str, d: Dialog) -> Optional[Dialog]:
    """Move to the Submit row (unnumbered in the real CLI: Enter anywhere else toggles the row under the
    selector), check that the selector stands on it, then press Enter. The dialog before Enter, or None."""
    target = ("s",) if ("s",) in d.nav else next((("o", o.num) for o in d.options if o.confirm), None)
    if target is None:
        return None
    at = _move_to(session, pane, d, target)
    if at is None or not _keys(pane, "Enter"):
        return None
    return at


def _after(cfg, bot, session: str, thread, previous: str, answered_mid, answered_text: str) -> None:
    """Close the answered card and, when another question follows (a form with several), show it. A
    screen that did not change (the key did not land) gets its card back instead of a false "answered"."""
    time.sleep(1.2)
    lang = _lang(cfg)
    found = place(cfg, session)
    chat = found.away if found else None  # the session's own group, whichever chat the answer came from
    _pane, body, d, is_open = open_screen(session)
    if is_open and _identity_of(d, body) == previous:
        if answered_mid:
            if d:
                pr = proof_for(session, d)
                _edit(bot, cfg, answered_mid, card_text(d, body, lang, proof=pr), markup(d, lang, pr), chat=chat)
                rec_set(session, dict(rec_get(session), stamp=d.stamp))
            else:
                _edit(bot, cfg, answered_mid, card_text(None, body, lang), unknown_markup(stamp_of(body), lang, body),
                      chat=chat)
        return
    if answered_mid:
        _edit(bot, cfg, answered_mid, answered_text, chat=chat)
    if is_open:
        send_card(cfg, bot, session, thread, chat=chat)
    else:
        rec_del(session)
        _hand_over(session)


def _hand_over(session: str) -> None:
    """The dialog has closed: messages that waited behind it are handed over (the nudge itself checks that
    the prompt is idle and empty)."""
    from . import inbox as I
    try:
        box = I.default_inbox()
        if box.pending(session):
            threading.Thread(target=I._reopen_or_nudge, args=(box, session), daemon=True).start()
    except Exception as exc:
        _log(f"hand over failed: {type(exc).__name__}")


def _release_held(cfg, bot, session: str, pane: str) -> None:
    """After Cancel the turn may stop at the prompt: hand over the messages that waited behind the dialog.
    Never while the session still works (the Escape of the dialog is then not an interrupt of a turn)."""
    from . import inbox as I
    try:
        if "esc to interrupt" in I.pane_text(pane, 12):
            return
        I._interrupted_idle(cfg, bot, session)
    except Exception as exc:
        _log(f"release failed: {type(exc).__name__}")


def _answered_text(lang: str, labels: List[str]) -> str:
    return f"✅ <b>{html.escape(t('dlg_answered', lang=lang))}</b>\n" + "\n".join(html.escape(x) for x in labels)


# --- buttons ------------------------------------------------------------------------------------

def _card_live(session, pane, body, d, is_open, rec, stamp) -> bool:
    """The card's screen is still the screen it was made for: same stamp, same identity, still open."""
    cur = d.stamp if d else stamp_of(body)
    return bool(pane and stamp and stamp == cur and rec and rec.get("identity") == _identity_of(d, body)
                and (rec.get("unparsed") or d is not None) and (is_open or rec.get("loose")))


def _refresh_card(cfg, bot, session, mid, lang, chat: Optional[int] = None) -> None:
    """After a key that left the dialog open: show the new screen in the same card and move the record."""
    from . import inbox as I
    _pane, body, d, is_open = open_screen(session)
    if not (d or is_open or I._dialog_open(body, session)) or mid is None:
        return
    pr = proof_for(session, d)
    kb = markup(d, lang, pr) if d else unknown_markup(stamp_of(body), lang, body)
    _edit(bot, cfg, mid, card_text(d, body, lang, proof=pr), kb, chat=chat)
    rec = rec_get(session)
    if rec:
        rec_set(session, dict(rec, identity=_identity_of(d, body), stamp=d.stamp if d else stamp_of(body)))


def button(q: dict, cfg: Config, bot, m: dict, session: str) -> str:
    """dg: callbacks from a card, in the terminal background lane. Cancel and Full screen are always there.
    Answer buttons (o, x, t, d) exist only on a card built from a PreToolUse record that the live pane still
    matches; the proof and the screen are checked again at the tap. Key buttons (k) exist only for a screen
    we do not understand and that does not look sensitive."""
    from . import inbox as I
    lang = _lang(cfg)
    data = q.get("data") or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    thread = m.get("message_thread_id")
    chat = I._chat_arg(cfg, m)  # the group the card is in (None = the home chat)
    mid = m.get("message_id")

    def toast(key: str, **kw):
        I._answer_button(bot, q, t(key, lang=lang, **kw))

    if action == "s":  # the last resort: the raw screen with the keypad
        pane = I.pane_for_session(session)
        if not pane:
            toast("toast_not_available")
            return "ignored"
        toast("toast_refreshing")
        body = I.pane_text(pane)
        _edit(bot, cfg, mid, I.screen_html(t("screen_session_title", lang=lang), body), I.menu_buttons(body, cfg=cfg), chat=chat)
        return "dialog-screen"
    if action not in ("e", "o", "x", "t", "d", "k"):
        toast("dlg_toast_typed")
        return "typed-required"
    pane, body, d, is_open = open_screen(session)
    rec = rec_get(session)
    stamp = parts[-1]
    if not _card_live(session, pane, body, d, is_open, rec, stamp):
        toast("toast_question_closed")  # one press: a card whose screen has changed does nothing
        return "stale"
    ident = _identity_of(d, body)
    if action == "e":
        I.forget_replay(session)
        if not _keys(pane, "Escape"):
            toast("toast_not_available")
            return "ignored"
        toast("toast_key_sent", name="esc")
        _after(cfg, bot, session, thread, ident, mid, _answered_text(lang, [t("dlg_cancelled", lang=lang)]))
        _release_held(cfg, bot, session, pane)
        return "dialog-cancel"
    if action == "k":
        name = parts[2] if len(parts) > 3 else ""
        key = {"up": "Up", "down": "Down", "enter": "Enter"}.get(name)
        if d is not None or key is None or not keys_ok(body):
            toast("dlg_toast_typed")
            return "typed-required"
        if not _keys(pane, key):
            toast("toast_not_available")
            return "ignored"
        toast("toast_key_sent", name=name)
        time.sleep(0.6)
        _pane, body2, d2, open2 = open_screen(session)
        if d2 is None and _identity_of(None, body2) != ident and not open2 and not I._dialog_open(body2, session):
            _after(cfg, bot, session, thread, ident, mid, _answered_text(lang, [t("dlg_typed_answer", lang=lang)]))
        else:
            _refresh_card(cfg, bot, session, mid, lang, chat=chat)
        return "dialog-key"
    # answer buttons: positive proof, checked again now
    proof = proof_for(session, d)
    num = int(parts[2]) if len(parts) > 3 and parts[2].isdigit() else 0
    k = len(proof["options"]) if proof else 0
    opt = next((o for o in (d.options if d else []) if o.num == num), None)
    if proof is None or (action in ("o", "x", "t") and opt is None) or (action != "d" and parts[-1] != d.stamp):
        toast("dlg_toast_typed")
        return "typed-required"
    if action == "o" and (d.multi or num > k or opt.other or opt.typed_only):
        toast("dlg_toast_typed")
        return "typed-required"
    if action == "t" and (not d.multi or num > k):
        toast("dlg_toast_typed")
        return "typed-required"
    if action == "x" and (d.multi or not opt.other or num <= k):
        toast("dlg_toast_typed")
        return "typed-required"
    if action == "d" and not d.multi:
        toast("dlg_toast_typed")
        return "typed-required"
    if action == "o":
        if not _answer(session, pane, d, num):
            toast("toast_not_available")
            return "ignored"
        toast("toast_key_sent", name=str(num))
        _after(cfg, bot, session, thread, ident, mid, _answered_text(lang, [f"{opt.num}. {opt.label}"]))
        return "dialog-choice"
    if action == "x":
        if not _answer(session, pane, d, num):
            toast("toast_not_available")
            return "ignored"
        toast("toast_key_sent", name=str(num))
        rec_set(session, dict(rec, identity=ident, text_mode=True, text_at=time.time(), thread=thread, message_id=mid))
        _edit(bot, cfg, mid, _answered_text(lang, [f"{opt.num}. {opt.label}"]), chat=chat)
        _say(bot, cfg, thread, t("dlg_text_prompt", lang=lang), chat=chat)
        return "dialog-other"
    if action == "t":
        if _toggle(session, pane, d, num) is None:  # every key is verified on the screen; a mismatch stops here
            toast("toast_not_available")
            _refresh_card(cfg, bot, session, mid, lang, chat=chat)
            return "ignored"
        toast("toast_key_sent", name=str(num))
        _refresh_card(cfg, bot, session, mid, lang, chat=chat)
        return "dialog-toggle"
    # action == "d"
    if any(o.typed_only and o.checked for o in d.options):
        toast("dlg_toast_typed")
        return "typed-required"
    at = _confirm_multi(session, pane, d)
    if at is None:
        toast("toast_not_available")
        _refresh_card(cfg, bot, session, mid, lang, chat=chat)
        return "ignored"
    toast("toast_key_sent", name="enter")
    chosen = [f"{o.num}. {o.label}" for o in at.options if o.checked and not o.confirm and o.num <= k]
    _after(cfg, bot, session, thread, ident, mid, _answered_text(lang, chosen or [t("dlg_none_picked", lang=lang)]))
    return "dialog-done"


# --- typed answers --------------------------------------------------------------------------------

def _fold(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def _resolve(text: str, rec: dict) -> List[int]:
    """Option numbers the typed text names: numbers ("2", "1, 3"), or one option's name (whole or the
    start of it, 3+ letters). Empty when nothing, or more than one option, matches by name."""
    raw = text.strip().rstrip(".!")
    options = rec.get("options") or []
    valid = {o["num"] for o in options}
    if _ANSWER_LIKE.match(raw):
        nums = [int(x) for x in re.findall(r"\d+", raw)]
        return nums if nums and all(n in valid for n in nums) else []
    folded = _fold(raw)
    if len(folded) < 2:
        return []
    exact = [o["num"] for o in options if _fold(o["label"]) == folded]
    if len(exact) == 1:
        return exact
    loose = [o["num"] for o in options if len(folded) >= 3 and _fold(o["label"]).startswith(folded)]
    return loose if len(loose) == 1 else []


UNKNOWN_KEYS = {"enter": "Enter", "esc": "Escape", "up": "Up", "down": "Down", "tab": "Tab", "space": "Space"}


def pending_for(session: str, m: dict, chat: Optional[int] = None) -> bool:
    """Cheap check (no terminal access): is this message possibly an answer to the topic's open dialog?
    `chat`: the group the message came from (None = the home chat); the card must be in that same topic."""
    rec = rec_get(session)
    text = m.get("text")
    return bool(rec and (rec.get("typed") or rec.get("text_mode")) and isinstance(text, str) and text.strip()
                and not m.get("_forwarded") and not text.lstrip().startswith("/")
                and rec.get("thread") == m.get("message_thread_id") and rec.get("chat") == chat)


def _kept_hint(session: str, rec: dict) -> None:
    rec_set(session, dict(rec_get(session) or rec, hint_at=time.time()))


def typed_answer(cfg: Config, bot, m: dict, session: str) -> Optional[str]:
    """The owner's typed message in the session's topic while a typed-answer dialog is open.
    The decision is made here (one screen read); the keys are sent in the session's terminal lane.
    None: not an answer, so the message goes on to Claude as ordinary text (after a short hint)."""
    from . import inbox as I
    chat = I._chat_arg(cfg, m)
    if not pending_for(session, m, chat):
        return None
    rec = rec_get(session)
    text = m["text"].strip()
    thread = m.get("message_thread_id")
    lang = _lang(cfg)
    if rec.get("text_mode") and time.time() - rec.get("text_at", 0) > TYPED_TTL:
        rec_set(session, dict(rec, text_mode=False))
        rec = rec_get(session)
    if not rec.get("typed") and not rec.get("text_mode"):
        return None
    pane, body, d, is_open = open_screen(session)
    in_text = bool(rec.get("text_mode"))  # selecting "Other" moves the selector: only the identity must hold
    if loose_ok := bool(rec.get("loose") and pane and I._dialog_open(body, session)):
        is_open = True
    if (not pane or not is_open or _identity_of(d, body) != rec.get("identity")
            or (not in_text and ((d is not None and rec.get("stamp") != d.stamp)
                                 or (d is None and not rec.get("unparsed"))))):
        rec_del(session)
        if _ANSWER_LIKE.match(text):
            _say(bot, cfg, thread, t("dlg_closed_nothing_sent", lang=lang), chat=chat)
            return "dialog-stale"
        return None
    if len(text) > MAX_ANSWER or is_secret(text) or any(ord(c) < 32 or ord(c) == 127 for c in text):
        I._refuse(bot, I._reply_chat(cfg, m), m, cfg)
        return "refused"
    mid, ident = rec.get("message_id"), rec.get("identity")

    def lane(action, name: str) -> str:
        if not I._terminal_background(session, action):
            _say(bot, cfg, thread, t("toast_busy", lang=lang), chat=chat)
            return "busy"
        return name

    if rec.get("text_mode"):  # after "Other": the next message is the answer, and Enter submits it
        def do_text():
            if not _literal(pane, text):
                _say(bot, cfg, thread, t("dlg_not_sent", lang=lang), chat=chat)
                return
            I.typed_mark(text)
            rec_set(session, dict(rec_get(session), text_mode=False))
            I.react(bot, I._reply_chat(cfg, m), m, "\U0001F44D", optional=True)
            _after(cfg, bot, session, thread, ident, None, "")
        return lane(do_text, "dialog-text")
    if rec.get("unparsed") or d is None:  # explicit key words or an option number only, never free text
        word = text.lower()
        key = UNKNOWN_KEYS.get(word)  # explicit key words only: no digits, no free text
        if key is None:
            _say(bot, cfg, thread, t("dlg_unknown_hint", lang=lang), chat=chat)
            _kept_hint(session, rec)
            return None
        def do_key():
            if not _keys(pane, key):
                _say(bot, cfg, thread, t("dlg_not_sent", lang=lang), chat=chat)
                return
            I.react(bot, I._reply_chat(cfg, m), m, "\U0001F44D", optional=True)
            _after(cfg, bot, session, thread, ident, mid, _answered_text(lang, [t("dlg_typed_answer", lang=lang)]))
        return lane(do_key, "dialog-typed")
    nums = _resolve(text, {"options": [{"num": o.num, "label": o.label} for o in d.options]})
    if not nums or (len(nums) > 1 and not d.multi):
        listing = "\n".join(f"{o.num}. {o.label}" for o in d.options)
        _say(bot, cfg, thread, t("dlg_no_match", lang=lang) + "\n" + listing + "\n" + t("dlg_kept", lang=lang), chat=chat)
        _kept_hint(session, rec)
        return None  # the message is not an answer: it goes to Claude, after the question
    if d.cut and d.perm:
        _say(bot, cfg, thread, t("dlg_cut", lang=lang), chat=chat)
        return "dialog-cut"
    if d.multi:
        picked = [next((o for o in d.options if o.num == n), None) for n in nums]
        if any(o is None or o.checked is None or o.other or o.confirm for o in picked):
            listing = "\n".join(f"{o.num}. {o.label}" for o in d.options if o.checked is not None and not o.other)
            _say(bot, cfg, thread, t("dlg_no_match", lang=lang) + "\n" + listing + "\n" + t("dlg_kept", lang=lang), chat=chat)
            _kept_hint(session, rec)
            return None

        def do_multi():
            want = set(nums)
            cur_d = d
            for _ in range(len(d.options) + 1):
                todo = next((o for o in cur_d.options if o.checked is not None and not o.other
                             and bool(o.checked) != (o.num in want)), None)
                if todo is None:
                    break
                cur_d = _toggle(session, pane, cur_d, todo.num)  # each key is verified on the screen
                if cur_d is None:
                    _say(bot, cfg, thread, t("dlg_not_sent", lang=lang), chat=chat)
                    return
            # exactly the named options are ticked (a pre-ticked one the owner did not name was unticked)
            if any(bool(o.checked) != (o.num in want) for o in cur_d.options if o.checked is not None and not o.other):
                _say(bot, cfg, thread, t("dlg_not_sent", lang=lang), chat=chat)
                return
            at = _confirm_multi(session, pane, cur_d)
            if at is None:
                _say(bot, cfg, thread, t("dlg_not_sent", lang=lang), chat=chat)
                return
            I.react(bot, I._reply_chat(cfg, m), m, "\U0001F44D", optional=True)
            chosen = [f"{o.num}. {o.label}" for o in at.options if o.checked and not o.other]  # as ticked on screen
            _after(cfg, bot, session, thread, ident, mid, _answered_text(lang, chosen or [t("dlg_none_picked", lang=lang)]))
        return lane(do_multi, "dialog-typed")
    n = nums[0]
    opt = next(o for o in d.options if o.num == n)

    def do_one():
        if not _answer(session, pane, d, n):
            _say(bot, cfg, thread, t("dlg_not_sent", lang=lang), chat=chat)
            return
        I.react(bot, I._reply_chat(cfg, m), m, "\U0001F44D", optional=True)
        if opt.other:
            rec_set(session, dict(rec_get(session), text_mode=True, text_at=time.time()))
            _say(bot, cfg, thread, t("dlg_text_prompt", lang=lang), chat=chat)
            return
        _after(cfg, bot, session, thread, ident, mid, _answered_text(lang, [f"{opt.num}. {opt.label}"]))
    return lane(do_one, "dialog-other" if opt.other else "dialog-typed")


# --- the watcher: a dialog stays visible until it is answered ----------------------------------

_first_seen: dict = {}
_watch_lock = threading.Lock()


def remind(cfg: Config, bot, topics, now: Optional[float] = None) -> int:
    """One pass over the sessions that work or wait: show a dialog nobody has been told about, re-show
    one that is still open every REMIND_EVERY seconds, and retire the card of one that was answered
    (in the terminal, say). Returns how many cards were sent."""
    from . import inbox as I
    now = time.time() if now is None else now
    sent = 0
    live = set()
    for session, entry in topics.all_places().items():
        thread = entry.get("thread") if isinstance(entry, dict) else None
        if not isinstance(thread, int):
            continue
        chat = None if entry.get("home", True) else entry["chat"]
        rec = rec_get(session)
        state = I.session_state(session).get("state")
        if not rec and state not in ("busy", "waiting"):
            continue
        try:
            pane, body, d, is_open = open_screen(session)
        except Exception:
            continue
        if not pane:
            continue
        if not is_open:
            if rec:
                _drop_keyboard(bot, cfg, rec)
                rec_del(session)
            _first_seen.pop(session, None)
            continue
        ident = _identity_of(d, body)
        live.add(session)
        seen = _first_seen.get(session)
        if not seen or seen[0] != ident:
            _first_seen[session] = seen = (ident, now)
        if rec.get("identity") == ident and (now - rec.get("sent_at", 0) < REMIND_EVERY or rec.get("text_mode")):
            continue
        if rec.get("identity") != ident and now - seen[1] < SEEN_GRACE:
            continue
        reminder = rec.get("identity") == ident
        try:
            if send_card(cfg, bot, session, thread, silent=False,
                         title_key="dlg_title_again" if reminder else "dlg_title", chat=chat):
                sent += 1
        except Exception as exc:
            _log(f"watcher failed: {type(exc).__name__}")
    for gone in set(_first_seen) - live:
        _first_seen.pop(gone, None)
    return sent


def held_notice(cfg: Config, bot, m: dict, session: str) -> bool:
    """A message arrives while a dialog is open: say so (not "between steps") and show the dialog again.
    True when a dialog was open."""
    from . import inbox as I
    try:
        _pane, body, d, is_open = open_screen(session)
    except Exception:
        return False
    if not is_open:
        return False
    thread = m.get("message_thread_id")
    chat = I._chat_arg(cfg, m)
    rec = rec_get(session)
    if time.time() - rec.get("hint_at", 0) > 15:  # the hint just sent already said it
        _say(bot, cfg, thread, t("dlg_held", lang=_lang(cfg)),
             reply_parameters={"message_id": m.get("message_id"), "allow_sending_without_reply": True}, chat=chat)
    if rec.get("identity") != _identity_of(d, body):
        send_card(cfg, bot, session, thread, chat=chat)
    return True
