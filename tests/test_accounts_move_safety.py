"""Account moves fail closed around signed-out profiles, using only isolated test state."""
import json

from conftest import signed_in
from ctb import accounts, inbox
from ctb.config import Config, state_dir, write_private

SID = "11111111-2222-4333-8444-555555555555"


def profile(name):
    (accounts.profiles_dir() / name).mkdir(parents=True, exist_ok=True)


def put(name, value):
    write_private(state_dir() / name, json.dumps(value))


def test_all_automatic_pickers_skip_explicitly_signed_out_targets(monkeypatch):
    profile("ready")
    profile("signedout")
    signed_in("main", "ready")
    put("account-status.json", {"signedout": "out"})
    put("session-accounts.json", {SID: "main"})
    monkeypatch.setattr(accounts, "has_headroom", lambda name, now=None: True)
    monkeypatch.setattr(accounts, "used", lambda name, now=None: 100 if name == "main" else 0)

    assert accounts.distinct_others("main") == ["ready"]
    assert accounts.fallback_plan(SID, now=1_800_000_000)["fresh"] == ["ready"]
    assert accounts.pick_account("main", now=1_800_000_000) == "ready"


def test_manual_move_to_signed_out_target_never_touches_tmux(monkeypatch):
    profile("signedout")
    put("account-status.json", {"signedout": "out"})
    put("session-accounts.json", {SID: "main"})
    touched = []
    monkeypatch.setattr(inbox.identity, "check", lambda *a, **k: None)
    monkeypatch.setattr(inbox, "_tmux", lambda *a, **k: touched.append(a))
    said = []

    moved = inbox.account_move(Config(), object(), SID, "signedout", lambda text, **kw: said.append(text))

    assert moved is False
    assert not touched
    assert said and "signedout" in said[0]
    assert accounts.origin_account(SID) == "main"


def test_signed_out_recovery_returns_to_previous_logged_in_account_once(monkeypatch):
    profile("bad")
    signed_in("main", "bad")
    put("session-accounts.json", {SID: "bad"})
    marker = state_dir() / "move-origins" / SID
    write_private(marker, json.dumps({"previous": "main", "target": "bad"}))
    calls = []

    def fake_move(cfg, bot, session, target, say, idle_only, defer, limit, expected_origin,
                  remember_origin):
        calls.append((session, target, remember_origin))
        if remember_origin:
            inbox._remember_move_origin(session, accounts.origin_account(session), target)
        accounts.record(session, target)
        return True

    monkeypatch.setattr(inbox, "_account_move", fake_move)
    assert inbox.recover_logged_out_move(Config(), object(), SID, lambda *a, **k: None)
    assert calls == [(SID, "main", False)]
    assert accounts.origin_account(SID) == "main"
    assert accounts.logged_out("bad")
    assert not marker.exists()
    assert not inbox.recover_logged_out_move(Config(), object(), SID, lambda *a, **k: None)
    assert calls == [(SID, "main", False)]


def test_signed_out_recovery_ignores_marker_for_a_different_current_account(monkeypatch):
    profile("bad")
    profile("other")
    signed_in("main", "bad", "other")
    put("session-accounts.json", {SID: "other"})
    marker = state_dir() / "move-origins" / SID
    write_private(marker, json.dumps({"previous": "main", "target": "bad"}))
    monkeypatch.setattr(inbox, "account_move", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a stale marker must not move the session")))

    assert not inbox.recover_logged_out_move(Config(), object(), SID, lambda *a, **k: None)
    assert accounts.origin_account(SID) == "other"
    assert not accounts.logged_out("bad")
    assert not marker.exists()


def test_profile_without_a_finished_login_is_never_a_target(monkeypatch):
    profile("fresh")
    profile("ready")
    signed_in("main", "ready")
    put("session-accounts.json", {SID: "main"})
    monkeypatch.setattr(accounts, "has_headroom", lambda name, now=None: True)
    monkeypatch.setattr(accounts, "used", lambda name, now=None: 100 if name == "main" else 0)

    assert not accounts.eligible_target("fresh")
    assert not accounts.eligible_target("missing")
    assert accounts.eligible_target("ready")
    assert accounts.distinct_others("main") == ["ready"]
    assert accounts.pick_account("main", now=1_800_000_000) == "ready"
