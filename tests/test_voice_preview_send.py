"""The transcript preview of a voice note ("🎤 זה מה ששמעתי:" with Send / Edit / Cancel).

Send delivers exactly once or says plainly why it did not; the preview appears whenever transcript approval is on
(also for a recording sent to the dispatcher from General), unless the driving window is open; the driving window
closes by itself. Fakes only: no Telegram, tmux or real config."""
import json
import os
from pathlib import Path

import pytest

from conftest import GOOD_TOKEN, FakeBot
from ctb import dispatcher, i18n, panel_settings, voice
from ctb import inbox as I
from ctb.config import Config, state_dir
from ctb.telegram import TelegramError
from ctb.topics import TopicMap

SID = "11111111-2222-3333-4444-555555555555"
DEAD_PID = 999_999  # above every pid macOS and Linux hand out by default: never a running process
OWNER = 7
GROUP = -1001234567890


@pytest.fixture(autouse=True)
def no_nudge(monkeypatch):
    monkeypatch.setattr(I, "_reopen_or_nudge", lambda inbox, session: None)


def cfg(**kw):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", **kw)


class Media:
    def content_parts(self, m):
        return "🎤 (הודעה קולית, תומללה) fix the bug", ""

    def content_of(self, m):
        return "🎤 (הודעה קולית, תומללה) fix the bug" if m.get("voice") else m.get("text", "")


def rig(bot):
    topics = TopicMap(state_dir() / "topics.json", bot, OWNER)
    thread = topics.ensure(SID, "Title")
    return topics, I.Inbox(state_dir() / "inbox"), thread


def recording(thread, mid=8, chat=None):
    return {"message": {"message_id": mid, "date": 2_000_000_000, "voice": {"file_id": "f"},
                        **({"message_thread_id": thread} if thread else {}),
                        "chat": chat or {"id": OWNER, "type": "private"}, "from": {"id": OWNER}}}


def tap(bot, c, topics, box, data, thread, chat=None, mid=60):
    preview = {"chat": chat or {"id": OWNER, "type": "private"}, "message_id": mid,
               "text": "🎤 זה מה ששמעתי:\nfix the bug", **({"message_thread_id": thread} if thread else {})}
    return I.handle_button({"id": "q", "from": {"id": OWNER}, "message": preview, "data": data}, c, bot, topics, box)


def toasts(bot):
    return [p["text"] for p in bot.methods("answerCallbackQuery")]


# --- Send: exactly once, or a clear reason ----------------------------------------------------------------------

def test_send_delivers_once_and_a_second_tap_says_it_was_already_sent(bot):
    topics, box, thread = rig(bot)
    c = cfg()
    assert I.handle(recording(thread), c, bot, topics, box, Media(), 0) == "voice"
    assert tap(bot, c, topics, box, "vd:send:8", thread) == "voice"
    assert tap(bot, c, topics, box, "vd:send:8", thread) == "ignored"
    [item] = box.take(SID)
    assert item["id"] == 8 and item.get("voice") and not item.get("typed")
    assert toasts(bot) == [i18n.t("voice_note_sent", lang="he"), i18n.t("voice_note_already_sent", lang="he")]
    assert bot.methods("editMessageText")[0]["text"].startswith(i18n.t("voice_sent_body", lang="he", body="")[:4])


def test_the_toast_answers_only_after_the_message_is_queued(bot):
    """The toast said "sent" before anything was queued; a delivery that then failed still said "sent"."""
    topics, box, thread = rig(bot)
    c = cfg()
    I.handle(recording(thread), c, bot, topics, box, Media(), 0)
    order = []
    real_call = bot.call

    def call(method, params=None, timeout=15):
        if method == "answerCallbackQuery":
            order.append(("toast", bool(box.pending(SID))))
        return real_call(method, params, timeout)
    bot.call = call
    tap(bot, c, topics, box, "vd:send:8", thread)
    assert order == [("toast", True)]


def test_a_send_that_queues_nothing_keeps_the_draft_and_says_tap_again(bot, monkeypatch):
    topics, box, thread = rig(bot)
    c = cfg()
    I.handle(recording(thread), c, bot, topics, box, Media(), 0)
    real_put = box.put
    monkeypatch.setattr(box, "put", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert tap(bot, c, topics, box, "vd:send:8", thread) == "voice-failed"
    assert toasts(bot)[-1] == i18n.t("voice_note_send_failed", lang="he")
    assert "8" in voice.load() and not box.pending(SID)
    assert not bot.methods("editMessageText")                    # the buttons stay for the next tap
    monkeypatch.setattr(box, "put", real_put)
    assert tap(bot, c, topics, box, "vd:send:8", thread) == "voice"
    assert [i["id"] for i in box.take(SID)] == [8] and voice.load() == {}


def test_a_failing_receipt_after_queueing_never_delivers_twice(bot, monkeypatch):
    """The message is queued, then a receipt fails: the draft must not come back, or a second tap sends it again."""
    topics, box, thread = rig(bot)
    c = cfg()
    I.handle(recording(thread), c, bot, topics, box, Media(), 0)
    monkeypatch.setattr(I, "main_working", lambda session: (_ for _ in ()).throw(RuntimeError("boom")))
    assert tap(bot, c, topics, box, "vd:send:8", thread) == "voice"
    assert tap(bot, c, topics, box, "vd:send:8", thread) == "ignored"
    assert [i["id"] for i in box.take(SID)] == [8]


def test_the_preview_shows_it_was_sent_even_through_a_flood_limit(bot):
    """The edit to "✅ נשלח" under a 429 was swallowed, so the preview looked untouched and Send seemed dead."""
    topics, box, thread = rig(bot)
    c = cfg()
    I.handle(recording(thread), c, bot, topics, box, Media(), 0)
    bot.fail["editMessageText"] = TelegramError("Too Many Requests", 429, 12)
    tap(bot, c, topics, box, "vd:send:8", thread)
    edits = bot.methods("editMessageText")
    assert len(edits) == 2 and edits[-1]["text"].startswith("‏✅")    # tried again, off the poll loop


def test_a_preview_that_can_never_be_edited_loses_its_buttons(bot):
    class NoEdits(FakeBot):
        def call(self, method, params=None, timeout=15):
            if method == "editMessageText":
                self.calls.append((method, params or {}))
                raise TelegramError("network error")
            return super().call(method, params, timeout)
    bot = NoEdits()
    topics, box, thread = rig(bot)
    c = cfg()
    I.handle(recording(thread), c, bot, topics, box, Media(), 0)
    tap(bot, c, topics, box, "vd:send:8", thread)
    assert [i["id"] for i in box.take(SID)] == [8]
    assert bot.methods("editMessageReplyMarkup")[-1]["reply_markup"] == {"inline_keyboard": []}


def test_cancel_then_send_says_it_was_cancelled(bot):
    topics, box, thread = rig(bot)
    c = cfg()
    I.handle(recording(thread), c, bot, topics, box, Media(), 0)
    tap(bot, c, topics, box, "vd:cancel:8", thread)
    assert tap(bot, c, topics, box, "vd:send:8", thread) == "ignored"
    assert toasts(bot)[-1] == i18n.t("voice_note_already_cancelled", lang="he") and not box.pending(SID)


def test_a_task_that_fails_to_open_is_never_opened_twice(bot, monkeypatch):
    topics, box, _thread = rig(bot)
    c = cfg()
    calls = []
    monkeypatch.setattr(I, "project_picker", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(RuntimeError("x")))
    assert I.handle(recording(555), c, bot, topics, box, Media(), 0) == "voice"   # a topic with no session
    assert tap(bot, c, topics, box, "vd:send:8", 555) == "voice-failed"
    assert tap(bot, c, topics, box, "vd:send:8", 555) == "ignored"
    assert calls == [1] and toasts(bot)[0] == i18n.t("voice_note_start_failed", lang="he")
    assert "fix the bug" in bot.methods("editMessageText")[-1]["text"]           # the words stay on screen


# --- the preview appears whenever approval is on -----------------------------------------------------------------

def test_a_recording_sent_to_the_dispatcher_from_general_gets_its_preview(bot):
    """General with the offer off routes to the dispatcher, and that path skipped the preview without a word."""
    c = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP, ui_language="he", target_language="he")
    topics, box = TopicMap(state_dir() / "topics.json", bot, GROUP), I.Inbox(state_dir() / "inbox")
    I.set_setting(I.GENERAL_OFFER_OFF, True)
    dispatcher.set_dispatcher("disp")
    group = {"id": GROUP, "type": "supergroup"}
    assert I.handle(recording(None, mid=10, chat=group), c, bot, topics, box, Media(), 0) == "voice"
    assert not box.pending("disp")
    assert "vd:send:10" in json.dumps(bot.methods("sendMessage")[-1]["reply_markup"])
    assert tap(bot, c, topics, box, "vd:send:10", None, chat=group) == "voice"
    [item] = box.take("disp")
    assert item["text"].startswith("NEW TASK") and "fix the bug" in item["text"]
    assert item.get("voice") is True and not item.get("typed")


def test_a_preview_left_unshown_by_a_restart_is_delivered_at_the_next_start(bot):
    topics, box, thread = rig(bot)
    voice.add(str(9), {"session": SID, "text": "🎤 (הודעה קולית, תומללה) fix it", "thread": thread, "chat": None,
                       "date": 2_000_000_000, "message": 9, "preview": "pending", "pid": DEAD_PID})
    voice.add(str(11), {"session": SID, "text": "x", "thread": thread, "chat": None, "message": 11,
                        "preview": "pending", "pid": os.getpid()})       # this process is still retrying it
    I.voice_preview_sweep(cfg(), bot, box)
    assert [i["id"] for i in box.take(SID)] == [9] and list(voice.load()) == ["11"]
    assert voice.outcome("9") == "direct"


# --- driving -------------------------------------------------------------------------------------------------------

def test_the_driving_window_sends_directly_with_a_note_and_closes_by_itself(bot):
    topics, box, thread = rig(bot)
    c = cfg()
    voice.set_driving(True, now=1000.0)
    assert voice.driving_on(now=1000.0 + voice.DRIVING_SECONDS - 1)
    assert not voice.driving_on(now=1000.0 + voice.DRIVING_SECONDS + 1)
    voice.set_driving(True)
    assert I.handle(recording(thread), c, bot, topics, box, Media(), 0) == "queued"
    assert voice.load() == {} and [i["id"] for i in box.take(SID)] == [8]
    note = bot.methods("sendMessage")[-1]
    assert "נהיגה" in note["text"] and "fix the bug" in note["text"] and "reply_markup" not in note
    voice.set_driving(False)
    assert I.handle(recording(thread, 9), c, bot, topics, box, Media(), 0) == "voice"


def test_a_far_future_driving_window_counts_as_closed():
    (state_dir()).mkdir(parents=True, exist_ok=True)
    (state_dir() / "voice-driving.json").write_text(json.dumps({"until": 10_000_000_000}))
    assert not voice.driving_on(now=1000.0)


def test_one_tap_driving_button_beside_voice_confirmation(bot):
    topics, box, _thread = rig(bot)
    rows = I.global_markup(lang="he")["inline_keyboard"]
    [row] = [r for r in rows if any(b["callback_data"] == "more:voiceconfirm" for b in r)]
    assert [b["callback_data"] for b in row] == ["more:voiceconfirm", "more:driving"]
    assert row[1]["text"] == i18n.t("global_btn_driving", lang="he", state=i18n.t("state_off", lang="he"))
    q = {"id": "q", "from": {"id": OWNER}, "data": "more:driving",
         "message": {"message_id": 5, "chat": {"id": OWNER, "type": "private"}}}
    assert I.handle_button(q, cfg(), bot, topics, box) == "settings"
    assert voice.driving_on() and "🚗" in toasts(bot)[-1]
    labels = [b["text"] for r in bot.methods("editMessageText")[-1]["reply_markup"]["inline_keyboard"] for b in r]
    assert any(x.startswith("🚗 נהיגה: עד ") for x in labels)
    I.handle_button(q, cfg(), bot, topics, box)
    assert not voice.driving_on() and toasts(bot)[-1] == i18n.t("driving_toast_off", lang="he")


def test_cockpit_settings_carry_both_driving_controls():
    assert panel_settings.read()["voice_driving"] is False
    assert panel_settings.read()["voice_confirm_driving"] == "preview"
    assert panel_settings.allowed("voice_confirm_driving", "send") and not panel_settings.allowed("voice_confirm_driving", "x")
    panel_settings.write("voice_driving", True)
    panel_settings.write("voice_confirm_driving", "send")
    assert voice.driving_on() and voice.car_choice() == "send"
    I.set_car_mode(True)
    voice.set_driving(False)
    assert I._preview_skip() == "car"


# --- a failed hand-over stays recoverable -------------------------------------------------------------------------

def unshown_draft(thread, mid=9, **extra):
    voice.add(str(mid), {"session": SID, "text": "🎤 (הודעה קולית, תומללה) fix it", "thread": thread, "chat": None,
                         "date": 2_000_000_000, "message": mid, "preview": "pending", "pid": DEAD_PID, **extra})


def test_a_failed_hand_over_is_tried_again_and_delivered_once(bot, monkeypatch):
    """A fallback that failed used to be parked as "failed", which the restart sweep never picked up again."""
    topics, box, thread = rig(bot)
    unshown_draft(thread)
    real = I._queue_owner
    fails = [OSError("disk full"), OSError("disk full")]

    def flaky(*a, **k):
        if fails:
            raise fails.pop()
        return real(*a, **k)
    monkeypatch.setattr(I, "_queue_owner", flaky)
    I.voice_preview_sweep(cfg(), bot, box)
    assert [i["id"] for i in box.take(SID)] == [9] and voice.load() == {} and voice.outcome("9") == "direct"


def test_a_failed_hand_over_left_by_a_dead_process_is_picked_up_by_the_sweep(bot):
    topics, box, thread = rig(bot)
    unshown_draft(thread, attempts=1)
    I.voice_preview_sweep(cfg(), bot, box)
    assert [i["id"] for i in box.take(SID)] == [9] and voice.load() == {}


def test_a_recording_that_can_never_be_handed_over_is_dropped_with_its_words_shown(bot, monkeypatch):
    topics, box, thread = rig(bot)
    unshown_draft(thread)
    tries = []
    monkeypatch.setattr(I, "_queue_owner", lambda *a, **k: tries.append(1) or (_ for _ in ()).throw(OSError("x")))
    I.voice_preview_sweep(cfg(), bot, box)
    assert len(tries) == I.UNSHOWN_ATTEMPTS and voice.load() == {} and voice.outcome("9") == "dropped"
    note = bot.methods("sendMessage")[-1]
    assert "fix it" in note["text"] and "ההקלטה לא הגיעה לסוכן" in note["text"]
    assert note["message_thread_id"] == thread and note["reply_parameters"]["message_id"] == 9


def test_a_dropped_recording_stays_until_the_owner_was_told(bot, monkeypatch):
    """The draft was deleted before the notice: a notice that failed too lost the words for good."""
    topics, box, thread = rig(bot)
    unshown_draft(thread)
    monkeypatch.setattr(I, "_queue_owner", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    real_call = bot.call
    telegram_down = [True]

    def call(method, params=None, timeout=15):
        if method == "sendMessage" and telegram_down[0]:
            raise TelegramError("network error")
        return real_call(method, params, timeout)
    bot.call = call
    I.voice_preview_sweep(cfg(), bot, box, passes=1)
    draft = voice.load()["9"]
    assert draft["preview"] == "pending" and draft["attempts"] == I.UNSHOWN_ATTEMPTS and "claim" not in draft
    assert voice.outcome("9") == ""
    telegram_down[0] = False                                               # the next sweep (or a restart)
    I.voice_preview_sweep(cfg(), bot, box, passes=1)
    assert voice.load() == {} and voice.outcome("9") == "dropped"
    assert "fix it" in bot.methods("sendMessage")[-1]["text"]


# --- one claim across processes -------------------------------------------------------------------------------------

def hold_as(pid):
    """Another running claimer process, simulated: its marker lock taken through a file of our own."""
    import fcntl
    path = state_dir() / "voice-claimers" / f"{pid}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_a_live_claim_is_never_taken_over_however_long_it_runs():
    """A time lease let a second process take over a slow delivery whose owner was still alive: sent twice."""
    voice.add("5", {"text": "x"})
    state, draft = voice.claim("5")
    assert state == "ok" and draft["claim"]["pid"] == os.getpid()
    assert voice.claim("5") == ("busy", None)
    voice.add("7", {"text": "z", "claim": {"pid": 424242, "at": 0, "token": "other"}})
    fd = hold_as(424242)                                                   # another listener, still delivering
    assert voice.claim("7", now=1e12) == ("busy", None)                   # hours later: still its claim
    os.close(fd)                                                           # that listener ended
    assert voice.claim("7")[0] == "ok"


def test_an_abandoned_or_reused_pid_claim_is_taken_over():
    voice.add("6", {"text": "y", "claim": {"pid": DEAD_PID, "at": 1e12, "token": "old"}})
    state, taken = voice.claim("6")
    assert state == "ok" and taken["claim"]["token"] != "old"              # its process is gone
    assert not voice.finish("6", "old") and voice.finish("6", taken["claim"]["token"])
    assert voice.claim("6") == ("gone", None)
    # the pid now belongs to a running process that never claimed anything (pid reuse): free to take over
    voice.add("8", {"text": "w", "claim": {"pid": os.getppid(), "at": 1e12, "token": "reused"}})
    assert voice.claim("8")[0] == "ok"


def test_a_tap_while_another_process_delivers_says_so_and_sends_nothing(bot):
    topics, box, thread = rig(bot)
    c = cfg()
    I.handle(recording(thread), c, bot, topics, box, Media(), 0)
    drafts = json.loads((state_dir() / "voice-drafts.json").read_text())
    drafts["8"]["claim"] = {"pid": 424243, "at": 0, "token": "other"}
    (state_dir() / "voice-drafts.json").write_text(json.dumps(drafts))
    fd = hold_as(424243)
    try:
        assert tap(bot, c, topics, box, "vd:send:8", thread) == "ignored"
        assert toasts(bot)[-1] == i18n.t("voice_note_sending", lang="he") and not box.pending(SID)
    finally:
        os.close(fd)


def test_threads_with_only_the_file_lock_never_claim_one_draft_twice(monkeypatch):
    """The thread lock taken away: the fcntl lock alone keeps the claim exclusive (one open file each)."""
    import threading
    for k in range(30):
        voice.add(str(k), {"text": "x"})
    monkeypatch.setattr(voice, "_lock", __import__("contextlib").nullcontext())
    won, go = [], threading.Event()

    def worker():
        go.wait()
        for k in range(30):
            state, draft = voice.claim(str(k))
            if state == "ok":
                won.append(k)
                voice.finish(str(k), draft["claim"]["token"])
    threads = [threading.Thread(target=worker) for _ in range(6)]
    for th in threads:
        th.start()
    go.set()
    for th in threads:
        th.join(10)
    assert sorted(won) == list(range(30))


def test_separate_processes_racing_on_one_drafts_file_deliver_each_draft_once(tmp_path):
    """Two listeners overlapping in a restart: real processes, the same state folder, one winner per draft."""
    import subprocess
    import sys
    keys = 40  # within voice.KEEP
    for k in range(keys):
        voice.add(str(k), {"text": "x", "preview": "pending"})
    go = tmp_path / "go"
    script = (
        "import json, os, sys, time\n"
        "from ctb import voice\n"
        "while not os.path.exists(sys.argv[1]): time.sleep(0.005)\n"
        "won = []\n"
        f"for k in range({keys}):\n"
        "    state, d = voice.claim(str(k))\n"
        "    if state == 'ok':\n"
        "        won.append(k); voice.finish(str(k), d['claim']['token'])\n"
        "print(json.dumps(won))\n")
    src = str(Path(I.__file__).resolve().parents[1])
    env = dict(os.environ, PYTHONPATH=src, CTB_STATE_DIR=str(state_dir()))
    procs = [subprocess.Popen([sys.executable, "-c", script, str(go)], env=env, cwd=str(tmp_path),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
    go.write_text("")
    wins = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err[-500:]
        wins += json.loads(out)
    assert sorted(wins) == list(range(keys)) and voice.load() == {}
