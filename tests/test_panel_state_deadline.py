"""The state read outlives the connection deadline, a failed build is logged in one line without its message, and
the page's render-error report is logged with only an error type and a place."""
import json
import time

import pytest

from ctb import panel
from test_panel import call, server, signed  # noqa: F401 (the server fixture)


def test_a_slow_state_build_still_gets_its_reply(server, monkeypatch):
    # The connection deadline protects the slots from slow senders. A busy Mac builds the state in several
    # seconds; cutting that reply made the page's first load say "no access".
    monkeypatch.setattr(panel, "DEADLINE", 0.3)
    real = panel.build_state

    def slow(*a, **k):
        time.sleep(1.0)
        return real(*a, **k)
    monkeypatch.setattr(panel, "build_state", slow)
    start = time.time()
    code, _, data = call(server, init=signed())
    assert code == 200 and json.loads(data)["ok"] is True
    assert time.time() - start >= 1.0


def test_an_unauthenticated_state_read_keeps_the_short_deadline(server, monkeypatch):
    monkeypatch.setattr(panel, "DEADLINE", 0.3)
    monkeypatch.setattr(panel, "build_state", lambda *a, **k: pytest.fail("built for a stranger"))
    code, _, _ = call(server, init=signed(tamper=True))
    assert code == 403


def test_a_failed_build_logs_type_and_place_but_no_message(server, monkeypatch, capsys):
    def broken(*a, **k):
        raise KeyError("private-owner-text")
    monkeypatch.setattr(panel, "build_state", broken)
    code, _, data = call(server, init=signed())
    assert code == 500 and json.loads(data) == {"error": "unavailable"}
    err = capsys.readouterr().err
    lines = [x for x in err.splitlines() if "state build failed" in x]
    assert len(lines) == 1, err
    assert "KeyError at test_panel_state_deadline.py:" in lines[0] and "in broken" in lines[0]
    assert "private-owner-text" not in err


def test_the_page_reports_a_render_error_in_one_line(server, capsys):
    body = json.dumps({"name": "TypeError", "at": "render:2950:12"}).encode()
    code, _, data = call(server, path="/api/client-error", init=signed(), body=body)
    assert code == 200 and json.loads(data) == {"ok": True}
    lines = [x for x in capsys.readouterr().err.splitlines() if "page render failed" in x]
    assert len(lines) == 1 and lines[0].endswith("TypeError at render:2950:12")


@pytest.mark.parametrize("payload", [
    {"name": "TypeError", "at": "render:1:2", "message": "private-owner-text"},
    {"name": "Type Error", "at": ""},
    {"name": "TypeError", "at": "private owner text:1:2"},
    {"name": "X" * 41, "at": ""},
    {"name": 5, "at": ""},
    {"at": "render:1:2"},
    [],
])
def test_a_report_with_anything_more_is_refused_and_not_logged(server, capsys, payload):
    code, _, _ = call(server, path="/api/client-error", init=signed(), body=json.dumps(payload).encode())
    assert code == 400
    assert "page render failed" not in capsys.readouterr().err


def test_a_report_needs_the_owners_login(server, capsys):
    code, _, _ = call(server, path="/api/client-error", init=signed(tamper=True),
                      body=json.dumps({"name": "TypeError", "at": ""}).encode())
    assert code == 403
    assert "page render failed" not in capsys.readouterr().err
