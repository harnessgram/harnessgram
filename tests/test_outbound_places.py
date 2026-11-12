"""Outbound sends follow the session's place: a session in a project group is answered there and only there,
and the home chat hears nothing about it. A session at home is unchanged. Thread ids are only unique inside
one chat, so the tests put a home session and a project session on the very same thread number."""
import ast
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import ctb
from conftest import GOOD_TOKEN, FakeBot
from ctb import config as C
from ctb import dashboard, delivery, dialogs, flood, mirror as M, pins, progress, topic_icons, topics as T
from ctb.config import state_dir
from ctb.topics import TopicMap

OWNER, HOME, ORBIT = 7, -1001234567890, -1001111111111
THREAD = 5  # the same number in both chats, on purpose
PICK = "Pick one\n\n❯ 1. Yes\n  2. No\n\nEnter to select · ↑/↓ to navigate · Esc to cancel"


@pytest.fixture
def rig(bot, monkeypatch):
    cfg = C.from_dict({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "chat_id": HOME, "target_language": "en",
                       "projects": [{"name": "Orbit", "chat_id": ORBIT}]})
    tmap = TopicMap(state_dir() / "topics.json", bot, HOME, cfg=cfg)
    tmap.ensure("home-s", "Home work", thread=THREAD)
    tmap.ensure("orbit-s", "Orbit: game", thread=THREAD, chat=ORBIT)
    monkeypatch.setattr(M, "Bot", SimpleNamespace(from_config=lambda c: bot))
    monkeypatch.setattr(M.time, "sleep", lambda s: None)
    return SimpleNamespace(cfg=cfg, bot=bot, topics=tmap, mirror=M.Mirror(cfg, bot, tmap))


def chats(bot, since=0):
    """Every chat the calls since `since` went to. A call that names a topic must name THREAD (edits and
    deletes of a message name no topic)."""
    sent = bot.calls[since:]
    assert all(p.get("message_thread_id", THREAD) == THREAD for _m, p in sent)
    return {p.get("chat_id") for _m, p in sent}


def write_rows(session, rows):
    path = state_dir() / "progress"
    path.mkdir(parents=True, exist_ok=True)
    with open(path / f"{session}.jsonl", "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def stuck_rig(rig, session):
    """A busy session whose transcript has been quiet for 20 minutes."""
    transcript = state_dir() / f"{session}.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "go"}}) + "\n")
    T.set_state(rig.cfg, session, "busy", bot=rig.bot)
    return str(transcript), time.time() + 1200


def test_a_project_session_is_served_only_in_its_own_group(rig, monkeypatch):
    bot, cfg = rig.bot, rig.cfg
    monkeypatch.setattr(dialogs, "open_screen", lambda s: ("%1", PICK, dialogs.parse(PICK), True))
    transcript, later = stuck_rig(rig, "orbit-s")  # the placeholder
    write_rows("orbit-s", [{"kind": "action", "t": 1.0, "l": "⚙️ step 0"}])
    steps = [
        lambda: rig.mirror.send_reply("Orbit: game", "orbit-s", "Did the work.\n\nSummary: all good"),  # reply, pin index
        lambda: M.refresh_card(cfg, bot, "orbit-s", create=True),  # status card
        lambda: progress.run_send_progress(cfg, "orbit-s", bot=bot, throttle=0),  # progress card
        lambda: M.run_typing(cfg, "orbit-s"),
        lambda: dashboard.stuck_check(cfg, rig.topics.all_places(), bot, now=later, find=lambda s: transcript),
        lambda: M.run_notify(cfg, "orbit-s", "permission_prompt", bot=bot),  # dialog card (marks it waiting)
    ]
    start = len(bot.calls)
    for step in steps:
        step()
    assert chats(bot, start) == {ORBIT}
    assert len(bot.calls) - start >= 8  # every kind of send really happened
    texts = [p.get("text", "") for p in bot.methods("sendMessage")]
    assert any(T.PLACEHOLDER == x for x in texts) and any("t.me/c/1111111111/5/" in x for x in texts)
    assert any("Pick one" in x for x in texts) and any(p.get("action") == "typing" for p in bot.methods("sendChatAction"))
    assert any("quiet" in x.lower() or "stuck" in x.lower() or "minutes" in x.lower() for x in texts)
    # the state kept per topic is keyed by chat and thread, so thread 5 of the home group stays free
    assert set(json.loads((state_dir() / "status-footer.json").read_text())) == {f"{ORBIT}:{THREAD}"}
    assert set(json.loads((state_dir() / "status-card-pace.json").read_text())) == {f"{ORBIT}:{THREAD}"}
    assert dialogs.rec_get("orbit-s")["chat"] == ORBIT
    assert HOME not in chats(bot)


def test_the_home_session_on_the_same_thread_number_still_goes_home(rig):
    bot, cfg = rig.bot, rig.cfg
    rig.mirror.send_reply("Home work", "home-s", "Done.\n\nSummary: ok")
    M.refresh_card(cfg, bot, "home-s", create=True)
    T.set_state(cfg, "home-s", "busy", bot=bot)
    M.run_typing(cfg, "home-s")
    assert chats(bot) == {HOME}
    assert set(json.loads((state_dir() / "status-footer.json").read_text())) == {str(THREAD)}  # the old key, no migration


def test_two_groups_at_once_do_not_mix(rig):
    bot, cfg = rig.bot, rig.cfg
    for session, title in (("home-s", "Home work"), ("orbit-s", "Orbit: game")):
        rig.mirror.send_reply(title, session, f"Reply of {session}.\n\nSummary: {session} done")
        M.refresh_card(cfg, bot, session, create=True)
    by_chat = {}
    for _m, p in bot.calls:
        by_chat.setdefault(p.get("chat_id"), []).append(json.dumps(p))
    assert set(by_chat) == {HOME, ORBIT}
    assert "orbit-s" not in "".join(by_chat[HOME]) and "home-s" not in "".join(by_chat[ORBIT])
    # one card each: the status card of one session is never edited in place of the other's
    assert not bot.methods("editMessageText") or all("orbit-s" not in p["text"] for p in bot.methods("editMessageText")
                                                      if p["chat_id"] == HOME)


def test_a_flooded_home_chat_does_not_hold_back_the_project_group(rig):
    bot, cfg = rig.bot, rig.cfg
    T.set_state(cfg, "orbit-s", "busy", bot=bot)
    T.set_state(cfg, "home-s", "busy", bot=bot)
    flood.record_429(HOME, 120)
    bot.calls.clear()
    assert M.refresh_card(cfg, bot, "orbit-s", create=True)  # optional traffic of the project group goes out
    T.set_state(cfg, "orbit-s", "bottom", bot=bot, optional=True)
    assert chats(bot) == {ORBIT} and bot.methods("sendMessage")
    bot.calls.clear()
    assert not M.refresh_card(cfg, bot, "home-s", create=True)  # while the flooded home chat is skipped quietly
    T.set_state(cfg, "home-s", "bottom", bot=bot, optional=True)
    assert bot.calls == []


def test_the_heartbeat_serves_the_group_that_is_not_flooded(rig, monkeypatch):
    from ctb import heartbeat
    bot, cfg = rig.bot, rig.cfg
    T.set_state(cfg, "orbit-s", "busy", bot=bot)
    T.set_state(cfg, "home-s", "busy", bot=bot)
    flood.record_429(HOME, 120)
    monkeypatch.setattr(heartbeat, "working", lambda s, now=None, find=None: True)
    monkeypatch.setattr(heartbeat, "status_line", lambda s: "")
    monkeypatch.setattr("ctb.accounts.live_session_records", lambda: [])
    bot.calls.clear()
    for _ in range(2):  # the turns alternate; only the open group is ever served
        heartbeat.run_once(cfg, bot=bot, now=time.time())
    assert chats(bot) == {ORBIT}


def test_a_gone_project_topic_forgets_only_its_own_session(rig):
    bot, cfg = rig.bot, rig.cfg
    bot.fail["sendMessage"] = "Bad Request: message thread not found"  # the project's topic was deleted
    assert not M.refresh_card(cfg, bot, "orbit-s", create=True)
    assert rig.topics.place_of("home-s") is not None  # the home session shares the number and is untouched
    assert rig.topics.place_of("orbit-s") is None  # forgotten, by the error of its OWN chat


def test_agents_topic_lives_in_the_session_group(rig):
    from ctb import agents_topic
    bot, cfg = rig.bot, rig.cfg
    st = {}
    thread = agents_topic._open_topic(cfg, bot, "orbit-s", st, 1000.0)
    assert bot.methods("createForumTopic")[0]["chat_id"] == ORBIT and st["chat"] == ORBIT
    agents_topic._post(cfg, bot, "orbit-s", st, "agent says hi", THREAD, True, 1000.0)
    assert {p["chat_id"] for p in bot.methods("sendMessage")} == {ORBIT}
    assert thread == st["thread"]
    agents_topic._save(agents_topic._path("orbit-s"), st)
    assert agents_topic.input_ignored(thread) is None  # the same number at home is not this topic
    assert agents_topic.input_ignored(thread, ORBIT) is True  # first message written in the project's agents topic


def test_receipts_and_delivered_ids_of_two_chats_do_not_collide(rig):
    bot, cfg = rig.bot, rig.cfg
    delivery.send_ack(bot, cfg, THREAD, 77, False, chat=ORBIT)
    assert bot.methods("sendMessage")[-1]["chat_id"] == ORBIT
    delivery.record_delivered([77])  # message 77 of the HOME chat was delivered
    assert delivery.is_delivered(77) and not delivery.is_delivered(77, ORBIT)
    delivery.drop_acks(bot, cfg, [77], ORBIT)
    assert bot.methods("deleteMessage")[-1]["chat_id"] == ORBIT
    assert delivery.is_delivered(77, ORBIT)


def test_topic_icons_are_picked_per_chat_and_thread(rig):
    bot = rig.bot
    bot.call = _with_stickers(bot.call)
    topic_icons.run(rig.cfg, bot=bot)
    edits = bot.methods("editForumTopic")
    assert {(p["chat_id"], p["message_thread_id"]) for p in edits if "icon_custom_emoji_id" in p} == {
        (HOME, THREAD), (ORBIT, THREAD)}
    picks = json.loads((state_dir() / "topic-icons.json").read_text())
    assert set(picks) == {str(THREAD), f"{ORBIT}:{THREAD}"}


def _with_stickers(call):
    def wrapped(method, params=None, timeout=15):
        if method == "getForumTopicIconStickers":
            return [{"emoji": "💡", "custom_emoji_id": "id1"}]
        return call(method, params, timeout)
    return wrapped


def test_pin_index_is_kept_per_group(rig):
    bot, cfg = rig.bot, rig.cfg
    rig.mirror.send_reply("Orbit: game", "orbit-s", "One.\n\nSummary: one")
    rig.mirror.send_reply("Home work", "home-s", "Two.\n\nSummary: two")
    state = json.loads((state_dir() / pins.STATE_FILE).read_text(encoding="utf-8"))
    assert set(state) == {f"{ORBIT}:{THREAD}", f"{HOME}:{THREAD}"}
    pinned = {p["chat_id"] for p in bot.methods("pinChatMessage")}
    assert pinned == {ORBIT, HOME}


def test_an_account_move_is_answered_in_the_session_group(rig, monkeypatch):
    from ctb import inbox as I
    bot, cfg = rig.bot, rig.cfg
    monkeypatch.setattr(I.time, "sleep", lambda s: None)

    def fake_move(cfg_, bot_, session, target, say, **kw):
        say("moving now")
        return False
    monkeypatch.setattr(I, "account_move", fake_move)
    for session, chat in (("orbit-s", ORBIT), ("home-s", HOME)):
        start = len(bot.calls)
        assert I.auto_move(cfg, bot, session, THREAD, "small") is False  # the thread argument is the same number
        assert chats(bot, start) == {chat}


def test_the_keyboard_comes_back_in_the_session_group(rig, monkeypatch):
    from ctb import inbox as I
    bot, cfg = rig.bot, rig.cfg
    # The restore runs on a timer thread; here it runs at once, in this test. Waiting for a real thread
    # failed about one run in four: a thread another test left running could beat it to the shared
    # timer table or the flood gate. Which chat the keyboard comes back in is what this test checks.
    fired = []
    monkeypatch.setattr(I.threading, "Timer", lambda delay, fn: SimpleNamespace(
        start=lambda: fired.append(fn) or fn(), cancel=lambda: None, daemon=False))
    I._restore_timers.pop(f"{ORBIT}:{THREAD}", None)
    I.keyboard_back(THREAD, delay=0, cfg=cfg, bot=bot, chat=ORBIT)
    assert fired and chats(bot) == {ORBIT}


def test_a_project_topic_is_not_looked_up_among_the_home_topics(rig, monkeypatch):
    from ctb import usage
    seen = []
    monkeypatch.setattr(usage, "quota_line", lambda lang, session=None: seen.append(session) or "")
    M.live_keyboard("", thread=THREAD)  # a home topic: its session is found
    M.live_keyboard("", thread=THREAD, chat=ORBIT)  # the same number in a project group is another topic
    assert seen == ["home-s", None]


def test_idle_rebalancing_sees_the_sessions_of_project_groups(rig, monkeypatch):
    import os
    import subprocess
    from ctb import accounts
    from test_port_1101_accounts import setup_account, write_usage
    now = 1_000_000.0
    setup_account("main", "main@example.com", "default_claude_max_20x", is_main=True)
    setup_account("pro1", "pro1@example.com", "pro")
    write_usage("main", 20, 20, five_resets=now + 3600, weekly_resets=now + 86400)
    write_usage("pro1", 30, 30, five_resets=now + 3600, weekly_resets=now + 86400)
    accounts.record("orbit-s", "main")
    sessions_dir = accounts.claude_home() / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "999.json").write_text(json.dumps({"sessionId": "orbit-s", "pid": 999, "status": "idle",
                                                       "statusUpdatedAt": (now - 2000) * 1000, "tmux": "%1"}))
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    real_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **kw: type("P", (), {"stdout": "1000 sleep\n", "returncode": 0})()
                        if cmd[0] == "pgrep" else real_run(cmd, *a, **kw))
    spawned = []
    assert accounts.rebalance(rig.cfg, now=now, spawn_fn=spawned.append) == ("orbit-s", "pro1")
    assert spawned[0][:3] == ["account", "auto-move", "--session=orbit-s"]


def test_the_listener_map_reminds_about_dialogs_of_project_sessions(rig, monkeypatch):
    bot, cfg = rig.bot, rig.cfg
    monkeypatch.setattr(dialogs, "open_screen", lambda s: ("%1", PICK, dialogs.parse(PICK), True))
    T.set_state(cfg, "orbit-s", "busy", bot=bot)
    listener_map = TopicMap(state_dir() / "topics.json", bot, C.home_chat(cfg), cfg=cfg)  # as serve() builds it
    start, now = len(bot.calls), time.time()
    dialogs.remind(cfg, bot, listener_map, now=now)  # first seen
    assert dialogs.remind(cfg, bot, listener_map, now=now + dialogs.SEEN_GRACE + 1) == 1
    assert chats(bot, start) == {ORBIT}


def test_every_map_built_with_the_home_chat_knows_the_project_groups():
    """A TopicMap(path, bot, home_chat(cfg)) without cfg= sees the home topics alone."""
    offenders = []
    for path in sorted(Path(ctb.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and _name_of(node) == "TopicMap" and len(node.args) >= 3
                    and isinstance(node.args[2], ast.Call) and _name_of(node.args[2]) == "home_chat"
                    and not any(k.arg == "cfg" for k in node.keywords)):
                offenders.append((path.name, node.lineno))
    assert offenders == []


@pytest.mark.real_type_command
def test_auto_compact_runs_in_the_session_group(rig, monkeypatch):
    from ctb import inbox as I
    bot, cfg = rig.bot, rig.cfg
    from conftest import BoxedTmux
    ran = []
    tmux = BoxedTmux()
    monkeypatch.setattr(I, "_tmux", tmux)
    monkeypatch.setattr(I.time, "sleep", lambda s: None)
    monkeypatch.setattr(I, "pane_for_session", lambda s: "%1")
    monkeypatch.setattr(I, "pane_text", lambda pane, n=0: "idle")
    monkeypatch.setattr(I, "session_busy", lambda s: False)
    monkeypatch.setattr(I, "_start_compact_progress", lambda cfg_, bot_, thread, session, pane, chat=None: ran.append((thread, chat)))
    assert I.auto_compact("orbit-s", 500_000, cold=True, cfg=cfg, bot=bot, topics=rig.topics)
    assert chats(bot) == {ORBIT} and tmux.sent == ["/compact"]
    assert ran == [(THREAD, ORBIT)]  # the progress follower runs in the session's own group
    assert I._reply_chat(cfg, {"message_thread_id": THREAD}) == HOME  # a message with no chat: home
    assert I._reply_chat(cfg, {"chat": {"id": 12345}}) == HOME  # a chat the owner is not obeyed in: home


def test_delivered_receipts_are_dropped_in_the_chat_they_came_from(rig, monkeypatch):
    from ctb import inbox as I
    dropped = []
    monkeypatch.setattr(delivery, "drop_acks", lambda b, c, mids, chat=None: dropped.append((chat, list(mids))))
    assert I.run_delivered(rig.cfg, [f"{HOME}:1", f"{ORBIT}:2", f"{HOME}:3"], bot=rig.bot) == 0
    assert dropped == [(None, [1, 3]), (ORBIT, [2])]


def test_a_moved_session_forgets_its_agents_topic_of_the_old_group(rig):
    from ctb import agents_topic
    bot, cfg = rig.bot, rig.cfg
    st = {"thread": 40, "chat": ORBIT, "open": True, "posted": True}
    assert agents_topic._open_topic(cfg, bot, "home-s", st, 1000.0) is not None  # the session now lives at home
    assert bot.methods("createForumTopic")[0]["chat_id"] == HOME and not bot.methods("reopenForumTopic")
    assert st["thread"] != 40 and "chat" not in st
    st = {"thread": 40, "open": True}  # an old home state, the session now in the project group
    agents_topic._open_topic(cfg, bot, "orbit-s", st, 1000.0)
    assert bot.methods("createForumTopic")[-1]["chat_id"] == ORBIT and st["chat"] == ORBIT


def test_the_agents_topic_is_closed_where_it_was_saved(rig):
    from ctb import agents_topic
    bot, cfg = rig.bot, rig.cfg
    st = {"thread": 40, "chat": ORBIT, "open": True}
    agents_topic._close(cfg, bot, st, None, chat=HOME)
    assert bot.methods("closeForumTopic")[-1]["chat_id"] == ORBIT
    agents_topic._save(agents_topic._path("home-s"), {"thread": 41, "chat": ORBIT, "open": True})
    agents_topic.session_ended(cfg, "home-s", bot)  # the session itself is at home now
    assert bot.methods("closeForumTopic")[-1] == {"chat_id": ORBIT, "message_thread_id": 41}


# --- the lint: a session-scoped send never mixes a topic with the home chat ----------------------------------

# Where a function may call home_chat() and also name a topic (a dict key, a subscript store, a keyword, .update): only
# the inbound handlers that are home-only on purpose or that compare the message's chat with the home chat. Every
# entry says why.
ALLOWED = {
    ("radio_room.py", "ensure_topic"): "the brainstorm room's own topic lives in the home group, where the radio runs",
    ("inbox.py", "general_start"): "a General topic of the home group or a project group; the chat is the message's own",
    ("inbox.py", "_deliver_held"): "the General offer is home-only (a project group's General opens its picker)",
    ("alerts.py", "show"): "/alerts: the alerts settings live in the home group; a project group's /alerts is ignored",
    ("members.py", "show"): "/members: the team requests live in the home group; a project group's /members is ignored",
    ("sessions_menu.py", "button"): "sessions menu press, checked to be in the home chat (a project group has no menu)",
}


def _names_a_topic(node) -> bool:
    """A dict key, a subscript store, or a keyword argument (dict(...), .update(...), a call) spelling
    message_thread_id."""
    if isinstance(node, ast.Dict):
        return any(isinstance(k, ast.Constant) and k.value == "message_thread_id" for k in node.keys)
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
        return isinstance(node.slice, ast.Constant) and node.slice.value == "message_thread_id"
    if isinstance(node, ast.keyword):
        return node.arg == "message_thread_id"
    return False


def _is_default_chat(parent, node=None) -> bool:
    """`home_chat(cfg) if chat is None else chat`: the home chat only as the default of a chat= argument.
    Also `place.chat if place else home_chat(cfg)`: the session's own chat, home only for a session with none."""
    if isinstance(parent, ast.IfExp) and parent.orelse is node:
        body = parent.body
        if isinstance(body, ast.Attribute) and body.attr == "chat":
            return True
        if isinstance(body, ast.Subscript) and isinstance(body.slice, ast.Constant) and body.slice.value == "chat":
            return True
    return (isinstance(parent, ast.IfExp) and isinstance(parent.test, ast.Compare)
            and len(parent.test.ops) == 1 and isinstance(parent.test.ops[0], ast.Is)
            and isinstance(parent.test.comparators[0], ast.Constant) and parent.test.comparators[0].value is None)


def _name_of(call) -> str:
    return getattr(call.func, "id", getattr(call.func, "attr", ""))


def _calls_home_chat(node, parent=None, in_map=False) -> bool:
    """A home_chat() that could be a send's chat_id. Not counted: the default of a chat= argument, a
    comparison, and the argument of a TopicMap(...) (the map only reads, the send uses the place)."""
    return (isinstance(node, ast.Call) and _name_of(node) == "home_chat" and not in_map
            and not _is_default_chat(parent, node) and not isinstance(parent, ast.Compare))


def _offenders():
    """Every function that calls home_chat() and also names a topic in any form: the pair sends a
    session's message to the wrong chat once the session lives in a project group."""
    root = Path(ctb.__file__).parent
    found = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scopes = {}  # innermost function -> [home_chat calls, topic mentions, first line]

        def visit(node, scope, parent=None, in_map=False):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = scopes.setdefault(id(node), [node.name, False, False, node.lineno])
            elif scope is None:
                scope = scopes.setdefault("<module>", ["<module>", False, False, 1])
            if _calls_home_chat(node, parent, in_map):
                scope[1] = True
            if _names_a_topic(node):
                scope[2] = True
            for child in ast.iter_child_nodes(node):
                visit(child, scope, node, in_map or (isinstance(node, ast.Call) and _name_of(node) == "TopicMap"))

        visit(tree, None)
        for name, home, topic, line in scopes.values():
            if home and topic:
                found.append((path.name, name, line))
    return found


def test_no_session_send_names_a_topic_together_with_the_home_chat():
    offenders = [(f, fn, line) for f, fn, line in _offenders() if (f, fn) not in ALLOWED]
    assert offenders == [], ("a dict with message_thread_id and home_chat(...) sends a session's message to the "
                             "wrong chat once the session lives in a project group; use the session's place "
                             f"(topics.place) or add a reasoned entry to ALLOWED: {offenders}")


def test_every_allowlist_entry_still_matches_and_has_a_reason():
    live = {(f, fn) for f, fn, _l in _offenders()}
    for key, reason in ALLOWED.items():
        assert reason.strip(), key
        assert key in live, f"{key} no longer sends a topic with home_chat(); drop it from ALLOWED"
