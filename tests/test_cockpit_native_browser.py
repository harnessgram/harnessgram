"""The cockpit_native switch in a real browser: Telegram's own controls when it is on, nothing new when it is off,
and a clean page in a plain browser. Every request is intercepted; Telegram is a local recording stub."""
import copy
import json

import pytest

pytest.importorskip("playwright")

import panel_browser
from cockpit_v2_browser import open_first_request, open_v2, rich_state, schedule_sample
from ctb import panel_page

sync_api = pytest.importorskip("playwright.sync_api")

NEW_APIS = {"MainButton", "SecondaryButton", "BottomButton", "showPopup", "showConfirm", "showAlert", "CloudStorage",
            "requestFullscreen", "exitFullscreen", "themeParams", "BiometricManager", "shareMessage", "lockOrientation"}

THEME = {"bg_color": "#1c1c1d", "secondary_bg_color": "#000000", "section_bg_color": "#2c2c2e", "text_color": "#ffffff",
         "hint_color": "#98989e", "section_separator_color": "#3d3d3f", "bottom_bar_bg_color": "#1d1d1d",
         "button_color": "#3e88f7", "button_text_color": "#ffffff"}


def native_stub(cloud=None, platform="ios", theme=THEME):
    """A recording Telegram 9.0 with the native APIs: every call lands in window.__nat."""
    return """
;(function(){var w=window.Telegram.WebApp,log=window.__nat=[],ev={};
w.version="9.0";w.platform=%s;w.isFullscreen=false;w.colorScheme="dark";w.themeParams=%s;
w.onEvent=function(n,f){(ev[n]=ev[n]||[]).push(f);};window.__fire=function(n){(ev[n]||[]).forEach(function(f){f();});};
w.requestFullscreen=function(){log.push(["fullscreen"]);w.isFullscreen=true;window.__fire("fullscreenChanged");};
w.setBackgroundColor=function(c){log.push(["bg",c]);};w.setBottomBarColor=function(c){log.push(["bar",c]);};
function bb(name){return {setParams:function(p){log.push([name,"params",p]);},show:function(){},hide:function(){log.push([name,"hide"]);},
 showProgress:function(){log.push([name,"progress"]);},hideProgress:function(){log.push([name,"noprogress"]);},onClick:function(f){window["__"+name]=f;}};}
w.MainButton=bb("main");w.SecondaryButton=bb("second");
w.showPopup=function(p,cb){log.push(["popup",p]);window.__popup=cb;};
var store=window.__cloud=%s;
w.CloudStorage={getItem:function(k,cb){log.push(["cloud-get",k]);setTimeout(function(){cb(null,store[k]||"");},0);},
 setItem:function(k,v,cb){log.push(["cloud-set",k,v]);store[k]=v;if(cb)cb(null,true);}};
w.HapticFeedback={impactOccurred:function(s){log.push(["haptic","tap",s]);},selectionChanged:function(){log.push(["haptic","sel"]);},
 notificationOccurred:function(t){log.push(["haptic","n",t]);}};
})();""" % (json.dumps(platform), json.dumps(theme), json.dumps(cloud or {}))


# Every property the page reads from Telegram.WebApp, so a test can prove the switch's off state touches nothing new.
RECORD_READS = """
;(function(){var w=window.Telegram.WebApp,reads=window.__reads=[];
window.Telegram.WebApp=new Proxy(w,{get:function(t,k){if(typeof k==="string")reads.push(k);return t[k];}});})();"""


@pytest.fixture(scope="module")
def browser():
    panel_browser.prepare_env()
    with sync_api.sync_playwright() as p:
        b = panel_browser.launch(p)
        if b is None:
            pytest.skip("headless Chromium cannot run in this environment")
        yield b
        b.close()


def nat_log(page, kind=None):
    log = page.evaluate("window.__nat")
    return [x for x in log if kind is None or x[0] == kind]


def last_params(page, name="main"):
    rows = [x for x in nat_log(page, name) if x[1] == "params"]
    return rows[-1][2] if rows else None


LAST_PARAMS = "window.__nat.filter(x=>x[0]==='main'&&x[1]==='params').slice(-1)[0]"


def schedule_bridge(lang="he"):
    view, posts = schedule_sample(lang), []

    def reply(path, body):
        if path == "/api/schedule":
            posts.append(body)
        return 200, {"ok": True, "view": view}
    reply.posts = posts
    return reply


@pytest.mark.parametrize("offered", [False, True])
def test_switch_off_reads_no_new_native_api(browser, offered):
    """Off: the native APIs are never read, whether or not this Telegram offers them."""
    stub = (native_stub() if offered else "\n;window.__nat=[];") + RECORD_READS
    ctx, page, calls, errors, state = open_v2(browser, stub_extra=stub, native=False)
    try:
        page.locator(".tabs .tab").nth(1).click()
        page.locator(".tabs .tab").nth(0).click()
        page.wait_for_timeout(300)
        reads = set(page.evaluate("window.__reads"))
        assert not reads & NEW_APIS
        assert page.evaluate("document.documentElement.className").split().count("tg-native") == 0
        assert not [x for x in nat_log(page) if x[0] in ("fullscreen", "popup", "cloud-get", "cloud-set", "main", "second")]
        assert not [x for x in nat_log(page) if x[0] in ("bg", "bar") and not x[1].startswith("#")]  # our own colours only
        assert page.locator("#v2-exit").is_visible()
        assert not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
def test_plain_browser_without_telegram_degrades_cleanly(browser, lang):
    """The switch on, but no Telegram.WebApp at all: no error, no native class, the page's own message."""
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def route(r):
        url = r.request.url
        if url.endswith("telegram-web-app.js"):
            r.fulfill(content_type="application/javascript", body="")
        elif url.rstrip("/").endswith("127.0.0.1"):
            r.fulfill(content_type="text/html", body=panel_page.render_page(lang, "fixture", native=True))
        else:
            r.fulfill(status=404, body="")
    page.route("**/*", route)
    try:
        page.goto("http://127.0.0.1/")
        page.wait_for_function("document.getElementById('app').textContent.length>0&&!document.querySelector('#app .sk')")
        assert errors == []
        assert "tg-native" not in page.evaluate("document.documentElement.className")
        assert panel_page.STRINGS[lang]["open"] in page.locator("#app").inner_text()
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
def test_theme_follows_telegram_and_reacts_to_theme_changes(browser, lang):
    ctx, page, calls, errors, state = open_v2(browser, lang, stub_extra=native_stub(), native=True)
    try:
        root = "getComputedStyle(document.documentElement).getPropertyValue(%s).trim()"
        assert page.evaluate(root % "'--bg'") == "#000000"
        assert page.evaluate(root % "'--v2-page'") == "#000000"
        assert page.evaluate(root % "'--v2-ink'") == "#ffffff"
        assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "dark"
        assert ["bg", "secondary_bg_color"] in nat_log(page) and ["bar", "bottom_bar_bg_color"] in nat_log(page)
        # the brand stays: the answer colours are ours, not Telegram's
        assert page.evaluate(root % "'--v2-blue'") == "#0069fa"
        page.evaluate("""()=>{var w=window.Telegram.WebApp;w.colorScheme="light";
          w.themeParams={bg_color:"#ffffff",secondary_bg_color:"#efeff4",text_color:"#000000",hint_color:"#8e8e93"};window.__fire("themeChanged");}""")
        assert page.evaluate(root % "'--bg'") == "#efeff4"
        assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "light"
        # a key Telegram stopped sending gives the token back to our own value
        assert page.evaluate(root % "'--card'") != "#2c2c2e"
        assert not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("platform,full", [("ios", True), ("android", True), ("tdesktop", False), ("weba", False)])
def test_fullscreen_on_phones_only_with_safe_area_padding(browser, platform, full):
    ctx, page, calls, errors, state = open_v2(browser, stub_extra=native_stub(platform=platform) + """
;document.documentElement.style.setProperty("--tg-safe-area-inset-top","47px");
document.documentElement.style.setProperty("--tg-content-safe-area-inset-top","46px");""", native=True)
    try:
        assert bool(nat_log(page, "fullscreen")) is full
        assert ("tg-full" in page.evaluate("document.documentElement.className")) is full
        top = page.locator("header.top")
        pad = page.evaluate("parseFloat(getComputedStyle(document.querySelector('header.top')).paddingBlockStart)")
        assert pad == (14 + 47 + 46 if full else 14)
        # fullscreen: the header art runs under the status bar; otherwise the page keeps its own top inset
        assert top.bounding_box()["y"] == (0 if full else 47)
        assert not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("width", [390, 768, 1280])
@pytest.mark.parametrize("lang", ["he", "en"])
def test_no_overflow_at_phone_tablet_and_desktop_widths(browser, width, lang):
    ctx, page, calls, errors, state = open_v2(browser, lang, width=width, stub_extra=native_stub(), native=True)
    try:
        for tab in range(page.locator(".tabs .tab").count()):
            page.locator(".tabs .tab").nth(tab).click()
            page.wait_for_timeout(150)
            assert page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 0
        assert not errors
    finally:
        ctx.close()


def test_back_button_replaces_the_page_back_arrow_and_exit(browser):
    ctx, page, calls, errors, state = open_v2(browser, stub_extra=native_stub(), native=True)
    try:
        assert "tg-back" in page.evaluate("document.documentElement.className")
        assert page.locator("#v2-exit").count() == 1 and not page.locator("#v2-exit").is_visible()
        page.locator(".tabs .tab").nth(1).click()
        assert ["back", "show"] in page.evaluate("window.__log")
        page.evaluate("window.__backClick()")
        page.wait_for_timeout(200)
        assert ["back", "hide"] in page.evaluate("window.__log")
        assert not errors
    finally:
        ctx.close()


def test_one_haptic_per_tap_through_the_one_helper(browser):
    ctx, page, calls, errors, state = open_v2(browser, stub_extra=native_stub(), native=True)
    try:
        page.evaluate("window.__nat.length=0")
        page.locator(".tabs .tab").nth(1).click()
        page.wait_for_timeout(50)
        assert nat_log(page, "haptic") == [["haptic", "sel"]]  # the tab's own selection haptic, not doubled
        page.evaluate("window.__nat.length=0")
        page.wait_for_timeout(200)
        page.evaluate("""()=>{var b=document.createElement('button');b.id='plain';b.type='button';b.textContent='x';
          document.getElementById('app').appendChild(b);}""")
        page.locator("#plain").click()
        assert nat_log(page, "haptic") == [["haptic", "tap", "light"]]  # a tap the screen did not answer itself
        assert not errors
    finally:
        ctx.close()


def test_primary_action_moves_to_the_bottom_button_with_disabled_and_progress(browser):
    ctx, page, calls, errors, state = open_v2(browser, stub_extra=native_stub(), native=True)
    try:
        page.evaluate("""()=>{var b=document.createElement('button');b.type='button';b.id='prim';b.textContent='Save it';
          b.disabled=true;b.setAttribute('data-native-main','');b.addEventListener('click',function(){b.disabled=true;window.__pressed=1;});
          document.getElementById('app').appendChild(b);}""")
        page.wait_for_function("window.__nat.some(x=>x[0]==='main'&&x[1]==='params')")
        assert last_params(page) == {"text": "Save it", "is_visible": True, "is_active": False}
        assert not page.locator("#prim").is_visible()  # the page's own button gives way
        page.evaluate("document.getElementById('prim').disabled=false")
        page.wait_for_function(LAST_PARAMS + "[2].is_active===true")
        page.evaluate("window.__main()")
        page.wait_for_function("window.__nat.filter(x=>x[0]==='main').slice(-1)[0][1]==='progress'")
        assert page.evaluate("window.__pressed") == 1
        page.evaluate("document.getElementById('prim').disabled=false")
        page.wait_for_function("window.__nat.filter(x=>x[0]==='main').slice(-1)[0][1]==='noprogress'")
        # two candidates: neither moves, both stay on the page
        page.evaluate("""()=>{var b=document.createElement('button');b.type='button';b.id='prim2';b.textContent='Other';
          b.setAttribute('data-native-main','');document.getElementById('app').appendChild(b);}""")
        page.wait_for_function("window.__nat.filter(x=>x[0]==='main').slice(-1)[0][1]==='hide'")
        assert page.locator("#prim").is_visible() and page.locator("#prim2").is_visible()
        assert not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
def test_schedule_sheet_save_is_the_bottom_button(browser, lang):
    bridge = schedule_bridge(lang)
    ctx, page, calls, errors, state = open_v2(browser, lang, state=copy.deepcopy(rich_state(lang)), schedule=bridge,
                                              stub_extra=native_stub(), native=True)
    try:
        page.locator("[data-sc=add]").click()
        page.locator("dialog[open] [data-sc=field]").wait_for()
        save = page.locator("dialog[open] [data-sc=save]")
        page.wait_for_function("window.__nat.some(x=>x[0]==='main'&&x[1]==='params')")
        assert not save.is_visible()
        label = save.inner_text().strip() or save.evaluate("b=>b.textContent.trim()")
        assert last_params(page) == {"text": label, "is_visible": True, "is_active": False}
        page.locator("dialog[open] [data-sc=field]").fill("milk" if lang == "en" else "חלב")
        page.wait_for_function(LAST_PARAMS + "[2].is_active===true")
        page.evaluate("window.__main()")
        page.wait_for_function("document.querySelectorAll('dialog[open]').length===0")
        assert bridge.posts == [{"action": "add", "text": "milk" if lang == "en" else "חלב"}]
        page.wait_for_function("window.__nat.filter(x=>x[0]==='main').slice(-1)[0][1]==='hide'")
        assert not errors
    finally:
        ctx.close()


def test_native_mode_keeps_request_actions_behind_the_in_page_second_tap(browser):
    ctx, page, calls, errors, state = open_v2(browser, stub_extra=native_stub(), native=True)
    try:
        row = open_first_request(page)
        action = row.locator(".request-actions button").nth(1)
        action.click()
        assert not nat_log(page, "popup")
        assert not [c for c in calls if c[0] == "/api/request"]
        action.click()
        page.wait_for_timeout(400)
        sent = [c for c in calls if c[0] == "/api/request"]
        assert len(sent) == 1
        assert sent[0][1] == {"project": "לימוד", "id": 2, "action": "drop"}
        assert not errors
    finally:
        ctx.close()


def test_cloud_storage_restores_last_tab_and_saves_changes(browser):
    cloud = {"cockpit_prefs": json.dumps({"tab": "settings", "blocks": {}})}
    ctx, page, calls, errors, state = open_v2(browser, stub_extra=native_stub(cloud=cloud), native=True, ready=".tabs .tab.on")
    try:
        assert page.evaluate("document.querySelector('.tabs .tab.on').getAttribute('data-v2-tab')") == "settings"
        assert nat_log(page, "cloud-get") == [["cloud-get", "cockpit_prefs"]]
        page.locator('.tabs .tab[data-v2-tab="me"]').click()
        page.wait_for_function("window.__nat.some(x=>x[0]==='cloud-set')", timeout=3000)
        saved = json.loads(nat_log(page, "cloud-set")[-1][2])
        assert saved["tab"] == "me" and len(nat_log(page, "cloud-set")[-1][2]) <= 4096
        assert not errors
    finally:
        ctx.close()


def test_broken_cloud_value_falls_back_to_the_page_default(browser):
    cloud = {"cockpit_prefs": "{not json"}
    ctx, page, calls, errors, state = open_v2(browser, stub_extra=native_stub(cloud=cloud), native=True)
    try:
        assert page.locator(".v2-needs").count()
        assert not errors
    finally:
        ctx.close()
