// "What the system knows" (knowledge.py): the first group of Settings in the new Cockpit.
// Owner-only data comes from POST /api/knowledge (the same fresh owner login as every action) and is never part of
// the state poll. The bridge decides what is locked; this page only shows it. A switch takes effect from the next
// conversation, because the agents read the generated overlay when a session starts.
var vkData=null,vkLoading=false,vkError=0,vkNav={open:false,kind:"",vault:false,history:false,item:""},vkLast={},vkQuery={},vkChip={},vkHist="all",vkSnack=null,vkDraft={},vkSheetY=0;
var VK_KINDS=["rule","memory","doc","tool","skill"],VK_ART={rule:"k-rules",memory:"k-memories",tool:"k-tools",doc:"k-docs",skill:"k-skills"};
var VK_ICON={rule:"vkscale",memory:"vkimage",tool:"vkwrench",doc:"vkdoc",skill:"vkribbon"},VK_HEAD={rule:"k-clipboard",memory:"k-memories",tool:"k-tools",doc:"k-docs",skill:"k-skills"};
function vkT(k){return (V2.knowledge||{})[k]||k;}
// The bridge withholds a folder name it cannot show safely; it is then simply "a private folder".
function vkGroup(i){return i.group==="\u0001private"?vkT("privateFolder"):i.group||vkT("general");}
// Plain-words notes (knowledge.py): an agent wrote a Hebrew and an English title, one sentence and an advice per item.
// Without a note the raw source words are shown, in their own element and direction, never inside a Hebrew line.
var VK_ADVICE=["keep","optional","review"];
function vkL(){return /^he/.test(String(LANG||""))?"he":"en";}
function vkNote(i){var n=i&&i.note;return n&&typeof n==="object"&&typeof n["title_"+vkL()]==="string"&&n["title_"+vkL()]?n:null;}
function vkShown(i){var n=vkNote(i),l=vkL();
 // A locked safety rule always says keep, with the fixed reason: an agent's words never explain it away.
 if(n)return {title:n["title_"+l],summary:n["summary_"+l]||"",why:i.locked?vkT("lockedWhy"):n["why_"+l]||"",advice:i.locked?"keep":VK_ADVICE.indexOf(n.advice)>=0?n.advice:"",note:true};
 return {title:i.title||"",summary:i.summary||"",why:"",advice:"",note:false};}
function vkDirOf(text){return /[\u0590-\u05FF]/.test(text||"")?"rtl":"ltr";}
function vkText(tag,cls,text,raw){var e=el(tag,cls,text);if(raw){e.dir=vkDirOf(text);e.classList.add("vk-raw");}return e;}
function vkAdvice(a){var c=el("span","vk-advice vk-advice-"+a,vkT("advice_"+a));c.setAttribute("data-vk-advice",a);return c;}
// A heading's note title, or the heading as it is (raw) when there is none.
function vkGroupLabel(g){var n=((vkData||{}).sections||{})[g],t=n&&n["title_"+vkL()];
 if(typeof t==="string"&&t)return {text:t,raw:false};return {text:g,raw:g!==vkT("general")&&g!==vkT("privateFolder")};}
function vkImage(name,cls){var img=el("img",cls);img.src=V2_ASSET+name+".webp";img.alt="";img.decoding="async";return img;}
function vkRerender(){if(last&&view==="settings")render(last,true);}
// One navigation step: Settings -> home -> (a list, the vault or history) -> one item's sheet.
function vkGo(patch){Object.keys(patch).forEach(function(k){vkNav[k]=patch[k];});vkNav.open=true;haptic("sel");
 if(!vkData&&!vkLoading)vkLoad(false);if(view!=="settings")switchTo("settings");else vkRerender();if(!patch.item)window.scrollTo(0,0);}
function vkLoad(rescan){vkLoading=true;vkError=0;
 return post("/api/knowledge",{action:rescan?"scan":"view"}).then(function(r){if(!r.ok)throw r.status||1;return r.json();})
  .then(function(j){vkData=j;}).catch(function(st){vkError=st||1;if(st===403)toast(S.reopen,true);})
  .then(function(){vkLoading=false;vkRerender();});}
function vkItems(kind){return ((vkData&&vkData.items)||[]).filter(function(i){return !kind||i.kind===kind;});}
function vkItem(id){return vkItems().filter(function(i){return i.id===id;})[0]||null;}
function vkCountText(kind){var c=((vkData||{}).counts||{})[kind]||{total:0,on:0};
 if(kind==="doc")return nf.format(c.total)+" "+vkT("guides");
 if(kind==="tool")return nf.format(c.total)+" · "+nf.format(c.on)+" "+vkT("connected");
 return nf.format(c.total)+" · "+nf.format(c.on)+" "+vkT(kind==="skill"?"activeF":"active");}
function vkFill(text,n){return text.replace("#",nf.format(n));}
function vkBack(set){var b=el("button","vk-back");b.type="button";b.setAttribute("aria-label",vkT("back"));b.setAttribute("data-vk-back","");b.appendChild(icon("vkarrow"));
 b.addEventListener("click",function(){haptic("sel");set(false);});return b;}
function vkHead(art,title,sub,set,cls){var h=el("header","vk-head"+(cls?" "+cls:""));h.appendChild(vkImage(art,"vk-head-art"));
 var t=el("div","vk-head-text");t.appendChild(el("h2",null,title));if(sub)t.appendChild(el("p",null,sub));h.appendChild(t);h.appendChild(vkBack(set));return h;}
// An empty field takes the page's direction, so a Hebrew placeholder sits on the right; typed text decides after.
function vkSearch(key,placeholder,onInput){var box=el("label","vk-search"),ta=el("textarea");ta.rows=1;ta.maxLength=80;ta.placeholder=placeholder;
 ta.setAttribute("aria-label",placeholder);ta.setAttribute("data-vk-search",key);ta.value=vkQuery[key]||"";box.appendChild(icon("vksearch"));box.appendChild(ta);tune(ta,true);
 ta.addEventListener("keydown",function(e){if(e.key==="Enter")e.preventDefault();});
 ta.addEventListener("input",function(){vkQuery[key]=ta.value.replace(/\n/g," ");onInput(vkQuery[key]);});return box;}
function vkMatch(i,q){q=(q||"").trim().toLowerCase();if(!q)return true;var n=i.note||{},s=((vkData||{}).sections||{})[i.group]||{};
 return [i.title,i.summary,i.text,i.group,i.id,n.title_he,n.summary_he,n.title_en,n.summary_en,s.title_he,s.title_en].some(function(v){return typeof v==="string"&&v.toLowerCase().indexOf(q)>=0;});}
function vkSwitch(i){var sw=el("button","sw vk-sw"+(i.on?" on":""));sw.type="button";sw.setAttribute("role","switch");sw.setAttribute("aria-checked",String(!!i.on));
 sw.setAttribute("aria-label",vkShown(i).title);sw.setAttribute("data-vk-toggle",i.id);sw.appendChild(el("i"));
 sw.addEventListener("click",function(){vkToggle(i,!i.on,sw);});return sw;}
function vkToggle(i,on,sw){haptic("tap");sw.disabled=true;sw.className="sw vk-sw"+(on?" on":"");sw.setAttribute("aria-checked",String(on));
 post("/api/knowledge",{action:"toggle",id:i.id,on:on}).then(function(r){if(!r.ok)throw r.status;return r.json();})
  .then(function(){i.on=on;haptic("n","success");toast(vkT(on?"toastOn":"toastOff"));vkLoad(false);})
  .catch(function(st){haptic("n","error");toast(st===403?S.reopen:st===409?vkT("lockedErr"):vkT("failed"),true);vkRerender();});}
function vkRow(i,bare){var row=el("div","vk-row");row.setAttribute("data-vk-item",i.id);
 var main=el("button","vk-row-main");main.type="button";if(!bare){var ic=el("span","vk-row-icon");ic.appendChild(icon(i.locked?"vklock":VK_ICON[i.kind]));main.appendChild(ic);}
 var sh=vkShown(i),tx=el("span","vk-row-text");tx.appendChild(vkText("b",null,sh.title,!sh.note));
 var sum=sh.summary||(i.kind==="tool"?(i.agents||[]).join(", "):"");if(sum)tx.appendChild(vkText("span",null,sum,!sh.note||!sh.summary));
 if(sh.advice&&!i.locked)tx.appendChild(vkAdvice(sh.advice));main.appendChild(tx);
 main.addEventListener("click",function(){vkGo({item:i.id});});row.appendChild(main);
 if(i.locked){var lk=el("span","vk-locked");lk.appendChild(icon("vklock"));lk.setAttribute("aria-label",vkT("locked"));row.appendChild(lk);}else row.appendChild(vkSwitch(i));
 if(!i.on&&!i.locked)row.classList.add("off");return row;}
// --- home ---------------------------------------------------------------------------------------------------------
function vkEntry(){var b=el("button","vk-entry");b.type="button";b.setAttribute("data-v2-section","knowledge-entry");
 b.appendChild(vkImage("k-brain","vk-entry-art"));var t=el("span","vk-entry-text");t.appendChild(el("b",null,vkT("title")));t.appendChild(el("span",null,vkT("entryHint")));b.appendChild(t);
 var ch=el("span","v2-chev");ch.appendChild(icon("chev"));b.appendChild(ch);b.addEventListener("click",function(){vkGo({open:true});});return b;}
function vkStatus(){var d=vkData,b=el("button","vk-status");b.type="button";b.setAttribute("data-vk-status","");
 b.appendChild(icon("check"));
 var age=Math.max(0,(d.now||0)-(d.scanned||0));b.appendChild(el("span",null,vkT("upToDate")+" · "+ago(age)));
 b.setAttribute("aria-description",vkT("rescan"));b.addEventListener("click",function(){haptic("tap");vkLoad(true);});return b;}
function vkTile(kind){var b=el("button","vk-tile"+(kind==="skill"?" wide":""));b.type="button";b.setAttribute("data-vk-kind",kind);
 b.appendChild(vkImage(VK_ART[kind],"vk-tile-art"));var cap=el("span","vk-tile-cap"),ic=el("span","vk-tile-icon");ic.appendChild(icon(VK_ICON[kind]));cap.appendChild(ic);
 var tx=el("span","vk-tile-text");tx.appendChild(el("b",null,vkT("kind_"+kind)));tx.appendChild(el("span",null,vkCountText(kind)));cap.appendChild(tx);b.appendChild(cap);
 b.addEventListener("click",function(){vkGo({kind:kind,vault:false,history:false,item:""});});return b;}
function vkHome(page,setHome){var d=vkData;
 page.appendChild(vkHead("k-brain",vkT("title"),vkT("subtitle"),setHome,"vk-home-head"));
 var results=el("div","vk-results");results.setAttribute("data-vk-results","");
 var body=el("div","vk-home-body");
 function draw(q){results.textContent="";var hits=q.trim()?vkItems().filter(function(i){return vkMatch(i,q);}):[];body.hidden=!!q.trim();
  if(q.trim()){results.appendChild(el("div","vk-sub",vkT("results")+" · "+nf.format(hits.length)));var box=el("div","vk-group-rows");hits.slice(0,60).forEach(function(i){box.appendChild(vkRow(i));});
   if(!hits.length)box.appendChild(el("div","v2-empty",vkT("noResults")));results.appendChild(box);}}
 page.appendChild(vkSearch("home",vkT("search"),draw));page.appendChild(vkStatus());
 var grid=el("div","vk-tiles");grid.setAttribute("data-vk-section","tiles");VK_KINDS.forEach(function(k){grid.appendChild(vkTile(k));});body.appendChild(grid);
 var vault=el("button","vk-vault-tile");vault.type="button";vault.setAttribute("data-vk-open","vault");vault.appendChild(vkImage("k-lock","vk-vault-art"));
 var vt=el("span","vk-vault-text"),vh=el("b");vh.appendChild(icon("vkshield"));vh.appendChild(el("span",null,vkT("vault")));vt.appendChild(vh);vt.appendChild(el("span",null,vkFill(vkT("vaultHint"),d.locked||0)));vault.appendChild(vt);
 vault.addEventListener("click",function(){vkGo({vault:true,kind:"",history:false,item:""});});body.appendChild(vault);
 var hist=el("button","vk-history-row");hist.type="button";hist.setAttribute("data-vk-open","history");hist.appendChild(icon("clock"));
 hist.appendChild(el("b",null,d.week?(d.week===1?vkT("changesWeek1"):vkFill(vkT("changesWeek"),d.week)):vkT("noChanges")));hist.appendChild(el("span","vk-link",vkT("toHistory")));
 hist.addEventListener("click",function(){vkGo({history:true,kind:"",vault:false,item:""});});body.appendChild(hist);
 page.appendChild(results);page.appendChild(body);draw(vkQuery.home||"");}
// --- one kind -----------------------------------------------------------------------------------------------------
function vkList(page,kind,set){var all=vkItems(kind),open=all.filter(function(i){return !i.locked;}),c=((vkData||{}).counts||{})[kind]||{total:0,on:0};
 var sec=el("section","vk-list");sec.setAttribute("data-vk-section","list");sec.setAttribute("data-vk-kind",kind);
 sec.appendChild(vkHead(VK_HEAD[kind],vkT("kind_"+kind),nf.format(c.total)+" "+vkT("unit_"+kind)+" · "+nf.format(c.on)+" "+vkT(kind==="tool"?"connected":kind==="skill"?"activeF":"active"),set));
 var groups=[],seen={};open.forEach(function(i){var g=vkGroup(i);if(!seen[g]){seen[g]=[];groups.push(g);}seen[g].push(i);});
 var chips=el("div","vk-chips");chips.setAttribute("role","group");chips.setAttribute("aria-label",vkT("kind_"+kind));var cur=vkChip[kind]||"";if(groups.indexOf(cur)<0)cur="";
 var listBox=el("div","vk-groups");
 function draw(){listBox.textContent="";var q=vkQuery[kind]||"",any=false;
  groups.forEach(function(g){if(cur&&g!==cur)return;var rows=seen[g].filter(function(i){return vkMatch(i,q);});if(!rows.length)return;any=true;
   var card=el("section","vk-group");card.setAttribute("data-vk-group",g);var h=el("h3","vk-group-head");h.appendChild(icon(VK_ICON[kind]));var gl=vkGroupLabel(g),gt=vkText("bdi",null,gl.text,gl.raw);h.appendChild(gt);h.appendChild(el("span","vk-group-n",nf.format(rows.length)));card.appendChild(h);
   var box=el("div","vk-group-rows");rows.forEach(function(i){box.appendChild(vkRow(i,true));});card.appendChild(box);listBox.appendChild(card);});
  if(!any)listBox.appendChild(el("div","v2-empty",q.trim()?vkT("noResults"):vkT("empty")));}
 [""].concat(groups).forEach(function(g){var b=el("button","vk-chip"+(g===cur?" on":""));b.type="button";b.setAttribute("aria-pressed",String(g===cur));b.setAttribute("data-vk-chip",g);
  var gl=g?vkGroupLabel(g):{text:vkT("all"),raw:false},lb=vkText("bdi",null,gl.text,gl.raw);b.appendChild(lb);
  b.addEventListener("click",function(){cur=vkChip[kind]=g;haptic("sel");Array.prototype.forEach.call(chips.children,function(x){var on=x===b;x.classList.toggle("on",on);x.setAttribute("aria-pressed",String(on));});draw();});chips.appendChild(b);});
 sec.appendChild(vkSearch(kind,vkT("searchIn").replace("#",vkT("kind_"+kind)),draw));if(groups.length>1)sec.appendChild(chips);sec.appendChild(listBox);draw();
 var locked=all.length-open.length;
 if(locked){var lr=el("button","vk-locked-row");lr.type="button";lr.setAttribute("data-vk-open","vault");lr.appendChild(icon("vklock"));lr.appendChild(el("b",null,vkFill(vkT("vaultLine"),locked)));var ch=el("span","v2-chev");ch.appendChild(icon("chev"));lr.appendChild(ch);
  lr.addEventListener("click",function(){vkGo({vault:true,kind:"",history:false,item:""});});sec.appendChild(lr);}
 page.appendChild(sec);return sec;}
// --- the vault ----------------------------------------------------------------------------------------------------
function vkVault(page,set){var sec=el("section","vk-vault");sec.setAttribute("data-vk-section","vault");
 var hero=el("header","vk-vault-hero");hero.appendChild(vkImage("k-vault","vk-vault-hero-art"));hero.appendChild(vkBack(set));sec.appendChild(hero);
 sec.appendChild(el("h2","vk-vault-title",vkT("vault")));sec.appendChild(el("p","vk-vault-lead",vkT("vaultText")));
 var box=el("div","vk-vault-list"),locked=vkItems().filter(function(i){return i.locked;});
 locked.forEach(function(i){var b=el("button","vk-vault-row");b.type="button";b.setAttribute("data-vk-item",i.id);var lk=el("span","vk-vault-lock");lk.appendChild(icon("vklock"));b.appendChild(lk);
  var sh=vkShown(i),tx=el("span","vk-row-text");tx.appendChild(vkText("b",null,sh.title,!sh.note));if(sh.summary&&sh.summary!==sh.title)tx.appendChild(vkText("span",null,sh.summary,!sh.note));b.appendChild(tx);
  b.addEventListener("click",function(){vkGo({item:i.id});});box.appendChild(b);});
 if(!locked.length)box.appendChild(el("div","v2-empty",vkT("empty")));sec.appendChild(box);
 var note=el("div","vk-note");note.appendChild(icon("vkinfo"));note.appendChild(el("span",null,vkT("vaultNote")));sec.appendChild(note);page.appendChild(sec);return sec;}
// --- history ------------------------------------------------------------------------------------------------------
function vkBucket(at,now){var d=new Date(at*1000),n=new Date(now*1000),day=86400000,start=new Date(n.getFullYear(),n.getMonth(),n.getDate()).getTime();
 return d.getTime()>=start?"today":d.getTime()>=start-day?"yesterday":d.getTime()>=start-6*day?"week":"older";}
function vkUndo(id,snack){haptic("tap");
 post("/api/knowledge",{action:"undo",log:id}).then(function(r){if(!r.ok)throw r.status;return r.json();})
  .then(function(j){haptic("n","success");vkSnack=snack?{text:vkT("undoneSnack"),redo:j.entry&&j.entry.id,at:Date.now()}:null;if(!snack)toast(vkT("nextSession"));
   setTimeout(function(){if(vkSnack&&Date.now()-vkSnack.at>=5900){vkSnack=null;vkRerender();}},6000);vkLoad(false);})
  .catch(function(st){haptic("n","error");toast(st===403?S.reopen:st===409?vkT("changed"):vkT("failed"),true);vkLoad(false);});}
function vkHistory(page,set){var sec=el("section","vk-history");sec.setAttribute("data-vk-section","history");var now=(vkData||{}).now||Date.now()/1000;
 sec.appendChild(vkHead("k-calendar",vkT("historyTitle"),vkT("historySub"),set));
 var seg=el("div","vk-seg");seg.setAttribute("role","group");seg.setAttribute("aria-label",vkT("historyTitle"));
 [["all","all"],["mine","mine"],["team","team"]].forEach(function(p){var b=el("button","vk-chip"+(vkHist===p[0]?" on":""),vkT(p[1]));b.type="button";b.setAttribute("aria-pressed",String(vkHist===p[0]));b.setAttribute("data-vk-filter",p[0]);
  b.addEventListener("click",function(){vkHist=p[0];haptic("sel");vkRerender();});seg.appendChild(b);});sec.appendChild(seg);
 var rows=((vkData||{}).history||[]).filter(function(e){return vkHist==="all"||(vkHist==="mine")===(e.actor==="owner");}),groups={},order=["today","yesterday","week","older"];
 rows.forEach(function(e){var k=vkBucket(e.at,now);(groups[k]=groups[k]||[]).push(e);});
 order.forEach(function(k){if(!groups[k])return;var h=el("h3","vk-day");h.appendChild(icon("vkcalendar"));h.appendChild(el("span",null,vkT(k)));sec.appendChild(h);
  var card=el("div","vk-group-rows vk-hist-card");groups[k].forEach(function(e){var r=el("div","vk-hist");r.setAttribute("data-vk-log",e.id);
   var ic=el("span","vk-row-icon");ic.appendChild(icon(e.action==="suggest"?"pencil":VK_ICON[e.kind]||"vkdoc"));r.appendChild(ic);
   var owner=e.actor==="owner",verb=e.action==="suggest"?(owner?"didSuggest":"teamSuggest"):e.after?(owner?"didOn":"teamOn"):(owner?"didOff":"teamOff");
   var hi=vkItem(e.item),hs=hi?vkShown(hi):{title:e.title||"",note:false},tx=el("span","vk-row-text"),t=el("b");
   // A raw (English) title gets its own line on a Hebrew page; a note's title stays in the sentence.
   if(hs.note||vkL()!=="he"){t.appendChild(el("span",null,vkT(verb)+": "));t.appendChild(vkText("bdi",null,hs.title,!hs.note));tx.appendChild(t);}
   else{t.appendChild(el("span",null,vkT(verb)));tx.appendChild(t);tx.appendChild(vkText("span","vk-raw-line",hs.title,true));}
   var at=new Date(e.at*1000),time=k==="today"||k==="yesterday"?at.toLocaleTimeString(LANG,{hour:"2-digit",minute:"2-digit"}):at.toLocaleDateString(LANG,{day:"numeric",month:"short"});
   tx.appendChild(el("span",null,time+" · "+vkT(owner?"byYou":"byTeam")));r.appendChild(tx);
   if(e.action==="toggle"){if(e.undone){r.appendChild(el("span","vk-undone",vkT("undone")));}
    else{var u=el("button","vk-undo",vkT("undo"));u.type="button";u.setAttribute("data-vk-undo",e.id);u.addEventListener("click",function(){u.disabled=true;vkUndo(e.id,true);});r.appendChild(u);}}
   card.appendChild(r);});sec.appendChild(card);});
 if(!rows.length)sec.appendChild(el("div","v2-empty",vkT("noHistory")));page.appendChild(sec);return sec;}
function vkSnackBar(page){if(!vkSnack)return;var s=el("div","vk-snack");s.setAttribute("role","status");s.appendChild(icon("check"));s.appendChild(el("b",null,vkSnack.text));
 if(vkSnack.redo){var b=el("button","vk-snack-btn",vkT("redo"));b.type="button";b.setAttribute("data-vk-redo",vkSnack.redo);var id=vkSnack.redo;b.addEventListener("click",function(){b.disabled=true;vkSnack=null;vkUndo(id,false);});s.appendChild(b);}
 page.appendChild(s);}
// --- one item -----------------------------------------------------------------------------------------------------
// A file path (and a raw English heading) is English: each gets its own left-to-right line, never mixed into Hebrew.
function vkFact(ic,label,value,path){var r=el("div","vk-fact");var i=el("span","vk-fact-icon");i.appendChild(icon(ic));r.appendChild(i);var lines=el("span","vk-fact-lines"),t=el("span");t.appendChild(el("b",null,label+": "));
 var v=el("bdi",null,value);t.appendChild(v);lines.appendChild(t);[].concat(path||[]).forEach(function(x){if(!x)return;var p=el("span","vk-path",x);p.dir=vkDirOf(x);lines.appendChild(p);});r.appendChild(lines);return r;}
function vkAgents(list){var names={claude:"Claude",codex:"Codex",agy:"AGY"};list=list||[];
 return list.length>=3&&["claude","codex","agy"].every(function(a){return list.indexOf(a)>=0;})?vkT("allAgents"):list.map(function(a){return names[a]||a;}).join(", ");}
function vkSheet(page,i,set){var shown=vkShown(i);var shade=el("div","vk-shade");shade.addEventListener("click",function(){set(false);});page.appendChild(shade);
 var sh=el("section","vk-sheet");sh.setAttribute("role","dialog");sh.setAttribute("aria-modal","true");sh.setAttribute("aria-label",shown.title);sh.setAttribute("data-vk-sheet",i.id);
 var close=el("button","vk-grab");close.type="button";close.setAttribute("aria-label",vkT("closeSheet"));close.appendChild(el("i"));close.addEventListener("click",function(){haptic("sel");set(false);});sh.appendChild(close);
 sh.addEventListener("keydown",function(e){if(e.key==="Escape"){e.preventDefault();set(false);}});
 var head=el("div","vk-sheet-head"),hi=el("span","vk-sheet-icon");hi.appendChild(icon(i.locked?"vklock":VK_ICON[i.kind]));head.appendChild(hi);head.appendChild(vkText("h2",null,shown.title,!shown.note));sh.appendChild(head);
 if(i.kind==="skill"&&i.internal){var badge=el("div","vk-internal");badge.appendChild(icon("vkribbon"));var bt=el("span");bt.appendChild(el("b",null,vkT("internal")));bt.appendChild(el("span",null,vkT("internalHint")));badge.appendChild(bt);sh.appendChild(badge);}
 if(i.locked){var ln=el("div","vk-note locked");ln.appendChild(icon("vklock"));ln.appendChild(el("span",null,vkT("lockedNote")));sh.appendChild(ln);}
 else{var tr=el("div","vk-toggle-row");tr.appendChild(el("b",null,vkT(i.on?"on":"off")));tr.appendChild(vkSwitch(i));sh.appendChild(tr);}
 if(shown.advice){var ab=el("div","vk-advice-box");ab.setAttribute("data-vk-why","");ab.appendChild(vkAdvice(shown.advice));if(shown.why)ab.appendChild(el("p",null,shown.why));sh.appendChild(ab);}
 var body=el("div","vk-text-card"),raw=i.text||i.summary||i.title;
 if(shown.note){if(shown.summary)body.appendChild(el("p",null,shown.summary));
  if(raw){body.appendChild(el("span","vk-orig-label",vkT("original")));body.appendChild(vkText("p","vk-orig",raw,true));}}
 else body.appendChild(vkText("p",null,raw,true));
 if(!i.locked){var ed=el("button","vk-edit");ed.type="button";ed.appendChild(icon("pencil"));ed.appendChild(el("span",null,vkT("edit")));ed.addEventListener("click",function(){var f=sh.querySelector("textarea");if(f){f.focus();if(f.scrollIntoView)f.scrollIntoView({block:"center"});}});body.appendChild(ed);}
 sh.appendChild(body);
 var src=i.source||{},sl=src.section?vkGroupLabel(src.section):null,where=sl&&!sl.raw?sl.text:vkT("whereFrom");
 sh.appendChild(vkFact("vkbook",vkT("source"),where+(src.line>1?" · "+vkT("line")+" "+nf.format(src.line):""),[sl&&sl.raw?sl.text:"",src.path]));
 if(i.date)sh.appendChild(vkFact("vkcalendar",vkT("added"),new Date(i.date*1000).toLocaleDateString(LANG,{day:"numeric",month:"long",year:"numeric"})));
 sh.appendChild(vkFact("vkpeople",vkT("appliesTo"),vkAgents(i.agents)));
 if(i.kind==="tool"&&(i.native_off||[]).length)sh.appendChild(vkFact("vkwarn",vkT("off"),vkT("nativeOff")+" ("+vkAgents(i.native_off)+")"));
 // Every switch reaches the agents through the overlay they read when a conversation starts.
 if(!i.locked){var ns=el("div","vk-next");ns.setAttribute("data-vk-next","");ns.appendChild(icon("clock"));ns.appendChild(el("span",null,vkT("nextSession")));sh.appendChild(ns);
  sh.appendChild(el("h3","vk-suggest-title",vkT("suggestTitle")));var ta=el("textarea","vk-suggest");ta.rows=2;ta.maxLength=1000;ta.placeholder=vkT("suggestPh");ta.setAttribute("aria-label",vkT("suggestTitle"));
  ta.value=vkDraft[i.id]||"";tune(ta,true);sh.appendChild(ta);var send=el("button","vk-send",vkT("suggestSend"));send.type="button";send.setAttribute("data-vk-suggest",i.id);send.disabled=!ta.value.trim();
  ta.addEventListener("input",function(){vkDraft[i.id]=ta.value;send.disabled=!ta.value.trim();});
  send.addEventListener("click",function(){var text=ta.value.trim();if(!text)return;send.disabled=true;haptic("tap");
   post("/api/knowledge",{action:"suggest",id:i.id,text:text}).then(function(r){if(!r.ok)throw r.status;return r.json();})
    .then(function(){haptic("n","success");delete vkDraft[i.id];ta.value="";toast(vkT("suggestSent"));vkLoad(false);})
    .catch(function(st){haptic("n","error");send.disabled=false;toast(st===403?S.reopen:st===409?vkT("lockedErr"):vkT("failed"),true);});});
  sh.appendChild(send);sh.appendChild(el("p","vk-small",vkT("suggestNote")));}
 page.appendChild(sh);return sh;}
// --- the page and its back steps ------------------------------------------------------------------------------------
function vkPage(d){
 var page=el("div","vk-page");page.setAttribute("data-v2-section","knowledge");app.appendChild(page);
 var setHome=bindBackLayer("vk-home",page,true,function(on){
  if(on){if(!vkNav.open){vkNav.open=true;vkRerender();}return;}
  vkNav={open:false,kind:"",vault:false,history:false,item:""};vkRerender();window.scrollTo(0,0);});
 if(!vkData){var box=el("div","vk-loading");box.setAttribute("data-vk-loading","");
  page.appendChild(vkHead("k-brain",vkT("title"),vkT("subtitle"),setHome,"vk-home-head"));
  if(vkError&&!vkLoading){box.appendChild(el("p",null,vkT("loadFailed")));var r=el("button","vk-send",vkT("retry"));r.type="button";r.addEventListener("click",function(){vkLoad(false);vkRerender();});box.appendChild(r);}
  else{box.appendChild(el("span","dotpulse"));box.appendChild(el("span",null,vkT("loading")));if(!vkLoading)vkLoad(false);}
  page.appendChild(box);return;}
 var sub=vkNav.kind?"kind":vkNav.vault?"vault":vkNav.history?"history":"";
 var setSub=function(on){if(!on){vkLast.sub={kind:vkNav.kind,vault:vkNav.vault,history:vkNav.history};vkNav.kind="";vkNav.vault=vkNav.history=false;vkNav.item="";vkRerender();window.scrollTo(0,0);}};
 var node;
 if(sub==="kind")node=vkList(page,vkNav.kind,function(v){bound(v);});
 else if(sub==="vault")node=vkVault(page,function(v){bound(v);});
 else if(sub==="history")node=vkHistory(page,function(v){bound(v);});
 else vkHome(page,setHome);
 var bound=node?bindBackLayer("vk-sub",node,true,function(on){
  if(on){if(!vkNav.kind&&!vkNav.vault&&!vkNav.history&&vkLast.sub){vkNav.kind=vkLast.sub.kind;vkNav.vault=vkLast.sub.vault;vkNav.history=vkLast.sub.history;vkRerender();}return;}
  setSub(false);}):null;
 var item=vkNav.item&&vkItem(vkNav.item);
 if(vkNav.item&&!item)vkNav.item="";
 if(item){var sheet=vkSheet(page,item,function(v){sheetSet(v);});document.documentElement.classList.add("vk-sheet-open");
  var sheetSet=bindBackLayer("vk-sheet",sheet,true,function(on){
   if(on){if(!vkNav.item&&vkLast.item){vkNav.item=vkLast.item;vkRerender();}return;}
   vkLast.item=vkNav.item;vkNav.item="";document.documentElement.classList.remove("vk-sheet-open");vkRerender();});}
 else document.documentElement.classList.remove("vk-sheet-open");
 vkSnackBar(page);}
ICONS.vkarrow="M5 12h14M13 6l6 6-6 6";
ICONS.vksearch="M11 4a7 7 0 1 0 0 14a7 7 0 0 0 0-14M16 16l5 5";
ICONS.vkscale="M12 4v16M7 20h10M5 7h14M5 7l-3 7h6zM19 7l-3 7h6z";
ICONS.vkimage="M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4M15 9h.01";
ICONS.vkwrench="M14 5a5 5 0 0 0-6 6L3 16l5 5 5-5a5 5 0 0 0 6-6l-3 3-4-4z";
ICONS.vkdoc="M6 3h8l4 4v14H6zM14 3v4h4M9 12h6M9 16h6";
ICONS.vkribbon="M12 3a6 6 0 1 0 0 12a6 6 0 0 0 0-12M8.5 13.5L7 21l5-3 5 3-1.5-7.5";
ICONS.vkshield="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM9 12l2 2 4-4";
ICONS.vklock="M6 10h12v11H6zM8 10V7a4 4 0 0 1 8 0v3M12 14v3";
ICONS.vkinfo="M12 3a9 9 0 1 0 0 18a9 9 0 0 0 0-18M12 11v6M12 7.5h.01";
ICONS.vkwarn="M12 3l10 18H2zM12 10v5M12 18h.01";
ICONS.vkcalendar="M4 6h16v15H4zM4 10h16M8 3v4M16 3v4";
ICONS.vkbook="M3 5c3-1 6-1 9 1 3-2 6-2 9-1v14c-3-1-6-1-9 1-3-2-6-2-9-1zM12 6v14";
ICONS.vkpeople="M9 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6M3 20a6 6 0 0 1 12 0M17 11a2.5 2.5 0 1 0 0-5M16 14a5 5 0 0 1 5 6";
