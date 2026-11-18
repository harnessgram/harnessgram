// Direction D, milestone 2: Projects, one project's page, Tools and Settings.
// Every control here is an existing classic control, moved and restyled, or it calls the same
// authenticated API with the same payload. Nothing below decides status: the bridge does.
var v2ProjectKey="",v2ProjectLast="",v2ProjectClose=null,v2ListY=0,v2Seg=null,V2_PLAYER=document.getElementById("radio-player");
var V2_SEG_ICONS={studio:"layers",radio:"v2radio",more:"v2grid",tower:"mic"},V2_SEG_KEY="ctb-v2-seg";
var V2_GROUPS=[["notify","bell",["activity","live_activity_cockpit","voice_digest","progress","progress_details","compact_actions","voice_confirm","delivery_end","status_repost","agent_notices"]],
 ["auto","gears",["auto_cards","cold_compact","auto_account_switch","quota_auto_continue","emergency_credit","session_auto_revive","away","auto_topic_names","state_emoji","agents_topic","general_offer","day_briefing","day_nudges","day_auto_plan","brief_home"]],
 ["models","server",["auto_compact"]],["radio","mic",["voice_copilot","radio_every_reply","radio_board","radio_music","radio_room"]],["connect","laptop",[]]];
// --- shared parts ------------------------------------------------------------------------------------------------
function v2Card(section,title,ic){var c=el("section","v2-card");c.setAttribute("data-v2-section",section);c.appendChild(v2Heading(title,ic));return c;}
// Classic builders that append to #app are moved, unchanged, into a v2 container.
function v2Capture(fn){var n=app.children.length,out=[];fn();while(app.children.length>n)out.unshift(app.removeChild(app.lastChild));return out;}
function v2Updated(age){var row=el("span","v2-updated");row.appendChild(icon("clock"));
 if(age==null){row.appendChild(el("span",null,V2.now.noUpdate));return row;}
 var base=((last&&last.now)||Date.now()/1000)*1000,at=new Date(base-age*1000),today=at.toDateString()===new Date(base).toDateString();
 var time=at.toLocaleTimeString(LANG,{hour:"numeric",minute:"2-digit"});
 row.appendChild(el("span",null,today?V2.projects.today:V2.projects.on));
 row.appendChild(el("bdi",null,today?time:at.toLocaleDateString(LANG,{day:"numeric",month:"short"})+" "+time));return row;}
function v2WaitChip(n){var c=el("span","v2-wait-chip");c.appendChild(el("span",null,V2.projects.waitingChip));c.appendChild(el("bdi",null,nf.format(n)));return c;}
function v2StatusChip(p){var c=el("span","v2-status-chip");c.setAttribute("data-status",p.status);c.appendChild(v2Badge(p));
 c.appendChild(el("span",null,p.stale&&p.status==="on_track"?V2.now.staleUpdate:V2.now[p.status]));return c;}
function v2Chev(){var c=el("span","v2-chev");c.appendChild(icon("chev"));return c;}
function v2SectionBlock(key,section,title,hint,ic,body,force){
 var block=compactBlock(key,title,el("span","v2-block-hint",hint),body,"v2-block",force),toggle=block.querySelector(".compact-toggle"),lead=el("span","v2-block-icon");
 lead.appendChild(icon(ic));toggle.insertBefore(lead,toggle.firstChild);block.setAttribute("data-v2-section",section);return block;}
// --- Projects ----------------------------------------------------------------------------------------------------
function v2OpenProject(key){v2ListY=view==="agents"?window.scrollY:0;v2ProjectKey=v2ProjectLast=key;haptic("sel");
 if(view!=="agents")switchTo("agents");else{renderNewContent=true;render(last,true);}window.scrollTo(0,0);}
function v2TabTap(d,tab){
 // Tapping Projects on a project page returns to the list through the same back step.
 if(tab==="agents"&&view==="agents"&&v2ProjectKey&&v2ProjectClose){v2ProjectClose();return;}
 switchTo(tab==="tools"?v2ToolView(d):tab);}
function v2ProjectCard(p){
 var b=el("button","v2-pcard");b.type="button";b.setAttribute("data-status",p.status);b.setAttribute("data-project",p.key);
 var pic=el("span","v2-pcard-pic");pic.appendChild(v2Image(p.asset,null,p.art));pic.appendChild(v2Badge(p));b.appendChild(pic);
 var body=el("span","v2-pcard-body");var nm=el("bdi","v2-pcard-name",v2Name(p));nm.title=shown(p,"name");body.appendChild(nm);
 var text=shown(p,"summary")||(p.stale&&p.status==="on_track"?V2.now.staleUpdate:V2.now[p.status]),sum=el("span","v2-pcard-sum",text);sum.dir="auto";body.appendChild(sum);
 var meta=el("span","v2-pcard-meta");meta.appendChild(v2Updated(p.age));if(p.waiting)meta.appendChild(v2WaitChip(p.waiting));body.appendChild(meta);b.appendChild(body);
 b.appendChild(v2Ring(p));b.appendChild(v2Chev());
 b.setAttribute("aria-description",[V2.projects.open,p.stale&&p.status==="on_track"?V2.now.staleUpdate:V2.now[p.status]].join(" · "));
 b.addEventListener("click",function(){v2OpenProject(p.key);});return b;}
function v2Quota(d){var box=el("div","v2-quota");
 (d.quota||[]).forEach(function(q,i){if(typeof q.weekly!=="number")return;var r=el("div","v2-quota-row");r.setAttribute("data-tone",String(i%4));
  var ic=el("span","v2-quota-ic");ic.appendChild(icon("chart"));r.appendChild(ic);r.appendChild(el("bdi","v2-quota-name",q.label));
  var bar=el("span","v2-quota-bar"),fill=el("i");bar.setAttribute("aria-hidden","true");fill.style.inlineSize=Math.max(2,Math.min(100,q.weekly))+"%";bar.appendChild(fill);r.appendChild(bar);
  r.appendChild(el("b","v2-quota-pc",pf.format(Math.round(q.weekly)/100)));box.appendChild(r);});
 if(!box.children.length)box.appendChild(el("div","safe",V2.projects.noQuota));return box;}
function v2SessionGroup(host,title,list,lead,d){if(!list.length)return;host.appendChild(el("div","v2-sub",title+" · "+nf.format(list.length)));
 var g=el("div","card group");list.forEach(function(s){g.appendChild(sessionRow(s,d,lead));});host.appendChild(g);}
function v2Team(d){var by=groups(d),body=el("div"),tiles=el("div","v2-team-tiles");
 [[by.working.length,V2.projects.working,"working","v2settings"],[by.idle.length,V2.projects.idle,"idle","clock"],[by.ended.length+by.left.length,V2.projects.finished,"ended","check"]].forEach(function(t){
  var x=el("div","v2-team-tile");x.setAttribute("data-kind",t[2]);var top=el("span","v2-team-n");top.appendChild(icon(t[3]));top.appendChild(el("b",null,nf.format(t[0])));x.appendChild(top);x.appendChild(el("span",null,t[1]));tiles.appendChild(x);});
 // The classic rows keep every session action: write, screen, compact, fork and close.
 v2SessionGroup(body,V2.projects.working,by.working,"now",d);v2SessionGroup(body,V2.projects.idle,by.idle,"summary",d);
 if(by.left.length){var lf=fold("v2-left",S.leftList,by.left.length),lg=el("div","card group");lg.appendChild(el("div","hint pad",S.leftHint));
  by.left.forEach(function(s){lg.appendChild(sessionRow(s,d,"summary"));});
  if(by.left.length>1){var lrow=el("div","btns pad");lrow.appendChild(closeBtn(by.left.slice(0,20).map(function(s){return s.id;}),S.closeAll));lg.appendChild(lrow);}
  lf.body.appendChild(lg);body.appendChild(lf);}
 if(by.ended.length){var ef=fold("v2-ended",S.endedList,by.ended.length),eg=el("div","card group");by.ended.forEach(function(s){eg.appendChild(sessionRow(s,d,"none"));});ef.body.appendChild(eg);body.appendChild(ef);}
 if(!body.children.length)body.appendChild(el("div","safe",S.none));
 var block=v2SectionBlock("v2-team","team",V2.projects.team,V2.projects.teamHint,"person",body);block.insertBefore(tiles,block.children[1]);return block;}
function v2System(d){var body=el("div");
 if(d.mac){var mac=macCard(d.mac);body.appendChild(mac);}
 v2Capture(function(){usageCard(d.usage_totals);}).forEach(function(n){body.appendChild(n);});
 if(!body.children.length)body.appendChild(el("div","safe",V2.projects.noQuota));
 var block=v2SectionBlock("v2-system","system",V2.projects.system,V2.projects.systemHint,"v2pulse",body);block.insertBefore(v2Quota(d),block.children[1]);return block;}
function v2Projects(d){
 var projects=(d.cockpit_v2||{}).projects||[],p=v2ProjectKey&&projects.filter(function(x){return x.key===v2ProjectKey;})[0];
 if(p){v2ProjectPage(d,p);return;}
 v2ProjectKey="";v2ProjectClose=null;
 v2PageHead(V2.shell.projects,"v2folder",V2.projects.subtitle);
 var list=el("div","v2-plist");list.setAttribute("data-v2-section","project-list");
 projects.forEach(function(x){list.appendChild(v2ProjectCard(x));});if(!projects.length)list.appendChild(el("div","v2-empty",V2.now.noProjects));app.appendChild(list);
 var handled=v2Capture(function(){idleLines(d);});
 if(handled.length){var hb=el("div");handled.forEach(function(n){if(!/\bsec\b/.test(n.className))hb.appendChild(n);});
  app.appendChild(v2SectionBlock("v2-handled","team-handles",V2.projects.teamHandles,V2.projects.teamHandlesHint,"clock",hb));}
 app.appendChild(v2Team(d));app.appendChild(v2System(d));}
// --- one project -------------------------------------------------------------------------------------------------
// The label already says "decision" or "next": the ledger's own marker is not repeated.
function v2Fact(ic,label,text){var r=el("div","v2-fact");r.appendChild(icon(ic));r.appendChild(el("b",null,label));var t=el("span",null,String(text).replace(/^\s*(?:Decided|Next|הוחלט|השלב הבא)\s*[:：]\s*/i,""));t.dir="auto";r.appendChild(t);return r;}
function v2ProjectPage(d,p){
 var page=el("div","v2-ppage");page.setAttribute("data-v2-section","project");page.setAttribute("data-project",p.key);
 // One reversible step: the header arrow, the Projects tab and native/browser back all close it.
 var setOpen=bindBackLayer("v2-project",page,true,function(on){
  if(on){if(!v2ProjectKey&&v2ProjectLast){v2ProjectKey=v2ProjectLast;if(last)render(last,true);}return;}
  v2ProjectKey="";v2ProjectClose=null;if(last&&view==="agents")render(last,true);var y=v2ListY;requestAnimationFrame(function(){window.scrollTo(0,y);});});
 v2ProjectClose=function(){setOpen(false);};
 var head=el("header","v2-phead");head.setAttribute("data-status",p.status);head.appendChild(v2Image(p.asset,"v2-phead-bg",p.art));
 var back=el("button","v2-back");back.type="button";back.setAttribute("aria-label",V2.projects.back);back.setAttribute("data-v2-back","");back.appendChild(icon("chev"));
 back.addEventListener("click",function(){haptic("sel");setOpen(false);});head.appendChild(back);if(typeof v2ArtButton==="function")head.appendChild(v2ArtButton(p));
 var tx=el("div","v2-phead-text"),h=el("h2");h.appendChild(el("bdi",null,v2Name(p)));tx.appendChild(h);
 if(shown(p,"summary")){var sm=el("p",null,shown(p,"summary"));sm.dir="auto";tx.appendChild(sm);}tx.appendChild(v2Updated(p.age));head.appendChild(tx);
 source("v2-project:"+p.key,[[p.name,he(p,"name")],[p.summary,he(p,"summary")]],tx);
 var st=el("div","v2-phead-ring");st.appendChild(v2Ring(p));if(p.waiting)st.appendChild(v2WaitChip(p.waiting));else st.appendChild(v2StatusChip(p));head.appendChild(st);page.appendChild(head);
 // 1. Every open decision of this project, with its full detail before yes and no.
 var needs=v2Card("project-needs",V2.projects.needs,"v2target");
 var decs=(d.decisions||[]).filter(function(x){return x.project_key===p.key&&!closed[x.id];}),asks=(d.cards||[]).filter(function(c){return c.project_key===p.key&&ASKS[c.kind]&&!c.ended&&!cardDone[c.id];});
 decs.forEach(function(x){needs.appendChild(v2Decision(x));});asks.forEach(function(c){needs.appendChild(v2AskCard(c));});
 if(!decs.length&&!asks.length)needs.appendChild(el("div","v2-empty",V2.projects.nothingNeeded));page.appendChild(needs);
 var wmCard=typeof v2WorkMapCard==="function"?v2WorkMapCard(p):null;if(wmCard)page.appendChild(wmCard);  // cockpit_v2_workmap.js
 // 1b. Ideas waiting: open ledger items older than a day, so none is forgotten.
 var ideas=v2Ideas(p);if(ideas)page.appendChild(ideas);
 // 2. Progress from the newest live progress card; nothing is invented when none was reported.
 var prog=v2Card("project-progress",V2.projects.progress,"chart"),card=(d.cards||[]).filter(function(c){return c.project_key===p.key&&c.kind==="progress"&&!c.auto_closed;}).sort(function(a,b){return a.age-b.age;})[0];
 if(card&&Array.isArray(card.steps)&&card.steps.length){var at=typeof card.at==="number"?card.at:0,ol=el("ol","v2-steps"),labels=shown(card,"steps");ol.style.gridTemplateColumns="repeat("+card.steps.length+",minmax(0,1fr))";
  card.steps.forEach(function(s,i){var li=el("li",i<at?"done":i===at?"cur":""),dot=el("span","v2-step-dot");if(i<at)dot.appendChild(icon("check"));else dot.textContent=nf.format(i+1);li.appendChild(dot);
   var lb=el("span","v2-step-label",labels[i]);lb.dir="auto";li.appendChild(lb);if(i===at)li.setAttribute("aria-current","step");ol.appendChild(li);});prog.appendChild(ol);}
 var sentence=card?(shown(card,"live")||unprefixed(shown(card,"title"),card)):"";
 var said=el("div","v2-sentence",sentence||(typeof p.percent==="number"?pf.format(p.percent/100):V2.projects.progressNone));said.dir="auto";prog.appendChild(said);page.appendChild(prog);
 // 3. Topics of this project; each opens the existing detail sheet with its own way out.
 var keys={};(p.topics||[]).forEach(function(k){keys[JSON.stringify(k)]=1;});
 var rows=(d.topic_cards||[]).filter(function(t){return keys[JSON.stringify(t.key)];}),active=rows.filter(function(t){return t.state!=="done";}),done=rows.filter(function(t){return t.state==="done";});
 var tc=v2Card("project-topics",V2.projects.topics,"layers"),tl=el("div","v2-topics");
 active.forEach(function(t){var c=topicCard(t),lead=el("span","v2-topic-icon");lead.appendChild(icon("list"));c.insertBefore(lead,c.firstChild);c.appendChild(v2Chev());tl.appendChild(c);});
 if(!active.length)tl.appendChild(el("div","v2-empty",V2.projects.noTopics));tc.appendChild(tl);
 var decided=rows.filter(function(t){return topicText(t,"decision");})[0],next=active.filter(function(t){return topicText(t,"next");})[0];
 if(decided)tc.appendChild(v2Fact("check",V2.projects.lastDecision,topicText(decided,"decision")));
 if(next)tc.appendChild(v2Fact("v2next",V2.projects.nextStep,topicText(next,"next")));page.appendChild(tc);
 // 4. Deliverables: the agents' own https links, opened through the existing safe opener.
 var links=(d.cards||[]).filter(function(c){return c.project_key===p.key&&c.kind==="link"&&!c.ended&&typeof c.url==="string"&&/^https:\/\//i.test(c.url);});
 if(links.length){var dl=v2Card("project-deliverables",V2.projects.deliverables,"film"),grid=el("div","v2-deliverables");
  links.slice(0,6).forEach(function(c){var b=el("button","v2-deliverable");b.type="button";b.appendChild(v2Image(p.asset,null,p.art));var cap=el("span","v2-deliverable-cap");cap.appendChild(icon("link"));
   var t=el("span",null,shown(c,"label")||unprefixed(shown(c,"title"),c)||hostOf(c.url));t.dir="auto";cap.appendChild(t);b.appendChild(cap);
   b.addEventListener("click",function(){haptic("tap");openSafe(c.url);});grid.appendChild(b);});dl.appendChild(grid);page.appendChild(dl);}
 // 5. Reminders of this project, with the same clear and restore actions.
 var refs={};(p.reminders||[]).forEach(function(r){refs[r]=1;});var rems=(d.reminders||[]).filter(function(r){return r.ref&&refs[r.ref];});
 if(rems.length){var rc=v2Card("project-reminders",V2.projects.reminders,"clock"),ul=el("ul","v2-reminders");rems.forEach(function(r,i){ul.appendChild(remRow(r,"p"+i));});rc.appendChild(ul);page.appendChild(rc);}
 // 6. Its live conversations and the way into each Telegram topic.
 var ids={};(p.sessions||[]).forEach(function(id){ids[id]=1;});var ss=(d.sessions||[]).filter(function(s){return ids[s.id]&&s.state!=="ended";});
 var sc=v2Card("project-sessions",V2.projects.activeSessions,"chat");
 ss.forEach(function(s){var r=el("div","v2-session");r.appendChild(robot(s.id,s.state,false));var b=el("div","v2-session-body"),n=el("b",null,shown(s,"name"));n.dir="auto";b.appendChild(n);
  var line=shown(s,"now")||shown(s,"summary");if(line){var l=el("span",null,line);l.dir="auto";b.appendChild(l);}source("v2-session:"+s.id,s,b);r.appendChild(b);
  if(typeof s.url==="string"&&/^https:\/\/t\.me\//i.test(s.url))r.appendChild(tgBtn(s.url));sc.appendChild(r);});
 if(!ss.length)sc.appendChild(el("div","v2-empty",V2.projects.noSessions));page.appendChild(sc);
 // 7. Finished topics stay reachable, folded.
 if(done.length){var hb=el("div","v2-topics");done.forEach(function(t){hb.appendChild(topicCard(t));});page.appendChild(compactBlock("v2-history:"+p.key,V2.projects.history,nf.format(done.length),hb,"v2-older"));}
 app.appendChild(page);}
// Each waiting idea opens in place: its full words, and one tap asks the project's lead session through the same
// per-session message the classic Write box sends. Without a live session it only shows the words.
function v2Ideas(p){var list=p.ideas||[];if(!list.length)return null;
 var card=v2Card("project-ideas",V2.projects.ideas,"v2bulb"),ul=el("ul","v2-ideas");card.appendChild(el("div","v2-block-hint",V2.projects.ideasHint));
 list.forEach(function(r){var li=el("li","v2-idea"),head=el("button","v2-idea-head"),body=el("div","v2-idea-body"),id="v2-idea-"+(++compactSerial);
  li.setAttribute("data-v2-idea",String(r.id));head.type="button";head.setAttribute("aria-expanded","false");head.setAttribute("aria-controls",id);body.id=id;body.hidden=true;
  var t=el("span","v2-idea-title",shown(r,"title"));t.dir="auto";head.appendChild(t);
  var meta=el("span","v2-idea-meta");meta.appendChild(el("span","chip"+(r.wait==="owner"?" hot":""),r.wait==="owner"?V2.projects.ideaYou:V2.projects.ideaTeam));
  meta.appendChild(el("span","v2-idea-age",ago(r.age)));head.appendChild(meta);head.appendChild(v2Chev());li.appendChild(head);
  var full=el("p","v2-idea-text",shown(r,"title"));full.dir="auto";body.appendChild(full);
  source("v2-idea:"+p.key+":"+r.id,r,li);
  if(p.lead){var ask=el("button","btn v2-idea-ask");ask.type="button";ask.setAttribute("data-v2-idea-ask",p.lead);ask.appendChild(icon("chat"));ask.appendChild(el("span",null,V2.projects.ideaAsk));
   ask.addEventListener("click",function(){if(ask.disabled)return;ask.disabled=true;haptic("tap");
    var text=V2.projects.ideaQuestion.replace("{ref}",r.project+" #"+r.id).replace("{title}",String(r.title||"").slice(0,300));
    post("/api/say",{session:p.lead,text:text,send:sendId("idea"+p.lead+r.id,text)}).then(function(x){if(!x.ok)throw x.status;return x.json();})
     .then(function(){haptic("n","success");ask.replaceWith(el("div","v2-empty",V2.projects.ideaAsked));toast(V2.projects.ideaAsked);})
     .catch(function(st){ask.disabled=false;haptic("n","error");toast(st===404||st===409?S.sayGone:st===403?S.reopen:S.failed,true);});});
   body.appendChild(ask);}
  else body.appendChild(el("div","v2-empty",V2.projects.ideaNoLead));
  head.addEventListener("click",function(){var open=body.hidden;body.hidden=!open;head.setAttribute("aria-expanded",String(open));li.classList.toggle("open",open);haptic("sel");});
  li.appendChild(body);ul.appendChild(li);});
 card.appendChild(ul);
 if(p.ideas_more)card.appendChild(el("div","v2-block-hint",V2.projects.ideasMore.replace("{n}",nf.format(p.ideas_more))));
 return card;}
// --- Tools -------------------------------------------------------------------------------------------------------
function v2Segs(d){var s=[];if(appNamed(d,"studio"))s.push("studio");if(d&&d.radio)s.push("radio");if(d&&(d.settings||{}).voice_copilot)s.push("tower");s.push("more");return s;}
function v2SavedSeg(d){var seg=v2Seg,segs=v2Segs(d);if(seg==null){try{seg=sessionStorage.getItem(V2_SEG_KEY);}catch(e){}}
 return segs.indexOf(seg)>=0?seg:segs.indexOf("radio")>=0?"radio":segs[0];}
// The last tool is kept; Studio is the existing app frame, so it has its own view.
function v2ToolView(d){return v2SavedSeg(d)==="studio"?"app:studio":"tools";}
function v2ToolSeg(d){var name=appOf(view);if(name)return name==="studio"?"studio":"more";var seg=v2SavedSeg(d);
 return seg!=="studio"?seg:v2Segs(d).indexOf("radio")>=0?"radio":"more";}
function v2RadioHere(d){return v2Enabled(d)&&view==="tools"&&v2ToolSeg(d)==="radio";}
function v2Tools(d){
 var seg=v2ToolSeg(d),bar=el("div","v2-seg");v2PageHead(V2.shell.tools,"v2briefcase",V2.tools.subtitle);
 if((d.settings||{}).voice_copilot)bar.setAttribute("data-vcp-choice","");
 bar.setAttribute("role","group");bar.setAttribute("aria-label",V2.shell.tools);bar.setAttribute("data-v2-section","tool-choice");
 v2Segs(d).forEach(function(s){var b=el("button","v2-seg-btn"+(s===seg?" on":""));b.type="button";b.setAttribute("aria-pressed",String(s===seg));b.setAttribute("data-v2-tool",s);
  b.appendChild(icon(V2_SEG_ICONS[s]));b.appendChild(el("span",null,V2.tools[s]));
  b.addEventListener("click",function(){v2Seg=s;try{sessionStorage.setItem(V2_SEG_KEY,s);}catch(e){}haptic("sel");
   var to=s==="studio"?"app:studio":"tools";if(view===to)render(last,true);else switchTo(to,false);});bar.appendChild(b);});
 app.appendChild(bar);
 if(appOf(view))return;  // the app's own sandboxed frame follows in #apps
 if(seg==="tower")vcpPage(d);else if(seg==="radio")v2Radio(d);else v2More(d);}
function v2Radio(d){
 // The same persistent player (one audio element) is moved here; tabs and polls never interrupt it.
 var hero=el("section","v2-radio-hero");hero.setAttribute("data-v2-section","radio");hero.appendChild(V2_PLAYER);
 var off=el("div","v2-radio-off");off.appendChild(v2Image("artwork"));var ot=el("div");ot.appendChild(el("b",null,S.radio));ot.appendChild(el("span",null,V2.tools.offAir));off.appendChild(ot);hero.appendChild(off);app.appendChild(hero);
 // Start/stop and the station details: the classic card, unchanged. Its announcement composer moves to its tile.
 var station=radioCard(d.radio),say=station.querySelector('[data-block="say"]'),tiles=el("div","v2-radio-tiles");tiles.setAttribute("data-v2-section","radio-actions");
 var roomBody=el("div");if(d.room&&d.room.enabled&&(d.room.room||d.radio.on))roomBody.appendChild(roomCard(d));else roomBody.appendChild(el("div","safe",S.roomRadioOff));
 var sayBody=say?say.body:el("div","safe",V2.tools.announceOff);if(say)say.remove();
 var podBody=el("div");if(d.podcast)podBody.appendChild(podcastCard(d.podcast,d.radio));  // radio_podcast.py; the classic card, unchanged
 [["v2-room",V2.tools.room,roomBody,"v2bulb"],["v2-say",V2.tools.announce,sayBody,"v2announce"]].concat(d.podcast?[["v2-podcast",V2.tools.podcast,podBody,"v2play"]]:[]).forEach(function(t){
  var b=compactBlock(t[0],t[1],"",t[2],"v2-tile"),toggle=b.querySelector(".compact-toggle"),ic=el("span","v2-tile-icon");ic.appendChild(icon(t[3]));toggle.insertBefore(ic,toggle.firstChild);tiles.appendChild(b);});
 app.appendChild(tiles);station.classList.add("v2-station");station.setAttribute("data-v2-section","station");app.appendChild(station);
 var rows=v2SettingRows(d);
 if(rows.voice_digest){var dg=el("section","v2-card v2-digest"),hd=el("div","v2-digest-head"),ic=el("span","v2-head-icon");dg.setAttribute("data-v2-section","voice-digest");
  ic.appendChild(icon("mic"));hd.appendChild(ic);var tx=el("div");tx.appendChild(el("h2",null,V2.tools.digestTitle));tx.appendChild(el("div","v2-greeting",V2.tools.digestHint));hd.appendChild(tx);dg.appendChild(hd);
  var seg=rows.voice_digest.querySelector(".seg");if(seg)dg.appendChild(seg);var q=el("div","v2-quiet");q.appendChild(icon("v2moon"));q.appendChild(el("span",null,V2.tools.quiet));dg.appendChild(q);app.appendChild(dg);}
 if(d.youtube){var y=d.youtube,yt=el("button","v2-yt");yt.type="button";yt.setAttribute("data-v2-section","youtube");
  var mark=el("span","v2-yt-mark");mark.appendChild(icon("v2play"));yt.appendChild(mark);yt.appendChild(el("b",null,V2.tools.youtube+":"));
  yt.appendChild(el("span","v2-yt-state"+(y.live?" on":""),y.live?V2.tools.ytLive:y.connected?V2.tools.ytReady:V2.tools.ytOff));yt.appendChild(icon("v2radio"));
  // Connecting, choosing content and confirming a public broadcast stay explicit in Settings.
  yt.addEventListener("click",function(){try{localStorage.setItem(compactStorageKey("v2-set-radio"),"1");}catch(e){}switchTo("settings");});app.appendChild(yt);}}
function v2More(d){var list=el("div","v2-more");list.setAttribute("data-v2-section","more-tools");
 appsOf(d).filter(function(a){return a.name!=="studio";}).forEach(function(a){var b=el("button","v2-app");b.type="button";var ic=el("span","v2-head-icon");ic.appendChild(icon(a.icon));b.appendChild(ic);
  var t=el("bdi",null,appTitle(a));b.appendChild(t);b.appendChild(v2Chev());b.addEventListener("click",function(){v2Seg="more";switchTo("app:"+a.name,false);});list.appendChild(b);});
 if(d.app_request)list.insertBefore(v2AppRequestTile(),list.firstChild);  // cockpit_app_request.js; only when configured
 if(d.mac_tools){var mtTile=v2MacToolsTile(d.mac_tools),arTile=list.querySelector("[data-v2-app-request]");list.insertBefore(mtTile,arTile?arTile.nextSibling:list.firstChild);}  // cockpit_mac_tools.js; only when configured
 if(!list.children.length)list.appendChild(el("div","v2-empty",V2.tools.noApps));app.appendChild(list);}
// --- Settings ----------------------------------------------------------------------------------------------------
// The classic Settings rows, built by the classic code and found by their own label; same controls, same API.
function v2SettingRows(d){var out={},box=settingsBox(d),names={};
 SWITCHES.concat(["activity","voice_digest","auto_compact","cold_compact"]).forEach(function(k){if(S["set_"+k])names[S["set_"+k]]=k;});
 Array.prototype.forEach.call(box.querySelectorAll(".row"),function(r){if(r.classList.contains("v2-mode-row")){out.ui_v2=r;return;}
  var nm=r.querySelector(".nm"),k=nm&&names[nm.textContent];if(k&&!out[k])out[k]=r;});return out;}
function v2Settings(d){
 // "What the system knows" is the first group; its own pages replace Settings while open (cockpit_v2_knowledge.js).
 if(vkNav.open){vkPage(d);return;}
 // "What HarnessGram adds to your agents" is the second; its page replaces Settings while open (cockpit_v2_addons.js).
 if(vaOpen){vaPage(d);return;}
 // Curated owner-local packs sit beside knowledge and add-ons.
 if(typeof vpOpen!=="undefined"&&vpOpen){vpPage(d);return;}
 v2PageHead(V2.shell.settings,"v2settings",V2.settings.subtitle);app.appendChild(vkEntry());app.appendChild(vaEntry());if(typeof vpEntry==="function")app.appendChild(vpEntry());var rows=v2SettingRows(d),placed={},st=d.settings||{};
 var appBody=el("div","v2-group-body");APP_SWITCHES.forEach(function(spec){var key=spec[0],copy=spec[2];placed[key]=1;if(key in st){var row=switchRow(key,d),nm=row.querySelector(".nm"),hint=row.querySelector(".mt");nm.textContent=V2.settings[copy];hint.textContent=V2.settings[copy+"Hint"];row.querySelector(".sw").setAttribute("aria-label",V2.settings[copy]);row.setAttribute("data-app-switch",spec[1]);appBody.appendChild(row);}});
 if(appBody.children.length){var appGroup=el("section","v2-card v2-app-appearance");appGroup.setAttribute("data-v2-section","settings-app-appearance");appGroup.appendChild(el("h2",null,V2.settings.appAppearance));appGroup.appendChild(el("div","v2-block-hint",V2.settings.appAppearanceHint));appGroup.appendChild(appBody);app.appendChild(appGroup);}
 V2_GROUPS.forEach(function(g){g[2].forEach(function(k){placed[k]=1;});});
 V2_GROUPS.forEach(function(g){var body=el("div","v2-group-body"),name=g[0];
  if(name==="models"&&d.default_model)body.appendChild(modelCard(d.default_model));
  g[2].forEach(function(k){if(rows[k])body.appendChild(rows[k]);
   else if(name==="radio"&&d.radio&&k in st&&(k==="radio_every_reply"||"radio_every_reply" in st))body.appendChild(switchRow(k,d));});
  // A switch added later still has a home: Automatic work, never lost.
  if(name==="auto")Object.keys(rows).forEach(function(k){if(!placed[k]&&k!=="ui_v2")body.appendChild(rows[k]);});
  if(name==="models"&&(d.bank||[]).length)body.appendChild(bankCard(d.bank));
  if(name==="radio"&&d.youtube)body.appendChild(youtubeCard(d.youtube));
  if(name==="radio"){var lrow=listenRevokeRow(d);if(lrow)body.appendChild(lrow);}
  if(name==="connect"){var drow=devicesRow(d);if(drow)body.appendChild(drow);}
  if(name==="connect"){v2Capture(function(){hookHealthCard(d);}).forEach(function(n){var b=n.querySelector&&n.querySelector(".btn");
    var ok=!!(d.hook_health&&d.hook_health.ok),p=n.querySelector&&n.querySelector("p");n.setAttribute("data-ok",String(ok));if(b)b.insertBefore(icon("refresh"),b.firstChild);if(ok&&p)p.insertBefore(icon("check"),p.firstChild);body.appendChild(n);});}
  if(!body.children.length)body.appendChild(el("div","safe",V2.settings.empty));
  var broken=name==="connect"&&d.hook_health&&!d.hook_health.ok,block=compactBlock("v2-set-"+name,V2.settings[name],el("span","v2-block-hint",V2.settings[name+"Hint"]),body,"v2-group",broken);
  var toggle=block.querySelector(".compact-toggle");toggle.insertBefore(v2Image(g[1],"v2-group-pic"),toggle.firstChild);block.setAttribute("data-v2-section","settings-"+name);app.appendChild(block);});
 // The way back to the classic screens is never folded away.
 if(rows.ui_v2){var mode=el("section","v2-card v2-mode-card");mode.setAttribute("data-v2-section","settings-mode");mode.appendChild(rows.ui_v2);app.appendChild(mode);}}
ICONS.v2radio="M8.5 8.5a5 5 0 0 0 0 7M15.5 8.5a5 5 0 0 1 0 7M5.6 5.6a9 9 0 0 0 0 12.8M18.4 5.6a9 9 0 0 1 0 12.8M12 11a1 1 0 1 0 0 2a1 1 0 0 0 0-2";
ICONS.v2grid="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z";
ICONS.v2briefcase="M3 8h18v12H3zM9 8V5h6v3M3 13h18";
ICONS.v2play="M8 5l11 7-11 7z";
ICONS.v2pause="M7 5h3v14H7zM14 5h3v14h-3z";
ICONS.v2target="M12 3a9 9 0 1 0 0 18a9 9 0 0 0 0-18M12 7a5 5 0 1 0 0 10a5 5 0 0 0 0-10M12 11a1 1 0 1 0 0 2a1 1 0 0 0 0-2";
ICONS.v2pulse="M2 12h4l3-7 4 14 3-7h6";
ICONS.v2next="M5 6l6 6-6 6M12 6l6 6-6 6";
ICONS.v2bulb="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5c.8.8 1 1.6 1 2.5h6c0-.9.2-1.7 1-2.5A6 6 0 0 0 12 3";
ICONS.v2moon="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5";
