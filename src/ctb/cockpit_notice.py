"""Question notice state lives with the question, never around a Telegram call."""
import re
import random
import threading
import time

from . import alerts, cards, decisions, panel_pin
from .i18n import t
from .telegram import Bot, TelegramError


def url(cfg, bot, ident):
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{16}", ident):
        return ""
    name = panel_pin.known_name(cfg) or panel_pin._bot_name(cfg, bot)
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,64}", name):
        return ""
    return f"https://t.me/{name}?startapp=q{ident}"


def _store(rec):
    return cards if rec.get("kind") in cards.ASKS else decisions


def update(cfg, rec, opened, bot=None):
    try:
        if rec.get("chat") not in decisions.owner_chats(cfg) or not isinstance(rec.get("id"), str) or not decisions.ID_RE.fullmatch(rec["id"]):
            return False
        store = _store(rec)
        ident = rec["id"]
        with store._locked() as path:
            data = store._load(path)
            current = data.get(ident)
            if not current or current["chat"] != rec["chat"] or current["session"] != rec["session"]:
                return False
            if opened:
                if current["state"] != "open" or current.get("notice_message"):
                    return False
                current["notice_message"] = -1  # send in progress
                store._save(path, data)
            else:
                if current["state"] == "open" or not current.get("notice_pending"):
                    return False
                message = current.get("notice_message")
                if not message or message == -1:
                    return False
        bot = bot or Bot.from_config(cfg)
        lang = getattr(cfg, "ui_language", "en")
        text = t("decide_waiting", lang=lang)
        if not opened:
            try:
                bot.call("editMessageText", {"chat_id": current["chat"], "message_id": message, "text": text,
                                           "reply_markup": {"inline_keyboard": []}}, timeout=5)
            except TelegramError as exc:
                if not (exc.code == 400 and ("message to edit not found" in str(exc).lower()
                                               or "message is not modified" in str(exc).lower())):
                    return False
            with store._locked() as path:
                data = store._load(path)
                current = data.get(ident)
                if current and current.get("notice_message") == message and current["state"] != "open":
                    current.pop("notice_pending", None)
                    store._save(path, data)
            return True
        link = url(cfg, bot, ident)
        markup = {"inline_keyboard": [[{"text": t("cockpit_answer_button", lang=lang), "url": link}]]} if link else {"inline_keyboard": []}
        # Re-read after link lookup, which can itself contact Telegram.
        with store._locked() as path:
            data = store._load(path)
            current = data.get(ident)
            if not current or current["state"] != "open":
                if current and current.get("notice_message") == -1:
                    current.pop("notice_message", None)
                    current.pop("notice_pending", None)
                    store._save(path, data)
                return False
        try:
            sent = bot.call("sendMessage", {"chat_id": current["chat"], "message_thread_id": current["thread"],
                                          "text": text, "reply_markup": markup}, timeout=5) or {}
        except Exception:
            with store._locked() as path:
                data = store._load(path)
                current = data.get(ident)
                if current and current.get("notice_message") == -1:
                    current.pop("notice_message", None)
                    store._save(path, data)
            return False
        message = sent.get("message_id")
        with store._locked() as path:
            data = store._load(path)
            current = data.get(ident)
            if not current:
                return False
            if type(message) is int and message > 0:
                current["notice_message"] = message
            else:
                current.pop("notice_message", None)
            closed = current["state"] != "open"
            alert = bool(not closed and link and not current.get("notice_alerted"))
            if alert:
                current["notice_alerted"] = True
            store._save(path, data)
        if closed:
            return update(cfg, current, False, bot)
        if alert:
            with store._locked() as path:
                current = store._load(path).get(ident)
                if not current or current["state"] != "open":
                    return bool(message)
            alerts.alert(cfg, t("cockpit_question_waiting", lang=lang), current["session"], bot=bot,
                         url=link, kind="cockpit", label="cockpit_answer_button")
        return bool(message)
    except Exception:
        try:
            if opened:
                _abandon(_store(rec), rec.get("id"))
        except Exception:
            pass
        return False


def later(cfg, rec, bot=None):
    """Never make a page answer wait for Telegram."""
    threading.Thread(target=update, args=(cfg, rec, False, bot), daemon=True).start()


def _abandon(store, ident):
    """Clear a send-in-progress mark that no send owns any more."""
    with store._locked() as path:
        data = store._load(path)
        current = data.get(ident)
        if current and current.get("notice_message") == -1:
            current.pop("notice_message", None)
            store._save(path, data)
            return current
    return None


def sweep(cfg, bot=None, now=None):
    """At most three Telegram calls a round, picked at random so one stuck message never starves the rest:
    buttons of closed questions are removed, and a send that was cut off over a minute ago is tried again."""
    now = time.time() if now is None else now
    todo = []
    for store in (decisions, cards):
        for rec in store._load(store.state_dir() / store.STATE_FILE).values():
            if rec["state"] != "open" and rec.get("notice_pending") and rec.get("notice_message", 0) > 0:
                todo.append((store, rec, False))
            elif rec.get("notice_message") == -1 and now - rec.get("created", now) > 60:
                todo.append((store, rec, True))
    for store, rec, resend in random.sample(todo, min(3, len(todo))):
        if not resend:
            update(cfg, rec, False, bot)
        elif _abandon(store, rec["id"]) and rec["state"] == "open":
            update(cfg, rec, True, bot)
