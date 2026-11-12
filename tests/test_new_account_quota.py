"""2026-11-03: a newly signed-in account showed no quota, an account whose sign-in was never finished
looked like "no data" (and could be picked as a move target), and the status card's quota/account buttons
only changed after a reply. No network, no Keychain, no real claude."""
from __future__ import annotations

import json
import os
import time

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import accounts, heartbeat, mirror
from ctb import inbox as I
from ctb.config import Config, state_dir, write_private
from ctb.topics import TopicMap
from test_real_identity import FakeWorld, get_json, set_json, usage_body

NOW = time.time()


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language="en")


@pytest.fixture
def world(monkeypatch):
    return FakeWorld(monkeypatch)


# --- 1. a new account gets its reading at once ---------------------------------------------------------------

def test_a_stale_signed_out_state_no_longer_blocks_the_first_reading(world):
    accounts.add("account10")
    set_json("account-status.json", {"account10": "out"})        # read before the sign-in finished
    world.tokens["account10"] = "T-ten"
    world.usage_api["T-ten"] = usage_body(week=90.0)
    assert accounts.refresh_usage(now=NOW, only=["account10"]) == ["account10"]
    assert get_json("claude-usage-account10.json")["weekly"] == 90


def test_a_revoked_token_backs_off_on_401(world):
    accounts.add("account10")
    world.tokens["account10"] = "T-ten"
    world.usage_api["T-ten"] = (401, {}, 0.0)
    accounts.refresh_usage(now=NOW, only=["account10"])
    n = len(world.calls)
    accounts.refresh_usage(now=NOW + 400, only=["account10"], force=True)
    assert len(world.calls) == n


def test_force_ignores_the_five_minute_throttle_but_keeps_a_short_gap(world):
    accounts.add("account10")
    world.tokens["account10"] = "T-ten"
    world.usage_api["T-ten"] = usage_body()
    accounts.refresh_usage(now=NOW, only=["account10"])
    n = len(world.calls)
    accounts.refresh_usage(now=NOW + 5, only=["account10"], force=True)
    assert len(world.calls) == n                                  # too soon even when forced
    accounts.refresh_usage(now=NOW + 30, only=["account10"], force=True)
    assert len(world.calls) == n + 1                              # normal throttle would wait 300 s


def test_ensure_usage_fetches_only_missing_or_stale_readings(world):
    for n in ("account2", "account10"):
        accounts.add(n)
        world.tokens[n] = "T-" + n
        world.usage_api["T-" + n] = usage_body()
    fresh = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 30)), "five_hour": 1, "weekly": 2,
             "five_hour_resets": "", "weekly_resets": "", "source": "statusline", "extra": []}
    write_private(accounts.usage_path("account2"), json.dumps(fresh))
    assert accounts.ensure_usage(now=NOW) == ["account10"]
    assert get_json("claude-usage-account2.json")["weekly"] == 2   # untouched


def test_a_profile_read_as_signed_out_is_re_read_within_a_minute(monkeypatch):
    accounts.add("account10")
    set_json("account-status.json", {"account10": "out"})
    set_json("account-emails-checked.json", {"account10": NOW - 90})
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: {"email": "lib@example.com", "loggedIn": True})
    accounts.refresh_emails(now=NOW, only=["account10"])
    assert not accounts.logged_out("account10") and accounts.cached_email("account10") == "lib@example.com"


def test_a_signed_in_profile_is_not_re_read_that_often(monkeypatch):
    accounts.add("account10")
    set_json("account-emails.json", {"account10": "lib@example.com"})
    set_json("account-emails-checked.json", {"account10": NOW - 90})
    calls = []
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: calls.append(n) or {})
    accounts.refresh_emails(now=NOW, only=["account10"])
    assert calls == []


# --- 3. an account whose sign-in is not finished -------------------------------------------------------------

def unfinished_setup():
    accounts.add("account9")
    set_json("account-status.json", {"account9": "out"})


def test_unfinished_sign_in_is_not_a_move_target_and_not_no_data():
    accounts.add("account2")
    set_json("account-emails.json", {"main": "a@example.com", "account2": "b@example.com"})
    unfinished_setup()
    assert accounts.sign_in_unfinished("account9") and not accounts.sign_in_unfinished("account2")
    assert accounts.logged_out("account9")
    assert "account9" not in accounts.distinct_others("main")
    lines = "\n".join(accounts.usage_lines(now=NOW, lang="en"))
    assert "account9: sign-in not finished" in lines and "account9: No data" not in lines


def test_a_signed_out_account_that_was_once_signed_in_is_not_unfinished():
    accounts.add("account9")
    set_json("account-status.json", {"account9": "out"})
    set_json("account-emails.json", {"account9": "old@example.com"})
    assert not accounts.sign_in_unfinished("account9") and accounts.logged_out("account9")


def test_a_login_in_progress_counts_as_unfinished():
    accounts.add("account9")
    accounts.login_started("account9", "acct-login-account9")
    assert accounts.sign_in_unfinished("account9")


def test_hebrew_line_keeps_the_account_name_isolated():
    unfinished_setup()
    he = [l for l in accounts.usage_lines(now=NOW, lang="he") if "account9" in l][0]
    assert "⁨account9⁩" in he and "ההתחברות לא הסתיימה" in he


def test_the_account_menu_offers_to_finish_the_sign_in(monkeypatch):
    accounts.add("account2")
    set_json("account-emails.json", {"main": "a@example.com", "account2": "b@example.com"})
    unfinished_setup()
    sent = []
    I.account_command(cfg(), FakeBot(), {}, None, "", lambda text, **kw: sent.append((text, kw)))
    text, kw = sent[-1]
    rows = {r[0]["callback_data"]: r[0]["text"] for r in kw["reply_markup"]["inline_keyboard"]}
    assert rows["more:/account add account9"] == "🔑 account9 · sign-in not finished, tap to finish"
    assert not any(cb.endswith("use account9") for cb in rows)
    assert "sign-in was not finished" in text


def test_the_session_menu_also_offers_it_and_never_moves_there(monkeypatch):
    accounts.add("account2")
    set_json("account-emails.json", {"main": "a@example.com", "account2": "b@example.com"})
    unfinished_setup()
    sent = []
    I.account_command(cfg(), FakeBot(), {}, "sess-1", "", lambda text, **kw: sent.append((text, kw)))
    cbs = [r[0]["callback_data"] for r in sent[-1][1]["reply_markup"]["inline_keyboard"]]
    assert "more:/account add account9" in cbs and not any("move account9" in c for c in cbs)


def test_opening_the_menu_fetches_a_missing_reading(world):
    accounts.add("account10")
    set_json("account-emails.json", {"main": "a@example.com", "account10": "lib@example.com"})
    world.tokens["account10"] = "T-ten"
    world.usage_api["T-ten"] = usage_body(week=90.0)
    set_json("account-login-finished.json", {"account10": time.time() - 30})   # signed in half a minute ago
    I.account_command(cfg(), FakeBot(), {}, None, "", lambda *a, **k: None)
    assert get_json("claude-usage-account10.json")["weekly"] == 90


# --- 2. the status card follows the data ---------------------------------------------------------------------

def write_usage(acct, five, week, at=None):
    at = time.time() if at is None else at
    write_private(accounts.usage_path(acct), json.dumps({
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)), "five_hour": five, "weekly": week,
        "five_hour_resets": int(time.time()) + 3600, "weekly_resets": int(time.time()) + 86400,
        "source": "statusline", "extra": []}))


def test_live_keyboard_fresh_ignores_the_quota_numbers_of_an_old_text():
    write_usage("main", 7, 8)
    old = "📏 Context 50k\n⏳ weekly 71% · 3 min ago"
    stale = mirror.live_keyboard(old, lang="en")["keyboard"][0]
    fresh = mirror.live_keyboard(old, lang="en", fresh=True)["keyboard"][0]
    assert any("71%" in b["text"] for b in stale)
    assert any("8%" in b["text"] for b in fresh) and not any("71%" in b["text"] for b in fresh)
    assert not any("7%" in b["text"].replace("8%", "") for b in fresh)  # the 5-hour number is never shown


def test_restamp_footer_swaps_in_todays_quota_line():
    write_usage("main", 7, 8)
    out = mirror.restamp_footer("📏 Context 50k\n⏳ 5h 70% · weekly 71% · 3 min ago\nbg", None, "en")
    assert "70%" not in out and out.startswith("📏 Context 50k")


def sent_messages(bot):
    return [p for m, p in bot.calls if m in ("sendMessage", "editMessageText")]


def card_world(tmp_path, monkeypatch, idle=0.0):
    """A live session with a topic; its transcript was last written `idle` seconds before NOW."""
    bot = FakeBot()
    topics = TopicMap(state_dir() / "topics.json", bot, 7)
    thread = topics.ensure("sess-1", "Title")
    monkeypatch.setattr(accounts, "live_session_records", lambda: [{"sessionId": "sess-1"}])
    transcript = tmp_path / "sess-1.jsonl"
    transcript.write_text("{}")
    os.utime(transcript, (NOW - idle, NOW - idle))
    monkeypatch.setattr(heartbeat.status, "transcript_of", lambda session: str(transcript))
    return bot, topics, thread


LATER = 16 * 60  # past the quarter hour between two redraws of one topic


def test_a_new_session_gets_its_first_card_with_quota_on_the_heartbeat(tmp_path, monkeypatch):
    bot, topics, thread = card_world(tmp_path, monkeypatch)
    write_usage("main", 4, 40)
    assert heartbeat.refresh_cards(cfg(), bot, NOW) == 1
    sent = [p for m, p in bot.calls if m == "sendMessage" and p.get("message_thread_id") == thread][-1]
    assert any("40%" in b["text"] for row in sent["reply_markup"]["keyboard"] for b in row)
    n = len(bot.calls)
    assert heartbeat.refresh_cards(cfg(), bot, NOW) == 0 and len(bot.calls) == n   # nothing changed: no traffic


def test_the_card_is_redrawn_when_the_quota_moves(tmp_path, monkeypatch):
    bot, topics, thread = card_world(tmp_path, monkeypatch)
    write_usage("main", 4, 40)
    heartbeat.refresh_cards(cfg(), bot, NOW)
    write_usage("main", 4, 55)
    assert heartbeat.refresh_cards(cfg(), bot, NOW + LATER) == 1
    last = [p for m, p in bot.calls if m == "sendMessage"][-1]
    assert any("55%" in b["text"] for row in last["reply_markup"]["keyboard"] for b in row)


def test_the_card_follows_a_session_to_another_account(tmp_path, monkeypatch):
    bot, topics, thread = card_world(tmp_path, monkeypatch)
    (accounts.profiles_dir() / "account2").mkdir(parents=True)
    write_usage("main", 4, 40)
    write_usage("account2", 1, 9)
    heartbeat.refresh_cards(cfg(), bot, NOW)
    accounts.record("sess-1", "account2")
    assert heartbeat.refresh_cards(cfg(), bot, NOW + LATER) == 1
    last = [p for m, p in bot.calls if m == "sendMessage"][-1]
    assert any("9%" in b["text"] for row in last["reply_markup"]["keyboard"] for b in row)


def test_a_flooded_chat_skips_the_refresh_and_retries_later(tmp_path, monkeypatch):
    bot, topics, thread = card_world(tmp_path, monkeypatch)
    write_usage("main", 4, 40)
    monkeypatch.setattr(heartbeat.flood, "is_open", lambda chat, now=None: False)
    monkeypatch.setattr(mirror.flood, "is_open", lambda chat, now=None: False)
    assert mirror.refresh_card(cfg(), bot, "sess-1", topics=topics, create=True) is False
    assert sent_messages(bot) == []
    monkeypatch.setattr(heartbeat.flood, "is_open", lambda chat, now=None: True)
    assert heartbeat.refresh_cards(cfg(), bot, NOW) == 1


def test_a_session_that_is_not_running_gets_no_card(tmp_path, monkeypatch):
    bot, topics, thread = card_world(tmp_path, monkeypatch)
    monkeypatch.setattr(accounts, "live_session_records", lambda: [])
    assert heartbeat.refresh_cards(cfg(), bot, NOW) == 0 and sent_messages(bot) == []


# --- review round: the first card is optional traffic, paced, and never repeated at once --------------------

def first_cards(bot, thread):
    return [p for m, p in bot.calls if m == "sendMessage" and p.get("message_thread_id") == thread]


def test_the_first_card_goes_through_the_flood_gate(tmp_path, monkeypatch):
    bot, topics, thread = card_world(tmp_path, monkeypatch)
    routed = []
    real = mirror.flood.optional_call
    monkeypatch.setattr(mirror.flood, "optional_call", lambda b, m, p, **kw: routed.append(m) or real(b, m, p, **kw))
    assert heartbeat.refresh_cards(cfg(), bot, NOW) == 1
    assert routed == ["sendMessage"]


def test_a_failed_first_card_is_not_retried_for_a_quarter_hour(tmp_path, monkeypatch):
    bot, topics, thread = card_world(tmp_path, monkeypatch)
    bot.fail["sendMessage"] = "Bad Request: something odd"
    assert heartbeat.refresh_cards(cfg(), bot, NOW) == 0
    n = len(first_cards(bot, thread))
    assert heartbeat.refresh_cards(cfg(), bot, NOW + 60) == 0 and len(first_cards(bot, thread)) == n   # backed off
    assert heartbeat.refresh_cards(cfg(), bot, NOW + LATER) == 1                                       # then again


def test_a_failed_attempt_ends_the_round_for_the_other_topics(tmp_path, monkeypatch):
    bot, topics, thread = card_world(tmp_path, monkeypatch)
    other = topics.ensure("sess-2", "Other")
    monkeypatch.setattr(accounts, "live_session_records", lambda: [{"sessionId": "sess-1"}, {"sessionId": "sess-2"}])
    bot.fail["sendMessage"] = "Bad Request: something odd"
    assert heartbeat.refresh_cards(cfg(), bot, NOW) == 0
    assert len(first_cards(bot, thread)) == 1 and first_cards(bot, other) == []   # one attempt per round
    assert heartbeat.refresh_cards(cfg(), bot, NOW + 30) == 1                    # the next round: the other topic
    assert len(first_cards(bot, other)) == 1 and len(first_cards(bot, thread)) == 1


def test_a_deleted_topic_is_forgotten(tmp_path, monkeypatch):
    bot, topics, thread = card_world(tmp_path, monkeypatch)
    bot.fail["sendMessage"] = "Bad Request: message thread not found"
    assert heartbeat.refresh_cards(cfg(), bot, NOW) == 0
    assert TopicMap(state_dir() / "topics.json", bot, 7).thread_of("sess-1") is None


def test_a_closed_topic_is_kept_and_waits(tmp_path, monkeypatch):
    bot, topics, thread = card_world(tmp_path, monkeypatch)
    bot.fail["sendMessage"] = "Bad Request: TOPIC_CLOSED"
    assert heartbeat.refresh_cards(cfg(), bot, NOW) == 0
    assert TopicMap(state_dir() / "topics.json", bot, 7).thread_of("sess-1") == thread
    assert mirror.card_waiting(thread, False, NOW + 60)


def test_a_failing_redraw_waits_so_other_topics_get_a_turn(tmp_path, monkeypatch):
    bot, topics, thread = card_world(tmp_path, monkeypatch)
    write_usage("main", 4, 40)
    assert heartbeat.refresh_cards(cfg(), bot, NOW) == 1
    write_usage("main", 4, 55)
    for method in ("sendMessage", "editMessageText"):
        bot.fail[method] = "Bad Request: not enough rights"
    assert heartbeat.refresh_cards(cfg(), bot, NOW + LATER) == 0
    assert mirror.card_waiting(thread, True, NOW + LATER + 60)


def test_a_flood_skip_leaves_no_backoff(tmp_path, monkeypatch):
    bot, topics, thread = card_world(tmp_path, monkeypatch)
    monkeypatch.setattr(mirror.flood, "allow_optional", lambda *a, **k: False)
    assert heartbeat.refresh_cards(cfg(), bot, NOW) == 0
    assert not mirror.card_waiting(thread, False, NOW)


def test_no_first_card_for_a_session_idle_over_half_an_hour(tmp_path, monkeypatch):
    bot, topics, thread = card_world(tmp_path, monkeypatch, idle=31 * 60)
    assert heartbeat.refresh_cards(cfg(), bot, NOW) == 0 and sent_messages(bot) == []
    bot, topics, thread = card_world(tmp_path, monkeypatch, idle=10 * 60)
    assert heartbeat.refresh_cards(cfg(), bot, NOW) == 1


def test_a_paced_topic_does_not_hold_up_the_others(tmp_path, monkeypatch):
    bot, topics, thread = card_world(tmp_path, monkeypatch)
    topics.ensure("sess-2", "Other")
    monkeypatch.setattr(accounts, "live_session_records", lambda: [{"sessionId": "sess-1"}, {"sessionId": "sess-2"}])
    write_usage("main", 4, 40)
    assert heartbeat.refresh_cards(cfg(), bot, NOW) == 1 and heartbeat.refresh_cards(cfg(), bot, NOW) == 1
    write_usage("main", 4, 55)                                          # both cards stale, both drawn lately
    assert heartbeat.refresh_cards(cfg(), bot, NOW + 60) == 0
    assert heartbeat.refresh_cards(cfg(), bot, NOW + LATER) == 1


def test_a_card_is_not_redrawn_within_a_quarter_hour(tmp_path, monkeypatch):
    bot, topics, thread = card_world(tmp_path, monkeypatch)
    write_usage("main", 4, 40)
    heartbeat.refresh_cards(cfg(), bot, NOW)
    write_usage("main", 4, 55)
    n = len(bot.calls)
    assert heartbeat.refresh_cards(cfg(), bot, NOW + 300) == 0 and len(bot.calls) == n


def test_one_percent_of_quota_does_not_make_the_card_stale(tmp_path, monkeypatch):
    bot, topics, thread = card_world(tmp_path, monkeypatch)
    write_usage("main", 4, 41)
    key = mirror.card_data_key("sess-1")
    write_usage("main", 4, 48)
    assert mirror.card_data_key("sess-1") == key
    write_usage("main", 4, 52)
    assert mirror.card_data_key("sess-1") != key


# --- review round: parallel usage fetches ---------------------------------------------------------------------

def test_a_parallel_429_backoff_is_not_erased_by_a_slower_call(world):
    for n in ("account2", "account10"):
        accounts.add(n)
        world.tokens[n] = "T-" + n
        world.usage_api["T-" + n] = usage_body()
    inner = []

    def slow(path, token, timeout=8.0):          # while account2 is being fetched, another call hits a 429 on account10
        if token == "T-account2" and not inner:
            inner.append(1)
            world.usage_api["T-account10"] = (429, {}, 0.0)
            accounts.refresh_usage(now=NOW, only=["account10"])
        return world.http(path, token, timeout)

    accounts.refresh_usage(now=NOW, only=["account2", "account10"], fetch=slow)
    state = get_json("account-usage-fetch.json")
    assert state["account10"].get("until", 0) > NOW and state["account10"]["fails"] == 1


def test_the_fetch_slot_is_claimed_before_the_request(world):
    accounts.add("account10")
    world.tokens["account10"] = "T-ten"
    world.usage_api["T-ten"] = usage_body()
    seen = []

    def spy(path, token, timeout=8.0):
        seen.append(get_json("account-usage-fetch.json").get("account10", {}).get("at"))
        return world.http(path, token, timeout)

    accounts.refresh_usage(now=NOW, only=["account10"], fetch=spy)
    assert seen == [NOW]
    n = len(world.calls)
    accounts.refresh_usage(now=NOW + 5, only=["account10"], force=True)   # a second thread, inside the gap
    assert len(world.calls) == n


# --- review round: the account menu -----------------------------------------------------------------------------

def test_only_a_just_finished_sign_in_is_fetched_before_the_menu_draws(world, monkeypatch):
    for n in ("account2", "account10"):
        accounts.add(n)
        set_json("account-emails.json", {"main": "a@example.com", "account2": "b@example.com", "account10": "c@example.com"})
        world.tokens[n] = "T-" + n
        world.usage_api["T-" + n] = usage_body()
    set_json("account-login-finished.json", {"account10": time.time() - 3000})   # 50 minutes ago: not new
    assert accounts.ensure_usage(max_age=float("inf"), recent_only=True) == []
    set_json("account-login-finished.json", {"account10": time.time() - 60})
    assert accounts.ensure_usage(max_age=float("inf"), recent_only=True) == ["account10"]


def test_login_done_remembers_when_the_sign_in_finished():
    accounts.login_started("account9", "acct-login-account9")
    accounts.login_done()
    assert accounts.recently_signed_in("account9") and not accounts.recently_signed_in("account9", time.time() + 700)
    assert accounts.login_pending() is None


def test_the_menu_waits_at_most_three_seconds_for_the_fetch(monkeypatch):
    monkeypatch.setattr(accounts, "ensure_usage", lambda **kw: time.sleep(6) if kw.get("recent_only") else None)
    t0 = time.time()
    I._refresh_usage_for_menu()
    assert 2.5 < time.time() - t0 < 4.5


def test_opening_the_menu_twice_starts_one_background_refresh(monkeypatch):
    import threading
    started, gate = [], threading.Event()
    monkeypatch.setattr(accounts, "ensure_usage", lambda **kw: None if kw else (started.append(1), gate.wait(5)))
    I._refresh_usage_for_menu()
    I._refresh_usage_for_menu()
    I._refresh_usage_for_menu()
    time.sleep(0.2)
    gate.set()
    time.sleep(0.2)
    assert started == [1]
    assert I._MENU_REFRESH.acquire(blocking=False)    # released afterwards
    I._MENU_REFRESH.release()


# --- review round: the profile re-read, the Hebrew line, the pending sign-in ---------------------------------

def test_an_old_never_signed_in_profile_is_not_re_read_every_minute(monkeypatch):
    accounts.add("account10")
    set_json("account-status.json", {"account10": "out"})
    set_json("account-emails-checked.json", {"account10": NOW - 90})
    calls = []
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: calls.append(n) or {})
    monkeypatch.setattr(accounts, "_profile_age", lambda name, now_ts: 5 * 3600.0)
    accounts.refresh_emails(now=NOW, only=["account10"])
    assert calls == []                                    # the normal interval applies
    monkeypatch.setattr(accounts, "_profile_age", lambda name, now_ts: 600.0)
    accounts.refresh_emails(now=NOW, only=["account10"])
    assert calls == ["account10"]                         # a folder made ten minutes ago


def test_a_pending_sign_in_keeps_the_fast_re_read_even_on_an_old_folder(monkeypatch):
    accounts.add("account10")
    set_json("account-status.json", {"account10": "out"})
    set_json("account-emails-checked.json", {"account10": NOW - 90})
    calls = []
    monkeypatch.setattr(accounts, "auth_status", lambda n, timeout=30: calls.append(n) or {})
    monkeypatch.setattr(accounts, "_profile_age", lambda name, now_ts: 5 * 3600.0)
    accounts.login_started("account10", "acct-login-account10")
    accounts.refresh_emails(now=NOW, only=["account10"])
    assert calls == ["account10"]


def menu_rows(lang="en", session=None):
    sent = []
    I.account_command(Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language=lang), FakeBot(), {}, session, "",
                      lambda text, **kw: sent.append((text, kw)))
    return sent[-1][1]["reply_markup"]["inline_keyboard"]


def test_the_account_menu_row_isolates_the_name_in_hebrew():
    accounts.add("account2")
    set_json("account-emails.json", {"main": "a@example.com", "account2": "b@example.com"})
    unfinished_setup()
    set_json("account-login-finished.json", {})
    texts = [r[0]["text"] for r in menu_rows("he")]
    assert any("\u2068account9\u2069 · " in x for x in texts)
    accounts.add("account4")
    set_json("account-emails.json", {"main": "a@example.com", "account2": "b@example.com", "account4": "c@example.com"})
    set_json("account-status.json", {"account9": "out", "account4": "out"})
    texts = [r[0]["text"] for r in menu_rows("he")]
    assert any("\u2068account4\u2069 · לא מחובר" in x for x in texts)


def test_a_pending_sign_in_shows_link_sent_and_does_not_restart_it():
    accounts.add("account2")
    set_json("account-emails.json", {"main": "a@example.com", "account2": "b@example.com"})
    unfinished_setup()
    accounts.login_started("account9", "acct-login-account9")
    for session in (None, "sess-1"):
        rows = {r[0]["callback_data"]: r[0]["text"] for r in menu_rows("en", session)}
        mine = [text for text in rows.values() if "account9" in text][0]
        assert "link sent" in mine and "🔑" not in mine
        assert "more:/account add account9" not in rows


def test_the_finish_button_returns_after_the_window():
    accounts.add("account2")
    set_json("account-emails.json", {"main": "a@example.com", "account2": "b@example.com"})
    unfinished_setup()
    set_json("account-login.json", {"name": "account9", "tmux": "acct-login-account9",
                                    "at": time.time() - accounts.LOGIN_WINDOW - 5})
    rows = {r[0]["callback_data"]: r[0]["text"] for r in menu_rows("en")}
    assert rows["more:/account add account9"].startswith("🔑 account9")


def test_the_link_sent_string_exists_in_both_languages():
    from ctb import i18n
    assert i18n.t("account_link_sent_tag", lang="en") and i18n.t("account_link_sent_tag", lang="he") != i18n.t("account_link_sent_tag", lang="en")
