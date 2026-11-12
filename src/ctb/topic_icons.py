"""Give each session's Telegram topic an icon that fits its subject.

A topic gets a new icon only when its name changed since the last pick. Gemini picks one emoji from
Telegram's allowed topic icons; a small keyword map is the fallback. Outbound only, to the owner's
chat. Network-heavy (Gemini may take seconds): run it from a scheduled command, never from a hook.
"""
from __future__ import annotations

import json
import re
import time
from typing import Dict, List, Optional

from .config import Config, home_chat, state_dir, write_private
from .projects import thread_key
from .telegram import Bot, TelegramError
from .topics import TopicMap

# Never for this audience: adult, other holidays, party and dating icons.
BANNED = set("🔞 🫦 💄 👠 💘 🍹 🎄 🎃 🤡 🏴‍☠️ 🕺 💃 💅 🤰 🪩 🍓 🦄".split())
KEYWORDS = [(r"telegram|טלגרם|bridge|גשר", "💬"), (r"video|וידאו|סרט|פודקאסט|podcast", "🎬"),
            (r"torah|תורה|תניא|גמרא|שיעור", "📚"), (r"server|שרת|\bci\b|cloud|ענן", "💻"),
            (r"research|מחקר|last30", "🔎"), (r"whatsapp|ווצאפ", "📱"), (r"\bpr\b|bug|באג|fix|תיקון|test", "🧪"),
            (r"money|payment|price|כסף|תשלום|מחיר", "💰")]
RETRY_AFTER = 3600  # seconds before a topic whose icon failed is tried again
MAX_PER_RUN = 5  # a few per run keeps us far from Telegram rate limits
MAX_TRIES = 3  # failed attempts before a topic is skipped


def pick(name: str, icons: List[str], cfg: Config) -> Optional[str]:
    allowed = [e for e in icons if e and e not in BANNED]
    if not allowed:
        return None
    from .creds import for_outside
    from .translate import ask
    out = ask("Pick the single emoji from this list that best fits a work chat titled: "
              + json.dumps(for_outside(name), ensure_ascii=False) + "\nList: " + " ".join(allowed)
              + "\nReply with the emoji only.", cfg).strip()
    if out in allowed:
        return out
    for emoji in allowed:
        if emoji in out:
            return emoji
    low = name.lower()
    for pattern, emoji in KEYWORDS:
        if re.search(pattern, low) and emoji in allowed:
            return emoji
    return "💡" if "💡" in allowed else None


def _key(entry: dict) -> str:
    """The picks are kept per topic: the thread id at home, "chat:thread" in a project group."""
    return thread_key(entry["thread"], None if entry.get("home", True) else entry["chat"])


def run(cfg: Config, bot=None) -> int:
    """Pick and set an icon for every topic whose name changed. Returns how many were set."""
    if not cfg.ready:
        return 0
    bot = bot or Bot.from_config(cfg)
    path = state_dir() / "topic-icons.json"
    try:
        picks = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(picks, dict):
            picks = {}
    except (OSError, ValueError):
        picks = {}
    topics = TopicMap(state_dir() / "topics.json", bot, home_chat(cfg), cfg=cfg).all_places()
    todo = {sid: e for sid, e in topics.items()
            if isinstance(e, dict) and type(e.get("thread")) is int and e.get("name")
            and (picks.get(_key(e)) or {}).get("name") != e["name"]
            and time.time() - (picks.get(_key(e)) or {}).get("failed_at", 0) > RETRY_AFTER}
    if not todo:
        return 0
    try:
        stickers = bot.call("getForumTopicIconStickers", {})
    except TelegramError:
        return 0
    icons: Dict[str, str] = {s.get("emoji", ""): s["custom_emoji_id"] for s in stickers or []
                             if isinstance(s, dict) and s.get("custom_emoji_id")}
    done = 0
    # Newest topics first. A topic that fails (deleted, closed) is recorded with a try count: it is
    # retried twice, then skipped for good, or it blocked every newer topic behind it.
    for _sid, entry in list(todo.items())[::-1][:MAX_PER_RUN]:
        emoji = pick(entry["name"], list(icons), cfg)
        key = _key(entry)
        before = picks.get(key) or {}
        failed = dict(before, failed_at=time.time())
        if not emoji:
            picks[key] = failed  # wait before retrying, so later topics get a turn
            continue
        try:
            bot.call("editForumTopic", {"chat_id": entry["chat"], "message_thread_id": entry["thread"],
                                        "icon_custom_emoji_id": icons[emoji]})
        except TelegramError as exc:
            if "not modified" not in str(exc).lower():
                tries = int(before.get("tries", 0) or 0) + 1
                failed.update(tries=tries, error=str(exc)[:80])
                if tries >= MAX_TRIES:
                    failed["name"] = entry["name"]  # counts as handled until the topic is renamed
                picks[key] = failed
                continue
        picks[key] = {"name": entry["name"], "emoji": emoji}
        done += 1
    write_private(path, json.dumps(picks, ensure_ascii=False))
    return done
