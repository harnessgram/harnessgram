"""Project groups, move: a session's topic moves to a project group (or back to the home group) with taps only.
The old topic gets a link and is closed, its messages are swept, and nothing typed there is ever delivered."""
import json
import re
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN
from ctb import config as C
from ctb import dialogs, inbox as I, moves, progress
from ctb.config import state_dir, write_private
from ctb.telegram import TelegramError
from ctb import topics as T
from ctb.topics import TopicMap, session_state_path

OWNER, HOME, ORBIT, PANTRY = 7, -1001234567890, -1001111111111, -1002222222222
THREAD = 5


class TextMedia:
    def content_of(self, m):
        return m.get("text") or ""


@pytest.fixture
def rig(bot, tmp_path, monkeypatch):
    cfg = C.from_dict({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "chat_id": HOME, "target_language": "en",
                       "ui_language": "en",
                       "projects": [{"name": "Orbit", "chat_id": ORBIT, "match": {"title_prefixes": ["Orbit"]}},
                                    {"name": "Pantry", "chat_id": PANTRY}]})
    tmap = TopicMap(state_dir() / "topics.json", bot, HOME, cfg=cfg)
    tmap.ensure("s1", "Chores", thread=THREAD)
    monkeypatch.setattr(I.time, "sleep", lambda _: None)
    return SimpleNamespace(cfg=cfg, bot=bot, topics=tmap, box=I.Inbox(state_dir() / "inbox"), tmp=tmp_path)


def msg(text="hello", chat=HOME, thread=THREAD, sender=OWNER, mid=44, chat_type="supergroup", **extra):
    m = {"message_id": mid, "date": 2_000_000_000, "text": text, "chat": {"id": chat, "type": chat_type},
         "from": {"id": sender}}
    if thread is not None:
        m["message_thread_id"] = thread
    m.update(extra)
    return m


def send(rig, m):
    return I.handle({"message": m}, rig.cfg, rig.bot, rig.topics, rig.box, TextMedia(), 0)


def press(rig, data, chat=HOME, thread=THREAD, sender=OWNER, mid=50, chat_type="supergroup"):
    m = {"chat": {"id": chat, "type": chat_type}, "message_id": mid}
    if thread is not None:
        m["message_thread_id"] = thread
    return I.handle_button({"id": "q", "from": {"id": sender}, "message": m, "data": data}, rig.cfg, rig.bot,
                           rig.topics, rig.box, TextMedia())


def buttons(rig, method="editMessageText"):
    """The callback data of the newest keyboard the bot sent or edited."""
    for call_method, params in reversed(rig.bot.calls):
        if call_method == method and params.get("reply_markup", {}).get("inline_keyboard"):
            return {b["callback_data"]: b["text"] for row in params["reply_markup"]["inline_keyboard"] for b in row}
    return {}


def moves_file():
    return json.loads((state_dir() / "topic-moves.json").read_text())


def seed_old_messages(session="s1"):
    """Everything the bridge keeps in the old topic: placeholder, status card, progress card, dialog keyboard."""
    path = session_state_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private(path, json.dumps({"state": "busy", "placeholder": 901, "all": [901, 902], "status_id": 903}))
    write_private(state_dir() / "status-footer.json", json.dumps({str(THREAD): 904}))
    pdir = state_dir() / "progress"
    pdir.mkdir(parents=True, exist_ok=True)
    write_private(pdir / f"{progress.safe_name(session)}.msg", json.dumps({"id": 905, "cards": ["x"], "dirty": True}))
    dialogs.rec_set(session, {"identity": "i", "stamp": "s", "message_id": 906, "thread": THREAD})


def deletions(rig, chat):
    return sorted(p["message_id"] for p in rig.bot.methods("deleteMessage") if p["chat_id"] == chat)


# --- the menu and the move ----------------------------------------------------------------

def test_the_more_menu_offers_the_move_only_with_project_groups(rig):
    plain = C.from_dict({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "chat_id": HOME})
    texts = lambda markup: [b["text"] for row in markup["inline_keyboard"] for b in row]
    assert "📁 Move to project group" in texts(I.more_markup("en", move=bool(rig.cfg.projects)))
    assert "📁 Move to project group" not in texts(I.more_markup("en", move=bool(plain.projects)))
    assert press(rig, "more:move") == "menu"
    shown = buttons(rig)
    assert [v for k, v in shown.items() if k.startswith("mv:")] == ["📁 Orbit", "📁 Pantry"]  # already home: no home entry


def test_a_session_in_a_project_can_go_home_or_to_another_project(rig):
    assert moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics) == "moved"
    new = rig.topics.place_of("s1")
    assert press(rig, "more:move", chat=ORBIT, thread=new.thread) == "menu"
    assert [v for k, v in buttons(rig).items() if k.startswith("mv:")] == ["📁 Pantry", "🏠 Home group"]


def test_the_move_sweeps_the_old_chat_and_writes_the_mapping(rig):
    seed_old_messages()
    assert moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics) == "moved"
    assert deletions(rig, HOME) == [901, 902, 903, 904, 905]  # placeholder, status card, progress card
    assert deletions(rig, ORBIT) == []
    assert {"chat_id": HOME, "message_id": 906, "reply_markup": {"inline_keyboard": []}} in rig.bot.methods("editMessageReplyMarkup")
    assert dialogs.rec_get("s1") == {}
    place = rig.topics.place_of("s1")
    assert (place.chat, place.home) == (ORBIT, False) and place.thread == 101
    assert "s1" not in json.loads((state_dir() / "topics.json").read_text())
    projects = json.loads((state_dir() / "topics-projects.json").read_text())
    assert projects["s1"]["chat"] == ORBIT and projects["s1"]["project"] == "Orbit"
    assert moves_file() == {f"{HOME}:{THREAD}": "s1"}
    state = json.loads(session_state_path("s1").read_text())
    assert "status_id" not in state and state["state"] == "busy"
    assert state["placeholder"] in state["all"]  # a busy session gets its placeholder in the new topic
    assert [p for p in rig.bot.methods("sendMessage") if p.get("text") == "🧑🏼‍💻"][-1]["chat_id"] == ORBIT
    assert not [p for p in (state_dir()).glob("topic-moves.json.*")]  # no temporary file left behind
    assert json.loads((state_dir() / "status-footer.json").read_text()).get(str(THREAD)) is None


def test_a_status_card_is_drawn_in_the_new_topic_only(rig):
    seed_old_messages()
    moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics)
    cards = [p for p in rig.bot.methods("sendMessage") if "reply_markup" in p and "mv:" not in json.dumps(p)]
    assert cards and all(p["chat_id"] == ORBIT and p["message_thread_id"] == 101 for p in cards)


def test_the_old_topic_gets_a_link_and_is_closed(rig):
    moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics)
    link = f"https://t.me/c/{str(ORBIT).removeprefix('-100')}/101"
    old = [p for p in rig.bot.methods("sendMessage") if p["chat_id"] == HOME and p["message_thread_id"] == THREAD]
    assert len(old) == 1 and link in old[0]["text"] and "the Orbit group" in old[0]["text"]
    assert rig.bot.methods("closeForumTopic") == [{"chat_id": HOME, "message_thread_id": THREAD}]
    arrived = [p for p in rig.bot.methods("sendMessage") if p["chat_id"] == ORBIT and p["message_thread_id"] == 101]
    assert any(p["text"] == "↪️ This session moved here." for p in arrived)


def test_input_in_the_old_topic_is_answered_with_the_link_and_never_delivered(rig):
    moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics)
    rig.bot.calls.clear()
    for m in (msg("still here?"), msg("a file", document={"file_id": "f"}), msg("/status"), msg("/more")):
        assert send(rig, m) == "moved"
    assert rig.box.take("s1") == []
    sent = rig.bot.methods("sendMessage")
    assert len(sent) == 4 and all(p["chat_id"] == HOME and p["message_thread_id"] == THREAD for p in sent)
    assert all("t.me/c/1111111111/101" in p["text"] for p in sent)  # no picker, no new session, no menu
    assert not rig.bot.methods("createForumTopic")
    # the new topic still takes the input
    assert send(rig, msg("hi there", chat=ORBIT, thread=101)) == "queued"
    assert [i["text"] for i in rig.box.take("s1")] == ["hi there"]


def test_a_project_picker_button_cannot_reopen_a_moved_topic(rig):
    moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics)
    rig.bot.calls.clear()
    assert press(rig, "n:" + "a" * 16 + ":0") == "ignored"
    assert rig.topics.session_at(HOME, THREAD) is None


def test_moving_back_to_the_home_group_works(rig):
    moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics)
    orbit_thread = rig.topics.place_of("s1").thread
    assert moves.move(rig.cfg, rig.bot, "s1", HOME, topics=rig.topics) == "moved"
    place = rig.topics.place_of("s1")
    assert place.home and place.chat == HOME and place.thread not in (THREAD, orbit_thread)
    assert json.loads((state_dir() / "topics.json").read_text())["s1"]["thread"] == place.thread
    assert "s1" not in json.loads((state_dir() / "topics-projects.json").read_text())
    assert moves_file() == {f"{HOME}:{THREAD}": "s1", f"{ORBIT}:{orbit_thread}": "s1"}
    assert {"chat_id": ORBIT, "message_thread_id": orbit_thread} in rig.bot.methods("closeForumTopic")
    assert send(rig, msg("old orbit topic", chat=ORBIT, thread=orbit_thread)) == "moved"
    assert send(rig, msg("old home topic", chat=HOME, thread=THREAD)) == "moved"
    assert rig.box.take("s1") == []
    assert send(rig, msg("home again", chat=HOME, thread=place.thread)) == "queued"


def test_moving_from_one_project_to_another(rig):
    moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics)
    assert moves.move(rig.cfg, rig.bot, "s1", PANTRY, topics=rig.topics) == "moved"
    place = rig.topics.place_of("s1")
    assert place.chat == PANTRY
    assert json.loads((state_dir() / "topics-projects.json").read_text())["s1"]["project"] == "Pantry"


def test_a_failed_topic_leaves_everything_as_it_was(rig):
    seed_old_messages()
    rig.bot.fail["createForumTopic"] = "Bad Request: not enough rights"
    assert moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics) == "failed"
    assert rig.topics.place_of("s1").thread == THREAD
    assert not (state_dir() / "topic-moves.json").exists()
    assert deletions(rig, HOME) == [] and not rig.bot.methods("closeForumTopic")
    assert send(rig, msg("still works")) == "queued"


def test_a_move_to_a_group_that_is_not_configured_or_to_the_same_group_does_nothing(rig):
    assert moves.move(rig.cfg, rig.bot, "s1", -1009999999999, topics=rig.topics) == "failed"
    assert moves.move(rig.cfg, rig.bot, "s1", HOME, topics=rig.topics) == "same"
    assert moves.move(rig.cfg, rig.bot, "nobody", ORBIT, topics=rig.topics) == "nosession"
    assert rig.bot.methods("createForumTopic") == []


def test_a_crash_while_switching_never_leaves_a_session_in_two_places(rig, monkeypatch):
    save = TopicMap._save_projects
    monkeypatch.setattr(TopicMap, "_save_projects", lambda self, data: (_ for _ in ()).throw(OSError("disk")))
    assert moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics) == "failed"
    monkeypatch.setattr(TopicMap, "_save_projects", save)  # not undo(): that would also drop the isolated state dir
    assert rig.topics.place_of("s1").thread == THREAD  # still in its first topic
    assert rig.topics.moved_session(HOME, THREAD) is None  # which is not marked as left
    assert rig.bot.methods("deleteForumTopic") == [{"chat_id": ORBIT, "message_thread_id": 101}]  # the new one is dropped
    assert not (state_dir() / "topic-moves.json").exists()
    assert send(rig, msg("please continue")) == "queued"  # input still reaches the session in its first topic
    assert [i["text"] for i in rig.box.take("s1")] == ["please continue"]


def test_a_crash_between_the_files_puts_both_back(rig, monkeypatch):
    rig.topics.ensure("s2", "Other", chat=ORBIT, thread=7)
    real = TopicMap._save
    monkeypatch.setattr(TopicMap, "_save", lambda self, data: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        rig.topics.relocate("s2", HOME, 300, "n", "n")  # project file written first, the home file then fails
    monkeypatch.setattr(TopicMap, "_save", real)
    assert rig.topics.place_of("s2").chat == ORBIT and rig.topics.place_of("s2").thread == 7
    assert rig.topics.moved_session(ORBIT, 7) is None


def test_an_unfinished_move_entry_is_ignored_while_the_session_is_still_there(rig):
    write_private(state_dir() / "topic-moves.json", json.dumps({f"{HOME}:{THREAD}": "s1"}))
    assert rig.topics.moved_session(HOME, THREAD) is None  # self-repairing: s1 is still in that topic
    rig.topics.relocate("s1", ORBIT, 101, "Chores", "Chores")
    assert rig.topics.moved_session(HOME, THREAD) == "s1"


def test_the_move_file_is_capped_and_the_newest_stay(rig, monkeypatch):
    monkeypatch.setattr("ctb.topics.MAX_MOVES", 3)
    for i in range(5):
        rig.topics.relocate("s1", ORBIT if i % 2 == 0 else HOME, 200 + i, "n", "n")
    assert len(moves_file()) == 3


# --- the callback token --------------------------------------------------------------------

def menu_token(rig, **kw):
    assert press(rig, "more:move", **kw) == "menu"
    return next(k for k in buttons(rig) if k.startswith("mv:"))


def test_the_tap_moves_the_session_and_turns_the_menu_into_the_link(rig):
    data = menu_token(rig)
    assert press(rig, data) == "move-moved"
    assert rig.topics.place_of("s1").chat == ORBIT
    edit = [p for p in rig.bot.methods("editMessageText") if p["message_id"] == 50][-1]
    assert "t.me/c/1111111111/101" in edit["text"] and edit["reply_markup"] == {"inline_keyboard": []}
    assert [p for p in rig.bot.methods("sendMessage") if p["chat_id"] == HOME and p["message_thread_id"] == THREAD] == []


def test_a_forged_or_reused_token_moves_nothing(rig):
    data = menu_token(rig)
    token = data.split(":")[1]
    for forged in (f"mv:{'0' * 16}:0", f"mv:{token}:1x", f"mv:{token}:9", "mv:short:0", f"mv:{token}:"):
        assert press(rig, forged) == "ignored"
    assert rig.topics.place_of("s1").chat == HOME
    assert press(rig, data) == "move-moved"
    assert press(rig, data) == "ignored"  # used up
    assert len(rig.bot.methods("createForumTopic")) == 1


def test_a_token_works_only_in_the_topic_and_for_the_session_it_was_made_for(rig):
    rig.topics.ensure("s2", "Other", thread=6)
    data = menu_token(rig)
    assert press(rig, data, thread=6) == "ignored"
    assert rig.topics.place_of("s2").home and rig.topics.place_of("s1").home
    assert press(rig, data, thread=999) == "ignored"  # not a session's topic at all
    assert press(rig, data) == "move-moved"


def test_only_the_paired_owner_can_move_a_session(rig):
    data = menu_token(rig)
    assert press(rig, data, sender=99) == "ignored"
    assert press(rig, data, chat_type="group") == "ignored"
    assert press(rig, data, chat=-1005555555555) == "ignored"
    assert press(rig, "more:move", sender=99) == "ignored"
    assert rig.topics.place_of("s1").chat == HOME and not rig.bot.methods("createForumTopic")
    assert press(rig, data) == "move-moved"  # the owner's tap still works: the forged taps did not use it up


def test_a_target_removed_from_the_settings_is_refused(rig):
    data = menu_token(rig)
    rig.cfg = C.from_dict({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "chat_id": HOME,
                           "projects": [{"name": "Pantry", "chat_id": PANTRY}]})
    rig.topics = TopicMap(state_dir() / "topics.json", rig.bot, HOME, cfg=rig.cfg)
    assert press(rig, data) == "ignored"
    assert rig.topics.place_of("s1").chat == HOME


def test_a_busy_session_move_says_so(rig, monkeypatch):
    data = menu_token(rig)
    monkeypatch.setattr(moves, "move", lambda *a, **k: "busy")
    assert press(rig, data) == "move-busy"
    assert "already running" in [p for p in rig.bot.methods("editMessageText") if p["message_id"] == 50][-1]["text"]


# --- the one-time offer when the title later names a project ---------------------------------

def offer(rig):
    return [p for p in rig.bot.methods("sendMessage") if "mv:" in json.dumps(p.get("reply_markup", {}))]


def test_a_late_title_offers_the_move_once_per_session(rig):
    rig.topics.ensure("s1", "Chores")
    assert offer(rig) == []
    rig.topics.ensure("s1", "Orbit: login screen")
    [card] = offer(rig)
    assert (card["chat_id"], card["message_thread_id"]) == (HOME, THREAD) and "Orbit" in card["text"]
    rig.topics.ensure("s1", "Orbit: login screen v2")
    rig.topics.ensure("s1", "Orbit: other")
    assert len(offer(rig)) == 1  # once, whatever the title does next
    assert rig.topics.place_of("s1").chat == HOME  # an offer never moves anything by itself


def test_the_offer_is_a_tap_to_move_or_to_keep(rig):
    rig.topics.ensure("s1", "Orbit: login screen")
    yes, no = (k for k in buttons(rig, "sendMessage"))
    assert press(rig, no) == "move-kept"
    assert rig.topics.place_of("s1").chat == HOME
    assert press(rig, yes) == "ignored"  # the offer was used up
    rig.topics.ensure("s2", "x", thread=7)
    rig.topics.ensure("s2", "Orbit: second")
    yes2 = next(k for k in buttons(rig, "sendMessage") if k.endswith(":0"))
    assert press(rig, yes2, thread=7) == "move-moved"
    assert rig.topics.place_of("s2").chat == ORBIT


def test_no_offer_when_the_session_is_already_there_or_the_title_names_nothing(rig):
    rig.topics.ensure("w", "Orbit: start", thread=8, chat=ORBIT)
    rig.topics.ensure("w", "Orbit: later")
    rig.topics.ensure("s1", "Orbituit: not a prefix")
    rig.topics.ensure("s1", "orbit without colon")
    assert offer(rig) == []


def test_a_session_that_was_moved_is_not_offered_a_move(rig):
    moves.move(rig.cfg, rig.bot, "s1", PANTRY, topics=rig.topics)
    rig.topics.ensure("s1", "Orbit: but the owner chose Pantry")
    assert offer(rig) == []


def test_the_offer_needs_no_project_groups_and_survives_a_telegram_error(rig):
    plain = C.from_dict({"bot_token": GOOD_TOKEN, "owner_chat_id": OWNER, "chat_id": HOME})
    TopicMap(state_dir() / "topics.json", rig.bot, HOME, cfg=plain).ensure("s1", "Orbit: x")
    assert offer(rig) == []
    rig.bot.fail["sendMessage"] = "Bad Request: chat not found"
    assert rig.topics.ensure("s1", "Orbit: x") == THREAD  # ensure itself is unharmed


# --- hooks racing a move ---------------------------------------------------------------------

def test_a_hook_that_read_the_place_before_the_move_never_touches_the_wrong_chat(rig, monkeypatch):
    seed_old_messages()
    old = rig.topics.place_of("s1")
    assert moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics) == "moved"
    new_placeholder = json.loads(session_state_path("s1").read_text())["placeholder"]
    rig.bot.calls.clear()
    real, first = TopicMap.place_of, {"stale": True}

    def stale_once(self, session):  # the hook's own read, made before the move, answered after the lock was won
        if first.pop("stale", False):
            return old
        return real(self, session)

    monkeypatch.setattr(TopicMap, "place_of", stale_once)
    T.set_state(rig.cfg, "s1", "done", bot=rig.bot)
    assert [p for p in rig.bot.methods("deleteMessage") if p["chat_id"] == HOME] == []
    assert not any(p.get("chat_id") == HOME and p.get("message_id") == new_placeholder
                   for _, p in rig.bot.calls)
    assert json.loads(session_state_path("s1").read_text())["chat"] == ORBIT


def test_tracked_messages_of_another_chat_are_forgotten_not_deleted(rig):
    rig.topics.relocate("s1", ORBIT, 101, "Chores", "Chores")
    path = session_state_path("s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private(path, json.dumps({"state": "busy", "chat": HOME, "placeholder": 901, "all": [901, 902],
                                    "status_id": 903, "status_text": "x"}))
    rig.bot.calls.clear()
    T.set_state(rig.cfg, "s1", "done", bot=rig.bot)
    assert rig.bot.methods("deleteMessage") == []
    cur = json.loads(path.read_text())
    assert cur["chat"] == ORBIT and cur["all"] == [] and "placeholder" not in cur and "status_id" not in cur


def test_the_progress_card_is_forgotten_before_the_session_changes_place(rig, monkeypatch):
    seed_old_messages()
    state_path = state_dir() / "progress" / f"{progress.safe_name('s1')}.msg"
    seen = {}
    real = TopicMap.relocate

    def spy(self, *args, **kw):
        seen["msg"] = json.loads(state_path.read_text())
        return real(self, *args, **kw)

    monkeypatch.setattr(TopicMap, "relocate", spy)
    assert moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics) == "moved"
    assert "id" not in seen["msg"] and "cards" not in seen["msg"]
    assert 905 in [p["message_id"] for p in rig.bot.methods("deleteMessage") if p["chat_id"] == HOME]


def test_a_failed_move_gives_the_progress_card_back(rig, monkeypatch):
    seed_old_messages()
    monkeypatch.setattr(TopicMap, "relocate", lambda self, *a, **k: None)
    assert moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics) == "failed"
    state_path = state_dir() / "progress" / f"{progress.safe_name('s1')}.msg"
    assert json.loads(state_path.read_text())["id"] == 905


def test_a_move_waits_for_a_progress_run_in_flight(rig):
    import fcntl
    import threading
    seed_old_messages()
    pdir = state_dir() / "progress"
    done = threading.Event()
    with open(pdir / f"{progress.safe_name('s1')}.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)  # a progress run holds this while it calls set_state
        worker = threading.Thread(
            target=lambda: (moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics), done.set()))
        worker.start()
        assert not done.wait(0.5)
        assert rig.topics.place_of("s1").chat == HOME
    worker.join(10)
    assert done.is_set() and rig.topics.place_of("s1").chat == ORBIT


# --- the menu and the offer do not replace each other --------------------------------------------

def test_opening_the_menu_keeps_the_offer_and_the_offer_keeps_the_menu(rig):
    menu = menu_token(rig)
    rig.topics.ensure("s1", "Orbit: login screen")  # the late-title offer arrives while the menu is on screen
    yes, _no = (k for k in buttons(rig, "sendMessage"))
    assert yes.split(":")[1] != menu.split(":")[1]
    assert press(rig, menu) == "move-moved"
    assert rig.topics.place_of("s1").chat == ORBIT


def test_the_offer_survives_the_menu_being_opened(rig):
    rig.topics.ensure("s1", "Orbit: login screen")
    yes, _no = (k for k in buttons(rig, "sendMessage"))
    menu_token(rig)
    assert press(rig, yes) == "move-moved"
    assert rig.topics.place_of("s1").chat == ORBIT


# --- a failing write of the moves file undoes the whole move ---------------------------------------

def test_a_failed_moves_write_leaves_place_card_state_and_topic_in_agreement(rig, monkeypatch):
    seed_old_messages()
    write_private(session_state_path("s1"), json.dumps({"state": "busy", "chat": HOME, "placeholder": 901,
                                                        "all": [901]}))
    real = T.write_private
    monkeypatch.setattr(T, "write_private", lambda p, t: (_ for _ in ()).throw(OSError("disk full"))
                        if str(p).endswith("topic-moves.json") else real(p, t))
    assert moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics) == "failed"
    monkeypatch.setattr(T, "write_private", real)
    assert (rig.topics.place_of("s1").chat, rig.topics.place_of("s1").thread) == (HOME, THREAD)
    assert rig.bot.methods("deleteForumTopic") == [{"chat_id": ORBIT, "message_thread_id": 101}]
    pstate = json.loads((state_dir() / "progress" / f"{progress.safe_name('s1')}.msg").read_text())
    assert pstate["id"] == 905
    cur = json.loads(session_state_path("s1").read_text())
    assert cur["chat"] == HOME and cur["placeholder"] == 901
    assert rig.bot.methods("deleteMessage") == []  # nothing of the old topic was touched
    assert rig.topics.moved_session(HOME, THREAD) is None


def test_a_dialog_posted_in_the_new_topic_during_the_move_is_left_alone(rig, monkeypatch):
    seed_old_messages()
    real_offered = moves._offered

    def offered(session, claim=False):  # a dialog card lands in the new topic right after the locks are released
        dialogs.rec_set(session, {"identity": "j", "stamp": "t", "message_id": 77, "thread": 101, "chat": ORBIT})
        return real_offered(session, claim)

    monkeypatch.setattr(moves, "_offered", offered)
    assert moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics) == "moved"
    assert [p for p in rig.bot.methods("editMessageReplyMarkup") if p["message_id"] == 77] == []
    assert dialogs.rec_get("s1")["message_id"] == 77


def test_the_old_topics_dialog_keyboard_is_still_removed(rig):
    seed_old_messages()
    moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics)
    assert [(p["chat_id"], p["message_id"]) for p in rig.bot.methods("editMessageReplyMarkup")] == [(HOME, 906)]
    assert dialogs.rec_get("s1") == {}


def test_ids_tracked_for_another_chat_are_not_swept_in_the_old_one(rig):
    seed_old_messages()
    write_private(session_state_path("s1"), json.dumps({"state": "idle", "chat": -999, "placeholder": 901,
                                                        "all": [901, 902], "status_id": 903}))
    moves.move(rig.cfg, rig.bot, "s1", ORBIT, topics=rig.topics)
    assert not {901, 902, 903} & {p["message_id"] for p in rig.bot.methods("deleteMessage")}
