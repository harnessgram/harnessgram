"""Two sessions whose rows the MCP tools must keep apart, and a real stdio MCP server child that serves them.

Run as a program, this is `create_server(...).run(transport="stdio")` bound by the real `decisions.caller_session`
to the Claude Code session named in its environment. It reads only the state below and the test's sandboxed stores
(the environment it inherits), never the real config, and it never wakes a session.
"""
import json
import os

OWN = "0123abcd-1111-2222-3333-444455556666"
FOREIGN = "0123abcd-9999-8888-7777-666655554444"  # the same 8-character short id as OWN
SECRET = "beta-private"  # only in rows the OWN caller must never see
PROJECTS = {OWN: "proj-a", FOREIGN: "proj-b"}
# account-wide parts of the state: they name no session, so every caller may see them
ACCOUNT_WIDE = {"quota": [{"account": "main", "weekly": 42}], "radio": {"on": False},
                "hook_health": {"ok": True, "events": []}}


def _foreign_row(key):
    return {"id": f"{SECRET}-{key}", "session": FOREIGN, "session_id": FOREIGN, "project": "Proj-B",
            "folder": f"/work/{SECRET}", "name": SECRET, "title": SECRET, "text": SECRET, "status": "open",
            "kind": "ask"}


class CallerState(dict):
    """The shape of panel.build_state(full_ids=True). Every key not set here, now or for a tool added later, holds
    only a foreign session's row, so a tool that hands a state list on unscoped shows SECRET and FOREIGN."""

    def __missing__(self, key):
        return [_foreign_row(key)]

    def get(self, key, default=None):
        return self[key]


def caller_state():
    state = CallerState(ACCOUNT_WIDE)
    state["sessions"] = [
        {"id": OWN[:8], "session_id": OWN, "name": "Alpha", "folder": "/work/alpha", "state": "idle", "age": 5,
         "waiting": []},
        {"id": FOREIGN[:8], "session_id": FOREIGN, "name": SECRET, "folder": f"/work/{SECRET}", "state": "working",
         "age": 3, "waiting": ["owner"]}]
    state["cards"] = [
        {"id": "card-a", "kind": "progress", "title": "alpha card", "session": OWN[:8], "session_id": OWN},
        {"id": f"card-{SECRET}", "kind": "ask", "title": SECRET, "session": FOREIGN[:8], "session_id": FOREIGN},
        {"id": "card-none", "kind": "progress", "title": SECRET, "session": "", "session_id": ""}]
    state["requests"] = [
        {"project": "Proj-A", "id": 1, "title": "alpha own item", "status": "open"},
        {"project": "Proj-A", "id": 2, "title": "alpha item by a teammate", "status": "open", "session": FOREIGN},
        {"project": "Proj-B", "id": 1, "title": SECRET, "status": "open"},
        {"project": "Proj-B", "id": 3, "title": "item made by alpha", "status": "open", "session": OWN},
        {"project": "Proj-C", "id": 4, "title": SECRET, "status": "open", "session": FOREIGN}]
    return state


def leaks(text: str) -> list:
    """What of the foreign session a tool result shows."""
    return [word for word in (FOREIGN, SECRET) if word in text]


class FileInbox:
    """Records each queued message as one JSON line, so the test can see what the child queued."""

    def __init__(self, path):
        self.path = path

    def put(self, session, item, **_):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"session": session, "item": item}) + "\n")


def main():
    from ctb import decisions, mcp_server
    from ctb.config import Config

    decisions.announce = lambda cfg, bot, inbox, rec: None  # a test child never wakes a session or edits a topic
    work = os.environ["MCP_CALLER_TEST_DIR"]
    tools = mcp_server.BridgeTools(
        Config(bot_token="123456:" + "A" * 35, owner_chat_id=7), state_builder=caller_state,
        inbox=FileInbox(os.path.join(work, "inbox.jsonl")), rate_path=os.path.join(work, "rate.json"),
        transcript=lambda session: None, project_of=PROJECTS.get, bind_caller=True)
    mcp_server.create_server(tools).run(transport="stdio")


if __name__ == "__main__":
    main()
