"""Shared fixtures: an isolated config/state directory, a fake Telegram bot, and a guard that keeps every test
away from the machine's live tmux server and from starting a real `claude`.

Why the guard (2026-11-10): `_queue_owner` starts a daemon thread (`_reopen_or_nudge`) that can outlive its test.
After the test's teardown monkeypatch had restored the real environment (TMUX_TMPDIR unset), so the thread's
`resume_session` ran `tmux new-session ... claude --resume <test session>` on the LIVE default tmux server. Now:
- the whole run starts inside a session sandbox (config, state, home, Claude dirs and TMUX_TMPDIR all private), so
  what a per-test monkeypatch "restores" is the sandbox, never the real machine;
- every subprocess that would run tmux or claude is refused with LiveProcessBlocked (an OSError, so the code under
  test sees "tmux unavailable"), recorded, and fails the test it happens in, or the run when it happens after the
  last test. A test that needs a real tmux asks for the `private_tmux` fixture and must use a private server (its
  own -S socket, or an existing private TMUX_TMPDIR); even then a real `claude` is never started.
"""
import contextlib
import functools
import inspect
import os
import re
import shlex
import subprocess
import tempfile
import threading

import pytest

from ctb.telegram import TelegramError


class LiveProcessBlocked(OSError):
    """A test tried to run tmux or claude outside a private, opted-in tmux server."""


VIOLATIONS = []  # every refused command: {"cmd", "thread", "why"}
_STATE = {"private_tmux": False}
_LOCK = threading.Lock()
_REAL_POPEN_INIT = subprocess.Popen.__init__
_PUBLIC_TMP = {"/tmp", "/private/tmp"}
_SHELLS = {"sh", "bash", "zsh", "dash", "fish"}
_CLAUDE_WORD = re.compile(r"(?:^|[\s;&|/(])claude(?:\s|$)")
_TMUX_WORD = re.compile(r"(?:^|[\s;&|/(])tmux(?:\s|$)")


def _argv(args):
    if isinstance(args, os.PathLike):  # one path, never a command line: spaces stay inside it (review of #161)
        return [os.fsdecode(args)]
    if isinstance(args, (str, bytes)):
        text = os.fsdecode(args)
        try:
            return shlex.split(text)
        except ValueError:
            return text.split()
    return [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in args]


# claude as a whole word of a command line, also right before ; & | ) or inside quotes (review of #161)
_CLAUDE_TOKEN = re.compile(r"""(?:^|[\s;&|("'])([^\s;&|()"']*/)?claude(?=[\s;&|)"']|$)""")


def _real_claude_in(text: str) -> bool:
    """A command line that would start the real claude: the bare command (found on PATH) or a path to it
    outside the temp folders. Fake `…/claude` programs a test builds under its tmp_path are not."""
    temp = tuple(os.path.realpath(p) + os.sep for p in {tempfile.gettempdir(), "/tmp", "/private/tmp"})
    for m in _CLAUDE_TOKEN.finditer(text):
        folder = m.group(1)
        if not folder or not os.path.realpath(folder).startswith(temp):
            return True
    return False


def _private_dir(path) -> bool:
    if not path:
        return False
    real = os.path.realpath(path)
    return os.path.isdir(real) and real not in _PUBLIC_TMP


def _private_server(argv, env) -> bool:
    """tmux would talk to a private server: an explicit -S socket that is not a default one, a non-default -L name
    under a private TMUX_TMPDIR, or a private TMUX_TMPDIR for the default name."""
    tmpdir = (env if env is not None else os.environ).get("TMUX_TMPDIR", "")
    flags, i = {}, 1  # only tmux's own flags, before the command: `select-pane -L` is no socket (review of #149)
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
        if argv[i] in ("-S", "-L", "-f", "-c", "-T") and i + 1 < len(argv):
            flags[argv[i]] = argv[i + 1]
            i += 2
        else:
            i += 1
    if "-S" in flags:
        sock = os.path.realpath(flags["-S"]) if flags["-S"] else ""
        folder = os.path.dirname(sock)  # the live server's sockets are /tmp/tmux-<uid>/<name>
        return bool(sock) and not (os.path.basename(folder).startswith("tmux-")
                                   and os.path.dirname(folder) in _PUBLIC_TMP)
    if "-L" in flags:
        return bool(flags["-L"]) and _private_dir(tmpdir)  # any name lives under the private TMUX_TMPDIR
    return _private_dir(tmpdir)


def refusal(args, env=None, executable=None) -> str:
    """Why this command may not run in a test, or ""."""
    argv = _argv(args)
    if not argv:
        return ""
    name = os.path.basename(str(executable or argv[0]))
    if name == "claude":
        return "a real claude"
    if name in _SHELLS:
        rest = " ".join(argv[1:])
        if _CLAUDE_WORD.search(rest):
            return "a shell that starts claude"
        if _TMUX_WORD.search(rest):
            return "a shell that runs tmux"
    if name == "tmux":
        if not _STATE["private_tmux"]:
            return "tmux without the private_tmux fixture"
        if not _private_server(argv, env):
            return "tmux on a server that is not private"
        if _real_claude_in(" ".join(argv[1:])):  # `new-session ... claude`: never, even on a private server
            return "a tmux session that starts claude"
    return ""


def _guarded_init(self, args, *a, **kw):
    why = refusal(args, kw.get("env"), kw.get("executable"))
    if why:
        cmd = " ".join(_argv(args))[:200]
        with _LOCK:
            VIOLATIONS.append({"cmd": cmd, "thread": threading.current_thread().name, "why": why})
        raise LiveProcessBlocked(f"blocked in tests ({why}): {cmd}")
    _REAL_POPEN_INIT(self, args, *a, **kw)


@contextlib.contextmanager
def expect_blocked():
    """For guard tests: the refusals made inside are expected; yields the list of them."""
    with _LOCK:
        start = len(VIOLATIONS)
    seen = []
    try:
        yield seen
    finally:
        with _LOCK:
            seen.extend(VIOLATIONS[start:])
            del VIOLATIONS[start:]


_SANDBOX_VARS = ("CTB_CONFIG_DIR", "CTB_STATE_DIR", "CTB_PROFILES_DIR", "CTB_CLAUDE_HOME", "CTB_CLAUDE_JSON",
                 "HOME", "TMUX_TMPDIR", "CLAUDE_CONFIG_DIR")


@pytest.fixture(scope="session", autouse=True)
def session_sandbox(tmp_path_factory):
    """The baseline every per-test restore goes back to: private dirs, no tmux session, no Claude session id.
    Installed for the whole run and never removed while the run lasts (a thread left by a test may still run)."""
    root = tmp_path_factory.mktemp("sandbox")
    for var in _SANDBOX_VARS:
        path = root / var.lower()
        if var != "CTB_CLAUDE_JSON":
            path.mkdir(parents=True, exist_ok=True)
        os.environ[var] = str(path)
    for var in ("TMUX", "TMUX_PANE", "CLAUDE_CODE_SESSION_ID"):
        os.environ.pop(var, None)
    subprocess.Popen.__init__ = _guarded_init
    yield root


@pytest.fixture(autouse=True)
def no_live_processes():
    """Fails the test in which tmux or claude was refused (also from a thread an earlier test left running)."""
    with _LOCK:
        start = len(VIOLATIONS)
    yield
    with _LOCK:
        new = VIOLATIONS[start:]
        del VIOLATIONS[start:]
    if new:
        pytest.fail("tests must never reach the live tmux server or start claude: "
                    + "; ".join(f"{v['why']} in thread {v['thread']}: {v['cmd']}" for v in new), pytrace=False)


@pytest.fixture
def private_tmux():
    """Opt in to a REAL tmux, on a private server only (own -S socket or a private TMUX_TMPDIR)."""
    _STATE["private_tmux"] = True
    yield
    _STATE["private_tmux"] = False


def pytest_sessionfinish(session, exitstatus):
    if VIOLATIONS:  # refused after the last test ended: a thread outlived its test
        print("\nblocked after the last test: " + "; ".join(f"{v['why']} in thread {v['thread']}: {v['cmd']}" for v in VIOLATIONS))
        session.exitstatus = 1


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own config and state dirs; nothing touches the real home."""
    monkeypatch.setenv("CTB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    # no late spinner-closing timers leaking from one test into the next; its own tests turn it on
    import ctb.inbox
    monkeypatch.setattr(ctb.inbox, "TAP_GRACE", None)
    # Claude accounts: never the real profiles, home folder or login of the machine running the tests.
    monkeypatch.setenv("CTB_PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.setenv("CTB_CLAUDE_HOME", str(tmp_path / "claude-home"))
    monkeypatch.setenv("CTB_CLAUDE_JSON", str(tmp_path / "claude.json"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    home_dir = tmp_path / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home_dir))
    # tmux: never the live server. tmux prefers $TMUX, and falls back to the live server when TMUX_TMPDIR points at a
    # directory that does not exist, so the private directory must exist. A test that needs a real tmux starts its own.
    no_tmux = tmp_path / "no-tmux"
    no_tmux.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TMUX_TMPDIR", str(no_tmux))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    from ctb import accounts, i18n, inbox
    # A queued owner message wakes its session from a daemon thread (`_reopen_or_nudge`) that sleeps for
    # seconds (up to a minute when it reopens) and then reads the pane: it outlived its test, ran with the
    # fixtures gone and was refused by the guard after the last test. No test waits for it, so it is a no-op
    # by default; a test of the wake itself asks for the `real_wake` fixture.
    _STATE.setdefault("wake", inbox._reopen_or_nudge)  # the real one, for the real_wake fixture
    monkeypatch.setattr(inbox, "_reopen_or_nudge", lambda box, session: None)
    # The UI language is process-wide (config.load() sets it): every test starts on the default, so one that
    # loaded an English config cannot turn the next test's Hebrew strings into English.
    monkeypatch.setattr(i18n, "_current_lang", "he")
    from ctb import policy, translate
    # The mirroring switch is read from the config file; most tests build their settings in memory, so it is
    # on here. Tests of the switch itself ask for the `real_policy` fixture.
    monkeypatch.setattr(policy, "on", lambda: True)
    translate._creds.clear()  # a credential remembered by an earlier test never reaches the next one
    monkeypatch.setattr(translate, "warm_later", lambda texts, cfg=None: False)  # never a detached process
    monkeypatch.setattr(accounts, "auth_status", lambda name, timeout=30: {})  # never run the real `claude`
    monkeypatch.setattr(accounts, "_keychain_read", lambda service, timeout=5.0: None)  # never the real Keychain
    monkeypatch.setattr(accounts, "_http_json", lambda *a, **k: (0, {}, 0.0))  # never the network
    return tmp_path


@pytest.fixture(autouse=True)
def existing_install_defaults(request, isolated, monkeypatch):
    """Keep legacy tests on the existing-install defaults unless they opt out."""
    if request.node.get_closest_marker("new_install"):
        return

    from ctb import config

    names = ("auto_topic_names", "topic_check_reminders", "auto_account_switch",
             "quota_auto_continue", "session_auto_revive", "progress", "progress_details")
    original_init = config.Config.__init__
    signature = inspect.signature(original_init)

    @functools.wraps(original_init)
    def init_with_existing_defaults(self, *args, **kwargs):
        supplied = signature.bind_partial(self, *args, **kwargs).arguments
        for name in names:
            if name not in supplied:
                kwargs[name] = True
        original_init(self, *args, **kwargs)

    original_load = config.load

    @functools.wraps(original_load)
    def load_existing(*args, **kwargs):
        state = config.state_dir()
        created_dir = not state.exists()
        state.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(prefix=".test-existing-", dir=state):
                try:
                    return original_load(*args, **kwargs)
                except config.ConfigError:
                    if args or kwargs or config.config_path().exists():
                        raise
                    return config.Config()  # a legacy test with no settings file: the old defaults
        finally:
            try:
                if created_dir:
                    state.rmdir()
            except OSError:
                pass

    monkeypatch.setattr(config.Config, "__init__", init_with_existing_defaults)
    monkeypatch.setattr(config, "load", load_existing)


@pytest.fixture
def real_wake(monkeypatch):
    """The real `_reopen_or_nudge` for a test that calls it directly (never from a background thread)."""
    from ctb import inbox
    monkeypatch.setattr(inbox, "_reopen_or_nudge", _STATE["wake"])
    return _STATE["wake"]


GOOD_TOKEN = "123456:" + "A" * 35  # shape of a real BotFather token


class FakeBot:
    """Records calls; `fail` maps a method name to a TelegramError description to raise once."""

    def __init__(self):
        self.calls = []
        self.fail = {}
        self.next_thread = 100

    def call(self, method, params=None, timeout=15):
        self.calls.append((method, params or {}))
        if method in self.fail:
            raise TelegramError(self.fail.pop(method))
        if method == "createForumTopic":
            self.next_thread += 1
            return {"message_thread_id": self.next_thread}
        if method == "sendMessage":
            mid = len(self.calls)
            if params is not None and isinstance(params, dict):
                params.setdefault("message_id", mid)
            return {"message_id": mid}
        return {}

    def upload(self, method, fields, file_field, filename, payload, timeout=120):
        self.calls.append((method, dict(fields, _file=filename, _bytes=len(payload))))
        return {}

    def methods(self, name):
        return [p for m, p in self.calls if m == name]


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture
def real_policy(monkeypatch):
    """The mirroring switch as production reads it: the config file, read at every call."""
    from ctb import policy
    monkeypatch.setattr(policy, "on", policy.__dict__["_on_from_file"])
    return policy


@pytest.fixture(autouse=True)
def _claude_probe_answers_found(monkeypatch):
    """Before a session window opens, usershell.probe asks the user's real shell whether it finds claude. Tests
    must not start the machine's shell profile, so every test gets "found"; the probe's own tests in
    test_usershell.py run the real one (with a stand-in program and a private HOME)."""
    from ctb import usershell
    monkeypatch.setattr(usershell, "probe", lambda *a, **k: True)


@pytest.fixture(autouse=True)
def _slash_typing_gate_is_a_plain_typist(request, monkeypatch):
    """Most tests have a fake tmux with no readable input box, and their subject is not the typing gate. For them
    `inbox.type_command` (a verified read of an empty box, the exact line, a verified send) is a plain typist:
    the line and Enter through whatever `inbox._tmux` is then. The gate's own rules are tested for real in
    test_compact_typing_gate.py (it asks for the real gate with the `real_type_command` marker)."""
    if request.node.get_closest_marker("real_type_command") or os.environ.get("CTB_TEST_REAL_GATE"):
        return
    import ctb.inbox as inbox

    def plain(session, pane, line, settle=1.0):
        inbox.typed_mark(line)
        first = inbox._tmux("send-keys", "-t", pane, "-l", line)
        second = inbox._tmux("send-keys", "-t", pane, "Enter")
        ok = all(getattr(r, "returncode", 0) == 0 for r in (first, second))  # some fakes return nothing
        return "sent" if ok else "failed"

    monkeypatch.setattr(inbox, "type_command", plain)


def pytest_configure(config):
    config.addinivalue_line("markers", "real_type_command: use the real inbox.type_command gate, not the plain typist")


class BoxedTmux:
    """A fake tmux with a live Claude Code input box under whatever the wrapped fake shows: `send-keys -l` types
    into it, Enter sends it (`sent`), BSpace and C-u erase, and capture-pane draws it between two rules, so the
    real `inbox.type_command` gate can read, type and verify. Every call is still passed on to the wrapped
    fake (its recorded keys and screens stay as the test wrote them). `box` may be set by a test to put a
    leftover or a draft there."""

    RULE = "─" * 60

    def __init__(self, inner=None, box=""):
        self.inner, self.box, self.sent, self.calls = inner, box, [], []

    def _forward(self, args):
        import subprocess as _sp
        res = self.inner(*args) if self.inner is not None else None
        return res if res is not None else _sp.CompletedProcess(args, 0, "", "")

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        res = self._forward(args)
        if args and args[0] == "send-keys":
            keys = list(args[3:])
            if keys[:2] == ["-l", "--"]:
                self.box += keys[2]
            elif keys[:1] == ["-l"]:
                self.box += keys[1]
            else:
                for key in keys:
                    if key == "Enter":
                        if self.box:
                            self.sent.append(self.box)
                            self.box = ""
                    elif key == "BSpace":
                        self.box = self.box[:-1]
                    elif key == "C-u":
                        self.box = ""
        elif args and args[0] == "capture-pane":
            import subprocess as _sp
            inner_out = getattr(res, "stdout", "")
            out = (inner_out if isinstance(inner_out, str) else "") + f"\n{self.RULE}\n❯ {self.box}\n{self.RULE}\n"
            return _sp.CompletedProcess(args, 0, out, "")
        return res
