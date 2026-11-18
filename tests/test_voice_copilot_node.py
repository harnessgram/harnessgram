"""Run the actual phone controller with fake media, fake fetch and a small DOM. No network."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ctb import panel_page

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node unavailable")
SOURCE = Path(panel_page.__file__).with_name("cockpit_voice_copilot.js").read_text()
PRELUDE = r'''
'use strict';
const assert=require('node:assert/strict');
class Element {
 constructor(tag,cls,text){this.tag=tag;this.className=cls;this.textContent=text||'';this.children=[];this.attrs={};this._dataset={};}
 get dataset(){return this._dataset;}
 appendChild(n){this.children.push(n);this.firstChild=this.children[0];return n;}
 removeChild(n){this.children.splice(this.children.indexOf(n),1);this.firstChild=this.children[0];}
 setAttribute(k,v){this.attrs[k]=v;}
 addEventListener(k,fn){this[k]=fn;}
}
function el(t,c,s){return new Element(t,c,s);}
let callbacks={},closed=0,tracksStopped=0,outputStopped=0,spoken=[],requests=[],nodes=[];
global.window={addEventListener:(k,f)=>callbacks[k]=f,
 speechSynthesis:{speak:u=>spoken.push(u.text)},SpeechSynthesisUtterance:class{constructor(t){this.text=t;}}};
global.document={hidden:false,addEventListener:(k,f)=>callbacks[k]=f};
var LANG='he',V2_COPY=__COPY__,wa={initData:'fake-signed-owner-login'},app=new Element('main'),view='tools';
function v2ToolSeg(){return 'tower';}
function audioNode(){let n={connect(){},disconnect(){},start(){},stop(){outputStopped++;},gain:{value:1}};nodes.push(n);return n;}
function context(){return {sampleRate:48000,currentTime:1,destination:{},
 createMediaStreamSource:()=>audioNode(),createScriptProcessor:()=>audioNode(),createGain:()=>audioNode(),
 createBufferSource:()=>audioNode(),createBuffer:(ch,len,rate)=>({getChannelData:()=>new Float32Array(len),duration:len/rate}),
 close:()=>{closed++;return Promise.resolve();}};}
let mic={getTracks:()=>[{stop:()=>tracksStopped++}]};
global.fetch=async function(url,options){requests.push([url,JSON.parse(options.body),options.headers]);
 return {ok:true,json:async()=>({ok:true}),body:{getReader:()=>({read:()=>new Promise(()=>{})})}};};
'''


def run(body):
    program = PRELUDE.replace("__COPY__", json.dumps({lang: {"voiceCopilot": panel_page.V2_STRINGS[lang]["voiceCopilot"]} for lang in ("he", "en")}, ensure_ascii=False)) + SOURCE
    result = subprocess.run([NODE, "-"], input=program + "\n(async()=>{" + body + "})().catch(e=>{console.error(e);process.exit(1);});",
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_page_off_makes_no_controls_then_pcm_and_cleanup():
    run(r'''
vcpPage({settings:{voice_copilot:false}});assert.equal(app.children.length,0);
vcpPage({settings:{voice_copilot:true}});assert.equal(app.children[0].attrs['data-v2-section'],'voice-copilot');
vcp.id='call1';vcp.ctx=context();vcp.mic=mic;vcpCapture('call1',vcp.generation);
vcp.processor.onaudioprocess({inputBuffer:{getChannelData:()=>new Float32Array(4096).fill(.25)}});
await vcp.sending;
assert.equal(requests.length,1);assert.equal(requests[0][0],'/api/voice-copilot/audio');
assert.deepEqual(Object.keys(requests[0][1]).sort(),['data','session']);
let pcm=Buffer.from(requests[0][1].data,'base64');assert.equal(pcm.length,2730);assert.equal(pcm.readInt16LE(0),8192);
assert.equal(requests[0][2]['X-Telegram-Init-Data'],'fake-signed-owner-login');
vcpSync({settings:{voice_copilot:false}});assert.equal(vcp.id,'');assert.equal(tracksStopped,1);assert.equal(closed,1);
assert.equal(requests[1][0],'/api/voice-copilot/stop');assert.equal(vcp.button.attrs['aria-pressed'],'false');
''')


def test_stream_fragmentation_transcripts_barge_in_and_terminal_speech():
    run(r'''
vcpPage({settings:{voice_copilot:true}});vcp.id='call2';vcp.ctx=context();vcp.mic=mic;
let events=[{type:'ready'},{type:'transcript',speaker:'owner',text:'שלום'},
 {type:'transcript',speaker:'owner',text:' עולם'},{type:'turn_complete'},
 {type:'audio',data:'AAA=',rate:24000},{type:'interrupted'},
 {type:'relay',queued:true},{type:'closed',message:'מכסת הדקות הסתיימה'}];
let wire=events.map(e=>'data: '+JSON.stringify(e)+'\n\n').join(''),bytes=Buffer.from(wire),chunks=[];
for(let at=0;at<bytes.length;at+=7)chunks.push(bytes.subarray(at,at+7));
global.fetch=async function(url,options){requests.push([url,JSON.parse(options.body),options.headers]);
 return {ok:true,body:{getReader:()=>({read:async()=>chunks.length?{value:chunks.shift(),done:false}:{done:true}})}};};
await vcpEvents('call2',new AbortController().signal,vcp.generation);
assert.equal(vcp.log.children.length,1);assert.equal(vcp.log.children[0].children[1].textContent,'שלום עולם');
assert.equal(outputStopped,1);assert.equal(vcp.id,'');assert.equal(tracksStopped,1);
assert.equal(vcp.status.textContent,'מכסת הדקות הסתיימה');assert.deepEqual(spoken,['מכסת הדקות הסתיימה']);
assert(requests.every(r=>r[0].startsWith('/api/voice-copilot/')));
''')


def test_hidden_screen_pauses_upload_and_heartbeat_then_resumes_capture():
    run(r'''
vcpPage({settings:{voice_copilot:true}});vcp.id='call3';vcp.ctx=context();vcp.mic=mic;
vcpCapture('call3',vcp.generation);vcpProgress();vcp.heartbeat=77;
document.hidden=true;callbacks.visibilitychange();assert.equal(vcp.id,'call3');assert.equal(vcp.processor,null);assert.equal(vcp.heartbeat,null);
document.hidden=false;callbacks.visibilitychange();assert(vcp.processor);assert(vcp.heartbeat);
vcpStop();assert.equal(tracksStopped,1);
let resolveMic;window.AudioContext=function(){return Object.assign(context(),{resume:()=>Promise.resolve()});};
Object.defineProperty(global,"navigator",{value:{mediaDevices:{getUserMedia:()=>new Promise(r=>resolveMic=r)}},configurable:true});
let pending=vcpStart();vcpStop();resolveMic(mic);await pending;
assert.equal(tracksStopped,2);assert(!requests.some(r=>r[0].endsWith('/start')));
''')


def test_watchdog_reconnect_state_then_clear_stopped_state():
    run(r'''
vcpPage({settings:{voice_copilot:true}});vcp.id='stalled';vcp.ctx=context();vcp.mic=mic;vcp.reconnects=0;
vcpStart=async function(){requests.push(['start-called']);};
let reconnect=vcpReconnect();assert.equal(vcp.state,'reconnecting');assert.equal(vcp.button.dataset.state,'reconnecting');
assert.equal(vcp.id,'');assert.equal(tracksStopped,1);assert.equal(requests[0][0],'/api/voice-copilot/stop');
await reconnect;assert.equal(requests[1][0],'start-called');
vcp.id='second-stall';vcp.ctx=context();vcp.mic=mic;vcpReconnect();
assert.equal(vcp.state,'stopped');assert.equal(vcp.button.dataset.state,'stopped');assert.equal(vcp.button.textContent,vcpCopy().continue);
''')


def test_stalled_event_read_cancels_reader_and_keeps_abort_link():
    run(r'''
vcpPage({settings:{voice_copilot:true}});vcp.id='stream';let cancelled=0,linkedSignal;
global.fetch=async function(url,options){linkedSignal=options.signal;return {ok:true,body:{getReader:()=>({
 read:()=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true})),
 cancel:()=>{cancelled++;return Promise.resolve();}})}};};
let outer=new AbortController(),pending=vcpEvents('stream',outer.signal,vcp.generation);
await new Promise(r=>setTimeout(r,0));outer.abort();assert.equal(linkedSignal.aborted,true);
vcp.id='';await assert.rejects(pending);assert(cancelled>=1);
let timedCancel=0;await assert.rejects(vcpRead({read:()=>new Promise(()=>{}),cancel:()=>{timedCancel++;}},1));
assert.equal(timedCancel,1);
''')


def test_button_tap_starts_fresh_never_as_reconnect():
    run(r'''
vcpPage({settings:{voice_copilot:true}});let args=[];
vcpStart=async function(){args.push([...arguments]);};
vcp.button.click({type:'click'});
assert.deepEqual(args,[[false]]);
''')
