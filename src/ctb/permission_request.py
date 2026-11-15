"""Claude Code permission prompts answered from the phone, through Claude Code's PermissionRequest hook.

Claude Code runs the PermissionRequest hook (`ctb hook-permission`) when it is about to ask for a tool permission,
while it shows its own prompt in the terminal. The hook gets the exact request on stdin (session_id, tool_name,
tool_input, permission_mode) and may answer it on stdout ({"hookSpecificOutput": {"hookEventName":
"PermissionRequest", "decision": {"behavior": "allow" | "deny"}}}). Whichever answers first wins: the owner at the
computer, or the hook. Nothing reads the pane and no key is pressed.

The hook posts a card with the exact request (the whole tool_input as JSON, every field, as Claude Code sent it) to
the session's topic and the same text to a Cockpit decision, then waits for the owner's answer for at most WAIT
seconds (below the hook's own timeout). No answer means no output: the terminal prompt stays as it is, and nothing
is ever allowed by default. An answer allows or denies exactly that one request (its own random id); it never adds
a permission rule ("don't ask again" is never offered).

Who may answer:
* "No": the card's No button, a typed "no" (as a reply to the card, or while it is the topic's only open request),
  or the Cockpit.
* "Yes": only an explicit answer to the exact visible request: a typed "yes" sent as a Telegram reply to that card,
  after the card, used once; or the Cockpit. There is no Yes button.
* A request the phone cannot show exactly (too long, a credential the bridge redacts, or any character outside
  plain visible text: letters, digits, punctuation, symbols, space and newline) can only be declined from the phone.
* Never a Telegram widget, a device token, a team member, a voice note, a forwarded message or an edit.

The card closes when the request is answered, when the wait ends, and (the watcher, `sweep`) when the hook process
is gone because the request was answered at the computer or Claude Code cancelled it.
"""
from __future__ import annotations

import fcntl
import html
import json
import os
import re
import secrets
import signal
import time
from typing import Optional, Tuple

from .config import Config, home_chat, private_dir, state_dir, write_private
from .i18n import t

STATE_FILE = "permission-requests.json"
HOOK_TIMEOUT = 300        # seconds: the hook's timeout in the settings (install.desired_hooks)
WAIT = HOOK_TIMEOUT - 30  # the hook gives up well before Claude Code would kill it
POLL = 0.25
KEEP_CLOSED = 30
SHOWN_CHARS = 1500        # a request longer than this is never shown in full, so never approved from the phone
SHOWN_HTML = 3000         # and its HTML-escaped form must fit a Telegram message with the card's words
CLIPPED_CHARS = 1000      # what a card shows of a request it cannot show in full
ID_RE = re.compile(r"[0-9a-f]{16}")
CALLBACK = re.compile(r"pq:n:([0-9a-f]{16})")  # the card's only button: No
YES_WORDS = {"yes", "y", "כן", "אשר", "מאשר", "אישור"}
NO_WORDS = {"no", "n", "לא", "דחה", "ביטול"}
# Sessions that really show a prompt to a person. Never dontAsk (assistant sessions: every prompt is refused, and a
# hook allow would bypass that) and never auto mode (its classifier decides).
PERMISSION_MODES = {"default", "acceptEdits", "plan", "bypassPermissions", ""}


# --- what the request is --------------------------------------------------------------------------

def visible(text: str) -> bool:
    """Plain printable ASCII and the newline only. Unicode has hidden characters in every category (bidi marks,
    variation selectors, blank letters such as U+1160), so anything else is shown escaped and can only be declined
    from the phone."""
    return all(c == "\n" or " " <= c <= "~" for c in text)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def describe(tool_input) -> Tuple[str, bool]:
    """(the request as shown, shown exactly and in full). The whole tool_input as JSON, every field, sorted keys.
    A request with an invisible character is shown with every non-ASCII character escaped, so nothing hides."""
    from .creds import redact, redact_channel
    try:
        exact = json.dumps(tool_input, ensure_ascii=False, indent=1, sort_keys=True)
    except (TypeError, ValueError):
        return repr(tool_input)[:CLIPPED_CHARS], False
    plain = all(visible(s) for s in _strings(tool_input)) and visible(exact)
    shown = exact if plain else json.dumps(tool_input, ensure_ascii=True, indent=1, sort_keys=True)
    safe = redact(shown)
    # Every surface (topic card, Cockpit) must show exactly what would run: if any redaction a surface applies would
    # change a single character, the request can only be declined from the phone.
    complete = (plain and safe == shown and redact_channel(shown) == shown and len(shown) <= SHOWN_CHARS
                and len(html.escape(shown, quote=False)) <= SHOWN_HTML)
    return (safe if complete else safe[:CLIPPED_CHARS] + ("\n…" if len(safe) > CLIPPED_CHARS else "")), complete


def tool_exact(tool: str) -> bool:
    """The tool's name is part of what runs: it must be plain and survive every surface's redaction unchanged."""
    from .creds import redact, redact_channel
    return visible(tool) and redact(tool) == tool and redact_channel(tool) == tool


def surfaces_exact(*shown: str) -> bool:
    """Every string a surface shows is unchanged by every redaction a surface applies."""
    from .creds import redact, redact_channel
    return all(redact(x) == x and redact_channel(x) == x for x in shown)


# --- state ----------------------------------------------------------------------------------------

def _path():
    return private_dir(state_dir()) / STATE_FILE


def _update(fn):
    with open(private_dir(state_dir()) / "permission-requests.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        out = fn(data)
        closed = sorted((k for k, r in data.items() if isinstance(r, dict) and r.get("state") != "open"),
                        key=lambda k: data[k].get("closed_at", 0), reverse=True)
        for gone in closed[KEEP_CLOSED:]:
            del data[gone]
        write_private(_path(), json.dumps(data, ensure_ascii=False))
        return out


def _all() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get(ident: str) -> dict:
    rec = _all().get(ident) if isinstance(ident, str) else None
    return rec if isinstance(rec, dict) else {}


def open_for(session: str, now: Optional[float] = None) -> list:
    """The open requests of a session, oldest first."""
    now = time.time() if now is None else now
    return sorted(((k, r) for k, r in _all().items() if isinstance(r, dict) and r.get("session") == session
                   and r.get("state") == "open" and now < r.get("deadline", 0)), key=lambda kr: kr[1].get("created", 0))


def _alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def answer(ident: str, choice: str, via: str, message: Optional[int] = None, now: Optional[float] = None) -> str:
    """Record the owner's answer once: "ok", "closed" (answered, expired, gone, or this message already answered
    one), or "refused" (a Yes this way may not give). `via`: "button" (the card's No), "reply" (the owner's words
    as a reply to the card), "typed" (the owner's words, no reply) or "cockpit"."""
    now = time.time() if now is None else now
    if choice not in ("yes", "no") or via not in ("button", "reply", "typed", "cockpit"):
        return "refused"
    if choice == "yes" and via not in ("reply", "cockpit"):
        return "refused"

    def put(data):
        rec = data.get(ident)
        if (not isinstance(rec, dict) or rec.get("state") != "open" or now >= rec.get("deadline", 0)
                or not _alive(rec.get("pid"))):
            return "closed"  # a hook that is gone can no longer deliver it: the answer would be a false "approved"
        if message is not None and any(isinstance(r, dict) and r.get("chat") == rec.get("chat")
                                       and r.get("answer_message") == message for r in data.values()):
            return "closed"  # one message answers one request, once
        if choice == "yes" and not rec.get("complete"):
            return "refused"
        rec.update(state="answered", choice=choice, via=via, closed_at=now,
                   **({} if message is None else {"answer_message": message}))
        return "ok"
    return _update(put)


def _close(ident: str, state: str, now: Optional[float] = None) -> Optional[dict]:
    """Close an open request (expired or gone); the record as it was, or None when it was not open."""
    now = time.time() if now is None else now

    def put(data):
        rec = data.get(ident)
        if not isinstance(rec, dict) or rec.get("state") != "open":
            return None
        data[ident] = dict(rec, state=state, closed_at=now)
        return rec
    return _update(put)


# --- the card -------------------------------------------------------------------------------------

def card_text(rec: dict, lang: str) -> str:
    return (f"🛡️ <b>{html.escape(t('pq_title', lang=lang))}</b>\n{html.escape(t('pq_intro', lang=lang))}\n"
            f"<pre>{html.escape(rec.get('tool', ''), quote=False)}</pre>\n"
            f"<pre>{html.escape(rec.get('text', ''), quote=False)}</pre>\n"
            + html.escape(t("pq_reply_yes" if rec.get("complete") else "pq_decline_only", lang=lang), quote=False))


def card_markup(ident: str, lang: str) -> dict:
    return {"inline_keyboard": [[{"text": t("pq_btn_no", lang=lang), "callback_data": f"pq:n:{ident}",
                                  "style": "danger"}]]}


def _edit(cfg, bot, rec: dict, text: str) -> None:
    if not isinstance(rec.get("message_id"), int):
        return
    try:
        bot.call("editMessageText", {"chat_id": rec.get("chat") or home_chat(cfg), "message_id": rec["message_id"],
                                     "text": text, "reply_markup": {"inline_keyboard": []}})
    except Exception:
        pass


def _settle_decision(rec: dict, state: str, choice: str = "") -> None:
    if rec.get("decision"):
        from . import decisions
        try:
            decisions.settle_permission(rec["decision"], state, choice)
        except Exception:
            pass


def finish(cfg, bot, ident: str) -> None:
    """After an answer: the card says what was answered, the Cockpit decision closes."""
    rec = get(ident)
    if rec.get("state") != "answered":
        return
    lang = getattr(cfg, "ui_language", None) or "en"
    _edit(cfg, bot, rec, t("pq_done_yes" if rec.get("choice") == "yes" else "pq_done_no", lang=lang))
    _settle_decision(rec, "answered", rec.get("choice", ""))


def retire(cfg, bot, rec: dict, key: str) -> None:
    lang = getattr(cfg, "ui_language", None) or "en"
    _edit(cfg, bot, rec, t(key, lang=lang))
    _settle_decision(rec, "cancelled")


# --- the hook -------------------------------------------------------------------------------------

def _decision(choice: str) -> dict:
    out = {"behavior": "allow"} if choice == "yes" else {"behavior": "deny", "message": t("pq_denied_note", lang="en")}
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": out}}


def run_hook(stdin_text: str, cfg: Optional[Config] = None, bot=None, wait: float = WAIT, out=None) -> int:
    """`ctb hook-permission`. Prints a decision only for the owner's answer; anything else prints nothing (the
    terminal prompt stays). Always exits 0."""
    import sys
    out = out or sys.stdout
    ident = None
    try:
        data = json.loads(stdin_text or "{}")
        if not isinstance(data, dict):
            return 0
        session, tool = data.get("session_id"), data.get("tool_name")
        if (data.get("hook_event_name") != "PermissionRequest" or not isinstance(session, str) or not session
                or not isinstance(tool, str) or not tool or str(data.get("permission_mode") or "") not in PERMISSION_MODES):
            return 0
        if cfg is None:
            from .hooks import load_config
            cfg = load_config()
        if not cfg.ready:
            return 0
        from .topics import place
        found = place(cfg, session)
        if found is None or not isinstance(found.thread, int):
            return 0
        from .telegram import Bot
        bot = bot or Bot.from_config(cfg)
        lang = getattr(cfg, "ui_language", None) or "en"
        text, complete = describe(data.get("tool_input"))
        tool_shown = tool if visible(tool) and len(tool) <= 120 else json.dumps(tool)[:120]
        complete = complete and tool_shown == tool and tool_exact(tool)
        now = time.time()
        ident = secrets.token_hex(8)
        rec = {"session": session, "tool": tool_shown, "text": text, "complete": complete, "created": now,
               "deadline": now + wait, "pid": os.getpid(), "state": "open", "chat": found.away, "thread": found.thread}
        # Each surface filters the whole string it shows, not its parts: the Cockpit's "tool\ntext" and the topic
        # card. If any redaction would change either joined string, the phone may only decline.
        rec["complete"] = complete = complete and surfaces_exact(tool_shown + "\n" + text, card_text(rec, lang))
        _update(lambda d: d.__setitem__(ident, rec))
        params = {"chat_id": found.chat, "message_thread_id": found.thread, "text": card_text(rec, lang),
                  "parse_mode": "HTML", "reply_markup": card_markup(ident, lang), "disable_notification": False}
        try:
            mid = (bot.call("sendMessage", params) or {}).get("message_id")
        except Exception:
            mid = None
        if not isinstance(mid, int):
            _close(ident, "unsent")
            return 0
        decision = None
        try:
            from . import decisions
            decision = decisions.create(session, found.chat, found.thread, t("pq_decision_title", lang=lang),
                                        t("pq_decision_detail" if complete else "pq_decline_only", lang=lang),
                                        lang=lang, perm=ident, request=tool_shown + "\n" + text,
                                        decline_only=not complete)["id"]
        except Exception:
            decision = None
        sent_at = time.time()
        _update(lambda d: d.get(ident, {}).update(message_id=mid, decision=decision, sent_at=sent_at))

        def gone(*_a):
            raise SystemExit(0)
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            try:
                signal.signal(sig, gone)
            except (ValueError, OSError):
                pass
        while time.time() < rec["deadline"]:
            cur = get(ident)
            if cur.get("state") == "answered" and cur.get("choice") in ("yes", "no"):
                out.write(json.dumps(_decision(cur["choice"])))
                out.flush()
                finish(cfg, bot, ident)
                return 0
            if cur.get("state") != "open":
                return 0
            time.sleep(POLL)
        closed = _close(ident, "expired")
        if closed:
            retire(cfg, bot, dict(closed, message_id=mid, decision=decision), "pq_expired")
    except BaseException as exc:  # noqa: BLE001 - nothing may escape into Claude Code; never an allow
        if ident:
            try:
                closed = _close(ident, "gone")
                if closed and cfg is not None and bot is not None:
                    retire(cfg, bot, closed, "pq_gone")
            except Exception:
                pass
        if isinstance(exc, KeyboardInterrupt):
            return 0
    return 0


# --- the owner's answers --------------------------------------------------------------------------

def button(q: dict, cfg: Config, bot, m: dict, session: str, data: str) -> str:
    """The card's No button: owner only, this session's own topic and card."""
    from . import inbox as I
    from .topics import place
    lang = getattr(cfg, "ui_language", None) or "en"
    match = CALLBACK.fullmatch(data or "")
    sender = q.get("from") or {}
    found = place(cfg, session)
    rec = get(match[1]) if match else {}
    if (not match or sender.get("id") != cfg.owner_user_id or sender.get("is_bot") or not found
            or found.chat != (m.get("chat") or {}).get("id") or found.thread != m.get("message_thread_id")
            or rec.get("session") != session or rec.get("message_id") != m.get("message_id")):
        I._answer_button(bot, q, t("toast_not_available", lang=lang))
        return "ignored"
    result = answer(match[1], "no", "button")
    I._answer_button(bot, q, t("pq_toast_sent" if result == "ok" else "toast_question_closed", lang=lang))
    if result == "ok":
        finish(cfg, bot, match[1])
    return "permission-" + result


def _word(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def typed(cfg: Config, bot, m: dict, session: str) -> Optional[str]:
    """The owner's typed yes or no for a request of this session. A yes counts only as a Telegram reply to that
    request's own card, sent after the card, once. A no also counts while the request is the topic's only one.
    None: not an answer to a request (the message goes on as usual)."""
    from . import inbox as I
    from .dialogs import _say
    text = m.get("text")
    if not isinstance(text, str) or m.get("_forwarded") or m.get("edit_date"):
        return None
    word = _word(text)
    choice = "yes" if word in YES_WORDS else "no" if word in NO_WORDS else None
    if choice is None:
        return None
    chat, thread = I._chat_arg(cfg, m), m.get("message_thread_id")
    lang = getattr(cfg, "ui_language", None) or "en"
    here = [(k, r) for k, r in open_for(session) if r.get("thread") == thread and r.get("chat") == chat]
    if not here:
        return None
    reply = (m.get("reply_to_message") or {}).get("message_id")
    mid, date = m.get("message_id"), m.get("date")
    target = [(k, r) for k, r in here if isinstance(reply, int) and r.get("message_id") == reply]
    if target:
        ident, rec = target[0]
        if not (isinstance(mid, int) and mid > rec["message_id"] and isinstance(date, (int, float))
                and date >= int(rec.get("sent_at", rec.get("created", 0)))):
            return None  # not a fresh message written after the card
        via = "reply"
    elif choice == "no" and len(here) == 1:
        ident, via = here[0][0], "typed"
    else:
        _say(bot, cfg, thread, t("pq_reply_needed", lang=lang), chat=chat)  # say which card: reply to it
        return "permission-typed-refused"
    result = answer(ident, choice, via, message=mid if isinstance(mid, int) else None)
    if result == "ok":
        finish(cfg, bot, ident)
        I.react(bot, I._reply_chat(cfg, m), m, "\U0001F44D", optional=True)
    else:
        _say(bot, cfg, thread, t("pq_decline_only" if result == "refused" else "toast_question_closed", lang=lang),
             chat=chat)
    return "permission-typed-" + result


def decide(cfg: Config, bot, ident: str, choice: str) -> str:
    """The owner's Cockpit answer (an authenticated panel request; never a widget or a device token)."""
    from . import decisions
    rec = next((r for r in decisions.open_decisions() if r["id"] == ident and r.get("perm")), None)
    if rec is None:
        return "closed"
    result = answer(rec["perm"], choice, "cockpit")
    if result == "ok":
        finish(cfg, bot, rec["perm"])
    elif result == "closed":
        decisions.settle_permission(ident, "cancelled")
    return result


# --- the watcher ----------------------------------------------------------------------------------

def sweep(cfg: Config, bot, now: Optional[float] = None) -> int:
    """Close the cards whose hook is gone (answered at the computer, or cancelled by Claude Code) or past their
    wait. Returns how many were closed."""
    now = time.time() if now is None else now
    n = 0
    for ident, rec in list(_all().items()):
        if not isinstance(rec, dict) or rec.get("state") != "open":
            continue
        if _alive(rec.get("pid")) and now < rec.get("deadline", 0) + 10:
            continue
        closed = _close(ident, "gone", now)
        if closed:
            retire(cfg, bot, closed, "pq_gone")
            n += 1
    return n
