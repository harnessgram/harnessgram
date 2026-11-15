// --- "My schedule": the personal schedule card on Now -------------------------------------------------------------------
// The data comes from POST /api/schedule-view (owner login, never part of the state poll); changes go to
// POST /api/schedule and answer with the new view. The bridge decides what falls on Today and Tomorrow; this page only
// shows it. Calendar events (schedule_sources, Google) are read-only rows without a done button.
// Google Calendar (google_calendar.py): "Connect" asks for a one-time link and opens it in the phone's browser; when the
// owner comes back to the Cockpit the card reloads and shows "Connected" with a one-tap "Disconnect".
var scData=null,scLoading=false,scAt=0,scTab="today",scErr=0,scViewer=null,scBusy={},scGoogleWait=false;
function scT(k){return (V2.schedule||{})[k]||k;}
function scRerender(){if(last&&view==="me")render(last,true);}
// A failed load is not retried until the next minute, so a render can never loop on an error.
function scLoad(force){
 if(scLoading||(!force&&scAt&&Date.now()-scAt<60000))return;scLoading=true;
 post("/api/schedule-view",{}).then(function(r){if(!r.ok)throw r.status||1;return r.json();})
  .then(function(j){scData=j.view||null;scErr=scData?0:1;})
  .catch(function(st){scErr=st||1;if(st===403)toast(S.reopen,true);})
  .then(function(){scLoading=false;scAt=Date.now();scRerender();});}
function scTake(j){if(j&&j.view){scData=j.view;scErr=0;scAt=Date.now();}}
function scTimeLabel(it){return it.allday?scT("allDay"):it.late?scT("late"):it.time||scT("noTime");}
function scToggle(it,btn){
 if(scBusy[it.id])return;scBusy[it.id]=1;btn.disabled=true;haptic("tap");
 post("/api/schedule",{action:"done",id:it.id,done:!it.done}).then(function(r){if(!r.ok)throw r.status;return r.json();})
  .then(function(j){delete scBusy[it.id];scTake(j);haptic("n","success");scRerender();})
  .catch(function(st){delete scBusy[it.id];btn.disabled=false;haptic("n","error");toast(st===403?S.reopen:scT("failed"),true);});}
function scRow(it){
 var li=el("li","sc-row"+(it.done?" done":"")+(it.kind==="cal"?" cal":""));li.setAttribute("data-sc-item",it.id||"cal");
 var when=el("span","sc-time"+(it.late?" late":""));if(it.kind==="cal")when.appendChild(icon("v2calendar"));when.appendChild(el("bdi",null,scTimeLabel(it)));li.appendChild(when);
 var title=el("bdi","sc-title",it.title);title.dir="auto";li.appendChild(title);
 if(it.kind==="cal"){li.setAttribute("aria-label",scT("calendar")+": "+it.title);return li;}
 var b=el("button","sc-check"+(it.done?" on":""));b.type="button";b.setAttribute("data-sc-done",it.id);b.setAttribute("aria-pressed",String(!!it.done));
 b.setAttribute("aria-label",(it.done?scT("undo"):scT("done"))+": "+it.title);b.appendChild(icon(it.done?"v2undo":"check"));
 b.addEventListener("click",function(){scToggle(it,b);});li.appendChild(b);return li;}
function scOpenUrl(u){if(typeof u!=="string"||!/^https:\/\//i.test(u))return false;
 if(typeof openSafe==="function")openSafe(u);else{try{window.open(u,"_blank","noopener");}catch(e){return false;}}return true;}
function scGoogle(g){
 var box=el("div","sc-google"+(g.connected?" on":""));box.setAttribute("data-sc-google",g.connected?"connected":"connect");
 var label=el("div","sc-g-label");label.appendChild(icon("v2calendar"));
 var text=el("div","sc-g-text");text.appendChild(el("span","sc-g-name",scT("googleName")));
 text.appendChild(el("span","sc-g-state",g.connected?scT("googleConnected"):scT("googleHint")));label.appendChild(text);box.appendChild(label);
 var b=el("button","btn sm"+(g.connected?" sc-g-off":" pri sc-g-on"),g.connected?scT("googleDisconnect"):scT("googleConnect"));b.type="button";
 b.setAttribute("data-sc",g.connected?"google-disconnect":"google-connect");
 b.addEventListener("click",function(){if(b.disabled)return;b.disabled=true;haptic("tap");
  var action=g.connected?"google-disconnect":"google-connect";
  post("/api/schedule",{action:action}).then(function(r){
   if(r.status===403){b.disabled=false;toast(S.reopen,true);return;}
   return r.json().catch(function(){return {};}).then(function(j){b.disabled=false;
    if(!r.ok||!j.ok)throw r.status||1;
    if(action==="google-connect"){if(scOpenUrl(j.url)){scGoogleWait=true;toast(scT("googleOpen"));}else toast(scT("googleFailed"),true);return;}
    scTake(j);haptic("n","success");toast(scT("googleDisconnected"));scRerender();});})
  .catch(function(){b.disabled=false;haptic("n","error");toast(scT("googleFailed"),true);});});
 box.appendChild(b);return box;}
// Back from the browser's sign-in: read the card again, so "Connected" shows at once.
if(typeof document!=="undefined"&&document.addEventListener)document.addEventListener("visibilitychange",function(){
 if(scGoogleWait&&!document.hidden){scGoogleWait=false;scLoad(true);}});
function scList(box){
 box.textContent="";
 if(!scData){var f=el("div","v2-empty sc-note",scErr?scT("loadFailed"):"…");f.setAttribute("data-sc-state",scErr?"error":"loading");box.appendChild(f);
  if(scErr){var r=el("button","btn sm sc-retry",scT("retry"));r.type="button";r.addEventListener("click",function(){haptic("sel");scLoad(true);scRerender();});box.appendChild(r);}return;}
 var rows=(scData.days&&scData.days[scTab])||[];
 if(!rows.length){var e=el("div","v2-empty sc-empty",scT(scTab==="today"?"empty":"emptyTomorrow"));e.setAttribute("data-sc-state","empty");box.appendChild(e);}
 else{var ul=el("ul","sc-rows");rows.forEach(function(it){ul.appendChild(scRow(it));});box.appendChild(ul);}
 if(scData.calendar&&scData.calendar.configured&&!scData.calendar.ok)box.appendChild(el("div","safe sc-warn",scT("calendarFailed")));
 if(scData.google&&scData.google.configured)box.appendChild(scGoogle(scData.google));}
function v2ScheduleCard(){
 scLoad(false);
 var card=el("section","v2-schedule");card.setAttribute("data-v2-section","schedule");card.setAttribute("aria-label",scT("title"));
 var head=el("header","sc-head"),h=el("h2","sc-heading");h.appendChild(icon("v2calendar"));h.appendChild(el("span",null,scT("title")));head.appendChild(h);
 var add=el("button","btn pri sc-add");add.type="button";add.setAttribute("data-sc","add");add.setAttribute("aria-haspopup","dialog");add.appendChild(icon("plus"));add.appendChild(el("span",null,scT("add")));
 add.addEventListener("click",function(){haptic("sel");scOpen(add);});head.appendChild(add);card.appendChild(head);
 var tabs=el("div","sc-tabs");tabs.setAttribute("role","tablist");tabs.setAttribute("aria-label",scT("tabs"));
 var box=el("div","sc-body");
 ["today","tomorrow"].forEach(function(day){
  var n=scData&&scData.open?scData.open[day]||0:0,b=el("button","sc-tab"+(scTab===day?" on":""));b.type="button";b.setAttribute("role","tab");b.setAttribute("data-sc-tab",day);b.setAttribute("aria-selected",String(scTab===day));
  b.appendChild(el("span",null,scT(day)));if(n)b.appendChild(el("span","sc-n",nf.format(n)));
  b.addEventListener("click",function(){scTab=day;haptic("sel");Array.prototype.forEach.call(tabs.children,function(x){var on=x===b;x.classList.toggle("on",on);x.setAttribute("aria-selected",String(on));});scList(box);});
  tabs.appendChild(b);});
 card.appendChild(tabs);scList(box);card.appendChild(box);
 if(typeof v2FamilyBlock==="function")card.appendChild(v2FamilyBlock());  // cockpit_v2_family.js
 return card;}
// The add sheet: one text field. An empty field takes the page's direction; typed text decides after.
function scOpen(opener){
 if(scViewer){scViewer.close();scViewer=null;}
 var dialog=el("dialog","topic-sheet sc-sheet"),head=el("div","topic-heading"),title=el("h2",null,scT("addTitle")),close=el("button","btn topic-close",scT("close"));
 dialog.dir=document.documentElement.dir;title.id="sc-sheet-title";dialog.setAttribute("aria-labelledby",title.id);dialog.setAttribute("data-v2-section","schedule-add");
 close.type="button";head.appendChild(title);head.appendChild(close);dialog.appendChild(head);
 dialog.appendChild(el("p","sc-hint",scT("hint")));
 var ta=el("textarea","sc-field");ta.rows=2;ta.maxLength=200;ta.placeholder=scT("placeholder");ta.setAttribute("aria-label",scT("addTitle"));ta.setAttribute("data-sc","field");
 ta.setAttribute("enterkeyhint","send");var fit=tune(ta,true);dialog.appendChild(ta);
 var row=el("div","sc-actions"),save=el("button","btn pri sc-save");save.type="button";save.disabled=true;save.setAttribute("data-sc","save");
 var label=el("span",null,scT("save"));save.appendChild(icon("check"));save.appendChild(label);row.appendChild(save);dialog.appendChild(row);
 var out=el("div","sc-out");out.setAttribute("aria-live","polite");dialog.appendChild(out);
 var busy=false,setOpen=null;
 function sync(){save.disabled=busy||!ta.value.trim();}
 function fail(text){busy=false;ta.disabled=false;label.textContent=scT("save");sync();out.textContent=text;haptic("n","error");}
 function go(){var text=ta.value.trim();if(!text||busy)return;busy=true;ta.disabled=true;save.disabled=true;label.textContent=scT("saving");out.textContent="";haptic("tap");
  post("/api/schedule",{action:"add",text:text}).then(function(r){
   if(r.status===403){fail("");toast(S.reopen,true);return;}
   return r.json().catch(function(){return {};}).then(function(j){
    if(r.status===200&&j.ok){scTake(j);if(j.item&&j.view&&j.item.date===j.view.tomorrow)scTab="tomorrow";else scTab="today";
     busy=false;ta.value="";haptic("n","success");setOpen(false);toast(scT("added"));scRerender();return;}
    fail(r.status===409?scT("full"):r.status===400?scT("bad"):scT("failed"));});})
  .catch(function(){fail(scT("failed"));});}
 ta.addEventListener("input",sync);
 ta.addEventListener("keydown",function(e){if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();go();}});
 save.addEventListener("click",go);
 document.body.appendChild(dialog);var oldOverflow=document.body.style.overflow;
 setOpen=bindBackLayer("schedule-add",dialog,false,function(on){
  if(on){if(!dialog.open){document.body.style.overflow="hidden";dialog.showModal();ta.focus();}}
  else{if(dialog.open)dialog.close();document.body.style.overflow=oldOverflow;if(opener&&opener.isConnected)opener.focus();}});
 scViewer={close:function(){setOpen(false);dialog.remove();}};
 close.addEventListener("click",function(){setOpen(false);});
 dialog.addEventListener("cancel",function(e){e.preventDefault();setOpen(false);});setOpen(true);sync();return dialog;}
ICONS.v2calendar="M4 6h16v14H4zM4 10h16M8 3v4M16 3v4";
ICONS.v2undo="M9 7L4 12l5 5M4 12h10a6 6 0 0 1 0 8h-3";
