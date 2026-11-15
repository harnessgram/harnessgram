"""The family board inside "My schedule" and its sheet, run in node with the tiny fake DOM (no browser).

Layout, 390 px fit and the real Now screen are covered by test_cockpit_v2_family_browser.py."""
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
var afterprint = [], printed = 0;
var opened2 = [];
sandbox.location = {origin: "https://panel.example"};
sandbox.window = {open: function (u) { opened2.push(u); }, print: function () { printed++; }, addEventListener: function (t, f) { afterprint.push(f); }, removeEventListener: function () {}};
sandbox.RTL = %(rtl)s;
var toasts = [], queues = {}, lastReply = {}, renders2 = 0;
lastReply['/api/family-print'] = {status: 200, body: {ok: true, url: '/family/print?auth=fam1.tok', expires: Math.floor(Date.now() / 1000) + 600}};
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
var NAMES = ["לימודים בערב", "ניקיון יסודי", "בישולים", "כביסות", "הכנות לשבת", "החלפת מצעים", "כביסה קלה מהשבת"];
function it(id, text, extra) { return Object.assign({id: String(id).repeat(12).slice(0, 12), text: text, day: null, done: false}, extra || {}); }
function view(extra) {
  var days = [];
  for (var d = 0; d < 7; d++) days.push({day: d, date: "2026-11-" + (15 + d), chores: [it(d, NAMES[d], {day: d})]});
  var v = Object.assign({today: "2026-11-18", week_start: "2026-11-15", day: 3, shabbat: false, days: days,
    daily: [it("a", "כלים"), it("b", "החזרת משחקים למקום")], shop: [it("c", "חלב"), it("d", "ביצים", {done: true})], meals: [it("e", "שניצל", {day: 2})],
    open: {chores: 1, daily: 2}, caps: {text: 100}}, extra || {});
  return v;
}
"""


def run(body, lang="he"):
    strings = {k: v for k, v in panel_page.STRINGS[lang].items() if isinstance(v, str)}
    strings.setdefault("reopen", "reopen")
    v2 = {"family": panel_page.V2_STRINGS[lang]["family"], "schedule": panel_page.V2_STRINGS[lang]["schedule"]}
    copy = {k: {"family": panel_page.V2_STRINGS[k]["family"]} for k in panel_page.V2_STRINGS}
    code = "\n".join(func(n) for n in ("el", "icon")) + "\n" + (SRC / "cockpit_v2_family.js").read_text()
    src = PRELUDE + (EXTRA % {"rtl": "true" if lang == "he" else "false"}) + (
        "\nsandbox.LANG=%s;sandbox.S=%s;sandbox.V2=%s;sandbox.V2_COPY=%s;sandbox.ICONS={};sandbox.nf=new Intl.NumberFormat(%s);docEl.dir=%s;\n"
        % (json.dumps(lang), json.dumps(strings, ensure_ascii=False), json.dumps(v2, ensure_ascii=False), json.dumps(copy, ensure_ascii=False),
           json.dumps(lang), json.dumps("rtl" if lang == "he" else "ltr"))
        + "vm.runInNewContext(%s, sandbox);\n" % json.dumps(code)
        + "(async()=>{%s})().catch(e=>{console.error(e);process.exit(1);});" % body)
    result = subprocess.run([NODE, "-"], input=src, capture_output=True, text=True, timeout=30, env=dict(os.environ, TZ="Asia/Jerusalem"))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")

BLOCK = ("lastReply['/api/family-view']={status:200,body:{ok:true,view:%s}};"
         "sandbox.v2FamilyBlock();await flush();await flush();"
         "var block=sandbox.v2FamilyBlock();root.appendChild(block);"
         "function rows(){return block.querySelectorAll('.fb-row');}")


def block(v="view()", body="", **kw):
    return run(BLOCK % v + body, **kw)


def test_the_block_reads_through_the_view_route_and_groups_today_and_the_routine():
    got = block(body="emit({read:posted[0],section:block.attrs['data-v2-section'],subs:block.querySelectorAll('.fb-sub').map(h=>h.textContent),"
                     "rows:rows().map(r=>r.querySelector('.fb-text').textContent),open:block.querySelector('[data-fb=open]').attrs['aria-haspopup'],"
                     "renders:renders2});")
    assert got["read"] == ["/api/family-view", {}] and got["section"] == "family-today"
    assert got["subs"] == ["משימות הבית להיום · רביעי", "שגרה יומית"]
    assert got["rows"] == ["כביסות", "כלים", "החזרת משחקים למקום"] and got["open"] == "dialog" and got["renders"] == 1


@pytest.mark.parametrize("lang,sub", [("he", "שגרה יומית"), ("en", "Daily routine")])
def test_both_languages(lang, sub):
    got = block(body="emit({sub:block.querySelectorAll('.fb-sub')[1].textContent,title:block.querySelector('.fb-title').textContent});", lang=lang)
    assert got["sub"] == sub and got["title"] == ("הלוח המשפחתי שלנו" if lang == "he" else "Our family board")


def test_an_empty_day_says_so_instead_of_showing_nothing():
    got = block("view({days:view().days.map(d=>Object.assign(d,{chores:[]}))})", "emit({empty:block.querySelectorAll('.fb-empty').map(e=>e.textContent)});")
    assert got["empty"] == ["אין משימות בית מיוחדות להיום"]


def test_on_shabbat_there_are_no_rows_just_a_quiet_line():
    got = block("view({shabbat:true,day:6})", "emit({rows:rows().length,state:block.querySelector('[data-fb-state]').attrs['data-fb-state'],"
                                              "text:block.querySelector('.fb-note').textContent});")
    assert got["rows"] == 0 and got["state"] == "shabbat" and got["text"].startswith("שבת שלום")


def test_one_tap_ticks_and_one_tap_undoes_with_exact_posts():
    got = block(body="var b=block.querySelector('[data-fb-done=\"' + 'a'.repeat(12) + '\"]');var p0=b.attrs['aria-pressed'];"
                     "var v=view();v.daily[0].done=true;reply('/api/family',200,{ok:true,view:v});b.click();await flush();await flush();"
                     "var d1=sandbox.fbData.daily[0].done;var v2=view();reply('/api/family',200,{ok:true,view:v2});"
                     "var fresh=sandbox.v2FamilyBlock();root.appendChild(fresh);fresh.querySelector('[data-fb-done=\"' + 'a'.repeat(12) + '\"]').click();await flush();await flush();"
                     "emit({p0,d1,end:sandbox.fbData.daily[0].done,posts:posted.filter(p=>p[0]==='/api/family')});")
    assert got["p0"] == "false" and got["d1"] is True and got["end"] is False
    assert got["posts"] == [["/api/family", {"action": "check", "id": "a" * 12, "done": True}],
                            ["/api/family", {"action": "check", "id": "a" * 12, "done": False}]]


def test_a_done_row_is_labeled_undo_pressed_and_crossed_out():
    got = block("view({daily:[it('a','כלים',{done:true})]})", "var b=block.querySelector('[data-fb-done=\"' + 'a'.repeat(12) + '\"]');emit({p:b.attrs['aria-pressed'],l:b.attrs['aria-label'],c:b.className,r:rows()[1].className});")
    assert got["p"] == "true" and got["l"].startswith("ביטול סימון") and "on" in got["c"] and "done" in got["r"]


@pytest.mark.parametrize("status,body,toast", [
    (503, {"error": "unavailable"}, "לא הצלחתי לשמור, נסו שוב"),
    (409, {"error": "shabbat"}, "בשבת לא מסמנים משימות בית"),
    (403, {"error": "forbidden"}, None)])
def test_a_failed_tick_keeps_the_button_usable_and_says_why(status, body, toast):
    got = block(body="var b=block.querySelector('[data-fb-done]');reply('/api/family',%d,%s);b.click();await flush();await flush();emit({disabled:b.disabled,toasts});"
                % (status, json.dumps(body)))
    assert got["disabled"] is False
    assert got["toasts"] == [[toast or panel_page.STRINGS["he"]["reopen"], True]]


def test_a_load_failure_shows_a_retry_and_does_not_loop():
    got = run("lastReply['/api/family-view']={status:500,body:{}};sandbox.v2FamilyBlock();await flush();await flush();"
              "var again=sandbox.v2FamilyBlock();root.appendChild(again);await flush();"
              "emit({reads:posted.length,renders:renders2,state:again.querySelector('[data-fb-state]').attrs['data-fb-state'],retry:!!again.querySelector('.fb-retry')});")
    assert got == {"reads": 1, "renders": 1, "state": "error", "retry": True}


def test_names_are_isolated_for_direction():
    got = block(body="var t=rows()[0].querySelector('.fb-text');emit({tag:t.tagName,dir:t.dir});")
    assert got == {"tag": "bdi", "dir": "auto"}


# --- the sheet ----------------------------------------------------------------------------------------------------------

OPEN = (BLOCK % "view()" + ";block.querySelector('[data-fb=open]').click();await flush();await flush();"
        "var dlg=doc.body.childNodes[0];"
        "function tab(t){return dlg.querySelector('[data-fb-tab=\"'+t+'\"]');}"
        "function field(){return dlg.querySelector('[data-fb=field]');}function btn(n){return dlg.querySelector('[data-fb=\"'+n+'\"]');}")


def sheet(body, lang="he", v=None):
    return run(OPEN.replace("view()", v or "view()", 1) + body, lang=lang)


def test_the_sheet_has_the_right_roles_and_four_tabs():
    got = sheet("emit({open:dlg.open,label:dlg.attrs['aria-labelledby'],dir:dlg.dir,section:dlg.attrs['data-v2-section'],"
                "tabs:dlg.querySelectorAll('.fb-tab').map(t=>[t.attrs['data-fb-tab'],t.attrs['aria-selected'],t.textContent]),"
                "list:dlg.querySelector('.fb-tabs').attrs.role,print:!!btn('print')});")
    assert got["open"] is True and got["label"] == "fb-sheet-title" and got["dir"] == "rtl" and got["section"] == "family-board"
    assert got["tabs"] == [["week", "true", "שבוע"], ["daily", "false", "יומי"], ["shop", "false", "קניות"], ["meals", "false", "תפריט"]]
    assert got["list"] == "tablist" and got["print"] is True


def test_the_week_has_seven_days_with_the_saturday_column_after_shabbat_and_today_selected():
    got = sheet("emit({tabs:dlg.querySelectorAll('.fb-daytab').map(t=>[t.textContent,t.attrs['aria-selected']]),"
                "days:dlg.querySelectorAll('.fb-day').map(d=>[d.attrs['data-fb-day'],d.className.includes(' on')]),"
                "first:dlg.querySelector('.fb-day').querySelectorAll('.fb-text').map(t=>t.textContent)});")
    assert [t[0] for t in got["tabs"]] == ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "מוצאי שבת"]
    assert [t[1] for t in got["tabs"]] == ["false", "false", "false", "true", "false", "false", "false"]
    assert [d[1] for d in got["days"]] == [False, False, False, True, False, False, False] and len(got["days"]) == 7
    assert got["first"] == ["לימודים בערב"]


def test_choosing_a_day_tab_switches_the_visible_day_without_asking_again():
    got = sheet("dlg.querySelector('[data-fb-daytab=\"6\"]').click();emit({on:dlg.querySelectorAll('.fb-day').map(d=>d.className.includes(' on')),reads:posted.filter(p=>p[0]==='/api/family-view').length});")
    assert got["on"] == [False] * 6 + [True] and got["reads"] == 1


def test_on_shabbat_the_chore_ticks_in_the_sheet_are_disabled():
    got = sheet("emit({off:dlg.querySelectorAll('.fb-check').every(b=>b.disabled)});", v="view({shabbat:true,day:6})")
    assert got["off"] is True


def test_adding_to_a_day_posts_the_weekday_and_the_empty_field_takes_the_page_direction():
    got = sheet("btn('x');dlg.querySelector('[data-fb-add=\"weekly:2\"]').click();var f=field();var d0=f.dir,off=btn('save').disabled;"
                "f.value='לגזום את העץ';f.fire('input');var v=view();v.days[2].chores.push(it('f','לגזום את העץ',{day:2}));reply('/api/family',200,{ok:true,view:v});"
                "btn('save').click();await flush();await flush();"
                "emit({d0,off,post:posted[posted.length-1],tab:dlg.querySelectorAll('.fb-day')[2].querySelectorAll('.fb-text').map(t=>t.textContent),editor:dlg.querySelector('.fb-editor').childNodes.length,toasts});")
    assert got["d0"] == "rtl" and got["off"] is True
    assert got["post"] == ["/api/family", {"action": "add", "list": "weekly", "text": "לגזום את העץ", "day": 2}]
    assert got["tab"] == ["בישולים", "לגזום את העץ"] and got["editor"] == 0 and got["toasts"] == [["נשמר", False]]


def test_an_empty_field_is_ltr_in_english():
    got = sheet("tab('shop').click();dlg.querySelector('[data-fb-add=\"shop\"]').click();emit({d:field().dir});", lang="en")
    assert got["d"] == "ltr"


def test_rename_posts_the_id_and_the_new_text():
    got = sheet("tab('shop').click();dlg.querySelector('[data-fb-edit=\"' + 'c'.repeat(12) + '\"]').click();var f=field();var before=f.value;"
                "f.value='חלב 3%';f.fire('input');var v=view();v.shop[0].text='חלב 3%';reply('/api/family',200,{ok:true,view:v});btn('save').click();await flush();await flush();"
                "emit({before,post:posted[posted.length-1],texts:dlg.querySelectorAll('.fb-text').map(t=>t.textContent)});")
    assert got["before"] == "חלב"
    assert got["post"] == ["/api/family", {"action": "edit", "id": "c" * 12, "text": "חלב 3%"}]
    assert got["texts"] == ["חלב 3%", "ביצים"]


def test_delete_offers_undo_and_undo_adds_the_same_line_back():
    got = sheet("tab('shop').click();dlg.querySelector('[data-fb-edit=\"' + 'c'.repeat(12) + '\"]').click();"
                "var v=view();v.shop.shift();reply('/api/family',200,{ok:true,view:v,removed:{id:'c'.repeat(12),text:'חלב',day:null,list:'shop'}});"
                "btn('delete').click();await flush();await flush();"
                "var bar=dlg.querySelector('.fb-undo');var shown=[!bar.hidden,bar.textContent];"
                "reply('/api/family',200,{ok:true,view:view()});btn('undo').click();await flush();await flush();"
                "emit({shown,posts:posted.filter(p=>p[0]==='/api/family'),hidden:dlg.querySelector('.fb-undo').hidden,texts:dlg.querySelectorAll('.fb-text').map(t=>t.textContent)});")
    assert got["shown"][0] is True and "נמחק: חלב" in got["shown"][1]
    assert got["posts"] == [["/api/family", {"action": "remove", "id": "c" * 12}],
                            ["/api/family", {"action": "add", "list": "shop", "text": "חלב"}]]
    assert got["hidden"] is True and got["texts"] == ["חלב", "ביצים"]


def test_undoing_the_delete_of_a_weekly_chore_keeps_its_weekday():
    got = sheet("dlg.querySelector('[data-fb-edit=\"' + '3'.repeat(12) + '\"]').click();"
                "reply('/api/family',200,{ok:true,view:view(),removed:{id:'3'.repeat(12),text:'כביסות',day:3,list:'weekly'}});btn('delete').click();await flush();await flush();"
                "reply('/api/family',200,{ok:true,view:view()});btn('undo').click();await flush();await flush();emit({post:posted[posted.length-1]});")
    assert got["post"] == ["/api/family", {"action": "add", "list": "weekly", "text": "כביסות", "day": 3}]


def test_clear_checked_is_enabled_only_with_checked_items_and_posts_clear():
    got = sheet("tab('shop').click();var c=btn('clear');var on=!c.disabled;reply('/api/family',200,{ok:true,view:view({shop:[it('c','חלב')]}),cleared:1});c.click();await flush();await flush();"
                "emit({on,post:posted[posted.length-1],after:btn('clear').disabled,texts:dlg.querySelectorAll('.fb-text').map(t=>t.textContent),toasts});")
    assert got["on"] is True and got["post"] == ["/api/family", {"action": "clear"}]
    assert got["after"] is True and got["texts"] == ["חלב"] and got["toasts"] == [["הוסר 1 פריטים".replace("הוסר", "הוסרו"), False]]


def test_the_meal_plan_rows_have_no_tick_and_a_full_list_says_so():
    got = sheet("tab('meals').click();var n=dlg.querySelectorAll('.fb-check').length,rows=dlg.querySelectorAll('.fb-row').length;"
                "dlg.querySelector('[data-fb-add=\"meals\"]').click();var f=field();f.value='פסטה';f.fire('input');reply('/api/family',409,{error:'full'});btn('save').click();await flush();await flush();"
                "emit({n,rows,out:dlg.querySelector('.fb-out').textContent,saveOn:!btn('save').disabled,post:posted[posted.length-1]});")
    assert got["n"] == 0 and got["rows"] == 1 and got["out"] == "הרשימה מלאה. מחקו משהו ונסו שוב." and got["saveOn"] is True
    assert got["post"] == ["/api/family", {"action": "add", "list": "meals", "text": "פסטה"}]


def test_cancel_closes_the_editor_and_close_closes_the_sheet():
    got = sheet("dlg.querySelector('[data-fb-add=\"weekly:1\"]').click();var had=dlg.querySelector('.fb-editor').childNodes.length>0;btn('cancel').click();"
                "var gone=dlg.querySelector('.fb-editor').childNodes.length===0;dlg.querySelector('.topic-close').click();emit({had,gone,open:dlg.open});")
    assert got == {"had": True, "gone": True, "open": False}


# --- print --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("lang", ["he", "en"])
def test_print_builds_a_hebrew_page_with_every_part_and_no_images(lang):
    got = sheet("btn('print').click();var p=doc.body.childNodes.filter(c=>c.id==='fb-print')[0];"
                "var boxes=p.querySelectorAll('.fbp-h').map(h=>h.textContent);"
                "emit({printed,boxes,dir:p.dir,lang:p.attrs.lang||p.lang,head:p.getElementsByTagName('h1')[0].textContent,marks:p.querySelectorAll('.fbp-mark').length,"
                "imgs:p.getElementsByTagName('img').length,cls:doc.body.className});", lang=lang)
    assert got["printed"] == 1 and got["imgs"] == 0 and got["dir"] == "rtl" and got["head"] == "הלוח המשפחתי שלנו"
    assert got["boxes"] == ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "מוצאי שבת", "שגרה יומית", "רשימת קניות", "תפריט שבועי"]
    assert got["marks"] == 7 + 2 + 2 and "fb-printing" in got["cls"]


def test_print_cleans_up_after_the_dialog():
    got = sheet("btn('print').click();afterprint.forEach(f=>f());emit({cls:doc.body.className,left:doc.body.childNodes.filter(c=>c.id==='fb-print').length});")
    assert got["cls"].strip() == "" and got["left"] == 0


def test_the_shabbat_note_says_when_shabbat_ends():
    got = block("view({shabbat:true,day:6,shabbat_until:'17:41'})", "emit({text:block.querySelector('.fb-note').textContent});")
    assert got["text"].endswith("השבת יוצאת ב-17:41.")


def test_the_print_link_is_fetched_when_the_sheet_opens_and_the_tap_opens_it_at_once():
    got = sheet("var asked=posted.filter(p=>p[0]==='/api/family-print').length;var before=posted.length;btn('page').click();"
                "emit({asked,opened:opened2,newPosts:posted.length-before});")
    assert got["asked"] == 1 and got["newPosts"] == 0           # no round-trip inside the tap
    assert got["opened"] == ["https://panel.example/family/print?auth=fam1.tok"]


def test_the_tap_opens_through_telegrams_own_link_opener_straight_away():
    got = sheet("sandbox.wa={openLink:function(u){opened2.push(['tg',u]);}};btn('page').click();emit({opened:opened2});")
    assert got["opened"] == [["tg", "https://panel.example/family/print?auth=fam1.tok"]]


def test_a_tap_before_the_link_arrived_or_after_it_expired_fetches_it_then_opens():
    got = sheet("sandbox.fbLink={url:'/family/print?auth=old',expires:1};lastReply['/api/family-print']={status:200,body:{ok:true,url:'/family/print?auth=new',expires:Math.floor(Date.now()/1000)+600}};"
                "btn('page').click();await flush();await flush();emit({opened:opened2,off:btn('page').disabled});")
    assert got["opened"] == ["https://panel.example/family/print?auth=new"] and got["off"] is False


@pytest.mark.parametrize("status,toast", [(403, None), (500, "לא הצלחתי לשמור, נסו שוב")])
def test_open_as_a_page_failures_are_said_and_nothing_opens(status, toast):
    got = sheet("sandbox.fbLink=null;lastReply['/api/family-print']={status:%d,body:{error:'x'}};btn('page').click();await flush();await flush();"
                "emit({opened:opened2,toasts,off:btn('page').disabled});" % status)
    assert got["opened"] == [] and got["off"] is False and got["toasts"] == [[toast or panel_page.STRINGS["he"]["reopen"], True]]
