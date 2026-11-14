"""The native BackButton and browser/OS history share the Cockpit's reversible UI stack."""
import json

import pytest

from ctb import panel_page
import panel_browser
from test_cockpit_overflow_browser import browser  # noqa: F401
from test_cockpit_compact_browser import state


def open_cockpit(browser, data=None):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, bypass_csp=True)
    page = ctx.new_page()
    if data is None:
        data = state()
        data["apps"] = [{"name": name, "title": name, "icon": "film"} for name in ("one", "two")]
    stub = panel_browser.STUB % json.dumps("test")
    stub += """
    const wa = Telegram.WebApp;
    wa.BackButton.isVisible = false;
    wa.BackButton.show = () => { wa.BackButton.isVisible = true; };
    wa.BackButton.hide = () => { wa.BackButton.isVisible = false; };
    wa.close = () => { window.__closed = true; };
    wa.enableClosingConfirmation = () => { wa.isClosingConfirmationEnabled = true; };
    wa.disableClosingConfirmation = () => { wa.isClosingConfirmationEnabled = false; };
    """

    def respond(route):
        url = route.request.url
        if url.endswith("/outside"):
            route.fulfill(content_type="text/html", body="<title>Outside</title><p>Outside Cockpit</p>")
        elif url.endswith("telegram-web-app.js"):
            route.fulfill(content_type="application/javascript", body=stub)
        elif url.endswith("/api/state"):
            route.fulfill(content_type="application/json", body=json.dumps(data))
        elif "/app/" in url:
            route.fulfill(content_type="text/html", body="<!doctype html><title>App</title>")
        else:
            route.fulfill(content_type="text/html", body=panel_page.render_page("he", "test").decode())

    page.route("**/*", respond)
    page.goto("http://panel.test/outside")
    page.goto("http://panel.test/")
    page.wait_for_selector(".tabs .tab")
    return ctx, page


def go_back(page, method):
    if method == "popstate":
        page.go_back()
    else:
        page.evaluate("window.__backClick()")


def assert_root(page):
    page.wait_for_function("history.state.cockpitBack.index === 0 && !Telegram.WebApp.BackButton.isVisible")


@pytest.mark.parametrize("method", ["popstate", "button"])
@pytest.mark.parametrize("tab", [0, 2])
def test_sheet_closes_before_the_tab_and_root_can_exit(browser, method, tab):
    ctx, page = open_cockpit(browser)
    try:
        assert_root(page)
        if tab:
            page.locator(".tabs .tab").nth(tab).click()
        page.locator(".tab-more").click()
        assert page.locator(".more-sheet").is_visible()
        go_back(page, method)
        page.wait_for_function("document.querySelector('.more-sheet').hidden")
        assert page.locator(".tabs .tab").nth(tab).get_attribute("aria-selected") == "true"
        if tab:
            assert page.evaluate("Telegram.WebApp.BackButton.isVisible")
            go_back(page, method)
        assert_root(page)
        assert page.locator(".tabs .tab").first.get_attribute("aria-selected") == "true"
        page.go_back()
        page.wait_for_url("**/outside")
        assert page.title() == "Outside"
    finally:
        ctx.close()


@pytest.mark.parametrize("method", ["popstate", "button"])
def test_non_home_tabs_share_one_step_and_forward_uses_the_latest_tab(browser, method):
    ctx, page = open_cockpit(browser)
    try:
        for index in (1, 2, 3):
            page.locator(".tabs .tab").nth(index).click()
        assert page.evaluate("history.state.cockpitBack.index") == 1
        go_back(page, method)
        assert_root(page)
        assert page.locator(".tabs .tab").first.get_attribute("aria-selected") == "true"
        page.go_forward()
        page.wait_for_function("document.querySelectorAll('.tabs .tab')[3].getAttribute('aria-selected') === 'true'")
        go_back(page, method)
        assert_root(page)
    finally:
        ctx.close()


@pytest.mark.parametrize("method", ["popstate", "button"])
def test_nested_expansion_unwinds_after_a_refresh_and_keeps_unsaved_text(browser, method):
    ctx, page = open_cockpit(browser)
    try:
        outer = page.locator('[data-block="usage-compact-session"] > button')
        outer.click()
        inner = page.locator('[data-block="usage-compact-session"] .usage-model > button').first
        inner.click()
        page.locator("#rf").click()
        page.wait_for_function("document.querySelector('#rf').className === 'icon-btn'")
        go_back(page, method)
        page.wait_for_function("!document.querySelector('[data-block=\"usage-compact-session\"] .usage-model').classList.contains('open')")
        assert outer.get_attribute("aria-expanded") == "true"
        go_back(page, method)
        assert_root(page)
        assert outer.get_attribute("aria-expanded") == "false"
        textarea = page.locator(".saybox textarea").first
        textarea.fill("טיוטה שלא נשלחה")
        assert page.evaluate("Telegram.WebApp.isClosingConfirmationEnabled")
        # A stale native callback at root must close, never add another history entry.
        page.evaluate("window.__backClick()")
        assert page.evaluate("window.__closed && Telegram.WebApp.isClosingConfirmationEnabled")
        assert_root(page)
        assert textarea.input_value() == "טיוטה שלא נשלחה"
    finally:
        ctx.close()


def test_manual_sheet_dismissal_does_not_leave_a_phantom_back_step(browser):
    ctx, page = open_cockpit(browser)
    try:
        page.locator(".tab-more").click()
        page.evaluate("document.body.click()")  # outside the sheet, on nothing that opens a layer
        assert_root(page)
        page.locator(".tab-more").click()
        page.locator(".more-sheet .tab").last.click()
        page.evaluate("window.__backClick()")
        assert_root(page)
        page.go_back()
        page.wait_for_url("**/outside")
    finally:
        ctx.close()


def test_revisited_root_closes_its_open_block_then_can_exit(browser):
    ctx, page = open_cockpit(browser)
    try:
        outer = page.locator('[data-block="usage-compact-session"] > button')
        outer.click()
        page.locator(".tabs .tab").nth(2).click()
        page.locator(".tabs .tab").first.click()
        page.wait_for_function("document.querySelectorAll('.tabs .tab')[0].getAttribute('aria-selected') === 'true'")
        assert outer.get_attribute("aria-expanded") == "true"
        page.evaluate("window.__backClick()")
        page.wait_for_function("!document.querySelector('[data-block=\"usage-compact-session\"]').classList.contains('open')")
        assert page.locator(".tabs .tab").first.get_attribute("aria-selected") == "true"
        assert_root(page)
        page.go_back()
        page.wait_for_url("**/outside")
    finally:
        ctx.close()


@pytest.mark.parametrize("method", ["popstate", "button"])
@pytest.mark.parametrize("home_layer", [True, False])
def test_a_thousand_non_home_switches_then_back_reaches_home_then_closes_or_exits(browser, method, home_layer):
    ctx, page = open_cockpit(browser)
    try:
        if home_layer:
            page.locator('[data-block="usage-compact-session"] > button').click()
        page.evaluate("""() => {
            for (let i = 0; i < 1000; i++) {
                const index = 1 + i % 3;
                document.querySelectorAll('.tabs .tab')[index].click();
            }
        }""")
        assert page.evaluate("history.state.cockpitBack.index") == (2 if home_layer else 1)
        go_back(page, method)
        page.wait_for_function("document.querySelectorAll('.tabs .tab')[0].getAttribute('aria-selected') === 'true'")
        if home_layer:
            outer = page.locator('[data-block="usage-compact-session"] > button')
            assert outer.get_attribute("aria-expanded") == "true"
            go_back(page, method)
            assert_root(page)
            assert outer.get_attribute("aria-expanded") == "false"
            page.go_back()
            page.wait_for_url("**/outside")
        else:
            assert_root(page)
            go_back(page, method)
            if method == "popstate":
                page.wait_for_url("**/outside")
            else:
                assert page.evaluate("window.__closed")
    finally:
        ctx.close()


@pytest.mark.parametrize("method", ["popstate", "button"])
def test_many_distinct_app_tabs_keep_one_step_and_matching_browser_history(browser, method):
    data = state()
    data["apps"] = [{"name": "app%d" % i, "title": "App %d" % i, "icon": "film"} for i in range(12)]
    ctx, page = open_cockpit(browser, data)
    try:
        for i in range(12):
            page.locator(".tab-more").click()
            page.locator(".more-sheet .tab").nth(i).click()
            page.wait_for_function("title => document.querySelector('.more-sheet [aria-selected=true]')?.textContent === title", arg="App %d" % i)
        page.wait_for_function("history.state.cockpitBack.index === 1")
        go_back(page, method)
        assert_root(page)
        page.go_forward()
        page.wait_for_function("document.querySelector('.more-sheet [aria-selected=true]')?.textContent === 'App 11'")
        go_back(page, method)
        assert_root(page)
        page.go_back()
        page.wait_for_url("**/outside")
    finally:
        ctx.close()


@pytest.mark.parametrize("method", ["popstate", "button"])
def test_refresh_removing_an_open_card_back_returns_to_home(browser, method):
    data = state()
    ctx, page = open_cockpit(browser, data)
    try:
        page.locator(".tabs .tab").nth(2).click()
        card = page.locator("details.srow").first
        card.locator("summary").click()
        page.wait_for_function("document.querySelector('details.srow').open")
        data["sessions"] = []
        page.locator("#rf").click()
        page.wait_for_function("document.querySelector('#rf').className === 'icon-btn' && !document.querySelector('details.srow')")
        page.wait_for_function("history.state.cockpitBack.index === 1")
        go_back(page, method)
        assert_root(page)
        assert page.locator(".tabs .tab").first.get_attribute("aria-selected") == "true"
    finally:
        ctx.close()
