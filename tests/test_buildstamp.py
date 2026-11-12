"""Long-running services and newly installed code (a stale keyboard kept coming back after an upgrade).
Nothing replaces itself: `harnessgram install` restarts what it manages, `doctor` names what still runs older
code. No test restarts or execs anything real: launchctl, systemctl and the stamps are injected."""
import json
import os
import re
from pathlib import Path

import pytest

from ctb import buildstamp as B, config as C, doctor, heartbeat, install as I, panel, setup_page, wake


# --- the stamp: read-only identity ----------------------------------------------------------------------------

def _site(tmp_path, record=b"ctb/__init__.py,sha256=aaa,1\n"):
    site = tmp_path / "lib" / "site-packages"
    (site / "ctb").mkdir(parents=True)
    if record is not None:
        info = site / "harnessgram-0.1.0.dist-info"
        info.mkdir()
        (info / "RECORD").write_bytes(record)
    return site / "ctb"


def test_an_installed_copy_is_known_by_its_record(tmp_path):
    pkg = _site(tmp_path)
    first = B.read(pkg)
    assert first and first.startswith("r") and B.read(pkg) == first  # the same code: the same stamp
    rec = pkg.parent / "harnessgram-0.1.0.dist-info" / "RECORD"
    rec.write_bytes(b"ctb/__init__.py,sha256=bbb,1\n")  # new code
    assert B.read(pkg) not in (None, first)


def test_a_missing_record_where_one_is_expected_is_unknown(tmp_path):
    pkg = _site(tmp_path, record=None)  # being replaced right now
    assert B.read(pkg) is None
    (pkg.parent / "harnessgram-0.1.0.dist-info").mkdir()
    assert B.read(pkg) is None  # the folder without its RECORD yet
    (pkg.parent / "harnessgram-0.2.0.dist-info").mkdir()
    assert B.read(pkg) is None  # two copies at once


def test_a_source_checkout_is_known_by_its_file_names_and_contents(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "_SOURCE_STAMPS", {})
    pkg = tmp_path / "src" / "ctb"
    pkg.mkdir(parents=True)
    (pkg / "a.py").write_text("x = 1\n")
    first = B.read(pkg)
    assert first and first.startswith("s") and B.read(pkg) == first
    os.utime(pkg / "a.py", (1, 1))  # a touch (or an identical file restored) is the same code
    B._SOURCE_STAMPS.clear()
    assert B.read(pkg) == first
    (pkg / "a.py").write_text("x = 22\n")  # new content
    assert B.read(pkg) == first  # computed once per process: the cost is paid once
    B._SOURCE_STAMPS.clear()
    assert B.read(pkg) != first


def test_this_package_has_a_stamp():
    assert B.read()


# --- what each service started with ---------------------------------------------------------------------------

def test_a_service_running_older_code_is_named_and_a_clean_exit_clears_it():
    B.record_running("heartbeat", stamp="old", pid=os.getpid())
    B.record_running("panel", stamp="new", pid=os.getpid())
    assert B.stale(current="new") == ["heartbeat"]
    B.clear_running("heartbeat", pid=os.getpid())
    assert B.stale(current="new") == []


def test_unknown_stamps_and_dead_processes_never_warn():
    B.record_running("listener", stamp=None, pid=os.getpid())  # it could not tell its code at start
    B.record_running("heartbeat", stamp="old", pid=999999999)  # gone without a clean exit
    assert B.stale(current="new", alive=lambda pid, start: pid == os.getpid()) == []
    B.record_running("panel", stamp="old", pid=os.getpid())
    assert B.stale(current="", alive=lambda pid, start: True) == []  # the installed code cannot be told


def test_a_malformed_record_file_never_breaks_doctor_and_never_warns():
    path = C.private_dir(C.state_dir()) / B.RUNNING
    for text in ("not json {", "[1, 2, 3]", json.dumps({"listener": [1]}), json.dumps({"panel": "x"}),
                 json.dumps({"heartbeat": {"stamp": "old", "pid": 10 ** 30, "start": "x"}}),
                 json.dumps({"heartbeat": {"stamp": "old", "pid": "123", "start": "x"}}),
                 json.dumps({"heartbeat": {"stamp": "old", "pid": True, "start": "x"}}),
                 json.dumps({"heartbeat": {"stamp": "old", "pid": -5, "start": "x"}}),
                 json.dumps({"heartbeat": {"stamp": "old", "start": "x"}}),
                 json.dumps({"heartbeat": {"stamp": 7, "pid": os.getpid(), "start": "x"}})):
        path.write_text(text)
        assert B.stale(current="new") == [], text
        assert doctor.check_running_code() == [], text
    for pid in (10 ** 30, 2 ** 22 + 1, 0, -1, "1", None, 1.5, True):
        assert B._alive(pid, "Mon Jan  1 00:00:00 2026") is False, pid  # no OverflowError, no TypeError


def test_a_reused_pid_is_not_the_service():
    """The service crashed and the system gave its pid to another process: no warning."""
    B.record_running("heartbeat", stamp="old", pid=os.getpid())
    rows = json.loads((C.state_dir() / B.RUNNING).read_text())
    assert rows["heartbeat"]["start"]  # its start time was recorded next to the pid
    assert B.stale(current="new") == ["heartbeat"]  # this very process: still the one recorded
    rows["heartbeat"]["start"] = "Thu Jan  1 00:00:00 1970"  # another process holds the pid now
    (C.state_dir() / B.RUNNING).write_text(json.dumps(rows))
    assert B.stale(current="new") == []
    rows["heartbeat"]["start"] = ""  # the start time could not be read when it started: unknown
    (C.state_dir() / B.RUNNING).write_text(json.dumps(rows))
    assert B.stale(current="new") == []


def test_a_newer_copy_is_not_cleared_by_the_old_one():
    B.record_running("listener", stamp="new", pid=4242)
    B.clear_running("listener", pid=os.getpid())  # the old copy exits after the new one wrote its row
    rows = json.loads((C.state_dir() / B.RUNNING).read_text())
    assert rows["listener"]["stamp"] == "new" and rows["listener"]["pid"] == 4242


def test_doctor_warns_per_stale_service_and_names_the_fix():
    out = doctor.check_running_code(stale=lambda: ["heartbeat", "radio"])
    assert out[0] == ("warn", "heartbeat still runs the previous version. Fix: run harnessgram install")
    assert out[1] == ("warn", "the radio still runs the previous version; turn it off and on")
    assert doctor.check_running_code(stale=lambda: []) == []


def test_each_long_running_service_records_and_clears_its_code(monkeypatch):
    seen = []
    monkeypatch.setattr(B, "record_running", lambda name, **k: seen.append(("start", name)))
    monkeypatch.setattr(B, "clear_running", lambda name, **k: seen.append(("stop", name)))
    monkeypatch.setattr(heartbeat, "run_once", lambda cfg, bot=None: 0)
    monkeypatch.setattr(heartbeat.revive, "revive_in_background", lambda: None)
    monkeypatch.setattr(heartbeat.revive, "revived", __import__("threading").Event())  # no snapshot -> no tmux
    monkeypatch.setattr(heartbeat.revive, "snapshot", lambda *a, **k: None)
    monkeypatch.setattr(heartbeat.Bot, "from_config", classmethod(lambda cls, cfg: object()))

    class Stop(Exception):
        pass

    def sleep(s):
        raise Stop
    with pytest.raises(Stop):
        heartbeat.run(C.Config(bot_token="1:x", owner_chat_id=7), sleep_fn=sleep)
    assert seen == [("start", "heartbeat"), ("stop", "heartbeat")]
    seen.clear()
    assert heartbeat.run(C.Config(bot_token="1:x", owner_chat_id=7), once=True) == 0
    assert seen == []  # a single round is no service

    class Server:
        def __init__(self, addr, cfg):
            pass

        def serve_forever(self):
            raise Stop

    monkeypatch.setattr(panel, "PanelServer", Server)
    with pytest.raises(Stop):
        panel.serve(C.Config())
    assert seen == [("start", "panel"), ("stop", "panel")]
    for module, name in (("inbox", "listener"), ("radio", "radio")):
        source = Path(__import__(f"ctb.{module}", fromlist=["x"]).__file__).read_text(encoding="utf-8")
        assert f'buildstamp.record_running("{name}")' in source and f'buildstamp.clear_running("{name}")' in source


def test_nothing_restarts_or_replaces_itself():
    """The removed design: services exiting on a stamp change, a waiter re-execing itself. None of it is back."""
    for module in ("inbox", "heartbeat", "panel", "wake", "radio"):
        source = Path(__import__(f"ctb.{module}", fromlist=["x"]).__file__).read_text(encoding="utf-8")
        assert "execv" not in source and "CTB_WAKE_RESUME" not in source and ".changed()" not in source, module


def test_the_waiter_posts_no_telegram_message_with_its_own_code():
    """A waiter can outlive an install by hours. Its only Telegram sends already run in fresh processes of the
    installed ctb: the 👌 (spawn_delivered), a cold compaction (spawn_auto_compact) and the status refresh
    (`refresh-status`, spawned by the SessionStart hook). refresh_status itself is called only by that command."""
    source = Path(wake.__file__).read_text(encoding="utf-8")
    body = source[source.index("def wait_for_message"):source.index("def _silence_stderr")]
    assert "refresh_status(" not in body and "status_message(" not in body and "Bot." not in body
    assert "spawn_delivered(" in body
    calls = [m.start() for m in re.finditer(r"\brefresh_status\(", source)]
    assert len(calls) == 1  # only its own definition
    assert '"refresh-status"' in source  # started as a fresh process


# --- install is the only restarter ------------------------------------------------------------------------------

@pytest.fixture
def svc(tmp_path, monkeypatch):
    home = tmp_path / "svc-home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setattr(I, "HOME", home)
    calls = []
    jobs = {}  # label -> pid (None: loaded but not running)
    monkeypatch.setattr(I, "_sh", lambda *cmd: calls.append(cmd) or 0)

    def out(*cmd):
        calls.append(cmd)
        label = cmd[-1].rsplit("/", 1)[-1]
        if label not in jobs:
            return 113, ""
        pid = jobs[label]
        return 0, f"gui/501/{label} = {{\n\tstate = {'running' if pid else 'not running'}\n" + \
            (f"\tpid = {pid}\n" if pid else "") + "}\n"

    monkeypatch.setattr(I, "_out", out)

    def job(label, pid):
        (home / "Library" / "LaunchAgents" / f"{label}.plist").write_text("x")
        jobs[label] = pid
    return calls, job


def test_install_restarts_listener_heartbeat_and_a_running_panel(svc, tmp_path, monkeypatch, capsys):
    calls, job = svc
    monkeypatch.setattr(I.sys, "platform", "darwin")
    job(I.PANEL_JOB, 4321)
    report = I.run_install(settings_file=tmp_path / "s.json")
    assert report.restarted == ["listener", "heartbeat", "panel"] and not report.radio_kept
    assert ("launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{I.PANEL_JOB}") in calls
    assert "restarted on the new code: listener, heartbeat, panel" in capsys.readouterr().out


def test_install_never_starts_a_stopped_panel(svc, tmp_path, monkeypatch):
    calls, job = svc
    monkeypatch.setattr(I.sys, "platform", "darwin")
    job(I.PANEL_JOB, None)  # loaded but stopped on purpose
    report = I.run_install(settings_file=tmp_path / "s.json")
    assert report.restarted == ["listener", "heartbeat"]
    assert not any(c[:2] == ("launchctl", "kickstart") for c in calls)


def test_install_leaves_a_running_radio_alone_and_says_so(svc, tmp_path, monkeypatch, capsys):
    calls, job = svc
    monkeypatch.setattr(I.sys, "platform", "darwin")
    job(I.RADIO_JOB, 777)
    report = I.run_install(settings_file=tmp_path / "s.json")
    assert report.radio_kept and "radio" not in report.restarted
    assert not any(c[:2] == ("launchctl", "kickstart") or (c[:2] == ("launchctl", "bootout") and
                                                            c[-1].endswith(".radio")) for c in calls)
    out = capsys.readouterr().out
    assert "the radio keeps its current code until it is next turned off and on" in out


def test_install_on_linux_restarts_listener_and_heartbeat_only(svc, tmp_path, monkeypatch):
    calls, job = svc
    monkeypatch.setattr(I.sys, "platform", "linux")
    report = I.run_install(settings_file=tmp_path / "s.json")
    assert report.restarted == ["listener", "heartbeat"]
    assert not any(c[0] == "launchctl" for c in calls)


def test_no_start_restarts_nothing(svc, tmp_path, monkeypatch):
    calls, job = svc
    monkeypatch.setattr(I.sys, "platform", "darwin")
    job(I.PANEL_JOB, 4321)
    assert I.run_install(settings_file=tmp_path / "s.json", start=False).restarted == [] and calls == []


def test_the_page_names_services_left_on_older_code_in_both_languages():
    for lang in ("en", "he"):
        assert "{names}" in setup_page.STRINGS[lang]["r_stale_code"]
        assert setup_page.STRINGS[lang]["r_stale_radio"].strip()
    assert "⁨{names}⁩" in setup_page.STRINGS["he"]["r_stale_code"]
    source = Path(setup_page.__file__).read_text(encoding="utf-8")
    assert '{"key": "r_stale_code", "warn": True, "cmd": "install", "vars": {"names": ", ".join(old)}}' in source
    assert '{"key": "r_stale_radio", "warn": True}' in source  # the radio gets its own line, never filtered out
