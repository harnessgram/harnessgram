var NATIVE=__NATIVE__===true&&!!wa,natState={main:null,second:null,mainBusy:false,secondBusy:false,mainKey:"",secondKey:"",queued:false,prefs:{tab:"",blocks:{}},prefsTimer:null},NAT_PREFS="cockpit_prefs",NAT_TOKENS=[["--bg","secondary_bg_color"],["--card","section_bg_color"],["--field","bg_color"],["--line","section_separator_color"],["--text","text_color"],["--hint","hint_color"],["--v2-page","secondary_bg_color"],["--v2-ink","text_color"],["--v2-muted","hint_color"],["--v2-soft","section_separator_color"]];
// --- Telegram's own controls (the cockpit_native switch) -------------------------------------------------------
// One door: every native call added for the switch goes through natCall. Off (the default), or outside Telegram (a
// plain browser, no Telegram.WebApp), natCall does nothing and returns false, so each caller keeps the page's own
// control. Server checks and the page's guarded action flows never move into this module.
function natOn(v){return NATIVE===true&&!!wa&&(!v||ver(v));}
function natCall(v,fn){if(!natOn(v))return false;try{return fn(wa)!==false;}catch(e){return false;}}
function natHex(v){return typeof v==="string"&&/^#[0-9a-f]{6}$/i.test(v)?v:"";}
// Surfaces follow the Telegram theme; the brand (header art, accents, answer colours) stays ours.
function natTheme(){natCall("6.1",function(w){var t=w.themeParams||{},root=document.documentElement;
 var choice=root.getAttribute("data-theme-choice");if((choice==="light"||choice==="dark")&&choice!==w.colorScheme){NAT_TOKENS.forEach(function(p){root.style.removeProperty(p[0]);});return;}
 NAT_TOKENS.forEach(function(p){var c=natHex(t[p[1]]);if(c)root.style.setProperty(p[0],c);else root.style.removeProperty(p[0]);});
 if(w.colorScheme==="dark"||w.colorScheme==="light")root.setAttribute("data-theme",w.colorScheme);
 if(natHex(t.secondary_bg_color)){w.setBackgroundColor("secondary_bg_color");w.setHeaderColor("secondary_bg_color");}
 if(ver("7.10")&&w.setBottomBarColor&&natHex(t.bottom_bar_bg_color))w.setBottomBarColor("bottom_bar_bg_color");});}
// Taps that the screens do not answer themselves still answer with the right haptic; one helper, no doubles.
function natFeel(e){var t=e.target&&e.target.closest&&e.target.closest("button,a,summary,select,[role=tab],[role=switch],input[type=checkbox],input[type=radio]");
 if(!t||t.disabled||Date.now()-(haptic.at||0)<120)return;
 haptic(t.matches("[role=tab],[role=switch],[aria-pressed],[aria-expanded],summary,select,input")?"sel":"tap");}
// The screen's primary action ([data-native-main]) and its companion ([data-native-second]) move to Telegram's bottom
// buttons: one visible candidate in the top dialog (or the page) is mirrored with its label, disabled state and a
// progress spinner while its request runs. Two or more candidates, or none, leave the page's own buttons in place.
function natLabel(b){return String(b.getAttribute("data-native-text")||b.textContent||b.getAttribute("aria-label")||"").replace(/\s+/g," ").trim().slice(0,64);}
function natFind(attr){var open=document.querySelectorAll("dialog[open]"),scope=open.length?open[open.length-1]:document;
 var list=Array.prototype.filter.call(scope.querySelectorAll("["+attr+"]"),function(b){return b.isConnected&&!b.closest("[hidden],dialog:not([open])")&&!!natLabel(b);});
 return list.length===1?list[0]:null;}
function natBottomQueue(){if(natState.queued)return;natState.queued=true;requestAnimationFrame(natBottomSync);}
function natBottomSync(){natState.queued=false;
 [["main","MainButton","data-native-main"],["second","SecondaryButton","data-native-second"]].forEach(function(p){var k=p[0];
  natCall("7.10",function(w){var nb=w[p[1]];if(!nb)return false;var b=natFind(p[2]),prev=natState[k];
   if(prev&&prev!==b){prev.classList.remove("nat-bottom");natState[k+"Busy"]=false;}natState[k]=b;
   if(!b){if(natState[k+"Key"]!=="off"){natState[k+"Key"]="off";nb.hideProgress();nb.hide();}return true;}
   b.classList.add("nat-bottom");if(!b.disabled)natState[k+"Busy"]=false;
   var busy=natState[k+"Busy"],text=natLabel(b),key=text+"|"+b.disabled+"|"+busy;if(natState[k+"Key"]===key)return true;natState[k+"Key"]=key;
   nb.setParams({text:text,is_visible:true,is_active:!b.disabled||busy});if(busy)nb.showProgress(false);else nb.hideProgress();return true;});});}
function natPress(k){var b=natState[k];if(!b||!b.isConnected||b.disabled)return;natState[k+"Busy"]=true;b.click();natBottomQueue();}
// Fullscreen on phones only; the page then pads itself below Telegram's own top controls.
function natFull(){natCall("8.0",function(w){document.documentElement.classList.toggle("tg-full",!!w.isFullscreen);});}
// Per-user UI preferences (last tab, opened blocks) live in Telegram's CloudStorage, one small JSON value; the
// browser's own storage stays the fast local copy and the whole answer when CloudStorage is missing or slow.
function natPrefsLoad(then){var done=false,finish=function(){if(done)return;done=true;then();};
 var asked=natCall("6.9",function(w){if(!w.CloudStorage)return false;
  w.CloudStorage.getItem(NAT_PREFS,function(err,value){if(!err&&value){try{var p=JSON.parse(value);
   if(p&&typeof p==="object"){if(typeof p.tab==="string"&&p.tab.length<=80){natState.prefs.tab=p.tab;if(!view)view=p.tab;}
    if(p.blocks&&typeof p.blocks==="object")Object.keys(p.blocks).forEach(function(key){var v=p.blocks[key];if(v!==0&&v!==1)return;
     natState.prefs.blocks[key]=v;try{localStorage.setItem(compactStorageKey(key),v?"1":"0");}catch(e){}});}}catch(e){}}
   finish();});setTimeout(finish,900);});
 if(!asked)finish();}
function natPref(name,key,value){if(!natOn("6.9"))return;var p=natState.prefs;
 if(name==="tab")p.tab=String(value||"").slice(0,80);else{delete p.blocks[key];p.blocks[key]=value?1:0;}
 clearTimeout(natState.prefsTimer);natState.prefsTimer=setTimeout(function(){var keys=Object.keys(p.blocks),s=JSON.stringify(p);
  while(s.length>4000&&keys.length){delete p.blocks[keys.shift()];s=JSON.stringify(p);}  // CloudStorage values hold 4096
  natCall("6.9",function(w){if(!w.CloudStorage)return false;w.CloudStorage.setItem(NAT_PREFS,s,function(){});});},600);}
function natStart(){if(!natOn())return;var root=document.documentElement;root.classList.add("tg-native");
 natTheme();natCall("",function(w){w.onEvent("themeChanged",natTheme);});
 // Telegram's BackButton replaces the page's own back arrows wherever it exists.
 if(natCall("6.1",function(w){return !!w.BackButton;}))root.classList.add("tg-back");
 natCall("8.0",function(w){w.onEvent("fullscreenChanged",natFull);if(/^(ios|android)$/.test(w.platform)&&!w.isFullscreen)w.requestFullscreen();});natFull();
 natCall("7.10",function(w){if(!w.MainButton||!w.SecondaryButton)return false;w.MainButton.onClick(function(){natPress("main");});
  w.SecondaryButton.onClick(function(){natPress("second");});
  new MutationObserver(natBottomQueue).observe(document.body,{subtree:true,childList:true,attributes:true,attributeFilter:["disabled","hidden","open","data-native-text"]});
  root.classList.add("tg-bottom");natBottomQueue();});
 document.addEventListener("click",natFeel);document.addEventListener("change",natFeel);}
