"""Forks are separate runs, never input or control applied to the source pane."""
import json
import shlex
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN
from ctb import accounts, inbox, menus, panel, session_fork as fork, usershell
from ctb.config import Config, ProjectConfig, state_dir
from ctb.i18n import t
from ctb.topics import TopicMap, session_override_name
from test_panel import signed

SOURCE = "abcdefab-0000-4000-8000-000000000001"
OTHER = "bbbbbbbb-0000-4000-8000-000000000001"


@pytest.fixture
def env(tmp_path, monkeypatch, bot):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=7, chat_id=-1001, ui_language="en",
                 projects=[ProjectConfig(name="Sample", chat_id=-1002)])
    topics = TopicMap(state_dir() / "topics.json", bot, cfg.home_chat, cfg=cfg)
    topics.ensure(SOURCE, "Sample conversation")
    calls = []
    monkeypatch.setattr(inbox, "session_folder", lambda sid: str(tmp_path))
    monkeypatch.setattr(fork.shutil, "which", lambda name: "/bin/" + name)
    def tmux(*args):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="")
    monkeypatch.setattr(inbox, "_tmux", tmux)
    monkeypatch.setattr(inbox, "pass_trust", lambda target, folder: calls.append(("trust", target, folder)) or True)
    monkeypatch.setattr(inbox, "_resume_dialog_pending", lambda target: calls.append(("resume", target)) or False)
    monkeypatch.setattr(inbox, "_pane_command", lambda target: calls.append(("command", target)) or "claude")
    monkeypatch.setattr(inbox, "_terminal_background", lambda sid, action: action() or True)
    bot.calls.clear()
    return cfg, bot, topics, calls


def tap(env, data, sender=7, chat=-1001, thread=None, message=50):
    cfg, bot, topics, calls = env
    thread = topics.place_of(SOURCE).thread if thread is None else thread
    return {"id": "sample", "data": data, "from": {"id": sender},
            "message": {"message_id": message, "message_thread_id": thread,
                        "chat": {"id": chat, "type": "supergroup"}}}


def press(env, q):
    cfg, bot, topics, _ = env
    return inbox.handle_button(q, cfg, bot, topics, None)


def confirmed(env, scope="panel", now=100):
    cfg, bot, topics, _ = env
    token = fork.arm(cfg, topics, SOURCE, scope, now=now)
    return fork.start(cfg, bot, topics, SOURCE, scope, token, now=now)


def test_new_argv_account_topic_and_link_leave_source_untouched(env, monkeypatch):
    cfg, bot, topics, calls = env
    accounts.record(SOURCE, accounts.MAIN)
    monkeypatch.setattr(accounts, "current", lambda: "other")
    old = topics.all_places()[SOURCE].copy()
    sid = confirmed(env)
    launch = calls[0]
    assert launch[:5] == ("new-session", "-d", "-s", "tg-" + sid[:8], "-c")
    argv = shlex.split(launch[-1])
    at = argv.index("--resume")
    assert argv[at:at + 3] == ["--resume", SOURCE, "--fork-session"]
    assert argv[argv.index("--session-id") + 1] == sid != SOURCE
    assert "--remote-control" in argv
    assert "--dangerously-skip-permissions" not in argv
    assert accounts.account_for(sid) == accounts.MAIN
    assert topics.all_places()[SOURCE] == old
    assert topics.place_of(sid).thread != old["thread"]
    assert topics.all_places()[sid]["name"] == "Sample conversation · fork"
    assert session_override_name(sid) == "Sample conversation · fork"
    assert calls[1:] == [("trust", "tg-" + sid[:8], launch[5]),
                         ("resume", "tg-" + sid[:8]), ("command", "tg-" + sid[:8])]
    notices = bot.methods("sendMessage")
    assert len(notices) == 1
    assert notices[0]["message_thread_id"] == old["thread"]
    assert f"https://t.me/c/1/{topics.place_of(sid).thread}" in notices[0]["text"]


def test_project_fork_stays_in_project_group(env):
    cfg, bot, topics, _ = env
    topics.forget(SOURCE, chat=cfg.home_chat)
    topics.ensure(SOURCE, "Project conversation", chat=-1002)
    sid = confirmed(env)
    assert topics.place_of(sid).chat == -1002
    assert bot.methods("sendMessage")[-1]["chat_id"] == -1002
    assert "https://t.me/c/2/" in bot.methods("sendMessage")[-1]["text"]


@pytest.mark.parametrize("lang,label", [("en", "fork"), ("he", "פיצול")])
def test_title_preserves_localized_suffix_for_long_names(lang, label):
    assert fork.title("Short", lang) == "Short · " + label
    name = fork.title("א" * 140, lang)
    assert len(name) == 120 and name.endswith(" · " + label)


def test_topic_requires_confirmation_and_cancel_is_harmless(env):
    cfg, bot, topics, calls = env
    assert press(env, tap(env, "fork:go:unknown")) == "fork"
    assert not calls and len(topics.all_places()) == 1
    assert press(env, tap(env, "fork:ask")) == "fork-confirm"
    text = bot.methods("editMessageText")[-1]
    assert text["text"] == t("fork_confirm", lang="en")
    rows = text["reply_markup"]["inline_keyboard"]
    assert rows[-1] == menus.close_row("en")
    assert not calls
    assert press(env, tap(env, menus.CLOSE)) == "dismissed"
    assert not calls


def test_topic_confirm_starts_once_and_is_bound_to_message(env):
    cfg, bot, topics, calls = env
    press(env, tap(env, "fork:ask"))
    callback = bot.methods("editMessageText")[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    press(env, tap(env, callback, message=51))
    assert not calls
    press(env, tap(env, callback))
    assert len([c for c in calls if c[0] == "new-session"]) == 1
    press(env, tap(env, callback))
    assert len([c for c in calls if c[0] == "new-session"]) == 1


@pytest.mark.parametrize("data", ["fork:ask", "fork:go:0123456789abcdef"])
@pytest.mark.parametrize("change", [{"sender": 8}, {"chat": -1003}, {"thread": 999}])
def test_topic_owner_chat_and_session_guards(env, data, change):
    _, bot, topics, calls = env
    assert press(env, tap(env, data, **change)) == "ignored"
    assert not calls and len(topics.all_places()) == 1
    assert not bot.methods("editMessageText")


@pytest.mark.parametrize("scope,session,now", [("another-surface", SOURCE, 100),
                                                ("panel", OTHER, 100), ("panel", SOURCE, 220)])
def test_confirm_is_scoped_to_source_surface_and_deadline(env, scope, session, now):
    cfg, bot, topics, calls = env
    topics.ensure(OTHER, "Other")
    token = fork.arm(cfg, topics, SOURCE, "panel", now=100)
    with pytest.raises(fork.ForkError, match="fork_confirm_again"):
        fork.start(cfg, bot, topics, session, scope, token, now=now)
    assert not calls


@pytest.mark.parametrize("missing,key", [("folder", "fork_no_folder"), ("deleted-folder", "fork_no_folder"),
                                         ("tmux", "fork_no_tmux"), ("claude", "fork_no_claude")])
def test_missing_prerequisite_refuses_without_touching_terminal(env, monkeypatch, missing, key):
    cfg, bot, topics, calls = env
    if missing in ("folder", "deleted-folder"):
        monkeypatch.setattr(inbox, "session_folder", lambda sid: None if missing == "folder" else "/nonexistent/sample-folder")
    elif missing == "tmux":
        monkeypatch.setattr(fork.shutil, "which", lambda name: None)
    else:
        monkeypatch.setattr(usershell, "probe", lambda *a, **k: False)
    with pytest.raises(fork.ForkError, match=key):
        confirmed(env)
    assert not calls and len(topics.all_places()) == 1
    assert not json.loads((state_dir() / "session-forks.json").read_text())["started"]


def test_rate_limit_shared_between_panel_and_topic_and_expires(env):
    cfg, bot, topics, calls = env
    confirmed(env)
    with pytest.raises(fork.ForkError, match="fork_rate_limited"):
        confirmed(env, scope="topic:-1001:101:50", now=129.999)
    assert len([c for c in calls if c[0] == "new-session"]) == 1
    confirmed(env, now=130)
    assert len([c for c in calls if c[0] == "new-session"]) == 2


@pytest.mark.parametrize("failure,key", [("launch", "fork_failed"), ("trust", "fork_trust_needed"),
                                        ("resume", "fork_dialog_needed"), ("exited", "fork_failed"),
                                        ("topic", "fork_topic_failed")])
def test_background_failure_reports_once_and_only_cleans_new_pane(env, monkeypatch, failure, key):
    cfg, bot, topics, calls = env
    if failure == "launch":
        def failed(*args):
            calls.append(args)
            return SimpleNamespace(returncode=1)
        monkeypatch.setattr(inbox, "_tmux", failed)
    if failure == "trust":
        monkeypatch.setattr(inbox, "pass_trust", lambda *a: False)
    if failure == "resume":
        monkeypatch.setattr(inbox, "_resume_dialog_pending", lambda *a: True)
    if failure == "exited":
        monkeypatch.setattr(inbox, "_pane_command", lambda *a: None)
    if failure == "topic":
        monkeypatch.setattr(topics, "ensure", lambda *a, **k: None)
    sid = confirmed(env)
    assert topics.place_of(SOURCE) is not None
    assert len(bot.methods("sendMessage")) == 1
    assert bot.methods("sendMessage")[0]["text"] == t(key, lang="en")
    assert not any(c[0] == "send-keys" for c in calls)
    assert all(c[2] == "tg-" + sid[:8] for c in calls if c[0] == "kill-session")


def request(env, req, auth=None):
    cfg, bot, topics, calls = env
    obj = object.__new__(panel.PanelHandler)
    obj.server = SimpleNamespace(cfg=cfg, bot=lambda: bot, bot_token=GOOD_TOKEN, owner_id=7)
    obj.path = "/api/fork"
    obj.headers = {panel.INIT_HEADER: auth if auth is not None else signed()}
    obj._off = lambda: False
    obj._body_ok = lambda size: True
    obj._request = lambda: req
    replies = []
    obj._json = lambda status, data: replies.append((status, data))
    obj.do_POST()
    return replies[-1]


def test_panel_arm_then_start_requires_the_confirmation(env):
    _, _, _, calls = env
    body = {"action": "start", "session": SOURCE[:8], "armed": "0" * 16}
    assert request(env, body)[0] == 409
    assert not calls
    code, data = request(env, {"action": "arm", "session": SOURCE[:8]})
    assert code == 200 and not calls
    body["armed"] = data["armed"]
    assert request(env, body)[0] == 202
    assert request(env, body)[0] == 409
    assert len([c for c in calls if c[0] == "new-session"]) == 1


@pytest.mark.parametrize("auth", ["", signed(user_id=8), signed(tamper=True), signed(auth_date=1)])
def test_panel_fork_is_owner_only_with_fresh_signed_auth(env, auth):
    _, _, _, calls = env
    assert request(env, {"action": "arm", "session": SOURCE[:8]}, auth=auth)[0] == 403
    assert not calls


@pytest.mark.parametrize("body", [{}, {"action": "start", "session": SOURCE[:8]},
                                   {"action": "arm", "session": SOURCE},
                                   {"action": "arm", "session": SOURCE[:8], "extra": True}])
def test_panel_rejects_invalid_payload(env, body):
    assert request(env, body)[0] == 400


def test_panel_rejects_ambiguous_prefix(env):
    _, _, topics, calls = env
    topics.ensure(SOURCE[:-1] + "2", "Another")
    assert request(env, {"action": "arm", "session": SOURCE[:8]})[0] == 404
    assert not calls


def test_more_menu_only_offers_fork_in_a_session():
    buttons = lambda sid: [b["callback_data"] for row in inbox.more_markup(session=sid)["inline_keyboard"] for b in row]
    assert "fork:ask" in buttons(SOURCE)
    assert "fork:ask" not in buttons(None)


def test_topic_missing_folder_is_one_readable_line(env, monkeypatch):
    cfg, bot, _, calls = env
    monkeypatch.setattr(inbox, "session_folder", lambda sid: None)
    press(env, tap(env, "fork:ask"))
    callback = bot.methods("editMessageText")[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.calls.clear()
    press(env, tap(env, callback))
    lines = bot.methods("editMessageText")
    assert len(lines) == 1 and lines[0]["text"] == t("fork_no_folder", lang=cfg.ui_language)
    assert "\n" not in lines[0]["text"] and not calls


def test_busy_scheduler_refuses_and_does_not_reserve_a_fork(env, monkeypatch):
    _, _, _, calls = env
    monkeypatch.setattr(inbox, "_terminal_background", lambda sid, action: False)
    with pytest.raises(fork.ForkError, match="fork_busy"):
        confirmed(env)
    assert not calls
    assert not json.loads((state_dir() / "session-forks.json").read_text())["started"]


def test_other_harness_and_unknown_session_cannot_be_armed(env):
    cfg, _, topics, calls = env
    for sid in ("codex_default_" + SOURCE, "claude:sample:" + SOURCE, OTHER):
        with pytest.raises(fork.ForkError, match="fork_unavailable"):
            fork.arm(cfg, topics, sid, "panel")
    assert not calls


def test_simultaneous_confirmations_only_start_one_fork(env, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    cfg, bot, topics, calls = env
    tokens = [fork.arm(cfg, topics, SOURCE, "panel", now=100) for _ in range(2)]
    barrier = threading.Barrier(2)
    def argv(line):
        barrier.wait(timeout=5)
        return ["/bin/sh", "-lc", line]
    monkeypatch.setattr(inbox, "shell_argv", argv)
    # Keep terminal work pending: the second confirmation must refuse even before
    # the first child's process exists. Only scheduling is observed here.
    pending = []
    monkeypatch.setattr(inbox, "_terminal_background", lambda sid, action: pending.append(sid) or True)
    def start(token):
        try:
            return fork.start(cfg, bot, topics, SOURCE, "panel", token, now=100)
        except fork.ForkError as exc:
            return exc.key
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, tokens))
    assert len(pending) == 1 and results.count("fork_rate_limited") == 1
    assert not calls


def test_topic_reserved_before_session_start_hook_can_route_it(env, monkeypatch):
    cfg, bot, topics, calls = env
    def launch(*args):
        argv = shlex.split(args[-1])
        sid = argv[argv.index("--session-id") + 1]
        assert topics.place_of(sid).chat == topics.place_of(SOURCE).chat
        assert topics.all_places()[sid]["name"] == "Sample conversation · fork"
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(inbox, "_tmux", launch)
    confirmed(env)


def test_failed_start_removes_only_new_topic(env, monkeypatch):
    cfg, bot, topics, calls = env
    old = topics.place_of(SOURCE)
    monkeypatch.setattr(inbox, "_tmux", lambda *a: SimpleNamespace(returncode=1))
    sid = confirmed(env)
    assert topics.place_of(SOURCE) == old
    assert topics.place_of(sid) is None
    deletes = bot.methods("deleteForumTopic")
    assert len(deletes) == 1 and deletes[0]["message_thread_id"] != old.thread
