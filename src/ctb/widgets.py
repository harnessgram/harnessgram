"""Small private-chat decision widgets (Bot API 10.4)."""
from __future__ import annotations

import html
import json
import re
import secrets
import zlib

from .config import locked_map, read_map, state_dir

STAMP = re.compile(r"[0-9a-f]{12}")
PAYLOAD = re.compile(r"([0-2]):([0-9a-f]{12})")


def build_html(question: str, options: list[str], stamp: str) -> str:
    if not STAMP.fullmatch(stamp) or not 2 <= len(options) <= 3:
        raise ValueError("invalid widget options or stamp")
    if not isinstance(question, str) or not all(isinstance(o, str) for o in options):
        raise ValueError("question and options must be strings")
    buttons = "".join(f'<button id="b{i}">{html.escape(label)}</button>' for i, label in enumerate(options))
    code = f'''<!doctype html><html dir="rtl" lang="he"><head><meta charset="utf-8"><style>
body{{padding:12px;font-family:sans-serif}}button{{display:block;width:100%;min-height:52px;margin:8px 0;font-size:18px}}.selected{{outline:3px solid var(--tg-theme-button-color)}}
</style></head><body><p>{html.escape(question)}</p>{buttons}<script>
const W=window.TelegramWidget;
function draw(){{let n=W.chatData;for(let i=0;i<{len(options)};i++){{let b=document.getElementById('b'+i);b.className=n===String(i)?'selected':'';b.disabled=n!=null;}}}}
for(let i=0;i<{len(options)};i++)document.getElementById('b'+i).addEventListener('click',()=>W.request(i+':{stamp}').catch(()=>{{}}));
W.onEvent('chatDataChanged',draw);draw();W.ready();
</script></body></html>'''
    raw = code.encode('utf-8')
    if len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096:
        raise ValueError("widget exceeds Telegram's size limits")
    return code


def _path():
    return state_dir() / "widget-decisions.json"


def send_decision(cfg, bot, rec=None):
    """Send a snapshot of an existing decision, or a harmless sample if rec is None.

    A private registry binds each random stamp to the sent message and exact decision snapshot.
    A missing registry entry always fails closed.
    """
    if not isinstance(cfg.owner_chat_id, int) or cfg.owner_chat_id <= 0:
        raise ValueError("a paired private owner chat is required")
    if rec is None:
        question, options = "בדיקה: האם הווידג׳ט פועל?", ["כן", "לא"]
        ident = None
    else:
        question, options, ident = rec["title"], [rec["yes"], rec["no"]], rec["id"]
    stamp = secrets.token_hex(6)
    document = build_html(question, options, stamp)
    sent = bot.call("sendWidget", {"chat_id": cfg.owner_chat_id, "html": document,
                                  "caption": question, "allow_queries": True})
    if not isinstance(sent, dict) or not isinstance(sent.get("message_id"), int):
        raise ValueError("Telegram did not return a message id")
    with locked_map(_path()) as data:
        data[stamp] = {"message_id": sent["message_id"], "id": ident,
                       "question": question, "options": options}
    return sent


def send_sample(cfg, bot):
    return send_decision(cfg, bot)


def handle_query(query, cfg, bot, inbox):
    """Answer all queries; only a matched owner, message and decision can change anything."""
    from . import decisions, panel
    if not isinstance(query, dict):
        return "ignored"
    qid = query.get("id")
    result = "ignored"
    change = None
    if isinstance(qid, str) and qid:
        try:
            bot.call("answerWidgetQuery", {"widget_query_id": qid})
        except Exception:
            pass
    try:
        actor, chat, kind = query.get("from"), query.get("chat"), query.get("type")
        payload = query.get("data")
        match = PAYLOAD.fullmatch(payload) if isinstance(payload, str) else None
        if (not isinstance(actor, dict) or actor.get("id") != cfg.owner_chat_id or actor.get("is_bot")
                or not isinstance(chat, dict) or chat.get("id") != cfg.owner_chat_id
                or chat.get("type") != "private" or cfg.owner_chat_id <= 0
                or not isinstance(kind, dict) or kind.get("type") != "data" or not match):
            return result
        index, stamp = int(match[1]), match[2]
        entry = read_map(_path()).get(stamp)
        if (not isinstance(entry, dict) or entry.get("message_id") != query.get("message_id")
                or not isinstance(query.get("message_id"), int) or index >= len(entry.get("options", []))):
            return result
        ident = entry.get("id")
        if ident is not None:
            current = [r for r in decisions.open_decisions() if r["id"] == ident]
            if (index > 1 or len(current) != 1 or current[0]["title"] != entry.get("question")
                    or [current[0]["yes"], current[0]["no"]] != entry.get("options")
                    or panel.stale_reasons_now(cfg).get(ident) in ("ended", "replaced")):
                return result
            rec = decisions.answer(cfg, inbox, ident, ("yes", "no")[index])
        change = {"chat_id": cfg.owner_chat_id, "message_id": query["message_id"], "chat_data": str(index)}
        result = "widget-answered"
        if ident is not None:
            decisions.announce(cfg, bot, inbox, rec)
    except Exception:
        pass
    finally:
        if change is not None:
            try:
                bot.call("editMessageWidgetData", change)
            except Exception:
                pass
    return result
