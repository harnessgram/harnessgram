"""Bounded ledger input regressions; helpers use pipes and never sockets."""
import json
import sys

import pytest

from ctb import work_lines as W


@pytest.mark.parametrize("channel", [1, 2, "both"])
def test_3_stream_limit_kills_helper_before_it_finishes(tmp_path, monkeypatch, channel):
    monkeypatch.setattr(W, "FILE_BYTES", 8192)
    finished = tmp_path / "finished"
    code = ("import os,pathlib\n"
            "for i in range(10000):\n"
            f" for fd in {([1, 2] if channel == 'both' else [channel])!r}: os.write(fd,b'x'*4096)\n"
            f"pathlib.Path({str(finished)!r}).touch()\n")
    children = []
    popen = W.subprocess.Popen

    def start(*a, **kw):
        child = popen(*a, **kw)
        children.append(child)
        return child

    monkeypatch.setattr(W.subprocess, "Popen", start)
    provider = W.LedgerProvider(command=[sys.executable, "-c", code])
    assert provider.items(0) == [] and not provider.ok
    assert not finished.exists()
    assert len(children) == 1 and children[0].returncode < 0


def test_3_bounded_valid_output_and_stderr_are_accepted(monkeypatch):
    monkeypatch.setattr(W, "FILE_BYTES", 1024)
    data = {"Studio": {"items": [{"id": 1, "text": "Build", "status": "open"}]}}
    code = f"import os;os.write(2,b'x'*1024);print({json.dumps(data)!r})"
    provider = W.LedgerProvider(command=[sys.executable, "-c", code])
    assert len(provider.items(0)) == 1 and provider.ok


@pytest.mark.parametrize("close_pipes", [False, True])
def test_3_deadline_kills_helper_even_after_pipes_close(monkeypatch, close_pipes):
    monkeypatch.setattr(W, "COMMAND_TIMEOUT", 0.15)
    children = []
    popen = W.subprocess.Popen

    def start(*a, **kw):
        child = popen(*a, **kw)
        children.append(child)
        return child

    monkeypatch.setattr(W.subprocess, "Popen", start)
    code = "import os,time;" + ("os.close(1);os.close(2);" if close_pipes else "") + "time.sleep(60)"
    provider = W.LedgerProvider(command=[sys.executable, "-c", code])
    assert provider.items(0) == [] and not provider.ok
    assert len(children) == 1 and children[0].returncode < 0


@pytest.mark.parametrize("name", ["x" * 61, "x" * 10000, "ß" * 40])
def test_5_command_rejects_oversized_project_names_and_folded_keys(name):
    body = {"items": [{"id": 1, "text": "Build", "status": "open"}]}
    data = {name: body, "Studio": body}
    provider = W.LedgerProvider(command=[sys.executable, "-c", f"print({json.dumps(data)!r})"])
    assert [row["line"] for row in provider.items(0)] == ["studio"]


def test_5_project_name_at_limit_is_preserved():
    rows = W.rows_of("S" * 60, {"items": [{"id": 1, "text": "Build", "status": "open"}]})
    assert rows[0]["name"] == "S" * 60 and rows[0]["line"] == "s" * 60


def test_text_normalizes_separators_and_removes_all_bidi_controls():
    controls = "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    assert W.clean_text("A\t\u00a0B\nC\r\nD" + controls + "E\x00") == "A B\nC\nDE"


def test_classic_questions_and_answers_keep_lines_and_redact_after_normalization():
    from ctb import panel
    data = {"question": "First line\nSecond line", "answer": "One\nTwo", "detail": "pin\t482913"}
    panel._secret_free(data)
    assert data == {"question": "First line\nSecond line", "answer": "One\nTwo", "detail": "pin [redacted]"}


def test_classic_state_preserves_multiline_decisions_with_brief_off(monkeypatch):
    from ctb import panel
    from ctb.config import Config

    decision = {"title": "First line\nSecond line", "yes": "Yes\ncontinue", "no": "No\nstop"}
    monkeypatch.setattr(panel.brief, "on", lambda: False)
    monkeypatch.setattr(panel, "collect", lambda *a, **k: [])
    monkeypatch.setattr(panel, "waiting_for_owner", lambda *a, **k: ([decision.copy()], []))
    monkeypatch.setattr(panel.accounts, "names", lambda: [])
    monkeypatch.setattr(panel.cards, "open_cards", lambda: [])
    monkeypatch.setattr(panel, "open_tasks", lambda *a, **k: [])
    monkeypatch.setattr(panel.bank, "view", lambda: {})
    monkeypatch.setattr(panel.cockpit_apps, "listing", lambda: [])
    monkeypatch.setattr(panel.panel_settings, "read", lambda: {"brief_home": False})
    state = panel.build_state(Config(), now=1)
    assert "brief" not in state
    assert state["decisions"] == [decision]


def test_information_separators_become_a_space_before_the_secret_filter():
    from ctb.work_lines import clean_text
    for sep in ("\t", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x1f", "\x85"):
        assert clean_text("pin" + sep + "482913").split() == ["pin", "482913"]
