"""Inbound routing for project groups: the owner is obeyed in the home group and in each configured project
group, nobody else is, and a topic is always looked up by chat AND thread. Thread ids are only unique inside
one chat, so the tests put a home session and a project session on the very same thread number."""
import json
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN
from ctb import config as C
from ctb import dialogs, inbox as I, sessions_menu, voice
from ctb.config import state_dir
from ctb.topics import TopicMap

OWNER, HOME, ORBIT, OTHER = 7, -1001234567890, -1001111111111, -1001999999999
THREAD = 5  # the same number in both chats, on purpose
PICK = "Pick one\n\n❯ 1. Yes\n  2. No\n\nEnter to select · ↑/↓ to navigate · Esc to cancel"


class TextMedia:
    def content_of(self, m):
        return m.get("text") or ""


@pytest.fixture
def rig(bot, tmp_path, monkeypatch):
    folder = tmp_path / "orbit"
    folder.mkdir()
    site = tmp_path / "orbit-site"  # two folders: the group asks which one (one folder opens directly)
    site.mkdir()
    cfg = C.from_dict({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "chat_id": HOME, "target_language": "en",
                       "projects": [{"name": "Orbit", "chat_id": ORBIT, "folders": [str(folder), str(site)]}]})
    tmap = TopicMap(state_dir() / "topics.json", bot, HOME, cfg=cfg)
    tmap.ensure("home-s", "Home work", thread=THREAD)
    tmap.ensure("orbit-s", "Orbit: game", thread=THREAD, chat=ORBIT)
    monkeypatch.setattr(I, "_tmux", lambda *a: subprocess.CompletedProcess(a, 0, ""))
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    return SimpleNamespace(cfg=cfg, bot=bot, topics=tmap, box=I.Inbox(state_dir() / "inbox"), tmp=tmp_path)


def msg(text="hello", chat=ORBIT, thread=THREAD, sender=OWNER, mid=44, chat_type="supergroup", **extra):
    m = {"message_id": mid, "date": 2_000_000_000, "text": text, "chat": {"id": chat, "type": chat_type},
         "from": {"id": sender}}
    if thread is not None:
        m["message_thread_id"] = thread
    m.update(extra)
    return m


def send(rig, m, key="message"):
    return I.handle({key: m}, rig.cfg, rig.bot, rig.topics, rig.box, TextMedia(), 0)


def press(rig, data, chat=ORBIT, thread=THREAD, sender=OWNER, mid=50, chat_type="supergroup"):
    m = {"chat": {"id": chat, "type": chat_type}, "message_id": mid}
    if thread is not None:
        m["message_thread_id"] = thread
    return I.handle_button({"id": "q", "from": {"id": sender}, "message": m, "data": data}, rig.cfg, rig.bot,
                           rig.topics, rig.box, TextMedia())


def queued(rig, session):
    return [i["text"] for i in rig.box.take(session)]


# --- who is obeyed -----------------------------------------------------------------------

def test_a_stranger_in_a_project_group_is_ignored(rig):
    for forged in (msg(sender=99), msg(chat_type="group"), msg(chat_type="private")):
        assert send(rig, forged) == "ignored"
    assert queued(rig, "orbit-s") == [] and queued(rig, "home-s") == []
    assert rig.bot.calls == []


def test_the_owner_in_a_project_group_is_obeyed(rig):
    assert send(rig, msg("do the thing")) == "queued"
    assert queued(rig, "orbit-s") == ["do the thing"]
    assert {p["chat_id"] for p in rig.bot.methods("setMessageReaction")} == {ORBIT}


def test_an_owner_message_in_an_unconfigured_group_is_ignored_and_recorded_without_content(rig):
    assert send(rig, msg("secret plan", chat=OTHER, chat_type="supergroup", chat_title=None)) == "ignored"
    seen = json.loads((state_dir() / "telegram-groups.json").read_text())
    assert set(seen) == {str(OTHER)} and "secret plan" not in json.dumps(seen)
    assert queued(rig, "orbit-s") == [] and queued(rig, "home-s") == []
    # a configured project group is served, not recorded as a stranger
    assert send(rig, msg("hello", mid=45)) == "queued"
    assert set(json.loads((state_dir() / "telegram-groups.json").read_text())) == {str(OTHER)}


def test_a_callback_from_an_unconfigured_chat_is_refused(rig, monkeypatch):
    called = []
    monkeypatch.setattr(I, "cli_command", lambda cfg, bot, m, session, text, **kw: called.append(text) or "cli")
    assert press(rig, "model:sonnet", chat=OTHER) == "ignored"
    assert press(rig, "more:global", chat=OTHER) == "ignored"
    assert not called
    assert all("editMessageText" != m for m, _p in rig.bot.calls)


def test_a_non_owner_callback_in_a_project_chat_is_refused(rig, monkeypatch):
    called = []
    monkeypatch.setattr(I, "cli_command", lambda cfg, bot, m, session, text, **kw: called.append(text) or "cli")
    assert press(rig, "model:sonnet", sender=99) == "ignored"
    assert press(rig, "more:global", sender=99) == "ignored"
    assert press(rig, "model:sonnet", chat_type="group") == "ignored"
    assert not called
    assert press(rig, "model:sonnet") == "cli" and called == ["/model claude-sonnet-5-5"]
    assert press(rig, "model:fable") == "cli" and called[-1] == "/model claude-fable-5-2"
    assert press(rig, "model:gpt") == "ignored" and called[-1] == "/model claude-fable-5-2"


# --- the same thread number in two chats ---------------------------------------------------

def test_text_goes_to_the_session_of_its_own_chat(rig):
    assert send(rig, msg("for orbit", chat=ORBIT)) == "queued"
    assert send(rig, msg("for home", chat=HOME, mid=45)) == "queued"
    assert queued(rig, "orbit-s") == ["for orbit"]
    assert queued(rig, "home-s") == ["for home"]
    # the queued item remembers its chat, so a receipt and an edit find the right message
    send(rig, msg("again", chat=ORBIT, mid=46))
    assert [i["chat"] for i in rig.box.take("orbit-s")] == [ORBIT]


def test_an_edit_reaches_the_session_of_its_own_chat(rig):
    send(rig, msg("first", chat=ORBIT, mid=42))
    send(rig, msg("other first", chat=HOME, mid=42))  # the same message id in the other group
    edit = msg("second", chat=ORBIT, mid=42, edit_date=2_000_000_010)
    assert send(rig, edit, key="edited_message") == "edited"
    assert queued(rig, "orbit-s") == ["second"]
    assert queued(rig, "home-s") == ["other first"]
    stranger = msg("hacked", chat=OTHER, mid=42, edit_date=2_000_000_020)
    assert send(rig, stranger, key="edited_message") == "ignored"


def test_a_button_acts_on_the_session_of_its_own_chat(rig, monkeypatch):
    seen = []
    monkeypatch.setattr(I, "cli_command", lambda cfg, bot, m, session, text, **kw: seen.append(session) or "cli")
    assert press(rig, "model:opus", chat=ORBIT) == "cli"
    assert press(rig, "model:opus", chat=HOME) == "cli"
    assert seen == ["orbit-s", "home-s"]
    rig.topics.forget("orbit-s", chat=ORBIT)
    assert press(rig, "model:opus", chat=ORBIT) == "ignored"  # no session in that topic: the home one is not used


def test_say_buttons_edit_the_tapped_chat(rig, monkeypatch):
    monkeypatch.setattr(I, "main_working", lambda s: True)
    monkeypatch.setattr(I, "_deliver_button_word", lambda *a, **k: None)
    assert press(rig, "say:status", chat=ORBIT) == "status"
    sent = rig.bot.methods("sendMessage")
    assert sent[-1]["chat_id"] == ORBIT and sent[-1]["message_thread_id"] == THREAD
    assert "reply_markup" not in sent[-1]  # the session manager lists the home group: not offered here
    assert "orbit-s" not in json.dumps(sent[-1]) and "Home work" not in sent[-1]["text"]


def test_a_dialog_answer_typed_in_a_project_topic_goes_to_that_session(rig, monkeypatch):
    monkeypatch.setattr(dialogs, "open_screen", lambda s: ("%1", PICK, dialogs.parse(PICK), True))
    monkeypatch.setattr(dialogs, "_answer", lambda session, pane, d, n: seen.append((session, n)) or True)
    monkeypatch.setattr(I, "_terminal_background", lambda s, a: a() or True)
    monkeypatch.setattr(I, "react", lambda *a, **k: True)
    monkeypatch.setattr(dialogs.time, "sleep", lambda s: None)
    seen = []
    assert dialogs.send_card(rig.cfg, rig.bot, "orbit-s", THREAD, chat=ORBIT)
    rec = dialogs.rec_get("orbit-s")
    assert rec["chat"] == ORBIT and rec["thread"] == THREAD
    # the same words in the home topic with that number are not an answer to the project's dialog
    assert dialogs.pending_for("orbit-s", msg("1", chat=HOME), None) is False
    assert dialogs.pending_for("orbit-s", msg("1", chat=ORBIT), ORBIT) is True
    assert send(rig, msg("1", chat=ORBIT)) == "dialog-typed"
    assert seen == [("orbit-s", 1)]
    assert {p["chat_id"] for p in rig.bot.methods("editMessageText")} <= {ORBIT}


def test_a_dialog_button_is_checked_against_chat_and_thread(rig, monkeypatch):
    monkeypatch.setattr(dialogs, "open_screen", lambda s: ("%1", PICK, dialogs.parse(PICK), True))
    monkeypatch.setattr(I, "_terminal_background", lambda s, a: a() or True)
    taps = []
    monkeypatch.setattr(dialogs, "button", lambda q, cfg, bot, m, session: taps.append((m["chat"]["id"], session)) or "dialog")
    assert press(rig, "dg:s", chat=ORBIT) == "dialog"
    assert taps == [(ORBIT, "orbit-s")]
    assert press(rig, "dg:s", chat=OTHER) == "ignored"
    assert press(rig, "dg:s", chat=ORBIT, thread=THREAD + 1) == "ignored"  # a topic that has no session
    assert len(taps) == 1


def test_a_resume_button_is_checked_against_chat_and_thread(rig, monkeypatch):
    taps = []
    monkeypatch.setattr(I, "_terminal_background", lambda s, a: a() or True)
    monkeypatch.setattr(I, "resume_button_press", lambda q, cfg, bot, m, session: taps.append(session))
    assert press(rig, "rs:orbit-s", chat=ORBIT) == "resume"
    assert press(rig, "rs:home-s", chat=ORBIT) == "ignored"  # the home session's button, in the wrong chat
    assert taps == ["orbit-s"]


def test_close_menu_edits_the_tapped_chat(rig):
    assert press(rig, "close:ask", chat=ORBIT) == "close"
    edit = rig.bot.methods("editMessageText")[-1]
    assert edit["chat_id"] == ORBIT and edit["message_id"] == 50
    assert press(rig, "close:ask", chat=OTHER) == "ignored"


def test_a_voice_draft_is_kept_per_chat(rig):
    media = SimpleNamespace(content_parts=lambda m: ("", "a voice note"), content_of=lambda m: "a voice note")
    voice.set_confirm(True)
    for chat in (ORBIT, HOME):
        m = msg("", chat=chat, mid=60, voice={"file_id": "x"})
        m.pop("text")
        assert I.handle({"message": m}, rig.cfg, rig.bot, rig.topics, rig.box, media, 0) == "voice"
    drafts = voice.load()
    assert set(drafts) == {f"{ORBIT}:60", "60"}  # message ids repeat from one group to the next
    assert press(rig, "vd:send:60", chat=ORBIT, mid=61) == "voice"
    assert queued(rig, "orbit-s") == ["a voice note"] and queued(rig, "home-s") == []
    assert set(voice.load()) == {"60"}  # the home group's draft is untouched


def test_the_next_text_after_an_edit_tap_is_the_project_topics_own(rig):
    voice.add(f"{ORBIT}:60", {"session": "orbit-s", "text": "x", "thread": THREAD, "chat": ORBIT})
    voice.mark_editing(f"{ORBIT}:60")
    assert voice.edit_pending(THREAD, None) is False  # the home topic with that number
    assert voice.edit_pending(THREAD, ORBIT) is True


# --- forwards ----------------------------------------------------------------------------------

def test_forwards_are_refused_in_a_project_chat(rig):
    forwarded = msg("someone else's words", forward_origin={"type": "user"})
    assert send(rig, forwarded) == "forward-refused"
    refusal = rig.bot.methods("sendMessage")[-1]
    assert refusal["chat_id"] == ORBIT and refusal["message_thread_id"] == THREAD
    assert queued(rig, "orbit-s") == []
    # a stranger's forward is not even answered
    rig.bot.calls.clear()
    assert send(rig, msg("x", sender=99, forward_origin={"type": "user"})) == "ignored"
    assert rig.bot.calls == []


def test_the_forward_notice_is_throttled_per_chat_and_topic(rig):
    I._forward_notices.clear()
    send(rig, msg("one", forward_origin={"type": "user"}))
    send(rig, msg("two", chat=HOME, forward_origin={"type": "user"}, mid=45))  # the same thread number, another chat
    assert [p["chat_id"] for p in rig.bot.methods("sendMessage")] == [ORBIT, HOME]


# --- General of a project group ------------------------------------------------------------------

def test_general_in_a_project_group_opens_the_picker_there_with_the_project_folder_first(rig, monkeypatch):
    other = rig.tmp / "other"
    other.mkdir()
    monkeypatch.setattr(I, "projects", lambda *a, **k: [str(other), "/tmp/general"])
    monkeypatch.setattr(I, "_offer_applies", lambda *a: pytest.fail("the offer is home-only"))
    from ctb import dispatcher
    monkeypatch.setattr(dispatcher, "get_dispatcher", lambda: pytest.fail("the dispatcher is home-only"))
    assert send(rig, msg("build the thing", thread=None)) == "picker"
    assert rig.bot.methods("createForumTopic")[-1]["chat_id"] == ORBIT
    sends = rig.bot.methods("sendMessage")
    picker = [p for p in sends if "inline_keyboard" in str(p.get("reply_markup"))][-1]
    assert picker["chat_id"] == ORBIT and picker["message_thread_id"] == 101  # the topic just created
    labels = [b["text"] for row in picker["reply_markup"]["inline_keyboard"] for b in row]
    assert labels[:3] == ["orbit", "orbit-site", "other"]  # the project's folders first, then the rest
    assert labels[3] == I.t("project_picker_general", lang="en")  # General last
    guide = sends[-1]
    assert guide["chat_id"] == ORBIT and "message_thread_id" not in guide and "t.me/c/1111111111/" in guide["text"]
    pending = json.loads((state_dir() / "projects-pending.json").read_text())
    (key, job), = pending.items()
    assert key.startswith(f"{ORBIT}:") and job["dirs"][0] == str(rig.tmp / "orbit") and job["dirs"][-1] == "/tmp/general"
    assert HOME not in {p.get("chat_id") for _m, p in rig.bot.calls}


def test_choosing_a_folder_creates_the_session_in_the_project_group(rig, monkeypatch):
    monkeypatch.setattr(I, "projects", lambda *a, **k: ["/tmp/general"])
    monkeypatch.setattr(I.threading, "Thread", lambda *a, **k: SimpleNamespace(start=lambda: None))
    send(rig, msg("build the thing", thread=None))
    picker = [p for p in rig.bot.methods("sendMessage") if "inline_keyboard" in str(p.get("reply_markup"))][-1]
    data = picker["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    thread = picker["message_thread_id"]
    assert press(rig, data, chat=HOME, thread=thread) == "ignored"  # the home group has no such waiting topic
    assert press(rig, data, chat=ORBIT, thread=thread) == "picker"
    entry = json.loads((state_dir() / "topics-projects.json").read_text())
    (new,) = [e for s, e in entry.items() if s not in ("orbit-s",)]
    assert new["chat"] == ORBIT and new["thread"] == thread and new["project"] == "Orbit"
    assert thread not in {e["thread"] for e in json.loads((state_dir() / "topics.json").read_text()).values()}
    assert json.loads((state_dir() / "projects-pending.json").read_text()) == {}
    edits = rig.bot.methods("editMessageText") + rig.bot.methods("editForumTopic")
    assert edits and {p["chat_id"] for p in edits} == {ORBIT}


def test_a_project_group_with_one_folder_asks_only_the_model_and_one_tap_opens_it_there(rig, monkeypatch):
    rig.cfg.projects[0].folders = rig.cfg.projects[0].folders[:1]
    monkeypatch.setattr(I, "projects", lambda *a, **k: ["/tmp/general"])
    monkeypatch.setattr(I.threading, "Thread", lambda *a, **k: SimpleNamespace(start=lambda: None))
    started = []
    monkeypatch.setattr(I, "_tmux", lambda *a: started.append(a) or SimpleNamespace(returncode=0, stdout=""))
    assert send(rig, msg("build the thing", thread=77)) == "picker"
    sends = rig.bot.methods("sendMessage")
    asked = [p for p in sends if "inline_keyboard" in str(p.get("reply_markup"))]
    assert len(asked) == 1 and "nm:" in str(asked[0]["reply_markup"]) and '"n:' not in str(asked[0]["reply_markup"])
    assert not started  # nothing starts before the model tap
    token = json.loads((state_dir() / "projects-pending.json").read_text())[f"{ORBIT}:77"]["token"]
    press(rig, f"nm:{token}:d", thread=77)  # one tap: the default model, the session opens there
    assert started and started[0][0] == "new-session" and str(rig.tmp / "orbit") in started[0]
    assert "--model" not in started[0][-1]
    entry = json.loads((state_dir() / "topics-projects.json").read_text())
    assert any(e["chat"] == ORBIT and e["thread"] == 77 for e in entry.values())
    assert json.loads((state_dir() / "projects-pending.json").read_text()) == {}
    assert {p["chat_id"] for _m, p in rig.bot.calls if "chat_id" in p} == {ORBIT}


def test_a_new_topic_typed_in_a_project_group_gets_its_picker_there(rig, monkeypatch):
    monkeypatch.setattr(I, "projects", lambda *a, **k: ["/tmp/general"])
    assert send(rig, msg("a new idea", thread=77)) == "picker"
    assert {p["chat_id"] for p in rig.bot.methods("sendMessage")} == {ORBIT}
    assert set(json.loads((state_dir() / "projects-pending.json").read_text())) == {f"{ORBIT}:77"}


def test_cancelling_the_picker_forgets_only_that_chats_waiting_topic(rig, monkeypatch):
    monkeypatch.setattr(I, "projects", lambda *a, **k: ["/tmp/general"])
    send(rig, msg("one", thread=77))
    send(rig, msg("two", chat=HOME, thread=77, mid=45))
    token = {k: v["token"] for k, v in json.loads((state_dir() / "projects-pending.json").read_text()).items()}
    assert set(token) == {f"{ORBIT}:77", "77"}
    assert press(rig, f"n:{token['77']}:x", chat=ORBIT, thread=77) == "cancelled"
    assert set(json.loads((state_dir() / "projects-pending.json").read_text())) == {"77"}


def test_general_of_the_home_group_is_unchanged(rig, monkeypatch):
    monkeypatch.setattr(I, "projects", lambda *a, **k: ["/tmp/general"])
    assert send(rig, msg("home task", chat=HOME, thread=None)) in ("offer", "picker")
    assert {p["chat_id"] for p in rig.bot.methods("sendMessage")} == {HOME}


def test_a_project_general_never_reaches_the_dispatcher(rig, monkeypatch):
    from ctb import dispatcher
    monkeypatch.setattr(dispatcher, "get_dispatcher", lambda: "dispatcher-s")
    monkeypatch.setattr(I, "projects", lambda *a, **k: ["/tmp/general"])
    assert send(rig, msg("task", thread=None)) == "picker"
    assert queued(rig, "dispatcher-s") == []


# --- keyboard, relay, commands -------------------------------------------------------------------

def test_the_keyboard_is_restored_in_the_chat_of_the_message(rig, monkeypatch):
    kicked = []
    monkeypatch.setattr(I, "keyboard_back", lambda thread, delay=2.0, cfg=None, bot=None, chat=None:
                        kicked.append((thread, chat)))
    wrapped = I._watch_buttons(lambda *a, **k: {}, I.owner_chats(rig.cfg), HOME)
    keyboard = {"inline_keyboard": [[{"text": "x", "callback_data": "y"}]]}
    wrapped("sendMessage", {"chat_id": ORBIT, "message_thread_id": 9, "reply_markup": keyboard})
    wrapped("sendMessage", {"chat_id": HOME, "message_thread_id": 9, "reply_markup": keyboard})
    wrapped("sendMessage", {"chat_id": OTHER, "message_thread_id": 9, "reply_markup": keyboard})  # the team chat, say
    assert kicked == [(9, ORBIT), (9, None)]


def test_the_keyboard_timers_of_two_chats_do_not_replace_each_other(rig, monkeypatch):
    I._restore_timers.clear()
    monkeypatch.setattr(I.threading, "Timer", lambda delay, fn: SimpleNamespace(
        start=lambda: None, cancel=lambda: None, daemon=False))
    I.keyboard_back(9, cfg=rig.cfg, bot=rig.bot)
    I.keyboard_back(9, cfg=rig.cfg, bot=rig.bot, chat=ORBIT)
    assert set(I._restore_timers) == {"9", f"{ORBIT}:9"}
    I._restore_timers.clear()


def test_the_relay_serves_the_project_groups(rig, bot, monkeypatch):
    from ctb import creds as K, telegram as T
    from test_hotfixes import running_relay
    with tempfile.TemporaryDirectory(dir="/tmp") as short:  # a unix socket path is limited to about a hundred characters
        monkeypatch.setenv("CTB_STATE_DIR", short)
        with running_relay(bot, rig.cfg):
            for chat in (ORBIT, HOME, OWNER):
                K.relay_call({"method": "sendMessage", "params": {"chat_id": chat, "text": "x"}})
            with pytest.raises(T.TelegramError, match="chat_not_relayed"):
                K.relay_call({"method": "sendMessage", "params": {"chat_id": OTHER, "text": "x"}})
    assert [p["chat_id"] for p in bot.methods("sendMessage")] == [ORBIT, HOME, OWNER]


def test_commands_are_registered_for_the_owner_in_each_project_group(rig):
    I.set_commands(rig.bot, rig.cfg)
    scopes = [p["scope"] for p in rig.bot.methods("setMyCommands")]
    assert {(s["type"], s["chat_id"], s["user_id"]) for s in scopes} == {("chat_member", HOME, OWNER),
                                                                         ("chat_member", ORBIT, OWNER)}
    menus = {p["scope"]["chat_id"]: {c["command"] for c in p["commands"]} for p in rig.bot.methods("setMyCommands")}
    assert {"members", "alerts"} <= menus[HOME] and not {"members", "alerts"} & menus[ORBIT]  # home-only commands


def test_a_closed_project_topic_reopens_when_the_owner_writes_in_it(rig, monkeypatch):
    reopened = []
    monkeypatch.setattr(sessions_menu, "reopen_for_message", lambda cfg, bot, session: reopened.append(session))
    sessions_menu._set_mark("orbit-s", THREAD, chat=ORBIT)
    sessions_menu.note_input(rig.cfg, rig.bot, {"message": msg("hi", chat=HOME)})  # the home topic with that number
    assert reopened == []
    sessions_menu.note_input(rig.cfg, rig.bot, {"message": msg("hi", chat=ORBIT)})
    assert reopened == ["orbit-s"]


# --- nothing changes without projects -------------------------------------------------------------

def test_without_projects_a_project_chat_is_a_stranger(bot):
    cfg = C.from_dict({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "chat_id": HOME, "target_language": "en"})
    tmap = TopicMap(state_dir() / "topics.json", bot, HOME, cfg=cfg)
    tmap.ensure("home-s", "Home work", thread=THREAD)
    box = I.Inbox(state_dir() / "inbox")
    assert I.handle({"message": msg("x", chat=ORBIT)}, cfg, bot, tmap, box, TextMedia(), 0) == "ignored"
    assert I.handle({"message": msg("x", chat=HOME)}, cfg, bot, tmap, box, TextMedia(), 0) == "queued"


def test_the_panel_button_and_panel_command_answer_in_the_topic_and_never_reach_the_session(rig, monkeypatch):
    from ctb import panel_pin
    from ctb.i18n import both_langs
    asked = []
    monkeypatch.setattr(panel_pin, "send_link", lambda cfg, bot, chat, thread, topics=None: asked.append((chat, thread)) or True)
    monkeypatch.setattr(I, "keyboard_back", lambda *a, **k: None)
    old = ("🎛 לוח בקרה", "🎛 Control panel")  # a keyboard sent before the rename still shows the old name
    for text in (*both_langs("fixed_btn_panel"), "/panel", "/panel@DemoBot", *old):
        assert send(rig, msg(text)) == "panel"
    assert all(I._keyboard_word(t) for t in old)  # and is never read as an answer to a dialog
    assert send(rig, msg("/panel", chat=HOME, thread=None)) == "panel"  # General too
    assert asked == [(ORBIT, THREAD)] * 6 + [(HOME, None)]
    # a real message that only starts with the icon is the owner's words: it reaches the session (review of #175)
    queued(rig, "orbit-s")
    words = ("🎛 please review the mixing console code and fix the layout", "🎛 fix slider", "🎛")
    for n, text in enumerate(words):  # short ones too (review of #188: up to 25 characters were lost)
        assert send(rig, msg(text, mid=100 + n)) != "panel"
        assert not I._keyboard_word(text)  # and it may answer a question that is open in the session
    assert len(asked) == 7
    assert queued(rig, "orbit-s") == list(words)
