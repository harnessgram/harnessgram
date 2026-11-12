"""Real processes on a private tmux server: what a hung-exit stop signals, and what it leaves alone.

Nothing here is faked on the path to os.kill. Each test starts its own tmux server (a fresh TMUX_TMPDIR) and
its own fake programs: copies of one tiny compiled program that waits for a signal, named like the real
shapes (…/claude/versions/2.1.314 native, …/npm/node, …/claude) under a temp directory, so tmux and ps
report them as they report Claude. The only processes ever signalled are the ones those tests started;
teardown ends the private server and any process whose command line names the test's temp directory."""
from __future__ import annotations

import json
import os
import stat
import shutil
import subprocess
import tempfile
import sys
import time
from pathlib import Path

import pytest

from ctb import accounts, inbox as I

SID = "abcd1234-real-process"
TM = "tg-" + SID[:8]
EXACT = "=" + TM
REAL_SLEEP = time.sleep
REAL_KILL = os.kill
SHELL = shutil.which("zsh") or shutil.which("bash")
CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")

pytestmark = pytest.mark.skipif(
    not (shutil.which("tmux") and SHELL and CC and os.path.exists("/bin/sleep") and os.path.exists("/bin/sh")),
    reason="needs tmux, a shell and a C compiler")

FAKE_C = r"""
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
/* fake [--ignore-term] [--child PROGRAM PIDFILE] [--job SH 'COMMAND']: waits until it is signalled. */
int main(int c, char **v) {
  for (int i = 1; i < c; i++) {
    if (!strcmp(v[i], "--ignore-term")) {
      signal(SIGTERM, SIG_IGN);
    } else if (!strcmp(v[i], "--child") && i + 2 < c) {
      signal(SIGCHLD, SIG_IGN);
      pid_t p = fork();
      if (p == 0) { execl(v[i + 1], v[i + 1], (char *)0); _exit(1); }
      FILE *f = fopen(v[i + 2], "w"); fprintf(f, "%d\n", (int)p); fclose(f);
      i += 2;
    } else if (!strcmp(v[i], "--job") && i + 2 < c) {
      if (fork() == 0) { execl(v[i + 1], "sh", "-c", v[i + 2], (char *)0); _exit(1); }
      i += 2;
    }
  }
  char *dir = getenv("FAKE_READY_DIR"); /* set up: say so, because a fresh binary can start slowly */
  if (dir) {
    char name[600];
    snprintf(name, sizeof name, "%s/%d", dir, (int)getpid());
    FILE *f = fopen(name, "w");
    if (f) fclose(f);
  }
  for (;;) pause();
}
"""


@pytest.fixture(scope="module")
def fake_program(tmp_path_factory):
    src = tmp_path_factory.mktemp("fakesrc") / "fake.c"
    src.write_text(FAKE_C)
    out = src.with_name("fake")
    built = subprocess.run([CC, "-o", str(out), str(src)], capture_output=True, text=True)
    if built.returncode != 0:
        pytest.skip("cannot compile the fake program: " + built.stderr[-200:])
    return out


def until(check, what, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        REAL_SLEEP(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def process_table():
    out = subprocess.run(["ps", "-axo", "pid=,stat=,command="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            rows.append((int(parts[0]), parts[1], parts[2]))
    return rows


def alive(pid):
    return any(p == pid and not stat.startswith("Z") for p, stat, _ in process_table())


class Lab:
    def __init__(self, sock, prog, fake):
        self.sock, self.prog = sock, prog
        for rel in ("claude/versions/2.1.314", "claude/versions/2.1.315", "claude/claude", "node", "npm/node",
                    "rc/rcserver", "idle"):
            (self.prog / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fake, self.prog / rel)
        for rel, target in (("bin/sh", "/bin/sh"), ("bin/sleep", "/bin/sleep")):
            (self.prog / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.prog / rel).symlink_to(target)
        self.native = str(self.prog / "claude/versions/2.1.314")
        self.pidfile = self.prog / "child.pid"
        self.ready_dir = self.prog / "ready"
        self.ready_dir.mkdir()

    def child_pid(self):
        pid = int(until(lambda: self.pidfile.exists() and self.pidfile.read_text().strip(), "child pid"))
        until(lambda: (self.ready_dir / str(pid)).exists(), "the child to be set up")
        return pid

    def start(self, command, name=TM):
        res = I._tmux("new-session", "-d", "-s", name, "-x", "120", "-y", "30", command)
        assert res.returncode == 0, res.stderr

    def shell(self, name=TM):
        self.start(f"exec {SHELL} -f -i" if "zsh" in SHELL else f"exec {SHELL} --norc -i", name)
        until(lambda: I._pane_command(name) in I.SHELLS and self.screen_has("", name) and
              I._tmux("capture-pane", "-p", "-t", name + ":").stdout.strip(), "the shell prompt")

    def type(self, text, name=TM):
        I._tmux("send-keys", "-t", name + ":", "-l", text)
        I._tmux("send-keys", "-t", name + ":", "Enter")

    def screen_has(self, text, name=TM):
        return text in I._tmux("capture-pane", "-p", "-t", name + ":").stdout

    def pid_of(self, rel_command):
        """The pid of the process whose command line starts with this path (relative to the temp dir)."""
        want = str(self.prog / rel_command)
        pid = until(lambda: next((p for p, stat, cmd in process_table()
                                  if cmd.startswith(want) and not stat.startswith("Z")), None), rel_command)
        if "bin/" not in rel_command:
            until(lambda: (self.ready_dir / str(pid)).exists(), f"{rel_command} to be set up")
        return pid

    def ready(self, pane=EXACT, count=1):
        until(lambda: len(I._pane_claude_targets(pane)) == count, f"{count} foreground Claude process(es)")

    def old_enough(self, pid):
        until(lambda: I._ps_field("etime=", pid) not in ("00:00", ""), "one second of age", timeout=5)

    def record(self, pid, session=SID):
        sessions = Path(os.environ["HOME"]) / ".claude" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": session, "status": "idle"}))

    def teardown(self):
        assert os.environ["TMUX_TMPDIR"] == self.sock  # only ever the private server
        # -S names the private socket outright, so this can never reach another server
        subprocess.run([shutil.which("tmux"), "-S", f"{self.sock}/tmux-{os.getuid()}/default", "kill-server"],
                       capture_output=True, env=os.environ.copy())
        for pid, _stat, command in process_table():
            if str(self.prog) in command and pid != os.getpid():
                try:
                    REAL_KILL(pid, 9)
                except OSError:
                    pass
        shutil.rmtree(self.sock, ignore_errors=True)
        shutil.rmtree(self.prog, ignore_errors=True)


@pytest.fixture
def lab(monkeypatch, fake_program, private_tmux):
    sock = tempfile.mkdtemp(prefix="hgt", dir="/tmp")  # short: a unix socket path has a length limit
    prog = Path(tempfile.mkdtemp(prefix="hgp", dir="/tmp"))
    monkeypatch.setenv("TMUX_TMPDIR", sock)
    monkeypatch.setenv("FAKE_READY_DIR", str(prog / "ready"))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert "claude" not in str(prog).lower()
    monkeypatch.setattr(I.time, "sleep", lambda s: REAL_SLEEP(s * 0.15))  # the 1s/2s/5s waits, shortened
    box = Lab(sock, prog, fake_program)
    yield box
    box.teardown()


@pytest.fixture
def signals(monkeypatch):
    """Every real signal sent through os.kill (not the 0 probes), delegating to the real call."""
    sent = []

    def kill(pid, sig):
        if sig:
            sent.append((pid, sig))
        return REAL_KILL(pid, sig)

    monkeypatch.setattr(I.os, "kill", kill)
    return sent


def stop(pane=EXACT, **kw):
    kw.setdefault("session", SID)
    return I._stop_hung_claude(pane, **kw)


def native_in_shell(box, ignore_term=False, plain_input=False):
    box.shell()
    if plain_input:  # line editing off: the Escape and /exit typed meanwhile must not become an `exit`
        box.type("unsetopt zle" if "zsh" in SHELL else "set +o emacs +o vi")
        REAL_SLEEP(0.5)
    box.type(f"{box.native} --ignore-term" if ignore_term else box.native)
    pid = box.pid_of("claude/versions/2.1.314")
    box.ready()
    return pid


# --- the hung process is ended: SIGTERM first, SIGKILL when that is ignored ------------------------------

def test_a_hung_native_claude_gets_sigterm_and_the_shell_stays(lab, signals):
    pid = native_in_shell(lab)
    lab.record(pid)
    assert stop(pid=pid, started=I._proc_start(pid)) in I.SHELLS
    assert signals == [(pid, 15)]
    assert not alive(pid)
    assert I._pane_command(EXACT) in I.SHELLS  # tmux and its shell are still there


def test_a_hung_claude_that_ignores_sigterm_gets_sigkill(lab, signals):
    pid = native_in_shell(lab, ignore_term=True)
    lab.record(pid)
    assert stop(pid=pid) in I.SHELLS
    assert signals == [(pid, 15), (pid, 9)]
    assert not alive(pid)


def test_an_npm_install_of_claude_is_ended_too(lab, signals):
    lab.start(f"exec {lab.prog}/npm/node /x/claude-code/cli.js")
    pid = lab.pid_of("npm/node /x/claude-code/cli.js")
    lab.ready()
    lab.record(pid)
    stop(pid=pid)
    assert (pid, 15) in signals and not alive(pid)


# --- what is left alone ----------------------------------------------------------------------------------

def test_a_background_node_job_is_never_signalled(lab, signals):
    """`npm run dev &` and then `claude` in one shell: only the foreground Claude is the target."""
    lab.shell()
    lab.type(f"{lab.prog}/node &")
    dev = lab.pid_of("node")
    pid = native_in_shell_after(lab)
    lab.record(pid)
    stop(pid=pid)
    assert {p for p, _ in signals} == {pid}
    assert not alive(pid) and alive(dev)


def native_in_shell_after(box):
    box.type(box.native)
    pid = box.pid_of("claude/versions/2.1.314")
    box.ready()
    return pid


def test_a_second_claude_in_the_background_of_the_same_shell_is_left_alone(lab, signals):
    """Only the terminal's foreground process group is a target, even among Claude-looking processes."""
    lab.shell()
    lab.type(f"{lab.prog}/claude/claude &")
    other = lab.pid_of("claude/claude")
    pid = native_in_shell_after(lab)
    lab.record(pid)
    stop(pid=pid)
    assert {p for p, _ in signals} == {pid}
    assert not alive(pid) and alive(other)


def test_an_idle_shell_with_only_a_background_claude_has_no_target(lab, signals):
    lab.shell()
    lab.type(f"{lab.prog}/claude/claude &")
    other = lab.pid_of("claude/claude")
    assert I._pane_claude_targets(EXACT) == []
    stop(min_age=1)
    assert signals == [] and alive(other)


def test_a_background_node_job_survives_even_without_a_known_session_pid(lab, signals, monkeypatch):
    monkeypatch.setattr(I, "HUNG_MIN_AGE", 1)
    lab.shell()
    lab.type(f"{lab.prog}/node &")
    dev = lab.pid_of("node")
    pid = native_in_shell_after(lab)
    lab.old_enough(pid)
    stop(min_age=1)
    assert {p for p, _ in signals} == {pid} and alive(dev)


def test_a_plain_node_in_a_bridge_window_survives_a_reopen(lab, signals, monkeypatch):
    """Old code took any `node` older than 90s with no record for a hung Claude."""
    monkeypatch.setattr(I, "HUNG_MIN_AGE", 1)
    monkeypatch.setattr(accounts, "shell_command", lambda *a, **k: "echo reopened")
    lab.start(f"exec {lab.prog}/node /srv/devserver.js")
    pid = lab.pid_of("node")
    lab.old_enough(pid)
    assert I._pane_command(EXACT) == "node"
    assert I.resume_session(SID) is False  # busy: left as it was
    assert signals == [] and alive(pid)


def test_an_rc_wrapper_and_its_session_child_both_matching_means_nothing_dies(lab, signals):
    """`claude rc` as the pane process with the session on its child: the child shares the wrapper's
    group without leading it, so the Remote Control server is the only target, and it is not the
    session's process: nothing is signalled, whichever pid the caller names."""
    lab.start(f"exec {lab.prog}/claude/claude --child {lab.native} {lab.pidfile}")
    server = lab.pid_of("claude/claude")
    child = lab.child_pid()
    lab.ready()
    lab.record(child)
    assert [p for p, _ in I._pane_claude_targets(EXACT)] == [server]
    stop(pid=child, min_age=1)
    stop(min_age=1)  # no pid known: its own record (the child's) does not name the server, but it is too young
    assert signals == [] and alive(server) and alive(child)


def rc_server_with_child(lab):
    """The pane process is a Remote Control server whose own command line does not name claude."""
    lab.start(f"exec {lab.prog}/rc/rcserver --child {lab.native} {lab.pidfile}")
    server = lab.pid_of("rc/rcserver")
    child = lab.child_pid()
    lab.record(child)
    return server, child


def test_with_an_rc_server_around_it_nothing_dies(lab, signals):
    """The session child never leads the terminal's foreground group, so it is not a target; the server is
    not Claude. Recovery is declined rather than guessed."""
    server, child = rc_server_with_child(lab)
    assert I._pane_claude_targets(EXACT) == []
    stop(pid=child, started=I._proc_start(child))
    assert signals == [] and alive(server) and alive(child)


def test_a_child_of_claude_with_claude_in_its_path_is_not_a_second_target(lab, signals):
    """MCP servers share Claude's foreground group (a `node` under a folder named claude…): only the
    leader counts, so the one Claude is still found and ended."""
    (lab.prog / "claudeplug").mkdir()
    shutil.copy2(lab.prog / "node", lab.prog / "claudeplug" / "node")
    lab.start(f"exec {lab.native} --child {lab.prog}/claudeplug/node {lab.pidfile}")
    pid = lab.pid_of("claude/versions/2.1.314")
    child = lab.child_pid()
    lab.record(pid)
    assert [p for p, _ in I._pane_claude_targets(EXACT)] == [pid]
    stop(pid=pid)
    assert {p for p, _ in signals} == {pid} and not alive(pid)
    REAL_KILL(child, 9) if alive(child) else None


def test_a_process_that_is_not_the_one_first_seen_is_left_alone(lab, signals):
    pid = native_in_shell(lab)
    lab.record(pid)
    stop(pid=pid, started="Mon Jan  1 00:00:00 2001")  # the pid now belongs to another process
    assert signals == [] and alive(pid)


def test_a_live_shell_job_under_the_target_stops_the_kill(lab, signals):
    lab.start(f"exec {lab.prog}/claude/versions/2.1.315 --job {lab.prog}/bin/sh '{lab.prog}/bin/sleep 600; :'")
    pid = lab.pid_of("claude/versions/2.1.315")
    lab.ready()
    until(lambda: accounts.shell_children(pid) is True, "its shell job")
    lab.record(pid)
    stop(pid=pid)
    assert signals == [] and alive(pid)


def test_after_clear_the_pane_runs_another_id_and_a_move_for_the_old_one_stops(lab, signals, monkeypatch):
    """The record of the process now names the new session: a move for the old id may not end it."""
    monkeypatch.setattr(I, "HUNG_MIN_AGE", 1)
    pid = native_in_shell(lab)
    lab.record(pid, session="the-id-after-clear")
    lab.old_enough(pid)
    stop(min_age=1)  # the old id has no record of its own: the age rule applies, and the other record refuses
    assert signals == [] and alive(pid)


# --- the callers ---------------------------------------------------------------------------------------

def reopen_stubs(monkeypatch, lab):
    monkeypatch.setattr(I, "HUNG_MIN_AGE", 1)
    monkeypatch.setattr(accounts, "shell_command", lambda *a, **k: f"echo ❯; exec {lab.prog}/idle")
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)
    # the launch preamble checks for `claude` (tested in test_usershell.py); this lab runs a stand-in instead
    monkeypatch.setattr(I, "shell_argv", lambda line, interactive=True: ["/bin/sh", "-c", line])


@pytest.mark.skipif(sys.platform != "darwin", reason="the reopened window never starts on the Linux CI runner; "
                    "the bridge runs on macOS, where this passes")
def test_a_reopen_ends_a_hung_old_claude_and_opens_the_session_again(lab, signals, monkeypatch):
    """resume_session on tmux's exact target: the stop is called, and it reads the pane on tmux 3.7."""
    reopen_stubs(monkeypatch, lab)
    lab.start(f"exec {lab.native}")
    pid = lab.pid_of("claude/versions/2.1.314")
    lab.ready()
    lab.old_enough(pid)
    assert I._pane_command(EXACT) == "2.1.314"
    assert I.resume_session(SID) is True
    assert (pid, 15) in signals and not alive(pid)
    assert lab.pid_of("idle")  # the reopened session runs in the same named window


def test_a_reopen_ends_a_hung_old_npm_claude(lab, signals, monkeypatch):
    reopen_stubs(monkeypatch, lab)
    lab.start(f"exec {lab.prog}/npm/node /x/claude-code/cli.js")
    pid = lab.pid_of("npm/node /x/claude-code/cli.js")
    lab.ready()
    lab.old_enough(pid)
    assert I.resume_session(SID) is True
    assert not alive(pid)


def test_a_reopen_spares_a_claude_that_a_record_names(lab, signals, monkeypatch):
    reopen_stubs(monkeypatch, lab)
    lab.start(f"exec {lab.native}")
    pid = lab.pid_of("claude/versions/2.1.314")
    lab.ready()
    lab.record(pid, session="some-other-session")
    lab.old_enough(pid)
    assert I.resume_session(SID) is False
    assert signals == [] and alive(pid)


def test_a_limit_move_ends_a_claude_that_ignored_exit_and_finishes(lab, signals, monkeypatch, bot):
    """The whole move on real processes: /exit is typed, the process ignores it, the stop ends it, the
    move types the resume into the shell that is left."""
    from conftest import GOOD_TOKEN
    from ctb.config import Config
    accounts.add("small")
    accounts.record(SID, "main")
    monkeypatch.setattr(accounts, "shell_command", lambda *a, **k: "echo MOVED-AND-RESUMED")
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)
    monkeypatch.setattr(I, "_idle_after_move", lambda *a, **k: None)
    monkeypatch.setattr(I.identity, "check", lambda *a, **k: None)
    monkeypatch.setattr(I.time, "sleep", lambda s: REAL_SLEEP(s * 0.04))  # 45 one-second waits
    pid = native_in_shell(lab, plain_input=True)
    lab.record(pid)
    pane = until(lambda: I._tmux("display", "-p", "-t", EXACT + ":", "#{pane_id}").stdout.strip(), "pane id")
    monkeypatch.setattr(I, "pane_for_session", lambda s: pane)
    said = []
    ok = I._account_move(Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language="en"), None, SID, "spare",
                         lambda text, **kw: said.append(text), limit={"kind": "five_hour"})
    assert ok is True, (said, signals, I._pane_command(EXACT))
    assert not alive(pid)
    assert (pid, 15) in signals and {p for p, _ in signals} == {pid}
    until(lambda: lab.screen_has("MOVED-AND-RESUMED"), "the resume typed into the shell")


# --- round 3: what a second review found ------------------------------------------------------------------

def test_with_no_pid_any_record_naming_the_process_stops_the_kill(lab, signals, monkeypatch):
    """A record that was half written on the first look is complete now: whichever session it names, the
    process is alive in the registry, so a reopen may not end it."""
    monkeypatch.setattr(I, "HUNG_MIN_AGE", 1)
    lab.start(f"exec {lab.native}")
    pid = lab.pid_of("claude/versions/2.1.314")
    lab.ready()
    lab.old_enough(pid)
    lab.record(pid, session=SID)
    stop(min_age=1)
    assert signals == [] and alive(pid)


def test_a_record_finished_between_the_two_looks_of_a_reopen_spares_the_session(lab, signals, monkeypatch):
    reopen_stubs(monkeypatch, lab)
    lab.start(f"exec {lab.native}")
    pid = lab.pid_of("claude/versions/2.1.314")
    lab.ready()
    lab.old_enough(pid)
    sessions = Path(os.environ["HOME"]) / ".claude" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    record = sessions / f"{pid}.json"
    record.write_text('{"pid": %d, "sessionId": "%s", "sta' % (pid, SID))  # half written
    real = I.session_pid

    def session_pid(session):
        found = real(session)
        record.write_text(json.dumps({"pid": pid, "sessionId": SID, "status": "idle"}))  # now finished
        return found

    monkeypatch.setattr(I, "session_pid", session_pid)
    assert I.resume_session(SID) is False  # busy: the live session is left alone
    assert signals == [] and alive(pid)


def test_a_node_dev_server_in_a_folder_named_claude_survives_a_reopen(lab, signals, monkeypatch):
    monkeypatch.setattr(I, "HUNG_MIN_AGE", 1)
    lab.start(f"exec {lab.prog}/node {lab.prog}/claude-dashboard/server.js")
    pid = lab.pid_of("node ")
    lab.old_enough(pid)
    assert I._pane_command(EXACT) == "node"
    stop(min_age=1)
    assert signals == [] and alive(pid)


@pytest.mark.parametrize("why,kw", [("no pid", dict(min_age=1)), ("known pid", dict(pid=None))])
def test_a_shell_job_stops_the_kill_on_every_path(lab, signals, monkeypatch, why, kw):
    monkeypatch.setattr(I, "HUNG_MIN_AGE", 1)
    lab.start(f"exec {lab.prog}/claude/versions/2.1.315 --job {lab.prog}/bin/sh '{lab.prog}/bin/sleep 600; :'")
    pid = lab.pid_of("claude/versions/2.1.315")
    lab.ready()
    until(lambda: accounts.shell_children(pid) is True, "its shell job")
    lab.old_enough(pid)
    kw = dict(kw, pid=pid) if why == "known pid" else kw
    stop(**kw)
    assert signals == [] and alive(pid)


@pytest.mark.parametrize("known_pid", [True, False])
def test_a_records_folder_that_cannot_be_listed_is_unknown_so_nothing_dies(lab, signals, monkeypatch, known_pid):
    monkeypatch.setattr(I, "HUNG_MIN_AGE", 1)
    lab.start(f"exec {lab.native}")
    pid = lab.pid_of("claude/versions/2.1.314")
    lab.ready()
    lab.old_enough(pid)
    lab.record(pid, session="some-other-session")
    folder = Path(os.environ["HOME"]) / ".claude" / "sessions"
    os.chmod(folder, 0)
    try:
        assert I._live_records_strict(pid) is None
        stop(**(dict(pid=pid) if known_pid else dict(min_age=1)))
    finally:
        os.chmod(folder, stat.S_IRWXU)
    assert signals == [] and alive(pid)


def move_on_real_processes(lab, monkeypatch, window=TM, subagent=False):
    """A limit move where the exit hooks remove the record and the process stays (the 2026-11-02 incident)."""
    from conftest import GOOD_TOKEN
    from ctb.config import Config
    accounts.add("small")
    accounts.record(SID, "main")
    monkeypatch.setattr(accounts, "shell_command", lambda *a, **k: "echo MOVED-AND-RESUMED")
    monkeypatch.setattr(I, "pass_trust", lambda *a, **k: True)
    monkeypatch.setattr(I, "_idle_after_move", lambda *a, **k: None)
    monkeypatch.setattr(I.identity, "check", lambda *a, **k: None)
    monkeypatch.setattr(I.time, "sleep", lambda s: REAL_SLEEP(s * 0.04))
    lab.shell(window)
    lab.type("unsetopt zle" if "zsh" in SHELL else "set +o emacs +o vi", window)
    REAL_SLEEP(0.5)
    lab.type(lab.native, window)
    pid = lab.pid_of("claude/versions/2.1.314")
    lab.ready("=" + window)
    lab.record(pid)
    record = Path(os.environ["HOME"]) / ".claude" / "sessions" / f"{pid}.json"
    pane = until(lambda: I._tmux("display", "-p", "-t", "=" + window + ":", "#{pane_id}").stdout.strip(), "pane id")
    monkeypatch.setattr(I, "pane_for_session", lambda s: pane)
    if subagent:
        folder = accounts.claude_home() / "projects" / "p" / SID / "subagents"
        folder.mkdir(parents=True)
        (folder / "a.jsonl").write_text("{}")
    real = I._tmux

    def tmux(*args):
        if args[:1] == ("send-keys",) and "/exit" in args and record.exists():
            record.unlink()
        return real(*args)

    monkeypatch.setattr(I, "_tmux", tmux)
    said = []
    ok = I._account_move(Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language="en"), None, SID, "spare",
                         lambda text, **kw: said.append(text), defer=True,
                         limit={"id": "L1", "kind": "five_hour", "reset": time.time() + 9000})
    return ok, pid, said


def test_a_vanished_record_in_a_bridge_window_still_lets_the_move_end_the_process(lab, signals, monkeypatch):
    ok, pid, said = move_on_real_processes(lab, monkeypatch)
    assert ok is True and not alive(pid) and {p for p, _ in signals} == {pid}


def test_a_vanished_record_in_a_plain_tmux_window_still_lets_the_move_end_the_process(lab, signals, monkeypatch):
    """pane_runs_session has no record to follow here: the pid read before /exit stands in for it."""
    ok, pid, said = move_on_real_processes(lab, monkeypatch, window="work")
    assert ok is True and not alive(pid) and {p for p, _ in signals} == {pid}


def test_a_deferred_move_whose_record_vanished_is_retried_once_agents_are_quiet(lab, signals, monkeypatch):
    """A subagent wrote lately, so the first try waits; its record is gone, so restart_is_safe could never
    say yes again and the retry never fired (recover.py)."""
    from ctb import recover
    ok, pid, said = move_on_real_processes(lab, monkeypatch, subagent=True)
    assert ok is False and alive(pid) and signals == []
    assert accounts.deferred_move(SID, time.time())
    later = time.time() + 3600  # the agent is long quiet and the retry gap has passed
    spawned = []
    assert recover._retry_deferred(None, None, SID, 5, later, lambda argv: spawned.append(argv),
                                   {"moved": None}, {"id": "L1"}) is True
    assert spawned and spawned[0][:2] == ["account", "auto-move"]


# --- round 4: a deferred retry types only into the process that ignored /exit --------------------------------

def deferred_cfg():
    from conftest import GOOD_TOKEN
    from ctb.config import Config
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, ui_language="en")


def keys_sent(monkeypatch):
    sent = []
    real = I._tmux

    def tmux(*args):
        if args[:1] == ("send-keys",):
            sent.append(args)
        return real(*args)

    monkeypatch.setattr(I, "_tmux", tmux)
    return sent


def quiet_agents(window_files=None):
    for f in (accounts.claude_home() / "projects").glob("*/*/subagents/*.jsonl"):
        f.unlink()


def retry(later, spawned, old):
    from ctb import recover
    return recover._retry_deferred(None, None, SID, 5, later, lambda argv: spawned.append(argv), old,
                                   {"id": "L1"})


def test_the_deferral_saves_the_process_that_ignored_exit(lab, signals, monkeypatch):
    ok, pid, said = move_on_real_processes(lab, monkeypatch, subagent=True)
    wait = accounts.deferred_move(SID, time.time())
    assert ok is False and wait["pid"] == pid and wait["started"] == I._proc_start(pid)


@pytest.mark.parametrize("how", ["clear", "resume"])
def test_after_clear_or_resume_in_the_window_the_retry_sends_no_keys(lab, signals, monkeypatch, how):
    """The owner ran /clear (or /resume) in the same process after the deferral: its record now names the new
    session, and the old id has none. The old retry counted that as gone and typed /exit into it."""
    ok, pid, said = move_on_real_processes(lab, monkeypatch, subagent=True)
    assert ok is False
    quiet_agents()
    lab.record(pid, session="the-new-conversation")
    sent = keys_sent(monkeypatch)
    spawned, old = [], {"moved": None}
    assert retry(time.time() + 3600, spawned, old) is False
    assert spawned == [] and accounts.deferred_move(SID, time.time()) is None
    # and the move itself, were it started anyway, types nothing either
    accounts.defer_move(SID, "small", time.time(), origin="main", limit={"id": "L1", "reset": time.time() + 9000},
                        proc=(pid, I._proc_start(pid)))
    assert I._account_move(deferred_cfg(), None, SID, "spare", lambda *a, **k: None, defer=True,
                           limit={"id": "L1", "kind": "five_hour"}) is False
    assert sent == [] and signals == [] and alive(pid)


def test_the_retry_stops_rearming_after_a_mismatch(lab, signals, monkeypatch):
    ok, pid, said = move_on_real_processes(lab, monkeypatch, subagent=True)
    quiet_agents()
    lab.record(pid, session="the-new-conversation")
    spawned, old = [], {"moved": None}
    later = time.time() + 3600
    assert retry(later, spawned, old) is False
    assert old["moved"] == "dropped"  # so the "another account freed up" move is not sent in its place either
    for n in range(3):
        assert retry(later + 120 * n, spawned, old) is False
    assert spawned == [] and alive(pid) and signals == []


def test_a_new_process_in_the_window_drops_the_retry(lab, signals, monkeypatch):
    ok, pid, said = move_on_real_processes(lab, monkeypatch, subagent=True)
    quiet_agents()
    wait = accounts.deferred_move(SID, time.time())
    accounts.defer_move(SID, wait["target"], time.time(), proc=(pid, "Mon Jan  1 00:00:00 2001"))
    spawned = []
    assert retry(time.time() + 3600, spawned, {"moved": None}) is False
    assert spawned == [] and alive(pid)


def test_a_deferred_move_in_a_plain_tmux_window_retries_and_recovers(lab, signals, monkeypatch):
    """No tg- name to go by: the pid saved at the deferral shows the window is still this session's."""
    ok, pid, said = move_on_real_processes(lab, monkeypatch, window="work", subagent=True)
    assert ok is False and alive(pid) and signals == []
    quiet_agents()
    spawned = []
    assert retry(time.time() + 3600, spawned, {"moved": None}) is True
    assert spawned and spawned[0][:2] == ["account", "auto-move"]
    ok = I._account_move(deferred_cfg(), None, SID, "spare", lambda *a, **k: None, defer=True,
                         limit={"id": "L1", "kind": "five_hour", "reset": time.time() + 9000})
    assert ok is True and not alive(pid) and {p for p, _ in signals} == {pid}


def test_a_live_record_for_the_session_keeps_the_ordinary_retry(lab, signals, monkeypatch):
    """Review 2026-11-02: the saved-pid check is for a vanished record only. With a live record naming the
    session (a resume by hand, an rc wrapper), a different saved process is not a reason to drop the move."""
    ok, pid, said = move_on_real_processes(lab, monkeypatch, subagent=True)
    quiet_agents()
    lab.record(pid, session=SID)
    accounts.defer_move(SID, "small", time.time(), origin="main", limit={"id": "L1", "reset": time.time() + 9000},
                        proc=(pid, "Mon Jan  1 00:00:00 2001"))
    keys_sent(monkeypatch)
    I._account_move(deferred_cfg(), None, SID, "spare", lambda *a, **k: None, defer=True,
                    limit={"id": "L1", "kind": "five_hour"})
    assert accounts.deferred_move(SID, time.time()) is not None or not alive(pid)
