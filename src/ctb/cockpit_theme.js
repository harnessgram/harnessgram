(function(root){
"use strict";
var KEY="harnessgram-cockpit-theme",VALUES={auto:1,light:1,dark:1};
function safeLocalGet(){try{return localStorage.getItem(KEY);}catch(e){return null;}}
function safeLocalSet(value){try{localStorage.setItem(KEY,value);}catch(e){}}
function create(options){
 var telegram=options&&options.telegram,native=!!(options&&options.native),preference="auto",listeners=[];
 var media=null;try{media=root.matchMedia("(prefers-color-scheme: dark)");}catch(e){}
 function systemTheme(){try{if(telegram&&(telegram.colorScheme==="light"||telegram.colorScheme==="dark"))return telegram.colorScheme;}catch(e){}try{return media&&media.matches?"dark":"light";}catch(e){return "light";}}
 function apply(){var resolved=preference==="auto"?systemTheme():preference;root.document.documentElement.setAttribute("data-theme",resolved);root.document.documentElement.setAttribute("data-theme-choice",preference);listeners.slice().forEach(function(fn){try{fn(preference,resolved);}catch(e){}});return resolved;}
 function themeLoad(){var value=safeLocalGet();if(VALUES[value])preference=value;apply();}
 function themeSave(value){if(!VALUES[value])return;preference=value;apply();safeLocalSet(value);}
 function changed(){if(preference==="auto")apply();}
 try{if(telegram&&telegram.onEvent)telegram.onEvent("themeChanged",changed);}catch(e){}try{if(media){if(media.addEventListener)media.addEventListener("change",changed);else if(media.addListener)media.addListener(changed);}}catch(e){}
 return {init:themeLoad,setPreference:themeSave,getPreference:function(){return preference;},apply:apply,subscribe:function(fn){listeners.push(fn);return function(){listeners=listeners.filter(function(x){return x!==fn;});};}};
}root.CockpitTheme={create:create,key:KEY};
})(window);
