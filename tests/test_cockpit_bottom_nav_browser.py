"""Phone-sized coverage of the Cockpit navigation using deterministic local responses."""
import json

import pytest

from ctb import panel_page
import panel_browser

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def browser():
    panel_browser.prepare_env()
    with sync_api.sync_playwright() as p:
        b = panel_browser.launch(p)
        if b is None:
            pytest.skip("headless Chromium cannot run in this environment")
        yield b
        b.close()


def open_mock(browser, lang, count=1, scheme="light"):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=scheme, bypass_csp=True)
    page = ctx.new_page()
    apps = [{"name": "studio" if i == 0 else "extra%d" % i, "title": "Studio" if i == 0 else "Extra %d" % i,
             "title_he": "סטודיו" if i == 0 else "נוסף %d" % i, "icon": "film"} for i in range(count)]
    state = {"topic": {"name": "נושא" if lang == "he" else "Topic"}, "apps": apps, "now": 0}
    def respond(route):
        url = route.request.url
        if url.endswith("telegram-web-app.js"):
            route.fulfill(content_type="application/javascript", body=panel_browser.STUB % json.dumps("test"))
        elif url.endswith("/api/state"):
            route.fulfill(content_type="application/json", body=json.dumps(state))
        else:
            route.fulfill(content_type="text/html", body=panel_page.render_page(lang, "test").decode())
    page.route("**/*", respond)
    page.goto("http://panel.test/")
    page.wait_for_selector(".tabs .tab")
    return ctx, page


@pytest.mark.parametrize("lang", ["he", "en"])
def test_bottom_nav_labels_position_and_content(browser, lang):
    ctx, page = open_mock(browser, lang)
    labels = page.locator(".tabs .tab-label")
    assert labels.count() == 5
    assert labels.evaluate_all("els => els.every(e => e.scrollWidth <= e.clientWidth && e.scrollHeight <= e.clientHeight)")
    bar = page.locator(".tabs").bounding_box()
    assert bar["y"] + bar["height"] == pytest.approx(844, abs=1)
    assert page.locator(".tabs .tab").evaluate_all("els => els.every(e => e.offsetHeight >= 44 && e.offsetWidth >= 44)")
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    assert page.locator("#app").bounding_box()["y"] + page.locator("#app").bounding_box()["height"] <= bar["y"]
    order = page.locator(".tabs .tab").evaluate_all("els => els.map(e => e.getBoundingClientRect().x)")
    assert (order[0] > order[-1]) == (lang == "he")
    ctx.close()


def test_more_sheet_selects_additional_app(browser):
    ctx, page = open_mock(browser, "he", count=2)
    assert page.locator(".tabs .tab").count() == 5
    page.locator(".tabs .tab-more").click()
    assert page.locator(".more-sheet").is_visible()
    assert page.locator(".more-sheet .tab").count() == 2
    page.locator(".more-sheet .tab").last.click()
    assert not page.locator(".more-sheet").is_visible()
    assert page.locator(".tabs .tab-more").get_attribute("aria-expanded") == "false"
    assert "on" in page.locator(".tabs .tab-more").get_attribute("class")
    ctx.close()


def test_more_sheet_closes_on_outside_tap_and_app_frame_clears_the_bar(browser):
    ctx, page = open_mock(browser, "he", count=3)
    page.locator(".tabs .tab-more").click()
    assert page.locator(".more-sheet").is_visible()
    page.mouse.click(195, 300)
    assert not page.locator(".more-sheet").is_visible()
    assert page.locator(".tabs .tab-more").get_attribute("aria-expanded") == "false"
    page.locator(".tabs .tab-more").click()
    page.locator(".more-sheet .tab").first.click()
    page.wait_for_timeout(300)
    frame_bottom, bar_top = page.evaluate(
        "()=>[[...document.querySelectorAll('.appframe')].find(x=>!x.hidden).getBoundingClientRect().bottom,"
        "document.querySelector('.tabs').getBoundingClientRect().top]")
    assert frame_bottom <= bar_top
    toast_bottom = page.evaluate("()=>document.getElementById('toast').getBoundingClientRect().bottom")
    assert toast_bottom <= bar_top
    ctx.close()
