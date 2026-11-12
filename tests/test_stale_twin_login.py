"""2026-11-02: main held a login from before the plan upgrade (status: pro) while the same address in
account2 reported max. Among twins the bridge uses the login with the bigger plan, never the stale one."""
from __future__ import annotations

import json

import pytest

from conftest import GOOD_TOKEN, FakeBot
from types import SimpleNamespace

from ctb import accounts, identity, inbox as I
from ctb.config import Config, state_dir, write_private

NOW = 2_000_000.0
SID = "sess-stale-1"
MARKER = "FAKE-CREDENTIAL-MARKER-DO-NOT-LOG"


def cfg(lang="en"):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language=lang)


def set_emails(**mails):
    write_private(state_dir() / "account-emails.json", json.dumps(mails))


def set_plans(**plans):
    """Status plans in the stored record form (the address each profile is cached as, no tier)."""
    mails = json.loads((state_dir() / "account-emails.json").read_text()) if (state_dir() / "account-emails.json").exists() else {}
    write_private(state_dir() / "account-plans.json", json.dumps({
        n: {"email": mails.get(n, ""), "plan": accounts.norm_plan(p), "tier": "", "source": "status"}
        for n, p in plans.items()}))


def general_texts(bot):
    return [p["text"] for p in bot.methods("sendMessage") if "message_thread_id" not in p]


def stale_texts(bot):
    return [x for x in general_texts(bot) if "older login" in x or "התחברות ישנה" in x]


def full_window(name, until):
    write_private(accounts.usage_path(name), json.dumps({"at": "2026-11-01T10:00:00Z", "five_hour": 100,
                                                        "five_hour_resets": until, "weekly": 10, "weekly_resets": until}))


@pytest.fixture
def twins_pair():
    """main = stale pro login, account2 = max login, one address."""
    accounts.add("account2")
    set_emails(main="me@example.com", account2="me@example.com")
    set_plans(main="pro", account2="max")


def test_the_plan_name_is_read_from_the_login_status_and_cached(monkeypatch):
    accounts.add("account2")
    reads = {"main": {"email": "me@example.com", "subscriptionType": "pro", "token": MARKER},
             "account2": {"email": "me@example.com", "subscriptionType": "max"}}
    monkeypatch.setattr(accounts, "auth_status", lambda name, timeout=30: reads[name])
    accounts.refresh_emails(force=True, now=NOW)
    assert accounts.cached_plan("main") == "claude_pro" and accounts.cached_plan("account2") == "claude_max"
    assert MARKER not in (state_dir() / "account-plans.json").read_text()
    reads["account2"] = {"loggedIn": False}
    accounts.refresh_emails(force=True, now=NOW + 100)
    assert accounts.cached_plan("account2") == ""                                   # signed out: forgotten


def test_twins_with_different_plans_pick_the_bigger_plan(twins_pair):
    assert accounts.usable_in(["main", "account2"]) == "account2"
    assert accounts.better_twins("main") == ["account2"] and accounts.better_twins("account2") == []
    assert accounts.stale_twins(["main", "account2"]) == [("main", "account2")]
    assert accounts.current() == "account2"                                         # new sessions: never the stale one
    assert accounts.account_for(None) == "account2"


def test_without_plan_data_nothing_changes(twins_pair):
    set_plans()
    assert accounts.usable_in(["main", "account2"]) == "main"
    assert accounts.stale_twins(["main", "account2"]) == [] and accounts.current() == "main"


def test_equal_plans_are_one_shared_quota_as_before(twins_pair):
    set_plans(main="max", account2="max")
    full_window("main", NOW + 3600)
    assert accounts.spent("account2", NOW) and accounts.used("account2", NOW) == 100.0


def test_a_limit_move_never_targets_the_stale_twin(twins_pair):
    accounts.add("small")
    set_emails(main="me@example.com", account2="me@example.com", small="other@example.com")
    set_plans(main="pro", account2="max", small="pro")
    accounts.record(SID, "small")
    assert accounts.distinct_others("small") == ["account2"]
    plan = accounts.fallback_plan(SID, now=NOW)
    assert plan["others"] == ["account2"] and plan["fresh"] == ["account2"]
    full_window("small", NOW + 3600)
    assert accounts.pick_account("small", now=NOW) == "account2"


def test_the_stale_twins_own_limit_does_not_spend_the_better_twin(twins_pair):
    accounts.note_limit("main", now=NOW)
    assert accounts.used("main", NOW + 1) == 100.0 and accounts.spent("main", NOW + 1)
    assert accounts.used("account2", NOW + 1) < 100.0 and not accounts.spent("account2", NOW + 1)


def test_the_incident_replay_a_session_on_the_stale_main_moves_to_account2(twins_pair):
    accounts.record(SID, "main")
    accounts.note_limit("main", now=NOW)
    plan = accounts.fallback_plan(SID, now=NOW + 5)
    assert plan["here"] == "main" and plan["others"] == ["account2"] and plan["fresh"] == ["account2"]
    accounts.record(SID, "account2")
    assert accounts.distinct_others("account2") == []                                # never back to the stale one


def test_is_big_plan_follows_the_login_not_the_old_json(twins_pair):
    for name in ("main", "account2"):
        path = accounts.claude_json() if name == "main" else accounts.profiles_dir() / name / ".claude.json"
        write_private(path, json.dumps({"oauthAccount": {"emailAddress": "me@example.com",
                                                         "organizationRateLimitTier": "default_claude_max_20x"}}))
    assert accounts.is_big_plan("account2") is True
    assert accounts.is_big_plan("main") is False                                     # stale login: pro
    set_plans()
    assert accounts.is_big_plan("main") is True                                      # no status plan: the old json rule


def test_a_signed_out_better_twin_is_not_used(twins_pair):
    write_private(state_dir() / "account-status.json", json.dumps({"account2": "out"}))
    assert accounts.usable_in(["main", "account2"]) == "main" and accounts.better_twins("main") == []


def test_the_saved_default_is_left_alone_and_current_swaps_in_the_better_twin(twins_pair):
    accounts.use("main")
    accounts.check_default_account(now=NOW)
    assert (state_dir() / "account-current").read_text().strip() == "main"           # nothing is written for the swap
    assert accounts.current() == "account2"
    set_plans(main="max", account2="max")                                            # the owner signed in again
    assert accounts.current() == "main"


def test_an_unreadable_status_keeps_the_cached_plan(twins_pair, monkeypatch):
    monkeypatch.setattr(accounts, "auth_status", lambda name, timeout=30: {})
    accounts.refresh_emails(force=True, now=NOW)
    assert accounts.cached_plan("main") == "claude_pro" and accounts.cached_plan("account2") == "claude_max"
    assert accounts.better_twins("main") == ["account2"]


def test_a_status_without_a_plan_keeps_the_cached_plan_so_the_notice_does_not_repeat(twins_pair, monkeypatch):
    monkeypatch.setattr(accounts, "auth_status", lambda name, timeout=30: {"email": "me@example.com"})
    accounts.refresh_emails(force=True, now=NOW)
    assert accounts.cached_plan("main") == "claude_pro" and accounts.cached_plan("account2") == "claude_max"
    bot = FakeBot()
    for k in range(3):
        identity.check(cfg(), bot, force=True, now=NOW + 100 * k)
    assert len(stale_texts(bot)) == 1


def test_a_spent_better_twin_is_not_replaced_by_the_stale_one_for_a_third_account(twins_pair):
    accounts.add("small")
    set_emails(main="me@example.com", account2="me@example.com", small="other@example.com")
    set_plans(main="pro", account2="max", small="pro")
    accounts.record(SID, "small")
    full_window("account2", NOW + 3600)                                              # account2 is spent, main has room
    plan = accounts.fallback_plan(SID, now=NOW)
    assert plan["others"] == ["account2"] and plan["fresh"] == []                    # main is never offered
    full_window("small", NOW + 3600)
    assert accounts.pick_account("small", now=NOW) is None


def test_the_menu_dot_of_the_better_twin_ignores_the_stale_twins_usage(twins_pair):
    soon = 4_000_000_000
    write_private(state_dir() / "claude-usage-main.json", json.dumps({"five_hour": 100, "five_hour_resets": soon}))
    write_private(state_dir() / "claude-usage-account2.json", json.dumps({"five_hour": 5, "five_hour_resets": soon}))
    assert I._account_dot("account2") == "🟢" and I._account_dot("main") == "🔴"


def _movable(monkeypatch):
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)
    monkeypatch.setattr(I, "_tmux", lambda *a: SimpleNamespace(returncode=0, stdout="/x\n"))
    states = iter(["zsh"])
    monkeypatch.setattr(I, "_pane_command", lambda p: next(states))
    monkeypatch.setattr(accounts, "auth_status", lambda name, timeout=30: {
        "email": "me@example.com", "subscriptionType": "pro" if name == "main" else "max"})


def test_a_move_from_the_stale_twin_to_the_better_twin_is_not_refused(twins_pair, monkeypatch):
    accounts.record(SID, "main")
    _movable(monkeypatch)
    said = []
    assert I.account_move(cfg(), FakeBot(), SID, "account2", lambda text, **kw: said.append(text)) is True
    assert not [x for x in said if "same account" in x]
    assert accounts.account_for(SID) == "account2"


def test_a_move_the_other_way_between_twins_is_still_refused(twins_pair, monkeypatch):
    accounts.record(SID, "account2")
    _movable(monkeypatch)
    monkeypatch.setattr(I, "_tmux", lambda *a: pytest.fail("must not type anything"))
    said = []
    assert I.account_move(cfg(), FakeBot(), SID, "main", lambda text, **kw: said.append(text)) is False
    assert len(said) == 1 and "same account" in said[0]


def test_the_stale_twin_is_flagged_once_in_the_log_and_in_general(twins_pair, monkeypatch):
    monkeypatch.setattr(accounts, "auth_status", lambda name, timeout=30: {
        "email": "me@example.com", "subscriptionType": "pro" if name == "main" else "max", "accessToken": MARKER})
    bot = FakeBot()
    for k in range(4):
        identity.check(cfg(), bot, force=True, now=NOW + 100 * k)
    assert stale_texts(bot) == ["⚠️ main is signed in with an older login of the same account; using account2. "
                                "Signing in again on main fixes it."]
    assert not [x for x in general_texts(bot) if "same account (" in x]              # not also the generic duplicate notice
    log = (state_dir() / "poll.log").read_text()
    assert log.count("stale twin login") == 1 and "using account2" in log
    assert MARKER not in log and "me@example.com" not in log
    assert MARKER not in (state_dir() / "account-notices.json").read_text()


def test_a_resolved_stale_twin_that_returns_is_flagged_again(twins_pair):
    bot = FakeBot()
    identity._announce_stale(cfg(), bot, "en", NOW)
    identity._announce_stale(cfg(), bot, "en", NOW + 100)
    assert len(stale_texts(bot)) == 1
    set_plans(main="max", account2="max")
    identity._announce_stale(cfg(), bot, "en", NOW + 200)
    set_plans(main="pro", account2="max")
    identity._announce_stale(cfg(), bot, "en", NOW + 300)
    assert len(stale_texts(bot)) == 2


def test_a_failed_stale_post_is_retried(twins_pair):
    class Down(FakeBot):
        def call(self, method, params=None, timeout=15):
            raise RuntimeError("down")
    assert identity._announce_stale(cfg(), Down(), "en", NOW) == []
    assert identity._announce_stale(cfg(), FakeBot(), "en", NOW + 100) == [("main", "account2")]


def test_the_notice_is_in_hebrew_for_a_hebrew_owner(twins_pair):
    bot = FakeBot()
    identity._announce_stale(cfg("he"), bot, "he", NOW)
    text = stale_texts(bot)[0]
    assert text.startswith("\u200f")                                                 # RTL mark
    assert "\u2068main\u2069" in text and "\u2068account2\u2069" in text           # names in bidi isolates
    assert "{" not in text
