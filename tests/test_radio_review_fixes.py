"""Upgrade isolation, cutoff latency, caption clocks and optional encoding; PCM and fakes only."""
import array
import threading
from types import SimpleNamespace

import pytest

from ctb import panel, radio, radio_filler as F, radio_music as M, radio_podcast as P, radio_suitability
from ctb.config import Config


def pcm(value, frames=1):
    return array.array("h", [value] * (radio.FRAME * frames)).tobytes()


class Feed:
    paused = False

    def read(self, size):
        return b"\x10\x00" * (size // 2)


def test_private_player_reads_only_the_new_directory():
    legacy = radio.folder() / "hls"
    legacy.mkdir()
    for root, data in ((legacy, b"old speech"), (radio.prepare_hls(), b"private music")):
        (root / "live.m3u8").write_text("#EXTM3U\nsegment-1.ts\n")
        (root / "segment-1.ts").write_bytes(data)
    assert panel.radio_audio("segment-1.ts") == b"private music"
    radio.clear_hls()
    with pytest.raises(OSError):
        panel.radio_audio("segment-1.ts")  # never fall back to the legacy mirror input


@pytest.mark.parametrize("slot", ["background", "show"])
@pytest.mark.parametrize("cutoff", ["quiet", "off", "error"])
def test_writer_mutes_the_bed_within_one_chunk_without_a_controller_step(monkeypatch, slot, cutoff):
    M.set_enabled(True)
    quiet = [False]

    def quiet_now(*args):
        if quiet[0] and cutoff == "error":
            raise ValueError("unreadable quiet windows")
        return quiet[0]

    private = radio.PrivateMixer(bytes(4), radio.SpeechMixer(bytes(4)))
    filler = F.Filler(Config(), private, SimpleNamespace(cur=True), quiet=quiet_now)
    assert filler.step()
    setattr(private, slot, Feed())
    pipe = radio.Pipeline("", "", radio.folder() / "card", "", private, lambda pid: None, audio_only=True)
    written = []

    class Input:
        def write(self, data):
            written.append(data)
            if len(written) == 1:
                if cutoff == "off":
                    M.set_enabled(False)
                else:
                    quiet[0] = True
            else:
                raise OSError("fake writer finished")

    pipe.proc = SimpleNamespace(stdin=Input())
    pipe._writer(threading.Event())
    assert len(written) == 2 and any(written[0]) and not any(written[1])
    assert private.background_enabled  # the controller has not run again
    filler.close()


def test_slow_episode_check_never_blocks_control_or_quiet_muting(monkeypatch):
    M.set_enabled(True)
    private = radio.PrivateMixer(bytes(4), radio.SpeechMixer(bytes(4)))
    player = P.Player(private, private.lock, cfg=Config(), make=lambda path: pytest.fail("no decoder"))
    quiet = [False]
    filler = F.Filler(Config(), private, player, quiet=lambda *args: quiet[0])
    assert filler.step()
    private.background = Feed()
    P._queue_episode({"id": "a" * 16, "raw_title": "Program", "desc": "Birds",
                      "url": "https://cdn.example.org/episode.mp3"})
    entered, release = threading.Event(), threading.Event()
    control_thread = threading.current_thread()

    def slow_gate(*args):
        assert threading.current_thread() is not control_thread
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(P, "gate", slow_gate)
    try:
        player.step()
        download = player.dl
        assert entered.wait(2) and not download.done.is_set()
        assert any(private.next())
        quiet[0] = True
        assert not any(private.next())
        assert not filler.step()  # control remains responsive while suitability is still waiting
        player.stop_all()
    finally:
        release.set()
        if player.dl is not None:
            player.stop_all()
        assert download.done.wait(2)
        filler.close()


def test_cutoff_crossing_during_read_drops_the_bed_and_preserves_station_speech():
    M.set_enabled(True)
    quiet = [False]
    private = radio.PrivateMixer(bytes(4), radio.SpeechMixer(bytes(4)))
    filler = F.Filler(Config(), private, SimpleNamespace(cur=True), quiet=lambda *args: quiet[0])
    assert filler.step()

    class CrossingFeed:
        def read(self, size):
            quiet[0] = True
            return b"\x10\x00" * (size // 2)

    private.background = CrossingFeed()
    private.add(pcm(1000, 2), tag=1)
    assert private.next() == bytes(2 * radio.FRAME)
    for _ in range(24):
        assert private.next() == bytes(2 * radio.FRAME)
    assert private.next() == pcm(1000)
    filler.close()


def test_feed_worker_prepares_the_queue_verdict_off_the_controller(monkeypatch):
    M.set_enabled(True)
    ep = {"id": "a" * 16, "raw_title": "Program", "description": "Birds",
          "audio_url": "https://cdn.example.org/episode.mp3"}
    monkeypatch.setattr(P, "feeds", lambda cfg: [{}])
    monkeypatch.setattr(P, "episodes", lambda *args: [ep])
    checked = []
    control_thread = threading.current_thread()

    def gate(*args):
        assert threading.current_thread() is not control_thread
        checked.append(True)

    monkeypatch.setattr(P, "gate", gate)
    private = radio.PrivateMixer(bytes(4), radio.SpeechMixer(bytes(4)))
    filler = F.Filler(Config(), private, SimpleNamespace(cur=True), quiet=lambda *args: False)
    assert filler.step()
    assert filler.done.wait(2)
    filler.worker.join(2)
    assert filler.step()
    assert checked == [True] and P._read()["items"][0]["id"] == ep["id"]
    filler.close()


def test_checker_verdict_is_cached_for_exact_text_and_checker_revision(monkeypatch, tmp_path):
    checker = tmp_path / "checker.py"
    checker.write_text("# fake revision one\n")
    monkeypatch.setattr(radio_suitability, "SHARED_CHECKER", checker)
    checked = []
    monkeypatch.setattr(P, "_checker_says_ok", lambda path, text: checked.append(text) or True)
    P.gate(Config(), "Program", "Birds")
    P.gate(Config(), "Program", "Birds")
    assert len(checked) == 1
    checker.write_text("# fake revision two, changed\n")
    P.gate(Config(), "Program", "Birds")
    assert len(checked) == 2
    P.gate(Config(), "Another program", "Birds")
    assert len(checked) == 3


def test_rules_changed_after_worker_completion_are_rechecked_before_attaching(monkeypatch, tmp_path):
    checker = tmp_path / "checker.py"
    checker.write_text("allow")
    monkeypatch.setattr(radio_suitability, "SHARED_CHECKER", checker)
    control_thread = threading.current_thread()

    def gate(*args):
        assert threading.current_thread() is not control_thread
        if checker.read_text() == "deny":
            raise P.PodcastError("unsuitable")

    monkeypatch.setattr(P, "gate", gate)
    private = radio.PrivateMixer(bytes(4), radio.SpeechMixer(bytes(4)))
    player = P.Player(private, private.lock, cfg=Config(),
                      get=lambda url, dest, cancel: dest.write_bytes(b"fake audio"),
                      make=lambda path: pytest.fail("a stale verdict must never reach the decoder"))
    P._queue_episode({"id": "a" * 16, "raw_title": "Program", "desc": "Birds",
                      "url": "https://cdn.example.org/episode.mp3"})
    try:
        player.step()
        assert player.dl.done.wait(2)
        checker.write_text("deny")
        player.step()  # the completed verdict is stale: start a new background check
        assert player.dl is not None and player.dl.done.wait(2)
        player.step()
        assert private.show is None and not P._read()["items"]
        assert P._read()["status"]["error"] == "unsuitable"
    finally:
        player.stop_all()


def test_group_captions_follow_public_fades_and_gaps():
    public = radio.SpeechMixer(bytes(4))
    private = radio.PrivateMixer(bytes(4), public)
    private.add(pcm(1000, 8), tag=1)
    private.add(pcm(2000, 8), tag=2)
    station = object.__new__(radio.Station)
    station.mixer, station.group_mixer, station.s = private, public, {"group_live": True, "lang": "en"}
    station.snap, station.caption_lock, station.talks = {}, threading.Lock(), {}
    station.captions = {1: (0, len(pcm(1000, 8)), ["First."]), 2: (0, len(pcm(2000, 8)), ["Second."])}
    drawn = []
    station._safe_draw = lambda top: drawn.append((top, public.progress()[0], private.progress()[0]))

    class Quit:
        ticks = 0

        def wait(self, _):
            self.ticks += 1
            if self.ticks > 100:
                return True
            private.next()
            public.next()
            return False

    station.quit = Quit()
    station.caption_loop()
    captions = [(top, tag, private_tag) for top, tag, private_tag in drawn if top is not None]
    assert [tag for top, tag, private_tag in captions] == [1, 2]
    assert all(("First." if tag == 1 else "Second.") in "".join(top["lines"]) for top, tag, _ in captions)
    assert any(tag != private_tag for _, tag, private_tag in captions)
    assert any(top is None and tag is None for top, tag, _ in drawn)  # public inter-clip gap clears captions


@pytest.mark.parametrize("consumer", [False, True])
def test_private_mode_encodes_public_audio_only_for_an_enabled_consumer(monkeypatch, tmp_path, consumer):
    station = object.__new__(radio.Station)
    station.cfg, station.dir, station.card = Config(), tmp_path, tmp_path / "card"
    station.group_mixer = radio.SpeechMixer(bytes(4))
    station.group_mixer.add(pcm(1000), tag=1)
    station.quit = threading.Event()
    monkeypatch.setattr(radio.radio_play, "mirror_in_use", lambda cfg: consumer)
    monkeypatch.setattr(station.quit, "wait", lambda seconds: station.quit.set() or True)
    pipelines = []

    class Pipeline:
        def __init__(self, *args, **kwargs):
            pipelines.append((args[4], kwargs))

        def run(self):
            station.quit.set()
            return ""

    monkeypatch.setattr(radio, "Pipeline", Pipeline)
    station._start_audio(public=True)
    station.audio_thread.join(2)
    assert not station.audio_thread.is_alive()
    assert len(pipelines) == int(consumer)
    if consumer:
        assert pipelines[0][0] is station.group_mixer and pipelines[0][1]["public_audio"]
    else:
        assert not station.group_mixer.pending()  # old private-only speech must not replay when the mirror starts


def test_public_encoder_follows_a_consumer_enabled_after_private_start(monkeypatch, tmp_path):
    station = object.__new__(radio.Station)
    station.cfg, station.dir, station.card = Config(), tmp_path, tmp_path / "card"
    station.group_mixer = radio.SpeechMixer(bytes(4))
    station.quit = threading.Event()
    consumer, pipelines = [False], []
    monkeypatch.setattr(radio.radio_play, "mirror_in_use", lambda cfg: consumer[0])

    def wait(seconds):
        if station.quit.is_set():
            return True
        assert not pipelines  # no public pipeline was started before the consumer was enabled
        consumer[0] = True
        return False

    class Pipeline:
        def __init__(self, *args, **kwargs):
            pipelines.append(kwargs)

        def run(self):
            assert pipelines[0]["keep"]()
            consumer[0] = False
            assert not pipelines[0]["keep"]()
            station.quit.set()
            return radio.MODE_CHANGED

    monkeypatch.setattr(station.quit, "wait", wait)
    monkeypatch.setattr(radio, "Pipeline", Pipeline)
    station._start_audio(public=True)
    station.audio_thread.join(2)
    assert not station.audio_thread.is_alive() and len(pipelines) == 1
