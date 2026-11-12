"""The panel's POST /api/screen: the owner sees what one live session's terminal shows, read-only."""
import json

import pytest

from ctb import inbox, panel
from test_decisions import cfg, rig, roomy_budget, server  # noqa: F401
from test_panel import call, signed
from test_panel_say import HOME_S, ORBIT_S, entry, live  # noqa: F401


def post(server, init=None, **body):
    status, _, data = call(server, path="/api/screen", init=init or signed(), body=json.dumps(body).encode())
    return status, json.loads(data)


@pytest.fixture
def pane(monkeypatch):
    asked = []
    monkeypatch.setattr(inbox, "stop_pane", lambda session: asked.append(session) or "%7")
    monkeypatch.setattr(inbox, "pane_text", lambda pane, lines=45: "old row\n‏Pick a theme\n  Dark\n\n\n")
    return asked


def test_the_owner_sees_the_sessions_screen(rig, server, live, pane):
    status, out = post(server, session=ORBIT_S[:8])
    assert status == 200 and out == {"ok": True, "text": "old row\nPick a theme\n  Dark"}
    assert pane == [ORBIT_S]  # only that session's own pane is read


def test_a_session_without_a_terminal_shows_nothing(rig, server, live, monkeypatch):
    monkeypatch.setattr(inbox, "stop_pane", lambda session: None)
    assert post(server, session=ORBIT_S[:8]) == (200, {"ok": True, "text": ""})


def test_an_ended_closed_or_unknown_session_shows_nothing(rig, server, live, pane):
    assert post(server, session=HOME_S[:8])[0] == 404
    assert post(server, session="deadbeef")[0] == 404
    live[0].closed = True
    assert post(server, session=ORBIT_S[:8])[0] == 404
    assert pane == []


@pytest.mark.parametrize("body", [{}, {"session": ORBIT_S}, {"session": "../../ab"}, {"session": 5},
                                  {"session": ORBIT_S[:8], "extra": 1}])
def test_a_malformed_request_is_refused(rig, server, live, pane, body):
    assert post(server, **body)[0] == 400 and pane == []


def test_it_needs_the_owners_fresh_signature(rig, server, live, pane):
    status, _, _ = call(server, path="/api/screen", init="bad", body=json.dumps({"session": ORBIT_S[:8]}).encode())
    assert status == 403 and pane == []


def test_the_page_offers_the_screen_in_both_languages():
    assert "screenRow(s,host)" in panel.PAGE and "/api/screen" in panel.PAGE
    assert all(k in panel.STRINGS[lang] for lang in panel.STRINGS for k in ("screen", "screenHide", "screenNone"))


def test_a_saved_pane_that_another_window_took_over_is_never_shown(rig, server, live, monkeypatch):
    """A saved pane id outlives a tmux restart and is then reused by another window: only a pane proven to host
    this session is read (security review of the live screen)."""
    monkeypatch.setattr(inbox, "pane_for_session", lambda session: "%7")
    monkeypatch.setattr(inbox, "pane_runs_session", lambda pane, session, known_pid=None: False)
    monkeypatch.setattr(inbox, "_pane_from_process", lambda session: None)
    read = []
    monkeypatch.setattr(inbox, "pane_text", lambda pane, lines=45: read.append(pane) or "someone else's screen")
    assert post(server, session=ORBIT_S[:8]) == (200, {"ok": True, "text": ""}) and read == []
