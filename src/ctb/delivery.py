"""When a message written while a session works reaches it.

Three choices, per message and as a default in the settings menu:

* now: delivered between the session's steps (the PostToolUse hook), the long-standing behaviour;
* end: held until the turn ends (the Stop and wake hooks), so it cannot derail the current step;
* stop: interrupt the session; the wake hook delivers the message to the idle prompt.

A small receipt under the message carries the buttons; it is deleted once the message is delivered.
"""
from __future__ import annotations

import fcntl
import json
import os
from typing import Dict, Iterable, Optional

from .config import Config, home_chat, private_dir, state_dir, write_private
from .i18n import t
from .projects import thread_key
from .telegram import TelegramError

KEEP_ACKS = 200
DELIVERED_IDS_FILE = "delivery-delivered-ids.json"
# Message ids are only unique inside one chat: the receipts and delivered ids of a project group are
# kept under "chat:id" (thread_key), the home chat's under the bare id, as before.


def _delivered_ids_path():
    return state_dir() / DELIVERED_IDS_FILE


def _delivered_ids() -> list[str]:
    try:
        data = json.loads(_delivered_ids_path().read_text(encoding="utf-8"))
        return [str(x) for x in data if isinstance(x, (int, str))]
    except (OSError, ValueError):
        return []


def is_delivered(mid: int | str, chat: Optional[int] = None) -> bool:
    return thread_key(mid, chat) in _delivered_ids()


def _append_delivered(mids: Iterable[int | str]) -> None:
    """Add ids to the delivered list. The caller holds the receipts lock."""
    path = private_dir(state_dir()) / DELIVERED_IDS_FILE
    try:
        done = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(done, list):
            done = []
    except (OSError, ValueError):
        done = []
    write_private(path, json.dumps((done + [str(x) for x in mids])[-KEEP_ACKS:]))


def record_delivered(mids: Iterable[int | str], chat: Optional[int] = None) -> None:
    """Remember recently delivered message ids so late receipts know they are already done."""
    root = private_dir(state_dir())
    with open(root / "delivery-acks.lock", "a") as lock:
        os.chmod(root / "delivery-acks.lock", 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        _append_delivered([thread_key(x, chat) for x in mids])


def _mode_path():
    return state_dir() / "delivery-mode"


def default_mode() -> str:
    """"now" (between steps) or "end" (after the turn)."""
    try:
        return "end" if _mode_path().read_text(encoding="utf-8").strip() == "end" else "now"
    except OSError:
        return "now"


def set_default_mode(mode: str) -> None:
    write_private(_mode_path(), "end" if mode == "end" else "now")


def markup(mid: int, hold: bool, lang: Optional[str] = None) -> dict:
    other = ({"text": t("delivery_btn_now", lang=lang), "callback_data": f"dl:now:{mid}"} if hold
             else {"text": t("delivery_btn_hold", lang=lang), "callback_data": f"dl:hold:{mid}"})
    return {"inline_keyboard": [[other, {"text": t("delivery_btn_stop", lang=lang), "callback_data": f"dl:stop:{mid}"}],
                                cancel_row(mid, lang)]}


def cancel_row(mid: int | str, lang: Optional[str] = None) -> list:
    """Take a message back while it still waits; shown under every receipt and waiting notice."""
    return [{"text": t("delivery_btn_cancel", lang=lang), "callback_data": f"dl:cancel:{mid}"}]


def cancel_markup(mid: int | str, lang: Optional[str] = None) -> dict:
    return {"inline_keyboard": [cancel_row(mid, lang)]}


def forget_ack(mid: int | str, chat: Optional[int] = None) -> None:
    """A cancelled message: its receipt stays on screen as the note, and no later receipt is sent for it."""
    key = thread_key(mid, chat)

    def take(acks):
        _append_delivered([key])
        acks.pop(key, None)
    try:
        _update_acks(take)
    except OSError:
        pass


def _acks() -> Dict[str, int]:
    try:
        data = json.loads((state_dir() / "delivery-acks.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _update_acks(change) -> None:
    root = private_dir(state_dir())
    with open(root / "delivery-acks.lock", "a") as lock:
        os.chmod(root / "delivery-acks.lock", 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        acks = _acks()
        change(acks)
        write_private(root / "delivery-acks.json", json.dumps(dict(list(acks.items())[-KEEP_ACKS:])))


def send_ack(bot, cfg: Config, thread: Optional[int], mid: int, hold: bool, chat: Optional[int] = None) -> None:
    """A small receipt under a message sent while the session works, with its delivery choice.
    `chat`: the project group of the message (None = the home chat)."""
    if is_delivered(mid, chat):
        return
    lang = getattr(cfg, "ui_language", "en")
    text = t("delivery_ack_hold", lang=lang) if hold else t("delivery_ack_now", lang=lang)
    params = {"chat_id": home_chat(cfg) if chat is None else chat, "disable_notification": True,
              "text": text,
              "reply_parameters": {"message_id": mid, "allow_sending_without_reply": True},
              "reply_markup": markup(mid, hold, lang=lang)}
    if thread:
        params["message_thread_id"] = thread
    try:
        ack = bot.call("sendMessage", params)["message_id"]
        _update_acks(lambda acks: acks.__setitem__(thread_key(mid, chat), ack))
        # Delivered before this receipt was saved: it has nothing left to do
        if is_delivered(mid, chat):
            drop_acks(bot, cfg, [mid], chat)
    except (TelegramError, KeyError, TypeError, OSError):
        pass


def has_ack(mid: int | str, chat: Optional[int] = None) -> bool:
    """A receipt (between steps, at the turn's end, or still waiting) is already shown under this message."""
    return thread_key(mid, chat) in _acks()


def send_waiting(bot, cfg: Config, mid: int, chat: Optional[int] = None, closed: bool = False) -> bool:
    """A short visible notice under a message the session has not received a minute after it was sent
    (the sweeper keeps delivering it). Kept with the receipts, so the 👌 of the real delivery deletes it.
    `chat`: the project group (None = the home chat). False when nothing was sent."""
    if is_delivered(mid, chat) or has_ack(mid, chat):
        return False
    lang = getattr(cfg, "ui_language", "en")
    params = {"chat_id": home_chat(cfg) if chat is None else chat, "disable_notification": True,
              "text": t("delivery_waiting_closed" if closed else "delivery_waiting", lang=lang),
              "reply_parameters": {"message_id": mid, "allow_sending_without_reply": True},
              "reply_markup": cancel_markup(mid, lang)}
    try:
        ack = bot.call("sendMessage", params)["message_id"]
        _update_acks(lambda acks: acks.__setitem__(thread_key(mid, chat), ack))
        if is_delivered(mid, chat):  # delivered while the notice was on its way
            drop_acks(bot, cfg, [mid], chat)
        return True
    except (TelegramError, KeyError, TypeError, OSError):
        return False


def send_failed(bot, cfg: Config, mid: int, chat: Optional[int] = None) -> bool:
    """Automatic delivery gave up on a message to a closed session (the sweeper stops reopening it): its
    waiting notice turns into a visible failure, or one is sent. It stays with the receipts, so a later real
    delivery's 👌 still deletes it. False when nothing was shown."""
    if is_delivered(mid, chat):
        return False
    lang = getattr(cfg, "ui_language", "en")
    text = t("delivery_failed_closed", lang=lang)
    target = home_chat(cfg) if chat is None else chat
    ack = _acks().get(thread_key(mid, chat))
    try:
        if ack:
            bot.call("editMessageText", {"chat_id": target, "message_id": ack, "text": text,
                                         "reply_markup": cancel_markup(mid, lang)})
            return True
        sent = bot.call("sendMessage", {"chat_id": target, "disable_notification": True, "text": text,
                                        "reply_parameters": {"message_id": mid, "allow_sending_without_reply": True},
                                        "reply_markup": cancel_markup(mid, lang)})
        _update_acks(lambda acks: acks.__setitem__(thread_key(mid, chat), sent["message_id"]))
        if is_delivered(mid, chat):
            drop_acks(bot, cfg, [mid], chat)
        return True
    except (TelegramError, KeyError, TypeError, OSError):
        return False


def drop_acks(bot, cfg: Config, mids: Iterable[int | str], chat: Optional[int] = None) -> None:
    """The receipts of delivered messages have done their job."""
    mids = [thread_key(x, chat) for x in mids]
    found = []

    def take(acks):
        # Recorded first: a receipt sent a moment after a fast delivery finds itself here and goes
        _append_delivered(mids)
        for mid in mids:
            ack = acks.pop(mid, None)
            if ack:
                found.append(ack)
    try:
        _update_acks(take)
    except OSError:
        return
    for ack in found:
        try:
            bot.call("deleteMessage", {"chat_id": home_chat(cfg) if chat is None else chat, "message_id": ack})
        except TelegramError:
            pass


NOTES = frozenset(("hold", "now", "stop", "cancel"))


def note(mode: str, lang: Optional[str] = None) -> str:
    return t(f"delivery_note_{mode}", lang=lang)
