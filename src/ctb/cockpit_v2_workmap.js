// --- Work map (a project page card) and the "Good morning" card (Now) --------------------------------------------------
// The data is already in the state poll: cockpit_v2.projects[].work_map (lines the bridge ordered, waiting for the owner
// first) and state.morning (today's briefing, absent once dismissed). Nothing is computed here but the grouping.
var wmOpen={},wmGone="";
function wmT(k){return (V2.workmap||{})[k]||k;}
function wmLine(it){
 var li=el("li","wm-line");li.setAttribute("data-wm-status",it.status);
 var what=el("bdi","wm-what",it.what);what.dir="auto";li.appendChild(what);
 if(it.impact){var im=el("div","wm-impact");im.appendChild(el("span","wm-impact-label",wmT("impact")));var tx=el("bdi",null,it.impact);tx.dir="auto";im.appendChild(tx);li.appendChild(im);}
 return li;}
function v2WorkMapCard(p){
 if(!last||!last.cockpit_v2||last.cockpit_v2.work_map_off)return null;
 var lines=p.work_map||[],card=v2Card("project-workmap",wmT("title"),"list");
 if(!lines.length){card.appendChild(el("div","v2-empty",wmT("empty")));return card;}
 var limit=6,open=!!wmOpen[p.key],shownLines=open?lines:lines.slice(0,limit),status="",ul=null;
 shownLines.forEach(function(it){
  if(it.status!==status){status=it.status;card.appendChild(el("div","v2-sub wm-head wm-"+status,wmT(status)));ul=el("ul","wm-lines");card.appendChild(ul);}
  ul.appendChild(wmLine(it));});
 if(lines.length>limit){var b=el("button","btn sm wm-more",open?wmT("less"):wmT("all")+" ("+nf.format(lines.length)+")");b.type="button";b.setAttribute("data-wm","more");
  b.addEventListener("click",function(){haptic("sel");wmOpen[p.key]=!open;render(last,true);});card.appendChild(b);}
 return card;}
function wmList(title,rows,fmt,proj){
 var box=el("div","wm-sec");box.appendChild(el("div","v2-sub wm-head",title));var ul=el("ul","wm-lines");
 rows.forEach(function(r){var li=el("li","wm-line"),w=el("bdi","wm-what",fmt(r));w.dir="auto";li.appendChild(w);
  var pn=proj?proj(r):"";if(pn){var pe=el("bdi","wm-proj",pn);pe.dir="auto";li.appendChild(pe);}
  if(r.impact){var im=el("div","wm-impact"),tx=el("bdi",null,r.impact);tx.dir="auto";im.appendChild(tx);li.appendChild(im);}ul.appendChild(li);});
 box.appendChild(ul);return box;}
function v2MorningCard(d){
 var m=d&&d.morning;if(!m||wmGone===m.date)return null;
 var card=el("section","v2-morning");card.setAttribute("data-v2-section","morning");card.setAttribute("aria-label",wmT("morning"));
 var head=el("header","wm-mhead"),h=el("h2","wm-mtitle");h.appendChild(icon("v2sun"));h.appendChild(el("span",null,wmT("morning")));head.appendChild(h);
 var x=el("button","btn sm wm-dismiss",wmT("dismiss"));x.type="button";x.setAttribute("data-wm","dismiss");
 x.addEventListener("click",function(){haptic("tap");x.disabled=true;
  post("/api/briefing-dismiss",{}).then(function(r){if(!r.ok)throw r.status;wmGone=m.date;if(d.morning===m)d.morning=null;render(last,true);})
   .catch(function(st){x.disabled=false;toast(st===403?S.reopen:wmT("failed"),true);});});
 head.appendChild(x);card.appendChild(head);
 if(m.schedule.length)card.appendChild(wmList(wmT("schedule"),m.schedule,function(r){return (r.time?r.time+" ":"")+r.title;}));
 else card.appendChild(el("div","wm-none",wmT("noSchedule")));
 if(m.chores.length)card.appendChild(wmList(wmT("chores"),m.chores.map(function(c){return {title:c};}),function(r){return r.title;}));
 if(m.overnight.length)card.appendChild(wmList(wmT("overnight"),m.overnight,function(r){return r.what;}));
 if(m.waiting.length)card.appendChild(wmList(wmT("waiting"),m.waiting,function(r){return r.what;},function(r){return r.what===r.project?"":r.project;}));
 else card.appendChild(el("div","wm-none",wmT("quiet")));
 return card;}
ICONS.v2sun="M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4";
