"""What HarnessGram adds to your agents, at phone widths in a real browser: contract, fit, spacing, taps, undo, back.

Every request is answered by the in-memory fake of /api/addons in cockpit_v2_browser: no bridge and no network."""
import pytest

pytest.importorskip("playwright")

import panel_browser
from cockpit_v2_browser import assert_screen_contract, open_v2, rich_state
from cockpit_v2_screenshots import go_addons

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    panel_browser.prepare_env()
    with sync_api.sync_playwright() as p:
        b = panel_browser.launch(p)
        if b is None:
            pytest.skip("headless Chromium cannot run in this environment")
        yield b
        b.close()


@pytest.fixture
def opened(browser):
    made = []

    def make(lang="he", scheme="light", width=390):
        ctx, page, calls, errors, st = open_v2(browser, lang, scheme, state=rich_state(lang), width=width)
        made.append(ctx)
        go_addons(page)
        return page, calls, errors

    yield make
    for ctx in made:
        ctx.close()


FIT = """()=>{const W=document.documentElement.clientWidth,rows=[...document.querySelectorAll('.va-row')];
 const out=rows.filter(r=>{const q=r.getBoundingClientRect();return q.left<15.5||q.right>W-15.5;}).map(r=>r.dataset.vaKey);
 const groups=[...document.querySelectorAll('.va-group')].map(g=>{const s=getComputedStyle(g);return [parseFloat(s.paddingInlineStart),parseFloat(s.paddingInlineEnd)];});
 const text=rows.map(r=>{const m=r.querySelector('.va-row-main'),s=getComputedStyle(m);return parseFloat(s.paddingInlineStart);});
 const clipped=[...document.querySelectorAll('.va-text')].filter(t=>t.scrollHeight>t.clientHeight+1).length;
 return {overflow:document.documentElement.scrollWidth-W,out,groups,text,clipped};}"""


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [360, 390])
def test_contract_fit_and_spacing(opened, lang, scheme, width):
    page, calls, errors = opened(lang, scheme, width)
    assert_screen_contract(page, "addons")
    got = page.evaluate(FIT)
    assert got["overflow"] <= 0 and not got["out"], got
    assert all(12 <= p <= 16 for pair in got["groups"] for p in pair), got["groups"]
    assert all(12 <= p <= 16 for p in got["text"]), got["text"]
    assert got["clipped"] == 0
    assert page.locator(".va-group").count() == 5
    assert page.locator("html").get_attribute("dir") == ("rtl" if lang == "he" else "ltr")
    assert not errors


def test_switch_and_undo_take_one_tap_each(opened):
    page, calls, errors = opened()
    sw = page.locator('[data-va-toggle="missed_replies"]')
    assert sw.get_attribute("aria-checked") == "true"
    sw.click()
    page.locator(".va-snack").wait_for()
    assert page.locator('[data-va-toggle="missed_replies"]').get_attribute("aria-checked") == "false"
    page.locator('[data-va-undo="missed_replies"]').click()
    page.locator(".va-snack").wait_for(state="detached")
    assert page.locator('[data-va-toggle="missed_replies"]').get_attribute("aria-checked") == "true"
    sets = [b for p, b in calls if p == "/api/addons" and b["action"] == "set"]
    assert sets == [{"action": "set", "key": "missed_replies", "on": False}, {"action": "set", "key": "missed_replies", "on": True}]
    assert page.locator('.va-group[data-va-group="safety"] [role="switch"]').count() == 0
    assert not errors


def test_back_returns_to_settings(opened):
    page, calls, errors = opened()
    page.locator('[data-v2-section="addons"] [data-vk-back]').click()
    page.locator('[data-v2-section="addons-entry"]').wait_for()
    assert page.locator('[data-v2-section="addons"]').count() == 0
    assert not errors
