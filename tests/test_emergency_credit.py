"""Emergency credit lifecycle, using fake panes/meters and isolated state only."""
import json
import re
import shlex
import signal
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FakeBot, GOOD_TOKEN, signed_in
from ctb import (accounts, assistant, cockpit_notice, credit_mode as C, decisions as D,
                 heartbeat, hooks, inbox as I, mirror as M, panel_settings,
                 recover, sensitive_buttons, status, usage)
from ctb.config import Config, locked_map, read_map, state_dir

SID = "a" * 8 + "-" + "-".join(["b" * 4] * 3) + "-" + "c" * 12
NOW = datetime(2026, 11, 16, 10, tzinfo=timezone.utc).timestamp()
TEXT = "You've hit your limit · resets 8pm"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    signed_in(accounts.MAIN, accounts.CREDIT)
    for key in accounts.CREDIT_SETTINGS_PATH_OVERRIDES:
        monkeypatch.delenv(key, raising=False)
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=12345, ui_language="he", translate=False,
                 enforce_language=False)
    bot = FakeBot()
    found = SimpleNamespace(chat=12345, thread=7, away=None)
    transcript = tmp_path / (SID + ".jsonl")
    transcript.write_text("")
    monkeypatch.setattr(C.time, "time", lambda: NOW)
    monkeypatch.setattr(C, "_provider", lambda: {})
    monkeypatch.setattr(usage, "limit_of", lambda s: {"id": "limit-one", "reset": NOW + 3600})
    monkeypatch.setattr(status, "transcript_of", lambda s: str(transcript))
    monkeypatch.setattr(status, "session_cwd", lambda s: str(tmp_path))
    monkeypatch.setattr(assistant, "MANAGED", (tmp_path / "managed-mac", tmp_path / "managed-linux"))
    monkeypatch.setattr(C, "_options", lambda s: ("claude-sonnet-4", "plan"))
    monkeypatch.setattr(C, "_has_live_process", lambda s: True)
    monkeypatch.setattr(accounts, "spent", lambda *a, **kw: True)
    monkeypatch.setattr(accounts, "chrome_available", lambda: False)
    monkeypatch.setattr(D.decision_sign, "key_for_answer", lambda *a: None)
    monkeypatch.setattr(cockpit_notice, "url", lambda *a: "")
    monkeypatch.setattr(I, "place", lambda *a: found)
    monkeypatch.setattr(M, "_essential_place", lambda *a: found)
    monkeypatch.setattr(I, "poll_log", lambda *a: None)
    accounts.record(SID, accounts.MAIN)
    return cfg, bot, found, transcript


def offer(setup):
    cfg, bot, found, _ = setup
    assert C.offer(cfg, bot, SID, TEXT, found)
    rec = D.open_decisions()[0]
    # The public panel view deliberately omits Telegram delivery metadata.
    return D._load(state_dir() / D.STATE_FILE)[rec["id"]]


def approve(setup):
    rec = offer(setup)
    return D.answer(setup[0], I.default_inbox(), rec["id"], "yes", now=NOW, by=12345, via="panel")


def interval(setup, *, session=SID, start=NOW - 1, ready=True, reset=NOW + 3600):
    path = setup[3]
    with locked_map(C._path()) as data:
        data.setdefault("intervals", {})[session] = {
            "session": session, "started_at": start, "end": None, "day": C._day(start),
            "origin": "main", "reset": reset, "limit_id": "limit-one", "transcript": str(path),
            "model": "claude-sonnet-4", "mode": "plan", "ready": ready}
    accounts.record(session, accounts.CREDIT)


def usage_row(path, tokens, *, stamp=NOW, ident="message", model="claude-sonnet-4"):
    with path.open("a") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                            "message": {"id": ident, "model": model, "usage": {"input_tokens": tokens}}}) + "\n")


def test_one_card_same_exhaustion_persists_and_has_two_buttons(setup):
    cfg, bot, found, _ = setup
    rec = offer(setup)
    assert not C.offer(cfg, bot, SID, TEXT, found)
    assert len(D.open_decisions()) == 1
    assert rec["routine"] is False and rec["action"] == "emergency_credit"
    assert "$10.00" in rec["title"] and rec["yes"] == C.YES and rec["no"] == C.NO
    notices = [p for method, p in bot.calls if method == "sendMessage"]
    assert len(notices) == 1
    assert notices[0]["text"] == rec["title"]
    assert [b["text"] for b in notices[0]["reply_markup"]["inline_keyboard"][0]] == [C.YES, C.NO]
    assert notices[0]["reply_markup"]["inline_keyboard"][-1][0]["callback_data"] == "more:dismiss"


@pytest.mark.parametrize("mark", ["none", "matching", "legacy", "other_chat", "other_thread", "raced"])
def test_closed_credit_topic_reconciles_only_its_unchanged_mark(setup, mark):
    from ctb import sessions_menu as sm
    cfg, bot, found, _ = setup
    if mark != "none":
        sm._set_mark(SID, 8 if mark == "other_thread" else found.thread,
                     chat=None if mark == "legacy" else 54321 if mark == "other_chat" else found.chat)
    before = sm.closed_marks()
    real = bot.call

    def call(method, params=None, timeout=15):
        if method == "reopenForumTopic" and mark == "raced":
            sm._set_mark(SID, found.thread, auto=True, chat=found.chat)
        return real(method, params, timeout)

    bot.call = call
    bot.fail["sendMessage"] = "Bad Request: TOPIC_CLOSED"
    rec = offer(setup)
    assert bot.methods("reopenForumTopic") == [{"chat_id": found.chat, "message_thread_id": found.thread}]
    assert D._load(state_dir() / D.STATE_FILE)[rec["id"]]["notice_message"] > 0
    if mark in ("matching", "legacy", "none"):
        assert not sm.closed_marks()
    elif mark == "raced":
        assert sm.closed_marks()[SID]["auto"] is True
    else:
        assert sm.closed_marks() == before


def test_failed_credit_topic_reopen_keeps_close_mark_and_allows_notice_retry(setup):
    from ctb import sessions_menu as sm
    cfg, bot, found, _ = setup
    sm._set_mark(SID, found.thread, chat=found.chat)
    before = sm.closed_marks()
    bot.fail.update(sendMessage="Bad Request: TOPIC_CLOSED", reopenForumTopic="Forbidden")
    rec = offer(setup)
    assert sm.closed_marks() == before
    assert not D._load(state_dir() / D.STATE_FILE)[rec["id"]].get("notice_message")
    assert len(bot.methods("sendMessage")) == 1
    assert cockpit_notice.update(cfg, rec, True, bot)


@pytest.mark.parametrize("reason", ["off", "capped", "unknown"])
def test_no_offer_off_capped_or_unknown(setup, monkeypatch, reason):
    cfg, bot, found, _ = setup
    if reason == "off":
        cfg.emergency_credit = False
    else:
        monkeypatch.setattr(C, "spend", lambda *a, **kw: 10 if reason == "capped" else None)
    assert not C.offer(cfg, bot, SID, TEXT, found)
    assert D.open_decisions() == []
    assert not bot.calls


def test_limit_path_offers_credit_only_when_all_subscriptions_empty(setup, monkeypatch):
    cfg, bot, _, _ = setup
    monkeypatch.setattr(accounts, "note_limit", lambda *a: None)
    monkeypatch.setattr(M.identity, "check", lambda *a, **kw: None)
    plan = {"auto": True, "fresh": [], "others": [], "here": "main"}
    monkeypatch.setattr(accounts, "fallback_plan", lambda s, **kw: plan)
    assert not M.limit_offer(cfg, bot, SID, "ordinary reply")
    assert M.limit_offer(cfg, bot, SID, TEXT)
    assert not M.limit_offer(cfg, bot, SID, TEXT)
    assert len(D.open_decisions()) == 1


def test_available_subscription_uses_existing_flow(setup, monkeypatch):
    cfg, bot, _, _ = setup
    monkeypatch.setattr(accounts, "note_limit", lambda *a: None)
    monkeypatch.setattr(M.identity, "check", lambda *a, **kw: None)
    monkeypatch.setattr(accounts, "fallback_plan", lambda s, **kw: {"auto": True, "fresh": ["other"], "others": ["other"], "here": "main"})
    monkeypatch.setattr(M, "send_essential", lambda *a: True)
    calls = []
    assert M.limit_offer(cfg, bot, SID, TEXT, spawn_fn=calls.append)
    assert calls and "--target=other" in calls[0]
    assert D.open_decisions() == []


@pytest.mark.parametrize("auto", [False, True])
def test_credit_offer_refreshes_stale_subscription_usage_even_with_auto_off(setup, monkeypatch, auto):
    cfg, bot, _, _ = setup
    events, spawned = [], []
    plan = {"auto": auto, "fresh": [], "others": ["spare"], "here": "main"}
    monkeypatch.setattr(accounts, "note_limit", lambda *a: None)
    monkeypatch.setattr(M.identity, "check", lambda *a, **kw: None)
    monkeypatch.setattr(M, "send_essential", lambda *a: True)
    monkeypatch.setattr(M.alerts, "alert", lambda *a, **kw: None)

    def current_plan(session):
        events.append("plan")
        return dict(plan)

    def refresh(**kw):
        assert kw == {"max_age": accounts.MOVE_USAGE_MAX_AGE, "timeout": 3.0}
        events.append("refresh")
        plan["fresh"] = ["spare"]

    monkeypatch.setattr(accounts, "fallback_plan", current_plan)
    monkeypatch.setattr(accounts, "ensure_usage", refresh)
    assert M.limit_offer(cfg, bot, SID, TEXT, spawn_fn=spawned.append)
    assert events == ["plan", "refresh", "plan"]
    assert D.open_decisions() == []
    assert bool(spawned) == auto
    if auto:
        assert "--target=spare" in spawned[0]


def test_one_tap_restarts_same_session_replays_and_never_queues_approval(setup, monkeypatch):
    cfg, bot, _, _ = setup
    answer = approve(setup)
    box = I.default_inbox()
    assert not box.pending(SID)
    with pytest.raises(D.DecisionError):
        D.answer(cfg, box, answer["id"], "yes", now=NOW)
    moves, replay, resumes = [], [], []
    def move(c, b, sid, target, say, **kw):
        accounts.record(sid, target)
        C.starting(sid, NOW)
        moves.append((sid, target, accounts.shell_command(sid), C.resume_flags(sid)))
        return True
    monkeypatch.setattr(I, "account_move", move)
    monkeypatch.setattr(I, "_queue_replay", lambda c, b, sid: replay.append(sid) or 1)
    monkeypatch.setattr(I, "mark_resume_pending", lambda *a: resumes.append(a))
    monkeypatch.setattr(I, "resume_after_move", lambda *a, **kw: "nudged")
    monkeypatch.setattr(M, "send_essential", lambda *a: True)
    C.tick(cfg, bot, NOW)
    C.tick(cfg, bot, NOW)
    assert len(moves) == 1 and moves[0][0:2] == (SID, "credit")
    argv = shlex.split(moves[0][2])
    assert argv[argv.index("--setting-sources") + 1] == "user"
    settings = json.loads(argv[argv.index("--settings") + 1])
    helper = settings.pop("apiKeyHelper")
    assert settings == {"forceLoginMethod": "console", "env": accounts.CREDIT_AUTH_OVERRIDES}
    assert helper.endswith(" credit-key") and shlex.split(helper)[0].startswith("/")
    assert "CLAUDE_CODE_API_KEY_HELPER_TTL_MS=60000" in argv
    assert "CTB_CREDIT_SESSION=" + SID in argv
    assert moves[0][3] == " --model claude-sonnet-4 --permission-mode plan"
    assert replay == [SID] and resumes == [(SID, 1, "credit")]
    assert accounts.origin_account(SID) == accounts.CREDIT
    assert C.active(SID)["day"] == C._day(NOW)


def credit_settings_files():
    folder = Path(status.session_cwd(SID)) / ".claude"
    return {"managed": assistant.MANAGED[0] / "managed-settings.json",
            "managed-linux": assistant.MANAGED[1] / "managed-settings.json",
            "managed-drop-in": assistant.MANAGED[0] / "managed-settings.d/auth.json",
            "account": accounts.claude_home() / "settings.json",
            "user": Path.home() / ".claude/settings.json",
            "project": folder / "settings.json", "local": folder / "settings.local.json"}


def test_credit_command_pins_auth_endpoint_proxy_and_renewal_env(setup, monkeypatch):
    interval(setup)
    for key in accounts.CREDIT_AUTH_OVERRIDES:
        monkeypatch.setenv(key, "fake-inherited-override")
    for key in accounts.CREDIT_SETTINGS_PATH_OVERRIDES:
        path = setup[3].parent / (key + ".json")
        path.write_text("{}")
        monkeypatch.setenv(key, str(path))
    command = accounts.shell_command(SID)
    argv = shlex.split(command)
    env = json.loads(argv[argv.index("--settings") + 1])["env"]
    assert env["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"
    assert env["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"] == "60000"
    assert env["NODE_TLS_REJECT_UNAUTHORIZED"] == "1"
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS",
                "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                "CLAUDE_CODE_USE_FOUNDRY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                "https_proxy", "http_proxy", "all_proxy", "NODE_EXTRA_CA_CERTS", "NODE_OPTIONS"):
        assert env[key] == ""
    assert command.startswith("unset " + " ".join(accounts.CREDIT_AUTH_OVERRIDES) + "; ")
    assert "fake-inherited-override" not in command
    assert argv[argv.index("--setting-sources") + 1] == "user"
    for key in accounts.CREDIT_SETTINGS_PATH_OVERRIDES:
        assert key not in accounts.env_for(accounts.CREDIT) and env[key] == ""


@pytest.mark.parametrize("key", accounts.CREDIT_SETTINGS_PATH_OVERRIDES)
@pytest.mark.parametrize("setting", ["env", *accounts.CREDIT_AUTH_SETTINGS])
def test_credit_refuses_auth_in_inherited_settings_paths(setup, monkeypatch, key, setting, caplog):
    interval(setup)
    path = setup[3].parent / "fake-private-settings.json"
    value = {"ANTHROPIC_BASE_URL": "https://collector.example"} if setting == "env" else "fake-private-command"
    path.write_text(json.dumps({setting: value}))
    monkeypatch.setenv(key, str(path))
    with pytest.raises(accounts.CreditActivationUnavailable, match="^credit activation unavailable$"):
        accounts.shell_command(SID)
    assert "override settings override credit authentication" in caplog.text
    assert all(private not in caplog.text for private in (str(path), "collector.example", "fake-private-command"))


@pytest.mark.parametrize("key", accounts.CREDIT_SETTINGS_PATH_OVERRIDES)
@pytest.mark.parametrize("failure", ["missing", "permission", "directory", "symlink", "json", "utf8", "shape"])
def test_credit_refuses_unreadable_inherited_settings_paths(setup, monkeypatch, key, failure, caplog):
    interval(setup)
    path = setup[3].parent / "fake-private-settings.json"
    if failure == "directory":
        path.mkdir()
    elif failure == "symlink":
        path.symlink_to(path.parent / "absent.json")
    elif failure != "missing":
        path.write_bytes({"json": b"{fake-private-content", "utf8": b"\xff", "shape": b"[]"}.get(failure, b"{}"))
    if failure == "permission":
        read = Path.read_text
        def unreadable(self, *args, **kw):
            if self == path:
                raise PermissionError("fake-private-error")
            return read(self, *args, **kw)
        monkeypatch.setattr(Path, "read_text", unreadable)
    monkeypatch.setenv(key, str(path))
    with pytest.raises(accounts.CreditActivationUnavailable, match="^credit activation unavailable$"):
        accounts.shell_command(SID)
    assert ("settings are malformed" if failure == "shape" else "settings cannot be read") in caplog.text
    assert all(private not in caplog.text for private in (str(path), "fake-private-content", "fake-private-error"))


@pytest.mark.parametrize("key", accounts.CREDIT_SETTINGS_PATH_OVERRIDES)
def test_clean_inherited_settings_paths_are_checked_then_removed(setup, monkeypatch, key):
    interval(setup)
    path = setup[3].parent / "fake-settings.json"
    path.write_text('{"model":"claude-sonnet-4"}')
    monkeypatch.setenv(key, str(path))
    argv = shlex.split(accounts.shell_command(SID))
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert str(path) not in argv and key not in accounts.env_for(accounts.CREDIT)


def test_credit_checks_inherited_config_dir_before_clearing_it(setup, monkeypatch):
    interval(setup)
    folder = setup[3].parent / "fake-config"
    folder.mkdir()
    (folder / "settings.json").write_text('{"apiKeyHelper":"fake-command"}')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(folder))
    with pytest.raises(accounts.CreditActivationUnavailable):
        accounts.shell_command(SID)


@pytest.mark.parametrize("source", ["managed", "managed-linux", "managed-drop-in", "account", "user", "project", "local"])
@pytest.mark.parametrize("setting", ["env", *accounts.CREDIT_AUTH_SETTINGS])
def test_credit_refuses_auth_settings_from_every_source(setup, source, setting, caplog):
    interval(setup)
    path = credit_settings_files()[source]
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"ANTHROPIC_BASE_URL": "https://collector.example"} if setting == "env" else "fake-private-command"
    path.write_text(json.dumps({setting: value}))
    with pytest.raises(ValueError, match="^credit activation unavailable$"):
        accounts.shell_command(SID)
    assert "settings override credit authentication" in caplog.text
    assert "collector.example" not in caplog.text and "fake-private-command" not in caplog.text


@pytest.mark.parametrize("key", accounts.CREDIT_AUTH_OVERRIDES)
def test_credit_refuses_every_pinned_env_key_even_when_empty(setup, key):
    interval(setup)
    path = credit_settings_files()["local"]
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"env": {key: ""}}))
    with pytest.raises(ValueError, match="credit activation unavailable"):
        accounts.shell_command(SID)


@pytest.mark.parametrize("source", ["managed", "managed-linux", "managed-drop-in", "account", "user", "project", "local"])
@pytest.mark.parametrize("body", [b"{fake-private-content", b"[]", b'{"env":null}', b'{"env":[]}', b"\xff"])
def test_credit_refuses_malformed_settings_without_parser_details(setup, source, body, caplog):
    interval(setup)
    path = credit_settings_files()[source]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    with pytest.raises(ValueError, match="^credit activation unavailable$"):
        accounts.shell_command(SID)
    assert "settings" in caplog.text and "fake-private-content" not in caplog.text


@pytest.mark.parametrize("source", ["managed", "managed-linux", "managed-drop-in", "account", "user", "project", "local"])
def test_credit_refuses_unreadable_settings(setup, monkeypatch, source, caplog):
    interval(setup)
    path = credit_settings_files()[source]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    read = Path.read_text
    def unreadable(self, *args, **kw):
        if self == path:
            raise PermissionError("fake-private-error")
        return read(self, *args, **kw)
    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(ValueError, match="credit activation unavailable"):
        accounts.shell_command(SID)
    assert "settings cannot be read" in caplog.text and "fake-private-error" not in caplog.text


def test_credit_clean_settings_still_launch(setup):
    interval(setup)
    for path in credit_settings_files().values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": "claude-sonnet-4", "env": {"MAX_THINKING_TOKENS": "1000"}}))
    assert "--settings" in shlex.split(accounts.shell_command(SID))


def test_credit_unknown_session_folder_refuses(setup, monkeypatch):
    interval(setup)
    monkeypatch.setattr(status, "session_cwd", lambda s: None)
    with pytest.raises(ValueError, match="credit activation unavailable"):
        accounts.shell_command(SID)


@pytest.mark.parametrize("worktree", [False, True])
def test_credit_refuses_local_settings_at_repo_or_main_worktree_root(setup, monkeypatch, tmp_path, worktree):
    interval(setup)
    root, main = tmp_path / "repo", tmp_path / "main"
    cwd = root / "nested"
    cwd.mkdir(parents=True)
    (root / ".git").mkdir()
    common = (main if worktree else root) / ".git"
    path = common.parent / ".claude/settings.local.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"apiKeyHelper": "fake-private-command"}))
    monkeypatch.setattr(status, "session_cwd", lambda s: str(cwd))
    def git(argv, **kw):
        assert argv == ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute",
                        "--git-common-dir", "--show-toplevel"]
        assert not any(key.startswith("GIT_") for key in kw["env"])
        return SimpleNamespace(returncode=0, stdout=str(common) + "\n" + str(root) + "\n")
    monkeypatch.setattr(accounts.subprocess, "run", git)
    with pytest.raises(ValueError, match="credit activation unavailable"):
        accounts.shell_command(SID)


def test_credit_refuses_when_repo_settings_cannot_be_located(setup, monkeypatch, tmp_path):
    interval(setup)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(accounts.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=128, stdout=""))
    with pytest.raises(ValueError, match="credit activation unavailable"):
        accounts.shell_command(SID)


def test_credit_settings_refusal_does_not_crash_or_stop_the_bridge_session(setup, monkeypatch):
    interval(setup)
    accounts.record(SID, accounts.MAIN)
    path = credit_settings_files()["project"]
    path.parent.mkdir(parents=True)
    path.write_text("{malformed")
    monkeypatch.setattr(I, "_account_move", lambda *a: pytest.fail("refuse before stopping the subscription"))
    assert not I.account_move(setup[0], setup[1], SID, accounts.CREDIT, lambda *a: None)
    assert accounts.account_for(SID) == accounts.MAIN
    accounts.record(SID, accounts.CREDIT)
    monkeypatch.setattr(I, "_pane_command", lambda *a: None)
    monkeypatch.setattr(I, "_tmux", lambda *a: pytest.fail("no credit process may start"))
    assert not I.resume_session(SID)


def test_no_now_and_new_exhaustion_need_new_tap(setup, monkeypatch):
    cfg, bot, found, _ = setup
    rec = offer(setup)
    D.answer(cfg, I.default_inbox(), rec["id"], "no", now=NOW)
    monkeypatch.setattr(I, "account_move", lambda *a, **kw: pytest.fail("decline must not move"))
    C.tick(cfg, bot, NOW)
    assert not C.active(SID)
    assert not C.offer(cfg, bot, SID, TEXT, found)
    monkeypatch.setattr(usage, "limit_of", lambda s: {"id": "limit-two", "reset": NOW + 7200})
    assert C.offer(cfg, bot, SID, TEXT, found)
    assert len(D.open_decisions()) == 1
    C.tick(cfg, bot, NOW)
    assert not C.active(SID)


@pytest.mark.parametrize("change", ["off", "cap", "reset"])
def test_stale_yes_rechecked_at_tap(setup, monkeypatch, change):
    cfg = setup[0]
    rec = offer(setup)
    if change == "off":
        cfg.emergency_credit = False
    if change == "cap":
        monkeypatch.setattr(C, "spend", lambda *a, **kw: 10)
    with pytest.raises(D.DecisionError):
        D.answer(cfg, I.default_inbox(), rec["id"], "yes", now=NOW + 3600 if change == "reset" else NOW)
    assert not I.default_inbox().pending(SID)


def test_cap_total_main_and_subagents_all_sessions_and_stop_once(setup, monkeypatch):
    cfg, bot, _, path = setup
    interval(setup)
    usage_row(path, 2_000_000)  # $6
    child = path.parent / SID / "subagents" / "agent.jsonl"
    child.parent.mkdir(parents=True)
    usage_row(child, 1_000_000, ident="child")  # $3
    other = path.parent / "other.jsonl"
    other.write_text("")
    interval(setup, session="other")
    with locked_map(C._path()) as data:
        data["intervals"]["other"]["transcript"] = str(other)
    usage_row(other, 400_000, ident="other")  # $1.20
    assert C.spend(NOW) == pytest.approx(10.2)
    assert C.blocked(SID, NOW) and C.blocked("other", NOW)
    sent = []
    monkeypatch.setattr(M, "send_essential", lambda c, b, s, params: sent.append((s, params["text"])) or True)
    moves = []
    monkeypatch.setattr(I, "account_move", lambda c, b, s, target, say, **kw:
                        moves.append((s, target, kw)) or False)
    C.tick(cfg, bot, NOW)
    C.tick(cfg, bot, NOW)
    assert len(sent) == 2 and all("בחצות" in text for _, text in sent)
    assert not C.active(SID) and C._open(SID)["stop_day"] == C._day(NOW)
    assert {target for _, target, _ in moves} == {accounts.MAIN}
    assert all(not kw["idle_only"] for _, _, kw in moves)
    assert "$10.20" in C.status_line(SID, NOW)


def test_prompt_gate_blocks_before_config_read_and_preserves_queue(setup, monkeypatch, capsys):
    interval(setup)
    usage_row(setup[3], 4_000_000)
    box = I.default_inbox()
    box.put(SID, {"text": "הודעה ממתינה", "at": int(NOW), "id": 1})
    monkeypatch.setattr(hooks, "load_config", lambda: pytest.fail("blocked prompt must return before loading config"))
    assert hooks.hook_prompt(json.dumps({"session_id": SID, "prompt": "continue"})) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block" and "בחצות" in out["reason"]
    assert hooks.telegram_messages(SID, via="stop") == []
    assert box.pending(SID)


def test_stop_allows_current_turn_to_end_without_feeding_next(setup, monkeypatch):
    interval(setup)
    usage_row(setup[3], 4_000_000)
    monkeypatch.setattr(hooks, "mirror_files", lambda *a: None)
    monkeypatch.setattr(hooks, "drop_progress", lambda *a: None)
    out = hooks.stop_output({"session_id": SID}, [], Config(), answer_to=None)
    assert "decision" not in out and "בחצות" in out["systemMessage"]


def test_day_rollover_and_closed_credit_segments_exclude_subscription(setup):
    interval(setup, start=NOW - 86400)
    usage_row(setup[3], 4_000_000, stamp=NOW - 86400)
    assert C.spend(NOW - 86400) == 12
    assert C.spend(NOW) == 0
    usage_row(setup[3], 1_000_000)
    C.leaving(SID, NOW + 1)
    usage_row(setup[3], 9_000_000, stamp=NOW + 2, ident="subscription")
    assert C.spend(NOW + 3) == 3
    assert not C.active(SID)


@pytest.mark.parametrize("early", [False, True])
def test_subscription_returns_only_at_idle_and_posts_once(setup, monkeypatch, early):
    cfg, bot, _, path = setup
    interval(setup, reset=NOW + 100 if early else NOW)
    if early:
        monkeypatch.setattr(accounts, "_read", lambda *a: {"at": datetime.fromtimestamp(NOW, timezone.utc).isoformat()})
        monkeypatch.setattr(accounts, "spent", lambda *a: False)
    sent, moves = [], []
    monkeypatch.setattr(M, "send_essential", lambda c, b, sid, p: sent.append(p["text"]) or True)
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a, **kw: False)
    C.tick(cfg, bot, NOW)
    assert C._open(SID) and not sent  # recovery interval survives expired authorization
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a, **kw: True)
    def move(c, b, sid, target, say, **kw):
        moves.append((sid, target, kw))
        assert C.resume_flags(sid) == " --model claude-sonnet-4 --permission-mode plan"
        accounts.record(sid, target)
        return True
    monkeypatch.setattr(I, "account_move", move)
    C.tick(cfg, bot, NOW)
    C.tick(cfg, bot, NOW)
    assert not C.active(SID) and len(moves) == 1
    assert moves[0][0:2] == (SID, "main") and moves[0][2]["idle_only"]
    assert sent == [C.BACK]
    assert "--settings" not in accounts.shell_command(SID)


def test_provider_meter_is_conservative_and_unknown_stops(setup, monkeypatch):
    monkeypatch.setattr(C, "_provider", lambda: {"balance": 50})
    assert C.spend(NOW, provider=True) == 0
    monkeypatch.setattr(C, "_provider", lambda: {"balance": 46.6, "spent": 2})
    assert C.spend(NOW, provider=True) == pytest.approx(3.4)
    monkeypatch.setattr(C, "_provider", lambda: {"balance": 100})
    assert C.spend(NOW, provider=True) == pytest.approx(3.4)
    monkeypatch.setattr(C, "_provider", lambda: {"balance": 95})
    assert C.spend(NOW, provider=True) == pytest.approx(8.4)
    monkeypatch.setattr(C, "_provider", lambda: None)
    assert C.spend(NOW, provider=True) is None
    assert C.spend(NOW) is None


def test_provider_output_parser_never_runs_real_helper(setup, monkeypatch):
    helper = Path.home() / ".codex/bin/anthropic-credit"
    helper.parent.mkdir(parents=True)
    helper.write_text("placeholder")
    # Load the real implementation saved at module import below.
    monkeypatch.setattr(C, "_provider", REAL_PROVIDER)
    seen = []
    def run(argv, **kw):
        seen.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout='{"spent_today_usd": 3.4, "balance_usd": 20}')
    monkeypatch.setattr(C.subprocess, "run", run)
    assert C._provider() == {"spent": 3.4, "balance": 20}
    assert seen[0][0] == [str(helper)]
    assert seen[0][1]["timeout"] == 10
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="$20"))
    assert C._provider() is None


REAL_PROVIDER = C._provider
REAL_HAS_LIVE_PROCESS = C._has_live_process


def test_margin_line_blocks_both_key_and_paid_launch(setup, monkeypatch, capfdbinary):
    interval(setup)
    with locked_map(C._path()) as data:
        data.setdefault("days", {})[C._day(NOW)] = {
            "spent": C.credit_usage.DAILY_CAP_USD - C.credit_usage.MARGIN_USD}
    monkeypatch.setenv("CTB_CREDIT_SESSION", SID)
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **kw: pytest.fail("must not fetch key"))
    assert C.blocked(SID, NOW)
    with pytest.raises(ValueError, match="credit activation unavailable"):
        accounts.shell_command(SID)
    assert C.key_main() != 0
    assert capfdbinary.readouterr() == (b"", b"")


@pytest.mark.parametrize("cost", [8.50, None])
def test_tick_interrupts_all_credit_sessions_before_parking_any(setup, monkeypatch, cost):
    cfg, bot, _, _ = setup
    interval(setup)
    interval(setup, session="other-session")
    box = I.default_inbox()
    box.put(SID, {"text": "continue", "id": 1, "at": int(NOW)})
    monkeypatch.setattr(C, "spend", lambda *a, **kw: cost)
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a, **kw: False)
    panes = {SID: "%7", "other-session": "%8"}
    monkeypatch.setattr(I, "pane_for_session", panes.get)
    monkeypatch.setattr(I, "pane_runs_session", lambda pane, sid: panes[sid] == pane)
    events = []
    monkeypatch.setattr(I, "_tmux", lambda *args: events.append(("interrupt", args)))
    monkeypatch.setattr(M, "send_essential", lambda *a: True)
    monkeypatch.setattr(I, "resume_after_move", lambda *a, **kw: pytest.fail("must not resume credit"))
    def move(c, b, sid, target, say, **kw):
        assert not kw["idle_only"]  # an active paid turn must be stopped, not left until idle
        assert not C.active(SID) and not C.active("other-session")
        assert target == accounts.MAIN
        events.append(("move", sid))
        accounts.record(sid, target)
        return True
    monkeypatch.setattr(I, "account_move", move)
    C.tick(cfg, bot, NOW)
    assert events[:2] == [("interrupt", ("send-keys", "-t", pane, "Escape"))
                          for pane in panes.values()]
    assert {sid for kind, sid in events[2:] if kind == "move"} == set(panes)
    assert all(C._open(sid) is None and accounts.account_for(sid) == accounts.MAIN for sid in panes)
    assert box.pending(SID)


@pytest.fixture
def cached_clients(setup, monkeypatch):
    """Old clients can keep using a cached credential until their process ends."""
    clients = {
        654321: {"session": SID, "start": "first-start", "alive": True, "record": True},
        654322: {"session": "other-session", "start": "second-start", "alive": True, "record": True},
        654323: {"session": "subscription", "start": "subscription-start", "alive": True, "record": True}}
    events = []
    monkeypatch.setattr(I, "session_pid", lambda sid: next((pid for pid, row in clients.items()
                        if row["session"] == sid and row["alive"] and row["record"]), None))
    monkeypatch.setattr(I, "_proc_start", lambda pid: clients[pid]["start"] if clients[pid]["alive"] else "")
    monkeypatch.setattr(I, "_live_records_strict", lambda pid: [dict(pid=p, sessionId=r["session"])
                        for p, r in clients.items() if r["alive"] and r["record"]])
    monkeypatch.setattr(I, "_ps_field", lambda field, pid: "fake claude")
    monkeypatch.setattr(I, "_looks_like_claude", lambda pid, cmd: clients[pid]["alive"])
    monkeypatch.setattr(C, "_has_live_process", lambda sid: any(row["alive"] and row["session"] == sid
                        for row in clients.values()))
    def kill(pid, sig):
        assert pid in clients  # never call a real signal function
        events.append(("signal", pid, sig))
        if sig == signal.SIGKILL or sig == signal.SIGTERM and clients[pid].get("exit_on_term"):
            clients[pid]["alive"] = False
    monkeypatch.setattr(C.os, "kill", kill)
    monkeypatch.setattr(C.time, "sleep", lambda seconds: events.append(("wait", seconds)))
    return clients, events


@pytest.mark.parametrize("reason", ["cap", "unknown", "revoked"])
@pytest.mark.parametrize("exit_on_term", [False, True])
@pytest.mark.parametrize("pane", ["valid", "missing", "reused"])
def test_blocked_cached_clients_end_without_waiting_for_account_move(
        setup, monkeypatch, cached_clients, capfdbinary, reason, exit_on_term, pane):
    cfg, bot, _, _ = setup
    clients, events = cached_clients
    for sid in (SID, "other-session"):
        interval(setup, session=sid)
        I.default_inbox().put(sid, {"text": "queued", "id": 1, "at": int(NOW)})
    accounts.record("subscription", accounts.MAIN)
    if reason == "revoked":
        with locked_map(C._path()) as data:
            for row in data["intervals"].values():
                row["revoked"] = True
    monkeypatch.setattr(C, "spend", lambda *a, **kw: {"cap": 8.5, "unknown": None, "revoked": 0}[reason])
    monkeypatch.setenv("CTB_CREDIT_SESSION", SID)
    assert C.key_main() != 0  # renewal fails, but these fake clients still hold a cached key
    assert capfdbinary.readouterr() == (b"", b"")
    monkeypatch.delenv("CTB_CREDIT_SESSION")
    for row in clients.values():
        row["exit_on_term"] = exit_on_term
    panes = {SID: "%7", "other-session": "%8"}
    monkeypatch.setattr(I, "pane_for_session", lambda sid: None if pane == "missing" else panes[sid])
    monkeypatch.setattr(I, "pane_runs_session", lambda *a: pane == "valid")
    monkeypatch.setattr(I, "_tmux", lambda *a: events.append(("escape", a)))
    monkeypatch.setattr(I, "account_move", lambda *a, **kw: pytest.fail("cached-key stop must not depend on a move"))
    monkeypatch.setattr(M, "send_essential", lambda *a: True)
    C.tick(cfg, bot, NOW)
    signals = [event for event in events if event[0] == "signal"]
    expected = [("signal", pid, signal.SIGTERM) for pid in (654321, 654322)]
    if not exit_on_term:
        expected += [("signal", pid, signal.SIGKILL) for pid in (654321, 654322)]
    assert signals == expected
    first_wait = next(i for i, event in enumerate(events) if event[0] == "wait")
    assert all(event in events[:first_wait] for event in expected[:2])
    assert sum(event[1] for event in events if event[0] == "wait") <= 3
    if pane == "valid":
        assert events[:2] == [("escape", ("send-keys", "-t", p, "Escape")) for p in panes.values()]
    else:
        assert not any(event[0] == "escape" for event in events)
    assert not clients[654321]["alive"] and not clients[654322]["alive"]
    assert clients[654323]["alive"]
    for sid in panes:
        assert I.default_inbox().pending(sid)
        assert accounts.account_for(sid) == accounts.MAIN and C._open(sid) is None
    C.tick(cfg, bot, NOW + 120)  # no cached paid client survives past its key TTL
    assert [event for event in events if event[0] == "signal"] == expected


@pytest.mark.parametrize("change", ["pid", "executable", "records", "other-session", "account", "start", "init-pid"])
def test_credit_process_stop_refuses_unverified_targets(setup, monkeypatch, cached_clients, change):
    interval(setup)
    clients, events = cached_clients
    if change == "pid":
        monkeypatch.setattr(I, "session_pid", lambda sid: None)
    elif change == "init-pid":
        monkeypatch.setattr(I, "session_pid", lambda sid: 1)
    elif change == "executable":
        monkeypatch.setattr(I, "_looks_like_claude", lambda *a: False)
    elif change == "records":
        monkeypatch.setattr(I, "_live_records_strict", lambda *a: None)
    elif change == "other-session":
        monkeypatch.setattr(I, "_live_records_strict", lambda *a: [{"pid": 654321, "sessionId": "unrelated"}])
    elif change == "account":
        accounts.record(SID, accounts.MAIN)
    elif change == "start":
        monkeypatch.setattr(I, "_proc_start", lambda *a: "")
    C._stop_credit_processes([SID], I)
    assert events == [] and all(row["alive"] for row in clients.values())


@pytest.mark.parametrize("change", ["pid-reused", "other-session", "account", "executable", "records", "record-removed"])
def test_credit_process_stop_rechecks_identity_before_escalation(setup, monkeypatch, cached_clients, change):
    interval(setup)
    clients, events = cached_clients
    def sleep(seconds):
        events.append(("wait", seconds))
        if change == "pid-reused":
            clients[654321]["start"] = "replacement-start"
        elif change == "other-session":
            clients[654321]["session"] = "unrelated"
        elif change == "account":
            accounts.record(SID, accounts.MAIN)
        elif change == "executable":
            monkeypatch.setattr(I, "_looks_like_claude", lambda *a: False)
        elif change == "records":
            monkeypatch.setattr(I, "_live_records_strict", lambda *a: None)
        elif change == "record-removed":
            clients[654321]["record"] = False  # exit hooks removed it, but the paid process is hung
    monkeypatch.setattr(C.time, "sleep", sleep)
    C._stop_credit_processes([SID], I)
    signals = [event for event in events if event[0] == "signal"]
    assert signals[0] == ("signal", 654321, signal.SIGTERM)
    assert signals[1:] == ([("signal", 654321, signal.SIGKILL)] if change == "record-removed" else [])
    assert clients[654322]["alive"] and clients[654323]["alive"]


def test_deferred_unbilled_credit_is_never_a_process_stop_target(setup, monkeypatch, cached_clients):
    cfg, bot, _, _ = setup
    interval(setup, ready=False)
    with locked_map(C._path()) as data:
        data["intervals"][SID]["billing_start"] = None
    accounts.record(SID, accounts.MAIN)
    monkeypatch.setattr(C, "spend", lambda *a, **kw: 10)
    monkeypatch.setattr(I, "pane_for_session", lambda *a: pytest.fail("unbilled subscription must not be interrupted"))
    monkeypatch.setattr(M, "send_essential", lambda *a: True)
    C.tick(cfg, bot, NOW)
    assert not cached_clients[1] and all(row["alive"] for row in cached_clients[0].values())


@pytest.mark.parametrize("failure", ["opening", "iteration"])
@pytest.mark.parametrize("error", [PermissionError, FileNotFoundError, OSError])
def test_subagent_scan_failure_blocks_accounting_and_launch(setup, monkeypatch, failure, error):
    interval(setup)
    folder = setup[3].parent / SID / "subagents"
    folder.mkdir(parents=True)
    real_scan = C.os.scandir
    class BrokenScan:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def __iter__(self):
            raise error("scan refused")
    def scan(path):
        if path != folder:
            return real_scan(path)
        if failure == "opening":
            raise error("scan refused")
        return BrokenScan()
    monkeypatch.setattr(C.os, "scandir", scan)
    assert C.spend(NOW) is None
    assert C.blocked(SID, NOW)
    with pytest.raises(ValueError):
        accounts.shell_command(SID)


@pytest.mark.parametrize("field", ["spent", "balance_last", "balance_spent"])
@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -1, "5", True, None, {}])
def test_invalid_stored_amounts_never_become_zero_spend(setup, field, amount):
    interval(setup)
    with locked_map(C._path()) as data:
        data.setdefault("days", {})[C._day(NOW)] = {field: amount}
    assert C.spend(NOW, provider=True) is None
    assert C.spend(NOW) is None
    assert C.blocked(SID, NOW)


@pytest.mark.parametrize("change", ["cancelled", "reset", "budget", "provider-budget", "billing-window"])
def test_credit_key_rechecks_all_gates_after_fetch(setup, monkeypatch, capfdbinary, change):
    from ctb import cli
    interval(setup)
    monkeypatch.setenv("CTB_CREDIT_SESSION", SID)
    monkeypatch.setattr(cli.C, "load", lambda *a: pytest.fail("must not load bridge config"))
    fetched = []
    def helper(argv, **kw):
        assert argv == [str(Path.home() / ".codex/bin/anthropic-key-helper")]
        assert kw["stdout"] == C.subprocess.PIPE and kw["timeout"] == 10
        assert kw["stderr"] == C.subprocess.DEVNULL
        fetched.append(True)
        if change == "cancelled":
            C.session_changed(SID, "other-session", str(setup[3]), NOW)
        elif change == "reset":
            monkeypatch.setattr(C.time, "time", lambda: NOW + 3600)
        elif change == "budget":
            with locked_map(C._path()) as data:
                data.setdefault("days", {})[C._day(NOW)] = {"spent": 8.50}
        elif change == "provider-budget":
            monkeypatch.setattr(C, "_provider", lambda: {"spent": 8.50})
        else:
            with locked_map(C._path()) as data:
                data["intervals"][SID]["billing_start"] = None
        return SimpleNamespace(returncode=0, stdout=b"fake-credit-key\n")
    monkeypatch.setattr(C.subprocess, "run", helper)
    assert cli.main(["credit-key"]) != 0
    assert fetched == [True]
    assert capfdbinary.readouterr() == (b"", b"")


def test_credit_key_fetch_timeout_discards_partial_output(setup, monkeypatch, capfdbinary):
    interval(setup)
    monkeypatch.setenv("CTB_CREDIT_SESSION", SID)
    def helper(argv, **kw):
        assert kw["stdout"] == C.subprocess.PIPE and kw["timeout"] == 10
        raise C.subprocess.TimeoutExpired(argv, 10, output=b"fake-credit-key\n")
    monkeypatch.setattr(C.subprocess, "run", helper)
    assert C.key_main() != 0
    assert capfdbinary.readouterr() == (b"", b"")


@pytest.mark.parametrize("reason", ["inactive", "capped", "provider", "unknown", "expired", "revoked",
                                    "wrong-session", "no-session", "deferred"])
def test_credit_key_refuses_without_touching_key_helper(setup, monkeypatch, capfdbinary, reason):
    from ctb import cli
    interval(setup)
    monkeypatch.setenv("CTB_CREDIT_SESSION", SID)
    if reason == "inactive":
        C.leaving(SID, NOW)
    elif reason == "capped":
        usage_row(setup[3], 4_000_000)
    elif reason == "provider":
        monkeypatch.setattr(C, "_provider", lambda: {"spent": 10.01})
    elif reason == "unknown":
        monkeypatch.setattr(C, "_provider", lambda: None)
    elif reason in ("expired", "revoked"):
        with locked_map(C._path()) as data:
            data["intervals"][SID]["reset" if reason == "expired" else "revoked"] = NOW if reason == "expired" else True
    elif reason == "wrong-session":
        monkeypatch.setenv("CTB_CREDIT_SESSION", "unapproved-session")
    elif reason == "no-session":
        monkeypatch.delenv("CTB_CREDIT_SESSION")
    elif reason == "deferred":
        with locked_map(C._path()) as data:
            data["intervals"][SID]["billing_start"] = None
    monkeypatch.setattr(cli.C, "load", lambda *a: pytest.fail("must not load bridge config"))
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **kw: pytest.fail("must not fetch key"))
    assert cli.main(["credit-key"]) != 0
    assert capfdbinary.readouterr() == (b"", b"")


def test_credit_key_releases_fake_helper_bytes_untouched_after_checks(setup, monkeypatch, capfdbinary):
    from ctb import cli
    interval(setup)
    monkeypatch.setenv("CTB_CREDIT_SESSION", SID)
    monkeypatch.setenv("CLAUDE_CODE_API_KEY_HELPER_TTL_MS", "999999")
    assert accounts.env_for("credit")["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"] == "60000"
    helper = Path.home() / ".codex/bin/anthropic-key-helper"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/sh\nprintf 'fake-credit-key\\r\\n'\n")
    helper.chmod(0o700)
    # A lower provider reading cannot erase higher transcript spend.
    usage_row(setup[3], 1_000_000)
    monkeypatch.setattr(C, "_provider", lambda: {"spent": 1})
    monkeypatch.setattr(cli.C, "load", lambda *a: pytest.fail("must not load bridge config"))
    assert cli.main(["credit-key"]) == 0
    assert capfdbinary.readouterr() == (b"fake-credit-key\r\n", b"")
    assert C.spend(NOW) == 3
    # Each invocation checks again instead of reusing the previous approval.
    usage_row(setup[3], 4_000_000, ident="later")
    assert cli.main(["credit-key"]) != 0
    assert capfdbinary.readouterr() == (b"", b"")


def test_credit_key_real_meter_parser_blocks_higher_spend(setup, monkeypatch, capfdbinary):
    interval(setup)
    monkeypatch.setenv("CTB_CREDIT_SESSION", SID)
    helper = Path.home() / ".codex/bin/anthropic-credit"
    helper.parent.mkdir(parents=True)
    helper.write_text('#!/bin/sh\nprintf \'{"spent_today_usd":10}\'\n')
    helper.chmod(0o700)
    monkeypatch.setattr(C, "_provider", REAL_PROVIDER)
    assert C.key_main() != 0
    assert capfdbinary.readouterr() == (b"", b"")
    assert C.spend(NOW) == 10


def test_credit_key_meter_timeout_refuses_silently(setup, monkeypatch, capfdbinary):
    interval(setup)
    monkeypatch.setenv("CTB_CREDIT_SESSION", SID)
    helper = Path.home() / ".codex/bin/anthropic-credit"
    helper.parent.mkdir(parents=True)
    helper.touch()
    monkeypatch.setattr(C, "_provider", REAL_PROVIDER)
    def timeout(argv, **kwargs):
        assert argv == [str(helper)]
        assert kwargs["timeout"] == 10
        raise C.subprocess.TimeoutExpired(argv, 10, output="fake-private-output")
    monkeypatch.setattr(C.subprocess, "run", timeout)
    assert C.key_main() != 0
    assert capfdbinary.readouterr() == (b"", b"")


def test_credit_key_rechecks_approval_after_meter(setup, monkeypatch, capfdbinary):
    interval(setup)
    monkeypatch.setenv("CTB_CREDIT_SESSION", SID)
    def meter():
        C.leaving(SID, NOW)
        return {"spent": 0}
    monkeypatch.setattr(C, "_provider", meter)
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **kw: pytest.fail("approval ended during meter query"))
    assert C.key_main() != 0
    assert capfdbinary.readouterr() == (b"", b"")


@pytest.mark.parametrize("missing", [False, True])
def test_credit_key_child_failure_stays_silent(setup, monkeypatch, capfdbinary, missing):
    interval(setup)
    monkeypatch.setenv("CTB_CREDIT_SESSION", SID)
    if not missing:
        helper = Path.home() / ".codex/bin/anthropic-key-helper"
        helper.parent.mkdir(parents=True)
        helper.write_text("#!/bin/sh\nprintf 'fake-credit-key'\nprintf 'fake-private-error' >&2\nexit 3\n")
        helper.chmod(0o700)
    assert C.key_main() != 0
    assert capfdbinary.readouterr() == (b"", b"")


def test_pseudo_account_is_never_default_or_real_profile(setup, monkeypatch):
    assert not accounts.valid_name("credit")
    assert "credit" not in accounts.names()
    with pytest.raises(ValueError):
        accounts.use("credit")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-placeholder")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-placeholder")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "test-profile")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-placeholder")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    assert not (set(accounts.CREDIT_AUTH_OVERRIDES) - {"CLAUDE_CODE_API_KEY_HELPER_TTL_MS"}) & accounts.env_for("credit").keys()
    assert accounts.roomier_account("credit") is None
    accounts.record(SID, "credit")
    with pytest.raises(ValueError):
        accounts.shell_command(SID)


def test_ui_switch_default_on_and_hebrew_copy(setup, monkeypatch):
    assert Config().emergency_credit is True
    cfg = setup[0]
    monkeypatch.setattr(panel_settings, "_config_get", lambda name: cfg.emergency_credit)
    monkeypatch.setattr(panel_settings, "_config_set", lambda name, value: setattr(cfg, name, value))
    assert panel_settings.SETTINGS["emergency_credit"][0]() is True
    panel_settings.write("emergency_credit", False)
    assert cfg.emergency_credit is False
    for value in (C.YES, C.NO, C.BACK, C.UNKNOWN, C.cap_line(NOW)):
        assert not re.search(r"[A-Za-z]", value)
    assert sensitive_buttons.callback_sensitive("cr:" + "a" * 16 + ":yes")
    assert not sensitive_buttons.callback_sensitive("cr:" + "a" * 16 + ":no")


def test_status_keyboard_shows_shared_credit_spend(setup, monkeypatch):
    interval(setup)
    usage_row(setup[3], 1_000_000)
    monkeypatch.setattr(M, "context_numbers", lambda *a: (0, 0))
    monkeypatch.setattr(M, "state_labels", lambda *a: {})
    monkeypatch.setattr(usage, "quota_line", lambda *a, **kw: "")
    keyboard = M.live_keyboard("", lang="he", session=SID)
    assert any(b["text"] == "בקרדיט · $3.00 מתוך $10 היום" for row in keyboard["keyboard"] for b in row)


def test_subscription_card_key_never_reads_credit_ledger(setup, monkeypatch):
    monkeypatch.setattr(C, "_open", lambda *a: pytest.fail("subscription card must not read credit ledger"))
    monkeypatch.setattr(M, "session_context", lambda *a: 100_000)
    before = M.card_data_key(SID)
    assert len(before.split("|")) == 4 and before.endswith("~k" + M.layout_id())
    monkeypatch.setattr(M, "session_context", lambda *a: 150_000)
    after = M.card_data_key(SID)
    assert M._only_context_moved(before, after)


def test_credit_card_key_tracks_spend_and_preserves_context_pace(setup, monkeypatch):
    interval(setup)
    monkeypatch.setattr(M, "session_context", lambda *a: 100_000)
    before = M.card_data_key(SID)
    usage_row(setup[3], 1_000_000)
    billed = M.card_data_key(SID)
    assert before != billed and not M._only_context_moved(before, billed)
    assert "$3.00" in billed and billed.endswith("~k" + M.layout_id())
    monkeypatch.setattr(M, "session_context", lambda *a: 150_000)
    assert M._only_context_moved(billed, M.card_data_key(SID))
    monkeypatch.setattr(C, "spend", lambda *a, **kw: None)
    assert C.UNKNOWN in M.card_data_key(SID)


def test_credit_card_without_status_line_keeps_original_key_shape(setup, monkeypatch):
    interval(setup)
    monkeypatch.setattr(C, "status_line", lambda *a: "")
    key = M.card_data_key(SID)
    assert len(key.split("|")) == 4 and key.endswith("~k" + M.layout_id())


@pytest.mark.parametrize("paid", [False, True])
def test_panel_state_includes_credit_line_without_a_server(setup, paid):
    from ctb import panel
    from ctb.config import write_private
    if paid:
        interval(setup)
        usage_row(setup[3], 1_000_000)
    write_private(state_dir() / "topics.json", json.dumps({SID: {"thread": 7, "name": "Sample"}}))
    state = panel.build_state(setup[0], now=NOW, light=True)
    assert len(state["sessions"]) == 1
    assert state["sessions"][0]["credit"] == ("בקרדיט · $3.00 מתוך $10 היום" if paid else "")


def test_recover_does_not_bypass_credit_lifecycle(setup, monkeypatch):
    interval(setup)
    monkeypatch.setattr(recover, "_finish_resume", lambda *a: pytest.fail("credit lifecycle owns recovery"))
    recover._one(setup[0], setup[1], I.default_inbox(), SID, 7, {}, state_dir() / "unused", NOW, None, "he")


@pytest.fixture
def credit_move_pane(setup, monkeypatch):
    """Exercise the real move path with only a fake terminal and isolated state."""
    calls, typed = [], []
    monkeypatch.setattr(I, "pane_for_session", lambda sid: "%7")
    monkeypatch.setattr(I, "session_pid", lambda sid: None)
    monkeypatch.setattr(I, "background_work_visible", lambda pane: False)
    monkeypatch.setattr(I, "_pane_command", lambda pane: "zsh")
    def type_command(sid, pane, text, before_enter=None):
        if before_enter is not None:  # the real type_command erases the line and returns "failed" on a raise
            try:
                before_enter()
            except Exception:
                return "failed"
        typed.append(text)
        return "sent"

    monkeypatch.setattr(I, "type_command", type_command)
    monkeypatch.setattr(I, "_hung_at_limit", lambda *a, **kw: False)
    monkeypatch.setattr(I.time, "sleep", lambda *a: None)
    monkeypatch.setattr(I, "pass_trust", lambda *a: True)
    monkeypatch.setattr(I, "_resume_dialog_pending", lambda *a: False)
    monkeypatch.setattr(I, "_idle_after_move", lambda *a: None)
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **kw: None)

    def tmux(*args):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=str(setup[3].parent))

    monkeypatch.setattr(I, "_tmux", tmux)
    return calls, typed


@pytest.mark.parametrize("change", ["expired", "revoked", "cap", "settings"])
def test_credit_move_rechecks_eligibility_immediately_before_exit(setup, monkeypatch, credit_move_pane, change):
    cfg, bot, _, _ = setup
    interval(setup, ready=False)
    with locked_map(C._path()) as data:
        data["intervals"][SID]["billing_start"] = None
    accounts.record(SID, accounts.MAIN)
    calls, typed = credit_move_pane
    waits = []

    def wait(seconds):
        if seconds != 0.5:
            return
        waits.append(seconds)
        # Eligibility changes during the pause before /exit.
        if change == "expired":
            monkeypatch.setattr(C.time, "time", lambda: NOW + 3600)
        elif change == "revoked":
            with locked_map(C._path()) as data:
                data["intervals"][SID]["revoked"] = True
        elif change == "cap":
            monkeypatch.setattr(C, "spend", lambda *a, **kw: 10)
        else:
            settings = credit_settings_files()["project"]
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps({"apiKeyHelper": "fake-conflicting-helper"}))

    monkeypatch.setattr(I.time, "sleep", wait)
    assert not I.account_move(cfg, bot, SID, accounts.CREDIT, lambda *a, **kw: None,
                              defer=True, limit={"id": "limit-one", "reset": NOW + 3600})
    assert waits == [0.5]
    assert typed == []
    assert calls == [("send-keys", "-t", "%7", "Escape")]
    assert accounts.origin_account(SID) == accounts.MAIN
    assert C._open(SID)["billing_start"] is None
    assert not C._open(SID)["ready"]
    assert not I.planned_restart_marker(SID).exists()
    assert not I.resume_pending(SID)


@pytest.mark.parametrize("move_entry", ["account_move", "_account_move"])
def test_credit_activation_cannot_use_stale_manual_deferred_target(setup, monkeypatch, credit_move_pane, move_entry):
    cfg, bot, _, _ = setup
    accounts.add("spare")
    accounts.defer_move(SID, "spare", origin=accounts.MAIN, manual=True)
    approve(setup)
    calls, typed = credit_move_pane
    monkeypatch.setattr(I.identity, "check", lambda *a, **kw: None)
    monkeypatch.setattr(accounts, "same_login", lambda *a: False)
    monkeypatch.setattr(accounts, "logged_out", lambda *a: False)
    monkeypatch.setattr(accounts, "email", lambda *a: "")
    monkeypatch.setattr(accounts, "ensure_usage", lambda **kw: None)
    monkeypatch.setattr(I, "_queue_replay", lambda *a: 1)
    monkeypatch.setattr(I, "resume_after_move", lambda *a, **kw: "nudged")
    monkeypatch.setattr(M, "send_essential", lambda *a: True)
    if move_entry == "_account_move":
        monkeypatch.setattr(I, "account_move", I._account_move)
    C.tick(cfg, bot, NOW)
    assert calls == [] and typed == []
    assert accounts.origin_account(SID) == accounts.MAIN
    assert accounts.deferred_move(SID)["target"] == "spare"
    assert C.active(SID)["billing_start"] is None
    assert not C.active(SID)["ready"]
    assert not I.resume_pending(SID)
    assert not I.planned_restart_marker(SID).exists()


@pytest.mark.parametrize("back", [False, True])
def test_real_account_move_emits_safe_resume_in_same_pane(setup, monkeypatch, back):
    cfg, bot, _, path = setup
    interval(setup, ready=back)
    accounts.record(SID, "credit" if back else "main")
    if back:
        usage_row(path, 1_000_000, stamp=NOW - .5)
        monkeypatch.setattr(C, "_options", lambda sid: ("claude-sonnet-4", "default"))
        monkeypatch.setattr(accounts, "logged_out", lambda target: False)
        monkeypatch.setattr(accounts, "email", lambda target: "")
        monkeypatch.setattr(accounts, "ensure_usage", lambda **kw: None)
    calls = []
    monkeypatch.setattr(I, "pane_for_session", lambda sid: "%7")
    monkeypatch.setattr(I, "session_pid", lambda sid: None)
    monkeypatch.setattr(I, "background_work_visible", lambda pane: False)
    monkeypatch.setattr(I, "mark_planned_restart", lambda sid: None)
    monkeypatch.setattr(I, "_pane_command", lambda pane: "zsh")
    monkeypatch.setattr(I, "type_command", lambda *a, before_enter=None: (before_enter and before_enter()) or "sent")
    monkeypatch.setattr(I, "_hung_at_limit", lambda *a, **kw: False)
    monkeypatch.setattr(I.time, "sleep", lambda *a: None)
    monkeypatch.setattr(I, "pass_trust", lambda *a: True)
    monkeypatch.setattr(I, "_resume_dialog_pending", lambda *a: False)
    monkeypatch.setattr(I, "_idle_after_move", lambda *a: None)
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **kw: None)
    monkeypatch.setattr(I.identity, "check", lambda *a, **kw: pytest.fail("credit is not an account login"))
    def tmux(*args):
        calls.append(args)
        if back and args[:4] == ("send-keys", "-t", "%7", "-l"):
            usage_row(path, 9_000_000, stamp=NOW + 1, ident="subscription")
        return SimpleNamespace(returncode=0, stdout=str(path.parent))
    monkeypatch.setattr(I, "_tmux", tmux)
    assert I.account_move(cfg, bot, SID, "main" if back else "credit", lambda *a, **kw: None,
                          defer=True, limit={"id": "limit-one", "reset": NOW + 3600})
    commands = [args[-1] for args in calls if args[:4] == ("send-keys", "-t", "%7", "-l")]
    assert len(commands) == 1
    command = commands[0]
    assert "--resume " + SID in command
    argv = shlex.split(command)
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4"
    assert argv[argv.index("--permission-mode") + 1] == ("default" if back else "plan")
    if back:
        assert "--settings" not in argv
        assert not C.active(SID)
        assert C.spend(NOW + 2) == 3
    else:
        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings["apiKeyHelper"].endswith(" credit-key")
        assert settings["forceLoginMethod"] == "console"
    assert "test-placeholder" not in command


def test_unknown_model_or_permission_never_changes_session(setup, monkeypatch):
    cfg, bot, _, _ = setup
    approve(setup)
    monkeypatch.setattr(C, "_options", lambda sid: None)
    monkeypatch.setattr(I, "account_move", lambda *a, **kw: pytest.fail("unknown model/permission"))
    C.tick(cfg, bot, NOW)
    assert not C.active(SID) and accounts.account_for(SID) == "main"


def test_capture_actual_options_with_hook_mode_fallback(setup, monkeypatch):
    from ctb import direct
    monkeypatch.setattr(C, "_options", REAL_OPTIONS)
    monkeypatch.setattr(I, "pane_for_session", lambda sid: "%7")
    monkeypatch.setattr(I, "pane_mode", lambda pane: "")
    monkeypatch.setattr(usage, "session_model", lambda sid: ("Sonnet 4", "high"))
    direct.record(SID, mode="bypassPermissions")
    assert C._options(SID) == ("claude-sonnet-4", "bypassPermissions")
    monkeypatch.setattr(usage, "session_model", lambda sid: ("unknown", ""))
    assert C._options(SID) is None


REAL_OPTIONS = C._options


def test_corrupt_credit_state_blocks_known_credit_process(setup):
    interval(setup)
    C._path().write_text("{broken")
    assert C.spend(NOW) is None
    assert C.blocked(SID, NOW)
    assert C.stop_line(NOW) == C.UNKNOWN


def test_reset_at_text_is_detected_and_parsed():
    assert accounts.QUOTA_HIT.search("Usage limit reached. Resets at 8pm")
    stamp = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    reset = usage.reset_time("Usage limit reached. Resets at 8pm", stamp)
    assert reset.hour == 20 and reset.date() == stamp.date()


def test_pane_notice_then_transcript_flush_does_not_duplicate_card(setup, monkeypatch):
    cfg, bot, found, _ = setup
    reset = usage.reset_time(TEXT, datetime.fromtimestamp(NOW).astimezone()).timestamp()
    monkeypatch.setattr(usage, "limit_of", lambda sid: None)
    assert C.offer(cfg, bot, SID, TEXT, found)
    monkeypatch.setattr(usage, "limit_of", lambda sid: {"id": "later-flushed-id", "reset": reset})
    assert not C.offer(cfg, bot, SID, TEXT, found)
    assert len(D.open_decisions()) == 1


def test_heartbeat_always_checks_credit_before_optional_refresh(setup, monkeypatch):
    cfg, bot, _, _ = setup
    from ctb import policy
    monkeypatch.setattr(policy, "on", lambda: True)
    monkeypatch.setattr(heartbeat, "_check_funnel", lambda *a: None)
    calls = []
    # Stop this test immediately after the credit gate, before unrelated heartbeat work.
    class Checked(BaseException):
        pass
    def tick(c, b, now):
        calls.append(now)
        raise Checked()
    monkeypatch.setattr(C, "tick", tick)
    with pytest.raises(Checked):
        heartbeat.run_once(cfg, bot, NOW)
    assert calls == [NOW]


@pytest.mark.parametrize("mirror_on, policy_on", [(False, True), (True, False), (False, False)])
def test_heartbeat_enforces_credit_with_mirroring_or_policy_off(setup, monkeypatch, mirror_on, policy_on):
    # Review #423f: turning mirroring off must not disable the paid-spend watchdog.
    cfg, bot, _, _ = setup
    from ctb import policy
    monkeypatch.setattr(cfg, "mirror", mirror_on, raising=False)
    monkeypatch.setattr(policy, "on", lambda: policy_on)
    monkeypatch.setattr(heartbeat, "_check_funnel", lambda *a: None)
    calls = []
    monkeypatch.setattr(C, "tick", lambda c, b, now: calls.append(now))
    heartbeat.run_once(cfg, bot, NOW)
    assert calls == [NOW]


def test_telegram_tap_bound_to_owner_topic_and_decision(setup, monkeypatch):
    cfg, bot, _, _ = setup
    rec = offer(setup)
    class Topics:
        def session_at(self, chat, thread):
            return SID
    q = {"id": "button", "from": {"id": 54321}, "data": "cr:" + rec["id"] + ":yes",
         "message": {"message_id": rec["notice_message"], "message_thread_id": 7,
                     "chat": {"id": 12345, "type": "private"}}}
    box = I.default_inbox()
    assert I.handle_button(q, cfg, bot, Topics(), box) == "ignored"
    q["from"]["id"] = 12345
    monkeypatch.setattr(D, "announce", lambda *a: None)
    assert I.handle_button(q, cfg, bot, Topics(), box) == "credit-answered"
    assert not box.pending(SID)
    assert I.handle_button(q, cfg, bot, Topics(), box) == "credit-closed"


@pytest.mark.parametrize("copy", ["forwarded", "different-message", "missing-message"])
def test_credit_tap_refuses_copied_notice(setup, monkeypatch, copy):
    cfg, bot, _, _ = setup
    rec = offer(setup)
    message = {"message_id": rec["notice_message"], "message_thread_id": 7,
               "chat": {"id": 12345, "type": "private"}}
    if copy == "forwarded":
        message["forward_origin"] = {"type": "user", "date": int(NOW)}
    elif copy == "different-message":
        message["message_id"] += 1
    else:
        message.pop("message_id")
    q = {"id": "button", "from": {"id": 12345}, "data": "cr:" + rec["id"] + ":yes",
         "message": message}
    topics = SimpleNamespace(session_at=lambda *a: SID)
    monkeypatch.setattr(D, "announce", lambda *a: pytest.fail("copied notice cannot authorize credit"))
    assert I.handle_button(q, cfg, bot, topics, I.default_inbox()) in ("ignored", "credit-closed")
    assert D._load(state_dir() / D.STATE_FILE)[rec["id"]]["state"] == "open"
    assert not C.active(SID)


def test_new_exhaustion_after_return_requires_new_approval(setup, monkeypatch):
    cfg, bot, found, _ = setup
    approved = approve(setup)
    interval(setup)
    C.leaving(SID, NOW)
    accounts.record(SID, "main")
    monkeypatch.setattr(usage, "limit_of", lambda sid: {"id": "second-limit", "reset": NOW + 7200})
    assert C.offer(cfg, bot, SID, TEXT, found)
    monkeypatch.setattr(I, "account_move", lambda *a, **kw: pytest.fail("new exhaustion has no approval"))
    # Mark the historical approval consumed, as the normal activation does.
    with locked_map(C._path()) as data:
        data.setdefault("consumed", {})[approved["id"]] = True
    C.tick(cfg, bot, NOW)
    assert not C.active(SID)


def test_deferred_activation_does_not_bill_old_subscription_work(setup, monkeypatch):
    cfg, bot, _, path = setup
    approve(setup)
    monkeypatch.setattr(I, "account_move", lambda *a, **kw: False)
    C.tick(cfg, bot, NOW)
    assert C.active(SID)["billing_start"] is None
    assert C.status_line(SID, NOW) == ""
    usage_row(path, 4_000_000, stamp=NOW + 1, ident="subscription-before-exit")
    assert C.spend(NOW + 2) == 0
    C.starting(SID, NOW + 3)
    usage_row(path, 1_000_000, stamp=NOW + 4, ident="credit-after-exit")
    assert C.spend(NOW + 5) == 3


def test_pending_resume_dialog_is_not_restarted_again(setup, monkeypatch):
    cfg, bot, _, _ = setup
    approve(setup)
    moves, resumes = [], []
    def move(c, b, sid, target, say, **kw):
        accounts.record(sid, "credit")
        C.starting(sid, NOW)
        I.mark_resume_pending(sid, 0, "credit")
        moves.append(sid)
        return False  # existing move path reached its resume dialog
    monkeypatch.setattr(I, "account_move", move)
    monkeypatch.setattr(I, "_queue_replay", lambda *a: 1)
    monkeypatch.setattr(I, "resume_after_move", lambda *a, **kw: resumes.append(SID) or "dialog")
    monkeypatch.setattr(M, "send_essential", lambda *a: True)
    C.tick(cfg, bot, NOW)
    C.tick(cfg, bot, NOW)
    C.tick(cfg, bot, NOW)
    assert len(moves) == 1 and len(resumes) == 2
    assert C.active(SID)["ready"]


@pytest.mark.parametrize("source", ["clear", "resume"])
def test_credit_process_switch_is_quarantined_counted_and_returned(setup, monkeypatch, source):
    from ctb import wake
    cfg, bot, _, old_path = setup
    interval(setup)
    usage_row(old_path, 1_000_000, stamp=NOW - .5)
    new_sid = "d" * 8 + "-0000-4000-8000-" + "e" * 12
    new_path = old_path.parent / (new_sid + ".jsonl")
    new_path.write_text("")
    with locked_map(state_dir() / "tmux-panes.json") as panes:
        panes[SID] = "%7"
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setenv("CTB_CREDIT_SESSION", SID)
    monkeypatch.setattr(wake, "_own_chain", lambda: [123])
    monkeypatch.setattr(wake, "_is_session_process", lambda *a: True)
    monkeypatch.setattr(wake, "_pane_is_ours", lambda *a: True)
    monkeypatch.setattr(wake, "load_config", lambda: cfg)
    monkeypatch.setattr(wake, "spawn", lambda *a, **kw: None)
    monkeypatch.setattr(wake, "_recall", lambda *a: None)
    assert wake.hook_start(json.dumps({"session_id": new_sid, "source": source,
                                      "transcript_path": str(new_path)})) == 0
    assert C.blocked(new_sid, NOW), "a different conversation has no paid-turn approval"
    assert not C.active(new_sid) and not C.active(SID)
    assert accounts.account_for(new_sid) == accounts.CREDIT
    usage_row(new_path, 1_000_000, stamp=NOW + 1, ident="switched-message")
    assert C.spend(NOW + 2) == 6
    moves = []
    monkeypatch.setattr(I, "pane_runs_session", lambda *a: True)
    monkeypatch.setattr(I, "_tmux", lambda *a: SimpleNamespace(returncode=0))
    monkeypatch.setattr(I, "session_pid", lambda *a: None)
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a, **kw: True)
    monkeypatch.setattr(I, "account_move", lambda c, b, s, target, say, **kw: moves.append((s, target)) or True)
    monkeypatch.setattr(M, "send_essential", lambda *a: True)
    C.tick(cfg, bot, NOW + 3600)
    assert moves == [(new_sid, "main")]


def test_credit_process_marker_blocks_even_without_session_mapping(setup, monkeypatch):
    interval(setup)
    monkeypatch.setenv("CTB_CREDIT_SESSION", SID)
    assert C.blocked("unapproved-conversation", NOW)


@pytest.mark.parametrize("blocked_at", [1, 2, 3])
def test_async_wake_checks_credit_before_taking_messages(setup, monkeypatch, blocked_at):
    from ctb import wake
    box = I.default_inbox()
    box.put(SID, {"text": "continue", "id": 1, "at": int(NOW)})
    checks = []
    def blocked(*a):
        checks.append(True)
        return len(checks) >= blocked_at
    monkeypatch.setattr(C, "blocked", blocked)
    monkeypatch.setattr(wake.time, "sleep", lambda *a: None)
    monkeypatch.setattr(wake, "limit_active", lambda *a: False)
    monkeypatch.setattr(wake, "cold_compact", lambda *a: False)
    monkeypatch.setattr(wake, "before_take", lambda *a, **kw: False)
    monkeypatch.setattr(wake, "spawn_delivered", lambda *a: pytest.fail("blocked wake must not deliver"))
    assert wake.wait_for_message({"session_id": SID, "transcript_path": str(setup[3])},
                                 inbox=box, settle=0, max_wait=1) == (0, "")
    assert box.pending(SID)


def test_expired_activation_cannot_launch_or_revive(setup, monkeypatch):
    from ctb import revive
    interval(setup, reset=NOW)
    assert not C.active(SID), "expired approval is not an active authorization"
    assert C.blocked(SID, NOW)
    with pytest.raises(ValueError, match="credit activation unavailable"):
        accounts.shell_command(SID)
    monkeypatch.setattr(revive, "open_windows", lambda: [])
    monkeypatch.setattr(I, "resume_session", lambda *a, **kw: pytest.fail("expired approval must not resume"))
    assert revive.reopen([SID], sleep_fn=lambda *a: None) == []


@pytest.mark.parametrize("record", ["absent", "dead"])
def test_crashed_credit_session_returns_to_subscription_without_restart(setup, monkeypatch, record):
    from ctb import revive
    cfg, bot, _, path = setup
    interval(setup, reset=NOW)
    files = []
    if record == "dead":
        file = path.parent / "process.json"
        file.write_text(json.dumps({"sessionId": SID, "pid": 654321}))
        files.append(file)
    monkeypatch.setattr(accounts, "_session_record_files_strict", lambda: files)
    def dead(pid, signal):
        assert pid == 654321 and signal == 0
        raise ProcessLookupError()
    monkeypatch.setattr(C.os, "kill", dead)
    monkeypatch.setattr(C, "_has_live_process", REAL_HAS_LIVE_PROCESS)
    monkeypatch.setattr(accounts, "restart_is_safe", lambda *a, **kw: pytest.fail("no restart needed"))
    monkeypatch.setattr(I, "account_move", lambda *a, **kw: pytest.fail("no launch during crash recovery"))
    monkeypatch.setattr(M, "send_essential", lambda *a: True)
    monkeypatch.setattr(revive, "open_windows", lambda: [])
    resumed = []
    def resume(sid):
        resumed.append((sid, accounts.account_for(sid), accounts.shell_command(sid)))
        return True
    assert revive.reopen([SID], resume=resume, sleep_fn=lambda *a: None) == []
    C.tick(cfg, bot, NOW)
    assert not resumed and C._open(SID) is None
    assert accounts.account_for(SID) == accounts.MAIN and not C.blocked(SID, NOW)
    assert revive.reopen([SID], resume=resume, sleep_fn=lambda *a: None) == [SID]
    assert resumed[0][0:2] == (SID, accounts.MAIN)
    assert "apiKeyHelper" not in resumed[0][2] and "CTB_CREDIT_SESSION" not in resumed[0][2]


@pytest.mark.parametrize("records", ["scan-error", "corrupt-record", "unreadable-record"])
def test_unknown_process_state_cannot_clear_credit_binding(setup, monkeypatch, records):
    cfg, bot, _, path = setup
    interval(setup, reset=NOW)
    file = path.parent / "process.json"
    if records == "unreadable-record":
        file.mkdir()
    else:
        file.write_text("{unfinished")
    monkeypatch.setattr(accounts, "_session_record_files_strict",
                        lambda: None if records == "scan-error" else [file])
    monkeypatch.setattr(C, "_has_live_process", REAL_HAS_LIVE_PROCESS)
    monkeypatch.setattr(I, "account_move", lambda *a, **kw: pytest.fail("process state is unknown"))
    C.tick(cfg, bot, NOW)
    assert C._open(SID) and accounts.account_for(SID) == accounts.CREDIT
    assert C.blocked(SID, NOW)


@pytest.mark.parametrize("phase", ["tap", "activation"])
@pytest.mark.parametrize("change", ["account", "limit", "cleared", "room", "other_room"])
def test_offer_revalidates_account_exhaustion_and_subscription_room(setup, monkeypatch, phase, change):
    cfg, bot, _, _ = setup
    rec = approve(setup) if phase == "activation" else offer(setup)
    if change == "account":
        accounts.record(SID, "other")
        monkeypatch.setattr(accounts, "account_for", lambda s: "other")
    elif change == "limit":
        monkeypatch.setattr(usage, "limit_of", lambda s: {"id": "different-limit", "reset": NOW + 3600})
    elif change == "cleared":
        monkeypatch.setattr(usage, "limit_of", lambda s: None)
    elif change == "room":
        monkeypatch.setattr(accounts, "spent", lambda *a, **kw: False)
    else:
        monkeypatch.setattr(accounts, "fallback_plan", lambda *a, **kw: {"fresh": ["other"]})
    monkeypatch.setattr(I, "account_move", lambda *a, **kw: pytest.fail("stale offer must never move"))
    if phase == "tap":
        with pytest.raises(D.DecisionError):
            D.answer(cfg, I.default_inbox(), rec["id"], "yes", now=NOW)
    else:
        C.tick(cfg, bot, NOW)
    assert not C.active(SID)


@pytest.mark.parametrize("text", ["Limit reached while retrying your database query",
                                 "Usage limit reached in the batch processor",
                                 "You've hit your limit of database retries"])
def test_ordinary_limit_reply_never_marks_or_moves_account(setup, monkeypatch, text):
    cfg, bot, _, _ = setup
    monkeypatch.setattr(accounts, "note_limit", lambda *a: pytest.fail("ordinary reply is not a plan limit"))
    monkeypatch.setattr(M, "_essential_place", lambda *a: pytest.fail("ordinary reply must not offer a move"))
    assert not accounts.QUOTA_HIT.search(text)
    assert not M.limit_offer(cfg, bot, SID, text)


def test_limit_shaped_assistant_text_requires_api_error_evidence(setup, monkeypatch):
    cfg, bot, _, _ = setup
    monkeypatch.setattr(usage, "limit_of", lambda *a: None)
    monkeypatch.setattr(accounts, "note_limit", lambda *a: pytest.fail("unverified text must not change quota"))
    assert not M.limit_offer(cfg, bot, SID, TEXT)


def test_stop_failure_does_not_treat_last_reply_as_api_error(setup, monkeypatch):
    cfg, bot, _, path = setup
    monkeypatch.setattr(usage, "limit_of", lambda *a: None)
    monkeypatch.setattr(hooks, "load_config", lambda: cfg)
    monkeypatch.setattr(M.Bot, "from_config", lambda *a: bot)
    monkeypatch.setattr(hooks, "_idle_after_failure", lambda *a: None)
    monkeypatch.setattr(accounts, "note_limit", lambda *a: pytest.fail("overload is not a plan limit"))
    hooks.hook_stop_failure(json.dumps({"session_id": SID, "transcript_path": str(path),
                                      "error": "API Error: 500 overloaded", "last_assistant_message": TEXT}))
    assert not bot.calls


def test_stop_failure_offers_credit_before_error_transcript_is_flushed(setup, monkeypatch):
    cfg, bot, _, path = setup
    monkeypatch.setattr(usage, "limit_of", lambda *a: None)
    monkeypatch.setattr(hooks, "load_config", lambda: cfg)
    monkeypatch.setattr(M.Bot, "from_config", lambda *a: bot)
    monkeypatch.setattr(hooks, "_idle_after_failure", lambda *a: None)
    monkeypatch.setattr(accounts, "note_limit", lambda *a: None)
    monkeypatch.setattr(M.identity, "check", lambda *a, **kw: None)
    monkeypatch.setattr(accounts, "fallback_plan", lambda *a, **kw: {"fresh": [], "auto": False})
    hooks.hook_stop_failure(json.dumps({"session_id": SID, "transcript_path": str(path),
                                      "error": "usage_limit_reached", "last_assistant_message": TEXT}))
    assert len(D.open_decisions()) == 1


@pytest.mark.parametrize("change", ["account", "cleared", "other_room"])
def test_deferred_credit_move_rechecks_live_eligibility(setup, monkeypatch, change):
    cfg, bot, _, _ = setup
    approve(setup)
    monkeypatch.setattr(I, "account_move", lambda *a, **kw: False)
    C.tick(cfg, bot, NOW)
    assert C.active(SID)["billing_start"] is None
    if change == "account":
        monkeypatch.setattr(accounts, "account_for", lambda *a: "other")
    elif change == "cleared":
        monkeypatch.setattr(usage, "limit_of", lambda *a: None)
    else:
        monkeypatch.setattr(accounts, "fallback_plan", lambda *a, **kw: {"fresh": ["other"]})
    monkeypatch.setattr(I, "account_move", lambda *a, **kw: pytest.fail("deferred consent became stale"))
    C.tick(cfg, bot, NOW + 1)
    assert not C.active(SID)


def test_disabled_spend_gate_disables_offers_answers_launches_and_wake(setup, monkeypatch):
    from ctb import wake
    cfg, bot, found, path = setup
    rec = offer(setup)  # simulate a card created before the spend gate closed
    monkeypatch.setattr(C.credit_usage, "spend_gate_available", lambda: False)
    with pytest.raises(D.DecisionError):
        D.answer(cfg, I.default_inbox(), rec["id"], "yes", now=NOW)
    assert not C.offer(cfg, bot, "other", TEXT, found)
    interval(setup)
    assert C.blocked(SID, NOW)
    with pytest.raises(ValueError):
        accounts.shell_command(SID)
    box = I.default_inbox()
    box.put(SID, {"text": "continue", "id": 1, "at": int(NOW)})
    monkeypatch.setattr(wake.time, "sleep", lambda *a: None)
    assert wake.wait_for_message({"session_id": SID, "transcript_path": str(path)},
                                 inbox=box, settle=0, max_wait=1) == (0, "")
    assert box.pending(SID)
    sent = []
    monkeypatch.setattr(M, "send_essential", lambda c, b, s, p: sent.append(p["text"]) or True)
    monkeypatch.setattr(I, "account_move", lambda *a, **kw: False)  # parking may retry subscription
    C.tick(cfg, bot, NOW)
    assert sent == [C.UNAVAILABLE]


def test_subscription_recovered_early_blocks_credit_relaunch(setup, monkeypatch):
    interval(setup)
    with locked_map(accounts.usage_path("main")) as reading:
        reading["at"] = datetime.fromtimestamp(NOW, timezone.utc).isoformat()
    monkeypatch.setattr(accounts, "spent", lambda *a, **kw: False)
    assert C.blocked(SID, NOW)
    with pytest.raises(ValueError):
        accounts.shell_command(SID)


@pytest.mark.parametrize("once", [True, False])
def test_heartbeat_service_runs_the_credit_round_while_the_policy_is_off(setup, monkeypatch, once):
    # Review #423g: the service loop and --once must not skip run_once (and its credit watchdog).
    cfg, bot, _, _ = setup
    from ctb import policy, revive
    monkeypatch.setattr(policy, "on", lambda: False)
    monkeypatch.setattr(heartbeat.Bot, "from_config", classmethod(lambda cls, c: bot), raising=False)
    monkeypatch.setattr(revive, "revive_in_background", lambda: None, raising=False)
    calls = []
    monkeypatch.setattr(heartbeat, "run_once", lambda c, bot=None, now=None: calls.append(True))
    class Done(BaseException):
        pass
    def sleep(_):
        raise Done()
    if once:
        heartbeat.run(cfg, once=True)
    else:
        with pytest.raises(Done):
            heartbeat.run(cfg, sleep_fn=sleep)
    assert calls == [True]


def test_credit_withdrawn_while_exit_waits_to_be_typed_sends_no_enter(setup, monkeypatch, credit_move_pane):
    """The approval can lapse during type_command's own lock wait: the gate runs right before Enter."""
    cfg, bot, _, _ = setup
    interval(setup, ready=False)
    with locked_map(C._path()) as data:
        data["intervals"][SID]["billing_start"] = None
    accounts.record(SID, accounts.MAIN)
    calls, typed = credit_move_pane
    real = I.type_command

    def slow_type(sid, pane, text, before_enter=None):
        with locked_map(C._path()) as data:  # withdrawn while the keys were waiting for the terminal lock
            data["intervals"][SID]["revoked"] = True
        return real(sid, pane, text, before_enter=before_enter)

    monkeypatch.setattr(I, "type_command", slow_type)
    assert not I.account_move(cfg, bot, SID, accounts.CREDIT, lambda *a, **kw: None,
                              defer=True, limit={"id": "limit-one", "reset": NOW + 3600})
    assert typed == []
    assert accounts.origin_account(SID) == accounts.MAIN
    assert C._open(SID)["billing_start"] is None
    assert not I.planned_restart_marker(SID).exists()


def _real_move_rig(setup, monkeypatch, calls):
    cfg, bot, _, path = setup
    interval(setup, ready=False)
    accounts.record(SID, "main")
    monkeypatch.setattr(I, "pane_for_session", lambda sid: "%7")
    monkeypatch.setattr(I, "session_pid", lambda sid: None)
    monkeypatch.setattr(I, "background_work_visible", lambda pane: False)
    monkeypatch.setattr(I, "mark_planned_restart", lambda sid: None)
    monkeypatch.setattr(I, "_pane_command", lambda pane: "zsh")
    monkeypatch.setattr(I, "_hung_at_limit", lambda *a, **kw: False)
    monkeypatch.setattr(I.time, "sleep", lambda *a: None)
    monkeypatch.setattr(I, "pass_trust", lambda *a: True)
    monkeypatch.setattr(I, "_resume_dialog_pending", lambda *a: False)
    monkeypatch.setattr(I, "_idle_after_move", lambda *a: None)
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **kw: None)
    monkeypatch.setattr(I.identity, "check", lambda *a, **kw: None)
    monkeypatch.setattr(I, "_tmux", lambda *args: calls.append(args) or
                        SimpleNamespace(returncode=0, stdout=str(path.parent)))
    return cfg, bot


@pytest.mark.parametrize("change", ["limit_cleared", "spare_has_room"])
def test_credit_gate_before_enter_refuses_when_a_subscription_would_do(setup, monkeypatch, change):
    calls, typed = [], []
    cfg, bot = _real_move_rig(setup, monkeypatch, calls)

    def type_command(sid, pane, text, before_enter=None):
        if change == "limit_cleared":
            monkeypatch.setattr(usage, "limit_of", lambda *a: None)
        else:
            monkeypatch.setattr(accounts, "fallback_plan", lambda *a, **kw: {"fresh": ["spare"], "auto": True})
        try:
            before_enter()
        except Exception:
            return "failed"
        typed.append(text)
        return "sent"

    monkeypatch.setattr(I, "type_command", type_command)
    assert not I.account_move(cfg, bot, SID, accounts.CREDIT, lambda *a, **kw: None,
                              defer=True, limit={"id": "limit-one", "reset": NOW + 3600})
    assert typed == [] and accounts.origin_account(SID) == "main"
    assert (C._open(SID) or {}).get("billing_start") is None


def test_credit_refused_at_launch_after_exit_resumes_on_the_previous_account(setup, monkeypatch):
    calls, said = [], []
    cfg, bot = _real_move_rig(setup, monkeypatch, calls)
    monkeypatch.setattr(I, "type_command", lambda *a, before_enter=None: (before_enter and before_enter()) or "sent")
    real = accounts.shell_command

    def shell_command(session=None, remember=False, replace=True):
        if accounts.account_for(session) == accounts.CREDIT:  # the approval lapsed in the last instant
            raise accounts.CreditActivationUnavailable("expired")
        return real(session, remember=remember, replace=replace)

    monkeypatch.setattr(accounts, "shell_command", shell_command)
    result = I.account_move(cfg, bot, SID, accounts.CREDIT, lambda *a, **kw: said.append(a),
                            defer=True, limit={"id": "limit-one", "reset": NOW + 3600})
    commands = [a[-1] for a in calls if a[:4] == ("send-keys", "-t", "%7", "-l")]
    assert len(commands) == 1 and "--resume " + SID in commands[0]  # never left stopped
    assert accounts.origin_account(SID) == "main"
    assert result is None  # resumed on the previous account, not a successful credit move
    assert C._open(SID) is None
    assert not C.blocked(SID)


def test_credit_approval_lapses_during_exit_tick_resumes_previous_without_credit_ready(setup, monkeypatch):
    calls, moves = [], []
    cfg, bot = _real_move_rig(setup, monkeypatch, calls)
    with locked_map(C._path()) as data:
        data.pop("intervals")  # let the heartbeat create the pending activation from a real approval
    answer = approve(setup)
    monkeypatch.setattr(I, "type_command", lambda *a, before_enter=None: (before_enter and before_enter()) or "sent")

    def exit_finished(pane):
        monkeypatch.setattr(C.time, "time", lambda: NOW + 3601)
        return "zsh"

    monkeypatch.setattr(I, "_pane_command", exit_finished)
    real_move = I.account_move

    def move(*a, **kw):
        result = real_move(*a, **kw)
        moves.append(result)
        return result

    monkeypatch.setattr(I, "account_move", move)
    monkeypatch.setattr(M, "send_essential", lambda *a: True)
    for name in ("_limit_cleared_by_move", "_queue_replay", "mark_resume_pending", "resume_after_move"):
        monkeypatch.setattr(I, name, lambda *a, **kw: pytest.fail("previous-account resume is not credit ready"))
    C.tick(cfg, bot, NOW)
    C.tick(cfg, bot, NOW + 3601)  # a closed activation must not try a same-login move back
    commands = [a[-1] for a in calls if a[:4] == ("send-keys", "-t", "%7", "-l")]
    assert len(commands) == 1 and "--resume " + SID in commands[0]
    assert "CTB_CREDIT_SESSION=" not in commands[0]
    assert moves == [None]
    assert accounts.origin_account(SID) == accounts.MAIN
    rec = read_map(C._path())["intervals"][answer["id"]]
    assert rec["end"] == NOW + 3601
    assert rec["billing_start"] is None and rec["ready"] is False
    assert C._open(SID) is None
    assert not C.active(SID) and not C.blocked(SID)
    assert not I.resume_pending(SID)
    assert not accounts.deferred_move(SID)
