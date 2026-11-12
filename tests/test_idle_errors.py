"""'Finished and waiting' is not sent when the terminal shows the turn failed (e.g. Not logged in)."""
import pytest

from ctb import inbox, mirror


def screen(monkeypatch, text):
    monkeypatch.setattr(inbox, "pane_for_session", lambda s: "tg-x")
    monkeypatch.setattr(inbox, "pane_text", lambda p, lines=30: text)
    return mirror.screen_error("s")


def test_not_logged_in_after_the_last_prompt(monkeypatch):
    assert screen(monkeypatch, "❯ hi\n  ⎿  Not logged in · Please run /login\n❯ ") == \
        "Not logged in · Please run /login"


@pytest.mark.parametrize("text", [
    "● Done.\n❯ ",
    "❯ hi\n  ⎿  Not logged in · Please run /login\n❯ again\n● Fixed it.\n❯ ",  # an older turn's error
    "❯ why\n● That 'API Error: 529' means overload; I refreshed the OAuth token.\n❯ ",  # Claude's words
    "❯ review\n  + SCREEN_ERRORS = 'not logged in|api error:'\n❯ ",  # code on screen
    "❯ check\n● Bash(gh auth status)\n  ⎿  You are not logged into any GitHub hosts\n● Done.\n❯ ",
    "❯ x\n● Bash(curl ...)\n  ⎿  {\"message\": \"API Error: 500\"}\n● The server was down; retried, fine now.\n❯ ",
    "❯ x\n● Agent(worker)\n  ⎿  5-hour limit reached · resets 3pm\n● Switched account and finished.\n❯ ",
    "  ⎿  Not logged in · Please run /login\n● Logged in again, all good.\n❯ ",  # prompt scrolled away
])
def test_no_false_errors(monkeypatch, text):
    assert screen(monkeypatch, text) == ""


def test_plain_text_kept(monkeypatch):
    assert screen(monkeypatch, "❯ x\n  ⎿  API Error: 500 a & b <c>\n❯ ") == "API Error: 500 a & b <c>"


def test_real_error_after_a_tool_row_is_caught(monkeypatch):
    text = "❯ go\n● Bash(ls)\n  ⎿  a b\n  ⎿  API Error: 529 overloaded\n❯ "
    assert screen(monkeypatch, text) == "API Error: 529 overloaded"
