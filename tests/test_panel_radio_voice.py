"""The radio card describes the NEXT bulletin's choice, rather than claiming to know current playback."""
import json
import subprocess

import pytest

from ctb import panel_page
from test_panel_page import func


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("on,source,expected", [
    (False, "none", "radioOff"), (False, "unknown", "radioOff"), (False, "player", "radioOff"),
    (True, "none", "radioNotListening"), (True, "unknown", "radioListenerUnknown"),
    (True, "player", "radioListening"), (True, "unavailable", "radioDetectionOff"),
])
def test_radio_card_listener_hint_matches_on_state_and_next_bulletin(lang, on, source, expected):
    strings = panel_page.STRINGS[lang]
    state = {"on": on, "pending": True, "listening": source == "player", "listening_source": source,
             "listening_detection": source != "unavailable", "route": "vertex"}
    # Exercise the actual card function with a tiny DOM. A previously prepared paid clip may still be playing.
    program = r'''
class Element {
 constructor(){this.children=[];this.style={};this.text="";}
 appendChild(n){this.children.push(n);return n;}
 addEventListener(){}
}
function el(tag,cls,text){var n=new Element();n.cls=cls;n.text=text||"";return n;}
function icon(){return new Element();}
var radioOp=null;
''' + "\nvar S=" + json.dumps(strings) + ",LANG=" + json.dumps(lang) + ";\n" + func("radioCard") + "\n" + '''
var card=radioCard(STATE);
console.log(JSON.stringify(card.children.filter(n=>n.cls==="safe").map(n=>n.text)));
'''.replace("STATE", json.dumps(state))
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True, timeout=5)
    hints = json.loads(result.stdout)
    assert hints[0].startswith(strings[expected])
    if not on:
        assert hints[0] == strings["radioOff"]
    if on and source in ("none", "unknown"):
        assert ("למבזק הבא" if lang == "he" else "next bulletin") in hints[0]
        assert ("פעיל" if lang == "he" else "active") not in hints[0]
