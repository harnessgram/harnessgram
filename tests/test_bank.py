"""The reset bank: saved quota resets per provider, kept by the owner, shown in the panel and told to agents."""
import json
from datetime import date, timedelta

import pytest

from ctb import bank, hooks, panel
from ctb.config import state_dir
from test_decisions import cfg, rig, roomy_budget, server  # noqa: F401
from test_panel import call, signed


def post(server, init=None, **body):
    status, _, data = call(server, path="/api/bank", init=init or signed(), body=json.dumps(body).encode())
    return status, json.loads(data)


def test_counts_are_stored_private_and_listed_known_first(rig, capsys):
    unset = {"expires": None, "days": None}
    assert bank.view() == [{"name": "claude", "n": None, **unset}, {"name": "codex", "n": None, **unset}]
    assert bank.run("set", "Codex", 2) == 0 and bank.run("set", "gemini", 1) == 0
    assert bank.read() == {"codex": 2, "gemini": 1}
    assert bank.view() == [{"name": "claude", "n": None, **unset}, {"name": "codex", "n": 2, **unset},
                           {"name": "gemini", "n": 1, **unset}]
    assert (state_dir() / bank.STATE_FILE).stat().st_mode & 0o777 == 0o600
    assert "Codex: 2" in capsys.readouterr().out
    assert bank.run("forget", "gemini") == 0 and "gemini" not in bank.read()
    assert bank.run("forget", "gemini") == 1


@pytest.mark.parametrize("name,n", [("", 1), ("Claude", 1), ("a" * 21, 1), ("../x", 1), ("claude", -1), ("claude", 100),
                                    ("claude", True), ("claude", "1"), ("claude", 1.0), (5, 1)])
def test_a_bad_name_or_number_is_refused(rig, name, n):
    assert not bank.allowed(name, n)
    with pytest.raises(ValueError):
        bank.write(name, n)
    assert bank.read() == {}


def test_no_more_than_six_providers(rig):
    for i in range(bank.MAX_NAMES):
        bank.write(f"p{i}", 1)
    with pytest.raises(ValueError):
        bank.write("one-more", 1)
    bank.write("p0", 3)  # an existing row can still change
    assert bank.read()["p0"] == 3


def test_a_damaged_file_reads_as_empty_and_bad_rows_are_dropped(rig):
    path = state_dir() / bank.STATE_FILE
    path.write_text("not json")
    assert bank.read() == {}
    path.write_text(json.dumps({"claude": {"n": 1}, "BAD": {"n": 1}, "codex": {"n": "2"}, "x": 5}))
    assert bank.read() == {"claude": 1}


def test_agents_are_told_only_when_a_reset_is_saved(rig):
    assert bank.hint() == ""
    bank.write("claude", 0)
    assert bank.hint() == ""
    bank.write("claude", 1)
    bank.write("codex", 2)
    hint = bank.hint()
    assert "Claude 1, Codex 2" in hint and "owner's decision, never yours" in hint and "ctb decide ask" in hint


def test_the_session_hint_carries_the_bank(rig, monkeypatch):
    monkeypatch.setattr(hooks, "home_chat", lambda cfg: -1001234567890)
    assert hooks.board_hint(1, rig.cfg) == hooks.BOARD_HINT
    bank.write("codex", 2)
    assert hooks.board_hint(1, rig.cfg) == hooks.BOARD_HINT + " " + bank.hint()
    assert hooks.board_hint(2, rig.cfg) is None


def test_the_panel_shows_and_sets_the_bank(rig, server):
    status, data = post(server, name="claude", n=1)
    assert status == 200 and data["bank"][0] == {"name": "claude", "n": 1, "expires": None, "days": None}
    assert bank.read() == {"claude": 1}
    state = panel.build_state(rig.cfg, now=1000.0) if hasattr(panel, "build_state") else {}
    assert state.get("bank", [{}])[0] == {"name": "claude", "n": 1, "expires": None, "days": None}


@pytest.mark.parametrize("body", [{"name": "claude"}, {"n": 1}, {"name": "claude", "n": "1"}, {"name": "Claude", "n": 1},
                                  {"name": "claude", "n": 100}, {"name": "claude", "n": 1, "x": 1}])
def test_a_malformed_request_changes_nothing(rig, server, body):
    assert post(server, **body)[0] == 400 and bank.read() == {}


def test_the_route_needs_the_owners_login(rig, server):
    assert post(server, init=signed(user_id=999), name="claude", n=1)[0] == 403 and bank.read() == {}


def test_the_page_has_the_bank_card():
    assert 'post("/api/bank",body)' in panel.PAGE and "save({name:r.name,n:to},x)" in panel.PAGE
    for lang in panel.STRINGS.values():
        assert lang["bank"] and lang["bankHint"] and lang["bankUnset"]


# --- the expiry date of the saved resets -----------------------------------------------------------

TODAY = date(2030, 1, 11)
NEAR = (date.today() + timedelta(days=9)).isoformat()  # the CLI and the panel check against the real date
FAR = (date.today() + timedelta(days=bank.MAX_AHEAD_DAYS + 1)).isoformat()


def test_an_expiry_is_kept_next_to_the_count_and_shown_in_days(rig):
    bank.write("claude", 1, now=10.0)
    assert bank.set_expiry("claude", "2030-01-20", today=TODAY) == {"claude": "2030-01-20"}
    assert bank.read() == {"claude": 1} and bank.expiries() == {"claude": "2030-01-20"}
    saved = json.loads((state_dir() / bank.STATE_FILE).read_text())
    assert saved == {"claude": {"n": 1, "at": 10.0, "expires": "2030-01-20"}}  # count and time stay
    assert bank.view(today=TODAY)[0] == {"name": "claude", "n": 1, "expires": "2030-01-20", "days": 9}
    assert bank.view(today=date(2030, 1, 20))[0]["days"] == 0
    assert bank.view(today=date(2030, 1, 22))[0]["days"] == -2
    bank.set_expiry("claude", None)
    assert bank.expiries() == {} and bank.view()[0]["expires"] is None


@pytest.mark.parametrize("value", ["2030-02-30", "2030-1-20", "20300120", "2030-01-20T00:00", " 2030-01-20", "2031-02-16",
                                   "", "none", 20300120, True, ["2030-01-20"]])
def test_a_bad_or_far_date_is_refused(rig, value):
    bank.write("claude", 1)
    assert not bank.valid_expiry(value, today=TODAY)
    with pytest.raises(ValueError):
        bank.set_expiry("claude", value, today=TODAY)
    assert bank.expiries() == {}


def test_a_past_date_and_the_furthest_date_are_allowed(rig):
    bank.write("claude", 1)
    assert bank.valid_expiry("2031-02-15", today=TODAY)  # exactly 400 days ahead
    bank.set_expiry("claude", "2029-12-31", today=TODAY)
    assert bank.expiries() == {"claude": "2029-12-31"}


def test_an_expiry_needs_a_saved_reset(rig):
    with pytest.raises(ValueError, match="set a count first"):
        bank.set_expiry("claude", "2030-01-20", today=TODAY)
    bank.write("claude", 0)
    with pytest.raises(ValueError, match="set a count first"):
        bank.set_expiry("claude", "2030-01-20", today=TODAY)
    with pytest.raises(ValueError):
        bank.set_expiry("Bad Name", None)
    assert bank.read() == {"claude": 0} and bank.expiries() == {}


def test_other_writes_keep_the_expiry_and_zero_clears_it(rig):
    bank.write("claude", 1)
    bank.write("codex", 2)
    bank.set_expiry("claude", "2030-01-20", today=TODAY)
    bank.set_expiry("codex", "2030-02-01", today=TODAY)
    bank.write("codex", 3)  # a new count keeps its own date too
    bank.write("gemini", 1)
    assert bank.forget("gemini")
    assert bank.expiries() == {"claude": "2030-01-20", "codex": "2030-02-01"}
    bank.write("claude", 0)
    assert bank.expiries() == {"codex": "2030-02-01"}
    bank.write("claude", 1)  # a fresh reset starts without the old date
    assert bank.expiries() == {"codex": "2030-02-01"}


def test_a_damaged_date_in_the_file_is_ignored(rig):
    (state_dir() / bank.STATE_FILE).write_text(json.dumps({"claude": {"n": 1, "expires": "2030-02-30"},
                                                           "codex": {"n": 0, "expires": "2030-01-20"}}))
    assert bank.read() == {"claude": 1, "codex": 0} and bank.expiries() == {}


def test_the_hint_tells_agents_the_date_far_away(rig):
    bank.write("claude", 1)
    bank.write("codex", 2)
    bank.set_expiry("claude", "2030-02-01", today=TODAY)
    hint = bank.hint(today=TODAY)
    assert "Claude 1 (expires 2030-02-01, in 21 days), Codex 2." in hint and "owner's decision, never yours" in hint
    assert "use the current quota fully" not in hint and "past its date" not in hint


@pytest.mark.parametrize("today,shown", [(date(2030, 1, 13), "in 7 days"), (date(2030, 1, 19), "in 1 day"),
                                         (date(2030, 1, 20), "today")])
def test_the_hint_says_to_use_the_quota_fully_before_a_reset_expires(rig, today, shown):
    bank.write("claude", 1)
    bank.set_expiry("claude", "2030-01-20", today=TODAY)
    hint = bank.hint(today=today)
    assert f"Claude 1 (expires 2030-01-20, {shown})." in hint and "owner's decision, never yours" in hint
    assert "expires unused is lost" in hint and "use the current quota fully" in hint and "ctb decide ask" in hint
    assert len(hint) < 900  # the hint goes into every prompt


def test_the_hint_asks_to_update_the_bank_after_the_date(rig):
    bank.write("claude", 1)
    bank.set_expiry("claude", "2030-01-10", today=TODAY)
    hint = bank.hint(today=TODAY)
    assert "Claude 1 (expired 2030-01-10)." in hint and "tell the owner to update the reset bank" in hint
    assert "use the current quota fully" not in hint


def test_a_zero_count_with_no_reset_tells_nothing(rig):
    bank.write("claude", 0)
    assert bank.hint(today=TODAY) == ""


def test_the_cli_sets_and_clears_the_date(rig, capsys):
    from ctb import cli
    assert cli.main(["bank", "set", "claude", "1"]) == 0
    assert cli.main(["bank", "expire", "Claude", NEAR]) == 0
    assert bank.expiries() == {"claude": NEAR}
    assert f"Claude: 1 (expires {NEAR}, in 9 days)" in capsys.readouterr().out
    assert cli.main(["bank", "expire", "claude", "2030-02-30"]) == 1
    assert cli.main(["bank", "expire", "codex", NEAR]) == 1  # no saved reset there
    assert "set a count first" in capsys.readouterr().err
    assert cli.main(["bank", "expire", "claude", "none"]) == 0 and bank.expiries() == {}


def test_the_panel_sets_and_clears_the_date(rig, server):
    bank.write("claude", 1)
    status, data = post(server, name="claude", expires=NEAR)
    assert status == 200 and data["bank"][0]["expires"] == NEAR and isinstance(data["bank"][0]["days"], int)
    assert bank.read() == {"claude": 1}
    status, data = post(server, name="claude", expires=None)
    assert status == 200 and data["bank"][0]["expires"] is None and bank.expiries() == {}


@pytest.mark.parametrize("body", [{"name": "claude", "expires": "2030-02-30"}, {"name": "claude", "expires": "soon"}, {"name": "claude", "expires": FAR},
                                  {"name": "claude", "expires": 20300120}, {"name": "codex", "expires": NEAR},
                                  {"name": "Claude", "expires": NEAR}, {"expires": NEAR},
                                  {"name": "claude", "expires": NEAR, "n": 1},
                                  {"name": "claude", "expires": NEAR, "x": 1}])
def test_a_malformed_date_request_changes_nothing(rig, server, body):
    bank.write("claude", 1)
    assert post(server, **body)[0] == 400 and bank.expiries() == {} and bank.read() == {"claude": 1}


def test_the_page_offers_the_date_in_both_languages():
    assert 'd.type="date"' in panel.PAGE and "save({name:r.name,expires:d.value||null}" in panel.PAGE
    assert "save({name:r.name,expires:null},k)" in panel.PAGE
    for lang in panel.STRINGS.values():
        for key in ("bankExpires", "bankClear", "bankLeft", "bankLeft1", "bankToday", "bankExpired"):
            assert lang[key].strip(), key
        assert "#" in lang["bankLeft"]
