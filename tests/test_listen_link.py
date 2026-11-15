"""The private Listen in Safari link (listen_link.py): token auth, scope, revocation and the add-on switch."""
import json
import os
import re

import pytest

from conftest import GOOD_TOKEN
from ctb import addons, listen_link, panel, panel_page, radio
from ctb.config import state_dir
from test_panel import OWNER, signed
from test_radio_player import call, hls, server  # noqa: F401  (fixtures)


def make(server):
    status, _, body = call(server, "POST", "/api/listen-link", signed(), json.dumps({"action": "create"}).encode())
    assert status == 200
    return json.loads(body)["path"]


def test_create_needs_the_owner_login_and_stores_only_a_hash(server, hls):
    status, _, _ = call(server, "POST", "/api/listen-link", None, b'{"action": "create"}')
    assert status == 403
    path = make(server)
    token = path.rsplit("/", 1)[1]
    assert re.fullmatch(r"/listen/[A-Za-z0-9_-]{43}", path)
    stored = (state_dir() / listen_link.STORE).read_text()
    assert token not in stored and listen_link._digest(token) in stored
    assert oct(os.stat(state_dir() / listen_link.STORE).st_mode & 0o777) == "0o600"


def test_page_and_audio_need_the_token(server, hls):
    path = make(server)
    status, headers, body = call(server, "GET", path)
    assert status == 200 and b"mediaSession" in body and b"<audio" in body
    assert "nonce-" in headers["Content-Security-Policy"] and "connect-src 'self'" in headers["Content-Security-Policy"]
    assert path.encode() not in body  # the page finds its own path at runtime
    status, headers, body = call(server, "GET", path + "/live.m3u8")
    assert status == 200 and body.startswith(b"#EXTM3U") and b"segment-0.ts" in body
    assert headers["Content-Type"] == "application/vnd.apple.mpegurl"
    status, headers, body = call(server, "GET", path + "/segment-0.ts")
    assert (status, body) == (200, b"private audio") and headers["Content-Type"] == "video/mp2t"


@pytest.mark.parametrize("suffix", ["", "/live.m3u8", "/segment-0.ts"])
def test_wrong_token_is_forbidden(server, hls, suffix):
    make(server)
    for bad in ("A" * 43, "short", "x" * 200, "..", "%2e%2e"):
        assert call(server, "GET", "/listen/" + bad + suffix)[0] == 403


def test_revoked_token_is_forbidden_and_revoke_needs_login(server, hls):
    path = make(server)
    other = make(server)
    assert call(server, "POST", "/api/listen-link", None, b'{"action": "revoke"}')[0] == 403
    assert call(server, "GET", path)[0] == 200
    status, _, body = call(server, "POST", "/api/listen-link", signed(), b'{"action": "revoke"}')
    assert (status, json.loads(body)["revoked"]) == (200, 2)
    for p in (path, other):
        for suffix in ("", "/live.m3u8", "/segment-0.ts"):
            assert call(server, "GET", p + suffix)[0] == 403
    assert listen_link.active() == 0


def test_only_the_newest_tokens_stay_valid(server, hls):
    paths = [make(server) for _ in range(listen_link.MAX_TOKENS + 1)]
    assert call(server, "GET", paths[0])[0] == 403
    assert all(call(server, "GET", p)[0] == 200 for p in paths[1:])


def test_token_cannot_reach_any_api(server, hls):
    path = make(server)
    token = path.rsplit("/", 1)[1]
    for route in ("/api/state", "/api/addons", "/api/listen-link", "/api/radio-player", "/api/radio"):
        assert call(server, "GET", route, None, headers={"Authorization": "Bearer " + token})[0] in (403, 404)
        status, _, body = call(server, "POST", route, None, b'{"action": "view"}',
                               headers={"Authorization": "Bearer " + token, panel.INIT_HEADER: token})
        assert status == 403 and token.encode() not in body
        assert call(server, "GET", path + route)[0] == 404
    assert call(server, "GET", "/radio/live.m3u8?token=" + token)[0] == 403  # not a radio-player token either


@pytest.mark.parametrize("tail", ["/../../api/state", "/..%2f..%2fapi/state", "/live.m3u8/../x", "/%2e%2e/live.m3u8",
                                  "/segment-0.ts%00", "/segment-../x.ts", "//live.m3u8", "/LIVE.m3u8", "/other.ts",
                                  "/segment-0.ts/extra", "/live.m3u8x"])
def test_path_traversal_is_rejected(server, hls, tail):
    path = make(server)
    status, _, body = call(server, "GET", path + tail)
    assert status in (403, 404) and b"private audio" not in body and b"EXTM3U" not in body


def test_symlinked_segment_is_not_served(server, hls, tmp_path):
    path = make(server)
    secret = tmp_path / "secret.ts"
    secret.write_bytes(b"outside")
    (hls / "segment-9.ts").symlink_to(secret)
    assert call(server, "GET", path + "/segment-9.ts")[0] == 404


def test_stale_playlist_and_oversize_segments_use_the_radio_guards(server, hls, monkeypatch):
    from ctb import radio_play
    monkeypatch.setattr(radio_play, "request", lambda cfg: radio_play.OFF)  # Play's start has its own tests
    path = make(server)
    old = os.stat(hls / "live.m3u8").st_mtime - 3600
    os.utime(hls / "live.m3u8", (old, old))
    assert call(server, "GET", path + "/live.m3u8")[0] == 404
    assert call(server, "GET", path + "/segment-0.ts")[0] == 404
    os.utime(hls / "live.m3u8", None)
    (hls / "segment-1.ts").write_bytes(b"x" * 256_001)
    assert call(server, "GET", path + "/segment-1.ts")[0] == 404


def test_playlist_with_foreign_uris_is_refused(server, hls):
    path = make(server)
    (hls / "live.m3u8").write_text("#EXTM3U\n#EXTINF:2,\nhttp://evil.example/x.ts\n")
    assert call(server, "GET", path + "/live.m3u8")[0] == 404
    (hls / "live.m3u8").write_text('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:2,\nsegment-0.ts\n')
    assert call(server, "GET", path + "/live.m3u8")[0] == 404


def test_switch_off_means_404_for_everything(server, hls):
    path = make(server)
    addons.set_on(server.cfg, "listen_link", False)
    for p in (path, path + "/live.m3u8", path + "/segment-0.ts", "/listen/" + "A" * 43, "/listen/x"):
        assert call(server, "GET", p)[0] == 404
    assert call(server, "POST", "/api/listen-link", signed(), b'{"action": "create"}')[0] == 404
    # switching off revoked the link: it does not return when the switch comes back on
    addons.set_on(server.cfg, "listen_link", True)
    assert call(server, "GET", path)[0] == 403
    assert call(server, "POST", "/api/listen-link", signed(), b'{"action": "revoke"}')[0] == 200


def test_switch_defaults_on_and_reaches_the_cockpit(server, hls):
    assert addons.BY_KEY["listen_link"].default is True and addons.enabled(server.cfg, "listen_link")
    status, _, body = call(server, "POST", "/api/radio-player", signed())
    assert json.loads(body)["listen_link"] is True
    addons.set_on(server.cfg, "listen_link", False)
    assert json.loads(call(server, "POST", "/api/radio-player", signed())[2])["listen_link"] is False


def test_no_radio_means_404(server, hls):
    path = make(server)
    server.cfg.radio = None
    assert call(server, "GET", path)[0] == 404


def test_page_is_self_contained_and_handles_recovery():
    html = listen_link.render_page("he", "N0nce")
    assert 'dir="rtl"' in html and "nonce=\"N0nce\"" in html and "http" not in html.replace("http://www.w3.org", "")
    for needle in ("setActionHandler(\"play\"", "setActionHandler(\"pause\"", "MediaMetadata", "403", "recover"):
        assert needle in html
    assert "live.m3u8" in html and "/listen/" not in html
    assert 'lang="en"' in listen_link.render_page("en", "n")


def test_cockpit_button_opens_safari_without_instant_view_and_settings_can_revoke():
    assert "try_instant_view:false" in panel_page.PAGE and "/api/listen-link" in panel_page.PAGE
    assert "data-listen-revoke" in panel_page.PAGE and "data-listen-safari" in panel_page.PAGE
    assert set(panel_page.STRINGS["he"]) == set(panel_page.STRINGS["en"])


def test_player_switch_off_means_404_and_no_new_link(server, hls, monkeypatch):
    path = make(server)
    monkeypatch.setattr(radio, "settings", lambda cfg: {"radio_player": False})
    for suffix in ("", "/live.m3u8", "/segment-0.ts"):
        assert call(server, "GET", path + suffix)[0] == 404
    assert call(server, "POST", "/api/listen-link", signed(), b'{"action": "create"}')[0] == 404
    assert call(server, "POST", "/api/listen-link", signed(), b'{"action": "revoke"}')[0] == 200


def test_token_expires_after_thirty_days(server, hls, monkeypatch):
    import time
    path = make(server)
    token = path.rsplit("/", 1)[1]
    real = time.time()
    monkeypatch.setattr(listen_link.time, "time", lambda: real + listen_link.TTL - 60)
    assert listen_link.valid(token) and call(server, "GET", path)[0] == 200
    monkeypatch.setattr(listen_link.time, "time", lambda: real + listen_link.TTL + 1)
    assert not listen_link.valid(token) and listen_link.active() == 0
    assert call(server, "GET", path)[0] == 403
    fresh = listen_link.mint()  # minting drops the expired row
    assert listen_link.active() == 1 and listen_link.valid(fresh) and not listen_link.valid(token)
