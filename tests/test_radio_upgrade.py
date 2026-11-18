"""Install handover: fake services and private test files only; never run launchctl or a radio."""
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from ctb import buildstamp, heartbeat, panel, radio, radio_podcast, radio_room, radio_upgrade as U, radio_youtube
from ctb.config import Config
from test_panel import signed
from test_radio_player import call, hls, server  # noqa: F401


@pytest.fixture
def upgrade(monkeypatch):
    cfg = Config(radio={"radio_player": True})
    fake = SimpleNamespace(cfg=cfg, stamp="installed", stale=["radio"],
                           mirror={"on": False, "broadcast": False}, private=False, room=False,
                           podcast=None, calls=[], logs=[], code=0)
    monkeypatch.setattr(U.sys, "platform", "darwin")
    monkeypatch.setattr(buildstamp, "read", lambda: fake.stamp)
    monkeypatch.setattr(buildstamp, "stale", lambda **kw: fake.stale)
    monkeypatch.setattr(radio_youtube, "status", lambda cfg: fake.mirror)
    monkeypatch.setattr(radio, "private_audio_on", lambda now: fake.private)
    monkeypatch.setattr(radio_room, "live", lambda: fake.room)
    monkeypatch.setattr(radio_podcast, "view", lambda cfg, now=None: {"now": fake.podcast})
    monkeypatch.setattr(radio, "log", fake.logs.append)

    def runner(args, **kw):
        fake.calls.append((args, kw))
        return SimpleNamespace(returncode=fake.code)

    fake.runner = runner
    # The production default is fake too: even a missing injection cannot reach launchd.
    monkeypatch.setattr(U.subprocess, "run", runner)
    return fake


def test_stale_radio_restarts_once_with_exact_bounded_command(upgrade):
    f = upgrade
    assert U.check(f.cfg, now=1000, runner=f.runner)
    assert f.calls == [(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{radio.LABEL}"],
                        {"stdin": subprocess.DEVNULL, "capture_output": True, "timeout": 10, "check": False})]
    assert len(f.logs) == 1 and "restarted" in f.logs[0]
    # A lagging stamp or another heartbeat process must not cause another kick, even after cooldown.
    assert not U.check(f.cfg, now=1001, runner=f.runner)
    assert not U.check(f.cfg, now=1000 + U.COOLDOWN * 2, runner=f.runner)
    assert len(f.calls) == len(f.logs) == 1
    assert json.loads((radio.folder() / U.ATTEMPT).read_text()) == {"stamp": "installed", "at": 1000}


@pytest.mark.parametrize("mirror", [{"on": True, "broadcast": False}, {"on": False, "broadcast": True},
                                    {"on": True, "broadcast": True}, {}, {"on": False},
                                    {"on": None, "broadcast": False}, {"on": 0, "broadcast": False}])
def test_live_or_uncertain_youtube_never_restarts_or_claims(upgrade, mirror):
    f = upgrade
    f.mirror = mirror
    assert not U.check(f.cfg, now=1000)
    assert f.calls == f.logs == []
    assert not (radio.folder() / U.ATTEMPT).exists()
    assert buildstamp.stale() == ["radio"]  # remains visible to doctor


@pytest.mark.parametrize("kind", ["private", "room", "playing", "paused", "downloading"])
def test_private_item_waits_then_restarts_after_the_item_ends(upgrade, kind):
    f = upgrade
    if kind in ("private", "room"):
        setattr(f, kind, True)
    else:
        f.podcast = {"state": kind}
    assert not U.check(f.cfg, now=1000)
    assert not f.calls and not (radio.folder() / U.ATTEMPT).exists()
    f.private = f.room = False
    f.podcast = None
    assert U.check(f.cfg, now=1030)


@pytest.mark.parametrize("stamp,stale", [(None, ["radio"]), ("", ["radio"]), ("installed", []),
                                         ("installed", ["panel", "heartbeat"])])
def test_unknown_current_or_nonstale_radio_never_restarts(upgrade, stamp, stale):
    f = upgrade
    f.stamp, f.stale = stamp, stale
    assert not U.check(f.cfg, now=1000)
    assert f.calls == f.logs == []


@pytest.mark.parametrize("platform,configured", [("linux", True), ("darwin", False)])
def test_unsupported_or_unconfigured_is_a_noop(upgrade, monkeypatch, platform, configured):
    f = upgrade
    monkeypatch.setattr(U.sys, "platform", platform)
    if not configured:
        f.cfg.radio = {}
    assert not U.check(f.cfg, now=1000)
    assert f.calls == f.logs == []


@pytest.mark.parametrize("failure", ["exit", "missing", "timeout"])
def test_failed_restart_logs_once_and_never_loops(upgrade, failure):
    f = upgrade

    def fail(*args, **kw):
        f.calls.append(args)
        if failure == "missing":
            raise OSError("unavailable")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("launchctl", 10)
        return SimpleNamespace(returncode=1)

    assert not U.check(f.cfg, now=1000, runner=fail)
    assert not U.check(f.cfg, now=1000 + U.COOLDOWN * 2, runner=fail)
    assert len(f.calls) == len(f.logs) == 1 and "failed" in f.logs[0]


def test_new_build_obeys_cooldown_then_gets_one_attempt(upgrade):
    f = upgrade
    assert U.check(f.cfg, now=1000)
    f.stamp = "next-install"
    assert not U.check(f.cfg, now=1001)
    assert U.check(f.cfg, now=1000 + U.COOLDOWN)
    assert not U.check(f.cfg, now=1000 + U.COOLDOWN * 3)
    assert len(f.calls) == len(f.logs) == 2


def test_concurrent_checks_claim_only_one_restart(upgrade):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: U.check(upgrade.cfg, now=1000), range(8)))
    assert sum(results) == 1 and len(upgrade.calls) == len(upgrade.logs) == 1


@pytest.mark.parametrize("probe", ["read", "stale", "mirror", "private", "room", "podcast", "claim"])
def test_uncertain_checks_fail_closed_without_breaking_heartbeat(upgrade, monkeypatch, probe):
    def broken(*a, **kw):
        raise OSError("unreadable")

    target, name = {"read": (buildstamp, "read"), "stale": (buildstamp, "stale"),
                    "mirror": (radio_youtube, "status"), "private": (radio, "private_audio_on"),
                    "room": (radio_room, "live"), "podcast": (radio_podcast, "view"),
                    "claim": (U, "_claim")}[probe]
    monkeypatch.setattr(target, name, broken)
    assert not U.check(upgrade.cfg, now=1000)
    assert upgrade.calls == upgrade.logs == []


def test_heartbeat_checks_upgrade_even_without_topic_mirroring(upgrade, monkeypatch):
    upgrade.cfg.mirror = False
    monkeypatch.setattr(heartbeat, "_check_funnel", lambda *a: None)
    assert heartbeat.run_once(upgrade.cfg, now=1000) == 0
    assert len(upgrade.calls) == 1


@pytest.mark.parametrize("stamp,alive", [("old", True), ("installed", True), (None, True), ("old", False)])
def test_repair_uses_the_recorded_build_and_process_identity(upgrade, monkeypatch, stamp, alive):
    # Exercise the real stamp comparison without probing or signalling any OS process.
    from ctb.buildstamp import _path
    _path().write_text(json.dumps({"radio": {"stamp": stamp, "pid": 123, "start": "fake-start"}}))
    # Reload-free access to the original function retained outside the patched module attribute.
    monkeypatch.setattr(buildstamp, "stale", lambda **kw: _real_stale(alive=lambda *a: alive, **kw))
    assert U.check(upgrade.cfg, now=1000) is (stamp == "old" and alive)


_real_stale = buildstamp.stale


@pytest.fixture
def legacy(hls, monkeypatch):
    root = hls.with_name("hls")
    root.mkdir()
    (root / "live.m3u8").write_bytes((hls / "live.m3u8").read_bytes())
    (root / "segment-0.ts").write_bytes(b"legacy audio")
    monkeypatch.setattr(buildstamp, "stale", lambda: ["radio"])
    return root


@pytest.mark.parametrize("private_state", ["missing", "expired"])
def test_old_radio_serves_legacy_playlist_and_segments(server, hls, legacy, private_state):
    if private_state == "missing":
        (hls / "live.m3u8").unlink()
    else:
        os.utime(hls / "live.m3u8", (1, 1))
    token = panel.radio_token(server.bot_token, server.owner_id)
    code, _, body = call(server, "GET", "/radio/live.m3u8?token=" + token)
    assert code == 200 and b"segment-0.ts?token=" in body
    assert call(server, "GET", "/radio/segment-0.ts?token=" + token)[2] == b"legacy audio"
    assert json.loads(call(server, path="/api/radio-player", init=signed())[2])["on"] is True


@pytest.mark.parametrize("stale", [[], ["panel"], ["heartbeat"]])
def test_legacy_is_retired_immediately_when_radio_is_not_known_stale(server, hls, legacy, monkeypatch, stale):
    (hls / "live.m3u8").unlink()
    monkeypatch.setattr(buildstamp, "stale", lambda: stale)
    with pytest.raises(OSError):
        panel.radio_audio("live.m3u8")
    assert json.loads(call(server, path="/api/radio-player", init=signed())[2])["on"] is False


def test_new_private_feed_wins_without_mixing_same_named_segments(hls, legacy):
    assert panel.radio_audio("segment-0.ts") == b"private audio"
    (hls / "segment-0.ts").unlink()
    with pytest.raises(OSError):
        panel.radio_audio("segment-0.ts")  # never splice a legacy segment into a fresh private playlist


@pytest.mark.parametrize("unsafe", ["root-link", "playlist-link", "segment-link", "expired", "oversize", "fifo"])
def test_legacy_fallback_preserves_file_guards(hls, legacy, tmp_path, unsafe):
    (hls / "live.m3u8").unlink()
    target = "live.m3u8"
    if unsafe == "root-link":
        saved = legacy.with_name("saved")
        legacy.rename(saved)
        legacy.symlink_to(saved, target_is_directory=True)
    elif unsafe in ("playlist-link", "segment-link"):
        target = "live.m3u8" if unsafe == "playlist-link" else "segment-0.ts"
        (legacy / target).unlink()
        outside = tmp_path / "outside"
        outside.write_bytes(b"private")
        (legacy / target).symlink_to(outside)
    elif unsafe == "expired":
        os.utime(legacy / "live.m3u8", (1, 1))
    elif unsafe == "oversize":
        target = "segment-0.ts"
        (legacy / target).write_bytes(b"x" * 256001)
    else:
        target = "segment-0.ts"
        (legacy / target).unlink()
        os.mkfifo(legacy / target)
    with pytest.raises(OSError):
        panel.radio_audio(target)


def test_listen_link_also_uses_legacy_audio_while_stale(server, hls, legacy):
    from test_listen_link import make
    path = make(server)
    (hls / "live.m3u8").unlink()
    assert call(server, "GET", path + "/live.m3u8")[0] == 200
    assert call(server, "GET", path + "/segment-0.ts")[2] == b"legacy audio"


def test_legacy_playlist_still_rejects_external_uris(server, hls, legacy):
    (hls / "live.m3u8").unlink()
    (legacy / "live.m3u8").write_text("#EXTM3U\nhttps://example.invalid/private.ts\n")
    token = panel.radio_token(server.bot_token, server.owner_id)
    assert call(server, "GET", "/radio/live.m3u8?token=" + token)[0] == 404


def test_busy_mirror_start_lock_skips_without_claiming(upgrade):
    f = upgrade
    with radio_youtube._locked():  # a mirror start or stop holds the start/stop lock
        assert not U.check(f.cfg, now=1000, runner=f.runner)
    assert f.calls == [] and not (radio.folder() / U.ATTEMPT).exists()
    assert U.check(f.cfg, now=1000, runner=f.runner)  # the next quiet round still gets its one attempt


def test_restart_holds_the_start_lock_until_launchd_returns(upgrade):
    f = upgrade
    seen = []

    def runner(args, **kw):
        with radio_youtube.try_control_lock() as free:  # a mirror start racing the kick must wait
            seen.append(free)
        return SimpleNamespace(returncode=0)

    assert U.check(f.cfg, now=1000, runner=runner)
    assert seen == [False]


def test_marker_written_during_a_slow_round_counts_as_audible(upgrade, monkeypatch):
    f = upgrade
    clock = {"t": 1000.0}
    monkeypatch.setattr(U.time, "time", lambda: clock["t"])
    monkeypatch.setattr(radio, "private_audio_on", lambda now: now >= 1002)  # marker written at 1002
    clock["t"] = 1003.0  # the round began at 1000; a funnel check then took three seconds
    assert not U.check(f.cfg, runner=f.runner)
    assert f.calls == []


def test_heartbeat_lets_the_upgrade_read_its_own_clock(monkeypatch):
    got = []
    monkeypatch.setattr(U, "check", lambda cfg, **kw: got.append(kw) or False)
    monkeypatch.setattr(heartbeat, "_check_funnel", lambda cfg, now: None)
    heartbeat.run_once(Config(), now=1000)
    assert got == [{}]


def test_marker_written_during_the_mirror_status_read_counts_as_audible(upgrade, monkeypatch):
    f = upgrade
    clock = {"t": 1000.0}
    monkeypatch.setattr(U.time, "time", lambda: clock["t"])
    monkeypatch.setattr(radio, "private_audio_on", lambda now: now >= 1002)

    def slow_status(cfg):
        clock["t"] = 1003.0  # a private item started while the status was read
        return f.mirror

    monkeypatch.setattr(radio_youtube, "status", slow_status)
    assert not U.check(f.cfg, runner=f.runner)
    assert f.calls == [] and not (radio.folder() / U.ATTEMPT).exists()
