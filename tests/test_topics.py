import json

from ctb.topics import TopicMap


def make(tmp_path, bot):
    return TopicMap(tmp_path / "state" / "topics.json", bot, 42)


def test_creates_once_and_reuses(tmp_path, bot):
    t = make(tmp_path, bot)
    first = t.ensure("s1", "Project: task")
    assert first == 101
    assert t.ensure("s1", "Project: task") == 101
    assert len(bot.methods("createForumTopic")) == 1
    assert bot.methods("editForumTopic") == []


def test_rename_when_title_changes(tmp_path, bot):
    t = make(tmp_path, bot)
    thread = t.ensure("s1", "Old name")
    assert t.ensure("s1", "New name") == thread
    edit = bot.methods("editForumTopic")
    assert edit == [{"chat_id": 42, "message_thread_id": thread, "name": "New name"}]
    assert t.ensure("s1", "New name") == thread
    assert len(bot.methods("editForumTopic")) == 1  # no second rename
    stored = json.loads((tmp_path / "state" / "topics.json").read_text())
    assert stored["s1"] == {"thread": thread, "name": "New name", "visible_name": "New name"}


def test_failed_rename_keeps_working_thread(tmp_path, bot):
    t = make(tmp_path, bot)
    thread = t.ensure("s1", "A")
    bot.fail["editForumTopic"] = "Too Many Requests"
    assert t.ensure("s1", "B") == thread


def test_threaded_mode_off_means_main_chat(tmp_path, bot):
    bot.fail["createForumTopic"] = "the chat is not a forum"
    assert make(tmp_path, bot).ensure("s1", "A") is None


def test_name_is_capped_and_default(tmp_path, bot):
    t = make(tmp_path, bot)
    t.ensure("s1", "x" * 500)
    t.ensure("s2", "")
    names = [p["name"] for p in bot.methods("createForumTopic")]
    assert len(names[0]) == 120 and names[1] == "Claude"


def test_reverse_lookup_and_forget(tmp_path, bot):
    t = make(tmp_path, bot)
    thread = t.ensure("s1", "A")
    assert t.session_for_thread(thread) == "s1"
    assert t.session_for_thread(999) is None and t.session_for_thread(None) is None
    t.forget("s1")
    assert t.session_for_thread(thread) is None
    assert t.ensure("s1", "A") != thread  # a new topic is created


def test_state_file_is_private(tmp_path, bot):
    make(tmp_path, bot).ensure("s1", "A")
    path = tmp_path / "state" / "topics.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
