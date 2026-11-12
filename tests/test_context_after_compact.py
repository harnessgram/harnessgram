"""After a compaction the context size is the new one, never the pre-compaction usage record."""
import json

from ctb import usage
from ctb.config import Config
from ctb.topics import TopicMap
from ctb.config import state_dir
from ctb.wake import refresh_status

OWNER = 123456789


def _assistant(tokens):
    return {"type": "assistant", "message": {"role": "assistant", "model": "claude-x",
                                              "usage": {"input_tokens": tokens}}}


def _boundary(post=None):
    meta = {"trigger": "manual", "preTokens": 436_696}
    if post:
        meta["postTokens"] = post
    return {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted",
            "compactMetadata": meta}


def _summary(text="x" * 4000):
    return {"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": text}}


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(path)


def test_context_tokens_ignores_usage_from_before_the_compaction(tmp_path):
    p = _write(tmp_path / "t.jsonl", [_assistant(436_000), _boundary(post=14_836), _summary()])
    assert usage.context_tokens(p) == (14_836, "")


def test_context_tokens_estimates_from_the_summary_without_post_tokens(tmp_path):
    p = _write(tmp_path / "t.jsonl", [_assistant(436_000), _boundary(), _summary("y" * 8000)])
    assert usage.context_tokens(p) == (2000, "")


def test_context_tokens_next_reply_wins_over_the_boundary(tmp_path):
    p = _write(tmp_path / "t.jsonl", [_assistant(436_000), _boundary(post=14_836), _summary(), _assistant(21_000)])
    assert usage.context_tokens(p) == (21_000, "claude-x")


def test_context_tokens_text_mentioning_the_boundary_is_not_one(tmp_path):
    talk = {"type": "assistant", "message": {"role": "assistant", "usage": {"input_tokens": 90_000},
                                             "content": "the compact_boundary record"}}
    assert usage.context_tokens(_write(tmp_path / "t.jsonl", [talk])) == (90_000, "")


def test_refresh_status_waits_for_the_compaction_and_never_shows_the_old_size(tmp_path, bot):
    c = Config(bot_token="123456:" + "A" * 35, owner_chat_id=OWNER)
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("sess-w", "W")
    t = tmp_path / "t.jsonl"
    _write(t, [_assistant(436_000)])  # the hook ran before the compaction reached the transcript
    naps = []

    def nap(sec):
        naps.append(sec)
        if len(naps) == 2:
            _write(t, [_assistant(436_000), _boundary(post=14_836), _summary()])

    assert refresh_status("sess-w", str(t), cfg=c, bot=bot, sleep=nap)
    sent = bot.methods("sendMessage")[-1]
    assert "15 אלף" in sent["text"] or "15k" in sent["text"]
    assert "436" not in json.dumps(sent, ensure_ascii=False)


def test_refresh_status_drops_the_old_number_if_the_compaction_never_shows(tmp_path, bot):
    c = Config(bot_token="123456:" + "A" * 35, owner_chat_id=OWNER)
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("sess-n", "N")
    t = tmp_path / "t.jsonl"
    _write(t, [_assistant(436_000)])
    assert refresh_status("sess-n", str(t), cfg=c, bot=bot, sleep=lambda s: None)
    assert "436" not in json.dumps(bot.methods("sendMessage")[-1], ensure_ascii=False)


def test_hook_start_spawns_refresh_instead_of_waiting_inline(monkeypatch):
    from ctb import wake
    calls = []
    monkeypatch.setattr(wake, "load_config", lambda: Config(bot_token="123456:" + "A" * 35, owner_chat_id=OWNER))
    monkeypatch.setattr(wake, "spawn", lambda args: calls.append(args))
    monkeypatch.setattr(wake, "refresh_status", lambda *a, **k: calls.append("inline"))
    monkeypatch.setattr(wake.compact, "mark_done", lambda s: False)
    wake.hook_start(json.dumps({"session_id": "s", "source": "compact", "transcript_path": "/x/s.jsonl"}))
    assert "inline" not in calls
    assert ["refresh-status", "--session=s", "--transcript=/x/s.jsonl", "--source=compact"] in calls


def test_refresh_status_command_runs_it_only_for_a_projects_transcript(tmp_path, monkeypatch):
    from ctb import cli, wake
    home = tmp_path / "home"
    good = home / "projects" / "p" / "s.jsonl"
    good.parent.mkdir(parents=True)
    good.write_text("")
    monkeypatch.setenv("CTB_CLAUDE_HOME", str(home))
    monkeypatch.setenv("CTB_PROFILES_DIR", str(tmp_path / "none"))
    seen = []
    monkeypatch.setattr(wake, "refresh_status", lambda s, t, **k: seen.append((s, t, k.get("source"))) or True)
    assert cli.main(["refresh-status", "--session=s", f"--transcript={good}"]) == 0
    assert cli.main(["refresh-status", "--session=s", f"--transcript={tmp_path / 'evil.jsonl'}"]) == 1
    assert cli.main(["refresh-status", "--session=s", f"--transcript={good.parent}/../../x.txt"]) == 1
    assert seen == [("s", str(good), "compact")]


def test_clear_leaves_the_number_out_until_the_first_reply(tmp_path, bot):
    c = Config(bot_token="123456:" + "A" * 35, owner_chat_id=OWNER)
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("sess-c", "C")
    t = tmp_path / "t.jsonl"
    _write(t, [_assistant(436_000)])  # the same session's file still holds the old conversation
    assert refresh_status("sess-c", str(t), cfg=c, bot=bot, sleep=lambda s: None, source="clear")
    assert "436" not in json.dumps(bot.methods("sendMessage")[-1], ensure_ascii=False)


def test_new_topic_startup_gets_a_card_and_resume_shows_its_size(tmp_path, bot):
    c = Config(bot_token="123456:" + "A" * 35, owner_chat_id=OWNER)
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("sess-s", "S")
    t = tmp_path / "t.jsonl"
    _write(t, [])
    assert refresh_status("sess-s", str(t), cfg=c, bot=bot, sleep=lambda s: None, source="startup")
    assert len(bot.methods("sendMessage")) == 1  # a card exists for the new topic, with no number
    _write(t, [_assistant(120_000)])
    assert refresh_status("sess-s", str(t), cfg=c, bot=bot, sleep=lambda s: None, source="resume")
    assert any("120" in json.dumps(m, ensure_ascii=False) for m in bot.methods("sendMessage"))


def test_hook_start_refreshes_for_clear_and_startup(monkeypatch):
    from ctb import wake
    calls = []
    monkeypatch.setattr(wake, "load_config", lambda: Config(bot_token="123456:" + "A" * 35, owner_chat_id=OWNER))
    monkeypatch.setattr(wake, "spawn", lambda args: calls.append(args))
    for src in ("clear", "startup", "resume"):
        wake.hook_start(json.dumps({"session_id": "s", "source": src, "transcript_path": "/x/s.jsonl"}))
        assert ["refresh-status", "--session=s", "--transcript=/x/s.jsonl", f"--source={src}"] in calls
