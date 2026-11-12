"""The Studio's home screen and its states in a real browser, inside the real panel (the app's own page, the panel's
frame and its messages), with the must-keep contract checked for every state. Skipped as a whole when Playwright or a
Chromium build is missing."""
import html
import json
import re
from pathlib import Path

import pytest

from ctb import cockpit_apps
from ctb.studio import page as studio_page
import panel_browser
import studio_browser as sb

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
KEEP = json.loads((Path(__file__).parent / "studio_must_keep.json").read_text(encoding="utf-8"))
HEBREW = re.compile(r"[֐-׿]")


@pytest.fixture(scope="module")
def browser():
    panel_browser.prepare_env()
    with sync_api.sync_playwright() as p:
        b = panel_browser.launch(p)
        if b is None:
            pytest.skip("no headless browser is installed")
        yield b
        b.close()


@pytest.fixture
def make(browser):
    opened = []

    def open_(lang="he", scheme="light", height=844, **scenario):
        srv = sb.start_panel(lang)
        holder = {}

        def setup(page):
            if scenario is not None and ("status" in scenario or "hang" in scenario or "abort" in scenario or "body" in scenario):
                holder["sc"] = sb.scenario(page, **scenario)

        ctx, page, fr = sb.open_studio(browser, srv, lang=lang, scheme=scheme, height=height, setup=setup)
        opened.append((ctx, srv))
        return page, fr, holder.get("sc")

    yield open_
    for ctx, srv in opened:
        ctx.close()
        sb.stop_panel(srv)


def desk_ok(**kw):
    return dict(zip(("status", "body"), sb.ok(sb.sample_desk(**kw))))


def visible(fr, key):
    loc = fr.locator('[data-keep="%s"]' % key)
    return loc.count() > 0 and loc.first.is_visible()


def wait_keep(fr, key):
    fr.wait_for_selector('[data-keep="%s"]' % key, state="visible")


def settle(page, ms=200):
    page.wait_for_timeout(ms)


def tab_text(page):
    return page.inner_text(".tab.app")


# --- every state keeps its must-keep elements ------------------------------------------------------------------------

STATES = {
    "home": desk_ok(),
    "empty-home": desk_ok(waiting=[], working=[], shelf=[]),
    "loading-home": {"hang": True},
    "error-offline": {"abort": True},
    "bridge-off": {"status": 503, "body": {"ok": False, "error": "mirroring is off"}},
    "error-desk": {"status": 502, "body": {"error": "app failed"}},
    "no-permission": {"status": 200, "body": {"ok": False, "error": "no_permission"}},
}


@pytest.mark.parametrize("state", sorted(STATES))
@pytest.mark.parametrize("lang,scheme", [("he", "light"), ("he", "dark"), ("en", "light")])
def test_every_state_keeps_its_core_elements_and_fits_the_phone(make, state, lang, scheme):
    page, fr, _ = make(lang=lang, scheme=scheme, **STATES[state])
    for key in KEEP[state]:
        wait_keep(fr, key)  # a loaded machine may draw late: wait for each key, then assert
    settle(page)
    for key in KEEP[state]:
        assert visible(fr, key), (state, key)
    assert page.is_visible(".tab.app")  # the Cockpit's own Studio tab stays (the home keeps the panel's chrome)
    assert fr.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert fr.evaluate("document.documentElement.dir") == ("rtl" if lang == "he" else "ltr")
    assert fr.evaluate("document.documentElement.lang") == lang
    if scheme == "dark":
        assert fr.evaluate("document.documentElement.getAttribute('data-theme')") == "dark"
    text = fr.inner_text("body")
    assert not (HEBREW.search(text) if lang == "en" else re.search(r"[A-Za-z]", text)), text  # never one line in both
    for b in fr.locator("button:visible").all():  # touch targets
        box = b.bounding_box()
        assert box["height"] >= 43.5 and box["width"] >= 27.5, (state, b.inner_text(), box)


def test_the_loading_state_has_no_digits_or_actions_and_says_it_is_busy(make):
    page, fr, _ = make(hang=True)
    wait_keep(fr, "loading-line")
    assert fr.evaluate("document.getElementById('app').getAttribute('aria-busy')") == "true"
    assert fr.locator("button:visible").count() == 0


# --- the home screen ---------------------------------------------------------------------------------------------------

def test_the_home_screen_through_the_real_command(make):
    page, fr, _ = make()  # no scenario: the installed command answers
    wait_keep(fr, "waiting-stack")
    assert fr.locator(".sec.hot .n").inner_text() == "2"
    assert "2" in tab_text(page)  # the tab's count: the waiting videos
    assert visible(fr, "working-job") and visible(fr, "new-video")
    assert fr.locator(".banner.blue").count() == 1  # a sample is never mistaken for the real desk
    assert "הצוות מציע שלושה תיקונים קטנים" in fr.inner_text("body")  # counts in words on a Hebrew line
    assert "בערך דקה" in fr.inner_text("body")


def test_the_series_filter_narrows_the_shelf(make):
    page, fr, _ = make(**desk_ok())
    wait_keep(fr, "series-filter")
    rows = lambda: fr.locator(".shelf .row").count()
    assert rows() == 3
    fr.locator(".fchip", has_text="מדריכים").click()
    settle(page)
    assert rows() == 1 and "איך שותלים זרע" in fr.inner_text(".shelf")
    assert fr.locator(".fchip.on").inner_text() == "מדריכים"
    fr.locator(".fchip", has_text="הכל").click()
    settle(page)
    assert rows() == 3


def test_the_waiting_stack_shows_one_card_and_dots_switch_it(make):
    page, fr, _ = make(**desk_ok())
    wait_keep(fr, "waiting-stack")
    first = fr.locator(".slate .q").inner_text()
    dots = fr.locator(".dots button")
    assert dots.count() == 2 and dots.nth(0).get_attribute("aria-pressed") == "true"
    dots.nth(1).click()
    settle(page)
    assert fr.locator(".slate .q").inner_text() != first and fr.locator(".slate").count() == 1


def test_a_single_waiting_video_has_no_dots_and_no_ghost_cards(make):
    one = sb.sample_desk()["waiting"][:1]
    page, fr, _ = make(**desk_ok(waiting=one))
    wait_keep(fr, "waiting-stack")
    assert fr.locator(".dots").count() == 0 and fr.locator(".ghost").count() == 0


def test_english_uses_plural_forms_and_ltr(make):
    one = sb.sample_desk()["waiting"][:1]
    one[0]["suggestions"] = 1
    page, fr, _ = make(lang="en", **desk_ok(waiting=one))
    wait_keep(fr, "waiting-stack")
    assert "The team suggests one small fix." in fr.inner_text(".slate")
    assert fr.evaluate("getComputedStyle(document.body).direction") == "ltr"


# --- entry points lead to the designed "coming next" page, and the shell's exit always works ---------------------------

def header_shown(page):
    return page.is_visible("header.top") and page.is_visible("#tabs .tabs")


def test_new_video_opens_coming_next_folds_the_header_and_back_returns(make):
    page, fr, _ = make(**desk_ok())
    wait_keep(fr, "new-video")
    assert header_shown(page)
    fr.locator('[data-keep="new-video"]').click()
    wait_keep(fr, "coming-next")
    page.wait_for_function("document.body.classList.contains('focus')")
    assert not header_shown(page)
    for key in KEEP["coming-next"]:
        assert visible(fr, key)
    assert page.evaluate("window.__log.filter(x => x[0] === 'back' && x[1] === 'show').length") >= 1
    box = fr.locator('[data-keep="primary-dock"] .btn.pri').bounding_box()
    assert box["y"] + box["height"] > 844 * 0.7 and box["height"] >= 48  # the main action sits in thumb reach
    page.evaluate("window.__backClick()")  # the native Telegram back button
    wait_keep(fr, "new-video")
    page.wait_for_function("!document.body.classList.contains('focus')")
    page.wait_for_timeout(1500)  # past the shell's wait: the app confirmed, so the page stays in the app
    assert page.is_visible("iframe.appframe") and header_shown(page) and visible(fr, "waiting-stack")


def test_the_bar_and_the_dock_both_lead_home_and_nothing_is_sent(make):
    page, fr, sc = make(**desk_ok())
    wait_keep(fr, "slate-actions")
    calls = sc.calls
    for opener, label in (('[data-keep="slate-actions"] .btn:not(.yes)', "approve"), (".shelf .row >> nth=0", "item"),
                          ("button.link-btn.end", "more")):
        fr.locator(opener).click()
        wait_keep(fr, "coming-next")
        assert "השלב הזה בדרך" in fr.inner_text("body"), label
        assert "שום דבר לא נשלח" in fr.inner_text("body")
        fr.locator('[data-keep="back"]').click()
        wait_keep(fr, "waiting-stack")
        fr.locator('[data-keep="new-video"]').click()
        wait_keep(fr, "coming-next")
        fr.locator('[data-keep="primary-dock"] .btn.pri').click()
        wait_keep(fr, "waiting-stack")
        page.wait_for_function("!document.body.classList.contains('focus')")
    assert sc.calls == calls  # opening a page of the next step asks the studio for nothing and writes nothing


def test_empty_home_new_video_and_templates_lead_to_the_same_page(make):
    page, fr, _ = make(**desk_ok(waiting=[], working=[], shelf=[]))
    wait_keep(fr, "empty-desk")
    assert fr.locator(".tpl").count() == 4
    fr.locator(".tpl").first.click()
    wait_keep(fr, "coming-next")
    page.wait_for_function("document.body.classList.contains('focus')")
    settle(page, 150)  # the panel has taken the app's back request, as a person's tap always comes after
    page.evaluate("window.__backClick()")
    wait_keep(fr, "empty-desk")


def test_a_back_press_on_the_home_screen_is_left_to_the_panel(make):
    page, fr, _ = make(**desk_ok())
    wait_keep(fr, "waiting-stack")
    page.evaluate("window.__backClick()")  # back on the app's home leaves to the panel's home tab, as for any app
    page.wait_for_selector("iframe.appframe", state="hidden")
    assert page.evaluate("window.__log.filter(x => x[0] === 'back').pop()[1]") == "hide"


# --- states recover and stay honest ----------------------------------------------------------------------------------------

def test_an_unreachable_studio_keeps_what_was_loaded_and_recovers(make):
    page, fr, sc = make(**desk_ok())
    wait_keep(fr, "waiting-stack")
    sc.set(abort=True)
    sb.wake(fr)
    wait_keep(fr, "offline-banner")
    assert visible(fr, "waiting-stack")  # what already loaded stays on screen
    sc.set(**desk_ok())
    fr.locator('[data-keep="offline-banner"] button').click()
    fr.wait_for_selector('[data-keep="offline-banner"]', state="detached")
    assert visible(fr, "waiting-stack")


def test_the_bridge_switched_off_while_open_shows_the_off_state_not_stale_videos(make):
    page, fr, sc = make(**desk_ok())
    wait_keep(fr, "waiting-stack")
    sc.set(503, {"ok": False, "error": "mirroring is off"})
    sb.wake(fr)
    wait_keep(fr, "bridge-off")
    assert not visible(fr, "waiting-stack") and not visible(fr, "new-video")
    sc.set(**desk_ok())
    fr.locator('[data-keep="bridge-off"] button').click()
    wait_keep(fr, "waiting-stack")


def test_an_unchanged_desk_is_not_redrawn(make):
    page, fr, sc = make(**desk_ok())
    wait_keep(fr, "waiting-stack")
    fr.evaluate("window.__mark = document.querySelector('[data-keep=waiting-stack]')")
    sb.wake(fr)
    settle(page, 400)
    assert sc.calls >= 2 and fr.evaluate("window.__mark === document.querySelector('[data-keep=waiting-stack]')")


def test_a_failed_reload_with_videos_on_screen_keeps_them_under_a_notice(make):
    page, fr, sc = make(**desk_ok())
    wait_keep(fr, "waiting-stack")
    sc.set(502, {"error": "app failed"})
    sb.wake(fr)
    fr.wait_for_selector(".banner.red")
    assert visible(fr, "waiting-stack")


# --- a host that answers the app's messages directly: the states the panel itself would take over -------------------------

HOST = """<!doctype html><meta charset="utf-8"><body style="margin:0"><iframe id="f" sandbox="allow-scripts allow-forms"
style="border:0;width:390px;height:800px" srcdoc="%s"></iframe><script>
window.__seen = []; window.__answer = __ANSWER__; window.__hello = __HELLO__;
addEventListener("message", function (e) {
  var m = e.data || {}; window.__seen.push(m);
  var f = document.getElementById("f").contentWindow; if (e.source !== f) return;
  if (m.cockpit === "hello" && window.__hello) f.postMessage({cockpit: "hello", initData: "x", lang: "he", dir: "rtl", theme: "", focus: false, inset: {bottom: 34}}, "*");
  if (m.cockpit === "api") f.postMessage({cockpit: "api", id: m.id, status: window.__answer.status, data: window.__answer.data}, "*");
  if (m.cockpit === "back_done") window.__confirmed = true;
});
</script>"""


def host(browser, status=200, data=None, hello=True):
    """The app's page in a plain iframe whose parent answers hello and the API with (status, data)."""
    ctx = browser.new_context(viewport={"width": 390, "height": 800}, device_scale_factor=2)
    page = ctx.new_page()
    doc = (HOST.replace("__ANSWER__", json.dumps({"status": status, "data": data})).replace("__HELLO__", "true" if hello else "false")
           .replace("%s", html.escape(studio_page.render_page(), quote=True)))
    page.set_content(doc)
    return ctx, page


def frame_of(page):
    return page.frame_locator("#f")


DESK = {"ok": True, "desk": sb.sample_desk()}


def test_an_expired_login_shows_the_expired_state_with_a_way_back(browser):
    ctx, page = host(browser, 403, None)
    try:
        fr = frame_of(page)
        fr.locator('[data-keep="expired"]').wait_for(state="visible")
        assert fr.locator('[data-keep="expired"] button').count() == 1
        page.evaluate("window.__answer = {status: 200, data: %s}" % json.dumps(DESK))
        fr.locator('[data-keep="expired"] button').click()
        fr.locator('[data-keep="waiting-stack"]').wait_for(state="visible")
    finally:
        ctx.close()


def test_the_expired_state_keeps_its_core_elements(browser):
    ctx, page = host(browser, 403, None)
    try:
        fr = frame_of(page)
        for key in KEEP["error-expired"]:
            fr.locator('[data-keep="%s"]' % key).wait_for(state="visible")
    finally:
        ctx.close()


def test_the_page_pads_its_main_action_with_the_bottom_inset(browser):
    ctx, page = host(browser, 200, DESK)
    try:
        fr = frame_of(page)
        fr.locator('[data-keep="new-video"]').click()
        fr.locator('[data-keep="primary-dock"]').wait_for(state="visible")
        pad = fr.locator('[data-keep="primary-dock"]').evaluate("e => getComputedStyle(e).paddingBlockEnd")
        assert float(pad.replace("px", "")) >= 18 + 34  # --inset-bottom from the panel's hello
        page.wait_for_function("window.__seen.filter(m => m.cockpit === 'focus' && m.on === true).length === 1")
    finally:
        ctx.close()


def test_the_app_confirms_every_forwarded_back_even_on_its_home(browser):
    ctx, page = host(browser, 200, DESK)
    try:
        fr = frame_of(page)
        fr.locator('[data-keep="waiting-stack"]').wait_for(state="visible")
        page.evaluate("document.getElementById('f').contentWindow.postMessage({cockpit: 'back'}, '*')")
        page.wait_for_function("window.__confirmed === true")
    finally:
        ctx.close()


def test_a_page_opened_without_the_panel_says_so_instead_of_hanging(browser):
    ctx, page = host(browser, hello=False)
    try:
        fr = frame_of(page)
        fr.locator('[data-keep="error-card"]').wait_for(state="visible", timeout=8000)
        assert fr.locator('[data-keep="error-actions"]').count() == 1
    finally:
        ctx.close()


def test_a_wrongly_shaped_desk_is_an_error_and_a_title_is_never_markup(browser):
    ctx, page = host(browser, 200, {"ok": True, "desk": "nope"})
    try:
        fr = frame_of(page)
        fr.locator('[data-keep="error-card"]').wait_for(state="visible")
        title = "<img src=x onerror=alert(1)>"
        page.evaluate("window.__answer = {status: 200, data: {ok: true, desk: {waiting: [{title: %s, scene: 'nope', series: 'x',"
                      " draft: 9, suggestions: 'a'}]}}}" % json.dumps(title))
        fr.locator('[data-keep="error-card"] button').first.click()
        fr.locator('[data-keep="waiting-stack"]').wait_for(state="visible")
        assert fr.locator("img").count() == 0
        assert title in fr.locator(".slate .q").inner_text()
    finally:
        ctx.close()



# --- the desk of one video ----------------------------------------------------------------------------------------------------

@pytest.fixture
def desk(browser):
    """The panel with the Studio open on the first waiting video. `answers` per op (studio_browser.Router); an op
    without an answer goes to the real installed command. Returns (page, frame, router)."""
    opened = []

    def open_(lang="he", scheme="light", answers=None, width=390, height=844, wait="cue-stack", srv=None):
        srv = srv or sb.start_panel(lang)
        holder = {}
        ctx, page, fr = sb.open_studio(browser, srv, lang=lang, scheme=scheme, width=width, height=height,
                                       setup=lambda pg: holder.setdefault("r", sb.router(pg, answers)))
        opened.append((ctx, srv))
        wait_keep(fr, "slate-actions")
        fr.locator('[data-keep="slate-actions"] .btn.yes').click()
        if wait:
            wait_keep(fr, wait)
        return page, fr, holder["r"]

    yield open_
    seen = set()
    for ctx, srv in opened:
        ctx.close()
        if id(srv) not in seen:
            seen.add(id(srv))
            sb.stop_panel(srv)


def any_visible(fr, key):
    loc = fr.locator('[data-keep="%s"]' % key)
    return any(loc.nth(i).is_visible() for i in range(loc.count()))


def wait_any(fr, key):
    fr.wait_for_selector('[data-keep="%s"] >> visible=true' % key)


def wait_ops(page, r, op, n, ms=8000):
    """Wait until the page has sent `n` calls of `op` (a request reaches the network a moment after the tap)."""
    for _ in range(ms // 50):
        if len(r.ops(op)) >= n:
            return r.ops(op)
        page.wait_for_timeout(50)
    raise AssertionError("%s was sent %d times, not %d" % (op, len(r.ops(op)), n))


def tap_answer(fr, n):
    fr.locator('[data-keep="cue-answers"] .btn').nth(n).click()


ALL_DONE = {"2": {"mark": "remove"}, "5": {"mark": "tighten"}, "10": {"mark": "keep"}}
DESK_STATES = {  # name: (answers, what to do once the desk is open, the key the state waits on)
    "desk": ({}, None, "cue-stack"),
    "desk-scenes": ({}, lambda fr: fr.locator('[data-keep="desk-modes"] .opt').nth(1).click(), "storyboard"),
    "desk-done": ({"project.get": sb.project_ok(marks=ALL_DONE)}, None, "no-suggestions"),
    "words": ({}, lambda fr: fr.locator('[data-keep="to-map"]').click(), "sentence-list"),
    "loading-desk": ({"project.get": "hang"}, None, "poster-first"),
    "error-project": ({"project.get": (502, {"error": "app failed"})}, None, "error-card"),
}


@pytest.mark.parametrize("state", sorted(DESK_STATES))
@pytest.mark.parametrize("lang,scheme", [("he", "light"), ("he", "dark"), ("en", "light")])
def test_every_desk_state_keeps_its_core_elements_and_fits_the_phone(desk, state, lang, scheme):
    answers, then, key = DESK_STATES[state]
    page, fr, _ = desk(lang=lang, scheme=scheme, answers=answers, wait=None)
    if then:
        wait_keep(fr, "desk-modes")
        then(fr)
    for k in KEEP[state]:
        wait_any(fr, k)  # a loaded machine may draw late: wait for each key, then assert
    settle(page)
    for k in KEEP[state]:
        assert any_visible(fr, k), (state, k)
    page.wait_for_function("document.body.classList.contains('focus')")  # a sub-page folds the panel's header and tabs
    assert fr.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert fr.evaluate("document.documentElement.dir") == ("rtl" if lang == "he" else "ltr")
    text = fr.inner_text("body")
    assert not (HEBREW.search(text) if lang == "en" else re.search(r"[A-Za-z]", text)), text
    for b in fr.locator("button:visible").all():  # touch targets
        box = b.bounding_box()
        if box:
            assert box["height"] >= 43.5 and box["width"] >= 27.5, (state, b.inner_text(), box)
    if any_visible(fr, "primary-dock"):  # the main action sits in thumb reach
        box = fr.locator('[data-keep="primary-dock"] .btn.pri').bounding_box()
        assert box["y"] + box["height"] > 844 * 0.7 and box["height"] >= 48


def test_an_unreachable_studio_keeps_the_loaded_video_and_saves_nothing(desk):
    page, fr, r = desk()
    page.evaluate("window.__backClick()")
    wait_keep(fr, "waiting-stack")
    r.answers["project.get"] = "abort"
    fr.locator('[data-keep="slate-actions"] .btn.yes').click()
    wait_keep(fr, "offline-banner")
    for k in KEEP["offline-desk"]:
        assert any_visible(fr, k), k
    assert fr.locator('[data-keep="cue-answers"] .btn[disabled]').count() == 3  # nothing pretends to save
    marks_before = len(r.ops("marks.set"))
    fr.locator('[data-keep="cue-answers"] .btn').nth(2).click(force=True)
    settle(page)
    assert len(r.ops("marks.set")) == marks_before


def test_a_mark_is_kept_by_the_real_command_and_comes_back_on_the_next_visit(desk):
    page, fr, r = desk()  # no answers: the installed command answers and stores the mark in the app's state folder
    assert fr.locator('[data-keep="map"] i').nth(1).get_attribute("class") == "s cur"
    tap_answer(fr, 2)  # the team suggests removing sentence two: remove it
    fr.wait_for_selector('[data-keep="cue-stack"] >> text=0:20–0:27')
    fr.wait_for_function("document.querySelector('[data-keep=new-length]').textContent.indexOf('1:05') >= 0")
    assert wait_ops(page, r, "marks.set", 1)[-1] == {"op": "marks.set", "project": "weekly-12", "i": 2, "mark": "remove"}
    stored = cockpit_apps.apps_dir() / "studio" / "state" / "marks.json"
    for _ in range(160):  # the command has written it before the next visit asks
        if stored.is_file() and '"remove"' in stored.read_text():
            break
        page.wait_for_timeout(50)
    page2, fr2, _ = desk()  # a fresh panel on the same Studio computer
    assert fr2.locator('[data-keep="map"] i').nth(1).get_attribute("class") == "x"
    assert "שינוי אחד" in fr2.inner_text('[data-keep="new-length"]')


def test_back_walks_from_the_words_view_to_the_desk_and_then_home(desk):
    page, fr, _ = desk()
    fr.locator('[data-keep="to-map"]').click()
    wait_keep(fr, "sentence-list")
    page.evaluate("window.__backClick()")
    wait_keep(fr, "cue-stack")
    assert page.evaluate("document.body.classList.contains('focus')")
    page.evaluate("window.__backClick()")
    wait_keep(fr, "waiting-stack")
    page.wait_for_function("!document.body.classList.contains('focus')")
    page.wait_for_timeout(1500)  # past the shell's wait: every back was confirmed, so the page stays in the app
    assert page.is_visible("iframe.appframe")


@pytest.mark.parametrize("lang,page_dir", [("he", "rtl"), ("en", "ltr")])
def test_the_empty_shorter_wording_field_takes_the_page_direction(desk, lang, page_dir):
    page, fr, r = desk(lang=lang)
    tap_answer(fr, 0)  # keep sentence two: the next suggestion is "shorten"
    wait_ops(page, r, "marks.set", 1)
    fr.wait_for_selector("button.fix")
    fr.locator("button.fix").click()
    wait_keep(fr, "short-edit")
    field = fr.locator('[data-keep="short-edit"] textarea')
    assert field.get_attribute("dir") == "auto"  # it holds the team's wording
    field.fill("")
    assert field.get_attribute("dir") == page_dir
    assert field.evaluate("e => getComputedStyle(e).direction") == page_dir  # the placeholder sits on the page's side
    field.fill("שמש" if lang == "en" else "Sun")
    assert field.get_attribute("dir") == "auto"
    assert field.evaluate("e => getComputedStyle(e).direction") == ("rtl" if lang == "en" else "ltr")
    tap_answer(fr, 1)
    fr.wait_for_selector('[data-keep="short-edit"]', state="detached")
    assert wait_ops(page, r, "marks.set", 2)[-1]["text"] == ("שמש" if lang == "en" else "Sun")


def test_the_storyboard_opens_the_words_view_at_that_scene(desk):
    page, fr, _ = desk()
    fr.locator('[data-keep="desk-modes"] .opt').nth(1).click()
    wait_keep(fr, "storyboard")
    assert fr.locator('[data-keep="scene-card"]').count() == 7
    fr.locator('[data-keep="scene-card"]').nth(4).click()
    wait_keep(fr, "sentence-list")
    page.wait_for_timeout(300)
    box = fr.locator('[data-keep="scene-group"]').nth(4).bounding_box()
    assert box and -2 <= box["y"] < 300  # its group is at the top of the screen


def test_the_desk_on_a_wide_screen_keeps_the_player_beside_the_cue(desk):
    page, fr, _ = desk(width=1280, height=900)
    p = fr.locator('[data-keep="player"]').first.bounding_box()
    c = fr.locator('[data-keep="cue-stack"]').bounding_box()
    assert p and c and abs(p["y"] - c["y"]) < 120 and (p["x"] > c["x"])  # side by side, the player at the start (RTL)
    assert fr.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
