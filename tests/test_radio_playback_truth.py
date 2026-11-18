"""Playback truth and interruption recovery, using only Node media doubles and fake processes."""
import json
import os
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from ctb import listen_link, panel_page, radio_rtmp
from test_panel_page import func, js


MEDIA = r'''
const assert=require('node:assert/strict');
let clock=0;Date.now=()=>clock;
let timers=new Map(),tid=0;
function setTimeout(fn,delay){timers.set(++tid,{fn,delay});return tid;}
function clearTimeout(id){timers.delete(id);}
async function tick(delay){const jobs=[...timers].filter(([id,t])=>t.delay===delay);
 for(const [id,t] of jobs){timers.delete(id);await t.fn();}await flush();}
async function flush(){for(let i=0;i<8;i++)await Promise.resolve();}
function el(tag,cls,text){
 const n={tag,cls,text:text||'',textContent:text||'',children:[],events:{},attrs:{},paused:true,ended:false,error:null,currentTime:0,src:'',loads:0,plays:0,
  appendChild(c){this.children.push(c);},setAttribute(k,v){this.attrs[k]=v;},removeAttribute(k){this[k]='';},
  addEventListener(k,f){(this.events[k]||(this.events[k]=[])).push(f);},
  emit(k){for(const f of this.events[k]||[])f();},
  play(){this.plays++;this.paused=false;this.emit('play');return this.nextPlay||Promise.resolve();},
  pause(){const was=this.paused;this.paused=true;if(!was)this.emit('pause');},
  load(){this.loads++;this.paused=true;this.error=null;this.ended=false;this.emit('emptied');}};
 if(tag!=='audio')Object.defineProperty(n,'textContent',{get(){return this.text||'';},set(t){this.text=t;this.children=[];}});
 return n;
}
const nodes={};
const document={getElementById:id=>nodes[id],body:el('body'),addEventListener(k,f){this[k]=f;}};
const navigator={mediaSession:{playbackState:'none',setActionHandler(k,f){this[k]=f;}}};
function MediaMetadata(v){Object.assign(this,v);}
'''


def run_node(script):
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    result = subprocess.run(["node", "-e", MEDIA + script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def cockpit(checks):
    functions = '\n'.join(func(n) for n in ('radioPlayerDraw', 'radioPlayerSchedule', 'radioPlayerSource',
                                          'radioPlayerPlay', 'radioPlayerStop', 'radioPlayerRefresh'))
    variables = next(line for line in js().splitlines() if line.startswith('var radioAudio='))
    setup = '''
const box=nodes['radio-player']=el('div');
let view='me',targetClosed=false;function home(){return 'me';}function v2RadioArtwork(){}
let info={enabled:true,on:true,token:'first',expires:600},status=200,pending=null;
function post(){return pending||Promise.resolve({ok:status===200,status,json:()=>Promise.resolve(info)});}
function button(){return box.children[0].children[2];}function label(){return box.children[0].children[1];}
'''
    run_node('const S=' + json.dumps(panel_page.STRINGS['en']) + ';\n' + setup + variables + functions +
             '\n(async()=>{radioInfo=info;radioPlayerDraw();const a=radioAudio;\n' + checks +
             '\n})().catch(e=>{console.error(e);process.exitCode=1;});')


def test_cockpit_real_events_drive_status_and_controls():
    cockpit('''
assert.equal(a.plays,0);assert.equal(button().textContent,S.radioPlay);assert.notEqual(label().textContent,S.radioLive);
button().emit('click');await flush();
assert.equal(label().textContent,S.radioPlayerWaiting);assert.equal(button().attrs['aria-pressed'],'false');
a.emit('playing');assert.equal(label().textContent,S.radioLive);assert.equal(button().attrs['aria-pressed'],'true');
a.emit('waiting');assert.equal(label().textContent,S.radioPlayerWaiting);assert.equal(button().textContent,S.radioPause);
a.pause();assert.equal(button().textContent,S.radioPlay);assert.notEqual(label().textContent,S.radioLive);
a.error={code:2};a.emit('error');assert.equal(label().textContent,S.radioPlayerError);assert.equal(button().textContent,S.radioPlay);
''')


def test_cockpit_download_stall_keeps_playing_and_time_progress_ends_waiting():
    cockpit('''
button().emit('click');a.emit('playing');const loads=a.loads;
a.emit('stalled');assert.equal(label().textContent,S.radioLive);
assert.equal(button().attrs['aria-pressed'],'true');assert.equal(a.loads,loads);
a.emit('waiting');a.emit('timeupdate');assert.equal(label().textContent,S.radioPlayerWaiting);
a.currentTime=1;a.emit('timeupdate');assert.equal(label().textContent,S.radioLive);
a.emit('waiting');a.emit('playing');assert.equal(label().textContent,S.radioLive);
a.pause();a.currentTime=2;a.emit('timeupdate');assert.equal(button().textContent,S.radioPlay);
assert.notEqual(label().textContent,S.radioLive);
''')


def test_cockpit_external_pause_and_refresh_never_resume_stale_audio():
    cockpit('''
button().emit('click');a.emit('playing');const loads=a.loads;
a.pause();const plays=a.plays;await radioPlayerRefresh();
assert.equal(a.plays,plays);assert.equal(a.loads,loads);assert.equal(timers.size,0);
button().emit('click');assert.equal(a.loads,loads+1);assert.equal(a.plays,plays+1);assert.match(a.src,/first/);
view='agents';radioPlayerDraw();assert.equal(box.hidden,true);assert.equal(a.paused,false);assert.equal(radioAudio,a);
info={enabled:true,on:false};await radioPlayerRefresh();assert.equal(a.paused,true);
info={enabled:true,on:true,token:'third',expires:800};await radioPlayerRefresh();assert.equal(a.paused,true);
view='me';radioPlayerDraw();button().emit('click');assert.match(a.src,/third/);
clock=680001;info={enabled:true,on:true,token:'renewed',expires:1280};await radioPlayerRefresh();
assert.match(a.src,/renewed/);assert.equal(a.paused,false);
button().emit('click');await radioPlayerRefresh();assert.equal(a.paused,true);
status=403;await radioPlayerRefresh();assert.equal(a.src,'');assert.equal(box.hidden,true);
''')


def test_cockpit_in_flight_refresh_and_old_play_rejection_respect_newer_pause_or_play():
    cockpit('''
let reject;a.nextPlay=new Promise((_,r)=>{reject=r;});button().emit('click');
let resolve;pending=new Promise(r=>resolve=r);const refresh=radioPlayerRefresh();
a.pause();const plays=a.plays;
resolve({ok:true,json:()=>Promise.resolve({enabled:true,on:true,token:'next',expires:1})});await refresh;
assert.equal(a.plays,plays);pending=null;a.nextPlay=null;
button().emit('click');a.emit('playing');reject(new Error('old interrupted source'));await flush();
assert.equal(label().textContent,S.radioLive);assert.equal(button().textContent,S.radioPause);
''')


def safari(checks):
    source = re.search(r'<script nonce="n">(.*?)</script>', listen_link.render_page('en', 'n'), re.S)[1]
    setup = '''
const a=nodes.au=el('audio'),b=nodes.btn=el('button'),s=nodes.st=el('div');
const location={pathname:'/listen/fake'};let reply={status:200},fetches=0;
function fetch(){fetches++;return Promise.resolve(reply);}
'''
    run_node(setup + source + '\n(async()=>{\n' + checks +
             '\n})().catch(e=>{console.error(e);process.exitCode=1;});')


def test_safari_status_and_lock_screen_follow_playing_waiting_pause_error():
    safari('''
const ms=navigator.mediaSession;assert.equal(a.plays,0);b.emit('click');await flush();
assert.notEqual(ms.playbackState,'playing');assert.equal(s.textContent,'Waiting for audio…');
a.emit('playing');assert.equal(ms.playbackState,'playing');assert.equal(s.textContent,'Live');
a.emit('waiting');assert.notEqual(ms.playbackState,'playing');assert.notEqual(s.textContent,'Live');
a.pause();assert.equal(ms.playbackState,'paused');assert.equal(b.textContent,'Listen');
a.error={code:2};a.emit('error');assert.equal(s.textContent,'Playback stopped. Try again');assert.notEqual(ms.playbackState,'playing');
''')


@pytest.mark.parametrize('resume', ['button', 'lock_screen'])
def test_safari_explicit_resume_loads_fresh_live_source(resume):
    safari('''
b.emit('click');a.emit('playing');const url=a.src,loads=a.loads;a.pause();
if(document.visibilitychange)document.visibilitychange();await tick(1000);await tick(10000);
assert.equal(a.loads,loads);assert.equal(a.paused,true);assert.equal(fetches,0);
''' + ("b.emit('click');" if resume == 'button' else "navigator.mediaSession.play();") + '''
assert.equal(a.loads,loads+1);assert.notEqual(a.src,url);assert.equal(a.paused,false);
''')


@pytest.mark.parametrize('status', [200, 403, 503])
def test_safari_pause_cancels_in_flight_recovery(status):
    safari('''
b.emit('click');a.emit('playing');a.emit('waiting');await tick(10000);
let resolve;reply=new Promise(r=>resolve=r);await tick(1000);assert.equal(fetches,1);
a.pause();const loads=a.loads,plays=a.plays;
resolve({status:''' + str(status) + '''});await flush();await tick(1000);await tick(10000);
assert.equal(a.loads,loads);assert.equal(a.plays,plays);assert.equal(a.paused,true);assert.equal(b.textContent,'Listen');
assert.equal(timers.size,0);
''')


def test_safari_old_play_failure_cannot_restart_after_pause_or_override_new_play():
    safari('''
let reject;a.nextPlay=new Promise((_,r)=>reject=r);b.emit('click');a.pause();a.nextPlay=null;
b.emit('click');a.emit('playing');reject(new Error('interrupted'));await flush();await tick(1000);
assert.equal(s.textContent,'Live');assert.equal(fetches,0);assert.equal(a.plays,2);
''')


def test_safari_waiting_recovers_and_revocation_stops_playback():
    safari('''
b.emit('click');a.emit('playing');const loads=a.loads;a.emit('waiting');await tick(10000);await tick(1000);
assert.equal(fetches,1);assert.equal(a.loads,loads+1);assert.notEqual(navigator.mediaSession.playbackState,'playing');
a.emit('playing');a.error={code:2};a.emit('error');reply={status:403};await tick(1000);
assert.equal(a.paused,true);assert.match(s.textContent,/revoked/);const plays=a.plays;
navigator.mediaSession.play();assert.equal(a.plays,plays);assert.equal(timers.size,0);
''')


def test_safari_download_stall_does_not_reload_buffered_playback():
    safari('''
b.emit('click');a.emit('playing');const loads=a.loads,plays=a.plays;
a.emit('stalled');await tick(10000);await tick(1000);
assert.equal(s.textContent,'Live');assert.equal(navigator.mediaSession.playbackState,'playing');
assert.equal(fetches,0);assert.equal(a.loads,loads);assert.equal(a.plays,plays);
a.emit('waiting');a.emit('timeupdate');assert.equal(s.textContent,'Waiting for audio…');
a.currentTime=1;a.emit('timeupdate');await tick(10000);await tick(1000);
assert.equal(s.textContent,'Live');assert.equal(navigator.mediaSession.playbackState,'playing');
assert.equal(fetches,0);assert.equal(a.loads,loads);
a.pause();a.currentTime=2;a.emit('timeupdate');assert.equal(navigator.mediaSession.playbackState,'paused');
''')


def test_safari_progress_invalidates_in_flight_recovery():
    safari('''
b.emit('click');a.emit('playing');a.emit('waiting');await tick(10000);
let resolve;reply=new Promise(r=>resolve=r);await tick(1000);const loads=a.loads;
a.currentTime=1;a.emit('timeupdate');resolve({status:200});await flush();
assert.equal(a.loads,loads);assert.equal(s.textContent,'Live');assert.equal(timers.size,0);
''')


@pytest.fixture
def segmenter(tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(radio_rtmp.time, 'monotonic', lambda: now[0])
    # No real ffmpeg, writer thread, network or broadcast.
    monkeypatch.setattr(radio_rtmp.threading, 'Thread', lambda **k: SimpleNamespace(start=lambda: None))
    procs = []
    def popen(args, **kw):
        proc = SimpleNamespace(alive=True, killed=0, args=args, cwd=kw['cwd'])
        proc.poll = lambda: None if proc.alive else 1
        def kill():
            proc.alive = False
            proc.killed += 1
        proc.kill = kill
        procs.append(proc)
        return proc
    monkeypatch.setattr(radio_rtmp.subprocess, 'Popen', popen)
    logs = []
    player = radio_rtmp.PlayerSegmenter(tmp_path / 'private-hls', logs.append)
    yield player, now, procs, logs
    player.close()


@pytest.mark.parametrize('playlist', ['missing', 'stuck', 'old'])
def test_stalled_segment_writer_restarts_promptly_on_clean_flv_boundary(segmenter, playlist):
    from test_radio_group_live import HEADER, SCRIPT, SEQ, DATA, STREAM
    player, now, procs, logs = segmenter
    player.path.mkdir()
    path = player.path / 'live.m3u8'
    if playlist == 'old':
        path.write_text('old playlist')
    cut = len(HEADER + SCRIPT + SEQ) + len(DATA[0]) + 7
    player.offer(STREAM[:cut])
    if playlist == 'stuck':
        now[0] = 2
        path.write_text('stuck playlist')
        player.check_health()
    now[0] = 3
    player.offer(STREAM[cut:cut + 1])
    now[0] = 10
    player.offer(STREAM[cut + 1:cut + 2])
    now[0] = 18
    player.offer(STREAM[cut + 2:cut + 3])
    player.check_health()
    assert procs[0].killed == 1 and player.process is None and len(logs) == 1
    player.offer(STREAM[cut + 3:])  # no 60-second outage after a confirmed stall
    assert len(procs) == 2 and player.process is procs[1]
    assert player.queue.get_nowait() == HEADER + SCRIPT + SEQ
    body = player.queue.get_nowait()
    assert any(body.startswith(tag) for tag in DATA[2:])
    assert all(p.cwd == player.path for p in procs)  # same private output, no public fallback


def test_stall_restart_rate_has_a_hard_rolling_limit(segmenter):
    player, now, procs, _ = segmenter
    player.offer(b'FLV')
    for _ in range(radio_rtmp.PLAYER_RESTART_LIMIT):
        now[0] += 1
        player.offer(b'input')
        now[0] += radio_rtmp.PLAYER_STALL_SECONDS - 1
        player.offer(b'input')
        player.check_health()
        assert player.process is None
        player.offer(b'resume')
    assert len(procs) == 4
    now[0] += 1
    player.offer(b'input')
    now[0] += radio_rtmp.PLAYER_STALL_SECONDS - 1
    player.offer(b'input')
    player.check_health()
    for _ in range(100):
        player.offer(b'input')
        player.check_health()
    assert len(procs) == 4 and procs[-1].killed == 1
    now[0] = radio_rtmp.PLAYER_RESTART_WINDOW + radio_rtmp.PLAYER_STALL_SECONDS
    player.offer(b'after the rolling window')
    assert len(procs) == 5


def test_stale_playlist_without_recent_input_does_not_retire_writer(segmenter):
    player, now, procs, logs = segmenter
    player.offer(b'FLV')
    for second in (16, 32, 120):
        now[0] = second
        player.check_health()
    assert not procs[0].killed and not logs
    player.offer(b'input resumes after a long gap')
    player.check_health()
    now[0] += 1
    player.check_health()
    assert not procs[0].killed and not logs  # give returning input a full output window


@pytest.mark.parametrize('name', ['segment-1.ts', 'segment-2.ts.tmp'])
def test_segment_write_progress_keeps_writer_alive_with_stale_playlist(segmenter, name):
    player, now, procs, logs = segmenter
    player.offer(b'FLV')
    for n in range(1, 40):
        now[0] = n * 2
        player.offer(b'input')
        with (player.path / name).open('ab') as segment:
            segment.write(b'bytes')
        player.check_health()
    assert len(procs) == 1 and not procs[0].killed and not logs


def test_non_stall_failure_keeps_restart_cooldown(segmenter):
    player, now, procs, _ = segmenter
    player.offer(b'FLV')
    player._disable()
    now[0] = 16
    player.offer(b'input')
    assert len(procs) == 1
    now[0] = radio_rtmp.PLAYER_RESTART_SECONDS
    player.offer(b'input')
    assert len(procs) == 2


def test_restart_waits_for_old_writer_exit_before_removing_temps(segmenter):
    player, now, procs, _ = segmenter
    player.offer(b'FLV')
    temporary = player.path / 'segment-1.ts.tmp'
    temporary.write_bytes(b'old writer')
    os.utime(temporary, (0, 0))
    old = procs[0]
    old.kill = lambda: None  # kill requested, but the process has not exited yet
    player._disable(stalled=True)
    now[0] = 16
    player.offer(b'input')
    player.cleanup()
    assert temporary.exists() and len(procs) == 1
    old.alive = False
    player.offer(b'input')
    assert not temporary.exists() and len(procs) == 2


def test_restart_and_periodic_cleanup_remove_only_stale_orphan_temps(segmenter, tmp_path, monkeypatch):
    from ctb import radio
    player, now, procs, _ = segmenter
    monkeypatch.setattr(radio_rtmp.time, 'time', lambda: 1000)
    player.offer(b'FLV')
    old = player.path / 'segment-1.ts.tmp'
    recent = player.path / 'segment-2.ts.tmp'
    changed = player.path / 'segment-3.ts.tmp'
    for path, age in ((old, 10), (recent, 2), (changed, 2)):
        path.write_bytes(b'orphan')
        os.utime(path, (1000 - age, 1000 - age))
    outside = tmp_path / 'outside'
    outside.write_bytes(b'keep')
    os.utime(outside, (0, 0))
    linked = player.path / 'segment-4.ts.tmp'
    linked.symlink_to(outside)
    player._disable(stalled=True)
    player.offer(b'restart')
    assert len(procs) == 2 and not old.exists() and recent.exists()
    current = player.path / 'segment-5.ts.tmp'
    current.write_bytes(b'live writer')
    os.utime(current, (0, 0))  # age alone does not prove the live writer has closed it
    changed.write_bytes(b'changed since the orphan snapshot')
    monkeypatch.setattr(radio_rtmp.time, 'time', lambda: 1010)
    pipe = radio.Pipeline('', '', None, '', None, lambda pid: None, audio_only=True)
    pipe.hls_dir = player.path
    waits = iter((False, True))
    pipe.stop = SimpleNamespace(wait=lambda _: next(waits))
    pipe._hls_freshness(player)  # cleanup still runs without a playlist
    assert not recent.exists() and current.exists() and changed.exists()
    assert linked.is_symlink() and outside.read_bytes() == b'keep'


def test_cleanup_refuses_linked_directory_and_is_bounded(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    segment = outside / 'segment-0.ts'
    segment.write_bytes(b'keep')
    os.utime(segment, (0, 0))
    linked = tmp_path / 'linked'
    linked.symlink_to(outside, target_is_directory=True)
    radio_rtmp.cleanup_player_hls(linked)
    assert segment.exists()
    for n in range(1, radio_rtmp.PLAYER_CLEANUP_LIMIT + 2):
        path = outside / f'segment-{n}.ts'
        path.write_bytes(b'stale')
        os.utime(path, (0, 0))
    radio_rtmp.cleanup_player_hls(outside)
    assert len(list(outside.iterdir())) == 2


def test_advancing_playlist_is_healthy_and_closed_writer_stays_closed(segmenter):
    player, now, procs, logs = segmenter
    player.offer(b'FLV')
    for n in range(1, 100):
        now[0] = n * 2
        (player.path / 'live.m3u8').write_text(str(n))
        player.check_health()
    assert len(procs) == 1 and procs[0].killed == 0 and not logs
    player.close()
    now[0] += 1000
    player.offer(b'late bytes')
    assert len(procs) == 1 and not player._start()


def test_old_writer_failure_cannot_disable_replacement(segmenter):
    player, now, procs, _ = segmenter
    player.offer(b'FLV')
    old = procs[0]
    player._disable()
    now[0] = 60
    assert player._start()
    player._disable(expected=old)
    assert player.process is procs[1] and not procs[1].killed


def test_live_window_and_removed_segment_retention_are_bounded():
    args = radio_rtmp.PlayerSegmenter.args()
    window = int(args[args.index('-hls_list_size') + 1])
    retention = int(args[args.index('-hls_delete_threshold') + 1])
    assert window == 15 and retention >= window + 1  # RFC 8216 6.2.2
    assert window + retention <= 40  # roughly a minute on disk, not an archive
    flags = args[args.index('-hls_flags') + 1]
    assert 'delete_segments' in flags and 'temp_file' in flags  # segments and playlist publish by atomic rename
    assert args[-1] == 'live.m3u8' and 'rtmp' not in ' '.join(args)


def test_restart_cleanup_preserves_playlist_and_its_segments(tmp_path, monkeypatch):
    from ctb import radio_rtmp
    playlist = tmp_path / "live.m3u8"
    playlist.write_text("#EXTM3U\n#EXTINF:2,\nsegment-10.ts\n")
    keep, prune = tmp_path / "segment-10.ts", tmp_path / "segment-9.ts"
    keep.write_bytes(b"current")
    prune.write_bytes(b"old")
    monkeypatch.setattr(radio_rtmp.time, "time", lambda: 10_000)
    os.utime(keep, (1, 1))
    os.utime(prune, (1, 1))

    radio_rtmp.cleanup_player_hls(tmp_path)

    assert playlist.read_text().endswith("segment-10.ts\n")
    assert keep.read_bytes() == b"current"
    assert not prune.exists()


@pytest.mark.parametrize('state,icon', [('playing', 'v2pause'), ('waiting', 'v2pause'), ('paused', 'v2play'), ('error', 'v2play')])
def test_cockpit_v2_decoration_uses_the_audio_element(state, icon):
    from pathlib import Path
    source = (Path(panel_page.__file__).with_name('cockpit_v2.js')).read_text()
    source = source[source.index('function v2RadioArtwork()'):source.index('\nICONS.v2home=')]
    run_node('''
const btn=el('button',null,'Listen'),row=el('div'),box=nodes['radio-player']=el('div');
row.querySelector=q=>q==='button'?btn:null;row.insertBefore=()=>{};
box.querySelector=()=>row;function v2Enabled(){return true;}function v2RadioHere(){return true;}
function v2Image(){return el('img');}function icon(name){return {name};}
let last={},targetClosed=false,radioInfo={enabled:true},radioAudio=el('audio');
''' + source + '''
radioAudio.paused=''' + ('true' if state == 'paused' else 'false') + ''';
radioAudio.error=''' + ('{code:2}' if state == 'error' else 'null') + ''';
v2RadioArtwork();assert.equal(btn.children[0].name,''' + json.dumps(icon) + ''');
''')


def test_pipeline_filesystem_worker_checks_segmenter_health_without_restarting_station(tmp_path):
    from ctb import radio
    pipe = radio.Pipeline('', '', None, '', None, lambda pid: None, audio_only=True)
    pipe.hls_dir = tmp_path
    waits, checks = [], []
    def wait(seconds):
        waits.append(seconds)
        return len(waits) > 1
    pipe.stop = SimpleNamespace(wait=wait)
    pipe._hls_freshness(SimpleNamespace(check_health=lambda: checks.append(True), cleanup=lambda: None))
    assert checks == [True] and waits == [2.0, 2.0]
    assert pipe.proc is None  # only the existing segment writer is supervised


def test_safari_system_pause_cancels_recovery_even_with_media_error_present():
    safari('''
b.emit('click');a.emit('playing');a.error={code:2};a.emit('error');
a.pause();const plays=a.plays;await tick(1000);await tick(10000);
assert.equal(fetches,0);assert.equal(a.plays,plays);assert.equal(timers.size,0);
assert.notEqual(navigator.mediaSession.playbackState,'playing');
''')
