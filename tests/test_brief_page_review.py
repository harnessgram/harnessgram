"""Render the page functions in a tiny DOM, without a browser or sockets."""
import json
import re
import subprocess

import pytest

from ctb import panel_page
from test_panel_page import func


DOM = r"""
class Element {
 constructor(tag){this.tagName=tag;this.children=[];this.hidden=false;this.style={};this.listeners={};this._text="";}
 appendChild(child){this.children.push(child);return child;}
 set textContent(text){this._text=String(text);this.children=[];}
 get textContent(){return this._text+this.children.map(c=>c.textContent).join("");}
 setAttribute(k,v){this[k]=v;}
 addEventListener(k,f){this.listeners[k]=f;}
 get firstChild(){return this.children[0];}
}
var document={createElement:t=>new Element(t),createTextNode:t=>{var n=new Element('#text');n.textContent=t;return n;}};
var opened={},notes={},noteOpen={},S=STRINGS,RTL=true,nf={format:String};
function shown(x,f){return x[f+'_he']||x[f];}
function ageRow(){return new Element('div');}
function agoS(){return '';}
function ago(){return '';}
function isNew(){return false;}
function icon(){return new Element('svg');}
function source(){}
function says(k,text,he,cls){return el('div',cls,he||text);}
function he(d,f){return d[f+'_he'];}
function enter(c){return c;}
function tune(){return function(){};}
function haptic(){}
function muteBtn(){return new Element('button');}
function syncClose(){}
function all(n){return [n].concat(n.children.flatMap(all));}
function pick(n,cls){return all(n).filter(x=>(x.className||'').split(' ').includes(cls));}
function visible(n){return n.hidden?[]:[n].concat(n.children.flatMap(visible));}
"""


def render(script, lang="he"):
    helpers = ["el", "bAct", "waitTarget", "waitRow", "lineDetails", "lineRow", "decision"]
    # Include the attribution helper when present; the old page still runs and fails the assertions.
    for helper in ("bAgent", "agentText"):
        if "function " + helper + "(" in panel_page.PAGE:
            helpers.append(helper)
    program = DOM.replace("STRINGS", json.dumps(panel_page.STRINGS[lang]))
    program += "\nvar target=\"\";\n" + "\n".join(func(name) for name in helpers) + "\n" + script
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=5, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("lang", ["he", "en"])
def test_1_agent_attribution_is_visible_beside_status_and_needs_you(lang):
    out = render("""
var line={key:'cockpit',name:'Cockpit',text:'Owner approved publication',by:'a1b2c3d4',source:'status',tone:'ok',age:0};
var note={id:'sample',kind:'card',text:'Choose',line_name:'Cockpit',note:'Owner approved publication',note_by:'b2c3d4e5'};
var status=lineRow(line),wait=waitRow(note,{});
console.log(JSON.stringify([status,wait].map(n=>visible(n).filter(x=>x.className==='b-agent').map(x=>x.textContent))));
""", lang)
    assert panel_page.STRINGS[lang]["agentFrom"] + "a1b2c3d4" in out[0]
    assert panel_page.STRINGS[lang]["agentFrom"] + "b2c3d4e5" in out[1]


def test_1_every_checklist_text_keeps_its_attribution():
    out = render("""
var row=lineDetails({source:'ledger',age:0,checklist:{session:'a1b2c3d4',title:'Checklist',age:0,
 items:[{done:false,text:'Owner approved publication'}]}});
console.log(JSON.stringify(pick(row,'b-agent').map(x=>x.textContent)));
""")
    assert out == ["מאת הסוכןa1b2c3d4", "מאת הסוכןa1b2c3d4"]


def test_1_checklist_project_name_is_attributed_while_collapsed():
    out = render("""
var c=lineRow({key:'cockpit',name:'Cockpit',text:'',tone:'idle',age:0,
 checklist:{session:'a1b2c3d4',title:'List',age:0,items:[{done:false,text:'Build'}]}});
console.log(JSON.stringify(visible(c).filter(x=>x.className==='b-agent').map(x=>x.textContent)));
""")
    assert out == ["מאת הסוכןa1b2c3d4"]


def test_5_project_headings_wrap_with_logical_size_limits():
    css = panel_page.PAGE.split("<style", 1)[1].split("</style>", 1)[0]
    for selector in (".bl-n b", ".bw-k"):
        rules = [body for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
                 if selector in [s.strip() for s in selectors.split(",")]]
        combined = ";".join(rules)
        assert "overflow-wrap:anywhere" in combined
        assert "max-inline-size:100%" in combined
        assert not re.search(r"(?:^|[;\s])(?:width|min-width|max-width|left|right):", combined)


@pytest.mark.parametrize("show_topic", [False, True])
def test_8_decision_itself_contains_owner_approval_notice(show_topic):
    out = render("""
var c=decision({id:'sample',title:'Approve?',yes:'Yes',no:'No',age:0},SHOW);
console.log(JSON.stringify(pick(c,'safe').map(x=>x.textContent)));
""".replace("SHOW", json.dumps(show_topic)))
    assert out == [panel_page.STRINGS["he"]["tapNote"]]


@pytest.mark.parametrize("expanded", [False, True])
def test_8_brief_decision_expansion_keeps_the_notice(expanded):
    out = render("""
opened.bwsample=EXPANDED;
var w={id:'sample',kind:'decision',text:'Approve?',line_name:'Studio'};
var c=waitRow(w,{decisions:[{id:'sample',title:'Approve?',yes:'Yes',no:'No',age:0}]});
if(!opened.bwsample)pick(c,'bact')[0].listeners.click({stopPropagation(){}});
console.log(JSON.stringify(visible(c).filter(x=>x.className==='safe').map(x=>x.textContent)));
""".replace("EXPANDED", json.dumps(expanded)))
    assert out == [panel_page.STRINGS["he"]["tapNote"]]


def test_9_english_project_and_hebrew_question_render_on_separate_lines():
    out = render("""
var c=waitRow({id:'sample',kind:'ledger',line_name:'Studio',text:'איזה סגנון לבחור?'},{});
console.log(JSON.stringify(pick(c,'bw-t')[0].children.map(x=>[x.tagName,x.textContent,x.dir])));
""")
    assert out == [["div", "Studio", "auto"], ["div", "איזה סגנון לבחור?", "auto"]]


@pytest.mark.parametrize("lang", ["he", "en"])
def test_attribution_uses_localized_label_and_separate_lines(lang):
    out = render("""
var c=lineRow({key:'sample',name:'בדיקה',text:'',source:'status',by:'a1b2c3d4',tone:'ok',age:0});
var heading=pick(c,'bl-n')[0], attribution=pick(c,'b-agent')[0];
console.log(JSON.stringify({heading:heading.children.map(x=>[x.tagName,x.textContent]),
 parts:attribution.children.map(x=>[x.tagName,x.textContent,x.dir])}));
""", lang)
    assert out["heading"][0] == ["b", "בדיקה"]
    assert out["parts"] == [["div", panel_page.STRINGS[lang]["agentFrom"], None], ["div", "a1b2c3d4", "ltr"]]
    css = panel_page.PAGE.split("<style", 1)[1].split("</style>", 1)[0]
    assert re.search(r"\.bl-n\s*\{[^}]*flex-direction:\s*column", css)
