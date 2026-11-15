// --- Mac maintenance (Tools > More) -----------------------------------------------------------------------------
// The owner's cleanup preview, real cleanup and weekly routine. The server runs the configured commands in a background job;
// this sheet only starts one and polls /api/mac-tools-status. The tile exists only when the bridge reports `mac_tools`.
// The real cleanup and the weekly routine each need a second tap that names what will happen; the cleanup also needs a
// preview from the last ten minutes, and the server checks both.
var MT_POLL_IDLE=15000,MT_POLL_BUSY=3000,MT_ARM_MS=10000,mtViewer=null;
var MT_KIND={"dry-run":"mtKindDry",clean:"mtKindClean",maintenance:"mtKindMaint"};
function mtFmt(template,vars){return template.replace(/\{(\w+)\}/g,function(m,k){return vars&&vars[k]!=null?String(vars[k]):m;});}
function mtSize(bytes){
 if(typeof bytes!=="number")return S.macUnavailable;
 var f=new Intl.NumberFormat(LANG,{maximumFractionDigits:1});
 return bytes<1073741824?f.format(Math.max(0.1,Math.round(bytes/1048576*10)/10))+" "+V2.tools.mtMb:f.format(Math.round(bytes/1073741824*10)/10)+" "+S.macGb;}
function mtClock(seconds){var m=Math.floor(seconds/60),s=seconds%60,f=new Intl.NumberFormat(LANG,{minimumIntegerDigits:2});return new Intl.NumberFormat(LANG).format(m)+":"+f.format(s);}
function mtWhen(epoch){
 if(typeof epoch!=="number")return "";var d=new Date(epoch*1000),now=new Date(),same=d.toDateString()===now.toDateString();
 return new Intl.DateTimeFormat(LANG,same?{timeStyle:"short"}:{dateStyle:"short",timeStyle:"short"}).format(d);}
function mtRow(label,value,status){var r=el("div","mac-row"),a=el("div"),v=el("div","mac-value"+(status?" mac-status "+status:""));a.appendChild(el("div",null,label));v.textContent=value;r.appendChild(a);r.appendChild(v);return r;}
function mtStatus(m){
 var c=el("section","mt-card mt-status"),pct=new Intl.NumberFormat(LANG,{style:"percent",maximumFractionDigits:0}),num=new Intl.NumberFormat(LANG,{maximumFractionDigits:1});
 c.setAttribute("data-mt","status");c.appendChild(el("h3",null,V2.tools.mtStatus));
 if(!m||!m.available){c.appendChild(el("div","mac-detail",V2.tools.mtUnavailable));return c;}
 var disk=m.disk,mem=m.memory,cpu=m.cpu;
 c.appendChild(mtRow(V2.tools.mtDisk,disk&&disk.total_bytes?mtSize(disk.free_bytes)+" · "+pct.format(disk.free_bytes/disk.total_bytes):S.macUnavailable,disk&&disk.status));
 c.appendChild(mtRow(V2.tools.mtMemory,mem&&mem.pressure_free_percent!=null?pct.format(mem.pressure_free_percent/100):S.macUnavailable,mem&&mem.status));
 c.appendChild(mtRow(V2.tools.mtLoad,cpu&&cpu.load?mtFmt(V2.tools.mtLoadValue,{load:num.format(Math.round(cpu.load[0]*10)/10),cores:num.format(cpu.cores)}):S.macUnavailable,cpu&&cpu.status));
 var kinds={test:S.macTest,encoder:S.macEncoder,agent:S.macAgent,browser:S.macBrowser,build:S.macBuild,other:S.macOther},list=m.processes&&m.processes.memory;
 var p=el("div","mac-processes");p.appendChild(el("b",null,V2.tools.mtTopMemory));
 if(!list||!list.length)p.appendChild(el("div","mac-detail",V2.tools.mtUnavailable));
 else list.slice(0,3).forEach(function(x){var r=el("div","mac-row");r.appendChild(el("span",null,kinds[x.kind]||S.macOther));r.appendChild(el("span","mac-value",mtSize(x.memory_bytes)));p.appendChild(r);});
 c.appendChild(p);return c;}
function mtDryCard(dry,fresh){
 var c=el("div","mt-dry"+(fresh?"":" old"));c.setAttribute("data-mt","dry");
 c.appendChild(el("div","mt-big",mtFmt(V2.tools.mtReclaim,{size:mtSize(dry.bytes)})));
 var meta=[];if(typeof dry.items==="number")meta.push(mtFmt(V2.tools.mtItems,{n:new Intl.NumberFormat(LANG).format(dry.items)}));
 if(dry.finished_at)meta.push(mtFmt(V2.tools.mtChecked,{time:mtWhen(dry.finished_at)}));
 if(meta.length)c.appendChild(el("div","mt-meta",meta.join(" · ")));
 if(dry.categories&&dry.categories.length){
  var list=el("div","mt-cats");list.appendChild(el("b",null,V2.tools.mtTopCats));
  dry.categories.forEach(function(x){var r=el("div","mac-row"),n=el("bdi",null,x.name);n.dir="auto";r.appendChild(n);r.appendChild(el("span","mac-value",mtSize(x.bytes)));list.appendChild(r);});
  c.appendChild(list);}
 if(!fresh)c.appendChild(el("div","mt-note",V2.tools.mtStale));
 return c;}
function mtLastCard(last,fresh){
 var c=el("section","mt-card mt-last"+(fresh?" fresh":""));c.setAttribute("data-mt","last");c.setAttribute("aria-live","polite");
 c.appendChild(el("h3",null,V2.tools.mtLast));
 if(!last){c.appendChild(el("div","mt-meta",V2.tools.mtNever));return c;}
 var st=last.state,box=el("div","mt-result "+(st==="ok"?"ok":st==="skipped"?"wait":"bad"));box.setAttribute("data-mt-state",st);
 box.appendChild(el("div","mt-big",V2.tools[MT_KIND[last.kind]]+" · "+mtWhen(last.finished_at)));
 var text;
 if(st==="ok")text=last.kind==="dry-run"?mtFmt(V2.tools.mtReclaim,{size:mtSize(last.bytes)}):last.kind==="clean"&&typeof last.bytes==="number"?mtFmt(V2.tools.mtFreed,{size:mtSize(last.bytes)}):V2.tools.mtDone;
 else if(st==="skipped")text=mtFmt(V2.tools.mtSkipped,{why:V2.tools["mtSkip_"+last.skipped]||V2.tools.mtSkip_busy});
 else text=V2.tools["mtState_"+st]||V2.tools.mtState_error;
 box.appendChild(el("div","mt-state",text));
 if(typeof last.free_before==="number"&&typeof last.free_after==="number"&&last.kind!=="dry-run")
  box.appendChild(el("div","mt-meta",mtFmt(V2.tools.mtFreeLine,{before:mtSize(last.free_before),after:mtSize(last.free_after)})));
 if(typeof last.duration==="number")box.appendChild(el("div","mt-meta",mtFmt(V2.tools.mtTook,{time:mtClock(last.duration)})));
 c.appendChild(box);return c;}
function mtOpen(opener,cfg){
 if(mtViewer){mtViewer.close();mtViewer=null;}
 var dialog=el("dialog","topic-sheet mt-sheet"),head=el("div","topic-heading"),title=el("h2",null,V2.tools.mtTitle),close=el("button","btn topic-close",V2.tools.mtClose);
 dialog.dir=document.documentElement.dir;title.id="mt-sheet-title";dialog.setAttribute("aria-labelledby",title.id);dialog.setAttribute("data-v2-section","mac-tools");
 close.type="button";head.appendChild(title);head.appendChild(close);dialog.appendChild(head);
 var body=el("div","mt-body");dialog.appendChild(body);
 var st=null,gotAt=0,armed="",armTimer=null,pollTimer=null,alive=true,busy=false,own="",justDone="",note="",noteBad=false;
 var acts=cfg||{};
 function freshNow(){return !!(st&&st.dry&&st.dry.fresh&&st.dry.age+(Date.now()-gotAt)/1000<st.fresh_seconds);}
 function disarm(){armed="";if(armTimer){clearTimeout(armTimer);armTimer=null;}}
 function arm(kind){disarm();armed=kind;armTimer=setTimeout(function(){armed="";draw();},MT_ARM_MS);}
 function running(){return !!(st&&st.job);}
 function say(text,bad){note=text;noteBad=!!bad;}
 function button(id,cls,label,on,off){var b=el("button","btn "+cls);b.type="button";b.setAttribute("data-mt",id);b.textContent=label;b.disabled=!!off;if(on)b.addEventListener("click",on);return b;}
 function draw(){
  var focus=document.activeElement&&document.activeElement.getAttribute?document.activeElement.getAttribute("data-mt"):"";
  body.textContent="";
  body.appendChild(mtStatus(st&&st.mac));
  var job=st&&st.job;
  if(job){var run=el("div","mt-running");run.setAttribute("data-mt","running");run.setAttribute("role","status");
   run.appendChild(el("b",null,mtFmt(V2.tools.mtRunning,{what:V2.tools[MT_KIND[job.kind]],time:mtClock(job.elapsed)})));run.appendChild(el("div","mt-meta",V2.tools.mtRunningHint));body.appendChild(run);}
  var out=el("div","mt-out");out.setAttribute("aria-live","polite");
  if(note){var n=el("div","mt-note-box"+(noteBad?" bad":""),note);n.setAttribute("data-mt","note");out.appendChild(n);}
  body.appendChild(out);
  var off=running()||busy,dry=st&&st.dry,fresh=freshNow();
  if(acts.dry_run){
   var check=el("section","mt-card mt-check");check.appendChild(el("h3",null,V2.tools.mtCheck));check.appendChild(el("p","mt-hint",V2.tools.mtCheckHint));
   check.appendChild(button("check","pri mt-check-btn",V2.tools.mtCheckBtn,function(){disarm();start("dry-run");},off));
   if(dry)check.appendChild(mtDryCard(dry,fresh));body.appendChild(check);}
  if(acts.clean){
   var cl=el("section","mt-card mt-clean");cl.appendChild(el("h3",null,V2.tools.mtClean));
   var isArmed=armed==="clean"&&fresh,can=fresh&&!off&&dry&&typeof dry.bytes==="number";
   var row=el("div","mt-row");
   row.appendChild(button("clean",isArmed?"arm mt-confirm":"mt-clean-btn",isArmed?mtFmt(V2.tools.mtCleanAsk,{size:mtSize(dry.bytes)}):V2.tools.mtCleanBtn,function(){
    if(!freshNow()){disarm();say(V2.tools.mtStale,true);draw();return;}
    if(armed==="clean"){var id=st.dry.id;disarm();start("clean",id);}else{arm("clean");haptic("sel");draw();}},!can));
   if(isArmed)row.appendChild(button("cancel","sm",V2.tools.mtCancel,function(){disarm();draw();}));
   cl.appendChild(row);
   var why=el("p","mt-hint",!dry?V2.tools.mtCleanNeed:!fresh?V2.tools.mtStale:V2.tools.mtCleanHint);why.setAttribute("data-mt","clean-hint");cl.appendChild(why);body.appendChild(cl);}
  if(acts.maintenance){
   var mt=el("section","mt-card mt-maint");mt.appendChild(el("h3",null,V2.tools.mtMaint));mt.appendChild(el("p","mt-hint",V2.tools.mtMaintHint));
   var mrow=el("div","mt-row"),mArmed=armed==="maintenance";
   mrow.appendChild(button("maintenance",mArmed?"arm mt-confirm":"mt-maint-btn",mArmed?V2.tools.mtMaintAsk:V2.tools.mtMaintBtn,function(){
    if(armed==="maintenance"){disarm();start("maintenance",true);}else{arm("maintenance");haptic("sel");draw();}},off));
   if(mArmed)mrow.appendChild(button("cancel","sm",V2.tools.mtCancel,function(){disarm();draw();}));
   mt.appendChild(mrow);body.appendChild(mt);}
  body.appendChild(mtLastCard(st&&st.last,!!justDone&&st&&st.last&&st.last.id===justDone));
  if(focus){var again=body.querySelector('[data-mt="'+focus+'"]');if(again&&again.focus&&!again.disabled)again.focus();}}
 function schedule(){if(pollTimer)clearTimeout(pollTimer);if(!alive)return;pollTimer=setTimeout(poll,running()||busy?MT_POLL_BUSY:MT_POLL_IDLE);}
 function poll(){
  if(!alive)return Promise.resolve();
  return post("/api/mac-tools-status",{}).then(function(r){
   if(!alive)return;
   if(r.status===403){toast(S.reopen,true);return;}
   if(r.status!==200){say(V2.tools.mtError,true);draw();schedule();return;}
   return r.json().then(function(j){
    var was=own;st=j;gotAt=Date.now();
    if(own&&!j.job&&j.last&&j.last.id===own){justDone=own;own="";say("",false);haptic("n",j.last.state==="ok"?"success":"warning");}
    else if(own&&!j.job&&!(j.last&&j.last.id===own)){own="";}  // the job is gone and left no report: stop waiting
    if(armed==="clean"&&!freshNow())disarm();
    draw();schedule();});
  }).catch(function(){if(alive){say(V2.tools.mtError,true);draw();schedule();}});}
 function start(action,confirm){
  if(busy||running())return;busy=true;say("",false);justDone="";haptic("tap");draw();
  var payload={action:action};if(confirm!==undefined)payload.confirm=confirm;
  post("/api/mac-tools",payload).then(function(r){
   if(r.status===403){busy=false;toast(S.reopen,true);draw();return;}
   return r.json().catch(function(){return {};}).then(function(j){
    busy=false;
    if(r.status===200&&j&&j.id){own=j.id;st=st||{};st.job={id:j.id,kind:action,elapsed:0};gotAt=Date.now();}
    else if(r.status===409)say(j.error==="busy"?V2.tools.mtBusy:V2.tools.mtStale,true);
    else say(V2.tools.mtError,true);
    draw();if(pollTimer)clearTimeout(pollTimer);pollTimer=setTimeout(poll,1000);});})
  .catch(function(){busy=false;say(V2.tools.mtError,true);draw();});}
 document.body.appendChild(dialog);var oldOverflow=document.body.style.overflow;
 var setOpen=bindBackLayer("mac-tools",dialog,false,function(on){
  if(on){if(!dialog.open){document.body.style.overflow="hidden";dialog.showModal();}}
  else{alive=false;disarm();if(pollTimer)clearTimeout(pollTimer);if(dialog.open)dialog.close();document.body.style.overflow=oldOverflow;if(opener&&opener.isConnected)opener.focus();}});
 mtViewer={close:function(){setOpen(false);dialog.remove();}};
 close.addEventListener("click",function(){setOpen(false);});
 dialog.addEventListener("cancel",function(e){e.preventDefault();setOpen(false);});
 draw();setOpen(true);poll();return dialog;}
function v2MacToolsTile(cfg){
 var b=el("button","v2-app v2-mac-tools");b.type="button";b.setAttribute("data-v2-mac-tools","1");b.setAttribute("aria-haspopup","dialog");
 var ic=el("span","v2-head-icon");ic.appendChild(icon("v2drive"));b.appendChild(ic);
 var t=el("span","v2-app-text");t.appendChild(el("b",null,V2.tools.mtTile));t.appendChild(el("small",null,V2.tools.mtTileHint));b.appendChild(t);b.appendChild(v2Chev());
 b.addEventListener("click",function(){haptic("sel");mtOpen(b,cfg);});return b;}
ICONS.v2drive="M3 14h18v6H3zM3 14l3-9h12l3 9M7 17h.01M11 17h4";
