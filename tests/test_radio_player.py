"""Private native HLS: owner-only capabilities, bounded output and lifecycle."""
import json
import http.client
import io
import os
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest

from conftest import GOOD_TOKEN
from ctb import panel, panel_page, radio, radio_rtmp
from ctb.config import Config
from test_panel import OWNER, signed


@pytest.fixture
def server():
    # Exercise the real HTTP parser/handler without binding a port: also runs in networkless sandboxes.
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    return SimpleNamespace(cfg=cfg, bot_token=GOOD_TOKEN, owner_id=OWNER, lang="he")


def call(server, method="POST", path="/api/state", init=None, body=b"{}", headers=None):
    h = dict(headers or {})
    if init is not None:
        h[panel.INIT_HEADER] = init
    payload = body if method == "POST" else b""
    h["Content-Length"] = str(len(payload))
    wire = (f"{method} {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n").encode() + payload

    class Handler(panel.PanelHandler):
        def setup(self):
            self.rfile, self.wfile = io.BytesIO(wire), io.BytesIO()

        def finish(self):
            self.response = self.wfile.getvalue()

    handler = Handler(None, ("127.0.0.1", 0), server)
    response = http.client.HTTPResponse(SimpleNamespace(makefile=lambda *a, **k: io.BytesIO(handler.response)))
    response.begin()
    return response.status, dict(response.getheaders()), response.read()


@pytest.fixture
def hls(server):
    server.cfg.radio = {"radio_player": True}
    root = radio.prepare_hls()
    (root / "live.m3u8").write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nsegment-0.ts\n")
    (root / "segment-0.ts").write_bytes(b"private audio")
    return root


def test_token_round_trip_and_scope():
    token = panel.radio_token(GOOD_TOKEN, OWNER, now=100)
    assert panel.valid_radio_token(token, GOOD_TOKEN, OWNER, now=101)
    for secret, owner, now in (("other", OWNER, 101), (GOOD_TOKEN, OWNER + 1, 101),
                               (GOOD_TOKEN, OWNER, 700), (GOOD_TOKEN, OWNER, 99)):
        assert not panel.valid_radio_token(token, secret, owner, now=now)
    for bad in ("", "garbage", token + "x", "9" * 10000, token.upper()):
        assert not panel.valid_radio_token(bad, GOOD_TOKEN, OWNER, now=101)


@pytest.mark.parametrize("name", ["live.m3u8", "segment-0.ts"])
def test_radio_routes_require_their_own_fresh_token(server, hls, name):
    route = "/radio/" + name
    bad = ["", "?token=forged", "?token=" + panel.radio_token(GOOD_TOKEN, OWNER, now=0),
           "?token=" + panel.radio_token("other", OWNER),
           "?token=" + panel.radio_token(GOOD_TOKEN, OWNER + 1)]
    token = panel.radio_token(GOOD_TOKEN, OWNER)
    bad += ["?token=" + token + "&token=" + token, "?token=" + token + "&x=1"]
    for query in bad:
        assert call(server, "GET", route + query, init=signed())[0] == 403


def test_token_issuer_uses_owner_header_and_query_cannot_authorize_other_routes(server, hls):
    assert call(server, path="/api/radio-player")[0] == 403
    assert call(server, path="/api/radio-player", init=signed(user_id=OWNER + 1))[0] == 403
    code, _, data = call(server, path="/api/radio-player", init=signed())
    info = json.loads(data)
    assert code == 200 and info["on"] and info["enabled"]
    assert panel.valid_radio_token(info["token"], GOOD_TOKEN, OWNER)
    assert call(server, path="/api/state?token=" + info["token"])[0] != 200
    assert call(server, path="/api/state", headers={panel.INIT_HEADER: info["token"]})[0] == 403


def test_playlist_signs_each_segment_and_correct_private_headers(server, hls, capsys):
    token = panel.radio_token(GOOD_TOKEN, OWNER)
    code, headers, body = call(server, "GET", "/radio/live.m3u8?token=" + token)
    assert code == 200 and b"segment-0.ts?token=" + token.encode() in body
    assert headers["Content-Type"] == "application/vnd.apple.mpegurl"
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"
    code, headers, body = call(server, "GET", "/radio/segment-0.ts?token=" + token)
    assert code == 200 and body == b"private audio" and headers["Content-Type"] == "video/mp2t"
    assert "media-src 'self'" in panel.csp("nonce")
    logs = capsys.readouterr()
    assert not any(s in logs.out + logs.err for s in (GOOD_TOKEN, token, "segment-0.ts", "private audio"))


@pytest.mark.parametrize("name", ["../radio.log", "%2e%2e/radio.log", "live.m3u8/../radio.log", "live.m3u8.tmp",
                                  "segment-0.ts.tmp", "segment--1.ts", "segment-0.aac", "/live.m3u8"])
def test_filename_allowlist(server, hls, name):
    token = panel.radio_token(GOOD_TOKEN, OWNER)
    assert call(server, "GET", "/radio/" + name + "?token=" + token)[0] == 404


def test_off_disabled_stale_and_linked_files_are_never_served(server, hls, tmp_path):
    token = panel.radio_token(GOOD_TOKEN, OWNER)
    path = "/radio/segment-0.ts?token=" + token
    other = tmp_path / "secret"
    other.write_bytes(b"must not escape")
    (hls / "segment-0.ts").unlink()
    (hls / "segment-0.ts").symlink_to(other)
    assert call(server, "GET", path)[0] == 404
    (hls / "segment-0.ts").unlink()
    (hls / "segment-0.ts").write_bytes(b"audio")
    then = time.time() - panel.RADIO_FRESH - 1
    os.utime(hls / "live.m3u8", (then, then))
    assert call(server, "GET", path)[0] == 404
    assert not json.loads(call(server, path="/api/radio-player", init=signed())[2])["on"]
    (hls / "live.m3u8").touch()
    server.cfg.radio["radio_player"] = False
    assert call(server, "GET", path)[0] == 404
    (hls / "live.m3u8").unlink()
    server.cfg.radio["radio_player"] = True
    assert call(server, "GET", path)[0] == 404


@pytest.mark.parametrize("uri", ["https://example.org/private", "../secret.ts", '#EXT-X-KEY:METHOD=AES-128,URI="key"'])
def test_playlist_refuses_unexpected_uris(server, hls, uri):
    (hls / "live.m3u8").write_text("#EXTM3U\n" + uri + "\n")
    assert call(server, "GET", "/radio/live.m3u8?token=" + panel.radio_token(GOOD_TOKEN, OWNER))[0] == 404


@pytest.mark.parametrize("linked", ["playlist", "folder"])
def test_linked_playlist_or_hls_folder_cannot_escape(server, hls, linked):
    if linked == "playlist":
        (hls / "live.m3u8").rename(hls / "saved")
        (hls / "live.m3u8").symlink_to(hls / "saved")
    else:
        saved = hls.with_name("saved-hls")
        hls.rename(saved)
        hls.symlink_to(saved, target_is_directory=True)
    token = panel.radio_token(GOOD_TOKEN, OWNER)
    assert call(server, "GET", "/radio/live.m3u8?token=" + token)[0] == 404
    assert call(server, "GET", "/radio/segment-0.ts?token=" + token)[0] == 404


def test_mirror_off_blocks_player_and_capability_issuance(server, hls, monkeypatch):
    from ctb import policy
    monkeypatch.setattr(policy, "on", lambda: False)
    assert call(server, path="/api/radio-player", init=signed())[0] == 503
    assert call(server, "GET", "/radio/live.m3u8?token=" + panel.radio_token(GOOD_TOKEN, OWNER))[0] == 503


def test_private_folder_and_pipeline_close_empty_it():
    root = radio.prepare_hls()
    assert root.stat().st_mode & 0o777 == 0o700
    (root / "live.m3u8").write_text("audio")
    (root / "segment-0.ts.tmp").write_text("audio")
    pipe = radio.Pipeline("", "", root / "card", "", None, lambda pid: None, audio_only=True)
    pipe.close()
    assert root.exists() and not list(root.iterdir())
    assert radio.settings(Config())["radio_player"] is True
    assert radio.settings(Config(radio={"radio_player": False}))["radio_player"] is False


def test_radio_stop_also_removes_leftover_audio(monkeypatch):
    root = radio.prepare_hls()
    (root / "segment-1.ts").write_bytes(b"audio")
    monkeypatch.setattr(radio, "_end_room", lambda *a: None)
    monkeypatch.setattr(radio, "_unload", lambda: None)
    monkeypatch.setattr(radio, "_kill_leftover_encoder", lambda: None)
    monkeypatch.setattr(radio, "_end_call", lambda *a: True)
    assert radio.stop(Config()) == "radio_stopped"
    assert not list(root.iterdir())


def test_main_encoder_is_always_single_flv_output():
    args = radio.encoder_args("libx264", hls=True)
    assert args.count("-c:a") == 1 and args[args.index("-ar", args.index("-c:a")) + 1] == "48000"
    assert args[args.index("-b:a") + 1] == "96k"
    assert args[-5:] == ["-f", "flv", "-flvflags", "no_duration_filesize", "pipe:1"]
    assert "tee" not in args and args == radio.encoder_args("libx264")


def test_segmenter_queue_is_lossy_and_never_waits(tmp_path, monkeypatch):
    class Blocked:
        stdin = SimpleNamespace(write=lambda _data: time.sleep(10), flush=lambda: None)
        def poll(self): return None
        def kill(self): pass
    monkeypatch.setattr(radio_rtmp.subprocess, "Popen", lambda *a, **k: Blocked())
    player = radio_rtmp.PlayerSegmenter(tmp_path / "missing" / "hls", queue_size=1)
    started = time.monotonic()
    for _ in range(100):
        player.offer(b"chunk")
    assert time.monotonic() - started < .2
    assert player.dropped > 0
    assert (tmp_path / "missing" / "hls").is_dir()


def test_segmenter_death_only_disables_player(tmp_path, monkeypatch):
    logs = []
    dead = SimpleNamespace(stdin=io.BytesIO(), poll=lambda: 1, kill=lambda: None)
    monkeypatch.setattr(radio_rtmp.subprocess, "Popen", lambda *a, **k: dead)
    player = radio_rtmp.PlayerSegmenter(tmp_path / "hls", logs.append)
    player.offer(b"FLV")
    time.sleep(.05)
    assert player.process is None
    assert len(logs) == 1


def test_station_start_clears_stale_player_files(tmp_path, monkeypatch):
    monkeypatch.setattr(radio, "folder", lambda: tmp_path)
    hls = tmp_path / "hls"
    hls.mkdir()
    (hls / "segment-1.ts").write_bytes(b"old")
    monkeypatch.setattr(radio, "make_bed", lambda *_: b"")
    radio.Station(Config())
    assert not list(hls.iterdir())


@pytest.mark.parametrize("lang", ["he", "en"])
def test_player_renders_both_languages_and_survives_refresh(lang):
    page = panel_page.render_page(lang, "nonce").decode()
    assert 'id="radio-player"' in page and panel_page.STRINGS[lang]["radioPlayerOff"] in page
    assert 'radioAudio=el("audio")' in page and 'radioAudio.play()' in page
    assert 'encodeURIComponent(radioInfo.token)' in page and 'radioPlayerRefresh();status' in page
    assert 'dir="rtl"' in page if lang == "he" else 'dir="ltr"' in page


def test_player_recovers_renews_and_keeps_one_audio_element():
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    from test_panel_page import func, js
    functions = "\n".join(func(name) for name in ("radioPlayerDraw", "radioPlayerSchedule", "radioPlayerSource",
                                                  "radioPlayerPlay", "radioPlayerStop", "radioPlayerRefresh"))
    variables = next(line for line in js().splitlines() if line.startswith("var radioAudio="))
    script = r'''
const assert=require("node:assert/strict");
let clock=0;Date.now=()=>clock;
let timer=null;function setTimeout(fn,delay){timer={fn,delay};return timer;}
function clearTimeout(t){if(timer===t)timer=null;}
let audioCount=0;
function el(tag,cls,text){
 const n={tag,cls,text,children:[],events:{},paused:true,preload:"",src:"",
  appendChild(c){this.children.push(c);},setAttribute(){},removeAttribute(k){this[k]="";},
  addEventListener(k,f){this.events[k]=f;},
  play(){this.paused=false;if(this.events.play)this.events.play();return Promise.resolve();},
  pause(){const was=this.paused;this.paused=true;if(!was&&this.events.pause)this.events.pause();},
  load(){this.paused=true;}};
 Object.defineProperty(n,"textContent",{set(){this.children=[];}});
 if(tag==="audio")audioCount++;return n;
}
const box=el("div"),document={getElementById:()=>box,body:el("body")};
const S={radio:"Radio",radioLive:"Live",radioPlayerOff:"Off",radioPlay:"Play",radioPause:"Pause",radioPlayerError:"Error"};
let view="me",targetClosed=false;function home(){return "me";}function v2RadioArtwork(){}
let info={enabled:true,on:true,token:"first",expires:600},status=200;
function post(){return Promise.resolve({ok:status===200,status,json:()=>Promise.resolve(info)});}
'''
    checks = r'''
(async()=>{
 radioInfo=info;radioPlayerDraw();const audio=radioAudio;
 assert.equal(audioCount,1);assert.equal(audio.src,""); // no autoplay before a tap
 box.children[0].children[2].events.click();await Promise.resolve();
 assert.equal(audio.paused,false);assert.match(audio.src,/first/);
 assert.equal(timer.delay,480000); // dedicated renewal, independent of hidden-tab polling
 await radioPlayerRefresh();assert.equal(radioAudio,audio);assert.match(audio.src,/first/);
 view="agents";radioPlayerDraw();assert.equal(box.hidden,true);assert.equal(audio.paused,false);
 info={enabled:true,on:false,token:"second",expires:700};await radioPlayerRefresh();
 assert.equal(audio.paused,true);
 info={enabled:true,on:true,token:"third",expires:800};await radioPlayerRefresh();
 assert.equal(audio.paused,true);
 view="me";radioPlayerDraw();box.children[0].children[2].events.click();
 assert.equal(audio.paused,false);assert.match(audio.src,/third/);
 clock=680001;info={enabled:true,on:true,token:"renewed",expires:1280};
 await timer.fn();assert.match(audio.src,/renewed/);assert.equal(audio.paused,false);
 view="me";radioPlayerDraw();box.children[0].children[2].events.click();
 await radioPlayerRefresh();assert.equal(audio.paused,true);
 assert.equal(timer,null);assert.equal(audioCount,1);
 status=403;await radioPlayerRefresh();assert.equal(audio.src,"");assert.equal(box.hidden,true);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run(["node", "-e", script + variables + "\n" + functions + checks],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_the_player_queue_absorbs_a_burst_of_small_flv_reads(tmp_path, monkeypatch):
    """The publisher reads FLV tag by tag; a one-second writer hiccup must not kill the player."""
    player = radio_rtmp.PlayerSegmenter(tmp_path / "hls")
    monkeypatch.setattr(player, "_start", lambda: True)
    for _ in range(2000):  # ~11 s of tag-sized reads with nobody draining
        player.offer(b"x" * 300)
    assert player.dropped == 0 and player.queued == 600000


def test_the_player_queue_is_bounded_by_bytes(tmp_path, monkeypatch):
    player = radio_rtmp.PlayerSegmenter(tmp_path / "hls")
    monkeypatch.setattr(player, "_start", lambda: True)
    monkeypatch.setattr(radio_rtmp, "PLAYER_BYTES", 1000)
    for _ in range(4):  # the fourth read would pass the bound
        player.offer(b"x" * 300)
    assert player.dropped == 1 and player.queued == 0  # over the bound the player is switched off and emptied
