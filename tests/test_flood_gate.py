"""The shared flood gate: one view of Telegram's group limit for every sender and process."""
import io
import json
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import flood, heartbeat, progress as P
from ctb.config import Config, state_dir
from ctb.projects import Place
from ctb.telegram import Bot, TelegramError
from ctb.topics import PLACEHOLDER, TopicMap, session_state, set_state

OWNER = 7


def cfg(**kw):
    base = dict(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="en")
    base.update(kw)
    return Config(**base)


def http_429(retry_after):
    body = json.dumps({"ok": False, "error_code": 429, "description": "Too Many Requests",
                       "parameters": {"retry_after": retry_after}}).encode()
    return urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, io.BytesIO(body))


class Opener:
    """Stands in for urllib's opener: counts requests and answers 429 or ok."""

    def __init__(self, retry_after=None):
        self.retry_after, self.requests = retry_after, 0

    def open(self, req, timeout=None):
        self.requests += 1
        if self.retry_after is not None:
            raise http_429(self.retry_after)
        return io.BytesIO(json.dumps({"ok": True, "result": {"message_id": 1}}).encode())


# 1. the gate closes on a 429 and opens after retry_after ------------------------------------------

def test_gate_closes_on_429_and_opens_after_retry_after(monkeypatch):
    opener = Opener(retry_after=40)
    monkeypatch.setattr("ctb.telegram._OPENER", opener)
    monkeypatch.setattr("ctb.telegram.time.sleep", lambda s: None)
    bot = Bot(GOOD_TOKEN)
    assert flood.is_open(OWNER)
    with pytest.raises(TelegramError) as err:
        bot.call("sendMessage", {"chat_id": OWNER, "text": "hi"})
    assert err.value.code == 429
    now = time.time()
    assert 35 < flood.remaining(OWNER, now) <= 40
    assert not flood.is_open(OWNER, now)
    assert flood.is_open(OWNER, now + 41)  # opens once retry_after has passed
    assert flood.is_open(OWNER + 1, now)  # another chat is unaffected
    assert oct((state_dir() / flood.STATE_FILE).stat().st_mode & 0o777) == "0o600"


def test_a_longer_pause_is_never_shortened_by_a_shorter_one():
    flood.record_429(OWNER, 60, now=1000.0)
    flood.record_429(OWNER, 5, now=1001.0)
    assert flood.remaining(OWNER, now=1002.0) == pytest.approx(58.0)


# 2. optional sends skip with no network call; essential ones still try -----------------------------

def test_optional_sends_skip_while_closed_without_a_network_call(monkeypatch):
    opener = Opener()
    monkeypatch.setattr("ctb.telegram._OPENER", opener)
    bot = Bot(GOOD_TOKEN)
    flood.record_429(OWNER, 30)
    with pytest.raises(TelegramError) as err:
        flood.optional_call(bot, "sendMessage", {"chat_id": OWNER, "text": "card"})
    assert getattr(err.value, "skipped", False) and err.value.code == 429
    assert opener.requests == 0
    # an essential send is still attempted, and succeeds when Telegram lets it through
    assert bot.call("sendMessage", {"chat_id": OWNER, "text": "reply"}) == {"message_id": 1}
    assert opener.requests == 1


def test_an_optional_call_does_not_sleep_through_a_429_but_an_essential_one_waits_briefly(monkeypatch):
    slept = []
    monkeypatch.setattr("ctb.telegram.time.sleep", slept.append)
    monkeypatch.setattr("ctb.telegram._OPENER", Opener(retry_after=2))
    bot = Bot(GOOD_TOKEN)
    with pytest.raises(TelegramError):
        flood.optional_call(bot, "sendMessage", {"chat_id": OWNER, "text": "card"})
    assert slept == []
    with pytest.raises(TelegramError):
        bot.call("sendMessage", {"chat_id": OWNER, "text": "reply"})
    assert slept == [2.0, 2.0]  # the existing short-wait rule, unchanged


def test_optional_budget_is_per_minute_and_gate_only_calls_ignore_it():
    now = 5000.0
    assert all(flood.allow_optional(OWNER, now=now + i) for i in range(flood.OPTIONAL_PER_MIN))
    assert not flood.allow_optional(OWNER, now=now + 20)
    assert flood.allow_optional(OWNER, budget=False, now=now + 20)  # reactions and forced flushes
    assert flood.allow_optional(OWNER, now=now + 61)  # the minute has moved on


# 3. progress coalescing: one send and few edits instead of one call per tool call ------------------

def write_rows(session, rows):
    path = state_dir() / "progress"
    path.mkdir(parents=True, exist_ok=True)
    with open(path / f"{session}.jsonl", "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def actions(n, start=0):
    return [{"kind": "action", "t": 1.0 + i, "l": f"⚙️ step {i}"} for i in range(start, start + n)]


def test_progress_coalesces_tool_calls_into_one_send_then_paced_edits():
    bot = FakeBot()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    clock = [1000.0]
    c = cfg()
    write_rows("s", actions(8))
    bot.calls.clear()
    P.run_send_progress(c, "s", bot=bot, throttle=0, now_fn=lambda: clock[0])
    cards = [p for m, p in bot.calls if m in ("sendMessage", "editMessageText") and p.get("parse_mode") == "HTML"]
    assert [m for m, p in bot.calls if p.get("parse_mode") == "HTML"] == ["sendMessage"]  # 8 calls, 1 message
    assert "step 0" in cards[0]["text"] and "step 7" in cards[0]["text"]

    # more tool calls within the pacing window cost nothing yet ...
    write_rows("s", actions(5, start=8))
    bot.calls.clear()
    clock[0] += 3
    P.run_send_progress(c, "s", bot=bot, throttle=0, now_fn=lambda: clock[0])
    assert bot.methods("editMessageText") == [] and bot.methods("sendMessage") == []
    # ... and arrive in a single edit once MIN_EDIT has passed
    clock[0] += P.MIN_EDIT
    P.run_send_progress(c, "s", bot=bot, throttle=0, now_fn=lambda: clock[0])
    edits = bot.methods("editMessageText")
    assert len(edits) == 1 and "step 12" in edits[0]["text"] and "step 0" in edits[0]["text"]
    assert bot.methods("sendMessage") == []


def test_text_between_tools_keeps_the_order_and_flushes_the_card_first():
    bot = FakeBot()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    clock = [1000.0]
    c = cfg()
    write_rows("s", actions(2) + [{"kind": "text", "uuid": "u1", "text": "Now the second phase"}]
               + actions(2, start=2))
    bot.calls.clear()
    P.run_send_progress(c, "s", bot=bot, throttle=0, now_fn=lambda: clock[0])
    texts = [p["text"] for m, p in bot.calls if m == "sendMessage" and p["text"] != PLACEHOLDER]
    assert "step 0" in texts[0] and "step 1" in texts[0]  # card for the first tools
    assert texts[1] == "Now the second phase"
    assert "step 2" in texts[2] and "step 0" not in texts[2]  # a new card for the later tools
    assert len(texts) == 3


def test_a_closed_gate_keeps_the_card_pending_and_it_goes_out_when_it_opens():
    bot = FakeBot()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    clock = [1000.0]
    c = cfg()
    write_rows("s", actions(3))
    flood.record_429(OWNER, 30, now=time.time())
    bot.calls.clear()
    P.run_send_progress(c, "s", bot=bot, throttle=0, now_fn=lambda: clock[0])
    assert [m for m, _ in bot.calls if m in ("sendMessage", "editMessageText")] == []  # no network call
    (state_dir() / flood.STATE_FILE).unlink()  # the gate opens
    P.run_send_progress(c, "s", bot=bot, throttle=0, now_fn=lambda: clock[0])
    sent = [p for m, p in bot.calls if m == "sendMessage" and p.get("parse_mode") == "HTML"]
    assert len(sent) == 1 and "step 2" in sent[0]["text"]


# 4. the budget is shared across processes ---------------------------------------------------------

def test_the_budget_is_shared_by_separate_processes(tmp_path):
    src = str(Path(flood.__file__).resolve().parent.parent)
    code = ("import sys, json; sys.path.insert(0, %r)\n"
            "from ctb import flood\n"
            "print(sum(flood.allow_optional(7) for _ in range(10)))\n") % src
    env = {"CTB_STATE_DIR": str(state_dir()), "CTB_CONFIG_DIR": str(tmp_path / "config"),
           "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    procs = [subprocess.Popen([sys.executable, "-I", "-c", code], env=env, stdout=subprocess.PIPE, text=True)
             for _ in range(2)]
    granted = [int(p.communicate(timeout=30)[0]) for p in procs]
    assert sum(granted) == flood.OPTIONAL_PER_MIN  # 20 asked, the minute's budget granted exactly once
    assert not flood.allow_optional(OWNER)  # and this process sees the same spent budget


# 5. background senders back off while the gate is closed ------------------------------------------

def test_heartbeat_makes_no_calls_while_the_chat_is_flooded(monkeypatch):
    bot = FakeBot()
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    set_state(c, "s", "busy", bot=bot)
    monkeypatch.setattr(heartbeat, "working", lambda *a, **k: True)
    flood.record_429(OWNER, 30)
    bot.calls.clear()
    assert heartbeat.run_once(c, bot=bot) == 0
    assert bot.calls == []
    (state_dir() / flood.STATE_FILE).unlink()
    assert heartbeat.run_once(c, bot=bot) == 1 and bot.calls


def test_a_placeholder_refresh_is_paid_for_up_front_or_skipped_whole():
    bot = FakeBot()
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    set_state(c, "s", "busy", bot=bot)
    placeholder = session_state("s")["placeholder"]
    for _ in range(flood.OPTIONAL_PER_MIN):
        flood.allow_optional(OWNER)  # the minute's budget is gone
    bot.calls.clear()
    set_state(c, "s", "bottom", bot=bot, optional=True)
    assert bot.calls == [] and session_state("s")["placeholder"] == placeholder  # not deleted, not re-sent


# 6. review fixes ----------------------------------------------------------------------------------

def test_receipt_reactions_are_never_gated_and_run_delivered_keeps_acks_it_could_not_confirm(monkeypatch):
    from ctb import delivery, inbox
    bot = FakeBot()
    flood.record_429(OWNER, 30)
    assert inbox.react(bot, OWNER, {"message_id": 5}, "\U0001F44C") is True  # delivered: plain call
    assert [m for m, _ in bot.calls] == ["setMessageReaction"]
    bot.calls.clear()
    assert inbox.react(bot, OWNER, {"message_id": 5}, "✍", optional=True) is False  # decorative: skipped
    assert bot.calls == []
    dropped = []
    monkeypatch.setattr(delivery, "drop_acks", lambda b, c, mids, chat=None: dropped.append(list(mids)))
    bot.fail["setMessageReaction"] = "Bad Request: message to react not found"
    assert inbox.run_delivered(cfg(), [f"{OWNER}:1", f"{OWNER}:2"], bot=bot) == 0
    assert dropped == [[2]]  # message 1's reaction failed, so its receipt stays


def test_bad_state_values_never_replace_the_real_error(monkeypatch):
    path = state_dir() / flood.STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"until": {"-1": "x", "7": None}, "sent": {"7": ["a", None, 3], "8": 5}}))
    flood.record_429(OWNER, 10)  # must not raise
    assert flood.remaining(OWNER) > 0 and flood.allow_optional(8)
    monkeypatch.setattr("ctb.telegram._OPENER", Opener(retry_after=40))
    monkeypatch.setattr("ctb.telegram.time.sleep", lambda s: None)
    monkeypatch.setattr(flood, "record_429", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    with pytest.raises(TelegramError) as err:
        Bot(GOOD_TOKEN).call("sendMessage", {"chat_id": OWNER, "text": "x"})
    assert err.value.code == 429  # the 429 itself, not the ValueError from the bookkeeping


def test_typing_uses_the_gate_but_not_the_budget(monkeypatch):
    from ctb import hooks, mirror
    bot = FakeBot()
    monkeypatch.setattr(mirror.Bot, "from_config", classmethod(lambda cls, c: bot))
    monkeypatch.setattr(mirror.TopicMap, "place_of", lambda self, s: Place(self.chat_id, 11))
    assert hooks.TYPING_EVERY >= 15
    for _ in range(flood.OPTIONAL_PER_MIN):
        flood.allow_optional(OWNER)
    assert mirror.run_typing(cfg(), "s") == 0 and [m for m, _ in bot.calls] == ["sendChatAction"]
    bot.calls.clear()
    flood.record_429(OWNER, 30)
    mirror.run_typing(cfg(), "s")
    assert bot.calls == []


def test_clock_jumps_cannot_wedge_the_gate_or_the_budget():
    now = 10_000.0
    path = state_dir() / flood.STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"until": {str(OWNER): now + 86400}, "sent": {str(OWNER): [now + 3600] * 50}}))
    assert flood.is_open(OWNER, now)  # an `until` further away than any real pause is a clock jump
    assert flood.allow_optional(OWNER, now=now)  # entries from the future do not count


def test_step_end_makes_one_last_attempt_to_show_a_pending_card():
    bot = FakeBot()
    TopicMap(state_dir() / "topics.json", bot, OWNER).ensure("s", "T")
    c = cfg()
    pdir = state_dir() / "progress"
    pdir.mkdir(parents=True, exist_ok=True)
    state = {"cursor": 3, "cards": ["✔️ <b>step 2</b>"], "id": 55, "dirty": True}

    def end(gate_closed, spent=False):
        (pdir / "s.msg").write_text(json.dumps(state))
        (pdir / "s.jsonl").write_text("")
        (state_dir() / flood.STATE_FILE).unlink(missing_ok=True)
        if gate_closed:
            flood.record_429(OWNER, 30)
        if spent:
            for _ in range(flood.OPTIONAL_PER_MIN):
                flood.allow_optional(OWNER)
        bot.calls.clear()
        assert P.run_end_progress(c, "s", bot=bot) == 0
        assert not (pdir / "s.msg").exists()  # the state is always cleaned up
        return bot.methods("editMessageText")

    edits = end(gate_closed=False, spent=True)  # budget spent: the final edit ignores it
    assert len(edits) == 1 and edits[0]["message_id"] == 55 and "step 2" in edits[0]["text"]
    assert end(gate_closed=True) == []  # a closed gate still means no call
