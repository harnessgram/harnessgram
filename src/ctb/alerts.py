"""Optional alerts channel: a one-way Telegram channel where only what needs you rings.

Conversations stay quiet in their topics; a copy of what needs a human (a session waiting for an
approval, a quota that ran out or came back, a session that closed) goes to a channel you own.
It is off until you make the bot an administrator of a channel; removing the bot turns it off.
Only the owner's own action registers a channel. Nothing here ever raises: an alert is a copy,
never the only notice.

What reaches the channel is the owner's choice (`/alerts` in the home chat, buttons edited in place):
waiting sessions, system events, replies (off, a summary or the full text) and files. Every alert
declares its kind and is gated by the saved choice, read again on each send so a tap takes effect at
once; with no settings file the behaviour is what it always was. Text is plain text cleaned of known secret patterns
(not everything: full replies warn about that), because the channel may have readers the session does not. Files cannot be redacted, so they are off
until the owner turns them on, with a plain warning that everyone in the channel will see them.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from typing import Optional

from . import flood, fmt
from . import lang as L
from .config import Config, home_chat, state_dir, write_private
from .creds import get_token, hide_owner_links, redact_channel, relay_call
from . import menus
from .i18n import t
from .telegram import Bot, TelegramError
from .topics import TopicMap

FILE = "alerts-channel.json"
SETTINGS_FILE = "alerts-settings.json"
DEFAULTS = {"waiting": "on", "system": "on", "replies": "summary", "files": "off", "radio": "on", "cockpit": "on"}  # what it always did
CHOICES = {"waiting": ("on", "off"), "system": ("on", "off"),
           "replies": ("summary", "full", "off"), "files": ("off", "on"), "radio": ("on", "off"), "cockpit": ("on", "off")}  # in the order a tap cycles
# An alert's kind -> the setting and value that lets it through.
KINDS = {"waiting": ("waiting", "on"), "system": ("system", "on"), "summary": ("replies", "summary"),
         "full": ("replies", "full"), "files": ("files", "on"), "radio": ("radio", "on"), "cockpit": ("cockpit", "on")}
MAX_PARTS = 5  # one huge reply must not flood the channel
PART_CHARS = 3500  # leaves room for the header under Telegram's 4096
REPLY_BUDGET = 15.0  # seconds for all parts of one reply; the rest is dropped


def _path():
    return state_dir() / FILE


def channel() -> Optional[dict]:
    """The registered channel: {"id": int, "title": str, "bot": str}, or None. The file is only a record:
    anything that could widen where the relay sends goes through trusted_channel()."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("id"), int) else None


# Channel ids this process registered, or verified as a channel the owner administers, with the state folder
# they belong to and the monotonic time of the check. Any session can write alerts-channel.json and
# alerts-settings.json, so a file never decides where anything is sent: only an id that passes trusted_channel()
# does, and that is kept in memory only (never on disk). A check older than TRUST_FOR is made again.
_trusted: dict = {}
_untrusted: dict = {}  # the same key -> time of the last failed check
TRUST_FOR = 3600  # seconds the owner's administration of the channel is trusted before it is checked again
VERIFY_RETRY = 60  # seconds before an id that failed the check is checked again


def _verified(chat_id: int, cfg: Config, bot) -> bool:
    """The id is a channel (as confirm() requires) and the owner is one of its administrators."""
    try:
        chat = bot.call("getChat", {"chat_id": chat_id}) or {}
    except TelegramError:
        return False
    return chat.get("type") == "channel" and chat.get("id") == chat_id and _owner_is_admin(chat_id, cfg, bot)


def trusted_channel(cfg: Config, bot) -> Optional[int]:
    """The id of the registered channel, if it can be trusted as a target: registered or confirmed by this
    process, or else verified once through the bot. None otherwise."""
    chan = channel()
    if not chan:
        return None
    key = (str(state_dir()), chan["id"])
    now = time.monotonic()
    if key in _trusted and now - _trusted[key] < TRUST_FOR:
        return chan["id"]
    last = _untrusted.get(key)
    if last is not None and now - last < VERIFY_RETRY:
        return None
    if _verified(chan["id"], cfg, bot):
        _trusted[key] = now
        _untrusted.pop(key, None)
        return chan["id"]
    _trusted.pop(key, None)
    _untrusted[key] = now
    return None


def _sanitize(raw, fill: bool = True) -> dict:
    out = dict(DEFAULTS) if fill else {}
    if isinstance(raw, dict):
        for key, choices in CHOICES.items():
            if isinstance(raw.get(key), str) and raw[key] in choices:
                out[key] = raw[key]
    return out


def _mac(cfg: Config, data: dict) -> Optional[str]:
    """A signature only the bot's owner can make: keyed by the bot token, so a session that cannot read the
    token (the case the relay exists for) cannot forge a settings file."""
    token = get_token(cfg)
    if not token:
        return None
    key = hashlib.sha256(b"harnessgram-alerts:" + token.encode()).digest()
    return hmac.new(key, json.dumps(data, sort_keys=True).encode(), hashlib.sha256).hexdigest()


# The last choices this process saved or read from a valid signed file: (state folder, channel id) -> (counter,
# choices). The listener serves these, so a signed copy written back later (a rollback) changes nothing there.
# A fresh process (a hook) has nothing yet and cannot tell an old signed copy from a new one by itself: that
# is why a sender with the token asks the listener first (state()) and reads the file only when it is down.
_saved: dict = {}


def _signed(cfg: Config, choices: dict, channel_id: int, counter: int) -> Optional[str]:
    return _mac(cfg, {"choices": choices, "channel": channel_id, "n": counter})


def _read_signed(cfg: Config, channel_id: int, shape: bool = False):
    """(counter, choices) of the settings file when it is signed for this channel, else None. The signature is
    checked against exactly the choice keys the file holds (a file signed before a key was added has fewer);
    keys it lacks get their default. With `shape`: (counter, choices, whether the file holds every key)."""
    try:
        raw = json.loads((state_dir() / SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("n"), int) or isinstance(raw.get("n"), bool):
        return None
    held = _sanitize({k: v for k, v in raw.items() if k in CHOICES}, fill=False)
    expected = _signed(cfg, held, channel_id, raw["n"])
    mac = raw.get("mac")
    if not (expected and isinstance(mac, str) and hmac.compare_digest(expected, mac)):
        return None
    choices = {**DEFAULTS, **held}
    return (raw["n"], choices, set(held) == set(CHOICES)) if shape else (raw["n"], choices)


def settings(cfg: Optional[Config] = None) -> dict:
    """The owner's choices. A missing or corrupt file, or a value that is not an allowed one, means the default.
    With `cfg` the choices must be ones the owner saved (set_option()): signed for this channel with a counter
    that never goes down in this process, so a file written by a session, a copy for another channel, or an
    old copy written back all fall back to what this process last knew, or else the defaults. Without `cfg`
    the file is read as a hint only."""
    if cfg is None:
        try:
            return _sanitize(json.loads((state_dir() / SETTINGS_FILE).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return dict(DEFAULTS)
    chan = channel()
    if not chan:
        return dict(DEFAULTS)
    key = (str(state_dir()), chan["id"])
    known = _saved.get(key)
    read = _read_signed(cfg, chan["id"], shape=True)
    if read and (known is None or read[0] >= known[0]):
        _saved[key] = known = read[:2]
        if not read[2]:  # signed before a choice was added: keep the owner's choices, sign the full set again
            try:
                save_settings(cfg, read[1], chan["id"])
                known = _saved[key]
            except Exception:
                pass
    return dict(known[1]) if known else dict(DEFAULTS)


def save_settings(cfg: Config, data: dict, channel_id: Optional[int] = None) -> None:
    """Write the choices signed for the channel, with a counter above every one seen so far."""
    chan = channel()
    channel_id = channel_id if channel_id is not None else (chan["id"] if chan else 0)
    key = (str(state_dir()), channel_id)
    data = _sanitize(data)
    read = _read_signed(cfg, channel_id)
    top = max(read[0] if read else 0, _saved[key][0] if key in _saved else 0)
    counter = max(top + 1, time.time_ns() // 1_000_000)  # time keeps a re-registered channel above its old copies
    mac = _signed(cfg, data, channel_id, counter)
    _saved[key] = (counter, data)
    write_private(state_dir() / SETTINGS_FILE, json.dumps(dict(data, n=counter, mac=mac) if mac else data))


def _forget_settings(cfg: Config, *channel_ids: int) -> None:
    """Back to the defaults. The counters of those channels (in memory, and of the file being removed) are
    kept, so an old signed copy written back after a reconnect is not taken for a newer one."""
    for channel_id in channel_ids:
        key = (str(state_dir()), channel_id)
        read = _read_signed(cfg, channel_id)
        top = max(read[0] if read else 0, _saved[key][0] if key in _saved else 0)
        if top:
            _saved[key] = (top + 1, dict(DEFAULTS))  # above the copy being removed, so that copy stays dead
    try:
        (state_dir() / SETTINGS_FILE).unlink()
    except OSError:
        pass


def enabled(kind: str, cfg: Optional[Config] = None) -> bool:
    """Whether the owner lets this kind of alert through (an unknown kind never does). Pass `cfg` to count
    only choices the owner saved (see settings()); without it this is a hint, never a permission."""
    pair = KINDS.get(kind)
    return bool(pair) and settings(cfg).get(pair[0]) == pair[1]


def set_option(key: str, value: str, cfg: Optional[Config] = None) -> bool:
    """Save one choice (signed when `cfg` is given); False (nothing written) unless both are allowed ones."""
    if key not in CHOICES or value not in CHOICES[key]:
        return False
    current = settings(cfg)
    current[key] = value
    if cfg is not None:
        save_settings(cfg, current)
    else:
        write_private(state_dir() / SETTINGS_FILE, json.dumps(current))
    return True


def state(cfg: Config, bot) -> Optional[tuple]:
    """(channel, settings) for a process that is about to send, or None: no channel, or one that cannot be
    trusted. A process without the bot token asks the listener (the relay) and so never decides the target
    itself; one with the token asks the listener too and, only when it is down, checks the channel through
    the bot, in memory only."""
    if not channel():
        return None  # nothing registered: no network for the common case
    if getattr(bot, "direct", True) is False:
        listener = _ask_listener()
        return listener[1] if listener else None
    listener = _ask_listener()  # the listener first: its copy of the choices cannot be rolled back
    if listener:
        return listener[1]
    chan = channel()
    if not chan or trusted_channel(cfg, bot) != chan["id"]:
        return None
    return chan, settings(cfg)


def _ask_listener() -> Optional[tuple]:
    """(True, state) with what the listener says (state None: no trusted channel), or None when the
    listener cannot be reached."""
    try:
        reply = relay_call({"kind": "alerts-state"}, timeout=5)
    except TelegramError:
        return None
    return True, _from_relay(reply)


def _from_relay(reply) -> Optional[tuple]:
    chan = reply.get("channel") if isinstance(reply, dict) else None
    if not (isinstance(chan, dict) and isinstance(chan.get("id"), int)):
        return None
    return ({"id": chan["id"], "title": str(chan.get("title") or ""), "bot": str(chan.get("bot") or "")},
            _sanitize(reply.get("settings")))


def public_state(cfg: Config, bot) -> dict:
    """What the relay tells a token-less sender: the trusted channel and the owner's saved choices."""
    chan = channel()
    if not chan or trusted_channel(cfg, bot) != chan["id"]:
        return {"channel": None}
    return {"channel": {"id": chan["id"], "title": chan.get("title", ""), "bot": chan.get("bot", "")},
            "settings": settings(cfg)}


def allowed(kind: str, cfg: Config, bot) -> bool:
    """Whether an alert of this kind may be sent now: a trusted channel, and the owner's saved choice."""
    got = state(cfg, bot)
    pair = KINDS.get(kind)
    return bool(got and pair and got[1].get(pair[0]) == pair[1])


def _owner_is_admin(chat_id: int, cfg: Config, bot) -> bool:
    try:
        admins = bot.call("getChatAdministrators", {"chat_id": chat_id}) or []
    except TelegramError:
        return False
    return any((a.get("user") or {}).get("id") == cfg.owner_chat_id for a in admins if isinstance(a, dict))


def _register(chat: dict, cfg: Config, bot) -> None:
    try:
        username = (bot.call("getMe") or {}).get("username") or ""
    except TelegramError:
        username = ""
    previous = channel()
    if not previous or previous["id"] != chat["id"]:
        _forget_settings(cfg, chat["id"], *([previous["id"]] if previous else []))  # a new channel has other readers: it starts from the defaults, never from the old choices
    write_private(_path(), json.dumps({"id": chat["id"], "title": chat.get("title", ""), "bot": username},
                                      ensure_ascii=False))
    _trusted[(str(state_dir()), chat["id"])] = time.monotonic()  # the owner's own action: trusted from now on
    ui_lang = getattr(cfg, "ui_language", "en")
    for target, key in ((chat["id"], "alerts_channel_intro"), (cfg.owner_chat_id, "alerts_channel_connected")):
        try:
            bot.call("sendMessage", {"chat_id": target, "text": t(key, lang=ui_lang)})
        except TelegramError:
            pass
    try:
        show({}, cfg, bot)  # the owner sees at once what is sent, and can change it
    except Exception:
        pass


def joined(update: dict, cfg: Config, bot) -> str:
    """A my_chat_member update: the bot became (or stopped being) an administrator of a channel.
    Only the owner's own action counts: the update must say the owner did it. Returns
    registered | removed | ignored."""
    chat = update.get("chat") or {}
    if chat.get("type") != "channel" or not isinstance(chat.get("id"), int):
        return "ignored"
    if (update.get("from") or {}).get("id") != cfg.owner_chat_id:
        return "ignored"
    status = (update.get("new_chat_member") or {}).get("status")
    if status == "administrator":
        _register(chat, cfg, bot)
        return "registered"
    if status in ("left", "kicked", "member"):
        current = channel()
        if current and current["id"] == chat["id"]:
            try:
                _path().unlink()
            except OSError:
                pass
            _forget_settings(cfg, chat["id"])
            _trusted.pop((str(state_dir()), chat["id"]), None)
            return "removed"
    return "ignored"


PROPOSE_EVERY = 86400  # seconds: ask about the same channel at most once a day


def propose(post: dict, cfg: Config, bot, now: Optional[float] = None) -> str:
    """A post in a channel where the bot is an administrator, with no channel registered (the join was
    missed). A post has no sender, so nothing is saved: the owner is asked in the private chat and
    confirms with a button (confirm())."""
    chat = post.get("chat") or {}
    if chat.get("type") != "channel" or not isinstance(chat.get("id"), int) or channel():
        return "ignored"
    if not _owner_is_admin(chat["id"], cfg, bot):
        return "ignored"
    now = time.time() if now is None else now
    path = state_dir() / "alerts-proposed.json"
    try:
        asked = json.loads(path.read_text(encoding="utf-8"))
        asked = asked if isinstance(asked, dict) else {}
    except (OSError, ValueError):
        asked = {}
    last = asked.get(str(chat["id"]))
    if isinstance(last, (int, float)) and now - last < PROPOSE_EVERY:
        return "ignored"
    ui_lang = getattr(cfg, "ui_language", "en")
    try:
        bot.call("sendMessage", {"chat_id": cfg.owner_chat_id,
                                 "text": t("alerts_confirm_text", lang=ui_lang, title=chat.get("title") or "?"),
                                 "reply_markup": {"inline_keyboard": [[{
                                     "text": t("alerts_confirm_btn", lang=ui_lang),
                                     "callback_data": f"alerts:use:{chat['id']}"}],
                                     menus.not_now_row(ui_lang)]}})
    except TelegramError:
        return "ignored"
    asked[str(chat["id"])] = now
    write_private(path, json.dumps(dict(list(asked.items())[-20:])))
    return "proposed"


def confirm(q: dict, cfg: Config, bot) -> str:
    """The owner pressed the button of propose(): only from the owner, in the private chat, for a
    channel the owner administers. Returns registered | ignored."""
    ui_lang = getattr(cfg, "ui_language", "en")
    message = q.get("message") or {}
    chat_type = (message.get("chat") or {}).get("type")
    prefix, _, raw = (q.get("data") or "").rpartition(":")
    if (prefix != "alerts:use" or (q.get("from") or {}).get("id") != cfg.owner_chat_id
            or chat_type != "private" or (message.get("chat") or {}).get("id") != cfg.owner_chat_id):
        return "ignored"
    try:
        chat_id = int(raw)
        chat = bot.call("getChat", {"chat_id": chat_id}) or {}
    except (ValueError, TelegramError):
        return "ignored"
    if chat.get("type") != "channel" or chat.get("id") != chat_id or not _owner_is_admin(chat_id, cfg, bot):
        return "ignored"
    _register(chat, cfg, bot)
    try:
        bot.call("editMessageReplyMarkup", {"chat_id": cfg.owner_chat_id, "message_id": message.get("message_id"),
                                            "reply_markup": {"inline_keyboard": []}})
    except TelegramError:
        pass
    return "registered"


def topic_name(session: str) -> str:
    try:
        return str((TopicMap(state_dir() / "topics.json", None, 0).all().get(session or "") or {}).get("name") or "")
    except Exception:
        return ""


def _clean(text: str) -> str:
    """What may leave for the channel: owner-only links first (the whole link, host included), then the
    secret patterns. Cleaning catches known patterns, not everything."""
    return redact_channel(hide_owner_links(text))


def _header(cfg: Config, session: str) -> str:
    name = topic_name(session)
    mark = L.RLM if getattr(cfg, "ui_language", "en") == "he" else ""
    return f"{mark}💬 {name}" if name else ""


def topic_link(cfg: Config, session: str) -> str:
    """The session's topic in the group, else the group's General topic; "" when there is no group."""
    from .panel_view import topic_url
    try:
        entry = TopicMap(state_dir() / "topics.json", None, 0).all().get(session or "") or {}
    except Exception:
        entry = {}
    chat = entry.get("chat") if type(entry.get("chat")) is int else home_chat(cfg)
    thread = entry.get("thread")
    return (topic_url(chat, thread) if type(thread) is int else "") or topic_url(home_chat(cfg), 1)


def _button(chan: dict, ui_lang: str, url: str, label: str = "alerts_open_reply", topic: str = "") -> Optional[dict]:
    if url:  # a summary links straight to its reply in the topic
        return {"inline_keyboard": [[{"text": t(label, lang=ui_lang), "url": url}]]}
    if topic:  # no reply link: open the session's topic (or the group), never the bot's private chat
        return {"inline_keyboard": [[{"text": t("alerts_open_chat", lang=ui_lang), "url": topic}]]}
    if re.fullmatch(r"[A-Za-z0-9_]{3,64}", chan.get("bot") or ""):  # the channel is one-way: a button opens the bot chat to answer
        return {"inline_keyboard": [[{"text": t("alerts_open_chat", lang=ui_lang), "url": "https://t.me/" + chan["bot"]}]]}
    return None


def alert(cfg: Config, text: str, session: str = "", bot=None, silent: bool = False,
          url: str = "", kind: str = "system", html: bool = False, label: str = "alerts_open_reply") -> bool:
    """Send a copy of `text` to the alerts channel, headed by the session's topic name. A no-op
    (False) until a channel is registered, when the owner has turned this `kind` off, or while the
    flood gate is closed; never raises. `html`: `text` is Telegram HTML (the caller escaped it); it is still cleaned.
    """
    try:
        bot = bot or Bot.from_config(cfg)
        got = state(cfg, bot)
        if not got or KINDS.get(kind) is None or got[1].get(KINDS[kind][0]) != KINDS[kind][1]:
            return False
        chan = got[0]
        name = topic_name(session)
        ui_lang = getattr(cfg, "ui_language", "en")
        mark = L.RLM if ui_lang == "he" else ""
        body = text if not name else f"{mark}💬 {name}\n{text}"
        params = {"chat_id": chan["id"], "text": _clean(body)}
        if silent:
            params["disable_notification"] = True
        if html:
            params["parse_mode"] = "HTML"
        markup = _button(chan, ui_lang, url, label, "" if url else topic_link(cfg, session))
        if markup:
            params["reply_markup"] = markup
        flood.optional_call(bot, "sendMessage", params, timeout=5)
        return True
    except Exception:
        return False


def plain_text(md: str) -> str:
    """A reply as plain text: link targets, code fences, heading marks and bold marks removed."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md or "")
    text = re.sub(r"```[^\n]*\n?", "", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    return re.sub(r"\*\*|`", "", text)


def send_reply(cfg: Config, text: str, session: str = "", bot=None, url: str = "") -> bool:
    """The full reply, as the owner got it in the topic, to the alerts channel: cleaned plain text,
    silent, at most MAX_PARTS messages (then a note that the rest is in the topic), the link button
    on the first part. Stops at the first failed part; when the time budget runs out the note is left
    too. `text` must be the owner-only-link-free text (the placeholders hidden). Never raises."""
    try:
        bot = bot or Bot.from_config(cfg)
        got = state(cfg, bot)
        if not got or got[1]["replies"] != "full":
            return False
        chan = got[0]
        ui_lang = getattr(cfg, "ui_language", "en")
        parts = fmt.chunks(fmt.unmix(_clean(plain_text(text))), PART_CHARS)
        if not parts:
            return False
        cut = len(parts) > MAX_PARTS
        parts = parts[:MAX_PARTS]
        head = _header(cfg, session)
        deadline = time.monotonic() + REPLY_BUDGET

        def post(body: str, markup: Optional[dict], timeout: float) -> None:
            params = {"chat_id": chan["id"], "text": _clean(body), "disable_notification": True,
                      "link_preview_options": {"is_disabled": True}}
            if markup:
                params["reply_markup"] = markup
            flood.optional_call(bot, "sendMessage", params, timeout=timeout)

        for i, part in enumerate(parts):
            left = deadline - time.monotonic()
            if i and left < 1:  # out of time: leave the note instead of the rest
                try:
                    post((head + "\n" if head else "") + t("alerts_reply_cut", lang=ui_lang),
                         _button(chan, ui_lang, url, "alerts_more_in_topic") if url else None, 3)
                except Exception:
                    pass
                return False
            last = i == len(parts) - 1
            title = " ".join(filter(None, (head, f"({i + 1}/{len(parts)})" if len(parts) > 1 else "")))
            body = (title + "\n" if title else "") + part + ("\n\n" + t("alerts_reply_cut", lang=ui_lang) if last and cut else "")
            markup = _button(chan, ui_lang, url, "alerts_more_in_topic" if last and cut else "alerts_open_reply") if (
                i == 0 or (last and cut and url)) else None
            post(body, markup, max(1, min(5, left)))
        return True
    except Exception:
        return False


def send_file(cfg: Config, session: str, title: str, method: str, field: str, filename: str, payload: bytes,
              caption: str = "", bot=None, media_fields=None) -> bool:
    """A file the session sent to the topic, sent silently to the alerts channel too when the owner
    turned files on. The caption and the file name are cleaned; the file itself cannot be. Never raises."""
    try:
        bot = bot or Bot.from_config(cfg)
        got = state(cfg, bot)
        if not got or got[1]["files"] != "on":
            return False
        chan = got[0]
        name = topic_name(session) or title
        mark = L.RLM if getattr(cfg, "ui_language", "en") == "he" else ""
        text = _clean((f"{mark}💬 {name}\n" if name else "") + "📎 " + (caption or filename))
        fields = {"chat_id": chan["id"], "disable_notification": "true", "caption": text[:1000]}
        fields.update({k: _clean(v) if isinstance(v, str) else v for k, v in (media_fields or {}).items()})
        flood.optional_upload(bot, method, fields, field, _clean(filename) or "file", payload, timeout=60)
        return True
    except Exception:
        return False


# -- /alerts: the owner's settings, one message edited in place --------------------------------------

_CODES = {"w": "waiting", "s": "system", "r": "replies", "f": "files", "o": "radio", "c": "cockpit"}
_BUTTON = re.compile(r"al:([wsrfoc]):([a-z]+)")


def _screen(cfg: Config) -> tuple:
    """(text, keyboard) of the settings message. One button per choice shows its value; a tap moves it
    to the next value."""
    ui_lang = getattr(cfg, "ui_language", "en")
    chan = channel()
    if not chan:
        return t("alerts_none", lang=ui_lang), []
    current = settings(cfg)
    rows = []
    for code, key in _CODES.items():
        choices = CHOICES[key]
        nxt = choices[(choices.index(current[key]) + 1) % len(choices)]
        rows.append([{"text": t(f"alerts_btn_{key}", lang=ui_lang, value=t(f"alerts_val_{current[key]}", lang=ui_lang)),
                      "callback_data": f"al:{code}:{nxt}"}])
    text = t("alerts_settings", lang=ui_lang, title="\u2068" + (chan.get("title") or "?") + "\u2069")  # a title in another script must not reorder the line
    if current["replies"] == "full":
        text += "\n\n" + t("alerts_full_warning", lang=ui_lang)
    if current["files"] == "on":
        text += "\n\n" + t("alerts_files_warning", lang=ui_lang)
    rows.append(menus.close_row(ui_lang))
    return text, rows


def show(m: dict, cfg: Config, bot) -> str:
    """/alerts from the owner (the caller has checked that): the settings, or how to connect a channel."""
    text, rows = _screen(cfg)
    params = {"chat_id": home_chat(cfg), "text": text, "disable_notification": True}
    if rows:
        params["reply_markup"] = {"inline_keyboard": rows}
    if m.get("message_thread_id"):
        params["message_thread_id"] = m["message_thread_id"]
    try:
        bot.call("sendMessage", params)
    except TelegramError:
        pass
    return "alerts-settings"


def _answer(bot, q: dict, text: str) -> None:
    try:
        bot.call("answerCallbackQuery", {"callback_query_id": q.get("id") or "", "text": text})
    except TelegramError:
        pass


def button(q: dict, cfg: Config, bot) -> str:
    """A press on the settings message. Only the owner, in the home chat (the caller has checked that
    and it is checked again here); the data must match exactly and name an allowed value, otherwise
    nothing changes. Returns changed | ignored; never raises."""
    ui_lang = getattr(cfg, "ui_language", "en")
    message = q.get("message") or {}
    chat = message.get("chat") or {}
    try:
        match = _BUTTON.fullmatch(q.get("data") or "")
        if (not match or (q.get("from") or {}).get("id") != cfg.owner_chat_id or chat.get("id") != home_chat(cfg)
                or (q.get("from") or {}).get("is_bot")):
            _answer(bot, q, t("toast_not_available", lang=ui_lang))
            return "ignored"
        if not channel():  # the channel was removed since this message was drawn
            try:
                bot.call("editMessageText", {"chat_id": home_chat(cfg), "message_id": message.get("message_id"),
                                             "text": t("alerts_none", lang=ui_lang), "reply_markup": {"inline_keyboard": []}})
            except TelegramError:
                pass
            _answer(bot, q, t("toast_not_available", lang=ui_lang))
            return "ignored"
        key, value = _CODES[match.group(1)], match.group(2)
        if not set_option(key, value, cfg):
            _answer(bot, q, t("toast_not_available", lang=ui_lang))
            return "ignored"
        text, rows = _screen(cfg)
        toast = {("files", "on"): "alerts_toast_files_on", ("replies", "full"): "alerts_toast_full_on"}.get(
            (key, value), "alerts_toast_saved")
        toast = t(toast, lang=ui_lang)
        try:
            bot.call("editMessageText", {"chat_id": home_chat(cfg), "message_id": message.get("message_id"),
                                         "text": text, "reply_markup": {"inline_keyboard": rows}})
        except TelegramError:
            pass
        _answer(bot, q, toast)
        return "changed"
    except Exception:
        _answer(bot, q, t("alerts_failed", lang=ui_lang))
        return "ignored"
