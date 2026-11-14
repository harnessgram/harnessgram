"""Mirror approval, the local live input and lifecycle, without a network or real encoders/jobs."""
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from ctb import cli, policy, radio, radio_board, radio_youtube as Y
from test_radio_youtube import FakeGoogle, Store, STREAM_KEY, REFRESH_TOKEN, cfg, service


def mirror_cfg(**kw):
    return cfg(edition="mirror", **kw)


@pytest.fixture(autouse=True)
def isolated_channel(monkeypatch):
    with Y.connection_lock():
        Y._set_connection(connected=True, channel_id="UCsample", channel_title="Sample channel", live_enabled=True)
    monkeypatch.setattr(Y, "_job_on", lambda: False)
    monkeypatch.setattr(Y, "_load_job", lambda s: None)
    monkeypatch.setattr(Y, "_unload_job", lambda: None)
    monkeypatch.setattr(Y, "_kill_leftover_encoder", lambda: None)
    monkeypatch.setattr(Y, "_launchctl", lambda *a: pytest.fail("no real launchd"))
    monkeypatch.setattr(Y, "_spawn_home_sync", lambda argv: None)


def live_home():
    radio.set_wanted(True)
    path = radio.folder() / "hls"
    path.mkdir(exist_ok=True)
    (path / "segment-123.ts").write_bytes(b"sample audio")
    (path / "live.m3u8").write_text("#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:123\n#EXTINF:2,\nsegment-123.ts\n")
    return path / "live.m3u8"


def station(c=None, g=None):
    c, g = c or mirror_cfg(), g or FakeGoogle()
    return Y.MirrorStation(c, service(c, g), reload=lambda: Y.settings(c)), g


@pytest.mark.parametrize("value,expected", [(None, "staff"), ("staff", "staff"), ("mirror", "mirror"),
                                            ("other", "staff"), (True, "staff"), ([], "staff")])
def test_edition_is_validated(value, expected):
    assert Y.settings(cfg(edition=value))["edition"] == expected


def test_default_edition_is_staff():
    assert Y.settings(cfg())["edition"] == "staff"


def test_only_explicit_mirror_on_arms_the_job(monkeypatch):
    loaded = []
    monkeypatch.setattr(Y, "_load_job", loaded.append)
    live_home()
    c = mirror_cfg()
    Y.home_changed(c, True)
    assert not loaded and not Y.mirror_started()
    st, g = station(c)
    assert not st.ensure_broadcast(st.service) and not g.calls
    assert Y.set_mirror(c, True) == "youtube_mirror_on"
    assert Y.mirror_started() and len(loaded) == 1 and not g.calls
    rec = Y._read(Y.folder() / Y.MIRROR_START)
    assert rec["approval"] == Y.MIRROR_APPROVAL and rec["channel_digest"] == Y._channel_digest("UCsample")
    Y.home_changed(c, True)
    Y.sync_home(c)
    assert len(loaded) == 2  # mock says no job loaded; still no automatic standing-start writer
    assert Y._read(Y.folder() / Y.MIRROR_START) == rec


def test_mirror_off_revokes_even_if_cleanup_fails(monkeypatch):
    c = mirror_cfg()
    Y.set_mirror(c, True)
    monkeypatch.setattr(Y, "_stop", lambda *a: (_ for _ in ()).throw(OSError("cleanup failed")))
    with pytest.raises(OSError):
        Y.set_mirror(c, False)
    assert not Y.mirror_started()


def test_channel_change_revokes_standing_start():
    Y.set_mirror(mirror_cfg(), True)
    Y.bump_generation()
    assert not Y.mirror_started()


def test_stop_and_start_home_follow_only_the_armed_mirror(monkeypatch):
    c, seen = mirror_cfg(), []
    monkeypatch.setattr(Y, "_load_job", lambda s: seen.append("load"))
    monkeypatch.setattr(Y, "_stop", lambda *a: seen.append("stop"))
    Y.home_changed(c, True)
    assert not seen
    Y.set_mirror(c, True)  # home is off: arm only
    assert not seen
    Y.home_changed(c, True)
    Y.sync_home(c)
    Y.home_changed(c, False)
    Y.sync_home(c)
    assert seen == ["load", "stop"] and Y.mirror_started()
    Y.set_mirror(c, False)
    Y.home_changed(c, True)
    Y.sync_home(c)
    assert seen == ["load", "stop", "stop"]


def test_staff_home_lifecycle_does_nothing(monkeypatch):
    monkeypatch.setattr(Y, "_load_job", lambda s: pytest.fail("staff must not follow home"))
    monkeypatch.setattr(Y, "_stop", lambda *a: pytest.fail("staff must not follow home"))
    Y.home_changed(cfg(), True)
    Y.home_changed(cfg(), False)


def test_home_lifecycle_hooks_and_failure_isolation(monkeypatch, tmp_path):
    c, seen = cfg(), []
    monkeypatch.setattr(radio, "job_pid", lambda: None)
    monkeypatch.setattr(radio, "plist_path", lambda: tmp_path / "home.plist")
    monkeypatch.setattr(radio, "_job_loaded", lambda: True)
    monkeypatch.setattr(radio, "_launchctl", lambda *a: SimpleNamespace(returncode=0))
    monkeypatch.setattr(radio, "call_helper", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(radio, "_unload", lambda: None)
    monkeypatch.setattr(radio, "_kill_leftover_encoder", lambda: None)
    monkeypatch.setattr(Y, "home_changed", lambda c, on: seen.append(on))
    radio._start_locked(c, radio.settings(c))
    assert seen == [True]
    radio.stop(c)
    assert seen == [True, False]
    monkeypatch.setattr(Y, "home_changed", lambda *a: (_ for _ in ()).throw(RuntimeError("YouTube failed")))
    assert radio._start_locked(c, radio.settings(c)) == "radio_started"
    assert radio.stop(c) == "radio_stopped"


def test_home_stop_does_not_wait_for_blocked_youtube_cleanup(monkeypatch):
    c, ended, spawned = mirror_cfg(), [], []
    monkeypatch.setattr(radio, "_unload", lambda: None)
    monkeypatch.setattr(radio, "_kill_leftover_encoder", lambda: None)
    monkeypatch.setattr(radio, "clear_hls", lambda: None)
    monkeypatch.setattr(radio, "_end_call", lambda s: ended.append(True) or True)
    monkeypatch.setattr(Y, "_spawn_home_sync", lambda argv: spawned.append((argv,)))
    monkeypatch.setattr(Y, "end_broadcast", lambda *a: threading.Event().wait(30))
    started = time.monotonic()
    assert radio.stop(c) == "radio_stopped"
    assert time.monotonic() - started < 1 and ended == [True] and spawned
    assert Y._read(Y.folder() / Y.HOME_INTENT)["on"] is False


def test_new_mirror_files_and_encoder_argv_contain_no_connection_secrets():
    c = mirror_cfg()
    rid = Y.put_start_request(Y.generation(), "UCsample")
    Y.set_mirror(c, True, Y.take_start_request(rid))
    args = radio.encoder_args("libx264", hls_input=Y.mirror_playlist(), output_fps=25,
                              bitrate=Y.MIRROR_BITRATE)
    forbidden = ("UCsample", "owner@example.test", "ingest.example", STREAM_KEY)
    files = [p for p in Y.folder().rglob("*") if p.is_file() and p.name != "connection.json"]
    text = "\n".join(p.read_text(errors="replace") for p in files) + "\n" + " ".join(args)
    assert not any(value in text for value in forbidden)
    assert (Y.folder() / Y.CHANNEL_KEY).stat().st_mode & 0o777 == 0o600


def test_old_station_cleanup_cannot_end_or_clear_replacement_broadcast():
    c, g = mirror_cfg(), FakeGoogle()
    old, replacement = Y.MirrorStation(c, service(c, g)), ("bc-new", "nonce-new")
    old.broadcast_id, old.start_nonce = "bc-old", "nonce-old"
    Y._set_state(on=True, broadcast_id=replacement[0], owner_start=replacement[1])
    old.end_current(old.service)
    state = Y._state()
    assert (state["broadcast_id"], state["owner_start"]) == replacement
    assert g.did("DELETE", "/liveBroadcasts", "bc-old")
    assert not g.did("DELETE", "/liveBroadcasts", "bc-new")


def test_policy_off_wins_over_standing_start(monkeypatch):
    c = mirror_cfg()
    live_home()
    Y.set_mirror(c, True)
    monkeypatch.setattr(policy, "on", lambda: False)
    monkeypatch.setattr(Y, "_load_job", lambda s: pytest.fail("no job while bridge off"))
    Y.home_changed(c, True)
    Y.sync_home(c)
    st, g = station(c)
    assert not st.allowed() and not st.ensure_broadcast(st.service) and not g.calls
    with pytest.raises(Y.YouTubeError, match="mirror_off"):
        Y.set_mirror(c, True)
    assert Y.set_mirror(c, False) == "youtube_mirror_off"


@pytest.mark.parametrize("condition", ["absent", "stale", "ended", "player_off", "home_off", "missing_segment"])
def test_no_broadcast_without_live_home_audio(condition):
    c = mirror_cfg()
    path = live_home()
    if condition == "absent":
        path.unlink()
    elif condition == "stale":
        os.utime(path, (time.time() - 60,) * 2)
    elif condition == "ended":
        path.write_text(path.read_text() + "#EXT-X-ENDLIST\n")
    elif condition == "player_off":
        c.radio["radio_player"] = False
    elif condition == "home_off":
        radio.set_wanted(False)
    else:
        (path.parent / "segment-123.ts").unlink()
    Y.set_mirror(c, True)
    st, g = station(c)
    assert not st.ensure_broadcast(st.service) and not g.calls
    assert not Y._state().get("broadcast_id")


def test_mirror_creates_unlisted_broadcast_only_with_standing_start_and_hls():
    c = mirror_cfg()
    live_home()
    Y.set_mirror(c, True)
    st, g = station(c)
    assert st.ensure_broadcast(st.service)
    assert g.did("POST", "/liveBroadcasts")[0][3]["status"]["privacyStatus"] == "unlisted"
    assert Y._state()["edition"] == "mirror" and Y._state()["owner_start"]
    assert st.ensure_broadcast(st.service)
    assert len(g.did("POST", "/liveBroadcasts")) == 1


def test_normal_start_does_not_grant_mirror_permission():
    c, g = mirror_cfg(), FakeGoogle()
    live_home()
    with pytest.raises(Y.YouTubeError, match="youtube_mirror_not_armed"):
        Y.start(c, service(c, g), load_job=lambda s: None)
    assert not g.calls and not Y.mirror_started()


def test_armed_normal_start_waits_without_a_broadcast():
    c, g = mirror_cfg(), FakeGoogle()
    Y.set_mirror(c, True)
    assert Y.start(c, service(c, g), load_job=lambda s: None) == "youtube_mirror_on"
    assert not g.calls


def test_bridge_switched_off_during_setup_creates_no_broadcast(monkeypatch):
    c = mirror_cfg()
    live_home()
    Y.set_mirror(c, True)
    class Google(FakeGoogle):
        def __call__(self, *a, **kw):
            out = super().__call__(*a, **kw)
            if self.did("POST", "/liveStreams"):
                monkeypatch.setattr(policy, "on", lambda: False)
            return out
    st, g = station(c, Google())
    assert not st.ensure_broadcast(st.service)
    assert not g.did("POST", "/liveBroadcasts")


def test_mirror_pipeline_reads_hls_and_full_board_never_staff(monkeypatch):
    c = mirror_cfg()
    live_home()
    Y.set_mirror(c, True)
    st, g = station(c)
    show = SimpleNamespace(fps=10, codec="mjpeg", failed="")
    monkeypatch.setattr(st, "start_board", lambda: setattr(st, "show", show))
    for name in ("build_script", "render_inputs", "staff_view"):
        monkeypatch.setattr(Y.radio_staff, name, lambda *a, **k: pytest.fail("no staff content in mirror"))
    monkeypatch.setattr(Y.radio_voice, "system_voice", lambda *a: pytest.fail("no extra voice in mirror"))
    monkeypatch.setattr(radio, "make_bed", lambda *a: pytest.fail("no extra audio in mirror"))
    monkeypatch.setattr(radio, "pick_encoder", lambda *a: "libx264")
    captured = {}
    class Pipeline:
        def __init__(self, *a, **kw):
            captured.update(args=a, kw=kw)
        def run(self):
            st.quit.set()
            return ""
        def close(self):
            pass
    monkeypatch.setattr(radio, "Pipeline", Pipeline)
    assert st._run() == 0
    assert captured["args"][4] is None  # no mixer, synthesis or staff audio
    assert captured["kw"]["source"] is show
    assert captured["kw"]["hls_input"] == Y.mirror_playlist()
    assert captured["kw"]["output_fps"] == 25
    assert not Y._state()["broadcast_id"] and radio.wanted()


def test_mirror_board_uses_home_collector(monkeypatch):
    st, _ = station()
    seen = []
    st.show = SimpleNamespace(update=lambda projects: seen.append(projects))
    monkeypatch.setattr(radio_board, "gather", lambda *a: {"home": True})
    monkeypatch.setattr(radio_board, "collect", lambda source, *a, **k: [source])
    st.feed_board()
    assert seen == [[{"home": True}]]


@pytest.mark.parametrize("encoder", ["libx264", "h264_videotoolbox"])
def test_encoder_args_are_live_hls_and_motion_with_youtube_rate_control(encoder):
    args = radio.encoder_args(encoder, 7, Y.MARKER, fps=10, codec="mjpeg", strip_fd=8,
                              strip=(1280, 650, 6), hls_input=Y.mirror_playlist(),
                              output_fps=Y.MIRROR_FPS, bitrate=Y.MIRROR_BITRATE)
    inputs = [args[i + 1] for i, a in enumerate(args) if a == "-i"]
    assert inputs == [str(Y.mirror_playlist()), "pipe:7", "pipe:8"]
    assert "-re" in args and "s16le" not in args
    assert "fps=25" in args[args.index("-filter_complex") + 1]
    assert args[args.index("-g") + 1] == "50"
    assert args[args.index("-maxrate") + 1] == "1800k"
    assert args[args.index("-bufsize") + 1] == "3600k"
    assert args[-1] == "pipe:1"
    assert not any(secret in " ".join(args) for secret in (STREAM_KEY, REFRESH_TOKEN, "rtmp"))


def test_mirror_secrets_never_reach_encoder_or_logs(monkeypatch):
    seen = {}
    class Pub:
        def __init__(self, url, key):
            seen["publisher"] = (url, key)
        def connect(self):
            pass
        def close(self):
            pass
    def popen(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env")
        raise OSError(STREAM_KEY)  # deliberately includes a secret; only the exception type may escape
    monkeypatch.setattr("ctb.radio_rtmp.Publisher", Pub)
    monkeypatch.setattr(radio.subprocess, "Popen", popen)
    source = SimpleNamespace(fps=10, codec="mjpeg", frame=lambda: b"frame")
    pipe = radio.Pipeline("rtmps://ingest.example/live2", STREAM_KEY, Y.folder() / "unused", "libx264", None,
                          lambda pid: None, marker=Y.MARKER, source=source, hls_input=Y.mirror_playlist(),
                          output_fps=25, err_path=Y.folder() / "ffmpeg.err")
    result = pipe.run()
    assert seen["publisher"] == ("rtmps://ingest.example/live2", STREAM_KEY)
    assert seen["env"] is None and STREAM_KEY not in " ".join(seen["argv"])
    text = "\n".join(p.read_text(errors="replace") for p in Y.folder().rglob("*") if p.is_file())
    assert STREAM_KEY not in text + result and "ingest.example" not in text + result


def test_cli_and_cockpit_dispatch_use_same_explicit_action(monkeypatch):
    parsed = cli._parser().parse_args(["radio", "youtube", "mirror", "on"])
    assert parsed.mirror == "on" and parsed.youtube_cmd == "mirror"
    calls = []
    monkeypatch.setattr(Y, "set_mirror", lambda cfg, on, **kw: calls.append(on) or "youtube_mirror_on")
    assert Y.cli(mirror_cfg(), "mirror", mirror="on") == 0 and calls == [True]
    monkeypatch.setattr(Y.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    request = Y.put_start_request(Y.generation(), "UCsample")
    Y.spawn("mirror-on", request)
    assert calls[-1][-5:] == ["youtube", "mirror", "on", "--request", request]


def test_mirror_off_returns_to_disabled_staff_edition():
    c = cfg(enabled=False)
    Y.set_mirror(c, True)
    assert Y.settings(c)["edition"] == "mirror" and Y.settings(c)["enabled"]
    Y.set_mirror(c, False)
    assert Y.settings(c)["edition"] == "staff" and not Y.settings(c)["enabled"]


def test_missing_playlist_waits_then_reconnects_without_failure_budget(monkeypatch):
    c, path = mirror_cfg(), live_home()
    Y.set_mirror(c, True)
    st, g = station(c)
    show = SimpleNamespace(fps=10, codec="mjpeg", failed="")
    monkeypatch.setattr(st, "start_board", lambda: setattr(st, "show", show))
    monkeypatch.setattr(radio, "pick_encoder", lambda *a: "libx264")
    runs, waits = [], []
    class Pipeline:
        def __init__(self, *a, **kw):
            pass
        def run(self):
            runs.append(True)
            if len(runs) == 1:
                path.unlink()
                return radio.MODE_CHANGED
            st.quit.set()
            return ""
        def close(self):
            pass
    monkeypatch.setattr(radio, "Pipeline", Pipeline)
    def wait(seconds):
        waits.append(seconds)
        assert not Y._state().get("broadcast_id")
        live_home()
    monkeypatch.setattr(st.quit, "wait", wait)
    assert st._run() == 0
    assert len(runs) == 2 and waits == [1]
    assert len(g.did("POST", "/liveBroadcasts")) == 2
    assert not Y._read(Y.folder() / "failures.json") and Y.mirror_started() and radio.wanted()


def test_bridge_off_midstream_ends_broadcast_and_preserves_home(monkeypatch):
    c = mirror_cfg()
    live_home()
    Y.set_mirror(c, True)
    st, g = station(c)
    show = SimpleNamespace(fps=10, codec="mjpeg", failed="")
    monkeypatch.setattr(st, "start_board", lambda: setattr(st, "show", show))
    monkeypatch.setattr(radio, "pick_encoder", lambda *a: "libx264")
    class Pipeline:
        def __init__(self, *a, **kw):
            self.keep = kw["keep"]
        def run(self):
            monkeypatch.setattr(policy, "on", lambda: False)
            assert not self.keep()
            return radio.MIRROR_OFF
        def close(self):
            pass
    monkeypatch.setattr(radio, "Pipeline", Pipeline)
    def wait(seconds):
        assert not Y._state().get("broadcast_id") and radio.wanted()
        st.quit.set()
    monkeypatch.setattr(st.quit, "wait", wait)
    assert st._run() == 0
    assert len(g.did("POST", "/liveBroadcasts")) == 1 and g.did("DELETE", "/liveBroadcasts")
    assert Y.mirror_started() and radio.wanted()


def test_cockpit_requires_fresh_owner_confirmation_for_mirror(monkeypatch):
    from conftest import GOOD_TOKEN
    from ctb import panel_youtube
    from test_panel import OWNER, signed
    from test_radio_player import call
    c, spawned = cfg(enabled=False), []
    srv = SimpleNamespace(cfg=c, bot_token=GOOD_TOKEN, owner_id=OWNER, lang="he",
                          youtube_flow=panel_youtube.Flow(),
                          youtube_spawn=lambda *a: spawned.append(a))
    def post(body, init=None):
        code, _, payload = call(srv, path="/api/youtube", init=signed() if init is None else init,
                                body=json.dumps(body).encode())
        return code, json.loads(payload)
    assert post({"action": "mirror-on", "armed": "forged"})[0] == 409
    assert post({"action": "mirror-arm"}, init="forged")[0] == 403
    code, confirmation = post({"action": "mirror-arm"})
    assert code == 200 and confirmation["channel"] == "Sample channel"
    assert not Y.mirror_started() and not spawned
    assert post({"action": "mirror-on", "armed": confirmation["armed"]})[0] == 202
    action, request = spawned[0]
    assert action == "mirror-on"
    Y.switch(c, action, request)
    assert Y.mirror_started()
    assert post({"action": "mirror-on", "armed": confirmation["armed"]})[0] == 409
    monkeypatch.setattr(policy, "on", lambda: False)
    assert post({"action": "mirror-arm"})[0] == 503
    assert post({"action": "mirror-off"})[0] == 202
    assert spawned[-1] == ("mirror-off",)


def test_real_ffmpeg_reads_local_hls_and_emits_25fps_flv(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("needs ffmpeg and ffprobe")
    playlist = tmp_path / "live.m3u8"
    audio = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                            "sine=frequency=440:sample_rate=48000", "-t", "4", "-c:a", "aac", "-f", "hls",
                            "-hls_time", "6", "-hls_segment_filename", str(tmp_path / "segment-%d.ts"), str(playlist)],
                           capture_output=True, timeout=10)
    assert audio.returncode == 0, audio.stderr.decode()
    args = radio.encoder_args("libx264", fps=10, codec="mjpeg", hls_input=playlist,
                              output_fps=25, bitrate=Y.MIRROR_BITRATE)
    # Keep the real HLS input and output options; substitute only the board's image pipe with a test picture.
    args = (args[:args.index("-analyzeduration")] + ["-f", "lavfi", "-i", "color=s=64x64:r=10"] +
            args[args.index("-map"):])
    idx = args.index("-f", args.index("-c:a"))
    args[idx:idx] = ["-t", "3"]
    out = subprocess.run(args, capture_output=True, timeout=15)
    assert out.returncode == 0, out.stderr.decode()
    path = tmp_path / "mirror.flv"
    path.write_bytes(out.stdout)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,r_frame_rate",
                            "-of", "json", str(path)], capture_output=True, timeout=5)
    assert probe.returncode == 0, probe.stderr.decode()
    streams = json.loads(probe.stdout)["streams"]
    assert any(s.get("codec_name") == "aac" for s in streams)
    assert any(s.get("codec_name") == "h264" and s.get("r_frame_rate") == "25/1" for s in streams)


def test_home_worker_restart_reconciles_mirror(monkeypatch):
    c, seen = cfg(), []
    monkeypatch.setattr(radio, "make_bed", lambda *a: bytes(4))
    monkeypatch.setattr(Y, "home_changed", lambda c, on: seen.append(on))
    monkeypatch.setattr(radio.Station, "_run", lambda self: 0)
    assert radio.Station(c).run() == 0
    assert seen == [True, False]


def test_disabled_mirror_unloads_instead_of_leaving_a_dormant_job(monkeypatch):
    c = mirror_cfg()
    live_home()
    Y.set_mirror(c, True)
    Y.set_cockpit_prefs(enabled=False)
    st, _ = station(c)
    ended = []
    monkeypatch.setattr(st, "give_up", lambda **kw: ended.append(kw) or 0)
    assert st._run() == 0 and ended == [{"why": "ended"}]


def test_board_initialization_failure_never_reuses_an_empty_show(monkeypatch):
    from ctb import radio_paint, radio_show
    st, _ = station()
    shows = []
    def show(*a):
        obj = SimpleNamespace(failed="", frame=lambda: b"", step=lambda: None)
        shows.append(obj)
        return obj
    monkeypatch.setattr(radio_paint, "available", lambda *a: True)
    monkeypatch.setattr(radio_show, "BoardShow", show)
    monkeypatch.setattr(st, "feed_board", lambda: (_ for _ in ()).throw(OSError("transient state read")))
    for _ in range(2):
        with pytest.raises(OSError):
            st.start_board()
        assert st.show is None
    assert len(shows) == 2 and not Y._state().get("broadcast_id")
