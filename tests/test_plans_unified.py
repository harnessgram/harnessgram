"""2026-11-02 batch: #89 (identity from the OAuth token) and #87 (stale twin login) both wrote
account-plans.json, one as {email, plan, tier} records and one as plain strings. One record form now serves
both; a legacy string still reads; the token's plan is never replaced by a status answer; a failed read never
overwrites a known state; a login that names no address is nobody's twin. No network, Keychain or claude."""
from __future__ import annotations

import json

from conftest import GOOD_TOKEN, FakeBot
from ctb import accounts, identity
from ctb.config import Config, state_dir, write_private

NOW = 2_000_000.0
TWIN = "twin@example.com"


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language="en")


def plans_file():
    return json.loads((state_dir() / "account-plans.json").read_text())


def set_json(name, data):
    write_private(state_dir() / name, json.dumps(data))


def claim(name, mail):
    path = accounts.claude_json() if name == accounts.MAIN else accounts.profiles_dir() / name / ".claude.json"
    write_private(path, json.dumps({"oauthAccount": {"emailAddress": mail}}))


def test_a_legacy_plain_string_plan_reads_as_a_status_record():
    accounts.add("account2")
    set_json("account-emails.json", {"main": TWIN, "account2": TWIN})
    set_json("account-plans.json", {"main": "pro", "account2": "max", "solo3": 7})
    assert accounts.cached_plan("main") == "claude_pro" and accounts.cached_plan("account2") == "claude_max"
    assert accounts.cached_plan("solo3") == ""                                    # junk reads as nothing
    assert accounts.known_plan("main") is None                                       # a status plan is no token proof
    assert accounts.better_twins("main") == ["account2"]


def test_a_legacy_token_record_without_source_still_counts_as_the_token():
    set_json("account-emails.json", {"main": TWIN})
    set_json("account-plans.json", {"main": {"email": TWIN, "plan": "claude_max", "tier": "default_claude_max_20x"}})
    assert accounts.known_plan("main")["source"] == "token" and accounts.is_big_plan("main") is True


def test_plan_names_have_one_form_and_the_tier_is_kept(monkeypatch):
    accounts.add("account2")
    tokens = {"main": {"email": TWIN, "plan": "claude_max", "tier": "default_claude_max_5x", "fp": "a" * 64}}
    monkeypatch.setattr(accounts, "real_profile", lambda n, timeout=0: tokens.get(n))
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: {"loggedIn": True, "email": "b@example.com",
                                                                        "subscriptionType": "Max"})
    accounts.refresh_emails(force=True, now=NOW)
    stored = plans_file()
    assert stored["main"] == {"email": TWIN, "plan": "claude_max", "tier": "default_claude_max_5x", "source": "token"}
    assert stored["account2"] == {"email": "b@example.com", "plan": "claude_max", "tier": "", "source": "status"}
    assert accounts.is_big_plan("main") is False                                     # Max 5x is not the big plan


def test_a_status_answer_never_replaces_the_token_plan_of_the_same_address(monkeypatch):
    tokens = {"main": {"email": TWIN, "plan": "claude_max", "tier": "default_claude_max_20x", "fp": "a" * 64}}
    monkeypatch.setattr(accounts, "real_profile", lambda n, timeout=0: tokens.get(n))
    accounts.refresh_emails(force=True, now=NOW)
    tokens.clear()                                                                   # the token can't answer now
    claim("main", "other@example.com")                                               # so the status gets to look
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: {"loggedIn": True, "email": TWIN,
                                                                        "subscriptionType": "pro"})
    accounts.refresh_emails(force=True, now=NOW + 1000)
    assert accounts.cached_plan("main") == "claude_max" and accounts.known_plan("main")["source"] == "token"
    assert accounts.is_big_plan("main") is True


def test_a_plan_of_the_address_a_profile_used_to_be_does_not_count():
    set_json("account-emails.json", {"main": "new@example.com"})
    set_json("account-plans.json", {"main": {"email": TWIN, "plan": "claude_pro", "tier": "", "source": "status"}})
    assert accounts.cached_plan("main") == "" and accounts.plan_rank("main") is None


def test_a_login_that_answers_without_loggedin_true_or_false_and_no_email_is_unreadable(monkeypatch):
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: {"subscriptionType": "pro"})
    accounts.refresh_emails(force=True, now=NOW)
    assert accounts.login_state("main") == "unreadable" and accounts.cached_plan("main") == ""


def test_a_signed_out_answer_with_an_old_address_is_still_signed_out(monkeypatch):
    set_json("account-emails.json", {"main": TWIN})
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: {"loggedIn": False, "email": TWIN})
    accounts.refresh_emails(force=True, now=NOW)
    assert accounts.logged_out("main")


# --- the live layout on 2026-11-02 ----------------------------------------------------------------------------
# main: logged in, names no address, plan pro. account2 + account6: one address, max (real twins).
# solo3, solo4, solo5: three different pro accounts.

LIVE = {
    "main": {"loggedIn": True, "subscriptionType": "pro"},
    "account2": {"loggedIn": True, "email": TWIN, "subscriptionType": "max"},
    "account6": {"loggedIn": True, "email": TWIN, "subscriptionType": "max"},
    "solo3": {"loggedIn": True, "email": "three@example.com", "subscriptionType": "pro"},
    "solo4": {"loggedIn": True, "email": "four@example.com", "subscriptionType": "pro"},
    "solo5": {"loggedIn": True, "email": "five@example.com", "subscriptionType": "pro"},
}


def live(monkeypatch, reads=None):
    for n in ("account2", "solo3", "solo4", "solo5", "account6"):
        accounts.add(n)
    set_json("account-emails.json", {"main": TWIN})            # main WAS cached as the twins' address
    claim("main", TWIN)                                         # and its .claude.json still names it
    calls = []

    def status(n, timeout=30):
        calls.append(n)
        return (reads or LIVE)[n]
    monkeypatch.setattr(accounts, "auth_status", status)
    accounts.refresh_emails(force=True, now=NOW)
    return calls


def test_live_layout_groups_and_plans(monkeypatch):
    live(monkeypatch)
    assert accounts.login_state("main") == "noemail" and accounts.cached_email("main") == ""
    assert accounts.cached_plan("main") == "claude_pro"
    groups = accounts.real_accounts()
    assert ["account2", "account6"] in groups and ["main"] in groups
    assert ["solo3"] in groups and ["solo4"] in groups and ["solo5"] in groups
    assert accounts.twins("main") == [] and accounts.better_twins("main") == []
    assert accounts.duplicate_groups() == [["account2", "account6"]]
    assert accounts.stale_twins(["account2", "account6"]) == []                     # same plan: no stale one
    assert accounts.quota_twins("account2") == ["account6"]                         # one shared quota
    assert accounts.distinct_others("solo3") == ["main", "account2", "solo4", "solo5"]
    assert accounts.distinct_others("account2") == ["main", "solo3", "solo4", "solo5"]


def test_live_layout_a_failed_read_keeps_the_known_states(monkeypatch):
    live(monkeypatch)
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: {})         # every read times out
    accounts.refresh_emails(force=True, now=NOW + 1000)
    assert accounts.login_state("main") == "noemail"                                # not "unreadable"
    assert accounts.twins("main") == []                                             # its .claude.json hint pairs nothing
    assert accounts.duplicate_groups() == [["account2", "account6"]]
    set_json("account-status.json", {"solo3": "out"})
    accounts.refresh_emails(force=True, now=NOW + 2000)
    assert accounts.logged_out("solo3")                                          # signed out stays signed out


def test_live_layout_notices_never_pair_main_or_advise_signing_in_again(monkeypatch):
    live(monkeypatch)
    bot = FakeBot()
    for k in range(3):
        identity.check(cfg(), bot, force=True, now=NOW + 1000 * (k + 1))
    texts = [p["text"] for p in bot.methods("sendMessage")]
    dup = [x for x in texts if "same account" in x]
    assert len(dup) == 1 and "account2 + account6" in dup[0].replace("⁨", "").replace("⁩", "")
    assert not [x for x in texts if "main" in x]                                     # no pairing, no "sign in again as"
    assert not [x for x in texts if "older login" in x]


def test_live_layout_mirrored_replies_do_not_run_auth_status_for_main(monkeypatch):
    calls = live(monkeypatch)
    calls.clear()
    for _ in range(5):
        assert accounts.email("main") == ""                                          # mirror.py asks on every reply
    assert calls == []
    assert accounts.email("account2") == TWIN and calls == []


def test_live_layout_main_names_an_address_again(monkeypatch):
    live(monkeypatch)
    reads = dict(LIVE, main={"loggedIn": True, "email": "main@example.com", "subscriptionType": "pro"})
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: reads[n])
    accounts.refresh_emails(force=True, now=NOW + 1000)
    assert accounts.login_state("main") == "" and accounts.cached_email("main") == "main@example.com"
    assert accounts.twins("main") == []


# --- review fix: a status with no address never undoes what the token confirmed ---------------------------------

PRO = "pro-account@example.com"


def test_a_token_hiccup_plus_an_addressless_status_keeps_main_and_its_twin_paired(monkeypatch):
    """main's token says it is the same Pro account as solo3. When main's token cannot answer (expired while
    idle, 429, offline) and its .claude.json names something else, `claude auth status` looks and answers with
    no address: main must stay solo3's twin, with one duplicate notice over the cycles and no move between them."""
    from ctb import inbox as I
    accounts.add("solo3")
    record = {"email": PRO, "plan": "claude_pro", "tier": "", "fp": "f" * 64}
    token_ok = {"main": True, "solo3": True}
    monkeypatch.setattr(accounts, "real_profile", lambda n, timeout=0: dict(record) if token_ok.get(n) else None)
    statuses = []
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: statuses.append(n) or
                        {"loggedIn": True, "subscriptionType": "pro"})
    claim("main", "someone-else@example.com")
    bot = FakeBot()
    identity.check(cfg(), bot, force=True, now=NOW)
    assert accounts.twins("main") == ["solo3"]
    for k in range(1, 4):                                                           # three cycles, main's token down
        token_ok["main"] = k % 2 == 0                                                # and one good read in between
        identity.check(cfg(), bot, force=True, now=NOW + 1000 * k)
        assert accounts.login_state("main") == "" and accounts.cached_email("main") == PRO
        assert accounts.twins("main") == ["solo3"] and accounts.known_plan("main")["source"] == "token"
    assert "main" in statuses                                                        # the status did look
    dup = [p["text"] for p in bot.methods("sendMessage") if "same account" in p["text"]]
    assert len(dup) == 1
    assert accounts.distinct_others("main") == [] and accounts.same_login("main", "solo3")
    accounts.record("sess-pair", "main")
    said = []
    assert I.account_move(cfg(), FakeBot(), "sess-pair", "solo3", lambda text, **kw: said.append(text)) is False
    assert said and "same account" in said[0]


def test_without_a_token_confirmed_address_an_addressless_status_is_still_noemail(monkeypatch):
    set_json("account-emails.json", {"main": PRO})
    set_json("account-plans.json", {"main": {"email": PRO, "plan": "claude_pro", "tier": "", "source": "status"}})
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: {"loggedIn": True, "subscriptionType": "pro"})
    accounts.refresh_emails(force=True, now=NOW)
    assert accounts.login_state("main") == "noemail" and accounts.cached_email("main") == ""


# --- the sign-in fingerprint follows the account id, not the rotating tokens ---------------------------------

def _token_world(monkeypatch, state):
    def keychain(service, timeout=5.0):
        if service == accounts.keychain_service("main"):
            return json.dumps({"claudeAiOauth": {"accessToken": "T" + str(state["access"]), "refreshToken": "R" + str(state["access"]),
                                                 "expiresAt": (NOW + 10 ** 9) * 1000}})
        return None

    def http(path, token, timeout=8.0):
        return 200, {"account": {"email": state["email"], "uuid": state["uuid"]},
                     "organization": {"organization_type": "claude_pro"}}, 0.0
    monkeypatch.setattr(accounts, "_keychain_read", keychain)
    monkeypatch.setattr(accounts, "_http_json", http)


def test_a_token_refresh_plus_a_label_correction_keeps_usage(monkeypatch):
    state = {"access": 1, "email": PRO, "uuid": "acct-1"}
    _token_world(monkeypatch, state)
    accounts.refresh_emails(force=True, now=NOW)
    set_json("account-emails.json", {"main": "stale-label@example.com"})
    set_json("claude-usage-main.json", {"five_hour": 40, "weekly": 20})
    state["access"] = 2                                                              # Claude Code refreshed the tokens
    changes = accounts.refresh_emails(force=True, now=NOW + 1000)
    assert ("main", "stale-label@example.com", PRO) in changes
    assert accounts.usage_path("main").exists()                                      # same account: nothing wiped
    state.update(access=3, email="other@example.com", uuid="acct-2")                 # a real sign-in as another account
    accounts.refresh_emails(force=True, now=NOW + 2000)
    assert not accounts.usage_path("main").exists()
    assert "T3" not in (state_dir() / "account-fingerprints.json").read_text()
