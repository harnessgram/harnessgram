from conftest import GOOD_TOKEN
from ctb import hooks as H, progress as P
from ctb.config import AGENT_NOTICES, Config, set_setting, state_dir


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, target_language="en")


def test_background_agent_steps_stay_off_the_card_while_agent_notices_are_off(monkeypatch):
    monkeypatch.setattr(H, "spawn", lambda args, text=None: None)
    set_setting(AGENT_NOTICES, False)
    P.append_progress("q", {"tool_name": "Bash", "agent_id": "a1", "tool_input": {"description": "agent step"}}, cfg())
    assert not (state_dir() / "progress" / "q.jsonl").exists()
    P.append_progress("q", {"tool_name": "Bash", "tool_input": {"description": "own step"}}, cfg())
    assert len((state_dir() / "progress" / "q.jsonl").read_text().splitlines()) == 1


def test_background_agent_steps_show_when_agent_notices_are_on(monkeypatch):
    monkeypatch.setattr(H, "spawn", lambda args, text=None: None)
    set_setting(AGENT_NOTICES, True)
    P.append_progress("r", {"tool_name": "Bash", "agent_id": "a1", "tool_input": {"description": "agent step"}}, cfg())
    assert len((state_dir() / "progress" / "r.jsonl").read_text().splitlines()) == 1
