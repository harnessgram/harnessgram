"""Automatic compaction per session, and a compaction the agent asks for itself (the owner's idea)."""
import json

import pytest

from ctb import cli, compact, hooks, panel
from test_decisions import cfg, rig, roomy_budget, server  # noqa: F401
from test_panel import call, signed
from test_panel_say import HOME_S, ORBIT_S, entry, live  # noqa: F401


@pytest.fixture
def big(monkeypatch):
    """A transcript whose context is 250k tokens."""
    from ctb import usage
    monkeypatch.setattr(usage, "context_tokens", lambda path: (250_000, 0))
    ran = []
    return lambda session: (compact.auto_compact_after_reply(
        session, "t.jsonl", auto_compact_fn=lambda s, ctx, **kw: ran.append((s, ctx, kw))), ran)


def test_a_session_follows_the_global_threshold_until_it_gets_its_own(big):
    compact.set_auto_compact_limit(400)
    assert compact.limit_for("s") == 400 and big("s") == (False, [])
    compact.set_session_limit("s", 200)               # this session needs no huge context: compact early
    assert compact.limit_for("s") == 200 and compact.limit_for("other") == 400
    done, ran = big("s")
    assert done and ran == [("s", 250_000, {"cold": False})]
    compact.set_session_limit("s", 0)                 # never, for this session only
    compact.set_auto_compact_limit(200)
    assert big("s")[0] is False and big("other")[0] is True
    compact.set_session_limit("s", None)              # back on the global one
    assert compact.session_limit("s") is None and compact.limit_for("s") == 200
    with pytest.raises(ValueError):
        compact.set_session_limit("s", 123)


def test_the_agent_asks_and_the_compaction_runs_when_its_turn_ends(big, capsys, monkeypatch):
    compact.set_auto_compact_limit(0)                 # automatic compaction is off: the agent's own call still runs
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s")
    assert cli.main(["compact", "--note", "keep the open PR numbers\nand\tthe plan"]) == 0
    assert "when this turn ends" in capsys.readouterr().out
    done, ran = big("s")
    assert done and ran == [("s", 250_000, {"cold": False, "note": "keep the open PR numbers and the plan",
                                            "asked": True})]
    assert big("s")[0] is False                       # asked once, runs once
    cli.main(["compact"])
    assert cli.main(["compact", "--cancel"]) == 0 and big("s")[0] is False
    compact.ask("s", "x")
    assert compact.take_ask("s", now=__import__("time").time() + compact.ASK_KEPT + 5) is None   # too old


def test_the_note_is_one_short_safe_line():
    assert compact.clean_note(" a\x1b[31m\nb " + "x" * 900).startswith("a [31m b xxx")
    assert len(compact.clean_note("x" * 900)) == compact.ASK_NOTE


def test_agents_are_told_about_the_command():
    assert "ctb compact --note" in hooks.BOARD_HINT


def post(server, init=None, **body):
    status, _, data = call(server, path="/api/compact", init=init or signed(), body=json.dumps(body).encode())
    return status, json.loads(data)


def test_the_owner_sets_one_sessions_threshold_in_the_panel(rig, server, live):
    assert post(server, session=ORBIT_S[:8], limit=200) == (200, {"ok": True, "limit": 200})
    assert compact.session_limit(ORBIT_S) == 200
    assert post(server, session=ORBIT_S[:8], limit=0)[0] == 200 and compact.session_limit(ORBIT_S) == 0
    assert post(server, session=ORBIT_S[:8], limit=None)[0] == 200 and compact.session_limit(ORBIT_S) is None
    for bad in ({"session": ORBIT_S[:8], "limit": 123}, {"session": ORBIT_S[:8]}, {"session": ORBIT_S[:8], "limit": "200"},
                {"session": ORBIT_S[:8], "limit": True}, {"session": "../../ab", "limit": 200}):
        assert post(server, **bad)[0] == 400
    assert post(server, session=HOME_S[:8], limit=200)[0] == 404      # an ended session
    assert post(server, session="deadbeef", limit=200)[0] == 404
    status, _, _ = call(server, path="/api/compact", init="", body=b'{"session": "a1b2c3d4", "limit": 200}')
    assert status in (401, 403) and compact.session_limit(ORBIT_S) is None


def test_the_page_shows_the_sessions_own_threshold():
    page = panel.PAGE
    assert 'post("/api/compact",{session:s.id,limit:next})' in page and "compactRow(s,d)" in page
    assert "var at=compactOf(s,d)" in page


def test_a_request_refused_for_now_stays_and_runs_at_the_next_turn_end(monkeypatch):
    """Review of PR #209: the request was removed before the compaction started, so a refusal for now
    (the owner wrote meanwhile, a compaction ran minutes ago) lost it for good."""
    from ctb import usage
    monkeypatch.setattr(usage, "context_tokens", lambda path: (250_000, 0))
    compact.set_auto_compact_limit(0)
    compact.ask("s", "keep the plan")
    answers = [False, True]
    seen = []
    fn = lambda s, ctx, **kw: seen.append(kw["note"]) or answers.pop(0)  # noqa: E731
    assert compact.auto_compact_after_reply("s", "t.jsonl", auto_compact_fn=fn)
    assert compact.peek_ask("s")["note"] == "keep the plan" and not compact.claimed("s")   # refused: waits
    assert compact.auto_compact_after_reply("s", "t.jsonl", auto_compact_fn=fn)
    assert seen == ["keep the plan"] * 2 and compact.claimed("s")
    # Review of PR #212: typing /compact proves nothing, so the request stays until a compaction really
    # ended, and one run only may hold it meanwhile.
    assert compact.peek_ask("s") is not None
    assert compact.auto_compact_after_reply("s", "t.jsonl", auto_compact_fn=fn) is False and len(seen) == 2
    compact.mark_done("s")
    assert compact.peek_ask("s") is None


def test_a_compaction_that_ended_after_the_request_meets_it(monkeypatch):
    """The owner compacted by hand while the request waited: no second compaction later."""
    from ctb import usage
    monkeypatch.setattr(usage, "context_tokens", lambda path: (250_000, 0))
    compact.ask("s", "x")
    compact.note_compacting("s")                      # it compacts right now: nothing is started on top
    ran = []
    fn = lambda s, ctx, **kw: ran.append(1)  # noqa: E731
    assert compact.auto_compact_after_reply("s", "t.jsonl", auto_compact_fn=fn) is False and not ran
    assert compact.peek_ask("s") is not None
    compact.mark_done("s")
    assert compact.peek_ask("s") is None
    assert compact.auto_compact_after_reply("s", "t.jsonl", auto_compact_fn=fn) is False and not ran
    compact.ask("s", "later")                         # a request made after that compaction is a new need
    assert compact.peek_ask("s")["note"] == "later"


def test_finishing_an_old_request_never_removes_a_newer_one():
    compact.ask("s", "first")
    old = compact.peek_ask("s")
    compact.ask("s", "second")
    assert compact.finish_ask("s", old["at"]) is False
    assert compact.peek_ask("s")["note"] == "second"
    assert compact.finish_ask("s", compact.peek_ask("s")["at"]) is True and compact.peek_ask("s") is None


def test_one_detached_run_per_request_and_a_refusal_frees_it(monkeypatch, capsys):
    from ctb import inbox
    calls = []
    monkeypatch.setattr(compact, "ASK_GAP", 0.0)
    monkeypatch.setattr(inbox, "auto_compact", lambda s, ctx, cold=False, **kw: calls.append(kw) or False)
    compact.ask("s", "- keep the plan")                # a note that starts with a dash
    assert cli.main(["auto-compact", "s", "250000", "--asked"]) == 0
    assert calls == [{"note": "- keep the plan", "asked": True}] * compact.ASK_TRIES  # refused: tried again here
    assert compact.peek_ask("s") is not None and not compact.claimed("s")    # and still free for a later turn

    def typed(s, ctx, cold=False, **kw):               # /compact was typed and the compaction runs
        calls.append(kw)
        compact.note_compacting(s)
        return True
    monkeypatch.setattr(inbox, "auto_compact", typed)
    del calls[:]
    assert cli.main(["auto-compact", "s", "250000", "--asked"]) == 0 and len(calls) == 1 and compact.claimed("s")
    assert cli.main(["auto-compact", "s", "250000", "--asked"]) == 0 and len(calls) == 1  # a second run types nothing
    capsys.readouterr()
    assert cli.main(["compact", "--cancel", "--session", "s"]) == 0
    assert "already started" in capsys.readouterr().out
    assert cli.main(["auto-compact", "s", "250000", "--asked"]) == 0 and len(calls) == 1  # taken back: nothing runs


def test_a_yes_from_the_runner_is_not_a_compaction(monkeypatch):
    """Review of #215: the runner says yes before the keys are sent; a refusal after that (an open question,
    a busy command channel) left the request claimed for 15 minutes with nobody to retry it."""
    from ctb import inbox
    calls = []
    monkeypatch.setattr(compact, "ASK_GAP", 0.0)
    monkeypatch.setattr(inbox, "auto_compact", lambda s, ctx, cold=False, **kw: calls.append(kw) or True)
    compact.ask("s", "keep")
    assert cli.main(["auto-compact", "s", "250000", "--asked"]) == 0
    assert len(calls) == compact.ASK_TRIES and compact.peek_ask("s") is not None and not compact.claimed("s")

    def compacted(s, ctx, cold=False, **kw):           # this time the compaction really runs and ends
        calls.append(kw)
        compact.note_compacting(s)
        compact.mark_done(s)
        return True
    monkeypatch.setattr(inbox, "auto_compact", compacted)
    del calls[:]
    assert cli.main(["auto-compact", "s", "250000", "--asked"]) == 0
    assert len(calls) == 1 and compact.peek_ask("s") is None


def test_a_dashed_note_is_one_argument_for_the_detached_run(monkeypatch):
    seen = []
    monkeypatch.setattr(compact.subprocess, "Popen", lambda argv, **kw: seen.append(argv))
    assert compact.spawn_auto_compact("s", 250_000, note="- keep", asked=True)
    assert seen[0][-2:] == ["--asked", "--note=- keep"]
    args = cli.build_parser().parse_args(seen[0][seen[0].index("auto-compact"):]) if hasattr(cli, "build_parser") else None
    assert args is None or args.note == "- keep"


def test_a_compaction_seen_to_end_on_the_screen_meets_the_request():
    """Review of #216: without a SessionStart nothing wrote the ended marker, the run concluded that no
    compaction happened and typed /compact again a minute later."""
    class Bot:
        def call(self, method, params):
            return {"message_id": 7}
    compact.ask("s", "keep")
    at = compact.peek_ask("s")["at"]
    screens = iter(["Compacting conversation", "Compacting conversation", "> "])
    now = [at + 1.0]

    def sleep(_):
        now[0] += 3
    assert compact.run(type("C", (), {"ui_language": "en", "chat_id": 1})(), Bot(), 5, "s",
                       lambda: next(screens, "> "), sleep=sleep, clock=lambda: now[0], chat=1) is True
    assert compact.ended_since("s", at) and compact.peek_ask("s", now[0]) is None
