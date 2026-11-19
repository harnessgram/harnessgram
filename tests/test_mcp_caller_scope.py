"""Every registered MCP tool, called through the real server stack (loopback HTTP and stdio), serves only its bound
caller: no session row, card or ledger row of another session, and no tool at all without a caller binding."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

import mcp_caller_fixture as fx
from test_mcp_server import Box, FakeStore, _call, _open, ask, decision, real_cfg, signed_headers
from ctb import mcp_server
from ctb.config import write_private


def probes(own_ask: str, foreign_ask: str) -> dict:
    """The calls each registered tool gets, as the OWN caller: own targets and foreign ones."""
    return {
        "list_sessions": [{}],
        "read_replies": [{"session": fx.OWN}, {"session": fx.FOREIGN}, {"session": fx.OWN[:8]}],
        "send_to_session": [{"session": fx.FOREIGN, "text": "hijack"}, {"session": fx.OWN[:8], "text": "hijack"},
                            {"session": fx.OWN, "text": "own"}],
        "status": [{}],
        "ledger_list": [{"project": ""}, {"project": "proj-b"}, {"project": "proj-c"},
                        {"project": "Proj-A", "open_only": False}],
        "cards_list": [{}],
        "answer_routine_ask": [{"id": foreign_ask, "choice": "yes"}, {"id": own_ask, "choice": "yes"}],
    }


def bound_tools(tmp_path, **kw):
    return mcp_server.BridgeTools(real_cfg(), state_builder=fx.caller_state, inbox=Box(), clock=lambda: 1000,
                                  rate_path=tmp_path / "rate.json", transcript=lambda session: None,
                                  project_of=fx.PROJECTS.get, **kw)


def registered(tools) -> set:
    return {tool.name for tool in asyncio.run(mcp_server.create_server(tools).list_tools())}


def test_every_registered_tool_has_an_isolation_probe(tmp_path):
    """A new tool must be added to `probes`, so the end-to-end tests below prove it shows no other session."""
    pytest.importorskip("mcp.server.fastmcp")
    assert set(probes("a", "b")) == registered(bound_tools(tmp_path, http_bound=True)), (
        "a new MCP tool needs a caller-isolation probe in tests/test_mcp_caller_scope.py")


def test_a_facade_bound_to_no_caller_refuses_every_tool_and_cannot_be_served(tmp_path, monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")
    tools = bound_tools(tmp_path)
    tools._state_builder = lambda: pytest.fail("an unbound call must not read bridge state")
    monkeypatch.setattr("ctb.decisions.answer", lambda *a, **k: pytest.fail("an unbound caller reached decisions"))
    for name, calls in probes("ask-a", "ask-b").items():
        for arguments in calls:
            with pytest.raises(mcp_server.MCPError, match="no caller binding"):
                getattr(tools, name)(**arguments)
    assert tools._inbox.items == []
    with pytest.raises(mcp_server.MCPError, match="bound to an authenticated HTTP or stdio caller"):
        mcp_server.create_server(tools)


def test_unbound_read_and_send_report_binding_before_bad_arguments(tmp_path):
    tools = bound_tools(tmp_path)
    with pytest.raises(mcp_server.MCPError, match="no caller binding"):
        tools.read_replies("", limit=0)
    with pytest.raises(mcp_server.MCPError, match="no caller binding"):
        tools.send_to_session("", "")


def test_status_drops_reload_session_ids(tmp_path):
    state = fx.caller_state()
    state["hook_health"] = {
        "ok": True,
        "events": ["Stop"],
        "restored": False,
        "reload": {"running": False, "reloaded": [fx.FOREIGN], "would_reload": [], "skipped": [fx.OWN]},
    }
    tools = mcp_server.BridgeTools(
        real_cfg(), state_builder=lambda: state, inbox=Box(), http_bound=True,
        http_caller=lambda: fx.OWN, project_of=fx.PROJECTS.get,
    )
    result = tools.status()
    assert result["listener_health"] == {"ok": True, "events": ["Stop"], "restored": False}
    assert fx.FOREIGN not in json.dumps(result)


def test_ledger_project_resolution_never_uses_an_idle_lead_prefix(tmp_path):
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "foreign.json").write_text("{}", encoding="utf-8")
    caller = "12345678-1111-2222-3333-444455556666"
    cfg = real_cfg(brief={"ledger_dir": str(ledger)}, idle_lines={"leads": {"foreign": caller[:8]}})
    state = {"requests": [{"id": 7, "project": "foreign", "status": "open", "session": "someone-else"}]}
    tools = mcp_server.BridgeTools(
        cfg, state_builder=lambda: state, inbox=Box(), http_bound=True, http_caller=lambda: caller,
    )
    assert tools.ledger_list("")["untrusted_data"] == []
    assert tools.status()["open_request_ledger"] == 0


def _check_results(results: dict, own_ask: str) -> None:
    """`results`: (tool, arguments) -> (is_error, text) for every probe, as the OWN caller saw them."""
    for (tool, arguments), (_, text) in results.items():
        assert not fx.leaks(text), f"{tool}({arguments}) showed another session: {text[:300]}"
    payload = lambda tool, args: json.loads(results[(tool, args)][1])
    data = lambda tool, args: payload(tool, args)["untrusted_data"]
    assert [(s["name"], s["session"]) for s in data("list_sessions", "{}")] == [("Alpha", fx.OWN)]
    assert [c["id"] for c in data("cards_list", "{}")] == ["card-a"]
    ledger = data("ledger_list", json.dumps({"project": ""}))
    assert sorted((r["project"], r["id"]) for r in ledger) == [("Proj-A", 1), ("Proj-A", 2), ("Proj-B", 3)]
    assert all("session" not in r for r in ledger)
    assert data("ledger_list", json.dumps({"project": "proj-c"})) == []
    assert [r["id"] for r in data("ledger_list", json.dumps({"project": "proj-b"}))] == [3]
    assert payload("status", "{}")["open_request_ledger"] == 3
    own_replies = results[("read_replies", json.dumps({"session": fx.OWN}))]
    assert not own_replies[0] and json.loads(own_replies[1])["untrusted_data"] == []
    for session in (fx.FOREIGN, fx.OWN[:8]):
        assert results[("read_replies", json.dumps({"session": session}))][0]
        assert results[("send_to_session", json.dumps({"session": session, "text": "hijack"}))][0]
    assert not results[("send_to_session", json.dumps({"session": fx.OWN, "text": "own"}))][0]
    foreign = results[("answer_routine_ask", json.dumps({"id": "FOREIGN", "choice": "yes"}))]
    assert foreign[0] and "no such decision" in foreign[1]
    assert not results[("answer_routine_ask", json.dumps({"id": own_ask, "choice": "yes"}))][0]


def _key(tool, arguments, foreign_ask):
    if arguments.get("id") == foreign_ask:
        arguments = {**arguments, "id": "FOREIGN"}
    return tool, json.dumps(arguments)


def _asks():
    own, foreign = ask(fx.OWN), ask(fx.FOREIGN)
    return own, foreign


def test_http_end_to_end_no_registered_tool_shows_another_session(tmp_path, monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")
    pytest.importorskip("starlette.testclient")
    from starlette.testclient import TestClient
    monkeypatch.setattr("ctb.decisions.announce", lambda *args: None)
    own_ask, foreign_ask = _asks()
    tools, store = bound_tools(tmp_path, http_bound=True), FakeStore()
    headers, results, ids = signed_headers(store, fx.OWN), {}, iter(range(100, 200))
    with TestClient(mcp_server.create_http_app(tools, store), base_url="http://127.0.0.1:8765") as client:
        mcp_session = _open(client, headers)
        for tool, calls in probes(own_ask, foreign_ask).items():
            for arguments in calls:
                result = _call(client, headers, mcp_session, tool, arguments, next(ids))
                results[_key(tool, arguments, foreign_ask)] = (bool(result.get("isError")),
                                                               result["content"][0]["text"])
    assert {tool for tool, _ in results} == registered(tools)
    _check_results(results, own_ask)
    assert {sid for sid, _ in tools._inbox.items} == {fx.OWN}
    assert decision(foreign_ask)["state"] == "open" and decision(own_ask)["state"] == "answered"


async def _stdio_calls(env: dict, err_path: Path, calls: dict) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=[fx.__file__], env=env)
    results = {}
    with open(err_path, "w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                results["registered"] = {tool.name for tool in (await client.list_tools()).tools}
                for key, (tool, arguments) in calls.items():
                    result = await client.call_tool(tool, arguments)
                    results[key] = (bool(result.isError), result.content[0].text if result.content else "")
    return results


def _record_claude_process(session: str, pid: int) -> None:
    """A live Claude Code session record, as Claude Code writes it, in the sandboxed Claude home."""
    from ctb.accounts import claude_home
    folder = claude_home() / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    write_private(folder / f"{pid}.json", json.dumps({"sessionId": session, "pid": pid}))


def test_stdio_end_to_end_binds_every_tool_to_the_proven_calling_session(tmp_path):
    """The real stdio transport and the real caller proof: this test process is recorded as OWN's Claude Code and
    is the server's parent. A server claiming FOREIGN (no such process above it) or no session refuses everything."""
    pytest.importorskip("mcp.server.fastmcp")
    pytest.importorskip("mcp.client.stdio")
    import anyio
    own_ask, foreign_ask = _asks()
    _record_claude_process(fx.OWN, os.getpid())
    work = tmp_path / "child"
    work.mkdir()
    calls = {_key(tool, arguments, foreign_ask): (tool, arguments)
             for tool, many in probes(own_ask, foreign_ask).items() for arguments in many}
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["MCP_CALLER_TEST_DIR"] = str(work)

    def run(**extra):
        return anyio.run(_stdio_calls, {**env, **extra}, tmp_path / "child.err", calls)

    results = run(CLAUDE_CODE_SESSION_ID=fx.OWN)
    err = (tmp_path / "child.err").read_text(encoding="utf-8")
    assert results.pop("registered") == set(probes("a", "b")), err
    _check_results(results, own_ask)
    queued = [json.loads(line) for line in (work / "inbox.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {q["session"] for q in queued} == {fx.OWN}
    assert decision(foreign_ask)["state"] == "open" and decision(own_ask)["state"] == "answered"

    for claim, reason in ((fx.FOREIGN, "cannot verify this session"), (None, "no session")):
        refused = run(**({"CLAUDE_CODE_SESSION_ID": claim} if claim else {}))
        refused.pop("registered")
        for key, (is_error, text) in refused.items():
            assert is_error and reason in text and not fx.leaks(text), (claim, key, text[:300])
    assert {json.loads(line)["session"] for line in
            (work / "inbox.jsonl").read_text(encoding="utf-8").splitlines()} == {fx.OWN}
