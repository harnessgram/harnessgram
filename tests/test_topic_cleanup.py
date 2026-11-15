"""Old topics: the detector's rules, the owner-only endpoint (login, confirm), the mocked Telegram calls."""
import json
import threading
import time

import pytest

from conftest import GOOD_TOKEN
from ctb import board, cards, decisions, panel, scheduled, sessions_menu, topic_cleanup
from ctb.config import Config, state_dir
from ctb.telegram import TelegramError
from ctb.topics import TopicMap
from test_panel import OWNER, call, signed

CHAT = -1001234567
DAY = 86400
E = sessions_menu.Entry
GOOD = "a1b2c3d4"


class FakeBot:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def call(self, method, params=None):
        self.calls.append((method, dict(params or {})))
        if self.fail:
            raise TelegramError(self.fail)
        return True


def cfg_():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=CHAT, target_language="he")


def entries(monkeypatch, *rows):
    """rows: (session, thread, age in days, state, closed)."""
    monkeypatch.setattr(sessions_menu, "collect", lambda topics, now=None: [
        E(s, f"Topic {s[:4]}", th, None if age is None else age * DAY, st, closed, None)
        for s, th, age, st, closed in rows])


def found(cfg=None):
    cfg = cfg or cfg_()
    return [e.session for e in topic_cleanup.candidates(cfg, TopicMap(state_dir() / "topics.json", None, CHAT, cfg=cfg))]


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(scheduled, "task_of", lambda s: "")
    monkeypatch.setattr(cards, "open_cards", lambda: [])
    monkeypatch.setattr(board, "open_reminders", lambda: [])
    monkeypatch.setattr(board, "open_checks", lambda: [])
    monkeypatch.setattr(board, "items_of", lambda chat, thread: [])
    monkeypatch.setattr(topic_cleanup.inbox_mod, "_closed", lambda s: True)
    monkeypatch.setattr(topic_cleanup, "_pause", lambda seconds: None)
    monkeypatch.setattr(topic_cleanup.flood, "is_open", lambda chat, now=None: True)


def test_default_is_fourteen_days_and_the_setting_changes_it(monkeypatch):
    assert cfg_().topic_cleanup_days == 14
    entries(monkeypatch, (GOOD + "-x", 5, 13, "ended", False), ("b" * 8 + "-y", 6, 15, "ended", False))
    assert found() == ["b" * 8 + "-y"]
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=CHAT, topic_cleanup_days=10)
    assert set(found(cfg)) == {GOOD + "-x", "b" * 8 + "-y"}


@pytest.mark.parametrize("row", [
    (GOOD + "-g", 1, 40, "ended", False),       # the General topic
    (GOOD + "-c", 5, 40, "ended", True),        # already closed
    (GOOD + "-w", 5, 40, "working", False),     # still working
    (GOOD + "-u", 5, None, "ended", False),     # no trace of activity: stale cannot be proven
    (GOOD + "-i", 5, 40, "idle", False),        # a live idle session is never offered, only ended ones
])
def test_never_offered(monkeypatch, row):
    entries(monkeypatch, row)
    assert found() == []


def test_open_decision_card_reminder_checklist_and_pinned_link_keep_a_topic(monkeypatch):
    rows = [(f"{n}{n}{n}{n}{n}{n}{n}{n}-s", 10 + i, 30, "ended", False) for i, n in enumerate("abcdef")]
    entries(monkeypatch, *rows)
    assert len(found()) == 6
    decisions.create(rows[0][0], CHAT, 10, "Question?", now=time.time())
    monkeypatch.setattr(cards, "open_cards", lambda: [{"session": "zz", "chat": CHAT, "thread": 11}])
    monkeypatch.setattr(board, "open_reminders", lambda: [(CHAT, 12, {"i": 1})])
    monkeypatch.setattr(board, "open_checks", lambda: [(CHAT, 13, {"i": 1})])
    monkeypatch.setattr(board, "items_of", lambda chat, thread: [{"k": "link"}] if thread == 14 else [])
    assert found() == [rows[5][0]]


def test_scheduled_task_topic_and_foreign_chat_are_left_alone(monkeypatch):
    entries(monkeypatch, (GOOD + "-t", 5, 30, "ended", False))
    monkeypatch.setattr(scheduled, "task_of", lambda s: "daily")
    assert found() == []
    monkeypatch.setattr(scheduled, "task_of", lambda s: "")
    monkeypatch.setattr(topic_cleanup.decisions, "owner_chats", lambda cfg: frozenset({-100999}))
    assert found() == []


def test_keep_forever_hides_the_topic_and_is_logged(monkeypatch):
    entries(monkeypatch, (GOOD + "-k", 5, 30, "ended", False))
    cfg = cfg_()
    topics = TopicMap(state_dir() / "topics.json", None, CHAT, cfg=cfg)
    assert topic_cleanup.keep(cfg, topics, [GOOD]) == {"kept": 1, "skipped": 0}
    assert found() == []
    assert topic_cleanup.log_lines()[0]["action"] == "keep"


def test_close_only_what_the_detector_offers_and_logs_every_close(monkeypatch):
    entries(monkeypatch, (GOOD + "-a", 5, 30, "ended", False), ("bbbbbbbb-b", 6, 2, "ended", False))
    cfg, bot = cfg_(), FakeBot()
    topics = TopicMap(state_dir() / "topics.json", None, CHAT, cfg=cfg)
    out = topic_cleanup.close(cfg, bot, topics, [GOOD, "bbbbbbbb"])
    assert out == {"closed": 1, "skipped": 1, "closed_ids": [GOOD]}
    assert bot.calls == [("closeForumTopic", {"chat_id": CHAT, "message_thread_id": 5})]  # never a delete
    log = topic_cleanup.log_lines()
    assert [(r["action"], r["session"], r["result"]) for r in log] == [("close", GOOD, "ok")]


def test_reopen_undoes_only_closes_made_here(monkeypatch):
    entries(monkeypatch, (GOOD + "-a", 5, 30, "ended", False))
    cfg, bot = cfg_(), FakeBot()
    topics = TopicMap(state_dir() / "topics.json", None, CHAT, cfg=cfg)
    topic_cleanup.close(cfg, bot, topics, [GOOD])
    sessions_menu._set_mark("zzzzzzzz-other", 99, chat=CHAT)  # closed by something else
    assert topic_cleanup.reopen(cfg, bot, topics, ["zzzzzzzz"]) == {"reopened": 0, "skipped": 1}
    assert topic_cleanup.reopen(cfg, bot, topics, [GOOD]) == {"reopened": 1, "skipped": 0}
    assert bot.calls[-1] == ("reopenForumTopic", {"chat_id": CHAT, "message_thread_id": 5})
    assert [r["action"] for r in topic_cleanup.log_lines()] == ["close", "reopen"]


def test_telegram_refusal_is_not_counted_as_closed(monkeypatch):
    entries(monkeypatch, (GOOD + "-a", 5, 30, "ended", False))
    cfg = cfg_()
    topics = TopicMap(state_dir() / "topics.json", None, CHAT, cfg=cfg)
    out = topic_cleanup.close(cfg, FakeBot(fail="Bad Request: not enough rights"), topics, [GOOD])
    assert out["closed"] == 0 and topic_cleanup.log_lines()[0]["result"] == "failed"


# --- the endpoint ---------------------------------------------------------------------------------

@pytest.fixture
def server(monkeypatch):
    srv = panel.PanelServer(("127.0.0.1", 0), cfg_())
    srv._bot = FakeBot()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def post(server, init, **body):
    return call(server, path="/api/topic-cleanup", init=init, body=json.dumps(body).encode())


def test_endpoint_is_owner_only_with_the_fresh_login(server, monkeypatch):
    entries(monkeypatch, (GOOD + "-a", 5, 30, "ended", False))
    body = dict(action="close", ids=[GOOD])
    assert post(server, None, **body)[0] == 403
    assert post(server, signed(auth_date=int(time.time()) - panel.DECIDE_MAX_AGE - 5), **body)[0] == 403
    assert post(server, signed(user_id=OWNER + 1), **body)[0] == 403
    assert server._bot.calls == []
    code, _, data = post(server, signed(), **body)
    assert code == 200 and json.loads(data)["closed_ids"] == [GOOD]
    assert server._bot.calls[0][0] == "closeForumTopic"


def test_closing_several_needs_the_confirmed_count(server, monkeypatch):
    entries(monkeypatch, (GOOD + "-a", 5, 30, "ended", False), ("bbbbbbbb-b", 6, 30, "ended", False))
    ids = [GOOD, "bbbbbbbb"]
    assert post(server, signed(), action="close", ids=ids)[0] == 409
    assert post(server, signed(), action="close", ids=ids, confirm=3)[0] == 409
    assert post(server, signed(), action="close", ids=ids, confirm=True)[0] == 409
    assert server._bot.calls == []
    code, _, data = post(server, signed(), action="close", ids=ids, confirm=2)
    assert code == 200 and json.loads(data)["closed"] == 2
    assert {c[0] for c in server._bot.calls} == {"closeForumTopic"}


@pytest.mark.parametrize("bad", [
    dict(action="delete", ids=[GOOD]), dict(action="close", ids=[]), dict(action="close", ids="a"),
    dict(action="close", ids=["../x"]), dict(action="close", ids=[GOOD, GOOD]), dict(ids=[GOOD]),
    dict(action="close", ids=[GOOD], extra=1), dict(action="close", ids=[f"{n:08x}" for n in range(21)])])
def test_bad_requests_are_refused(server, bad):
    assert post(server, signed(), **bad)[0] == 400
    assert server._bot.calls == []


def test_keep_and_reopen_through_the_endpoint(server, monkeypatch):
    entries(monkeypatch, (GOOD + "-a", 5, 30, "ended", False), ("bbbbbbbb-b", 6, 30, "ended", False))
    assert json.loads(post(server, signed(), action="close", ids=[GOOD])[2])["closed"] == 1
    assert json.loads(post(server, signed(), action="keep", ids=["bbbbbbbb"])[2])["kept"] == 1
    assert found() == [GOOD + "-a"]  # (the mocked listing still shows the closed one as open)
    assert json.loads(post(server, signed(), action="reopen", ids=[GOOD])[2])["reopened"] == 1
    assert server._bot.calls[-1][0] == "reopenForumTopic"


def test_state_carries_the_card_only_behind_ui_v2(server, monkeypatch):
    entries(monkeypatch, (GOOD + "-a", 5, 30, "ended", False))
    off = json.loads(call(server, init=signed())[2])
    assert "topic_cleanup" not in off
    from ctb import panel_settings
    monkeypatch.setattr(panel_settings, "read", lambda: {"ui_v2": "on"})
    on = json.loads(call(server, init=signed())[2])
    row = on["topic_cleanup"]["topics"][0]
    assert on["topic_cleanup"]["days"] == 14 and row["id"] == GOOD and row["age"] == 30 * DAY
    assert "chat" not in row and "thread" not in row


# --- review fixes (P1/P2) -------------------------------------------------------------------------

def test_p1_a_topic_shared_with_a_working_session_is_never_offered(monkeypatch):
    # B is quiet for 30 days, A (same thread) is working: the whole topic stays out, whatever the order
    for rows in ((("aaaaaaaa-A", 5, 0, "working", False), ("bbbbbbbb-B", 5, 30, "ended", False)),
                 (("bbbbbbbb-B", 5, 30, "ended", False), ("aaaaaaaa-A", 5, 0, "working", False))):
        entries(monkeypatch, *rows)
        assert found() == []
    entries(monkeypatch, ("aaaaaaaa-A", 5, 20, "ended", False), ("bbbbbbbb-B", 5, 30, "ended", False))
    assert found() == ["aaaaaaaa-A"]  # both quiet: one row, represented by the newest session
    # and a close attempt for the quiet one cannot slip through
    entries(monkeypatch, ("aaaaaaaa-A", 5, 0, "working", False), ("bbbbbbbb-B", 5, 30, "ended", False))
    cfg, bot = cfg_(), FakeBot()
    out = topic_cleanup.close(cfg, bot, TopicMap(state_dir() / "topics.json", None, CHAT, cfg=cfg), ["bbbbbbbb"])
    assert out["closed"] == 0 and bot.calls == []


def test_p1_a_queued_owner_message_a_running_process_or_a_move_keeps_the_topic(monkeypatch):
    entries(monkeypatch, (GOOD + "-q", 5, 30, "ended", False))
    assert found() == [GOOD + "-q"]
    from ctb.inbox import Inbox
    queue = Inbox(state_dir() / "inbox")._paths(GOOD + "-q")[0]
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text('{"text": "hi"}\n')  # a queued owner message (a failed wake)
    assert Inbox(state_dir() / "inbox").pending(GOOD + "-q")
    assert found() == []
    queue.unlink()
    assert found() == [GOOD + "-q"]
    monkeypatch.setattr(topic_cleanup.inbox_mod, "_closed", lambda s: False)  # running, moving or limit wait
    assert found() == []


def test_p2_close_pauses_honours_the_flood_gate_and_caps_at_twenty(monkeypatch):
    rows = [(f"{n:08x}-s", 10 + n, 30, "ended", False) for n in range(25)]
    entries(monkeypatch, *rows)
    cfg, bot = cfg_(), FakeBot()
    topics = TopicMap(state_dir() / "topics.json", None, CHAT, cfg=cfg)
    pauses = []
    monkeypatch.setattr(topic_cleanup, "_pause", pauses.append)
    out = topic_cleanup.close(cfg, bot, topics, [f"{n:08x}" for n in range(25)])
    assert out["closed"] == 20 and len(bot.calls) == 20 and pauses == [1.0] * 19
    assert topic_cleanup.view(cfg, topics)["count"] == 20


def test_p2_flood_gate_is_read_per_target_chat(monkeypatch):
    other = CHAT - 1
    monkeypatch.setattr(sessions_menu, "collect", lambda topics, now=None: [
        E(GOOD + "-h", "Home", 5, 30 * DAY, "ended", False, None),
        E("cccccccc-p", "Proj", 6, 30 * DAY, "ended", False, other)])
    monkeypatch.setattr(topic_cleanup.decisions, "owner_chats", lambda cfg: frozenset({CHAT, other}))
    asked = []
    # the home group is flooded, the project group is not: only the project topic closes
    monkeypatch.setattr(topic_cleanup.flood, "is_open", lambda chat, now=None: asked.append(chat) or chat != CHAT)
    cfg, bot = cfg_(), FakeBot()
    out = topic_cleanup.close(cfg, bot, TopicMap(state_dir() / "topics.json", None, CHAT, cfg=cfg), [GOOD, "cccccccc"])
    assert out["closed"] == 1 and out["skipped"] == 1
    assert bot.calls == [("closeForumTopic", {"chat_id": other, "message_thread_id": 6})] and CHAT in asked and other in asked


def test_p1_eligibility_is_checked_again_right_before_each_close(monkeypatch):
    rows = [(f"{n:08x}-s", 10 + n, 30, "ended", False) for n in range(3)]
    entries(monkeypatch, *rows)
    cfg, bot = cfg_(), FakeBot()
    topics = TopicMap(state_dir() / "topics.json", None, CHAT, cfg=cfg)

    def wakes_during_the_wait(seconds):  # while the pause runs, topic 1 gets a live session and topic 2 a queued message
        entries(monkeypatch, rows[0], (rows[1][0], rows[1][1], 0, "working", False), rows[2])
    monkeypatch.setattr(topic_cleanup, "_pause", wakes_during_the_wait)
    out = topic_cleanup.close(cfg, bot, topics, [f"{n:08x}" for n in range(3)])
    assert [c[1]["message_thread_id"] for c in bot.calls] == [10, 12] and out["closed"] == 2 and out["skipped"] == 1
    # a queued owner message that arrives mid-run is caught the same way
    bot.calls.clear()
    entries(monkeypatch, *rows)
    monkeypatch.setattr(topic_cleanup.inbox_mod.Inbox, "pending", lambda self, s: s.startswith("00000002") and bool(bot.calls))
    monkeypatch.setattr(topic_cleanup, "_pause", lambda seconds: None)
    out = topic_cleanup.close(cfg, bot, topics, [f"{n:08x}" for n in range(3)])
    assert [c[1]["message_thread_id"] for c in bot.calls] == [10, 11]


def test_p2_a_failed_reopen_keeps_the_mark_and_the_undo_entry(monkeypatch):
    entries(monkeypatch, (GOOD + "-a", 5, 30, "ended", False))
    cfg, bot = cfg_(), FakeBot()
    topics = TopicMap(state_dir() / "topics.json", None, CHAT, cfg=cfg)
    topic_cleanup.close(cfg, bot, topics, [GOOD])
    broken = FakeBot(fail="Bad Request: not enough rights")
    assert topic_cleanup.reopen(cfg, broken, topics, [GOOD]) == {"reopened": 0, "skipped": 1}
    assert len(broken.calls) == 2 and any(k.startswith(GOOD) for k in sessions_menu.closed_marks())
    assert topic_cleanup.undoable(cfg, topics, time.time()) == [GOOD]  # still offered as an undo
    assert topic_cleanup.reopen(cfg, bot, topics, [GOOD]) == {"reopened": 1, "skipped": 0}
    assert not any(k.startswith(GOOD) for k in sessions_menu.closed_marks())


def test_p2_keep_has_an_undo_and_writes_under_a_lock(monkeypatch):
    entries(monkeypatch, (GOOD + "-k", 5, 30, "ended", False))
    cfg = cfg_()
    topics = TopicMap(state_dir() / "topics.json", None, CHAT, cfg=cfg)
    assert topic_cleanup.keep(cfg, topics, [GOOD])["kept"] == 1 and found() == []
    assert (state_dir() / "topic-cleanup-keep.json.lock").exists()
    assert topic_cleanup.unkeep(cfg, topics, [GOOD]) == {"unkept": 1, "skipped": 0}
    assert found() == [GOOD + "-k"]
    assert topic_cleanup.unkeep(cfg, topics, [GOOD])["unkept"] == 0
    assert [r["action"] for r in topic_cleanup.log_lines()] == ["keep", "unkeep"]


def test_p2_unkeep_through_the_endpoint(server, monkeypatch):
    entries(monkeypatch, (GOOD + "-k", 5, 30, "ended", False))
    assert json.loads(post(server, signed(), action="keep", ids=[GOOD])[2])["kept"] == 1
    assert json.loads(post(server, signed(), action="unkeep", ids=[GOOD])[2])["unkept"] == 1
