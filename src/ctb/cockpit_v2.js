// Direction D is additive: reuse the existing authenticated actions and draft/back machinery.
function v2Enabled(d){return !!d&&d.ui_v2_enabled===true&&!!d.settings&&["preview","on"].indexOf(d.settings.ui_v2)>=0;}
function v2Image(name,cls){var img=el("img",cls);img.src=V2_ASSET+(["plane","book","books","visor","clapper","catalog","header","artwork","bell","gears","server","mic","laptop"].indexOf(name)>=0?name:"generic")+".webp";img.alt="";img.decoding="async";return img;}
function v2Heading(text,ic){var h=el("h2","v2-heading");if(ic)h.appendChild(icon(ic));h.appendChild(el("span",null,text));return h;}
function v2Prepare(d){
 var on=v2Enabled(d),root=document.documentElement,was=root.classList.contains("cockpit-v2");root.classList.toggle("cockpit-v2",on);
 var exit=document.getElementById("v2-exit");
 if(on&&!exit){exit=el("button","v2-exit");exit.id="v2-exit";exit.type="button";exit.setAttribute("aria-label",V2.shell.wayOut);exit.appendChild(icon("x"));exit.addEventListener("click",backTap);document.querySelector("header.top").appendChild(exit);}
 if(exit)exit.hidden=!on;
 if(on){document.getElementById("t").textContent="Cockpit";document.getElementById("sub").textContent=V2.shell.brand;}
 else if(was){document.getElementById("t").textContent=S.title;}
 // Move the same audio controls after the content. Never create a second audio element.
 var player=document.getElementById("radio-player")||V2_PLAYER,apps=document.getElementById("apps");
 if(on)document.body.insertBefore(player,apps);else document.body.insertBefore(player,app);
}
function v2TabForView(){return view==="me"?"me":view==="settings"?"settings":view==="tools"||appOf(view)?"tools":"agents";}
function v2Tabs(d){
 if(view==="all")view="me";
 if(!view||["here","me","agents","settings","tools"].indexOf(view)<0&&!appNamed(d,appOf(view)))view=d.topic?"here":"me";
 var box=document.getElementById("tabs"),nav=el("div","tabs"),selected=v2TabForView();box.textContent="";nav.setAttribute("role","tablist");
 [["me",V2.shell.now,"v2home"],["agents",V2.shell.projects,"v2folder"],["tools",V2.shell.tools,"v2tools"],["settings",V2.shell.settings,"v2settings"]].forEach(function(p){
  var b=el("button","tab"+(selected===p[0]?" on":"")),ic=el("span","tab-icon");b.type="button";b.setAttribute("role","tab");b.setAttribute("aria-selected",String(selected===p[0]));b.setAttribute("data-v2-tab",p[0]);
  ic.appendChild(icon(p[2]));var count=v2OpenCount(d);if(p[0]==="me"&&selected!=="me"&&count)ic.appendChild(el("span","tb",nf.format(count)));
  b.appendChild(ic);b.appendChild(el("span","tab-label",p[1]));b.addEventListener("click",function(){v2TabTap(d,p[0]);});nav.appendChild(b);
 });box.appendChild(nav);
}
// Ended or replaced decisions can no longer be answered. "old" alone is still answerable (classic rule), so it stays live.
function v2IsStale(x){return !!x.stale&&x.stale!=="old";}
function v2OpenCount(d){return (d.decisions||[]).filter(function(x){return !closed[x.id]&&!v2IsStale(x);}).length+(d.cards||[]).filter(function(c){return ASKS[c.kind]&&!c.ended&&!cardDone[c.id];}).length;}
function v2PageHead(title,ic,greeting){var h=el("div","v2-page-head"),i=el("span","v2-head-icon");i.appendChild(icon(ic));h.appendChild(i);var b=el("div");b.appendChild(el("h2",null,title));if(greeting)b.appendChild(el("div","v2-greeting",greeting));h.appendChild(b);app.appendChild(h);}
function v2Decision(d){
 var c=el("article","v2-decision");c.id="question-"+d.id;c.setAttribute("data-v2-decision",d.id);
 if(d.id===target)c.classList.add("question-highlight");
 var project=el("bdi","v2-decision-project",shown(d,"project_name")||shown(d,"topic")||S.waiting);c.appendChild(project);
 var q=el("p","v2-question",shown(d,"title"));q.dir="auto";c.appendChild(q);
 // Only explicit source text is a recommendation; never fabricate one from a status.
 var rec=shown(d,"recommendation"),detail=shown(d,"detail"),match=detail&&detail.match(/(?:המלצה|recommendation)\s*[:：]\s*([^\n]+)/i);
 if(!rec&&match)rec=match[1];var recommendation=el("div","v2-recommendation",rec?V2.now.recommendation+": "+rec:V2.now.noRecommendation);recommendation.dir="auto";c.appendChild(recommendation);
 // Approval conditions must stay visible before the answer controls, even
 // when the optional source, topic link and note disclosure is collapsed.
 // A detail that is nothing but the recommendation line is already shown in full above: show it once.
 if(detail&&!(match&&!detail.replace(match[0],"").trim())){var dt=el("div","detail",detail);dt.dir="auto";c.appendChild(dt);}
 // A Claude Code permission request: its exact text, as is (textContent, never translated, left to right).
 if(typeof d.request==="string"&&d.request){var rq=el("pre","pq-request",d.request);rq.dir="ltr";c.appendChild(rq);}
 var ta=el("textarea");ta.maxLength=300;ta.rows=2;ta.placeholder=S.notePh;ta.value=notes[d.id]||"";tune(ta,true);ta.addEventListener("input",function(){notes[d.id]=ta.value;syncClose();});
 var content=el("div");content.appendChild(el("div","safe",ago(d.age)+(d.old?" · "+V2.now.older:"")));
 source("v2"+d.id,[[d.title,he(d,"title")],[d.detail,he(d,"detail")]],content);
 if(d.url)content.appendChild(topicBtn(d.url));content.appendChild(ta);content.appendChild(el("div","safe",S.tapNote));
 var more=compactBlock("v2-decision:"+d.id,V2.now.details,"",content,"v2-detail",!!notes[d.id]);
 // Reuse the actual back-aware disclosure button. Moving it beside the picture
 // keeps aria-expanded correct even when native Back collapses the details.
 var picture=el("div","v2-picture");picture.appendChild(v2Image(d.project_asset));var mb=more.querySelector(".compact-toggle");
 mb.textContent="";mb.appendChild(el("span","v2-more",V2.now.more));mb.appendChild(el("span","v2-less",V2.now.less));picture.appendChild(mb);c.appendChild(picture);
 var buttons=el("div","v2-answer-buttons"),yes=el("button",null,shown(d,"yes")||V2.now.yes),no=el("button",null,shown(d,"no")||V2.now.no);
 [[yes,"yes"],[no,"no"]].forEach(function(p){p[0].type="button";p[0].setAttribute("data-v2-answer",p[1]);p[0].addEventListener("click",function(){answer(c,d,p[1],[yes,no],ta);});buttons.appendChild(p[0]);});c.appendChild(buttons);
 // An old but still-open decision stays answerable; the owner may also just clear it (same endpoint, no confirm).
 if(d.old){var rm=el("button","v2-old-remove",V2.now.remove);rm.type="button";rm.setAttribute("data-v2-dismiss",d.id);rm.addEventListener("click",function(){v2Dismiss([d.id],[rm,yes,no]);});c.appendChild(rm);}
 if(d.decline_only===true)yes.disabled=true;  // a request the phone cannot show exactly is approved at the computer
 if(d.stale&&d.stale!=="old"){yes.disabled=no.disabled=true;c.appendChild(el("div","err",S["why_"+d.stale]||V2.now.unavailable));}
 c.appendChild(more);return c;
}
// An agent's ask/choice card keeps its own answer controls; the picture is decoration.
function v2AskCard(c){var box=agentCard(c,false);box.classList.add("v2-card-decision");box.setAttribute("data-v2-decision",c.id);box.insertBefore(v2Image(c.project_asset,"v2-card-picture"),box.firstChild);return box;}
function v2Ring(p){
 var ring=el("span","v2-progress"),known=typeof p.percent==="number";ring.textContent=known?pf.format(p.percent/100):"–";
 ring.setAttribute("role","progressbar");ring.setAttribute("aria-valuemin","0");ring.setAttribute("aria-valuemax","100");if(known)ring.setAttribute("aria-valuenow",String(p.percent));else ring.setAttribute("aria-valuetext",V2.now.progressUnknown);
 ring.setAttribute("aria-label",known?pf.format(p.percent/100):V2.now.progressUnknown);
 var svg=document.createElementNS(NS,"svg");svg.setAttribute("viewBox","0 0 44 44");svg.setAttribute("aria-hidden","true");
 svgAdd(svg,"circle",{cx:22,cy:22,r:18});svgAdd(svg,"circle",{cx:22,cy:22,r:18,"stroke-dasharray":(known?p.percent/100*113.1:0)+" 113.1"});ring.appendChild(svg);return ring;
}
// The bridge computes the status; the picture's stamp only shows it.
function v2Badge(p){var badge=el("span","v2-status-badge");badge.setAttribute("aria-hidden","true");
 if(p.status==="waiting")badge.textContent="⚑";else if(p.status==="blocked")badge.textContent="☁";else if(p.status==="shipped")badge.appendChild(icon("check"));else badge.textContent="☀";return badge;}
// A raw session-title slug ("app-store-review-watch") reads as words; configured names are shown as given.
// Display only: the key, the data and every API call keep the original name.
function v2Name(p){var n=String(shown(p,"name")||"");if(!/^[\x21-\x7e]+$/.test(n)||!/[-_]/.test(n))return n;
 var w=n.replace(/[-_]+/g," ").trim();return w?w.charAt(0).toUpperCase()+w.slice(1):n;}
// Quiet: no update for a day and no reported progress. Waiting-for-you projects are never folded away.
function v2Quiet(p){return !!p.stale&&typeof p.percent!=="number"&&!p.waiting&&p.status!=="waiting";}
function v2ProjectOrder(list){var rank={waiting:0,blocked:1};
 return list.map(function(p,i){return [p,i];}).sort(function(a,b){var x=a[0],y=b[0],r=(x.status in rank?rank[x.status]:2)-(y.status in rank?rank[y.status]:2);
  if(r)return r;var ax=x.age==null?Infinity:x.age,ay=y.age==null?Infinity:y.age;return ax===ay?a[1]-b[1]:ax-ay;}).map(function(e){return e[0];});}
function v2Project(p){
 var b=el("button","v2-project");b.type="button";b.setAttribute("data-status",p.status);b.setAttribute("data-project",p.key);
 // The picture and its stamp; the ring sits beside the status lines, never on top of the picture.
 var art=el("span","v2-project-art");art.appendChild(v2Image(p.asset));art.appendChild(v2Badge(p));b.appendChild(art);
 var name=el("bdi","v2-project-name",v2Name(p));name.title=shown(p,"name");b.appendChild(name);
 var meta=el("span","v2-project-meta"),lines=el("span","v2-project-lines");meta.appendChild(v2Ring(p));
 lines.appendChild(el("span","v2-project-status",p.stale&&p.status==="on_track"?V2.now.staleUpdate:V2.now[p.status]));
 var age=p.age==null?V2.now.noUpdate:V2.now.age+" "+ago(p.age);lines.appendChild(el("span","v2-project-age",age));meta.appendChild(lines);b.appendChild(meta);
 b.title=[shown(p,"name"),shown(p,"summary"),p.stale?V2.now.staleUpdate:"",age].filter(Boolean).join(" · ");b.setAttribute("aria-description",b.title);
 b.addEventListener("click",function(){v2OpenProject(p.key);});return b;
}
function v2Changes(d){var row=el("div","v2-changes");row.appendChild(icon("v2announce"));row.appendChild(el("b",null,V2.now.changed));var text=V2.now.noChange;
 var since=(d.cockpit_v2||{}).since;
 if(since){var changes=[];if(since.done)changes.push(fill(since.done,S.sDone1,S.sDone));if(since.stuck)changes.push(fill(since.stuck,S.sStuck1,S.sStuck));if(since.new)changes.push(fill(since.new,S.sNew1,S.sNew));text=changes.join(" · ")||V2.now.noChange;}
 else if(digest&&digest.items.length)text=digest.items.map(function(i){return nf.format(i[0])+" "+plural(i[0],i[1],i[2]);}).join(" · ");
 else if(!digest)text=V2.now.firstVisit;row.appendChild(el("span","v2-change-text",text));return row;}
// Same call and body as the classic "clear all": the server only closes ids it itself counts as stale.
function v2Dismiss(ids,btns){
 haptic("tap");btns.forEach(function(b){b.disabled=true;});
 post("/api/dismiss",{ids:ids}).then(function(r){if(!r.ok)throw r.status;return r.json();})
  .then(function(j){(j.closed||[]).forEach(function(id){closed[id]=1;});haptic("n","success");toast(S.cleared+": "+nf.format((j.closed||[]).length));render(last,true);})
  .catch(function(st){haptic("n","error");btns.forEach(function(b){b.disabled=false;});toast(st===403?S.reopen:st===503?S.busy:S.failed,true);});
}
function v2StaleFold(stale){
 var body=el("div","v2-stale-body"),btns=[],all=el("button","v2-stale-all",V2.now.clearAll);all.type="button";all.setAttribute("data-v2-dismiss","all");btns.push(all);
 body.appendChild(el("div","safe",S.staleHint));
 stale.forEach(function(x){
  var row=el("div","v2-stale-row"),text=el("div","v2-stale-text"),t=el("bdi","v2-stale-title",shown(x,"title")),b=el("button","v2-stale-remove",V2.now.remove);
  t.dir="auto";text.appendChild(t);text.appendChild(el("span","v2-stale-why",S["why_"+x.stale]||V2.now.unavailable));
  b.type="button";b.setAttribute("data-v2-dismiss",x.id);btns.push(b);b.addEventListener("click",function(){v2Dismiss([x.id],btns);});
  row.appendChild(text);row.appendChild(b);body.appendChild(row);});
 all.addEventListener("click",function(){v2Dismiss(stale.map(function(x){return x.id;}),btns);});body.appendChild(all);
 var f=compactBlock("v2-stale",V2.now.staleList+" ("+nf.format(stale.length)+")","",body,"v2-stale-fold");f.setAttribute("data-v2-section","stale");return f;
}
// Old topics (topic_cleanup.py): the bridge only lists them; the owner decides. Close is reversible (Telegram's
// closeForumTopic), never delete. "Close all" arms on the first tap, names the count, and sends it on the second.
var v2CleanupArmed=false,v2CleanupResult=null;
function v2CleanupPost(body,btns,done){
 haptic("tap");btns.forEach(function(b){b.disabled=true;});
 post("/api/topic-cleanup",body).then(function(r){if(!r.ok)throw r.status;return r.json();})
  .then(function(j){haptic("n","success");done(j);v2CleanupArmed=false;load();})
  .catch(function(st){haptic("n","error");v2CleanupArmed=false;btns.forEach(function(b){b.disabled=false;});toast(st===403?S.reopen:st===409?V2.cleanup.changed:S.failed,true);});
}
function v2CleanupResultCard(res){
 var kept=res.kind==="keep",box=el("div","v2-cleanup-result"),line=el("div","v2-cleanup-result-text",(kept?V2.cleanup.keptN:V2.cleanup.closedN).replace("{n}",nf.format(res.ids.length))),undo=el("button","v2-cleanup-undo",kept?V2.cleanup.undoKeep:V2.cleanup.reopen);
 undo.type="button";undo.setAttribute("data-v2-cleanup",kept?"unkeep":"reopen");
 undo.addEventListener("click",function(){v2CleanupPost({action:kept?"unkeep":"reopen",ids:res.ids},[undo],function(j){v2CleanupResult=null;toast(kept?V2.cleanup.unkept:V2.cleanup.reopenedN.replace("{n}",nf.format(j.reopened||0)));});});
 var ok=el("button","v2-cleanup-ok",V2.cleanup.dismiss);ok.type="button";ok.setAttribute("data-v2-cleanup","dismiss");
 ok.addEventListener("click",function(){v2CleanupResult=null;render(last,true);});
 box.appendChild(line);box.appendChild(undo);box.appendChild(ok);return box;
}
function v2TopicCleanup(d){
 var tc=d.topic_cleanup,rows=(tc&&tc.topics)||[];
 if(!rows.length&&!v2CleanupResult)return null;
 var body=el("div","v2-cleanup-body"),btns=[];
 if(v2CleanupResult)body.appendChild(v2CleanupResultCard(v2CleanupResult));
 if(rows.length)body.appendChild(el("div","safe",V2.cleanup.hint.replace("{days}",nf.format(tc.days))));
 rows.forEach(function(x){
  var row=el("div","v2-cleanup-row"),text=el("div","v2-cleanup-text"),name=el("bdi","v2-cleanup-name",x.name);name.dir="auto";
  text.appendChild(name);text.appendChild(el("span","v2-cleanup-meta",(x.project?x.project+" · ":"")+V2.cleanup.last+" "+ago(x.age)));
  var acts=el("div","v2-cleanup-actions"),keep=el("button","v2-cleanup-keep",V2.cleanup.keep),close=el("button","v2-cleanup-close",V2.cleanup.close);
  keep.type=close.type="button";keep.setAttribute("data-v2-cleanup","keep");keep.setAttribute("data-id",x.id);close.setAttribute("data-v2-cleanup","close");close.setAttribute("data-id",x.id);btns.push(keep,close);
  keep.addEventListener("click",function(){v2CleanupPost({action:"keep",ids:[x.id]},btns,function(j){if(j.kept)v2CleanupResult={kind:"keep",ids:[x.id]};});});
  close.addEventListener("click",function(){v2CleanupPost({action:"close",ids:[x.id]},btns,function(j){var ids=j.closed_ids||[];if(ids.length)v2CleanupResult={kind:"close",ids:ids};else toast(V2.cleanup.changed,true);});});
  acts.appendChild(keep);acts.appendChild(close);row.appendChild(text);row.appendChild(acts);body.appendChild(row);});
 if(rows.length){
  var ids=rows.map(function(x){return x.id;}),all=el("button","v2-cleanup-all",v2CleanupArmed?V2.cleanup.confirmAll.replace("{n}",nf.format(ids.length)):V2.cleanup.closeAll);
  all.type="button";all.setAttribute("data-v2-cleanup","all");all.setAttribute("aria-live","polite");btns.push(all);
  all.addEventListener("click",function(){
   if(!v2CleanupArmed){v2CleanupArmed=true;render(last,true);return;}
   v2CleanupPost({action:"close",ids:ids,confirm:ids.length},btns,function(j){var done=j.closed_ids||[];if(done.length)v2CleanupResult={kind:"close",ids:done};});});
  body.appendChild(all);
  if(v2CleanupArmed){var no=el("button","v2-cleanup-cancel",S.cancelIt);no.type="button";no.setAttribute("data-v2-cleanup","cancel");
   no.addEventListener("click",function(){v2CleanupArmed=false;render(last,true);});body.appendChild(no);}
 }
 var f=compactBlock("v2-cleanup",V2.cleanup.title+" ("+nf.format(rows.length)+")","",body,"v2-cleanup-fold",v2CleanupArmed||!!v2CleanupResult);f.setAttribute("data-v2-section","cleanup");return f;
}
function v2RequestList(rows){var ul=el("ul","v2-requests");rows.forEach(function(r){var li=requestRow(r,true);li.setAttribute("data-v2-request",r.project+":"+r.id);ul.appendChild(li);});return ul;}
function v2Now(d){
 briefSeen=d.now;
 v2PageHead(V2.shell.now,"v2home",V2.now.greeting);
 var morning=typeof v2MorningCard==="function"?v2MorningCard(d):null;if(morning)app.appendChild(morning);  // cockpit_v2_workmap.js
 var zone=el("section","v2-needs");zone.setAttribute("data-v2-section","decisions");zone.setAttribute("aria-label",V2.now.needs);var heading=v2Heading(V2.now.needs,"v2tower");
 var live=(d.decisions||[]).filter(function(x){return !closed[x.id];}),decs=live.filter(function(x){return !v2IsStale(x);}),stale=live.filter(v2IsStale),asks=(d.cards||[]).filter(function(c){return ASKS[c.kind]&&!c.ended&&!cardDone[c.id];});heading.appendChild(el("span","v2-count",nf.format(decs.length+asks.length)));zone.appendChild(heading);
 decs.forEach(function(x){zone.appendChild(v2Decision(x));});asks.forEach(function(c){zone.appendChild(v2AskCard(c));});
 if(!decs.length&&!asks.length)zone.appendChild(el("div","v2-empty",V2.now.empty));app.appendChild(zone);
 if(stale.length)app.appendChild(v2StaleFold(stale));
 var cleanup=v2TopicCleanup(d);if(cleanup)app.appendChild(cleanup);
 if(typeof v2ScheduleCard==="function"&&(d.addons||{}).schedule_card!==false)app.appendChild(v2ScheduleCard());  // cockpit_v2_schedule.js; the schedule_card add-on
 app.appendChild(v2Changes(d));app.appendChild(v2Heading(V2.now.work,"layers"));var tiles=el("div","v2-projects");tiles.setAttribute("data-v2-section","projects");
 // Active projects first; quiet ones (no update for a day, no progress) fold below, one tap away.
 var projects=v2ProjectOrder((d.cockpit_v2||{}).projects||[]),quiet=projects.filter(v2Quiet);
 projects.filter(function(p){return !v2Quiet(p);}).forEach(function(p){tiles.appendChild(v2Project(p));});
 if(!projects.length)tiles.appendChild(el("div","safe",V2.now.noProjects));app.appendChild(tiles);
 if(quiet.length){var qt=el("div","v2-projects");quiet.forEach(function(p){qt.appendChild(v2Project(p));});
  var qf=compactBlock("v2-quiet-projects",V2.now.lessActive+" ("+nf.format(quiet.length)+")","",qt,"v2-less");qf.setAttribute("data-v2-section","quiet-projects");app.appendChild(qf);}
 app.appendChild(v2Heading(V2.now.requests,"list"));var rows=d.requests||[],active=rows.filter(function(r){return r.status!=="done"&&!r.older;}),older=rows.filter(function(r){return r.status==="done"||r.older;});
 var requests=v2RequestList(active);requests.setAttribute("data-v2-section","requests");if(!active.length)requests.appendChild(el("li","v2-empty",S.requestsEmpty));app.appendChild(requests);
 // Old decisions remain in the first zone. This fold holds request history and reminders only.
 var aged=(d.reminders||[]).filter(function(r){return r.old;}),oldBody=el("div");oldBody.appendChild(v2RequestList(older));var oldRem=el("ul","v2-reminders");aged.forEach(function(r,i){oldRem.appendChild(remRow(r,"v2-old"+i));});oldBody.appendChild(oldRem);
 var old=compactBlock("v2-older",V2.now.older+" ("+nf.format(older.length+aged.length)+")","",oldBody,"v2-older");old.setAttribute("data-v2-section","older");app.appendChild(old);
 app.appendChild(v2Heading(V2.now.reminders,"clock"));var reminders=el("ul","v2-reminders");reminders.setAttribute("data-v2-section","reminders");(d.reminders||[]).filter(function(r){return !r.old;}).forEach(function(r,i){reminders.appendChild(remRow(r,"v2"+i));});app.appendChild(reminders);
 var answeredHere=Object.keys(answered);if(answeredHere.length){app.appendChild(v2Heading(V2.now.answers,"check"));answeredHere.forEach(function(id){app.appendChild(doneCard(answered[id].d,answered[id]));});}
 Object.keys(cardDone).forEach(function(id){app.appendChild(agentCard(cardDone[id].c,false));});
}
function v2RadioArtwork(){if(!v2Enabled(last))return;var box=document.getElementById("radio-player")||V2_PLAYER,row=box.querySelector(".s-head");
 if(row){row.insertBefore(v2Image("artwork","v2-radio-art"),row.firstChild);
  // Decoration only: the waveform and icon never change what the button does or announces.
  var wave=el("span","v2-wave");wave.setAttribute("aria-hidden","true");for(var i=0;i<23;i++)wave.appendChild(el("i"));row.insertBefore(wave,row.querySelector("button"));
  var btn=row.querySelector("button");if(btn){var label=btn.textContent;btn.textContent="";btn.setAttribute("aria-label",label);btn.appendChild(icon(radioWanted?"v2pause":"v2play"));btn.appendChild(el("span","v2-play-label",label));}
  var live=row.querySelector(".pill.on");if(live)live.insertBefore(icon("v2radio"),live.firstChild);}
 // The Radio tool shows this same player; elsewhere it keeps its classic place and visibility.
 if(radioInfo&&radioInfo.enabled&&!targetClosed&&v2RadioHere(last))box.hidden=false;
 if("mediaSession" in navigator&&typeof MediaMetadata!=="undefined"){try{navigator.mediaSession.metadata=new MediaMetadata({title:"רדיו הצוות",artist:"Harnessgram",artwork:[{src:new URL(V2_ASSET+"artwork.webp",location.href).href,sizes:"512x512",type:"image/webp"}]});}catch(e){}}
}
ICONS.v2home="M3 11l9-8 9 8M5 10v11h5v-7h4v7h5V10";
ICONS.v2folder="M3 7h7l2 2h9v11H3zM3 7V4h7l2 3";
ICONS.v2tools="M14 5a6 6 0 0 0-7 7L2 17l5 5 5-5a6 6 0 0 0 7-7l-4 3-4-4z";
ICONS.v2settings="M9 3h6l1 3 3 1 2 5-2 3-3 1-1 5H9l-1-4-3-2-2-4 2-4 3-1zM12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8";
ICONS.v2tower="M7 3h10v4H7zM5 7h14l-2 5H7zM10 12v9M14 12v9M7 21h10";
ICONS.v2announce="M3 10h5l10-6v16L8 14H3zM8 14l2 6M21 9v6";
// Top-level values come last: each page function, classic ones included, stays self-contained
// for the source-level tests, and every use of these is at call time.
var V2_COPY=__V2_STRINGS__,V2=V2_COPY[LANG]||V2_COPY.en,V2_ASSET="/assets/cockpit/v1/";
