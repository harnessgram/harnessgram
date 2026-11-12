"""Later live changes: permission-mode switching, live keyboard labels, quiet status card, background-agent
notices off by default, settings toggles and close buttons, delivery toggle, idle rebalance."""
from __future__ import annotations

from ctb.usershell import GUARD_END
import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import accounts, delivery, hooks as H, i18n, inbox as I, mirror as M
from ctb.config import AGENT_NOTICES, STATUS_REPOST, Config, set_setting, setting_on, state_dir, write_private
from ctb.telegram import TelegramError
from ctb.topics import TopicMap

OWNER = 7
SID = "sess-later-1"


def cfg(**kw):
    base = dict(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", ui_language="he")
    base.update(kw)
    return Config(**base)


def screen(mode_phrase: str, extra_below: int = 0) -> str:
    """A terminal screen: some output, then the mode line, then helper-agent rows."""
    lines = ["output line"] * 4 + [f"  ⏵⏵ {mode_phrase} on (shift+tab to cycle)"]
    return "\n".join(lines + ["  helper agent row"] * extra_below)


# --- 1. mode detection --------------------------------------------------------

@pytest.mark.parametrize("phrase,key", [
    ("bypass permissions", "bypassPermissions"), ("auto mode", "auto"), ("accept edits", "acceptEdits"),
    ("manual mode", "default"),
])
def test_mode_from_screen_maps_each_phrase(phrase, key):
    assert I.mode_from_screen(screen(phrase)) == key


def test_plan_mode_uses_the_pause_marker():
    assert I.mode_from_screen("text\n  ⏸ plan mode on (shift+tab to cycle)") == "plan"


def test_mode_is_unknown_when_a_question_hides_the_line():
    assert I.mode_from_screen("Do you want to proceed?\n❯ 1. Yes\n  2. No") == ""
    # a mention of a mode in ordinary text is not the status line
    assert I.mode_from_screen("we discussed plan mode and bypass permissions") == ""


def test_mode_scan_covers_the_last_twelve_lines_only():
    assert I.mode_from_screen(screen("auto mode", extra_below=7)) == "auto"          # 7 helper rows below: line 8 from the end
    assert I.mode_from_screen(screen("auto mode", extra_below=12)) == ""             # pushed out of the last 12 lines


# --- 2. switching by cycling the mode key -----------------------------------------

CYCLE = ["default", "acceptEdits", "plan", "auto", "bypassPermissions"]


class FakePane:
    """A pane whose status line advances one step in CYCLE per BTab key."""

    def __init__(self, start, cycle=CYCLE, frozen=False, hidden=False):
        self.mode, self.cycle, self.frozen, self.hidden = start, cycle, frozen, hidden
        self.keys = []

    def __call__(self, *args):
        if args[0] == "send-keys":
            self.keys.append(args[-1])
            if not self.frozen:
                self.mode = self.cycle[(self.cycle.index(self.mode) + 1) % len(self.cycle)]
            return SimpleNamespace(returncode=0, stdout="")
        if args[0] == "capture-pane":
            phrase = I.MODE_BAR[self.mode]
            text = "Allow this edit?\n❯ 1. Yes" if self.hidden else screen(phrase)
            return SimpleNamespace(returncode=0, stdout=text)
        return SimpleNamespace(returncode=0, stdout="")


@pytest.fixture
def pane_env(monkeypatch):
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda session: "%3")
    monkeypatch.setattr(accounts, "session_is_idle", lambda session, now=None: True)

    def install(pane):
        monkeypatch.setattr(I, "_tmux", pane)
        return pane
    return install


def said(bot):
    return [p["text"] for p in bot.methods("sendMessage")]


def test_switch_cycles_shift_tab_until_the_status_line_shows_the_target(pane_env, bot):
    pane = pane_env(FakePane("bypassPermissions"))   # next steps: default, acceptEdits, plan
    m = {"message_thread_id": 5}
    assert I.switch_mode(cfg(), bot, m, SID, "plan") == "switched"
    assert pane.keys == ["BTab"] * 3 and pane.mode == "plan"
    assert i18n.t("mode_switch_success", lang="he", label=i18n.t("mode_plan", lang="he")) in said(bot)[-1]


def test_switch_gives_up_after_seven_presses(pane_env, bot):
    pane = pane_env(FakePane("auto", frozen=True))
    assert I.switch_mode(cfg(), bot, {"message_thread_id": 5}, SID, "plan") == "failed"
    assert pane.keys == ["BTab"] * I.MODE_CYCLE_TRIES == ["BTab"] * 7
    assert "⚠️" in said(bot)[-1]


def test_looser_mode_is_refused_and_stricter_is_allowed(pane_env, bot):
    pane = pane_env(FakePane("plan"))
    assert I.switch_mode(cfg(), bot, {"message_thread_id": 5}, SID, "auto") == "looser"
    assert pane.keys == [] and "מאפליקציית קלוד" in said(bot)[-1]
    pane = pane_env(FakePane("default"))
    assert I.switch_mode(cfg(), bot, {"message_thread_id": 5}, SID, "plan") == "switched"
    assert pane.keys == ["BTab", "BTab"]


def test_rank_order():
    assert sorted(I.MODE_RANK, key=I.MODE_RANK.get) == ["plan", "default", "acceptEdits", "auto", "bypassPermissions"]


def test_same_mode_presses_nothing(pane_env, bot):
    pane = pane_env(FakePane("auto"))
    assert I.switch_mode(cfg(), bot, {}, SID, "auto") == "same"
    assert pane.keys == []


def test_unknown_mode_asks_to_answer_the_question_first(pane_env, bot):
    pane = pane_env(FakePane("auto", hidden=True))
    assert I.switch_mode(cfg(), bot, {}, SID, "plan") == "unknown"
    assert pane.keys == [] and said(bot)[-1].lstrip("‏") == i18n.t("mode_question_waiting", lang="he")


def test_switch_without_a_terminal_says_so(monkeypatch, bot):
    monkeypatch.setattr(I, "pane_for_session", lambda session: None)
    assert I.switch_mode(cfg(ui_language="en", target_language="en"), bot, {}, SID, "plan") == "no_terminal"
    assert said(bot)[-1] == i18n.t("mode_no_terminal", lang="en")


def test_mode_button_runs_the_switch_instead_of_queueing_a_message(tmp_path, bot, monkeypatch, pane_env):
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Title")
    box = I.Inbox(state_dir() / "inbox")
    pane = pane_env(FakePane("bypassPermissions"))
    monkeypatch.setattr(I, "_terminal_background", lambda session, action: action() or True)
    q = {"id": "cb", "from": {"id": OWNER}, "data": "mode:plan",
         "message": {"message_id": 9, "chat": {"id": OWNER, "type": "private"}, "message_thread_id": thread}}
    assert I.handle_button(q, cfg(), bot, topics, box) == "mode"
    assert box.take(SID) == [] and pane.mode == "plan"


def test_permissions_button_with_a_live_label_opens_the_mode_menu(tmp_path, bot, monkeypatch, pane_env):
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Title")
    pane_env(FakePane("auto"))
    msg = {"message_id": 5, "date": int(time.time()), "text": "🔐 אוטומטי", "message_thread_id": thread,
           "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}}
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **k: None)
    media = SimpleNamespace(content_of=lambda m: m.get("text") or "")
    assert I.handle({"message": msg}, cfg(), bot, topics, I.Inbox(state_dir() / "inbox"), media, time.time()) == "mode"
    sent = bot.methods("sendMessage")[-1]
    assert i18n.t("mode_now", lang="he", label=i18n.t("mode_auto", lang="he")) in sent["text"]
    assert len(sent["reply_markup"]["inline_keyboard"]) == 6  # five modes and "close menu"


# --- 3. fixed keyboard rows with live labels -------------------------------------

def test_fixed_keyboard_rows():
    rows = [[b["text"] for b in r] for r in M.KEYBOARD["keyboard"]]
    # the settings row (live values) above the actions row, which sits closest to the thumb
    assert rows == [["🧠 מודל", "🎚️ חשיבה", "🔐 הרשאות"], ["⏹️ עצור", "🗜️ דחיסה", "⚙️ עוד", "🎛 הקוקפיט"]]
    rows_en = [[b["text"] for b in r] for r in M.KEYBOARD_EN["keyboard"]]
    assert rows_en == [["🧠 Model", "🎚️ Thinking", "🔐 Permissions"], ["⏹️ Stop", "🗜️ Compact", "⚙️ More", "🎛 Cockpit"]]


def test_live_keyboard_shows_account_effort_model_and_mode(monkeypatch):
    TopicMap(state_dir() / "topics.json", None, OWNER).ensure(SID, "Title", thread=77)
    write_private(state_dir() / "session-models.json", json.dumps({SID: "Claude Opus 5.5|xhigh"}))
    monkeypatch.setattr(accounts, "account_for", lambda s: "work")
    monkeypatch.setattr(accounts, "email", lambda a: "dev@example.com")
    monkeypatch.setattr(accounts, "multiple", lambda: True)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "_tmux", FakePane("acceptEdits"))
    kb = M.live_keyboard("הקשר 10 אלף", thread=77)
    labels = [b["text"] for r in kb["keyboard"] for b in r]
    assert "👤 dev" in labels and "🧠 Opus 5.5" in labels
    assert "🎚️ " + i18n.t("effort_xhigh", lang="he") in labels and "🔐 עריכות" in labels
    kb_en = M.live_keyboard("Context 10k", base_kb=M.KEYBOARD_EN, lang="en", thread=77)
    assert "🔐 Edits" in [b["text"] for r in kb_en["keyboard"] for b in r]


def test_live_keyboard_keeps_the_plain_permissions_label_while_a_question_hides_the_mode(monkeypatch):
    TopicMap(state_dir() / "topics.json", None, OWNER).ensure(SID, "Title", thread=78)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "_tmux", FakePane("auto", hidden=True))
    kb = M.live_keyboard("הקשר 10 אלף", thread=78)
    assert "🔐 הרשאות" in [b["text"] for r in kb["keyboard"] for b in r]


# --- 4. the status card is updated in place ----------------------------------------------------

def card(bot, footer, thread=40, **kw):
    return M.status_message(cfg(), thread, footer, M.KEYBOARD, bot=bot, **kw)


def calls(bot, name):
    return bot.methods(name)


def test_status_card_is_edited_in_place_by_default(bot):
    first = card(bot, "הקשר 10 אלף")
    assert first["message_id"] and len(calls(bot, "sendMessage")) == 1
    # the info row of the keyboard carries the context size, so keep it the same to leave the labels alone
    card(bot, "הקשר 10 אלף · חדש")
    assert len(calls(bot, "sendMessage")) == 1 and len(calls(bot, "editMessageText")) == 1
    assert calls(bot, "editMessageText")[0]["message_id"] == first["message_id"]
    assert not calls(bot, "deleteMessage")


def test_status_card_edit_is_skipped_when_the_text_is_unchanged(bot):
    card(bot, "הקשר 10 אלף")
    card(bot, "הקשר 10 אלף")
    assert len(calls(bot, "sendMessage")) == 1 and calls(bot, "editMessageText") == []


def test_status_card_is_posted_new_after_a_yes_no_question(bot):
    card(bot, "הקשר 10 אלף")
    card(bot, "הקשר 10 אלף", asked=True)
    assert len(calls(bot, "sendMessage")) == 2 and len(calls(bot, "deleteMessage")) == 1


def test_status_card_is_posted_new_when_the_keyboard_labels_changed(bot, monkeypatch):
    TopicMap(state_dir() / "topics.json", None, OWNER).ensure(SID, "Title", thread=40)
    write_private(state_dir() / "session-models.json", json.dumps({SID: "Claude Sonnet 5.5|high"}))
    card(bot, "הקשר 10 אלף")
    write_private(state_dir() / "session-models.json", json.dumps({SID: "Claude Opus 5.5|high"}))
    card(bot, "הקשר 10 אלף · חדש")
    assert len(calls(bot, "sendMessage")) == 2 and calls(bot, "editMessageText") == []
    assert len(calls(bot, "deleteMessage")) == 1


def test_status_card_setting_posts_a_new_card_after_every_reply(bot):
    set_setting(STATUS_REPOST, True)
    card(bot, "הקשר 10 אלף")
    card(bot, "הקשר 10 אלף · חדש")
    assert len(calls(bot, "sendMessage")) == 2 and calls(bot, "editMessageText") == []


def test_status_card_falls_back_to_a_new_card_when_the_edit_fails(bot):
    card(bot, "הקשר 10 אלף")
    bot.fail["editMessageText"] = "Bad Request: message to edit not found"
    card(bot, "הקשר 10 אלף · חדש")
    assert len(calls(bot, "sendMessage")) == 2


def test_keyboard_back_forces_a_new_card(bot, monkeypatch):
    seen = []
    monkeypatch.setattr(M, "status_message", lambda *a, **k: seen.append(k))
    write_private(state_dir() / M.STATUS_TEXT_FILE, json.dumps({"9": "footer"}))

    class Now:
        def __init__(self, interval, fn, args=None, kwargs=None):
            self.fn = fn
            self.daemon = False

        def cancel(self):
            pass

        def start(self):
            self.fn()
    monkeypatch.setattr(I.threading, "Timer", Now)
    I.keyboard_back(9, delay=0, cfg=cfg(), bot=bot)
    assert seen and seen[0]["repost"] is True


# --- 5. background-agent notices are off by default -----------------------------------

def test_agent_ends_always_reach_the_agents_card_whatever_the_notices_setting_says(monkeypatch, bot):
    spawned = []
    monkeypatch.setattr(H, "load_config", lambda: cfg())
    monkeypatch.setattr(H, "spawn", lambda args: spawned.append(args))
    event = json.dumps({"session_id": "s1", "hook_event_name": "SubagentStop"})
    assert H.hook_lifecycle(event) == 0 and len(spawned) == 1 and spawned[0][2] == "--kind=subagent-stop"
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s1", "Title")
    assert not setting_on(AGENT_NOTICES)
    assert M.run_lifecycle(cfg(), "s1", "subagent-stop", "worker", "task", bot=bot) == 0
    assert len(bot.methods("sendMessage")) == 1  # one card, not one notice line per agent
    assert "Background agent finished" not in bot.methods("sendMessage")[0]["text"]


# --- 6. settings toggles and the close button --------------------------------------------

def button(bot, topics, thread, data, mid=50):
    q = {"id": "q", "from": {"id": OWNER}, "data": data,
         "message": {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": thread, "message_id": mid,
                     "text": "🌐 settings"}}
    return I.handle_button(q, cfg(), bot, topics, I.Inbox(state_dir() / "inbox"))


def labels(markup):
    return [b["text"] for r in markup["inline_keyboard"] for b in r]


def test_global_page_has_both_noise_toggles(bot):
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Title")
    he = labels(I.global_markup(lang="he"))
    assert i18n.t("global_btn_status_repost", lang="he", state=i18n.t("status_repost_inplace", lang="he")) in he
    assert i18n.t("global_btn_agent_notices", lang="he", state=i18n.t("agent_notices_quiet", lang="he")) in he

    assert button(bot, topics, thread, "more:statusrepost") == "settings" and setting_on(STATUS_REPOST)
    assert button(bot, topics, thread, "more:agentnotices") == "settings" and setting_on(AGENT_NOTICES)
    page = bot.methods("editMessageText")[-1]["reply_markup"]
    assert i18n.t("global_btn_agent_notices", lang="he", state=i18n.t("agent_notices_loud", lang="he")) in labels(page)
    assert i18n.t("global_btn_status_repost", lang="he", state=i18n.t("status_repost_new", lang="he")) in labels(
        I.global_markup(lang="he"))
    button(bot, topics, thread, "more:statusrepost")
    button(bot, topics, thread, "more:agentnotices")
    assert not setting_on(STATUS_REPOST) and not setting_on(AGENT_NOTICES)


@pytest.mark.parametrize("lang", ["he", "en"])
def test_close_menu_button_on_more_global_and_plugins_pages(lang, monkeypatch):
    monkeypatch.setattr(I, "plugin_list", lambda session: [("alpha@market", True)])
    dismiss = i18n.t("menu_btn_dismiss", lang=lang)
    assert dismiss in labels(I.more_markup(lang=lang))
    assert dismiss in labels(I.global_markup(lang=lang))
    _, plugins_markup = I.plugins_page(SID, cfg=cfg(ui_language=lang))
    assert dismiss in labels(plugins_markup)
    for markup in (I.more_markup(lang=lang), I.global_markup(lang=lang), plugins_markup):
        assert any(b["callback_data"] == "more:dismiss" for r in markup["inline_keyboard"] for b in r)


def test_close_menu_deletes_the_message(bot):
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Title")
    assert button(bot, topics, thread, "more:dismiss", mid=61) == "dismissed"
    assert bot.methods("deleteMessage") == [{"chat_id": OWNER, "message_id": 61}]
    assert bot.methods("editMessageText") == []


def test_close_menu_falls_back_to_a_one_line_note_when_delete_is_refused(bot):
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Title")
    bot.fail["deleteMessage"] = "Bad Request: message can't be deleted"
    button(bot, topics, thread, "more:dismiss", mid=62)
    edit = bot.methods("editMessageText")[-1]
    assert edit["message_id"] == 62 and edit["text"] == i18n.t("menu_dismissed", lang="he")


def test_close_menu_from_another_user_is_ignored(bot):
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Title")
    q = {"id": "q", "from": {"id": 999}, "data": "more:dismiss",
         "message": {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": thread, "message_id": 9}}
    assert I.handle_button(q, cfg(), bot, topics, I.Inbox(state_dir() / "inbox")) == "ignored"
    assert bot.methods("deleteMessage") == []


# --- 7. the delivery toggle reads the value before writing -----------------------------

def test_delivery_toggle_flips_and_never_leaves_an_empty_file(bot):
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Title")
    path = state_dir() / "delivery-mode"
    assert delivery.default_mode() == "now"
    button(bot, topics, thread, "more:delivery")
    assert path.read_text() == "end" and delivery.default_mode() == "end"
    button(bot, topics, thread, "more:delivery")
    assert path.read_text() == "now" and delivery.default_mode() == "now"
    button(bot, topics, thread, "more:delivery")
    assert path.read_text() == "end"


# --- 8. idle rebalance ------------------------------------------------------------------------

NOW = 2_000_000.0


class Rig:
    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        accounts.add("small")
        for name, tier in (("main", "default_claude_max_20x"), ("small", "pro")):
            path = accounts.claude_json() if name == "main" else accounts.profiles_dir() / name / ".claude.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"oauthAccount": {"emailAddress": f"{name}@example.com",
                                                         "organizationRateLimitTier": tier}}))
        write_private(state_dir() / "account-emails.json",
                      json.dumps({"main": "main@example.com", "small": "small@example.com"}))
        for name in ("main", "small"):
            write_private(accounts.usage_path(name), json.dumps(
                {"five_hour": 20, "weekly": 20, "five_hour_resets": NOW + 3600, "weekly_resets": NOW + 86400,
                 "at": "2026-11-01T00:00:00Z"}))
        self.topics = {}
        self.kids = ""
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)
        real_run = subprocess.run
        monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **kw: SimpleNamespace(stdout=self.kids, returncode=0)
                            if cmd[0] == "pgrep" else real_run(cmd, *a, **kw))
        self.spawned = []

    def session(self, sid, pid, idle_for=1200, thread=None):
        accounts.record(sid, "main")
        self.topics[sid] = {"thread": thread or pid, "name": sid}
        write_private(state_dir() / "topics.json", json.dumps(self.topics))
        folder = accounts.claude_home() / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{pid}.json").write_text(json.dumps({
            "sessionId": sid, "pid": pid, "status": "idle", "tmux": "%1",
            "statusUpdatedAt": (NOW - idle_for) * 1000}))

    def agent_transcript(self, sid, age):
        d = accounts.claude_home() / "projects" / "proj" / sid / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "agent-1.jsonl"
        f.write_text("{}")
        os.utime(f, (NOW - age, NOW - age))

    def run(self, at=NOW):
        return accounts.rebalance(now=at, spawn_fn=lambda args: self.spawned.append(args))


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


def test_rebalance_waits_for_five_idle_minutes(rig):
    rig.session("s-recent", 501, idle_for=240)
    assert rig.run() is None
    rig.session("s-ok", 502, idle_for=310)
    assert rig.run() == ("s-ok", "small")


def test_rebalance_skips_sessions_with_recent_subagent_transcripts(rig):
    rig.session("s-busy", 503)
    rig.agent_transcript("s-busy", age=300)          # changed 5 minutes ago
    assert rig.run() is None and rig.spawned == []
    rig.agent_transcript("s-busy", age=900)          # quiet for 15 minutes
    assert rig.run() == ("s-busy", "small")


def test_rebalance_skips_sessions_with_shell_children(rig):
    rig.session("s-shell", 504)
    rig.kids = "9001 zsh\n"
    assert rig.run() is None
    rig.kids = "9001 sleep\n"
    assert rig.run() == ("s-shell", "small")


def test_rebalance_has_a_three_hour_cooldown_and_moves_one_per_round(rig):
    rig.session("s-a", 505)
    rig.session("s-b", 506)
    first = rig.run()
    assert first is not None and len(rig.spawned) == 1              # one per round
    second = rig.run(at=NOW + 60)
    assert second is not None and second[0] != first[0]             # the other one next round
    assert rig.run(at=NOW + 120) is None                            # both cooled down
    assert rig.run(at=NOW + 3 * 3600 + 200) is not None            # cooldown over


def test_auto_compact_toggle_steps_through_the_limits(bot):
    # the global page cycles off -> 200 -> 250 ... (the handler used an undefined name)
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Title")
    assert I.auto_compact_limit() == 0
    assert button(bot, topics, thread, "more:autocompact") == "settings"
    assert I.auto_compact_limit() == 200
    button(bot, topics, thread, "more:autocompact")
    assert I.auto_compact_limit() == 250


# =====================================================================================
# Review round: safety of the mode switch, idle moves, trust prompt, new project, resume
# =====================================================================================

class ScriptedPane:
    """A pane that shows a scripted sequence of modes (None = a question on screen) after each BTab."""

    def __init__(self, shown):
        self.shown, self.keys, self.i = list(shown), [], 0

    def __call__(self, *args):
        if args[0] == "send-keys":
            self.keys.append(args[-1])
            self.i = min(self.i + 1, len(self.shown) - 1)
            return SimpleNamespace(returncode=0, stdout="")
        if args[0] == "capture-pane":
            mode = self.shown[self.i]
            text = "Allow this edit?\n❯ 1. Yes, allow all edits\n  2. No" if mode is None else screen(I.MODE_BAR[mode])
            return SimpleNamespace(returncode=0, stdout=text)
        return SimpleNamespace(returncode=0, stdout="")


def test_switch_from_auto_to_default_may_pass_through_bypass_but_ends_on_the_target(pane_env, bot):
    pane = pane_env(ScriptedPane(["auto", "bypassPermissions", "default"]))
    assert I.switch_mode(cfg(), bot, {}, SID, "default") == "switched"
    assert pane.keys == ["BTab", "BTab"]


def test_a_question_appearing_mid_cycle_stops_the_keys_at_once(pane_env, bot):
    pane = pane_env(ScriptedPane(["bypassPermissions", "default", None, "plan"]))
    assert I.switch_mode(cfg(), bot, {}, SID, "plan") == "unknown"
    assert pane.keys == ["BTab", "BTab"]            # no key is sent into the question
    assert said(bot)[-1].lstrip("‏") == i18n.t("mode_question_waiting", lang="he")


def test_after_a_failed_switch_the_cycle_continues_until_no_looser_than_the_start(pane_env, bot):
    # start: acceptEdits (rank 2); the target never appears; the cycle ends up looser (auto, bypass) before it
    # comes back round to default
    shown = ["acceptEdits"] + ["auto", "bypassPermissions", "bypassPermissions", "bypassPermissions",
                               "bypassPermissions", "bypassPermissions", "bypassPermissions", "default"]
    pane = pane_env(ScriptedPane(shown))
    assert I.switch_mode(cfg(), bot, {}, SID, "plan") == "failed"
    assert len(pane.keys) == 8                      # 7 attempts + 1 more press to get back to a rank <= 2
    assert pane.shown[pane.i] == "default"
    assert "⚠️" in said(bot)[-1]


def test_mode_is_not_switched_while_the_session_is_working(pane_env, bot, monkeypatch):
    pane = pane_env(FakePane("bypassPermissions"))
    monkeypatch.setattr(accounts, "session_is_idle", lambda session, now=None: False)
    assert I.switch_mode(cfg(), bot, {}, SID, "plan") == "busy"
    assert pane.keys == [] and said(bot)[-1].lstrip("‏") == i18n.t("mode_wait_idle", lang="he")


def write_record(sid, status, updated_at, pid=900):
    d = accounts.claude_home() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps({"sessionId": sid, "pid": pid, "status": status,
                                               "statusUpdatedAt": updated_at * 1000, "tmux": "%1"}))


def test_session_is_idle_reads_the_session_record(monkeypatch):
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    write_record(SID, "idle", NOW - 10)
    assert accounts.session_is_idle(SID, now=NOW)
    write_record(SID, "busy", NOW - 10)
    assert not accounts.session_is_idle(SID, now=NOW)
    write_record("other", "idle", NOW - 10, pid=901)
    assert not accounts.session_is_idle("unknown-session", now=NOW)


# --- idle account move re-checks right before it types -------------------------------

class MovePane:
    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "display" and args[-1] == "#{pane_current_command}":
            return SimpleNamespace(returncode=0, stdout="zsh\n")
        return SimpleNamespace(returncode=0, stdout="")


@pytest.fixture
def mover(monkeypatch):
    accounts.add("small")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    pane = MovePane()
    monkeypatch.setattr(I, "_tmux", pane)
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **kw: SimpleNamespace(
        stdout="", returncode=1) if cmd[0] == "pgrep" else SimpleNamespace(stdout="", returncode=0))
    monkeypatch.setattr(time, "time", lambda: NOW)
    accounts.record(SID, "main")
    return pane


def typed_keys(pane):
    return [c for c in pane.calls if c[0] == "send-keys"]


def test_idle_move_goes_ahead_when_still_idle(mover):
    write_record(SID, "idle", NOW - 900)
    assert I.account_move(cfg(), FakeBot(), SID, "small", lambda *a, **k: None, idle_only=True)
    assert any(c[-1] == "Escape" for c in typed_keys(mover)) and accounts.account_for(SID) == "small"


def test_idle_move_aborts_when_the_session_became_busy(mover):
    write_record(SID, "busy", NOW - 5)
    assert not I.account_move(cfg(), FakeBot(), SID, "small", lambda *a, **k: None, idle_only=True)
    assert typed_keys(mover) == [] and accounts.account_for(SID) == "main"


def test_idle_move_aborts_when_it_just_turned_idle(mover):
    write_record(SID, "idle", NOW - 20)              # idle, but not for the required five minutes
    assert not I.account_move(cfg(), FakeBot(), SID, "small", lambda *a, **k: None, idle_only=True)
    assert typed_keys(mover) == []


def test_idle_move_aborts_for_recent_subagent_activity(mover):
    write_record(SID, "idle", NOW - 900)
    d = accounts.claude_home() / "projects" / "p" / SID / "subagents"
    d.mkdir(parents=True)
    (d / "a.jsonl").write_text("{}")
    os.utime(d / "a.jsonl", (NOW - 60, NOW - 60))
    assert not I.account_move(cfg(), FakeBot(), SID, "small", lambda *a, **k: None, idle_only=True)
    assert typed_keys(mover) == []


def test_idle_move_aborts_with_shell_children_and_when_pgrep_fails(mover, monkeypatch):
    write_record(SID, "idle", NOW - 900)
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **kw: SimpleNamespace(stdout="77 zsh\n", returncode=0))
    assert not I.account_move(cfg(), FakeBot(), SID, "small", lambda *a, **k: None, idle_only=True)

    def boom(cmd, *a, **kw):
        raise OSError("pgrep missing")
    monkeypatch.setattr(subprocess, "run", boom)
    assert not I.account_move(cfg(), FakeBot(), SID, "small", lambda *a, **k: None, idle_only=True)
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **kw: SimpleNamespace(stdout="", returncode=2))
    assert not I.account_move(cfg(), FakeBot(), SID, "small", lambda *a, **k: None, idle_only=True)
    assert typed_keys(mover) == []


def test_a_manual_move_does_not_need_the_session_to_be_idle(mover):
    write_record(SID, "busy", NOW - 5)
    assert I.account_move(cfg(), FakeBot(), SID, "small", lambda *a, **k: None)
    assert typed_keys(mover)


def test_limit_marker_is_written_only_after_the_move_succeeded(mover, monkeypatch):
    import ctb.usage as U
    monkeypatch.setattr(U, "limit_of", lambda s: {"id": "err-1"})
    marker = state_dir() / "limit-cleared" / SID
    monkeypatch.setattr(I, "resume_after_move", lambda *a, **k: "nudged")  # its own tests: test_continue_after_limit_move
    monkeypatch.setattr(I, "account_move", lambda *a, **k: False)
    assert not I.auto_move(cfg(), FakeBot(), SID, 5, "small", idle=True)
    assert not marker.exists()
    monkeypatch.setattr(I, "account_move", lambda *a, **k: True)
    assert I.auto_move(cfg(), FakeBot(), SID, 5, "small", idle=True)
    assert marker.read_text() == "err-1"


# --- rebalance and the default account ------------------------------------------------------

def test_rebalance_skips_a_session_when_pgrep_fails(rig, monkeypatch):
    rig.session("s-pg", 701)

    def boom(cmd, *a, **kw):
        raise OSError("no pgrep")
    monkeypatch.setattr(subprocess, "run", boom)
    assert rig.run() is None and rig.spawned == []


def test_default_account_is_not_switched_when_automatic_switching_is_off(rig):
    write_private(accounts.usage_path("main"), json.dumps(
        {"five_hour": 99, "weekly": 99, "five_hour_resets": NOW + 3600, "weekly_resets": NOW + 86400,
         "at": iso_now()}))
    accounts.use("main")
    accounts.toggle_auto()                                  # off
    assert not accounts.auto_enabled()
    assert accounts.check_default_account(now=NOW) is None and accounts.current() == "main"
    accounts.toggle_auto()
    assert accounts.check_default_account(now=NOW) == "small"


def iso_now():
    import datetime
    return datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


# --- pane ids ------------------------------------------------------------------------------

def test_pane_from_process_rejects_anything_but_a_plain_pane_id(tmp_path, monkeypatch):
    sessions = I.status.projects_dir().parent / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    for bad in ("tg:@0.%7;x", "%", "%7a", "%-1", "tmux-%7"):
        (sessions / "5.json").write_text(json.dumps({"sessionId": SID, "pid": 5, "tmux": f"s:@0.{bad}"}))
        assert I._pane_from_process(SID) is None, bad


# --- resume: never kill a live Claude window --------------------------------------------------

class ResumePane:
    def __init__(self, running):
        self.running, self.calls = running, []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "display":
            return SimpleNamespace(returncode=0 if self.running else 1, stdout=(self.running or "") + "\n")
        return SimpleNamespace(returncode=0, stdout="❯ ")


def test_resume_does_not_kill_a_window_that_runs_claude(monkeypatch):
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    pane = ResumePane("claude")
    monkeypatch.setattr(I, "_tmux", pane)
    assert I.resume_session(SID) is False
    assert not any(c[0] in ("kill-session", "new-session") for c in pane.calls)


def test_resume_replaces_an_idle_shell_window_with_exact_targets(monkeypatch):
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    pane = ResumePane("zsh")
    monkeypatch.setattr(I, "_tmux", pane)
    assert I.resume_session(SID) is True
    kill = next(c for c in pane.calls if c[0] == "kill-session")
    assert kill[-1] == "=tg-" + SID[:8]
    assert next(c for c in pane.calls if c[0] == "display")[3] == "=tg-" + SID[:8] + ":"  # tmux 3.7 needs the colon


# --- new project: one && chain, unique folder ---------------------------------------------

def test_new_project_folder_is_unique_and_never_started_in_the_parent(tmp_path, bot, monkeypatch):
    root = tmp_path / "projects"
    (root / time.strftime("project-%m%d-%H%M")).mkdir(parents=True)
    first = I._fresh_project_folder(root)
    assert first.endswith("-2") and not os.path.exists(first)

    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    token = "abcdef0123456789"
    write_private(state_dir() / "projects-pending.json",
                  json.dumps({"400": {"task": "build", "dirs": ["/dir/a"], "token": token}}))
    cmds = []
    monkeypatch.setattr(I, "_tmux", lambda *a: cmds.append(a) or SimpleNamespace(returncode=0, stdout="❯ "))
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "projects_root", lambda cfg=None: root)
    monkeypatch.setattr(I.threading, "Thread", lambda target, daemon: SimpleNamespace(start=lambda: None))
    I.project_chosen(cfg(), bot, {"message_id": 15, "message_thread_id": 400}, topics, token, "new")
    line = next(c for c in cmds if c[0] == "new-session")[-1].split(GUARD_END, 1)[-1]  # after the launch preamble
    head = line.split("--remote-control")[0]
    assert head.startswith("mkdir ") and "mkdir -p" not in head
    assert head.count("&&") >= 3 and ";" not in head
    assert "-2" in head                                             # the unique folder name


def test_new_project_trust_dialog_is_answered_by_moving_the_cursor(tmp_path, bot, monkeypatch):
    from test_port_1101_sessions import TrustPane
    pane = TrustPane(cursor=2)
    monkeypatch.setattr(I, "_tmux", pane)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    assert I.answer_trust_dialog("tg-x") is True
    assert pane.keys == ["Up", "Enter"] and pane.accepted


# --- the status card signature ----------------------------------------------------------

def test_status_card_stays_in_place_while_only_the_context_and_quota_numbers_change(bot):
    TopicMap(state_dir() / "topics.json", None, OWNER).ensure(SID, "Title", thread=40)
    first = card(bot, "הקשר 10 אלף · Claude 11% · Claude 30%")
    for ctx in (11, 12, 14):
        card(bot, f"הקשר {ctx} אלף · Claude {ctx}% · Claude 31%")
    assert len(calls(bot, "sendMessage")) == 1 and len(calls(bot, "editMessageText")) == 3
    assert all(c["message_id"] == first["message_id"] for c in calls(bot, "editMessageText"))


def test_keyboard_signature_ignores_numbers_but_not_labels():
    kb = lambda ctx, acct: {"keyboard": [[{"text": f"📏 {ctx} אלף"}, {"text": "⏱️ 5% · 📅 6%"}, {"text": "👤 " + acct}],
                                         [{"text": "🔐 אוטומטי"}, {"text": "🧠 Opus"}]]}
    assert M.keyboard_signature(kb(10, "a")) == M.keyboard_signature(kb(19, "a"))  # within one CONTEXT_STEP
    assert M.keyboard_signature(kb(10, "a")) != M.keyboard_signature(kb(20, "a"))  # a step: the cell updates
    assert M.keyboard_signature(kb(10, "a")) != M.keyboard_signature(kb(99, "a"))  # a real move reposts
    assert M.keyboard_signature(kb(10, "a")) != M.keyboard_signature(kb(10, "b"))


def test_keyboard_signature_ignores_reset_times():
    def kb(t):
        return {"keyboard": [[{"text": t}]]}
    assert M.keyboard_signature(kb("📅 47% ↻ 03.11 14:00")) == M.keyboard_signature(kb("📅 41% ↻ מחר"))
    assert M.keyboard_signature(kb("⏱️ 80% · 📅 47%")) != M.keyboard_signature(kb("⏱️ 91% · 📅 47%"))
    assert M.keyboard_signature(kb("📏 1,231 אלף")) == M.keyboard_signature(kb("📏 1,239 אלף"))
    assert M.keyboard_signature(kb("📏 1,234 אלף")) != M.keyboard_signature(kb("📏 1,240 אלף"))
