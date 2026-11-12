"""A full, detailed log file of a session's work, built mechanically from its transcript: no model and no
quota. Every message you sent, every reply, every tool call (title, command, output) and every
background agent, as one self-contained HTML page sent to the session's topic as a document.

Sent automatically (from the dashboard tick) when a session's turn ended with at least MIN_ACTIONS new
actions since its last log, or on demand: `harnessgram session-log SESSION [--all]`. Secrets are hidden
before anything is written; a tool's output is cut to OUTPUT_CAP characters.
"""
from __future__ import annotations

import glob
import html
import json
import os
import re
import time
from typing import List, Optional, Tuple

from . import lang as L, status
from .config import home_chat, private_dir, state_dir, write_private
from .creds import redact_detail
from .i18n import t
from .telegram import Bot, TelegramError
from .topics import TopicMap, session_state

MIN_ACTIONS = 20
OUTPUT_CAP = 4000  # characters of each tool output kept in the file
MARKS = "session-log.json"


def _rows(path: str) -> List[dict]:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        pass
    return out


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) if b.get("type") == "text" else "[" + str(b.get("type")) + "]"
                         for b in content if isinstance(b, dict))
    return str(content or "")


def _agent_meta(path: str, tool_id) -> dict:
    for meta in glob.glob(os.path.join(glob.escape(os.path.splitext(path)[0]), "subagents", "*.meta.json")):
        try:
            with open(meta, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("toolUseId") == tool_id:
            return data
    return {}


def _safe(text: str) -> str:
    return html.escape(redact_detail(text))


def build(session: str, path: str, since_uuid: Optional[str] = None, name: str = "",
          lang: Optional[str] = None) -> Tuple[str, int, Optional[str]]:
    """(html page, number of actions, uuid of the last row)."""
    rows = _rows(path)
    start = 0
    if since_uuid:
        for i, r in enumerate(rows):
            if r.get("uuid") == since_uuid:
                start = i + 1
    results = {}
    for r in rows:
        msg = r.get("message") if isinstance(r.get("message"), dict) else {}
        content = msg.get("content")
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                results[b.get("tool_use_id")] = (_result_text(b.get("content")), bool(b.get("is_error")))
    parts, actions, first, last, last_uuid = [], 0, None, None, since_uuid
    for r in rows[start:]:
        msg = r.get("message") if isinstance(r.get("message"), dict) else {}
        stamp = str(r.get("timestamp", ""))[11:16]
        first = first or r.get("timestamp")
        last = r.get("timestamp") or last
        last_uuid = r.get("uuid") or last_uuid
        content = msg.get("content")
        if r.get("type") == "user" and isinstance(content, str):
            body = content.strip()
            if not body or body.startswith(("<local-command", "<command-")):
                continue
            system = body.startswith(("<", "#"))
            who = t("log_system" if system else "log_owner", lang=lang)
            if system:
                body = re.sub(r"<[^>]{1,80}>", " ", body)
            parts.append(f'<div class="msg owner"><div class="who">{html.escape(who)} · {stamp}</div>'
                         f'<pre>{_safe(body.strip()[:3000])}</pre></div>')
        elif r.get("type") == "assistant":
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and str(b.get("text", "")).strip():
                    parts.append(f'<div class="msg reply" dir="ltr"><div class="who">Claude · {stamp}</div>'
                                 f'<pre>{_safe(str(b["text"]).strip())}</pre></div>')
                elif b.get("type") == "tool_use":
                    actions += 1
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    tool = str(b.get("name"))
                    title = inp.get("description") or tool
                    extra = ""
                    if tool in ("Agent", "Task"):
                        meta = _agent_meta(path, b.get("id"))
                        bits = [x for x in (meta.get("model") or inp.get("model"),
                                            meta.get("effort") or inp.get("effort")) if x]
                        label = " · ".join(map(str, bits)) or t("log_agent_default", lang=lang)
                        extra = f' <span class="tag">{html.escape(t("log_agent", lang=lang))} · {html.escape(label)}</span>'
                    detail = (inp.get("command") or inp.get("prompt") or inp.get("file_path") or inp.get("pattern")
                              or inp.get("query") or inp.get("url") or "")
                    if not isinstance(detail, str):
                        detail = json.dumps(detail, ensure_ascii=False)
                    if not detail and inp:
                        detail = json.dumps(inp, ensure_ascii=False)[:2000]
                    out, err = results.get(b.get("id"), ("", False))
                    out_html = ""
                    if out:
                        cut = f"\n{t('log_truncated', lang=lang)}" if len(out) > OUTPUT_CAP else ""
                        out_html = f'<pre dir="ltr" class="out">{_safe(out[:OUTPUT_CAP])}{html.escape(cut)}</pre>'
                    parts.append(
                        f'<details class="act"><summary>{"❌" if err else "✔️"} <b dir="auto">{_safe(str(title))}</b>'
                        f' <span class="tool">{html.escape(tool)}</span>{extra} · {stamp}</summary>'
                        + (f'<pre dir="ltr" class="cmd">{_safe(detail[:6000])}</pre>' if detail else "")
                        + out_html + "</details>")
    name = name or session[:8]
    span = f"{str(first)[:16].replace('T', ' ')} – {str(last)[11:16]}" if first else ""
    rtl = L.is_rtl("he" if (lang or "").startswith("he") else "en")
    page = f"""<!doctype html><html lang="{'he' if rtl else 'en'}" dir="{'rtl' if rtl else 'ltr'}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(t('log_title', lang=lang, name=name))}</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--mute:#6b7383;--line:#e3e6ec;--own:#e8f1ff;--code:#0f1420;--codeink:#d7e0f0}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1218;--card:#171b23;--ink:#e6e9ef;--mute:#9aa3b2;--line:#2a303b;--own:#1b2a40;--code:#0a0d13;--codeink:#cfd8e8}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,"Segoe UI",Arial,sans-serif}}
main{{max-width:860px;margin:0 auto;padding:16px}}
h1{{font-size:20px;margin:8px 0 2px}} .sub{{color:var(--mute);margin-bottom:16px}}
.msg{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin:10px 0}}
.owner{{background:var(--own)}} .who{{color:var(--mute);font-size:13px;margin-bottom:4px}}
pre{{white-space:pre-wrap;word-break:break-word;margin:0;font:inherit}}
.act{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 10px;margin:6px 0}}
.act summary{{cursor:pointer}} .tool,.tag{{color:var(--mute);font-size:12px}}
.cmd,.out{{background:var(--code);color:var(--codeink);border-radius:8px;padding:8px;margin-top:6px;
font:12.5px/1.5 ui-monospace,Menlo,monospace;text-align:left}}
</style></head><body><main>
<h1>📒 {html.escape(t('log_title', lang=lang, name=name))}</h1>
<div class="sub">{html.escape(t('log_sub', lang=lang, span=span, n=actions))}</div>
{''.join(parts) or '<p>' + html.escape(t('log_none', lang=lang)) + '</p>'}
</main></body></html>"""
    return page, actions, last_uuid


def _marks() -> dict:
    try:
        data = json.loads((state_dir() / MARKS).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def send(cfg, session: str, everything: bool = False, min_actions: int = 1, bot=None,
         find=status.transcript_of) -> bool:
    """Build the log since the last one sent (or everything) and send it to the session's topic."""
    path = find(session)
    bot = bot or Bot.from_config(cfg)
    topics = TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg)
    found = topics.place_of(session)
    if not path or found is None:
        return False
    lang = getattr(cfg, "ui_language", "en")
    marks = _marks()
    name = str((topics.all_places().get(session) or {}).get("name") or "")
    page, actions, last_uuid = build(session, path, None if everything else marks.get(session), name, lang)
    if actions < min_actions:
        return False
    fields = {"chat_id": found.chat, "message_thread_id": found.thread, "disable_notification": "true",
              "caption": t("log_caption", lang=lang, n=actions)}
    try:
        bot.upload("sendDocument", fields, "document", f"log-{time.strftime('%Y%m%d-%H%M')}.html",
                   page.encode("utf-8"))
    except TelegramError:
        return False
    if not everything:
        marks[session] = last_uuid
        write_private(state_dir() / MARKS, json.dumps(dict(list(marks.items())[-300:])))
    return True


SEEN = "session-log-seen.json"


def _last_uuid(path: str) -> Optional[str]:
    """uuid of the transcript's last row, read from the file's tail."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for raw in reversed(tail):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("uuid"):
            return str(row["uuid"])
    return None


def auto(cfg, bot=None, find=status.transcript_of) -> int:
    """Off unless `session_log_auto` is on. Sessions whose turn ended (state "done") with enough new
    actions since their last log get one. The first run only records where each session stands now, so
    no old history is sent. A transcript is read again only when its size or time changed. Returns how
    many logs were sent; never raises."""
    if not getattr(cfg, "session_log_auto", False):
        return 0
    sent = 0
    try:
        bot = bot or Bot.from_config(cfg)
        marks, seen = _marks(), _load_seen()
        for session in TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg).all_places():
            try:
                path = find(session)
                if not path:
                    continue
                st = os.stat(path)
                stamp = [st.st_size, int(st.st_mtime)]
                if session not in marks:  # first sight: what exists now counts as already sent
                    uuid = _last_uuid(path)
                    if uuid:
                        marks[session] = uuid
                        write_private(state_dir() / MARKS, json.dumps(dict(list(marks.items())[-300:])))
                    seen[session] = stamp
                    continue
                if seen.get(session) == stamp or session_state(session).get("state") != "done":
                    continue
                if send(cfg, session, min_actions=MIN_ACTIONS, bot=bot, find=find):
                    sent += 1
                seen[session] = stamp  # below the threshold or sent: not read again until it changes
            except Exception:
                continue
        write_private(state_dir() / SEEN, json.dumps(dict(list(seen.items())[-300:])))
    except Exception:
        pass
    return sent


def _load_seen() -> dict:
    try:
        data = json.loads((state_dir() / SEEN).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
