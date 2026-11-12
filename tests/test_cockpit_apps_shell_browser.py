"""The same shell rules as test_cockpit_apps_shell.py, in a real headless browser with the real page. Skipped as a
whole when Playwright or a Chromium build is missing; the rules themselves are also checked without a browser."""
import threading

import pytest

from conftest import GOOD_TOKEN
from ctb import cockpit_apps, panel, panel_page
from ctb.config import Config
from test_cockpit_apps import make_app
from test_panel import OWNER, signed
import panel_browser

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")


@pytest.fixture
def panel_server():
    cockpit_apps._warned.clear()
    folder = make_app(with_media=False, with_command=False)
    (folder / "index.html").write_text(panel_browser.SAMPLE_PAGE, encoding="utf-8")
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    srv = panel.PanelServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def browser():
    panel_browser.prepare_env()
    with sync_api.sync_playwright() as p:
        b = panel_browser.launch(p)
        if b is None:
            pytest.skip("no headless browser is installed")
        yield b
        b.close()


def open_app(browser, srv, **kw):
    ctx, page = panel_browser.open_panel(browser, srv, signed(), **kw)
    page.click(".tab.app")
    fr = panel_browser.app_frame(page)
    fr.wait_for_selector("#primary")
    fr.wait_for_function("document.getElementById('log').textContent.includes('hello')")  # the page answered its hello
    return ctx, page, fr


def header_shown(page) -> bool:
    return page.is_visible("header.top") and page.is_visible("#tabs .tabs")


def say(frame, msg):
    frame.evaluate("m => parent.postMessage(m, '*')", msg)


def settle(page):
    page.wait_for_timeout(150)


def test_an_app_folds_the_header_and_tabs_and_unfolds_them(browser, panel_server):
    ctx, page, fr = open_app(browser, panel_server)
    assert header_shown(page)
    say(fr, {"cockpit": "focus", "on": True})
    page.wait_for_function("document.body.classList.contains('focus')")
    assert not page.is_visible("header.top") and not page.is_visible("#tabs .tabs")
    assert fr.evaluate("innerHeight") >= 800  # the frame now reaches nearly the whole phone screen
    say(fr, {"cockpit": "focus", "on": False})
    page.wait_for_function("!document.body.classList.contains('focus')")
    assert header_shown(page)
    ctx.close()


def test_switching_tabs_restores_the_chrome_and_returning_folds_it_again(browser, panel_server):
    ctx, page, fr = open_app(browser, panel_server)
    say(fr, {"cockpit": "focus", "on": True})
    page.wait_for_function("document.body.classList.contains('focus')")
    page.evaluate("document.querySelector('.tab:not(.app)').click()")  # switching tabs restores the chrome at once
    page.wait_for_function("!document.body.classList.contains('focus')")
    assert header_shown(page) and not page.is_visible("iframe.appframe")
    page.click(".tab.app")
    page.wait_for_function("document.body.classList.contains('focus')")
    assert not page.is_visible("header.top")
    ctx.close()


def test_a_foreign_or_malformed_message_changes_nothing(browser, panel_server):
    ctx, page, fr = open_app(browser, panel_server)
    # the page itself, and a second sandboxed copy of the app that the page did not create as its app frame
    page.evaluate("window.postMessage({cockpit: 'focus', on: true}, '*')")
    page.evaluate("""u => { const f = document.createElement('iframe'); f.id = 'foreign'; f.setAttribute('sandbox', 'allow-scripts');
        f.src = u; document.body.appendChild(f); }""", fr.url)
    page.wait_for_function("document.getElementById('foreign') && document.getElementById('foreign').contentWindow")
    page.wait_for_function("document.getElementById('foreign').contentDocument === null")  # cross-origin: loaded
    for _ in range(50):
        foreign = next((f for f in page.frames if f != fr and f.url == fr.url), None)
        if foreign:
            break
        page.wait_for_timeout(100)
    foreign.wait_for_selector("#primary")
    say(foreign, {"cockpit": "focus", "on": True})
    say(foreign, {"cockpit": "badge", "n": 9})
    # our own frame, with wrong shapes
    for bad in ({"cockpit": "focus"}, {"cockpit": "focus", "on": "true"}, {"cockpit": "badge", "n": "<b>1</b>"},
                {"cockpit": "badge", "n": 100000}, {"cockpit": "navigate", "url": "https://example.com"},
                {"cockpit": "open_chat", "url": "https://example.com"}):
        say(fr, bad)
    settle(page)
    assert header_shown(page) and not page.evaluate("document.body.classList.contains('focus')")
    assert page.locator(".tab .tb").count() == 0
    assert page.evaluate("window.__log.filter(x => x[0] === 'link' || x[0] === 'tg').length") == 0
    ctx.close()


def test_a_hidden_app_cannot_fold_the_page_but_may_show_a_count(browser, panel_server):
    ctx, page, fr = open_app(browser, panel_server)
    page.click(".tab:not(.app) >> nth=0")  # another tab is showing; the app frame is kept alive but hidden
    assert header_shown(page)
    say(fr, {"cockpit": "focus", "on": True})
    say(fr, {"cockpit": "badge", "n": 3})
    page.wait_for_selector(".tab.app .tb")
    assert page.inner_text(".tab.app .tb").strip("\u200f\u200e") == "3"
    assert header_shown(page) and not page.evaluate("document.body.classList.contains('focus')")
    page.click(".tab.app")  # the ignored request left nothing behind: the app opens with the page's chrome intact
    settle(page)
    assert header_shown(page)
    ctx.close()


def test_the_native_back_button_reaches_the_app_when_it_confirms(browser, panel_server):
    ctx, page, fr = open_app(browser, panel_server)
    say(fr, {"cockpit": "back", "on": True})
    settle(page)
    page.evaluate("window.__backClick()")
    fr.wait_for_function("document.getElementById('log').textContent.includes('back')")
    page.wait_for_timeout(1600)  # past the wait: the app confirmed, so the page stays in the app
    assert page.is_visible("iframe.appframe")
    ctx.close()


def test_an_app_that_ignores_back_cannot_trap_the_page(browser, panel_server):
    ctx, page, fr = open_app(browser, panel_server)
    fr.evaluate("window.ackBack = false")
    say(fr, {"cockpit": "focus", "on": True})
    say(fr, {"cockpit": "back", "on": True})
    page.wait_for_function("document.body.classList.contains('focus')")
    page.evaluate("window.__backClick()")
    page.wait_for_function("!document.body.classList.contains('focus')", timeout=4000)
    assert header_shown(page) and not page.is_visible("iframe.appframe")
    page.click(".tab.app")  # a second press also leaves at once, without waiting
    say(fr, {"cockpit": "focus", "on": True})
    say(fr, {"cockpit": "back", "on": True})
    page.wait_for_function("document.body.classList.contains('focus')")
    page.evaluate("window.__backClick()")
    page.evaluate("window.__backClick()")
    page.wait_for_function("!document.body.classList.contains('focus')", timeout=500)
    ctx.close()


def test_a_reloaded_frame_starts_unfolded(browser, panel_server):
    ctx, page, fr = open_app(browser, panel_server)
    say(fr, {"cockpit": "focus", "on": True})
    say(fr, {"cockpit": "back", "on": True})
    page.wait_for_function("document.body.classList.contains('focus')")
    fr.evaluate("location.reload()")
    page.wait_for_function("!document.body.classList.contains('focus')")
    assert header_shown(page)
    ctx.close()


def test_a_login_error_while_folded_leaves_the_app_and_shows_the_message(browser, panel_server):
    ctx, page, fr = open_app(browser, panel_server)
    say(fr, {"cockpit": "focus", "on": True})
    say(fr, {"cockpit": "back", "on": True})
    page.wait_for_function("document.body.classList.contains('focus')")
    page.route("**/api/state", lambda route: route.fulfill(status=403, body="{}"))
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_function("!document.body.classList.contains('focus')")
    assert header_shown(page) and not page.is_visible("iframe.appframe")
    assert panel_page.STRINGS["he"]["reopen"] in page.inner_text("#app")
    ctx.close()


def test_the_app_learns_the_bottom_inset_in_focus_mode_only(browser, panel_server):
    ctx, page, fr = open_app(browser, panel_server)
    page.evaluate("document.documentElement.style.setProperty('--tg-safe-area-inset-bottom', '34px')")
    say(fr, {"cockpit": "focus", "on": True})
    fr.wait_for_function("document.documentElement.style.getPropertyValue('--inset-bottom') === '34px'")
    dock = fr.evaluate("parseFloat(getComputedStyle(document.querySelector('.dock')).paddingBottom)")
    assert dock == 46  # the main action stays above the home-indicator strip
    say(fr, {"cockpit": "focus", "on": False})
    fr.wait_for_function("document.documentElement.style.getPropertyValue('--inset-bottom') === '0px'")
    ctx.close()
