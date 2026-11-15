"""The podcast tile in the new Cockpit's Radio tool, at phone widths, in both languages and both schemes: it is one
more tile beside the room and the announcement, it opens to the now-playing line, the transport and the queue, its
actions post to /api/podcast, and nothing overflows or breaks the 16 px gutter. Skipped without Playwright/Chromium."""
import pytest

pytest.importorskip("playwright")

import panel_browser
from cockpit_v2_browser import open_v2, rich_state
from cockpit_v2_screenshots import go_radio
from test_cockpit_v2_layout_browser import BLEED, CARDS, GUTTER, MEASURE

sync_api = pytest.importorskip("playwright.sync_api")

OPEN = "[data-v2-section='radio-actions'] [data-block='v2-podcast']"


@pytest.fixture(scope="module")
def browser():
    panel_browser.prepare_env()
    with sync_api.sync_playwright() as p:
        b = panel_browser.launch(p)
        if b is None:
            pytest.skip("headless Chromium cannot run in this environment")
        yield b
        b.close()


def open_tile(page):
    go_radio(page)
    toggle = page.locator(OPEN + " > .compact-toggle")
    toggle.click()
    page.locator(OPEN + " .card.pod").wait_for()


@pytest.mark.parametrize("width", (360, 390, 430))
@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_tile_opens_and_keeps_the_spacing_rule(browser, lang, scheme, width):
    ctx, page, calls, errors, state = open_v2(browser, lang, scheme, width=width, state=rich_state(lang))
    try:
        open_tile(page)
        assert page.locator(OPEN + " .podnow").inner_text().strip() == state["podcast"]["now"]["title"]
        assert page.locator(OPEN + " .podrow").count() == 2
        got = page.evaluate(MEASURE, [BLEED, CARDS + ",.v2-tile .card.pod", GUTTER])
        assert got["overflow"] <= 0 and not got["clipped"] and not got["small"], got
        inner = page.eval_on_selector(OPEN + " .card.pod", "e => { const r = e.getBoundingClientRect(); return [r.left, r.right, getComputedStyle(e).paddingInlineStart]; }")
        assert inner[0] >= GUTTER and page.evaluate("innerWidth") - inner[1] >= GUTTER, inner
        assert 12 <= float(inner[2].replace("px", "")) <= 16, inner
        assert not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
def test_the_tile_actions_post_to_the_podcast_endpoint(browser, lang):
    ctx, page, calls, errors, state = open_v2(browser, lang, state=rich_state(lang))
    try:
        open_tile(page)
        page.locator(OPEN + " .btns button").nth(1).click()           # skip
        page.locator(OPEN + " .btns button").nth(2).click()           # stop
        page.locator(OPEN + " .podrow button").first.click()          # remove
        page.wait_for_timeout(300)
        posts = [b for p, b in calls if p == "/api/podcast"]
        assert {"action": "control", "op": "skip"} in posts and {"action": "control", "op": "stop"} in posts
        assert {"action": "remove", "id": "a" * 16} in posts
        assert not errors
    finally:
        ctx.close()


def test_the_tile_is_absent_when_the_bridge_sends_no_podcast_block(browser):
    state = rich_state("en")
    state.pop("podcast")
    ctx, page, calls, errors, _ = open_v2(browser, "en", state=state)
    try:
        go_radio(page)
        assert page.locator(OPEN).count() == 0 and page.locator("[data-v2-section='radio-actions'] .v2-tile").count() == 2
    finally:
        ctx.close()
