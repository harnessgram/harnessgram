"""Claude Code hooks, including PreToolUse AskUserQuestion proof (hook-ask).

Handlers read the hook's JSON from stdin and may print JSON context on stdout.
The question-proof handler prints nothing and exits 0 even when recording fails:
a bridge problem must never break the Claude Code session.
"""
from __future__ import annotations

import contextlib
import hashlib
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from . import accounts, framing
from . import lang as L
from . import launch, scheduled
from .lazy import lazy_module
from .config import (Config, ConfigError, home_chat, inbox_dir, load, private_dir, safe_name, setting_on, state_dir,
                     write_private)
from .progress import append_progress, drop_progress


cards = lazy_module(".cards")
fmt = lazy_module(".fmt")
compact = lazy_module(".compact")
framing = lazy_module(".framing")
replay = lazy_module(".replay")
usage = lazy_module(".usage")


def default_inbox():
    from .inbox import default_inbox as make
    return make()


def spawn_delivered(*args, **kwargs):
    from .inbox import spawn_delivered as spawn_it
    return spawn_it(*args, **kwargs)


def translate_reply(*args, **kwargs):
    from .translate import translate_reply as translate
    return translate(*args, **kwargs)


def translate_text(*args, **kwargs):
    from .translate import translate_text as translate
    return translate(*args, **kwargs)

MIN_LETTERS = 40  # shorter replies are not judged for language

# The security contract (framing.py): a Telegram message is a chat message; only the owner's own typed yes
# approves, and only the one sensitive action the session last asked about by name (framing.OWNER_YES).
# Prompts that start with these are not the owner typing: bridge headers, and the carry-on prompt a reopened
# session starts with (revive.WAKE_TEXT). They never clear the nudge pause, are never mirrored to the topic
# as the owner's words and never name the session.
NOT_OWNER_PROMPT = ("<", "[Owner message", "[Owner messages", "[HarnessGram:", "[Harnessgram:",
                    framing.FORWARDED_HEADER, framing.TEAM_ONLY_HEADER,
                    framing.TEAM_HEADER.split("{name}", 1)[0], framing.PANEL_HEADER)
STOP_HEADER = "[Owner message via Telegram, written in this session's topic]"
STOP_CLOSING = "Treat it as the owner's chat message within the current authorized scope."
MIDTURN_HEADER = "[Owner message via Telegram, arrived mid-step]"
MIDTURN_CLOSING = "Take it into account now."
MIDTURN_STOP = ("[Owner via Telegram, mid-step: STOP. Make no further tool calls; "
                "briefly report what is done and what is left.]")


def _has_owner_messages(items: List[dict]) -> bool:
    return any(m.get("source") not in ("team", "mcp") and not m.get("forwarded") and not m.get("dropped")
               and (m.get("text") or "").strip() for m in items)


def _owner_delivery(header: str, items: List[dict], closing: str, session: str = "") -> str:
    text = framing.deliver(header, items, closing)
    if session and _has_owner_messages(items):
        try:
            from . import open_lines
            reminder = open_lines.reminder(session)
            if reminder:
                text += "\n\n" + reminder
        except Exception:
            pass  # a reminder must never lose an already-taken message
    return text


def delivery_text(items: List[dict], session: str = "") -> str:
    """What the Stop hook and the wake hook hand to the model (queued messages, owner or team)."""
    return _owner_delivery(STOP_HEADER, items, STOP_CLOSING, session)


# --- reading the transcript --------------------------------------------------

def _ends_with_text(lines: List[str]) -> bool:
    """True when the last assistant event after the last user/tool_result is a text block without tool_use."""
    # Never fall back to older text while the last JSON record is still being flushed.
    if lines:
        try:
            json.loads(lines[-1])
        except ValueError:
            return False
    for raw in reversed(lines[-200:]):
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type") not in ("assistant", "user"):
            continue  # api-request-blob and similar records carry system text
        msg = ev.get("message") or {}
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or ev.get("type")
        if role == "user":
            return False
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        kinds = {b.get("type") for b in content if isinstance(b, dict)}
        if "text" in kinds or "tool_use" in kinds:
            return "tool_use" not in kinds and any(
                isinstance(b, dict) and b.get("type") == "text" and
                isinstance(b.get("text"), str) and b["text"].strip() for b in content)
    return False


def read_final_transcript(data: dict, wait: float = 3.0, poll: float = 0.15) -> List[str]:
    """The Stop hook can fire before the turn's final text is flushed; wait up to wait s (poll 0.15 s)
    until the last assistant event after the last user/tool_result is a text block without tool_use."""
    path = data.get("transcript_path") or ""
    deadline = time.monotonic() + wait
    while True:
        try:
            with open(path, encoding="utf-8") as f:
                lines = this_turn_only(f.readlines(), data.get("_offset"))
        except OSError:
            return []
        if _ends_with_text(lines) or time.monotonic() >= deadline:
            if lines:
                try:
                    json.loads(lines[-1])
                except ValueError:
                    return []  # A partial tail may hide a new answer; retry on the next Stop.
            return lines
        time.sleep(poll)


def this_turn_only(lines: List[str], offset: Optional[int]) -> List[str]:
    """Keep a detached Stop worker on its original turn, including a late final flush.

    Offsets are bytes (transcripts can contain multibyte text). A genuine user
    prompt or a completed-turn record appended after Stop starts ends this view.
    Tool results and request blobs never establish a new turn.
    """
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return lines
    pos = 0
    for i, raw in enumerate(lines):
        if pos >= offset:
            try:
                ev = json.loads(raw)
            except ValueError:
                ev = {}
            if isinstance(ev, dict):
                if ev.get("type") == "system" and ev.get("subtype") in ("stop_hook_summary", "turn_duration"):
                    return lines[:i]
                if ev.get("type") == "user" and not ev.get("isMeta"):
                    msg = ev.get("message") or {}
                    content = msg.get("content") if isinstance(msg, dict) else None
                    items = content if isinstance(content, list) else [{"type": "text"}]
                    if any(isinstance(b, dict) and b.get("type") == "text" for b in items):
                        return lines[:i]
        pos += len(raw.encode("utf-8"))
    return lines


def _scan_blocks(lines: List[str]) -> Tuple[List[Tuple[str, str]], bool, bool]:
    """(blocks, whether a genuine user prompt was met, whether a block was keyed by its line number)."""
    blocks: List[Tuple[str, str]] = []
    prompt = numbered = False
    for n, raw in enumerate(lines):
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type") not in ("assistant", "user"):
            continue
        msg = ev.get("message") or {}
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or ev.get("type")
        content = msg.get("content")
        if role == "user":
            items = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            # A tool result is also a "user" event; only genuine prompt text starts a new reply.
            if any(isinstance(b, dict) and b.get("type") == "text" for b in items) and not ev.get("isMeta"):
                blocks = []
                prompt, numbered = True, False
        elif role == "assistant" and isinstance(content, list):
            for i, b in enumerate(content):
                if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip():
                    numbered = numbered or not ev.get("uuid")
                    blocks.append((f"{ev.get('uuid') or n}:{i}", b["text"]))
    return blocks, prompt, numbered


def read_blocks(lines: List[str]) -> List[Tuple[str, str]]:
    """(key, text) of the assistant text blocks written since the last real user prompt."""
    return _scan_blocks(lines)[0]


TAIL_BYTES = 256 * 1024  # what read_blocks_of reads first from the end of a transcript


def read_blocks_of(path: str) -> List[Tuple[str, str]]:
    """read_blocks of a transcript file's lines, without parsing the whole file when its end is enough.

    A long conversation is megabytes of JSON and this runs on every tool call, but the answer only depends on what
    follows the last genuine user prompt. The end of the file is read in growing windows (cut at a line break, so
    its lines are the file's own) until one holds such a prompt; the file is read whole when none does, and
    whenever a block has no id of its own (its key is then its line number in the file)."""
    size = os.path.getsize(path)
    window = TAIL_BYTES
    while window < size:
        with open(path, "rb") as f:
            f.seek(size - window)
            data = f.read()
        cut = data.find(b"\n")
        if cut >= 0:
            blocks, prompt, numbered = _scan_blocks(data[cut + 1:].decode("utf-8").splitlines())
            if prompt and not numbered:
                return blocks
        window *= 4
    return read_blocks(Path(path).read_text(encoding="utf-8").splitlines())


def last_reply(lines: List[str]) -> str:
    return "\n\n".join(t for _, t in read_blocks(lines))


_NOTIFICATION = "<task-notification>"
_STOP_FEEDBACK = "Stop hook feedback"
# Any of these anywhere in the turn (a user row, a queued command, a hook's added context or blocking error, in
# whatever shape Claude Code records it) means the owner wrote: the turn is mirrored.
_OWNER_MARKS = (STOP_HEADER, MIDTURN_HEADER, MIDTURN_STOP, "[Owner also wrote in this session's Telegram topic",
                "\U0001F4E9 Telegram")


def _row_text(event: dict) -> Optional[str]:
    """The prompt text of a user row or a queued command; None for every other row."""
    if event.get("type") == "user" and not event.get("isMeta"):
        msg = event.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            return "\n".join(t for t in texts if isinstance(t, str)).strip() if texts else None
        return None
    if event.get("type") == "attachment":
        att = event.get("attachment") or {}
        if isinstance(att, dict) and att.get("type") == "queued_command" and isinstance(att.get("prompt"), str):
            return att["prompt"].strip()
    return None


_CHATTER = re.compile(
    r"(?i)^\W*(?:ok(?:ay)?|done|got it|thanks?|noted|understood|on it|still (?:working|waiting|running)|"
    r"no (?:response|reply|action) (?:requested|needed)|nothing (?:new|to do|to report)(?: for you)?|"
    r"(?:the )?(?:monitor|watch|watcher)(?: is)? (?:re-?armed|still running|armed)|re-?armed(?: the (?:monitor|watch))?|"
    r"waiting(?: for [^.\n]{1,60})?|continuing|קיבלתי|בסדר|ממשיך|עדיין עובד|אין חדש)\W*$")


def chatter_reply(reply: str) -> bool:
    """A reply to a background event that is only chatter, recognised for certain: a known acknowledgement
    ("Monitor re-armed.", "No response requested.") with no summary line or a "nothing new" one. Anything else,
    however short ("The build failed; action required."), is a report and reaches the topic."""
    body, summary = fmt.split_summary(reply or "")
    if summary is not None and not _NO_NEWS.match(summary.strip()):
        return False
    return bool(body) and len(body) <= 160 and bool(_CHATTER.match(body)) or (not body and summary is not None)


def background_only_turn(lines: List[str], answer_to=None, session: str = "") -> bool:
    """True only when the turn was started by a background notification and nothing in it came from the
    owner. Errs toward mirroring: an owner reply waiting (`answer_to`), any owner mark in any row, or any
    prompt that is not a notification makes it an owner turn. A Stop-hook feedback row continues the turn
    it blocked (it never starts one). The bridge's own idle-lines nudge counts as a notification only when the
    whole prompt is its fixed line and the bridge delivered it to this `session` moments ago (idle_lines.py)."""
    if answer_to:
        return False
    rows = []
    for raw in lines:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if isinstance(event, dict):
            rows.append((raw, event))
    def prompt(event: dict) -> bool:
        text = _row_text(event)
        return (event.get("type") == "user" and bool(text)
                and not (text.startswith(_NOTIFICATION) and _STOP_FEEDBACK in text[:300]))

    last = next((i for i in range(len(rows) - 1, -1, -1) if prompt(rows[i][1])), None)
    if last is None:
        return False
    # The turn begins at the first prompt after the answer before it: prompts with no answer between are one turn.
    start = last
    for i in range(last - 1, -1, -1):
        if rows[i][1].get("type") == "assistant":
            break
        if prompt(rows[i][1]):
            start = i
    for raw, event in rows[start:]:
        # Only rows that carry what arrived in this turn: a "last-prompt" or title record repeats an older prompt.
        if event.get("type") in ("user", "attachment", "system") and any(mark in raw for mark in _OWNER_MARKS):
            return False
        text = _row_text(event)
        if text and not text.startswith(_NOTIFICATION) and not _bridge_prompt(text, session):
            return False
    return True


def _bridge_prompt(text: str, session: str) -> bool:
    """The bridge's own idle-lines nudge, exactly, just delivered to this session: a background event."""
    try:
        from .idle_lines import is_bridge_prompt
        return is_bridge_prompt(text, session)
    except Exception:
        return False


def session_title(path: Optional[str], session: str = "") -> str:
    """Use the app's latest custom title, falling back to the saved task name."""
    from .topics import session_name
    saved = session_name(session) if session else ""
    title = ""
    try:
        with open(path or "", encoding="utf-8") as f:
            for line in f:
                if '"custom-title"' in line:
                    title = json.loads(line).get("customTitle") or title
    except Exception:
        pass
    return title or saved


def is_not_target(text: str, lang: str) -> bool:
    """Mostly Latin letters although the target language has its own script (Hebrew, Arabic, ...)."""
    if not L.script_class(lang):
        return False  # Latin-script target: cannot be told apart from English by script
    target, latin = L.letter_counts(text, lang)
    return target + latin >= MIN_LETTERS and latin > target


# --- remembering what was already mirrored -----------------------------------

def _ledger(session: str) -> Path:
    return state_dir() / "mirrored" / (re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_")


def _seen_list(session: str) -> List[str]:
    try:
        data = json.loads(_ledger(session).read_text(encoding="utf-8"))
        return [k for k in data if isinstance(k, str)]
    except (OSError, ValueError):
        return []


def _seen(session: str) -> set:
    return set(_seen_list(session))


LEDGER_KEEP = 3000


def _mark(session: str, keys: List[str]) -> None:
    """Remember which assistant text blocks were already shown/mirrored. A turn continued by a
    background notification, or after a block-and-continue, still contains the earlier text in the
    transcript; without this ledger it would be sent again. The list keeps transcript order and
    drops the OLDEST entries first."""
    try:
        old = _seen_list(session)
        merged = old + [k for k in keys if k not in set(old)]
        write_private(_ledger(session), json.dumps(merged[-LEDGER_KEEP:]))
    except OSError:
        pass


# --- detached children -------------------------------------------------------

def spawn(args: List[str], text: Optional[str] = None) -> None:
    """Start `ctb <args>` detached, so it survives this hook being cancelled. Text goes through
    a private temp file (a pipe could be cut off half-way when the hook dies). The child runs in
    the private state directory, never in the project, and is started by launch.argv(). Values are
    passed as --name=value so a value that starts with "-" cannot be read as an option."""
    cmd = launch.argv() + list(args)
    path = None
    if text is not None:
        tmp = private_dir(state_dir() / "tmp")
        path = tmp / f"reply-{os.getpid()}-{time.time_ns()}.txt"
        write_private(path, text)
        cmd += [f"--text-file={path}", "--delete-text-file"]
    try:
        subprocess.Popen(cmd, cwd=str(private_dir(state_dir())), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        if path is not None:
            path.unlink(missing_ok=True)
        raise


def mirror_files(data: dict, lines: List[str]) -> None:
    """Every file Claude sent with the SendUserFile tool this turn also goes to Telegram,
    once (ledger keyed by path and mtime), mechanically and without model tokens."""
    try:
        files = []
        for raw in lines[-400:]:
            if '"SendUserFile"' not in raw:
                continue
            event = json.loads(raw)
            if not isinstance(event, dict) or event.get("type") != "assistant":
                continue
            for b in (event.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "SendUserFile":
                    inp = b.get("input") or {}
                    files += [(os.path.expanduser(x), inp.get("caption") or "") for x in inp.get("files") or []]
        if not files:
            return
        ledger = state_dir() / "files-sent.txt"
        sent = set(ledger.read_text(encoding="utf-8").split("\n")) if ledger.exists() else set()
        title = session_title(data.get("transcript_path"), data.get("session_id") or "") or "Claude"
        session = data.get("session_id") or title
        for path, caption in files:
            path = os.path.abspath(path)
            key = f"{path}|{int(os.path.getmtime(path))}" if os.path.exists(path) else ""
            if not key or key in sent:
                continue
            private_dir(ledger.parent)
            with open(ledger, "a", encoding="utf-8") as fh:
                fh.write(key + "\n")
            os.chmod(ledger, 0o600)
            spawn(["mirror", f"--title={title}", f"--session={session}", f"--file={path}", f"--caption={caption}"])
    except Exception:
        pass


TAKE_WAIT = 2.0  # seconds a hook may wait for the queue's lock; then the messages stay queued (sweeper.py)


def hook_deadline() -> float:
    """One time.monotonic() deadline for all of a hook's waits on the session's lock (review of #141, P2-6):
    a stalled lock holder costs a hook at most TAKE_WAIT seconds, never Claude Code's own hook time-out."""
    return time.monotonic() + TAKE_WAIT


def delivery_lease(data: Optional[dict], via: str) -> dict:
    """A fresh in-flight record for one hook output: its nonce goes into the framing, and the sweeper
    (or the detached confirm worker) finds it in the transcript before the messages count as delivered."""
    transcript = (data or {}).get("transcript_path") or ""
    offset, where = 0, {}
    try:
        if transcript:
            with open(transcript, "rb") as fh:  # the take point, and what lets a later look check it (scan_valid)
                st = os.fstat(fh.fileno())
                offset = st.st_size
                from .sweeper import mark_at
                where = {"ino": st.st_ino, "mark": mark_at(fh, offset)}
    except OSError:
        offset = 0
    lease = {"nonce": secrets.token_hex(8), "transcript": transcript, "offset": offset, "via": via,
             "pid": os.getpid(), **where}  # pid: whose output carries the nonce (never put back before it ends)
    agent = re.sub(r"[^A-Za-z0-9_-]", "", str((data or {}).get("agent_id") or ""))
    if agent and transcript.endswith(".jsonl"):  # a background agent's step: proven in its own transcript
        lease["agent_id"] = agent
        try:
            lease["agent_offset"] = os.path.getsize(f"{transcript[:-6]}/subagents/agent-{agent}.jsonl")
        except OSError:
            lease["agent_offset"] = 0
    return lease


def _NOTHING(item: dict) -> bool:
    return False


def telegram_messages(session: str, midturn: bool = False, only=None, data: Optional[dict] = None,
                      via: str = "", until: Optional[float] = None) -> List[dict]:
    """Take the session's queued Telegram messages. Between steps (midturn) the ones the owner chose
    to hold until the turn's end stay queued. With `only`, the others stay queued too.
    Taken under a lease: they stay in the session's in-flight file until the transcript proves the model
    got them, and come back to the queue if Claude Code drops this hook's output.
    First an earlier delivery the model missed is put back, so it is taken now and ahead of newer messages;
    while an earlier one is still undecided, the newer ones wait (sweeper.before_take). A background agent's
    step (via "agent") takes only an owner stop word, which never waits."""
    if not session:
        return []
    from . import credit_mode
    if via != "agent" and credit_mode.blocked(session):
        return []  # preserve queued owner messages for subscription/reset
    until = hook_deadline() if until is None else until
    try:
        inbox = default_inbox()
        if via != "agent":
            from .sweeper import before_take
            try:
                if before_take(inbox, session, until=until):
                    only = _NOTHING  # newer messages wait; the take still drops copies a lease already owns
            except Exception:
                pass  # a failed look never blocks delivery: the take below works as before
        items = inbox.take(session, midturn=midturn, only=only, lease=delivery_lease(data, via), until=until)
    except Exception:
        return []
    spawn_delivered(items)  # 👌 once the transcript shows the session has them (a detached confirm worker)
    return items


# --- Stop --------------------------------------------------------------------

_UNSET = object()


_NO_NEWS = re.compile(r"(?i)^\W*(?:nothing new(?: for you)?|no (?:news|change|update)s?(?: for you)?|still (?:waiting|running)|"
                      r"אין (?:חדש|שינוי|חדשות)(?: בשבילך| עבורך)?)\W*$")


def live_card(session: str, reply: str, cfg: Config) -> None:
    """Use only assistant reply text already selected for the mirror; no transcript/tool-result reads.
    A cached translation is cheap; otherwise keep the original. Card contention never blocks a turn."""
    try:
        body = fmt.FENCE.sub("\n", reply)
        _, summary = fmt.split_summary(body)
        if not summary:
            summary = re.split(r"(?<=[.!?。！？])\s+|\n", body.strip(), maxsplit=1)[0]
        if not summary or _NO_NEWS.match(summary.strip()):
            return  # a no-news line keeps the card's last real outcome
        if cfg.translate and summary:
            from .translate import cached
            summary = cached(summary, cfg) or summary
        cards.update_live(session, summary, until=time.monotonic() + CARD_LOCK_WAIT)
    except (cards.CardError, OSError):
        pass


def stop_output(data: dict, lines: List[str], cfg: Config, answer_to=_UNSET) -> dict:
    out: dict = {}
    session = data.get("session_id") or ""
    blocks = [b for b in read_blocks(lines) if b[0] not in _seen(session)]
    reply = "\n\n".join(t for _, t in blocks).strip()
    # Snapshot before detached work or taking the next turn's Telegram messages.
    if answer_to is _UNSET:
        answer_to = default_inbox().consume_answer_to(session, include_text=True) if session and reply else None
    if cfg.ready:
        mirror_files(data, lines)
    if reply:
        _mark(session, [k for k, _ in blocks])
        title = session_title(data.get("transcript_path"), session) or os.path.basename(data.get("cwd") or "") or "Claude"
        has_summary = fmt.split_summary(reply)[1] is not None
        wants = cfg.translate and (has_summary or is_not_target(reply, cfg.target_language))
        if cfg.mirror and cfg.ready and not (cfg.quiet_background_turns and chatter_reply(reply)
                                              and background_only_turn(lines, answer_to, session)):
            live_card(session, reply, cfg)  # only a reply the topic shows reaches the card
            # Detached first: the app cancels a running Stop hook when the next prompt is sent.
            args = ["mirror", f"--title={title}", f"--session={session or title}"] + (
                ["--translate"] if wants else ["--no-translate"])
            prompt_path = None
            if answer_to:
                args += [f"--reply-chat={answer_to['chat']}", f"--reply-to={answer_to['id']}"]
                if answer_to.get("text"):
                    tmp = private_dir(state_dir() / "tmp")
                    prompt_path = tmp / f"prompt-{os.getpid()}-{time.time_ns()}.txt"
                    write_private(prompt_path, answer_to["text"])
                    args += [f"--reply-text-file={prompt_path}", "--delete-reply-text-file"]
                if answer_to.get("source"):
                    args += [f"--reply-source={answer_to['source']}"]
            if cfg.context_footer and data.get("transcript_path"):
                args.append(f"--transcript={data['transcript_path']}")
            try:
                spawn(args, text=reply)
            except Exception:
                if prompt_path:
                    prompt_path.unlink(missing_ok=True)
        shown = False
        if wants:
            try:
                translated = translate_reply(fmt.join_summary(reply), lambda t: translate_text(t, cfg, fast=True),
                                             cfg.target_language)
                out["systemMessage"] = (L.RLM if L.is_rtl(cfg.target_language) else "") + translated
                shown = True
            except Exception:
                pass
        if cfg.enforce_language and not shown and not data.get("stop_hook_active") \
                and is_not_target(reply, cfg.target_language):
            out.update(decision="block", reason=L.strings("en")["rewrite"].format(
                target=L.lang_name(cfg.target_language)))
    # Messages written in this session's Telegram topic continue the session now.
    msgs = telegram_messages(session, data=data, via="stop") if cfg.ready else []
    if msgs:
        out.update(decision="block", reason=delivery_text(msgs, session if not data.get("agent_id") else ""))
    try:
        drop_progress(session, cfg)  # the step ended: remove the live progress message
    except Exception:
        pass
    from . import credit_mode
    if credit_mode.blocked(session):
        # Stop's decision=block asks Claude to keep working; at the budget gate
        # allow this turn to end instead, without feeding another turn.
        out.pop("decision", None)
        out.pop("reason", None)
        out["systemMessage"] = credit_mode.stop_line()
    return out


def _retire_permissions(data: dict) -> None:
    try:
        from . import permission_request
        permission_request.supersede(data.get("session_id") or "", data.get("agent_id"))
    except Exception:
        pass  # permission tracking must never interrupt the session's normal hooks


def hook_stop(stdin_text: str) -> int:
    try:
        data = json.loads(stdin_text)
        if not isinstance(data, dict):
            return 0
        _clear_ask(data.get("session_id") or "")  # the turn ended: no question is pending any more
        _retire_permissions(data)
        _direct(data)
        cfg = load_config()
        session = data.get("session_id") or ""
        data["_at"] = time.time()  # messages taken after this belong to the next turn (replay.py)
        try:
            data["_offset"] = os.path.getsize(data.get("transcript_path") or "")
        except OSError:
            pass
        # Freeze reply routing before dequeuing the next Telegram turn. The slow
        # worker receives private-file input and survives the app's 5-second cap.
        deadline = hook_deadline()
        try:
            data["_answer_to"] = (default_inbox().snapshot_answer_to(session, until=deadline)
                                  if session and cfg.ready else None)
        except OSError:
            data["_answer_to"] = None  # the lock is stuck: the reply goes to the topic, not under a message
        try:  # this turn's radio mark, before the take below writes the next turn's (review of #153)
            from . import radio_talk
            data["_radio_mark"] = radio_talk.take_mark(session) if session else {}
        except Exception:
            data["_radio_mark"] = None
        out = {}
        if cfg.ready:
            try:
                spawn(["finish-stop"], text=json.dumps(data, ensure_ascii=False))
            except Exception:
                from .mirror import log
                log("stop worker launch failed")
                out["systemMessage"] = "Telegram delivery could not start; this reply remains available in the app."
        if cfg.enforce_language and not data.get("stop_hook_active"):
            # Local, bounded display check only: no translation or network in Stop.
            try:
                with open(data.get("transcript_path") or "", "rb") as f:
                    end = min(data.get("_offset", 0), os.fstat(f.fileno()).st_size)
                    start = max(0, end - 512 * 1024)
                    f.seek(start)
                    tail = f.read(end - start)
                if start:
                    tail = tail.partition(b"\n")[2]
                reply = last_reply(tail.decode("utf-8", errors="replace").splitlines(True))
                if is_not_target(reply, cfg.target_language):
                    out.update(decision="block", reason=L.strings("en")["rewrite"].format(
                        target=L.lang_name(cfg.target_language)))
            except OSError:
                pass
        msgs = telegram_messages(session, data=data, via="stop", until=deadline) if cfg.ready else []
        if msgs:
            out.update(decision="block", reason=delivery_text(msgs, session if not data.get("agent_id") else ""))
        from . import credit_mode
        if credit_mode.blocked(session):
            out = {"systemMessage": credit_mode.stop_line()}
    except Exception:
        return 0
    if out:
        print(json.dumps(out, ensure_ascii=False))
    return 0


def _newer_turn(transcript: str, offset: int) -> bool:
    """A real user prompt was written to the transcript past `offset`: a new turn has started."""
    try:
        with open(transcript, encoding="utf-8") as f:
            current = f.readlines()
        pos = 0
        for raw in current:
            if pos >= offset:
                event = json.loads(raw)
                if event.get("type") == "user" and not event.get("isMeta"):
                    content = (event.get("message") or {}).get("content")
                    if not isinstance(content, list) or any(
                            isinstance(b, dict) and b.get("type") == "text" for b in content):
                        return True
            pos += len(raw.encode("utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return False


WORKERS_LOCK = "stop-workers.lock"


class _worker_gate:
    """Shared lock held while a Stop worker runs; `ctb wait-idle` (and install) waits on it."""
    def __enter__(self):
        self.f = None
        try:
            self.f = open(private_dir(state_dir()) / WORKERS_LOCK, "a", encoding="utf-8")
            fcntl.flock(self.f, fcntl.LOCK_SH)
        except OSError:
            self.f = None
        return self

    def __exit__(self, *exc):
        if self.f is not None:
            self.f.close()  # closing releases the lock
        return False


def wait_idle(timeout: float = 30.0, poll: float = 0.2) -> bool:
    """Wait until no Stop worker holds the gate, up to `timeout` s. True when idle."""
    try:
        f = open(private_dir(state_dir()) / WORKERS_LOCK, "a", encoding="utf-8")
    except OSError:
        return True
    with f:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(poll)


# Errors a worker sees while `uv tool install --force` is replacing the package under it.
MID_INSTALL_ERRORS = (ImportError, AttributeError, SyntaxError)
RETRY_DELAY = 4.0


def _safe_exc_line(exc: BaseException) -> str:
    """Exception type plus a short, redacted one-line message (never reply text or tokens)."""
    msg = " ".join(str(exc).split())[:120]
    msg = re.sub(r"\d{5,}:[A-Za-z0-9_-]{20,}", "<token>", msg)
    msg = re.sub(r"(?i)(bot|token|key|secret)[=/:\s]+\S+", r"\1=<redacted>", msg)
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _log_safe(line: str) -> None:
    try:
        from .mirror import log
        log(line)
    except Exception:
        pass


def _already_delivered(data: dict) -> bool:
    """True only when the delivery ledger shows every reply block of this turn was already sent."""
    try:
        session = data.get("session_id") or ""
        blocks = read_blocks(read_final_transcript(data, wait=0.0))
        seen = _seen(session)
        return bool(blocks) and all(k in seen for k, _ in blocks)
    except Exception:
        return False


# --- per-turn delivery claims ---------------------------------------------------
# A claim is written under the session's stop lock BEFORE a reply's first send or enqueue. It is the one
# rule against duplicates: a turn with a claim belongs to its claimant forever (only the claimant's own
# install retry, holding its token, may continue, and only while nothing was accepted). The heartbeat's
# missed-reply sweep redelivers only a turn that has no claim at all (missed_replies.py).

CLAIM_KEEP = 6 * 3600.0  # claims older than this are pruned (the sweep looks back 3 hours)


def _safe(session: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_"


def stop_lock_path(session: str) -> Path:
    return private_dir(state_dir() / "stop-locks") / _safe(session)


@contextlib.contextmanager
def stop_lock(session: str, blocking: bool = True):
    """The session's Stop delivery lock (serializes workers, the ledger and claims). Yields False when
    non-blocking and another holder has it."""
    path = stop_lock_path(session)
    with open(path, "a", encoding="utf-8") as lock:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True


def turn_id(lines: List[str]) -> str:
    """A turn's identity: the key of its last reply block ("" without one)."""
    blocks = read_blocks(lines)
    return blocks[-1][0] if blocks else ""


def _claim_path(session: str, turn: str) -> Path:
    return private_dir(state_dir() / "stop-claims" / _safe(session)) / (
        hashlib.sha1(turn.encode()).hexdigest() + ".json")


def read_claim(session: str, turn: str) -> Optional[dict]:
    try:
        data = json.loads(_claim_path(session, turn).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"token": ""}
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"token": ""}  # unreadable: someone claimed it; hands off


def update_claim(session: str, turn: str, **fields) -> None:
    """Callers hold the session's stop lock."""
    try:
        claim = read_claim(session, turn)
        if claim is not None:
            write_private(_claim_path(session, turn), json.dumps({**claim, **fields}))
    except OSError:
        pass


def drop_claim(session: str, turn: str) -> None:
    """Callers hold the session's stop lock: forget a claim whose sender never started (nothing was sent)."""
    try:
        _claim_path(session, turn).unlink()
    except OSError:
        pass


def take_claim(session: str, turn: str, token: Optional[str] = None, now: Optional[float] = None) -> Optional[str]:
    """Under the session's stop lock: the token to send this turn with, or None (hands off). A turn without
    a claim is claimed now; the claimant's own retry continues only while nothing was accepted."""
    now = time.time() if now is None else now
    claim = read_claim(session, turn)
    if claim is None:
        token = secrets.token_hex(8)
        folder = _claim_path(session, turn).parent
        for old in folder.glob("*.json"):
            try:
                if now - old.stat().st_mtime > CLAIM_KEEP:
                    old.unlink()
            except OSError:
                pass
        write_private(_claim_path(session, turn), json.dumps(
            {"token": token, "at": now, "accepted": False, "done": False}))
        return token
    if token and claim.get("token") == token and not claim.get("accepted") and not claim.get("done"):
        update_claim(session, turn, at=now)
        return token
    return None


def _claim_of(data: dict) -> Tuple[str, Optional[dict]]:
    try:
        session = data.get("session_id") or ""
        turn = turn_id(read_final_transcript(data, wait=0.0))
        return turn, (read_claim(session, turn) if turn else None)
    except Exception:
        return "", None


def _settle_delivered(data: dict) -> None:
    """The reply is already out: still end the step (progress card gone, topic done)."""
    try:
        from .topics import set_state
        cfg = load_config()
        session = data.get("session_id") or ""
        if not _newer_turn(data.get("transcript_path") or "", data.get("_offset", 0)):
            drop_progress(session, cfg)
            set_state(cfg, session, "done")
    except Exception:
        _log_safe("stop settle failed")


def finish_stop(data: dict) -> int:
    """Deliver one Stop turn. A failure from a package being reinstalled mid-turn is retried once in a
    fresh process; the owner is told only if the reply truly was not delivered."""
    attempt = int(data.get("_attempt") or 0)
    if attempt:
        time.sleep(RETRY_DELAY)
    try:
        return _finish_stop(data)
    except Exception as exc:
        _log_safe(f"stop delivery failed ({_safe_exc_line(exc)}) attempt={attempt + 1}")
        if _already_delivered(data):
            _log_safe("stop delivery: reply already delivered; no notice")
            _settle_delivered(data)
            return 0
        turn, claim = _claim_of(data)
        if claim is not None and (claim.get("accepted") or claim.get("token") != data.get("_claim")):
            # A part may already be out (or another sender owns the turn): a retry could deliver twice.
            # The claim stays open, so the sweep posts one "may be incomplete" notice later.
            _log_safe("stop delivery: send had started; not retrying")
            return 1
        if isinstance(exc, MID_INSTALL_ERRORS) and not attempt:
            try:
                spawn(["finish-stop"], text=json.dumps({**data, "_attempt": 1}, ensure_ascii=False))
                return 0
            except Exception:
                _log_safe("stop delivery retry launch failed")
        try:
            from . import mirror
            cfg = load_config()
            mirror.alert(cfg, data.get("session_id") or "",
                         "Reply delivery failed; the reply is available in the app.", exc)
        except Exception:
            _log_safe("stop delivery alert failed")
        if claim is not None:
            try:
                with stop_lock(data.get("session_id") or ""):
                    update_claim(data.get("session_id") or "", turn, done=True)  # the owner was told
            except OSError:
                pass
        return 1


def _claimed(data: dict, session: str, lines: List[str]) -> bool:
    """Under the stop lock: take (or continue, with this worker's own token) the claim on this turn."""
    turn = turn_id(lines)
    token = take_claim(session, turn, data.get("_claim")) if turn else None
    if token:
        data["_claim"] = token  # an install retry of this worker carries it (finish_stop)
    return token is not None


def _finish_stop(data: dict) -> int:
    """One delivery attempt; raises on failure."""
    from . import mirror
    from .progress import run_send_progress
    from .topics import set_state
    cfg = load_config()
    session = data.get("session_id") or ""
    if not cfg.ready:
        return 0
    # A session can stop again before an earlier worker has finished translating.
    # Serialize delivery and the dedupe ledger across those workers.
    spoken = None
    with _worker_gate(), stop_lock(session):
        lines = read_final_transcript(data, wait=8.0)
        run_send_progress(cfg, session, fast=True)
        blocks = read_blocks(lines)
        unseen = [(k, text) for k, text in blocks if k not in _seen(session)]
        reply = "\n\n".join(text for _, text in unseen).strip()
        # Before the reply is mirrored (which may start an account move): did the turn end on the
        # plan-limit text, or with a real answer? A move replays only the messages that got the former.
        last_text = (data.get("last_assistant_message") or "").strip() or (unseen[-1][1] if unseen else "")
        limit_turn = bool(accounts.QUOTA_HIT.search(last_text))
        replay.turn_ended(session, last_text, data.get("_at"), data.get("transcript_path"))
        mirror_files(data, lines)
        compact_checked = False
        # A turn that ended on the plan-limit text is never quiet: its offer to move or continue follows the reply.
        quiet = (cfg.quiet_background_turns and not limit_turn and chatter_reply(reply)
                 and background_only_turn(lines, data.get("_answer_to"), session))
        if quiet and reply and cfg.mirror:  # a reply to a background event only: kept out of the topic
            _mark(session, [k for k, _ in unseen])
            if data.get("transcript_path"):  # a monitor-heavy session still compacts past the threshold
                from .compact import auto_compact_after_reply
                auto_compact_after_reply(session, data["transcript_path"])
                compact_checked = True
        elif reply and cfg.mirror and not _claimed(data, session, lines):
            _log_safe("stop delivery: another sender owns this turn; not sent again")
        elif reply and cfg.mirror:
            turn = turn_id(lines)
            wants = cfg.translate and (fmt.split_summary(reply)[1] is not None or
                                      is_not_target(reply, cfg.target_language))
            title = session_title(data.get("transcript_path"), session) or os.path.basename(data.get("cwd") or "") or "Claude"
            live_card(session, reply, cfg)  # only a reply the topic shows reaches the card
            # The claim (taken above, before any send) records the first accepted or queued part: from then
            # on even this worker's own install retry stays off (an import once failed before any send, and a
            # "started" mark written up front had skipped the retry).
            code = mirror.run_reply(cfg, title, session or title, reply, wants,
                                    data.get("transcript_path") or "" if cfg.context_footer else "",
                                    answer_to=data.get("_answer_to"),
                                    on_accept=lambda: update_claim(session, turn, accepted=True))
            update_claim(session, turn, done=True)  # delivered, queued, or the owner was alerted
            if code:
                mirror.alert(cfg, session, "Reply delivery failed; the reply is available in the app.", mirror.last_reply_failure())
            else:
                # run_reply succeeds only after every part was delivered or durably queued.
                _mark(session, [k for k, _ in unseen])
                default_inbox().ack_answer_to(session, data.get("_answer_to"))
                if not scheduled.suppressed(session, "radio", reply):
                    spoken = (title, reply)  # said on the radio after the lock (radio_talk.py)
                if data.get("transcript_path"):  # past the threshold: compact once idle
                    from .compact import auto_compact_after_reply
                    auto_compact_after_reply(session, data["transcript_path"])
                    compact_checked = True
        elif not blocks and cfg.mirror and not quiet:
            mirror.alert(cfg, session, "The turn ended, but no reply was found. Please check the app.")
        if not compact_checked and data.get("transcript_path"):
            # The agent's own request (`ctb compact`) does not depend on the reply reaching Telegram:
            # mirroring off, a failed send or a turn without words must not leave it waiting.
            from . import compact as _compact
            if _compact.peek_ask(session) is not None:
                _compact.auto_compact_after_reply(session, data["transcript_path"])
        # A later prompt must retain its working state; stale workers cannot
        # rename its topic back to done.
        newer_turn = _newer_turn(data.get("transcript_path") or "", data.get("_offset", 0))
        # A turn that ended on the limit text is over, whatever waits in the queue: leaving the topic
        # busy raised a stall notice ten minutes later.
        if not newer_turn and (limit_turn or not default_inbox().pending(session)):
            drop_progress(session, cfg)
            set_state(cfg, session, "done")
    if spoken:  # the radio's short spoken version (a Gemini call): never while the next turn's worker waits
        try:
            from . import radio_talk
            radio_talk.on_reply(cfg, session, *spoken, mark=data.get("_radio_mark"))
        except Exception as exc:
            _log_safe(f"radio talk failed ({_safe_exc_line(exc)})")
    return 0


def hook_stop_failure(stdin_text: str) -> int:
    """Claude Code fires StopFailure (not Stop) when a turn ends on an API/usage-limit error.
    Input fields: error, error_details, last_assistant_message, session_id, transcript_path.
    Triggers the account limit offer/fallback with 300 s dedupe without renaming topics."""
    try:
        data = json.loads(stdin_text or "{}")
        if not isinstance(data, dict):
            return 0
        session = data.get("session_id") or ""
        if not session:
            return 0
        _retire_permissions(data)
        transcript = data.get("transcript_path") or ""
        try:
            offset = os.path.getsize(transcript)
        except OSError:
            offset = None
        text = (data.get("last_assistant_message") or "").strip()
        error_text = str(data.get("error") or "").strip()
        api_error = bool(error_text == "usage_limit_reached" or accounts.QUOTA_HIT.search(error_text))
        if not text or accounts.QUOTA_HIT.search(error_text):
            text = error_text
        cfg = load_config()
        if not cfg.ready:
            return 0
        if accounts.QUOTA_HIT.search(text) and (api_error or usage.limit_of(session)):
            if not data.get("agent_id"):
                replay.turn_ended(session, text, None, data.get("transcript_path"))  # first: the offer below may start the account move
            from .mirror import limit_offer
            from .telegram import Bot
            limit_offer(cfg, Bot.from_config(cfg), session, text, api_error=api_error)
        # A background agent's failure is not the main turn's end; a prompt that arrived while the
        # limit offer ran started a new turn that must keep its working state and progress card.
        if not data.get("agent_id") and not (offset is not None and _newer_turn(transcript, offset)):
            _idle_after_failure(cfg, session)
    except Exception:
        pass
    return 0


def _idle_after_failure(cfg: Config, session: str) -> None:
    """No Stop follows a failed turn, so the session stayed "busy" and queued messages waited for a
    PostToolUse that never came (a session sat idle 25 minutes after a 5-hour limit)."""
    from .inbox import _nudge_without_waiter
    from .topics import set_state
    drop_progress(session, cfg)
    set_state(cfg, session, "done")
    inbox = default_inbox()
    if inbox.pending(session):
        _nudge_without_waiter(inbox, session, settle=2.0)  # holds itself while the limit is active


# --- UserPromptSubmit --------------------------------------------------------

TOOLS_HINT = ('Pin board: `ctb board add "<title>" [<url>]` pins a link, `ctb board check "<text>"` adds a checklist '
              'item and `ctb board remind "<text>" [--due "<text>"]` posts something waiting on the owner to this '
              "topic's board (`ctb board done <n>` ticks or clears one). Use it sparingly, only for things the owner "
              "will want to open or act on again. For a yes/no question the owner can tap in the control panel: "
              '`ctb decide ask "<question>" [--detail "<text>"]` (add `--routine` only when a yes or no approves '
              'nothing sensitive, so the owner may also answer it from a phone widget). Live cards in the control panel: '
              '`ctb card progress "<title>" --step "<name>" ... --at <done> --percent <n>` (update it with '
              '`--id <id>`), `ctb card choice "<question>" --option "<a>" --option "<b>"` (when the options are visual, designs, screenshots or '
              'pictures, attach one `--image <png|jpg|webp path>` per option, in option order, so the owner sees them '
              'on the card instead of choosing blind; a session that cannot attach files sends the pictures as one '
              'message right before the card and names each option in its caption), `ctb card ask '
              '"<question>"`, `ctb card link "<title>" <https-url>`, `ctb card numbers "<title>" --figure '
              '"<label>=<value>"`; `ctb card close <id>` removes one. Close a question when the owner answers it in the topic. An answer in the control panel is the owner\'s '
              'own word and approves exactly what its question names, so a question about a sensitive action must '
              'name that one action precisely.')
COMPACT_HINT = ('Context: at a natural break, when the old details are no longer '
                'needed, `ctb compact --note "<what must be kept>"` asks the bridge to compact this session once '
                'the turn ends; save anything important (ledger, issue, file) first.')
BOARD_HINT = TOOLS_HINT + " " + COMPACT_HINT


def _addon_store() -> dict:
    """The add-on switch file, read once per hook call and handed to each _addon_on of that call."""
    try:
        from . import addons
        return addons.read_store()
    except Exception:
        return {}


def _addon_on(cfg, key: str, default: bool = True, store: Optional[dict] = None) -> bool:
    """One add-on switch (addons.py); its default when the registry cannot be read or even imported."""
    try:
        from . import addons
        return addons.enabled(cfg, key, store=store)
    except Exception:
        return default


def board_hint(n: int, cfg: Config, store: Optional[dict] = None) -> Optional[str]:
    """The board reminder, on a session's first prompt and then with every topic check; only where a board
    can exist (a topic in a forum group)."""
    from .pins import SUPERGROUP_PREFIX
    if (n == 1 or n % 8 == 0) and str(home_chat(cfg)).startswith(SUPERGROUP_PREFIX):
        from .bank import hint as bank_hint
        saved = bank_hint()
        parts = []
        if _addon_on(cfg, "cockpit_hint", store=store):
            parts.append(TOOLS_HINT)
        if _addon_on(cfg, "compact_helper", store=store):
            parts.append(COMPACT_HINT)
        if saved:
            parts.append(saved)
        return " ".join(parts) or None
    return None


def topic_check_hint(session: str, cfg: Config, store: Optional[dict] = None) -> Optional[str]:
    """Every 8th prompt in a session, remind the model to check if the topic name still fits; the pin
    board reminder rides along (and also opens the session)."""
    if not session:
        return None
    check_dir = private_dir(state_dir() / "topic-check")
    safe_session = re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_"
    count_file = check_dir / safe_session
    try:
        n = int(count_file.read_text(encoding="utf-8").strip() or "0") + 1
    except Exception:
        n = 1
    write_private(count_file, str(n))
    board = board_hint(n, cfg, store)
    if n % 8 != 0 or getattr(cfg, "auto_topic_names", False) or not getattr(cfg, "topic_check_reminders", False):
        return board  # with auto-naming the bridge keeps the name current; a session rename would lock it
    from .topics import TopicMap, session_name, session_override_name
    topics = TopicMap(state_dir() / "topics.json", None, cfg.owner_chat_id).all()
    name = session_override_name(session) or (topics.get(session) or {}).get("name") or session_name(session)
    if not name:
        return board
    lang = getattr(cfg, "target_language", None) or getattr(cfg, "language", None) or ""
    base_lang = L.base(lang) if lang else ""
    if not base_lang or base_lang == "en":
        name_hint = "a short name"
    else:
        name_hint = f"a short name in {L.lang_name(lang)}"
    return (f'Topic check: this session\'s Telegram topic is named "{name}". If the current task changed, '
            f'rename it to {name_hint} "project: task" with '
            'ctb topic-rename "<name>"; if it still fits, do nothing and do not mention it.'
            + (" " + board if board else ""))


def prompt_output(data: dict, cfg: Config, store: Optional[dict] = None) -> Optional[dict]:
    s = L.strings("en")
    parts = []
    if cfg.translate:
        parts.append(s["reminder_translate"].format(source=L.lang_name(cfg.source_language),
                                                    target=L.lang_name(cfg.target_language)))
    elif cfg.enforce_language:
        parts.append(s["reminder_enforce"].format(target=L.lang_name(cfg.target_language)))
    if store is None:
        store = _addon_store()
    if cfg.extra_reminder and _addon_on(cfg, "rules_reminder", store=store):
        parts.append(cfg.extra_reminder)
    deadline = hook_deadline()
    if data.get("session_id"):
        try:
            default_inbox().consume_answer_to(data["session_id"], until=deadline)
        except OSError:
            pass  # a stuck lock: the old target expires by itself (ten minutes)
    if data.get("session_id") and not is_loop_prompt(data.get("prompt", "")):
        hint = topic_check_hint(data["session_id"], cfg, store)
        if hint:
            parts.append(hint)
    msgs = telegram_messages(data.get("session_id") or "", data=data, via="prompt", until=deadline) if cfg.ready else []
    # A wake/loop prompt is synthetic; the messages actually taken determine whether the owner wrote.
    data['_owner_messages_delivered'] = _has_owner_messages(msgs)
    if msgs:
        parts.append(framing.deliver("[Owner also wrote in this session's Telegram topic (chat message):",
                                     msgs, "End of the Telegram message.") + "]")
    elif cfg.ready and is_wake_prompt(data.get("prompt", "")):
        # The bridge's own wake line and nothing was handed over with it. Three different cases:
        # still queued (the lock was busy): say nothing, the message is delivered on the next hook;
        # in flight (handed over earlier, not yet proven read): the wake exists to make Claude Code read it;
        # nothing at all: a second wake whose message was already delivered (sessions told the owner
        # "your message arrived empty").
        state = wake_state(data.get("session_id") or "")
        if state == "in-flight":
            parts.append(WAKE_IN_FLIGHT_NOTE)
        elif state == "empty":
            parts.append(EMPTY_WAKE_NOTE)
    if not parts:
        return None
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": " ".join(parts)}}


EMPTY_WAKE = "HarnessGram: this wake had no message left to deliver (it was already delivered). Nothing to do."
EMPTY_WAKE_NOTE = ("[HarnessGram: this wake line carried no new message; the owner's message was already delivered "
                   "to you earlier. Do not tell the owner that a message arrived empty and do not ask them to "
                   "resend: end this turn with no reply.]")


WAKE_IN_FLIGHT_NOTE = ("[HarnessGram: this wake line carries no text of its own. A message from the owner that was "
                       "handed over a moment ago may reach you right after it: answer that message normally. Do not "
                       "tell the owner that a message arrived empty.]")


def wake_state(session: str) -> str:
    """What a wake line finds: "queued" (a message still waits in the inbox), "in-flight" (one was handed over
    and is not yet proven read, so Claude Code may still hold it in its own queue), "empty" (nothing at all),
    or "unknown" when the inbox cannot be read."""
    if not session:
        return "unknown"
    try:
        inbox = default_inbox()
        if inbox.pending(session):
            return "queued"
        if any(not d.get("requeued_at") for d in inbox.leases(session)):
            return "in-flight"
    except OSError:
        return "unknown"
    return "empty"


def empty_wake(data: dict, cfg: Config) -> bool:
    """The bridge's own wake line, and nothing is queued or in flight for the session: a second wake typed while
    the session was busy, after the first one handed the message over. It must not start a turn.
    A wake with a delivery in flight must start one: the sweeper types it exactly to make Claude Code read a
    message stuck in its own queue, and stopping it would trap that message for good."""
    session = data.get("session_id")
    if not (cfg.ready and isinstance(session, str) and session and is_wake_prompt(data.get("prompt"))):
        return False
    return wake_state(session) == "empty"


def is_wake_prompt(prompt) -> bool:
    """The prompt is exactly the line the bridge types to wake a session (inbox.NUDGE_TEXT), nothing else."""
    from .inbox import NUDGE_TEXT
    return isinstance(prompt, str) and prompt.strip() == NUDGE_TEXT


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


def _project_label(cwd: str) -> str:
    """The name of the project group whose folder rules cover cwd, or "" (no projects, or no match)."""
    try:
        from .projects import _folder_project
        found = _folder_project(load_config(), os.path.realpath(cwd), os.path.abspath(cwd))
        return found.name if found else ""
    except Exception:
        return ""


def hook_prompt(stdin_text: str) -> int:
    try:
        data = json.loads(stdin_text or "{}")
        from .topics import main_turn_active, name_for_task, save_session_name, session_name
        session, prompt, cwd = data.get("session_id"), data.get("prompt"), data.get("cwd")
        from . import credit_mode
        if credit_mode.blocked(session):
            print(json.dumps({"decision": "block", "reason": credit_mode.stop_line()}, ensure_ascii=False))
            return 0
        if empty_wake(data, load_config()):
            # before anything marks the session busy: no turn starts, so no Stop hook would clear the mark
            print(json.dumps({"decision": "block", "reason": EMPTY_WAKE}))
            return 0
        if isinstance(session, str):
            if (session and not data.get("agent_id") and isinstance(prompt, str) and prompt.strip()
                    and not is_wake_prompt(prompt) and not is_loop_prompt(prompt)
                    and not prompt.lstrip().startswith(NOT_OWNER_PROMPT)):
                from .inbox import clear_nudge_pause, was_typed
                if not was_typed(prompt):
                    clear_nudge_pause(session)
            main_turn_active(session)
            _clear_ask(session)  # a new owner prompt means no question is pending any more
            _direct(data)
        if (isinstance(session, str) and session and isinstance(prompt, str) and prompt.strip()
                and not prompt.lstrip().startswith(NOT_OWNER_PROMPT)
                and isinstance(cwd, str) and cwd and not session_name(session)):
            save_session_name(session, name_for_task(cwd, prompt, _project_label(cwd)))
        cfg = load_config()
        if cfg.ready and data.get("session_id"):
            try:
                spawn(["session-state", f"--session={data['session_id']}", "--state=busy"])
            except Exception:
                pass
            if (isinstance(prompt, str) and prompt.strip()
                    and not prompt.lstrip().startswith(NOT_OWNER_PROMPT)
                    and not is_loop_prompt(prompt)):
                from .inbox import was_typed
                if not was_typed(prompt):
                    try:
                        spawn(["prompt-mirror", f"--session={session}"], text=prompt)
                    except Exception:
                        pass
        try:
            if not data.get("agent_id"):
                path, sid = data.get("transcript_path"), data.get("session_id") or ""
                last = float((_turn_state(sid) or {}).get("nudged") or 0) if sid else 0
                # the transcript is searched only when a reminder could follow (the rate limit allows one)
                check = isinstance(path, str) and path and not (last and 0 <= time.time() - last < PROGRESS_EVERY)
                turn_started(sid, background=background_soon(path) if check else 0)
        except Exception:
            pass
        store = _addon_store()  # one read of the add-on switches for this whole hook call
        out = prompt_output(data, cfg, store)
        try:
            if not data.get("agent_id") and cfg.ready:
                out = with_reminder(out, progress_nudge(session, store=store), "UserPromptSubmit")
        except Exception:
            pass
        try:
            if (cfg.ready and not data.get("agent_id") and isinstance(session, str) and session
                    and (data.get('_owner_messages_delivered')
                         or (isinstance(prompt, str) and prompt.strip() and not is_wake_prompt(prompt)
                             and not is_loop_prompt(prompt) and not prompt.lstrip().startswith(NOT_OWNER_PROMPT)))):
                from . import open_lines
                out = with_reminder(out, open_lines.reminder(session), "UserPromptSubmit")
        except Exception:
            pass
        try:  # owner rules or skills changed since this session loaded them: one line, once per change
            if not data.get("agent_id") and cfg.ready and isinstance(session, str) and session:
                from . import recall
                out = with_reminder(out, recall.prompt_notice(session), "UserPromptSubmit")
        except Exception:
            pass
    except Exception:
        return 0
    if out:
        print(json.dumps(out, ensure_ascii=False))
    return 0


# --- PostToolUse ---------------------------------------------------------------

def post_output(data: dict, cfg: Config, stop_only: bool = False) -> Optional[dict]:
    """After each tool call: hand over what was written in the session's topic mid-step.
    An OWNER message that is only a stop word asks the session to stop and report; a team member's
    "stop" is just a suggestion like any other of theirs. With stop_only (a background agent's tool
    call) only an owner stop word is taken: it halts the agent's work; everything else stays queued
    for the main conversation."""
    stops = {w.strip().lower() for w in cfg.stop_words}

    def is_stop(m: dict) -> bool:
        return (m.get("source") not in ("team", "panel", "mcp") and not m.get("forwarded")  # someone else's words never stop
                and (m.get("text") or "").strip().lower().rstrip("!. ") in stops)

    deadline = hook_deadline()
    taken = telegram_messages(data.get("session_id") or "", midturn=True, only=is_stop if stop_only else None,
                              data=data, via="agent" if stop_only else "post", until=deadline)
    items = [m for m in taken if (m.get("text") or "").strip()]
    if not items:
        if taken and framing.lease_nonce(taken):  # nothing to show: blank items are done, never put back
            try:
                default_inbox().settle(data.get("session_id") or "", framing.lease_nonce(taken), "proven",
                                       until=deadline)
            except Exception:
                pass  # a stuck lock: the sweeper settles it later (it puts it back, the next take settles it)
        return None
    nonce = framing.lease_nonce(items)
    team = [m for m in items if m.get("source") == "team"]
    owner = [m for m in items if m.get("source") not in ("team", "panel", "mcp")]
    session = (data.get("session_id") or "") if not stop_only and not data.get("agent_id") else ""
    if any(is_stop(m) for m in owner):
        try:  # a stopped turn is cancelled: nothing it was given may come back as a replay
            replay.clear_all(data.get("session_id") or "")
        except OSError:
            pass
        # The stop word itself becomes MIDTURN_STOP; every other message taken with it (the owner's other
        # instructions, team members, panel taps) is still handed over: the lease certifies the whole batch.
        rest = [m for m in items if not is_stop(m)]
        note = MIDTURN_STOP + (("\n" + _owner_delivery(MIDTURN_HEADER, rest, MIDTURN_CLOSING, session)) if rest
                               else ("\n" + framing.receipt(nonce) if nonce else ""))
    else:
        note = _owner_delivery(MIDTURN_HEADER, items, MIDTURN_CLOSING, session)
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}}


TYPING_EVERY = 15  # seconds between "typing" signals per session


def typing(session: str, cfg: Config) -> None:
    """Show 'typing...' in the session's topic while it works. Throttled per session with a marker
    file (one stat call when throttled) and sent by a detached child so the tool call never waits."""
    mark = state_dir() / "typing" / (re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_")
    try:
        if time.time() - mark.stat().st_mtime < TYPING_EVERY:
            return
    except OSError:
        pass
    if not cfg.ready:
        return
    private_dir(mark.parent)
    mark.touch()
    spawn(["typing", f"--session={session}"])


def periodic_reminder(session: str, transcript: str, cfg: Config, now: Optional[float] = None,
                      store: Optional[dict] = None) -> str:
    """The `periodic_reminder` sentence, at most once per `periodic_reminder_minutes` per session and
    only once the transcript is long (`periodic_reminder_min_kb`). "" when off, too soon or too short."""
    text = getattr(cfg, "periodic_reminder", "")
    if not text or not session or not transcript or not _addon_on(cfg, "rules_reminder", store=store):
        return ""
    now = time.time() if now is None else now
    mark = state_dir() / "reminder" / (re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_")
    try:
        if now - mark.stat().st_mtime < max(0, cfg.periodic_reminder_minutes) * 60:
            return ""
    except OSError:
        pass
    try:
        if os.path.getsize(transcript) < cfg.periodic_reminder_min_kb * 1024:
            return ""
    except OSError:
        return ""
    private_dir(mark.parent)
    mark.touch()
    os.utime(mark, (now, now))
    return text


PROGRESS_AFTER = 3 * 60     # a turn this long counts as a long task
PROGRESS_TOOLS = 25         # so does a turn with this many tool calls
PROGRESS_EVERY = 10 * 60    # at most one "show a progress card" reminder per session this often
PROGRESS_NUDGE = ('The owner follows this work on a phone and this session is on a long task with no progress '
                  'card of its own. Show one now: `ctb card progress "<short title>" --step "<name>" --step "<name>" '
                  "... --at <finished steps> --percent <n>` (3 to 6 short steps, in the owner's language), then keep "
                  "it current with `ctb card progress --id <id> --at <n> --percent <n>` as each step finishes. Skip it "
                  "only if the work ends within a minute or two.")
TASK_LIST_NUDGE = ("Keep your own task list current with TaskCreate/TaskUpdate (or TodoWrite); it is the primary "
                   "source for the automatic progress card.")
PROGRESS_UPDATE = ("Your progress card {id} has not changed for a while. Update it to where the work really is: "
                   "`ctb card progress --id {id} --at <finished steps> --percent <n>`. If the work has moved on "
                   "to something else, rewrite the steps too with `--step <name> --step <name> ...` "
                   "and set --at to match the new steps; do not just bump --at.")
BACKGROUND_TOOLS = ("Agent", "Task", "Workflow")


def _turn_mark(session: str):
    return state_dir() / "turn" / (re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_")


def _turn_state(session: str) -> Optional[dict]:
    try:
        state = json.loads(_turn_mark(session).read_text(encoding="utf-8"))
        float(state["start"])
        return state if isinstance(state, dict) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


TURN_LOCK_WAIT = 0.25  # seconds a hook waits for another hook of the same session; then it skips its update
CARD_LOCK_WAIT = 0.25  # seconds a hook waits for the card lock; then it skips (a hook never blocks a turn)


class TurnBusy(Exception):
    """Another hook of the same session holds its turn state: this one skips, it never waits long."""


@contextlib.contextmanager
def _turn_locked(session: str, wait: float = TURN_LOCK_WAIT):
    """The session's turn state, held for one read-modify-write: parallel tool calls of one session run their
    hooks at the same time, and without this two of them could both claim the same reminder or overwrite each
    other's count. Raises TurnBusy after `wait` seconds instead of slowing the turn."""
    mark = _turn_mark(session)
    private_dir(mark.parent)
    with open(str(mark) + ".lock", "a") as lock:
        until = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= until:
                    raise TurnBusy() from None
                time.sleep(0.01)
        try:
            yield mark
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def turn_started(session: str, now: Optional[float] = None, background: int = 0) -> None:
    """A new prompt reached the session: the turn's clock and tool count start here. The last reminder's time
    is kept (the rate limit is per session, not per turn); `background` says background tasks still run."""
    if not session:
        return
    now = time.time() if now is None else now
    try:
        with _turn_locked(session) as mark:
            old = _turn_state(session) or {}
            write_private(_tools_file(session), "")  # the count starts again with the turn
            write_private(mark, json.dumps({"start": now, "nudged": float(old.get("nudged") or 0),
                                            "bg": bool(background)}))
    except TurnBusy:
        pass


def launches_background(data: dict) -> bool:
    """This tool call started work that keeps running in the background: a command asked to, or an agent or
    workflow whose structured result says it was launched (status "async_launched", or isAsync true). A
    foreground agent has already finished when this runs, so the request alone proves nothing, and the result's
    text is never read for it (an agent's report may quote any phrase)."""
    inp = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    name = data.get("tool_name")
    if name == "Bash":
        return inp.get("run_in_background") is True
    if name not in BACKGROUND_TOOLS or inp.get("run_in_background") is False:
        return False
    resp = data.get("tool_response")
    if not isinstance(resp, dict):
        return False
    return resp.get("status") == "async_launched" or resp.get("isAsync") is True


def _tools_file(session: str) -> Path:
    return Path(str(_turn_mark(session)) + ".tools")


def tool_called(session: str, background: bool = False) -> None:
    """One more tool call in the session's main turn (PostToolUse): one byte appended with O_APPEND, "b" for a
    call that started background work, "." for any other. The file's size is the count, so parallel hooks
    lose no call and none of them waits for a lock."""
    if not session or _turn_state(session) is None:
        return
    path = _tools_file(session)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except OSError:
        return
    try:
        os.write(fd, b"b" if background else b".")
    except OSError:
        pass
    finally:
        os.close(fd)


def tool_count(session: str) -> Tuple[int, bool]:
    """(tool calls in this turn, whether one of them started background work), from the per-turn file."""
    try:
        with open(_tools_file(session), "rb") as f:
            data = f.read(1 << 16)
    except OSError:
        return 0, False
    return len(data), b"b" in data


def long_task(state: dict, now: float, tools: int = 0, background: bool = False) -> bool:
    """A turn that runs background tasks, has run PROGRESS_AFTER or made PROGRESS_TOOLS tool calls."""
    return (bool(state.get("bg")) or background or now - float(state["start"]) >= PROGRESS_AFTER
            or tools >= PROGRESS_TOOLS)


def progress_nudge(session: str, now: Optional[float] = None, store: Optional[dict] = None) -> str:
    """_progress_nudge under the session's turn lock, so a reminder is claimed once however many hooks of the
    session run at the same time. "" when another hook holds the lock (it decides for both)."""
    if not session or not _addon_on(None, "progress_reminder", store=store):
        return ""
    try:
        with _turn_locked(session):
            return _progress_nudge(session, now)
    except TurnBusy:
        return ""


def _progress_nudge(session: str, now: Optional[float] = None) -> str:
    """One sentence asking a session on a long task (long_task) that has no progress card of its own for one,
    at most once per PROGRESS_EVERY, or asking for an update of its card left untouched (at most once per
    PROGRESS_STALE). A fresh automatic card replaces either card-command nudge with a short reminder to keep
    the agent's task list current. "" when it is too soon,
    when the owner chose the quiet activity level, or when a reminder is not yet due. One small read per tool call
    until the turn is long."""
    from .config import activity_level
    now = time.time() if now is None else now
    mark = _turn_mark(session)
    state = _turn_state(session)
    if state is None:
        return ""
    start, nudged = float(state["start"]), float(state.get("nudged") or 0)
    if now < start:  # wall clock moved backwards; start a new observation window
        state.update(start=now)
        start = now
        write_private(mark, json.dumps(state))
    if activity_level() == "quiet" or not long_task(state, now, *tool_count(session)):
        return ""
    # A changed atomic state file invalidates the cheap per-turn check marker.
    try:
        stamp = (state_dir() / cards.STATE_FILE).stat().st_mtime_ns
    except OSError:
        stamp = 0
    if state.get("cards_stamp") == stamp and start <= now < state.get("cards_next", 0):
        return ""
    progress_cards = [c for c in cards.open_cards() if c["session"] == session and c["kind"] == "progress"]
    from . import auto_cards
    if auto_cards.enabled() and any(c.get("automatic") and 0 <= now - c["updated"] < PROGRESS_EVERY
                                    for c in progress_cards):
        if nudged and 0 <= now - nudged < PROGRESS_EVERY:
            state.update(cards_stamp=stamp, cards_next=max(start, nudged + PROGRESS_EVERY))
            write_private(mark, json.dumps(state))
            return ""
        state.update(nudged=now, cards_stamp=stamp, cards_next=now + PROGRESS_EVERY)
        write_private(mark, json.dumps(state))
        return TASK_LIST_NUDGE
    mine = [c for c in progress_cards if not c.get("automatic")]
    if mine:
        last = max((c.get("nudged_at", 0) for c in mine), default=0)
        eligible = [c for c in mine if cards.is_stale(c, now) and
                    (not c.get("nudged_at") or c["nudged_at"] < c["updated"] or now < c["nudged_at"])]
        if eligible and (not last or now < last or now - last >= cards.PROGRESS_STALE_AFTER):
            try:  # a hook never waits long on the card lock: busy now, asked again on a later call
                ident = cards.stale_reminder(session, now, until=time.monotonic() + CARD_LOCK_WAIT)
            except cards.CardError:
                return ""
            if ident:
                return PROGRESS_UPDATE.format(id=ident)
        deadlines = [c["updated"] + cards.PROGRESS_STALE_AFTER + 1 for c in mine
                     if not cards.finished(c) and (not c.get("nudged_at") or c["nudged_at"] < c["updated"])]
        if last and now >= last:
            deadlines = [max(d, last + cards.PROGRESS_STALE_AFTER) for d in deadlines]
        state.update(cards_stamp=stamp, cards_next=max(now + 1, min(deadlines, default=now + cards.PROGRESS_STALE_AFTER)))
        write_private(mark, json.dumps(state))
        return ""
    if nudged and 0 <= now - nudged < PROGRESS_EVERY:
        state.update(cards_stamp=stamp, cards_next=max(start, nudged + PROGRESS_EVERY))
        write_private(mark, json.dumps(state))
        return ""
    state.update(nudged=now, cards_stamp=stamp, cards_next=now + PROGRESS_EVERY)
    write_private(mark, json.dumps(state))
    return PROGRESS_NUDGE


def with_reminder(out: Optional[dict], reminder: str, event: str = "PostToolUse") -> Optional[dict]:
    """Add the reminder to the hook context, creating output when necessary."""
    if not reminder:
        return out
    if not out:
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": reminder}}
    if not isinstance(out.get("hookSpecificOutput"), dict):
        out["hookSpecificOutput"] = {"hookEventName": event}
    spec = out["hookSpecificOutput"]
    previous = spec.get("additionalContext", "")
    if not isinstance(previous, str):
        previous = ""
    spec["additionalContext"] = (previous + "\n\n" if previous else "") + reminder
    return out


def _direct(data: dict) -> None:
    """Note the permission mode and agent that this hook's input reports (direct.py): a change made in
    Claude itself gets its own line in the topic (detached). Never raises."""
    try:
        from . import direct
        session = str(data.get("session_id") or "")
        direct.notify(session, direct.from_hook(data))
    except Exception:
        pass


def hook_post(stdin_text: str) -> int:
    """Runs after every tool call, so it must be quick and silent when there is nothing queued."""
    try:
        data = json.loads(stdin_text or "{}")
        session = data.get("session_id") or ""
        if not session:
            return 0
        # Not here: with parallel tool calls another tool can finish while a permission prompt is still waiting.
        try:
            from . import live_activity
            live_activity.record(session, data)
        except Exception:
            pass  # activity must never delay or break the hook
        _clear_ask(session)  # any finished tool call, AskUserQuestion included, ends the pending question
        _direct(data)
        if not data.get("agent_id"):
            try:
                from .topics import main_turn_active
                main_turn_active(session)
            except OSError:
                pass
        cfg = load_config()
        try:
            if not data.get("agent_id"):
                from . import state_emoji
                state_emoji.update(cfg, session, data)
        except Exception:
            pass
        try:
            from . import auto_cards
            if not data.get("agent_id") and data.get("tool_name") == "TodoWrite" and auto_cards.enabled():
                inp = data.get("tool_input") or {}
                if isinstance(inp, dict) and isinstance(inp.get("todos"), list):
                    from .decisions import _place_of
                    from .i18n import t
                    chat, thread = _place_of(cfg, session)
                    cards.sync_todos(session, chat, thread, inp["todos"], t("todo_plan", lang=cfg.target_language),
                                     until=time.monotonic() + CARD_LOCK_WAIT)  # busy: skipped, the next list syncs
        except Exception:
            pass
        try:
            typing(session, cfg)
            append_progress(session, data, cfg)
        except Exception:
            pass
        # A background agent's tool calls share the session id: the owner's messages and the periodic
        # reminder are for the main conversation, so they wait for its next step (one
        # reached a writer agent). Only a lone owner stop word is delivered to an agent too, so it halts.
        main = not data.get("agent_id")
        store = _addon_store() if main else {}  # one read of the add-on switches for this hook call
        try:
            reminder = periodic_reminder(session, data.get("transcript_path") or "", cfg, store=store) if main else ""
        except Exception:
            reminder = ""
        # Inbox.pending is this one stat; asking it here would load the whole inbox module for every tool call.
        queued = (inbox_dir() / (safe_name(session) + ".jsonl")).exists()
        out = post_output(data, cfg, stop_only=not main) if queued else None
        try:
            if main:
                tool_called(session, launches_background(data))
            nudge = progress_nudge(session, store=store) if main and cfg.ready else ""
        except Exception:
            nudge = ""
        reminder = "\n\n".join(x for x in (reminder, nudge) if x)
        out = with_reminder(out, reminder)
    except Exception:
        return 0
    if out:
        print(json.dumps(out, ensure_ascii=False))
    return 0


# --- AskUserQuestion (PreToolUse) -----------------------------------------------------------

def hook_ask(stdin_text: str) -> int:
    """PreToolUse for AskUserQuestion only (matcher in the registration): record the call's questions and
    options with full-text hashes and a thirty-minute expiry, so the bridge can prove that a numbered
    dialog on the pane is this question. It never blocks,
    denies or comments on the tool: it prints nothing and always exits 0, whatever happens."""
    try:
        data = json.loads(stdin_text or "{}")
        if (isinstance(data, dict) and data.get("hook_event_name") == "PreToolUse"
                and data.get("tool_name") == "AskUserQuestion" and not data.get("agent_id")
                and isinstance(data.get("session_id"), str)):
            from . import asks
            asks.record(data["session_id"], data.get("tool_input"))
    except BaseException as exc:  # noqa: BLE001 - nothing may escape into Claude Code
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
    return 0


LONG_COMMAND_WARNING = "run this with run_in_background and wait for the notification"
_LONG_BASH_PATTERNS = (
    re.compile(r"(?is)(?:^|[;&|]\s*)(?:while|until)\b.*?\b(?:sleep)\b"),
    re.compile(r"(?i)(?:^|[;&|]\s*)tail\s+(?:[^\n;&|]*-[^\n;&|]*f\b|-f\b)"),
    re.compile(r"(?i)(?:^|[;&|]\s*)watch(?:\s|$)"),
    re.compile(r"(?i)\bpython(?:3)?\s+-m\s+http\.server\b"),
    re.compile(r"(?i)\bnpm\s+run\s+(?:dev|start)\b"),
    re.compile(r"(?i)(?:^|[;&|]\s*)uvicorn(?:\s|$)"),
    re.compile(r"(?i)(?:^|[;&|]\s*)(?:uv\s+run\s+)?(?:python\s+-m\s+)?pytest(?:\s+[^\n;&|]*)?\s+tests\s*(?:$|[;&|])"),
    re.compile(r"(?i)(?:^|[;&|]\s*)codex\s+exec\b"),
)


def long_running_bash(tool_input) -> bool:
    """Whether a foreground Bash input has the bounded set of long-running warning signs."""
    if not isinstance(tool_input, dict) or tool_input.get("run_in_background"):
        return False
    timeout = tool_input.get("timeout")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 300_000:
        return True
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return False
    # An explicit shell background makes the warning unnecessary for server-like commands.
    if re.search(r"(?:^|\s)&\s*(?:$|[#;])", command):
        return False
    return any(pattern.search(command) for pattern in _LONG_BASH_PATTERNS)


def hook_bash(stdin_text: str) -> int:
    """Advise on likely long foreground Bash calls without blocking or changing their input."""
    try:
        data = json.loads(stdin_text or "{}")
        if (isinstance(data, dict) and data.get("hook_event_name") == "PreToolUse"
                and data.get("tool_name") == "Bash" and long_running_bash(data.get("tool_input"))):
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                      "additionalContext": LONG_COMMAND_WARNING}}))
    except (ValueError, TypeError):
        pass
    return 0


def _clear_ask(session: str) -> None:
    try:
        from . import asks
        asks.clear(session)
    except Exception:
        pass


# --- Notification -----------------------------------------------------------------------

NOTIFY_EVERY = 600  # seconds: at most one ring per session per 10 minutes


BACKGROUND_WAIT = 0.2  # seconds the prompt hook gives the background-task search before it goes on without it


def background_soon(transcript: str, wait: float = BACKGROUND_WAIT) -> int:
    """background_running, given at most `wait` seconds (a large transcript or slow disk never holds up a
    prompt): 0 when it did not finish in time. The search goes on in a daemon thread that dies with the hook."""
    import threading
    box: list = []

    def run():
        try:
            box.append(background_running(transcript))
        except Exception:
            box.append(0)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(wait)
    return box[0] if box else 0


def background_running(transcript: str, now: Optional[float] = None) -> int:
    """Count recently started background tasks without a task notification."""
    from datetime import datetime
    now = time.time() if now is None else now
    try:
        with open(transcript, "rb") as f:
            f.seek(0, 2)
            start = max(0, f.tell() - 3_000_000)
            f.seek(start)
            raw = f.read()
        if start:
            raw = raw.partition(b"\n")[2]
    except OSError:
        return 0
    started, done = {}, set()
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                continue
            try:
                at = datetime.fromisoformat(event.get("timestamp", "").replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                at = now
            msg = event.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                inp = block.get("input") or {}
                if not isinstance(inp, dict):
                    inp = {}
                if block.get("type") == "tool_use" and block.get("id") and (
                    block.get("name") in ("Agent", "Task", "Workflow") and inp.get("run_in_background", True)
                    or block.get("name") == "Bash" and inp.get("run_in_background")):
                    started[block["id"]] = at
                if block.get("type") == "tool_result":
                    body = str(block.get("content"))
                    if "moved to the background" in body:
                        started[block.get("tool_use_id")] = at
                    elif block.get("tool_use_id") in started and (
                            block.get("is_error") or not any(k in body for k in ("launched successfully", "in background", "in the background", "task ID", "task_id"))):
                        done.add(block.get("tool_use_id"))  # denied, failed or foreground: never ran in the background
                done.update(re.findall(r"<tool-use-id>([^<]+)</tool-use-id>", json.dumps(block)))
            done.update(re.findall(r"<tool-use-id>([^<]+)</tool-use-id>", json.dumps(event.get("attachment"))))
        except (ValueError, TypeError):
            continue
    return sum(1 for task, at in started.items() if task and task not in done and 0 <= now - at < 10800)


def hook_notify(stdin_text: str) -> int:
    """Claude Code needs you (permission prompt, idle, a question): ring the phone, in the session's
    topic, with a sound. At most once per 10 minutes per session. Silent on any failure."""
    try:
        data = json.loads(stdin_text or "{}")
        session = data.get("session_id") or ""
        cfg = load_config()
        if not session or not cfg.ready:
            return 0
        ntype = re.sub(r"[^a-z_]", "", str(data.get("notification_type") or "").lower())
        if ntype == "idle_prompt" and _addon_on(cfg, "title_sync"):
            from . import title_sync
            title_sync.flush(session)
        if ntype == "idle_prompt" and background_running(data.get("transcript_path") or ""):
            return 0
        # Real terminal dialogs each need a fresh card. Generic rings retain the
        # existing throttle when there is no mapped terminal to capture.
        from .inbox import pane_for_session
        if ntype != "idle_prompt" and pane_for_session(session):
            spawn(["notify", f"--session={session}", f"--type={ntype}"])
            return 0
        mark = state_dir() / "notify" / (re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_")
        try:
            if time.time() - mark.stat().st_mtime < NOTIFY_EVERY:
                return 0
        except OSError:
            pass
        private_dir(mark.parent)
        mark.touch()
        spawn(["notify", f"--session={session}", f"--type={ntype}"])
    except Exception:
        pass
    return 0


# --- Lifecycle (PreCompact / SessionEnd / SubagentStart / SubagentStop) ------------------------


def hook_lifecycle(stdin_text: str) -> int:
    """One lifecycle notice in the session's topic, dispatched by the event name in the hook JSON: compaction starting, the session closed, a background subagent finished.
    Never raises and always exits 0: a notice must never break the session."""
    try:
        data = json.loads(stdin_text or "{}")
        if not isinstance(data, dict):
            return 0
        cfg = load_config()
        session = data.get("session_id") or ""
        if not isinstance(session, str) or not session or not cfg.ready:
            return 0
        event = data.get("hook_event_name") or ""
        if event == "PreCompact":
            compact.note_compacting(session, hook=True)  # before anything else: the screen is a spinner from now on
            if compact.started_from_telegram(session):
                return 0  # started from Telegram: its progress bar is the notice
            spawn(["lifecycle", f"--session={session}", "--kind=precompact"])
        elif event == "SessionEnd":
            if data.get("reason") == "clear":
                return 0  # /clear starts a fresh conversation in the SAME session: it is not closed
            from .inbox import consume_planned_restart
            if consume_planned_restart(session):
                return 0
            if data.get("reason") in ("prompt_input_exit", "logout"):
                from . import revive
                revive.closed_on_purpose(session)  # not a crash loss: it stays closed
            spawn(["lifecycle", f"--session={session}", "--kind=session-end"])
        elif event == "PostModelSwitch":  # /model typed in Claude (or a fallback): the card shows it now
            from . import models, statusline
            level = (data.get("effort") or {}).get("level") if isinstance(data.get("effort"), dict) else ""
            statusline.save_model(session, models.display_name(str(data.get("to_model") or "")), level or "")
            spawn(["direct-note", f"--session={session}", "--field=model", "--value="])
        elif event == "SubagentStart":
            spawn(["lifecycle", f"--session={session}", "--kind=subagent-start",
                   f"--agent-type={str(data.get('agent_type') or '')}",
                   f"--agent-id={str(data.get('agent_id') or '')}",
                   f"--agent-transcript={str(data.get('agent_transcript_path') or '')}",
                   f"--transcript={str(data.get('transcript_path') or '')}"])
        elif event == "SubagentStop":  # every end is counted (no throttle): the agents card needs them all
            spawn(["lifecycle", f"--session={session}", "--kind=subagent-stop",
                   f"--agent-id={str(data.get('agent_id') or '')}",
                   f"--agent-type={str(data.get('agent_type') or '')}",
                   f"--description={str(data.get('description') or '')}",
                   f"--agent-transcript={str(data.get('agent_transcript_path') or '')}",
                   f"--transcript={str(data.get('transcript_path') or '')}"])
    except Exception:
        pass
    return 0


def load_config() -> Config:
    try:
        return load()
    except ConfigError:
        return Config()  # not configured: hooks stay silent instead of failing


# -- usage and quota helpers shared with the other modules --------------------

_LAZY_NAMES = {"PROGRESS_STALE": "cards", "claude_account": "usage", "claude_usage_file": "usage", "claude_five_hour": "usage", "quota_lines": "usage",
               "quota_rows": "usage", "context_footer": "usage", "unmix": "fmt", "wrap_reset": "fmt"}


def __getattr__(name: str):
    """The names other modules and the tests take from here, resolved on first use (see _Lazy)."""
    if name in _LAZY_NAMES:
        # PROGRESS_STALE: a progress card not touched this long while the session works gets a nudge to update it
        value = getattr(globals()[_LAZY_NAMES[name]], "PROGRESS_STALE_AFTER" if name == "PROGRESS_STALE" else name)
    elif name in ("job_hebrew", "mostly_english_label"):
        from . import translate
        value = getattr(translate, name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value

