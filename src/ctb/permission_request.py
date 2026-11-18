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

from . import menus
from .config import Config, home_chat, private_dir, state_dir, write_private
from .i18n import t

STATE_FILE = "permission-requests.json"
HOOK_TIMEOUT = 300        # seconds: the hook's timeout in the settings (install.desired_hooks)
WAIT = HOOK_TIMEOUT - 30  # the hook gives up well before Claude Code would kill it
POLL = 0.25
SWEEP_GRACE = 10         # let a live waiter retire its own expired request
ORPHAN_GRACE = 60
STOP_LOCK_WAIT = 0.5
KEEP_CLOSED = 30
SHOWN_CHARS = 1500        # a request longer than this is never shown in full, so never approved from the phone
SHOWN_HTML = 3000         # and its HTML-escaped form must fit a Telegram message with the card's words
CLIPPED_CHARS = 1000      # what a card shows of a request it cannot show in full
ID_RE = re.compile(r"[0-9a-f]{16}")
CALLBACK = re.compile(r"pq:(?:n:[0-9a-f]{16}|[qmd]:[0-9a-f]{16}:[0-9a-f]{8}|x:[0-9a-f]{16}|p:[ary]:[0-9a-f]{16})")
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
    if len(shown) > SHOWN_CHARS or len(html.escape(shown, quote=False)) > SHOWN_HTML:
        value = tool_input if isinstance(tool_input, str) else next(
            (tool_input.get(k) for k in ("command", "file_path", "path", "description")
             if isinstance(tool_input, dict) and isinstance(tool_input.get(k), str)), "")
        line = next((line.strip() for line in value.splitlines() if line.strip()), "")
        line = re.sub(r"(?<![\w.])/(?:[^\s/]+/){2,}([^\s/]+)", r"…/\1", line)
        line = json.dumps(line, ensure_ascii=True)[1:-1]
        line = redact_channel(redact(line))
        return line[:180] + ("…" if len(line) > 180 else ""), False
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


def _update(fn, until=None, trim=True):
    with open(private_dir(state_dir()) / "permission-requests.lock", "a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX if until is None else fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    return None
                time.sleep(min(0.05, max(0, until - time.monotonic())))
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        out = fn(data)
        closed = sorted((k for k, r in data.items() if isinstance(r, dict) and r.get("state") != "open"
                         and (r.get("state") not in ("gone", "expired") or r.get("retired"))),
                        key=lambda k: data[k].get("closed_at", 0), reverse=True)
        for gone in closed[KEEP_CLOSED:] if trim else ():
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
    now = time.time() if now is None else now

    def put(data):
        rec = data.get(ident)
        if not isinstance(rec, dict) or rec.get("state") != "open":
            return None
        rec.update(state=state, closed_at=now)
        return dict(rec)
    return _update(put)


def supersede(session: str, agent_id=None) -> None:
    """Mark this session's requests gone; never delay a Stop hook for a busy lock."""
    if not isinstance(session, str) or not session:
        return
    now = time.time()
    def put(data):
        for rec in data.values():
            if (isinstance(rec, dict) and rec.get("state") == "open" and rec.get("session") == session
                    and rec.get("agent_id") == agent_id):
                rec.update(state="gone", closed_at=now)
    _update(put, until=time.monotonic() + STOP_LOCK_WAIT, trim=False)


def pending(ident: str, now: Optional[float] = None) -> bool:
    rec = get(ident)
    now = time.time() if now is None else now
    return rec.get("state") == "open" and now < rec.get("deadline", 0) and _alive(rec.get("pid"))


# --- the card -------------------------------------------------------------------------------------

def card_text(rec: dict, lang: str) -> str:
    return (f"🛡️ <b>{html.escape(t('pq_title', lang=lang))}</b>\n{html.escape(t('pq_intro', lang=lang))}\n"
            f"<pre>{html.escape(rec.get('tool', ''), quote=False)}</pre>\n"
            f"<pre>{html.escape(rec.get('text', ''), quote=False)}</pre>\n"
            + html.escape(t("pq_reply_yes" if rec.get("complete") else "pq_decline_only", lang=lang), quote=False))


def card_markup(ident: str, lang: str) -> dict:
    return {"inline_keyboard": [[{"text": t("pq_btn_no", lang=lang), "callback_data": f"pq:n:{ident}",
                                  "style": "danger"}]]}


def _question_text(question: dict, lang: str) -> str:
    return (f"❓ <b>{html.escape(t('pq_question_title', lang=lang))}</b>\n"
            f"{html.escape(question['question'])}")


def _question_markup(ident: str, rec: dict, index: int, lang: str) -> dict:
    question = rec["questions"][index]
    selected = set(rec.get("selected", {}).get(str(index), []))
    rows = []
    for option in question["options"]:
        mark = "☑️ " if option["token"] in selected else ""
        action = "m" if question["multi"] else "q"
        rows.append([{"text": mark + option["label"],
                      "callback_data": f"pq:{action}:{ident}:{option['token']}"}])
    if question["multi"]:
        rows.append([{"text": t("pq_btn_done", lang=lang), "callback_data": f"pq:d:{ident}:{question['token']}"}])
    rows.append([{"text": t("pq_btn_terminal", lang=lang), "callback_data": f"pq:x:{ident}"}])
    rows.append(menus.close_row(lang))
    return {"inline_keyboard": rows}


def _plan_text(plan: str, lang: str) -> str:
    head = f"📋 <b>{html.escape(t('pq_plan_title', lang=lang))}</b>\n"
    suffix = "\n…" if len(plan) > 3500 else ""
    return head + html.escape(plan[:3500], quote=False) + suffix


def _plan_markup(ident: str, lang: str) -> dict:
    return {"inline_keyboard": [
        [{"text": t("pq_btn_approve", lang=lang), "callback_data": f"pq:p:a:{ident}"},
         {"text": t("pq_btn_reject", lang=lang), "callback_data": f"pq:p:r:{ident}", "style": "danger"}],
        [{"text": t("pq_btn_terminal", lang=lang), "callback_data": f"pq:p:y:{ident}"}],
        menus.close_row(lang),
    ]}


def _edit(cfg, bot, rec: dict, text: str) -> bool:
    if not isinstance(rec.get("message_id"), int):
        return True
    try:
        bot.call("editMessageText", {"chat_id": rec.get("chat") or home_chat(cfg), "message_id": rec["message_id"],
                                     "text": text, "reply_markup": {"inline_keyboard": []}})
        return True
    except Exception as exc:
        description = str(exc).lower()
        return "message is not modified" in description or "message to edit not found" in description


def _settle_decision(rec: dict, state: str, choice: str = "") -> bool:
    if not rec.get("decision"):
        return True
    from . import decisions
    try:
        decisions.settle_permission(rec["decision"], state, choice)
        return True
    except Exception:
        return False


def finish(cfg, bot, ident: str) -> None:
    """After an answer: the card says what was answered, the Cockpit decision closes."""
    rec = get(ident)
    if rec.get("state") != "answered":
        return
    lang = getattr(cfg, "ui_language", None) or "en"
    _edit(cfg, bot, rec, t("pq_done_yes" if rec.get("choice") == "yes" else "pq_done_no", lang=lang))
    _settle_decision(rec, "answered", rec.get("choice", ""))


def retire(cfg, bot, rec: dict, key: str) -> bool:
    lang = getattr(cfg, "ui_language", None) or "en"
    edited = _edit(cfg, bot, rec, t(key, lang=lang))
    settled = _settle_decision(rec, "cancelled")
    return edited and settled


# --- the hook -------------------------------------------------------------------------------------

def _decision(choice: str) -> dict:
    out = {"behavior": "allow"} if choice == "yes" else {"behavior": "deny", "message": t("pq_denied_note", lang="en")}
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": out}}


def _structured_decision(rec: dict) -> Optional[dict]:
    if rec.get("kind") == "questions" and rec.get("state") == "answered":
        decision = {"behavior": "allow", "updatedInput": {**rec["tool_input"], "answers": rec["answers"]}}
    elif rec.get("kind") == "plan" and rec.get("state") == "answered":
        decision = ({"behavior": "allow", "updatedInput": rec["tool_input"]}
                    if rec.get("choice") == "yes" else
                    {"behavior": "deny", "message": t("pq_plan_denied_note", lang="en")})
    else:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


def _make_questions(tool_input) -> Optional[list]:
    """Validate UI fields while retaining the original input separately for updatedInput."""
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("questions"), list) or not tool_input["questions"]:
        return None
    result = []
    for raw in tool_input["questions"]:
        if (not isinstance(raw, dict) or not isinstance(raw.get("question"), str)
                or not raw["question"] or not isinstance(raw.get("options"), list) or not raw["options"]):
            return None
        options = []
        for option in raw["options"]:
            if not isinstance(option, dict) or not isinstance(option.get("label"), str) or not option["label"]:
                return None
            options.append({"label": option["label"], "token": secrets.token_hex(4)})
        multi = raw.get("multiSelect", False)
        if not isinstance(multi, bool):
            return None
        result.append({"question": raw["question"], "options": options, "multi": multi,
                       "token": secrets.token_hex(4)})
    return result


def _publish_structured(data: dict, session: str, tool: str, found, cfg: Config, bot, lang: str,
                        wait: float) -> Optional[str]:
    tool_input = data.get("tool_input")
    questions = _make_questions(tool_input) if tool == "AskUserQuestion" else None
    if tool == "AskUserQuestion" and questions is None:
        return None
    if tool == "ExitPlanMode" and not isinstance(tool_input, dict):
        return None
    ident = secrets.token_hex(8)
    now = time.time()
    rec = {"session": session, "kind": "questions" if questions is not None else "plan",
           "tool_input": tool_input, "created": now, "deadline": now + wait, "pid": os.getpid(),
           "state": "open", "chat": found.away, "thread": found.thread, "agent_id": data.get("agent_id")}
    if questions is not None:
        rec.update(questions=questions, answers={}, selected={}, message_ids=[])
    else:
        plan = tool_input.get("plan", "")
        if not isinstance(plan, str):
            plan = json.dumps(plan, ensure_ascii=False)

    def publish(records):
        records[ident] = rec
        write_private(_path(), json.dumps(records, ensure_ascii=False))
        messages = []
        cards = ([( _question_text(q, lang), _question_markup(ident, rec, i, lang))
                  for i, q in enumerate(questions)] if questions is not None
                 else [(_plan_text(plan, lang), _plan_markup(ident, lang))])
        for text, markup in cards:
            try:
                mid = (bot.call("sendMessage", {"chat_id": found.chat, "message_thread_id": found.thread,
                       "text": text, "parse_mode": "HTML", "reply_markup": markup,
                       "disable_notification": False}) or {}).get("message_id")
            except Exception:
                mid = None
            if not isinstance(mid, int):
                rec.update(state="gone", closed_at=time.time())
                write_private(_path(), json.dumps(records, ensure_ascii=False))
                return False
            messages.append(mid)
        rec["message_ids"] = messages
        rec["message_id"] = messages[0]
        rec["sent_at"] = time.time()
        write_private(_path(), json.dumps(records, ensure_ascii=False))
        return True
    return ident if _update(publish) else None


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
        if tool in ("AskUserQuestion", "ExitPlanMode"):
            ident = _publish_structured(data, session, tool, found, cfg, bot, lang, wait)
            if ident is None:
                return 0
            rec = get(ident)
        else:
            text, complete = describe(data.get("tool_input"))
            tool_shown = tool if visible(tool) and len(tool) <= 120 else json.dumps(tool)[:120]
            complete = complete and tool_shown == tool and tool_exact(tool)
            now = time.time()
            ident = secrets.token_hex(8)
            rec = {"session": session, "tool": tool_shown, "text": text, "complete": complete, "created": now,
                   "deadline": now + wait, "pid": os.getpid(), "state": "open", "chat": found.away, "thread": found.thread,
                   "agent_id": data.get("agent_id")}
            rec["complete"] = complete = complete and surfaces_exact(tool_shown + "\n" + text, card_text(rec, lang))
            def publish(d):
                d[ident] = rec
                write_private(_path(), json.dumps(d, ensure_ascii=False))
                try:
                    from . import decisions
                    rec["decision"] = decisions.create(
                        session, found.chat, found.thread, t("pq_decision_title", lang=lang),
                        t("pq_decision_detail" if complete else "pq_decline_only", lang=lang),
                        lang=lang, perm=ident, request=tool_shown + "\n" + text,
                        decline_only=not complete)["id"]
                except Exception:
                    pass
                write_private(_path(), json.dumps(d, ensure_ascii=False))
                params = {"chat_id": found.chat, "message_thread_id": found.thread, "text": card_text(rec, lang),
                          "parse_mode": "HTML", "reply_markup": card_markup(ident, lang), "disable_notification": False}
                try:
                    mid = (bot.call("sendMessage", params) or {}).get("message_id")
                except Exception:
                    mid = None
                if not isinstance(mid, int):
                    rec.update(state="gone", closed_at=time.time())
                    return False
                rec.update(message_id=mid, sent_at=time.time())
                write_private(_path(), json.dumps(d, ensure_ascii=False))
                return True
            if not _update(publish):
                return 0

        def gone(*_a):
            raise SystemExit(0)
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            try:
                signal.signal(sig, gone)
            except (ValueError, OSError):
                pass
        while time.time() < rec["deadline"]:
            cur = get(ident)
            structured = _structured_decision(cur)
            if structured is not None:
                out.write(json.dumps(structured, ensure_ascii=False))
                out.flush()
                return 0
            if cur.get("state") == "answered" and cur.get("choice") in ("yes", "no"):
                out.write(json.dumps(_decision(cur["choice"])))
                out.flush()
                finish(cfg, bot, ident)
                return 0
            if cur.get("state") != "open":
                return 0
            time.sleep(POLL)
        _close(ident, "expired")
    except BaseException as exc:  # noqa: BLE001 - nothing may escape into Claude Code; never an allow
        if ident:
            try:
                _close(ident, "gone")
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
    parts = (data or "").split(":")
    ident = parts[-1] if len(parts) == 3 and parts[1] in ("x",) else (
        parts[3] if len(parts) == 4 and parts[1] == "p" else parts[2] if len(parts) >= 3 else "")
    rec = get(ident) if match else {}
    if (not match or sender.get("id") != cfg.owner_user_id or sender.get("is_bot") or not found
            or found.chat != (m.get("chat") or {}).get("id") or found.thread != m.get("message_thread_id")
            or rec.get("session") != session or m.get("message_id") not in rec.get("message_ids", [rec.get("message_id")])):
        I._answer_button(bot, q, t("toast_not_available", lang=lang))
        return "ignored"
    if rec.get("kind") in ("questions", "plan"):
        result = _structured_button(ident, parts, rec, m.get("message_id"), cfg, bot, lang)
        I._answer_button(bot, q, t("pq_toast_sent" if result == "ok" else "toast_question_closed", lang=lang))
        return "permission-" + result
    result = answer(ident, "no", "button")
    I._answer_button(bot, q, t("pq_toast_sent" if result == "ok" else "toast_question_closed", lang=lang))
    if result == "ok":
        finish(cfg, bot, ident)
    return "permission-" + result


def _structured_button(ident: str, parts: list, snapshot: dict, message_id: int,
                       cfg: Config, bot, lang: str) -> str:
    now = time.time()
    redraw = None

    def choose(records):
        nonlocal redraw
        rec = records.get(ident)
        if (not isinstance(rec, dict) or rec.get("state") != "open" or now >= rec.get("deadline", 0)
                or not _alive(rec.get("pid"))):
            return "closed"
        action = parts[1]
        if action == "x" or (action == "p" and parts[2] == "y"):
            rec.update(state="terminal", closed_at=now)
            return "ok"
        if rec.get("kind") == "plan" and action == "p" and parts[2] in ("a", "r"):
            rec.update(state="answered", choice="yes" if parts[2] == "a" else "no", closed_at=now)
            return "ok"
        if rec.get("kind") != "questions" or action not in ("q", "m", "d"):
            return "closed"
        index = next((i for i, mid in enumerate(rec["message_ids"]) if mid == message_id), None)
        if index is None:
            return "closed"
        question = rec["questions"][index]
        token = parts[3]
        if action == "d":
            selected = rec.get("selected", {}).get(str(index), [])
            if token != question["token"] or not question["multi"] or not selected:
                return "closed"
            labels = [o["label"] for o in question["options"] if o["token"] in selected]
            rec["answers"][question["question"]] = ", ".join(labels)
        else:
            option = next((o for o in question["options"] if o["token"] == token), None)
            if option is None or question["multi"] != (action == "m"):
                return "closed"
            if action == "m":
                selected = rec.setdefault("selected", {}).setdefault(str(index), [])
                selected.remove(token) if token in selected else selected.append(token)
                redraw = (index, dict(rec))
                return "ok"
            rec["answers"][question["question"]] = option["label"]
        if len(rec["answers"]) == len(rec["questions"]):
            rec.update(state="answered", closed_at=now)
        return "ok"

    result = _update(choose)
    if result == "ok" and redraw:
        index, current = redraw
        try:
            bot.call("editMessageReplyMarkup", {"chat_id": current["chat"], "message_id": message_id,
                     "reply_markup": _question_markup(ident, current, index, lang)})
        except Exception:
            pass
    return result or "closed"


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
        if not isinstance(rec, dict):
            continue
        if rec.get("state") == "open":
            alive = _alive(rec.get("pid"))
            if alive and now < rec.get("deadline", 0) + SWEEP_GRACE:
                continue
            rec = _close(ident, "expired" if alive else "gone", now)
            if not rec:
                continue
            n += 1
        if rec.get("state") in ("gone", "expired") and not rec.get("retired"):
            if retire(cfg, bot, rec, "pq_expired" if rec["state"] == "expired" else "pq_gone"):
                def mark(data):
                    current = data.get(ident)
                    if isinstance(current, dict) and current.get("state") == rec["state"]:
                        current["retired"] = True
                _update(mark)

    # Hold the publication lock while checking the unfiltered decisions: a publisher
    # must not be between creating its decision and saving its request here.
    def cancel_orphans(data):
        from . import decisions
        for decision in decisions._load(state_dir() / decisions.STATE_FILE).values():
            if (decision["state"] == "open" and decision.get("perm")
                    and decision["perm"] not in data and decision["created"] <= now - ORPHAN_GRACE):
                decisions.settle_permission(decision["id"], "cancelled", now=now)
    _update(cancel_orphans)
    return n
