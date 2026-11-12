"""Background-agent notices go only to the topic of the session that started the agent.

Regression (2026-11-10): Claude Code fires SubagentStop for its own internal helpers at the end of every
turn (no agent type, no description, no transcript). Each one became a "background agent finished" card in the
topic of a session that never started an agent, which the owner read as a notice from another session."""
from __future__ import annotations

import json

from conftest import GOOD_TOKEN
from ctb import agent_owner as O, cli, hooks as H, mirror as M
from ctb.config import Config

SID = "11111111-aaaa-4bbb-8ccc-000000000001"
OTHER = "22222222-aaaa-4bbb-8ccc-000000000002"


def layout(tmp_path, session=SID, agent="a1b2c3", write=True):
    proj = tmp_path / "projects" / "-Users-me-work"
    main = proj / f"{session}.jsonl"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text('{"type":"user"}\n', encoding="utf-8")
    sub = proj / session / "subagents" / f"agent-{agent}.jsonl"
    if write:
        sub.parent.mkdir(parents=True, exist_ok=True)
        sub.write_text('{"type":"user"}\n', encoding="utf-8")
    return str(main), str(sub)


def test_real_agent_of_the_session_is_proven(tmp_path):
    main, sub = layout(tmp_path)
    assert O.proven(SID, "a1b2c3", main, sub)
    assert O.proven(SID, "a1b2c3", main)  # SubagentStart may not carry the agent's path: it is derived


def test_internal_helper_without_transcript_is_dropped(tmp_path):
    main, sub = layout(tmp_path, write=False)
    assert not O.proven(SID, "a1b2c3", main, sub)
    assert not O.proven(SID, "", main)  # no id: never "the oldest running agent"


def test_agent_of_another_session_is_dropped(tmp_path):
    main, _ = layout(tmp_path)
    _, theirs = layout(tmp_path, session=OTHER, agent="ffee01")
    assert not O.proven(SID, "ffee01", main, theirs)  # its transcript is in the other session's folder
    other_main, _ = layout(tmp_path, session=OTHER, agent="a1b2c3")
    assert not O.proven(SID, "a1b2c3", other_main)  # the hook's session id and its transcript disagree


def test_wrong_file_name_or_bad_id_is_dropped(tmp_path):
    main, sub = layout(tmp_path)
    assert not O.proven(SID, "zzz999", main, sub)
    assert not O.proven(SID, "../x", main)
    assert not O.proven("", "a1b2c3", main, sub)
    assert not O.proven(SID, "a1b2c3", "", sub)


def test_start_waits_for_the_transcript_to_appear(tmp_path):
    main, _ = layout(tmp_path, write=False)
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds
        if now[0] >= 1.0:
            layout(tmp_path)  # the agent writes its first row

    assert O.proven(SID, "a1b2c3", main, wait=5, sleep=sleep, clock=lambda: now[0])
    main2, _ = layout(tmp_path, agent="never1", write=False)
    now[0] = 0.0
    assert not O.proven(SID, "never1", main2, wait=2, sleep=lambda s: now.__setitem__(0, now[0] + s),
                        clock=lambda: now[0])


def _run(monkeypatch, argv):
    calls = []
    monkeypatch.setattr(O, "START_WAIT", 0.0)
    monkeypatch.setattr(O, "STOP_WAIT", 0.0)
    monkeypatch.setattr(M, "run_lifecycle", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(cli.C, "load", lambda: Config())
    assert cli.main(["lifecycle", f"--session={SID}"] + argv) == 0
    return calls


def test_lifecycle_command_drops_unproven_agent_events(tmp_path, monkeypatch):
    main, sub = layout(tmp_path, write=False)
    for kind in ("subagent-start", "subagent-stop"):
        assert _run(monkeypatch, [f"--kind={kind}", "--agent-id=a1b2c3", f"--transcript={main}",
                                  f"--agent-transcript={sub}"]) == []
        assert _run(monkeypatch, [f"--kind={kind}"]) == []  # nothing to prove ownership with


def test_lifecycle_command_shows_proven_agent_events(tmp_path, monkeypatch):
    main, sub = layout(tmp_path)
    for kind in ("subagent-start", "subagent-stop"):
        calls = _run(monkeypatch, [f"--kind={kind}", "--agent-id=a1b2c3", f"--transcript={main}",
                                   f"--agent-transcript={sub}"])
        assert len(calls) == 1 and calls[0][0][1] == SID and calls[0][1]["agent_id"] == "a1b2c3"


def test_other_lifecycle_kinds_need_no_agent_proof(monkeypatch):
    assert len(_run(monkeypatch, ["--kind=precompact"])) == 1
    assert len(_run(monkeypatch, ["--kind=session-end"])) == 1


def test_start_hook_passes_the_transcripts(monkeypatch):
    spawned = []
    monkeypatch.setattr(H, "load_config", lambda: Config(bot_token=GOOD_TOKEN, owner_chat_id=7, chat_id=-1001))
    monkeypatch.setattr(H, "spawn", lambda args: spawned.append(args))
    H.hook_lifecycle(json.dumps({"session_id": SID, "hook_event_name": "SubagentStart", "agent_id": "a1b2c3",
                                 "agent_type": "writer", "transcript_path": f"/p/{SID}.jsonl"}))
    assert f"--transcript=/p/{SID}.jsonl" in spawned[0] and "--agent-id=a1b2c3" in spawned[0]
