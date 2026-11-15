// Extends the existing tiny DOM for semantic/action checks when no browser is available.
Object.defineProperty(El.prototype,"children",{get:function(){return this.childNodes.filter(c=>c.nodeType===1);}});
Object.defineProperty(El.prototype,"classList",{get:function(){var e=this;return {
 contains:c=>e.className.split(/\s+/).includes(c),
 add:c=>{if(!e.className.split(/\s+/).includes(c))e.className+=" "+c;},
 remove:c=>{e.className=e.className.split(/\s+/).filter(x=>x!==c).join(" ");},
 toggle:(c,on)=>{var has=e.className.split(/\s+/).includes(c),v=on===undefined?!has:on;e.className=e.className.split(/\s+/).filter(x=>x!==c).concat(v?[c]:[]).join(" ");return v;}
};}});
function matches(e,s){
 var tag=s.match(/^[a-z]+/);if(tag&&e.tagName!==tag[0])return false;
 var id=s.match(/#([\w-]+)/);if(id&&(e.id||e.attrs.id)!==id[1])return false;
 var classes=Array.from(s.matchAll(/\.([\w-]+)/g));if(classes.some(m=>!e.classList.contains(m[1])))return false;
 var attrs=Array.from(s.matchAll(/\[([\w-]+)(?:=['"]?([^'"\]]+)['"]?)?\]/g));
 return attrs.every(m=>m[1] in e.attrs&&(!m[2]||e.attrs[m[1]]===m[2]));
}
El.prototype.querySelectorAll=function(selector){var found=[];walk(this,e=>{if(e!==this&&matches(e,selector))found.push(e);});return found;};
El.prototype.querySelector=function(s){return this.querySelectorAll(s)[0]||null;};
El.prototype.insertBefore=function(e,before){if(e.parentNode)e.parentNode.removeChild(e);var i=this.childNodes.indexOf(before);e.parentNode=this;if(i<0)this.childNodes.push(e);else this.childNodes.splice(i,0,e);return e;};
El.prototype.remove=function(){if(this.parentNode)this.parentNode.removeChild(this);};
El.prototype.replaceWith=function(e){if(!this.parentNode)return;this.parentNode.insertBefore(e,this);this.remove();};
El.prototype.setAttribute=(function(original){return function(k,v){original.call(this,k,v);if(k==="id")this.id=String(v);};})(El.prototype.setAttribute);
function memory(){var m={};return {getItem:k=>m[k]||null,setItem:(k,v)=>{m[k]=String(v);}};}
var posted=[],loads=0,exitTaps=0,renders=0;
Object.assign(sandbox,{
 app:root,LANG:"he",RTL:true,S:{},notes:{},closed:{},answered:{},cardDone:{},compactSerial:0,target:"",NS:"http://www.w3.org/2000/svg",
 wa:null,ICONS:{},localStorage:memory(),sessionStorage:memory(),nf:new Intl.NumberFormat("he"),pf:new Intl.NumberFormat("he",{style:"percent",maximumFractionDigits:0}),
 opened:{},last:null,digest:null,document:doc,
 haptic:()=>{},adoptBackLayers:()=>{},syncClose:()=>{},source:()=>{},ago:n=>String(n),
 bindBackLayer:(key,node,on,apply)=>(value)=>apply(value),
 tune:ta=>{ta.dir=ta.value?"auto":docEl.dir;return ()=>{};},
 topicBtn:()=>new El("button"),
 post:(path,body)=>{posted.push([path,body]);return Promise.resolve({ok:true,json:()=>Promise.resolve({ok:true,delivery:"queued",key:"sample"})});},
 load:()=>{loads++;},render:()=>{renders++;},cleared:0,toast:()=>{},backTap:()=>{exitTaps++;},
 answer:(card,d,choice,buttons,ta)=>sandbox.post("/api/decide",{id:d.id,choice:choice,note:ta.value.trim()}),
 remRow:r=>{var li=new El("li");li.textContent=r.text;return li;},
 agentCard:c=>{var box=new El("div");box.textContent=c.title;return box;},
 ASKS:{ask:1,choice:1},appsOf:()=>[],appNamed:()=>null,appOf:()=>null,
 switchTo:v=>{sandbox.view=v;},view:"me",appTitle:a=>a.title,
 setTimeout:setTimeout,clearTimeout:clearTimeout,URL:URL,navigator:{mediaSession:{}},MediaMetadata:function(m){Object.assign(this,m);},location:{href:"https://example.com/"}
});
function bootV2(code,state,strings,lang){
 sandbox.LANG=lang||"he";sandbox.RTL=sandbox.LANG==="he";docEl.dir=sandbox.RTL?"rtl":"ltr";sandbox.last=state;sandbox.S=strings;
 vm.runInNewContext(code,sandbox);sandbox.v2Now(state);
}
