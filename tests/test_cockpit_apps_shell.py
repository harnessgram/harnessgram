"""What a Cockpit app may ask of the page around it: fold the header and tabs (focus), take over the native back
button, show a count on its tab, open the topic's chat, and learn the bottom safe-area inset. The messages are
validated (exact shapes), obeyed only from our own app frames (the visible one for anything that changes the page),
and the shell always keeps an exit.

These tests need no browser: the page's own functions are run in node against stubs (they skip only when node is
missing, and the source checks below always run). The same behaviour in a real browser is in
test_cockpit_apps_shell_browser.py."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ctb import panel_page
from test_panel_page import func, js

NODE = shutil.which("node")
DOC = (Path(__file__).resolve().parent.parent / "docs" / "panel.md").read_text(encoding="utf-8")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")

FUNCS = ("appOf", "activeApp", "uiOf", "frameName", "appCmd", "resetApp", "leaveApp", "freshFrame", "frameLoaded",
         "getAppAuth", "forgetAppAuth", "appAuthError", "clearAppErr", "appFailed",
         "appsOf", "appNamed", "appTitle", "showApp",
         "authFailed", "onAppMessage", "backTap", "appBackLayer", "pushBack", "backDepth", "retireBack",
         "bindBackLayer", "travelBack", "restoreBack", "trimBack", "currentBackLayer")

PRELUDE = r"""
var out = [], timers = [], clock = 100000, view = "app:sample", last = {sessions: []}, wa = {initData: "INIT"}, LANG = "he", RTL = true;
var appFrames = {}, appUi = {}, backWait = {}, helloAt = {}, layoutSent = {};
var appAuth = {sample: {token: "sample-token", expires: 1000}, other: {token: "other-token", expires: 1000}}, delivered = [];
var backEntries = [{active:true,undo:function(){switchTo(home());},redo:function(){switchTo("app:sample");}}];
var backIndex=1,backPending=false,backReplay=false,backHistory=false,backLayers={},backTrim=null,backSwitch=null;
var APP_BODY = 16384, APP_BADGE_MAX = 999, BACK_WAIT = 1200, HELLO_SAME_PAGE = 2000;
var APP_NAME=/^[a-z0-9-]{1,24}$/, ICONS={box:"box"};
function mkWin(name) { return {postMessage: function (m) { out.push(["post", name, m.cockpit, m.focus]); delivered.push([name,m]); }}; }
var winSample = mkWin("sample"), winOther = mkWin("other"), winForeign = mkWin("foreign");
appFrames.sample = {contentWindow: winSample, hidden: false};
appFrames.other = {contentWindow: winOther, hidden: true};
var boxes = {};
var document = {getElementById: function (id) { return boxes[id] || (boxes[id] = {hidden: false}); },
  documentElement: {getAttribute: function () { return ""; }}};
var Date = {now: function () { return clock; }};
function setTimeout(f, ms) { timers.push({f: f, at: clock + ms, on: true}); return timers.length; }
function clearTimeout(id) { if (id && timers[id - 1]) timers[id - 1].on = false; }
function tick(ms) { clock += ms; timers.forEach(function (t) { if (t.on && t.at <= clock) { t.on = false; t.f(); } }); }
function applyChrome() { var n = activeApp(); out.push(["chrome", n && uiOf(n).focus]); }
function syncBack() {}
function adoptBackLayers() {}
function syncClose() {}
function radioPlayerStop() { out.push(["player-stop"]); }
function tabs() { out.push(["tabs"]); }
function openChat() { out.push(["chat"]); }
function appLayout(n) { return {focus: !!uiOf(n).focus, inset: {bottom: 0}}; }
function toApp(n, msg) { out.push(["to", n, msg.cockpit]); delivered.push([n,msg]); }
function switchTo(v) { view = v; out.push(["switch", v]); }
function home() { return "me"; }
function haptic() {}
function fail() { out.push(["fail"]); }
var S = {reopen: "x", offline: "o", retry: "r"};
var appErr = null;
function empty() { var c = {className: "card empty", attrs: {}, setAttribute: function (k, v) { this.attrs[k] = v; }, getAttribute: function (k) { return this.attrs[k]; }, appendChild: function () {}}; return c; }
function el() { return {type: "", addEventListener: function () {}}; }
var window = {addEventListener: function () {}};
function pushLayout() {}
function msg(win, data, origin) { onAppMessage({source: win, origin: origin === undefined ? "null" : origin, data: data}); }
"""


def scenario(body: str):
    """Run the page's own functions in node and return what the stubs recorded (`out`)."""
    src = PRELUDE + "\n".join(func(n) for n in FUNCS) + "\n(async function () {\n" + body + \
        "\nawait new Promise(function(r){setImmediate(r);});\nprocess.stdout.write(JSON.stringify({out: out, ui: appUi, view: view, boxes: boxes, delivered: delivered}));\n})();"
    done = subprocess.run([NODE, "-e", src], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def effects(got, kind):
    return [e for e in got["out"] if e[0] == kind]


# --- the message protocol ---------------------------------------------------------------------------

def run_cmd(messages):
    src = func("appCmd") + "\nvar APP_BADGE_MAX=999;\nprocess.stdout.write(JSON.stringify(%s.map(appCmd)));" % json.dumps(messages)
    return json.loads(subprocess.run([NODE, "-e", src], capture_output=True, text=True, check=True).stdout)


@needs_node
def test_valid_messages_become_fixed_commands():
    got = run_cmd([{"cockpit": "focus", "on": True}, {"cockpit": "focus", "on": False}, {"cockpit": "back", "on": True},
                   {"cockpit": "badge", "n": 0}, {"cockpit": "badge", "n": 999}, {"cockpit": "open_chat"},
                   {"cockpit": "back_done"}])
    assert got == [{"t": "focus", "on": True}, {"t": "focus", "on": False}, {"t": "back", "on": True},
                   {"t": "badge", "n": 0}, {"t": "badge", "n": 999}, {"t": "open_chat"}, {"t": "back_done"}]


@needs_node
def test_malformed_unknown_or_overfull_messages_are_rejected():
    bad = [None, 5, "focus", [], {}, {"cockpit": "focus"}, {"cockpit": "focus", "on": "true"}, {"cockpit": "focus", "on": 1},
           {"cockpit": "back", "on": None}, {"cockpit": "badge", "n": -1}, {"cockpit": "badge", "n": 1000},
           {"cockpit": "badge", "n": 1.5}, {"cockpit": "badge", "n": "3"}, {"cockpit": "badge", "n": None},
           {"cockpit": "badge"}, {"cockpit": "navigate", "url": "https://example.com"}, {"cockpit": "html", "html": "<b>x</b>"},
           {"cockpit": ["focus"], "on": True}, {"type": "focus", "on": True},
           # a valid message with any extra field is rejected whole, never trimmed
           {"cockpit": "open_chat", "url": "https://example.com"}, {"cockpit": "focus", "on": True, "css": "x"},
           {"cockpit": "badge", "n": 1, "html": "<i>x</i>"}, {"cockpit": "back_done", "x": 1}, {"cockpit": "back", "on": True, "id": 1}]
    assert run_cmd(bad) == [None] * len(bad)


# --- who is obeyed ---------------------------------------------------------------------------------

@needs_node
def test_only_our_own_frames_with_an_opaque_origin_are_obeyed():
    got = scenario('''
        msg(winSample, {cockpit: "focus", on: true}, "https://example.com");   // right window, wrong origin
        msg(winSample, {cockpit: "focus", on: true}, "http://127.0.0.1:1");
        msg(winForeign, {cockpit: "focus", on: true});                          // a window that is not an app frame
        msg(winForeign, {cockpit: "badge", n: 5});
        msg(winSample, {cockpit: "focus", on: true});                           // the real thing
    ''')
    assert effects(got, "chrome") == [["chrome", True]] and got["ui"]["sample"]["focus"] is True
    assert "foreign" not in got["ui"] and got["ui"]["sample"]["badge"] == 0


@needs_node
def test_a_hidden_app_may_only_set_its_tab_count():
    got = scenario('''
        view = "app:other";                                                      // another app is showing
        msg(winSample, {cockpit: "focus", on: true});
        msg(winSample, {cockpit: "back", on: true});
        msg(winSample, {cockpit: "open_chat"});
        msg(winSample, {cockpit: "back_done"});
        msg(winSample, {cockpit: "badge", n: 3});
        view = "me";                                                             // a built-in tab is showing
        msg(winSample, {cockpit: "focus", on: true});
    ''')
    assert got["ui"]["sample"] == {"focus": False, "back": False, "badge": 3}
    assert effects(got, "chat") == [] and effects(got, "chrome") == [] and effects(got, "tabs") == [["tabs"]]


@needs_node
def test_the_visible_app_may_take_over_and_open_the_chat():
    got = scenario('''
        msg(winSample, {cockpit: "back", on: true});
        msg(winSample, {cockpit: "open_chat"});
    ''')
    assert got["ui"]["sample"]["back"] is True and effects(got, "chat") == [["chat"]]


# --- the shell always owns an exit -----------------------------------------------------------------

@needs_node
def test_an_unanswered_back_is_followed_by_the_shell_leaving_and_the_takeover_is_dropped():
    got = scenario('''
        msg(winSample, {cockpit: "focus", on: true}); msg(winSample, {cockpit: "back", on: true});
        backTap();                       // forwarded to the app
        tick(1199);                      // still waiting
        out.push(["mid", view]);
        tick(2);                         // no answer in time
    ''')
    assert ["to", "sample", "back"] in got["out"] and ["mid", "app:sample"] in got["out"]
    assert got["view"] == "me" and got["ui"]["sample"] == {"focus": False, "back": False, "badge": 0}


@needs_node
def test_a_second_back_press_leaves_at_once():
    got = scenario('''
        msg(winSample, {cockpit: "focus", on: true}); msg(winSample, {cockpit: "back", on: true});
        backTap(); backTap();
    ''')
    assert got["view"] == "me" and len(effects(got, "to")) == 1 and got["ui"]["sample"]["focus"] is False


@needs_node
def test_a_confirmed_back_keeps_the_app_open_and_a_plain_back_leaves():
    got = scenario('''
        msg(winSample, {cockpit: "back", on: true});
        backTap(); msg(winSample, {cockpit: "back_done"}); tick(5000);
        out.push(["after-confirm", view]);
        backTap(); msg(winSample, {cockpit: "back_done"}); tick(5000);   // a later back is forwarded again
        out.push(["second", view, effects_count()]);
        msg(winSample, {cockpit: "back", on: false});
        backTap();                       // no takeover: the shell leaves
        function effects_count() { return out.filter(function (e) { return e[0] === "to"; }).length; }
    ''')
    assert ["after-confirm", "app:sample"] in got["out"] and ["second", "app:sample", 2] in got["out"]
    assert got["view"] == "me"


@needs_node
def test_a_hello_or_a_page_load_starts_the_app_afresh():
    got = scenario('''
        msg(winSample, {cockpit: "focus", on: true}); msg(winSample, {cockpit: "back", on: true}); msg(winSample, {cockpit: "badge", n: 4});
        msg(winSample, {cockpit: "hello"});         // the page was reloaded and says hello again
        out.push(["after-hello", JSON.stringify(appUi.sample)]);
        msg(winSample, {cockpit: "focus", on: true}); msg(winSample, {cockpit: "back", on: true});
        clock += 10000; frameLoaded("sample");      // a navigation inside the frame, no hello
        out.push(["after-load", JSON.stringify(appUi.sample)]);
        msg(winSample, {cockpit: "hello"});
        msg(winSample, {cockpit: "focus", on: true});
        clock += 500; frameLoaded("sample");        // the load event of the page that has just said hello
        out.push(["same-page", JSON.stringify(appUi.sample)]);
    ''')
    off = json.dumps({"focus": False, "back": False, "badge": 0}, separators=(",", ":"))
    assert ["after-hello", off] in got["out"] and ["after-load", off] in got["out"]
    assert ["same-page", json.dumps({"focus": True, "back": False, "badge": 0}, separators=(",", ":"))] in got["out"]
    assert ["post", "sample", "hello", False] in got["out"]  # the hello answer says the header is not folded


@needs_node
def test_an_auth_error_leaves_the_app_and_unfolds_everything_before_the_error_shows():
    got = scenario('''
        msg(winSample, {cockpit: "focus", on: true}); msg(winSample, {cockpit: "back", on: true}); backTap();
        var calls = 0;
        var fetch = function () { return Promise.resolve({status: 403, json: function () { return Promise.resolve(null); }}); };
        globalThis.fetch = fetch;
        msg(winSample, {cockpit: "api", id: 1, body: {}});
        await new Promise(function (r) { setImmediate(r); });
        out.push(["order", out.map(function (e) { return e[0]; }).slice(-3)]);
    ''')
    assert got["view"] == "me" and got["boxes"]["apps"]["hidden"] is True
    assert got["ui"]["sample"]["focus"] is False and got["ui"]["sample"]["back"] is False
    assert ["post", "sample", "api", None] in got["out"] or any(e[:3] == ["post", "sample", "api"] for e in got["out"])
    kinds = [e[0] for e in got["out"]]
    assert kinds.index("fail") > max(i for i, k in enumerate(kinds) if k == "chrome")  # unfolded first, then the error


@needs_node
def test_hello_and_api_use_only_the_sending_apps_token():
    got = scenario('''
        globalThis.fetch = function (path, options) {
          out.push(["fetch", path, options]);
          return Promise.resolve({status: 200, json: function () { return Promise.resolve({ok: true}); }});
        };
        msg(winSample, {cockpit: "hello"});
        msg(winOther, {cockpit: "hello"});
        msg(winSample, {cockpit: "api", id: 1, body: {app: "other"}});
    ''')
    hellos = {name: m for name, m in got["delivered"] if m["cockpit"] == "hello"}
    assert hellos["sample"]["token"] == "sample-token" and hellos["other"]["token"] == "other-token"
    assert "INIT" not in json.dumps(got["delivered"]) and "initData" not in json.dumps(got["delivered"])
    fetched = effects(got, "fetch")
    assert len(fetched) == 1 and fetched[0][1] == "/app/sample/api"
    assert fetched[0][2]["headers"] == {"X-Cockpit-App-Token": "sample-token", "Content-Type": "application/json"}


@needs_node
def test_the_parent_mints_and_refreshes_before_expiry_without_reloading_the_frame():
    got = scenario('''
        appAuth = {}; var calls=0;
        globalThis.fetch = function(path, options) {
          out.push(["mint", path, options]);
          return Promise.resolve({status: 200, json: function() {
            return Promise.resolve({token: "scoped-"+(++calls), expires: clock/1000+600});
          }});
        };
        await Promise.all([getAppAuth("sample"), getAppAuth("sample")]);
        tick(480000);
        await new Promise(function(r){setImmediate(r);});
        msg(winSample, {cockpit:"hello"});
        await new Promise(function(r){setImmediate(r);});
        out.push(["frame", appFrames.sample.contentWindow === winSample]);
    ''')
    minted = effects(got, "mint")
    assert len(minted) == 2  # concurrent requests share one mint; one automatic refresh
    assert all(m[1] == "/api/app-token" and json.loads(m[2]["body"]) == {"app": "sample"} for m in minted)
    assert all(m[2]["headers"]["X-Telegram-Init-Data"] == "INIT" for m in minted)
    refreshed = [m for name, m in got["delivered"] if m["cockpit"] == "auth"]
    assert refreshed == [{"cockpit": "auth", "token": "scoped-2", "expires": 1180}]
    assert "INIT" not in json.dumps(got["delivered"])
    assert effects(got, "frame") == [["frame", True]]


@needs_node
def test_removing_auth_discards_pending_mints_and_cancels_refresh():
    got = scenario('''
        appAuth = {}; var resolve;
        globalThis.fetch = function() { return new Promise(function(r){resolve=r;}); };
        var request=getAppAuth("sample");
        forgetAppAuth("sample");
        resolve({status:200, json:function(){return Promise.resolve({token:"late", expires:700});}});
        try {await request;} catch(e) {out.push(["retired",true]);}
        tick(600000);
    ''')
    assert effects(got, "retired") == [["retired", True]] and not got["delivered"]


@needs_node
def test_frame_url_and_hello_never_contain_the_full_owner_login():
    got = scenario('''
        appFrames={}; appAuth={};
        var box=document.getElementById("apps");
        box.setAttribute=function(){};
        box.appendChild=function(f){f.parentNode=box;out.push(["url",f.src]);};
        document.createElement=function(){return {contentWindow:winSample,setAttribute:function(){},addEventListener:function(){}};};
        globalThis.fetch=function(){return Promise.resolve({status:200,json:function(){return Promise.resolve({token:"scoped-url-token",expires:700});}});};
        last={apps:[{name:"sample",title:"Sample",icon:"box"}]};
        showApp(last);
        await new Promise(function(r){setImmediate(r);});
        msg(winSample,{cockpit:"hello"});
    ''')
    assert effects(got, "url") == [["url", "/app/sample/?auth=scoped-url-token"]]
    assert got["delivered"][0][1]["token"] == "scoped-url-token"
    assert "INIT" not in json.dumps(got) and "initData" not in json.dumps(got)


@needs_node
def test_a_transient_refresh_failure_retries_without_reloading_the_app():
    got = scenario('''
        appAuth={};var count=0;
        globalThis.fetch=function(){
          ++count;if(count===2)return Promise.reject(new Error("offline"));
          return Promise.resolve({status:200,json:function(){return Promise.resolve({token:"scoped-"+count,expires:clock/1000+600});}});
        };
        await getAppAuth("sample");tick(480000);
        await new Promise(function(r){setImmediate(r);});
        tick(5000);await new Promise(function(r){setImmediate(r);});
        out.push(["calls",count]);
    ''')
    assert effects(got, "calls") == [["calls", 3]]
    assert [m for name, m in got["delivered"] if m["cockpit"] == "auth"] == [
        {"cockpit": "auth", "token": "scoped-3", "expires": 1185}]
    assert not effects(got, "fail")


@needs_node
def test_a_frame_reloaded_after_its_original_url_expires_uses_the_parents_current_token():
    got = scenario('''
        appFrames.sample.appTokenExpires=90;
        appFrames.sample.src="/app/sample/?auth=expired-token";
        frameLoaded("sample");
        await new Promise(function(r){setImmediate(r);});
        out.push(["url", appFrames.sample.src]);
    ''')
    assert effects(got, "url") == [["url", "/app/sample/?auth=sample-token"]]
    assert not got["delivered"]  # only an actual navigation reloads the page


# --- source checks (always run) --------------------------------------------------------------------

def handler() -> str:
    code = js()
    return code[code.index("function onAppMessage("):code.index('window.addEventListener("message",onAppMessage);')]


def test_commands_come_only_from_our_own_visible_app_frame():
    h = handler()
    assert h.index("frameName(e.source)") < h.index("appCmd(m)") and h.index('e.origin!=="null"') < h.index("appCmd(m)")
    assert h.index('c.t==="badge"') < h.index("if(name!==activeApp())return;") < h.index('c.t==="focus"')
    assert 'c.t==="back"' in h and 'c.t==="open_chat"' in h
    assert "innerHTML" not in js() and "textContent=m." not in h  # nothing from a message is drawn as markup


def test_the_exit_rules_are_wired():
    code = js()
    assert 'f.addEventListener("load",function(){frameLoaded(name);})' in func("showApp")
    assert "freshFrame(name)" in handler() and "authFailed();fail(S.reopen)" in handler()
    assert "authFailed();fail(S.reopen);status(\"\")" in func("load")
    assert "BACK_WAIT=1200" in code and "leaveApp(n)" in func("backTap")
    assert "delete appUi[k]" in func("showApp") and "resetApp" in func("leaveApp")


def test_the_docs_state_the_same_rules_as_the_code():
    for text in ("`{\"cockpit\": \"back_done\"}`", "one of the app's own frames", "exactly its fields",
                 "second back press", "starts unfolded"):
        assert text in DOC, text
    assert "is the one message taken from a hidden app too" in DOC


def test_the_frame_is_framed_for_a_player_and_drops_its_card():
    page = panel_page.PAGE
    assert 'f.setAttribute("allow","fullscreen; autoplay")' in page
    css = page[page.index(".appframe{"):page.index("#apps[hidden]")]
    assert "background:transparent" in css and "box-shadow" not in css
    assert "film" in panel_page.ICON_NAMES


def test_the_new_page_code_uses_logical_css_only():
    page = panel_page.PAGE
    block = page[page.index("/* focus mode:"):page.index(".tab .tb{")]
    assert not re.search(r"(?<![a-z-])(left|right)\b|margin-(left|right)|padding-(left|right)|border-(left|right)", block)
    assert "padding-block-end" in block and "padding-inline:0" in block


def test_focus_hides_only_the_chrome_and_is_restored_by_the_pages_own_state():
    code = js()
    chrome = func("applyChrome")
    for hidden in ('"tabs"', '"ptr"', 'header.top', '"foot"'):
        assert hidden in chrome
    assert 'classList.toggle("focus",on)' in chrome and "activeApp()" in chrome
    assert "applyChrome();" in func("render")  # every redraw (a tab switch included) recomputes it from the view


def test_open_chat_opens_only_a_telegram_link_of_this_topics_own_session():
    chat = func("openChat")
    assert "s.here" in chat and "t\\.me" in chat and "openLink(me.url)" in chat


def test_the_hello_answer_carries_the_layout_and_the_probe_exists():
    assert "focus:l.focus,inset:l.inset" in handler()
    assert 'id="probe"' in panel_page.PAGE and "safe-area-inset-bottom" in panel_page.PAGE[panel_page.PAGE.index("#probe{"):]


def test_the_panel_page_keeps_its_core_elements():
    page = panel_page.PAGE
    for keep in ('<header class="top">', 'id="rf"', 'id="ptr"', 'id="tabs"', 'id="app"', 'id="apps" hidden', 'id="foot"', 'id="toast"'):
        assert keep in page, keep
    assert 'f.setAttribute("sandbox","allow-scripts allow-forms")' in page and "allow-same-origin" not in page
    assert panel_page.csp("N", "https://web.telegram.org").count("frame-src 'self'") == 1
