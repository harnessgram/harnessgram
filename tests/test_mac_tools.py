"""The Mac maintenance tools: parsers, the job manager and the owner-only panel endpoints.

Every command is a fake runner or a throwaway script written here. No real `mo`, no maintenance routine and no real
config is touched, and nothing is ever cleaned."""
import http.client
import json
import logging
import subprocess
import sys
import threading
import time

import pytest

from conftest import GOOD_TOKEN
from ctb import mac_resources, mac_tools, panel
from ctb.config import Config, ConfigError, from_dict
from test_panel import OWNER, signed

GB = 1024 ** 3
MB = 1024 ** 2

# What `mo clean --dry-run` printed on 2026-11-14 (shortened), with the colour codes it adds on a terminal.
MO_DRY = """\x1b[0;35m
Clean Your Mac\x1b[0m

Dry Run Mode, Preview only, no deletions

◎ System caches need sudo, run sudo -v && mo clean --dry-run for full preview

⚙ Apple Silicon | Free space: 34.79GB

\x1b[1m➤ User essentials\x1b[0m
  ○ User app cache · 25 items, 43.26GB dry
  ○ User app logs · 17 items, 26.9MB dry

➤ App caches
  ○ Siri suggestions cache · 11 items, 598KB dry
  ○ Group Containers logs/caches · 2.4MB dry
  ○ Handoff clipboard cache · 0B dry

➤ Developer tools
  ○ npm cache · would clean
  ○ Docker build cache and dangling images · would skip (whitelist)
  ○ Xcode DerivedData · 6 projects, 2.60GB dry
  ○ Clang module cache · 263.5MB dry

➤ Cloud & Office
  ✓ Nothing to clean

➤ Large files
  ⊙ Simulator data · 6.89GB · 11 simulators · ~/Library/Developer/CoreSimulator/Devices
  ⊙ Android emulators · 35.21GB · 2026-10-26 · /Volumes/Dev-Storage/Android/avd

======================================================================
Dry run complete - no changes made
Potential space: 48.01GB · 380 items
Detailed file list: /Users/someone/.config/mole/clean-list.txt
======================================================================
"""


# --- parsing ------------------------------------------------------------------------------------------------------------

def test_dry_run_total_items_and_the_three_biggest_categories():
    got = mac_tools.parse_dry_run(MO_DRY)
    assert got["bytes"] == int(48.01 * GB) and got["items"] == 380
    assert [c["name"] for c in got["categories"]] == ["User app cache", "Xcode DerivedData", "Clang module cache"]
    assert got["categories"][0]["bytes"] == int(43.26 * GB)


def test_large_files_and_would_clean_lines_are_not_categories():
    names = [c["name"] for c in mac_tools.parse_dry_run(MO_DRY + "  ○ Tiny · 1KB dry\n")["categories"]]
    assert "Simulator data" not in names and "npm cache" not in names


def test_without_a_total_line_the_categories_are_summed():
    text = "  ○ A · 3 items, 1.0GB dry\n  ○ B · 512MB dry\n"
    assert mac_tools.parse_dry_run(text)["bytes"] == GB + 512 * MB


@pytest.mark.parametrize("text", ["", None, 5, "garbage\n\x00\x1b[", "Potential space: lots", "○ x · dry", "\r\r\r" * 1000,
                                  "Potential space: 99999999999999999999999GB"])
def test_unreadable_output_gives_no_size_and_never_raises(text):
    got = mac_tools.parse_dry_run(text)
    assert got["bytes"] is None and got["categories"] == [] and got["items"] is None


def test_category_names_lose_paths_links_and_secret_like_tokens():
    text = ("  ○ Cache at /Users/me/private · 1.0GB dry\n  ○ Sync ~/Documents/x · 1.0GB dry\n"
            "  ○ See https://example.com/a?token=1 · 1.0GB dry\n"
            "  ○ key sk_" + "live_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 · 1.0GB dry\n  ○ " + "x" * 200 + " · 1.0GB dry\n")
    for cat in mac_tools.parse_dry_run(text)["categories"]:
        assert not any(w in cat["name"] for w in ("/Users", "~/", "https", "sk_live", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")) and len(cat["name"]) <= 48


def test_sizes_use_binary_units_and_reject_nonsense():
    assert mac_tools.to_bytes("1.5", "KB") == 1536 and mac_tools.to_bytes("2", "GB") == 2 * GB and mac_tools.to_bytes("1", "XB") is None


@pytest.mark.parametrize("text,want", [("Space freed: 1.25GB", int(1.25 * GB)), ("freed up 300MB", 300 * MB), ("nothing here", None), ("", None)])
def test_freed_space(text, want):
    assert mac_tools.parse_freed(text) == want


def test_maintenance_log_tells_skipped_from_done():
    assert mac_tools.parse_maintenance_log("== x start dry=0\nskip: heavy job running\n")["skipped"] == "busy"
    assert mac_tools.parse_maintenance_log("skip: memory free 9%")["skipped"] == "memory"
    assert mac_tools.parse_maintenance_log("skip: Mole whitelist missing")["skipped"] == "setup"
    assert mac_tools.parse_maintenance_log("2026-11-14 10:00:00 already running")["skipped"] == "running"
    done = mac_tools.parse_maintenance_log("-- x\n== 2026-11-14 10:20:00 done free 34G -> 40G\n")
    assert done == {"skipped": "", "free_before": 34 * GB, "free_after": 40 * GB}
    assert mac_tools.parse_maintenance_log("")["free_after"] is None


# --- configuration ------------------------------------------------------------------------------------------------------

def cfg_of(section):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", mac_tools=section)


FULL = {"clean_dry_run": ["/bin/echo", "dry"], "clean": ["/bin/echo", "real"], "maintenance": ["/bin/echo", "weekly"]}


def test_nothing_is_offered_without_configuration():
    assert not mac_tools.configured(Config()) and Config().mac_tools == {}
    assert mac_tools.actions(Config()) == {"dry_run": False, "clean": False, "maintenance": False}
    assert mac_tools.actions(cfg_of(FULL)) == {"dry_run": True, "clean": True, "maintenance": True}


def test_the_real_cleanup_is_never_offered_without_the_check():
    cfg = cfg_of({"clean": ["/bin/echo", "real"], "maintenance": ["/bin/echo"]})
    assert mac_tools.actions(cfg) == {"dry_run": False, "clean": False, "maintenance": True}
    assert not mac_tools.available(cfg, "clean") and not mac_tools.available(cfg, "dry-run")


@pytest.mark.parametrize("section", [
    {}, FULL, {"clean_dry_run": ["~/bin/mo", "clean"]}, {**FULL, "maintenance_log": "~/x.log"}, {"maintenance_log": ""},
])
def test_good_sections(section):
    assert mac_tools.valid_section(section) and from_dict({"mac_tools": section}).mac_tools == section


@pytest.mark.parametrize("section", [
    [], "mo", {"unknown": ["/x"]}, {"clean": "mo clean"}, {"clean": []}, {"clean": ["mo", "clean"]}, {"clean": ["/x", ""]},
    {"clean": ["/x", 5]}, {"clean": ["/x\x00"]}, {"maintenance_log": 5}, {"maintenance_log": "relative.log"}, {"maintenance_log": "/a\x00"},
])
def test_bad_sections_are_refused(section):
    assert not mac_tools.valid_section(section)
    with pytest.raises(ConfigError):
        from_dict({"mac_tools": section})


def test_the_program_may_start_with_home_and_is_expanded(monkeypatch):
    monkeypatch.setenv("HOME", "/home/testhome")
    cfg = cfg_of({"maintenance": ["~/.codex/bin/mac-maintenance", "--quiet"]})
    assert mac_tools.command(cfg, "maintenance")[0].endswith("/.codex/bin/mac-maintenance") and "~" not in mac_tools.command(cfg, "maintenance")[0]


# --- the job manager ----------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Fake:
    """A runner that records what it was given and answers from a table by the command's last word."""

    def __init__(self, answers=None, gate=None):
        self.calls, self.answers, self.gate = [], answers or {}, gate

    def __call__(self, argv, env, timeout):
        self.calls.append({"argv": argv, "env": env, "timeout": timeout})
        if self.gate is not None:
            self.gate.wait(5)
        answer = self.answers.get(argv[-1], (0, ""))
        if isinstance(answer, Exception):
            raise answer
        return answer


def make(tmp_path, monkeypatch, runner=None, disk=None, clock=None):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    return mac_tools.Manager(clock=clock or Clock(), runner=runner or Fake({"dry": (0, MO_DRY), "real": (0, "Space freed: 40GB")}),
                             disk=disk or (lambda: 100 * GB))


def run_job(mgr, cfg, kind, confirm=None):
    out = mgr.start(cfg, kind, confirm)
    mgr.join()
    return out


def test_a_preview_runs_and_is_remembered_with_sizes_only(tmp_path, monkeypatch):
    mgr = make(tmp_path, monkeypatch)
    out = run_job(mgr, cfg_of(FULL), "dry-run")
    assert out["ok"] and len(out["id"]) == 12
    st = mgr.status(cfg_of(FULL))
    assert st["job"] is None and st["last"]["state"] == "ok" and st["last"]["id"] == out["id"]
    assert st["dry"]["id"] == out["id"] and st["dry"]["bytes"] == int(48.01 * GB) and st["dry"]["fresh"] is True
    assert st["dry"]["categories"][0]["name"] == "User app cache"
    assert "Potential space" not in json.dumps(st) and "someone" not in json.dumps(st) and "mac" in st


def test_the_report_survives_a_restart(tmp_path, monkeypatch):
    mgr = make(tmp_path, monkeypatch)
    run_job(mgr, cfg_of(FULL), "dry-run")
    again = mac_tools.Manager(clock=mgr.clock, runner=mgr.runner, disk=mgr.disk)
    st = again.status(cfg_of(FULL))
    assert st["last"]["kind"] == "dry-run" and st["dry"]["fresh"] is True
    path = tmp_path / "state" / mac_tools.STORE_FILE
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_a_damaged_store_is_ignored(tmp_path, monkeypatch):
    mgr = make(tmp_path, monkeypatch)
    (tmp_path / "state").mkdir()
    for text in ("not json", "[1]", json.dumps({"last": {"id": "zz", "kind": "clean"}, "dry": 5}),
                 json.dumps({"last": {"id": "a" * 12, "kind": "rm -rf", "state": "ok", "finished_at": 1}})):
        (tmp_path / "state" / mac_tools.STORE_FILE).write_text(text)
        st = mac_tools.Manager(clock=Clock()).status(cfg_of(FULL))
        assert st["last"] is None and st["dry"] is None


def test_the_real_cleanup_needs_a_fresh_preview(tmp_path, monkeypatch):
    clock, fake = Clock(), Fake({"dry": (0, MO_DRY)})
    mgr = make(tmp_path, monkeypatch, runner=fake, clock=clock)
    cfg = cfg_of(FULL)
    assert mgr.start(cfg, "clean", "a" * 12) == {"ok": False, "error": "stale"}
    dry = run_job(mgr, cfg, "dry-run")
    clock.now += mac_tools.FRESH_SECONDS + 1
    assert mgr.start(cfg, "clean", dry["id"]) == {"ok": False, "error": "stale"}
    assert mgr.status(cfg)["dry"]["fresh"] is False
    assert [c["argv"][-1] for c in fake.calls] == ["dry"]


def test_the_confirmation_must_name_the_preview(tmp_path, monkeypatch):
    fake = Fake({"dry": (0, MO_DRY)})
    mgr = make(tmp_path, monkeypatch, runner=fake)
    cfg = cfg_of(FULL)
    dry = run_job(mgr, cfg, "dry-run")
    for bad in (None, True, "", "0" * 12, 5):
        assert mgr.start(cfg, "clean", bad) == {"ok": False, "error": "confirm"}
    assert len(fake.calls) == 1
    assert run_job(mgr, cfg, "clean", dry["id"])["ok"]


def test_a_preview_is_spent_by_the_cleanup(tmp_path, monkeypatch):
    mgr = make(tmp_path, monkeypatch)
    cfg = cfg_of(FULL)
    dry = run_job(mgr, cfg, "dry-run")
    assert run_job(mgr, cfg, "clean", dry["id"])["ok"]
    assert mgr.status(cfg)["dry"] is None
    assert mgr.start(cfg, "clean", dry["id"]) == {"ok": False, "error": "stale"}


def test_the_approval_variable_reaches_only_the_real_cleanup(tmp_path, monkeypatch):
    fake = Fake({"dry": (0, MO_DRY)})
    mgr = make(tmp_path, monkeypatch, runner=fake)
    cfg = cfg_of(FULL)
    dry = run_job(mgr, cfg, "dry-run")
    run_job(mgr, cfg, "clean", dry["id"])
    run_job(mgr, cfg, "maintenance", True)
    envs = {c["argv"][-1]: c["env"] for c in fake.calls}
    assert envs["real"]["OWNER_APPROVED_CLEAN"] == "1"
    assert "OWNER_APPROVED_CLEAN" not in envs["dry"] and "OWNER_APPROVED_CLEAN" not in envs["weekly"]
    import os
    assert os.environ.get("OWNER_APPROVED_CLEAN") is None  # never set in the bridge's own environment
    assert set(envs["real"]) <= {"PATH", "LANG", "LC_ALL", "TERM", "NO_COLOR", "HOME", "USER", "LOGNAME", "TMPDIR", "OWNER_APPROVED_CLEAN"}


def test_the_routine_needs_its_confirmation(tmp_path, monkeypatch):
    fake = Fake()
    mgr = make(tmp_path, monkeypatch, runner=fake)
    for bad in (None, False, "yes", 1):
        assert mgr.start(cfg_of(FULL), "maintenance", bad) == {"ok": False, "error": "confirm"}
    assert not fake.calls
    assert run_job(mgr, cfg_of(FULL), "maintenance", True)["ok"]


def test_unconfigured_or_unknown_work_never_runs(tmp_path, monkeypatch):
    fake = Fake()
    mgr = make(tmp_path, monkeypatch, runner=fake)
    for kind in ("dry-run", "clean", "maintenance", "rm", "", None):
        assert mgr.start(Config(), kind, True) == {"ok": False, "error": "unconfigured"}
    assert mgr.start(cfg_of({"maintenance": ["/bin/echo"]}), "dry-run") == {"ok": False, "error": "unconfigured"}
    assert not fake.calls


def test_one_job_at_a_time(tmp_path, monkeypatch):
    gate = threading.Event()
    fake = Fake({"dry": (0, MO_DRY)}, gate=gate)
    mgr = make(tmp_path, monkeypatch, runner=fake)
    cfg = cfg_of(FULL)
    first = mgr.start(cfg, "dry-run")
    assert first["ok"]
    assert mgr.start(cfg, "dry-run") == {"ok": False, "error": "busy", "kind": "dry-run"}
    assert mgr.start(cfg, "maintenance", True) == {"ok": False, "error": "busy", "kind": "dry-run"}
    running = mgr.status(cfg)["job"]
    assert running["id"] == first["id"] and running["kind"] == "dry-run"
    gate.set()
    mgr.join()
    assert mgr.status(cfg)["job"] is None and len(fake.calls) == 1
    assert mgr.start(cfg, "maintenance", True)["ok"]
    mgr.join()


def test_the_lock_is_released_when_the_runner_fails(tmp_path, monkeypatch):
    fake = Fake({"dry": OSError("no such program")})
    mgr = make(tmp_path, monkeypatch, runner=fake)
    run_job(mgr, cfg_of(FULL), "dry-run")
    assert mgr.status(cfg_of(FULL))["last"]["state"] == "error" and mgr.status(cfg_of(FULL))["job"] is None
    fake.answers["dry"] = RuntimeError("boom")
    run_job(mgr, cfg_of(FULL), "dry-run")
    assert mgr.status(cfg_of(FULL))["last"]["state"] == "error"
    fake.answers["dry"] = subprocess.TimeoutExpired("x", 1)
    run_job(mgr, cfg_of(FULL), "dry-run")
    assert mgr.status(cfg_of(FULL))["last"]["state"] == "timeout"
    fake.answers["dry"] = (0, MO_DRY)
    assert run_job(mgr, cfg_of(FULL), "dry-run")["ok"]


@pytest.mark.parametrize("answer,state", [((1, "x"), "failed"), ((0, "no sizes at all"), "unreadable")])
def test_failures_are_reported_without_a_preview(tmp_path, monkeypatch, answer, state):
    mgr = make(tmp_path, monkeypatch, runner=Fake({"dry": answer}))
    run_job(mgr, cfg_of(FULL), "dry-run")
    st = mgr.status(cfg_of(FULL))
    assert st["last"]["state"] == state and st["dry"] is None
    assert mgr.start(cfg_of(FULL), "clean", st["last"]["id"]) == {"ok": False, "error": "stale"}


def test_the_cleanup_reports_the_space_it_freed(tmp_path, monkeypatch):
    free = iter([10 * GB, 10 * GB, 10 * GB, 15 * GB])  # before and after the preview, before and after the cleanup
    mgr = make(tmp_path, monkeypatch, runner=Fake({"dry": (0, MO_DRY), "real": (0, "all done")}), disk=lambda: next(free))
    cfg = cfg_of(FULL)
    dry = run_job(mgr, cfg, "dry-run")
    run_job(mgr, cfg, "clean", dry["id"])
    last = mgr.status(cfg)["last"]
    assert last["kind"] == "clean" and last["state"] == "ok"
    assert (last["free_before"], last["free_after"], last["bytes"]) == (10 * GB, 15 * GB, 5 * GB)


def test_the_cleanup_prefers_the_amount_the_tool_printed(tmp_path, monkeypatch):
    mgr = make(tmp_path, monkeypatch, runner=Fake({"dry": (0, MO_DRY), "real": (0, "Space freed: 2GB")}))
    dry = run_job(mgr, cfg_of(FULL), "dry-run")
    run_job(mgr, cfg_of(FULL), "clean", dry["id"])
    assert mgr.status(cfg_of(FULL))["last"]["bytes"] == 2 * GB


def test_a_skipped_routine_is_not_called_done(tmp_path, monkeypatch):
    logfile = tmp_path / "maintenance.log"
    logfile.write_text("== old run\nskip: heavy job running\n")  # an earlier run: must not count

    def runner(argv, env, timeout):
        with open(logfile, "a") as f:
            f.write("== now start dry=0\nskip: memory free 9%\n")
        return 0, ""
    mgr = make(tmp_path, monkeypatch, runner=runner)
    cfg = cfg_of({**FULL, "maintenance_log": str(logfile)})
    run_job(mgr, cfg, "maintenance", True)
    last = mgr.status(cfg)["last"]
    assert last["state"] == "skipped" and last["skipped"] == "memory"

    def done(argv, env, timeout):
        with open(logfile, "a") as f:
            f.write("== x done free 30G -> 35G\n")
        return 0, ""
    mgr.runner = done
    run_job(mgr, cfg, "maintenance", True)
    last = mgr.status(cfg)["last"]
    assert last["state"] == "ok" and last["skipped"] == ""


def test_nothing_the_command_printed_is_logged_or_stored(tmp_path, monkeypatch, caplog):
    secret = "SUPERSECRETVALUE"
    mgr = make(tmp_path, monkeypatch, runner=Fake({"dry": (0, MO_DRY + "\n token=" + secret + "\n"), "real": (1, "boom " + secret)}))
    with caplog.at_level(logging.DEBUG):
        dry = run_job(mgr, cfg_of(FULL), "dry-run")
        run_job(mgr, cfg_of(FULL), "clean", dry["id"])
    assert secret not in caplog.text and "Potential space" not in caplog.text
    assert secret not in (tmp_path / "state" / mac_tools.STORE_FILE).read_text()
    assert secret not in json.dumps(mgr.status(cfg_of(FULL)))


# --- real processes -----------------------------------------------------------------------------------------------------

def script(tmp_path, body, name="job.py"):
    path = tmp_path / name
    path.write_text("import json, os, sys, time\n" + body)
    return [sys.executable, str(path)]


def test_a_real_command_runs_without_shell_stdin_or_extra_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SECRET_TOKEN_FOR_TEST", "must-not-leak")
    out = tmp_path / "seen.json"
    body = ("data = {'argv': sys.argv[1:], 'approved': os.environ.get('OWNER_APPROVED_CLEAN'), 'leak': os.environ.get('SECRET_TOKEN_FOR_TEST'),\n"
            "        'stdin': sys.stdin.read(), 'tty': sys.stdin.isatty()}\n"
            "open(%r + '.' + sys.argv[1].split(';')[0], 'w').write(json.dumps(data))\n"
            "print('Potential space: 1.0GB \\u00b7 3 items')\n" % str(out))
    cmd = script(tmp_path, body)
    cfg = cfg_of({"clean_dry_run": cmd + ["dry; touch " + str(tmp_path / "pwned")], "clean": cmd + ["real"], "maintenance": cmd + ["weekly"]})
    mgr = mac_tools.Manager(disk=lambda: None)
    dry = run_job(mgr, cfg, "dry-run")
    assert mgr.status(cfg)["dry"]["bytes"] == GB
    run_job(mgr, cfg, "clean", dry["id"])
    run_job(mgr, cfg, "maintenance", True)
    assert not (tmp_path / "pwned").exists()
    seen = {n: json.loads((tmp_path / ("seen.json." + n)).read_text()) for n in ("weekly", "real", "dry")}
    assert seen["real"]["approved"] == "1" and seen["weekly"]["approved"] is None and seen["dry"]["approved"] is None
    assert seen["dry"]["argv"] == ["dry; touch " + str(tmp_path / "pwned")]  # one argument, never parsed by a shell
    assert all(s["leak"] is None and s["stdin"] == "" for s in seen.values())


def test_a_command_that_runs_too_long_is_stopped_and_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setitem(mac_tools.TIMEOUTS, "dry-run", 1)
    monkeypatch.setattr(mac_tools, "KILL_GRACE", 1)
    pidfile = tmp_path / "pid"
    cmd = script(tmp_path, "open(%r, 'w').write(str(os.getpid()))\ntime.sleep(60)\n" % str(pidfile))
    mgr = mac_tools.Manager(disk=lambda: None)
    cfg = cfg_of({"clean_dry_run": cmd})
    started = time.monotonic()
    assert mgr.start(cfg, "dry-run")["ok"]
    mgr.join(10)
    assert time.monotonic() - started < 8
    st = mgr.status(cfg)
    assert st["last"]["state"] == "timeout" and st["job"] is None and st["dry"] is None
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        import os
        for _ in range(20):
            os.kill(pid, 0)
            time.sleep(0.1)


def test_a_missing_program_is_an_error_report(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    mgr = mac_tools.Manager(disk=lambda: None)
    cfg = cfg_of({"clean_dry_run": [str(tmp_path / "missing")]})
    run_job(mgr, cfg, "dry-run")
    assert mgr.status(cfg)["last"]["state"] == "error"


def test_output_is_bounded_in_memory(tmp_path):
    cmd = script(tmp_path, "sys.stdout.write('x' * (3 * 1024 * 1024))\nsys.stdout.write('\\nPotential space: 2.0GB')\n")
    code, text = mac_tools.spawn(cmd, mac_tools.child_env(), 30)
    assert code == 0 and len(text) < mac_tools.OUT_HEAD + mac_tools.OUT_TAIL + 8 and mac_tools.parse_dry_run(text)["bytes"] == 2 * GB


# --- a job that outlives the bridge process ------------------------------------------------------------------------------

def orphan(tmp_path, kind="clean", seconds=60, age=5, now=None):
    """A real job process left running by an earlier bridge process, with the record that bridge wrote."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(%d)" % seconds], start_new_session=True)
    record = {"id": "a1b2c3d4e5f6", "kind": kind, "started_at": int((now or time.time()) - age), "pid": proc.pid, "pgid": proc.pid}
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / mac_tools.JOB_FILE).write_text(json.dumps(record))
    return proc, tmp_path / "state" / mac_tools.JOB_FILE


def test_a_restart_while_a_job_runs_shows_it_and_refuses_a_second_one(tmp_path, monkeypatch):
    fake = Fake({"dry": (0, MO_DRY)})
    mgr = make(tmp_path, monkeypatch, runner=fake, clock=time.time)   # a new bridge process: no thread, no memory of the job
    proc, record = orphan(tmp_path, kind="clean")
    try:
        cfg = cfg_of(FULL)
        st = mgr.status(cfg)
        assert st["job"]["kind"] == "clean" and st["job"]["id"] == "a1b2c3d4e5f6" and st["job"]["elapsed"] >= 5
        for kind, confirm in (("dry-run", None), ("clean", "a" * 12), ("maintenance", True)):
            assert mgr.start(cfg, kind, confirm) == {"ok": False, "error": "busy", "kind": "clean"}
        assert not fake.calls and record.exists()
    finally:
        proc.kill()
        proc.wait()
    st = mgr.status(cfg)                                              # the process ended: no longer running, record cleared
    assert st["job"] is None and not record.exists()
    assert run_job(mgr, cfg, "dry-run")["ok"]


def test_a_dead_process_in_the_record_is_cleared(tmp_path, monkeypatch):
    mgr = make(tmp_path, monkeypatch, clock=time.time)
    proc, record = orphan(tmp_path, seconds=0)
    proc.wait()
    assert mgr.status(cfg_of(FULL))["job"] is None and not record.exists()
    proc, record = orphan(tmp_path, seconds=0)
    proc.wait()
    assert mgr.start(cfg_of(FULL), "dry-run")["ok"] and not record.exists()
    mgr.join()


@pytest.mark.parametrize("text", ["not json", "[1]", "{}", json.dumps({"id": "zz", "kind": "clean", "started_at": 1, "pid": 5, "pgid": 5}),
                                  json.dumps({"id": "a1b2c3d4e5f6", "kind": "rm", "started_at": 1, "pid": 5, "pgid": 5})])
def test_a_damaged_job_record_is_ignored_and_removed(tmp_path, monkeypatch, text):
    mgr = make(tmp_path, monkeypatch, clock=time.time)
    path = tmp_path / "state" / mac_tools.JOB_FILE
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    assert mgr.status(cfg_of(FULL))["job"] is None
    assert not path.exists()


def test_a_record_that_is_too_old_is_not_believed_even_if_the_id_is_alive(tmp_path, monkeypatch):
    mgr = make(tmp_path, monkeypatch, clock=time.time)
    proc, record = orphan(tmp_path, kind="dry-run", age=mac_tools.TIMEOUTS["dry-run"] + mac_tools.JOB_SLACK + 10)
    try:
        assert mgr.status(cfg_of(FULL))["job"] is None and not record.exists()
    finally:
        proc.kill()
        proc.wait()


def test_a_record_that_points_at_no_group_or_at_init_is_dead():
    assert not mac_tools.group_alive(None) and not mac_tools.group_alive(0) and not mac_tools.group_alive(1) and not mac_tools.group_alive(True)
    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    proc.wait()
    assert not mac_tools.group_alive(proc.pid)


def test_a_running_job_is_recorded_and_the_record_goes_with_it(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    cmd = script(tmp_path, "time.sleep(2)\nprint('Potential space: 1.0GB')\n")
    mgr = mac_tools.Manager(disk=lambda: None)
    cfg = cfg_of({"clean_dry_run": cmd})
    record = tmp_path / "state" / mac_tools.JOB_FILE
    started = mgr.start(cfg, "dry-run")
    for _ in range(50):
        if record.exists():
            break
        time.sleep(0.1)
    data = json.loads(record.read_text())
    assert data["id"] == started["id"] and data["kind"] == "dry-run" and data["pid"] == data["pgid"] and mac_tools.group_alive(data["pgid"])
    assert oct(record.stat().st_mode & 0o777) == "0o600"
    mgr.join(10)
    assert not record.exists() and mgr.status(cfg)["last"]["state"] == "ok"


# --- the panel endpoints ------------------------------------------------------------------------------------------------

@pytest.fixture
def make_server(tmp_path, monkeypatch):
    made = []
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(mac_resources, "snapshot", lambda: {"available": True, "status": "ok", "disk": {"free_bytes": GB, "total_bytes": 4 * GB, "status": "ok"}})

    def make(section, runner=None, clock=None):
        srv = panel.PanelServer(("127.0.0.1", 0), cfg_of(section))
        srv.mac_tools_manager = mac_tools.Manager(clock=clock or time.time, runner=runner or Fake({"dry": (0, MO_DRY)}), disk=lambda: 5 * GB)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        made.append(srv)
        return srv
    yield make
    for srv in made:
        srv.shutdown()
        srv.server_close()


def post(srv, body, path="/api/mac-tools", init="ok"):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
    headers = {}
    if init is not None:
        headers[panel.INIT_HEADER] = signed() if init == "ok" else init
    payload = body if isinstance(body, bytes) else json.dumps(body).encode()
    conn.request("POST", path, body=payload, headers=headers)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, json.loads(data)


def test_routes_are_declared():
    assert panel.ROUTES["/api/mac-tools"] >= 128 and panel.ACTIONS["/api/mac-tools"] == "_mac_tools"
    assert panel.ROUTES["/api/mac-tools-status"] and panel.ACTIONS["/api/mac-tools-status"] == "_mac_tools_status"


@pytest.mark.parametrize("path,body", [("/api/mac-tools", {"action": "dry-run"}), ("/api/mac-tools-status", {})])
def test_owner_login_is_required(make_server, path, body):
    fake = Fake({"dry": (0, MO_DRY)})
    srv = make_server(FULL, runner=fake)
    for init in (None, "", "garbage", signed(tamper=True), signed(user_id=OWNER + 1),
                 signed(auth_date=int(time.time()) - panel.MAX_AGE - 30)):
        code, out = post(srv, body, path=path, init=init)
        assert code == 403 and out == {"error": "forbidden"}, init
    assert not fake.calls


def test_starting_a_job_needs_a_fresh_login_but_reading_does_not(make_server):
    fake = Fake({"dry": (0, MO_DRY)})
    srv = make_server(FULL, runner=fake)
    older = signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 60)
    assert post(srv, {"action": "dry-run"}, init=older)[0] == 403 and not fake.calls
    assert post(srv, {}, path="/api/mac-tools-status", init=older)[0] == 200


def test_hidden_when_not_configured(make_server):
    fake = Fake()
    srv = make_server({}, runner=fake)
    assert post(srv, {"action": "dry-run"})[0] == 404 and post(srv, {}, path="/api/mac-tools-status")[0] == 404
    code, state = post(srv, {}, path="/api/state")
    assert code == 200 and state["mac_tools"] is False and not fake.calls


def test_only_the_configured_buttons_work(make_server):
    srv = make_server({"maintenance": ["/bin/echo"]}, runner=Fake())
    assert post(srv, {"action": "dry-run"})[0] == 404
    assert post(srv, {"action": "clean", "confirm": "a" * 12})[0] == 404
    code, state = post(srv, {}, path="/api/state")
    assert state["mac_tools"] == {"dry_run": False, "clean": False, "maintenance": True}


def test_state_tells_the_page_what_exists(make_server):
    srv = make_server(FULL)
    assert post(srv, {}, path="/api/state")[1]["mac_tools"] == {"dry_run": True, "clean": True, "maintenance": True}


@pytest.mark.parametrize("body", [
    {}, {"action": "rm"}, {"action": 5}, {"action": None}, {"action": ["clean"]}, {"action": {"a": 1}}, {"action": "dry-run", "confirm": True}, {"action": "dry-run", "extra": 1},
    {"action": "clean"}, {"action": "clean", "confirm": True}, {"action": "clean", "confirm": "x" * 33}, {"action": "clean", "confirm": 5},
    {"action": "maintenance"}, {"action": "maintenance", "confirm": "yes"}, {"action": "maintenance", "confirm": 1},
    {"action": "maintenance", "confirm": True, "extra": 1}, [1], b"not json", b"[1]",
])
def test_bad_bodies_are_a_400_and_run_nothing(make_server, body):
    fake = Fake()
    srv = make_server(FULL, runner=fake)
    code, out = post(srv, body)
    assert code == 400 and out == {"error": "bad request"} and not fake.calls


def test_the_whole_flow_over_http(make_server):
    fake = Fake({"dry": (0, MO_DRY), "real": (0, "Space freed: 3GB")})
    srv = make_server(FULL, runner=fake)
    mgr = srv.mac_tools_manager
    assert post(srv, {"action": "clean", "confirm": "a" * 12}) == (409, {"error": "stale"})
    code, started = post(srv, {"action": "dry-run"})
    assert code == 200 and started["ok"] and started["kind"] == "dry-run"
    mgr.join()
    code, st = post(srv, {}, path="/api/mac-tools-status")
    assert code == 200 and st["actions"] == {"dry_run": True, "clean": True, "maintenance": True}
    assert st["dry"]["id"] == started["id"] and st["dry"]["fresh"] and st["mac"]["available"] and st["last"]["state"] == "ok"
    assert post(srv, {"action": "clean", "confirm": "f" * 12}) == (409, {"error": "confirm"})
    assert [c["argv"][-1] for c in fake.calls] == ["dry"]
    assert post(srv, {"action": "clean", "confirm": st["dry"]["id"]})[0] == 200
    mgr.join()
    assert post(srv, {}, path="/api/mac-tools-status")[1]["last"]["bytes"] == 3 * GB
    assert post(srv, {"action": "maintenance", "confirm": False})[0] == 400
    assert post(srv, {"action": "maintenance", "confirm": True})[0] == 200
    mgr.join()
    assert [c["argv"][-1] for c in fake.calls] == ["dry", "real", "weekly"]


def test_a_second_job_is_a_409_while_one_runs(make_server):
    gate = threading.Event()
    fake = Fake({"dry": (0, MO_DRY)}, gate=gate)
    srv = make_server(FULL, runner=fake)
    assert post(srv, {"action": "dry-run"})[0] == 200
    code, out = post(srv, {"action": "dry-run"})
    assert code == 409 and out == {"error": "busy", "kind": "dry-run"}
    assert post(srv, {"action": "maintenance", "confirm": True})[0] == 409
    code, st = post(srv, {}, path="/api/mac-tools-status")
    assert st["job"]["kind"] == "dry-run" and st["job"]["elapsed"] >= 0
    gate.set()
    srv.mac_tools_manager.join()
    assert len(fake.calls) == 1


def test_the_request_returns_at_once_while_the_job_runs(make_server):
    gate = threading.Event()
    srv = make_server(FULL, runner=Fake({"dry": (0, MO_DRY)}, gate=gate))
    started = time.monotonic()
    assert post(srv, {"action": "dry-run"})[0] == 200
    assert time.monotonic() - started < 2
    gate.set()
    srv.mac_tools_manager.join()


def test_a_broken_manager_is_a_500_not_a_crash(make_server):
    srv = make_server(FULL)

    class Broken:
        def start(self, *a):
            raise RuntimeError("boom")

        def status(self, *a):
            raise RuntimeError("boom")
    srv.mac_tools_manager = Broken()
    assert post(srv, {"action": "dry-run"}) == (500, {"error": "unavailable"})
    assert post(srv, {}, path="/api/mac-tools-status") == (500, {"error": "unavailable"})


def test_over_http_a_job_left_by_an_earlier_bridge_is_running_and_blocks_a_new_one(make_server, tmp_path):
    fake = Fake({"dry": (0, MO_DRY)})
    srv = make_server(FULL, runner=fake)
    proc, record = orphan(tmp_path, kind="maintenance")
    try:
        code, st = post(srv, {}, path="/api/mac-tools-status")
        assert code == 200 and st["job"]["kind"] == "maintenance"
        assert post(srv, {"action": "dry-run"}) == (409, {"error": "busy", "kind": "maintenance"})
        assert not fake.calls
    finally:
        proc.kill()
        proc.wait()
    assert post(srv, {}, path="/api/mac-tools-status")[1]["job"] is None
