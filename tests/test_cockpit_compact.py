"""Run the production disclosure and accounting renderer without a browser/network."""
import json
import shutil
import subprocess

import pytest

from ctb import panel_page
from test_panel_page import func


FUNCTIONS = ("compactStorageKey", "compactBlock", "fold", "compactLong", "cost", "shortUsageModel",
             "usageTokens", "cacheShare", "usageFigures", "usageRows", "usageActive", "usageBreakdown", "usageDetails", "usageCard")


def run_js(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    script = r'''
const assert=require("node:assert/strict");
class Element {
 constructor(tag,cls,text){this.tag=tag;this.className=cls||"";this.textContent=text||"";this.children=[];this.attrs={};this.events={};this.style={setProperty(k,v){this[k]=v;}};}
 appendChild(x){this.children.push(x);return x;}
 get firstChild(){return this.children[0];}
 setAttribute(k,v){this.attrs[k]=v;}
 addEventListener(k,f){this.events[k]=f;}
 click(){this.events.click();}
}
function el(t,c,s){return new Element(t,c,s);}
function icon(){return el("svg");} function haptic(){} function sec(){}
function bindBackLayer(key,node,on,apply){return apply;} function adoptBackLayers(){}
function shown(o,k){return o[k];} function iso(s){return s;}
function kv(k,v){var n=el("div","kv");n.appendChild(el("span",null,k));n.appendChild(el("span",null,v));return n;}
function all(n){return [n,...n.children.flatMap(all)];}
function text(n){return all(n).map(x=>x.textContent).join(" ");}
function toggle(n){return n.children[0];} function panel(n){return n.children[1].children[0];}
var S=STRINGS,wa={initDataUnsafe:{user:{id:1}}},compactSerial=0,LANG="en",app=el("main");
var nf=new Intl.NumberFormat(LANG),cf=new Intl.NumberFormat(LANG,{notation:"compact",maximumFractionDigits:0}),
 pf=new Intl.NumberFormat(LANG,{style:"percent",maximumFractionDigits:0}),usd=new Intl.NumberFormat(LANG,{style:"currency",currency:"USD"});
const saved=new Map();var localStorage={getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v)};
'''.replace("STRINGS", json.dumps(panel_page.STRINGS["en"]))
    script += "\n".join(func(n) for n in FUNCTIONS) + "\n" + body
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_default_toggle_keyboard_semantics_and_independent_remembered_state():
    run_js('''
const a=compactBlock("a","Title","42"),b=compactBlock("b","Other");
assert.equal(toggle(a).tag,"button");assert.equal(toggle(a).type,"button");
assert.equal(toggle(a).attrs["aria-expanded"],"false");assert.equal(panel(a).inert,true);
assert.equal(toggle(a).attrs["aria-controls"],panel(a).id);
assert.notEqual(panel(a).id,panel(b).id);
toggle(a).click();assert.equal(toggle(a).attrs["aria-expanded"],"true");assert.equal(panel(a).inert,false);
assert.equal(toggle(b).attrs["aria-expanded"],"false");
assert.equal(toggle(compactBlock("a","Reloaded")).attrs["aria-expanded"],"true");
toggle(a).click();assert.equal(panel(a).attrs["aria-hidden"],"true");
assert.equal(toggle(compactBlock("a","Reloaded")).attrs["aria-expanded"],"false");
''')


def test_preferences_are_scoped_to_the_viewer_including_signed_data_fallback():
    run_js('''
toggle(compactBlock("a","Title")).click();
wa={initDataUnsafe:{user:{id:2}}};assert.equal(toggle(compactBlock("a","Other viewer")).attrs["aria-expanded"],"false");
wa={initData:"user="+encodeURIComponent(JSON.stringify({id:1}))};
assert.equal(toggle(compactBlock("a","Original viewer")).attrs["aria-expanded"],"true");
assert([...saved.keys()].every(k=>k.startsWith("ctb-panel-block:1:")));
''')


def test_unavailable_or_corrupt_storage_fails_closed_but_toggle_still_works():
    run_js('''
localStorage={getItem(){throw Error("denied")},setItem(){throw Error("quota")}};
const block=compactBlock("a","Title");assert.equal(toggle(block).attrs["aria-expanded"],"false");
toggle(block).click();assert.equal(toggle(block).attrs["aria-expanded"],"true");
toggle(block).click();assert.equal(toggle(block).attrs["aria-expanded"],"false");
assert.equal(toggle(compactBlock("a","Reloaded")).attrs["aria-expanded"],"false");
localStorage={getItem:()=>"garbled"};assert.equal(toggle(compactBlock("a","Bad preference")).attrs["aria-expanded"],"false");
assert.equal(toggle(compactBlock("old","Target",null,null,null,true)).attrs["aria-expanded"],"true");
''')


def test_zero_rows_and_empty_unknown_models_hide_but_unpriced_usage_survives():
    run_js('''
const zero={input_tokens:0,output_tokens:0,cache_creation_input_tokens:0,cache_read_input_tokens:0,estimate_usd:null,has_unpriced:true,unpriced_tokens:0};
const used={...zero,input_tokens:12,cache_read_input_tokens:8,unpriced_tokens:20};
const body=el("div");usageRows(zero,body);assert.equal(body.children.length,0);
usageBreakdown([["Period",{tokens:0,models:{unknown:zero}}]],body,"test");
assert(!all(body).some(n=>n.attrs["data-block"]));
const full=el("div");usageBreakdown([["Period",{tokens:20,models:{unknown:used,"claude-sonnet-4-20250514":zero}}]],full,"period");
const models=all(full).filter(n=>n.attrs["data-block"]);
assert.equal(models.length,1);assert.equal(models[0].attrs["data-block"],"period-model-unknown");
assert.equal(toggle(models[0]).attrs["aria-expanded"],"false");
assert(text(full).includes(S.usageUnknown));assert(text(full).includes(S.usageUnpriced));
assert(!text(full).includes(S.usageOutput));assert(!text(full).includes(S.usageWrite));
assert.equal(shortUsageModel("claude-sonnet-4-20250514"),"Sonnet 4");
assert.equal(cacheShare(used),.4);
''')


def test_visual_summary_has_cost_tokens_cache_and_models_have_relative_bars():
    run_js('''
const v={tokens:2000,input_tokens:200,output_tokens:300,cache_creation_input_tokens:500,cache_read_input_tokens:1000,estimate_usd:.25,models:{"claude-opus-4-6":{input_tokens:200,estimate_usd:.1}}};
const host=el("div");usageDetails({today:v,session:v},host,"session");
const block=host.children[0],summary=toggle(block);
assert.equal(block.attrs["data-block"],"usage-session");
assert.equal(toggle(block).attrs["aria-expanded"],"false");
assert(text(summary).includes("$0.25"));assert(text(summary).includes("2K"));assert(text(summary).includes("75%"));
assert(all(summary).some(n=>n.className==="cache-ring"&&n.style["--share"]==="75%"));
assert.equal(all(block).filter(n=>n.className==="compact-mini").length,1);
assert(all(summary).some(n=>n.attrs["aria-label"]===S.usageTokens+": 2,000"));
''')


def test_compact_copy_and_css_keep_both_languages_rtl_and_touch_targets():
    he, en = panel_page.STRINGS["he"], panel_page.STRINGS["en"]
    assert he.keys() == en.keys()
    for key in ("compactDetails", "compactExpand", "compactCollapse", "compactSettings", "compactEnabled"):
        assert he[key] and en[key]
    css = panel_page.PAGE.split("/* Shared disclosure:")[1].split("</style>")[0]
    assert "min-block-size:48px" in css
    assert "padding-inline" in css and "text-align:start" in css
    assert "var(--ok)" in css and "var(--accent)" in css
    assert "grid-template-rows .16s" in css
    assert ".compact:not(.open)>.compact-reveal *{visibility:hidden!important}" in css


def test_totals_precede_one_group_per_model_and_short_lists_stay_visible():
    run_js('''
const m={input_tokens:40,output_tokens:0,estimate_usd:.01};
const v={tokens:40,estimate_usd:.01,models:{"claude-opus-4-6":m}};
const body=el("div");usageBreakdown([["Today",v],["Session",v]],body,"key");
const groups=body.children.filter(n=>n.attrs["data-block"]);assert.equal(groups.length,1);
assert.equal(body.children.at(-1),groups[0]);
assert(text(groups[0].body).includes("Today"));assert(text(groups[0].body).includes("Session"));
const short=el("div"),long=el("div"),host=el("div");
compactLong(host,"short","List",short,4);assert.equal(host.children[0],short);
compactLong(host,"long","List",long,5);assert.equal(host.children[1].body,long);
assert.equal(toggle(host.children[1]).attrs["aria-expanded"],"false");
''')
