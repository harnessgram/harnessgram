// --- The family board: today's chores and routine inside "My schedule", and the board sheet ------------------------------
// The data comes from POST /api/family-view (owner login, never part of the state poll); changes go to POST /api/family
// and answer with the new view. The bridge decides what is done today or this week and whether it is Shabbat; this page
// only shows it. The sheet holds the week (tabs on phones, a grid on wide screens), the daily routine, the shopping list
// and the meal plan, one field for adding or editing, and a print view.
var fbData=null,fbLoading=false,fbAt=0,fbErr=0,fbTab="week",fbDay=-1,fbBusy={},fbSheet=null,fbEdit=null,fbUndo=null,fbSaving=false;
function fbT(k){return (V2.family||{})[k]||k;}
function fbRerender(){if(last&&view==="me")render(last,true);if(fbSheet)fbSheet.paint();}
// A failed load is not retried until the next minute, so a render can never loop on an error.
function fbLoad(force){
 if(fbLoading||(!force&&fbAt&&Date.now()-fbAt<60000))return;fbLoading=true;
 post("/api/family-view",{}).then(function(r){if(!r.ok)throw r.status||1;return r.json();})
  .then(function(j){fbData=j.view||null;fbErr=fbData?0:1;})
  .catch(function(st){fbErr=st||1;if(st===403)toast(S.reopen,true);})
  .then(function(){fbLoading=false;fbAt=Date.now();fbRerender();});}
function fbTake(j){if(j&&j.view){fbData=j.view;fbErr=0;fbAt=Date.now();}}
function fbSend(body){
 return post("/api/family",body).then(function(r){return r.json().catch(function(){return {};}).then(function(j){
  if(r.ok&&j.ok){fbTake(j);return j;}
  var e=new Error(j.error||"failed");e.status=r.status;e.code=j.error;throw e;});});}
function fbFail(e){haptic("n","error");toast(e&&e.status===403?S.reopen:e&&e.code==="shabbat"?fbT("shabbatLocked"):e&&e.code==="full"?fbT("full"):e&&e.status===400?fbT("bad"):fbT("failed"),true);}
function fbToggle(it,btn){
 if(fbBusy[it.id])return;fbBusy[it.id]=1;btn.disabled=true;haptic("tap");
 fbSend({action:"check",id:it.id,done:!it.done}).then(function(){delete fbBusy[it.id];haptic("n","success");fbRerender();})
  .catch(function(e){delete fbBusy[it.id];btn.disabled=false;fbFail(e);});}
function fbRow(it,opt){
 var li=el("li","fb-row"+(it.done?" done":"")+(opt.check?"":" plain"));li.setAttribute("data-fb-item",it.id);
 if(opt.check){
  var b=el("button","fb-check"+(it.done?" on":""));b.type="button";b.setAttribute("data-fb-done",it.id);b.setAttribute("aria-pressed",String(!!it.done));
  b.setAttribute("aria-label",(it.done?fbT("undo"):fbT("done"))+": "+it.text);b.appendChild(icon(it.done?"v2undo":"check"));
  if(opt.locked){b.disabled=true;b.setAttribute("aria-disabled","true");}
  b.addEventListener("click",function(){fbToggle(it,b);});li.appendChild(b);}
 var t=el("bdi","fb-text",it.text);t.dir="auto";li.appendChild(t);
 if(opt.edit){var e=el("button","fb-edit");e.type="button";e.setAttribute("data-fb-edit",it.id);e.setAttribute("aria-label",fbT("edit")+": "+it.text);e.appendChild(icon("v2pencil"));
  e.addEventListener("click",function(){haptic("sel");opt.edit(it);});li.appendChild(e);}
 return li;}
function fbGroup(title,rows,opt,empty){
 var g=el("section","fb-group"),h=el("h4","fb-sub",title);g.appendChild(h);
 if(!rows.length){g.appendChild(el("div","fb-empty",empty));return g;}
 var ul=el("ul","fb-rows");rows.forEach(function(it){ul.appendChild(fbRow(it,opt));});g.appendChild(ul);return g;}
// The part inside the "My schedule" card.
function v2FamilyBlock(){
 fbLoad(false);
 var box=el("div","fb-block");box.setAttribute("data-v2-section","family-today");
 var head=el("div","fb-head"),h=el("h3","fb-title");h.appendChild(icon("v2home"));h.appendChild(el("span",null,fbT("title")));head.appendChild(h);
 var open=el("button","btn sm fb-open");open.type="button";open.setAttribute("data-fb","open");open.setAttribute("aria-haspopup","dialog");open.appendChild(el("span",null,fbT("open")));
 open.addEventListener("click",function(){haptic("sel");fbOpen(open);});head.appendChild(open);box.appendChild(head);
 if(!fbData){var f=el("div","v2-empty fb-note",fbErr?fbT("loadFailed"):"…");f.setAttribute("data-fb-state",fbErr?"error":"loading");box.appendChild(f);
  if(fbErr){var r=el("button","btn sm fb-retry",fbT("retry"));r.type="button";r.addEventListener("click",function(){haptic("sel");fbLoad(true);fbRerender();});box.appendChild(r);}return box;}
 if(fbData.shabbat){var s=el("div","safe fb-note",fbT("shabbat")+(fbData.shabbat_until?" "+fbT("shabbatUntil").replace("{t}",fbData.shabbat_until):""));s.setAttribute("data-fb-state","shabbat");box.appendChild(s);return box;}
 var chores=(fbData.days[fbData.day]||{}).chores||[];
 box.appendChild(fbGroup(fbT("todayChores")+" · "+fbT("d"+fbData.day),chores,{check:true},fbT("noChores")));
 box.appendChild(fbGroup(fbT("daily"),fbData.daily,{check:true},fbT("emptyDaily")));
 return box;}
// --- the sheet ----------------------------------------------------------------------------------------------------------
function fbPaintWeek(body){
 var d=fbData,cur=fbDay<0?d.day:fbDay,tabs=el("div","fb-daytabs");tabs.setAttribute("role","tablist");tabs.setAttribute("aria-label",fbT("dayTabs"));
 var days=el("div","fb-days");
 d.days.forEach(function(col){
  var on=col.day===cur,b=el("button","fb-daytab"+(on?" on":"")+(col.day===d.day?" today":""));b.type="button";b.setAttribute("role","tab");b.setAttribute("data-fb-daytab",String(col.day));b.setAttribute("aria-selected",String(on));
  b.appendChild(el("span",null,fbT("d"+col.day)));tabs.appendChild(b);
  b.addEventListener("click",function(){fbDay=col.day;haptic("sel");fbSheet.paint();});
  var sec=el("section","fb-day"+(on?" on":""));sec.setAttribute("data-fb-day",String(col.day));
  var h=el("h4","fb-dayname",fbT("d"+col.day));if(col.day===d.day)h.appendChild(el("small","fb-today",fbT("todayMark")));sec.appendChild(h);
  if(!col.chores.length)sec.appendChild(el("div","fb-empty",fbT("emptyDay")));
  else{var ul=el("ul","fb-rows");col.chores.forEach(function(it){ul.appendChild(fbRow(it,{check:true,locked:d.shabbat,edit:function(x){fbEditOpen({mode:"edit",list:"weekly",id:x.id,text:x.text,day:col.day});}}));});sec.appendChild(ul);}
  sec.appendChild(fbAddBtn("weekly",col.day));days.appendChild(sec);});
 body.appendChild(tabs);body.appendChild(days);}
function fbAddBtn(list,day){
 var a=el("button","btn sm fb-add");a.type="button";a.setAttribute("data-fb-add",list+(day==null?"":":"+day));a.appendChild(icon("plus"));a.appendChild(el("span",null,fbT("add")));
 a.addEventListener("click",function(){haptic("sel");fbEditOpen({mode:"add",list:list,day:day==null?null:day,text:""});});return a;}
function fbPaintList(body,list){
 var d=fbData,rows=list==="daily"?d.daily:list==="shop"?d.shop:d.meals,empty=list==="daily"?"emptyDaily":list==="shop"?"emptyShop":"emptyMeals";
 var title=list==="daily"?"dailyTitle":list==="shop"?"shopTitle":"mealsTitle",check=list!=="meals",locked=list==="daily"&&d.shabbat;
 var g=fbGroup(fbT(title),rows,{check:check,locked:locked,edit:function(x){fbEditOpen({mode:"edit",list:list,id:x.id,text:x.text,day:x.day});}},fbT(empty));g.className+=" fb-list";body.appendChild(g);
 var bar=el("div","fb-bar");bar.appendChild(fbAddBtn(list,null));
 if(list==="shop"){var n=rows.filter(function(x){return x.done;}).length,c=el("button","btn sm fb-clear",fbT("clearChecked"));c.type="button";c.setAttribute("data-fb","clear");c.disabled=!n;
  c.addEventListener("click",function(){if(c.disabled)return;c.disabled=true;haptic("tap");fbSend({action:"clear"}).then(function(j){haptic("n","success");toast(fbT("clearedN").replace("{n}",nf.format(j.cleared||0)));fbSheet.paint();}).catch(function(e){c.disabled=false;fbFail(e);});});bar.appendChild(c);}
 body.appendChild(bar);}
function fbEditOpen(e){fbEdit=e;fbSheet.editor();}
function fbOpen(opener){
 if(fbSheet){fbSheet.close();fbSheet=null;}
 var dialog=el("dialog","topic-sheet fb-sheet"),head=el("div","topic-heading"),title=el("h2",null,fbT("title")),close=el("button","btn topic-close",fbT("close"));
 dialog.dir=document.documentElement.dir;title.id="fb-sheet-title";dialog.setAttribute("aria-labelledby",title.id);dialog.setAttribute("data-v2-section","family-board");
 close.type="button";head.appendChild(title);head.appendChild(close);dialog.appendChild(head);
 dialog.appendChild(el("p","fb-hint",fbT("subtitle")));
 var tools=el("div","fb-tools"),pr=el("button","btn sm fb-print");pr.type="button";pr.setAttribute("data-fb","print");pr.appendChild(icon("v2print"));pr.appendChild(el("span",null,fbT("print")));
 pr.addEventListener("click",function(){haptic("sel");fbPrint();});tools.appendChild(pr);
 var pg=el("button","btn sm fb-page");pg.type="button";pg.setAttribute("data-fb","page");pg.appendChild(icon("v2print"));pg.appendChild(el("span",null,fbT("openPage")));
 pg.addEventListener("click",function(){fbOpenPage(pg);});tools.appendChild(pg);dialog.appendChild(tools);
 var tabs=el("div","fb-tabs");tabs.setAttribute("role","tablist");tabs.setAttribute("aria-label",fbT("tabs"));
 var body=el("div","fb-body"),edit=el("div","fb-editor"),undo=el("div","fb-undo");undo.hidden=true;
 [["week","tabWeek"],["daily","tabDaily"],["shop","tabShop"],["meals","tabMeals"]].forEach(function(t){
  var b=el("button","fb-tab"+(fbTab===t[0]?" on":""),fbT(t[1]));b.type="button";b.setAttribute("role","tab");b.setAttribute("data-fb-tab",t[0]);b.setAttribute("aria-selected",String(fbTab===t[0]));
  b.addEventListener("click",function(){fbTab=t[0];fbEdit=null;haptic("sel");api.paint();api.editor();});tabs.appendChild(b);});
 dialog.appendChild(tabs);dialog.appendChild(body);dialog.appendChild(undo);dialog.appendChild(edit);
 var setOpen=null,api={close:function(){fbEdit=null;if(setOpen)setOpen(false);dialog.remove();},
  paint:function(){
   Array.prototype.forEach.call(tabs.children,function(x){var on=x.getAttribute("data-fb-tab")===fbTab;x.classList.toggle("on",on);x.setAttribute("aria-selected",String(on));});
   body.textContent="";
   if(!fbData){var f=el("div","v2-empty fb-note",fbErr?fbT("loadFailed"):"…");body.appendChild(f);if(fbErr){var r=el("button","btn sm",fbT("retry"));r.type="button";r.addEventListener("click",function(){fbLoad(true);fbRerender();});body.appendChild(r);}return;}
   if(fbTab==="week")fbPaintWeek(body);else fbPaintList(body,fbTab);
   undo.textContent="";undo.hidden=!fbUndo;
   if(fbUndo){var line=el("bdi","fb-undo-text",fbT("deleted").replace("{t}",fbUndo.text));line.dir="auto";undo.appendChild(line);
    var u=el("button","btn sm fb-undo-btn",fbT("undoDelete"));u.type="button";u.setAttribute("data-fb","undo");
    u.addEventListener("click",function(){var x=fbUndo;fbUndo=null;u.disabled=true;
     var req={action:"add",list:x.list,text:x.text};if(x.list==="weekly"||x.day!=null)req.day=x.day;
     fbSend(req).then(function(){haptic("n","success");toast(fbT("restored"));fbRerender();}).catch(function(e){fbUndo=x;u.disabled=false;fbFail(e);});});undo.appendChild(u);}},
  editor:function(){
   edit.textContent="";if(!fbEdit)return;
   var e=fbEdit,ta=el("textarea","fb-field");ta.rows=2;ta.maxLength=100;ta.value=e.text||"";ta.placeholder=fbT("placeholder");ta.setAttribute("aria-label",fbT(e.mode==="add"?"addTitle":"editTitle"));
   ta.setAttribute("data-fb","field");ta.setAttribute("enterkeyhint","send");tune(ta,true);
   edit.appendChild(el("h3","fb-edit-title",fbT(e.mode==="add"?"addTitle":"editTitle")));edit.appendChild(ta);
   var row=el("div","fb-actions"),save=el("button","btn pri fb-save");save.type="button";save.setAttribute("data-native-main","");save.setAttribute("data-fb","save");
   var label=el("span",null,fbT("save"));save.appendChild(icon("check"));save.appendChild(label);row.appendChild(save);
   var cancel=el("button","btn fb-cancel",fbT("cancel"));cancel.type="button";cancel.setAttribute("data-fb","cancel");cancel.setAttribute("data-native-second","");row.appendChild(cancel);
   var del=null;if(e.mode==="edit"){del=el("button","btn fb-delete",fbT("delete"));del.type="button";del.setAttribute("data-fb","delete");row.appendChild(del);}
   edit.appendChild(row);var out=el("div","fb-out");out.setAttribute("aria-live","polite");edit.appendChild(out);
   function sync(){save.disabled=fbSaving||!ta.value.trim();}
   function fail(err){fbSaving=false;ta.disabled=false;label.textContent=fbT("save");sync();haptic("n","error");
    out.textContent=err&&err.status===403?S.reopen:err&&err.code==="full"?fbT("full"):err&&err.status===400?fbT("bad"):fbT("failed");}
   function go(){var text=ta.value.trim();if(!text||fbSaving)return;fbSaving=true;ta.disabled=true;save.disabled=true;label.textContent=fbT("saving");out.textContent="";haptic("tap");
    var req=e.mode==="add"?{action:"add",list:e.list,text:text}:{action:"edit",id:e.id,text:text};
    if(e.mode==="add"&&e.day!=null)req.day=e.day;
    fbSend(req).then(function(){fbSaving=false;fbEdit=null;haptic("n","success");toast(fbT("saved"));edit.textContent="";api.paint();fbRerender();}).catch(fail);}
   ta.addEventListener("input",sync);ta.addEventListener("keydown",function(ev){if(ev.key==="Enter"&&!ev.shiftKey){ev.preventDefault();go();}});
   save.addEventListener("click",go);cancel.addEventListener("click",function(){fbEdit=null;haptic("sel");edit.textContent="";});
   if(del)del.addEventListener("click",function(){if(fbSaving)return;fbSaving=true;del.disabled=true;haptic("tap");
    fbSend({action:"remove",id:e.id}).then(function(j){fbSaving=false;fbEdit=null;fbUndo=j.removed?{list:j.removed.list,text:j.removed.text,day:j.removed.day}:null;haptic("n","success");edit.textContent="";api.paint();fbRerender();})
     .catch(function(err){fbSaving=false;del.disabled=false;fbFail(err);});});
   sync();ta.focus();}};
 document.body.appendChild(dialog);var oldOverflow=document.body.style.overflow;
 setOpen=bindBackLayer("family-board",dialog,false,function(on){
  if(on){if(!dialog.open){document.body.style.overflow="hidden";dialog.showModal();}}
  else{if(dialog.open)dialog.close();document.body.style.overflow=oldOverflow;if(opener&&opener.isConnected)opener.focus();}});
 fbSheet=api;
 close.addEventListener("click",function(){fbEdit=null;setOpen(false);});
 dialog.addEventListener("cancel",function(e){e.preventDefault();fbEdit=null;setOpen(false);});setOpen(true);fbLoad(false);fbPrefetch();api.paint();return dialog;}
// --- the print view: Hebrew, black on white, one A4 page; the page's own screens are hidden while it prints -------------
function fbPrintBox(title,rows,mark){
 var box=el("section","fbp-box");box.appendChild(el("h2","fbp-h",title));
 var ul=el("ul","fbp-rows");rows.forEach(function(it){var li=el("li","fbp-row");if(mark)li.appendChild(el("span","fbp-mark"));li.appendChild(el("bdi",null,it.text));ul.appendChild(li);});
 if(!rows.length)ul.appendChild(el("li","fbp-row fbp-none","—"));box.appendChild(ul);return box;}
function fbPrintPage(){
 var H=(V2_COPY.he||V2).family||{},d=fbData,page=el("div","fbp-page");page.id="fb-print";page.dir="rtl";page.lang="he";
 var hd=el("header","fbp-head");hd.appendChild(el("h1",null,H.printTitle));hd.appendChild(el("p",null,H.printNote));
 hd.appendChild(el("p","fbp-week",(H.printWeek||"").replace("{d}",d.week_start.split("-").reverse().join("."))));page.appendChild(hd);
 var grid=el("div","fbp-grid");d.days.forEach(function(c){grid.appendChild(fbPrintBox(H["d"+c.day],c.chores,true));});
 grid.appendChild(fbPrintBox(H.dailyTitle,d.daily,true));page.appendChild(grid);
 var low=el("div","fbp-grid fbp-low");low.appendChild(fbPrintBox(H.shopTitle,d.shop,true));low.appendChild(fbPrintBox(H.mealsTitle,d.meals,false));page.appendChild(low);
 return page;}
function fbPrint(){
 if(!fbData)return;var page=fbPrintPage(),old=document.getElementById("fb-print");if(old)old.remove();
 document.body.appendChild(page);document.body.classList.add("fb-printing");
 var done=function(){document.body.classList.remove("fb-printing");page.remove();window.removeEventListener("afterprint",done);};
 window.addEventListener("afterprint",done);
 try{window.print();}catch(e){done();}}
// The same page as a link that opens outside the app (window.print() may not work inside Telegram on iPhone). The link
// is fetched when the sheet opens, so the tap itself opens it with no network wait in between (Telegram may not count an
// opener called after a round-trip as a tap); a tap before it has arrived, or after it has expired, fetches it first.
var fbLink=null,fbLinkBusy=false;
function fbLinkFresh(slack){return !!fbLink&&fbLink.expires*1000>Date.now()+slack;}
function fbGo(url){var full=location.origin+url;if(wa&&typeof wa.openLink==="function")wa.openLink(full);else window.open(full,"_blank","noopener");}
function fbPrefetch(then){
 if(fbLinkBusy)return;if(!then&&fbLinkFresh(60000))return;fbLinkBusy=true;
 post("/api/family-print",{}).then(function(r){return r.json().catch(function(){return {};}).then(function(j){
  fbLinkBusy=false;
  if(r.ok&&j.url){fbLink={url:j.url,expires:j.expires||0};if(then)then(null);}
  else if(then)then(r.status);});})
 .catch(function(){fbLinkBusy=false;if(then)then(1);});}
function fbOpenPage(btn){
 if(fbLinkFresh(15000)){haptic("sel");fbGo(fbLink.url);return;}
 if(btn.disabled)return;btn.disabled=true;
 fbPrefetch(function(err){btn.disabled=false;
  if(err){haptic("n","error");toast(err===403?S.reopen:fbT("failed"),true);return;}
  fbGo(fbLink.url);});}
ICONS.v2pencil="M4 20l1-5L16 4l4 4L9 19zM14 6l4 4";
ICONS.v2print="M7 9V3h10v6M7 17H4v-7h16v7h-3M7 14h10v7H7z";
