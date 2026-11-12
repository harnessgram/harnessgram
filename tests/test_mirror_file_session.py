"""`ctb mirror --file` without --session goes to the caller's own topic, and a failure is said out loud."""
import pytest
from conftest import GOOD_TOKEN
from ctb import cli, config as C, decisions, mirror
from ctb.config import Config
from ctb.telegram import TelegramError


@pytest.fixture
def sends(monkeypatch):
    calls = []
    monkeypatch.setattr(C, "load", lambda: Config(bot_token=GOOD_TOKEN, owner_chat_id=1))
    monkeypatch.setattr(mirror, "run_file", lambda cfg, title, session, path, caption, audio_title, performer: calls.append((title, session)) or 0)
    return calls


def test_a_file_with_no_session_goes_to_the_callers_own_session(sends, monkeypatch):
    monkeypatch.setattr(decisions, "caller_session", lambda: "sess-mine")
    assert cli.main(["mirror", "--file", "x.png"]) == 0
    assert sends == [("Claude", "sess-mine")]
    assert cli.main(["mirror", "--session", "sess-other", "--file", "x.png"]) == 0      # said: as before
    assert cli.main(["mirror", "--title", "Build", "--file", "x.png"]) == 0
    assert sends[1:] == [("Claude", "sess-other"), ("Build", "Build")]


def test_outside_a_session_it_refuses_instead_of_using_a_general_topic(sends, monkeypatch, capsys):
    def nope():
        raise decisions.DecisionError("no session: run it inside a Claude Code session")
    monkeypatch.setattr(decisions, "caller_session", nope)
    assert cli.main(["mirror", "--file", "x.png"]) == 1
    assert sends == [] and "--session" in capsys.readouterr().err


def test_a_short_flood_wait_is_retried_once_and_a_failure_says_why(monkeypatch, capsys):
    tries, slept = [], []

    class Sender:
        def send_file(self, *a):
            tries.append(1)
            if len(tries) == 1:
                raise TelegramError("Too Many Requests: retry after 3", 429, 3)
    monkeypatch.setattr(mirror, "build", lambda cfg: Sender())
    monkeypatch.setattr(mirror.time, "sleep", slept.append)
    assert mirror.run_file(Config(bot_token=GOOD_TOKEN, owner_chat_id=1), "t", "s", "x.png", "") == 0
    assert len(tries) == 2 and slept == [3.5]

    class Never:
        def send_file(self, *a):
            raise TelegramError("Too Many Requests: retry after 90", 429, 90)
    monkeypatch.setattr(mirror, "build", lambda cfg: Never())
    assert mirror.run_file(Config(bot_token=GOOD_TOKEN, owner_chat_id=1), "t", "s", "x.png", "") == 1
    assert "was not sent" in capsys.readouterr().err and slept == [3.5]
