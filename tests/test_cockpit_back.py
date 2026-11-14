"""Exercise the actual navigation controller with asynchronous browser-history events, offline."""
import shutil
import subprocess

import pytest

from test_panel_page import func, js


def run_navigation(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    prelude = r'''
const assert = require("node:assert/strict");
var events={},queued=[],historyEntries=[null,null],position=1,exited=false;
var window={addEventListener:(event,f)=>events[event]=f,scrollTo(){}};
var history={get state(){return historyEntries[position];},
 replaceState(s){historyEntries[position]=structuredClone(s);},
 pushState(s){historyEntries.length=position+1;historyEntries.push(structuredClone(s));position++;},
 go(delta){queued.push(position+delta);},back(){this.go(-1);}};
function flush(){var guard=0;while(queued.length){assert.ok(++guard<20,"back must not loop");
 position=queued.shift();if(position<1){exited=true;return;}events.popstate({state:history.state});}}
var timers=[],backWait={},notes={},view="here",last={},TABKEY="tab",savedTab=null;
function setTimeout(f){timers.push(f);return timers.length;}
function clearTimeout(i){if(i)timers[i-1]=null;}
function tick(){var work=timers;timers=[];work.forEach(f=>f&&f());flush();}
var wa={BackButton:{visible:false,show(){this.visible=true;},hide(){this.visible=false;}},
 enableClosingConfirmation(){this.confirm=true;},disableClosingConfirmation(){this.confirm=false;},close(){this.closed=true;}};
var sessionStorage={setItem(k,v){savedTab=v;}};
var Node={DOCUMENT_POSITION_FOLLOWING:4};
function getComputedStyle(){return {visibility:"visible"};}
function visibleNode(){return {isConnected:true,getClientRects(){return [1];},compareDocumentPosition(){return 4;}};}
function ver(){return true;}function haptic(){}function activeApp(){return null;}
function render(){}function leaveApp(){throw Error("unexpected app exit");}
'''
    code = js()
    controller = code[code.index("// Each entry is one reversible UI change."):code.index("// --- text that agents wrote:")]
    script = prelude + func("syncClose").split("// Each entry")[0] + controller
    script += "\n".join(func(name) for name in ("appOf", "syncBack", "backTap", "switchTo")) + body
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("native", [True, False])
def test_sheet_and_tabs_share_one_stack_and_root_is_not_a_sentinel(native):
    run_navigation(r'''
assert.equal(historyEntries.length,2); // replacing the launch entry never adds a root trap
switchTo("agents");switchTo("settings");
var sheet=false,setSheet=bindBackLayer("sheet",null,false,on=>sheet=on);setSheet(true);
assert.equal(backDepth(),2);assert.equal(wa.BackButton.visible,true);
function back(){NATIVE ? history.back() : backTap();flush();}
back();assert.equal(sheet,false);assert.equal(view,"settings");
back();assert.equal(view,"here");assert.equal(savedTab,"here");
assert.equal(backDepth(),0);assert.equal(wa.BackButton.visible,false);
history.back();flush();assert.equal(exited,true);
'''.replace("NATIVE", "true" if native else "false"))


def test_manual_dismissal_and_tab_selection_skip_stale_sheet_entries():
    run_navigation(r'''
var sheet=false,setSheet=bindBackLayer("sheet",null,false,on=>sheet=on);
setSheet(true);setSheet(false);tick();assert.equal(backIndex,0);assert.equal(backDepth(),0);
setSheet(true);setSheet(false);switchTo("agents");tick();
backTap();flush();assert.equal(view,"here");assert.equal(backIndex,0);assert.equal(backDepth(),0);
history.back();flush();assert.equal(exited,true);
''')


def test_redraw_rebinds_layer_undo_and_forward_replays_the_same_path():
    run_navigation(r'''
switchTo("agents");var oldOpen=false,newOpen=true;
bindBackLayer("block",null,false,on=>oldOpen=on)(true);
bindBackLayer("block",null,true,on=>newOpen=on); // a polling render replaces the node
backTap();flush();assert.equal(oldOpen,true);assert.equal(newOpen,false);assert.equal(view,"agents");
history.go(1);flush();assert.equal(newOpen,true);assert.equal(backDepth(),2);
backTap();flush();backTap();flush();assert.equal(view,"here");assert.equal(backDepth(),0);
''')


def test_duplicate_native_taps_do_not_skip_layers_or_disable_unsaved_confirmation():
    run_navigation(r'''
switchTo("agents");var open=false;bindBackLayer("block",null,false,on=>open=on)(true);
notes.draft="unsent";syncClose();backTap();backTap();flush();
assert.equal(open,false);assert.equal(view,"agents");assert.equal(backDepth(),1);
backTap();flush();backTap();assert.equal(wa.closed,true);assert.equal(wa.confirm,true);
assert.equal(notes.draft,"unsent");assert.equal(backDepth(),0);assert.equal(wa.BackButton.visible,false);
''')


def test_tapping_home_removes_only_the_tab_step_and_preserves_its_open_layer():
    run_navigation(r'''
var open=false,setOpen=bindBackLayer("block",null,false,on=>open=on);setOpen(true);
switchTo("agents");switchTo("here");flush();
// Assert before any rebind: returning home must never invoke its layer's undo.
assert.equal(open,true);assert.equal(backIndex,1);assert.equal(backDepth(),1);
backTap();flush();assert.equal(open,false);assert.equal(view,"here");
assert.equal(view,"here");assert.equal(backDepth(),0);assert.equal(backIndex,0);
history.back();flush();assert.equal(exited,true);
''')


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("home_layer", [True, False])
def test_a_thousand_non_home_switches_have_one_tab_step(native, home_layer):
    run_navigation(r'''
var open=false;
if(HOME_LAYER)bindBackLayer("block",null,false,on=>open=on)(true);
for(var n=0;n<1000;n++)switchTo(["agents","settings","me"][n%3]);
assert.equal(backEntries.filter(e=>e.tab).length,1);
assert.equal(backIndex,HOME_LAYER?2:1);assert.equal(position,backIndex+1);
assert.equal(historyEntries.length,HOME_LAYER?4:3);
function back(){NATIVE ? history.back() : backTap();flush();}
back();assert.equal(view,"here");assert.equal(open,HOME_LAYER);assert.equal(backDepth(),HOME_LAYER?1:0);
back();if(HOME_LAYER){assert.equal(open,false);assert.equal(backIndex,0);assert.equal(exited,false);}
else if(NATIVE)assert.equal(exited,true);else assert.equal(wa.closed,true);
'''.replace("NATIVE", "true" if native else "false").replace("HOME_LAYER", "true" if home_layer else "false"))


def test_shared_open_disclosure_is_not_adopted_again_on_every_tab_visit():
    run_navigation(r'''
var open=false,node=visibleNode();
bindBackLayer("compact:usage",node,false,on=>open=on)(true);
render=function(){
 node.isConnected=false;node=visibleNode();
 if(view==="here"||view==="me")bindBackLayer("compact:usage",node,open,on=>open=on);
 retireMissingBackLayers();adoptBackLayers();
};
var replacements=0,replace=history.replaceState;
history.replaceState=function(s){replacements++;replace(s);};
for(var n=0;n<1000;n++)switchTo(["agents","settings","me"][n%3]);
assert.equal(backIndex,2);assert.equal(historyEntries.length,4);
assert.equal(replacements,999);assert.equal(backDepth(),2);
backTap();flush();assert.equal(view,"here");assert.equal(open,true);
backTap();flush();assert.equal(open,false);assert.equal(backIndex,0);
''')


def test_closing_a_shared_disclosure_from_another_tab_retires_its_original_step():
    run_navigation(r'''
var open=false,setShared;
bindBackLayer("compact:usage",visibleNode(),false,on=>open=on)(true);
render=function(){setShared=bindBackLayer("compact:usage",visibleNode(),open,on=>open=on);adoptBackLayers();};
switchTo("me");setShared(false);tick();assert.equal(backDepth(),1);
backTap();flush();assert.equal(view,"here");assert.equal(open,false);assert.equal(backIndex,0);
''')


def test_distinct_app_tabs_reuse_the_tab_step_after_dismissing_the_sheet():
    run_navigation(r'''
for(var n=0;n<12;n++){
 var setSheet=bindBackLayer("more",null,false,()=>{});setSheet(true);setSheet(false);
 switchTo("app:app"+n);flush();tick();
 assert.equal(view,"app:app"+n);assert.equal(backIndex,1);
 assert.equal(history.state.cockpitBack.index,1);assert.equal(backDepth(),1);
}
backTap();flush();assert.equal(view,"here");assert.equal(backIndex,0);
history.go(1);flush();assert.equal(view,"app:app11");assert.equal(backIndex,1);
backTap();flush();history.back();flush();assert.equal(exited,true);
''')


def test_leaving_an_app_retires_its_takeover_without_forwarding_back():
    run_navigation(func("appBackLayer") + r'''
var forwarded=0,BACK_WAIT=1200;
function toApp(){forwarded++;}
switchTo("app:one");appBackLayer("one",true);appBackLayer("one",true);
assert.equal(backDepth(),2);assert.equal(backIndex,2);
switchTo("app:two");flush();tick();
assert.equal(view,"app:two");assert.equal(backIndex,1);assert.equal(forwarded,0);
assert.equal(backEntries.some(e=>e.active&&e.key==="app:one|app-back:one"),false);
appBackLayer("two",true);assert.equal(backIndex,2);
backTap();flush();assert.equal(forwarded,1);assert.ok(backWait.two);
switchTo("settings");flush();tick();
assert.equal(view,"settings");assert.equal(backIndex,1);assert.equal(backWait.two,undefined);
backTap();flush();assert.equal(view,"here");assert.equal(backIndex,0);
''')


def test_tab_redo_uses_the_latest_destination_and_new_departure_truncates_forward():
    run_navigation(r'''
switchTo("agents");switchTo("settings");switchTo("me");
history.back();flush();assert.equal(view,"here");assert.equal(backIndex,0);
history.go(1);flush();assert.equal(view,"me");assert.equal(backIndex,1);
switchTo("here");flush();
assert.equal(view,"here");assert.equal(backIndex,0);assert.equal(position,1);
switchTo("agents");assert.equal(historyEntries.length,3);
history.back();flush();assert.equal(view,"here");history.go(1);flush();assert.equal(view,"agents");
backTap();flush();assert.equal(view,"here");
history.back();flush();assert.equal(exited,true);
''')


def test_tab_changes_retire_foreign_layers_without_undo_and_re_adopt_on_return():
    run_navigation(r'''
var homeOpen=false,agentOpen=false,homeNode=visibleNode(),agentNode=visibleNode();
bindBackLayer("home",homeNode,false,on=>homeOpen=on)(true);
render=function(){
 homeNode.isConnected=view==="here";agentNode.isConnected=view==="agents";
 retireMissingBackLayers();adoptBackLayers();
};
switchTo("agents");bindBackLayer("card",agentNode,false,on=>agentOpen=on)(true);
switchTo("settings");tick();
assert.equal(homeOpen,true);assert.equal(agentOpen,true);assert.equal(backDepth(),2);
assert.equal(backEntries[0].active,true); // disconnected home DOM is not a refresh removal
assert.equal(backEntries.find(e=>e.key==="agents|card").active,false);
switchTo("agents");assert.equal(agentOpen,true);assert.equal(backDepth(),3);
backTap();flush();assert.equal(agentOpen,false);assert.equal(view,"agents");
backTap();flush();assert.equal(view,"here");assert.equal(homeOpen,true);assert.equal(backDepth(),1);
backTap();flush();assert.equal(homeOpen,false);assert.equal(backDepth(),0);
''')


def test_tapping_home_keeps_other_views_open_and_forward_adopts_their_layers():
    run_navigation(r'''
var homeOpen=false,agentOpen=false,homeNode=visibleNode(),agentNode=visibleNode();
bindBackLayer("home",homeNode,false,on=>homeOpen=on)(true);
render=function(){
 homeNode.isConnected=view==="here";agentNode.isConnected=view==="agents";
 retireMissingBackLayers();adoptBackLayers();
};
switchTo("agents");bindBackLayer("card",agentNode,false,on=>agentOpen=on)(true);
switchTo("here");flush();
assert.equal(homeOpen,true);assert.equal(agentOpen,true);assert.equal(backIndex,1);
assert.equal(backEntries[0].active,true);assert.equal(backEntries[2].active,false);
history.go(1);flush();assert.equal(view,"agents");assert.equal(agentOpen,true);
assert.equal(backDepth(),3);assert.equal(backEntries.filter(e=>e.tab).length,1);
backTap();flush();assert.equal(agentOpen,false);assert.equal(view,"agents");
backTap();flush();assert.equal(view,"here");assert.equal(homeOpen,true);
backTap();flush();assert.equal(homeOpen,false);assert.equal(backIndex,0);
''')


def test_refresh_retires_removed_layers_but_keeps_rebound_nodes():
    run_navigation(r'''
switchTo("agents");var oldNode={isConnected:false},currentNode={isConnected:true};
bindBackLayer("card",oldNode,false,()=>{})(true);
bindBackLayer("card",currentNode,true,()=>{}); // same card, replacement DOM after render
retireMissingBackLayers();tick();assert.equal(backDepth(),2);
currentNode.isConnected=false; // the next refresh removes the card entirely
retireMissingBackLayers();tick();assert.equal(backDepth(),1);assert.equal(view,"agents");
backTap();flush();assert.equal(view,"here");assert.equal(backIndex,0);
history.back();flush();assert.equal(exited,true);
''')


def test_closing_a_parent_retires_nested_steps_without_erasing_their_preference():
    run_navigation(r'''
var childNode={contains(){return false;}},parentNode={contains(n){return n===childNode;}};
var parent=false,child=false;
var setParent=bindBackLayer("parent",parentNode,false,on=>parent=on);
setParent(true);bindBackLayer("child",childNode,false,on=>child=on)(true);
setParent(false);tick();assert.equal(parent,false);assert.equal(child,true);
assert.equal(backIndex,0);assert.equal(backDepth(),0);assert.equal(wa.BackButton.visible,false);
history.back();flush();assert.equal(exited,true);
''')
