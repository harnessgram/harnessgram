"""Paid speech has one durable budget; the room is a sub-total, never extra minutes."""
import base64
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ctb import radio, radio_listeners as L, radio_room as RR, radio_voice as V
from ctb.config import Config
from test_radio_room import _station


def _free(monkeypatch):
    monkeypatch.setattr(V, "system_voice", lambda *a: b"free")
    monkeypatch.setattr(V, "NO_CEILING", set())


def _cloud_settings(**values):
    return radio.settings(Config(radio={"voice_route": "cloud", **values}))


def _speech(seconds):
    answer = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"inlineData": {
        "mimeType": "audio/L16;rate=24000", "data": base64.b64encode(b"\0\0" * 24000 * seconds).decode()}}]}}],
        "usageMetadata": {"candidatesTokenCount": seconds * V.AUDIO_TOKENS_PER_SECOND}}
    return io.BytesIO(json.dumps(answer).encode())


def _news_usage(tmp_path):
    return V.NewsUsage(V.Usage(tmp_path / "tts-usage.json"), V.Usage(tmp_path / "news-tts-usage.json"))


def test_news_paid_piece_uses_one_capped_reservation_and_settles_both_ledgers(tmp_path, monkeypatch):
    usage = _news_usage(tmp_path)
    ceilings = []

    def vertex(*args, max_seconds, **kwargs):
        ceilings.append(max_seconds)
        return b"\0\0" * V.RATE * 2, 2

    monkeypatch.setattr(V, "vertex", vertex)
    settings = dict(radio.settings(Config()), tts_daily_minutes=10, tts_room_daily_minutes=1)
    why = []
    assert V._paid_piece("שלום", Config(), settings, tmp_path / "cache", usage, None, why)
    assert len(ceilings) == 1 and type(ceilings[0]) is float and ceilings[0] > 2
    assert usage.shared.today() == pytest.approx(2)
    assert usage.news.today() == pytest.approx(2)
    assert not usage.shared._ledger()["pending"] and not usage.news._ledger()["pending"]


def test_news_paid_piece_with_zero_news_cap_never_reserves_shared_budget(tmp_path, monkeypatch):
    usage = _news_usage(tmp_path)
    monkeypatch.setattr(V, "vertex", lambda *a, **k: pytest.fail("paid request with the news voice off"))
    settings = dict(radio.settings(Config()), tts_daily_minutes=10, tts_room_daily_minutes=0)
    assert V._paid_piece("שלום", Config(), settings, tmp_path / "cache", usage, None, []) == b""
    assert usage.shared.today() == 0 and usage.news.today() == 0


def test_news_reservation_failure_releases_news_and_leaves_shared_clean(tmp_path, monkeypatch):
    usage = _news_usage(tmp_path)
    monkeypatch.setattr(usage.shared, "reserve", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    settings = dict(radio.settings(Config()), tts_daily_minutes=10, tts_room_daily_minutes=1)
    assert V._paid_piece("שלום", Config(), settings, tmp_path / "cache", usage, None, []) == b""
    assert usage.shared.today() == 0 and usage.news.today() == 0
    assert not usage.shared._ledger()["pending"] and not usage.news._ledger()["pending"]


def _refuse_ceiling(calls, seconds=1):
    """A model that rejects maxOutputTokens with HTTP 400 and otherwise speaks `seconds` per request."""
    def urlopen(req, timeout=0):
        generation = json.loads(req.data)["generationConfig"]
        calls.append(generation.get("maxOutputTokens"))
        if "maxOutputTokens" in generation:
            raise V.urllib.error.HTTPError(req.full_url, 400, "invalid", {}, io.BytesIO(b"{}"))
        return _speech(seconds)
    return urlopen


def test_model_refusing_the_ceiling_still_gets_paid_speech_within_budget(monkeypatch):
    _free(monkeypatch)
    calls = []
    monkeypatch.setattr(V, "access_token", lambda *a: "token")
    monkeypatch.setattr(V, "to_pcm", lambda raw, args: raw * 2)
    monkeypatch.setattr(V.urllib.request, "urlopen", _refuse_ceiling(calls))
    st = _station(Config(gemini_vertex_project="my-gcp-project",
                         radio={"voice_route": "cloud", "tts_daily_minutes": 0}))
    L.player_fetch(st.dir)
    for text in ("מבזק ראשון.", "מבזק שני."):
        assert st.speak(text)
        assert st.status["route"] == "vertex"
    assert calls[0] > 0 and calls[1:] == [None, None]  # refused once, then remembered: no ceiling in later requests
    assert 0 < st.usage.today() <= 60 * 60


def test_long_text_is_split_into_short_requests_and_stays_in_budget(tmp_path, monkeypatch):
    _free(monkeypatch)
    monkeypatch.setattr(V, "access_token", lambda *a: "token")
    monkeypatch.setattr(V, "to_pcm", lambda raw, args: raw * 2)
    sent = []

    def urlopen(req, timeout=0):
        body = json.loads(req.data)
        sent.append(body["contents"][0]["parts"][0]["text"])
        assert "maxOutputTokens" not in body["generationConfig"]
        return _speech(max(1, round(V.estimate_seconds(sent[-1]) * 0.8)))

    V.NO_CEILING.add("gemini-2.5-flash-tts")
    monkeypatch.setattr(V.urllib.request, "urlopen", urlopen)
    text = " ".join(f"זהו משפט מספר {n} של עדכון הצוות הארוך מאוד." for n in range(40))
    s = dict(_cloud_settings(), tts_daily_minutes=10)
    usage = V.Usage(tmp_path / "usage.json")
    pcm, route = V.speak(text, Config(gemini_vertex_project="p"), s, tmp_path / "cache", usage)
    assert route == "vertex" and pcm
    assert len(sent) > 1
    assert all(V.estimate_seconds(t) <= V.MAX_REQUEST_SECONDS for t in sent)
    assert " ".join(sent) == V.clean_for_voice(text)  # nothing lost or repeated across the pieces
    assert 0 < usage.today() <= 10 * 60 and not json.loads(usage.path.read_text())["pending"]
    # the whole text is cached now: asking again sends nothing
    n = len(sent)
    assert V.speak(text, Config(gemini_vertex_project="p"), s, tmp_path / "cache", usage)[1] == "cache"
    assert len(sent) == n


def test_text_that_does_not_fit_the_remaining_budget_uses_the_free_voice_without_a_request(tmp_path, monkeypatch):
    _free(monkeypatch)
    monkeypatch.setattr(V, "access_token", lambda *a: "token")
    monkeypatch.setattr(V.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("paid request that cannot fit"))
    V.NO_CEILING.add("gemini-2.5-flash-tts")
    text = " ".join(f"משפט מספר {n} בעדכון." for n in range(40))  # far more than 30 s of speech
    s = dict(_cloud_settings(), tts_daily_minutes=1)
    usage = V.Usage(tmp_path / "usage.json")
    usage.add(30)  # 30 s left
    assert V.speak(text, Config(gemini_vertex_project="p"), s, tmp_path / "cache", usage) == (b"free", "say")
    assert usage.today() == 30 and V.last_cap_out and "left today" in V.last_reason


def test_split_requests_never_exceed_the_budget_when_the_provider_overruns(tmp_path, monkeypatch):
    _free(monkeypatch)
    monkeypatch.setattr(V, "access_token", lambda *a: "token")
    monkeypatch.setattr(V, "to_pcm", lambda raw, args: raw * 2)
    V.NO_CEILING.add("gemini-2.5-flash-tts")
    sent = []

    def urlopen(req, timeout=0):
        sent.append(1)
        return _speech(300)  # ignores the text length entirely

    monkeypatch.setattr(V.urllib.request, "urlopen", urlopen)
    text = " ".join(f"זהו משפט מספר {n} של העדכון." for n in range(60))
    s = dict(_cloud_settings(), tts_daily_minutes=10)
    usage = V.Usage(tmp_path / "usage.json")
    assert V.speak(text, Config(gemini_vertex_project="p"), s, tmp_path / "cache", usage) == (b"free", "say")
    assert len(sent) == 1  # the first overrun blocks the day: no further pieces are sent
    assert json.loads(usage.path.read_text())["blocked"] is True
    assert not list((tmp_path / "cache").glob("*.pcm"))


def test_split_text_keeps_every_word_and_the_per_request_limit():
    assert V.split_text("שלום.") == ["שלום."]
    long_sentence = " ".join(["מילה"] * 400)  # one sentence, no end mark
    pieces = V.split_text(long_sentence)
    assert len(pieces) > 1 and " ".join(pieces) == long_sentence
    assert all(V.estimate_seconds(p) <= V.MAX_REQUEST_SECONDS for p in pieces)
    assert all(V.estimate_seconds(p) <= V.MAX_REQUEST_SECONDS for p in V.split_text("א" * 2000))


@pytest.mark.parametrize("overrun", ["tokens", "audio"])
def test_overlong_provider_response_is_billed_but_never_aired_or_cached(tmp_path, monkeypatch, overrun):
    _free(monkeypatch)
    monkeypatch.setattr(V, "access_token", lambda *a: "token")
    monkeypatch.setattr(V, "to_pcm", lambda raw, args: raw * 2)
    calls = []

    def respond(req, timeout=0):
        ceiling = json.loads(req.data)["generationConfig"]["maxOutputTokens"]
        calls.append(ceiling)
        # Either the billed tokens or the actual PCM can reveal a provider ignoring the ceiling.
        seconds = 20 if overrun == "audio" else 1
        answer = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"inlineData": {
            "mimeType": "audio/L16;rate=24000", "data": base64.b64encode(b"\0\0" * 24000 * seconds).decode()
        }}]}}], "usageMetadata": {"candidatesTokenCount": (20 if overrun == "tokens" else 1) * 25}}
        return io.BytesIO(json.dumps(answer).encode())

    monkeypatch.setattr(V.urllib.request, "urlopen", respond)
    s = dict(_cloud_settings(), tts_daily_minutes=0.1)  # only 6 s remain
    usage, cache = V.Usage(tmp_path / "usage.json"), tmp_path / "cache"
    assert V.speak("שלום", Config(gemini_vertex_project="p"), s, cache, usage) == (b"free", "say")
    assert usage.today() == 20  # full observed bill; hiding it would incorrectly renew the budget
    assert "exceeded" in V.last_reason
    assert not list(cache.glob("*.pcm"))
    s["tts_daily_minutes"] = 60  # more budget still must not trust a model that ignored its ceiling
    assert V.speak("עוד מבזק", Config(), s, cache, usage) == (b"free", "say")
    assert len(calls) == 1 and calls[0] <= 6 * 25


@pytest.mark.parametrize("room", [False, True])
@pytest.mark.parametrize("billed", [2.0, None, 20.0])
def test_request_crossing_midnight_settles_its_original_day(tmp_path, monkeypatch, room, billed):
    _free(monkeypatch)
    before = time.mktime((2026, 11, 12, 23, 59, 59, 0, 0, -1))
    after = before + 2
    clock = [before]
    localtime = time.localtime
    monkeypatch.setattr(V.time, "time", lambda: clock[0])
    monkeypatch.setattr(V.time, "localtime", lambda at=None: localtime(clock[0] if at is None else at))
    shared = V.paid_usage(tmp_path)
    usage = shared.for_room() if room else shared
    reserved = []

    def vertex(*a, max_seconds=0, **kw):
        reserved.append(max_seconds)
        clock[0] = after
        # A different request has already consumed today's budget before yesterday's answer arrives.
        shared.add(10)
        return (b"audio" if billed is not None else b""), billed

    monkeypatch.setattr(V, "vertex", vertex)
    s = _cloud_settings(tts_daily_minutes=1)
    s["tts_room_daily_minutes"] = 0.5
    _, route = V.speak("שלום", Config(), s, tmp_path / "cache", usage)
    assert route == ("vertex" if billed == 2 else "say")
    assert shared._ledger()["days"][V.Usage._day(before)] == (reserved[0] if billed is None else billed)
    assert shared.today(now=after) == 10  # neither yesterday's refund nor overrun alters today's total
    if room:
        assert usage._ledger()["room_days"][V.Usage._day(before)] == (reserved[0] if billed is None else billed)
        assert usage.today(now=after) == 0


def test_legacy_room_usage_is_imported_once_and_survives_restart(tmp_path):
    day = V.Usage._day(None)
    path = tmp_path / "tts-usage.json"
    legacy = tmp_path / "room" / "tts-usage.json"
    legacy.parent.mkdir()
    path.write_text(json.dumps({"days": {day: 50}}))
    legacy.write_text(json.dumps({"date": day, "seconds": 9}))
    shared = V.paid_usage(tmp_path)
    assert shared.today() == 59 and shared.for_room().today() == 9
    assert shared.reserve(2, 1) is None  # status reads and refused calls must include legacy usage too
    reservation = shared.for_room().reserve(2, 2, room_cap_minutes=1)
    assert reservation is not None
    restarted = V.paid_usage(tmp_path)
    assert restarted.today() == 59 + reservation[0]
    assert restarted.for_room().today() == 9 + reservation[0]
    restarted.for_room().settle(reservation, 3)
    assert V.paid_usage(tmp_path).today() == 62
    assert V.paid_usage(tmp_path).for_room().today() == 12
    assert json.loads(legacy.read_text()) == {"date": day, "seconds": 9}  # legacy file never updated again


@pytest.mark.parametrize("shared_cap,room_cap", [(1, 0.5), (0.5, 1)])
def test_parallel_radio_and_room_requests_reserve_both_caps_atomically(tmp_path, shared_cap, room_cap):
    barrier = threading.Barrier(16)

    def reserve(i):
        # Separate objects (as in different workers) share only the persisted ledger and its file lock.
        shared = V.paid_usage(tmp_path)
        usage = shared.for_room() if i % 2 else shared
        barrier.wait(timeout=5)
        result = usage.reserve(5, shared_cap, room_cap_minutes=room_cap)
        return i % 2, result

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(reserve, range(16)))
    room_reserved = sum(r[0] for room, r in results if room and r)
    all_reserved = sum(r[0] for _, r in results if r)
    restarted = V.paid_usage(tmp_path)
    assert sum(r is not None for _, r in results) == 1
    assert 0 < all_reserved <= shared_cap * 60
    assert room_reserved <= room_cap * 60
    assert restarted.today() == all_reserved
    assert restarted.for_room().today() == room_reserved
    assert restarted.reserve(5, shared_cap) is None  # in-flight work stays reserved after restart
    for room, result in results:
        if result:
            (restarted.for_room() if room else restarted).settle(result, 4)
    assert V.paid_usage(tmp_path).today() == 4 * sum(r is not None for _, r in results)
    assert V.paid_usage(tmp_path).for_room().today() == 4 * sum(bool(room and r) for room, r in results)


@pytest.mark.parametrize("exhaust", ["shared", "room"])
def test_station_checks_total_budget_and_room_subcap(monkeypatch, exhaust):
    _free(monkeypatch)
    st = _station(Config(radio={"voice_route": "cloud", "tts_listening_minutes": 1,
                                "room_tts_daily_minutes": 0.5}))
    L.player_fetch(st.dir)
    st.s["room_tts_daily_minutes"] = 0.5
    monkeypatch.setattr(V, "vertex", lambda *a, **kw: (b"paid", 2))
    if exhaust == "shared":
        st.usage.add(60)
    else:
        st.usage.for_room().add(30)
    assert st.speak_room("המנחה: שלום.", "moderator", lambda: True, False)
    assert st.status["route"] == "say"
    assert RR.view(st.cfg)["tts_minutes_today"] == (0 if exhaust == "shared" else 0.5)
    # A spent room sub-cap leaves the rest for radio; a spent shared cap blocks both.
    assert st.speak("מבזק חדש.")
    assert st.status["route"] == ("say" if exhaust == "shared" else "vertex")
    assert radio.minutes(st.cfg)["tts_minutes_today"] == round(st.usage.today() / 60, 1)


def test_room_request_is_capped_by_remaining_shared_minutes(monkeypatch):
    _free(monkeypatch)
    st = _station(Config(radio={"voice_route": "cloud", "tts_listening_minutes": 1,
                                "room_tts_daily_minutes": 1}))
    L.player_fetch(st.dir)
    st.usage.add(57)
    calls = []

    def vertex(*a, max_seconds=0, hard=False, **kw):
        calls.append((max_seconds, hard))
        return b"paid", max_seconds

    monkeypatch.setattr(V, "vertex", vertex)
    assert st.speak_room("שלום.", "moderator", lambda: True, False)
    assert calls == [(3, True)]  # room has a whole minute left, but only three shared seconds remain
    restarted = V.paid_usage(st.dir)
    assert restarted.today() == 60 and restarted.for_room().today() == 3
    assert st.speak("מבזק אחרי החדר.")
    assert st.status["route"] == "say" and len(calls) == 1


@pytest.mark.parametrize("seconds", [0, 0.01])
def test_vertex_never_sends_a_request_without_a_usable_ceiling(monkeypatch, seconds):
    monkeypatch.setattr(V, "access_token", lambda *a: "token")
    monkeypatch.setattr(V.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("uncapped paid request"))
    assert V.vertex("שלום", Config(gemini_vertex_project="p"), "m", "v", "", max_seconds=seconds,
                    hard=False) == (b"", 0.0)
