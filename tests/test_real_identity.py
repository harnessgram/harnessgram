"""2026-11-02: the bridge named Claude accounts by a stale record (`claude auth status`, .claude.json)
while the profile's OAuth token belonged to another account. The token is now the source of truth;
idle accounts get their quota from the usage endpoint; an account can be removed from the Telegram menu.
No network, no Keychain and no real claude in these tests."""
from __future__ import annotations

import hashlib
import json
import time

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import accounts, identity, inbox as I
from ctb.config import Config, state_dir, write_private

NOW = 2_000_000.0
SECRET = "fake-token-for-tests-0123456789"


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language="en")


class Said(list):
    def __call__(self, text, **kw):
        self.append((text, kw))


def set_json(name, data):
    write_private(state_dir() / name, json.dumps(data))


def get_json(name):
    try:
        return json.loads((state_dir() / name).read_text(encoding="utf-8"))
    except OSError:
        return {}


class FakeWorld:
    """Keychain and OAuth API stand-ins. tokens: profile -> token; *_api: token -> (status, body[, retry])."""

    def __init__(self, monkeypatch):
        self.tokens, self.profile_api, self.usage_api = {}, {}, {}
        self.calls = []
        self.expires = {}  # profile -> expiresAt in ms (default: an hour from now); None leaves it out
        monkeypatch.setattr(accounts, "_keychain_read", self.keychain)
        monkeypatch.setattr(accounts, "_http_json", self.http)

    def keychain(self, service, timeout=5.0):
        for name, token in self.tokens.items():
            if accounts.keychain_service(name) == service:
                oauth = {"accessToken": token}
                exp = self.expires.get(name, (time.time() + 3600) * 1000)
                if exp is not None:
                    oauth["expiresAt"] = exp
                return json.dumps({"claudeAiOauth": oauth})
        return None

    def http(self, path, token, timeout=8.0):
        self.calls.append((path, token))
        table = self.profile_api if path == "/api/oauth/profile" else self.usage_api
        status, body, *rest = table.get(token, (0, {}, 0.0))
        return status, body, (rest[0] if rest else 0.0)


@pytest.fixture
def world(monkeypatch):
    return FakeWorld(monkeypatch)


def me(email, kind="claude_pro", tier=""):
    return (200, {"account": {"email": email}, "organization": {"organization_type": kind, "rate_limit_tier": tier}})


@pytest.fixture
def trio(world):
    """The incident: main's records say alice, but its token is bob (Pro); account6 is really
    alice (Max); account2's token is rejected."""
    for n in ("account2", "account6"):
        accounts.add(n)
    write_private(accounts.claude_json(), json.dumps({"oauthAccount": {"emailAddress": "alice@example.net"}}))
    set_json("account-emails.json", {"main": "alice@example.net", "account2": "alice@example.net",
                                     "account6": "alice@example.net"})
    world.tokens = {"main": "T-main", "account2": "T-two", "account6": "T-six"}
    world.profile_api = {"T-main": me("bob@example.net"), "T-two": (401, {}),
                         "T-six": me("alice@example.net", "claude_max", "default_claude_max_20x")}
    return world


# --- Bug 1: the token is the truth ---------------------------------------------------------------------

def test_keychain_service_names(world):
    accounts.add("account6")
    assert accounts.keychain_service("main") == "Claude Code-credentials"
    folder = str(accounts.config_dir("account6"))
    assert accounts.keychain_service("account6") == "Claude Code-credentials-" + hashlib.sha256(folder.encode()).hexdigest()[:8]
    assert accounts.keychain_service("Bad Name") is None
    assert accounts.keychain_service("account9") == "Claude Code-credentials-" + hashlib.sha256(
        str(accounts.profiles_dir() / "account9").encode()).hexdigest()[:8]    # also for a folder that is gone


def test_access_token_comes_from_the_keychain_json(world):
    world.tokens = {"main": SECRET}
    assert accounts.access_token("main") == SECRET
    assert accounts.access_token("account9") is None


def test_the_real_identity_replaces_a_stale_record_and_regroups_the_twins(trio, monkeypatch):
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: {"email": "alice@example.net", "loggedIn": True})
    assert [g for g in accounts.real_accounts() if len(g) > 1] == [["main", "account2", "account6"]]
    changes = accounts.refresh_emails(now=NOW)
    assert changes == [("main", "alice@example.net", "bob@example.net")]
    assert accounts.cached_email("main") == "bob@example.net"
    assert accounts.cached_email("account6") == "alice@example.net"
    assert accounts.logged_out("account2") and accounts.login_addresses("account2") == set()  # 401: signed out
    assert accounts.real_accounts() == [["main"], ["account2"], ["account6"]]                  # nobody is a twin now


def test_the_roomy_account_is_no_longer_hidden_behind_a_full_twin(trio):
    now = time.time()
    set_json("claude-usage-main.json", {"at": "2026-11-02T10:00:00Z", "five_hour": 10, "five_hour_resets": now + 3600,
                                        "weekly": 100, "weekly_resets": now + 86400})
    set_json("claude-usage-account6.json", {"at": "2026-11-02T10:00:00Z", "five_hour": 5, "five_hour_resets": now + 3600,
                                            "weekly": 31, "weekly_resets": now + 86400})
    assert accounts.used("account6", now) == 100                      # before: shares main's 100%
    # main's stale usage file is dropped on the identity change, so put a (different) reading back after it
    accounts.refresh_emails(now=NOW)
    set_json("claude-usage-main.json", {"at": "2026-11-02T10:00:00Z", "five_hour": 10, "five_hour_resets": now + 3600,
                                        "weekly": 100, "weekly_resets": now + 86400})
    assert accounts.used("account6", now) == 31 and accounts.used("main", now) == 100


def test_plan_comes_from_the_organization_type(trio):
    accounts.refresh_emails(now=NOW)
    assert accounts.is_big_plan("account6") is True        # claude_max
    assert accounts.is_big_plan("main") is False           # claude_pro, whatever .claude.json says
    trio.profile_api["T-six"] = me("alice@example.net", "claude_max", "default_claude_max_5x")
    accounts.refresh_emails(force=True, now=NOW + 1000)
    assert accounts.is_big_plan("account6") is False       # Max 5x is not the big plan


def test_a_changed_identity_drops_that_profiles_stale_usage_and_limit(trio):
    set_json("claude-usage-main.json", {"five_hour": 90, "weekly": 100})
    set_json("claude-usage-account6.json", {"five_hour": 5, "weekly": 31})
    set_json("account-limits.json", {"main": 123.0, "account6": 456.0})
    set_json("account-fingerprints.json", {"main": "0" * 64})          # main was a different sign-in before
    accounts.refresh_emails(now=NOW)
    assert not accounts.usage_path("main").exists()                     # main changed account
    assert accounts.usage_path("account6").exists()                     # account6 did not
    assert get_json("account-limits.json") == {"account6": 456.0}


def test_a_changed_identity_is_announced_once(trio):
    bot = FakeBot()
    identity.check(cfg(), bot, force=True, now=NOW)
    identity.check(cfg(), bot, force=True, now=NOW + 100)
    texts = [p["text"] for p in bot.methods("sendMessage") if "now signed in as" in p["text"]]
    assert len(texts) == 1 and "bob" in texts[0]


@pytest.mark.parametrize("answer", [(429, {}, 60.0), (500, {}), (0, {})])
def test_rate_limited_or_offline_keeps_the_previous_answer_and_uses_auth_status(trio, monkeypatch, answer):
    trio.profile_api["T-main"] = answer
    set_json("account-emails.json", {"main": "old@example.com"})
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: {"email": "fallback@example.com"} if n == "main" else {})
    accounts.refresh_emails(now=NOW, only=["main"])
    assert accounts.cached_email("main") == "fallback@example.com" and not accounts.logged_out("main")
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: {})        # nothing readable at all
    accounts.refresh_emails(force=True, now=NOW + 100, only=["main"])
    assert accounts.cached_email("main") == "fallback@example.com" and accounts.login_state("main") == "unreadable"


def test_no_keychain_item_falls_back_to_auth_status(world, monkeypatch):
    accounts.add("second")
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: {"email": "cli@example.com"})
    accounts.refresh_emails(now=NOW)
    assert accounts.cached_email("second") == "cli@example.com" and world.calls == []


def test_a_stale_claude_json_does_not_force_a_read_every_two_minutes(trio):
    accounts.refresh_emails(now=NOW)                                   # main's .claude.json still says alice
    n = len(trio.calls)
    accounts.refresh_emails(now=NOW + 200)                             # inside 10 minutes: no profile read
    assert len(trio.calls) == n
    accounts.refresh_emails(now=NOW + 601)
    assert len(trio.calls) > n


def test_the_token_never_reaches_state_or_notices(trio):
    bot = FakeBot()
    trio.tokens["main"] = SECRET
    trio.profile_api[SECRET] = me("bob@example.net")
    identity.check(cfg(), bot, force=True, now=NOW)
    blob = "".join(p.read_text(errors="ignore") for p in state_dir().glob("*") if p.is_file()) + json.dumps(bot.calls)
    assert SECRET not in blob


def test_the_token_goes_only_in_the_header(monkeypatch):
    seen = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"account": {"email": "a@b.c"}}'

    def fake_open(req, timeout=0):
        seen["url"], seen["headers"] = req.full_url, dict(req.header_items())
        return Resp()
    import urllib.request
    monkeypatch.undo()                                                  # the real _http_json, a fake socket
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    status, body, _ = accounts._http_json("/api/oauth/profile", SECRET)
    assert status == 200 and body["account"]["email"] == "a@b.c"
    assert SECRET not in seen["url"] and seen["headers"]["Authorization"] == "Bearer " + SECRET
    assert seen["headers"]["Anthropic-beta"] == "oauth-2025-04-20"


@pytest.mark.parametrize("expires", [(time.time() - 60) * 1000, None])
def test_a_401_with_an_expired_or_undated_token_is_not_signed_out(trio, monkeypatch, expires):
    trio.expires["account2"] = expires
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: {"email": "cli2@example.com", "loggedIn": True}
                        if n == "account2" else {})
    accounts.refresh_emails(now=NOW, only=["account2"])
    assert not accounts.logged_out("account2") and accounts.login_state("account2") == ""
    assert accounts.cached_email("account2") == "cli2@example.com"      # the auth status path answered
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: {})
    accounts.refresh_emails(force=True, now=NOW + 100, only=["account2"])
    assert not accounts.logged_out("account2") and accounts.cached_email("account2") == "cli2@example.com"


def test_a_401_with_a_still_valid_token_is_signed_out(trio, monkeypatch):
    trio.expires["account2"] = (time.time() + 7200) * 1000
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: {"email": "stale@example.com", "loggedIn": True})
    accounts.refresh_emails(now=NOW, only=["account2"])
    assert accounts.logged_out("account2")


def test_the_usage_fetch_is_skipped_for_an_expired_token(trio):
    trio.usage_api["T-six"] = usage_body()
    trio.expires["account6"] = (time.time() - 60) * 1000
    assert accounts.refresh_usage(only=["account6"]) == [] and trio.calls == []
    trio.expires["account6"] = None                                      # undated: also unusable
    assert accounts.refresh_usage(only=["account6"]) == [] and trio.calls == []
    trio.expires["account6"] = (time.time() + 3600) * 1000
    assert accounts.refresh_usage(only=["account6"]) == ["account6"]


# --- Bug 2: idle accounts get a quota reading -----------------------------------------------------------------

def usage_body(five=12.5, five_reset="2026-11-02T18:00:00.123456+00:00", week=31.0, week_reset="2026-11-07T06:00:00Z"):
    return (200, {"five_hour": {"utilization": five, "resets_at": five_reset},
                  "seven_day": {"utilization": week, "resets_at": week_reset}})


def test_iso_epoch_handles_the_server_shapes():
    assert accounts.iso_epoch("2026-11-02T18:00:00Z") == accounts.iso_epoch("2026-11-02T18:00:00.123456+00:00")
    assert accounts.iso_epoch("2026-11-02T20:00:00+02:00") == accounts.iso_epoch("2026-11-02T18:00:00Z")
    assert accounts.iso_epoch(None) == 0.0 and accounts.iso_epoch("garbage") == 0.0


def test_an_idle_account_gets_the_same_record_a_status_line_writes(trio):
    trio.usage_api["T-six"] = usage_body()
    assert accounts.refresh_usage(now=NOW, only=["account6"]) == ["account6"]
    rec = get_json("claude-usage-account6.json")
    assert rec["source"] == "oauth" and rec["five_hour"] in (12, 13) and rec["weekly"] == 31
    assert rec["five_hour_resets"] == accounts.iso_epoch("2026-11-02T18:00:00Z")
    assert rec["weekly_resets"] == accounts.iso_epoch("2026-11-07T06:00:00Z")
    assert {"at", "five_hour", "weekly", "five_hour_resets", "weekly_resets", "extra"} <= set(rec)
    assert accounts.usage_path("account6") == state_dir() / "claude-usage-account6.json"
    assert accounts._used_one("account6", accounts.iso_epoch("2026-11-02T10:00:00Z")) == 31   # readable by the quota logic


def test_usage_fetches_are_throttled_per_profile(trio):
    trio.usage_api["T-six"] = usage_body()
    accounts.refresh_usage(now=NOW, only=["account6"])
    n = len(trio.calls)
    assert accounts.refresh_usage(now=NOW + 200, only=["account6"]) == [] and len(trio.calls) == n
    accounts.refresh_usage(now=NOW + 301, only=["account6"])
    assert len(trio.calls) == n + 1


def test_usage_backs_off_after_429_and_doubles(trio):
    trio.usage_api["T-six"] = (429, {}, 0.0)
    accounts.refresh_usage(now=NOW, only=["account6"])
    n = len(trio.calls)
    accounts.refresh_usage(now=NOW + 600, only=["account6"])                  # inside the first back-off
    assert len(trio.calls) == n
    later = NOW + accounts.USAGE_BACKOFF + 1
    accounts.refresh_usage(now=later, only=["account6"])
    assert len(trio.calls) == n + 1
    st = get_json("account-usage-fetch.json")["account6"]
    assert st["fails"] == 2 and st["until"] - later == 2 * accounts.USAGE_BACKOFF
    trio.usage_api["T-six"] = usage_body()
    accounts.refresh_usage(now=st["until"] + 1, only=["account6"])
    assert get_json("claude-usage-account6.json")["weekly"] == 31
    assert "until" not in get_json("account-usage-fetch.json")["account6"]


def test_a_fresh_status_line_reading_is_not_fetched_over(trio):
    trio.usage_api["T-six"] = usage_body()
    set_json("claude-usage-account6.json", {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 30)),
                                            "five_hour": 1, "weekly": 2, "source": "statusline"})
    assert accounts.refresh_usage(now=NOW, only=["account6"]) == [] and trio.calls == []
    assert get_json("claude-usage-account6.json")["weekly"] == 2


def test_a_status_line_reading_written_during_the_fetch_wins(trio):
    def racing(path, token, timeout=8.0):
        set_json("claude-usage-account6.json", {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + 1)),
                                                "five_hour": 1, "weekly": 2, "source": "statusline"})
        return usage_body() + (0.0,)
    assert accounts.refresh_usage(now=NOW, only=["account6"], fetch=racing) == []
    assert get_json("claude-usage-account6.json")["weekly"] == 2


def test_an_old_status_line_reading_is_replaced(trio):
    trio.usage_api["T-six"] = usage_body()
    set_json("claude-usage-account6.json", {"at": "2026-10-01T00:00:00Z", "weekly": 2, "source": "statusline"})
    assert accounts.refresh_usage(now=accounts.iso_epoch("2026-11-02T10:00:00Z"), only=["account6"]) == ["account6"]
    assert get_json("claude-usage-account6.json")["source"] == "oauth"


def test_signed_out_profiles_and_missing_tokens_are_skipped(trio):
    accounts.refresh_emails(now=NOW)                                         # account2: 401, so signed out
    trio.calls.clear()
    assert accounts.refresh_usage(now=NOW, only=["account2"]) == [] and trio.calls == []
    del trio.tokens["account6"]
    assert accounts.refresh_usage(now=NOW, only=["account6"]) == []


def test_the_dashboard_job_refreshes_usage(trio, monkeypatch):
    from ctb import dashboard
    seen = []
    monkeypatch.setattr(accounts, "refresh_usage", lambda now=None, **k: seen.append(now) or [])
    monkeypatch.setattr(dashboard, "text", lambda *a, **k: "body")
    monkeypatch.setattr(dashboard, "stuck_check", lambda *a, **k: None)
    import ctb.recover as recover
    import ctb.sessionlog as sessionlog
    import ctb.autopilot as autopilot
    monkeypatch.setattr(recover, "limit_check", lambda *a, **k: None)
    monkeypatch.setattr(sessionlog, "auto", lambda *a, **k: None)
    monkeypatch.setattr(autopilot, "run_autopilot", lambda *a, **k: None)
    monkeypatch.setattr(identity, "check", lambda *a, **k: None)
    monkeypatch.setattr(dashboard, "TopicMap", lambda *a, **k: type("T", (), {"all_places": lambda self: {}})())
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language="en")
    dashboard.run(c, bot=FakeBot(), now=NOW)
    assert seen == [NOW]


# --- Feature 3: remove an account ---------------------------------------------------------------------------

REAL_LOGIN_TERMINAL_RUNNING = accounts.login_terminal_running


@pytest.fixture
def removable(world, monkeypatch, request):
    if "private_tmux" not in request.fixturenames:  # no real tmux here: the sign-in terminal check is not the point
        monkeypatch.setattr(accounts, "login_terminal_running", lambda name: False)
    accounts.add("account2")
    accounts.add("account4")
    set_json("account-emails.json", {"main": "m@example.org", "account2": "two@example.org", "account4": "three@example.org"})
    set_json("account-limits.json", {"account2": 5.0, "account4": 6.0})
    set_json("account-status.json", {"account2": "out"})
    (accounts.config_dir("account2") / "marker.txt").write_text("keep me")
    monkeypatch.setattr(accounts, "live_session_records", lambda: [])
    return world


def run_account(rest):
    said = Said()
    I.account_command(cfg(), FakeBot(), {"message_thread_id": 1}, None, rest, said)
    return said


def buttons(said):
    return [b for row in said[0][1]["reply_markup"]["inline_keyboard"] for b in row]


def test_both_menus_offer_remove(removable):
    assert any(b["callback_data"] == "more:/account remove" for b in buttons(run_account("")))
    said = Said()
    I.account_command(cfg(), FakeBot(), {"message_thread_id": 1}, "sess-1", "", said)
    assert any(b["callback_data"] == "more:/account remove" for b in buttons(said))


def test_the_pick_list_has_every_extra_account_but_not_main(removable):
    cbs = [b["callback_data"] for b in buttons(run_account("remove"))]
    assert "more:/account remove account2" in cbs and "more:/account remove account4" in cbs
    assert not any(c.endswith(" main") for c in cbs) and "more:/account" in cbs        # cancel returns to the menu


def test_nothing_to_remove_with_only_main(world):
    assert "no extra account" in run_account("remove")[0][0]


def test_removal_asks_by_name_and_changes_nothing_until_yes(removable):
    said = run_account("remove account2")
    assert "account2" in said[0][0] and "two" in said[0][0]
    cbs = [b["callback_data"] for b in buttons(said)]
    assert "more:/account remove account2 yes" in cbs and "more:/account" in cbs
    assert accounts.config_dir("account2") is not None and not accounts.removed_dir().exists()


def test_yes_moves_the_folder_aside_and_forgets_the_account(removable):
    write_private(state_dir() / "account-current", "account2")
    set_json("session-accounts.json", {"old-session": "account2", "other": "account4"})
    set_json("claude-usage-account2.json", {"weekly": 9})
    said = run_account("remove account2 yes")
    assert "removed" in said[0][0]
    assert accounts.config_dir("account2") is None and "account2" not in accounts.names()
    moved = list(accounts.removed_dir().glob("account2-*"))
    assert len(moved) == 1 and (moved[0] / "marker.txt").read_text() == "keep me"      # moved, never deleted
    assert str(moved[0]) in said[0][0]
    assert "account2" not in get_json("account-emails.json") and get_json("account-emails.json")["account4"]
    assert get_json("account-limits.json") == {"account4": 6.0}
    assert "account2" not in get_json("account-status.json")
    assert get_json("session-accounts.json") == {"other": "account4"}
    assert not accounts.usage_path("account2").exists()
    assert accounts.current() == "main"                                                  # the default no longer points at it
    assert accounts.config_dir("account4") is not None


def test_main_is_refused(removable):
    for rest in ("remove main", "remove main yes"):
        assert "main account cannot be removed" in run_account(rest)[0][0]
    with pytest.raises(ValueError):
        accounts.remove("main")


def test_an_account_with_a_running_session_is_refused_and_names_it(removable, monkeypatch):
    sid = "abcdef12-0000-0000-0000-000000000000"
    set_json("session-accounts.json", {sid: "account2"})
    monkeypatch.setattr(accounts, "live_session_records", lambda: [{"sessionId": sid, "pid": 99999}])
    for rest in ("remove account2", "remove account2 yes"):
        said = run_account(rest)
        assert "running session" in said[0][0] and "abcdef12" in said[0][0]
    assert accounts.config_dir("account2") is not None and not accounts.removed_dir().exists()


def test_a_session_found_only_by_its_process_environment_also_blocks(removable, monkeypatch):
    folder = accounts.config_dir("account4")
    monkeypatch.setattr(accounts, "live_session_records", lambda: [{"sessionId": "s-env", "pid": 4242}])
    monkeypatch.setattr(accounts, "_pid_uses_folder", lambda pid, f: f == folder and pid == 4242)
    assert accounts.removal_blocker("account4") == ("sessions", ["s-env"])
    assert accounts.removal_blocker("account2") == ("", [])


def test_the_process_environment_match_is_exact(monkeypatch):
    folder = accounts.profiles_dir() / "account2"

    class R:
        def __init__(self, out):
            self.stdout = out
    import subprocess
    for out, want in ((f"claude --x CLAUDE_CONFIG_DIR={folder} HOME=/h", True), (f"claude CLAUDE_CONFIG_DIR={folder}/", True),
                      (f"claude CLAUDE_CONFIG_DIR={folder}0 HOME=/h", False), ("claude HOME=/h", False)):
        monkeypatch.setattr(subprocess, "run", lambda *a, _o=out, **k: R(_o))
        assert accounts._pid_uses_folder(1, folder) is want


def test_a_login_waiting_for_its_code_blocks_removal(removable):
    accounts.login_started("account2", "acct-login-account2")
    assert "sign-in" in run_account("remove account2 yes")[0][0]
    assert accounts.config_dir("account2") is not None


def test_unknown_names_are_refused(removable):
    assert "No account named" in run_account("remove ghost")[0][0]
    assert "No account named" in run_account("remove ../x yes")[0][0]


def test_two_removals_in_one_second_do_not_collide(removable):
    first = accounts.remove("account2", now=NOW)
    second = accounts.remove("account4", now=NOW)
    assert first != second and first.exists() and second.exists()


def test_the_remove_texts_exist_in_both_languages():
    from ctb.i18n import HE, EN
    keys = [k for k in EN if k.startswith("account_remove") or k == "account_btn_remove"]
    assert len(keys) >= 10 and all(k in HE for k in keys)


def test_token_hiccup_keeps_the_token_address_over_a_stale_auth_status(monkeypatch):
    """A 429 after a good token read must not let a stale `claude auth status` flip the address back."""
    from ctb import accounts
    monkeypatch.setattr(accounts, "real_profile", lambda name, timeout=0: None)
    monkeypatch.setattr(accounts, "known_plan", lambda name: {"plan": "claude_pro", "email": "real@example.net"})
    monkeypatch.setattr(accounts, "cached_email", lambda name: "real@example.net")
    monkeypatch.setattr(accounts, "auth_status", lambda name, timeout=0: {"email": "stale@example.net", "loggedIn": True})
    assert accounts._read_identity("main", 1) == {"email": "real@example.net", "loggedIn": True}


# --- review fixes ---------------------------------------------------------------------------------------------

def tokens_json(access, refresh="r", expires=None):
    return {"accessToken": access, "refreshToken": refresh, "expiresAt": expires or (time.time() + 3600) * 1000}


def test_a_corrected_label_with_the_same_sign_in_keeps_the_recorded_limit_and_usage(trio):
    """The limit path notes a hit, then forces an identity check that corrects a stale label: nothing may be wiped."""
    accounts.refresh_emails(now=NOW)                                # stores the sign-in fingerprints
    set_json("account-emails.json", {**get_json("account-emails.json"), "account6": "stale@example.com"})
    accounts.note_limit("account6", time.time())
    set_json("claude-usage-account6.json", {"five_hour": 100, "weekly": 40})
    changes = accounts.refresh_emails(force=True, now=NOW + 100)
    assert ("account6", "stale@example.com", "alice@example.net") in changes
    assert accounts.usage_path("account6").exists() and "account6" in get_json("account-limits.json")


def test_the_first_correction_after_an_upgrade_has_no_fingerprint_and_wipes_nothing(trio):
    set_json("claude-usage-main.json", {"five_hour": 90, "weekly": 100})
    set_json("account-limits.json", {"main": time.time()})
    accounts.refresh_emails(now=NOW)                                # main's address changes, but this is the first look
    assert accounts.usage_path("main").exists() and "main" in get_json("account-limits.json")


def test_a_real_relogin_drops_the_old_usage_but_never_a_fresh_limit_hit(trio, monkeypatch):
    seen = {"t": 0}

    def keychain(service, timeout=5.0):
        for name, token in trio.tokens.items():
            if accounts.keychain_service(name) == service:
                return json.dumps({"claudeAiOauth": tokens_json(token + str(seen["t"] if name == "account6" else 0))})
        return None
    monkeypatch.setattr(accounts, "_keychain_read", keychain)
    trio.profile_api.update({"T-six0": me("alice@example.net", "claude_max"), "T-six1": me("carol@example.com")})
    trio.tokens["account6"] = "T-six"
    trio.profile_api["T-six0"] = trio.profile_api["T-six"]
    accounts.refresh_emails(now=NOW)
    set_json("claude-usage-account6.json", {"five_hour": 5, "weekly": 31})
    old_hit = time.time() - accounts.LIMIT_COOLDOWN - 60
    set_json("account-limits.json", {"account6": old_hit})
    seen["t"] = 1                                                    # the owner signed it in as someone else
    changes = accounts.refresh_emails(force=True, now=NOW + 100)
    assert ("account6", "alice@example.net", "carol@example.com") in changes
    assert not accounts.usage_path("account6").exists() and "account6" not in get_json("account-limits.json")
    # ...but a hit recorded just now survives even a real re-login
    set_json("account-limits.json", {"account6": time.time()})
    accounts.forget_account_data("account6")
    assert "account6" in get_json("account-limits.json")


def test_fingerprints_never_hold_the_token(trio):
    trio.tokens["main"] = SECRET
    trio.profile_api[SECRET] = me("bob@example.net")
    accounts.refresh_emails(now=NOW)
    text = (state_dir() / "account-fingerprints.json").read_text()
    assert SECRET not in text and len(json.loads(text)["main"]) == 64


def test_add_skips_names_that_still_have_a_keychain_item_or_a_removed_entry(removable, monkeypatch):
    # make account5 look removed
    removable.tokens["account5"] = "T-gone"                           # its Keychain item remains, folder gone
    assert accounts.name_taken("account5") and not accounts.name_taken("account6")
    assert accounts.next_free_name(accounts.names()) not in ("account5",)
    with pytest.raises(ValueError):
        accounts.add("account5")
    moved = accounts.remove("account2", now=NOW)
    assert moved.exists() and accounts.name_taken("account2")        # a removed-profiles entry also blocks the name
    assert accounts.next_free_name(accounts.names()) != "account2"
    with pytest.raises(ValueError):
        accounts.add("account2")


def test_the_remove_confirmation_says_it_does_not_sign_out(removable):
    assert "does not sign the account out" in run_account("remove account2")[0][0]


def test_a_cli_status_re_login_is_not_hidden_by_the_hiccup_fallback(trio, monkeypatch):
    accounts.refresh_emails(now=NOW)                                 # main confirmed by token as bob
    trio.profile_api["T-main"] = (429, {}, 60.0)                     # token can't answer now
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: {"email": "newlogin@example.com", "loggedIn": True})
    write_private(accounts.claude_json(), json.dumps({"oauthAccount": {"emailAddress": "newlogin@example.com"}}))
    accounts.refresh_emails(force=True, now=NOW + 100, only=["main"])
    assert accounts.cached_email("main") == "newlogin@example.com"   # claimed differs from the plan: auth status looked
    # ...and with a claim equal to the confirmed address (or none) the cached answer is kept
    write_private(accounts.claude_json(), json.dumps({"oauthAccount": {"emailAddress": "newlogin@example.com"}}))
    accounts._store_plan("main", "newlogin@example.com", "claude_pro", "")
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: {"email": "flipped@example.com", "loggedIn": True})
    accounts.refresh_emails(force=True, now=NOW + 200, only=["main"])
    assert accounts.cached_email("main") == "newlogin@example.com"


def test_removal_waits_for_nothing_it_refuses_while_a_move_holds_the_lock(removable):
    with accounts.ops_lock():                                        # what account_move / rebalance hold
        said = run_account("remove account2 yes")
        assert "being moved" in said[0][0]
        with pytest.raises(ValueError):
            accounts.remove("account2")
    assert accounts.config_dir("account2") is not None
    accounts.remove("account2")
    assert accounts.config_dir("account2") is None


def test_a_move_takes_the_shared_lock_and_a_vanished_target_is_not_moved(removable, monkeypatch):
    calls = []
    monkeypatch.setattr(I, "_account_move", lambda *a, **k: calls.append(a) or True)
    assert I.account_move(cfg(), FakeBot(), "sess-m1", "account2", Said()) is True
    accounts.remove("account2")
    assert I.account_move(cfg(), FakeBot(), "sess-m2", "account2", Said()) is False and len(calls) == 1
    with pytest.raises(OSError):                                     # a removal could not start meanwhile
        with accounts.ops_lock(exclusive=True, blocking=False):
            with accounts.ops_lock(exclusive=True, blocking=False):
                pass


def test_rebalance_holds_the_shared_lock_while_it_picks_and_spawns(removable, monkeypatch):
    held = []
    real = accounts.ops_lock

    class Spy:
        def __init__(self, *a, **k):
            self.inner = real(*a, **k)

        def __enter__(self):
            held.append("in")
            return self.inner.__enter__()

        def __exit__(self, *e):
            held.append("out")
            return self.inner.__exit__(*e)
    monkeypatch.setattr(accounts, "ops_lock", Spy)
    monkeypatch.setattr(accounts, "live_session_records", lambda: [{"sessionId": "s1", "pid": 0, "status": "idle",
                                                                    "statusUpdatedAt": 0, "tmux": True}])
    set_json("topics.json", {"s1": {"thread": 5}})
    monkeypatch.setattr(accounts, "account_for", lambda s=None: "account2")
    monkeypatch.setattr(accounts, "is_big_plan", lambda n: n == "account2")
    monkeypatch.setattr(accounts, "pick_account", lambda *a, **k: "account4")
    monkeypatch.setattr(accounts, "shell_children", lambda pid: False)
    monkeypatch.setattr(accounts, "_subagent_active", lambda *a, **k: False)
    spawned = []
    assert accounts.rebalance(spawn_fn=lambda argv: spawned.append(argv), now=time.time() + 10_000) == ("s1", "account4")
    assert held == ["in", "out"] and spawned


def test_a_login_terminal_still_running_blocks_removal(removable, monkeypatch):
    monkeypatch.setattr(accounts, "login_terminal_running", lambda name: name == "account2")
    assert accounts.removal_blocker("account2") == ("login", [])
    assert "sign-in" in run_account("remove account2 yes")[0][0]
    assert accounts.config_dir("account2") is not None
    assert accounts.removal_blocker("account4") == ("", [])


def test_login_terminal_detection_checks_tmux_and_the_process_list(removable, monkeypatch):
    import subprocess
    folder = accounts.config_dir("account2")

    class R:
        def __init__(self, rc=1, out=""):
            self.returncode, self.stdout = rc, out
    state = {"tmux": 1, "ps": ""}

    def fake(cmd, **k):
        return R(0 if (cmd[0] == "tmux" and state["tmux"] == 0) else 1, state["ps"] if cmd[0] == "ps" else "")
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(accounts, "login_terminal_running", REAL_LOGIN_TERMINAL_RUNNING)  # the real check, faked tmux
    assert accounts.login_terminal_running("account2") is False
    state["tmux"] = 0
    assert accounts.login_terminal_running("account2") is True
    state["tmux"], state["ps"] = 1, f"claude auth login CLAUDE_CONFIG_DIR={folder} BROWSER=/usr/bin/true\nclaude CLAUDE_CONFIG_DIR={folder}\n"
    assert accounts.login_terminal_running("account2") is True
    state["ps"] = f"claude CLAUDE_CONFIG_DIR={folder}\n"
    assert accounts.login_terminal_running("account2") is False


def test_the_dashboard_row_shows_the_age_of_a_reading_older_than_an_hour(removable):
    now = accounts.iso_epoch("2026-11-02T12:00:00Z")
    set_json("claude-usage-account2.json", {"at": "2026-11-02T08:30:00Z", "five_hour": 3, "weekly": 4, "weekly_resets": now + 999})
    set_json("claude-usage-account4.json", {"at": "2026-11-02T11:30:00Z", "five_hour": 3, "weekly": 4, "weekly_resets": now + 999})
    lines = accounts.usage_lines(now=now, lang="en")
    assert any("two" in ln and "old: 3h ago" in ln for ln in lines)
    assert not any("three" in ln.lower() and "old" in ln for ln in lines)


def test_a_failing_token_call_leaks_the_token_into_neither_state_nor_poll_log(trio, monkeypatch):
    trio.tokens["main"] = SECRET

    def boom(path, token, timeout=8.0):
        raise RuntimeError("request failed for " + token)
    monkeypatch.setattr(accounts, "_http_json", boom)
    accounts.refresh_emails(now=NOW, only=["main"])                  # swallowed by the worker, falls back
    try:
        accounts.refresh_usage(now=NOW, only=["main"])
    except RuntimeError:
        pass                                                         # the dashboard job swallows this and logs nothing
    I.poll_log("account removal failed: RuntimeError")
    blob = "".join(p.read_text(errors="ignore") for p in state_dir().glob("*") if p.is_file())
    assert SECRET not in blob and not any(SECRET in line for line in blob.splitlines())


@pytest.mark.skipif(not __import__("shutil").which("tmux"), reason="needs tmux")
def test_an_unfinished_sign_in_terminal_closes_by_itself(private_tmux, removable, monkeypatch):
    """A link nobody finished kept `claude auth login` open for hours and blocked removing the account."""
    import shutil
    import subprocess
    import tempfile
    folder = tempfile.mkdtemp(dir="/tmp")
    monkeypatch.setenv("TMUX_TMPDIR", folder)  # a private server: the live one is never touched
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setattr(accounts, "LOGIN_WINDOW", -119)  # the watchdog fires after one second
    line = accounts.login_shell("account2").replace("claude auth login", "sleep 60")
    try:
        assert subprocess.run(["tmux", "new-session", "-d", "-s", "acct-login-account2", "/bin/zsh", "-c", line],
                              check=False).returncode == 0
        assert accounts.login_terminal_running("account2")
        end = time.monotonic() + 10
        while accounts.login_terminal_running("account2") and time.monotonic() < end:
            time.sleep(0.2)
        assert not accounts.login_terminal_running("account2")
        assert accounts.removal_blocker("account2") == ("", [])
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True, check=False)
        shutil.rmtree(folder, ignore_errors=True)
