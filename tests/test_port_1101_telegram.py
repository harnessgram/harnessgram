"""Tests for Chunk 1 port: Telegram client errors and delivery receipts."""
import io
import json
import urllib.error
import urllib.request
import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import delivery, mirror
from ctb.config import Config, state_dir
from ctb.telegram import Bot, TelegramError

OWNER = 7


def cfg(**kw):
    base = dict(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    base.update(kw)
    return Config(**base)


class DummyHTTPResponse:
    def __init__(self, data: bytes):
        self.data = data

    def read(self, limit=None):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


def make_http_error(code: int, payload: dict) -> urllib.error.HTTPError:
    body = json.dumps(payload).encode("utf-8")
    fp = io.BytesIO(body)
    return urllib.error.HTTPError(
        url="https://api.telegram.org/bot123/sendMessage",
        code=code,
        msg="HTTP Error",
        hdrs={},
        fp=fp,
    )


# --- 1. Telegram API errors ---------------------------------------------------

def test_telegram_http_429_retries_and_succeeds(monkeypatch):
    """429 sleeps retry_after (clamped 1..30) and retries up to 3 attempts."""
    bot = Bot(GOOD_TOKEN)
    attempts = 0
    slept = []

    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))

    def fake_open(req, timeout=15):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise make_http_error(429, {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 5",
                "parameters": {"retry_after": 5},
            })
        return DummyHTTPResponse(json.dumps({"ok": True, "result": {"message_id": 999}}).encode())

    from ctb import telegram
    monkeypatch.setattr(telegram._OPENER, "open", fake_open)

    result = bot.call("sendMessage", {"chat_id": OWNER, "text": "test"})
    assert result == {"message_id": 999}
    assert attempts == 3
    assert slept == [5.0, 5.0]


def test_telegram_http_429_clamped_wait_bounds(monkeypatch):
    """A long 429 pause fails at once (never blocks the listener); a short one waits at least 1 s."""
    bot = Bot(GOOD_TOKEN)
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))

    # A 60 s pause is not slept through: the call fails with the 429 and its retry_after
    call_count = 0

    def fake_open_high(req, timeout=15):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise make_http_error(429, {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 60",
                "parameters": {"retry_after": 60},
            })
        return DummyHTTPResponse(json.dumps({"ok": True, "result": {"message_id": 1}}).encode())

    from ctb import telegram
    monkeypatch.setattr(telegram._OPENER, "open", fake_open_high)
    with pytest.raises(telegram.TelegramError) as err:
        bot.call("sendMessage", {"chat_id": OWNER, "text": "test"})
    assert (err.value.code, err.value.retry_after) == (429, 60.0) and slept == [] and call_count == 1

    # Test clamping lower bound (0.1s -> 1.0s)
    slept.clear()
    call_count = 0

    def fake_open_low(req, timeout=15):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise make_http_error(429, {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 0.1",
                "parameters": {"retry_after": 0.1},
            })
        return DummyHTTPResponse(json.dumps({"ok": True, "result": {"message_id": 2}}).encode())

    monkeypatch.setattr(telegram._OPENER, "open", fake_open_low)
    bot.call("sendMessage", {"chat_id": OWNER, "text": "test"})
    assert slept == [1.0]


def test_telegram_http_429_exhausted_raises_rejected(monkeypatch):
    """If 429 persists across all 3 attempts, TelegramError with code 429 is raised."""
    bot = Bot(GOOD_TOKEN)
    attempts = 0
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))

    def fake_open(req, timeout=15):
        nonlocal attempts
        attempts += 1
        raise make_http_error(429, {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 2},
        })

    from ctb import telegram
    monkeypatch.setattr(telegram._OPENER, "open", fake_open)

    with pytest.raises(TelegramError) as exc_info:
        bot.call("sendMessage", {"chat_id": OWNER, "text": "test"})

    assert attempts == 3
    assert len(slept) == 2  # slept after attempt 1 and attempt 2, then attempt 3 failed
    assert exc_info.value.code == 429
    assert mirror.reason_str(exc_info.value, lang="en") == "Telegram asked to slow down"
    assert mirror.reason_str(exc_info.value, lang="he") == "טלגרם ביקש להאט"


def test_telegram_http_error_rejected_thread_not_found(monkeypatch):
    """HTTP error mentions thread not found when Telegram description says so."""
    bot = Bot(GOOD_TOKEN)

    def fake_open(req, timeout=15):
        raise make_http_error(400, {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: message thread not found",
        })

    from ctb import telegram
    monkeypatch.setattr(telegram._OPENER, "open", fake_open)

    with pytest.raises(TelegramError) as exc_info:
        bot.call("sendMessage", {"chat_id": OWNER, "text": "test"})

    err = exc_info.value
    assert err.code == 400
    assert "thread not found" in err.description.lower()
    assert mirror._is_thread_error(err) is True
    assert mirror.reason_str(err, lang="en") == "Topic was deleted"
    assert mirror.reason_str(err, lang="he") == "הנושא נמחק"


def test_telegram_http_error_generic_rejected(monkeypatch):
    """Other HTTP errors report a clear rejection and keep the HTTP code, never masked as unknown."""
    bot = Bot(GOOD_TOKEN)

    def fake_open(req, timeout=15):
        raise make_http_error(403, {
            "ok": False,
            "error_code": 403,
            "description": "Forbidden: bot was blocked by the user",
        })

    from ctb import telegram
    monkeypatch.setattr(telegram._OPENER, "open", fake_open)

    with pytest.raises(TelegramError) as exc_info:
        bot.call("sendMessage", {"chat_id": OWNER, "text": "test"})

    err = exc_info.value
    assert err.code == 403
    assert "blocked" in err.description.lower()


def test_telegram_network_and_timeout_errors_remain_network_or_timeout(monkeypatch):
    """Network errors and socket timeouts stay network/timeout without an HTTP code."""
    bot = Bot(GOOD_TOKEN)
    from ctb import telegram

    # Timeout
    monkeypatch.setattr(telegram._OPENER, "open", lambda req, timeout=15: (_ for _ in ()).throw(TimeoutError("timed out")))
    with pytest.raises(TelegramError) as exc_info:
        bot.call("sendMessage", {"chat_id": OWNER, "text": "test"})
    assert exc_info.value.code is None
    assert exc_info.value.description == "timeout"
    assert mirror.reason_str(exc_info.value, lang="en") == "Telegram timed out"
    assert mirror.reason_str(exc_info.value, lang="he") == "טלגרם לא ענה בזמן"

    # Network / Connection error
    monkeypatch.setattr(telegram._OPENER, "open", lambda req, timeout=15: (_ for _ in ()).throw(ConnectionRefusedError("refused")))
    with pytest.raises(TelegramError) as exc_info:
        bot.call("sendMessage", {"chat_id": OWNER, "text": "test"})
    assert exc_info.value.code is None
    assert exc_info.value.description == "network error"
    assert mirror.reason_str(exc_info.value, lang="en") == "No internet connection"
    assert mirror.reason_str(exc_info.value, lang="he") == "אין חיבור לאינטרנט"


# --- 3. Late delivery receipt -------------------------------------------------

def test_drop_acks_records_delivered_ids(tmp_path, bot):
    """drop_acks deletes existing receipts and persists delivered message IDs."""
    c = cfg()
    # Pre-populate delivery-acks.json with receipt 101 for message 42
    delivery._update_acks(lambda acks: acks.__setitem__("42", 101))
    assert delivery._acks().get("42") == 101

    delivery.drop_acks(bot, c, [42])

    # Receipt deleted via deleteMessage
    deleted = [p for m, p in bot.calls if m == "deleteMessage"]
    assert len(deleted) == 1
    assert deleted[0]["message_id"] == 101
    assert delivery._acks().get("42") is None
    # ID is recorded in delivery-delivered-ids.json
    assert delivery.is_delivered(42) is True
    assert delivery.is_delivered("42") is True


def test_send_ack_cleans_up_late_receipt_at_once(tmp_path, bot):
    """When receipt arrives after the message was already delivered, clean it up at once."""
    c = cfg()
    # Message 77 was already delivered
    delivery.record_delivered([77])
    assert delivery.is_delivered(77) is True

    # Pre-delivered message: send_ack should not even call sendMessage
    delivery.send_ack(bot, c, thread=10, mid=77, hold=True)
    send_calls = [p for m, p in bot.calls if m == "sendMessage"]
    assert len(send_calls) == 0


def test_send_ack_cleans_up_receipt_delivered_in_flight(tmp_path, monkeypatch):
    """If delivery happens while sendMessage was in flight, clean up the receipt immediately."""
    fake_bot = FakeBot()
    c = cfg()
    mid = 88

    # When sendMessage is called for ack, simulate delivery happening concurrently
    orig_call = fake_bot.call

    def concurrent_delivery_call(method, params=None, timeout=15):
        res = orig_call(method, params, timeout)
        if method == "sendMessage":
            # Simulate delivery happening while sendMessage was in flight
            delivery.record_delivered([mid])
        return res

    fake_bot.call = concurrent_delivery_call

    delivery.send_ack(fake_bot, c, thread=10, mid=mid, hold=True)

    # sendMessage was called
    send_calls = [p for m, p in fake_bot.calls if m == "sendMessage"]
    assert len(send_calls) == 1
    ack_id = send_calls[0]["message_id"]

    # Late delivery receipt was immediately cleaned up
    delete_calls = [p for m, p in fake_bot.calls if m == "deleteMessage"]
    assert len(delete_calls) == 1
    assert delete_calls[0]["message_id"] == ack_id
    assert delivery._acks().get(str(mid)) is None


def test_keyboard_quota_cell_without_quota_in_footer(monkeypatch):
    from ctb import mirror, usage
    monkeypatch.setattr(usage, "quota_line", lambda lang, **kw: "\n⏳ 5 שעות 30% · שבועית 41% · לפני 0 דק׳")
    rows = mirror.live_keyboard("📏 253 אלף", lang="he")["keyboard"]
    assert any("41%" in b["text"] for b in rows[0])


def test_keyboard_shows_the_weekly_number_only():
    from ctb import mirror
    rows = mirror.live_keyboard("\n⏳ 5 שעות 30% · שבועית 41% · לפני 0 דק׳", lang="he")["keyboard"]
    assert {"text": "📅 41%"} in rows[0]  # an old footer's 5-hour number is never shown
    rows = mirror.live_keyboard("\n⏳ 5h 30% · weekly 41% · 0 min ago", lang="en")["keyboard"]
    assert {"text": "📅 41%"} in rows[0]
