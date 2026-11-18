// "What HarnessGram adds to your agents" (addons.py): the second group of Settings in the new Cockpit.
// The rows come from POST /api/addons (the same fresh owner login as every action), never from the state poll. The
// bridge decides what is locked; this page only shows it. A switch takes effect at once and offers a one-tap undo.
var vaData=null,vaLoading=false,vaError=0,vaOpen=false,vaSnack=null,vaBusy={};
var VA_ICON={conversation:"vachat",memory:"vkbook",voice:"mic",cockpit:"v2grid",safety:"vkshield"};
function vaT(k){return (V2.addons||{})[k]||k;}
function vaL(){return /^he/.test(String(LANG||""))?"he":"en";}
function vaRerender(){if(last&&view==="settings")render(last,true);}
function vaTitle(a){return a["title_"+vaL()]||a.title_en||a.key;}
// Harness names are English: on a Hebrew page each one sits inside its own isolate, never loose in a Hebrew line.
function vaProviders(list){return (list||[]).map(function(n){return vaL()==="he"?"⁨"+n+"⁩":n;}).join(", ");}
function vaLoad(){vaLoading=true;vaError=0;
 return post("/api/addons",{action:"view"}).then(function(r){if(!r.ok)throw r.status||1;return r.json();})
  .then(function(j){vaData=j;}).catch(function(st){vaError=st||1;if(st===403)toast(S.reopen,true);})
  .then(function(){vaLoading=false;vaRerender();});}
function vaGo(on){haptic("sel");vaOpen=on;if(on&&!vaData&&!vaLoading)vaLoad();if(view!=="settings")switchTo("settings");else vaRerender();window.scrollTo(0,0);}
function vaEntry(){var b=el("button","va-entry");b.type="button";b.setAttribute("data-v2-section","addons-entry");
 b.appendChild(vkImage("gears","vk-entry-art"));var t=el("span","vk-entry-text");t.appendChild(el("b",null,vaT("title")));t.appendChild(el("span",null,vaT("entryHint")));b.appendChild(t);
 var ch=el("span","v2-chev");ch.appendChild(icon("chev"));b.appendChild(ch);b.addEventListener("click",function(){vaGo(true);});return b;}
function vaFind(key){var out=null;((vaData||{}).groups||[]).forEach(function(g){g.items.forEach(function(a){if(a.key===key)out=a;});});return out;}
function vaSnackOff(at){setTimeout(function(){if(vaSnack&&vaSnack.at===at){vaSnack=null;vaRerender();}},6000);}
function vaSet(a,on,undo){if(vaBusy[a.key])return;haptic("tap");vaBusy[a.key]=1;var was=a.on;a.on=on;vaSnack=null;vaRerender();
 post("/api/addons",{action:"set",key:a.key,on:on}).then(function(r){if(!r.ok)throw r.status;return r.json();})
  .then(function(j){delete vaBusy[a.key];a.on=!!j.on;haptic("n","success");
   if(undo)toast(vaT("undone"));
   else{var at=Date.now();vaSnack={key:a.key,prev:!!j.previous,at:at,text:vaT(a.on?"toastOn":"toastOff").replace("#",vaTitle(a))};vaSnackOff(at);}
   vaRerender();})
  .catch(function(st){delete vaBusy[a.key];a.on=was;haptic("n","error");toast(st===403?S.reopen:st===409?vaT("lockedErr"):vaT("failed"),true);vaRerender();});}
function vaSwitch(a){var sw=el("button","sw vk-sw"+(a.on?" on":""));sw.type="button";sw.setAttribute("role","switch");sw.setAttribute("aria-checked",String(!!a.on));
 sw.setAttribute("aria-label",vaTitle(a));sw.setAttribute("data-va-toggle",a.key);sw.appendChild(el("i"));if(vaBusy[a.key])sw.disabled=true;
 sw.addEventListener("click",function(){vaSet(a,!a.on,false);});return sw;}
function vaRow(a){var row=el("div","vk-row va-row"+(!a.on&&!a.locked?" off":""));row.setAttribute("data-va-key",a.key);
 var main=el("div","vk-row-main va-row-main"),ic=el("span","vk-row-icon");ic.appendChild(icon(a.locked?"vklock":VA_ICON[a.group]||"vkinfo"));main.appendChild(ic);
 var tx=el("span","vk-row-text");tx.appendChild(el("b",null,vaTitle(a)));tx.appendChild(el("span","va-text",a["text_"+vaL()]||a.text_en||""));
 var who=el("span","va-providers");who.setAttribute("data-va-providers","");who.textContent=vaT("appliesTo")+": "+vaProviders(a.providers);tx.appendChild(who);
 main.appendChild(tx);row.appendChild(main);
 if(a.locked){var lk=el("span","va-always"+(a.on?"":" off"));lk.setAttribute("data-va-locked","");lk.appendChild(icon("vklock"));lk.appendChild(el("span",null,vaT(a.on?"alwaysOn":"offInConfig")));row.appendChild(lk);}
 else row.appendChild(vaSwitch(a));
 return row;}
function vaGroup(g){var card=el("section","vk-group va-group");card.setAttribute("data-va-group",g.key);
 var h=el("h3","vk-group-head");h.appendChild(icon(VA_ICON[g.key]||"vkinfo"));h.appendChild(el("bdi",null,vaT("group_"+g.key)));card.appendChild(h);
 if(g.key==="safety")card.appendChild(el("p","va-note",vaT("lockedNote")));
 var box=el("div","vk-group-rows");g.items.forEach(function(a){box.appendChild(vaRow(a));});card.appendChild(box);return card;}
function vaSnackBar(page){if(!vaSnack)return;var s=el("div","vk-snack va-snack");s.setAttribute("role","status");s.appendChild(icon("check"));s.appendChild(el("b",null,vaSnack.text));
 var b=el("button","vk-snack-btn",vaT("undo"));b.type="button";b.setAttribute("data-va-undo",vaSnack.key);var key=vaSnack.key,prev=vaSnack.prev;
 b.addEventListener("click",function(){var a=vaFind(key);b.disabled=true;vaSnack=null;if(a)vaSet(a,prev,true);});s.appendChild(b);page.appendChild(s);}
function vaPage(d){var page=el("div","vk-page va-page");page.setAttribute("data-v2-section","addons");app.appendChild(page);
 var setHome=bindBackLayer("va-home",page,true,function(on){if(on){if(!vaOpen){vaOpen=true;vaRerender();}return;}
  vaOpen=false;vaSnack=null;vaRerender();window.scrollTo(0,0);});
 page.appendChild(vkHead("gears",vaT("title"),vaT("subtitle"),setHome,"vk-home-head"));
 if(!vaData){var box=el("div","vk-loading");box.setAttribute("data-va-loading","");
  if(vaError&&!vaLoading){box.appendChild(el("p",null,vaT("loadFailed")));var r=el("button","vk-send",vaT("retry"));r.type="button";r.addEventListener("click",function(){vaLoad();vaRerender();});box.appendChild(r);}
  else{box.appendChild(el("span","dotpulse"));box.appendChild(el("span",null,vaT("loading")));if(!vaLoading)vaLoad();}
  page.appendChild(box);return;}
 var now=el("div","va-now");now.appendChild(icon("clock"));now.appendChild(el("span",null,vaT("now")));page.appendChild(now);
 (vaData.groups||[]).forEach(function(g){if(g.items&&g.items.length)page.appendChild(vaGroup(g));});
 vaSnackBar(page);}
ICONS.vachat="M4 5h16v11H9l-5 4zM8 9h8M8 12h5";
