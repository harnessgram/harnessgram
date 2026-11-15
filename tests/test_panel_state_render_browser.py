"""The Cockpit's first load in a real browser with state shaped like a live install's, and what the page says when a
load fails. The fixture (fixtures/cockpit_state_shape.json) keeps the real structure (every key, nesting, enum
value, translated "_he" copies and pending translations) with every text, name, link and id replaced. A first load
used to report "no access" for any failure, including a reply cut by the server's deadline. Skipped as a whole
when Playwright or a Chromium build is missing."""
import copy
import json
import re
from pathlib import Path

import pytest

from ctb import cockpit_v2, panel_page
import panel_browser

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

SHAPE = json.loads((Path(__file__).parent / "fixtures" / "cockpit_state_shape.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def browser():
    panel_browser.prepare_env()
    with sync_api.sync_playwright() as p:
        b = panel_browser.launch(p)
        if b is None:
            pytest.skip("no headless browser is installed")
        yield b
        b.close()


def shaped(v2):
    state = copy.deepcopy(SHAPE)
    state["settings"]["ui_v2"] = "on" if v2 else "off"
    state["ui_v2_enabled"] = v2
    return state


def open_page(browser, lang, state_reply):
    """The page on a stubbed origin. `state_reply(route)` answers /api/state; other reads get small valid replies."""
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, locale="he-IL" if lang == "he" else "en-US")
    page = ctx.new_page()
    errors, posts = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    stub = panel_browser.STUB % json.dumps("owner-fixture")

    def route(r):
        url = r.request.url
        path = url.split("127.0.0.1", 1)[-1].split("?", 1)[0]
        if "telegram.org/js/telegram-web-app.js" in url:
            r.fulfill(content_type="application/javascript", body=stub)
        elif path == "/":
            r.fulfill(content_type="text/html", body=panel_page.render_page(lang, "fixture"))
        elif path.startswith(cockpit_v2.ASSET_PREFIX):
            r.fulfill(content_type="image/webp", body=(cockpit_v2.ASSET_ROOT / path.rsplit("/", 1)[-1]).read_bytes())
        elif path == "/api/state":
            state_reply(r)
        elif path == "/api/app-token":
            panel_browser.app_token_reply(r)
        elif path.startswith("/api/"):
            posts.append((path, r.request.post_data_json))
            r.fulfill(json={"ok": True})
        else:
            r.fulfill(status=404, body="")

    page.route("**/*", route)
    page.goto("http://127.0.0.1/")
    return ctx, page, errors, posts


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("v2", [True, False], ids=["v2", "classic"])
def test_real_shaped_state_draws_every_tab_without_an_error(browser, lang, v2):
    state = shaped(v2)
    ctx, page, errors, posts = open_page(browser, lang, lambda r: r.fulfill(json=state))
    S = panel_page.STRINGS[lang]
    try:
        page.locator(".v2-needs" if v2 else "#tabs .tab").first.wait_for()
        tabs = page.locator("#tabs [role=tab]")
        for i in range(tabs.count()):
            if tabs.nth(i).is_visible():
                tabs.nth(i).click()
                page.wait_for_timeout(150)
            text = page.inner_text("#app")
            for bad in (S["denied"], S["renderFailed"], S["offline"]):
                assert bad not in text, (i, bad)
        assert not errors, errors
        assert not [p for p in posts if p[0] == "/api/client-error"]
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
def test_a_state_that_cannot_be_drawn_says_so_and_reports_only_type_and_place(browser, lang):
    state = shaped(True)
    state["decisions"] = 5  # not a list: drawing it throws a TypeError on the first load
    ctx, page, errors, posts = open_page(browser, lang, lambda r: r.fulfill(json=state))
    S = panel_page.STRINGS[lang]
    try:
        page.get_by_text(S["renderFailed"]).wait_for()
        assert S["denied"] not in page.inner_text("#app")
        assert page.locator("#app button").count() == 1  # the retry
        page.wait_for_timeout(200)
        reports = [b for p, b in posts if p == "/api/client-error"]
        assert len(reports) == 1, reports
        assert set(reports[0]) == {"name", "at"}
        assert reports[0]["name"] == "TypeError"
        assert re.fullmatch(r"[\w$.]{0,60}:\d+:\d+", reports[0]["at"]), reports[0]
        page.locator("#app button").click()  # the same error again is not reported twice
        page.get_by_text(S["renderFailed"]).wait_for()
        page.wait_for_timeout(200)
        assert len([1 for p, _ in posts if p == "/api/client-error"]) == 1
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("how", ["cut", "500"])
def test_a_first_load_that_gets_no_reply_is_not_no_access(browser, lang, how):
    def reply(r):
        if how == "cut":
            r.abort("connectionreset")  # what the server's deadline did to a slow state build
        else:
            r.fulfill(status=500, json={"error": "unavailable"})
    ctx, page, errors, posts = open_page(browser, lang, reply)
    S = panel_page.STRINGS[lang]
    try:
        page.get_by_text(S["offline"]).wait_for()
        assert S["denied"] not in page.inner_text("#app")
        assert page.locator("#app button").count() == 1  # the retry
        assert not [p for p in posts if p[0] == "/api/client-error"]
    finally:
        ctx.close()
