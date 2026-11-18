// "Change picture" (project_art.py): the owner picks a photo or a ready-made scene for a project, sees it beside the
// current one, and approves with one tap. The picture's bytes come from POST /api/project-art-image behind the same
// owner login as every call here; the page keeps them as data URLs (its image policy allows no blob: sources).
var v2ArtCache={},v2ArtWait={},v2ArtScenes=["plane","book","books","visor","clapper","catalog","generic"],v2ArtBusy=false;
function v2ArtT(k){return (V2.art||{})[k]||k;}
function v2ArtKey(a){return a.name+"@"+a.v;}
function v2ArtFetch(a){var k=v2ArtKey(a);if(v2ArtCache[k])return Promise.resolve(v2ArtCache[k]);
 if(!v2ArtWait[k])v2ArtWait[k]=post("/api/project-art-image",{name:a.name}).then(function(r){if(!r.ok)throw r.status;return r.blob();})
  .then(function(b){return new Promise(function(ok,no){var fr=new FileReader();fr.onload=function(){ok(String(fr.result));};fr.onerror=no;fr.readAsDataURL(b);});})
  .then(function(u){v2ArtCache[k]=u;return u;}).catch(function(e){delete v2ArtWait[k];throw e;});
 return v2ArtWait[k];}
// The scene shows first and until the picture arrives; a failed read keeps the scene.
function v2ArtFill(img,a){if(!a||typeof a.name!=="string")return;var k=v2ArtKey(a),scene=img.src;
 function show(u){img.onerror=function(){img.onerror=null;img.src=scene;};img.src=u;}
 if(v2ArtCache[k]){show(v2ArtCache[k]);return;}
 v2ArtFetch(a).then(show).catch(function(){});}
function v2ArtSceneImg(name){return v2Image(name);}
function v2ArtEdit(p,onClose){
 var old=document.querySelector(".v2-art-sheet");if(old)old.remove();
 var dialog=el("dialog","topic-sheet v2-art-sheet"),card=dialog;
 dialog.dir=document.documentElement.dir;dialog.setAttribute("aria-label",v2ArtT("title"));
 document.body.appendChild(dialog);var oldOverflow=document.body.style.overflow;
 var setOpen=bindBackLayer("project-art",dialog,false,function(on){
  if(on){if(!dialog.open){document.body.style.overflow="hidden";dialog.showModal();}}
  else{if(dialog.open)dialog.close();document.body.style.overflow=oldOverflow;dialog.remove();if(onClose)onClose();}});
 function close(){setOpen(false);}
 dialog.addEventListener("cancel",function(e){e.preventDefault();setOpen(false);});
 function top(back){card.textContent="";var h=el("div","v2-art-head"),t=el("b",null,v2ArtT("title")),x=el("button","v2-art-x");x.type="button";x.setAttribute("aria-label",v2ArtT(back?"back":"close"));
  x.appendChild(icon(back?"chev":"x"));if(back)x.setAttribute("data-back","");x.addEventListener("click",function(){haptic("sel");if(back)menu();else close();});h.appendChild(t);h.appendChild(x);card.appendChild(h);
  var nm=el("bdi","v2-art-name",v2Name(p));card.appendChild(nm);}
 function button(cls,text,ic,fn){var b=el("button","v2-art-btn "+cls);b.type="button";if(ic)b.appendChild(icon(ic));b.appendChild(el("span",null,text));b.addEventListener("click",fn);return b;}
 function menu(){top(false);card.appendChild(el("div","v2-art-hint",v2ArtT("hint")));
  var list=el("div","v2-art-list"),file=el("input");file.type="file";file.accept="image/png,image/jpeg,image/webp";file.className="v2-art-file";file.setAttribute("data-v2-art","file");
  file.addEventListener("change",function(){var f=file.files&&file.files[0];if(f)v2ArtRead(f,function(url,b64){
   var img=el("img");img.src=url;img.alt="";preview(img,{action:"set",project:p.key,data:b64},"set");});});
  list.appendChild(button("up",v2ArtT("upload"),"plus",function(){haptic("tap");file.click();}));list.appendChild(file);
  list.appendChild(button("pick",v2ArtT("scene"),"layers",function(){haptic("sel");scenes();}));
  if(p.art)list.appendChild(button("auto",v2ArtT("auto"),"refresh",function(){haptic("sel");preview(v2Image(p.asset),{action:"clear",project:p.key},"clear");}));
  list.querySelectorAll("button").forEach(function(b,i){b.setAttribute("data-v2-art",["upload","scene","auto"][i]);});card.appendChild(list);}
 function scenes(){top(true);card.appendChild(el("div","v2-art-hint",v2ArtT("pickScene")));
  var grid=el("div","v2-art-scenes");v2ArtScenes.forEach(function(s){var b=el("button","v2-art-scene");b.type="button";b.setAttribute("data-v2-art-scene",s);
   b.appendChild(v2Image(s));b.appendChild(el("span",null,v2ArtT("scene_"+s)));
   b.addEventListener("click",function(){haptic("sel");preview(v2Image(s),{action:"scene",project:p.key,scene:s},"scene");});grid.appendChild(b);});card.appendChild(grid);}
 function pane(label,node){var f=el("figure","v2-art-pane");var box=el("span","v2-art-pic");box.appendChild(node);f.appendChild(box);f.appendChild(el("figcaption",null,label));return f;}
 function preview(after,body,kind){top(true);card.appendChild(el("div","v2-art-hint",v2ArtT("previewHint")));
  var row=el("div","v2-art-compare");row.setAttribute("data-v2-art","compare");
  row.appendChild(pane(v2ArtT("before"),v2Image(p.asset,null,p.art)));row.appendChild(pane(v2ArtT("after"),after));card.appendChild(row);
  var ok=button("ok",v2ArtT("approve"),"check",function(){apply(body,kind,ok,no);}),no=button("no",v2ArtT("cancel"),"x",function(){haptic("sel");menu();});
  ok.setAttribute("data-v2-art","approve");no.setAttribute("data-v2-art","cancel");var acts=el("div","v2-art-actions");acts.appendChild(ok);acts.appendChild(no);card.appendChild(acts);}
 function apply(body,kind,ok,no){if(v2ArtBusy)return;v2ArtBusy=true;ok.disabled=no.disabled=true;haptic("tap");
  post("/api/project-art",body).then(function(r){if(!r.ok)return r.json().catch(function(){return {};}).then(function(j){throw {status:r.status,code:j.error};});return r.json();})
   .then(function(){v2ArtBusy=false;haptic("n","success");toast(v2ArtT(kind==="clear"?"cleared":"saved"));close();load();})
   .catch(function(e){v2ArtBusy=false;ok.disabled=no.disabled=false;haptic("n","error");e=e||{};
    toast(e.status===403?S.reopen:e.code==="size"?v2ArtT("tooBig"):e.code==="type"?v2ArtT("badType"):e.code==="needs_pillow"?v2ArtT("needsPillow"):e.code==="decode"?v2ArtT("unreadable"):v2ArtT("failed"),true);});}
 setOpen(true);menu();}
// A chosen file becomes a 512 px square (centre crop) in the page, so the upload stays small whatever the camera made.
function v2ArtRead(file,done){
 if(!/^image\/(png|jpeg|webp)$/.test(file.type)){toast(v2ArtT("badType"),true);return;}
 if(file.size>25000000){toast(v2ArtT("tooBig"),true);return;}
 var fr=new FileReader();fr.onerror=function(){toast(v2ArtT("unreadable"),true);};
 fr.onload=function(){var im=new Image();im.onerror=function(){toast(v2ArtT("unreadable"),true);};
  im.onload=function(){var side=Math.min(im.naturalWidth,im.naturalHeight);if(!side){toast(v2ArtT("unreadable"),true);return;}
   var c=document.createElement("canvas");c.width=c.height=512;var g=c.getContext("2d");
   g.drawImage(im,(im.naturalWidth-side)/2,(im.naturalHeight-side)/2,side,side,0,0,512,512);
   var url=c.toDataURL("image/webp",0.85);if(url.indexOf("data:image/webp")!==0)url=c.toDataURL("image/jpeg",0.85);
   var b64=url.split(",")[1]||"";if(!b64||b64.length>1300000){toast(v2ArtT("tooBig"),true);return;}done(url,b64);};
  im.src=String(fr.result);};
 fr.readAsDataURL(file);}
// A long press on a project tile opens the same sheet; the click that ends the press does not open the project.
function v2ArtHold(b,p){var timer=null,sx=0,sy=0;
 function stop(){clearTimeout(timer);timer=null;}
 b.addEventListener("pointerdown",function(e){b._artHeld=false;sx=e.clientX;sy=e.clientY;stop();timer=setTimeout(function(){timer=null;b._artHeld=true;haptic("tap");v2ArtEdit(p,function(){b._artHeld=false;});},600);});
 b.addEventListener("pointermove",function(e){if(timer&&(Math.abs(e.clientX-sx)>10||Math.abs(e.clientY-sy)>10))stop();});
 ["pointerup","pointercancel","pointerleave"].forEach(function(n){b.addEventListener(n,stop);});
 b.addEventListener("contextmenu",function(e){e.preventDefault();});}
function v2ArtButton(p){var b=el("button","v2-art-edit");b.type="button";b.setAttribute("data-v2-art","open");b.setAttribute("aria-label",v2ArtT("title"));b.title=v2ArtT("title");
 b.appendChild(icon("pencil"));b.addEventListener("click",function(){haptic("sel");v2ArtEdit(p);});return b;}
