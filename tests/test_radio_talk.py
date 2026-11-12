"""Talking with a session through the radio, step 1: a voice note goes to the session as usual, and its reply is said on the radio."""
import json
import threading
import time

from conftest import GOOD_TOKEN
from ctb import framing, hooks as H, panel_settings, radio, radio_talk as T, radio_voice as V
from ctb.config import Config, set_setting
from ctb.inbox import default_inbox

OWNER = 1


def cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, radio={"project_names": {"orbit": "מסלול"}})


def voice_item(text="מה המצב עם המסך?", mid=5):
    return {"text": text, "id": mid, "chat": OWNER, "source": "owner", "at": int(time.time()) - 20, "voice": True}


def typed_item(text="מה המצב?", mid=6):
    return {"text": text, "id": mid, "chat": OWNER, "source": "owner", "at": int(time.time()) - 5, "typed": True}


def english(text):
    return {"The home screen is ready.": "מסך הבית מוכן."}.get(text, "תורגם: " + text)


REPLY = "I rebuilt the home screen and ran the tests.\n\nSummary: The home screen is ready."
SAID = "בניתי מחדש את מסך הבית והבדיקות עברו. נשאר רק לאשר את המיזוג."


def gemini(prompt):
    assert "REPLY:" in prompt and "Summary: The home screen is ready." in prompt
    return f"<<<{SAID}>>>"


def spooled():
    return sorted((radio.folder() / "replies").glob("*.json"))


def test_a_voice_notes_reply_is_spoken_with_the_sessions_name():
    box = default_inbox()
    box.put("sess-1", voice_item())
    taken = box.take("sess-1")
    assert taken and taken[0]["voice"] is True
    rec = T.on_reply(cfg(), "sess-1", "orbit: מסך הבית", REPLY, radio_on=lambda: True, translate=english, ask=gemini)
    assert rec["text"] == "מסלול: מסך הבית: " + SAID
    assert rec["voice"] is True and rec["sent_at"] <= rec["queued_at"] <= rec["taken_at"] <= rec["replied_at"]
    assert len(spooled()) == 1
    assert T.on_reply(cfg(), "sess-1", "orbit: מסך הבית", REPLY, radio_on=lambda: True, translate=english,
                      ask=gemini)["voice"] is False  # the next reply is said too, as an ordinary one


def test_every_reply_is_spoken_by_default_and_only_voice_answers_with_the_switch_off():
    box = default_inbox()
    assert T.every_reply_on() is True
    rec = T.on_reply(cfg(), "sess-2", "x", REPLY, radio_on=lambda: True, translate=english, ask=gemini)
    assert rec and rec["voice"] is False and rec["text"] == "הסשן: " + SAID  # no owner message needed
    T.set_every_reply(False)
    box.put("sess-2", typed_item())
    box.take("sess-2")
    assert T.on_reply(cfg(), "sess-2", "x", REPLY, radio_on=lambda: True, translate=english, ask=gemini) is None
    box.put("sess-2", voice_item())
    box.take("sess-2")
    assert T.on_reply(cfg(), "sess-2", "x", REPLY, radio_on=lambda: True, translate=english, ask=gemini)


def test_nothing_is_spoken_when_the_radio_is_off_or_the_reply_is_trivial():
    assert T.on_reply(cfg(), "sess-3", "x", REPLY, radio_on=lambda: False, translate=english, ask=gemini) is None
    for reply in ("OK.", "Still working on it, more soon.\n\nSummary: Still working."):
        assert T.on_reply(cfg(), "sess-3", "x", reply, radio_on=lambda: True, translate=english,
                          ask=lambda p: "<<<SKIP>>>") is None
    assert spooled() == []


def test_a_failed_or_english_answer_falls_back_to_the_summary_in_hebrew():
    line = T.brief_line(cfg(), "x", REPLY, {}, ask=lambda p: "", translate=english)
    assert line == "הסשן: מסך הבית מוכן."
    line = T.brief_line(cfg(), "x", REPLY, {}, ask=lambda p: "<<<All done, tests pass.>>>", translate=english)
    assert line == "הסשן: מסך הבית מוכן."


def test_replies_that_come_close_together_go_out_as_one_bulletin():
    a, b = {"text": "מסלול: המסך מוכן"}, {"text": "מזווה: התשלום עובד."}
    assert not T.ready([a], [100.0], 105.0)  # waits a little for others
    assert not T.ready([a, b], [100.0, 104.0], 110.0)
    assert T.ready([a, b], [100.0, 104.0], 112.5)  # quiet for 8 s
    assert T.ready([a, b], [100.0, 120.0], 125.5)  # never more than 25 s after the first
    assert T.ready([dict(a, voice=True)], [100.0], 100.5)  # a voice note's answer goes at once
    assert T.bulletin([a, b]) == "עדכונים מהצוות. מסלול: המסך מוכן. מזווה: התשלום עובד."
    assert T.bulletin([a]) == "מסלול: המסך מוכן."


def test_the_spoken_line_is_short_hebrew():
    long_reply = "First thing. Second thing. Third thing. Fourth thing. " * 10
    line = T.spoken_reply("pantry: תשלומים", long_reply, lambda t: "אחת. שתיים. שלוש. ארבע. חמש.", {})
    assert line == "pantry: תשלומים: אחת. שתיים. שלוש." or line == "הסשן: אחת. שתיים. שלוש."
    hebrew = T.spoken_reply("אורה: עיצוב", "סיימתי את העיצוב. נשאר רק אישור. " * 30, lambda t: t, {})
    assert hebrew.startswith("אורה: עיצוב: סיימתי את העיצוב") and len(hebrew) <= T.MAX_CHARS + 20


def test_an_owner_voice_note_is_framed_as_voice_never_as_approval():
    box = default_inbox()
    box.put("sess-4", voice_item("כן, תמזג"))
    text = H.delivery_text(box.take("sess-4"))
    assert framing.VOICE_MARK in text and framing.NEVER_APPROVAL in text


def test_the_station_says_an_answer_ahead_of_routine_updates_and_logs_the_round_trip(tmp_path, monkeypatch):
    st = radio.Station.__new__(radio.Station)
    st.cfg, st.s, st.dir = cfg(), {"lang": "he"}, radio.folder()
    st.mixer, st.usage, st.cache = radio.Mixer(bytes(4)), V.Usage(tmp_path / "u.json"), tmp_path / "c"
    st.status, st.snap, st.quit, st._switch_day = {}, None, threading.Event(), ""
    st.draw_lock, st.caption_lock, st.captions, st.tag, st.headline, st.talks = (
        threading.Lock(), threading.Lock(), {}, 0, ("", ""), {})
    monkeypatch.setattr(V, "speak", lambda *a, **k: (b"\x01\x00" * 4800, "vertex"))
    logged = []
    monkeypatch.setattr(radio, "log", logged.append)
    st.speak("עדכון שגרתי.")  # a routine update already waiting
    box = default_inbox()
    box.put("sess-5", voice_item())
    box.take("sess-5")
    T.on_reply(cfg(), "sess-5", "x", REPLY, radio_on=lambda: True, translate=english, ask=gemini)
    t = threading.Thread(target=st.talk_loop, daemon=True)
    t.start()
    deadline = time.time() + 5
    while len(st.mixer.clips) < 2 and time.time() < deadline:
        time.sleep(0.05)
    st.quit.set()
    first_tag = st.mixer.clips[0][1]
    assert first_tag in st.talks  # the answer goes before the routine update
    st.mixer.gain = 0.0
    st.mixer.next(10)  # the answer's intro starts on the air
    tag, _ = st.mixer.progress()
    with st.caption_lock:
        talk = st.talks.pop(tag)
    line = T.round_trip(talk[0], time.time())
    assert line.startswith("talk sess-5: ") and "from voice note to air" in line and "taken" in line


def test_the_setting_is_in_the_panels_bridge_settings_on_by_default():
    assert "radio_every_reply" in panel_settings.SETTINGS
    assert panel_settings.read()["radio_every_reply"] is True
    panel_settings.write("radio_every_reply", False)
    assert panel_settings.read()["radio_every_reply"] is False and T.every_reply_on() is False


def test_when_the_paid_minutes_run_out_the_free_voice_goes_on_and_says_so_once(tmp_path, monkeypatch):
    st = radio.Station.__new__(radio.Station)
    st.cfg, st.s, st.dir = cfg(), dict(radio.settings(cfg()), lang="he", say_voice="", tts_daily_minutes=1), radio.folder()
    st.mixer, st.usage, st.cache = radio.Mixer(bytes(4)), V.Usage(tmp_path / "u.json"), tmp_path / "c"
    st.status, st.snap, st._switch_day = {}, None, ""
    st.draw_lock, st.caption_lock, st.captions, st.tag, st.headline, st.talks = (
        threading.Lock(), threading.Lock(), {}, 0, ("", ""), {})
    said = []
    monkeypatch.setattr(V, "system_voice", lambda text, voice: said.append(text) or b"\x02\x00" * 100)
    monkeypatch.setattr(V, "vertex", lambda *a, **k: (b"", 0.0))
    st.usage.add(60.0)  # the whole minute is used
    st.speak("שורה ראשונה.")
    st.speak("שורה שנייה.")
    notices = [t for t in said if "נגמרו" in t]
    assert len(notices) == 1 and said[-1] == "שורה שנייה."  # announced once, then the free voice simply goes on
    assert radio.minutes(cfg()) == {"tts_minutes_today": 0.0, "tts_daily_minutes": 25}  # its own usage file


def test_news_never_repeat_a_finished_session_whose_reply_is_spoken():
    from ctb import radio_script as R
    before = R.snapshot({"sessions": [{"id": "s1", "name": "א", "state": "working"}]}, [], ([], [], 0), [], 1.0)
    last = R.remember(before, 1.0)
    after = R.snapshot({"sessions": [{"id": "s1", "name": "א", "state": "idle"}]}, [], ([], [], 0), [], 500.0)
    assert [k for k, _ in R.news(after, last)] == ["finished"]
    after["replies_spoken"] = True
    assert R.news(after, last) == []


def test_the_round_trip_line_names_every_leg():
    rec = {"session": "abcd1234", "voice": True, "sent_at": 100.0, "queued_at": 104.0, "taken_at": 106.0,
           "replied_at": 130.0, "line_secs": 1.2}
    assert T.round_trip(rec, 140.0) == ("talk abcd1234: 40s from voice note to air (queued 4s, taken 2s, reply 24s, "
                                        "line 1.2s, to air 10s)")
    assert json.dumps(rec)  # plain data in the spool


# --- review of #153 ---------------------------------------------------------------------------------

def test_a_frozen_mark_answers_its_own_turn_while_the_next_turns_mark_stays():
    box = default_inbox()
    T.set_every_reply(False)
    box.put("sess-9", voice_item())
    box.take("sess-9")
    frozen = T.take_mark("sess-9")  # what the Stop hook freezes for the turn that just ended
    box.put("sess-9", voice_item(mid=8))
    box.take("sess-9")  # the next turn's voice note, taken by the same Stop hook
    assert T.on_reply(cfg(), "sess-9", "x", REPLY, radio_on=lambda: True, translate=english, ask=gemini,
                      mark=frozen)["voice"] is True
    # the next turn's mark is still there for its own reply
    assert T.on_reply(cfg(), "sess-9", "x", REPLY, radio_on=lambda: True, translate=english, ask=gemini)["voice"] is True


def test_answers_queued_ahead_keep_their_own_order():
    m = radio.Mixer(bytes(4))
    m.add(b"r1", 1)
    m.add(b"a", 2, front=True)
    m.add(b"b", 3, front=True)
    assert [tag for _, tag in m.clips] == [2, 3, 1]
    m.gain = 0.0
    m.next()  # "a" goes on the air
    m.add(b"c", 4, front=True)
    assert [tag for _, tag in m.clips] == [3, 4, 1]


def test_the_stop_hook_freezes_the_ending_turns_mark_before_taking_the_next_turn(monkeypatch, tmp_path):
    box = default_inbox()
    box.put("sess-10", voice_item())
    box.take("sess-10")  # this turn answers a voice note
    box.put("sess-10", typed_item(mid=12))  # the next turn's typed message waits in the queue
    sent = []
    monkeypatch.setattr(H, "load_config", cfg)
    monkeypatch.setattr(H, "spawn", lambda args, text=None: sent.append((args, text)))
    path = tmp_path / "sess-10.jsonl"
    path.write_text("")
    H.hook_stop(json.dumps({"session_id": "sess-10", "transcript_path": str(path)}))
    worker = json.loads(next(t for a, t in sent if a[0] == "finish-stop"))
    assert worker["_radio_mark"].get("voice") is True  # the ending turn keeps its voice-note mark
    assert T.take_mark("sess-10").get("voice") is not True  # the next turn's mark is its own (typed)
