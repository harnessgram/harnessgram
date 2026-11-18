"""The Work map (project page) and the Good morning card (Now) at 390 px in a real browser: fit, gutter, padding,
tap targets, direction, both languages and themes. Every request is intercepted; no bridge, no ledger, no network."""
import copy

import pytest

pytest.importorskip("playwright")

import panel_browser
from cockpit_v2_browser import open_v2, rich_state, workmap_sample
from cockpit_v2_screenshots import go_project

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

    def make(lang="he", scheme="light", width=390, morning=True, work_map=True, edit=None):
        state = copy.deepcopy(rich_state(lang))
        lines, brief = workmap_sample(lang)
        if work_map:
            for p in state["cockpit_v2"]["projects"]:
                p["work_map"] = lines
        if morning:
            state["morning"] = brief
        if edit:
            edit(state)
        ctx, page, calls, errors, st = open_v2(browser, lang, scheme, state=state, width=width)
        made.append(ctx)
        return page, calls, errors

    yield make
    for ctx in made:
        ctx.close()


FIT = """(sel)=>{const W=document.documentElement.clientWidth,card=document.querySelector(sel),c=card.getBoundingClientRect();
 const pad=parseFloat(getComputedStyle(card).paddingInlineStart);
 const small=[...card.querySelectorAll('button')].filter(b=>{const r=b.getBoundingClientRect();return r.height<43.5||r.width<43.5;}).map(b=>b.textContent);
 const out=[...card.querySelectorAll('.wm-line')].filter(r=>{const q=r.getBoundingClientRect();return q.left<c.left-0.5||q.right>c.right+0.5;}).length;
 return {overflow:document.documentElement.scrollWidth-W,left:c.left,right:W-c.right,pad,small,out,lines:card.querySelectorAll('.wm-line').length};}"""


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [360, 390])
def test_the_morning_card_fits_with_the_gutter_padding_and_tap_targets(opened, lang, scheme, width):
    page, calls, errors = opened(lang, scheme, width)
    page.locator(".v2-morning").wait_for()
    got = page.evaluate(FIT, ".v2-morning")
    assert got["overflow"] <= 0 and got["left"] >= 15.5 and got["right"] >= 15.5
    assert 12 <= got["pad"] <= 16 and got["small"] == [] and got["out"] == 0 and got["lines"] == 5
    assert page.locator("html").get_attribute("dir") == ("rtl" if lang == "he" else "ltr")
    assert errors == []


def test_the_morning_card_sits_first_on_now_and_dismisses_with_one_tap(opened):
    page, calls, errors = opened()
    order = page.evaluate("()=>[...document.querySelectorAll('#app>section')].map(s=>s.dataset.v2Section)")
    assert order[0] == "morning" and "decisions" in order
    page.locator("[data-wm=dismiss]").click()
    page.wait_for_function("()=>!document.querySelector('.v2-morning')")
    assert ("/api/briefing-dismiss", {}) in calls and errors == []


def test_no_morning_card_without_a_briefing(opened):
    page, calls, errors = opened(morning=False)
    page.locator("[data-v2-section=decisions]").wait_for()
    assert page.locator(".v2-morning").count() == 0


def test_morning_ledger_shows_hebrew_original_and_pending(opened):
    def edit(state):
        morning = state["morning"]
        morning["overnight"][0].update(what="Finished in file.py", what_he="הושלם בקובץ file.py")
        morning["waiting"][0].update(what="Awaiting approval", what_he=None)

    page, _, errors = opened(edit=edit)
    card = page.locator('.v2-morning')
    translated = card.locator('.wm-line').filter(has_text='הושלם בקובץ')
    assert translated.locator('.wm-what').get_attribute('dir') == 'rtl'
    assert '\u2068file.py\u2069' in translated.locator('.wm-what').inner_text()
    translated.get_by_role('button', name='מקור', exact=True).click()
    assert 'Finished in file.py' in translated.locator('.orig').inner_text()
    pending = card.locator('.wm-line').filter(has_text='Awaiting approval')
    assert pending.locator('.tr').count() == 1 and errors == []


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_work_map_fits_on_the_project_page_and_folds_after_six_lines(opened, lang, scheme):
    page, calls, errors = opened(lang, scheme)
    go_project(page)
    card = page.locator('[data-v2-section="project-workmap"]')
    card.wait_for()
    got = page.evaluate(FIT, '[data-v2-section="project-workmap"]')
    assert got["overflow"] <= 0 and got["left"] >= 15.5 and got["right"] >= 15.5
    assert 12 <= got["pad"] <= 16 and got["small"] == [] and got["out"] == 0 and got["lines"] == 6
    heads = card.locator(".wm-head").all_inner_texts()
    assert heads[0] == ("ממתין לתשובתך" if lang == "he" else "Waiting for you") and len(heads) == 2  # the done lines are folded
    card.locator("[data-wm=more]").click()
    page.wait_for_function("()=>document.querySelectorAll('[data-v2-section=project-workmap] .wm-line').length===8")
    assert card.locator(".wm-head").count() == 3 and errors == []


def test_the_work_map_sits_right_after_the_open_decisions_and_stays_inside_the_page(opened):
    page, calls, errors = opened()
    go_project(page)
    order = page.evaluate("()=>[...document.querySelectorAll('.v2-ppage>*')].map(e=>e.dataset.v2Section||e.tagName)")
    assert order[:3] == ["HEADER", "project-needs", "project-workmap"]


def test_a_project_without_lines_says_so_and_the_flag_hides_the_card(opened):
    page, calls, errors = opened(work_map=False)
    go_project(page)
    assert "אין כרגע" in page.locator('[data-v2-section="project-workmap"]').inner_text()
    state = copy.deepcopy(rich_state("he"))
    state["cockpit_v2"]["work_map_off"] = True
    page2, *_ = (lambda r: (r[1],))(open_v2(opened.__wrapped__ if False else page.context.browser, "he", "light", state=state))
    go_project(page2)
    assert page2.locator('[data-v2-section="project-workmap"]').count() == 0
