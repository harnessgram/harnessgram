"""The YouTube mirror edition reads the home radio's HLS audio, so while an episode is on the air it must hold:
the episode is never part of what the mirror carries."""
import os
import time

from ctb import radio, radio_youtube as Y
from test_radio_youtube_mirror import isolated_channel, live_home, mirror_cfg  # noqa: F401 (the autouse fixture)


def mark():
    (radio.folder() / radio.PRIVATE_AUDIO).write_text("")


def test_the_mirror_is_ready_only_while_no_episode_is_audible():
    c = mirror_cfg()
    live_home()
    assert Y.mirror_ready(c) is True
    mark()
    assert Y.mirror_ready(c) is False          # an episode is on the air: the mirror holds
    (radio.folder() / radio.PRIVATE_AUDIO).unlink()
    assert Y.mirror_ready(c) is True           # and carries on with the radio's own talk afterwards


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
