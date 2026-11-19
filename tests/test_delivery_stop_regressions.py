"""Regression coverage for durable Stop-and-send delivery receipts."""
import threading
import time

from conftest import FakeBot, GOOD_TOKEN
from ctb import config as C
from ctb import inbox as I

SID = "stop-regression-session"
OWNER = 77


def cfg():
    return C.Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER)


def queued(box, mid, hold=True):
    box.put(SID, {"id": mid, "chat": OWNER, "text": str(mid), "hold": hold, "source": "owner"})


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_stop_worker_rejects_cached_pane_and_uses_live_process_pane(tmp_path, monkeypatch):
    box = I.Inbox(tmp_path / "inbox")
    queued(box, 5)
    used = []
    monkeypatch.setattr(I, "pane_for_session", lambda session: "%1")
    monkeypatch.setattr(I, "pane_runs_session", lambda pane, session: pane == "%2")
    monkeypatch.setattr(I, "_pane_from_process", lambda session: "%2")
    monkeypatch.setattr(I, "stop_turn", lambda pane, session: used.append(pane) or ("failed", ""))
    monkeypatch.setattr(I, "_delivery_stop_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(I, "log_delivery_tap", lambda *args: None)

    I._delivery_stop_worker(cfg(), FakeBot(), {"message_id": 9, "_delivery_mid": "5"}, SID, box)

    assert used == ["%2"]
    assert box.peek(SID)[0]["hold"] is True


def test_failed_stop_keeps_hold_during_real_worker_and_serializes_second_tap(tmp_path, monkeypatch):
    box = I.Inbox(tmp_path / "inbox")
    queued(box, 5)
    queued(box, 6)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    monkeypatch.setattr(I, "stop_pane", lambda session: "%2")
    def slow_stop(pane, session):
        calls.append((pane, session))
        entered.set()
        assert release.wait(2)
        return "failed", ""
    monkeypatch.setattr(I, "stop_turn", slow_stop)
    monkeypatch.setattr(I, "_delivery_stop_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(I, "log_delivery_tap", lambda *args: None)
    bot = FakeBot()

    assert I.delivery_button({"id": "one", "data": "dl:stop:5"}, cfg(), bot,
                             {"message_id": 91}, SID, box) == "delivery"
    assert entered.wait(2)
    assert next(item for item in box.peek(SID) if item["id"] == 5)["hold"] is True
    assert I.delivery_button({"id": "two", "data": "dl:stop:6"}, cfg(), bot,
                             {"message_id": 92}, SID, box) == "delivery"
    time.sleep(0.05)
    assert len(calls) == 1
    release.set()
    assert wait_until(lambda: SID not in I._terminal_active)
    assert all(item["hold"] is True for item in box.peek(SID))


def test_successful_stop_hands_the_supplied_inbox_to_cleanup(tmp_path, monkeypatch):
    box = I.Inbox(tmp_path / "custom-inbox")
    queued(box, 5)
    seen = []
    monkeypatch.setattr(I, "stop_pane", lambda session: "%2")
    monkeypatch.setattr(I, "stop_turn", lambda pane, session: ("stopped", ""))
    monkeypatch.setattr(I, "forget_replay", lambda session: None)
    monkeypatch.setattr(I, "_interrupted_idle", lambda cfg, bot, session, inbox=None: seen.append(inbox))
    monkeypatch.setattr(I, "_delivery_stop_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(I, "log_delivery_tap", lambda *args: None)

    I._delivery_stop_worker(cfg(), FakeBot(), {"message_id": 9, "_delivery_mid": "5"}, SID, box)

    assert seen == [box]
    assert box.peek(SID)[0]["hold"] is False


def test_stop_tap_recognizes_message_owned_by_delivery_lease(tmp_path, monkeypatch):
    box = I.Inbox(tmp_path / "inbox")
    queued(box, 5)
    box.take(SID, lease={"nonce": "n", "transcript": "t", "offset": 0, "via": "test"})
    monkeypatch.setattr(I, "_terminal_background", lambda session, action: True)
    monkeypatch.setattr(I, "log_delivery_tap", lambda *args: None)
    bot = FakeBot()

    result = I.delivery_button({"id": "lease", "data": "dl:stop:5"}, cfg(), bot,
                               {"message_id": 91}, SID, box)

    assert result == "delivery"
    assert bot.methods("answerCallbackQuery")[-1]["text"] != "Already delivered"
