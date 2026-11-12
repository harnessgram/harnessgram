"""Progress activity has separate RTL-safe text, labels, age and job count in both languages."""
import json
import subprocess

import pytest

from ctb import panel_page
from test_panel_cards_by_session import DOM
from test_panel_page import func


@pytest.mark.parametrize("lang", ["he", "en"])
def test_live_activity_renders_below_steps_with_age_and_separate_language_lines(lang):
    c = {"id": "x", "steps": ["Prepare", "Test"], "at": 1, "live": "Tests running",
         "live_at": 800, "live_age": 200, "task": "Check the fix", "bg": 2}
    if lang == "he":
        c.update(live_he="הבדיקות פועלות", task_he="בדיקת התיקון")
    strings = panel_page.STRINGS[lang]
    program = (DOM.replace("STRINGS", json.dumps(strings)).replace("SESSIONS", "[]").replace("WA", "null")
               + "\nvar pf={format:String};function anim(k,v,f){f(v);}function plural(n,one,many){return n===1?one:many;}\n"
               + "\n".join(func(n) for n in ("el", "says", "he", "progressBody"))
               + "\nvar box=new Element('div');progressBody(" + json.dumps(c) + ",box);"
               + "console.log(JSON.stringify(box.children.map(n=>({cls:n.className,text:n.textContent,"
                 "dir:n.dir,childDirs:n.children.map(c=>c.dir)}))));")
    rendered = json.loads(subprocess.run(["node", "-e", program], capture_output=True, text=True,
                                         timeout=5, check=True).stdout)
    assert [n["cls"] for n in rendered[:2]] == ["pg", "track"]
    assert rendered[2]["text"] == strings["cardNow"]
    assert rendered[3]["text"] == c.get("live_he", c["live"])
    assert rendered[3]["childDirs"] == ["auto"]
    assert rendered[4]["text"] == strings["cardLiveUpdated"].replace("{0}", "3")
    assert rendered[5]["text"] == strings["cardTask"]
    assert rendered[6]["text"] == c.get("task_he", c["task"])
    assert rendered[6]["childDirs"] == ["auto"]
    assert rendered[7]["text"] == "2 " + strings["bg"]


def test_live_labels_exist_in_both_languages():
    assert panel_page.STRINGS["he"].keys() == panel_page.STRINGS["en"].keys()
    for key in ("cardNow", "cardTask", "cardLiveUpdated"):
        assert panel_page.STRINGS["he"][key] and panel_page.STRINGS["en"][key]
