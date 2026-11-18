"""The mirror reads speech-only public HLS; private episodes and music must never reach that source."""
import array
import io
import os
import time

import pytest

from ctb import radio, radio_youtube as Y
from test_radio_youtube_mirror import isolated_channel, live_home, mirror_cfg  # noqa: F401 (the autouse fixture)


def mark():
    (radio.folder() / radio.PRIVATE_AUDIO).write_text("")


@pytest.mark.parametrize("slot", ["show", "background"])
def test_audible_private_audio_never_reaches_the_mirrors_public_source(monkeypatch, slot):
    from ctb import radio_music
    radio_music.set_enabled(True)
    c = mirror_cfg()
    monkeypatch.setattr(radio, "make_bed", lambda *args: bytes(4))
    station = radio.Station(c)  # construct only: no jobs, encoders or streams
    private, public = station.mixer, station.group_mixer
    assert station.player.mixer is private and private.public is public
    path = live_home()
    assert Y.mirror_playlist() == path == radio.folder() / "public-hls" / "live.m3u8"
    assert Y.mirror_ready(c) is True

    audio = array.array("h", [10000] * radio.FRAME).tobytes()
    setattr(private, slot, io.BytesIO(audio * 100))
    private.background_enabled = True
    mark()
    assert private.next() == audio             # the episode/music really is audible privately
    assert public.next() == bytes(len(audio))  # the mirror's source contains none of it
    assert Y.mirror_ready(c) is True            # speech-only HLS can continue during private playback

    speech = array.array("h", [2000] * radio.FRAME).tobytes()
    private.add(speech)
    heard_speech = False
    for _ in range(100):
        frame = public.next()
        assert set(array.array("h", frame)) <= {0, 2000}
        heard_speech |= frame == speech
    assert heard_speech                         # isolation preserves station speech
    (radio.folder() / radio.PRIVATE_AUDIO).unlink()
    assert Y.mirror_ready(c) is True

    # A healthy private playlist must never substitute for a missing public source, even without a marker.
    private_hls = radio.prepare_hls()
    (private_hls / "segment-123.ts").write_bytes(audio)
    (private_hls / "live.m3u8").write_text(path.read_text())
    path.unlink()
    assert Y.mirror_ready(c) is False


def test_a_marker_left_by_a_dead_radio_job_does_not_hold_the_mirror_for_ever():
    c = mirror_cfg()
    live_home()
    mark()
    old = time.time() - radio.PRIVATE_AUDIO_FRESH - 5
    os.utime(radio.folder() / radio.PRIVATE_AUDIO, (old, old))
    assert Y.mirror_ready(c) is True


def test_a_running_mirror_ends_its_broadcast_when_an_episode_starts_and_does_not_time_out_while_holding():
    import inspect
    run = inspect.getsource(Y.MirrorStation._run)
    assert "not mirror_ready(self.cfg)" in run and "self.end_current(svc)" in run
    waiting = run[run.index("private_audio_on"):]
    assert "waiting_since = None" in waiting.split("elif self.allowed()")[0]   # holding is not a missing feed
    # the pipeline of a running mirror stops as soon as the guard says no
    assert "mirror_ready(self.cfg)" in run[run.index("keep=lambda"):run.index("hls_input=")]
