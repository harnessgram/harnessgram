"""Live progress: append a content-free label per tool call, and edit one Telegram message."""
from __future__ import annotations

import fcntl
import html
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Callable, Optional

from . import flood, fmt
from . import lang as L
from .config import AGENT_NOTICES, Config, activity_level, home_chat, private_dir, setting_on, state_dir, write_private
from .i18n import get_lang, t
from .telegram import Bot, TelegramError
from .topics import chat_of, place


def interim_text(text: str, cfg: Config, fast: bool = False) -> str:
    """Translate even short interim prose; fast Stop flushes never wait for an engine."""
    from .creds import redact_detail
    from .translate import translate_text, reset_engines, used_engines
    text = redact_detail(text)
    if cfg.translate and not fast and L.needs_translation(text, cfg.target_language):
        try:
            reset_engines()
            text = translate_text(text, cfg)
            if used_engines():
                text += "\nTranslated by " + ", ".join(used_engines())
        except Exception:
            pass
    return redact_detail(text)


INTERIM_CHUNK = 1800  # characters per interim message that will be translated: Hebrew + folded original fit 4096
INTERIM_TIMEOUT = 60.0  # seconds an interim translation may take before the original is left as it is
INTERIM_FLOOD_WAIT = 30.0  # longest flood wait an interim edit sits out; a longer one is retried on a later run


def interim_original(text: str) -> str:
    """Interim prose as it goes out at once: Claude's own words, secrets hidden."""
    from .creds import redact_detail
    return redact_detail(text)


def will_translate(text: str, cfg: Config, fast: bool = False) -> bool:
    """Will this interim text be edited to a translation after it is shown? Never in a fast Stop flush,
    which must not wait for an engine."""
    return bool(cfg.translate and not fast and L.needs_translation(text, cfg.target_language))


class InterimFlood(Exception):
    """An interim translation edit is held back by flood control; the step is retried later."""


def _edit_interim(bot, chat, mid, text: str) -> str:
    """Edit interim message `mid` to `text` (HTML). A closed gate or a 429 is waited out once (capped).
    Returns "ok" (also for "message is not modified"), "flood" or "failed"."""
    params = {"chat_id": chat, "message_id": mid, "text": text, "parse_mode": "HTML",
              "link_preview_options": {"is_disabled": True}}
    waited = False
    while True:
        try:
            flood.optional_call(bot, "editMessageText", params, budget=False)
            return "ok"
        except TelegramError as exc:
            if "not modified" in str(exc).lower():
                return "ok"
            if exc.code == 429:
                ra = exc.retry_after if isinstance(exc.retry_after, (int, float)) and exc.retry_after > 0 else 1.0
                delay = max(ra, flood.remaining(chat))
                if waited or delay > INTERIM_FLOOD_WAIT:
                    return "flood"
                waited = True
                time.sleep(delay)
                continue
            return "failed"
        except OSError:
            return "failed"


def _send_interim(bot, chat, thread, text: str) -> None:
    """One new interim message (HTML), through the flood gate; raises InterimFlood when it is closed."""
    try:
        flood.optional_call(bot, "sendMessage", {
            "chat_id": chat, "message_thread_id": thread, "text": text, "parse_mode": "HTML",
            "disable_notification": True, "link_preview_options": {"is_disabled": True}}, budget=False)
    except TelegramError as exc:
        if exc.code == 429:
            raise InterimFlood() from exc
        raise


def split_html_parts(text: str, limit: int = 3900) -> list:
    """Plain `text` as HTML-escaped parts of at most `limit` characters, cut between paragraphs, then
    lines, then words: every part is complete, valid HTML (no cut tag or entity)."""
    parts: list = []
    for chunk in fmt.chunks(text, 3200) or [text]:
        stack = [chunk]
        while stack:
            piece = stack.pop(0)
            if len(html.escape(piece, quote=False)) <= limit or len(piece) < 2:
                if piece.strip():
                    parts.append(html.escape(piece, quote=False))
                continue
            half = len(piece) // 2
            cut = max(piece.rfind("\n", 0, half), piece.rfind(" ", 0, half))
            cut = cut if cut > 0 else half
            stack[:0] = [piece[:cut], piece[cut:]]
    return parts


def translate_interim(cfg: Config, bot, chat, thread, pieces: list, translate_fn=None,
                      state: Optional[dict] = None, persist: Optional[Callable[[], None]] = None) -> int:
    """Edit each interim message (`pieces`: [message_id, original text]) to its translation, the
    original folded under it. A translation too long for that message keeps the original, and the
    translation follows as new messages, each a complete part. A failed translation leaves the
    original with a short note. Progress is kept in `state` (next piece, its translation, parts sent)
    and saved with `persist`, so a retry after a flood limit (InterimFlood) resumes where it stopped
    and never sends a part twice. Returns the number of pieces now shown translated."""
    from .creds import redact_detail
    from .translate import reset_engines, used_engines
    if translate_fn is None:
        from .translate import translate_text
        translate_fn = lambda text: translate_text(text, cfg)  # noqa: E731
    state = {} if state is None else state
    save = persist or (lambda: None)
    lang = cfg.target_language
    prefix = L.RLM if L.is_rtl(lang) else ""
    label = html.escape(L.strings(lang)["original"], quote=False)
    done = 0
    t0 = time.monotonic()
    while state.get("tr_next", 0) < len(pieces):
        i = state.get("tr_next", 0)
        mid, piece = pieces[i]
        he = state.get("tr_he")
        if he is None:
            engines: tuple = ()
            if time.monotonic() - t0 < INTERIM_TIMEOUT:
                try:
                    reset_engines()
                    out = translate_fn(piece)
                    engines = used_engines()
                    he = redact_detail(out.strip()) if out is not None else None
                except Exception:  # noqa: BLE001 - any engine failure keeps the original
                    he = None
            if he is None:  # failed: the original stays, and says so
                note = html.escape(t("translation_unavailable", lang=lang), quote=False)
                text = html.escape(piece, quote=False) + "\n\n<i>" + note + "</i>"
                if mid is not None and len(text) <= 4096 and _edit_interim(bot, chat, mid, text) == "flood":
                    raise InterimFlood()
                state.update(tr_next=i + 1, tr_sent=0)
                state.pop("tr_he", None)
                save()
                continue
            by = ""
            if engines:
                names = ", ".join(engines)
                shown = "\u2068" + names + "\u2069" if L.base(lang) == "he" else names
                by = "\n<i>" + html.escape(t("translated_by", lang=lang, engines=shown), quote=False) + "</i>"
            state.update(tr_he=he, tr_by=by, tr_sent=0)
            save()
        by = state.get("tr_by", "")
        if not he.strip() or he.strip() == piece.strip():
            outcome = "ok"  # nothing to change: the original stays as it is
        else:
            body = prefix + html.escape(he, quote=False) + by
            folded = body + f"\n\n\U0001F310 <b>{label}</b>\n<blockquote expandable>{html.escape(piece, quote=False)}</blockquote>"
            outcome = "failed"
            if mid is not None and len(folded) <= 4096 and not state.get("tr_sent"):
                outcome = _edit_interim(bot, chat, mid, folded)
                if outcome == "flood":
                    raise InterimFlood()
            if outcome != "ok":  # too long for one message, or the edit was refused: complete parts follow
                parts = split_html_parts(he)
                for k in range(state.get("tr_sent", 0), len(parts)):
                    text = prefix + parts[k] + (by if k == len(parts) - 1 else "")
                    _send_interim(bot, chat, thread, text)
                    state["tr_sent"] = k + 1
                    save()
                outcome = "ok"
            done += 1
        state.update(tr_next=i + 1, tr_sent=0)
        state.pop("tr_he", None)
        state.pop("tr_by", None)
        save()
    return done


# --- translation in the background: the live card and the interim texts never wait for an engine ---------------

JOB_CLAIM = 180.0  # seconds a claimed interim-text job belongs to the run that claimed it
MAX_HEADS = 40  # headings kept waiting at most (a long turn re-asks the rest on its next card edit)


def _jobs_paths(session: str, root: Optional[Path] = None) -> tuple:
    pdir = private_dir(root or (state_dir() / "progress"))
    name = safe_name(session)
    return pdir / f"{name}.jobs.json", pdir / f"{name}.jobs.lock"


def _load_jobs(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    heads = data.get("heads") if isinstance(data.get("heads"), list) else []
    texts = data.get("texts") if isinstance(data.get("texts"), list) else []
    return {"heads": [h for h in heads if isinstance(h, str)],
            "texts": [j for j in texts if isinstance(j, dict) and isinstance(j.get("ids"), list)]}


def _save_jobs(path: Path, data: dict) -> None:
    if data["heads"] or data["texts"]:
        write_private(path, json.dumps(data, ensure_ascii=False))
    else:
        try:
            path.unlink()
        except OSError:
            pass


def _jobs_locked(session: str, root: Optional[Path], change: Callable[[dict], object]):
    """Apply `change` to the session's background jobs under their own lock (never the card's lock, so a
    translation in progress never holds back a step) and save them. Returns what `change` returned."""
    path, lock_path = _jobs_paths(session, root)
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load_jobs(path)
        out = change(data)
        _save_jobs(path, data)
        return out


def queue_jobs(session: str, heads: list, texts: list, root: Optional[Path] = None) -> None:
    """Remember step headings and interim texts (message ids and the English each shows) to translate later."""
    def add(data):
        for h in heads:
            if h not in data["heads"]:
                data["heads"].append(h)
        del data["heads"][:-MAX_HEADS]
        for n, job in enumerate(texts):
            if job.get("ids"):  # a key of its own: two texts of one flush shared one, and finishing the first
                # removed the second untranslated (review of #154)
                data["texts"].append(dict(job, key=f"{time.time():.6f}:{n}:{secrets.token_hex(3)}", tr={}, claimed=0))
    try:
        _jobs_locked(session, root, add)
    except OSError:
        pass


def jobs_waiting(session: str, root: Optional[Path] = None) -> bool:
    data = _load_jobs(_jobs_paths(session, root)[0])
    return bool(data["heads"] or data["texts"])


def _refresh_card(cfg: Config, session: str, root: Optional[Path], bot) -> None:
    """Headings were translated after the card went out: the card is marked due an edit, so its next regular
    flush (the next step, or the end-of-turn flush) shows them in Hebrew. No edit of its own: one would use
    the optional budget and the MIN_EDIT pause that the next step needs to appear at once."""
    pdir = private_dir(root or (state_dir() / "progress"))
    name = safe_name(session)
    state_path = pdir / f"{name}.msg"
    with open(pdir / f"{name}.lock", "a") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if "cursor" not in state or not state.get("id") or not state.get("cards") or state.get("dirty"):
            return
        state["dirty"] = True
        write_private(state_path, json.dumps(state, ensure_ascii=False))


def run_jobs(cfg: Config, session: str, root: Optional[Path] = None, bot=None, translate_fn=None) -> int:
    """Translate what the live card and the interim texts showed in English, after they went out: step
    headings first (cached, then shown on a card edit), then each interim text (its messages edited to the
    translation, the original folded under it). A flood limit leaves the rest for a later run. Returns the
    number of items translated."""
    if not session or not cfg.ready or not cfg.translate:
        return 0
    done = 0
    heads = _jobs_locked(session, root, lambda data: [data["heads"].pop(0) for _ in list(data["heads"])])
    translated = 0
    for words in heads or []:
        try:
            if L.base(cfg.target_language) == "he":
                he = heading_he(words, cfg)
            else:
                from .translate import translate_text
                he = translate_text(words, cfg).strip()
        except Exception:
            he = ""
        translated += bool(he and he != words)
    if translated:
        _refresh_card(cfg, session, root, bot)
        done += translated

    def claim(data):
        now = time.time()
        for job in data["texts"]:
            if now - float(job.get("claimed") or 0) >= JOB_CLAIM:
                job["claimed"] = now
                return dict(job)
        return None

    while True:
        try:
            job = _jobs_locked(session, root, claim)
        except OSError:
            break
        if job is None:
            break
        bot = bot or Bot.from_config(cfg)
        key = job.get("key")

        def save(job=job, key=key):
            def put(data):
                for j in data["texts"]:
                    if j.get("key") == key:
                        j["tr"], j["claimed"] = job["tr"], time.time()
            _jobs_locked(session, root, put)

        flooded = False
        try:
            done += translate_interim(cfg, bot, job.get("chat"), job.get("thread"), job["ids"], translate_fn,
                                      state=job.setdefault("tr", {}), persist=save)
        except InterimFlood:
            flooded = True
        except Exception:  # noqa: BLE001 - a broken job is dropped, never retried forever
            pass

        def finish(data, flooded=flooded, key=key):
            for j in list(data["texts"]):
                if j.get("key") == key:
                    if flooded:
                        j["claimed"] = 0
                    else:
                        data["texts"].remove(j)
        _jobs_locked(session, root, finish)
        if flooded:
            break
    return done


def append_boundary(session: str, root: Optional[Path] = None, until: Optional[float] = None) -> None:
    """Mark where a new owner message starts. With `until` (a time.monotonic() deadline) the lock is polled
    and TimeoutError is raised when it stays taken, so a caller with a response to give never hangs."""
    if not session:
        return
    pdir = private_dir(root or (state_dir() / "progress"))
    name = safe_name(session)
    with open(pdir / f"{name}.lock", "a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX if until is None else fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= until:
                    raise TimeoutError("progress lock busy") from None
                time.sleep(0.05)
        with open(pdir / f"{name}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "boundary"}) + "\n")
        os.chmod(pdir / f"{name}.jsonl", 0o600)


def safe_name(value: str) -> str:
    from .inbox import safe_name as _safe
    return _safe(value)


def _parts(data: dict):
    """(icon, description, tool input) shared by label() and detail()."""
    name = data.get("tool_name") or "?"
    inp = data.get("tool_input") or {}
    if not isinstance(inp, dict):
        inp = {}
    icon = {"Bash": "⚙️", "Read": "📖", "Edit": "✏️", "Write": "📝", "Agent": "🤖", "WebSearch": "🔎",
            "WebFetch": "🌐", "Grep": "🔍", "Glob": "🔍"}.get(name, "•")
    text = inp.get("description") or ""
    if not text and name == "Bash" and isinstance(inp.get("command"), str):
        text = " ".join(hide_signin(inp["command"]).split()[:4])[:60]  # hidden first: a cut link is no longer one  # never the bare tool name, which got translated
    if not text and inp.get("file_path"):
        text = os.path.basename(str(inp["file_path"]))
    if not text and name.startswith("mcp__"):
        text = name.split("__")[-1].replace("_", " ")
    return icon, text or name, inp


def worker_of(name: str, inp: dict, delegates: Optional[list] = None) -> str:
    """Vendor and model of a delegated job, or "" for ordinary work: a Claude subagent, a Codex
    command, or any tool the config lists under `delegates` (matched in a Bash command line)."""
    lang = get_lang()
    if name == "Agent":
        who = "Claude " + str(inp.get("model") or t("progress_default", lang=lang))
        kind = inp.get("subagent_type")
        return who + (f" · {kind}" if isinstance(kind, str) and kind.strip() else "")
    cmd = str(inp.get("command") or "") if name == "Bash" else ""
    if not cmd:
        return ""
    match = re.search(r"\bcodex\s+exec\b.*?-m\s+(\S+)", cmd)
    if match:
        return "Codex " + match.group(1)
    for spec in delegates or ():
        if isinstance(spec, dict) and spec.get("match") and spec["match"] in cmd:
            return str(spec.get("label") or "")
    return "Codex" if "codex exec" in cmd else ""


def hide_signin(text: str, links: Optional[list] = None) -> str:
    """A Claude sign-in link never shows in the progress card (redaction cut its state and challenge,
    and the card cuts long lines with "…"): a short note stands in, and the whole link is added to
    `links` so the owner gets it as a message of its own (see the "link" rows)."""
    from .creds import extract_signin
    text, found = extract_signin(text, t("progress_signin_notice", lang=get_lang()))
    if links is not None:
        links.extend(x for x in found if x not in links)
    return text


def _hide_in(value, links: list):
    """`hide_signin` on every string inside a tool response (a string, or nested lists and dicts)."""
    if isinstance(value, str):
        return hide_signin(value, links)
    if isinstance(value, list):
        return [_hide_in(v, links) for v in value]
    if isinstance(value, dict):
        return {k: _hide_in(v, links) for k, v in value.items()}
    return value


def label(data: dict, delegates: Optional[list] = None) -> str:
    """A short, content-free line for one tool call (live progress)."""
    icon, text, inp = _parts(data)
    text = hide_signin(text)
    from .creds import redact
    name = data.get("tool_name") or "?"
    lang = get_lang()
    if name == "TodoWrite" and isinstance(inp.get("todos"), list):
        todos = [t for t in inp["todos"] if isinstance(t, dict)]
        done = sum(t.get("status") == "completed" for t in todos)
        marks = {"completed": "✅", "in_progress": "⏳"}
        todo_title = t("todo_plan", lang=lang)
        return redact(f"📋 {todo_title} ({done}/{len(todos)})\n" + "\n".join(
            f"{marks.get(t.get('status'), '⬜')} {str(t.get('content') or '')[:80]}" for t in todos[:12]))
    who = worker_of(name, inp, delegates)
    if who:
        bg_suf = t("progress_bg_suffix", lang=lang)
        return redact(f"🤖 {text} [{who} · {bg_suf}]")[:180]
    agent = agent_name(data)
    if agent:  # a background agent's own action says whose it is
        return redact(f"🤖 [{agent}] {text}")[:110]
    return redact(f"{icon} {text}")[:90]


def agent_name(data: dict) -> str:
    """Description and model of the subagent that made this tool call, from its .meta.json."""
    aid, transcript = data.get("agent_id"), data.get("transcript_path")
    if not aid or not transcript or not re.fullmatch(r"[A-Za-z0-9_-]+", str(aid)):
        return ""
    meta_path = os.path.join(os.path.splitext(str(transcript))[0], "subagents", f"agent-{aid}.meta.json")
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return str(data.get("agent_type") or "agent")[:40]
    if not isinstance(meta, dict):
        return ""
    return " · ".join(str(x) for x in (meta.get("description"), str(meta.get("model") or "").title()) if x)[:50]


def detail(data: dict, links: Optional[list] = None) -> str:
    """The action's command, path, pattern, query or url for the progress card, secrets hidden by creds.redact plus long tokens and bearer values. '' when it
    adds nothing to the description."""
    _, text, inp = _parts(data)
    out = (inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("query")
           or inp.get("url") or inp.get("prompt") or "")
    if not isinstance(out, str) or not out.strip():
        return ""
    same = out.strip() == text
    out = hide_signin(out, links)
    if same:
        return ""
    from .creds import redact_detail
    out = " ".join(redact_detail(out).split())
    return out[:220] + ("…" if len(out) > 220 else "")


BARE_COMMANDS = {"cd", "git", "ls", "cat", "grep", "sed", "python3", "uv", "gh", "find", "npm", "make"}


HEADINGS_CACHE_FILE = "heading-cache.json"


def heading_he(words: str, cfg: Config) -> str:
    """A tool title in Hebrew present tense, describing what Claude is doing ("Read the file" ->
    "קורא את הקובץ"). Gemini with its own prompt, cached."""
    from . import translate
    if not getattr(cfg, "translate", False):
        return words

    cache_path = state_dir() / HEADINGS_CACHE_FILE
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    if words in cache:
        return cache[words]

    he = ""
    engines = getattr(cfg, "engines", [])
    if engines and engines[0] == "gemini" and translate.external_allowed(cfg):
        from .creds import for_outside

        words_clean = for_outside(words)
        prompt = (
            "Translate this short title of an action an AI assistant is doing right now into natural Israeli "
            "Hebrew, present tense, masculine singular, describing what it is doing (e.g. 'Read the failing test' -> "
            "'קורא את הבדיקה שנכשלה', 'Run full test suite' -> 'מריץ את כל הבדיקות'). Keep code, file names, "
            "commands and product names as they are; translate ordinary English words. Hebrew letters only, never Arabic. Output only the Hebrew title.\n\n" + words_clean
        )
        prompt = for_outside(prompt)
        ask = lambda: translate.ask(prompt, cfg).strip()
        he = ask()
        if re.search(r"[\u0600-\u06ff]", he):
            he = ask()
        if re.search(r"[\u0600-\u06ff]", he) or "\n" in he:
            he = ""

    if not he:
        try:
            he = translate.translate_text(words, cfg).strip()
        except Exception:
            he = ""

    if he and "\n" not in he:
        cache[words] = he
        try:
            write_private(cache_path, json.dumps(dict(list(cache.items())[-2000:]), ensure_ascii=False))
        except OSError:
            pass
        return he
    return words


_heading_memo: dict = {}  # path -> (mtime, cache): the heading cache read once per change, not once per row


def heading_cached(words: str, cfg: Config) -> Optional[str]:
    """The heading's translation when it is already known (the heading cache, or the translation cache for a
    target other than Hebrew), without any engine call; None when it is not."""
    if L.base(cfg.target_language) != "he":
        from .translate import cached
        return cached(words, cfg)
    path = state_dir() / HEADINGS_CACHE_FILE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    memo = _heading_memo.get(str(path))
    if memo is None or memo[0] != mtime:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        memo = (mtime, data if isinstance(data, dict) else {})
        _heading_memo[str(path)] = memo
    value = memo[1].get(words)
    return value if isinstance(value, str) and value else None


def translated_heading(row: dict, cfg: Config, cached_only: bool = False, pending: Optional[list] = None) -> dict:
    """The row with its tool heading (the short description after the icon) translated. The command,
    path and output stay exactly as they are. Unchanged when translation is off, not needed or fails
    (translations are cached). `cached_only`: never call an engine (the live card must not wait): a heading
    not translated yet stays in English and its words are added to `pending`, for the background job
    (run_jobs) to translate; the next card edit then shows it in Hebrew."""
    head, sep, extra = str(row.get("l") or "").partition("\n     ↳ ")
    icon, _, words = head.partition(" ")
    tag = ""
    if words.startswith("[") and "] " in words:  # an agent tag stays as is; only the title is translated
        tag, _, words = words.partition("] ")
        tag += "] "
    bare = words.split()[:1] and words.split()[0] in BARE_COMMANDS  # a command shown as the title
    if not cfg.translate or "\n" in head or not words or bare or not L.needs_translation(words, cfg.target_language):
        return row
    try:
        if cached_only:
            done = heading_cached(words, cfg)
            if done is None:
                if pending is not None and words not in pending:
                    pending.append(words)
                return row
        elif L.base(cfg.target_language) == "he":
            done = heading_he(words, cfg)
        else:
            from .translate import translate_text
            done = translate_text(words, cfg).strip()
    except Exception:
        return row
    if not done or "\n" in done:
        return row
    prefix = L.RLM if L.is_rtl(cfg.target_language) else ""
    return dict(row, l=prefix + icon + " " + tag + done + sep + extra)


TAGGED_TOOLS = {"Bash", "Read", "Edit", "Write", "Grep", "Glob", "WebFetch", "WebSearch", "NotebookEdit"}


def tool_tagged(head: str, tool) -> str:
    """'⚙️ Run the tests' -> '⚙️ Bash: Run the tests' for the core tools, so each step reads as one short
    line. The English name is a bidi isolate, so it sits cleanly inside a Hebrew heading."""
    if tool not in TAGGED_TOOLS or "\n" in head:
        return head
    rlm = L.RLM if head.startswith(L.RLM) else ""
    icon, sep, words = head[len(rlm):].partition(" ")
    if not sep or not words or words.startswith("["):
        return head
    return f"{rlm}{icon} \u2068{tool}\u2069: {words}"


def card(row: dict, last: bool) -> str:
    """One action in the live progress message: ▶️ marks the current step, ✔️ a finished one;
    the description in bold, the command/path/pattern/query/url under it in a <code> line."""
    mark = "▶️" if last else "✔️"
    l_text = str(row.get("l") or "")
    if "\n     ↳ " in l_text:
        head, _, extra = l_text.partition("\n     ↳ ")
        d = row.get("d") or extra
    else:
        head = l_text
        d = row.get("d")
    head = tool_tagged(head, row.get("tool"))
    out = f"{mark} <b>{html.escape(head, quote=False)}</b>"
    if d:
        out += f"\n<code>{html.escape(str(d), quote=False)}</code>"
    return out


def append_progress(session: str, data: dict, cfg: Config, root: Optional[Path] = None) -> None:
    """Append this tool call to the session's live progress list; detached child edits the Telegram message."""
    from . import policy
    if not session or not cfg.progress or not cfg.ready or not policy.on():
        return
    if data.get("agent_id") and not setting_on(AGENT_NOTICES):
        # A background agent's own steps stay off the session's card unless the owner turned agent
        # notices on: dozens of them flooded the topic. Its launch still shows.
        return
    pdir = private_dir(root or (state_dir() / "progress"))
    path = pdir / f"{safe_name(session)}.jsonl"
    level = activity_level()
    row = {"kind": "action", "t": time.time(), "l": (label(data, getattr(cfg, "delegates", None)) if cfg.progress_details else str(data.get("tool_name") or "Tool")),
           "tool": data.get("tool_name") or ""}
    links: list = []  # Claude sign-in links met in this call: sent whole, as messages of their own
    d = detail(data, links) if cfg.progress_details else None
    if d:
        row["d"] = d
    from .creds import redact_detail
    value = data.get("tool_response")
    if value is not None and cfg.progress_details:
        # The links go before the JSON: its escapes ("\\n") would sit against a link and hide its start.
        value = _hide_in(value, links)
        value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        lines = redact_detail(value).strip().splitlines()
        most, chars = OUTPUT_FULL if level == "full" else OUTPUT_NORMAL
        preview = "\n".join(lines[:most])
        row["output"] = preview[:chars - 1] + "…" if len(lines) > most or len(preview) > chars else preview
    with open(pdir / f"{safe_name(session)}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            old = [json.loads(x) for x in path.read_text().splitlines()]
        except (OSError, ValueError):
            old = []
        queued = {r.get("uuid") for r in old}
        known = {r.get("url") for r in old if r.get("kind") == "link"}  # already queued for the owner
        from .hooks import read_blocks
        try:
            blocks = read_blocks(Path(data.get("transcript_path") or "").read_text(encoding="utf-8").splitlines()) if cfg.progress_details else []
        except OSError:
            blocks = []
        with open(path, "a", encoding="utf-8") as f:
            for uid, text in blocks:
                if uid not in queued:
                    f.write(json.dumps({"kind": "text", "uuid": uid,
                                        "text": redact_detail(hide_signin(text, links))}, ensure_ascii=False) + "\n")
            if level != "quiet":
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            else:  # quiet: the agent's own words and sign-in links go out; its steps only move the pulse line
                f.write(json.dumps({"kind": "pulse", "t": row["t"]}) + "\n")
            for url in dict.fromkeys(x for x in links if x not in known):
                f.write(json.dumps({"kind": "link", "t": time.time(), "url": url}, ensure_ascii=False) + "\n")
    os.chmod(path, 0o600)
    # One updater at a time is enough: it re-reads the list, so skip the spawn if one started < 3 s ago.
    mark = pdir / f"{safe_name(session)}.spawn"
    try:
        if time.time() - mark.stat().st_mtime < 3:
            return
    except OSError:
        pass
    mark.touch()
    from .hooks import spawn
    spawn(["progress", f"--session={session}"])


def drop_progress(session: str, cfg: Config) -> None:
    """Stop hook: the step is over. Forget the list now (cheap) and delete the Telegram message from
    a detached child."""
    if not session or not cfg.progress or not cfg.ready:
        return
    pdir = state_dir() / "progress"
    if not (pdir / f"{safe_name(session)}.jsonl").exists() and not (pdir / f"{safe_name(session)}.msg").exists():
        return
    from .hooks import spawn
    spawn(["progress", f"--session={session}", "--end"])


def run_send_progress(cfg: Config, session: str, root: Optional[Path] = None, bot=None,
                      throttle: float = 3.0, sleep_fn: Callable[[float], None] = time.sleep,
                      now_fn: Callable[[], float] = time.time, fast: bool = False) -> int:
    """Send or edit the single live progress message in the session's topic, then, with the card's lock
    released, translate what it showed in English (run_jobs). The fast Stop flush leaves that to a detached
    `progress --jobs` run instead."""
    out = _send_progress(cfg, session, root, bot, throttle, sleep_fn, now_fn, fast)
    if session and cfg.ready and getattr(cfg, "translate", False):
        try:
            if fast:
                if jobs_waiting(session, root):
                    from .hooks import spawn
                    spawn(["progress", f"--session={session}", "--jobs"])
            else:
                run_jobs(cfg, session, root, bot)
        except Exception:  # noqa: BLE001 - a translation must never break the card
            pass
    return out


def _send_progress(cfg: Config, session: str, root: Optional[Path], bot, throttle: float,
                   sleep_fn: Callable[[float], None], now_fn: Callable[[], float], fast: bool) -> int:
    """Send or edit the single live progress message in the session's topic."""
    if not session or not cfg.ready:
        return 0
    from .scheduled import suppressed
    if suppressed(session, "progress"):
        return 0  # a muted scheduled task: its steps stay local
    pdir = private_dir(root or (state_dir() / "progress"))
    name = safe_name(session)
    path = pdir / f"{name}.jsonl"
    state_path = pdir / f"{name}.msg"
    lock_path = pdir / f"{name}.lock"

    with open(lock_path, "a") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}

        now = now_fn()
        elapsed = now - state.get("edited", 0.0)
        if elapsed < throttle and throttle > 0:
            sleep_fn(throttle - elapsed)
            now = now_fn()

        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
            rows = [json.loads(x) for x in raw_lines if x.strip()]
        except (OSError, ValueError):
            return 0  # the step already ended or empty

        if not rows:
            return 0

        bot = bot or Bot.from_config(cfg)
        found = place(cfg, session)
        if found is None:
            return 0
        thread, chat = found.thread, found.chat

        if any("kind" in row for row in rows):
            return _send_ordered(cfg, session, rows, state, state_path, thread, bot, now_fn, fast, chat=chat)
        if rows[-1]["t"] <= state.get("shown", 0.0):
            return 0

        mins = int((now - rows[0]["t"]) // 60)
        s = L.strings(cfg.target_language)
        prefix = L.RLM if L.is_rtl(cfg.target_language) else ""
        header = (f"⏳ <b>{s['progress_working']}</b> {len(rows)} {s['progress_actions']} · "
                  f"{mins} {s['progress_mins']}\n━━━━━━━━━━━━\n")

        def body(items):
            out = "\n\n".join(card(r, i == len(items) - 1) for i, r in enumerate(items))
            if len(rows) > len(items):
                out = s["progress_hidden"] + "\n\n" + out
            return out

        shown = rows[-8:]
        # Telegram allows 4096 characters; the whole card stays below 3600 (oldest actions drop first).
        while shown and len(prefix + header + body(shown)) >= 3600:
            shown.pop(0)
        text = prefix + header + body(shown)

        msg_id = state.get("id")
        if msg_id:
            try:
                flood.optional_call(bot, "editMessageText", {
                    "chat_id": chat,
                    "message_id": msg_id,
                    "text": text,
                    "parse_mode": "HTML",
                })
            except TelegramError:
                pass
        else:
            try:
                msg = flood.optional_call(bot, "sendMessage", {
                    "chat_id": chat,
                    "message_thread_id": thread,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_notification": True,
                })
                state["id"] = msg.get("message_id")
            except TelegramError:
                pass

        state.update(edited=now_fn(), shown=rows[-1]["t"])
        write_private(state_path, json.dumps(state))
    return 0


PULSE_EVERY = 45.0  # seconds: at the quiet level the working emoji is sent again at most this often
MIN_EDIT = 10.0  # seconds: one edit of a session's card at most this often (the chat is shared)


KEEP_FULL = 3  # the newest steps keep their command and output; older ones fold into one line each
KEEP_FULL_VERBOSE = 6  # the same, when the owner asked to see everything (activity level "full")
OUTPUT_NORMAL = (2, 160)  # lines and characters of a step's output kept for the card
OUTPUT_FULL = (5, 420)


def compact_card(item: str) -> str:
    """One finished step as a single short line: its heading only (no command, no output), no bold."""
    head = re.split(r"\n<code>|\n<blockquote", item, maxsplit=1)[0]
    head = head.split("\n", 1)[0]  # a checklist keeps its title line
    return re.sub(r"</?b>", "", head).strip()


def card_text(entry, cfg: Config, pending: Optional[list] = None) -> str:
    """One card entry as shown: a step saved as {"row": ...} is rendered now, with its heading in Hebrew when
    the translation is already known (cache only, never an engine); an older entry is already a string."""
    if isinstance(entry, str):
        return entry
    row = entry.get("row") if isinstance(entry, dict) else None
    if not isinstance(row, dict):
        return ""
    item = card(translated_heading(row, cfg, cached_only=True, pending=pending), False)
    if row.get("output"):
        item += "\n<blockquote expandable>" + html.escape(str(row["output"]), quote=False) + "</blockquote>"
    return item


def card_texts(cards, cfg: Config, pending: Optional[list] = None) -> list:
    return [card_text(c, cfg, pending) for c in cards or []]


def render_cards(ui_lang: str, cards, now: float) -> str:
    """The live activity message: the newest steps in full, every older step folded into one collapsed
    quote of one-line entries, so a long turn stays one short message instead of a screen dump."""
    title = t("progress_action_updated", lang=ui_lang)
    prefix = L.RLM if ui_lang == "he" else ""
    cards = list(cards)
    keep = KEEP_FULL_VERBOSE if activity_level() == "full" else KEEP_FULL
    older, recent = (cards[:-keep] if len(cards) > keep else []), cards[-keep:]
    body = "\n\n".join(recent)
    if older:
        lines = "\n".join(compact_card(c) for c in older)
        label = t("progress_earlier_steps", lang=ui_lang, n=len(older))
        body = f"{label}\n<blockquote expandable>{lines}</blockquote>\n\n" + body
    return prefix + f"⏳ <b>{title}</b> {time.strftime('%H:%M:%S', time.localtime(now))}\n━━━━━━━━━━━━\n" + body


def _not_found(exc: Exception) -> bool:
    text = str(exc).lower()
    return "message to edit not found" in text or "message can't be edited" in text or "message_id_invalid" in text


def _send_link(cfg, bot, thread, url, ui_lang, chat: Optional[int] = None) -> None:
    """The owner's Claude sign-in link, whole, as a message of its own: a tappable label (like the account
    login link). Raises TelegramError when it could not be sent."""
    chat = home_chat(cfg) if chat is None else chat
    bot.call("sendMessage", {
        "chat_id": chat, "message_thread_id": thread,
        "text": t("progress_signin_link", lang=ui_lang, url=html.escape(str(url), quote=True)),
        "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}})


def _send_ordered(cfg, session, rows, state, state_path, thread, bot, now_fn, fast, chat: Optional[int] = None):
    """Show the turn in order (text, tools, text) with as few messages as possible.

    Tool calls only add lines to the session's current card in `state`; the card is pushed to Telegram
    (one edit, or one send for a new card) when the turn's text moves on, when it would grow too long,
    and otherwise at most every MIN_EDIT seconds. Everything here is optional traffic (flood.py): when
    the shared gate or budget says no, the card stays `dirty` and goes out on a later run.
    `chat`: the session's chat (None = the home chat)."""
    chat = home_chat(cfg) if chat is None else chat
    from .hooks import _seen, _mark
    from .topics import set_state
    set_state(cfg, session, "busy", bot=bot, optional=True)
    if "cursor" not in state:
        state = {"cursor": sum(1 for row in rows if row.get("t", 0) and row["t"] <= state.get("shown", 0)), "cards": []}
    changed = False
    def persist():
        write_private(state_path, json.dumps(state, ensure_ascii=False))
    ui_lang = getattr(cfg, "ui_language", "en")
    heads: list = []  # step headings not translated yet: the background job translates them (run_jobs)
    texts: list = []  # interim texts shown in English: the background job edits them to their translation

    def render(cards):
        return render_cards(ui_lang, card_texts(cards, cfg, heads), now_fn())

    def flush(force: bool) -> bool:
        """Push the card in `state` to Telegram. True when nothing is left unshown."""
        nonlocal changed
        if not state.get("dirty"):
            return True
        has_id = bool(state.get("id"))
        if has_id and not force and now_fn() - state.get("flushed", 0.0) < MIN_EDIT:
            return False
        params = {"chat_id": chat, "text": render(state.get("cards", [])), "parse_mode": "HTML"}
        # A forced flush (before text, at Stop) keeps the order but still obeys the closed gate.
        try:
            if has_id:
                params["message_id"] = state["id"]
                try:
                    flood.optional_call(bot, "editMessageText", params, budget=not force)
                except TelegramError as exc:
                    if _not_found(exc):
                        state.pop("id", None)  # the card is gone: the next flush sends a new one
                        persist()
                        return False
                    if "not modified" not in str(exc).lower():
                        raise
            else:
                params.update(message_thread_id=thread, disable_notification=True)
                state["id"] = flood.optional_call(bot, "sendMessage", params, budget=not force).get("message_id")
                changed = True
        except TelegramError:
            return False
        state["dirty"] = False
        state["flushed"] = now_fn()
        persist()
        return True

    pulse = False  # a hidden step (quiet level) was met: the working emoji is the sign of life
    for i in range(state["cursor"], len(rows)):
        row = rows[i]
        try:
            if row.get("kind") == "pulse":
                pulse = True
                state["cursor"] = i + 1
                persist()
                continue
            if row.get("kind") == "boundary" or (row.get("kind") == "text" and row["uuid"] not in _seen(session)):
                if not flush(force=True):
                    break  # keep the order: the card first, then the text; retried later
            if row.get("kind") == "link":
                # A sign-in link goes whole, as a message of its own (the card shows only a note). It never
                # waits for the card: a card that cannot be shown must not hold back the link.
                carded = flush(force=True)
                _send_link(cfg, bot, thread, row["url"], ui_lang, chat)
                changed = True
                if carded:  # the card is out, so the next tools start a new one below the link
                    state.pop("id", None)
                    state["cards"] = []
                    state.pop("dirty", None)
            elif row.get("kind") == "boundary":
                state.pop("id", None)
                state["cards"] = []
                state.pop("dirty", None)
            elif row.get("kind") == "text":
                if row["uuid"] not in _seen(session):
                    state.pop("id", None)
                    state["cards"] = []
                    state.pop("dirty", None)
                    # Instant: the text goes out as Claude wrote it (redacted), and is then edited in place
                    # to its translation. The exact body is persisted for offset retries.
                    text = state.get("text_body")
                    if text is None:
                        text = interim_original(row["text"])
                        state["text_body"] = text
                        state["text_ids"] = []
                        persist()
                    later = will_translate(text, cfg)  # even in the fast Stop flush: translated in the background
                    step = INTERIM_CHUNK if later else 3500
                    start = state.get("text_offset", 0)
                    while start < len(text):
                        prefix = L.RLM if L.is_rtl(cfg.target_language) and not L.needs_translation(text, cfg.target_language) else ""
                        piece = text[start:start + step]
                        sent = flood.optional_call(bot, "sendMessage", {
                            "chat_id": chat, "message_thread_id": thread,
                            "text": prefix + piece, "disable_notification": True,
                            "link_preview_options": {"is_disabled": True}}, budget=False)
                        changed = True
                        start += step
                        state["text_offset"] = start
                        mid = sent.get("message_id") if isinstance(sent, dict) else None
                        state.setdefault("text_ids", []).append([mid, piece])
                        persist()
                    if later:
                        # Never wait for the translation here (the next steps would wait with it): the
                        # background job edits these messages to it (run_jobs), and keeps its own progress.
                        texts.append({"ids": state.get("text_ids") or [], "chat": chat, "thread": thread})
                    _mark(session, [row["uuid"]])
                state.pop("text_body", None)
                state.pop("text_offset", None)
                state.pop("text_ids", None)
                state.pop("tr", None)
            else:
                item = {"row": {k: row[k] for k in ("l", "d", "tool", "output") if k in row}}
                cards = state.get("cards", []) + [item]
                if len(render(cards)) > 3500 and state.get("cards"):
                    if not flush(force=True):
                        break  # the full card goes out before a new one starts
                    state.pop("id", None)
                    cards = [item]
                state["cards"] = cards
                state["dirty"] = True
            state["cursor"] = i + 1
            persist()
        except TelegramError:
            break  # retry this event later, never skip a failed text chunk
    flush(force=fast)
    if changed:
        set_state(cfg, session, "bottom", bot=bot, optional=True)
        state["pulsed"] = now_fn()
        persist()
    elif pulse and now_fn() - state.get("pulsed", 0.0) >= PULSE_EVERY:
        # Quiet level: the steps stay hidden, so the working emoji is sent again now and then. A long
        # silent turn must never look stuck.
        set_state(cfg, session, "bottom", bot=bot, optional=True)
        state["pulsed"] = now_fn()
        persist()
    if heads or texts:
        queue_jobs(session, heads, texts, root=state_path.parent)
    return 0


def _final_flush(cfg: Config, session: str, state: dict, bot=None) -> None:
    """One last try to show the card still pending when the step ends: gate check only, no budget."""
    try:
        bot = bot or Bot.from_config(cfg)
        params = {"chat_id": chat_of(cfg, session), "text": render_cards(getattr(cfg, "ui_language", "en"),
                                                                          card_texts(state["cards"], cfg), time.time()),
                  "parse_mode": "HTML"}
        if state.get("id"):
            params["message_id"] = state["id"]
            flood.optional_call(bot, "editMessageText", params, budget=False)
        else:
            found = place(cfg, session)
            if found is None:
                return
            params.update(message_thread_id=found.thread, disable_notification=True)
            flood.optional_call(bot, "sendMessage", params, budget=False)
    except Exception:
        pass  # a card that cannot be shown now is dropped with the step


def _send_pending_links(cfg: Config, session: str, rows: list, bot=None) -> None:
    if not rows:
        return
    try:
        bot = bot or Bot.from_config(cfg)
        found = place(cfg, session)
        if found is None:
            return
        for url in dict.fromkeys(r.get("url") for r in rows):
            _send_link(cfg, bot, found.thread, url, getattr(cfg, "ui_language", "en"), found.chat)
    except Exception:
        pass  # the step is over: nothing more can be done


def run_end_progress(cfg: Config, session: str, root: Optional[Path] = None, bot=None) -> int:
    """Step over: delete the live progress message and clean up state."""
    if not session:
        return 0
    pdir = private_dir(root or (state_dir() / "progress"))
    name = safe_name(session)
    path = pdir / f"{name}.jsonl"
    state_path = pdir / f"{name}.msg"
    with open(pdir / f"{name}.lock", "a") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)  # wait for a send in flight, so its message id is known
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = None
        try:
            pending = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        except (OSError, ValueError):
            pending = []
        try:
            path.unlink()
        except OSError:
            pass
        if cfg.ready:  # a sign-in link not sent yet is not lost with the step (the card flush may have failed)
            seen = state or {}  # no state file: the sender never ran, so nothing was sent
            cursor = seen["cursor"] if "cursor" in seen else sum(
                1 for r in pending if r.get("t", 0) and r["t"] <= seen.get("shown", 0))
            _send_pending_links(cfg, session, [r for r in pending[cursor:] if r.get("kind") == "link"], bot)
        if state is None:
            return 0
        if "cursor" in state and state.get("dirty") and state.get("cards") and cfg.ready:
            _final_flush(cfg, session, state, bot)  # the last tool lines must not be lost to a closed gate
        try:
            state_path.unlink()
        except OSError:
            pass
    msg_id = state.get("id") if "cursor" not in state else None
    if msg_id and cfg.ready:
        bot = bot or Bot.from_config(cfg)
        try:
            bot.call("deleteMessage", {
                "chat_id": chat_of(cfg, session),
                "message_id": msg_id,
            })
        except TelegramError:
            pass
    return 0
