"""An idle main conversation gets its messages while background agents keep the topic "busy"."""
import json
import threading

from conftest import FakeBot
from ctb import config as C, hooks as H, inbox as I, topics as T
from ctb.config import state_dir, write_private


def cfg():
    return C.from_dict({"bot_token": "1:" + "x" * 35, "owner_chat_id": 5})


def test_a_background_agents_step_keeps_the_main_conversation_idle(monkeypatch):
    sid, c, bot = "sess-bg", cfg(), FakeBot()
    write_private(state_dir() / "topics.json", json.dumps({sid: {"thread": 7, "name": "x"}}))
    monkeypatch.setattr(H, "typing", lambda *a: None)
    monkeypatch.setattr(H, "append_progress", lambda *a: None)
    T.set_state(c, sid, "done", bot=bot)
    T.set_state(c, sid, "busy", bot=bot, optional=True)  # the progress card of an agent's tool call
    H.hook_post(json.dumps({"session_id": sid, "agent_id": "a1", "tool_name": "Bash"}))
    assert I.session_busy(sid) and not I.main_working(sid)  # commands still see a busy session

    H.hook_post(json.dumps({"session_id": sid, "tool_name": "Bash"}))  # the main conversation moved on
    assert I.main_working(sid)


def test_a_new_prompt_ends_the_idle_mark():
    sid, c, bot = "sess-bg2", cfg(), FakeBot()
    write_private(state_dir() / "topics.json", json.dumps({sid: {"thread": 7, "name": "x"}}))
    T.set_state(c, sid, "done", bot=bot)
    T.set_state(c, sid, "busy", bot=bot, optional=True)
    assert not I.main_working(sid)
    T.main_turn_active(sid)
    assert I.main_working(sid)


def test_a_held_message_is_nudged_once_the_turn_is_over(monkeypatch):
    """"Deliver at the end of the turn" has nothing to wait for when the turn has already ended."""
    sid, c, bot = "sess-bg3", cfg(), FakeBot()
    write_private(state_dir() / "topics.json", json.dumps({sid: {"thread": 7, "name": "x"}}))
    T.set_state(c, sid, "done", bot=bot)
    nudged = []
    monkeypatch.setattr(I, "_reopen_or_nudge", lambda inbox, session: nudged.append(session))
    monkeypatch.setattr(I.delivery, "default_mode", lambda: "end")
    monkeypatch.setattr(I, "limit_notice", lambda *a: False)
    box = I.default_inbox()
    I._queue_owner(c, bot, box, {"date": 1, "message_id": 2, "message_thread_id": 7}, sid, "later")
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and thread.daemon:
            thread.join(1)
    assert nudged == [sid]
