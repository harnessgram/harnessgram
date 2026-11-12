"""The Morning Brief (brief.py): every fact carries its age and goes stale, tones come from facts, the per-viewer
"since you last looked" marker, the waiting list, what the Cockpit notices by itself, and the switch that keeps
the classic home untouched. All sample data is invented."""
import json

import pytest

from ctb import blocks as B
from ctb import brief, panel_settings, work_lines
from ctb.config import Config, state_dir
from ctb.sessions_menu import Entry
from test_panel import call, server, signed  # noqa: F401

NOW = 1_000_000.0
S1, S2, S3 = "a1b2c3d4" + "0" * 28, "b2c3d4e5" + "0" * 28, "c3d4e5f6" + "0" * 28


def entry(sid, name, state, age):
    return Entry(sid, name, 5, age, state, False, None)


def session_row(e, **kw):
    return dict({"id": e.session[:8], "name": e.name, "state": e.state, "age": int(e.age), "bg": 0, "now": "",
                 "summary": "", "url": "https://t.me/c/1/5"}, **kw)


def make(rows=(), entries=(), cards=(), blocks=(), events=(), seen=0.0, state=None, cfg=None):
    st = dict({"sessions": [session_row(e) for e in entries], "decisions": [], "cards": [], "quota": []}, **(state or {}))
    return brief.build(cfg or Config(), st, list(entries), NOW, provider=work_lines.SampleProvider(rows),
                       block_rows=list(blocks), card_rows=list(cards), events=list(events), seen=seen)


def by_key(b):
    return {x["key"]: x for x in b["lines"]}


def test_every_fact_has_an_age_and_a_stale_flag():
    b = make(rows=[("studio", "Building", "open", "", 10), ("release", "Approve?", "open", "owner", 60 * 30)],
             entries=[entry(S1, "Studio: filters", "working", 30.0)], events=[{"kind": "note", "line": "", "text": "hi", "at": NOW - 5}])
    for group in ("lines", "waiting", "noticed"):
        assert b[group], group
        for item in b[group]:
            assert "age" in item and isinstance(item["stale"], bool), (group, item)
    assert "age" in b["teams"] and "stale" in b["teams"]
    (w,) = b["waiting"]
    assert w["stale"] is True and w["age"] == 30 * 3600  # a ledger item older than a day is shown stale


def test_tones_come_from_facts():
    rows = [("studio", "Three of eight screens are built", "open", "", 10),
            ("release", "Ready except your approval", "open", "", 60), ("release", "Approve", "open", "owner", 60),
            ("archive", "Index the old files", "open", "", 30 * 60), ("notes", "Tidy", "open", "", 5 * 60)]
    b = make(rows=rows, entries=[entry(S1, "Studio: filters", "working", 30.0)])
    lines = by_key(b)
    assert lines["studio"]["tone"] == "ok" and lines["studio"]["live"]
    assert lines["release"]["tone"] == "wait" and lines["release"]["waiting"] == 1
    assert lines["archive"]["tone"] == "bad" and lines["archive"]["why"] == "no_move"
    assert lines["notes"]["tone"] == "idle"
    assert [x["tone"] for x in b["lines"]] == ["bad", "wait", "ok", "idle"]
    assert b["headline"] == {"moving": 1, "stuck": 1, "waiting": 1, "lines": 4}


def test_a_card_whose_session_ended_is_never_shown_as_live():
    card = {"id": "f" * 16, "kind": "progress", "session": S3, "title": "Fix the save button", "updated": NOW - 60,
            "state": "open", "chat": -1001234567890, "thread": 9}
    b = make(entries=[entry(S3, "Bugs: save", "ended", 50.0)], cards=[card])
    (n,) = b["noticed"]
    assert n["kind"] == "stale_card" and n["why"] == "ended" and n["url"] == "https://t.me/c/1234567890/9"
    assert by_key(b)["bugs"]["tone"] == "bad" and by_key(b)["bugs"]["live"] is False


def test_a_quiet_card_of_an_idle_session_is_stale_and_a_working_one_is_not():
    card = {"id": "f" * 16, "kind": "progress", "session": S3, "title": "Build", "updated": NOW - brief.CARD_STALE - 1,
            "state": "open", "chat": -1001234567890, "thread": 9}
    idle = make(entries=[entry(S3, "Bugs: save", "idle", 50.0)], cards=[card])
    assert idle["noticed"][0]["why"] == "no_update" and by_key(idle)["bugs"]["why"] == "stale_card"
    working = make(entries=[entry(S3, "Bugs: save", "working", 50.0)], cards=[card])
    assert working["noticed"] == [] and by_key(working)["bugs"]["tone"] == "ok"
    asks = make(entries=[entry(S3, "Bugs: save", "idle", 50.0)], cards=[dict(card, kind="choice")])
    assert asks["noticed"] == []


def test_a_fresh_status_block_is_the_lines_sentence_and_a_stale_one_greys():
    rows = [("studio", "From the ledger", "open", "", 30)]
    blk = {"id": "1" * 16, "kind": "status", "line": "Studio", "title": "", "text": "From the agent", "session": S1,
           "created": NOW - 60, "updated": NOW - 60, "expires": NOW + 3600, "expires_in": 3700}
    x = by_key(make(rows=rows, entries=[entry(S1, "Studio: x", "working", 10.0)], blocks=[blk]))["studio"]
    assert x["text"] == "From the agent" and x["source"] == "status" and x["by"] == S1[:8] and not x["stale"]
    old = dict(blk, updated=NOW - B.TTL["status"] - 10)
    y = by_key(make(rows=[], blocks=[old]))["studio"]
    assert y["text"] == "From the agent" and y["stale"] is True


def test_a_muted_session_folds_and_cannot_raise_waiting(monkeypatch):
    monkeypatch.setattr(B, "muted", lambda: {S1[:8]})
    blk = {"id": "1" * 16, "kind": "status", "line": "studio", "title": "", "text": "Look at me", "session": S1,
           "created": NOW - 60, "updated": NOW - 60, "expires": NOW + 3600, "expires_in": 3700}
    need = dict(blk, id="2" * 16, kind="needs_you", text="Answer now", card="e" * 16)
    card = {"id": "e" * 16, "kind": "choice", "title": "Which style?", "age": 60, "session": S1[:8], "ended": False, "old": False}
    b = make(blocks=[blk, need], state={"cards": [card]})
    x = by_key(b)["studio"]
    assert x["muted"] and x["text"] == "" and x["muted_by"] == [S1[:8]]
    (w,) = b["waiting"]
    assert w["kind"] == "card" and w["note"] == ""  # the card itself still waits; the muted block adds nothing


def test_needs_you_adds_its_line_to_the_question_it_names():
    need = {"id": "2" * 16, "kind": "needs_you", "line": "studio", "title": "", "text": "The style blocks the next step",
            "card": "e" * 16, "session": S1, "created": NOW - 60, "updated": NOW - 60, "expires": NOW + 3600, "expires_in": 3700}
    card = {"id": "e" * 16, "kind": "choice", "title": "Which style?", "age": 60, "session": S1[:8], "ended": False,
            "old": False, "topic": "Studio: filters"}
    dec = {"id": "d" * 16, "title": "Ship it?", "age": 30, "stale": "", "topic": "Release: v2"}
    gone = {"id": "c" * 16, "title": "Old", "age": 30, "stale": "ended", "topic": ""}
    b = make(blocks=[need], state={"cards": [card], "decisions": [dec, gone]})
    assert [(w["kind"], w["line"]) for w in b["waiting"]] == [("decision", "release"), ("card", "studio")]
    assert b["waiting"][1]["note"] == "The style blocks the next step"


def test_since_you_last_looked():
    rows = [("studio", "Done a", "done", "", 30), ("studio", "Done long ago", "done", "", 600),
            ("release", "Approve", "open", "owner", 20), ("archive", "Index", "open", "", 30 * 60)]
    b = make(rows=rows, seen=NOW - 3600)
    assert b["since"] == {"age": 3600, "done": 1, "stuck": 0, "new": 1}
    assert make(rows=rows)["since"] is None  # the first look


def test_the_marker_is_per_viewer_and_only_moves_forward(tmp_path):
    assert brief.seen_at(7) == 0.0
    assert brief.mark_seen(7, 100.0, now=200.0) == 100.0
    assert brief.mark_seen(7, 50.0, now=200.0) == 100.0  # never backwards
    assert brief.mark_seen(7, 900.0, now=200.0) == 200.0  # never into the future
    assert brief.seen_at(7) == 200.0 and brief.seen_at(8) == 0.0
    assert (state_dir() / brief.SEEN_FILE).stat().st_mode & 0o077 == 0


def test_the_owner_moves_the_marker_from_the_page(server):  # noqa: F811
    code, _, data = call(server, path="/api/brief-seen", init=signed(), body=b'{"at": 1000}')
    assert code == 200 and json.loads(data)["at"] == 1000 and brief.seen_at(7) == 1000.0
    for bad in (b'{"at": "x"}', b'{"at": 0}', b'{"at": 1, "x": 2}', b"[]"):
        assert call(server, path="/api/brief-seen", init=signed(), body=bad)[0] == 400
    assert call(server, path="/api/brief-seen", init=signed(user_id=8), body=b'{"at": 5}')[0] == 403


def test_events_are_read_bounded_and_recent(tmp_path):
    path = tmp_path / "events.jsonl"
    lines = [{"kind": "job_died", "line": "Radio", "text": "  The render  stopped ", "at": NOW - 60},
             {"kind": "rm -rf", "line": "x", "text": "nope", "at": NOW - 60},
             {"kind": "note", "line": "", "text": "too old", "at": NOW - brief.EVENT_WINDOW - 1},
             {"kind": "note", "line": "", "text": "x" * 500, "at": NOW - 30}]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    evs = brief.read_events(path, NOW)
    assert [e["kind"] for e in evs] == ["note", "job_died"] and evs[1]["text"] == "The render stopped"
    assert evs[1]["line"] == "radio" and len(evs[0]["text"]) == brief.EVENT_TEXT
    b = make(events=evs)
    assert by_key(b)["radio"]["why"] == "job_died" and by_key(b)["radio"]["tone"] == "bad"


def test_a_promise_that_did_not_move_is_noticed_and_an_unreadable_source_too():
    b = make(rows=[("reviews", "Screen review", "open", "", 4 * 24 * 60)])
    assert [n["kind"] for n in b["noticed"]] == ["promise"] and b["noticed"][0]["line_name"] == "reviews"

    class Down(work_lines.Provider):
        name, ok = "ledger", False

        def items(self, now):
            return []
    out = brief.build(Config(), {}, [], NOW, provider=Down(), block_rows=[], card_rows=[], events=[], seen=0.0)
    assert out["noticed"][0]["kind"] == "source_down" and out["lines"] == []


def test_teams_and_the_mac_row():
    st = {"quota": [{"account": "a", "label": "Main", "weekly": 40, "weekly_resets": int(NOW + 3600), "at": NOW - 60},
                    {"account": "b", "label": "Spare", "weekly": 90, "weekly_resets": int(NOW + 3600), "at": NOW - 60},
                    {"account": "c", "label": "Later", "weekly": 10, "weekly_resets": int(NOW + 5 * 86400), "at": NOW - 60}],
          "mac": {"available": True, "status": "warn", "sampled_at": NOW - 20}}
    es = [entry(S1, "Studio: x", "working", 5.0), entry(S2, "Radio: y", "idle", 5.0)]
    b = make(entries=es, state=st)
    assert b["teams"]["working"] == 1 and b["teams"]["idle"] == 1
    assert b["teams"]["unused"] == [{"label": "Main", "left": 60}] and b["teams"]["stale"] is False
    assert b["mac"] == {"status": "warn", "age": 20, "stale": False}
    assert make()["mac"] is None  # no Mac reader installed: no claim about the Mac


def test_session_lines_follow_projects_and_title_prefixes():
    from ctb.config import ProjectConfig
    cfg = Config(projects=[ProjectConfig(name="Garden App", chat_id=-100555, title_prefixes=["ga"])])
    assert brief.session_line(cfg, -100555, "anything") == "garden app"
    assert brief.session_line(cfg, None, "GA: login") == "garden app"
    assert brief.session_line(cfg, None, "Studio: filters") == "studio"
    assert brief.session_line(cfg, None, "no prefix") == ""


def test_the_switch_is_off_by_default_and_keeps_the_classic_state(server):  # noqa: F811
    assert panel_settings.read()["brief_home"] is False
    data = json.loads(call(server, init=signed())[2])
    assert "brief" not in data
    panel_settings.write("brief_home", True)
    data = json.loads(call(server, init=signed())[2])
    assert data["settings"]["brief_home"] is True
    assert set(data["brief"]) >= {"headline", "since", "waiting", "lines", "noticed", "teams", "mac", "ask"}
    assert data["brief"]["ask"] == list(brief.ASK)


def test_the_brief_leaves_the_server_secret_free(server, monkeypatch):  # noqa: F811
    key = "sk-" + "proj-" + "Q" * 30
    panel_settings.write("brief_home", True)
    monkeypatch.setattr(work_lines, "from_config", lambda cfg: work_lines.SampleProvider([("studio", "token " + key, "open", "", 5)]))
    raw = call(server, init=signed())[2].decode()
    assert "Q" * 30 not in raw and "studio" in raw


def test_a_broken_brief_never_breaks_the_state(server, monkeypatch):  # noqa: F811
    panel_settings.write("brief_home", True)
    monkeypatch.setattr(brief, "build", lambda *a, **k: 1 / 0)
    code, _, data = call(server, init=signed())
    assert code == 200 and json.loads(data)["brief"] is None


@pytest.mark.parametrize("raw", [{"brief": []}, {"brief": "x"}])
def test_the_brief_config_must_be_an_object(raw):
    from ctb import config as C
    with pytest.raises(C.ConfigError, match="brief must be an object"):
        C.from_dict(dict({"bot_token": "1:" + "A" * 35, "owner_chat_id": 7}, **raw))
