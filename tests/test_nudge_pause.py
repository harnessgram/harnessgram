"""Unsent wakes must not accumulate or erase owner text; retries resume in ten minutes."""
import json
import pytest

from ctb import hooks, inbox as I
from ctb.config import Config, write_private
from test_heads_up_notice import Pane


@pytest.fixture
def wake(monkeypatch):
    pane = Pane(tip_has_keys=True)  # Enter is swallowed by the notice.
    monkeypatch.setattr(I, "_tmux", pane)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I.time, "monotonic", iter(range(10000)).__next__)
    monkeypatch.setattr(I, "pane_runs_session", lambda *a, **kw: True)
    monkeypatch.setattr(I, "recently_moving", lambda _: False)
    return pane


def keys(pane):
    return [a for a in pane.calls if a[0] == "send-keys"]


def test_unsent_wake_is_taken_back_and_retries_pause(wake, monkeypatch):
    monkeypatch.setattr(I.time, "time", lambda: 1000)
    assert not I.typed_nudge("s", "%1", settle=0)
    assert wake.box == "" and not wake.sent
    assert I._nudge_pause_path("s").read_text() == "1000"
    assert not I.nudge_recently_typed("s")
    sent = keys(wake)
    for now in (1001, 1031, 1599):
        monkeypatch.setattr(I.time, "time", lambda: now)
        assert not I.typed_nudge("s", "%1", resumed=True, settle=0)
    assert keys(wake) == sent
    # The same process-independent timestamp expires without any owner action.
    monkeypatch.setattr(I.time, "time", lambda: 1600)
    wake.tip_has_keys = False
    assert I.typed_nudge("s", "%1", settle=0)
    assert wake.sent == [I.NUDGE_TEXT] and not I.nudge_paused("s")


def test_pause_is_per_session_and_drafts_do_not_create_or_extend_it(wake, monkeypatch):
    monkeypatch.setattr(I.time, "time", lambda: 1000)
    assert not I.typed_nudge("s", "%1", settle=0)
    stamp = I._nudge_pause_path("s").read_text()
    wake.tip_has_keys = False
    assert I.typed_nudge("other", "%1", settle=0)
    assert I.nudge_paused("s") and not I.nudge_paused("other")
    monkeypatch.setattr(I.time, "time", lambda: 1600)
    wake.box = "owner draft"
    sent = keys(wake)
    assert not I.typed_nudge("s", "%1", settle=0)
    assert not I.nudge_paused("s") and keys(wake) == sent
    assert I._nudge_pause_path("s").read_text() == stamp


@pytest.mark.parametrize("box", ["owner draft " + I.NUDGE_TEXT, I.NUDGE_TEXT + " owner edit", "partial", None])
def test_failed_submission_never_erases_anything_but_exact_wake(wake, monkeypatch, box):
    def not_submitted(*a, **kw):
        wake.box = box
        return False

    monkeypatch.setattr(I, "submitted", not_submitted)
    assert not I.typed_nudge("s", "%1", settle=0)
    assert wake.box == box and I.nudge_paused("s")
    assert not any("BSpace" in a or "C-u" in a for a in keys(wake))


@pytest.mark.parametrize("failure", ["send", "enter", "not_ready", "changed_pane"])
def test_wake_failure_keeps_pause_and_only_cleans_its_own_text(wake, monkeypatch, failure):
    original = wake.__call__
    changed = []

    def tmux(*args):
        result = original(*args)
        if args[0] == "send-keys" and "-l" in args:
            changed.append(True)
            if failure == "send":
                result.returncode = 1
        if failure == "enter" and "Enter" in args:
            result.returncode = 1
        return result

    monkeypatch.setattr(I, "_tmux", tmux)
    if failure == "not_ready":
        read = I.input_box
        monkeypatch.setattr(I, "input_box", lambda p: (read(p)[0], not changed))
    if failure == "changed_pane":
        monkeypatch.setattr(I, "pane_runs_session", lambda *a: not changed)
    if failure in ("send", "enter"):
        with pytest.raises(OSError):
            I.typed_nudge("s", "%1", settle=0)
    else:
        assert not I.typed_nudge("s", "%1", settle=0)
    assert I.nudge_paused("s")
    assert wake.box == (I.NUDGE_TEXT if failure == "changed_pane" else "")


@pytest.mark.parametrize("timestamp", ["1000", "junk", "nan", "inf", "2000"])
def test_pause_never_exceeds_600_seconds(timestamp):
    write_private(I._nudge_pause_path("s"), timestamp)
    assert I.NUDGE_PAUSE == 600
    assert I.nudge_paused("s", now=1599) == (timestamp == "1000")
    assert not I.nudge_paused("s", now=1600)


@pytest.mark.parametrize("prompt,extra,clear", [
    ("hello", {}, True), ("hello", {"agent_id": "worker"}, False),
    ("", {}, False), (I.NUDGE_TEXT, {}, False),
    ("# Autonomous loop tick", {}, False), ("<task-notification>done</task-notification>", {}, False),
    ("bridge command", {}, False),
])
def test_only_real_owner_prompt_clears_pause(wake, monkeypatch, prompt, extra, clear):
    monkeypatch.setattr(hooks, "load_config", lambda: Config())
    monkeypatch.setattr(hooks, "empty_wake", lambda *a: False)
    monkeypatch.setattr(hooks, "_direct", lambda *a: None)
    monkeypatch.setattr(hooks, "prompt_output", lambda *a: None)
    monkeypatch.setattr(hooks, "spawn", lambda *a, **kw: None)
    write_private(I._nudge_pause_path("s"), repr(I.time.time()))
    I.typed_mark("bridge command")
    hooks.hook_prompt(json.dumps(dict(session_id="s", prompt=prompt, **extra)))
    assert I.nudge_paused("s") == (not clear)
