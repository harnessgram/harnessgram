import json
from conftest import GOOD_TOKEN
from ctb.config import AGENT_NOTICES, Config, set_setting, state_dir
from ctb.mirror import run_lifecycle, subagent_text
from ctb.topics import TopicMap


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=123)


def test_empty_background_agent_notice_text_is_empty_and_the_card_still_counts_the_agent(tmp_path, bot):
    c = cfg()
    topics = TopicMap(tmp_path / "state" / "topics.json", bot, c.owner_chat_id)
    topics.ensure("sess-1", "Session Title")
    bot.calls.clear()

    # 1. No agent metadata and no description -> subagent_text returns ""
    assert subagent_text(c, agent_type="Coder", description="") == ""
    assert subagent_text(c, agent_type="", description="") == ""

    # 2. the agents card still counts an agent without details (it must not stay "running")
    result = run_lifecycle(c, "sess-1", "subagent-stop", agent_type="Coder", description="", bot=bot)
    assert result == 0
    assert len(bot.methods("sendMessage")) == 1 and "1 ✅" in bot.methods("sendMessage")[0]["text"]

    # 3. With a description the card keeps its name
    result = run_lifecycle(c, "sess-1", "subagent-stop", agent_type="Coder", description="audit security", bot=bot,
                           agent_id="a2")
    assert result == 0
    assert "audit security" in json.dumps(json.loads((state_dir() / "agents-cards" / "sess-1.json").read_text()))

    # 4. With metadata file but no description argument -> sends notice
    transcript = tmp_path / "agent-1.jsonl"
    transcript.write_text("")
    meta = tmp_path / "agent-1.meta.json"
    meta.write_text(json.dumps({"description": "task from meta", "model": "haiku"}))

    text = subagent_text(c, agent_type="Coder", description="", agent_transcript=str(transcript))
    assert "task from meta" in text
    assert "(Haiku)" in text
