"""The home radio comes back by itself after the Mac went down while it was on, and only then.

Guards: a shutdown keeps the intent, an explicit stop clears it, the listener's startup resumes only when the intent
is set and the job is absent, failures back off and give up, and the YouTube staff radio is never started this way.
launchd, the account helper and every secret are faked; nothing here touches a real job."""
import inspect
import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN
from ctb import inbox, radio, radio_youtube
from ctb.config import Config

OWNER = 111
GROUP = -1001234


def _cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP,
                  radio={"telethon_python": "/usr/bin/python3", "account_api_command": ["x"],
                         "account_session_command": ["y"], "rtmp_url_command": ["u"], "rtmp_key_command": ["k"]})


@pytest.fixture
def launchd(monkeypatch, tmp_path):
    """A fake launchd: `job["loaded"]` is whether the radio job exists; bootstrap loads it and bootout removes it."""
    job = {"loaded": False, "pid": None, "calls": []}
    monkeypatch.setattr(radio.sys, "platform", "darwin")
    monkeypatch.setattr(radio, "job_pid", lambda: job["pid"])
    monkeypatch.setattr(radio, "plist_path", lambda: tmp_path / "agents" / "radio.plist")
    monkeypatch.setattr(radio, "_job_loaded", lambda: job["loaded"])

    def launchctl(*args):
        job["calls"].append(args[0])
        if args[0] == "bootstrap":
            job["loaded"] = True
        elif args[0] == "bootout":
            job["loaded"] = False
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(radio, "_launchctl", launchctl)
    monkeypatch.setattr(radio, "call_helper", lambda s, action, call=None, **k: (
        {"ok": True, "owned": True, "created": True, "call": {"id": 5, "access_hash": 6}} if action == "ensure"
        else {"ok": True, "ended": True}))
    import ctb.policy
    monkeypatch.setattr(ctb.policy, "on", lambda: True)
    return job


def _record():
    return json.loads((radio.folder() / radio.WANTED).read_text())


def _no_sleep(seen=None):
    return lambda s: seen.append(s) if seen is not None else None


# --- the intent: set by a start, kept by a shutdown, cleared only by a stop --------------------------------

def test_a_start_sets_the_intent(launchd):
    assert not radio.wanted()
    assert radio.start(_cfg()) == "radio_started"
    assert radio.wanted() and _record()["wanted"] is True
    assert radio.status(_cfg())["wanted"] is True


def test_a_shutdown_keeps_the_intent(launchd, monkeypatch):
    """launchd's SIGTERM ends the job cleanly (pipeline "stopped", exit 0); nothing on that path touches the intent."""
    radio.start(_cfg())
    monkeypatch.setattr(radio, "make_bed", lambda kind, db: bytes(4))
    monkeypatch.setattr(radio.Station, "_run", lambda self: 0)
    assert radio.run_station(_cfg()) == 0
    launchd["loaded"] = False  # the reboot: the job is gone, nobody called stop
    assert radio.wanted() and radio.status(_cfg())["on"] is False


def test_an_older_radio_without_the_file_counts_by_its_state(launchd):
    radio._write(radio.folder() / "state.json", {"on": True, "owned": True, "call": {"id": 5, "access_hash": 6}})
    assert radio.wanted()
    radio._write(radio.folder() / "state.json", {"on": False})
    assert not radio.wanted()


def test_an_explicit_stop_clears_the_intent(launchd):
    radio.start(_cfg())
    assert radio.stop(_cfg()) == "radio_stopped"
    assert not radio.wanted() and _record()["wanted"] is False
    assert radio.status(_cfg())["wanted"] is False


def test_giving_up_clears_the_intent(launchd, monkeypatch):
    radio.start(_cfg())
    monkeypatch.setattr(radio, "_end_call", lambda s, call=None: True)
    monkeypatch.setattr(radio, "_bootout_self", lambda: None)
    radio.give_up(radio.settings(_cfg()))
    assert not radio.wanted()


# --- the listener's startup resume --------------------------------------------------------------------------

def test_resume_starts_a_wanted_radio_whose_job_is_gone(launchd):
    radio.set_wanted(True)
    slept = []
    assert radio.resume_after_boot(_cfg(), sleep=_no_sleep(slept)) == "resumed"
    assert launchd["loaded"] and "bootstrap" in launchd["calls"]
    assert slept == [radio.RESUME_DELAY]  # one wait for the network, then straight on
    assert radio.wanted() and "resume_at" not in _record()  # a success starts a clean budget


def test_resume_does_nothing_when_the_owner_stopped_it(launchd):
    radio.start(_cfg())
    radio.stop(_cfg())
    launchd["calls"].clear()
    assert radio.resume_after_boot(_cfg(), sleep=_no_sleep()) == "off"
    assert launchd["calls"] == [] and not launchd["loaded"]


def test_resume_does_nothing_when_never_wanted(launchd):
    assert radio.resume_after_boot(_cfg(), sleep=_no_sleep()) == "off"
    assert launchd["calls"] == []


@pytest.mark.parametrize("pid", [4242, None])
def test_resume_never_restarts_a_radio_that_is_running(launchd, pid):
    radio.set_wanted(True)
    launchd.update(loaded=True, pid=pid)  # running, or loaded and waiting for launchd's throttle
    assert radio.resume_after_boot(_cfg(), sleep=_no_sleep()) == "running"
    assert launchd["calls"] == []


def test_a_stop_during_the_wait_is_not_undone(launchd):
    radio.start(_cfg())
    launchd["loaded"] = False
    launchd["calls"].clear()

    def stop_while_waiting(seconds):
        radio.set_wanted(False)  # the owner's `radio stop` lands while the resume sleeps
    assert radio.resume_after_boot(_cfg(), sleep=stop_while_waiting) == "off"
    assert launchd["calls"] == [] and not launchd["loaded"]


def test_resume_waits_while_mirroring_is_off(launchd, monkeypatch):
    import ctb.policy
    radio.set_wanted(True)
    states = iter([False, False, True])
    monkeypatch.setattr(ctb.policy, "on", lambda: next(states))
    slept = []
    assert radio.resume_after_boot(_cfg(), sleep=_no_sleep(slept)) == "resumed"
    assert slept == [radio.RESUME_DELAY, 60, 60]


def test_failed_resumes_back_off_then_give_up(launchd, monkeypatch):
    radio.set_wanted(True)
    tries = []

    def failing(cfg, s):
        tries.append(True)
        raise radio.RadioError("radio_call_failed")
    monkeypatch.setattr(radio, "_start_locked", failing)
    slept = []
    assert radio.resume_after_boot(_cfg(), sleep=_no_sleep(slept)) == "gave_up"
    assert tries == [True] * radio.MAX_RESUME
    assert slept == [radio.RESUME_DELAY] + [w for w in radio.RESUME_BACKOFF if w]  # the waits grow
    assert not radio.wanted()
    status = json.loads((radio.folder() / "status.json").read_text())
    assert status["gave_up"] is True and status["pipeline"] == "failed"
    assert radio.resume_after_boot(_cfg(), sleep=_no_sleep()) == "off"  # and the next listener start leaves it alone
    assert len(tries) == radio.MAX_RESUME


def test_the_failure_budget_survives_a_listener_restart(launchd, monkeypatch):
    radio.set_wanted(True, [1000.0] * radio.MAX_RESUME)
    monkeypatch.setattr(radio, "_start_locked", lambda *a, **k: pytest.fail("a radio that gave up was started again"))
    assert radio.resume_after_boot(_cfg(), sleep=_no_sleep(), clock=lambda: 1000.0 + 5) == "gave_up"
    assert not radio.wanted()


def test_old_failures_do_not_count(launchd):
    radio.set_wanted(True, [1.0] * radio.MAX_RESUME)  # long ago
    assert radio.resume_after_boot(_cfg(), sleep=_no_sleep(), clock=lambda: 1.0 + radio.RESUME_WINDOW + 1) == "resumed"


def test_an_unconfigured_radio_is_left_alone(launchd, monkeypatch):
    radio.set_wanted(True)
    bare = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP)  # no stream commands configured
    slept = []
    assert radio.resume_after_boot(bare, sleep=_no_sleep(slept)) == "unsupported"
    assert slept == [radio.RESUME_DELAY]


def test_nothing_resumes_off_the_mac(launchd, monkeypatch):
    radio.set_wanted(True)
    monkeypatch.setattr(radio.sys, "platform", "linux")
    assert radio.resume_after_boot(_cfg(), sleep=_no_sleep()) == "unsupported"
    assert launchd["calls"] == []


# --- the YouTube staff radio is never started this way ------------------------------------------------------

def test_youtube_is_never_auto_started(launchd, monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("the resume touched the YouTube staff radio")
    for name in ("switch", "spawn", "put_start_request", "take_start_request", "connect"):
        monkeypatch.setattr(radio_youtube, name, forbidden)
    radio_youtube._write(radio_youtube.folder() / "state.json", {"on": True})  # its own state says nothing here
    radio.set_wanted(True)
    assert radio.resume_after_boot(_cfg(), sleep=_no_sleep()) == "resumed"
    assert not (radio_youtube.folder() / radio.WANTED).exists()
    for fn in (radio.resume_after_boot, radio.resume_in_background, radio._resume_gave_up):
        code = inspect.getsource(fn)
        assert "radio_youtube." not in code and "import radio_youtube" not in code and "from . import radio_y" not in code


# --- the listener hook --------------------------------------------------------------------------------------

def test_the_listener_calls_the_resume_once_at_startup():
    src = inspect.getsource(inbox.serve)
    assert src.count("resume_in_background") == 1


def test_no_thread_while_the_radio_is_not_wanted(launchd, monkeypatch):
    monkeypatch.setattr(radio.threading, "Thread", lambda *a, **k: pytest.fail("a thread for an unwanted radio"))
    radio.resume_in_background(_cfg())


def test_the_background_resume_runs_the_resume(launchd, monkeypatch):
    radio.set_wanted(True)
    ran = []

    class Now:
        def __init__(self, target=None, **k):
            self.target = target

        def start(self):
            self.target()
    monkeypatch.setattr(radio.threading, "Thread", Now)
    monkeypatch.setattr(radio, "resume_after_boot", lambda cfg: ran.append(cfg) or "resumed")
    radio.resume_in_background(_cfg())
    assert len(ran) == 1


# --- races between the resume, start, stop and the job's giving up (everything under the control lock) -------

def _in_thread(fn):
    out = {}

    def work():
        out["value"] = fn()
    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t, out


def _blocked(thread):
    thread.join(0.4)
    return thread.is_alive()


def test_a_stop_landing_before_the_resume_decides_is_not_undone(launchd):
    radio.set_wanted(True)
    with radio._locked():  # a stop is in progress
        t, out = _in_thread(lambda: radio._resume_once(_cfg()))
        assert _blocked(t)  # the resume's decision waits for the lock; it has not written anything
        radio.set_wanted(False)
    t.join(5)
    assert out["value"] == "off" and launchd["calls"] == [] and not radio.wanted()


def test_a_radio_started_by_hand_during_the_wait_is_not_marked_failed(launchd):
    radio.set_wanted(True, [1000.0] * radio.MAX_RESUME)  # the budget is already used up
    with radio._locked():  # the owner's start is in progress
        t, out = _in_thread(lambda: radio._resume_once(_cfg(), clock=lambda: 1005.0))
        assert _blocked(t)
        launchd["loaded"] = True  # ... and it finished: the job is up
        radio.set_wanted(True)
    t.join(5)
    assert out["value"] == "running" and radio.wanted()
    assert not (radio.folder() / "status.json").exists()  # no "gave_up" written over a live radio


def test_a_stop_waiting_behind_a_start_wins(launchd):
    with radio._locked():  # a start holds the lock
        t, out = _in_thread(lambda: radio.stop(_cfg()))
        assert _blocked(t)
        radio.set_wanted(True)  # the start writes its intent last ...
        launchd["loaded"] = True
    t.join(5)
    assert out["value"] == "radio_stopped"
    assert not radio.wanted() and not launchd["loaded"]  # ... and the stop, which came after, clears it: off and not wanted


def test_the_job_giving_up_right_after_bootstrap_is_not_overwritten(launchd, monkeypatch):
    monkeypatch.setattr(radio, "_end_call", lambda s, call=None: True)
    monkeypatch.setattr(radio, "_bootout_self", lambda: None)
    job = {}
    real = radio._launchctl

    def with_job(*args):
        r = real(*args)
        if args[0] == "bootstrap":  # the fresh job has no stream key and gives up at once
            job["t"] = threading.Thread(target=radio.give_up, args=(radio.settings(_cfg()),), daemon=True)
            job["t"].start()
            time.sleep(0.3)  # it needs the control lock the start still holds
        return r
    monkeypatch.setattr(radio, "_launchctl", with_job)
    assert radio.start(_cfg()) == "radio_started"
    job["t"].join(5)
    assert not job["t"].is_alive() and not radio.wanted()


def test_a_job_already_gone_after_bootstrap_is_not_on_and_not_wanted(launchd, monkeypatch):
    real = radio._launchctl

    def vanishing(*args):
        r = real(*args)
        if args[0] == "bootstrap":
            launchd["loaded"] = False
        return r
    monkeypatch.setattr(radio, "_launchctl", vanishing)
    with pytest.raises(radio.RadioError) as e:
        radio.start(_cfg())
    assert e.value.key == "radio_job_failed" and not radio.wanted()


def test_a_stop_whose_intent_cannot_be_saved_says_so(launchd, monkeypatch):
    radio.start(_cfg())
    real = radio._write

    def failing(path, data):
        if path.name == radio.WANTED:
            raise OSError("disk full")
        return real(path, data)
    monkeypatch.setattr(radio, "_write", failing)
    with pytest.raises(radio.RadioError) as e:
        radio.stop(_cfg())
    assert e.value.key == "radio_stop_not_saved"
    assert not launchd["loaded"]  # the radio itself is still stopped
    assert radio.t("radio_stop_not_saved") and radio.t("radio_stop_not_saved", lang="he")


def test_only_a_real_boolean_counts(launchd):
    path = radio.folder() / radio.WANTED
    state = radio.folder() / "state.json"
    radio._write(state, {"on": False})
    path.write_text(json.dumps({"wanted": "false"}))  # a string is not an answer
    assert not radio.wanted()
    path.write_text(json.dumps({"wanted": "true"}))
    assert not radio.wanted()
    path.write_text(json.dumps({"wanted": 1}))
    assert not radio.wanted()
    path.write_text(json.dumps({"wanted": False}))  # an explicit stop outranks the older state
    radio._write(state, {"on": True})
    assert not radio.wanted()


def test_a_corrupt_file_falls_back_to_the_state(launchd):
    path = radio.folder() / radio.WANTED
    radio._write(radio.folder() / "state.json", {"on": True})
    path.write_text("{not json")
    assert radio.wanted()
    path.write_text("[]")
    assert radio.wanted()
    radio._write(radio.folder() / "state.json", {"on": False})
    assert not radio.wanted()


@pytest.mark.parametrize("answer,expected", [
    (SimpleNamespace(returncode=0, stdout="", stderr=""), True),
    (SimpleNamespace(returncode=113, stdout="", stderr='Could not find service "x" in domain'), False),
    (SimpleNamespace(returncode=1, stdout="", stderr="launchctl is busy"), None),
    (subprocess.TimeoutExpired("launchctl", 30), None),
    (OSError("no launchctl"), None),
])
def test_launchctl_print_failures_are_unknown_not_absent(monkeypatch, answer, expected):
    def fake(*args):
        if isinstance(answer, BaseException):
            raise answer
        return answer
    monkeypatch.setattr(radio, "_launchctl", fake)
    assert radio._job_loaded() is expected


def test_an_unknown_job_state_does_nothing_and_never_boots_out(launchd, monkeypatch):
    radio.set_wanted(True)
    monkeypatch.setattr(radio, "_job_loaded", lambda: None)
    assert radio.resume_after_boot(_cfg(), sleep=_no_sleep()) == "unknown"
    assert launchd["calls"] == [] and radio.wanted()
    assert radio._resume_attempts(time.time()) == []  # nothing was tried, so nothing was used up


def test_a_resume_list_of_the_wrong_shape_counts_as_no_attempts(tmp_path, monkeypatch):
    from ctb import radio
    monkeypatch.setattr(radio, "_read_wanted", lambda: {"wanted": True, "resume_at": None})
    assert radio._resume_attempts(1000.0) == []


def test_stop_boots_out_when_launchd_cannot_say(monkeypatch):
    from ctb import radio
    calls = []
    monkeypatch.setattr(radio, "_job_loaded", lambda: None)
    monkeypatch.setattr(radio, "_launchctl", lambda *a: calls.append(a))
    monkeypatch.setattr(radio.time, "sleep", lambda s: None)
    monkeypatch.setattr(radio, "plist_path", lambda: radio.Path("/nonexistent/radio.plist"))
    radio._unload()
    assert calls and calls[0][0] == "bootout"
