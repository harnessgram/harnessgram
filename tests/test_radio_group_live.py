"""The group's live stream is optional ("Live stream in the group" add-on): off, the radio opens no group stream and
publishes no RTMP, and only the private listen stream (HLS) carries the audio, with no picture work at all. On (the
default) it is today's radio. The YouTube mirror reads the home HLS audio, so it does not care."""
import io
import json
import threading
import time
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN
from ctb import addons, radio, radio_call, radio_listeners, radio_rtmp
from ctb.config import Config

OWNER = 111
GROUP = -1001234
RADIO = {"telethon_python": "/usr/bin/python3", "account_api_command": ["x"], "account_session_command": ["y"],
         "rtmp_url_command": ["u"], "rtmp_key_command": ["k"]}
VIDEO_FLAGS = ("-c:v", "libx264", "h264_videotoolbox", "image2pipe", "-vf", "-filter_complex", "pipe:3", "-g")


def cfg(**radio_over):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP, radio=dict(RADIO, **radio_over))


def group(on: bool, c=None):
    addons.set_on(c or cfg(), "radio_group_live", on)


def state():
    return json.loads((radio.folder() / "state.json").read_text())


@pytest.fixture
def no_launchd(monkeypatch):
    loaded = {"on": False}
    monkeypatch.setattr(radio.sys, "platform", "darwin")
    monkeypatch.setattr(radio, "job_pid", lambda: None)
    monkeypatch.setattr(radio, "plist_path", lambda: radio.folder() / "agents" / "radio.plist")
    monkeypatch.setattr(radio, "_job_loaded", lambda: loaded["on"])
    monkeypatch.setattr(radio, "_sync_copy", lambda *a: None)

    def launchctl(*args):
        loaded["on"] = args[0] == "bootstrap"
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(radio, "_launchctl", launchctl)
    return loaded


# --- the switch ------------------------------------------------------------------------------------------------

def test_the_switch_is_on_by_default_and_follows_the_add_on():
    c = cfg()
    assert addons.BY_KEY["radio_group_live"].default is True and radio.group_live_on(c) is True
    group(False, c)
    assert radio.group_live_on(c) is False and radio.settings(c)["group_live"] is False
    group(True, c)
    assert radio.group_live_on(c) is True


def test_a_failing_switch_read_keeps_todays_behaviour(monkeypatch):
    monkeypatch.setattr(addons, "enabled", lambda *a, **k: (_ for _ in ()).throw(OSError("damaged")))
    assert radio.group_live_on(cfg()) is True


def test_with_the_group_stream_off_the_private_stream_is_always_made():
    c = cfg(radio_player=False)
    assert radio.settings(c)["radio_player"] is False      # on: the owner's own choice stands
    group(False, c)
    assert radio.settings(c)["radio_player"] is True       # off: it is the radio's only output


# --- start and stop ----------------------------------------------------------------------------------------------

def _recorded():
    radio._write(radio.folder() / "state.json", {"on": False, "owned": True, "call": {"id": 777, "access_hash": 7}})


def test_off_start_never_runs_the_account_helper_and_needs_no_telegram_settings(monkeypatch, no_launchd):
    monkeypatch.setattr(radio, "call_helper", lambda *a, **k: pytest.fail("start must not call the account helper"))
    monkeypatch.setattr(radio, "_secret", lambda command: pytest.fail("start must not read the stream key"))
    bare = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP, radio={"every_minutes": 20})
    group(False, bare)
    assert radio.start(bare) == "radio_started_private" and no_launchd["on"] and radio.wanted()
    assert state()["on"] is True and state()["group_live"] is False and state().get("call", {}) == {}


def test_off_start_keeps_a_recorded_stream_exactly_and_never_ends_it(monkeypatch, no_launchd):
    """Only the existing stop path ends a stream recorded by an earlier run."""
    group(False)
    _recorded()
    monkeypatch.setattr(radio, "call_helper", lambda s, action, call=None, **k: pytest.fail("no ensure and no end"))
    assert radio.start(cfg()) == "radio_started_private"
    assert state()["owned"] is True and state()["call"] == {"id": 777, "access_hash": 7} and state()["on"] is True
    seen = []
    monkeypatch.setattr(radio, "call_helper", lambda s, action, call=None, **k: seen.append((action, call)) or
                        {"ok": True, "ended": True})
    assert radio.stop(cfg()) == "radio_stopped"
    assert seen == [("end", {"id": 777, "access_hash": 7})] and state()["call"] == {}


def test_a_failed_off_start_ends_nothing_and_forgets_nothing(monkeypatch, no_launchd):
    group(False)
    _recorded()
    monkeypatch.setattr(radio, "call_helper", lambda *a, **k: pytest.fail("a failed start must not end a stream"))

    def broken(extra=None):
        raise OSError("disk full")
    monkeypatch.setattr(radio, "plist_bytes", broken)
    with pytest.raises(radio.RadioError) as e:
        radio.start(cfg())
    assert e.value.key == "radio_job_failed" and not no_launchd["on"]
    assert state()["owned"] is True and state()["call"] == {"id": 777, "access_hash": 7}


def test_on_start_is_main_behaviour_and_records_the_mode(monkeypatch, no_launchd):
    group(True)
    seen = []

    def helper(s, action, call=None, **k):
        seen.append((action, call))
        return {"ok": True, "owned": True, "created": True, "call": {"id": 5, "access_hash": 6}}
    monkeypatch.setattr(radio, "call_helper", helper)
    assert radio.start(cfg()) == "radio_started"
    assert seen == [("ensure", None)] and state()["call"] == {"id": 5, "access_hash": 6}
    assert state()["owned"] is True and state()["group_live"] is True


def test_on_start_still_needs_the_stream_commands(no_launchd):
    group(True)
    with pytest.raises(radio.RadioError) as e:
        radio.start(Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP, radio={"every_minutes": 20}))
    assert e.value.key == "radio_not_configured"


def test_the_switch_is_read_only_at_start(monkeypatch, no_launchd):
    """A running radio keeps the mode it started with; the new switch value shows as pending until the next start."""
    c = cfg()
    group(True, c)
    monkeypatch.setattr(radio, "call_helper", lambda s, action, call=None, **k: {
        "ok": True, "owned": True, "created": True, "call": {"id": 5, "access_hash": 6}})
    radio.start(c)
    group(False, c)
    st = radio.status(c)
    assert st["group_live"] is True and st["mode"] == "group+private" and st["group_wanted"] is False
    assert radio.settings(c)["radio_player"] is True                      # configured value (default on) as before
    assert radio.settings(cfg(radio_player=False))["radio_player"] is False   # the running mode still wins over the switch
    monkeypatch.setattr(radio, "call_helper", lambda *a, **k: {"ok": True, "ended": True})
    radio.stop(c)                                                          # off: the radio no longer runs
    assert radio.status(c)["group_live"] is False and radio.status(c)["mode"] == "private"


def test_a_resume_after_a_restart_reads_the_switch_again_because_it_is_a_start(monkeypatch, no_launchd):
    group(False)
    monkeypatch.setattr(radio, "call_helper", lambda *a, **k: pytest.fail("no account use"))
    radio.set_wanted(True)
    assert radio._resume_once(cfg()) == "resumed" and state()["group_live"] is False


# --- status ------------------------------------------------------------------------------------------------------

def test_status_shows_the_mode_and_skips_telegram_when_private(monkeypatch):
    c = cfg()
    monkeypatch.setattr(radio, "job_pid", lambda: None)
    monkeypatch.setattr(radio, "call_helper", lambda s, action, call=None, **k: {"ok": True, "active": False})
    assert radio.status(c)["mode"] == "group+private" and radio.status(c)["group_live"] is True
    assert "call" in radio.status(c, live=True)
    group(False, c)
    monkeypatch.setattr(radio, "call_helper", lambda *a, **k: pytest.fail("no question to Telegram"))
    st = radio.status(c, live=True)
    assert st["mode"] == "private" and st["group_live"] is False and "call" not in st


def test_the_cockpit_card_carries_the_mode_and_the_next_start_note():
    from ctb import panel_page
    for lang in ("he", "en"):
        strings = panel_page.STRINGS[lang]
        assert strings["radioModePrivate"] and strings["radioModeGroup"] and strings["radioModeNext"]
    page = panel_page.render_page("en", "nonce").decode()
    assert "radioModePrivate" in page and "radioModeNext" in page and "group_wanted" in page
    a = addons.BY_KEY["radio_group_live"]
    assert "next time the radio starts" in a.text_en and "בהפעלה הבאה" in a.text_he


# --- the encoder -------------------------------------------------------------------------------------------------

def test_audio_only_encoder_has_no_video_arguments_at_all():
    args = radio.encoder_args("libx264", audio_only=True)
    for flag in VIDEO_FLAGS:
        assert flag not in args, flag
    assert args.count("-i") == 1 and args[args.index("-i") + 1] == "pipe:0"
    assert args[args.index("-c:a") + 1] == "aac" and args[args.index("-b:a") + 1] == "96k"
    assert args[args.index("-ar", args.index("-c:a")) + 1] == "48000"
    assert args[-5:] == ["-f", "flv", "-flvflags", "no_duration_filesize", "pipe:1"]
    assert "rtmp" not in " ".join(args) and "-map" in args and args[args.index("-map") + 1] == "0:a"


def test_audio_only_audio_matches_the_group_encoders_audio():
    """The private stream sounds the same either way: same codec, rate, channels and bit rate."""
    def audio(args):
        return [args[args.index(f) + 1] for f in ("-c:a", "-b:a")] + [args[args.index("-ar", args.index("-c:a")) + 1],
                                                                       args[args.index("-ac", args.index("-c:a")) + 1]]
    assert audio(radio.encoder_args("libx264", audio_only=True)) == audio(radio.encoder_args("libx264"))


def test_the_group_encoder_is_unchanged():
    args = radio.encoder_args("libx264")
    assert "-c:v" in args and "image2pipe" in args and args.count("-i") == 2


# --- the pipeline ------------------------------------------------------------------------------------------------

class FakeProc:
    """An encoder that has already produced some FLV bytes and then stops."""
    launched = []

    def __init__(self, args, **kw):
        FakeProc.launched.append((args, kw))
        self.pid = 4242
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(b"FLV\x01\x04\x00\x00\x00\x09" + b"audio" * 50)

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


class FakeSegmenter:
    made = []

    def __init__(self, path, report=None, *a, **k):
        self.path, self.data, self.closed = path, b"", False
        FakeSegmenter.made.append(self)

    def offer(self, data):
        self.data += data

    def close(self):
        self.closed = True


def test_an_audio_only_pipeline_publishes_nothing_and_feeds_only_the_private_stream(monkeypatch):
    FakeProc.launched, FakeSegmenter.made = [], []
    monkeypatch.setattr(radio.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(radio_rtmp, "PlayerSegmenter", FakeSegmenter)
    monkeypatch.setattr(radio_rtmp, "Publisher", lambda *a, **k: pytest.fail("no RTMP publisher without a group stream"))
    pids = []
    pipe = radio.Pipeline("", "", radio.folder() / "card.png", "", radio.Mixer(bytes(4)), pids.append, audio_only=True)
    assert pipe.run() == "the encoder stopped"
    args, kw = FakeProc.launched[0]
    assert pids == [4242] and kw.get("pass_fds") == () and args == radio.encoder_args("", audio_only=True,
                                                                                      marker=radio.MARKER)
    assert all(flag not in args for flag in VIDEO_FLAGS)
    seg = FakeSegmenter.made[0]
    assert seg.path == radio.folder() / "private-hls" and seg.data.startswith(b"FLV") and seg.closed


@pytest.mark.parametrize("source", ["background", "show"])
def test_legacy_hls_never_receives_library_or_podcast_segments(monkeypatch, source):
    legacy = radio.folder() / "hls"
    legacy.mkdir()
    (legacy / "segment-1.ts").write_bytes(b"old station speech")
    FakeSegmenter.made = []
    public = radio.SpeechMixer(bytes(4))
    private = radio.PrivateMixer(bytes(4), public)
    private.background_enabled = True
    setattr(private, source, SimpleNamespace(paused=False, read=lambda size: b"\x20\x00" * (size // 2)))
    music = private.next()
    assert music != bytes(len(music)) and public.next() == bytes(len(music))

    class EncodedPrivate(FakeProc):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.stdout = io.BytesIO(b"FLV" + music)  # fake encoding, no ffmpeg

    monkeypatch.setattr(radio.subprocess, "Popen", EncodedPrivate)
    monkeypatch.setattr(radio_rtmp, "PlayerSegmenter", FakeSegmenter)
    pipe = radio.Pipeline("", "", radio.folder() / "card", "", private, lambda pid: None, audio_only=True)
    assert pipe.run() == "the encoder stopped"
    assert [(seg.path, seg.data) for seg in FakeSegmenter.made] == [(radio.folder() / "private-hls", b"FLV" + music)]
    assert (legacy / "segment-1.ts").read_bytes() == b"old station speech"
    assert len(list(legacy.iterdir())) == 1


def test_an_audio_only_pipeline_never_starts_a_picture_writer(monkeypatch):
    FakeProc.launched, FakeSegmenter.made = [], []
    monkeypatch.setattr(radio.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(radio_rtmp, "PlayerSegmenter", FakeSegmenter)
    monkeypatch.setattr(radio.Pipeline, "_pictures", lambda *a, **k: pytest.fail("no picture is written"))
    pipe = radio.Pipeline("", "", radio.folder() / "card.png", "", radio.Mixer(bytes(4)), lambda pid: None,
                          audio_only=True)
    assert pipe.hls is True and pipe.run() == "the encoder stopped"


def test_the_group_pipeline_still_publishes(monkeypatch):
    FakeProc.launched, built = [], []

    class Pub:
        diag = None

        def __init__(self, url, key):
            built.append((url, key))

        def connect(self):
            pass

        def player_output(self, path, report=None):
            pass

        def send_flv(self, stream, stop=None):
            stream.read()

        def close(self):
            pass
    monkeypatch.setattr(radio.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(radio_rtmp, "Publisher", Pub)
    card = radio.folder() / "card.png"
    card.write_bytes(b"x")
    pipe = radio.Pipeline("rtmps://example.org/s/", "key", card, "libx264", radio.Mixer(bytes(4)), lambda pid: None)
    assert pipe.run() == "the encoder stopped"
    assert built == [("rtmps://example.org/s/", "key")] and "-c:v" in FakeProc.launched[0][0]


def test_the_local_sink_copies_what_the_encoder_makes_and_nothing_else():
    sink = radio_rtmp.LocalSink()
    sink.player = FakeSegmenter(None)
    sink.connect()
    sink.send_flv(io.BytesIO(b"x" * 10000))
    assert sink.player.data == b"x" * 10000 and sink.sent_bytes == 10000
    sink.close()
    assert sink.player.closed


# --- the station -------------------------------------------------------------------------------------------------

class Script:
    """A pipeline double: records how it was built and runs a scripted step."""
    built, steps = [], []

    def __init__(self, url, key, card, encoder, mixer, on_pid, **kw):
        Script.built.append(dict(url=url, key=key, encoder=encoder, **kw))
        self.kw, self.quit = kw, kw["quit"]

    def run(self):
        return Script.steps.pop(0)(self)


@pytest.fixture
def station(monkeypatch, no_launchd):
    Script.built, Script.steps = [], []
    c = cfg()
    monkeypatch.setattr(radio, "make_bed", lambda kind, db: bytes(4))
    monkeypatch.setattr(radio, "Pipeline", Script)
    monkeypatch.setattr(radio, "pick_encoder", lambda wanted: "libx264")
    monkeypatch.setattr(radio, "_secret", lambda command: "secret-" + command[0])
    monkeypatch.setattr(radio.radio_card, "plain", lambda path: None)
    monkeypatch.setattr(radio, "_sync_copy", lambda *a: None)
    monkeypatch.setattr(radio.Station, "_start_audio", lambda *a, **kw: None)
    st = radio.Station(c)
    for loop in ("listener_loop", "content_loop", "caption_loop", "talk_loop", "room_loop", "podcast_loop"):
        monkeypatch.setattr(st, loop, lambda: None)
    monkeypatch.setattr(st, "gather", lambda now: {"at": now})
    drawn = []
    monkeypatch.setattr(st, "draw", lambda snap, top=None: drawn.append(snap))
    st.drawn = drawn
    radio.set_wanted(True)
    return st




def _stop(pipe):
    pipe.quit.set()
    return ""


def test_an_off_station_runs_the_audio_only_pipeline_and_never_touches_telegram(monkeypatch, station):
    radio._write(radio.folder() / "state.json", {"on": True, "group_live": False})
    st = radio.Station(cfg())
    for loop in ("content_loop", "caption_loop", "talk_loop", "room_loop", "podcast_loop"):
        monkeypatch.setattr(st, loop, lambda: None)
    monkeypatch.setattr(radio, "_secret", lambda command: pytest.fail("no stream key without a group stream"))
    monkeypatch.setattr(radio, "pick_encoder", lambda w: pytest.fail("no video encoder"))
    monkeypatch.setattr(radio, "call_helper", lambda *a, **k: pytest.fail("no account use"))
    monkeypatch.setattr(st, "draw", lambda *a, **k: pytest.fail("nothing is drawn"))
    Script.steps = [_stop]
    assert st._run() == 0
    built = Script.built[0]
    assert built.get("audio_only") is True and built["url"] == "" and built["key"] == "" and built.get("source") is None
    assert json.loads((radio.folder() / "status.json").read_text())["picture"] == "none"


def test_an_on_station_is_main_behaviour(monkeypatch, station):
    """The group path: key read, first card drawn, encoder picked, the card or board pipeline with the key."""
    radio._write(radio.folder() / "state.json", {"on": True, "group_live": True})
    st = radio.Station(cfg())
    for loop in ("listener_loop", "content_loop", "caption_loop", "talk_loop", "room_loop", "podcast_loop"):
        monkeypatch.setattr(st, loop, lambda: None)
    drawn = []
    monkeypatch.setattr(st, "gather", lambda now: {"at": now})
    monkeypatch.setattr(st, "draw", lambda snap, top=None: drawn.append(snap))
    Script.steps = [_stop]
    assert st._run() == 0
    built = Script.built[0]
    assert not built.get("audio_only") and built["url"] == "secret-u" and built["key"] == "secret-k"
    assert built["encoder"] == "libx264" and built["hls"] is True and len(drawn) == 1


def test_a_pipeline_failure_is_counted_like_main_in_the_off_station(monkeypatch, station):
    radio._write(radio.folder() / "state.json", {"on": True, "group_live": False})
    st = radio.Station(cfg())
    for loop in ("content_loop", "caption_loop", "talk_loop", "room_loop", "podcast_loop"):
        monkeypatch.setattr(st, loop, lambda: None)
    monkeypatch.setattr(st.quit, "wait", lambda seconds: True)
    ended = []
    monkeypatch.setattr(radio, "give_up", lambda s: ended.append(True) or 0)
    Script.steps = [lambda pipe: "the encoder stopped"] * radio.MAX_FAILURES
    assert st._run() == 0 and ended == [True]


# --- no picture work without a group stream -----------------------------------------------------------------------

def test_nothing_is_drawn_or_shown_while_private(station, monkeypatch):
    station.s["group_live"] = False
    station._safe_draw(None)
    station.snap = {"at": time.time()}
    assert station.picture_wanted() is False and station.drawn == []
    monkeypatch.setattr(radio, "board_on", lambda: True)
    assert station.board_wanted() is False
    station.s["group_live"] = True
    station._safe_draw(None)
    assert len(station.drawn) == 1 and station.board_wanted() is True


def test_the_call_listener_is_not_asked_while_private(monkeypatch, tmp_path):
    st = object.__new__(radio.Station)
    st.s = dict(radio.DEFAULTS, telethon_python="/py", account_api_command=["a"], account_session_command=["b"],
                group_live=False)
    st.dir, st.quit = tmp_path, threading.Event()
    calls = []
    monkeypatch.setattr(st.quit, "wait", lambda seconds: st.quit.set() or True)
    monkeypatch.setattr(radio, "call_helper", lambda s, action: calls.append(action) or {"ok": False})
    st.listener_loop()
    assert calls == []
    alone = dict(st.s, radio_player=False)
    assert radio_listeners.snapshot(tmp_path, dict(alone, group_live=False))["listening_source"] == "unavailable"
    assert radio_listeners.snapshot(tmp_path, dict(alone, group_live=True))["listening_source"] == "unknown"


# --- the YouTube mirror ------------------------------------------------------------------------------------------

def test_the_mirror_reads_separate_speech_hls_so_it_needs_no_home_picture():
    """A separate local speech-only output exists even when the group has no video pipeline."""
    from ctb import radio_youtube as Y
    from test_radio_youtube_mirror import live_home
    c = cfg(radio_player=False)
    live_home()
    radio._write(radio.folder() / "state.json", {"on": True, "group_live": True})
    assert Y.mirror_ready(c) is False       # the owner switched the private stream off and the group stream is on
    radio._write(radio.folder() / "state.json", {"on": True, "group_live": False})
    assert Y.mirror_ready(c) is True        # private is the only output now, so it is always made
    assert Y.mirror_playlist() == radio.folder() / "public-hls" / "live.m3u8"


# --- the add-on --------------------------------------------------------------------------------------------------

def test_the_add_on_is_listed_in_voice_with_both_languages():
    a = addons.BY_KEY["radio_group_live"]
    assert a.group == "voice" and a.title_he == "שידור חי בקבוצה" and not a.locked
    assert a.title_en == "Live stream in the group"


# --- the segmenter restarts cleanly -------------------------------------------------------------------------------

def _tag(kind, payload, ts=0):
    return (bytes([kind]) + len(payload).to_bytes(3, "big") + ts.to_bytes(3, "big") + b"\x00" + b"\x00\x00\x00"
            + payload + (11 + len(payload)).to_bytes(4, "big"))


HEADER = b"FLV\x01\x04\x00\x00\x00\x09\x00\x00\x00\x00"
SCRIPT = _tag(18, b"\x02\x00\x0aonMetaData")
SEQ = _tag(8, b"\xaf\x00\x12\x10")
DATA = [_tag(8, b"\xaf\x01" + bytes([i]) * 20, ts=i * 21) for i in range(1, 30)]
STREAM = HEADER + SCRIPT + SEQ + b"".join(DATA)


def test_the_tracker_finds_tag_starts_and_the_stream_prefix_in_any_chunking():
    for size in (1, 5, 13, 37, 1000):
        tr = radio_rtmp.FlvTracker()
        starts = []
        for i in range(0, len(STREAM), size):
            first = tr.feed(STREAM[i:i + size])
            if first is not None:
                starts.append(i + first)
        assert tr.done and tr.prefix == HEADER + SCRIPT + SEQ, size
        assert starts[0] == len(HEADER)
        assert STREAM[starts[-1]:starts[-1] + 1] == b"\x08"


class Recorder:
    """A segmenter process double that records every byte written to it."""
    procs = []

    def __init__(self, args, **kw):
        self.data, self.alive = bytearray(), True
        self.stdin = self
        Recorder.procs.append(self)

    def write(self, b):
        self.data += b

    def flush(self):
        pass

    def poll(self):
        return None if self.alive else 1

    def kill(self):
        self.alive = False


def test_a_restarted_segmenter_gets_the_header_and_starts_on_a_whole_tag(tmp_path, monkeypatch):
    """P2: its retry used to receive bytes from the middle of the stream, with no header."""
    Recorder.procs = []
    monkeypatch.setattr(radio_rtmp.subprocess, "Popen", Recorder)
    player = radio_rtmp.PlayerSegmenter(tmp_path / "hls")
    cut = len(HEADER + SCRIPT + SEQ) + len(DATA[0]) + len(DATA[1]) + 7        # inside the third data tag
    player.offer(STREAM[:cut])
    deadline = time.time() + 3
    while not Recorder.procs[0].data and time.time() < deadline:
        time.sleep(0.01)
    assert bytes(Recorder.procs[0].data) == STREAM[:cut]                       # the first run gets everything
    player._disable()                                                          # the segmenter failed
    player._last_start = float("-inf")                                         # and its retry is allowed
    rest = STREAM[cut:]
    player.offer(rest[:10])                                                    # still inside the same tag
    player.offer(rest[10:])
    deadline = time.time() + 3
    while time.time() < deadline and (len(Recorder.procs) < 2 or
                                      len(Recorder.procs[1].data) < len(HEADER + SCRIPT + SEQ) + 100):
        time.sleep(0.01)
    got = bytes(Recorder.procs[1].data)
    assert got.startswith(HEADER + SCRIPT + SEQ)
    body = got[len(HEADER + SCRIPT + SEQ):]
    tags = b"".join(DATA)
    # the first tag the retry gets is a whole one (never a fragment), and everything after it is in order
    assert body and body in tags and any(body.startswith(tag) for tag in DATA[3:])
    assert body == tags[tags.index(body):]
    player.close()




def test_a_segmenter_replaced_before_its_writer_noticed_still_gets_a_clean_start(tmp_path, monkeypatch):
    """P2: the process exited, nobody called _disable, and _start replaced it: it must not get bytes from mid-stream."""
    Recorder.procs = []
    monkeypatch.setattr(radio_rtmp.subprocess, "Popen", Recorder)
    player = radio_rtmp.PlayerSegmenter(tmp_path / "hls")
    cut = len(HEADER + SCRIPT + SEQ) + len(DATA[0]) + len(DATA[1]) + 7
    player.offer(STREAM[:cut])
    deadline = time.time() + 3
    while not Recorder.procs[0].data and time.time() < deadline:
        time.sleep(0.01)
    Recorder.procs[0].alive = False          # it exited by itself; the writer is blocked waiting and has not noticed
    player._last_start = float("-inf")       # the restart pause is over
    rest = STREAM[cut:]
    player.offer(rest[:10])                  # replaces the process; still inside a tag
    assert len(Recorder.procs) == 2
    player.offer(rest[10:])
    deadline = time.time() + 3
    while time.time() < deadline and len(Recorder.procs[1].data) < len(HEADER + SCRIPT + SEQ) + 100:
        time.sleep(0.01)
    got = bytes(Recorder.procs[1].data)
    tags = b"".join(DATA)
    assert got.startswith(HEADER + SCRIPT + SEQ)
    body = got[len(HEADER + SCRIPT + SEQ):]
    assert any(body.startswith(tag) for tag in DATA[3:]) and body == tags[tags.index(body):]
    player.close()



# --- round 5 ------------------------------------------------------------------------------------------------------

def test_a_failed_on_start_ends_only_a_stream_it_created(monkeypatch, no_launchd):
    """Main ended any owned stream here, a taken-back one included; now only the stream THIS start created."""
    group(True)
    for created, ended in ((True, 1), (False, 0)):
        seen = []

        def helper(s, action, call=None, **k):
            seen.append(action)
            return {"ok": True, "owned": True, "created": created, "call": {"id": 5, "access_hash": 6}}
        monkeypatch.setattr(radio, "call_helper", helper)
        monkeypatch.setattr(radio, "_launchctl", lambda *a: (_ for _ in ()).throw(OSError("no launchctl")))
        with pytest.raises(radio.RadioError):
            radio.start(cfg())
        assert seen.count("end") == ended, created
        if not created:   # the taken-back stream stays recorded, so a later stop can still end it
            assert radio.owned_call(state()) == {"id": 5, "access_hash": 6}


def test_a_private_station_that_gives_up_never_ends_a_recorded_stream(monkeypatch, no_launchd):
    group(False)
    radio._write(radio.folder() / "state.json", {"on": True, "owned": True, "group_live": False,
                                                 "call": {"id": 777, "access_hash": 7}})
    monkeypatch.setattr(radio, "call_helper", lambda *a, **k: pytest.fail("a private radio must not end a stream"))
    monkeypatch.setattr(radio, "_bootout_self", lambda: None)
    assert radio.give_up(radio.settings(cfg())) == 0
    assert state()["call"] == {"id": 777, "access_hash": 7} and state()["owned"] is True
    assert json.loads((radio.folder() / "status.json").read_text())["gave_up"] is True


def test_a_group_station_that_gives_up_still_ends_its_stream(monkeypatch, no_launchd):
    group(True)
    radio._write(radio.folder() / "state.json", {"on": True, "owned": True, "group_live": True,
                                                 "call": {"id": 777, "access_hash": 7}})
    seen = []
    monkeypatch.setattr(radio, "call_helper", lambda s, action, call=None, **k: seen.append(action) or
                        {"ok": True, "ended": True})
    monkeypatch.setattr(radio, "_bootout_self", lambda: None)
    radio.give_up(radio.settings(cfg()))
    assert seen == ["end"]


def test_a_big_first_video_tag_does_not_lose_the_saved_start():
    big = _tag(9, b"\x17\x01" + b"v" * 200_000)                 # an AVC keyframe far larger than 64 KiB
    stream = HEADER + SCRIPT + SEQ + big + b"".join(DATA)
    for size in (4096, 65536, 1 << 20):
        tr = radio_rtmp.FlvTracker()
        for i in range(0, len(stream), size):
            tr.feed(stream[i:i + size])
        assert tr.done and tr.prefix == HEADER + SCRIPT + SEQ, size
    # and while the big tag is still arriving, the start is already settled (nothing grows without bound)
    tr = radio_rtmp.FlvTracker()
    tr.feed(stream[:len(HEADER + SCRIPT + SEQ) + 20])
    assert tr.done and tr.prefix == HEADER + SCRIPT + SEQ and len(tr.raw) == 0


def test_headers_that_never_settle_are_capped():
    tr = radio_rtmp.FlvTracker()
    tr.feed(HEADER)
    tr.feed(bytes([18]) + (1 << 24 - 1).to_bytes(3, "big") + b"\x00" * 7)    # a script tag declaring 16 MB
    for _ in range(40):
        tr.feed(b"x" * 65536)
    assert tr.done and tr.prefix == b"" and len(tr.raw) == 0
