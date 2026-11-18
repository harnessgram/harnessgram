// Tower protocol v1. Only signed same-origin requests; never direct Google access or client tools.
var vcp={node:null,button:null,status:null,log:null,pendingBox:null,id:"",starting:false,generation:0,ctx:null,mic:null,
 source:null,processor:null,gain:null,abort:null,playing:[],next:0,pending:0,sending:Promise.resolve(),turn:"",speaker:"",
 state:"idle",heartbeat:null,watchdog:null,lastProgress:0,reconnects:0};
function vcpCopy(){return V2_COPY[LANG].voiceCopilot;}
function vcpStatus(text){if(vcp.status)vcp.status.textContent=text;}
function vcpState(state,status){vcp.state=state;if(vcp.button){vcp.button.dataset.state=state;vcp.button.setAttribute("aria-pressed",state==="live"?"true":"false");
 var c=vcpCopy();vcp.button.textContent=state==="idle"?c.talk:state==="connecting"?c.connectingShort:state==="live"?c.stop:state==="reconnecting"?c.reconnectingShort:c.continue;}
 if(status)vcpStatus(status);}
function vcpProgress(){vcp.lastProgress=Date.now();}
function vcpRequest(action,body,signal,timeout){
 var controller=new AbortController(),timer=setTimeout(function(){controller.abort();},timeout||10000),off=function(){controller.abort();},released=false;
 if(signal){if(signal.aborted)controller.abort();else signal.addEventListener("abort",off,{once:true});}
 return fetch("/api/voice-copilot/"+action,{method:"POST",signal:controller.signal,
  headers:{"X-Telegram-Init-Data":wa&&wa.initData||"","Content-Type":"application/json"},body:JSON.stringify(body)})
  .then(function(r){clearTimeout(timer);r.vcpRelease=function(){if(released)return;released=true;if(signal)signal.removeEventListener("abort",off);};return r;},
   function(e){clearTimeout(timer);if(signal)signal.removeEventListener("abort",off);throw e;});
}
async function vcpJson(action,body){var r=await vcpRequest(action,body,null,10000);try{var j=await r.json();if(!r.ok||!j.ok)throw new Error(j.message||vcpCopy().failed);return j;}finally{if(r.vcpRelease)r.vcpRelease();}}
function vcpRead(reader,ms){return new Promise(function(resolve,reject){var settled=false,timer=setTimeout(function(){
  if(settled)return;settled=true;Promise.resolve(reader.cancel()).catch(function(){});reject(new Error(vcpCopy().slow));},ms);
 reader.read().then(function(value){if(settled)return;settled=true;clearTimeout(timer);resolve(value);},function(error){if(settled)return;settled=true;clearTimeout(timer);reject(error);});});}
function vcpTranscript(who,text){
 if(!vcp.log)return;
 if(vcp.speaker!==who||!vcp.turn){var row=el("p","vcp-line"),label=el("b",null,who==="owner"?vcpCopy().you:vcpCopy().title);
  row.appendChild(label);vcp.turn=el("span");vcp.turn.dir="auto";row.appendChild(vcp.turn);vcp.log.appendChild(row);vcp.speaker=who;
  while(vcp.log.children.length>60)vcp.log.removeChild(vcp.log.firstChild);}
 vcp.turn.textContent=(vcp.turn.textContent+text).slice(-4000);vcp.log.scrollTop=vcp.log.scrollHeight;
}
function vcpSilence(){vcp.playing.forEach(function(s){try{s.stop();}catch(e){}});vcp.playing=[];vcp.next=0;}
function vcpResume(){if(vcp.ctx&&vcp.ctx.state==="suspended")vcp.ctx.resume().then(vcpProgress).catch(function(){});}
function vcpPlay(data,rate){
 if(!vcp.ctx||rate!==24000)return;vcpResume();
 var bytes=atob(data);if(bytes.length%2)return;
 var buf=vcp.ctx.createBuffer(1,bytes.length/2,rate),samples=buf.getChannelData(0);
 for(var i=0;i<samples.length;i++){var n=bytes.charCodeAt(i*2)|(bytes.charCodeAt(i*2+1)<<8);samples[i]=(n>=32768?n-65536:n)/32768;}
 var source=vcp.ctx.createBufferSource();source.buffer=buf;source.connect(vcp.ctx.destination);
 vcp.next=Math.max(vcp.ctx.currentTime+0.025,vcp.next);if(vcp.next-vcp.ctx.currentTime>8){vcpReconnect();return;}
 source.start(vcp.next);vcp.next+=buf.duration;vcp.playing.push(source);vcpProgress();
 source.onended=function(){var at=vcp.playing.indexOf(source);if(at>=0)vcp.playing.splice(at,1);source.disconnect();vcpProgress();};
}
function vcpSpeakMessage(text){if(!text||!window.speechSynthesis||!window.SpeechSynthesisUtterance)return;
 try{var utter=new window.SpeechSynthesisUtterance(text);utter.lang="he-IL";window.speechSynthesis.speak(utter);}catch(e){}}
function vcpCleanup(notify){
 var id=vcp.id;vcp.id="";vcp.starting=false;vcp.generation++;
 clearInterval(vcp.heartbeat);clearInterval(vcp.watchdog);vcp.heartbeat=vcp.watchdog=null;
 if(vcp.abort)vcp.abort.abort();vcp.abort=null;
 if(vcp.processor){vcp.processor.onaudioprocess=null;vcp.processor.disconnect();}vcp.processor=null;
 if(vcp.source)vcp.source.disconnect();vcp.source=null;if(vcp.gain)vcp.gain.disconnect();vcp.gain=null;
 if(vcp.mic)vcp.mic.getTracks().forEach(function(t){t.onended=null;t.stop();});vcp.mic=null;
 vcpSilence();if(vcp.ctx){vcp.ctx.close().catch(function(){});vcp.ctx=null;}
 if(notify&&id)return vcpRequest("stop",{session:id}).then(function(r){if(r.vcpRelease)r.vcpRelease();},function(){});
 return Promise.resolve();
}
function vcpStop(message,speak){vcpCleanup(true);vcp.reconnects=0;vcpState(message?"stopped":"idle",message||vcpCopy().idle);if(speak)vcpSpeakMessage(message);}
async function vcpReconnect(){
 if(!vcp.id&&!vcp.starting)return;
 if(vcp.reconnects>=1){vcpStop(vcpCopy().stopped,true);return;}
 vcp.reconnects++;vcpState("reconnecting",vcpCopy().reconnecting);await vcpCleanup(true);
 if(vcp.state==="reconnecting")vcpStart(true);
}
function vcpTimers(id,generation){
 clearInterval(vcp.heartbeat);clearInterval(vcp.watchdog);vcpProgress();
 if(!document.hidden)vcp.heartbeat=setInterval(function(){if(vcp.id!==id||generation!==vcp.generation)return;
  vcpJson("heartbeat",{session:id}).then(vcpProgress).catch(function(){vcpReconnect();});},5000);
 vcp.watchdog=setInterval(function(){if(vcp.id===id&&generation===vcp.generation&&!document.hidden&&Date.now()-vcp.lastProgress>12000)vcpReconnect();},2000);
}
async function vcpEvents(id,signal,generation){
 var r=await vcpRequest("events",{session:id},signal,15000);if(!r.ok){try{var j=await r.json();throw new Error(j.message||vcpCopy().failed);}finally{if(r.vcpRelease)r.vcpRelease();}}
 var reader=r.body.getReader(),decode=new TextDecoder(),text="";
 try{while(vcp.id===id&&generation===vcp.generation){var chunk=await vcpRead(reader,12000);
  if(chunk.done)break;vcpProgress();text+=decode.decode(chunk.value,{stream:true});if(text.length>1000000)throw new Error(vcpCopy().failed);
  var end;while((end=text.indexOf("\n\n"))>=0){var line=text.slice(0,end);text=text.slice(end+2);if(line.indexOf("data: ")!==0)continue;
   var e=JSON.parse(line.slice(6));if(vcp.id!==id||generation!==vcp.generation)return;
   if(e.type==="ready"){vcpState("live",vcpCopy().listening);vcpCapture(id,generation);}
   else if(e.type==="reconnecting")vcpState("reconnecting",vcpCopy().reconnecting);
   else if(e.type==="audio")vcpPlay(e.data,e.rate);
   else if(e.type==="transcript")vcpTranscript(e.speaker,e.text);
   else if(e.type==="interrupted")vcpSilence();
   else if(e.type==="turn_complete"){vcp.turn="";vcp.speaker="";}
   else if(e.type==="relay")vcpStatus(e.queued?vcpCopy().queued:vcpCopy().notQueued);
   else if(e.type==="closed"){vcpStop(e.message,true);return;}
  }}}finally{try{await reader.cancel();}catch(e){}if(r.vcpRelease)r.vcpRelease();}
 if(vcp.id===id)vcpReconnect();
}
function vcpPauseCapture(){
 clearInterval(vcp.heartbeat);vcp.heartbeat=null;
 if(vcp.processor){vcp.processor.onaudioprocess=null;vcp.processor.disconnect();}vcp.processor=null;
 if(vcp.source)vcp.source.disconnect();vcp.source=null;if(vcp.gain)vcp.gain.disconnect();vcp.gain=null;
}
function vcpCapture(id,generation){
 if(vcp.processor||!vcp.ctx||!vcp.mic)return;vcp.source=vcp.ctx.createMediaStreamSource(vcp.mic);
 vcp.processor=vcp.ctx.createScriptProcessor(4096,1,1);vcp.gain=vcp.ctx.createGain();vcp.gain.gain.value=0;
 var sum=0,count=0,phase=0,ratio=vcp.ctx.sampleRate/16000;vcp.pending=0;vcp.sending=Promise.resolve();
 vcp.processor.onaudioprocess=function(e){if(vcp.id!==id||generation!==vcp.generation)return;vcpProgress();
  var input=e.inputBuffer.getChannelData(0),pcm=[];for(var i=0;i<input.length;i++){sum+=input[i];count++;phase++;if(phase>=ratio){pcm.push(Math.max(-32768,Math.min(32767,Math.round(sum/count*32768))));phase-=ratio;sum=0;count=0;}}
  var bytes="";pcm.forEach(function(n){bytes+=String.fromCharCode(n&255,(n>>8)&255);});if(++vcp.pending>8){vcpReconnect();return;}var data=btoa(bytes);
  vcp.sending=vcp.sending.then(async function(){if(vcp.id!==id||generation!==vcp.generation)return;await vcpJson("audio",{session:id,data:data});})
   .catch(function(){if(generation===vcp.generation)vcpReconnect();}).finally(function(){if(generation===vcp.generation)vcp.pending--;});};
 vcp.source.connect(vcp.processor);vcp.processor.connect(vcp.gain);vcp.gain.connect(vcp.ctx.destination);
}
async function vcpStart(reconnecting){
 if((vcp.id||vcp.starting)&&!reconnecting){vcpStop();return;}if(!reconnecting)vcp.reconnects=0;var generation=++vcp.generation;vcp.starting=true;
 vcpState(reconnecting?"reconnecting":"connecting",reconnecting?vcpCopy().reconnecting:vcpCopy().connecting);
 try{if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia)throw new Error(vcpCopy().mic);
  var Audio=window.AudioContext||window.webkitAudioContext;if(!Audio)throw new Error(vcpCopy().mic);vcp.ctx=new Audio();var resumed=vcp.ctx.resume();
  var mic=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
  if(generation!==vcp.generation){mic.getTracks().forEach(function(t){t.stop();});return;}vcp.mic=mic;
  mic.getTracks().forEach(function(t){t.onended=function(){if(vcp.id&&generation===vcp.generation)vcpReconnect();};});await resumed;
  var j=await vcpJson("start",{});if(generation!==vcp.generation){vcpRequest("stop",{session:j.session}).catch(function(){});return;}
  vcp.id=j.session;vcp.starting=false;vcp.abort=new AbortController();vcpTimers(j.session,generation);
  vcpEvents(j.session,vcp.abort.signal,generation).catch(function(){if(generation===vcp.generation)vcpReconnect();});
 }catch(err){if(generation===vcp.generation)vcpStop(err.name==="NotAllowedError"?vcpCopy().mic:err.message||vcpCopy().failed,true);}
}
function vcpPage(d){if(!(d.settings||{}).voice_copilot)return;if(!vcp.node){var s=vcpCopy();vcp.node=el("section","vcp-page");vcp.node.setAttribute("data-v2-section","voice-copilot");
 vcp.node.appendChild(el("h2",null,s.title));vcp.node.appendChild(el("p","vcp-hint",s.hint));vcp.button=el("button","vcp-talk",s.talk);vcp.button.type="button";
 vcp.button.setAttribute("aria-pressed","false");vcp.button.addEventListener("click",function(){vcpStart(false);});vcp.node.appendChild(vcp.button);vcp.status=el("p","vcp-status",s.idle);vcp.status.setAttribute("role","status");vcp.node.appendChild(vcp.status);
 vcp.node.appendChild(el("p","vcp-hint",s.privacy));vcp.pendingBox=el("div","vcp-pending");vcp.node.appendChild(vcp.pendingBox);vcp.log=el("div","vcp-transcript");vcp.log.setAttribute("role","log");vcp.log.setAttribute("aria-label",s.transcript);vcp.node.appendChild(vcp.log);vcpState("idle");}app.appendChild(vcp.node);}
function vcpSync(d){if((vcp.id||vcp.starting)&&(!(d.settings||{}).voice_copilot||view!=="tools"||v2ToolSeg(d)!=="tower"))vcpStop();
 if(vcp.pendingBox){var box=vcp.pendingBox,copy=vcpCopy();box.textContent="";(d.tower_pending||[]).forEach(function(p){var card=el("div","v2-card"),label=el("p",null,(p.kind==="shopping"?copy.pendingShopping:p.kind==="day_plan"?copy.pendingDayPlan:copy.pendingReminder)+p.text),b=el("button",null,copy.confirm);card.appendChild(label);if(p.kind==="day_plan"){var list=el("ul","vcp-day-plan");(p.blocks||[]).forEach(function(block){var li=el("li"),at=el("bdi",null,block.time),title=el("bdi",null,block.title);at.dir="ltr";title.dir="auto";li.appendChild(at);li.appendChild(title);list.appendChild(li);});card.appendChild(list);}b.type="button";b.addEventListener("click",function(){vcpJson("confirm",{id:p.id,digest:p.digest}).then(function(){card.remove();}).catch(function(e){vcpStatus(e.message);});});card.appendChild(b);box.appendChild(card);});}}
window.addEventListener("pagehide",function(){vcpStop();});
document.addEventListener("visibilitychange",function(){if(document.hidden){vcpPauseCapture();return;}vcpResume();
 var stale=(vcp.id||vcp.starting)&&Date.now()-vcp.lastProgress>12000;if(stale){vcpReconnect();return;}
 if(vcp.id){vcpTimers(vcp.id,vcp.generation);vcpCapture(vcp.id,vcp.generation);}});
window.addEventListener("focus",vcpResume);document.addEventListener("touchstart",vcpResume,{passive:true});
