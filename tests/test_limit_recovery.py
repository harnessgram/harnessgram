import json

from ctb import accounts, replay
from ctb.config import state_dir, write_private


HIT = 2_000_000.0


def save(name, value):
    write_private(state_dir() / name, json.dumps(value))


def limits():
    return json.loads((state_dir() / "account-limits.json").read_text())


def usage(at, five=12, weekly=34):
    return {"at": at, "five_hour": five, "weekly": weekly,
            "five_hour_resets": HIT + 3600, "weekly_resets": HIT + 7200}


def test_newer_below_limit_usage_clears_hit():
    accounts.note_limit("main", HIT)
    save("claude-usage-main.json", usage("1970-01-24T03:33:21Z"))

    assert accounts.used("main", HIT + 2) == 34
    assert "main" not in limits()


def test_older_usage_keeps_hit():
    accounts.note_limit("main", HIT)
    save("claude-usage-main.json", usage("1970-01-24T03:33:19Z"))

    assert accounts.used("main", HIT + 2) == 100
    assert limits()["main"] == HIT


def test_successful_turn_clears_hit():
    accounts.note_limit("main", HIT)

    replay.turn_ended("session", "Done.", HIT + 1)

    assert "main" not in limits()


def test_no_newer_evidence_keeps_account_skipped():
    accounts.note_limit("main", HIT)

    assert accounts.used("main", HIT + 1) == 100
    assert limits()["main"] == HIT
