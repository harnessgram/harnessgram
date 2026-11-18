"""Private background audio, speech priority and the public copyright boundary, using PCM and fakes."""
import array
from types import SimpleNamespace

import pytest

from ctb import cli, panel_page, panel_settings, radio, radio_filler as F, radio_music as M, radio_podcast as P
from ctb import radio_youtube as Y
from ctb.config import Config, locked_map


def pcm(value, n=radio.FRAME):
    return array.array("h", [value] * n).tobytes()


def samples(data):
    return array.array("h", data)


class Feed:
    def __init__(self, path="", value=10000):
        self.value, self.stopped, self.played = value, False, 0
        self.paused = False

    def read(self, size):
        if self.stopped:
            return None
        self.played += size
        return pcm(self.value, size // 2)

    def stop(self):
        self.stopped = True


def mixers():
    public = radio.SpeechMixer(bytes(4))
    private = radio.PrivateMixer(bytes(4), public)
    private.background_enabled = True
    return private, public


@pytest.mark.parametrize("slot", ["background", "show"])
def test_library_and_episode_cannot_reach_group_or_mirror(slot):
    private, public = mixers()
    setattr(private, slot, Feed())
    # Even accidental bed/show assignments to the public source cannot make them audible.
    public.bed = array.array("h", [12000] * radio.FRAME)
    public.show = Feed(value=9000)
    assert max(samples(private.next())) == 10000
    assert public.next() == bytes(2 * radio.FRAME)
    assert public.show.played == 0
    assert Y.mirror_playlist() == radio.folder() / "public-hls" / "live.m3u8"
    assert Y.mirror_playlist().parent != radio.folder() / "hls"
    private.add(pcm(1000, radio.RATE), tag=42)
    private.next()
    assert public.pending() == 1
    for _ in range(40):
        out = samples(public.next())
        assert set(out) <= {0, 1000}
    assert 1000 in out


@pytest.mark.parametrize("directory", ["hls", "private-hls"])
def test_public_pipeline_refuses_private_mixer_or_private_hls(directory):
    private, _ = mixers()
    with pytest.raises(ValueError, match="private audio"):
        radio.Pipeline("unused", "unused", radio.folder() / "card", "libx264", private, lambda pid: None)
    with pytest.raises(ValueError, match="private HLS"):
        radio.Pipeline("unused", "unused", radio.folder() / "card", "libx264", None, lambda pid: None,
                       hls_input=radio.folder() / directory / "live.m3u8")


def test_mirror_never_falls_back_to_private_audio_or_depends_on_expiring_marker():
    radio.set_wanted(True)
    path = radio.prepare_hls()
    (path / "segment-1.ts").write_bytes(b"private music")
    (path / "live.m3u8").write_text("#EXTM3U\n#EXTINF:2,\nsegment-1.ts\n")
    assert not Y.mirror_ready(Config())
    public = radio.folder() / "public-hls"
    public.mkdir()
    (public / "segment-2.ts").write_bytes(b"station speech")
    (public / "live.m3u8").write_text("#EXTM3U\n#EXTINF:2,\nsegment-2.ts\n")
    (radio.folder() / radio.PRIVATE_AUDIO).touch()
    assert Y.mirror_ready(Config())


def test_private_speech_starts_next_frame_and_ducks_then_restores():
    private, public = mixers()
    private.background = Feed()
    private.add(pcm(2000, radio.RATE), tag=7)
    faded = [samples(private.next()) for _ in range(25)]
    assert private.gain == pytest.approx(10 ** (-18 / 20), abs=1e-7)
    assert private.progress()[0] is None and all(frame[0] <= 10000 for frame in faded)
    out = samples(private.next())
    assert private.progress()[0] == 7
    assert out[0] == pytest.approx(2000 + 10000 * private.DUCK, abs=1)
    assert out[-1] == pytest.approx(2000 + 10000 * private.DUCK, abs=1)
    for _ in range(49):
        private.next()
    assert private.voice is None
    halfway = None
    for step in range(50):
        private.next()
        if step == 24:
            halfway = private.gain
    assert private.DUCK < halfway < 1 and private.gain == pytest.approx(1)
    assert private.background.played > 0 and public.show is None


def test_speech_pauses_a_show_without_advancing_it_then_resumes():
    private, _ = mixers()
    private.show = Feed(value=30000)
    private.add(pcm(30000), tag=4)
    assert max(samples(private.next())) == 30000
    assert private.pending() == 0 and private.show.played == 0
    private.next()
    assert private.show.played == 2 * radio.FRAME


def test_filler_uses_library_only_when_no_speech_and_stops_for_quiet_or_off(monkeypatch):
    M.set_enabled(True)
    private, _ = mixers()
    picks, quiet = [], [False]
    monkeypatch.setattr(M, "pick", lambda now, **_kw: picks.append(now) or {"id": "a" * 16})
    monkeypatch.setattr(M, "approved_for_attach", lambda item, cfg: True)
    filler = F.Filler(Config(), private, SimpleNamespace(cur=None), make=Feed, clock=lambda: 100,
                      quiet=lambda cfg, now: quiet[0])
    private.add(pcm(1))
    assert filler.step() and not picks
    private.next()
    assert filler.step() and picks == [100]
    feed = private.background
    quiet[0] = True
    assert not filler.step() and feed.stopped and not private.background_enabled
    assert private.next() == bytes(2 * radio.FRAME)
    quiet[0] = False
    M.set_enabled(False)
    assert not filler.step()
    filler.close()


def test_track_changes_keep_only_one_decoder_alive_and_recheck_after_construction(monkeypatch):
    M.set_enabled(True)
    private, _ = mixers()
    items = [{"id": "a" * 16}, {"id": "b" * 16}, {"id": "c" * 16}]
    live, peak, events = [0], [0], []

    class CountingFeed(Feed):
        def __init__(self, path="", value=10000):
            super().__init__(path, value)
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            events.append(("make", path))

        def stop(self):
            if not self.stopped:
                live[0] -= 1
            super().stop()

    monkeypatch.setattr(M, "pick", lambda *args, **kwargs: items.pop(0) if items else None)
    approvals = iter((True, True, False))
    monkeypatch.setattr(M, "approved_for_attach",
                        lambda item, cfg: events.append(("check", item["id"])) or next(approvals))
    monkeypatch.setattr(M, "skip_requested", lambda ident: ident in {"a" * 16, "b" * 16})
    now = [100]
    filler = F.Filler(Config(), private, SimpleNamespace(cur=None), make=CountingFeed,
                      clock=lambda: now[0], quiet=lambda *args: False)

    assert filler.step() and live[0] == 1
    now[0] += 5
    assert filler.step() and live[0] == 1
    now[0] += 5
    assert filler.step() and live[0] == 0 and private.background is None
    assert peak == [1]
    assert [kind for kind, _ in events] == ["make", "check", "make", "check", "make", "check"]
    filler.close()


def test_podcast_handoff_closes_music_decoder_before_the_episode_decoder(monkeypatch):
    M.set_enabled(True)
    private, _ = mixers()
    player = SimpleNamespace(cur=None)
    live, peak = [0], [0]

    class CountingFeed(Feed):
        def __init__(self, path="", value=10000):
            super().__init__(path, value)
            live[0] += 1
            peak[0] = max(peak[0], live[0])

        def stop(self):
            if not self.stopped:
                live[0] -= 1
            super().stop()

    monkeypatch.setattr(M, "pick", lambda *args, **kwargs: {"id": "a" * 16})
    monkeypatch.setattr(M, "approved_for_attach", lambda *args: True)
    filler = F.Filler(Config(), private, player, make=CountingFeed, clock=lambda: 100,
                      quiet=lambda *args: False)
    assert filler.step() and live[0] == 1

    player.cur = {"id": "b" * 16}  # Player downloads first and constructs its Feed on a later step.
    assert filler.step() and live[0] == 0 and private.background is None
    episode = CountingFeed("episode.audio")
    assert live[0] == 1 and peak == [1]
    episode.stop()
    filler.close()


def test_quiet_check_failure_is_silent(monkeypatch):
    private, _ = mixers()
    def fail(*args):
        raise ValueError("bad quiet window")
    filler = F.Filler(Config(), private, SimpleNamespace(cur=None), quiet=fail)
    assert not filler.step() and not private.background_enabled


def test_reuses_shabbat_and_configured_quiet_windows():
    from datetime import datetime
    cfg = Config()
    saturday = datetime.fromisoformat("2026-11-14T12:00:00+02:00").timestamp()
    friday = datetime.fromisoformat("2026-11-13T23:00:00+02:00").timestamp()
    monday = datetime.fromisoformat("2026-11-16T12:00:00+02:00").timestamp()
    assert F.briefing.rest_day(cfg, saturday) and F.briefing.rest_day(cfg, friday)
    assert not F.briefing.rest_day(cfg, monday)


def test_feed_cli_and_music_switch_share_cockpit_setting(monkeypatch, capsys):
    monkeypatch.setattr(cli.C, "load", lambda: Config())
    assert not M.enabled()  # add-ons are opt-in
    M.set_enabled(True)
    assert cli.main(["radio", "music", "off"]) == 0 and not M.enabled()
    assert not panel_settings.SETTINGS["radio_music"][0]()
    panel_settings.SETTINGS["radio_music"][1](True)
    assert M.enabled()
    assert cli.main(["radio", "music", "on"]) == 0
    url = "https://feeds.example.org/program.xml"
    assert cli.main(["radio", "podcast", "add", url]) == 0
    assert P.feeds(Config()) == [{"name": "feeds.example.org", "url": url}]
    assert cli.main(["radio", "podcast", "list"]) == 0
    assert cli.main(["radio", "podcast", "remove", url]) == 0 and not P.feeds(Config())
    assert cli.main(["radio", "podcast", "add", "http://feeds.example.org/feed"]) == 1
    assert "set_radio_music" in panel_page.STRINGS["he"]


def test_now_playing_like_skip_and_phone_safe_card_controls():
    M.set_enabled(True)
    item = {"id": "a" * 16, "title": "שיר ארוך", "artist": "אמנית"}
    M.playback(item)
    assert M.playback() == item
    source = open(panel_page.__file__, encoding="utf-8").read()
    assert 'post("/api/radio-music",{action:a[1],id:cur.id})' in source
    assert 'role","switch"' in source and ".music-now{min-inline-size:0" in source
    M.playback(False)
    assert M.playback() == {}


def test_skip_request_votes_and_only_stops_the_matching_private_track(monkeypatch):
    monkeypatch.setattr(M, "_probe", lambda path: {})
    item = M.add(b"audio", "Song", "Artist", now=1)
    M.playback(item)
    M.request_skip(item["id"])
    assert M.skip_requested("b" * 16) is False
    assert M.skip_requested(item["id"]) is True
    assert M.listing()[0]["skips"] == 1


def test_skip_request_is_cleared_when_the_track_changes_or_ends(monkeypatch):
    monkeypatch.setattr(M, "skip", lambda ident: None)
    first, second = ({"id": "a" * 16}, {"id": "b" * 16})
    M.playback(first)
    M.request_skip(first["id"])
    M.playback(second)
    assert not M.skip_requested(second["id"])
    with pytest.raises(ValueError):
        M.request_skip(first["id"])
    M.request_skip(second["id"])
    M.playback(False)
    assert not M.skip_requested(second["id"])


def test_feed_selection_is_newest_unplayed_and_suitability_fails_closed(monkeypatch):
    url = "https://feeds.example.org/program.xml"
    P.set_feed(Config(), url, True)
    eps = [{"id": letter * 16, "published": stamp, "raw_title": title, "description": "",
            "audio_url": "https://cdn.example.org/episode.mp3"}
           for letter, stamp, title in [("a", 1, "Older"), ("b", 2, "Played"), ("c", 3, "Forbidden")]]
    with locked_map(P.folder() / "played.json", keep=100) as played:
        played["b" * 16] = 10
    monkeypatch.setattr(P, "episodes", lambda cfg, feed: eps)
    checked = []
    def gate(cfg, title, *args):
        checked.append(title)
        if title == "Forbidden":
            raise P.PodcastError("unsuitable")
    monkeypatch.setattr(P, "gate", gate)
    private, _ = mixers()
    filler = F.Filler(Config(), private, SimpleNamespace(cur=None), quiet=lambda *args: False)
    filler._select()
    assert filler.selection["id"] == "a" * 16 and checked == ["Forbidden", "Older"]
    monkeypatch.setattr(P, "gate", lambda *a: (_ for _ in ()).throw(P.PodcastError("rules")))
    filler.selection = None
    filler._select()
    assert filler.selection is None


def test_configured_feed_can_be_removed_without_mutating_config():
    url = "https://feeds.example.org/program.xml"
    cfg = Config(radio_podcasts=[{"name": "Program", "url": url}])
    assert len(P.feeds(cfg)) == 1
    assert P.set_feed(cfg, url, False) == []
    assert cfg.radio_podcasts == [{"name": "Program", "url": url}]


def test_rss_does_not_accept_a_video_enclosure_with_audio_extension():
    body = b'<rss><channel><item><title>Program</title><enclosure url="https://cdn.example.org/a.mp3" type="video/mp4"/></item></channel></rss>'
    assert P.parse_feed(body) == []


def test_closing_public_pipeline_preserves_hls_for_running_readers():
    for name in ("private-hls", "public-hls"):
        path = radio.folder() / name
        path.mkdir(exist_ok=True)
        (path / "segment-1.ts").write_bytes(b"audio")
    playlist = radio.folder() / "public-hls" / "live.m3u8"
    playlist.write_text("#EXTM3U\n#EXTINF:2,\nsegment-1.ts\n")
    pipe = radio.Pipeline("", "", radio.folder() / "card", "", radio.SpeechMixer(bytes(4)), lambda pid: None,
                          audio_only=True, public_audio=True)
    pipe.close()
    assert (radio.folder() / "private-hls" / "segment-1.ts").is_file()
    assert (radio.folder() / "public-hls" / "segment-1.ts").read_bytes() == b"audio"
    # Kept for a reader that has it open, but retired: freshness checks never replay aired audio.
    assert playlist.is_file() and playlist.stat().st_mtime == 0
