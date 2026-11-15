"""The "My schedule" card and its add sheet, run in node with the tiny fake DOM (no browser).

Layout, 390 px fit and the real Now screen are covered by test_cockpit_v2_schedule_browser.py."""
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

EXTRA = r"""
doc.body = new El("body");
var realCreate = doc.createElement;
doc.createElement = function (t) {
  var e = realCreate(t);
  if (t === "textarea") { e.value = ""; e.disabled = false; }
  if (t === "dialog") { e.open = false; e.showModal = function () { e.open = true; }; e.close = function () { e.open = false; }; }
  return e;
};
Object.defineProperty(El.prototype, "isConnected", {get: function () { var n = this; while (n.parentNode) n = n.parentNode; return n === doc.body || n === shell; }});
sandbox.document = doc;
sandbox.RTL = %(rtl)s;
var toasts = [], queues = {}, lastReply = {}, renders2 = 0;
sandbox.toast = function (text, bad) { toasts.push([text, !!bad]); };
sandbox.haptic = function () {};
sandbox.bindBackLayer = function (key, node, on, apply) { return function (v) { apply(v); }; };
sandbox.render = function () { renders2++; };
sandbox.view = "me";
sandbox.last = {};
function reply(path, status, body) { (queues[path] = queues[path] || []).push({status: status, body: body}); }
sandbox.post = function (path, body) {
  posted.push([path, body]);
  var q = queues[path] || [], r = q.length ? q.shift() : lastReply[path];
  if (r === "reject") return Promise.reject(new Error("offline"));
  if (!r) return Promise.reject(new Error("no reply queued for " + path));
  return Promise.resolve({status: r.status, ok: r.status < 400, json: function () { return Promise.resolve(r.body); }});
};
function row(title, extra) { return Object.assign({id: "a".repeat(12), title: title, time: null, done: false, kind: "own", late: false, allday: false}, extra || {}); }
function view(today, tomorrow, extra) {
  var open = function (rows) { return rows.filter(function (r) { return r.kind === "own" && !r.done; }).length; };
  return Object.assign({today: "2026-11-15", tomorrow: "2026-11-16", days: {today: today, tomorrow: tomorrow},
    open: {today: open(today), tomorrow: open(tomorrow)}, calendar: {configured: false, ok: true}}, extra || {});
}
"""


def run(body, lang="he"):
    strings = {k: v for k, v in panel_page.STRINGS[lang].items() if isinstance(v, str)}
    strings.setdefault("reopen", "reopen")
    v2 = {"schedule": panel_page.V2_STRINGS[lang]["schedule"]}
    code = "\n".join(func(n) for n in ("el", "icon")) + "\n" + (SRC / "cockpit_v2_schedule.js").read_text()
    src = PRELUDE + (EXTRA % {"rtl": "true" if lang == "he" else "false"}) + (
        "\nsandbox.LANG=%s;sandbox.S=%s;sandbox.V2=%s;sandbox.ICONS={};sandbox.nf=new Intl.NumberFormat(%s);docEl.dir=%s;\n"
        % (json.dumps(lang), json.dumps(strings, ensure_ascii=False), json.dumps(v2, ensure_ascii=False), json.dumps(lang),
           json.dumps("rtl" if lang == "he" else "ltr"))
        + "vm.runInNewContext(%s, sandbox);\n" % json.dumps(code)
        + "(async()=>{%s})().catch(e=>{console.error(e);process.exit(1);});" % body)
    result = subprocess.run([NODE, "-"], input=src, capture_output=True, text=True, timeout=30, env=dict(os.environ, TZ="Asia/Jerusalem"))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")

CARD = ("lastReply['/api/schedule-view']={status:200,body:{ok:true,view:%s}};"
        "var first=sandbox.v2ScheduleCard();await flush();await flush();"   # the first draw starts the load; the screen then draws again
        "var card=sandbox.v2ScheduleCard();root.appendChild(card);"
        "function rows(){return card.querySelectorAll('.sc-row');}"
        "function tab(d){return card.querySelector('[data-sc-tab=\"'+d+'\"]');}")


def card(view="view([],[])", body="", **kw):
    return run(CARD % view + body, **kw)


def test_the_card_loads_through_the_read_route_and_has_the_right_roles():
    got = card("view([row('לשלם חשבון',{time:'09:30'})],[])",
               "emit({first:posted[0],section:card.attrs['data-v2-section'],label:card.attrs['aria-label'],tabs:card.querySelector('.sc-tabs').attrs.role,"
               "tabRoles:card.querySelectorAll('[role=tab]').map(t=>[t.attrs['data-sc-tab'],t.attrs['aria-selected']]),add:card.querySelector('[data-sc=add]').attrs['aria-haspopup'],"
               "renders:renders2});")
    assert got["first"] == ["/api/schedule-view", {}] and got["section"] == "schedule" and got["label"] == "הלו״ז שלי"
    assert got["tabs"] == "tablist" and got["tabRoles"] == [["today", "true"], ["tomorrow", "false"]]
    assert got["add"] == "dialog" and got["renders"] == 1  # the load finished and asked the screen to draw again


@pytest.mark.parametrize("lang,title,today", [("he", "הלו״ז שלי", "היום"), ("en", "My schedule", "Today")])
def test_both_languages(lang, title, today):
    got = card("view([row('x')],[])", "emit({title:card.querySelector('.sc-heading').textContent,tab:tab('today').textContent});", lang=lang)
    assert got["title"] == title and got["tab"].startswith(today)


def test_rows_keep_the_order_the_bridge_gave_and_show_time_or_no_time():
    got = card("view([row('breakfast',{time:'08:00',id:'1'.repeat(12)}),row('call',{time:'17:00',id:'2'.repeat(12)}),row('no time',{id:'3'.repeat(12)}),"
               "row('missed',{late:true,id:'4'.repeat(12)}),row('Family day',{kind:'cal',id:'',allday:true}),row('Sync',{kind:'cal',id:'',time:'10:00'})],[])",
               "emit(rows().map(r=>[r.querySelector('.sc-title').textContent,r.querySelector('.sc-time').textContent,!!r.querySelector('.sc-check'),r.className]));")
    assert [(r[0], r[1], r[2]) for r in got] == [
        ("breakfast", "08:00", True), ("call", "17:00", True), ("no time", "בלי שעה", True), ("missed", "באיחור", True),
        ("Family day", "כל היום", False), ("Sync", "10:00", False)]
    assert "cal" in got[4][3] and "cal" in got[5][3]


def test_names_are_isolated_for_direction_and_never_a_mixed_label():
    got = card("view([row('Call David about the invoice')],[])",
               "var t=rows()[0].querySelector('.sc-title');emit({tag:t.tagName,dir:t.dir,time:rows()[0].querySelector('.sc-time').childNodes[0].tagName});")
    assert got == {"tag": "bdi", "dir": "auto", "time": "bdi"}


def test_the_tabs_switch_without_asking_the_bridge_again():
    got = card("view([row('today thing',{time:'09:00'})],[row('tomorrow thing',{time:'10:00'}),row('another',{id:'b'.repeat(12)})])",
               "var before=rows().map(r=>r.querySelector('.sc-title').textContent);tab('tomorrow').click();"
               "var after=card.querySelectorAll('.sc-row').map(r=>r.querySelector('.sc-title').textContent);"
               "emit({before,after,selected:[tab('today').attrs['aria-selected'],tab('tomorrow').attrs['aria-selected']],"
               "counts:[tab('today').querySelector('.sc-n').textContent,tab('tomorrow').querySelector('.sc-n').textContent],reads:posted.length});")
    assert got["before"] == ["today thing"] and got["after"] == ["tomorrow thing", "another"]
    assert got["selected"] == ["false", "true"] and got["counts"] == ["1", "2"] and got["reads"] == 1


def test_empty_days_say_something_friendly_in_each_tab():
    got = card("view([],[])", "var a=card.querySelector('.sc-empty').textContent;tab('tomorrow').click();"
                              "emit({a,b:card.querySelector('.sc-empty').textContent,state:card.querySelector('[data-sc-state]').attrs['data-sc-state'],rows:rows().length});")
    assert "יום רגוע" in got["a"] and "מחר עוד ריק" in got["b"] and got["state"] == "empty" and got["rows"] == 0


def test_one_tap_done_and_one_tap_undo_post_exactly_and_use_the_answer():
    ident = "a" * 12
    got = card("view([row('pay',{id:'%s'})],[])" % ident,
               "var b=card.querySelector('[data-sc-done=\"%s\"]');var pressed0=b.attrs['aria-pressed'];"
               "reply('/api/schedule',200,{ok:true,view:view([row('pay',{id:'%s',done:true})],[])});b.click();await flush();await flush();"
               "var afterDone=sandbox.scData.days.today[0].done;"
               "reply('/api/schedule',200,{ok:true,view:view([row('pay',{id:'%s'})],[])});"
               "sandbox.scRerender();var fresh=sandbox.v2ScheduleCard();root.appendChild(fresh);"
               "var b2=fresh.querySelector('[data-sc-done]');b2.click();await flush();await flush();"
               "emit({pressed0,posts:posted.filter(p=>p[0]==='/api/schedule'),afterDone,end:sandbox.scData.days.today[0].done});" % (ident, ident, ident))
    assert got["pressed0"] == "false" and got["afterDone"] is True and got["end"] is False
    assert got["posts"] == [["/api/schedule", {"action": "done", "id": "a" * 12, "done": True}],
                            ["/api/schedule", {"action": "done", "id": "a" * 12, "done": False}]]


def test_a_done_row_is_labeled_undo_and_pressed():
    got = card("view([row('pay',{done:true})],[])", "var b=card.querySelector('[data-sc-done]');emit({p:b.attrs['aria-pressed'],l:b.attrs['aria-label'],c:b.className});")
    assert got["p"] == "true" and got["l"].startswith("ביטול סימון") and "on" in got["c"]


def test_a_failed_done_keeps_the_button_usable_and_says_so():
    got = card("view([row('pay')],[])", "var b=card.querySelector('[data-sc-done]');reply('/api/schedule',503,{error:'unavailable'});b.click();await flush();await flush();"
                                        "emit({disabled:b.disabled,toasts});")
    assert got["disabled"] is False and got["toasts"] == [["לא הצלחתי לשמור, נסו שוב", True]]


def test_a_stale_login_asks_to_reopen():
    got = card("view([row('pay')],[])", "var b=card.querySelector('[data-sc-done]');reply('/api/schedule',403,{error:'forbidden'});b.click();await flush();await flush();emit({toasts});")
    assert got["toasts"] == [[panel_page.STRINGS["he"]["reopen"], True]]


def test_a_load_failure_shows_a_retry_and_does_not_loop():
    got = run("lastReply['/api/schedule-view']={status:500,body:{}};var c=sandbox.v2ScheduleCard();root.appendChild(c);await flush();await flush();"
              "var again=sandbox.v2ScheduleCard();root.appendChild(again);await flush();"
              "emit({reads:posted.length,renders:renders2,state:again.querySelector('[data-sc-state]').attrs['data-sc-state'],retry:!!again.querySelector('.sc-retry')});")
    assert got == {"reads": 1, "renders": 1, "state": "error", "retry": True}


def test_a_calendar_that_failed_is_said_quietly_with_the_own_items_still_shown():
    got = card("view([row('mine')],[],{calendar:{configured:true,ok:false}})", "emit({rows:rows().length,warn:card.querySelector('.sc-warn').textContent});")
    assert got == {"rows": 1, "warn": "היומן לא זמין כרגע"}


# --- the add sheet --------------------------------------------------------------------------------------------------

OPEN = (CARD % "view([],[])" + "card.querySelector('[data-sc=add]').click();"
        "var dlg=doc.body.childNodes[0];var field=dlg.querySelector('[data-sc=field]'),save=dlg.querySelector('[data-sc=save]');")


def sheet(body, lang="he"):
    return run(OPEN + body, lang=lang)


def test_the_sheet_has_one_text_field_a_hebrew_placeholder_and_the_right_roles():
    got = sheet("emit({open:dlg.open,label:dlg.attrs['aria-labelledby'],dir:dlg.dir,section:dlg.attrs['data-v2-section'],"
                "fields:dlg.querySelectorAll('textarea').length,inputs:dlg.getElementsByTagName('input').length,ph:field.placeholder,fieldDir:field.dir,"
                "max:field.maxLength,saveOff:save.disabled,title:dlg.querySelector('.topic-heading').childNodes[0].textContent});")
    assert got["open"] is True and got["label"] == "sc-sheet-title" and got["dir"] == "rtl" and got["section"] == "schedule-add"
    assert got["fields"] == 1 and got["inputs"] == 0 and got["max"] == 200 and got["saveOff"] is True
    assert got["ph"] == "למשל: להתקשר לרופא מחר בשעה 10:00" and got["title"] == "פריט חדש ללו״ז"
    assert got["fieldDir"] == "rtl"  # an empty field takes the page's direction


def test_an_empty_field_takes_ltr_in_english_and_typing_enables_save():
    got = sheet("var empty=field.dir,off=save.disabled;field.value='hello';field.fire('input');emit({empty,off,on:!save.disabled,ph:field.placeholder});", lang="en")
    assert got == {"empty": "ltr", "off": True, "on": True, "ph": "For example: call the dentist tomorrow at 10:00"}


def test_saving_posts_the_trimmed_text_closes_and_opens_the_matching_tab():
    got = sheet("field.value='  פגישה מחר בשעה 17:00  ';field.fire('input');"
                "reply('/api/schedule',200,{ok:true,item:{id:'f'.repeat(12),title:'פגישה',date:'2026-11-16',time:'17:00',done:false},"
                "view:view([],[row('פגישה',{time:'17:00'})])});"
                "save.click();await flush();await flush();"
                "emit({posts:posted.filter(p=>p[0]==='/api/schedule'),open:dlg.open,tab:sandbox.scTab,toasts,field:field.value});")
    assert got["posts"] == [["/api/schedule", {"action": "add", "text": "פגישה מחר בשעה 17:00"}]]
    assert got["open"] is False and got["tab"] == "tomorrow" and got["toasts"] == [["נוסף ללו״ז", False]] and got["field"] == ""


def test_an_undated_item_opens_the_today_tab():
    got = sheet("sandbox.scTab='tomorrow';field.value='x';field.fire('input');"
                "reply('/api/schedule',200,{ok:true,item:{id:'f'.repeat(12),title:'x',date:null,time:null,done:false},view:view([row('x')],[])});"
                "save.click();await flush();await flush();emit({tab:sandbox.scTab});")
    assert got == {"tab": "today"}


@pytest.mark.parametrize("status,text", [(400, "כתבו שורה קצרה, עד 200 תווים"), (409, "הלו״ז מלא (200 פריטים). סמנו כמה כבוצעו ונסו שוב."),
                                         (503, "לא הצלחתי לשמור, נסו שוב")])
def test_errors_stay_in_the_sheet_with_the_text_kept(status, text):
    got = sheet("field.value='keep me';field.fire('input');reply('/api/schedule',%d,{error:'x'});save.click();await flush();await flush();"
                "emit({open:dlg.open,out:dlg.querySelector('.sc-out').textContent,field:field.value,disabled:field.disabled,saveOff:save.disabled});" % status)
    assert got == {"open": True, "out": text, "field": "keep me", "disabled": False, "saveOff": False}


def test_a_stale_login_in_the_sheet_asks_to_reopen_and_keeps_the_text():
    got = sheet("field.value='keep';field.fire('input');reply('/api/schedule',403,{error:'forbidden'});save.click();await flush();await flush();"
                "emit({open:dlg.open,field:field.value,toasts});")
    assert got == {"open": True, "field": "keep", "toasts": [[panel_page.STRINGS["he"]["reopen"], True]]}


def test_an_empty_or_busy_save_sends_nothing():
    got = sheet("save.click();field.value='   ';field.fire('input');save.click();emit({posts:posted.filter(p=>p[0]==='/api/schedule').length,off:save.disabled});")
    assert got == {"posts": 0, "off": True}


def test_enter_saves_and_shift_enter_does_not():
    got = sheet("field.value='x';field.fire('input');reply('/api/schedule',503,{});var prevented=0;"
                "field.fire('keydown',{key:'Enter',shiftKey:true,preventDefault:function(){prevented++;}});"
                "var shiftPosts=posted.filter(p=>p[0]==='/api/schedule').length;"
                "field.fire('keydown',{key:'Enter',shiftKey:false,preventDefault:function(){prevented++;}});await flush();"
                "emit({shiftPosts,posts:posted.filter(p=>p[0]==='/api/schedule').length,prevented});")
    assert got == {"shiftPosts": 0, "posts": 1, "prevented": 1}


def test_close_button_and_escape_both_close_the_sheet():
    got = sheet("var closed=dlg.querySelector('.topic-close');closed.click();var a=dlg.open;"
                "card.querySelector('[data-sc=add]').click();var dlg2=doc.body.childNodes[doc.body.childNodes.length-1];"
                "var b=dlg2.open;dlg2.fire('cancel',{preventDefault:function(){}});emit({a,b,c:dlg2.open,sheets:doc.body.childNodes.length});")
    assert got["a"] is False and got["b"] is True and got["c"] is False and got["sheets"] == 1  # the first sheet was replaced, not stacked


def test_the_now_screen_puts_the_card_right_after_the_decisions():
    from cockpit_v2_browser import sample_state
    script = (SRC / "cockpit_v2.js").read_text().replace("__V2_STRINGS__", json.dumps(panel_page.V2_STRINGS, ensure_ascii=False))
    script = "\n".join(func(n) for n in ("el", "icon", "svgAdd", "he", "shown", "compactStorageKey", "compactBlock", "requestAction", "requestRow",
                                         "v2ModeRow", "settingsBox")) + script + "\n" + (SRC / "cockpit_v2_schedule.js").read_text()
    src = PRELUDE + "\nsandbox.last=null;\nbootV2(%s,%s,%s,%s);\n(async()=>{await flush();%s})().catch(e=>{console.error(e);process.exit(1);});" % (
        json.dumps(script), json.dumps(sample_state("he")), json.dumps(panel_page.STRINGS["he"]), json.dumps("he"),
        "emit({sections:root.children.map(e=>e.attrs['data-v2-section']||e.className.split(' ')[0]).slice(0,4),card:byClass('v2-schedule').length});")
    result = subprocess.run([NODE, "-"], input=src, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)
    assert got["card"] == 1 and got["sections"][1:3] == ["decisions", "schedule"]
