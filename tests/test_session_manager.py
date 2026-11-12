"""The session manager: list, state icons, paging, end, close, reopen, delete and tidy."""
import json
import os
import time

import pytest

from conftest import FakeBot, BoxedTmux
from ctb import flood, inbox, sessions_menu as sm
from ctb.config import Config, state_dir, write_private
from ctb.identity import FSI, PDI
from ctb.telegram import TelegramError
from ctb.topics import TopicMap

CHAT = -1001234567890
DAY = 86400


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / "claude" / "projects" / "p").mkdir(parents=True)
    (tmp_path / "claude" / "sessions").mkdir(parents=True)
    monkeypatch.setattr(sm, "_pause", lambda s: None)
    monkeypatch.setattr(inbox, "_terminal_background", lambda key, action: action() or True)
    return tmp_path


def cfg(lang="en"):
    return Config(bot_token="tok", owner_chat_id=1, chat_id=CHAT, ui_language=lang)


def add(env, sid, thread, name, age, live=False):
    """A session topic whose last activity was `age` seconds ago; `live` gives it a running process."""
    topics_file = state_dir() / "topics.json"
    data = json.loads(topics_file.read_text()) if topics_file.exists() else {}
    data[sid] = {"thread": thread, "name": name, "visible_name": name}
    write_private(topics_file, json.dumps(data))
    path = env / "claude" / "projects" / "p" / f"{sid}.jsonl"
    path.write_text("{}\n")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    if live:
        (env / "claude" / "sessions" / f"{thread}.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": sid}))


def tmap(bot):
    return TopicMap(state_dir() / "topics.json", bot, CHAT)


def press(bot, data, lang="en", sender=1, chat=CHAT, message_id=50):
    q = {"id": "q", "from": {"id": sender}, "data": data, "message": {"chat": {"id": chat}, "message_id": message_id}}
    return inbox.handle_button(q, cfg(lang), bot, tmap(bot), inbox.Inbox(state_dir() / "inbox")), q


def last_edit(bot):
    return bot.methods("editMessageText")[-1]


def buttons(params):
    return [b for row in params["reply_markup"]["inline_keyboard"] for b in row]


def texts(params):
    return [b["text"] for b in buttons(params)]


def cb(params, fragment):
    return next(b["callback_data"] for b in buttons(params) if fragment in b.get("callback_data", ""))


def toasts(bot):
    return [p.get("text") for p in bot.methods("answerCallbackQuery")]


def test_list_is_newest_first_with_state_icons_and_ages(env):
    bot = FakeBot()
    add(env, "idle-sess-1", 11, "Idle one", 5 * 3600, live=True)
    add(env, "busy-sess-1", 12, "Busy one", 10, live=True)
    add(env, "dead-sess-1", 13, "Dead one", 3 * DAY)
    add(env, "mid-sess-01", 14, "Mid one", 40 * 60)
    result, _ = press(bot, "sm:l:0o")
    assert result == "sessions"
    edit = last_edit(bot)
    rows = [t for t in texts(edit) if " · " in t]
    assert rows == ["🟢 Busy one · 1m", "⚫ Mid one · 40m", "💤 Idle one · 5h", "⚫ Dead one · 3d"]
    assert "🟢 1 working · 💤 1 idle · ⚫ 2 ended" in edit["text"]


def test_list_pages_eight_at_a_time(env):
    bot = FakeBot()
    for i in range(19):
        add(env, f"sess-{i:02d}-xxxx", 100 + i, f"Topic {i:02d}", (i + 1) * 3600)
    press(bot, "sm:l:0o")
    page0 = last_edit(bot)
    assert len([t for t in texts(page0) if " · " in t]) == 8
    assert "Page 1 of 3" in page0["text"]
    assert "◀" not in texts(page0) and "▶" in texts(page0)
    press(bot, cb(page0, "sm:l:1o"))
    page1 = last_edit(bot)
    assert "Topic 08" in " ".join(texts(page1)) and "Page 2 of 3" in page1["text"]
    press(bot, cb(page1, "sm:l:2o"))
    page2 = last_edit(bot)
    assert len([t for t in texts(page2) if " · " in t]) == 3
    assert "▶" not in texts(page2)
    press(bot, "sm:l:9o")  # a page past the end lands on the last one
    assert "Page 3 of 3" in last_edit(bot)["text"]


def test_only_the_owner_in_the_home_chat_can_press(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 13, "Dead one", 3 * DAY)
    for sender, chat in ((99, CHAT), (1, 555)):
        result, _ = press(bot, "sm:xy:dead-sess-1:0o", sender=sender, chat=chat)
        assert result == "ignored"
    assert not bot.methods("editMessageText") and not bot.methods("deleteForumTopic")
    assert toasts(bot) == ["לא זמין כאן"] * 2 or toasts(bot) == ["Not available here"] * 2


def test_malformed_callbacks_are_refused(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 13, "Dead one", 3 * DAY)
    for data in ("sm:", "sm:zz:dead-sess-1:0o", "sm:xy:dead-sess-1", "sm:xy:dead sess:0o", "sm:l:abc",
                 "sm:ty:x:0o", "sm:xy:dead-sess-1:0o:9"):
        assert press(bot, data)[0] == "ignored", data
    assert not bot.methods("deleteForumTopic") and not bot.methods("closeForumTopic")


def test_stale_page_never_acts_on_another_session(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 13, "Dead one", 3 * DAY)
    result, _ = press(bot, "sm:xy:gone-sess-9:0o")
    assert result == "stale"
    assert not bot.methods("deleteForumTopic")
    assert "Dead one" in " ".join(texts(last_edit(bot)))  # the list was refreshed instead
    add(env, "dead-sess-2", 14, "Twin", 3 * DAY)  # a prefix that now matches two sessions acts on neither
    assert press(bot, "sm:xy:dead-sess-:0o")[0] == "stale"
    assert not bot.methods("deleteForumTopic")


def test_callback_data_stays_under_64_bytes(env):
    bot = FakeBot()
    for i in range(10):
        add(env, f"3f2a9c1e-7b4d-4e8a-9f10-{i:012d}", 100 + i, f"Topic {i}", (3 + i) * DAY)
    add(env, "3f2a9c1e-7b4d-4e8a-9f10-live00000000", 200, "Live", 60, live=True)
    for lang in ("en", "he"):
        press(bot, "sm:l:1o", lang)
        views = [last_edit(bot)]
        for data in ("sm:d:3f2a9c1e-7b4:1o", "sm:e:3f2a9c1e-7b4:1o", "sm:t:1o", "sm:x1:3f2a9c1e-7b4:1o"):
            press(bot, data, lang)
            views.append(last_edit(bot))
        for view in views:
            for b in buttons(view):
                assert len(b.get("callback_data", "").encode()) <= 64


def test_end_session_asks_first_then_exits_and_closes_the_topic(env, monkeypatch):
    bot = FakeBot()
    add(env, "live-sess-1", 21, "Live one", 60, live=True)
    ended = []
    monkeypatch.setattr(inbox, "pane_for_session", lambda s: "%7")
    monkeypatch.setattr(inbox, "pane_runs_session", lambda pane, s, known_pid=None: pane == "%7")
    monkeypatch.setattr(inbox, "_exit_pane", lambda s, pane: ended.append((s, pane)) or True)
    pids = iter([os.getpid(), os.getpid(), None])
    monkeypatch.setattr(inbox, "session_pid", lambda s: next(pids, None))
    press(bot, "sm:e:live-sess-1:0o")
    ask = last_edit(bot)
    assert "End “Live one”" in ask["text"] and not ended and not bot.methods("closeForumTopic")
    press(bot, cb(ask, "sm:ey:"))
    assert ended == [("live-sess-1", "%7")]
    assert bot.methods("closeForumTopic") == [{"chat_id": CHAT, "message_thread_id": 21}]
    assert "✅ Ended and closed: Live one" in last_edit(bot)["text"]
    assert "live-sess-1" in sm.closed_marks()


def test_end_session_that_does_not_stop_leaves_the_topic_open(env, monkeypatch):
    bot = FakeBot()
    add(env, "live-sess-1", 21, "Live one", 60, live=True)
    monkeypatch.setattr(inbox, "pane_for_session", lambda s: "%7")
    monkeypatch.setattr(inbox, "pane_runs_session", lambda pane, s, known_pid=None: True)
    monkeypatch.setattr(inbox, "_exit_pane", lambda s, pane: True)
    monkeypatch.setattr(inbox, "session_pid", lambda s: os.getpid())
    press(bot, "sm:ey:live-sess-1:0o")
    assert not bot.methods("closeForumTopic")
    assert "did not stop" in last_edit(bot)["text"]


def test_end_session_whose_exit_could_not_be_typed_leaves_the_topic_open_at_once(env, monkeypatch):
    bot = FakeBot()
    add(env, "live-sess-1", 21, "Live one", 60, live=True)
    monkeypatch.setattr(inbox, "pane_for_session", lambda s: "%7")
    monkeypatch.setattr(inbox, "pane_runs_session", lambda pane, s, known_pid=None: True)
    monkeypatch.setattr(inbox, "_exit_pane", lambda s, pane: False)  # a draft or an unreadable box: not typed
    monkeypatch.setattr(inbox, "session_pid", lambda s: os.getpid())
    press(bot, "sm:ey:live-sess-1:0o")
    assert not bot.methods("closeForumTopic")
    assert "did not stop" in last_edit(bot)["text"]


def test_tidy_skips_live_and_recent_sessions(env):
    bot = FakeBot()
    add(env, "old-ended-1", 31, "Old ended", 5 * DAY)
    add(env, "mid-ended-1", 32, "Mid ended", 3 * DAY)
    add(env, "new-ended-1", 33, "New ended", 3600)
    add(env, "edge-ended-1", 34, "Edge ended", 1.5 * DAY)
    add(env, "old-live-01", 35, "Old but live", 9 * DAY, live=True)
    press(bot, "sm:l:0o")
    tidy = next(t for t in texts(last_edit(bot)) if t.startswith("🧹"))
    assert tidy == "🧹 Tidy: close 2 ended sessions inactive 2+ days"
    press(bot, "sm:t:0o")
    ask = last_edit(bot)
    assert "Old ended" in ask["text"] and "Mid ended" in ask["text"] and "New ended" not in ask["text"]
    assert not bot.methods("closeForumTopic")  # nothing happens before the confirmation
    press(bot, cb(ask, "sm:ty:2"))
    assert sorted(p["message_thread_id"] for p in bot.methods("closeForumTopic")) == [31, 32]
    assert not bot.methods("deleteForumTopic")
    assert "✅ Closed 2 topics." in last_edit(bot)["text"]
    shown = " ".join(texts(last_edit(bot)))
    assert "Old ended" not in shown and "New ended" in shown and "Show closed (2)" in shown


def test_tidy_with_a_stale_count_asks_again(env):
    bot = FakeBot()
    add(env, "old-ended-1", 31, "Old ended", 5 * DAY)
    add(env, "old-ended-2", 32, "Old ended two", 5 * DAY)
    press(bot, "sm:ty:1:0o")  # the page said 1, there are 2 now
    assert not bot.methods("closeForumTopic")
    assert "Close 2 ended sessions" in last_edit(bot)["text"]
    assert cb(last_edit(bot), "sm:ty:2")


def test_tidy_waits_while_telegram_asks_to_slow_down(env):
    bot = FakeBot()
    add(env, "old-ended-1", 31, "Old ended", 5 * DAY)
    flood.record_429(CHAT, 30)
    press(bot, "sm:ty:1:0o")
    assert not bot.methods("closeForumTopic")
    assert toasts(bot)[-1] == "Telegram asked to slow down. Try again in a minute."


def test_tidy_stops_at_the_first_429(env):
    bot = FakeBot()
    for i in range(3):
        add(env, f"old-ended-{i}", 31 + i, f"Old {i}", (5 + i) * DAY)
    real = bot.call

    def call(method, params=None, timeout=15):
        if method == "closeForumTopic" and len(bot.methods("closeForumTopic")) == 1:
            bot.calls.append((method, params))
            raise TelegramError("Too Many Requests", 429, 5)
        return real(method, params, timeout)

    bot.call = call
    press(bot, "sm:ty:3:0o")
    assert len(bot.methods("closeForumTopic")) == 2  # one done, the second hit the limit, the third never sent
    assert "Closed 1 of 3" in last_edit(bot)["text"]


def test_close_then_reopen_moves_the_topic_between_lists(env):
    bot = FakeBot()
    add(env, "live-sess-1", 41, "Keep me", 60, live=True)
    add(env, "dead-sess-1", 42, "Archive me", 3 * DAY)
    press(bot, "sm:c:dead-sess-1:0o")
    assert bot.methods("closeForumTopic") == [{"chat_id": CHAT, "message_thread_id": 42}]
    after = last_edit(bot)
    assert "📦 Topic closed: Archive me" in after["text"]
    assert not any("Archive me" in t for t in texts(after) if " · " in t)
    press(bot, cb(after, "sm:l:0c"))
    closed = last_edit(bot)
    assert "Closed topics (1)" in closed["text"]
    assert any("Archive me" in t for t in texts(closed)) and "♻️" in texts(closed)
    press(bot, cb(closed, "sm:o:"))
    assert bot.methods("reopenForumTopic") == [{"chat_id": CHAT, "message_thread_id": 42}]
    assert "♻️ Topic reopened: Archive me" in last_edit(bot)["text"]
    assert "Nothing to show here." in last_edit(bot)["text"]  # the closed list stays open for the next one
    press(bot, "sm:l:0o")
    assert any("Archive me" in t for t in texts(last_edit(bot)))
    assert sm.closed_marks() == {}


def test_a_closed_topic_that_was_used_again_is_listed_as_open(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 42, "Archive me", 3 * DAY)
    press(bot, "sm:c:dead-sess-1:0o")
    path = env / "claude" / "projects" / "p" / "dead-sess-1.jsonl"
    os.utime(path, (time.time() + 120, time.time() + 120))
    assert [e.closed for e in sm.collect(tmap(bot))] == [False]


def test_delete_is_for_ended_sessions_and_asks_twice(env):
    bot = FakeBot()
    add(env, "live-sess-1", 51, "Live one", 60, live=True)
    add(env, "dead-sess-1", 52, "Dead one", 4 * DAY)
    live_detail = (press(bot, "sm:d:live-sess-1:0o"), last_edit(bot))[1]
    assert not any("Delete" in t for t in texts(live_detail))
    for step in ("x1", "x2", "xy"):  # even a hand-made callback does not delete a live session
        press(bot, f"sm:{step}:live-sess-1:0o")
    assert not bot.methods("deleteForumTopic")
    assert "Only an ended session can be deleted" in toasts(bot)[-1]

    press(bot, "sm:d:dead-sess-1:0o")
    detail = last_edit(bot)
    assert "🗑 Delete topic" in texts(detail)
    press(bot, cb(detail, "sm:x1:"))
    first = last_edit(bot)
    assert "permanent" in first["text"] and not bot.methods("deleteForumTopic")
    press(bot, cb(first, "sm:x2:"))
    second = last_edit(bot)
    assert "forever" in second["text"] and not bot.methods("deleteForumTopic")
    press(bot, cb(second, "sm:xy:"))
    assert bot.methods("deleteForumTopic") == [{"chat_id": CHAT, "message_thread_id": 52}]
    assert "dead-sess-1" not in tmap(bot).all()
    assert "🗑 Deleted: Dead one" in last_edit(bot)["text"]


def test_delete_rechecks_that_the_session_is_still_ended(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 52, "Dead one", 4 * DAY)
    press(bot, "sm:x2:dead-sess-1:0o")
    (env / "claude" / "sessions" / "52.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": "dead-sess-1"}))
    press(bot, "sm:xy:dead-sess-1:0o")  # it came back to life between the two taps
    assert not bot.methods("deleteForumTopic")


def test_hebrew_names_are_isolated_and_the_text_is_plain(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 52, "Orbit app", 4 * DAY)
    press(bot, "sm:l:0o", "he")
    listing = last_edit(bot)
    assert any(f"{FSI}Orbit app{PDI}" in t for t in texts(listing))
    assert listing["text"].startswith("‏") and "הסשנים" in listing["text"]
    press(bot, "sm:d:dead-sess-1:0o", "he")
    detail = last_edit(bot)
    assert f"{FSI}Orbit app{PDI}" in detail["text"]
    assert "הסתיים" in detail["text"] and "פעילות אחרונה: לפני 4 ימים" in detail["text"]
    press(bot, cb(detail, "sm:x1:"), "he")
    assert f"{FSI}Orbit app{PDI}" in last_edit(bot)["text"] and "סופית" in last_edit(bot)["text"]
    press(bot, "sm:l:0o", "en")
    assert FSI not in " ".join(texts(last_edit(bot)))


def test_the_manager_opens_from_the_status_message_and_the_more_menu(env):
    bot = FakeBot()
    add(env, "dead-sess-1", 52, "Dead one", 3600)
    inbox.send_status(bot, cfg(), CHAT, {"message_thread_id": None}, tmap(bot))
    status_msg = bot.methods("sendMessage")[-1]
    assert "🗂 Manage sessions" in texts(status_msg) and cb(status_msg, "sm:l:") == "sm:l:0o"
    assert "🗂 Manage sessions" in texts({"reply_markup": inbox.more_markup("en")})
    assert "🗂 ניהול סשנים" in "".join(texts({"reply_markup": inbox.more_markup("he")}))


def _end_setup(env, monkeypatch, pane_verified, command):
    """A live session whose saved pane is (not) verified, and a process whose program is `command`.
    Returns the lists of typed keys and signals; the process exits once it gets SIGTERM or /exit."""
    add(env, "live-sess-1", 21, "Live one", 60, live=True)
    typed, signals, alive = [], [], [True]
    monkeypatch.setattr(inbox, "pane_for_session", lambda s: "%7")
    monkeypatch.setattr(inbox, "pane_runs_session", lambda pane, s, known_pid=None: pane_verified)
    monkeypatch.setattr(inbox, "_tmux", BoxedTmux(lambda *a, **k: typed.append(a)))
    monkeypatch.setattr(inbox, "session_pid", lambda s: os.getpid() if alive[0] else None)
    monkeypatch.setattr(inbox, "_ps_field", lambda field, pid: command.split()[0] if field == "comm=" else command)

    def kill(pid, sig):
        if sig == 0:
            return
        signals.append((pid, sig))
        alive[0] = False

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(inbox.time, "sleep", lambda s: alive.__setitem__(0, alive[0] and not typed))
    return typed, signals


@pytest.mark.real_type_command
def test_exit_is_typed_only_into_a_verified_pane(env, monkeypatch):
    bot = FakeBot()
    typed, signals = _end_setup(env, monkeypatch, True, "/usr/local/bin/claude --resume x")
    press(bot, "sm:ey:live-sess-1:0o")
    assert ("send-keys", "-t", "%7", "-l", "/exit") in typed
    assert all(a[2] == "%7" for a in typed if a[0] == "send-keys") and not signals
    assert bot.methods("closeForumTopic") == [{"chat_id": CHAT, "message_thread_id": 21}]


def test_a_stale_pane_gets_nothing_typed_and_the_process_is_stopped_by_pid(env, monkeypatch):
    bot = FakeBot()
    typed, signals = _end_setup(env, monkeypatch, False, "/usr/local/bin/claude --resume x")
    press(bot, "sm:ey:live-sess-1:0o")
    assert typed == []  # %7 now belongs to some other window: no Escape, no /exit
    assert signals == [(os.getpid(), 15)]
    assert bot.methods("closeForumTopic") == [{"chat_id": CHAT, "message_thread_id": 21}]


def test_a_process_that_is_not_claude_is_never_signalled(env, monkeypatch):
    bot = FakeBot()
    for command in ("python3 server.py --name claude-helper", "node /srv/claude-dashboard/dev.js"):
        typed, signals = _end_setup(env, monkeypatch, False, command)
        press(bot, "sm:ey:live-sess-1:0o")
        assert typed == [] and signals == []
        assert not bot.methods("closeForumTopic")
        assert "Only a session running in a terminal" in last_edit(bot)["text"]


def test_short_session_ids_need_an_exact_match(env):
    bot = FakeBot()
    add(env, "abcd", 61, "Long one", 4 * DAY)
    result, _ = press(bot, "sm:xy:abc:0o")  # "abc" is only the start of "abcd"
    assert result == "stale" and not bot.methods("deleteForumTopic")
    add(env, "abc", 62, "Short one", 4 * DAY)
    press(bot, "sm:xy:abc:0o")
    assert bot.methods("deleteForumTopic") == [{"chat_id": CHAT, "message_thread_id": 62}]
    assert "abcd" in tmap(bot).all()
    add(env, "3f2a9c1e-7b4d-4e8a-9f10-000000000001", 63, "Uuid", 4 * DAY)
    press(bot, "sm:xy:3f2a9c1e-7b4:0o")  # the 12-character prefix of a real id still works
    assert {"chat_id": CHAT, "message_thread_id": 63} in bot.methods("deleteForumTopic")
