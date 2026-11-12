"""Execute the actual page functions in Node with a small DOM; no browser or network."""
import json
import shutil
import subprocess

import pytest

from ctb import panel_page
from test_panel_page import func


def run_js(body, lang="en"):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    names = ("deliveryLabel", "receiptFor", "receiptText", "deliveryReceipts", "doneCard", "cardDoneBox",
             "waitingBubble", "answer", "sendCard", "sayRow")
    script = '''
const assert=require("node:assert/strict");
class Element {
 constructor(tag,cls,text){this.tag=tag;this.className=cls||"";this.textContent=text||"";this.children=[];this.style={};this.disabled=false;}
 appendChild(x){this.children.push(x);return x;}
 append(x){this.children.push(x);}
 replaceWith(x){this.replacement=x;}
 querySelector(){return null;}
 addEventListener(){}
}
function el(t,c,s){return new Element(t,c,s);}
function icon(){return el("i");}
function shown(o,k){return o[k]||"";}
function enter(c){return c;}
function fold(){return el("details");}
function sidTag(s){return el("span",null,s);}
function haptic(){}
function later(){}
function syncClose(){}
function toast(s){toasts.push(s);}
function agentCard(c){return el("div");}
function render(){}
function composer(k,max,ph,send){submit=send;return {row:el("div")};}
function sendId(){return "sample";}
function allText(x){return [x.textContent,...x.children.map(allText)].join("\\n");}
function buttons(x){return (x.tag==="button"?1:0)+x.children.reduce((n,c)=>n+buttons(c),0);}
var S=STRINGS,last={deliveries:[]},answered={},cardDone={},cardBusy={},closed={},notes={},handled=0,
    app=el("main"),toasts=[],sends={},focusSay=null,submit=null,post=null;
'''.replace("STRINGS", json.dumps(panel_page.STRINGS[lang], ensure_ascii=False))
    script += "\n".join(func(name) for name in names)
    script += "\n(async()=>{\n" + body + "\n})().catch(e=>{console.error(e);process.exitCode=1;});"
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("lang", ["he", "en"])
def test_answered_cards_update_on_proof_and_reload_preserves_receipts(lang):
    run_js('''
const d={id:"card",title:"Sample",session:"sample"},a={key:"key",msg:S.deliveryQueued,choice:"yes",ok:true};
for(const state of ["queued","in_flight","delivered"]){
 last.deliveries=[{key:"key",state:state}];
 assert(allText(doneCard(d,a)).includes(deliveryLabel(state)));
 assert(allText(cardDoneBox(a)).includes(deliveryLabel(state)));
}
last.deliveries=[{key:"key",state:"queued",retrying:true}];
assert(allText(doneCard(d,a)).includes(S.deliveryRetry));
assert(allText(cardDoneBox(a)).includes(S.deliveryRetry));
// A reload has neither local answered map; the server's saved receipts still render.
deliveryReceipts({deliveries:[{key:"key",kind:"card",id:"card",title:"Sample",session:"sample",state:"delivered"}]},false);
assert(allText(app).includes(S.deliveryDone));
''', lang)


def test_inflight_words_remain_visible_but_cannot_be_edited_or_cancelled():
    run_js('''
assert.equal(buttons(waitingBubble({id:"s"},{text:"Sample",key:"key",state:"queued"})),2);
const flight=waitingBubble({id:"s"},{text:"Sample",key:"key",state:"in_flight"});
assert.equal(buttons(flight),0);
assert(allText(flight).includes(S.deliveryFlight));
''')


@pytest.mark.parametrize("kind", ["card", "decision"])
def test_queue_full_keeps_the_answer_editable_instead_of_closing_it(kind):
    run_js('''
post=()=>Promise.resolve({ok:false,status:409,json:()=>Promise.resolve({error:"queue full"})});
const box=el("div"),b=el("button"),ta=el("textarea");ta.value="Sample answer";
const c={id:"id",kind:"ask",title:"Sample",yes:"Yes",no:"No"};
CALL
await new Promise(resolve=>setImmediate(resolve));
assert.equal(b.disabled,false);assert.equal(ta.disabled,false);
assert(!closed.id&&!cardDone.id&&!cardBusy.id);
assert(allText(box).includes(S.queueFull));
'''.replace("CALL", 'sendCard(c,box,{id:"id",text:"Sample"},"Sample",[b],null,ta);' if kind == "card"
                            else 'answer(box,c,"yes",[b],ta);'))


def test_retried_say_already_delivered_does_not_show_a_waiting_bubble():
    run_js('''
post=()=>Promise.resolve({ok:true,json:()=>Promise.resolve({key:"key",delivery:"delivered"})});
const s={id:"sample",waiting:[]};sayRow(s);
submit("Sample",()=>{});
await new Promise(resolve=>setImmediate(resolve));
assert.deepEqual(s.waiting,[]);
assert.equal(toasts.at(-1),S.deliveryDone);
''')


def test_say_full_queue_has_its_own_actionable_error():
    run_js('''
post=()=>Promise.resolve({ok:false,status:409,json:()=>Promise.resolve({error:"queue full"})});
const s={id:"sample",waiting:[]};sayRow(s);
let cleared=null;submit("Sample",ok=>cleared=ok);
await new Promise(resolve=>setImmediate(resolve));
assert.equal(cleared,false);assert.equal(toasts.at(-1),S.queueFull);
''')


def test_complete_page_script_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = panel_page.PAGE.split('<script nonce="__NONCE__">', 1)[1].split("</script>", 1)[0]
    result = subprocess.run([node, "--check", "-"], input=script, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("lang,reason", [("he", "ממתין: השיחה אינה נגישה"),
                                        ("en", "Waiting: session not reachable")])
def test_unreachable_session_reason_is_visible_in_receipts_and_waiting_words(lang, reason):
    run_js('''
const reason=REASON;
last.deliveries=[{key:"key",state:"queued",retrying:true}];
assert(receiptText("key","").includes(reason));
deliveryReceipts({deliveries:last.deliveries},false);
assert(allText(app).includes(reason));
assert(allText(waitingBubble({id:"s"},{text:"Sample",key:"key",state:"queued",retrying:true})).includes(reason));
'''.replace("REASON", json.dumps(reason)), lang)
