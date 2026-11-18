"""The Mac maintenance sheet, run in node with the tiny fake DOM (no browser).

Layout, the real tile in Tools > More and the 390 px screenshots are covered by test_cockpit_mac_tools_browser.py."""
import json
import os
import re
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

GB = 1024 ** 3
EXTRA = r"""
doc.body = new El("body");
var realCreate = doc.createElement;
doc.createElement = function (t) {
  var e = realCreate(t);
  if (t === "dialog") { e.open = false; e.showModal = function () { e.open = true; }; e.close = function () { e.open = false; }; }
  return e;
};
Object.defineProperty(El.prototype, "isConnected", {get: function () { var n = this; while (n.parentNode) n = n.parentNode; return n === doc.body || n === shell; }});
sandbox.document = doc;
sandbox.RTL = %(rtl)s;
var toasts = [], queues = {}, lastReply = {}, haptics = [], clock = [], nextTimer = 1;
sandbox.toast = function (text, bad) { toasts.push([text, !!bad]); };
sandbox.haptic = function (kind, v) { haptics.push([kind, v || ""]); };
sandbox.bindBackLayer = function (key, node, on, apply) { return function (v) { apply(v); }; };
sandbox.setTimeout = function (f, ms) { var t = {id: nextTimer++, f: f, ms: ms, live: true}; clock.push(t); return t.id; };
sandbox.clearTimeout = function (id) { clock.forEach(function (t) { if (t.id === id) t.live = false; }); };
function pending() { return clock.filter(function (t) { return t.live; }); }
async function tick(ms) {  // run the newest live timer that waits `ms`, then let promises settle
  var t = pending().filter(function (x) { return x.ms === ms; }).pop();
  if (!t) throw new Error("no timer of " + ms);
  t.live = false; t.f(); await flush(); await flush();
}
function reply(path, status, body) { (queues[path] = queues[path] || []).push({status: status, body: body}); lastReply[path] = null; }
function sticky(path, status, body) { lastReply[path] = {status: status, body: body}; }
sandbox.post = function (path, body) {
  posted.push([path, body]);
  var q = queues[path] || [], r = q.length ? q.shift() : lastReply[path];
  if (r === "reject") return Promise.reject(new Error("offline"));
  if (!r) return Promise.reject(new Error("no reply queued for " + path));
  return Promise.resolve({status: r.status, ok: r.status < 400, json: function () { return Promise.resolve(r.body); }});
};
var MAC = {available: true, status: "ok", disk: {free_bytes: 35 * 1073741824, total_bytes: 460 * 1073741824, status: "warn"},
  memory: {pressure_free_percent: 42, status: "ok", available_bytes: 1}, cpu: {load: [2.34, 2, 1], cores: 10, status: "ok"},
  processes: {cpu: [], memory: [{kind: "browser", memory_bytes: 3 * 1073741824, cpu_percent: 1}, {kind: "build", memory_bytes: 2147483648, cpu_percent: 1},
                               {kind: "other", memory_bytes: 524288000, cpu_percent: 1}, {kind: "test", memory_bytes: 1, cpu_percent: 1}]}};
function status(extra) {
  return Object.assign({ok: true, now: 1800000000, actions: {dry_run: true, clean: true, maintenance: true}, fresh_seconds: 600,
                        job: null, dry: null, last: null, mac: MAC}, extra || {});
}
var DRY = {id: "abcdef012345", bytes: 48 * 1073741824, items: 380, finished_at: 1800000000, age: 5, fresh: true,
  categories: [{name: "User app cache", bytes: 43 * 1073741824}, {name: "Xcode DerivedData", bytes: 2800000000}, {name: "Clang module cache", bytes: 276000000}]};
"""


def run(body, lang="he", cfg=None):
    tools = panel_page.V2_STRINGS[lang]["tools"]
    strings = {k: v for k, v in panel_page.STRINGS[lang].items() if isinstance(v, str)}
    strings.setdefault("reopen", "reopen")
    code = "\n".join(func(n) for n in ("el", "icon")) + "\nfunction v2Chev(){var c=el('span','v2-chev');return c;}\n" \
        + (SRC / "cockpit_mac_tools.js").read_text()
    src = PRELUDE + (EXTRA % {"rtl": "true" if lang == "he" else "false"}) + (
        "\nsandbox.LANG=%s;sandbox.S=%s;sandbox.V2={tools:%s};sandbox.ICONS={};docEl.dir=%s;\n"
        % (json.dumps(lang), json.dumps(strings, ensure_ascii=False), json.dumps(tools, ensure_ascii=False), json.dumps("rtl" if lang == "he" else "ltr"))
        + "vm.runInNewContext(%s, sandbox);\n" % json.dumps(code)
        + "var CFG = %s;\n" % json.dumps(cfg or {"dry_run": True, "clean": True, "maintenance": True})
        + "(async()=>{%s})().catch(e=>{console.error(e);process.exit(1);});" % body)
    result = subprocess.run([NODE, "-"], input=src, capture_output=True, text=True, timeout=30,
                            env=dict(os.environ, TZ="Asia/Jerusalem"))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")

OPEN = ("sticky('/api/mac-tools-status',200,status(%s));var tile=sandbox.v2MacToolsTile(CFG);root.appendChild(tile);tile.click();"
        "var dlg=doc.body.childNodes[0];await flush();await flush();"
        "function btn(id){return dlg.querySelector('[data-mt=\"'+id+'\"]');}"
        "function txt(id){var n=dlg.querySelector('[data-mt=\"'+id+'\"]');return n?n.textContent:null;}"
        "function posts(path){return posted.filter(p=>p[0]===path);}")


def shown(body, state="", **kw):
    return run(OPEN % (state or "") + body, **kw)


def test_tile_and_sheet_have_the_right_roles():
    got = shown("emit({tile:tile.attrs['data-v2-mac-tools'],popup:tile.attrs['aria-haspopup'],text:tile.textContent,open:dlg.open,"
                "label:dlg.attrs['aria-labelledby'],dir:dlg.dir,section:dlg.attrs['data-v2-section'],"
                "title:dlg.querySelector('.topic-heading').childNodes[0].textContent,first:posted[0]});")
    assert got["tile"] == "1" and got["popup"] == "dialog" and got["open"] is True
    assert got["label"] == "mt-sheet-title" and got["dir"] == "rtl" and got["section"] == "mac-tools"
    assert got["title"] == "כשירות מערכות המחשב" and got["text"].startswith("בדיקת כשירות המחשב")
    assert got["first"] == ["/api/mac-tools-status", {}]


def test_only_configured_buttons_are_shown():
    got = shown("emit({check:!!btn('check'),clean:!!btn('clean'),maint:!!btn('maintenance')});")
    assert got == {"check": True, "clean": True, "maint": True}
    got = run(OPEN % "" + "emit({check:!!btn('check'),clean:!!btn('clean'),maint:!!btn('maintenance')});",
              cfg={"dry_run": False, "clean": False, "maintenance": True})
    assert got == {"check": False, "clean": False, "maint": True}


def test_status_shows_disk_memory_load_and_the_three_biggest_memory_users():
    got = shown("var s=dlg.querySelector('[data-mt=\"status\"]');var rows=s.querySelectorAll('.mac-row').map(r=>r.textContent);emit({rows});")
    rows = got["rows"]
    assert len(rows) == 3 + 3                                    # disk, memory, load + three processes
    assert "35 גיגה־בייט" in rows[0] and "8%" in rows[0]            # 35 of 460
    assert "42%" in rows[1] and "2.3 מתוך 10 ליבות" in rows[2]
    assert rows[3:] == ["דפדפן3 גיגה־בייט", "כלי בנייה2 גיגה־בייט", "תהליך אחר500 מגה־בייט"]


def test_status_unavailable_is_said_plainly():
    got = shown("emit({text:txt('status')});", state="{mac:{available:false}}")
    assert "המידע אינו זמין כרגע" in got["text"]


def test_a_preview_runs_polls_and_shows_the_result():
    got = shown("var before=btn('check').disabled;"
                "reply('/api/mac-tools',200,{ok:true,id:'abcdef012345',kind:'dry-run'});"
                "sticky('/api/mac-tools-status',200,status({job:{id:'abcdef012345',kind:'dry-run',elapsed:75}}));"
                "btn('check').click();await flush();await flush();await tick(1000);"
                "var running={text:txt('running'),checkOff:btn('check').disabled,cleanOff:btn('clean').disabled,maintOff:btn('maintenance').disabled};"
                "sticky('/api/mac-tools-status',200,status({dry:DRY,last:{id:'abcdef012345',kind:'dry-run',state:'ok',finished_at:1800000000,duration:80,bytes:DRY.bytes,items:380,categories:DRY.categories}}));"
                "await tick(3000);"
                "var dry=dlg.querySelector('[data-mt=\"dry\"]');"
                "emit({before,posted:posts('/api/mac-tools'),running,done:!btn('running'),dry:dry.textContent,cats:dry.querySelectorAll('.mac-row').map(r=>r.textContent),"
                "catDir:dry.querySelector('bdi').dir,cleanOn:!btn('clean').disabled,last:txt('last'),haptic:haptics.filter(h=>h[0]==='n')});")
    assert got["before"] is False
    assert got["posted"] == [["/api/mac-tools", {"action": "dry-run"}]]
    assert "בדיקת ניקוי" in got["running"]["text"] and "1:15" in got["running"]["text"]
    assert got["running"]["checkOff"] and got["running"]["cleanOff"] and got["running"]["maintOff"]
    assert got["done"] and "אפשר לפנות כ־48 גיגה־בייט" in got["dry"] and "380 פריטים" in got["dry"]
    assert len(got["cats"]) == 3 and got["cats"][0].startswith("User app cache") and got["catDir"] == "auto"
    assert got["cleanOn"] and "אפשר לפנות" in got["last"]
    assert got["haptic"] == [["n", "success"]]


def test_the_real_cleanup_is_off_without_a_preview_and_says_why():
    got = shown("emit({off:btn('clean').disabled,hint:txt('clean-hint')});")
    assert got["off"] is True and "קודם עושים בדיקת ניקוי" in got["hint"]


def test_the_cleanup_needs_two_taps_and_the_second_names_the_size():
    got = shown("var first=btn('clean');var off0=first.disabled;first.click();"
                "var armed={label:btn('clean').textContent,cancel:!!btn('cancel'),posts:posted.filter(p=>p[0]==='/api/mac-tools').length,cls:btn('clean').className};"
                "reply('/api/mac-tools',200,{ok:true,id:'fedcba543210',kind:'clean'});"
                "btn('clean').click();await flush();await flush();"
                "emit({off0,armed,posts:posts('/api/mac-tools')});",
                state="{dry:DRY}")
    assert got["off0"] is False
    assert got["armed"]["label"] == "לנקות 48 גיגה־בייט?" and got["armed"]["cancel"] is True and got["armed"]["posts"] == 0
    assert "arm" in got["armed"]["cls"]
    assert got["posts"] == [["/api/mac-tools", {"action": "clean", "confirm": "abcdef012345"}]]


def test_the_first_tap_disarms_by_itself_or_with_cancel():
    got = shown("btn('clean').click();var a=btn('clean').textContent;await tick(10000);var b=btn('clean').textContent;"
                "btn('clean').click();btn('cancel').click();var c=btn('clean').textContent;"
                "emit({a,b,c,posts:posts('/api/mac-tools').length,cancelGone:!btn('cancel')});", state="{dry:DRY}")
    assert got["a"] == "לנקות 48 גיגה־בייט?" and got["b"] == "פינוי מקום כעת" and got["c"] == "פינוי מקום כעת"
    assert got["posts"] == 0 and got["cancelGone"] is True


def test_an_old_preview_cannot_start_a_cleanup():
    got = shown("emit({off:btn('clean').disabled,stale:dlg.querySelector('[data-mt=\"dry\"]').className,"
                "hint:txt('clean-hint')});",
                state="{dry:Object.assign({},DRY,{fresh:false,age:900})}")
    assert got["off"] is True and "old" in got["stale"] and "הבדיקה ישנה" in got["hint"]


def test_a_preview_that_ages_while_armed_is_refused_on_the_second_tap():
    got = shown("btn('clean').click();"
                "vm.runInNewContext('var __real=Date.now;Date.now=function(){return __real()+601000;};',sandbox);"
                "btn('clean').click();await flush();"
                "emit({posts:posts('/api/mac-tools').length,note:txt('note'),label:btn('clean').textContent});",
                state="{dry:DRY}")
    assert got["posts"] == 0 and "הבדיקה ישנה" in got["note"]


def test_the_weekly_routine_needs_a_second_tap():
    got = shown("btn('maintenance').click();var armed={label:btn('maintenance').textContent,posts:posts('/api/mac-tools').length};"
                "reply('/api/mac-tools',200,{ok:true,id:'abcdef654321',kind:'maintenance'});"
                "btn('maintenance').click();await flush();await flush();"
                "emit({armed,posts:posts('/api/mac-tools'),running:txt('running')});")
    assert got["armed"] == {"label": "להריץ את התחזוקה השבועית?", "posts": 0}
    assert got["posts"] == [["/api/mac-tools", {"action": "maintenance", "confirm": True}]]
    assert "תחזוקה שבועית" in got["running"]


def test_taps_while_a_job_runs_do_nothing():
    got = shown("btn('check').click();btn('check').click();btn('maintenance').click();btn('maintenance').click();await flush();"
                "emit({posts:posts('/api/mac-tools').length});",
                state="{job:{id:'abcdef012345',kind:'clean',elapsed:3}}")
    assert got["posts"] == 0


def test_double_tap_sends_once():
    got = shown("reply('/api/mac-tools',200,{ok:true,id:'abcdef012345',kind:'dry-run'});"
                "var b=btn('check');b.click();b.click();await flush();emit({posts:posts('/api/mac-tools').length});")
    assert got["posts"] == 1


@pytest.mark.parametrize("status,body,text", [(409, {"error": "busy", "kind": "clean"}, "פעולה אחרת רצה עכשיו"),
                                              (409, {"error": "stale"}, "הבדיקה ישנה"), (409, {"error": "confirm"}, "הבדיקה ישנה"),
                                              (500, {"error": "unavailable"}, "משהו השתבש"), (404, {"error": "not found"}, "משהו השתבש")])
def test_refusals_are_said_in_words_and_nothing_hangs(status, body, text):
    got = shown("reply('/api/mac-tools',%d,%s);btn('check').click();await flush();await flush();"
                "emit({note:txt('note'),running:!!btn('running'),checkOn:!btn('check').disabled});" % (status, json.dumps(body)))
    assert text in got["note"] and got["running"] is False and got["checkOn"] is True


def test_expired_login_asks_to_reopen_and_a_network_failure_is_an_error():
    got = shown("reply('/api/mac-tools',403,{error:'forbidden'});btn('check').click();await flush();await flush();"
                "var a={toasts:toasts.slice(),checkOn:!btn('check').disabled};"
                "reply('/api/mac-tools','reject');btn('check').click();await flush();await flush();"
                "emit({a,note:txt('note'),checkOn:!btn('check').disabled});")
    assert got["a"]["toasts"] == [["ההתחברות לטלגרם פגה. סגרו את הלוח ופתחו אותו מחדש", True]]
    assert got["a"]["checkOn"] is True and "משהו השתבש" in got["note"] and got["checkOn"] is True


def test_closing_stops_the_polling_and_the_timers():
    got = shown("var n=posted.length;dlg.querySelector('.topic-close').click();var closed=!dlg.open;"
                "var live=pending().length;"
                "try{await tick(15000);}catch(e){}"
                "emit({closed,live,after:posted.length-n,sheets:doc.body.childNodes.filter(c=>c.tagName==='dialog').length});")
    assert got["closed"] is True and got["live"] == 0 and got["after"] == 0


def test_one_sheet_at_a_time():
    got = shown("tile.click();tile.click();emit({sheets:doc.body.childNodes.filter(c=>c.tagName==='dialog').length});")
    assert got == {"sheets": 1}


def test_polling_is_fast_while_working_and_slow_when_idle():
    got = shown("var idle=pending().map(t=>t.ms);"
                "sticky('/api/mac-tools-status',200,status({job:{id:'abcdef012345',kind:'maintenance',elapsed:3}}));"
                "await tick(15000);var busy=pending().map(t=>t.ms);emit({idle,busy});")
    assert got["idle"] == [15000] and got["busy"] == [3000]


def last_card(report):
    return ("var c=dlg.querySelector('[data-mt=\"last\"]');emit({text:c.textContent,state:(c.querySelector('.mt-result')||{attrs:{}}).attrs['data-mt-state'],"
            "cls:c.querySelector('.mt-result')?c.querySelector('.mt-result').className:''});")


@pytest.mark.parametrize("report,parts,tone", [
    ("{id:'a'.repeat(12),kind:'clean',state:'ok',finished_at:1800000000,duration:95,bytes:3*1073741824,free_before:10*1073741824,free_after:13*1073741824}",
     ["ניקוי", "התפנו 3 גיגה־בייט", "מקום פנוי: 10 גיגה־בייט ← 13 גיגה־בייט", "1:35"], "ok"),
    ("{id:'b'.repeat(12),kind:'maintenance',state:'ok',finished_at:1800000000,duration:600}", ["תחזוקה שבועית", "הפעולה הושלמה בהצלחה"], "ok"),
    ("{id:'c'.repeat(12),kind:'maintenance',state:'skipped',skipped:'memory',finished_at:1800000000,duration:2}", ["דולג: הזיכרון נמוך"], "wait"),
    ("{id:'d'.repeat(12),kind:'clean',state:'failed',finished_at:1800000000,duration:9}", ["נכשל"], "bad"),
    ("{id:'e'.repeat(12),kind:'dry-run',state:'timeout',finished_at:1800000000,duration:900}", ["נעצר: לקח יותר מדי זמן"], "bad"),
    ("{id:'f'.repeat(12),kind:'dry-run',state:'unreadable',finished_at:1800000000,duration:60}", ["לא הצלחתי לקרוא את התוצאה"], "bad"),
    ("{id:'1'.repeat(12),kind:'dry-run',state:'error',finished_at:1800000000,duration:1}", ["לא הצלחתי להפעיל"], "bad"),
])
def test_the_last_report_says_what_happened(report, parts, tone):
    got = shown(last_card(report), state="{last:%s}" % report)
    for part in parts:
        assert part in got["text"], (part, got["text"])
    assert got["cls"].endswith(tone)


def test_no_report_yet():
    got = shown("emit({text:txt('last')});")
    assert "עוד לא הורצה כאן פעולה" in got["text"]


def test_a_job_that_vanishes_without_a_report_stops_the_waiting():
    got = shown("reply('/api/mac-tools',200,{ok:true,id:'abcdef012345',kind:'dry-run'});btn('check').click();await flush();await flush();"
                "sticky('/api/mac-tools-status',200,status());await tick(1000);emit({running:!!btn('running'),checkOn:!btn('check').disabled});")
    assert got == {"running": False, "checkOn": True}


def test_english_layout_uses_gb_and_ltr():
    got = shown("btn('clean').click();emit({dir:dlg.dir,label:btn('clean').textContent,disk:dlg.querySelectorAll('.mac-row')[0].textContent});",
                state="{dry:DRY}", lang="en")
    assert got["dir"] == "ltr" and got["label"] == "Clean 48 GB?" and "35 GB" in got["disk"]


@pytest.mark.parametrize("lang", ["he", "en"])
def test_strings_cover_everything_the_sheet_says(lang):
    tools = panel_page.V2_STRINGS[lang]["tools"]
    js = (SRC / "cockpit_mac_tools.js").read_text()
    for key in re.findall(r"V2\.tools\.(mt\w+)", js):
        assert tools[key].strip(), key
    for kind in ("Dry", "Clean", "Maint"):
        assert tools["mtKind" + kind].strip()
    for state in ("failed", "timeout", "error", "unreadable"):
        assert tools["mtState_" + state].strip()
    for why in ("busy", "memory", "setup", "running"):
        assert tools["mtSkip_" + why].strip()
    for key in re.findall(r"S\.(mac\w+)", js):
        assert key in panel_page.STRINGS[lang], key
    assert "{size}" in tools["mtCleanAsk"] and "{size}" in tools["mtReclaim"] and "{why}" in tools["mtSkipped"]
    assert {k for k in tools if k.startswith("mt")} == {k for k in panel_page.V2_STRINGS["he" if lang == "en" else "en"]["tools"] if k.startswith("mt")}


def test_hebrew_copy_never_mixes_english_into_a_line_without_an_isolate():
    for key, text in panel_page.V2_STRINGS["he"]["tools"].items():
        if not key.startswith("mt"):
            continue
        for part in text.split("\n"):
            outside = re.sub(r"⁨[^⁩]*⁩|\{\w+\}", "", part)
            assert not re.search("[A-Za-z]", outside), (key, text)


def test_the_page_includes_the_sheet_and_the_tile_comes_after_the_app_request():
    page = panel_page.PAGE
    assert "function mtOpen(" in page and "function v2MacToolsTile(" in page
    assert page.index("function v2AppRequestTile(") < page.index("function v2MacToolsTile(")
    more = func("v2More")
    assert "v2MacToolsTile(d.mac_tools)" in more and "data-v2-app-request" in more
