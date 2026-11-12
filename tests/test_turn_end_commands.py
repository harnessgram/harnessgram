import json
import threading

import pytest

from ctb import compact, inbox as I
from ctb.config import Config


@pytest.fixture
def setup(monkeypatch, bot):
    cfg = Config(owner_chat_id=123, ui_language="en")
    message = {"chat": {"id": 123}, "message_thread_id": 44, "message_id": 55}
    monkeypatch.setattr(I, "session_busy", lambda session: False)
    monkeypatch.setattr(I, "pane_for_session", lambda session: "%1")
    monkeypatch.setattr(I, "_pane_from_process", lambda session: "%1")
    monkeypatch.setattr(I, "pane_text", lambda *args: "prompt")
    monkeypatch.setattr(I, "_dialog_open", lambda *args: False)
    monkeypatch.setattr(I, "dismiss_tip", lambda *args: None)
    monkeypatch.setattr(I, "_model_watch_start", lambda session: (None, 0))
    monkeypatch.setattr(I, "_watch_model_result", lambda *args, **kwargs: None)
    return cfg, message, bot


def entries():
    return list(compact.directory().glob("asked-command-" + "?" * 64))


def test_restart_and_busy_typing_retry(setup, monkeypatch):
    cfg, message, bot = setup
    q = {"id": "sample", "data": "cw:end:/model opus"}
    I.command_when(q, cfg, bot, message, "sample-session")
    path, = entries()
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["thread"] == 44
    calls = []
    def typing(*args, **kwargs):
        calls.append(args[-1])
        return "busy" if len(calls) == 1 else "sent"
    monkeypatch.setattr(I, "type_command", typing)
    I.run_turn_commands(cfg, bot)
    assert entries()
    I.run_turn_commands(cfg, bot)
    I.run_turn_commands(cfg, bot)
    assert len(calls) == 2 and entries() == []


def test_two_runners_claim_once(setup, monkeypatch):
    cfg, message, bot = setup
    I._queue_turn_command(cfg, message, "sample-session", "/model opus")
    started, release = threading.Event(), threading.Event()
    calls = []
    def execute(*args, **kwargs):
        calls.append(args[4])
        started.set()
        assert release.wait(2)
        return "sent"
    monkeypatch.setattr(I, "cli_command", execute)
    runner = threading.Thread(target=I.run_turn_commands, args=(cfg, bot))
    runner.start()
    assert started.wait(2)
    I.run_turn_commands(cfg, bot)
    release.set()
    runner.join(2)
    assert not runner.is_alive() and calls == ["/model opus"] and entries() == []


@pytest.mark.parametrize("lang,expected", [("en", "not run"), ("he", "לא בוצעה")])
def test_expiry_notifies_once_in_topic(setup, monkeypatch, lang, expected):
    cfg, message, bot = setup
    cfg.ui_language = lang
    I._queue_turn_command(cfg, message, "sample-session", "/model opus")
    path, = entries()
    request = json.loads(path.read_text())
    request["at"] -= I.TURN_COMMAND_TTL + 1
    I.write_private(path, json.dumps(request))
    I.run_turn_commands(cfg, bot)
    I.run_turn_commands(cfg, bot)
    notices = bot.methods("sendMessage")
    assert len(notices) == 1 and notices[0]["message_thread_id"] == 44
    assert expected in notices[0]["text"] and notices[0]["text"].endswith("\n/model")
    assert entries() == []


def test_newer_command_replaces_same_name(setup, monkeypatch):
    cfg, message, bot = setup
    I._queue_turn_command(cfg, message, "sample-session", "/model opus")
    I._queue_turn_command(cfg, message, "sample-session", "/model sonnet")
    path, = entries()
    assert json.loads(path.read_text())["text"] == "/model sonnet"
    calls = []
    monkeypatch.setattr(I, "cli_command", lambda *args, **kwargs: calls.append(args[4]) or "sent")
    I.run_turn_commands(cfg, bot)
    assert calls == ["/model sonnet"]


def test_stop_now_keeps_escape_behavior(setup, monkeypatch):
    cfg, message, bot = setup
    actions = []
    monkeypatch.setattr(I, "forget_replay", lambda session: None)
    monkeypatch.setattr(I, "_tmux", lambda *args: actions.append(args))
    monkeypatch.setattr(I.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(I, "cli_command", lambda *args, **kwargs: actions.append((args[4], kwargs["forced"])))
    I.command_when({"id": "sample", "data": "cw:now:/model opus"}, cfg, bot, message, "sample-session")
    assert actions == [("send-keys", "-t", "%1", "Escape"), ("/model opus", True)]
    assert entries() == []


def test_no_execution_when_bridge_is_off(setup, monkeypatch):
    from ctb import policy
    cfg, message, bot = setup
    I._queue_turn_command(cfg, message, "sample-session", "/model opus")
    monkeypatch.setattr(policy, "on", lambda: False)
    monkeypatch.setattr(I, "cli_command", lambda *args, **kwargs: pytest.fail("executed while off"))
    I.run_turn_commands(cfg, bot)
    assert len(entries()) == 1


@pytest.mark.parametrize("route", ["now", "typed"])
def test_new_choice_cancels_pending_under_its_lock(setup, monkeypatch, route):
    import contextlib

    cfg, message, bot = setup
    I._queue_turn_command(cfg, message, "sample-session", "/model opus")
    path, = entries()
    held = []
    original = compact._ask_lock

    @contextlib.contextmanager
    def locked(key):
        with original(key) as locked_path:
            held.append(key)
            yield locked_path
            assert not path.exists()

    monkeypatch.setattr(compact, "_ask_lock", locked)
    monkeypatch.setattr(I, "forget_replay", lambda session: None)
    monkeypatch.setattr(I, "_tmux", lambda *args: None)
    monkeypatch.setattr(I.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(I, "type_command", lambda *args, **kwargs: "sent")
    monkeypatch.setattr(I, "_terminal_background", lambda session, run: run() or True)
    if route == "now":
        I.command_when({"id": "sample", "data": "cw:now:/MODEL@sample_bot sonnet"},
                       cfg, bot, message, "sample-session")
    else:
        I.cli_command(cfg, bot, message, "sample-session", "/MODEL@sample_bot sonnet")
    assert held and entries() == []


@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("lang", ["en", "he"])
def test_claimed_request_is_dropped_with_uncertainty_notice(setup, monkeypatch, lang, busy):
    cfg, message, bot = setup
    cfg.ui_language = lang
    I._queue_turn_command(cfg, message, "sample-session", "/model opus")
    path, = entries()
    req = json.loads(path.read_text())
    I.write_private(path, json.dumps(dict(req, claimed=True)))
    monkeypatch.setattr(I, "session_busy", lambda session: busy)
    monkeypatch.setattr(I, "cli_command", lambda *args, **kwargs: pytest.fail("replayed a claim"))
    I.run_turn_commands(cfg, bot)
    I.run_turn_commands(cfg, bot)
    notices = bot.methods("sendMessage")
    assert entries() == [] and len(notices) == 1
    assert notices[0]["chat_id"] == 123 and notices[0]["message_thread_id"] == 44
    assert notices[0]["text"] == I.t("cw_uncertain", lang=lang, command="/model")
    assert notices[0]["text"] != I.t("cw_expired", lang=lang, command="/model")


@pytest.mark.parametrize("live_pane", [None, "%2"])
def test_reused_or_missing_pane_drops_request(setup, monkeypatch, live_pane):
    cfg, message, bot = setup
    I._queue_turn_command(cfg, message, "sample-session", "/model opus")
    monkeypatch.setattr(I, "_pane_from_process", lambda session: live_pane)
    monkeypatch.setattr(I, "cli_command", lambda *args, **kwargs: pytest.fail("sent to reused pane"))
    I.run_turn_commands(cfg, bot)
    assert entries() == []
    notice, = bot.methods("sendMessage")
    assert notice["message_thread_id"] == 44
    assert notice["text"] == I.t("cw_uncertain", lang="en", command="/model")


@pytest.mark.real_type_command
@pytest.mark.parametrize("lost_at", ["before_typing", "before_enter"])
def test_recovery_rechecks_live_session_before_keys(setup, monkeypatch, lost_at):
    from conftest import BoxedTmux

    cfg, message, bot = setup
    pane = BoxedTmux()
    monkeypatch.setattr(I, "_tmux", pane)
    monkeypatch.setattr(I.time, "sleep", lambda seconds: None)
    reads = []
    dismissed = []
    monkeypatch.setattr(I, "dismiss_tip", lambda *args: dismissed.append(args))

    def live(session):
        reads.append(session)
        if lost_at == "before_typing" and len(reads) > 1:
            return None
        if lost_at == "before_enter" and pane.box:
            return None
        return "%1"

    monkeypatch.setattr(I, "_pane_from_process", live)
    I._queue_turn_command(cfg, message, "sample-session", "/model opus")
    I.run_turn_commands(cfg, bot)
    assert pane.sent == [] and entries() == []
    if lost_at == "before_typing":
        assert not [call for call in pane.calls if call[0] == "send-keys"]
        assert dismissed == []
    else:
        assert pane.box.startswith("/model ")
        assert [call for call in pane.calls if call[0] == "send-keys"] == [
            ("send-keys", "-t", "%1", "-l", pane.box)]
    notice, = bot.methods("sendMessage")
    assert notice["text"] == I.t("cw_uncertain", lang="en", command="/model")


@pytest.mark.parametrize("bad", [
    {"at": "bad"}, [], "broken json", {},
    *[{field: value} for field, value in [
        ("id", None), ("session", 7), ("session", ""), ("text", " "), ("text", []),
        ("chat", "123"), ("chat", True), ("thread", []), ("message", None),
        ("at", "bad"), ("at", True), ("at", float("nan")), ("at", float("inf")),
        ("at", float("-inf")), ("at", 10 ** 400), ("claimed", "true"), ("text", "\ud800"),
    ]],
])
def test_malformed_entry_is_deleted_without_blocking_next(setup, monkeypatch, bad):
    cfg, message, bot = setup
    I._queue_turn_command(cfg, message, "sample-session", "/model opus")
    bad_path, = entries()
    req = json.loads(bad_path.read_text())
    payload = dict(req, **bad) if isinstance(bad, dict) and bad else bad
    I.write_private(bad_path, json.dumps(payload) if bad != "broken json" else bad)
    I._queue_turn_command(cfg, message, "other-session", "/model sonnet")
    good_path, = [path for path in entries() if path != bad_path]
    directory = compact.directory()
    original_glob = type(directory).glob
    monkeypatch.setattr(type(directory), "glob", lambda path, pattern: iter([bad_path, good_path])
                        if path == directory and pattern == "asked-command-*" else original_glob(path, pattern))
    calls = []
    monkeypatch.setattr(I, "cli_command", lambda *args, **kwargs: calls.append(args[3]) or "sent")
    I.run_turn_commands(cfg, bot)
    assert calls == ["other-session"]
    assert not bad_path.exists() and not good_path.exists()


def test_stop_now_does_not_cancel_a_later_turn_end_choice(setup, monkeypatch):
    cfg, message, bot = setup
    I._queue_turn_command(cfg, message, "sample-session", "/model opus")
    monkeypatch.setattr(I, "forget_replay", lambda session: None)
    monkeypatch.setattr(I, "_tmux", lambda *args: None)
    monkeypatch.setattr(I, "type_command", lambda *args, **kwargs: "sent")
    monkeypatch.setattr(I, "_terminal_background", lambda session, run: run() or True)

    def choose_later(seconds):
        I._queue_turn_command(cfg, message, "sample-session", "/model sonnet")

    monkeypatch.setattr(I.time, "sleep", choose_later)
    I.command_when({"id": "sample", "data": "cw:now:/model opus"}, cfg, bot, message, "sample-session")
    path, = entries()
    assert json.loads(path.read_text())["text"] == "/model sonnet"


@pytest.mark.parametrize("reason", ["claimed", "stale"])
def test_failed_uncertainty_notice_never_replays_the_request(setup, monkeypatch, reason):
    cfg, message, bot = setup
    I._queue_turn_command(cfg, message, "sample-session", "/model opus")
    path, = entries()
    if reason == "claimed":
        I.write_private(path, json.dumps(dict(json.loads(path.read_text()), claimed=True)))
    else:
        monkeypatch.setattr(I, "_pane_from_process", lambda session: None)
    bot.fail["sendMessage"] = "sample failure"
    monkeypatch.setattr(I, "cli_command", lambda *args, **kwargs: pytest.fail("replayed uncertain request"))
    I.run_turn_commands(cfg, bot)
    assert json.loads(path.read_text())["claimed"] is True
    monkeypatch.setattr(I, "_pane_from_process", lambda session: "%1")
    I.run_turn_commands(cfg, bot)
    assert entries() == []


def test_a_failed_typing_is_never_retried(setup, monkeypatch):
    """After a failed typing the line may have reached the session: the owner is told, nothing is sent twice."""
    cfg, message, bot = setup
    I._queue_turn_command(cfg, message, "sample-session", "/model opus")
    calls = []
    monkeypatch.setattr(I, "type_command", lambda *args, **kwargs: calls.append(1) or "failed")
    monkeypatch.setattr(I, "_question_or_screen", lambda *args, **kwargs: None)
    I.run_turn_commands(cfg, bot)
    I.run_turn_commands(cfg, bot)
    assert len(calls) == 1 and entries() == []
    assert any(method == "sendMessage" for method, _ in bot.calls)


def test_a_bad_entry_that_raises_does_not_stop_the_rest(setup, monkeypatch):
    cfg, message, bot = setup
    I._queue_turn_command(cfg, message, "sample-session", "/model opus")
    I._queue_turn_command(cfg, message, "other-session", "/model sonnet")
    first, second = sorted(entries())
    real = I._run_one_turn_command
    done = []

    def flaky(cfg_, bot_, path):
        if path == first:
            raise UnicodeEncodeError("utf-8", "x", 0, 1, "sample")
        done.append(path)
        return real(cfg_, bot_, path)
    monkeypatch.setattr(I, "_run_one_turn_command", flaky)
    monkeypatch.setattr(I, "cli_command", lambda *args, **kwargs: "sent")
    I.run_turn_commands(cfg, bot)
    assert done == [second]
