"""First-run experience: `harnessgram setup`, the extra doctor checks, projects_root and engine defaults."""
import json
import os
import stat
import subprocess
import sys

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import cli, config as C, doctor, inbox, setup
from ctb.telegram import TelegramError


class SetupBot(FakeBot):
    def __init__(self, topics=True, username="demo_bot", updates=None):
        super().__init__()
        self.topics, self.username, self.updates = topics, username, updates or []

    def call(self, method, params=None, timeout=15):
        if method == "getMe":
            self.calls.append((method, {}))
            if "getMe" in self.fail:
                raise TelegramError(self.fail.pop("getMe"))
            return {"id": 1, "username": self.username, "has_topics_enabled": self.topics}
        if method == "getUpdates":
            self.calls.append((method, params or {}))
            out, self.updates = self.updates, []
            return out
        return super().call(method, params, timeout)


def run_setup(tmp_path, bot, **kw):
    lines = []
    args = dict(settings_file=tmp_path / "claude-settings.json", service=False, start=False, run_doctor=False,
                say=lines.append, env={"CTB_BOT_TOKEN": GOOD_TOKEN}, interactive=False)
    args.update(kw)
    return setup.run(bot=bot, **args), "\n".join(lines)


def test_setup_non_interactive_writes_private_config_installs_and_sends_test_message(tmp_path):
    bot = SetupBot()
    code, out = run_setup(tmp_path, bot, owner_id=4242, language="he")
    assert code == 0, out
    path = C.config_path()
    raw = json.loads(path.read_text())
    assert raw["owner_chat_id"] == 4242 and raw["target_language"] == "he" and raw["bot_token"] == GOOD_TOKEN
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    hooks = json.loads((tmp_path / "claude-settings.json").read_text())["hooks"]
    assert "Stop" in hooks and "UserPromptSubmit" in hooks
    sent = bot.methods("sendMessage")
    assert sent and sent[0]["chat_id"] == 4242 and "Harnessgram מחובר" in sent[0]["text"]  # --language he gives Hebrew menus
    assert GOOD_TOKEN not in out  # the token is never printed


def test_setup_needs_a_token_and_an_owner_when_non_interactive(tmp_path):
    code, out = run_setup(tmp_path, SetupBot(), env={}, owner_id=1)
    assert code == 1 and "CTB_BOT_TOKEN" in out
    code, out = run_setup(tmp_path, SetupBot())
    assert code == 1 and "--owner-id" in out
    assert not C.config_path().exists()  # nothing half-written


def test_setup_rejects_a_malformed_token_without_echoing_it(tmp_path):
    code, out = run_setup(tmp_path, SetupBot(), env={"CTB_BOT_TOKEN": "not-a-token"}, owner_id=1)
    assert code == 1 and "invalid format" in out and "not-a-token" not in out


def test_setup_reports_a_rejected_token(tmp_path):
    bot = SetupBot()
    bot.fail["getMe"] = "Unauthorized"
    code, out = run_setup(tmp_path, bot, owner_id=1)
    assert code == 1 and "Unauthorized" in out and not C.config_path().exists()


def test_setup_warns_when_threaded_mode_is_off(tmp_path):
    code, out = run_setup(tmp_path, SetupBot(topics=False), owner_id=7)
    assert code == 1 and "Threaded Mode is OFF" in out and "Bot Settings" in out
    assert json.loads(C.config_path().read_text())["owner_chat_id"] == 7  # config is still saved


def test_setup_interactive_pairs_through_the_start_code(tmp_path, monkeypatch):
    bot = SetupBot()
    monkeypatch.setattr(setup.pairing, "new_code", lambda: "abcd1234")
    now = 2_000_000_000
    monkeypatch.setattr(setup.time, "time", lambda: now)
    bot.updates = [{"update_id": 1, "message": {"text": "/start abcd1234", "date": now + 1,
                                                 "chat": {"id": 555, "type": "private"},
                                                 "from": {"id": 555, "first_name": "Dana", "username": "dana"}}}]
    prompts = []

    def fake_input(prompt):
        prompts.append(prompt)
        return "y" if "Pair with" in prompt else "fr"

    code, out = run_setup(tmp_path, bot, interactive=True, env={}, getpass_fn=lambda p: GOOD_TOKEN,
                          input_fn=fake_input, pair_timeout=5)
    assert code == 0, out
    assert "/start abcd1234" in out
    raw = json.loads(C.config_path().read_text())
    assert raw["owner_chat_id"] == 555 and raw["target_language"] == "fr"
    assert any("Pair with Dana" in p for p in prompts)


def test_setup_declined_pairing_saves_nothing(tmp_path, monkeypatch):
    bot = SetupBot()
    monkeypatch.setattr(setup.pairing, "new_code", lambda: "abcd1234")
    now = 2_000_000_000
    monkeypatch.setattr(setup.time, "time", lambda: now)
    bot.updates = [{"update_id": 1, "message": {"text": "/start abcd1234", "date": now + 1,
                                                 "chat": {"id": 9, "type": "private"}, "from": {"id": 9}}}]
    code, out = run_setup(tmp_path, bot, interactive=True, env={}, getpass_fn=lambda p: GOOD_TOKEN,
                          input_fn=lambda p: "n", pair_timeout=5)
    assert code == 1 and "not paired" in out and not C.config_path().exists()


def test_setup_reuses_an_existing_pairing(tmp_path):
    C.save_raw({"bot_token": GOOD_TOKEN, "owner_chat_id": 99, "target_language": "ar"})
    bot = SetupBot()
    code, out = run_setup(tmp_path, bot, env={})
    assert code == 0, out
    raw = json.loads(C.config_path().read_text())
    assert raw["owner_chat_id"] == 99 and raw["target_language"] == "ar"
    assert bot.methods("sendMessage")[0]["chat_id"] == 99


def test_setup_command_line_flags_exist():
    out = subprocess.run([sys.executable, "-m", "ctb", "setup", "--help"], capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "src")))
    assert out.returncode == 0
    for flag in ("--owner-id", "--non-interactive", "--no-install", "--yes", "--language"):
        assert flag in out.stdout


# --- doctor ---------------------------------------------------------------------------------------------

def test_doctor_tmux_checks_have_a_fix_line():
    missing = doctor.check_tmux(which=lambda name: None)
    assert missing[0][0] == "warn" and "brew install tmux" in missing[0][1]
    old = doctor.check_tmux(which=lambda n: "/x/tmux",
                            run=lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="tmux 1.8\n"))
    assert old[0][0] == "warn" and "upgrade" in old[0][1]
    good = doctor.check_tmux(which=lambda n: "/x/tmux",
                             run=lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="tmux 3.4\n"))
    assert good == [("ok", "tmux 3.4")]


def test_doctor_claude_binary_and_login():
    none = doctor.check_claude(auth=lambda n, t: {}, binary=lambda: "claude")
    if doctor.shutil.which("claude") is None:
        assert none[0][0] == "warn" and "install Claude Code" in none[0][1]
    out = doctor.check_claude(auth=lambda n, t: {"loggedIn": False}, binary=lambda: "/bin/ls")
    assert out[0][0] == "ok" and out[1][0] == "warn" and "claude auth login" in out[1][1]
    out = doctor.check_claude(auth=lambda n, t: {"loggedIn": True, "email": "a@b.c"}, binary=lambda: "/bin/ls")
    assert [lvl for lvl, _ in out] == ["ok", "ok"]
    out = doctor.check_claude(auth=lambda n, t: {}, binary=lambda: "/bin/ls")
    assert out[1][0] == "warn" and "claude auth login" in out[1][1]


class MeBot:
    def __init__(self, **me):
        self.me = me

    def call(self, method, params=None, timeout=15):
        assert method == "getMe"
        return dict({"id": 1, "username": "b"}, **self.me)


def test_doctor_bot_topics_and_privacy():
    cfg = C.Config(bot_token=GOOD_TOKEN, owner_chat_id=1)
    off = doctor.check_bot(cfg, MeBot())
    assert off[1][0] == "FAIL" and "Threaded Mode" in off[1][1] and "Fix:" in off[1][1]
    on = doctor.check_bot(cfg, MeBot(has_topics_enabled=True))
    assert on[1][0] == "ok" and on[2][0] == "ok" and "privacy" in on[2][1]
    team = C.Config(bot_token=GOOD_TOKEN, owner_chat_id=1, team=C.TeamConfig(chat_id=-100))
    warn = doctor.check_bot(team, MeBot(has_topics_enabled=True, can_read_all_group_messages=False))
    assert warn[2][0] == "warn" and "Group Privacy" in warn[2][1]
    group = doctor.check_bot(C.Config(bot_token=GOOD_TOKEN, owner_chat_id=1, chat_id=-1001), MeBot())
    assert group[1][0] == "ok" and "not needed" in group[1][1]


def test_doctor_failures_name_a_fix(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "check_tmux", lambda: [])
    monkeypatch.setattr(doctor, "check_claude", lambda: [])
    assert doctor.run(offline=True) == 1
    out = capsys.readouterr().out
    assert "harnessgram setup" in out  # no config -> the fix is the wizard
    C.save_raw({"bot_token": GOOD_TOKEN, "owner_chat_id": 1})
    doctor.run(offline=True)
    out = capsys.readouterr().out
    assert "listener is not running. Fix:" in out
    for line in out.splitlines():
        if line.startswith("[FAIL]"):
            assert "run" in line or "Fix" in line or "harnessgram" in line, line


def test_translation_defaults_are_offline_and_google_is_opt_in(capsys):
    assert C.Config().engines == ["mlx", "nllb"]
    assert "google" not in C.Config().engines
    cfg = C.Config(translate=True, engines=["google"])
    assert any(lvl == "warn" and "unofficial" in msg for lvl, msg in doctor.check_extras(cfg))
    assert not any("google" in msg for lvl, msg in doctor.check_extras(C.Config(translate=True)))


@pytest.mark.new_install
def test_optional_features_are_off_and_soft_by_default():
    lines = doctor.check_extras(C.Config())
    assert all(lvl == "ok" for lvl, _ in lines), lines  # nothing optional warns when it is simply not set up
    assert any("team radio is off" in m for _, m in lines)
    assert not any("topic names" in m for _, m in lines)
    assert any("first message" in m for _, m in doctor.check_extras(C.Config(auto_topic_names=True)))
    half = C.Config(radio={"telethon_python": "/usr/bin/python3"})
    assert any(lvl == "warn" and "rtmp_url_command" in m for lvl, m in doctor.check_extras(half))


def test_macos_privacy_names_the_interpreter(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents" / "code").mkdir(parents=True)
    assert doctor.check_macos_privacy(C.Config(), platform="linux") == []
    [(lvl, msg)] = doctor.check_macos_privacy(C.Config(), platform="darwin", python="/usr/bin/python3")
    assert lvl == "ok" and "Full Disk Access" in msg and "/usr/bin/python3" in msg
    inside = C.Config(projects_root=str(tmp_path / "Documents" / "code"))
    [(lvl, msg)] = doctor.check_macos_privacy(inside, platform="darwin", python="/usr/bin/python3")
    assert lvl == "warn" and "Full Disk Access" in msg and "Fix:" in msg


# --- projects_root --------------------------------------------------------------------------------------

def test_projects_root_setting_controls_the_picker(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / "code"
    (root / "alpha").mkdir(parents=True)
    (root / "beta").mkdir()
    monkeypatch.setattr(inbox.Path, "home", classmethod(lambda cls: home))
    enc = "".join(c if c.isalnum() else "-" for c in str(root)) + "-"
    store = home / ".claude" / "projects"
    for name, cwd in (("alpha", root / "alpha"), ("beta", root / "beta"), ("gone", root / "gone"),
                      ("alpha--worktree", root / "alpha" / ".worktrees" / "x")):
        (store / (enc + name)).mkdir(parents=True)
        (store / (enc + name) / "s.jsonl").write_text(json.dumps({"cwd": str(cwd)}) + "\n")
    cfg = C.Config(projects_root=str(root))
    found = inbox.projects(cfg)
    assert set(found) == {str(root / "alpha"), str(root / "beta"), str(root)}  # "gone" does not exist
    assert inbox.projects_root(C.Config()) == home  # default: the home directory
    assert inbox.projects_root(cfg) == root


@pytest.mark.parametrize("assume_yes", [False, True])
def test_unattended_setup_keeps_the_paired_owner(tmp_path, assume_yes):
    C.save_raw({"bot_token": GOOD_TOKEN, "owner_chat_id": 5}, replace_owner=True)
    res, out = run_setup(tmp_path, SetupBot(), owner_id=6, assume_yes=assume_yes,
                         install_hooks=False, test_message=False)
    assert res == 1 and "Setup stopped:" in out
    assert json.loads(C.config_path().read_text())["owner_chat_id"] == 5
