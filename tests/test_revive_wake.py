"""Reopened sessions carry on by themselves: after the 15.11 reboot ten sessions came back but sat idle.

The reopened Claude process is started with the carry-on prompt as its own first prompt argument, so only
that process receives it. Nothing is typed into a pane."""
import json
import shlex

from ctb import hooks, inbox, revive
from conftest import GOOD_TOKEN
from ctb.config import Config

HOSTILE = 'it\'s "quoted" $(touch /tmp/pwned) `id` ; & | > x\nsecond line \\ [Harnessgram: y] -- --flag'


def test_wake_text_is_a_harnessgram_bracket():
    assert revive.WAKE_TEXT.startswith("[Harnessgram: ") and revive.WAKE_TEXT.endswith("]")
    assert "continue" in revive.WAKE_TEXT and "Do not redo" in revive.WAKE_TEXT


def test_real_reopen_hands_the_prompt_to_resume_session(monkeypatch):
    monkeypatch.setattr(revive, "open_windows", lambda: [])
    calls = []
    monkeypatch.setattr(inbox, "resume_session", lambda sid, first_prompt=None: calls.append((sid, first_prompt)) or True)
    assert revive.reopen(["aaaaaaaa-1", "bbbbbbbb-2"], sleep_fn=lambda s: None) == ["aaaaaaaa-1", "bbbbbbbb-2"]
    assert calls == [("aaaaaaaa-1", revive.WAKE_TEXT), ("bbbbbbbb-2", revive.WAKE_TEXT)]


def test_injected_resume_is_called_without_a_prompt(monkeypatch):
    monkeypatch.setattr(revive, "open_windows", lambda: ["tg-bbbbbbbb"])
    calls = []
    def resume(*args, **kwargs):
        calls.append((args, kwargs))
        return args[0] != "cccccccc-3"
    done = revive.reopen(["aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3"], resume=resume, sleep_fn=lambda s: None)
    assert done == ["aaaaaaaa-1"]  # the open one is skipped, the failing one never reopens
    assert all(kwargs == {} and len(args) == 1 for args, kwargs in calls)


def _resume(monkeypatch, first_prompt, **kwargs):
    """Run the real resume_session with tmux faked; return every tmux call."""
    calls = []
    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout = rc, out
    def tmux(*args):
        calls.append(args)
        if args[0] == "has-session":
            return R(1)  # no window by that name yet
        return R(0, "❯ ready" if args[0] == "capture-pane" else "")
    monkeypatch.setattr(inbox, "_tmux", tmux)
    monkeypatch.setattr(inbox, "shell_argv", lambda line: ["sh", "-c", line])
    monkeypatch.setattr(inbox.accounts, "shell_command", lambda s: "claude")
    monkeypatch.setattr(inbox.accounts, "account_for", lambda s: "main")
    monkeypatch.setattr(inbox.accounts, "roomier_account", lambda c: None)
    monkeypatch.setattr(inbox, "session_folder", lambda s: "/tmp")
    monkeypatch.setattr(inbox.time, "sleep", lambda s: None)
    assert inbox.resume_session("aaaaaaaa-1", **({"first_prompt": first_prompt} if first_prompt is not None else {}))
    return calls


def _new_session(calls):
    return next(c for c in calls if c[0] == "new-session")


def test_resume_session_passes_the_prompt_as_one_quoted_argument(monkeypatch):
    argv = _new_session(_resume(monkeypatch, HOSTILE))
    assert argv[:7] == ("new-session", "-d", "-s", "tg-aaaaaaaa", "-c", "/tmp", "sh")
    line = argv[-1]
    assert line.startswith("claude --remote-control --resume aaaaaaaa-1 ")
    # the shell reads the whole prompt back as exactly one word, byte for byte
    assert shlex.split(line) == ["claude", "--remote-control", "--resume", "aaaaaaaa-1", HOSTILE]


def test_resume_session_without_a_prompt_builds_the_same_command_as_before(monkeypatch):
    for kwargs in (None, ""):
        line = _new_session(_resume(monkeypatch, kwargs))[-1]
        assert line == "claude --remote-control --resume aaaaaaaa-1"


def test_carry_on_prompt_is_not_an_owner_prompt(monkeypatch):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=1)
    spawned, named = [], []
    monkeypatch.setattr(hooks, "load_config", lambda: cfg)
    monkeypatch.setattr(hooks, "empty_wake", lambda *a: False)
    monkeypatch.setattr(hooks, "_direct", lambda *a: None)
    monkeypatch.setattr(hooks, "prompt_output", lambda *a: None)
    monkeypatch.setattr(hooks, "spawn", lambda args, **kw: spawned.append(args[0]))
    monkeypatch.setattr(hooks, "save_session_name", lambda *a: named.append(a), raising=False)
    from ctb import topics
    monkeypatch.setattr(topics, "save_session_name", lambda *a: named.append(a))
    cleared = []
    monkeypatch.setattr(inbox, "clear_nudge_pause", lambda s: cleared.append(s))

    def run(prompt):
        spawned.clear(), named.clear(), cleared.clear()
        hooks.hook_prompt(json.dumps({"session_id": "s", "prompt": prompt, "cwd": "/work/proj"}))
        return list(spawned), list(named), list(cleared)

    spawns, names, clears = run(revive.WAKE_TEXT)
    assert "prompt-mirror" not in spawns and names == [] and clears == []  # not the owner's words
    spawns, names, clears = run("  " + revive.WAKE_TEXT)  # leading blanks do not change that
    assert "prompt-mirror" not in spawns and names == [] and clears == []
    spawns, names, clears = run("please fix the build")  # a real owner prompt still does all three
    assert "prompt-mirror" in spawns and len(names) == 1 and clears == ["s"]
