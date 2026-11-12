"""Tests for the Oct 30 ported live behaviours:
- full_width() button row splitting in telegram API wrapper ()
- live_keyboard() footer extraction and status_message() in mirror ()
- compaction-aware context in status.usage_of() and wake.refresh_status() ()
- cold_compact() idle context compaction in wake ()
"""
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ctb import compact
from ctb.config import Config, state_dir, write_private
from ctb.inbox import Inbox
from ctb.mirror import KEYBOARD, live_keyboard, status_message
from ctb.status import usage_of
from ctb.telegram import Bot, full_width
from ctb.topics import TopicMap
from ctb.wake import (
    COLD_AFTER,
    COLD_MIN,
    auto_compact_limit,
    cold_compact,
    refresh_status,
    wait_for_message,
)

OWNER = 123456789


def cfg():
    return Config(bot_token="123456:" + "A" * 35, owner_chat_id=OWNER)


# ---------------------------------------------------------------------------
# 1. full_width() tests
# ---------------------------------------------------------------------------

def test_full_width_non_dict_or_missing_markup():
    # graceful fallback when no inline keyboard exists
    assert full_width(None) is None
    assert full_width("string") == "string"
    assert full_width({}) == {}
    assert full_width({"text": "hello"}) == {"text": "hello"}
    assert full_width({"reply_markup": {}}) == {"reply_markup": {}}
    assert full_width({"reply_markup": {"keyboard": []}}) == {"reply_markup": {"keyboard": []}}


def test_full_width_single_button_rows_remain_unchanged():
    # single-button row is not split even with a long label
    params = {
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "A very long single action button", "callback_data": "1"}]
            ]
        }
    }
    res = full_width(params)
    assert res["reply_markup"]["inline_keyboard"] == [
        [{"text": "A very long single action button", "callback_data": "1"}]
    ]


def test_full_width_short_labels_stay_side_by_side():
    # only rows of tiny labels (digits, yes/no <= 6 chars) stay side by side
    params = {
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "Yes", "callback_data": "y"}, {"text": "No", "callback_data": "n"}],
                [{"text": "123456", "callback_data": "1"}, {"text": "abcdef", "callback_data": "2"}],
            ]
        }
    }
    res = full_width(params)
    assert res["reply_markup"]["inline_keyboard"] == [
        [{"text": "Yes", "callback_data": "y"}, {"text": "No", "callback_data": "n"}],
        [{"text": "123456", "callback_data": "1"}, {"text": "abcdef", "callback_data": "2"}],
    ]


def test_full_width_multi_button_row_split_when_any_label_exceeds_six_chars():
    # one button per row so labels are never cut on the phone
    params = {
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "Short", "callback_data": "s"}, {"text": "LongerThanSix", "callback_data": "l"}]
            ]
        }
    }
    res = full_width(params)
    assert res["reply_markup"]["inline_keyboard"] == [
        [{"text": "Short", "callback_data": "s"}],
        [{"text": "LongerThanSix", "callback_data": "l"}],
    ]


def test_full_width_mixed_rows():
    # row 1 stays together, row 2 splits, row 3 is single
    params = {
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "1", "callback_data": "1"}, {"text": "2", "callback_data": "2"}],
                [{"text": "Approve", "callback_data": "ok"}, {"text": "Cancel", "callback_data": "no"}],
                [{"text": "Full row button", "callback_data": "full"}],
            ]
        }
    }
    res = full_width(params)
    assert res["reply_markup"]["inline_keyboard"] == [
        [{"text": "1", "callback_data": "1"}, {"text": "2", "callback_data": "2"}],
        [{"text": "Approve", "callback_data": "ok"}],
        [{"text": "Cancel", "callback_data": "no"}],
        [{"text": "Full row button", "callback_data": "full"}],
    ]


def test_full_width_applied_in_bot_call(monkeypatch):
    # applied in the API send wrapper
    bot = Bot("123456:" + "A" * 35)
    captured = {}

    def fake_request(url, data, headers, timeout):
        captured["url"] = url
        captured["body"] = json.loads(data.decode("utf-8"))
        return {"ok": True, "result": {"message_id": 99}}

    monkeypatch.setattr(bot, "_request", fake_request)
    bot.call("sendMessage", {
        "chat_id": 123,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "Short", "callback_data": "s"}, {"text": "SevenChars", "callback_data": "7"}]
            ]
        }
    })
    assert captured["body"]["reply_markup"]["inline_keyboard"] == [
        [{"text": "Short", "callback_data": "s"}],
        [{"text": "SevenChars", "callback_data": "7"}],
    ]


# ---------------------------------------------------------------------------
# 2. live_keyboard() and status_message() tests
# ---------------------------------------------------------------------------

def test_live_keyboard_extracts_context_and_claude_quota():
    # info row (context thousands, Claude 5h/week %) on fixed reply keyboard
    footer = "📏 הקשר 120 אלף\nClaude 45% · Claude 20%"
    kb = live_keyboard(footer)
    assert kb["keyboard"][0] == [
        {"text": "📏 120 אלף"},
        {"text": "⏱️ 45% · 📅 20%"},
    ]
    assert kb["input_field_placeholder"] == "📏 120 אלף · ⏱️ 45% · 📅 20%"
    # base buttons follow below the info row
    assert kb["keyboard"][1:] == KEYBOARD["keyboard"]


def test_live_keyboard_extracts_context_brain_icon():
    # extracts context with brain emoji fallback
    footer = "🧠 150k\nClaude 50%"
    kb = live_keyboard(footer)
    assert kb["keyboard"][0] == [
        {"text": "📏 150 אלף"},
        {"text": "⏱️ 50%"},
    ]


def test_live_keyboard_regex_accepts_named_accounts():
    # regex accepts "Claude <account> NN%"
    footers = [
        "Claude main 52%",
        "Claude dev-team_1 65%",
        "Claude accountA 70% · Claude accountA 30%",
    ]
    kb1 = live_keyboard(footers[0])
    assert kb1["keyboard"][0] == [{"text": "⏱️ 52%"}]

    kb2 = live_keyboard(footers[1])
    assert kb2["keyboard"][0] == [{"text": "⏱️ 65%"}]

    kb3 = live_keyboard(footers[2])
    assert kb3["keyboard"][0] == [{"text": "⏱️ 70% · 📅 30%"}]


def test_live_keyboard_empty_or_plain_footer_keeps_default_keyboard():
    # no info row when footer has no context or quota
    kb = live_keyboard("")
    assert kb["keyboard"] == KEYBOARD["keyboard"]
    assert "input_field_placeholder" not in kb

    kb2 = live_keyboard("פשוט טקסט ללא מספרים")
    assert kb2["keyboard"] == KEYBOARD["keyboard"]
    assert "input_field_placeholder" not in kb2


def test_live_keyboard_custom_base_keyboard():
    # custom base keyboard preserved
    custom = {"keyboard": [[{"text": "Custom Button"}]]}
    kb = live_keyboard("הקשר 80 אלף", base_kb=custom)
    assert kb["keyboard"][0] == [{"text": "📏 80 אלף"}]
    assert kb["keyboard"][1] == [{"text": "Custom Button"}]


def test_status_message_sends_and_deletes_previous_message(tmp_path, bot):
    # With "new card after every reply" on, the status message in the topic is replaced on each update.
    # (Without it the card is edited in place: see tests/test_port_1101_later.py.)
    from ctb.config import STATUS_REPOST, set_setting
    set_setting(STATUS_REPOST, True)
    c = cfg()
    msg1 = status_message(c, thread=10, footer="הקשר 50 אלף", keyboard=KEYBOARD, bot=bot)
    assert msg1 is not None
    assert msg1.get("message_id") == 1

    footer_file = state_dir() / "status-footer.json"
    assert footer_file.exists()
    ids = json.loads(footer_file.read_text())
    assert ids.get("10") == 1

    # second status update deletes previous message_id (1) and writes new one
    msg2 = status_message(c, thread=10, footer="הקשר 75 אלף", keyboard=KEYBOARD, bot=bot)
    assert msg2 is not None
    assert msg2.get("message_id") == 2  # sendMessage (1), sendMessage (2), deleteMessage (3)
    ids = json.loads(footer_file.read_text())
    assert ids.get("10") == 2

    # check calls made
    deleted = [p for m, p in bot.calls if m == "deleteMessage"]
    assert len(deleted) == 1
    assert deleted[0]["chat_id"] == OWNER
    assert deleted[0]["message_id"] == 1


# ---------------------------------------------------------------------------
# 3. Compaction-aware context tests
# ---------------------------------------------------------------------------

def test_usage_of_normal_transcript(tmp_path):
    # normal transcript assistant usage
    t = tmp_path / "transcript.jsonl"
    record = {
        "message": {
            "role": "assistant",
            "model": "claude-3-7-sonnet",
            "usage": {
                "input_tokens": 1000,
                "cache_read_input_tokens": 20000,
                "cache_creation_input_tokens": 4000,
            },
        }
    }
    t.write_text(json.dumps(record) + "\n", encoding="utf-8")
    ctx, model = usage_of(str(t))
    assert ctx == 25000
    assert model == "claude-3-7-sonnet"


def test_usage_of_compact_boundary_newer_than_assistant(tmp_path):
    # compactMetadata.postTokens when compact_boundary is newer than assistant
    t = tmp_path / "transcript.jsonl"
    r1 = {
        "message": {
            "role": "assistant",
            "model": "claude-3-7-sonnet",
            "usage": {
                "input_tokens": 50000,
                "cache_read_input_tokens": 100000,
                "cache_creation_input_tokens": 0,
            },
        }
    }
    r2 = {
        "compact_boundary": True,
        "compactMetadata": {
            "postTokens": 18200,
        },
    }
    t.write_text(json.dumps(r1) + "\n" + json.dumps(r2) + "\n", encoding="utf-8")
    ctx, model = usage_of(str(t))
    assert ctx == 18200
    assert model == ""


def test_usage_of_assistant_newer_than_compact_boundary(tmp_path):
    # new assistant record after compact_boundary takes precedence
    t = tmp_path / "transcript.jsonl"
    r1 = {"compact_boundary": True, "compactMetadata": {"postTokens": 18200}}
    r2 = {
        "message": {
            "role": "assistant",
            "model": "claude-3-7-sonnet",
            "usage": {
                "input_tokens": 22000,
                "cache_read_input_tokens": 500,
                "cache_creation_input_tokens": 0,
            },
        }
    }
    t.write_text(json.dumps(r1) + "\n" + json.dumps(r2) + "\n", encoding="utf-8")
    ctx, model = usage_of(str(t))
    assert ctx == 22500
    assert model == "claude-3-7-sonnet"


def test_usage_of_missing_or_corrupt_file(tmp_path):
    assert usage_of(str(tmp_path / "nonexistent.jsonl")) == (0, "")

    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text("invalid json line\n{also corrupt\n", encoding="utf-8")
    assert usage_of(str(corrupt)) == (0, "")


def test_wake_refresh_status_re_sends_status_after_compaction(tmp_path, bot):
    # wake refresh_status() re-sends the status right after compaction
    c = cfg()
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure("sess-comp", "Compacted Session")

    # transcript with compact_boundary
    t = tmp_path / "transcript.jsonl"
    r = {"compact_boundary": True, "compactMetadata": {"postTokens": 35000}}
    t.write_text(json.dumps(r) + "\n", encoding="utf-8")

    ok = refresh_status("sess-comp", str(t), cfg=c, bot=bot)
    assert ok is True

    sent = bot.methods("sendMessage")
    assert len(sent) == 1
    assert sent[0]["message_thread_id"] == thread
    assert "📏 הקשר 35 אלף" in sent[0]["text"]


def test_wake_refresh_status_advises_compaction_above_400k(tmp_path, bot):
    # tips to compact when context >= 400k
    c = cfg()
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    topics.ensure("sess-big", "Big Session")

    t = tmp_path / "transcript.jsonl"
    r = {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"postTokens": 420000}}
    t.write_text(json.dumps(r) + "\n", encoding="utf-8")

    assert refresh_status("sess-big", str(t), cfg=c, bot=bot) is True
    sent = bot.methods("sendMessage")
    assert "כדאי לדחוס" in sent[0]["text"]


# ---------------------------------------------------------------------------
# 4. Auto compaction and cold_compact() tests
# ---------------------------------------------------------------------------

def test_auto_compact_limit():
    # reads state_dir() / "auto-compact"
    assert auto_compact_limit() == 0

    p = state_dir() / "auto-compact"
    write_private(p, "250\n")
    assert auto_compact_limit() == 250

    write_private(p, "0\n")
    assert auto_compact_limit() == 0

    write_private(p, "invalid\n")
    assert auto_compact_limit() == 0


def test_cold_compact_not_idle_enough(tmp_path):
    # idle < 1h does not trigger cold compaction
    now = 10_000.0
    idle_since = now - 1800  # 30 minutes idle
    write_private(state_dir() / "auto-compact", "200")
    t = tmp_path / "transcript.jsonl"
    assert cold_compact("s", str(t), idle_since, clock=lambda: now) is False


def test_cold_compact_auto_compact_disabled(tmp_path):
    # auto compaction turned off (limit 0)
    now = 10_000.0
    idle_since = now - COLD_AFTER - 100
    write_private(state_dir() / "auto-compact", "0")
    t = tmp_path / "transcript.jsonl"
    assert cold_compact("s", str(t), idle_since, clock=lambda: now) is False


def test_cold_compact_context_below_threshold(tmp_path):
    # context < 150k does not trigger cold compaction
    now = 10_000.0
    idle_since = now - COLD_AFTER - 100
    write_private(state_dir() / "auto-compact", "200")

    t = tmp_path / "transcript.jsonl"
    r = {"message": {"role": "assistant", "usage": {"input_tokens": 100_000}}}
    t.write_text(json.dumps(r) + "\n", encoding="utf-8")

    assert cold_compact("s", str(t), idle_since, clock=lambda: now) is False


def test_cold_compact_starts_and_waits_for_marker(tmp_path):
    # idle > 1h and ctx >= 150k -> compact first, marker appears -> True
    now = 10_000.0
    idle_since = now - COLD_AFTER - 100
    write_private(state_dir() / "auto-compact", "200")

    t = tmp_path / "transcript.jsonl"
    r = {"message": {"role": "assistant", "usage": {"input_tokens": 160_000}}}
    t.write_text(json.dumps(r) + "\n", encoding="utf-8")

    spawn_called = []

    def fake_spawn(session, ctx):
        spawn_called.append((session, ctx))
        m = compact.marker(session)
        m.parent.mkdir(parents=True, exist_ok=True)
        m.write_text("1")

    res = cold_compact(
        "sess-cold",
        str(t),
        idle_since,
        clock=lambda: now,
        spawn_fn=fake_spawn,
    )
    assert res is True
    assert spawn_called == [("sess-cold", 160_000)]


def test_cold_compact_times_out_and_falls_back(tmp_path):
    # falls back to delivery if no compaction marker within 30 s
    current_time = [10_000.0]

    def fake_clock():
        return current_time[0]

    def fake_sleep(secs):
        current_time[0] += 10.0  # advance clock past 30s timeout

    idle_since = fake_clock() - COLD_AFTER - 100
    write_private(state_dir() / "auto-compact", "200")

    t = tmp_path / "transcript.jsonl"
    r = {"message": {"role": "assistant", "usage": {"input_tokens": 160_000}}}
    t.write_text(json.dumps(r) + "\n", encoding="utf-8")

    # spawn does not write any marker
    res = cold_compact(
        "sess-timeout",
        str(t),
        idle_since,
        timeout=30.0,
        clock=fake_clock,
        sleep=fake_sleep,
        spawn_fn=lambda s, c: None,
    )
    assert res is False


def test_wait_for_message_cold_compact_delays_delivery(tmp_path, monkeypatch):
    # when cold_compact succeeds, message stays queued (waiter returns 0)
    inbox = Inbox(tmp_path / "inbox")
    inbox.put("sess-wait", {"text": "Hello owner"})

    # mock cold_compact in wake to return True
    import ctb.wake as W
    monkeypatch.setattr(W, "cold_compact", lambda session, transcript, idle_since: True)

    data = {"session_id": "sess-wait", "transcript_path": str(tmp_path / "t.jsonl")}
    code, text = wait_for_message(data, inbox=inbox, settle=0, max_wait=1)
    assert code == 0
    assert text == ""
    # message was NOT consumed; remains in inbox for post-compaction waiter
    assert inbox.pending("sess-wait") is True
    assert len(inbox.take("sess-wait")) == 1


def test_wait_for_message_cold_compact_fallback_delivers_immediately(tmp_path, monkeypatch):
    # when cold_compact returns False, message is delivered immediately
    inbox = Inbox(tmp_path / "inbox")
    inbox.put("sess-wait2", {"text": "Deliver now"})

    import ctb.wake as W
    monkeypatch.setattr(W, "cold_compact", lambda session, transcript, idle_since: False)

    data = {"session_id": "sess-wait2", "transcript_path": str(tmp_path / "t.jsonl")}
    code, text = wait_for_message(data, inbox=inbox, settle=0, max_wait=1)
    assert code == 2
    assert "Deliver now" in text
    assert inbox.pending("sess-wait2") is False
