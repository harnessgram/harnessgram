"""The 2026-10-30 live changes: several Claude accounts, the model/thinking/account stamp, effort and
in-place menus, per-message delivery choice, voice approval, held commands, compaction progress, /esc,
placeholder order, topic icon retries and the weekly reset form. No network, no Telegram, no tmux."""
import json
import os
import stat
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN, FakeBot, BoxedTmux
from ctb import accounts, cli, compact, dashboard, delivery, fmt, hooks, inbox as I, mirror, statusline
from ctb import topic_icons, usage, voice, wake
from ctb.config import Config, state_dir, write_private
from ctb.telegram import TelegramError
from ctb.topics import TopicMap, session_state, set_state

SID = "11111111-2222-3333-4444-555555555555"
OWNER = 7


def cfg(**kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, **kw)


def rig(bot, session=SID):
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(session, "Title")
    return topics, I.Inbox(state_dir() / "inbox"), thread


def write(name, data):
    write_private(state_dir() / name, json.dumps(data))


def make_profile(name):
    (accounts.profiles_dir() / name).mkdir(parents=True)


@pytest.fixture
def home(tmp_path):
    h = accounts.claude_home()
    h.mkdir(parents=True)
    for d in ("sessions", "projects", "session-env", "statsig", "backups", "daemon-x"):
        (h / d).mkdir()
    for f in ("settings.json", ".credentials.json", ".claude.json", "settings.json.bak-1"):
        (h / f).write_text("{}")
    accounts.claude_json().write_text(json.dumps({"hasCompletedOnboarding": True, "mcpServers": {"a": 1},
                                                  "oauthAccount": {"emailAddress": "secret@example.com"}}))
    return h


class Msgs:
    """A fake panel: records what the handlers say."""
    def __init__(self):
        self.said = []

    def __call__(self, text, **extra):
        self.said.append((text, extra))


# 1. accounts ---------------------------------------------------------------------------------

def test_add_shares_everything_but_login_and_seeds_a_private_claude_json(home):
    target = accounts.add("work")
    linked = {p.name for p in target.iterdir()}
    assert {"settings.json", "sessions", "projects"} <= linked          # sessions IS shared: live sessions stay visible
    assert not linked & {".credentials.json", "daemon-x", "session-env", "statsig", "backups", "settings.json.bak-1"}
    assert (target / "sessions").is_symlink()
    seeded = json.loads((target / ".claude.json").read_text())
    assert seeded == {"hasCompletedOnboarding": True, "mcpServers": {"a": 1}}   # whitelist: no login data
    assert stat.S_IMODE((target / ".claude.json").stat().st_mode) == 0o600
    accounts.add("work")                                                     # idempotent
    assert accounts.names() == ["main", "work"]
    for bad in ("main", "Bad Name", "../x", ""):
        with pytest.raises(ValueError):
            accounts.add(bad)


def test_account_of_a_session_is_sticky_and_launch_commands_go_through_it(home):
    make_profile("work")
    assert accounts.shell_command() == "unset CLAUDE_CONFIG_DIR; exec claude"
    accounts.use("work")
    assert accounts.current() == "work"
    cmd = accounts.shell_command(SID, remember=True)
    assert cmd.startswith(f"CLAUDE_CONFIG_DIR={accounts.profiles_dir() / 'work'} exec claude")
    accounts.use("main")
    assert accounts.account_for(SID) == "work"           # a known session keeps its account
    assert accounts.account_for("other") == "main"      # a new one takes the current choice
    assert "unset CLAUDE_CONFIG_DIR" in accounts.shell_command("other")
    with pytest.raises(ValueError):
        accounts.use("nope")
    assert accounts.name_from_env({"CLAUDE_CONFIG_DIR": str(accounts.profiles_dir() / "work") + "/"}) == "work"
    assert accounts.name_from_env({}) == "main"


def test_new_and_resumed_sessions_start_on_the_chosen_account(home, bot, monkeypatch):
    make_profile("work")
    accounts.use("work")
    calls = []
    monkeypatch.setattr(I, "_tmux", lambda *a: calls.append(a) or SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(I.threading, "Thread", lambda target, daemon=None: SimpleNamespace(start=lambda: None))
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    write("projects-pending.json", {"42": {"token": "t" * 16, "dirs": ["/tmp"], "task": "go"}})
    m = {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": 42, "message_id": 3}
    I.project_chosen(cfg(), bot, m, topics, "t" * 16, 0)
    started = calls[0][-1]
    assert "CLAUDE_CONFIG_DIR=" in started and "--session-id" in started
    sid = started.rsplit(" ", 1)[1]
    assert accounts.account_for(sid) == "work"           # recorded for the new session


def test_quota_hit_pattern_never_shadows_the_chunk_limit():
    # A constant named LIMIT in these modules once broke every long reply; keep them apart.
    assert not hasattr(accounts, "LIMIT") and not hasattr(mirror, "LIMIT")
    assert fmt.LIMIT == 3500 and len(fmt.chunks("word " * 3000)) > 1
    for text in ("You've hit your usage limit", "Claude usage limit reached", "Credit balance is too low"):
        assert accounts.QUOTA_HIT.search(text)
    assert not accounts.QUOTA_HIT.search("all good")


def limit_setup(bot, home, auto=True):
    make_profile("work")
    topics, _, thread = rig(bot)
    if not auto:
        accounts.toggle_auto()
    return thread


def test_quota_hit_moves_the_session_automatically_and_remembers_the_spent_account(home, bot):
    thread = limit_setup(bot, home)
    spawned = []
    assert mirror.limit_offer(cfg(), bot, SID, "You've hit your usage limit", spawn_fn=spawned.append)
    assert spawned == [["account", "auto-move", f"--session={SID}", f"--thread={thread}", "--target=work"]]
    sent = bot.methods("sendMessage")[-1]
    assert sent["message_thread_id"] == thread and "work" in sent["text"] and "reply_markup" not in sent
    assert "main" in json.loads((state_dir() / "account-limits.json").read_text())
    # a long ordinary reply that merely mentions the words is not a limit
    assert not mirror.limit_offer(cfg(), bot, SID, "x" * 700 + " usage limit", spawn_fn=spawned.append)


def test_quota_hit_offers_buttons_when_automatic_fallback_is_off(home, bot):
    limit_setup(bot, home, auto=False)
    assert not accounts.auto_enabled()
    assert mirror.limit_offer(cfg(), bot, SID, "Claude usage limit reached", spawn_fn=lambda a: pytest.fail("no auto move"))
    rows = bot.methods("sendMessage")[-1]["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["callback_data"] == "more:/account move work" and rows[-2][0]["callback_data"] == "more:/account add"
    assert rows[-1][0]["callback_data"] == "more:dismiss"  # "not now": the offer can be closed
    assert accounts.toggle_auto() is True


def test_quota_hit_matches_claude_limit_lines_and_ignores_ordinary_assistant_rate_limit(home, bot):
    for hit in (
        "You've hit your 5-hour limit · resets 12:50pm (Asia/Jerusalem)",
        "You've hit your weekly limit · resets Nov 1 at 8pm (Asia/Jerusalem)",
    ):
        assert accounts.QUOTA_HIT.search(hit)

    gh_reply = "The GitHub API returned a rate limit error; retrying."
    assert not accounts.QUOTA_HIT.search(gh_reply)
    thread = limit_setup(bot, home)
    spawned = []
    bot.calls.clear()
    assert not mirror.limit_offer(cfg(), bot, SID, gh_reply, spawn_fn=spawned.append)
    assert spawned == []
    assert bot.calls == []
    assert not (state_dir() / "account-limits.json").exists()


def test_fallback_skips_accounts_that_are_spent_or_just_ran_out(home, bot):
    make_profile("work")
    make_profile("spare")
    now = time.time()
    write("claude-usage-work.json", {"five_hour": 40, "weekly": 100, "weekly_resets": now + 3600})
    plan = accounts.fallback_plan(SID, now)
    assert plan["fresh"] == ["spare"] and plan["others"] == ["spare", "work"]
    write("claude-usage-work.json", {"five_hour": 100, "weekly": 10, "five_hour_resets": now - 5})
    assert "work" in accounts.fallback_plan(SID, now)["fresh"]            # its window already reset
    write("claude-usage-work.json", {"five_hour": 100, "five_hour_resets": now + 60})
    assert "work" not in accounts.fallback_plan(SID, now)["fresh"]
    write("claude-usage-work.json", {"five_hour": 99, "weekly": 99, "weekly_resets": now + 60})
    assert "work" in accounts.fallback_plan(SID, now)["fresh"]
    accounts.note_limit("spare", now)
    assert accounts.fallback_plan(SID, now)["fresh"] == ["work"]            # skipped for 5 hours
    assert "spare" in accounts.fallback_plan(SID, now + 5 * 3600 + 1)["fresh"]


def test_fallback_plan_and_limit_offer_derive_account_from_env(home, bot, monkeypatch):
    make_profile("work")
    make_profile("spare")
    accounts.use("main")
    assert accounts.current() == "main"
    acct3_dir = accounts.profiles_dir() / "work"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(acct3_dir))

    # A session the bridge did not start has no session record; env takes precedence
    unrecorded = "session-external-123"
    plan = accounts.fallback_plan(unrecorded)
    assert plan["here"] == "work"
    assert "work" not in plan["others"]
    assert "work" not in plan["fresh"]
    assert "main" in plan["others"]

    # limit_offer records limit against work, not main, and fallback moves to another account
    topics, _, thread = rig(bot, session=unrecorded)
    spawned = []
    limit_text = "You've hit your 5-hour limit · resets 12:50pm (Asia/Jerusalem)"
    assert mirror.limit_offer(cfg(), bot, unrecorded, limit_text, spawn_fn=spawned.append)
    recorded_limits = json.loads((state_dir() / "account-limits.json").read_text())
    assert "work" in recorded_limits
    assert "main" not in recorded_limits
    assert len(spawned) == 1
    target_arg = [arg for arg in spawned[0] if arg.startswith("--target=")][0]
    target = target_arg.split("=", 1)[1]
    assert target != "work"
    assert target in plan["fresh"]

    # When CLAUDE_CONFIG_DIR is unset: main only if no session record exists
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert accounts.fallback_plan("unrecorded-other")["here"] == "main"
    accounts.record("session-recorded-456", "spare")
    assert accounts.fallback_plan("session-recorded-456")["here"] == "spare"


def test_dashboard_has_a_quota_line_per_account_only_with_several(home, bot):
    c = cfg(target_language="he")
    assert accounts.usage_lines() == []
    make_profile("work")
    write("account-emails.json", {"main": "me@example.com", "work": "w@example.com"})
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write("claude-usage-main.json", {"at": stamp, "five_hour": 95, "weekly": 20,
                                     "five_hour_resets": time.time() + 3600})
    text = dashboard.text(c, {})
    assert "👤 חשבונות קלוד" in text
    # Weekly only (2026-11-10): a 5-hour reading is never shown, and the dot follows the weekly number
    assert "🟢 me: שבוע 20%" in text and "95%" not in text and "⚪ w: אין נתונים עדיין" in text
    write("claude-usage-work.json", {"at": stamp, "five_hour": 0, "weekly": 40})
    lines = accounts.usage_lines()
    assert any("w: שבוע 40%" in l and "5 שע׳" not in l for l in lines)


def test_account_menu_use_move_auto_add_and_login_code(home, bot, monkeypatch):
    make_profile("work")
    write("account-emails.json", {"main": "me@example.com"})
    topics, box, thread = rig(bot)
    say = Msgs()
    m = {"chat": {"id": OWNER}, "message_thread_id": thread, "message_id": 9}
    I.account_command(cfg(), bot, m, SID, "", say)
    rows = say.said[-1][1]["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["text"].startswith("📍 ⚪ \u2068me\u2069")
    assert any(r[0]["callback_data"] == "more:/account move work" for r in rows)
    I.account_command(cfg(), bot, m, SID, "use work", say)
    assert accounts.current() == "work" and "(" not in say.said[-1][0]       # unknown e-mail: no empty "()"
    monkeypatch.setattr(accounts, "email", lambda n, refresh=False: "w@example.com")
    I.account_command(cfg(), bot, m, SID, "use main", say)
    assert accounts.current() == "main" and "\nw\n" in say.said[-1][0]  # e-mail name, own line
    I.account_command(cfg(), bot, m, SID, "use ghost", say)
    assert "אין חשבון" in say.said[-1][0]
    I.account_command(cfg(), bot, m, SID, "auto", say)
    assert not accounts.auto_enabled() and "כבוי" in say.said[-1][0]
    # moving a working session asks first
    write_private(state_dir() / "session-state" / f"{SID}.json", json.dumps({"state": "busy"}))
    I.account_command(cfg(), bot, m, SID, "move work", say)
    assert bot.methods("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].endswith("work now")
    # login code: refused without a pending login, never reaches a session, the message is deleted
    out = I.account_code(cfg(), bot, dict(m, text="x"), "A" * 40)
    assert out == "refused" and bot.methods("deleteMessage")[-1]["message_id"] == 9
    assert accounts.login_pending() is None
    accounts.login_started("work", "acct-login-work")
    assert accounts.login_pending()["name"] == "work"
    login_file = state_dir() / "account-login.json"
    assert stat.S_IMODE(login_file.stat().st_mode) == 0o600
    update = {"message": {"chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}, "message_id": 4,
                          "date": 2_000_000_000, "text": "A" * 40, "message_thread_id": thread}}
    seen = []
    monkeypatch.setattr(I, "account_code", lambda c, b, mm, code: seen.append(code) or "account")
    assert I.handle(update, cfg(), bot, topics, box, SimpleNamespace(content_of=lambda mm: mm["text"]), 0) == "account"
    assert seen == ["A" * 40] and not box.pending(SID)


def test_move_session_resumes_it_on_the_other_account(home, bot, monkeypatch):
    make_profile("work")
    write("tmux-panes.json", {SID: "%5"})
    typed = []
    state = iter(["claude", "zsh"])
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "_pane_command", lambda pane: next(state))
    monkeypatch.setattr(I, "_tmux", lambda *a: typed.append(a) or SimpleNamespace(returncode=0, stdout=""))
    say = Msgs()
    assert I.account_move(cfg(), bot, SID, "work", say)
    assert accounts.account_for(SID) == "work"
    resume = [a for a in typed if a[:3] == ("send-keys", "-t", "%5") and "-l" in a and "--resume" in a[-1]][0][-1]
    assert resume.startswith("CLAUDE_CONFIG_DIR=") and resume.endswith(f"--remote-control --resume {SID}")
    assert "exec" not in resume                                    # typed into the pane's own shell
    assert not I.account_move(cfg(), bot, "../bad", "work", say)


# 2. stamp -----------------------------------------------------------------------------------

def test_statusline_saves_model_and_effort_per_session_and_quota_per_account(home):
    make_profile("work")
    payload = {"session_id": SID, "model": {"display_name": "Opus 5.5"}, "effort": {"level": "xhigh"},
               "rate_limits": {"five_hour": {"used_percentage": 41.6}, "seven_day": {"used_percentage": 7}}}
    env = {"CLAUDE_CONFIG_DIR": str(accounts.profiles_dir() / "work")}
    line = statusline.run(json.dumps(payload), env)
    assert line == "Opus 5.5 · week 7%"  # weekly only; the 5-hour reading is still saved below
    assert json.loads((state_dir() / "session-models.json").read_text())[SID] == "Opus 5.5|xhigh"
    for name in ("claude-usage.json", "claude-usage-work.json"):
        assert json.loads((state_dir() / name).read_text())["five_hour"] == 42
    assert not (state_dir() / "claude-usage-main.json").exists()
    assert statusline.run("not json") == ""
    assert statusline.run(json.dumps({"session_id": "short", "model": {"display_name": "M"}})) == "M"


def test_footer_stamp_has_model_effort_and_account_once_there_are_several(home, tmp_path):
    p = tmp_path / f"{SID}.jsonl"
    p.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-x", "usage": {"input_tokens": 5000}}}) + "\n")
    write("session-models.json", {SID: "Opus 5.5|high"})
    c = cfg()
    assert usage.footer(c, str(p)).startswith("🧠 הקשר 5 אלף\n🤖 \u2068Opus 5.5\u2069 · 🎚️ גבוהה")
    assert "👤" not in usage.footer(c, str(p))                                  # a single account: no account stamp
    make_profile("work")
    write("account-emails.json", {"main": "owner@example.com"})
    assert usage.footer(c, str(p)).startswith("🧠 הקשר 5 אלף\n🤖 \u2068Opus 5.5\u2069 · 🎚️ גבוהה\n👤 \u2068owner\u2069")
    assert usage.footer(c, "") == ""


def test_statusline_cli_prints_the_line(home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(read=lambda: json.dumps({"model": {"display_name": "Haiku"}})))
    assert cli.main(["statusline"]) == 0
    assert capsys.readouterr().out.strip() == "Haiku"


# 3. effort / menus -----------------------------------------------------------------------------

def menu_message(bot, text="👤 menu", thread=100, mid=50, buttons=None):
    return {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": thread, "message_id": mid, "text": text,
            "reply_markup": {"inline_keyboard": [[{"text": "🎚️ רמת חשיבה", "callback_data": "more:/effort"}]] if buttons is None else buttons}}


@pytest.mark.real_type_command
def test_effort_buttons_and_in_place_confirmation(home, bot, monkeypatch):
    topics, box, thread = rig(bot)
    write("tmux-panes.json", {SID: "%5"})
    sent = []
    monkeypatch.setattr(I, "_tmux", BoxedTmux(lambda *a: sent.append(a) or SimpleNamespace(returncode=0, stdout="", stderr="")))
    monkeypatch.setattr(I, "pane_text", lambda pane, lines=45: "idle")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "_terminal_background", lambda s, action: action() or True)
    m = menu_message(bot, thread=thread)
    assert I.cli_command(cfg(), bot, m, SID, "/effort") == "effort"
    rows = bot.methods("sendMessage")[-1]["reply_markup"]["inline_keyboard"]
    assert [b["callback_data"] for r in rows for b in r] == [f"more:/effort {x}" for x in I.EFFORTS] + ["more:dismiss"]
    assert rows[0][2]["text"] == "גבוהה" and all(len(b["callback_data"].encode()) <= 64 for r in rows for b in r)
    # a pick made in a menu message is typed into the session and confirmed in that same message
    bot.calls.clear()
    q = {"id": "q1", "from": {"id": OWNER}, "message": m, "data": "more:/effort high"}
    m["reply_markup"]["inline_keyboard"] = [[{"text": "גבוהה", "callback_data": "more:/effort high"}]]
    assert I.handle_button(q, cfg(), bot, topics, box) == "cli"
    assert bot.methods("answerCallbackQuery")[0]["text"] == "⏳ גבוהה"           # toast with the pressed label
    assert [a for a in sent if a[-1] == "/effort high"], "typed into the pane"
    edit = bot.methods("editMessageText")[-1]
    assert edit["message_id"] == 50 and edit["text"] == "‏✅ רמת חשיבה: גבוהה"
    assert not bot.methods("sendMessage")                                       # no screen dump, no extra message
    # the model menu links to the thinking level
    bot.calls.clear()
    I.cli_command(cfg(), bot, dict(m, reply_markup=None), SID, "/model")
    assert bot.methods("sendMessage")[-1]["reply_markup"]["inline_keyboard"][-2][0]["callback_data"] == "more:/effort"
    # only the bridge's own sub-commands are reachable through "more:"
    q["data"] = "more:/login"
    assert I.handle_button(q, cfg(), bot, topics, box) == "ignored"
    q["from"] = {"id": 99}
    q["data"] = "more:/effort low"
    assert I.handle_button(q, cfg(), bot, topics, box) == "ignored"


# 4. delivery choice ---------------------------------------------------------------------------

def test_held_messages_stay_queued_on_a_mid_turn_take(tmp_path):
    box = I.Inbox(tmp_path / "box")
    box.put("s", {"text": "now", "id": 1})
    box.put("s", {"text": "wait", "id": 2, "hold": True})
    assert [i["text"] for i in box.take("s", midturn=True)] == ["now"]
    assert [i["text"] for i in box.take("s", midturn=True)] == []             # only held ones are left
    assert box.pending("s")
    assert [i["text"] for i in box.take("s")] == ["wait"]                      # the turn's end delivers them
    box.put("s", {"text": "x", "id": 3, "hold": True})
    assert box.set_hold("s", 3, False) and [i["text"] for i in box.take("s", midturn=True)] == ["x"]
    assert not box.set_hold("s", 3, True)                                      # already delivered


def test_hooks_hand_over_held_messages_only_at_the_end_of_the_turn(home, bot, monkeypatch):
    box = I.default_inbox()
    box.put(SID, {"text": "hold me", "id": 2, "hold": True, "source": "owner", "chat": OWNER})
    monkeypatch.setattr(hooks, "spawn_delivered", lambda items: None)
    assert hooks.post_output({"session_id": SID}, cfg()) is None
    assert box.pending(SID)
    assert [m["text"] for m in hooks.telegram_messages(SID)] == ["hold me"]


def busy(session=SID):
    write_private(state_dir() / "session-state" / f"{session}.json", json.dumps({"state": "busy"}))


def owner_update(text, thread, mid=5, **extra):
    return {"message": dict({"message_id": mid, "date": 2_000_000_000, "text": text, "message_thread_id": thread,
                             "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}}, **extra)}


MEDIA = SimpleNamespace(content_of=lambda m: m.get("text") or "")


def test_message_while_busy_gets_a_receipt_with_the_delivery_choices(home, bot):
    topics, box, thread = rig(bot)
    assert I.handle(owner_update("idle one", thread, 4), cfg(), bot, topics, box, MEDIA, 0) == "queued"
    assert not any("יימסר" in p["text"] for p in bot.methods("sendMessage"))     # idle: no receipt
    box.take(SID)
    busy()
    assert I.handle(owner_update("work on", thread, 5), cfg(), bot, topics, box, MEDIA, 0) == "queued"
    ack = bot.methods("sendMessage")[-1]
    assert "בין צעדים" in ack["text"] and ack["reply_parameters"]["message_id"] == 5
    assert [b["callback_data"] for b in ack["reply_markup"]["inline_keyboard"][0]] == ["dl:hold:5", "dl:stop:5"]
    assert list(json.loads((state_dir() / "delivery-acks.json").read_text())) == ["5"]
    # the default can be "end of turn": then the message is held from the start
    delivery.set_default_mode("end")
    I.handle(owner_update("later", thread, 6), cfg(), bot, topics, box, MEDIA, 0)
    items = box.take(SID)
    assert [i.get("hold") for i in items] == [None, True]
    assert "סוף התור" in bot.methods("sendMessage")[-1]["text"]


def test_delivery_buttons_switch_a_queued_message_and_stop_interrupts(home, bot, monkeypatch):
    topics, box, thread = rig(bot)
    busy()
    I.handle(owner_update("a", thread, 5), cfg(), bot, topics, box, MEDIA, 0)
    ack_msg = {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": thread, "message_id": 77}
    q = {"id": "q", "from": {"id": OWNER}, "message": ack_msg, "data": "dl:hold:5"}
    assert I.handle_button(q, cfg(), bot, topics, box) == "delivery"
    assert box.take(SID, midturn=True) == [] and bot.methods("editMessageText")[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "dl:now:5"
    write("tmux-panes.json", {SID: "%5"})
    keys = []
    monkeypatch.setattr(I, "_tmux", lambda *a: keys.append(a) or SimpleNamespace(returncode=0))
    q["data"] = "dl:stop:5"
    assert I.handle_button(q, cfg(), bot, topics, box) == "delivery"
    assert ("send-keys", "-t", "%5", "Escape") in keys
    assert bot.methods("editMessageText")[-1]["reply_markup"] == {"inline_keyboard": []}
    box.take(SID)
    assert I.handle_button(q, cfg(), bot, topics, box) == "ignored"            # already delivered
    assert bot.methods("answerCallbackQuery")[-1]["text"] == "כבר נמסר"
    q["data"] = "dl:bogus:5"
    assert I.handle_button(q, cfg(), bot, topics, box) == "ignored"


def test_receipts_are_deleted_when_the_message_is_delivered(home, bot):
    topics, box, thread = rig(bot)
    busy()
    I.handle(owner_update("a", thread, 5), cfg(), bot, topics, box, MEDIA, 0)
    ack = json.loads((state_dir() / "delivery-acks.json").read_text())["5"]
    bot.calls.clear()
    assert I.run_delivered(cfg(), [f"{OWNER}:5"], bot) == 0
    assert bot.methods("deleteMessage") == [{"chat_id": OWNER, "message_id": ack}]
    assert json.loads((state_dir() / "delivery-acks.json").read_text()) == {}


def test_settings_menu_toggles_defaults_and_voice_confirmation(home, bot):
    topics, box, thread = rig(bot)
    m = menu_message(bot, "⚙️ עוד", thread=thread, buttons=[[{"text": "📨", "callback_data": "more:delivery"}]])
    q = {"id": "q", "from": {"id": OWNER}, "message": m, "data": "more:delivery"}
    assert I.handle_button(q, cfg(), bot, topics, box) == "settings" and delivery.default_mode() == "end"
    I.handle_button(q, cfg(), bot, topics, box)
    assert delivery.default_mode() == "now"
    q["data"] = "more:voiceconfirm"
    I.handle_button(q, cfg(), bot, topics, box)
    assert not voice.confirm_on()
    I.handle_button(q, cfg(), bot, topics, box)
    assert voice.confirm_on()
    bot.calls.clear()
    I.more_menu(bot, cfg(), m)
    session_labels = [b["text"] for r in bot.methods("sendMessage")[-1]["reply_markup"]["inline_keyboard"] for b in r]
    # model and thinking moved to the fixed keyboard (live labels); status and screen took their place here
    assert any("📋" in x for x in session_labels) and any("🖥️" in x for x in session_labels)
    assert any("חשבון לסשן הזה" in x for x in session_labels)
    # The menu split moved general settings (voice confirmation & delivery) to global page
    q_global = {"id": "q", "from": {"id": OWNER}, "message": dict(m, message_id=10), "data": "more:global"}
    I.handle_button(q_global, cfg(), bot, topics, box)
    global_labels = [b["text"] for r in bot.methods("editMessageText")[-1]["reply_markup"]["inline_keyboard"] for b in r]
    assert any("אישור הקלטות: פועל" in x for x in global_labels) and any("בין צעדים" in x for x in global_labels)


# 5. voice approval -----------------------------------------------------------------------------

class VoiceMedia:
    def content_parts(self, m):
        return "🎤 (הודעה קולית, תומללה) fix the bug", ""


def voice_update(thread, mid=8):
    return {"message": {"message_id": mid, "date": 2_000_000_000, "voice": {"file_id": "f"}, "message_thread_id": thread,
                        "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER}}}


def test_voice_transcript_waits_for_send_edit_or_cancel(home, bot):
    topics, box, thread = rig(bot)
    c = cfg(target_language="he")
    assert I.handle(voice_update(thread), c, bot, topics, box, VoiceMedia(), 0) == "voice"
    assert not box.pending(SID)                                                   # nothing reached the session
    card = bot.methods("sendMessage")[-1]
    assert "fix the bug" in card["text"] and "(הודעה קולית" not in card["text"]
    assert [b["callback_data"] for b in card["reply_markup"]["inline_keyboard"][0]] == ["vd:send:8", "vd:edit:8", "vd:cancel:8"]
    drafts = state_dir() / "voice-drafts.json"
    assert stat.S_IMODE(drafts.stat().st_mode) == 0o600 and "8" in json.loads(drafts.read_text())
    card_msg = {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": thread, "message_id": 60,
                "text": "🎤 זה מה ששמעתי:\nfix the bug"}
    q = {"id": "q", "from": {"id": OWNER}, "message": card_msg, "data": "vd:cancel:8"}
    assert I.handle_button(q, c, bot, topics, box) == "voice" and not box.pending(SID)
    assert I.handle_button(q, c, bot, topics, box) == "ignored"                   # a second tap does nothing
    # send: queued exactly once, with the original message id
    I.handle(voice_update(thread, 9), c, bot, topics, box, VoiceMedia(), 0)
    q["data"] = "vd:send:9"
    assert I.handle_button(q, c, bot, topics, box) == "voice"
    items = box.take(SID)
    assert [i["id"] for i in items] == [9] and "fix the bug" in items[0]["text"]
    assert voice.load() == {}
    # edit: the next text of the topic replaces the recording
    I.handle(voice_update(thread, 10), c, bot, topics, box, VoiceMedia(), 0)
    q["data"] = "vd:edit:10"
    I.handle_button(q, c, bot, topics, box)
    I.handle(owner_update("fix the other bug", thread, 11), c, bot, topics, box, MEDIA, 0)
    assert "the owner corrected its transcript" in box.take(SID)[0]["text"]



def test_a_refused_correction_in_a_session_topic_keeps_the_edit(home, bot):
    """The edit draft is taken only once the correction is accepted: a refused one left the next correction typed."""
    topics, box, thread = rig(bot)
    c = cfg(target_language="he")
    I.handle(voice_update(thread, 10), c, bot, topics, box, VoiceMedia(), 0)
    card_msg = {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": thread, "message_id": 60, "text": "x"}
    I.handle_button({"id": "q", "from": {"id": OWNER}, "message": card_msg, "data": "vd:edit:10"}, c, bot, topics, box)
    assert I.handle(owner_update("token ghp_" + "A" * 30, thread, 11), c, bot, topics, box, MEDIA, 0) == "refused"
    assert voice.load() and not box.pending(SID)
    I.handle(owner_update("fix the other bug", thread, 12), c, bot, topics, box, MEDIA, 0)
    [item] = box.take(SID)
    assert "the owner corrected its transcript" in item["text"] and item.get("voice") and not item.get("typed")
    assert voice.load() == {}

def test_voice_goes_straight_in_for_car_mode_dispatch_and_when_disabled(home, bot):
    topics, box, thread = rig(bot)
    c = cfg(target_language="he")
    voice.set_confirm(False)
    assert I.handle(voice_update(thread), c, bot, topics, box, VoiceMedia(), 0) == "queued"
    voice.set_confirm(True)
    box.take(SID)
    I.set_car_mode(True)
    assert I.handle(voice_update(thread, 12), c, bot, topics, box, VoiceMedia(), 0) == "queued"
    assert voice.load() == {}


def test_voice_is_never_lost_when_the_preview_cannot_be_sent(home, bot):
    topics, box, thread = rig(bot)
    bot.fail["sendMessage"] = "boom"
    assert I.handle(voice_update(thread, 13), cfg(target_language="he"), bot, topics, box, VoiceMedia(), 0) == "voice"
    assert [i["id"] for i in box.take(SID)] == [13] and voice.load() == {}


# 6. commands while busy ----------------------------------------------------------------------------

@pytest.mark.real_type_command
def test_command_while_busy_asks_end_of_turn_or_stop(home, bot, monkeypatch):
    topics, box, thread = rig(bot)
    write("tmux-panes.json", {SID: "%5"})
    keys = []
    monkeypatch.setattr(I, "_tmux", BoxedTmux(lambda *a: keys.append(a) or SimpleNamespace(returncode=0, stdout="", stderr="")))
    monkeypatch.setattr(I, "pane_text", lambda pane, lines=45: "idle prompt")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "_terminal_background", lambda s, action: action() or True)
    busy()
    m = {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": thread, "message_id": 20}
    assert I.cli_command(cfg(), bot, m, SID, "/context") == "cli"
    ask = bot.methods("sendMessage")[-1]
    assert [b["callback_data"] for b in ask["reply_markup"]["inline_keyboard"][0]] == ["cw:end:/context", "cw:now:/context"]
    assert not any(a[-1] == "/context" for a in keys)                           # nothing was typed yet
    # "stop and run now": Escape, then the command runs (the session reports idle)
    write_private(state_dir() / "session-state" / f"{SID}.json", json.dumps({"state": "busy"}))
    monkeypatch.setattr(I, "session_busy", lambda s: not any(a[-1] == "Escape" for a in keys))
    q = {"id": "q", "from": {"id": OWNER}, "message": dict(m, message_id=21), "data": "cw:now:/context"}
    I.command_when(q, cfg(), bot, q["message"], SID)
    assert any(a[-1] == "Escape" for a in keys) and any(a[-1] == "/context" for a in keys)
    assert bot.methods("editMessageText")[0]["text"].startswith("‏⏹️ עוצר ומבצע")


# 7/10. /esc, placeholder order -----------------------------------------------------------------------

def test_esc_answers_with_a_short_line_not_a_screen_dump(home, bot, monkeypatch):
    topics, box, thread = rig(bot)
    write("tmux-panes.json", {SID: "%5"})
    monkeypatch.setattr(I, "_tmux", lambda *a: SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(I, "pane_runs_session", lambda p, s: True)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "_terminal_background", lambda s, action: action() or True)
    m = {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": thread, "message_id": 20}
    monkeypatch.setattr(I, "pane_text", lambda pane, lines=45: "❯ ")
    rule = "─" * 80
    screens = ["✶ Ebbing… (16s · ↓ 1.1k tokens)\n" + rule + "\n❯ \n" + rule, "⏺ ok\n" + rule + "\n❯ \n" + rule]
    monkeypatch.setattr(I, "pane_raw", lambda pane, lines=45: screens.pop(0) if len(screens) > 1 else screens[0])
    I.cli_command(cfg(), bot, m, SID, "/esc")
    last = bot.methods("sendMessage")[-1]
    assert "נעצר" in last["text"] and "parse_mode" not in last and "reply_markup" not in last
    monkeypatch.setattr(I, "pane_raw", lambda pane, lines=45: (_ for _ in ()).throw(OSError("gone")))
    I.cli_command(cfg(), bot, m, SID, "/esc")  # the screen cannot be read: sent, not confirmed
    assert "עדיין מסיים" in bot.methods("sendMessage")[-1]["text"]


def test_old_placeholder_is_deleted_before_the_new_one_is_sent(bot):
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "Title")
    set_state(cfg(), "s", "busy", bot)
    first = session_state("s")["placeholder"]
    before = len(bot.calls)
    set_state(cfg(), "s", "bottom", bot)
    order = [m for m, _ in bot.calls[before:] if m in ("deleteMessage", "sendMessage")]
    assert order == ["deleteMessage", "sendMessage"]
    assert bot.methods("deleteMessage")[0]["message_id"] == first
    assert session_state("s")["placeholder"] != first


# 9. compaction progress ----------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, n):
        self.t += n


def test_compact_progress_is_one_self_updating_message_capped_at_95(bot):
    clock = Clock()
    screens = iter(["Compacting conversation ↓ 12.3k tokens"] * 40 + ["done"])
    assert compact.started_from_telegram(SID) is False
    ok = compact.run(cfg(), bot, 100, SID, lambda: next(screens), sleep=clock.sleep, clock=clock)
    assert ok
    assert len(bot.methods("sendMessage")) == 1                                   # one message, edited in place
    edits = [p["text"] for p in bot.methods("editMessageText")]
    pcts = [int(t.split("%")[0].split()[-1]) for t in edits[:-1]]
    assert pcts == sorted(pcts) and max(pcts) <= compact.CAP
    assert not any("12.3k" in e or "tokens" in e.lower() or "טוקנ" in e for e in edits)  # the spinner's counter is not a figure of the summary
    assert "הדחיסה הסתיימה" in edits[-1] and "100%" in edits[-1]
    assert compact.past_durations() and not compact.marker(SID).exists()           # history kept, markers gone


def test_compact_expected_time_comes_from_past_runs():
    assert compact.expected_seconds([]) == compact.DEFAULT_EXPECT
    assert compact.expected_seconds([200, 100, 60, 10, 10, 10]) == pytest.approx(38.0)
    assert compact.percent(10_000, 90) == compact.CAP and compact.percent(0, 90) == 0


def test_compact_progress_uses_the_result_object_message_id(bot):
    clock = Clock()
    assert compact.run(cfg(), bot, None, SID, lambda: "", sleep=clock.sleep, clock=clock) is False   # never started
    assert "לא ראיתי" in bot.methods("editMessageText")[-1]["text"]
    assert bot.methods("editMessageText")[0]["message_id"] == 1                   # FakeBot returns {"message_id": n} itself


def test_wake_writes_done_instead_of_its_own_notice_and_precompact_is_skipped(home, bot, monkeypatch):
    spawned = []
    monkeypatch.setattr(wake, "spawn", lambda args, text=None: spawned.append(args))
    monkeypatch.setattr(wake, "load_config", lambda: cfg())
    monkeypatch.setattr(hooks, "spawn", lambda args, text=None: spawned.append(args))
    monkeypatch.setattr(hooks, "load_config", lambda: cfg())
    event = json.dumps({"session_id": SID, "source": "compact"})
    wake.hook_start(event)
    assert spawned == [["lifecycle", f"--session={SID}", "--kind=compact-done"]]    # not started from Telegram
    spawned.clear()
    write_private(compact.marker(SID), str(time.time()))
    wake.hook_start(event)
    assert compact.done_marker(SID).exists()
    assert spawned == [["session-state", f"--session={SID}", "--state=done"]]
    spawned.clear()
    pre = json.dumps({"session_id": SID, "hook_event_name": "PreCompact"})
    hooks.hook_lifecycle(pre)
    assert spawned == []                                                             # the progress bar is the notice
    os.utime(compact.marker(SID), (time.time() - 500,) * 2)
    hooks.hook_lifecycle(pre)
    assert spawned == [["lifecycle", f"--session={SID}", "--kind=precompact"]]       # an old marker does not count


@pytest.mark.real_type_command
def test_compact_from_the_keyboard_starts_the_progress_message(home, bot, monkeypatch):
    topics, box, thread = rig(bot)
    write("tmux-panes.json", {SID: "%5"})
    started = []
    monkeypatch.setattr(I, "_tmux", BoxedTmux(lambda *a: SimpleNamespace(returncode=0, stdout="", stderr="")))
    monkeypatch.setattr(I, "pane_text", lambda pane, lines=45: "idle")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "_terminal_background", lambda s, action: action() or True)
    monkeypatch.setattr(I, "_start_compact_progress", lambda *a: started.append(a[3]))
    m = {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": thread, "message_id": 20}
    assert I.cli_command(cfg(), bot, m, SID, "/compact") == "cli"
    assert started == [SID] and not bot.methods("sendMessage")                       # no screen dump


# 12. topic icons ----------------------------------------------------------------------------------

class IconBot(FakeBot):
    def call(self, method, params=None, timeout=15):
        if method == "getForumTopicIconStickers":
            self.calls.append((method, params or {}))
            return [{"emoji": "💡", "custom_emoji_id": "id3"}]
        if method == "editForumTopic" and params.get("message_thread_id") in getattr(self, "dead", ()):
            self.calls.append((method, params))
            raise TelegramError("topic deleted")
        return super().call(method, params, timeout)


def test_topic_icons_newest_first_and_failures_are_recorded_then_skipped(monkeypatch):
    bot = IconBot()
    c = cfg()
    tm = TopicMap(state_dir() / "topics.json", bot, OWNER)
    for i in range(7):
        tm.ensure(f"s{i}", f"Topic {i}")
    from ctb import translate
    monkeypatch.setattr(translate, "ask", lambda prompt, cc: "")
    newest = tm.thread_of("s6")
    bot.dead = {newest}
    topic_icons.run(c, bot)
    first_round = [p["message_thread_id"] for p in bot.methods("editForumTopic") if "icon_custom_emoji_id" in p]
    assert first_round[0] == newest and len(first_round) == topic_icons.MAX_PER_RUN    # newest first
    picks = json.loads((state_dir() / "topic-icons.json").read_text())
    assert picks[str(newest)]["tries"] == 1 and "error" in picks[str(newest)]
    # the failing topic is retried (after the wait) twice more, then skipped for good
    for expected in (2, 3):
        picks = json.loads((state_dir() / "topic-icons.json").read_text())
        picks[str(newest)]["failed_at"] = 0
        write("topic-icons.json", picks)
        topic_icons.run(c, bot)
        assert json.loads((state_dir() / "topic-icons.json").read_text())[str(newest)]["tries"] == expected
    picks = json.loads((state_dir() / "topic-icons.json").read_text())
    picks[str(newest)]["failed_at"] = 0
    write("topic-icons.json", picks)
    bot.calls.clear()
    topic_icons.run(c, bot)
    assert newest not in [p.get("message_thread_id") for p in bot.methods("editForumTopic")]
    assert len(json.loads((state_dir() / "topic-icons.json").read_text())) == 7           # every other topic got its icon


# 13. weekly reset form ----------------------------------------------------------------------------------

JER = "Asia/Jerusalem"


def stamp(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


@pytest.mark.parametrize("body,expected", [
    ("Usage limit reached, resets 12:50pm", "2026-10-29 12:50"),
    ("limit reached, resets 8pm", "2026-10-29 20:00"),
    ("Weekly limit reached, resets Nov 1 at 8pm", "2026-11-01 20:00"),
    ("Weekly limit reached, resets Nov 1, 8:30am", "2026-11-01 08:30"),
    ("resets Jan 3 at 9am", "2027-01-03 09:00"),
])
def test_reset_time_understands_clock_only_and_weekly_forms(body, expected):
    got = usage.reset_time(body, stamp("2026-10-29T10:00:00"))
    assert got.strftime("%Y-%m-%d %H:%M") == expected


def test_reset_time_falls_back_to_five_hours_when_unreadable():
    s = stamp("2026-10-29T10:00:00")
    for body in ("no clock here", "resets Foo 1 at 8pm", "resets 13pm"):
        assert usage.reset_time(body, s) == (s + __import__("datetime").timedelta(hours=5)).astimezone()


def test_limit_clock_shows_the_date_when_the_reset_is_not_today(tmp_path, monkeypatch):
    def row(text):
        return {"type": "assistant", "isApiErrorMessage": True, "apiError": "usage_limit_reached",
                "timestamp": "2026-10-29T10:00:00Z", "message": {"content": [{"text": text}]}}
    project = tmp_path / "projects" / "p"
    project.mkdir(parents=True)
    monkeypatch.setattr(usage, "PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(usage.time, "time", lambda: 1_793_268_100.0)           # 2026-10-29 12:21 Jerusalem
    (project / "a.jsonl").write_text(json.dumps(row("Usage limit reached, resets 4:30pm")) + "\n")
    assert usage.limit_of("a")["clock"] == "16:30"
    (project / "b.jsonl").write_text(json.dumps(row("Weekly limit reached, resets Nov 1 at 8pm")) + "\n")
    assert usage.limit_of("b")["clock"] == "01.11 20:00"


def test_cancel_takes_a_waiting_message_back_and_keeps_the_note(home, bot):
    topics, box, thread = rig(bot)
    busy()
    I.handle(owner_update("a", thread, 5), cfg(), bot, topics, box, MEDIA, 0)
    I.handle(owner_update("b", thread, 6), cfg(), bot, topics, box, MEDIA, 0)
    rows = bot.methods("sendMessage")[-1]["reply_markup"]["inline_keyboard"]
    assert rows[-1][0]["callback_data"] == "dl:cancel:6"
    ack_msg = {"chat": {"id": OWNER, "type": "private"}, "message_thread_id": thread, "message_id": 77}
    q = {"id": "q", "from": {"id": OWNER}, "message": ack_msg, "data": "dl:cancel:5"}
    assert I.handle_button(q, cfg(), bot, topics, box) == "delivery"
    edit = bot.methods("editMessageText")[-1]
    assert "בוטל" in edit["text"] and edit["reply_markup"] == {"inline_keyboard": []}
    assert delivery.is_delivered(5) and "5" not in json.loads((state_dir() / "delivery-acks.json").read_text())
    assert [i["id"] for i in box.take(SID)] == [6]                             # only the other one arrives
    assert I.handle_button(q, cfg(), bot, topics, box) == "ignored"            # a second tap finds nothing
    assert bot.methods("answerCallbackQuery")[-1]["text"] == "כבר נמסר"
