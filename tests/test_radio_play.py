"""Start the radio on Play (radio_play.py): the first authenticated playlist request starts it, privately and once;
a Play-started station stops by itself when nobody fetches; nothing here touches a group stream or the mirror."""
import json
import time
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN
from ctb import addons, cli, listen_link, policy, radio, radio_listeners, radio_play, radio_quiet, radio_voice
from ctb.config import Config
from test_listen_link import make
from test_panel import OWNER
from test_radio_lifecycle import GROUP, no_launchd  # noqa: F401  (fixture)
from test_radio_player import call, server  # noqa: F401  (fixtures)


@pytest.fixture
def play(server, monkeypatch):
    """A panel with the radio configured, nothing running, and a recorder in place of the detached start."""
    server.cfg.radio = {"radio_player": True}
    started = []
    monkeypatch.setattr(radio_play.sys, "platform", "darwin")
    monkeypatch.setattr(radio, "job_pid", lambda: None)
    monkeypatch.setattr(radio_play, "_spawn_start", lambda: started.append(time.time()))
    monkeypatch.setattr(radio, "call_helper", lambda *a, **k: pytest.fail("the group account helper must not run"))
    return SimpleNamespace(server=server, started=started)


def playlist(server, path):
    return call(server, "GET", path + "/live.m3u8")


def test_first_fetch_starts_the_station_and_answers_with_a_live_playlist_of_silence(play):
    path = make(play.server)
    status, headers, body = playlist(play.server, path)
    assert status == 200 and headers["Content-Type"] == "application/vnd.apple.mpegurl"
    lines = body.decode().splitlines()
    names = [l for l in lines if not l.startswith("#")]
    assert lines[0] == "#EXTM3U" and "#EXT-X-TARGETDURATION:2" in lines
    assert "#EXT-X-ENDLIST" not in lines and not any(l.startswith("#EXT-X-PLAYLIST-TYPE") for l in lines)   # live
    assert len(names) == radio_quiet.WINDOW and all(radio_quiet.parse_name(n) is not None for n in names)
    assert sum(l.startswith("#EXTINF:") for l in lines) == len(names)
    assert "#EXT-X-DISCONTINUITY" not in lines                        # nothing real to switch to yet
    assert len(play.started) == 1
    assert radio_listeners._read(radio.folder() / "player-listener.json").get("at")  # the fetch counts as presence
    status, headers, ts = call(play.server, "GET", path + "/" + names[-1])          # and the silence can be fetched
    assert status == 200 and headers["Content-Type"] == "video/mp2t" and ts == radio_quiet.segment(radio_quiet.parse_name(names[-1]))


def test_a_second_fetch_within_a_minute_does_not_start_again(play):
    cfg = play.server.cfg
    assert radio_play.request(cfg, now=1000.0) == radio_play.STARTING
    assert radio_play.request(cfg, now=1030.0) == radio_play.STARTING   # still on its way: the player keeps polling
    assert len(play.started) == 1
    assert radio_play.request(cfg, now=1061.0) == radio_play.STARTING   # a minute later a new attempt is allowed
    assert len(play.started) == 2


def test_a_bad_token_never_starts_anything(play):
    make(play.server)
    for bad in ("A" * 43, "short", "..", "x" * 200):
        status = call(play.server, "GET", "/listen/" + bad + "/live.m3u8")[0]
        assert status in (403, 404)   # the link's existing answer for an unknown token is "forbidden"
    assert play.started == []


def test_the_segment_route_never_starts_the_station(play):
    path = make(play.server)
    assert call(play.server, "GET", path + "/segment-0.ts")[0] == 404
    assert play.started == []


def test_addon_off_or_player_off_means_no_start(play):
    path = make(play.server)
    addons.set_on(play.server.cfg, "listen_link", False)
    assert call(play.server, "GET", path + "/live.m3u8")[0] == 404
    addons.set_on(play.server.cfg, "listen_link", True)
    play.server.cfg.radio = {}
    assert call(play.server, "GET", path + "/live.m3u8")[0] == 404
    assert play.started == []


def test_the_play_start_switch_and_the_mirror_switch(play, monkeypatch):
    cfg = play.server.cfg
    cfg.radio = {"radio_player": True, "play_start": False}
    assert radio_play.request(cfg, now=1000.0) == radio_play.OFF
    cfg.radio = {"radio_player": True}
    monkeypatch.setattr(policy, "on", lambda: False)   # mirroring is off: nothing starts
    assert radio_play.request(cfg, now=2000.0) == radio_play.OFF
    assert play.started == []


def test_spent_caps_mean_no_start_and_the_old_off_answer(play, monkeypatch):
    path = make(play.server)
    monkeypatch.setattr(radio_voice.Usage, "today", lambda self, now=None: 61 * 60.0)   # cap while listening: 60 min
    assert call(play.server, "GET", path + "/live.m3u8")[0] == 404
    assert play.started == []
    play.server.cfg.radio = {"radio_player": True, "tts_listening_minutes": 0}   # no paid speech at all: costs nothing
    assert radio_play.request(play.server.cfg, now=5000.0) == radio_play.STARTING
    assert len(play.started) == 1


def test_an_unreadable_ledger_counts_as_spent(play, monkeypatch):
    monkeypatch.setattr(radio_voice.Usage, "_ledger", lambda self: {"unreadable": True})
    assert radio_play.caps_spent(radio.settings(play.server.cfg)) is True


def test_a_station_that_is_up_is_only_warming_when_play_started_it(play, monkeypatch):
    cfg = play.server.cfg
    monkeypatch.setattr(radio, "job_pid", lambda: 4242)
    assert radio_play.request(cfg, now=1000.0) == radio_play.OFF           # the owner's station: old behaviour
    radio_play.mark(now=1000.0)
    assert radio_play.request(cfg, now=1010.0) == radio_play.STARTING      # Play's station, no segments yet
    assert radio_play.request(cfg, now=1000.0 + radio_play.WARM + 1) == radio_play.OFF
    assert play.started == []


def test_a_wanted_radio_is_left_to_its_own_resume(play):
    radio.set_wanted(True)
    assert radio_play.request(play.server.cfg, now=1000.0) == radio_play.OFF
    assert play.started == []


def test_no_token_or_url_is_stored_or_logged(play):
    path = make(play.server)
    token = path.rsplit("/", 1)[1]
    playlist(play.server, path)
    for f in radio.folder().rglob("*"):
        if f.is_file():
            assert token.encode() not in f.read_bytes() and b"/listen/" not in f.read_bytes()


# --- the idle stop ---------------------------------------------------------------------------------------------

def test_idle_stop_applies_to_a_play_started_station_only(play):
    cfg, s = play.server.cfg, radio.settings(play.server.cfg)
    assert radio_play.idle_due(cfg, s, now=10 ** 9) is False            # no marker: the owner's station
    radio_play.mark(now=1000.0)
    assert radio_play.idle_due(cfg, s, now=1000.0 + 599) is False
    assert radio_play.idle_due(cfg, s, now=1000.0 + 600) is True
    radio_listeners.player_fetch(radio.folder(), now=1500.0)             # someone fetched: the clock restarts
    assert radio_play.idle_due(cfg, s, now=1500.0 + 599) is False
    assert radio_play.idle_due(cfg, s, now=1500.0 + 600) is True


def test_idle_minutes_are_configurable_and_bounded(play):
    cfg = play.server.cfg
    cfg.radio = {"radio_player": True, "play_idle_minutes": 3}
    s = radio.settings(cfg)
    radio_play.mark(now=0.0)
    assert (radio_play.idle_due(cfg, s, now=179), radio_play.idle_due(cfg, s, now=180)) == (False, True)
    assert radio_play.idle_minutes({"play_idle_minutes": 0}) == 1
    assert radio_play.idle_minutes({"play_idle_minutes": 10 ** 6}) == radio_play.IDLE_MAX
    assert radio_play.idle_minutes({"play_idle_minutes": "x"}) == 10


def test_a_live_mirror_keeps_the_station_up(play, monkeypatch):
    radio_play.mark(now=0.0)
    monkeypatch.setattr(radio_play, "mirror_in_use", lambda cfg: True)
    assert radio_play.idle_due(play.server.cfg, radio.settings(play.server.cfg), now=10 ** 6) is False


def test_the_idle_loop_asks_for_one_stop_and_ends_with_the_marker(play):
    cfg, asked, now = play.server.cfg, [], [1000.0]
    radio_play.mark(now=0.0)

    class Quit:
        def __init__(self):
            self.n = 0

        def wait(self, t):
            self.n += 1
            if self.n == 4:
                radio_play.forget()   # the owner took it over
            return self.n > 6

    radio_play.watch(cfg, Quit(), stop=lambda: asked.append(1), clock=lambda: now[0])
    assert asked == [1]   # asked once, not on every look


# --- the real start and stop, with launchd mocked ----------------------------------------------------------------

def ask_for_play(cfg):
    """What a Play request leaves behind for the queued start: a live listening link and the intent note."""
    listen_link.mint()
    (radio.folder() / radio_play.INTENT).write_text(json.dumps({"at": time.time()}))


def group_cfg():
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP,
                  radio={"telethon_python": "/usr/bin/python3", "account_api_command": ["x"],
                         "account_session_command": ["y"], "rtmp_url_command": ["u"], "rtmp_key_command": ["k"]})


def test_a_play_start_is_private_marked_and_never_touches_the_group_or_the_mirror(monkeypatch, no_launchd):
    cfg = group_cfg()
    ask_for_play(cfg)
    monkeypatch.setattr(radio, "call_helper", lambda *a, **k: pytest.fail("group helper ran"))
    monkeypatch.setattr(radio, "_sync_copy", lambda *a: pytest.fail("mirror synced"))
    assert radio.start(cfg, play=True) == "radio_started_private"
    state = json.loads((radio.folder() / "state.json").read_text())
    assert state["group_live"] is False and state["on"] is True and radio_play.started() and radio.wanted()
    assert no_launchd["on"]


def test_a_play_start_needs_no_stream_settings(monkeypatch, no_launchd):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP, radio={"radio_player": True})
    ask_for_play(cfg)
    assert radio.start(cfg, play=True) == "radio_started_private"
    with pytest.raises(radio.RadioError):
        radio.start(cfg)   # the owner's start still needs the group stream's settings while that switch is on


def test_an_owner_start_is_never_marked_and_takes_over_a_play_station(monkeypatch, no_launchd):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP, radio={"radio_player": True})
    monkeypatch.setattr(radio, "group_live_on", lambda c: False)
    assert radio.start(cfg) == "radio_started_private" and not radio_play.started()
    radio_play.mark(now=1.0)
    monkeypatch.setattr(radio, "job_pid", lambda: 77)
    monkeypatch.setattr(radio, "_sync_copy", lambda *a: None)
    assert radio.start(cfg) == "radio_already_on"
    assert not radio_play.started()   # his now: Play never stops it


def test_the_play_stop_stops_only_an_idle_play_station_and_leaves_a_group_stream_recorded(monkeypatch, no_launchd):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP, radio={"radio_player": True})
    monkeypatch.setattr(radio, "call_helper", lambda *a, **k: pytest.fail("group helper ran"))
    monkeypatch.setattr(radio, "_sync_copy", lambda *a: pytest.fail("mirror synced"))
    ask_for_play(cfg)
    radio.start(cfg, play=True)
    state = json.loads((radio.folder() / "state.json").read_text())
    state.update(owned=True, call={"id": 9, "access_hash": 1})   # a stream recorded by an earlier run
    (radio.folder() / "state.json").write_text(json.dumps(state))
    assert radio.stop(cfg, play=True) == "radio_already_on" and radio_play.started() and no_launchd["on"]   # not idle
    radio_play.mark(now=time.time() - 3600)
    assert radio.stop(cfg, play=True) == "radio_stopped"
    after = json.loads((radio.folder() / "state.json").read_text())
    assert after["on"] is False and after["call"] == {"id": 9, "access_hash": 1}   # recorded, not ended here
    assert not radio_play.started() and not radio.wanted() and not no_launchd["on"]


def test_the_play_stop_never_stops_an_owner_started_station(monkeypatch, no_launchd):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP, radio={"radio_player": True})
    monkeypatch.setattr(radio, "group_live_on", lambda c: False)
    radio.start(cfg)
    assert radio.stop(cfg, play=True) == "radio_already_on" and no_launchd["on"]


def test_a_play_station_is_not_resumed_after_a_restart(monkeypatch, no_launchd):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, chat_id=GROUP, radio={"radio_player": True})
    ask_for_play(cfg)
    radio.start(cfg, play=True)
    no_launchd["on"] = False   # the Mac went down: the job is gone, the intent and the marker are still on disk
    monkeypatch.setattr(radio, "_job_loaded", lambda: False)
    assert radio._resume_once(cfg) == "off"
    assert not radio_play.started() and not radio.wanted()


def test_giving_up_forgets_the_marker(monkeypatch, no_launchd):
    radio_play.mark(now=1.0)
    monkeypatch.setattr(radio, "_bootout_self", lambda: None)
    monkeypatch.setattr(radio, "_end_call", lambda *a: True)
    radio.give_up({"group_live": False})
    assert not radio_play.started()


# --- plumbing -----------------------------------------------------------------------------------------------------

def test_the_cli_has_hidden_play_flags():
    parser = cli._parser()
    assert parser.parse_args(["radio", "start", "--play"]).play is True
    assert parser.parse_args(["radio", "stop", "--play"]).play is True
    assert parser.parse_args(["radio", "start"]).play is False


def test_spawn_adds_the_play_flag(monkeypatch):
    seen = []
    monkeypatch.setattr(radio.subprocess, "Popen", lambda argv, **k: seen.append(argv))
    radio.spawn("start", play=True)
    radio.spawn("stop")
    assert seen[0][-3:] == ["radio", "start", "--play"] and seen[1][-2:] == ["radio", "stop"]


# --- the silence and the switch to real audio ----------------------------------------------------------------------

def packets(ts):
    assert len(ts) % 188 == 0
    out = [ts[i:i + 188] for i in range(0, len(ts), 188)]
    assert all(p[0] == 0x47 for p in out)
    return out


def pes_times(ts):
    """(pts, continuity counter) of every audio PES in a segment."""
    found = []
    for p in packets(ts):
        pid = ((p[1] & 0x1F) << 8) | p[2]
        if pid != radio_quiet.AUDIO_PID:
            continue
        body = p[4 + 1 + p[4]:] if p[3] & 0x20 else p[4:]
        assert body[:4] == b"\x00\x00\x01\xc0"
        b = body[9:14]
        pts = ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1
        found.append((pts, p[3] & 15, body[14:]))
    return found


def test_the_silent_segment_is_well_formed_transport_stream():
    ts = radio_quiet.segment(7)
    ps = packets(ts)
    pids = [((p[1] & 0x1F) << 8) | p[2] for p in ps]
    assert pids[:2] == [0, radio_quiet.PMT_PID] and pids.count(radio_quiet.AUDIO_PID) == radio_quiet.FRAMES
    for p in ps[:2]:   # PAT and PMT: the CRC of the section is the MPEG-2 CRC
        start = 4 + 1 + p[4] + 1
        end = start + 3 + (((p[start + 1] & 15) << 8) | p[start + 2])
        assert radio_quiet._crc32(p[start:end]) == 0
    frames = pes_times(ts)
    assert all(f[2] == radio_quiet.silent_frame() for f in frames)
    assert [b - a for (a, _, _), (b, _, _) in zip(frames, frames[1:])] == [radio_quiet.FRAME_TICKS] * (radio_quiet.FRAMES - 1)
    frame = radio_quiet.silent_frame()
    assert frame[0] == 0xFF and frame[1] & 0xF0 == 0xF0 and (((frame[3] & 3) << 11) | (frame[4] << 3) | (frame[5] >> 5)) == len(frame)


def test_consecutive_silent_segments_are_contiguous():
    a, b = pes_times(radio_quiet.segment(7)), pes_times(radio_quiet.segment(8))
    assert b[0][0] - a[-1][0] == radio_quiet.FRAME_TICKS                  # no gap or overlap in the timeline
    assert b[0][1] == (a[-1][1] + 1) % 16                                 # continuity counters carry on
    assert radio_quiet.segment(7) is radio_quiet.segment(7)               # built once, cached


def test_the_silence_decodes_as_stereo_aac():
    import shutil
    import subprocess
    tool = shutil.which("ff" + "mpeg")
    if not tool:
        pytest.skip("no decoder on this machine")
    out = subprocess.run([tool, "-v", "error", "-f", "mpegts", "-i", "pipe:0", "-f", "f32le", "-"], capture_output=True,
                         input=radio_quiet.segment(3) + radio_quiet.segment(4))
    assert out.returncode == 0 and out.stderr == b""
    assert len(out.stdout) == 2 * radio_quiet.FRAMES * 1024 * 2 * 4 and set(out.stdout) == {0}


def test_the_silent_window_slides_with_the_clock():
    a = radio_quiet.playlist(1000.0).decode().splitlines()
    b = radio_quiet.playlist(1004.0).decode().splitlines()
    seq = lambda lines: int(next(l for l in lines if l.startswith("#EXT-X-MEDIA-SEQUENCE:")).split(":")[1])
    assert seq(b) - seq(a) == 2
    assert [l for l in a if l.endswith(".ts")][2:] == [l for l in b if l.endswith(".ts")][:-2]   # the overlap is identical


def real(n, extra=""):
    return ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n" + extra + "#EXT-X-MEDIA-SEQUENCE:%d\n" % n
            + "".join("#EXTINF:2.000000,\nsegment-%d.ts\n" % k for k in range(n, n + 3)))


def test_bridge_puts_a_discontinuity_before_the_first_real_segment_and_keeps_it_while_it_is_listed():
    first, rec = radio_quiet.bridge(real(100), None)
    lines = first.splitlines()
    assert lines.count("#EXT-X-DISCONTINUITY") == 1 and "#EXT-X-DISCONTINUITY-SEQUENCE:0" in lines
    at = lines.index("segment-100.ts")
    assert lines[at - 2:at] == ["#EXT-X-DISCONTINUITY", "#EXTINF:2.000000,"] or lines[at - 2] == "#EXT-X-DISCONTINUITY"
    assert rec == {"first": "segment-100.ts", "mine": True, "offset": 0}
    second, rec2 = radio_quiet.bridge(real(101), rec)                     # window slid by one: still lists segment-100? no
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:1" in second.splitlines() and "#EXT-X-DISCONTINUITY\n" not in second
    held, _ = radio_quiet.bridge(real(100), rec)                          # a player that reloads early sees it again
    assert held.splitlines().count("#EXT-X-DISCONTINUITY") == 1


def test_bridge_does_not_double_a_tag_the_encoder_already_wrote():
    text = real(5).replace("#EXTINF:2.000000,\nsegment-5.ts", "#EXT-X-DISCONTINUITY\n#EXTINF:2.000000,\nsegment-5.ts")
    out, rec = radio_quiet.bridge(text, None)
    assert out.splitlines().count("#EXT-X-DISCONTINUITY") == 1 and rec["mine"] is False
    gone, _ = radio_quiet.bridge(real(6, "#EXT-X-DISCONTINUITY-SEQUENCE:1\n"), rec)   # the encoder counted its own tag
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:1" in gone.splitlines()


def test_the_switch_from_silence_to_real_audio_over_the_listen_link(play):
    path = make(play.server)
    quiet = playlist(play.server, path)[2].decode()                       # Play pressed: silence
    assert "quiet-" in quiet and "#EXT-X-DISCONTINUITY" not in quiet
    root = radio.prepare_hls()
    (root / "live.m3u8").write_text(real(500))
    (root / "segment-500.ts").write_bytes(b"real audio")
    status, _, body = playlist(play.server, path)                         # real audio exists now
    lines = body.decode().splitlines()
    assert status == 200 and "quiet-" not in body.decode() and lines.count("#EXT-X-DISCONTINUITY") == 1
    assert lines.index("#EXT-X-DISCONTINUITY") < lines.index("segment-500.ts") and "#EXT-X-DISCONTINUITY-SEQUENCE:0" in lines
    (root / "live.m3u8").write_text(real(502))
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:1" in playlist(play.server, path)[2].decode().splitlines()
    radio_play.forget()                                                   # the run ended: a new one is a new timeline
    assert "#EXT-X-DISCONTINUITY" not in playlist(play.server, path)[2].decode()


def test_a_playlist_with_no_silence_before_it_is_served_unchanged(play):
    path = make(play.server)
    root = radio.prepare_hls()
    (root / "live.m3u8").write_text(real(900))
    body = playlist(play.server, path)[2].decode()
    assert "#EXT-X-DISCONTINUITY" not in body and "segment-900.ts" in body


# --- review fixes: one monotonic media sequence, a queued start that checks again, an atomic idle stop ------------------

def media_sequence(text):
    return int(next(l for l in text.splitlines() if l.startswith("#EXT-X-MEDIA-SEQUENCE:")).split(":")[1])


def test_the_media_sequence_continues_from_the_silence_into_real_audio(play):
    path = make(play.server)
    quiet = playlist(play.server, path)[2].decode()
    top = media_sequence(quiet) + radio_quiet.WINDOW - 1                   # the last silent number served
    micro = 1_763_300_000_000_000                                          # the radio numbers segments by microseconds
    root = radio.prepare_hls()
    (root / "live.m3u8").write_text(real(micro))
    seen = [media_sequence(playlist(play.server, path)[2].decode())]
    assert seen[0] == top + 1                                              # no jump at the switch
    (root / "live.m3u8").write_text(real(micro + 1))
    seen.append(media_sequence(playlist(play.server, path)[2].decode()))
    (root / "live.m3u8").write_text(real(micro + 2))
    seen.append(media_sequence(playlist(play.server, path)[2].decode()))
    assert seen == [top + 1, top + 2, top + 3]
    body = playlist(play.server, path)[2].decode()
    assert "segment-%d.ts" % (micro + 2) in body                           # the file names are the real ones


def test_bridge_offset_is_fixed_once_per_run():
    first, rec = radio_quiet.bridge(real(10 ** 15), None, top=50)
    later, _ = radio_quiet.bridge(real(10 ** 15 + 4), rec, top=9999)       # a later top must not move the offset
    assert media_sequence(first) == 51 and media_sequence(later) == 55


def test_a_queued_start_is_cancelled_by_a_stop_made_after_the_request(play, monkeypatch, no_launchd):
    cfg = play.server.cfg
    assert radio_play.request(cfg, now=time.time()) == radio_play.STARTING        # Play pressed: start queued
    radio.stop(cfg)                                                               # the owner stops the radio
    assert radio.start(cfg, play=True) == "radio_play_skipped"                    # the queued start finds out
    assert not no_launchd["on"] and not radio_play.started() and not (radio.folder() / radio_play.INTENT).exists()


def test_a_queued_start_is_cancelled_when_the_addon_or_the_link_is_switched_off(play, no_launchd):
    cfg = play.server.cfg
    make(play.server)
    assert radio_play.request(cfg, now=time.time()) == radio_play.STARTING
    addons.set_on(cfg, "listen_link", False)                                      # revokes every listening token too
    assert radio.start(cfg, play=True) == "radio_play_skipped" and not no_launchd["on"]


def test_a_queued_start_that_is_still_wanted_goes_ahead_and_a_stale_one_does_not(play, no_launchd):
    cfg = play.server.cfg
    make(play.server)
    assert radio_play.request(cfg, now=time.time() - radio_play.INTENT_FRESH - 5) == radio_play.STARTING
    assert radio.start(cfg, play=True) == "radio_play_skipped"                    # the note is too old
    radio_play.clear_intent()
    (radio.folder() / radio_play.INTENT).write_text(json.dumps({"at": time.time()}))
    assert radio.start(cfg, play=True) == "radio_started_private" and radio_play.started()
    assert not (radio.folder() / radio_play.INTENT).exists()                      # used up


def test_the_start_keeps_the_note_that_silence_was_served_and_the_end_of_the_run_clears_it(play, no_launchd):
    cfg = play.server.cfg
    make(play.server)
    radio_play.quiet_playlist()                                                   # the first playlist went out
    (radio.folder() / radio_play.INTENT).write_text(json.dumps({"at": time.time()}))
    radio.start(cfg, play=True)                                                   # the queued start runs afterwards
    assert (radio.folder() / radio_play.QUIET).exists()                           # still known: the switch gets bridged
    radio_play.mark(now=time.time() - 3600)
    radio.stop(cfg, play=True)
    assert not (radio.folder() / radio_play.QUIET).exists()


def test_the_idle_decision_is_made_while_holding_the_fetch_lock(play, monkeypatch, no_launchd):
    cfg = play.server.cfg
    radio_play.mark(now=time.time() - 3600)
    seen = []

    def check(c, s, now=None):
        with radio_listeners.presence_lock(radio.folder(), wait=0.2) as held:   # a fetch at this moment would wait
            seen.append(held)
        return True
    monkeypatch.setattr(radio_play, "idle_due", check)
    assert radio.stop(cfg, play=True) == "radio_stopped" and seen == [False]


def test_a_fetch_recorded_before_the_check_keeps_the_station_up(play, no_launchd):
    cfg = play.server.cfg
    radio_play.mark(now=time.time() - 3600)
    radio_listeners.player_fetch(radio.folder())
    assert radio.stop(cfg, play=True) == "radio_already_on" and radio_play.started()


def test_a_fetch_waits_for_a_running_stop_and_is_still_recorded(play):
    import threading
    folder = radio.folder()
    done = []
    with radio_listeners.presence_lock(folder):
        t = threading.Thread(target=lambda: (radio_listeners.player_fetch(folder, now=123.0), done.append(1)))
        t.start()
        time.sleep(0.2)
        assert done == []                                                         # blocked while the decision runs
    t.join(5)
    assert done == [1] and radio_listeners._read(folder / "player-listener.json")["at"] == 123.0


def test_starting_the_mirror_takes_the_station_over(play, no_launchd):
    radio_play.mark(now=time.time() - 3600)
    radio_play.adopt()                                                            # what radio_youtube.set_mirror does
    assert not radio_play.started()
    assert radio.stop(play.server.cfg, play=True) == "radio_already_on"


def test_set_mirror_calls_adopt():
    import inspect
    from ctb import radio_youtube
    assert "radio_play.adopt()" in inspect.getsource(radio_youtube.set_mirror)


def test_owner_takeover_keeps_the_listen_link_bridge(tmp_path, monkeypatch):
    from ctb import radio_play
    monkeypatch.setattr(radio_play, "_folder", lambda: tmp_path)
    for name in (radio_play.STARTED, radio_play.BRIDGE, radio_play.QUIET):
        (tmp_path / name).write_text("{}")
    radio_play.release()
    assert not (tmp_path / radio_play.STARTED).exists()
    assert (tmp_path / radio_play.BRIDGE).exists() and (tmp_path / radio_play.QUIET).exists()


def test_idle_stop_never_stops_without_the_presence_lock(monkeypatch):
    import contextlib
    from ctb import radio, radio_listeners
    @contextlib.contextmanager
    def not_held(directory, wait=0):
        yield False
    monkeypatch.setattr(radio_listeners, "presence_lock", not_held)
    monkeypatch.setattr(radio, "settings", lambda cfg: {})
    monkeypatch.setattr(radio, "_stop_locked", lambda *a, **k: (_ for _ in ()).throw(AssertionError("stopped")))
    assert radio.stop(None, play=True) == "radio_already_on"
