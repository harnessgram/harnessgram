"""Topic places: a session's topic is a chat plus a thread. Home sessions stay in topics.json,
sessions in a project group live in topics-projects.json, and thread ids are never compared across chats."""
import json

from ctb import config as C
from ctb import projects as P
from ctb import topics as T
from ctb.config import state_dir
from ctb.topics import TopicMap

OWNER, HOME, ORBIT, PANTRY = 7, -1001234567890, -1001111111111, -1002222222222
BASE = {"bot_token": "1:" + "a" * 35, "owner_chat_id": OWNER, "chat_id": HOME}


def cfg_with(*chats):
    projects = [{"name": f"P{i}", "chat_id": c} for i, c in enumerate(chats)]
    return C.from_dict(dict(BASE, projects=projects))


def make(bot, *chats):
    cfg = cfg_with(*chats)
    return TopicMap(state_dir() / "topics.json", bot, HOME, cfg=cfg), cfg


def stored(name):
    return json.loads((state_dir() / name).read_text(encoding="utf-8"))


def test_same_thread_number_in_two_chats_maps_to_two_sessions(bot):
    topics, _ = make(bot, ORBIT)
    topics.ensure("home-session", "Home", thread=5)
    topics.ensure("orbit-session", "Orbit", thread=5, chat=ORBIT)
    assert topics.session_at(HOME, 5) == "home-session"
    assert topics.session_at(ORBIT, 5) == "orbit-session"
    assert topics.session_for_thread(5) == "home-session"  # the old lookup is home only
    assert topics.session_at(PANTRY, 5) is None  # an unconfigured chat is nobody's
    assert topics.session_at(ORBIT, None) is None
    assert topics.place_of("home-session") == P.Place(HOME, 5, home=True)
    assert topics.place_of("orbit-session") == P.Place(ORBIT, 5, home=False)
    assert topics.place_of("home-session").key != topics.place_of("orbit-session").key
    assert set(stored("topics.json")) == {"home-session"}  # older code never sees the project session
    assert set(stored("topics-projects.json")) == {"orbit-session"}


def test_a_project_thread_is_not_a_home_thread(bot):
    topics, _ = make(bot, ORBIT)
    topics.ensure("orbit-session", "Orbit", thread=9, chat=ORBIT)
    assert topics.session_for_thread(9) is None
    assert topics.thread_of("orbit-session") is None
    assert topics.session_at(HOME, 9) is None
    assert topics.session_at(ORBIT, 9) == "orbit-session"


def test_new_topic_is_created_in_the_requested_project_chat(bot):
    topics, _ = make(bot, ORBIT)
    thread = topics.ensure("s", "Orbit: game", chat=ORBIT)
    assert bot.methods("createForumTopic") == [{"chat_id": ORBIT, "name": "Orbit: game"}]
    assert topics.ensure("s", "Orbit: game", chat=ORBIT) == thread  # reused, not created again
    assert topics.ensure("s", "Orbit: game") == thread  # a later call without chat finds it too
    assert len(bot.methods("createForumTopic")) == 1
    assert stored("topics-projects.json")["s"] == {"chat": ORBIT, "thread": thread, "name": "Orbit: game",
                                                   "visible_name": "Orbit: game", "project": "P0", "why": "explicit"}
    assert not (state_dir() / "topics.json").exists()


def test_rename_edits_the_topic_in_its_own_chat(bot):
    topics, _ = make(bot, ORBIT)
    thread = topics.ensure("s", "Old", chat=ORBIT)
    assert topics.ensure("s", "New") == thread
    assert bot.methods("editForumTopic") == [{"chat_id": ORBIT, "message_thread_id": thread, "name": "New"}]
    assert stored("topics-projects.json")["s"]["name"] == "New"


def test_an_existing_topic_is_not_moved_by_ensure(bot):
    topics, _ = make(bot, ORBIT)
    thread = topics.ensure("s", "Title")
    assert topics.ensure("s", "Title", chat=ORBIT) == thread
    assert topics.place_of("s") == P.Place(HOME, thread, home=True)
    assert bot.methods("createForumTopic") == [{"chat_id": HOME, "name": "Title"}]


def test_an_unconfigured_or_home_chat_means_home(bot):
    topics, _ = make(bot, ORBIT)
    topics.ensure("a", "A", chat=PANTRY)
    topics.ensure("b", "B", chat=HOME)
    assert [p["chat_id"] for p in bot.methods("createForumTopic")] == [HOME, HOME]
    assert not (state_dir() / "topics-projects.json").exists()


def test_the_resolver_decides_a_new_session(bot, monkeypatch):
    topics, cfg = make(bot, ORBIT)
    topics.ensure("a", "Orbit: a")
    assert topics.place_of("a").home  # no rule matches: home
    monkeypatch.setattr(T, "resolve", lambda c, s, title: (P.project_by_chat(c, ORBIT), "path"))
    topics.ensure("b", "Orbit: b")
    assert topics.place_of("b").chat == ORBIT
    assert topics.ensure("a", "Orbit: a") and topics.place_of("a").home  # sticky: never re-resolved


def test_entry_of_a_removed_group_has_no_place_and_ensure_rehomes_it(bot):
    topics, _ = make(bot, ORBIT)
    old = topics.ensure("s", "Title", chat=ORBIT)
    topics.ensure("other", "Other", chat=ORBIT)
    gone = TopicMap(state_dir() / "topics.json", bot, HOME, cfg=cfg_with(PANTRY))  # Orbit left the settings
    assert gone.place_of("s") is None
    assert gone.session_at(ORBIT, old) is None
    assert gone.all_places() == {}
    thread = gone.ensure("s", "Title")
    assert gone.place_of("s") == P.Place(HOME, thread, home=True)
    assert bot.methods("createForumTopic")[-1] == {"chat_id": HOME, "name": "Title"}
    assert set(stored("topics-projects.json")) == {"other"}
    # a map built without any settings sees the home topics alone
    plain = TopicMap(state_dir() / "topics.json", bot, HOME)
    assert plain.place_of("other") is None and plain.session_at(ORBIT, old) is None


def test_thread_not_found_from_another_chat_does_not_forget(bot):
    topics, _ = make(bot, ORBIT, PANTRY)
    topics.ensure("home-session", "H")
    topics.ensure("orbit-session", "W", chat=ORBIT)
    topics.forget("orbit-session")  # the home chat reported a missing thread: not this session's chat
    topics.forget("orbit-session", chat=PANTRY)
    topics.forget("home-session", chat=ORBIT)
    assert topics.place_of("orbit-session") and topics.place_of("home-session")
    topics.forget("orbit-session", chat=ORBIT)
    assert topics.place_of("orbit-session") is None
    assert topics.place_of("home-session")
    topics.forget("home-session")
    assert topics.place_of("home-session") is None


def test_forgotten_session_gets_a_new_topic_in_its_chat(bot):
    topics, _ = make(bot, ORBIT)
    first = topics.ensure("s", "T", chat=ORBIT)
    topics.forget("s", chat=ORBIT)
    assert topics.ensure("s", "T", chat=ORBIT) != first
    assert [p["chat_id"] for p in bot.methods("createForumTopic")] == [ORBIT, ORBIT]


def test_failed_create_in_a_project_chat_keeps_nothing(bot):
    topics, _ = make(bot, ORBIT)
    bot.fail["createForumTopic"] = "Bad Request: not enough rights to create a topic"
    assert topics.ensure("s", "T", chat=ORBIT) is None
    assert topics.place_of("s") is None


def test_all_places_lists_both_files(bot):
    topics, _ = make(bot, ORBIT)
    h = topics.ensure("h", "Home")
    w = topics.ensure("w", "Orbit", chat=ORBIT)
    places = topics.all_places()
    assert set(places) == {"h", "w"}
    assert (places["h"]["chat"], places["h"]["thread"], places["h"]["home"]) == (HOME, h, True)
    assert (places["w"]["chat"], places["w"]["thread"], places["w"]["home"]) == (ORBIT, w, False)
    assert places["w"]["name"] == "Orbit"
    assert set(topics.all()) == {"h"}  # the old listing is unchanged


def test_project_store_keeps_the_newest_300(bot):
    topics, _ = make(bot, ORBIT)
    for i in range(305):
        topics.ensure(f"s{i}", f"T{i}", thread=1000 + i, chat=ORBIT)
    kept = stored("topics-projects.json")
    assert len(kept) == T.MAX_ENTRIES == 300
    assert "s304" in kept and "s4" not in kept


def test_team_map_has_no_project_store(bot):
    team = TopicMap(state_dir() / "topics-team.json", bot, -1001999999999, cfg=cfg_with(ORBIT))
    team.ensure("s", "T", chat=ORBIT)
    assert bot.methods("createForumTopic")[0]["chat_id"] == -1001999999999
    assert not (state_dir() / "topics-projects.json").exists()
    assert team.place_of("s") == P.Place(-1001999999999, 101, home=True)


def test_helpers_follow_the_session(bot):
    topics, cfg = make(bot, ORBIT)
    h = topics.ensure("h", "Home")
    w = topics.ensure("w", "Orbit", chat=ORBIT)
    assert T.place(cfg, "h") == P.Place(HOME, h, home=True)
    assert T.place(cfg, "w") == P.Place(ORBIT, w, home=False)
    assert T.place(cfg, "none") is None
    assert (T.chat_of(cfg, "h"), T.chat_of(cfg, "w"), T.chat_of(cfg, "none")) == (HOME, ORBIT, HOME)
    private = C.Config(owner_chat_id=OWNER)  # private mode: the owner's chat is home
    assert T.chat_of(private, "w") == OWNER


def test_corrupt_project_store_reads_as_empty(bot):
    topics, _ = make(bot, ORBIT)
    (state_dir()).mkdir(parents=True, exist_ok=True)
    (state_dir() / "topics-projects.json").write_text("{nope", encoding="utf-8")
    assert topics.place_of("s") is None and topics.all_places() == {}
    topics.ensure("s", "T", chat=ORBIT)
    assert topics.place_of("s").chat == ORBIT


def test_a_map_without_settings_never_writes_the_project_store(bot):
    topics, _ = make(bot, ORBIT)
    topics.ensure("w", "Orbit", chat=ORBIT)
    topics.ensure("stale", "Gone", chat=ORBIT)
    path = state_dir() / "topics-projects.json"
    before = path.read_bytes()
    plain = TopicMap(state_dir() / "topics.json", bot, HOME)  # built without cfg=, like many callers
    calls = len(bot.calls)
    assert plain.ensure("w", "Orbit renamed") is None  # fails closed: no topic is made or renamed
    assert plain.ensure("w", "Orbit", thread=77) is None
    plain.forget("w")
    plain.forget("w", chat=ORBIT)
    plain.forget("stale", chat=ORBIT)
    plain.ensure("home", "Home")  # home sessions work as before
    assert path.read_bytes() == before
    assert len(bot.calls) == calls + 1 and bot.calls[-1][1]["chat_id"] == HOME
    assert topics.place_of("w") and topics.place_of("stale")


def test_session_at_needs_a_whole_number_thread(bot):
    topics, _ = make(bot, ORBIT)
    topics.ensure("w", "W", thread=5, chat=ORBIT)
    for bad in ("5", 5.0, True, [5]):
        assert topics.session_at(ORBIT, bad) is None
    assert topics.session_at(ORBIT, 5) == "w"


def test_updating_an_old_entry_counts_as_recent(bot):
    topics, _ = make(bot, ORBIT)
    for i in range(T.MAX_ENTRIES):
        topics.ensure(f"s{i}", f"T{i}", thread=1000 + i, chat=ORBIT)
    topics.ensure("s0", "T0", thread=5000, chat=ORBIT)  # the oldest entry is updated...
    topics.ensure("new", "New", thread=6000, chat=ORBIT)  # ...so the next one pushes out s1, not s0
    kept = stored("topics-projects.json")
    assert len(kept) == T.MAX_ENTRIES
    assert "s0" in kept and "s1" not in kept and kept["s0"]["thread"] == 5000
