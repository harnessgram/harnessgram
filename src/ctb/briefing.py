"""The morning briefing: a two-minute start of the day for the owner, built from what the bridge already holds.

At the wake time (`briefing.time` in the config, "HH:MM" in `timezone`, default 07:00 Asia/Jerusalem) the heartbeat
builds it once a day:
- today's schedule (schedule.py: his own items and read-only calendar events) and today's home chores and routine
  (family_board.py);
- what the agents finished overnight, from the owner ledger (work_map.py);
- what waits for him across projects, the top five, each with what it changes for him.

It is delivered three ways, each only for the owner: a dismissable "Good morning" card on Cockpit Now
(`state["morning"]`, `/api/briefing-dismiss`), a short silent text in the home chat, and, when the home radio is on,
spoken on the radio's private home stream (`radio.say`; the YouTube staff edition has its own pipeline and never
reads that spool). The text passes the radio's child-suitability check (an item that fails is shown by its project
name only), the spoken script also the radio's privacy scrub.

Quiet days: Saturday, the family board's own Shabbat times, and every window of `voice_digest_quiet_windows` (the weekly Sabbath and the dated holiday
windows the owner entered; the bridge infers no holiday itself). `/briefing` sends it now (and says so on a quiet day
instead). Its switch is the add-on `morning_briefing` (addons.py).
"""
from __future__ import annotations

import html
import importlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from . import fmt, work_lines, work_map
from . import lang as L
from .config import Config, home_chat, private_dir, state_dir, write_private
from .i18n import t

ADDON = "morning_briefing"
STATE_FILE = "briefing.json"
DEFAULT_TIME = "07:00"
LATE_AFTER = 3 * 3600  # a briefing not sent within this long after the wake time is skipped for the day
OVERNIGHT_WINDOW = 14 * 3600
TOP_WAITING = 5
MAX_SCHEDULE = 8
MAX_OVERNIGHT = 5
MAX_CHORES = 6
SAY_ITEMS = 3
_TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


def valid_section(value) -> bool:
    if not isinstance(value, dict):
        return False
    when = value.get("time", DEFAULT_TIME)
    return isinstance(when, str) and bool(_TIME.fullmatch(when))


def enabled(cfg: Config) -> bool:
    from . import addons
    try:
        return addons.enabled(cfg, "morning_briefing")
    except Exception:
        return True


def wake_time(cfg: Config) -> tuple:
    conf = getattr(cfg, "briefing", None) or {}
    when = conf.get("time") if isinstance(conf.get("time"), str) else DEFAULT_TIME
    found = _TIME.fullmatch(when) or _TIME.fullmatch(DEFAULT_TIME)
    return int(found.group(1)), int(found.group(2))


def _local(cfg: Config, now: float) -> datetime:
    from . import schedule
    return datetime.fromtimestamp(now, schedule.zone(cfg))


def rest_day(cfg: Config, now: float) -> bool:
    """Saturday, or inside one of the owner's configured Sabbath/holiday windows (voice_digest_quiet_windows).
    An unreadable window list counts as a rest day: the briefing is never sent on a guess."""
    from . import voice_digest as V
    local = _local(cfg, now).replace(tzinfo=None)
    if local.weekday() == 5:
        return True
    try:
        from . import family_board
        if family_board.is_shabbat(cfg, _local(cfg, now)):
            return True  # the family board's own Shabbat times (candle-lighting to nightfall)
    except Exception:
        pass
    windows = getattr(cfg, "voice_digest_quiet_windows", None)
    if not V.valid_windows(windows):
        return True
    point = local.weekday() * 1440 + local.hour * 60 + local.minute
    for window in windows:
        a, b = V._endpoint(window["start"]), V._endpoint(window["end"])
        if isinstance(a, int):
            if (point - a) % V.WEEK < (b - a) % V.WEEK:
                return True
        elif a <= local < b:
            return True
    return False


# --- the content --------------------------------------------------------------------------------

def _schedule_rows(cfg: Config, now: float) -> List[dict]:
    from . import schedule
    try:
        view = schedule.view(cfg, now=_local(cfg, now))
    except Exception:
        return []
    return [{"time": r.get("time") or "", "title": work_lines._text(r.get("title") or "")}
            for r in view["days"]["today"] if not r.get("done") and r.get("title")]


def _chores(cfg: Config, now: float) -> List[str]:
    """Today's open home chores and daily routine from the family board (family_board.py); nothing when the board
    cannot be read or when it is Shabbat (the board offers none then)."""
    from . import family_board
    try:
        view = family_board.view(cfg, now=_local(cfg, now))
        rows = [] if view["shabbat"] else view["days"][view["day"]]["chores"] + view["daily"]
    except Exception:
        return []
    return [work_lines._text(r["text"]) for r in rows if isinstance(r, dict) and not r.get("done") and r.get("text")]


def _overnight(cfg: Config, now: float, folder: Optional[Path]) -> List[dict]:
    rows = []
    for key, found in work_map.ledger_items(work_map.ledger_dir(cfg) if folder is None else folder).items():
        for it in found["items"]:
            if it["status"] == "done" and 0 <= now - it["updated"] <= OVERNIGHT_WINDOW:
                rows.append({"project": found["name"], "what": work_map._short(it["text"], work_map.WHAT_CHARS),
                             "at": it["updated"]})
    rows.sort(key=lambda r: -r["at"])
    return [{k: v for k, v in r.items() if k != "at"} for r in rows]


def build(cfg: Config, entries, now: float, folder: Optional[Path] = None, maps: Optional[dict] = None,
          schedule_rows: Optional[List[dict]] = None, chores: Optional[List[str]] = None) -> dict:
    """The briefing as plain data. The keyword arguments replace a source (tests); left out, each is read."""
    maps = work_map.build_all(cfg, entries, now, folder=folder) if maps is None else maps
    sched = _schedule_rows(cfg, now) if schedule_rows is None else schedule_rows
    home = _chores(cfg, now) if chores is None else chores
    over = _overnight(cfg, now, folder)
    wait = work_map.waiting_lines(maps)
    out = {"date": _local(cfg, now).date().isoformat(), "built": int(now), "lang": work_map.lang_of(cfg),
           "schedule": sched[:MAX_SCHEDULE], "schedule_more": max(0, len(sched) - MAX_SCHEDULE),
           "chores": home[:MAX_CHORES], "overnight": over[:MAX_OVERNIGHT], "overnight_more": max(0, len(over) - MAX_OVERNIGHT),
           "waiting": wait[:TOP_WAITING], "waiting_more": max(0, len(wait) - TOP_WAITING)}
    return _suitable(out)


def _texts(data: dict):
    for key, field in (("schedule", "title"), ("chores", None), ("overnight", "what"), ("waiting", "what")):
        for i, row in enumerate(data[key]):
            yield key, i, field, (row[field] if field else row)


def _suitable(data: dict) -> dict:
    """Child-suitability (radio_suitability): an item whose words fail is shown by its project name only
    (a chore or an event, by a neutral dash); the briefing as a whole is checked again afterwards."""
    from . import radio_suitability as S
    whole = "\n".join(text for *_, text in _texts(data))
    if not whole or S.suitable(whole):
        return data
    for key, i, field, text in list(_texts(data)):
        if S.suitable(text):
            continue
        row = data[key][i]
        if field is None:
            data[key][i] = "–"
        else:
            row[field] = row.get("project") or "–"
            if key == "waiting":
                row["impact"] = ""
    return data


def empty(data: dict) -> bool:
    return not (data["schedule"] or data["chores"] or data["overnight"] or data["waiting"])


# --- the words ----------------------------------------------------------------------------------

def _label(text: str, lang: str) -> str:
    return work_map._label(text, lang)


def render(cfg: Config, data: dict) -> str:
    """The briefing as the HTML of a Telegram message (Hebrew-only headings; item words on their own lines)."""
    lang = data.get("lang") or work_map.lang_of(cfg)
    mark = L.RLM if lang == "he" else ""
    head = lambda key: f"\n<b>{html.escape(t(key, lang=lang), quote=False)}</b>"
    lines = [f"<b>{html.escape(t('br_title', lang=lang), quote=False)}</b>"]
    if data["schedule"]:
        lines.append(head("br_schedule"))
        for r in data["schedule"]:
            lines.append(f"{mark}• {r['time'] + ' ' if r['time'] else ''}{_label(r['title'], lang)}")
        if data["schedule_more"]:
            lines.append(t("br_more", lang=lang, n=data["schedule_more"]))
    else:
        lines.append(f"\n{t('br_none_today', lang=lang)}")
    if data["chores"]:
        lines.append(head("br_chores"))
        lines += [f"{mark}• {_label(c, lang)}" for c in data["chores"]]
    if data["overnight"]:
        lines.append(head("br_overnight"))
        for r in data["overnight"]:
            lines.append(f"{mark}• {_label(r['what'], lang)}")
        if data["overnight_more"]:
            lines.append(t("br_more", lang=lang, n=data["overnight_more"]))
    if data["waiting"]:
        lines.append(head("br_waiting"))
        for r in data["waiting"]:
            lines.append(f"{mark}• {_label(r['what'], lang)}")
            if r["what"] != r["project"]:
                lines.append(f"{mark}📁 {_label(r['project'], lang)}")
            if r.get("impact"):
                lines.append(f"{mark}↳ {_label(r['impact'], lang)}")
        if data["waiting_more"]:
            lines.append(t("br_more", lang=lang, n=data["waiting_more"]))
    else:
        lines.append(f"\n{t('br_quiet', lang=lang)}")
    return "\n".join(lines)[:4000]


def spoken(cfg: Config, data: dict) -> str:
    """The short script for the radio: counts first, then the top waiting items by project and (when Hebrew)
    their own words. At most radio.MAX_SAY characters, no links, no emoji."""
    from . import radio, radio_script as R
    lang = data.get("lang") or work_map.lang_of(cfg)
    parts = [t("br_say_open", lang=lang)]
    if data["schedule"]:
        parts.append(t("br_say_schedule", lang=lang, n=len(data["schedule"]) + data["schedule_more"]))
    if data["overnight"]:
        parts.append(t("br_say_overnight", lang=lang, n=len(data["overnight"]) + data["overnight_more"]))
    if data["waiting"]:
        parts.append(t("br_say_waiting", lang=lang, n=len(data["waiting"]) + data["waiting_more"]))
        for r in data["waiting"][:SAY_ITEMS]:
            said = R.spoken(r["project"])
            if r["what"] != r["project"] and (lang != "he" or R.hebrew_enough(r["what"], 0.6)):
                said += ", " + R.spoken(r["what"])
            parts.append(said + ".")
    else:
        parts.append(t("br_say_quiet", lang=lang))
    return R.whole(R.for_voice(" ".join(parts)), radio.MAX_SAY)


# --- the stored briefing and its delivery ---------------------------------------------------------

def _state() -> dict:
    try:
        data = json.loads((state_dir() / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    write_private(private_dir(state_dir()) / STATE_FILE, json.dumps(data, ensure_ascii=False))


def card(cfg: Config, now: float) -> Optional[dict]:
    """The briefing for the Cockpit's Now: today's, unless the owner dismissed it. None otherwise."""
    if not enabled(cfg):
        return None
    st = _state()
    data = st.get("data")
    today = _local(cfg, now).date().isoformat()
    if not isinstance(data, dict) or data.get("date") != today or st.get("dismissed") == today:
        return None
    return data


def dismiss(cfg: Config, now: float) -> None:
    st = _state()
    st["dismissed"] = _local(cfg, now).date().isoformat()
    _save(st)


def speak(cfg: Config, data: dict) -> bool:
    """On the radio's private home stream, only while the radio is on. True when the line was queued."""
    from . import radio, radio_privacy
    try:
        if not radio.job_pid():
            return False
        ids = radio_privacy.configured_identifiers(cfg)
        # scrubbed twice: the words first (before the voice drops an "@"), then the finished line
        text = radio_privacy.scrub(spoken(cfg, radio_privacy.scrub(data, identifiers=ids)), identifiers=ids)
        from . import radio_suitability
        if not radio_suitability.suitable(text):
            return False
        radio.say(text)
        return True
    except Exception:
        return False


def _send(cfg: Config, bot, text: str, chat: int, thread: Optional[int] = None) -> bool:
    from .telegram import TelegramError
    params = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_notification": True,
              "link_preview_options": {"is_disabled": True}}
    if thread:
        params["message_thread_id"] = thread
    try:
        bot.call("sendMessage", params)
        return True
    except TelegramError:
        return False


def deliver(cfg: Config, bot, data: dict, now: float, chat: Optional[int] = None, thread: Optional[int] = None) -> dict:
    """Store the briefing (the Cockpit card), send the text (to the home chat unless a chat is given) and speak
    it. Returns what happened: {"card", "text", "radio"}."""
    st = _state()
    st.update(data=data, dismissed="")
    _save(st)
    sent = _send(cfg, bot, render(cfg, data), home_chat(cfg) if chat is None else chat, thread)
    return {"card": True, "text": sent, "radio": speak(cfg, data)}


def tick(cfg: Config, bot, now: Optional[float] = None, entries=None, folder: Optional[Path] = None) -> bool:
    """The heartbeat's round: at the wake time, once a day, not on a quiet day. True when it was delivered."""
    now = time.time() if now is None else now
    if not enabled(cfg):
        return False
    local = _local(cfg, now)
    hour, minute = wake_time(cfg)
    due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    today = local.date().isoformat()
    if local < due or _state().get("sent") == today:
        return False
    st = _state()
    st["sent"] = today  # before anything is built or sent: a crash or a failure never repeats it
    _save(st)
    if (local - due).total_seconds() > LATE_AFTER or rest_day(cfg, now):
        return False
    if entries is None:
        from .sessions_menu import collect
        from .topics import TopicMap
        entries = collect(TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg), now=now)
    deliver(cfg, bot, build(cfg, entries, now, folder=folder), now)
    return True


def telegram_command(cfg: Config, bot, chat: int, thread: Optional[int], reply: Callable[[str], None],
                     now: Optional[float] = None, entries=None, folder: Optional[Path] = None) -> str:
    """`/briefing` from the owner: the briefing now, here (and on the Cockpit card and the radio)."""
    now = time.time() if now is None else now
    lang = work_map.lang_of(cfg)
    if not enabled(cfg):
        reply(t("br_off", lang=lang))
        return "briefing"
    if rest_day(cfg, now):
        reply(t("br_shabbat", lang=lang))
        return "briefing"
    if entries is None:
        from .sessions_menu import collect
        from .topics import TopicMap
        entries = collect(TopicMap(state_dir() / "topics.json", None, home_chat(cfg), cfg=cfg), now=now)
    deliver(cfg, bot, build(cfg, entries, now, folder=folder), now, chat=chat, thread=thread)
    return "briefing"
