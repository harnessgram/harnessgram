"""What Harnessgram adds to your agents: one fixed catalogue of everything the bridge puts into or around an agent
harness, with one switch each (docs/addons.md is generated from it).

An add-on that already has its own setting keeps it: `enabled` reads that setting (a config key, a Cockpit setting
from panel_settings.SETTINGS or a marker file), so a switch here and the same switch elsewhere are one thing. Only
behaviours that had no switch get a key in the private `addons.json` (0600) in the bridge state directory.

Safety add-ons and the bridge's own master switch are locked: they are shown, and never switched off from here.

Every read is fail-safe: an unreadable store, a damaged value or any other error answers the add-on's default, so a
hook never fails because of this module. Hooks that cannot even import it use the same default themselves.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .config import locked_map, read_map, setting_on, set_setting, state_dir

STORE = "addons.json"
# Every agent harness the bridge drives: name -> the module that implements it. A new harness module must be listed
# here, and at least one add-on must say it supports it (tests/test_addons.py).
HARNESSES = {"claude": "harness.py", "codex": "harness_codex.py"}
HARNESS_NAMES = {"claude": "Claude", "codex": "Codex"}
ALL = tuple(HARNESSES)
GROUPS = ("conversation", "memory", "voice", "cockpit", "safety")
# A Cockpit setting switched back on returns to the value it had; with none remembered, to this one.
ON_VALUES = {"auto_compact": 300, "voice_digest": "important", "cold_compact": "on"}
OFF_VALUES = {"auto_compact": 0, "voice_digest": "off", "cold_compact": "off"}


# Settings that stop the bridge, its listener or the Cockpit itself (or cut the owner off from them). No switchable
# add-on may map onto one: switching it off from the Cockpit could not be undone from the Cockpit.
MASTER_SWITCHES = frozenset({("config", "mirror"), ("config", "bot_token"), ("config", "bot_token_command"),
                             ("config", "owner_chat_id"), ("config", "chat_id"), ("config", "api_base"),
                             ("config", "panel_port"), ("config", "panel_button"), ("config", "panel_base_url"),
                             ("setting", "ui_v2")})


class AddonError(ValueError):
    code = "bad"


class Unknown(AddonError):
    code = "unknown"


class Locked(AddonError):
    code = "locked"


@dataclass(frozen=True)
class Addon:
    key: str
    group: str
    title_he: str
    title_en: str
    text_he: str
    text_en: str
    # ("config", attribute) | ("setting", panel_settings name) | ("marker", state file) | ("store",)
    # | ("locked", config attribute or "")
    source: Tuple[str, ...]
    providers: Tuple[str, ...] = ("claude",)
    default: bool = True
    # The branch that adds this add-on's gate, while it is not merged yet; its key already works here.
    pending: str = ""

    @property
    def locked(self) -> bool:
        return self.source[0] == "locked"


def _a(key, group, title_he, title_en, text_he, text_en, source, providers=("claude",), default=True, pending=""):
    return Addon(key, group, title_he, title_en, text_he, text_en, tuple(source), tuple(providers), default, pending)


CATALOGUE: Tuple[Addon, ...] = (
    # --- conversation -------------------------------------------------------------------------------------------
    # The bridge's master switch: off stops the listener, the heartbeat and every Cockpit route, this one included,
    # so the Cockpit only shows it (MASTER_SWITCHES).
    _a("reply_mirror", "conversation", "הגשר עצמו: תשובות בטלגרם", "The bridge itself: replies in Telegram",
       "המתג הראשי של הגשר. משנים אותו רק בקובץ ההגדרות במחשב, כדי שהקוקפיט לא יכבה את עצמו.",
       "The bridge's master switch. It changes only in the config file on the computer, so the Cockpit never "
       "switches itself off.", ("locked", "mirror"), ALL),
    _a("translation", "conversation", "תרגום ושורת סיכום", "Translation and the Summary line",
       "הסוכן כותב באנגלית ומסיים בשורת סיכום, ואתה מקבל את התשובה מתורגמת לשפה שלך.",
       "The agent writes in English and ends with a Summary line; you get the reply in your language.",
       ("config", "translate"), default=False),
    _a("context_footer", "conversation", "שורת גודל ההקשר", "Context size line",
       "שורה קטנה מתחת לתשובה מראה כמה מההקשר כבר בשימוש.",
       "A small line under each reply shows how much of the context is in use.", ("config", "context_footer")),
    _a("reply_index", "conversation", "אינדקס תשובות נעוץ", "Pinned reply index",
       "הודעה נעוצה בכל נושא מקשרת לתשובות האחרונות.",
       "A pinned message in each topic links to the latest replies.", ("config", "pin_replies")),
    _a("missed_replies", "conversation", "השלמת תשובה שלא הגיעה", "Missed reply redelivery",
       "אם התשובה האחרונה של הסוכן לא הגיעה לטלגרם, הגשר שולח אותה שוב.",
       "If an agent's last reply never reached Telegram, the bridge sends it again.", ("store",)),
    _a("topic_titles", "conversation", "שמות נושאים אוטומטיים", "Automatic topic names",
       "הגשר נותן לכל נושא שם של פרויקט ומשימה ושומר אותו מעודכן.",
       "The bridge names each topic after its project and task and keeps the name current.",
       ("config", "auto_topic_names"), default=False),
    _a("title_sync", "conversation", "שם השיחה לפי הנושא", "Session title from the topic",
       "שם הנושא בטלגרם הופך גם לשם השיחה אצל הסוכן.",
       "The Telegram topic's name also becomes the session's title in the agent.", ("store",)),
    _a("progress_cards", "conversation", "כרטיס עבודה חי", "Live work card",
       "הודעה אחת מתעדכנת עם הצעדים האחרונים של הסוכן בזמן העבודה.",
       "One message updates with the agent's latest steps while it works.", ("config", "progress"), default=False),
    _a("progress_reminder", "conversation", "תזכורת לכרטיס התקדמות", "Progress card reminder",
       "במשימה ארוכה הגשר מזכיר לסוכן לפתוח כרטיס התקדמות בקוקפיט.",
       "On a long task the bridge reminds the agent to open a progress card in the Cockpit.", ("store",)),
    _a("cockpit_hint", "conversation", "הדרכה על כלי הקוקפיט", "Cockpit tools guide",
       "הגשר מלמד את הסוכן להשתמש בלוח הנעוץ, בכרטיסים ובשאלות של כן או לא.",
       "The bridge teaches the agent the pin board, the cards and yes-or-no questions.", ("store",)),
    _a("rules_reminder", "conversation", "התזכורת הקבועה שלך", "Your standing reminder",
       "המשפט הקבוע שלך והתזכורת התקופתית מתווספים לבקשות שהסוכן מקבל.",
       "Your standing sentence and the periodic reminder are added to the agent's prompts.", ("store",)),
    # --- memory and continuity ----------------------------------------------------------------------------------
    _a("knowledge_overlay", "memory", "רשימת הפריטים הכבויים", "Switched-off items list",
       "הסוכנים מקבלים את רשימת הכללים והכלים שכיבית במסך „מה המערכת יודעת״.",
       "Agents receive the list of rules and tools you switched off in “What the system knows”.",
       ("store",), ("claude", "codex")),
    _a("auto_compact", "memory", "דחיסה אוטומטית", "Automatic compaction",
       "כשהשיחה ארוכה מדי, הגשר דוחס אותה בין משימות.",
       "When a conversation grows too long, the bridge compacts it between tasks.",
       ("setting", "auto_compact"), default=False),
    _a("cold_compact", "memory", "דחיסה אחרי הפסקה", "Compaction after a break",
       "אחרי שעה בלי פעילות, הגשר דוחס שיחה גדולה לפני ההודעה הבאה שלך.",
       "After an hour of quiet, the bridge compacts a large conversation before your next message.",
       ("setting", "cold_compact")),
    _a("compact_helper", "memory", "בקשת דחיסה מהסוכן", "Agent-requested compaction",
       "הסוכן יכול לבקש דחיסה בסוף משימה, ולציין מה חייב להישמר.",
       "The agent can ask for a compaction at the end of a task, naming what must be kept.", ("store",)),
    _a("recall_after_compact", "memory", "תזכורת אחרי דחיסה", "Recall after compaction",
       "אחרי דחיסה, הסוכן מקבל שוב את מה שחשוב לזכור.",
       "After a compaction the agent is reminded of what matters.", ("store",),
       pending="feat/recall-after-compact"),
    _a("rule_change_notice", "memory", "הודעה על שינוי כללים", "Rule change notice",
       "כשכלל משתנה, הסוכנים הפעילים שומעים על כך מיד.",
       "When a rule changes, the active agents hear about it right away.", ("store",),
       pending="feat/recall-after-compact"),
    # --- voice and radio ----------------------------------------------------------------------------------------
    _a("radio_replies", "voice", "הרדיו מקריא כל תשובה", "The radio reads every reply",
       "הרדיו אומר בקול כל תשובה, לא רק תשובות להודעות קוליות.",
       "The radio says every reply aloud, not only answers to voice notes.", ("setting", "radio_every_reply")),
    _a("radio_group_live", "voice", "שידור חי בקבוצה", "Live stream in the group",
       "הרדיו פותח בקבוצה שידור חי בטלגרם, עם תמונה. כבוי: ההאזנה רק פרטית, בנגן של האפליקציה ובקישור בספארי, "
       "והמחשב עובד פחות. השינוי חל בהפעלה הבאה של הרדיו.",
       "The radio opens a live stream with a picture in the group. Off: listening is private only, in the app "
       "player and the Safari link, and the computer works less. A change applies the next time the radio "
       "starts.", ("store",), ALL),
    _a("voice_digest", "voice", "סיכומים קוליים", "Voice summaries",
       "הגשר אוסף תשובות לסיכום קולי פרטי, מחוץ לשעות השקט.",
       "The bridge gathers replies into a private voice summary, outside quiet hours.", ("setting", "voice_digest")),
    _a("car_mode", "voice", "מצב רכב", "Car mode",
       "סיכומים מגיעים עם התראה כדי שיוקראו בקול, והכתבות הולכות לשיחה האחרונה.",
       "Summaries arrive with an alert so they are read aloud, and dictations go to the last session.",
       ("marker", "car-mode"), default=False),
    _a("car_voice", "voice", "הקראה במצב רכב", "Read aloud in car mode",
       "במצב רכב, הסיכום נשלח גם כהודעה קולית.",
       "In car mode the summary is also sent as a voice message.", ("config", "voice")),
    _a("voice_echo", "voice", "הד לתמלול כשאין תצוגה מקדימה", "Echo when sent without a preview",
       "הקלטה שנשלחת לסוכן בלי אישור התמלול (מצב רכב או תקלה) מקבלת תגובה שקטה עם מה ששמעתי.",
       "A recording that goes to the agent without the transcript preview (car mode or a failed preview) gets a "
       "quiet reply with what was heard.", ("store",)),
    _a("listen_link", "voice", "האזנה בספארי", "Listen in Safari",
       "כפתור ברדיו פותח קישור פרטי לדף האזנה בספארי, שממשיך לנגן ברקע ובמסך הנעילה.",
       "A radio button opens a private link to a listening page in Safari, which keeps playing in the background "
       "and on the lock screen.", ("store",), ALL),
    _a("listen_button", "voice", "כפתור האזנה", "Listen button",
       "כפתור מתחת לתשובה משמיע אותה בקול.",
       "A button under a reply plays it aloud.", ("store",), pending="feat/listen-button"),
    # --- Cockpit ------------------------------------------------------------------------------------------------
    _a("morning_brief", "cockpit", "תדריך הבוקר", "Morning Brief",
       "מסך הבית של הקוקפיט מראה מה השתנה ומה מחכה לך.",
       "The Cockpit home shows what changed and what waits for you.", ("setting", "brief_home"), ALL, default=False),
    _a("schedule_card", "cockpit", "הלו״ז שלי", "My schedule",
       "כרטיס של היום ומחר במסך עכשיו, עם הרשימה הפרטית שלך.",
       "A Today and Tomorrow card on Now, with your private list.", ("store",), ALL),
    _a("work_map", "cockpit", "מפת עבודה", "Work map",
       "לכל פרויקט מפה של מה שהסוכנים עושים, מה מחכה לך ומה זה משנה לך: בקוקפיט, בהודעה נעוצה ובפקודה בטלגרם.",
       "Each project gets a map of what the agents are doing, what waits for you and what it changes for you: in the "
       "Cockpit, in a pinned message and with /what.", ("store",), ALL),
    _a("morning_briefing", "cockpit", "תדריך בוקר", "Morning briefing",
       "בשעת הבוקר שבחרת מגיע תדריך קצר: הלו״ז, מה הושלם בלילה ומה מחכה לך. כרטיס בקוקפיט, הודעה ובקול ברדיו הפרטי.",
       "At your morning time a short briefing arrives: the schedule, what finished overnight and what waits for you. "
       "A Cockpit card, a message and, when the radio is on, spoken on the private stream.", ("store",), ALL),
    _a("app_request", "cockpit", "בקשת אפליקציה", "Request an app",
       "כפתור אחד שולח קישור לאפליקציה לאישור.",
       "One button sends an app link for approval.", ("store",), ALL),
    _a("mac_tools", "cockpit", "תחזוקת המחשב", "Mac maintenance",
       "ניקוי ותחזוקה של המחשב בלחיצה, אחרי תצוגה מקדימה.",
       "Clean and maintain the Mac in one tap, after a preview.", ("store",), ALL),
    _a("topic_cleanup", "cockpit", "סגירת נושאים ישנים", "Old topic cleanup",
       "הקוקפיט מציע לסגור נושאים שלא הייתה בהם פעילות זמן רב.",
       "The Cockpit offers to close topics that have been quiet for a long time.", ("store",), ALL),
    # --- safety (locked) ----------------------------------------------------------------------------------------
    _a("approval_gates", "safety", "אישור לפעולות רגישות", "Approval for sensitive actions",
       "פעולה רגישה מחכה לאישור שלך ואף פעם לא מאושרת מעצמה.",
       "A sensitive action waits for your approval and is never approved by itself.", ("locked", "")),
    _a("secret_redaction", "safety", "הסתרת סיסמאות ומפתחות", "Secret redaction",
       "מה שנראה כמו סיסמה או מפתח מוחלף לפני שהטקסט יוצא מהמחשב.",
       "Anything that looks like a password or a key is replaced before text leaves the computer.",
       ("locked", "redact_outgoing"), ALL),
    _a("owner_only", "safety", "רק אתה שולט", "Owner-only control",
       "רק החשבון שלך יכול לאשר, לשנות הגדרות ולכתוב לסוכנים.",
       "Only your account can approve, change settings and write to the agents.", ("locked", ""), ALL),
    _a("sensitive_wording", "safety", "ניסוח מדויק לשאלה רגישה", "Exact wording for sensitive questions",
       "שאלה על פעולה רגישה חייבת לנקוב בפעולה אחת בדיוק, והכרטיס שלה מסומן באדום.",
       "A question about a sensitive action must name exactly one action, and its card is marked red.",
       ("locked", "")),
)
BY_KEY: Dict[str, Addon] = {a.key: a for a in CATALOGUE}


# --- reading ----------------------------------------------------------------------------------------------------

def _path():
    return state_dir() / STORE


def read_store() -> dict:
    """The private switch file as a dict; {} when it is missing, damaged or cannot be read. A hook reads it once and
    passes it to every `enabled` call of that hook."""
    try:
        return read_map(_path())
    except Exception:
        return {}


def _settings():
    from . import panel_settings  # lazy: panel_settings pulls in the Cockpit modules
    return panel_settings


def _raw(cfg, a: Addon, store: dict) -> bool:
    kind = a.source[0]
    if kind == "store":
        mapping = store.get("on") if isinstance(store, dict) else None
        value = mapping.get(a.key, a.default) if isinstance(mapping, dict) else a.default
        return value if isinstance(value, bool) else a.default
    if kind == "config":
        if cfg is None:
            return a.default
        value = getattr(cfg, a.source[1], a.default)
        return bool(value) if isinstance(value, (bool, int)) else a.default
    if kind == "locked":
        return bool(getattr(cfg, a.source[1], True)) if (a.source[1] and cfg is not None) else True
    if kind == "marker":
        return setting_on(a.source[1])
    name = a.source[1]
    value = _settings().SETTINGS[name][0]()
    if name in OFF_VALUES:
        return value != OFF_VALUES[name]
    return bool(value)


def state(cfg, a: Addon, store: Optional[dict] = None) -> bool:
    """One add-on's global switch: the single reader behind `enabled`, `flags`, the view and a set's answer.
    Anything that goes wrong answers the add-on's default."""
    try:
        return _raw(cfg, a, read_store() if store is None else store)
    except Exception:
        return a.default


def enabled(cfg, key: str, provider: str = "claude", store: Optional[dict] = None) -> bool:
    """Whether one add-on is on for one harness. Unknown keys raise KeyError (a typo is a bug a test catches). An
    add-on that does not support the harness is off for it; otherwise its global switch decides. `store`: the
    switch file a caller already read (read_store), so one hook call reads it once."""
    a = BY_KEY[key]
    if provider not in a.providers:
        return False
    return state(cfg, a, store)


def flags(cfg) -> Dict[str, bool]:
    """Every add-on's global state, for the page (one store read)."""
    store = read_store()
    return {a.key: state(cfg, a, store) for a in CATALOGUE}


def item(cfg, a: Addon, on: Optional[bool] = None) -> dict:
    return {"key": a.key, "group": a.group, "title_he": a.title_he, "title_en": a.title_en, "text_he": a.text_he,
            "text_en": a.text_en, "on": state(cfg, a) if on is None else on,
            "locked": a.locked, "default": a.default,
            "providers": [HARNESS_NAMES[p] for p in a.providers]}


def view(cfg) -> dict:
    """What the Cockpit section shows: the groups in order, each with its rows."""
    now = flags(cfg)
    return {"groups": [{"key": g, "items": [item(cfg, a, now[a.key]) for a in CATALOGUE if a.group == g]}
                       for g in GROUPS]}


# --- switching --------------------------------------------------------------------------------------------------

def _config_write(cfg, name: str, value: Any) -> None:
    _settings()._config_set(name, value)
    if cfg is not None:
        setattr(cfg, name, value)  # the caller's copy follows at once (the panel keeps one for its life)


def _after(cfg, a: Addon) -> None:
    """Side effects a switch needs to take effect now."""
    if a.key == "listen_link" and not state(cfg, a):
        from . import listen_link
        try:
            listen_link.revoke()  # off means off: a link made earlier does not come back when it is switched on
        except Exception:
            pass
    if a.key == "knowledge_overlay" and cfg is not None:
        from . import knowledge
        try:
            knowledge.render(cfg)
        except Exception:
            pass  # the next render (any knowledge switch, `harnessgram knowledge render`) writes it


def set_on(cfg, key: str, on: bool) -> dict:
    """Switch one add-on globally. Returns {"key", "on", "previous"}; Unknown or Locked otherwise."""
    a = BY_KEY.get(key)
    if a is None:
        raise Unknown(key)
    if a.locked or tuple(a.source[:2]) in MASTER_SWITCHES:
        raise Locked(key)
    if not isinstance(on, bool):
        raise AddonError("on must be true or false")
    previous = state(cfg, a)
    kind = a.source[0]
    if kind == "store":
        with locked_map(_path()) as data:
            mapping = data.get("on") if isinstance(data.get("on"), dict) else {}
            mapping[key] = on
            data["on"] = mapping
    elif kind == "config":
        _config_write(cfg, a.source[1], on)
    elif kind == "marker":
        set_setting(a.source[1], on)
    else:
        name = a.source[1]
        settings = _settings()
        if name in OFF_VALUES:
            with locked_map(_path()) as data:
                restore = data.get("restore") if isinstance(data.get("restore"), dict) else {}
                if not on:
                    current = settings.SETTINGS[name][0]()
                    if current != OFF_VALUES[name]:
                        restore[name] = current
                value = OFF_VALUES[name] if not on else restore.get(name, ON_VALUES[name])
                if not settings.allowed(name, value):
                    value = ON_VALUES[name]
                data["restore"] = restore
            settings.write(name, value)
        else:
            settings.write(name, on)
    _after(cfg, a)
    return {"key": key, "on": on, "previous": previous}


# --- docs -------------------------------------------------------------------------------------------------------

GROUP_TITLES = {"conversation": "Conversation", "memory": "Memory and continuity", "voice": "Voice and radio",
                "cockpit": "Cockpit", "safety": "Safety (locked)"}


def _where(a: Addon) -> str:
    kind = a.source[0]
    if kind == "config":
        return "config `%s`" % a.source[1]
    if kind == "setting":
        return "Cockpit setting `%s`" % a.source[1]
    if kind == "marker":
        return "state file `%s`" % a.source[1]
    if kind == "locked":
        return "locked" + (" (config `%s`)" % a.source[1] if a.source[1] else "")
    return "`addons.json`" + (" (gate in `%s`)" % a.pending if a.pending else "")


def markdown() -> str:
    """docs/addons.md, generated from the catalogue (tests/test_addons.py keeps the file in sync)."""
    lines = ["# What Harnessgram adds to your agents", "",
             "<!-- Generated from src/ctb/addons.py by `harnessgram addons docs`. Do not edit by hand. -->", "",
             "Every behaviour the bridge adds into or around an agent harness, with its switch. The Cockpit shows",
             "them under Settings; a switch takes effect at once. Locked safety rows are shown and never switched",
             "off there. An add-on that already had a setting keeps it: the switch here is that same setting.", ""]
    for g in GROUPS:
        lines += ["## " + GROUP_TITLES[g], "", "| Key | Add-on | What it does | Default | Switch | Applies to |",
                  "|---|---|---|---|---|---|"]
        for a in CATALOGUE:
            if a.group != g:
                continue
            default = "always on" if a.locked else "on" if a.default else "off"
            lines.append("| `%s` | %s | %s | %s | %s | %s |" % (
                a.key, a.title_en, a.text_en, default, _where(a), ", ".join(HARNESS_NAMES[p] for p in a.providers)))
        lines.append("")
    return "\n".join(lines)


def listing(cfg) -> str:
    state = flags(cfg)
    return "\n".join("%-22s %-13s %s" % (a.key, a.group, "locked" if a.locked else "on" if state[a.key] else "off")
                     for a in CATALOGUE)

