"""Bulletins use a fake clock, fake calendar and fake queue, never a live radio or config."""
import inspect
import os
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN
from ctb import radio, radio_suitability, radio_youtube as Y, schedule_bulletin as B, schedule_hours as H
from ctb.config import Config

DAY = "2026-11-16"
START = datetime.fromisoformat(DAY + "T08:00:00+02:00").timestamp()


def setup_conductor(monkeypatch, offset=0, **kw):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=111, timezone="Asia/Jerusalem")
    monkeypatch.setattr(radio_suitability, "suitable", lambda *a: True)
    clock = [START + offset]
    calls = []
    permission = [True]
    rows = [H._block("first", "Project Alpha", 480, 540, "topic", "Review draft"),
            H._block("second", "לימוד", 540, 600, "topic")]
    data = {"today": DAY, "timeline": {"today": {"available": True, "blocks": rows}}}
    def speak(text, gate):
        calls.append((text, gate))
        return gate()
    conductor = B.Conductor(cfg, speak=speak, allowed=lambda: permission[0],
                            clock=lambda: clock[0], get_view=lambda *a, **k: data, **kw)
    return conductor, clock, calls, permission, data


def test_once_per_block_with_plan_and_next_topic_even_after_restart(monkeypatch):
    c, clock, calls, permission, data = setup_conductor(monkeypatch)
    c.step()
    assert len(calls) == 1 and all(s in calls[0][0] for s in ("Project Alpha", "Review draft", "לימוד"))
    clock[0] += 10
    c.step()
    assert len(calls) == 1
    restarted, _, again, _, _ = setup_conductor(monkeypatch, offset=20)
    restarted.step()
    assert again == []
    clock[0] = START + 3600
    c.step()
    assert len(calls) == 2 and "לימוד" in calls[1][0]
    assert not calls[0][1]()  # an older tag cannot borrow the next block's gate


@pytest.mark.parametrize("offset", [-1, 60, 1800, 3600 + 61])
def test_no_early_or_stale_catchup(monkeypatch, offset):
    c, clock, calls, _, _ = setup_conductor(monkeypatch, offset=offset)
    c.step()
    assert calls == []


def test_permission_failure_and_calendar_failure_are_silent(monkeypatch):
    c, clock, calls, permission, data = setup_conductor(monkeypatch)
    permission[0] = False
    c.step()
    assert calls == []
    permission[0] = True
    data["timeline"]["today"]["available"] = False
    c.step()
    assert calls == []


def test_queued_and_playing_clip_expire_on_edit_off_or_shabbat(monkeypatch):
    c, clock, calls, permission, data = setup_conductor(monkeypatch)
    c.step()
    gate = calls[0][1]
    assert gate() and gate.playing()
    data["timeline"]["today"]["blocks"] = []
    clock[0] += 6
    c.step()
    assert not gate() and not gate.playing()
    monkeypatch.setattr(H, "quiet_until", lambda *a: (True, clock[0]))
    c.step()
    assert c.active is None


def test_a_slow_voice_or_a_queue_backlog_never_announces_late(monkeypatch):
    c, clock, calls, _, _ = setup_conductor(monkeypatch)
    def slow(text, gate):
        clock[0] += 61
        calls.append((text, gate))
        return gate()
    c.speak = slow
    c.step()
    assert len(calls) == 1 and not calls[0][1]()
    restarted, _, repeated, _, _ = setup_conductor(monkeypatch, offset=20)
    restarted.step()
    assert repeated == []


@pytest.mark.parametrize("public_speech", [False, True])
def test_bulletin_never_enters_public_queue_or_changes_public_audio(monkeypatch, public_speech):
    c, clock, gate, station, mirror_ready = private_playback(monkeypatch, public_speech=public_speech)
    public = station.group_mixer
    before = tuple(public.clips)
    assert station.mixer.pending() == 1
    assert len(before) == int(public_speech)  # the bulletin has no entry in the shared queue
    assert Y.mirror_playlist() == radio.folder() / "public-hls" / "live.m3u8"
    assert Y.mirror_playlist().parent != radio.folder() / radio.PRIVATE_HLS
    # Compare every frame against the same public mixer without a private bulletin.
    control = radio.SpeechMixer(bytes(4))
    if public_speech:
        control.add(b"\x02\x00" * radio.RATE, tag=99)
        control.gain = 0
    for _ in range(50):
        assert gate.playing() and mirror_ready()
        assert station.mixer.next(100) == b"\x01\x00" * 100
        output = public.next(100)
        assert output == control.next(100)
        assert output == (b"\x02\x00" * 100 if public_speech else bytes(200))
    assert not radio.private_audio_on()  # no hold is created or required


@pytest.mark.parametrize("audio_only,public_audio", [(False, False), (True, True)])
def test_bulletin_mixer_cannot_be_attached_to_a_public_pipeline(monkeypatch, audio_only, public_audio):
    _, _, _, station, _ = private_playback(monkeypatch)
    with pytest.raises(ValueError, match="private audio"):
        radio.Pipeline("unused", "unused", station.card, "", station.mixer, lambda pid: None,
                       audio_only=audio_only, public_audio=public_audio)


def test_bulletin_encoder_feeds_only_private_hls_and_preserves_legacy_hls(monkeypatch):
    import io
    from ctb import radio_rtmp
    from test_radio_group_live import FakeProc, FakeSegmenter
    _, _, _, station, mirror_ready = private_playback(monkeypatch)
    legacy = radio.folder() / "hls"
    legacy.mkdir()
    (legacy / "segment-1.ts").write_bytes(b"old station speech")
    FakeSegmenter.made = []
    audio = station.mixer.next(100)
    assert audio == b"\x01\x00" * 100

    class EncodedBulletin(FakeProc):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.stdout = io.BytesIO(b"FLV" + audio)  # fake encoding, never ffmpeg

    monkeypatch.setattr(radio.subprocess, "Popen", EncodedBulletin)
    monkeypatch.setattr(radio_rtmp, "PlayerSegmenter", FakeSegmenter)
    monkeypatch.setattr(radio_rtmp, "Publisher", lambda *a, **kw: pytest.fail("no transmitter"))
    pipe = radio.Pipeline("", "", station.card, "", station.mixer, lambda pid: None, audio_only=True)
    assert pipe.run() == "the encoder stopped"
    assert [(s.path, s.data, s.closed) for s in FakeSegmenter.made] == [
        (radio.folder() / radio.PRIVATE_HLS, b"FLV" + audio, True)]
    assert (legacy / "segment-1.ts").read_bytes() == b"old station speech"
    assert len(list(legacy.iterdir())) == 1
    assert mirror_ready() and station.group_mixer.next(100) == bytes(200)


def test_only_the_private_station_attaches_the_conductor():
    assert "schedule_loop" in inspect.getsource(radio.Station._run_private)
    assert "schedule_loop" not in inspect.getsource(radio.Station._run)
    loop = inspect.getsource(radio.Station.schedule_loop)
    assert "allowed=self.schedule_allowed" in loop and "schedule_clip=True" in loop


def test_private_station_needs_no_hold_worker(monkeypatch):
    launched = []
    class Worker:
        def __init__(self, **kw):
            launched.append(kw)

        def start(self):
            pass  # never start a real station or background worker

    monkeypatch.setattr(radio.threading, "Thread", Worker)
    station = object.__new__(radio.Station)
    station.cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=111)
    station.quit = SimpleNamespace(wait=lambda seconds: True)
    station.mixer = radio.PrivateMixer(bytes(4), radio.SpeechMixer(bytes(4)))
    station.schedule_loop()
    assert launched == []


def test_current_private_clip_is_cut_before_the_next_audio_frame():
    mixer = radio.PrivateMixer(bytes(4), radio.SpeechMixer(bytes(4)))
    mixer.add_private(b"\x01\x00" * 10000, tag=9)
    allowed = [True]
    mixer.active_gate = lambda tag: allowed[0]
    assert mixer.next(100) == b"\x01\x00" * 100
    allowed[0] = False
    assert mixer.next(100) == b"\x00\x00" * 100


def test_a_private_bulletin_waiting_past_its_deadline_never_starts(monkeypatch):
    _, clock, _, station, mirror_ready = private_playback(monkeypatch)
    clock[0] = START + 60
    assert station.mixer.next(100) == bytes(200)
    assert station.mixer.pending() == 0
    assert mirror_ready() and station.group_mixer.next(100) == bytes(200)


@pytest.mark.parametrize("group,on,addon,mirror,job,expected", [
    (False, True, True, False, False, True),
    (True, True, True, False, False, False),
    (False, False, True, False, False, False),
    (False, True, False, False, False, False),
    (False, True, True, True, False, True),
    (False, True, True, False, True, True),
])
def test_station_permission_does_not_depend_on_public_mirror_state(monkeypatch, group, on, addon, mirror, job, expected):
    from ctb import addons, policy, radio_youtube
    station = object.__new__(radio.Station)
    station.cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=111)
    station.s = {"group_live": group}
    monkeypatch.setattr(policy, "on", lambda: on)
    monkeypatch.setattr(addons, "enabled", lambda *a: addon)
    monkeypatch.setattr(radio, "_mirror_clear", lambda *a: not mirror)
    monkeypatch.setattr(radio_youtube, "_job_on", lambda: job)
    assert station.schedule_allowed() is expected


def test_shabbat_deadline_cuts_voice_at_the_exact_boundary(monkeypatch):
    c, clock, calls, _, _ = setup_conductor(monkeypatch)
    monkeypatch.setattr(H, "quiet_until", lambda *a: (False, START + 10))
    c.step()
    assert calls[0][1].playing()
    clock[0] = START + 10
    assert not calls[0][1].playing()


def test_late_tick_expires_at_the_block_minute_not_a_minute_after_the_tick(monkeypatch):
    c, clock, calls, _, _ = setup_conductor(monkeypatch, offset=59)
    c.step()
    assert len(calls) == 1 and calls[0][1].playing()
    clock[0] = START + 60
    assert not calls[0][1].playing()


def test_failed_or_corrupt_claims_never_speak(monkeypatch):
    from ctb.config import state_dir
    path = state_dir() / B.CLAIMS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{broken')
    c, clock, calls, _, _ = setup_conductor(monkeypatch)
    c.step()
    assert calls == []


def private_playback(monkeypatch, public_speech=False):
    """Real conductor, speech routing and mixers; fake TTS/HLS, no encoder or transmitter."""
    from ctb import addons, policy, radio_voice
    c, clock, _, _, _ = setup_conductor(monkeypatch)
    monkeypatch.setattr(radio.time, "time", lambda: clock[0])
    monkeypatch.setattr(radio, "wanted", lambda: True)
    monkeypatch.setattr(radio, "group_live_on", lambda cfg: False)
    monkeypatch.setattr(addons, "enabled", lambda cfg, key: key == "schedule_card")
    monkeypatch.setattr(policy, "on", lambda: True)
    monkeypatch.setattr(radio_voice, "speak", lambda *a: (b"\x01\x00" * 10000, "fake"))
    monkeypatch.setattr(radio_voice, "intro", lambda: b"\x01\x00" * 100)
    monkeypatch.setattr(radio_voice, "spoken_log", lambda *a: None)
    station = radio.Station(c.cfg)  # only isolated temp files; run() is never called
    monkeypatch.setattr(station, "_status", lambda **kw: None)
    if public_speech:
        station.group_mixer.add(b"\x02\x00" * radio.RATE, tag=99)
        station.group_mixer.gain = 0
    monkeypatch.setattr(station, "post_channel", lambda report: None if report is None else pytest.fail("private report"))
    playlist = Y.mirror_playlist()
    playlist.parent.mkdir()
    (playlist.parent / "segment-123.ts").write_bytes(b"public station audio")
    playlist.write_text("#EXTM3U\n#EXTINF:2,\nsegment-123.ts\n")
    public_files = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in playlist.parent.iterdir()}

    def conductor(cfg, speak, allowed):
        assert cfg is c.cfg
        c.speak, c.allowed = speak, allowed
        return c

    monkeypatch.setattr(B, "Conductor", conductor)
    ticks = iter((False, True))
    station.quit = SimpleNamespace(wait=lambda seconds: next(ticks))
    station.schedule_loop()  # exercise the real private-only callback and playback gates
    gate = station._schedule_gates[station.tag]

    def mirror_ready():
        assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in playlist.parent.iterdir()} == public_files
        # A fake public encoder's heartbeat, unrelated to bulletin playback.
        os.utime(playlist, (clock[0],) * 2)
        public_files[playlist.name] = (playlist.read_bytes(), playlist.stat().st_mtime_ns)
        return Y.mirror_ready(c.cfg)

    return c, clock, gate, station, mirror_ready


def test_blocked_calendar_refresh_never_exposes_bulletin_to_public_audio(monkeypatch):
    c, clock, gate, station, mirror_ready = private_playback(monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def blocked_view(*a, **kw):
        entered.set()
        release.wait()
        return {"today": DAY, "timeline": {"today": {"available": False, "blocks": []}}}

    c.get_view = blocked_view
    clock[0] = START + 5
    refresh = threading.Thread(target=c.step, daemon=True)
    refresh.start()
    try:
        assert entered.wait(2)
        for second in range(5, 61):
            clock[0] = START + second
            assert mirror_ready()
            expected = b"\x01\x00" * 100 if second < 60 else bytes(200)
            assert station.mixer.next(100) == expected
            assert station.group_mixer.next(100) == bytes(200)
    finally:
        release.set()
        refresh.join(2)
    assert not refresh.is_alive()


@pytest.mark.parametrize("marker", ["missing", "corrupt", "stale"])
def test_bulletin_is_private_without_any_mirror_hold(monkeypatch, marker):
    c, clock, gate, station, mirror_ready = private_playback(monkeypatch)
    legacy_marker = radio.folder() / "schedule-private-audio"
    if marker != "missing":
        legacy_marker.write_text("bad timestamp" if marker == "corrupt" else str(START - 1000))
        os.utime(legacy_marker, (START - 1000,) * 2)
    for second in (0, 7, 50, 60):
        clock[0] = START + second
        assert mirror_ready()
        expected = b"\x01\x00" * 100 if second < 60 else bytes(200)
        assert station.mixer.next(100) == expected
        assert station.group_mixer.next(100) == bytes(200)


@pytest.mark.parametrize("jump", [-120, 120])
def test_clock_correction_never_releases_private_audio_to_the_mirror(monkeypatch, jump):
    c, clock, gate, station, mirror_ready = private_playback(monkeypatch)
    assert station.mixer.next(100) == b"\x01\x00" * 100
    clock[0] = START + jump
    assert mirror_ready()
    expected = b"\x01\x00" * 100 if jump < 0 else bytes(200)
    assert station.mixer.next(100) == expected
    assert station.group_mixer.next(100) == bytes(200)
    clock[0] = START + 1
    assert mirror_ready()
    assert station.group_mixer.next(100) == bytes(200)
    if jump > 0:
        assert station.mixer.next(100) == bytes(200)  # expired PCM was discarded, never revived
