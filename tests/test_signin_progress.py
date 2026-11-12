"""A Claude sign-in link in the progress card: a note there, the whole link as a message of its own."""
import html
import json

from conftest import GOOD_TOKEN, FakeBot
import pytest

from ctb import hooks, i18n, progress as P
from ctb.config import Config, state_dir
from ctb.topics import PLACEHOLDER, TopicMap

from test_handoff_links import OAUTH

OWNER = 7
NOTE = "(sign-in link sent separately)"


@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.setattr(i18n, "_current_lang", "en")


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="en", ui_language="en", progress=True)


def call(link, i=0):
    return {"tool_name": "Bash", "tool_input": {"command": f"claude auth login   # {link}", "description": f"login {i}"},
            "tool_response": {"stdout": f"Visit:\n{link}\nPaste code here", "stderr": ""}, "transcript_path": ""}


def rows(session="s"):
    path = state_dir() / "progress" / f"{session}.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_detail_and_label_show_a_note_not_the_link():
    links = []
    shown = P.detail(call(OAUTH), links)
    assert NOTE in shown and "state=" not in shown and "…" not in shown and links == [OAUTH]
    data = call("x")
    data["tool_input"] = {"command": f"echo {OAUTH}"}
    assert "oauth" not in P.label(data) and "state=" not in P.label(data)


def test_append_progress_queues_the_link_once_and_hides_it_everywhere_else(monkeypatch):
    monkeypatch.setattr(hooks, "spawn", lambda *a, **k: None)
    c = cfg()
    P.append_progress("s", call(OAUTH, 1), c)
    P.append_progress("s", call(OAUTH, 2), c)
    got = rows()
    assert [r["url"] for r in got if r["kind"] == "link"] == [OAUTH]          # not queued twice
    for r in got:
        if r["kind"] != "link":
            blob = json.dumps(r)
            assert "client_id" not in blob and "code_challenge" not in blob and "state=" not in blob, r
    assert any(NOTE in json.dumps(r, ensure_ascii=False) for r in got)


def test_the_owner_gets_the_link_whole_as_its_own_message_and_the_card_only_the_note(monkeypatch):
    monkeypatch.setattr(hooks, "spawn", lambda *a, **k: None)
    c, bot = cfg(), FakeBot()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    P.append_progress("s", call(OAUTH), c)
    bot.calls.clear()
    P.run_send_progress(c, "s", bot=bot, throttle=0, now_fn=lambda: 1000.0, fast=True)
    sent = [p for m, p in bot.calls if m in ("sendMessage", "editMessageText") and p["text"] != PLACEHOLDER]
    link_msgs = [p for p in sent if OAUTH in html.unescape(p["text"])]
    assert len(link_msgs) == 1 and link_msgs[0]["chat_id"] == OWNER and "…" not in link_msgs[0]["text"]
    assert link_msgs[0]["parse_mode"] == "HTML" and html.escape(OAUTH) in link_msgs[0]["text"]
    cards = [p["text"] for p in sent if p not in link_msgs]
    assert cards and all("state=" not in t and "code_challenge" not in t for t in cards)
    assert any(NOTE in t for t in cards)
    assert OAUTH not in json.dumps(state_dir().joinpath("progress", "s.msg").read_text())


def _rows_for(session, *rows):
    path = state_dir() / "progress"
    path.mkdir(parents=True, exist_ok=True)
    with open(path / f"{session}.jsonl", "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _link_msgs(bot):
    return [p for m, p in bot.calls if m == "sendMessage" and OAUTH in html.unescape(p["text"])]


def test_a_failed_card_flush_does_not_hold_back_the_link(monkeypatch):
    c, bot = cfg(), FakeBot()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    _rows_for("s", {"kind": "action", "t": 1.0, "l": "step"}, {"kind": "link", "t": 2.0, "url": OAUTH})
    bot.calls.clear()
    bot.fail["sendMessage"] = "boom"           # the card cannot be shown
    P.run_send_progress(c, "s", bot=bot, throttle=0, now_fn=lambda: 1000.0, fast=True)
    assert len(_link_msgs(bot)) == 1
    P.run_end_progress(c, "s", bot=bot)         # the turn ends: the link is not sent a second time
    assert len(_link_msgs(bot)) == 1


def test_a_link_not_sent_before_the_turn_ends_is_sent_by_the_end(monkeypatch):
    c, bot = cfg(), FakeBot()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    _rows_for("s", {"kind": "action", "t": 1.0, "l": "step"}, {"kind": "link", "t": 2.0, "url": OAUTH})
    bot.calls.clear()
    P.run_end_progress(c, "s", bot=bot)
    assert len(_link_msgs(bot)) == 1 and _link_msgs(bot)[0]["chat_id"] == OWNER
    assert not (state_dir() / "progress" / "s.jsonl").exists()


def test_a_sender_that_failed_on_the_link_leaves_it_for_the_end(monkeypatch):
    c, bot = cfg(), FakeBot()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    _rows_for("s", {"kind": "link", "t": 2.0, "url": OAUTH})
    bot.calls.clear()
    bot.fail["sendMessage"] = "boom"           # the link send itself fails once
    P.run_send_progress(c, "s", bot=bot, throttle=0, now_fn=lambda: 1000.0, fast=True)
    P.run_end_progress(c, "s", bot=bot)
    assert len(_link_msgs(bot)) == 1


def test_a_sample_link_is_hidden_on_the_card_but_never_queued_for_the_owner():
    sample = OAUTH.replace("S" * 43, "short")
    links: list = []
    out = P.hide_signin(f"Open {sample}", links)
    assert links == [] and "state=" not in out and "oauth" not in out
