"""Proof that a background-agent event belongs to the session that reports it, before any notice is shown.

Claude Code also fires SubagentStop for its own internal helpers at the end of a turn: no agent type, no
description and no transcript. Shown as cards they put "background agent finished" in the topic of a session that
never started an agent, which reads as a notice from another session. A real agent started by
the session has its own transcript in that session's folder:

    <projects dir>/<session>.jsonl                          the session's transcript (transcript_path)
    <projects dir>/<session>/subagents/agent-<id>.jsonl     the agent's transcript (agent_transcript_path)

So an agent event is shown only when the session's transcript is named after the session and the agent's
transcript exists under that session's own folder. Anything else (no id, a transcript of another session, no
transcript at all) is dropped, never guessed."""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, Optional

AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
START_WAIT = 15.0  # seconds a start waits for the agent's transcript to be written
STOP_WAIT = 3.0  # seconds a stop waits (the transcript normally exists long before)
POLL = 0.25


def agent_transcript(session: str, agent_id: str, transcript: str, given: str = "") -> Optional[Path]:
    """Where the agent's transcript must be for the agent to be this session's own; None when it cannot be."""
    if not session or not AGENT_ID.fullmatch(agent_id or "") or not transcript:
        return None
    main = Path(transcript)
    if main.suffix != ".jsonl" or main.stem != session:
        return None  # the hook's session id and its transcript disagree: owner unknown
    path = Path(given) if given else main.parent / session / "subagents" / f"agent-{agent_id}.jsonl"
    if path.name != f"agent-{agent_id}.jsonl" or path.parent.name != "subagents" or path.parent.parent.name != session:
        return None  # the transcript lives in another session's folder
    return path


def proven(session: str, agent_id: str, transcript: str, given: str = "", wait: float = 0.0,
           sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> bool:
    """True when the agent is proven to be `session`'s own (its transcript appears within `wait` seconds)."""
    path = agent_transcript(session, agent_id, transcript, given)
    if path is None:
        return False
    deadline = clock() + max(0.0, wait)
    while True:
        try:
            if path.is_file() and path.stat().st_size > 0:
                return True
        except OSError:
            pass
        if clock() >= deadline:
            return False
        sleep(POLL)
