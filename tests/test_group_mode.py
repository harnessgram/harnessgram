"""Group mode: a forum supergroup as the home chat. Only the owner is obeyed there; private mode is unchanged."""
import json
import subprocess

import pytest

from conftest import FakeBot, BoxedTmux
from ctb import config as C
from ctb import doctor, pairing
from ctb import inbox as I
from ctb.config import Config, home_chat, state_dir, write_private
from ctb.topics import TopicMap

pytestmark = pytest.mark.real_type_command  # the real gate (BoxedTmux gives it a box)

OWNER, GROUP = 7, -1001234567890


def rig(bot, chat_id=GROUP):
    cfg = Config(owner_chat_id=OWNER, chat_id=chat_id)
    topics = TopicMap(state_dir() / "topics.json", bot, home_chat(cfg))
    thread = topics.ensure("session", "Title")
    write_private(state_dir() / "tmux-panes.json", json.dumps({"session": "%12"}))
    return cfg, topics, thread, I.Inbox(state_dir() / "inbox")


def message(text, thread, chat_id=GROUP, chat_type="supergroup", sender=OWNER):
    return {"message_id": 44, "message_thread_id": thread, "date": 2000000000,
            "chat": {"id": chat_id, "type": chat_type}, "from": {"id": sender}, "text": text}


def terminal(monkeypatch):
    calls = []
    monkeypatch.setattr(I, "_terminal_background", lambda session, action: (action(), True)[1])
    monkeypatch.setattr(I, "pane_text", lambda pane: "❯")
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    monkeypatch.setattr(I, "_tmux", BoxedTmux(lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "")))
    return calls


def test_home_chat_defaults_to_the_private_chat():
    assert home_chat(Config(owner_chat_id=OWNER)) == OWNER
    assert home_chat(Config(owner_chat_id=OWNER, chat_id=GROUP)) == GROUP
    assert home_chat(Config(owner_chat_id=OWNER, chat_id=5)) == OWNER  # only a supergroup id counts


def test_topics_are_created_in_the_group(bot):
    rig(bot)
    assert bot.methods("createForumTopic")[0]["chat_id"] == GROUP


def test_owner_command_in_the_group_reaches_the_session(bot, monkeypatch):
    cfg, topics, thread, box = rig(bot)
    calls = terminal(monkeypatch)
    assert I.handle({"message": message("/review", thread)}, cfg, bot, topics, box, None, 0) == "cli"
    typed = [c for c in calls if c[0] == "send-keys" and "-l" in c]
    assert typed and typed[0][-1] == "/review"


def test_others_in_the_group_and_other_chats_are_ignored(bot, monkeypatch):
    cfg, topics, thread, box = rig(bot)
    calls = terminal(monkeypatch)
    for forged in (message("/review", thread, sender=99),                              # another member
                   message("/review", thread, chat_id=OWNER, chat_type="private"),     # private chat in group mode
                   message("/review", thread, chat_id=-100999, chat_type="supergroup")):  # another group
        assert I.handle({"message": forged}, cfg, bot, topics, box, None, 0) == "ignored"
    assert not calls and not box.pending("session")


def test_private_mode_ignores_group_messages(bot, monkeypatch):
    cfg, topics, thread, box = rig(bot, chat_id=0)
    calls = terminal(monkeypatch)
    assert I.handle({"message": message("/review", thread, chat_id=GROUP)}, cfg, bot, topics, box, None, 0) == "ignored"
    assert not calls


def test_button_from_another_member_is_refused(bot, monkeypatch):
    cfg, topics, thread, box = rig(bot)
    called = []
    monkeypatch.setattr(I, "cli_command", lambda cfg, bot, m, session, text, **kw: called.append(text) or "cli")
    m = {"chat": {"id": GROUP, "type": "supergroup"}, "message_thread_id": thread, "message_id": 2}
    q = {"id": "q", "from": {"id": 99}, "message": m, "data": "model:sonnet"}
    assert I.handle_button(q, cfg, bot, topics, box) == "ignored"
    assert not called
    q["from"] = {"id": OWNER}
    assert I.handle_button(q, cfg, bot, topics, box) == "cli"
    assert called == ["/model claude-sonnet-5-5"]


class GroupBot(FakeBot):
    def __init__(self, forum=True, status="administrator", manage=True, updates=None):
        super().__init__()
        self.forum, self.status, self.manage, self.updates = forum, status, manage, updates or []

    def call(self, method, params=None, timeout=15):
        if method == "getMe":
            return {"id": 1, "username": "demo_bot"}
        if method == "getChat":
            return {"id": GROUP, "is_forum": self.forum, "title": "Work"}
        if method == "getChatMember":
            return {"status": self.status, "can_manage_topics": self.manage}
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return out
        return super().call(method, params, timeout)


def test_doctor_reports_group_problems():
    cfg = Config(owner_chat_id=OWNER, chat_id=GROUP, bot_token="1:" + "a" * 35)
    assert all(level == "ok" for level, _ in doctor.check_group(cfg, GroupBot()))
    bad = doctor.check_group(cfg, GroupBot(forum=False, status="member", manage=False))
    assert [level for level, _ in bad] == ["FAIL", "FAIL"]
    assert any("Manage topics" in msg for _, msg in bad)
    assert doctor.check_group(Config(owner_chat_id=OWNER), GroupBot()) == []  # private mode: nothing to check


def test_group_pairing_only_accepts_the_owner():
    def update(uid, sender):
        return {"update_id": uid, "message": {"date": 2000000000, "text": "/start@demo_bot",
                                             "chat": {"id": GROUP, "type": "supergroup"}, "from": {"id": sender}}}
    bot = GroupBot(updates=[update(1, 99), update(2, OWNER)])
    found = pairing.wait_for_group_start(bot, "demo_bot", 2000000000, OWNER, timeout=5,
                                         now=iter(range(10 ** 9, 10 ** 9 + 100)).__next__)
    assert found and found["from"]["id"] == OWNER
    bot = GroupBot(updates=[update(1, 99)])
    assert pairing.wait_for_group_start(bot, "demo_bot", 2000000000, OWNER, timeout=5,
                                        now=iter(range(10 ** 9, 10 ** 9 + 100)).__next__) is None


def test_config_round_trip_keeps_the_group(tmp_path):
    path = C.save_raw({"bot_token": "1:" + "a" * 35, "owner_chat_id": OWNER, "chat_id": GROUP}, tmp_path / "c.json")
    assert home_chat(C.load(path)) == GROUP


def test_more_menu_works_in_the_group(bot, monkeypatch):
    cfg, topics, thread, box = rig(bot)
    m = {"chat": {"id": GROUP, "type": "supergroup"}, "message_thread_id": thread, "message_id": 2}
    q = {"id": "q", "from": {"id": OWNER}, "message": m, "data": "more:global"}
    assert I.more_button(q, cfg, bot, m, topics, box, "session") != "refused"
    assert not any("לא זמין" in str(p.get("text", "")) for p in bot.methods("answerCallbackQuery"))
    edits = bot.methods("editMessageText")
    assert edits and all(e["chat_id"] == GROUP for e in edits)  # the menu in the group, not the owner's DM


def test_login_code_is_deleted_from_the_group(bot):
    cfg, *_ = rig(bot)
    I.account_code(cfg, bot, {"message_id": 55, "chat": {"id": GROUP, "type": "supergroup"}}, "short")
    assert bot.methods("deleteMessage")[0] == {"chat_id": GROUP, "message_id": 55}


def test_home_group_and_team_group_must_differ(tmp_path):
    import pytest
    raw = {"bot_token": "1:" + "a" * 35, "owner_chat_id": OWNER, "chat_id": GROUP,
           "team": {"chat_id": GROUP, "members": {}}}
    with pytest.raises(C.ConfigError):
        C.from_dict(raw)


def test_switching_mode_resets_the_topic_map(tmp_path, monkeypatch):
    from ctb import cli
    write_private(state_dir() / "topics.json", "{}")
    cli._reset_topics_for_mode_switch()
    assert not (state_dir() / "topics.json").exists()
    assert list(state_dir().glob("topics.before-switch-*.json"))
