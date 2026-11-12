"""Account identity: a profile signed in as another account in a terminal was still called by its old address. Addresses are re-read, a change is announced once, profiles
that share a login are one account (one menu row, shared quota, never moved between)."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import accounts, identity, inbox as I
from ctb.config import Config, state_dir, write_private

SID = "sess-ident-1"
NOW = 2_000_000.0


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language="en")


class Said(list):
    def __call__(self, text, **kw):
        self.append((text, kw))


def set_emails(**mails):
    write_private(state_dir() / "account-emails.json", json.dumps(mails))


def logins(monkeypatch, **mails):
    """`claude auth status` stand-in: the login each profile really has; counts the reads."""
    calls = []

    def fake(name, timeout=30):
        calls.append(name)
        if mails.get(name) == "OUT":
            return {"loggedIn": False}                                   # `claude auth status` of a signed-out profile
        return {"email": mails[name]} if mails.get(name) else {}
    monkeypatch.setattr(accounts, "auth_status", fake)
    return calls


def general_texts(bot):
    return [p["text"] for p in bot.methods("sendMessage") if "message_thread_id" not in p]


@pytest.fixture
def pair():
    """The incident: main and second, caches say two different people."""
    accounts.add("second")
    set_emails(main="alice@example.com", second="bob@example.com")


# --- 1. a stale cache is refreshed ---------------------------------------------------------------

def test_a_stale_cached_address_is_refreshed_and_reported_as_a_change(pair, monkeypatch):
    logins(monkeypatch, main="bob@example.com", second="bob@example.com")
    assert accounts.label("main") == "alice"
    changes = accounts.refresh_emails(now=NOW)
    assert changes == [("main", "alice@example.com", "bob@example.com")]
    assert accounts.cached_email("main") == "bob@example.com" and accounts.label("main") == "bob"


def test_a_first_fill_is_not_a_change_and_an_unreadable_status_keeps_the_cache(pair, monkeypatch):
    accounts.add("fresh")
    logins(monkeypatch, main="", second="", fresh="new@example.com")
    assert accounts.refresh_emails(now=NOW) == []
    assert accounts.cached_email("fresh") == "new@example.com"
    assert accounts.cached_email("main") == "alice@example.com"


def test_reads_are_throttled_to_ten_minutes_but_forced_before_a_move(pair, monkeypatch):
    calls = logins(monkeypatch, main="alice@example.com", second="bob@example.com")
    accounts.refresh_emails(now=NOW)
    n = len(calls)
    assert accounts.refresh_emails(now=NOW + 300) == [] and len(calls) == n          # inside 10 minutes
    accounts.refresh_emails(force=True, now=NOW + 10) and None
    assert len(calls) == n                                                           # forced, but not within 30 s
    accounts.refresh_emails(force=True, now=NOW + 100)
    assert len(calls) == n + 2                                                       # forced skips the 10 minutes
    accounts.refresh_emails(now=NOW + 100 + 601)
    assert len(calls) == n + 4                                                       # due again after 10 minutes


def test_a_different_address_in_claude_json_brings_the_next_read_forward(pair, monkeypatch):
    calls = logins(monkeypatch, main="alice@example.com", second="bob@example.com")
    accounts.refresh_emails(now=NOW)
    n = len(calls)
    write_private(accounts.claude_json(), json.dumps({"oauthAccount": {"emailAddress": "someone.else@example.com"}}))
    accounts.refresh_emails(now=NOW + 130)
    assert calls[n:] == ["main"]                                                     # only the profile that disagrees


def test_a_hanging_status_does_not_block_and_keeps_the_cache(pair, monkeypatch):
    def slow(name, timeout=30):
        time.sleep(2.5)
        return {"email": "late@example.com"}
    monkeypatch.setattr(accounts, "auth_status", slow)
    start = time.time()
    assert accounts.refresh_emails(now=NOW, timeout=0.2) == []
    assert time.time() - start < 2.4 and accounts.cached_email("main") == "alice@example.com"


def test_the_menu_never_reads_the_login(pair, monkeypatch):
    monkeypatch.setattr(accounts, "auth_status", lambda *a, **k: pytest.fail("the menu must use the cache"))
    I.account_command(cfg(), FakeBot(), {"message_thread_id": 1}, SID, "", Said())


# --- 2. the owner hears about a changed login once ---------------------------------------------------

def test_a_changed_login_is_announced_once_in_general(pair, monkeypatch):
    bot = FakeBot()
    logins(monkeypatch, main="bob@example.com", second="bob@example.com")
    changes, _ = identity.check(cfg(), bot, force=True, now=NOW)
    assert len(changes) == 1
    texts = [x for x in general_texts(bot) if "now signed in as" in x]
    assert len(texts) == 1 and "main" in texts[0] and "bob" in texts[0] and "alice" in texts[0]
    identity.check(cfg(), bot, force=True, now=NOW + 100)
    identity.check(cfg(), bot, force=True, now=NOW + 200)
    assert len([x for x in general_texts(bot) if "now signed in as" in x]) == 1


# --- 3. profiles with one login are one account ----------------------------------------------------

def test_two_profiles_with_one_login_are_one_menu_row_with_both_names(pair, monkeypatch):
    set_emails(main="bob@example.com", second="Bob@example.com")   # case does not matter
    accounts.add("small")
    set_emails(main="bob@example.com", second="Bob@example.com", small="alice@example.com")
    monkeypatch.setattr(accounts, "account_for", lambda session=None: "second" if session else "main")
    said = Said()
    I.account_command(cfg(), FakeBot(), {"message_thread_id": 1}, SID, "", said)
    text, kw = said[0]
    rows = [r[0] for r in kw["reply_markup"]["inline_keyboard"] if "account_btn_add" not in r[0]["text"]]
    labelled = [r for r in rows if "bob" in r["text"] or "alice" in r["text"]]
    assert len(labelled) == 2                                                        # two real accounts, three profiles
    twin = [r for r in labelled if "bob" in r["text"]][0]
    assert "main" in twin["text"] and "second" in twin["text"] and twin["text"].startswith("📍")
    assert twin["callback_data"] == "more:/account"                                  # this session is already there
    assert "same account" in text
    other = [r for r in labelled if "alice" in r["text"]][0]
    assert other["callback_data"] == "more:/account move small"


def test_the_default_account_menu_also_shows_the_twins_once(pair, monkeypatch):
    set_emails(main="same@example.com", second="same@example.com")
    accounts.use("second")
    said = Said()
    I.account_command(cfg(), FakeBot(), {"message_thread_id": 1}, None, "", said)
    rows = [r[0] for r in said[0][1]["reply_markup"]["inline_keyboard"]][:-4]  # auto, add, remove, close
    assert len(rows) == 1 and rows[0]["text"].startswith("⭐") and rows[0]["callback_data"] == "more:/account use second"


def test_twins_share_one_quota(pair):
    set_emails(main="same@example.com", second="same@example.com")
    soon = time.time() + 3600
    write_private(accounts.usage_path("main"), json.dumps(
        {"at": "2026-11-01T10:00:00Z", "five_hour": 100, "five_hour_resets": soon, "weekly": 20, "weekly_resets": soon}))
    write_private(accounts.usage_path("second"), json.dumps(
        {"at": "2026-11-01T10:00:00Z", "five_hour": 5, "five_hour_resets": soon, "weekly": 20, "weekly_resets": soon}))
    assert accounts.spent("second") and accounts.used("second") == 100.0


# --- 4. nothing moves between twins ----------------------------------------------------------------

def test_the_fallback_never_offers_the_twin_of_the_account_that_ran_out(pair, monkeypatch):
    accounts.add("small")
    set_emails(main="bob@example.com", second="bob@example.com", small="alice@example.com")
    accounts.record(SID, "main")
    plan = accounts.fallback_plan(SID, now=NOW)
    assert plan["here"] == "main" and plan["others"] == ["small"] and plan["fresh"] == ["small"]
    accounts.record(SID, "second")
    assert accounts.fallback_plan(SID, now=NOW)["others"] == ["small"]
    assert accounts.pick_account("main", now=NOW) is None                            # nobody else to go to


def test_the_incident_replay_a_stale_cache_no_longer_picks_the_account_that_just_ran_out(pair, monkeypatch):
    accounts.record(SID, "main")
    logins(monkeypatch, main="bob@example.com", second="bob@example.com")
    stale = accounts.fallback_plan(SID, now=NOW)
    assert stale["others"] == ["second"]                                           # what the old cache said
    accounts.refresh_emails(force=True, now=NOW + 1)
    fresh = accounts.fallback_plan(SID, now=NOW + 1)
    assert fresh["others"] == [] and fresh["fresh"] == []


def test_a_move_between_twins_is_refused_before_anything_is_typed(pair, monkeypatch):
    set_emails(main="same@example.com", second="same@example.com")
    accounts.record(SID, "main")
    monkeypatch.setattr(I, "pane_for_session", lambda s: pytest.fail("must not look for a window"))
    monkeypatch.setattr(I, "_tmux", lambda *a: pytest.fail("must not type anything"))
    said = Said()
    assert I.account_move(cfg(), FakeBot(), SID, "second", said) is False
    assert accounts.account_for(SID) == "main" and len(said) == 1 and "same account" in said[0][0]


def test_a_move_re_reads_the_login_first(pair, monkeypatch):
    """The cache says two accounts; the login says one: the move is refused."""
    accounts.record(SID, "main")
    logins(monkeypatch, main="bob@example.com", second="bob@example.com")
    monkeypatch.setattr(I, "pane_for_session", lambda s: pytest.fail("must not look for a window"))
    said = Said()
    assert I.account_move(cfg(), FakeBot(), SID, "second", said) is False
    assert "same account" in said[0][0] and accounts.cached_email("main") == "bob@example.com"


def test_rebalance_style_picks_skip_the_twin(pair):
    accounts.add("small")
    set_emails(main="big@example.com", second="big@example.com", small="small@example.com")
    assert accounts.distinct_others("main") == ["small"]
    assert accounts.distinct_others("small") == ["main"]


# --- 5. the duplicate notice ------------------------------------------------------------------------

def test_a_duplicate_is_announced_once_with_a_hint_and_nothing_is_changed(pair, monkeypatch):
    bot = FakeBot()
    logins(monkeypatch, main="bob@example.com", second="bob@example.com")
    _, announced = identity.check(cfg(), bot, force=True, now=NOW)
    assert announced == [["main", "second"]]
    dup = [x for x in general_texts(bot) if "same account" in x]
    assert len(dup) == 1 and "main + second" in dup[0] and "sign main in as alice again" in dup[0]
    assert "I changed nothing" not in dup[0]                                         # the changed-profile hint is used
    for k in (1, 2, 3):
        identity.check(cfg(), bot, force=True, now=NOW + 100 * k)
    assert len([x for x in general_texts(bot) if "same account" in x]) == 1


def test_a_duplicate_that_was_there_without_a_change_gets_the_generic_hint(pair, monkeypatch):
    bot = FakeBot()
    set_emails(main="same@example.com", second="same@example.com")
    logins(monkeypatch, main="same@example.com", second="same@example.com")
    identity.check(cfg(), bot, force=True, now=NOW)
    dup = [x for x in general_texts(bot) if "same account" in x]
    assert len(dup) == 1 and "remove the extra profile" in dup[0] and "I changed nothing" in dup[0]


def test_a_resolved_duplicate_that_comes_back_is_announced_again(pair, monkeypatch):
    bot = FakeBot()
    set_emails(main="same@example.com", second="same@example.com")
    logins(monkeypatch, main="same@example.com", second="same@example.com")
    identity.check(cfg(), bot, force=True, now=NOW)
    logins(monkeypatch, main="own@example.com", second="same@example.com")
    identity.check(cfg(), bot, force=True, now=NOW + 100)
    logins(monkeypatch, main="same@example.com", second="same@example.com")
    identity.check(cfg(), bot, force=True, now=NOW + 200)
    assert len([x for x in general_texts(bot) if "same account" in x]) == 2


def test_a_failed_duplicate_post_is_retried_not_lost(pair, monkeypatch):
    bot = FakeBot()
    set_emails(main="same@example.com", second="same@example.com")
    logins(monkeypatch, main="same@example.com", second="same@example.com")
    bot.fail["sendMessage"] = "boom"
    identity.check(cfg(), bot, force=True, now=NOW)
    identity.check(cfg(), bot, force=True, now=NOW + 100)
    identity.check(cfg(), bot, force=True, now=NOW + 200)
    assert len([x for x in general_texts(bot) if "same account" in x]) == 2         # the failed try, then the one that arrived


def test_a_failed_change_post_stays_pending_and_arrives_once_later(pair, monkeypatch):
    bot = FakeBot()
    logins(monkeypatch, main="carol@example.com", second="bob@example.com")
    bot.fail["sendMessage"] = "boom"
    identity.check(cfg(), bot, force=True, now=NOW)                                  # the cache now has the new address...
    assert accounts.cached_email("main") == "carol@example.com"
    assert not [x for x in general_texts(bot) if "now signed in as" in x][1:]        # ...but the notice did not arrive
    identity.check(cfg(), bot, now=NOW + 5)                                          # throttled read, pending notice retried
    identity.check(cfg(), bot, now=NOW + 10)
    delivered = [x for x in general_texts(bot) if "now signed in as" in x]
    assert len(delivered) == 2 and bot.calls[0][0] == "sendMessage"                  # one failed try + the one that arrived


def test_the_notice_bookkeeping_is_not_locked_during_the_telegram_call(pair, monkeypatch):
    import fcntl
    logins(monkeypatch, main="carol@example.com", second="bob@example.com")
    held = []

    class Bot(FakeBot):
        def call(self, method, params=None, timeout=15):
            fh = open(str(state_dir() / "account-notices.json") + ".lock", "a")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except OSError:
                held.append(True)
            finally:
                fh.close()
            return super().call(method, params, timeout)
    identity.check(cfg(), Bot(), force=True, now=NOW)
    assert held and not any(held)


def test_the_notice_is_in_hebrew_for_a_hebrew_owner(pair, monkeypatch):
    bot = FakeBot()
    set_emails(main="same@example.com", second="same@example.com")
    logins(monkeypatch, main="same@example.com", second="same@example.com")
    he = Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language="he")
    identity.check(he, bot, force=True, now=NOW)
    assert "אותו חשבון" in general_texts(bot)[0]


# --- 6. it runs from the periodic jobs --------------------------------------------------------------

def test_the_dashboard_job_refreshes_logins_and_posts_the_notice(pair, monkeypatch):
    from ctb import dashboard, recover
    bot = FakeBot()
    monkeypatch.setattr(recover, "limit_check", lambda *a, **k: None)
    logins(monkeypatch, main="bob@example.com", second="bob@example.com")
    dashboard.run(cfg(), bot=bot, now=NOW)
    assert any("now signed in as" in x for x in general_texts(bot))
    assert accounts.cached_email("main") == "bob@example.com"
    before = len(general_texts(bot))
    dashboard.run(cfg(), bot=bot, now=NOW + 60)                                      # inside 10 minutes: no new read, no repeat
    assert not any("now signed in as" in x for x in general_texts(bot)[before:])


def test_the_limit_offer_re_reads_logins_before_it_plans(pair, monkeypatch):
    from ctb import mirror
    seen = []
    monkeypatch.setattr(identity, "check", lambda c, b, force=False, now=None: seen.append(force) or ([], []))
    src = open(mirror.__file__, encoding="utf-8").read()
    assert src.index("identity.check(cfg, bot, force=True, timeout=accounts.IDENTITY_QUICK_TIMEOUT)") < src.index("plan = accounts.fallback_plan(session)")


# --- 7. review of PR #52 -----------------------------------------------------------------------------

def test_a_signed_out_profile_is_marked_not_kept_as_its_old_address(pair, monkeypatch):
    logins(monkeypatch, main="alice@example.com", second="OUT")
    accounts.refresh_emails(now=NOW)
    assert accounts.logged_out("second") and not accounts.logged_out("main")
    assert accounts.twins("main") == [] and accounts.twins("second") == []
    logins(monkeypatch, main="alice@example.com", second="bob@example.com")
    accounts.refresh_emails(force=True, now=NOW + 100)
    assert not accounts.logged_out("second")                                         # signing in again clears the mark


def test_a_signed_out_twin_is_never_a_move_target_the_signed_in_twin_is(pair, monkeypatch):
    accounts.add("third")
    set_emails(main="alice@example.com", second="bob@example.com", third="bob@example.com")
    logins(monkeypatch, main="alice@example.com", second="OUT", third="bob@example.com")
    accounts.refresh_emails(now=NOW)
    assert accounts.distinct_others("main") == ["third"]
    assert "second" not in accounts.fallback_plan(SID, now=NOW)["others"]
    accounts.record(SID, "main")
    assert accounts.pick_account("main", now=NOW) is None or accounts.pick_account("main", now=NOW) == "third"


def test_a_fully_signed_out_group_is_not_offered_and_its_menu_row_does_nothing(pair, monkeypatch):
    logins(monkeypatch, main="alice@example.com", second="OUT")
    accounts.refresh_emails(now=NOW)
    assert accounts.distinct_others("main") == []
    monkeypatch.setattr(accounts, "account_for", lambda session=None: "main")
    said = Said()
    I.account_command(cfg(), FakeBot(), {"message_thread_id": 1}, SID, "", said)
    row = [r[0] for r in said[0][1]["reply_markup"]["inline_keyboard"] if "second" in r[0]["text"]][0]
    assert "signed out" in row["text"] and row["callback_data"] == "more:/account"
    accounts.record(SID, "main")
    msgs = Said()
    monkeypatch.setattr(I, "pane_for_session", lambda s: pytest.fail("must not look for a window"))
    assert I.account_move(cfg(), FakeBot(), SID, "second", msgs) is False
    assert "not signed in" in msgs[0][0]


def test_the_menu_button_of_a_twin_group_moves_to_its_signed_in_profile(pair, monkeypatch):
    accounts.add("third")
    set_emails(main="alice@example.com", second="bob@example.com", third="bob@example.com")
    monkeypatch.setattr(accounts, "account_for", lambda session=None: "main")
    said = Said()
    I.account_command(cfg(), FakeBot(), {"message_thread_id": 1}, SID, "", said)
    row = [r[0] for r in said[0][1]["reply_markup"]["inline_keyboard"] if "second" in r[0]["text"]][0]
    assert row["callback_data"] == "more:/account move second"                       # both signed in: the first
    logins(monkeypatch, main="alice@example.com", second="OUT", third="bob@example.com")
    accounts.refresh_emails(now=NOW)
    said = Said()
    I.account_command(cfg(), FakeBot(), {"message_thread_id": 1}, SID, "", said)
    labels = [r[0] for r in said[0][1]["reply_markup"]["inline_keyboard"]]
    assert any(r["callback_data"] == "more:/account move third" for r in labels)
    assert not any("move second" in r["callback_data"] for r in labels)


def test_the_incident_replay_with_the_status_unreadable(pair, monkeypatch):
    """main was re-signed as bob, but `claude auth status` cannot be read: .claude.json still names the truth."""
    accounts.record(SID, "main")
    logins(monkeypatch, main="", second="")                                          # nothing readable
    write_private(accounts.claude_json(), json.dumps({"oauthAccount": {"emailAddress": "bob@example.com"}}))
    assert accounts.fallback_plan(SID, now=NOW)["others"] == ["second"]              # before any read: the stale cache
    accounts.refresh_emails(force=True, now=NOW)
    assert accounts.login_state("main") == "unreadable"
    assert accounts.cached_email("main") == "alice@example.com"                      # the cache is kept...
    assert accounts.fallback_plan(SID, now=NOW)["others"] == []                      # ...but second is a possible twin
    assert accounts.same_login("main", "second")
    said = Said()
    monkeypatch.setattr(I, "pane_for_session", lambda s: pytest.fail("must not look for a window"))
    assert I.account_move(cfg(), FakeBot(), SID, "second", said) is False
    assert "same account" in said[0][0]


def test_an_unreadable_status_does_not_invent_twins_when_json_agrees_with_the_cache(pair, monkeypatch):
    logins(monkeypatch, main="", second="")
    write_private(accounts.claude_json(), json.dumps({"oauthAccount": {"emailAddress": "alice@example.com"}}))
    accounts.refresh_emails(force=True, now=NOW)
    assert accounts.twins("main") == []


def test_sign_in_from_telegram_goes_through_the_identity_check_and_shows_only_the_local_part(pair, monkeypatch):
    bot = FakeBot()
    accounts.add("fourth")
    write_private(state_dir() / "account-login.json", json.dumps({"name": "fourth", "tmux": "acct-login-fourth", "at": time.time()}))
    set_emails(main="alice@example.com", second="bob@example.com")
    monkeypatch.setattr(accounts, "login_pending", lambda: {"name": "fourth", "tmux": "acct-login-fourth"})
    monkeypatch.setattr(accounts, "login_done", lambda: None)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "_tmux", lambda *a: SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(I, "_terminal_background", lambda key, fn: fn() or True)
    logins(monkeypatch, main="alice@example.com", second="bob@example.com", fourth="bob@example.com")
    monkeypatch.setattr(accounts, "auth_status", lambda name, timeout=30: {"loggedIn": True, "email": "bob@example.com"})
    I.account_code(cfg(), bot, {"message_id": 1, "message_thread_id": 5, "chat": {"id": 7}}, "A" * 20)
    all_text = " ".join(str(p.get("text", "")) for p in bot.methods("sendMessage"))
    assert "bob@example.com" not in all_text and "bob" in all_text                   # local part only
    assert accounts.cached_email("fourth") == "bob@example.com"                      # filled by the identity check
    assert [x for x in general_texts(bot) if "same account" in x]                    # and the duplicate was announced


def test_the_waited_on_paths_use_the_short_timeout(pair, monkeypatch):
    seen = []
    monkeypatch.setattr(accounts, "auth_status", lambda name, timeout=30: seen.append(timeout) or {})
    accounts.record(SID, "main")
    monkeypatch.setattr(I, "pane_for_session", lambda s: None)
    I.account_move(cfg(), FakeBot(), SID, "second", Said())
    assert seen and set(seen) == {accounts.IDENTITY_QUICK_TIMEOUT} and accounts.IDENTITY_QUICK_TIMEOUT <= 5


def test_hebrew_move_and_menu_texts_isolate_the_profile_names(pair, monkeypatch):
    set_emails(main="same@example.com", second="same@example.com")
    he = Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language="he")
    accounts.record(SID, "main")
    said = Said()
    I.account_move(he, FakeBot(), SID, "second", said)
    assert "\u2068main\u2069" in said[0][0] and "\u2068second\u2069" in said[0][0]
    monkeypatch.setattr(accounts, "account_for", lambda session=None: "main")
    menu = Said()
    I.account_command(he, FakeBot(), {"message_thread_id": 1}, SID, "", menu)
    assert "\u2068main\u2069" in menu[0][0] and "\u2068second\u2069" in menu[0][0]
