"""A long turn with no progress card is asked for one, once; a card left untouched is asked for an update."""
import pytest

from ctb import cards, config, hooks

S = "a1b2c3d4-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(hooks, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(cards, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(config, "state_dir", lambda: tmp_path)
    return tmp_path


def test_a_long_turn_without_a_card_is_asked_once():
    assert hooks.progress_nudge(S, now=1000.0) == ""                       # no turn known: nothing
    hooks.turn_started(S, now=1000.0)
    assert hooks.progress_nudge(S, now=1000.0 + hooks.PROGRESS_AFTER - 1) == ""
    asked = hooks.progress_nudge(S, now=1000.0 + hooks.PROGRESS_AFTER)
    assert "ctb card progress" in asked and "--step" in asked
    assert hooks.progress_nudge(S, now=1000.0 + hooks.PROGRESS_AFTER + 5) == ""
    assert hooks.progress_nudge(S, now=1000.0 + 10 * hooks.PROGRESS_STALE) == ""   # it chose not to: left alone
    hooks.turn_started(S, now=90000.0)                                     # a new prompt starts the count again
    assert hooks.progress_nudge(S, now=90000.0 + hooks.PROGRESS_AFTER) == hooks.PROGRESS_NUDGE


def test_a_fresh_card_is_enough_and_a_stale_one_is_asked_for_an_update():
    hooks.turn_started(S, now=1000.0)
    rec = cards.publish(S, -100, 7, "progress", "Build", steps=["a", "b", "c"], at=1, percent=30, now=1100.0)
    assert hooks.progress_nudge(S, now=1000.0 + hooks.PROGRESS_AFTER) == ""
    late = 1101.0 + hooks.PROGRESS_STALE
    assert hooks.progress_nudge(S, now=late) == hooks.PROGRESS_UPDATE.format(id=rec["id"])
    assert hooks.progress_nudge(S, now=late + 60) == ""                    # not again right away
    assert not hooks.progress_nudge(S, now=late + hooks.PROGRESS_STALE)  # once until the card changes


def test_the_quiet_level_turns_the_nudge_off():
    config.set_activity_level("quiet")
    hooks.turn_started(S, now=1000.0)
    assert hooks.progress_nudge(S, now=1000.0 + hooks.PROGRESS_AFTER) == ""
