// --- Request an app (Tools > More) ------------------------------------------------------------------------------
// One pasted App Store link goes to /api/app-request; the page only shows the answer. The tile exists only when the
// bridge reports `app_request` (the approval command is configured).
var AR_STATES={approved:["check","arApproved"],denied:["x","arDenied"],review:["clock","arReview"],pending:["clock","arPending"],
 error:["x","arError"],bad:["x","arBad"],rate:["clock","arRate"]};
var arViewer=null;
function arTime(iso){var d=iso?new Date(iso):null;if(!d||!Number.isFinite(d.getTime()))return "";
 return new Intl.DateTimeFormat(LANG,{timeStyle:"short"}).format(d);}
function arFill(template,key,value){var out=el("span"),parts=template.split("{"+key+"}");out.appendChild(document.createTextNode(parts[0]));
 if(parts.length>1){out.appendChild(el("bdi",null,value));out.appendChild(document.createTextNode(parts.slice(1).join("{"+key+"}")));}return out;}
function arCard(res){
 var st=AR_STATES[res.state]?res.state:"error",c=el("div","ar-result "+st),head=el("div","ar-head");
 c.setAttribute("data-ar-state",st);head.appendChild(icon(AR_STATES[st][0]));
 var title=el("b","ar-title");title.appendChild(arFill(V2.tools[AR_STATES[st][1]],"n",String(res.retry||0)));head.appendChild(title);c.appendChild(head);
 if(st==="approved"){var when=arTime(res.until);
  c.appendChild(when?(function(){var u=el("div","ar-until");u.appendChild(arFill(V2.tools.arUntil,"time",when));return u;})():el("div","ar-until",V2.tools.arOpenNow));}
 if(st==="pending")c.appendChild(el("div","ar-note",V2.tools.arPendingHint));
 if(st==="error")c.appendChild(el("div","ar-note",V2.tools.arErrorHint));
 if(res.app){var app=el("div","ar-line ar-app");app.appendChild(el("small",null,V2.tools.arAppLabel));var name=el("bdi",null,res.app);name.dir="auto";app.appendChild(name);c.appendChild(app);}
 var why=res.reason_he||res.reason;
 if(why){var line=el("div","ar-line ar-reason");line.appendChild(el("small",null,V2.tools.arReasonLabel));var text=el("bdi",null,why);text.dir="auto";line.appendChild(text);c.appendChild(line);}
 return c;}
function arOutcome(status,body){
 if(status===200&&body&&body.result)return body.result;
 if(status===400)return {state:"bad"};
 if(status===429)return {state:"rate",retry:(body&&body.retry)||10};
 return {state:"error"};}
function arOpen(opener){
 if(arViewer){arViewer.close();arViewer=null;}
 var dialog=el("dialog","topic-sheet ar-sheet"),head=el("div","topic-heading"),title=el("h2",null,V2.tools.arTitle),close=el("button","btn topic-close",V2.tools.arClose);
 dialog.dir=document.documentElement.dir;title.id="ar-sheet-title";dialog.setAttribute("aria-labelledby",title.id);dialog.setAttribute("data-v2-section","app-request");
 close.type="button";head.appendChild(title);head.appendChild(close);dialog.appendChild(head);
 dialog.appendChild(el("p","ar-hint",V2.tools.arHint));
 var ta=el("textarea","ar-field");ta.rows=2;ta.maxLength=500;ta.placeholder=V2.tools.arPlaceholder;ta.setAttribute("aria-label",V2.tools.arTitle);
 ta.setAttribute("autocapitalize","off");ta.setAttribute("autocomplete","off");ta.setAttribute("autocorrect","off");ta.setAttribute("spellcheck","false");ta.setAttribute("enterkeyhint","send");
 var fit=tune(ta,true);dialog.appendChild(ta);
 var row=el("div","ar-actions"),send=el("button","btn pri ar-send");send.type="button";send.disabled=true;send.appendChild(icon("send"));
 var label=el("span",null,V2.tools.arSend);send.appendChild(label);row.appendChild(send);
 var paste=null;
 if(navigator.clipboard&&typeof navigator.clipboard.readText==="function"){paste=el("button","btn sm ar-paste",V2.tools.arPaste);paste.type="button";row.insertBefore(paste,send);}
 dialog.appendChild(row);
 var out=el("div","ar-out");out.setAttribute("aria-live","polite");dialog.appendChild(out);
 var busy=false;
 function sync(){send.disabled=busy||!ta.value.trim();}
 function show(res){out.textContent="";out.appendChild(arCard(res));}
 function go(){var text=ta.value.trim();if(!text||busy)return;busy=true;ta.disabled=true;send.disabled=true;label.textContent=V2.tools.arSending;out.textContent="";haptic("tap");
  var done=function(res){busy=false;ta.disabled=false;label.textContent=V2.tools.arSend;sync();show(res);haptic("n",res.state==="approved"?"success":"warning");
   if(res.state==="approved"||res.state==="review"||res.state==="pending"||res.state==="denied"){ta.value="";fit();sync();}};
  post("/api/app-request",{text:text}).then(function(r){
   if(r.status===403){busy=false;ta.disabled=false;label.textContent=V2.tools.arSend;sync();toast(S.reopen,true);return;}
   return r.json().catch(function(){return {};}).then(function(j){done(arOutcome(r.status,j));});})
  .catch(function(){done({state:"error"});});}
 ta.addEventListener("input",sync);
 ta.addEventListener("keydown",function(e){if(e.key==="Enter"&&(e.metaKey||e.ctrlKey)){e.preventDefault();go();}});
 send.addEventListener("click",go);
 if(paste)paste.addEventListener("click",function(){navigator.clipboard.readText().then(function(t){if(t){ta.value=t.slice(0,500);fit();sync();ta.focus();}}).catch(function(){ta.focus();});});
 document.body.appendChild(dialog);var oldOverflow=document.body.style.overflow;
 var setOpen=bindBackLayer("app-request",dialog,false,function(on){
  if(on){if(!dialog.open){document.body.style.overflow="hidden";dialog.showModal();ta.focus();}}
  else{if(dialog.open)dialog.close();document.body.style.overflow=oldOverflow;if(opener&&opener.isConnected)opener.focus();}});
 arViewer={close:function(){setOpen(false);dialog.remove();}};
 close.addEventListener("click",function(){setOpen(false);});
 dialog.addEventListener("cancel",function(e){e.preventDefault();setOpen(false);});setOpen(true);sync();return dialog;}
function v2AppRequestTile(){
 var b=el("button","v2-app v2-app-request");b.type="button";b.setAttribute("data-v2-app-request","1");b.setAttribute("aria-haspopup","dialog");
 var ic=el("span","v2-head-icon");ic.appendChild(icon("v2phone"));b.appendChild(ic);
 var t=el("span","v2-app-text");t.appendChild(el("b",null,V2.tools.arTile));t.appendChild(el("small",null,V2.tools.arTileHint));b.appendChild(t);b.appendChild(v2Chev());
 b.addEventListener("click",function(){haptic("sel");arOpen(b);});return b;}
ICONS.v2phone="M8 2h8a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2M10 5h4M12 18.5h.01";
