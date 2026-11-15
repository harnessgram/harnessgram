"""A realistic state directory for the CPU benchmarks: topics, session transcripts, cards and an owner ledger.

Everything is written under the folders the test sandbox already points at (CTB_STATE_DIR and the Claude
projects folder), so no real configuration or data is ever read.
"""
import json
import time

from ctb.config import state_dir, write_private

SESSIONS = 12
PROSE = ("Reviewed the retry path and wrote down what changed in the sync step; next is the follow-up on the "
         "list view, then the migration notes. See https://example.com/docs/page?id=7 for the background. ")


def _session_id(i: int) -> str:
    return "%08x-aaaa-bbbb-cccc-%012x" % (0x1000 + i, 0xabc000 + i)


def write_topics(sessions: int = SESSIONS) -> list:
    ids = [_session_id(i) for i in range(sessions)]
    write_private(state_dir() / "topics.json",
                  json.dumps({s: {"thread": 20 + i, "name": f"Topic number {i}"} for i, s in enumerate(ids)}))
    return ids


def write_transcript(projects, session: str, tasks: int = 6) -> None:
    """A small transcript with a task list, so the task and usage readers have something to parse."""
    folder = projects / "proj"
    folder.mkdir(parents=True, exist_ok=True)
    lines = []
    for t in range(tasks):
        lines.append(json.dumps({"type": "assistant", "message": {"model": "claude-opus-5", "content": [
            {"type": "tool_use", "id": f"tu{t}", "name": "TaskCreate", "input": {"subject": f"Task {t}"}}],
            "usage": {"input_tokens": 100, "output_tokens": 50}}}))
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"tu{t}", "content": f"Task #{t + 1} created"}]}}))
    lines.append(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "up", "name": "TaskUpdate", "input": {"taskId": "1", "status": "in_progress"}}]}}))
    (folder / f"{session}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_cards(sessions: list, count: int, now: float) -> None:
    cards = {}
    for n in range(count):
        ident = "%016x" % (0xc0de00000000 + n)
        session = sessions[n % len(sessions)]
        kind = ("progress", "choice", "link", "numbers", "ask")[n % 5]
        rec = {"kind": kind, "session": session, "title": f"Card {n}: {PROSE[:60]}", "detail": f"{PROSE}Card {n}. {PROSE}",
               "chat": 7, "thread": 20 + n % len(sessions), "created": now - 3600 - n, "updated": now - 60 - n,
               "state": "open" if n % 3 else "closed", "closed": now - 30}
        if kind == "progress":
            rec.update(steps=["Read the code", "Write the change", "Run the tests"], at=n % 3, percent=None)
        elif kind == "choice":
            rec.update(options=["Ship it", "Hold on", "Ask me later"])
        elif kind == "link":
            rec.update(url="https://example.com/page", label="Open")
        elif kind == "numbers":
            rec.update(figures=[{"label": "Tests", "value": "412"}, {"label": "Failing", "value": "0"}])
        cards[ident] = rec
    write_private(state_dir() / "cards.json", json.dumps(cards))


def write_ledger(folder, items: int, now: float, projects: int = 6) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    per = {p: [] for p in range(projects)}
    for n in range(items):
        notes = [{"at": now - 7200 + k, "text": f"{PROSE}Note {n}/{k}. " + ("token: hunter2xyz9 " if k == 3 else "")} for k in range(12)]
        per[n % projects].append({"id": n + 1, "text": f"Request {n}: {PROSE[:80]}", "status": "open" if n % 4 else "done",
                                  "created": now - 86400, "updated": now - 300 - n,
                                  "wait": "owner" if n % 5 == 0 else "team", "notes": notes,
                                  "topic": f"topic-{n % 40}"})
    for p, rows in per.items():
        (folder / f"project-{p}.json").write_text(json.dumps({"items": rows}), encoding="utf-8")


def build(tmp_path, cards: int = 300, items: int = 300, now: float = None):
    """Write the fixture and return (config, now)."""
    from ctb.config import Config
    now = time.time() if now is None else now
    sessions = write_topics()
    from ctb import status
    projects = status.projects_dir()
    for s in sessions:
        write_transcript(projects, s)
    write_cards(sessions, cards, now)
    ledger = tmp_path / "ledger"
    write_ledger(ledger, items, now)
    cfg = Config(bot_token="123456:" + "A" * 35, owner_chat_id=7, target_language="he")
    cfg.brief = {"ledger_dir": str(ledger)}
    return cfg, now, projects
