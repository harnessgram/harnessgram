"""The Work map card and the Good morning card, run in node with the tiny fake DOM (no browser).

Layout and the 390 px fit are covered by test_cockpit_v2_workmap_browser.py."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ctb import panel_page
from test_panel_page import func

NODE = shutil.which("node")
HERE = Path(__file__).parent
SRC = Path(panel_page.__file__).parent
PRELUDE = (HERE / "studio_fake_dom.js").read_text() + (HERE / "cockpit_v2_fake_dom.js").read_text()
pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")

EXTRA = r"""
doc.body = new El("body");
var toasts = [], gone = [], lastReply = {status: 200, body: {ok: true}};
sandbox.document = doc;
sandbox.toast = function (text, bad) { toasts.push([text, !!bad]); };
sandbox.haptic = function () {};
sandbox.render = function () { sandbox.renders++; };
sandbox.renders = 0;
sandbox.view = "agents";
sandbox.v2Card = function (section, title) { var c = sandbox.el("section", "v2-card"); c.setAttribute("data-v2-section", section); c.appendChild(sandbox.el("h2", "card-title", title)); return c; };
sandbox.post = function (path, body) {
  posted.push([path, body]);
  if (lastReply === "reject") return Promise.reject(new Error("offline"));
  return Promise.resolve({status: lastReply.status, ok: lastReply.status < 400, json: function () { return Promise.resolve(lastReply.body); }});
};
sandbox.srcOpen = {};
sandbox.bindBackLayer = function (k, o, on, set) { return set; };
function line(status, what, impact) { return {what: what, status: status, impact: impact || "", source: "ledger"}; }
"""


def run(body, lang="he"):
    strings = {k: v for k, v in panel_page.STRINGS[lang].items() if isinstance(v, str)}
    strings.setdefault("reopen", "reopen")
    v2 = {"workmap": panel_page.V2_STRINGS[lang]["workmap"]}
    code = "\n".join(func(n) for n in ("el", "icon", "shown", "source")) + "\n" + (SRC / "cockpit_v2_workmap.js").read_text()
    src = PRELUDE + EXTRA + (
        "\nsandbox.LANG=%s;sandbox.S=%s;sandbox.V2=%s;sandbox.ICONS={};sandbox.nf=new Intl.NumberFormat(%s);docEl.dir=%s;\n"
        % (json.dumps(lang), json.dumps(strings, ensure_ascii=False), json.dumps(v2, ensure_ascii=False), json.dumps(lang),
           json.dumps("rtl" if lang == "he" else "ltr"))
        + "vm.runInNewContext(%s, sandbox);\n" % json.dumps(code)
        + "(async()=>{%s})().catch(e=>{console.error(e);process.exit(1);});" % body)
    result = subprocess.run([NODE, "-"], input=src, capture_output=True, text=True, timeout=30, env=dict(os.environ, TZ="Asia/Jerusalem"))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def card(lines, body, lang="he", off=False):
    state = "{cockpit_v2:{%s}}" % ("work_map_off:true" if off else "")
    return run("sandbox.last=%s;var p={key:'studio',work_map:%s};var c=sandbox.v2WorkMapCard(p);" % (state, json.dumps(lines)) + body, lang)


def test_lines_are_grouped_by_status_in_the_order_the_bridge_gave():
    got = card([{"what": "Pick a caption", "status": "waiting", "impact": "Video can ship"},
                {"what": "Build filters", "status": "working", "impact": "More choice"},
                {"what": "Old intro removed", "status": "done", "impact": "Cleaner start"}],
               "emit({section:c.attrs['data-v2-section'],heads:c.querySelectorAll('.wm-head').map(h=>h.textContent),"
               "lines:c.querySelectorAll('.wm-line').map(l=>[l.attrs['data-wm-status'],l.querySelector('.wm-what').textContent,l.querySelector('.wm-impact').textContent])});")
    assert got["section"] == "project-workmap"
    assert got["heads"] == ["ממתין לתשובתך", "בטיסה פעילה", "נחת היום"]
    assert [l[:2] for l in got["lines"]] == [["waiting", "Pick a caption"], ["working", "Build filters"], ["done", "Old intro removed"]]
    assert all(l[2].startswith("מה זה משנה לך") for l in got["lines"])


def test_both_languages_title_and_label():
    for lang, title, label in (("he", "תמונת מצב שמיים", "מה זה משנה לך"), ("en", "Work map", "What it changes for you")):
        got = card([{"what": "x", "status": "working", "impact": "y"}],
                   "emit({title:c.querySelector('.card-title').textContent,label:c.querySelector('.wm-impact-label').textContent});", lang)
        assert got == {"title": title, "label": label}


def test_text_is_isolated_for_direction():
    got = card([{"what": "Call David about the invoice", "status": "working", "impact": "Faster"}],
               "var w=c.querySelector('.wm-what');emit({tag:w.tagName,dir:w.dir});")
    assert got == {"tag": "bdi", "dir": "auto"}


def test_six_lines_are_shown_and_a_button_shows_the_rest():
    lines = [{"what": "item %d" % i, "status": "working", "impact": ""} for i in range(9)]
    got = card(lines, "var before=c.querySelectorAll('.wm-line').length;var more=c.querySelector('[data-wm=more]');"
                      "var label=more.textContent;more.click();emit({before,label,renders:sandbox.renders});")
    assert got["before"] == 6 and "9" in got["label"] and got["renders"] == 1
    few = card(lines[:6], "emit({button:!!c.querySelector('[data-wm=more]')});")
    assert few == {"button": False}


def test_an_empty_map_says_so_and_the_flag_hides_the_card():
    assert card([], "emit({text:c.querySelector('.v2-empty').textContent});")["text"].startswith("אין כרגע")
    assert card([], "emit({none:c===null});", off=True) == {"none": True}


MORNING = ("{date:'2026-11-15',schedule:[{time:'10:00',title:'Team sync'},{time:'',title:'Buy milk'}],chores:[],"
           "overnight:[{project:'studio',what:'Landed overnight'}],waiting:[{project:'studio',what:'Pick a caption',impact:'Video can ship'}]}")


def test_the_morning_card_shows_the_sections_that_have_content():
    got = run("var m=%s;var c=sandbox.v2MorningCard({morning:m});"
              "emit({section:c.attrs['data-v2-section'],title:c.querySelector('.wm-mtitle').textContent,"
              "heads:c.querySelectorAll('.wm-head').map(h=>h.textContent),lines:c.querySelectorAll('.wm-what').map(l=>l.textContent),"
              "projects:c.querySelectorAll('.wm-proj').map(l=>l.textContent)});" % MORNING)
    assert got["section"] == "morning" and got["title"] == "בוקר טוב מהקוקפיט"
    assert got["heads"] == ["לוח ההמראות להיום", "נחתו במהלך הלילה", "ממתין לתשובתך"]  # no chores section when there are none
    assert got["lines"] == ["10:00 Team sync", "Buy milk", "Landed overnight", "Pick a caption"]
    assert got["projects"] == ["studio"]  # the project is its own line, never mixed into the sentence


def test_a_quiet_morning_and_no_card_when_there_is_no_briefing():
    got = run("var c=sandbox.v2MorningCard({morning:{date:'d',schedule:[],chores:[],overnight:[],waiting:[]}});"
              "emit({notes:c.querySelectorAll('.wm-none').map(n=>n.textContent),none:sandbox.v2MorningCard({morning:null})===null,absent:sandbox.v2MorningCard({})===null});")
    assert got["none"] and got["absent"] and got["notes"] == ["אין היום המראות מתוכננות בלוח.", "אין כרגע בקשה שמחכה לתשובתך. פתיחת יום רגועה בקוקפיט."]


def test_dismiss_posts_once_and_hides_the_card():
    got = run("var d={morning:%s};var c=sandbox.v2MorningCard(d);var x=c.querySelector('[data-wm=dismiss]');x.click();await flush();await flush();"
              "emit({posted:posted,gone:d.morning,again:sandbox.v2MorningCard({morning:%s})===null,renders:sandbox.renders});" % (MORNING, MORNING))
    assert got["posted"] == [["/api/briefing-dismiss", {}]] and got["gone"] is None and got["again"] and got["renders"] == 1


def test_a_failed_dismiss_keeps_the_card_and_says_so():
    got = run("lastReply='reject';var d={morning:%s};var c=sandbox.v2MorningCard(d);var x=c.querySelector('[data-wm=dismiss]');x.click();await flush();await flush();"
              "emit({kept:d.morning!==null,enabled:!x.disabled,toasts:toasts});" % MORNING)
    assert got["kept"] and got["enabled"] and got["toasts"] and got["toasts"][0][1] is True


def test_a_translated_ledger_line_shows_in_hebrew_rtl_and_the_untranslated_one_as_is():
    m = ("{date:'2026-11-16',schedule:[],chores:[],overnight:[{project:'studio',what:'Fixed PR #12',"
         "what_he:'תוקן PR #12'}],waiting:[{project:'studio',what:'Pick a caption'}]}")
    got = run("var c=sandbox.v2MorningCard({morning:%s});"
              "emit({lines:c.querySelectorAll('.wm-what').map(l=>[l.textContent,l.dir])});" % m)
    (he_text, he_dir), (en_text, en_dir) = got["lines"]
    assert he_dir == "rtl" and he_text.startswith("תוקן") and "⁨PR⁩" in he_text
    assert (en_text, en_dir) == ("Pick a caption", "auto")
