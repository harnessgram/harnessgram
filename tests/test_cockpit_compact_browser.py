"""Phone-sized compact stories, expansion, persistence and critical content visibility."""
import json

import pytest

from ctb import panel_page
import panel_browser
from test_cockpit_overflow_browser import browser  # shared headless Chromium fixture


def rich_usage():
    model = dict(input_tokens=12000, output_tokens=8000, cache_creation_input_tokens=40000,
                 cache_read_input_tokens=140000, estimate_usd=1.25, unpriced_tokens=0)
    zero = {k: 0 for k in model}
    period = dict({k: v * 2 for k, v in model.items()}, tokens=400000, unknown_models=False,
                  models={"claude-opus-4-6": model, "claude-sonnet-4-20250514": model, "unknown": zero})
    return {"today": period, "session": period}


def state():
    return {"now": 1700000000, "topic": {"name": "בדיקת תצוגה", "board": {}},
            "sessions": [{"id": "compact-session", "name": "משימה לדוגמה", "state": "working", "here": True,
                          "age": 30, "context": 12000, "bg": 0, "model": "claude-opus-4-6", "account": "test",
                          "now": "בודק את התצוגה", "usage": rich_usage(), "folder": "/project/source", "branch": "compact"}]}


def open_mock(browser, data, lang="he", scheme="light", storage_failure=False):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=scheme, bypass_csp=True)
    page = ctx.new_page()
    if storage_failure:
        page.add_init_script('Object.defineProperty(window,"localStorage",{get(){throw new Error("unavailable")}})')
    def respond(route):
        if route.request.url.endswith("telegram-web-app.js"):
            route.fulfill(content_type="application/javascript", body=(panel_browser.STUB % json.dumps("test")).replace('colorScheme: "light"', 'colorScheme: "' + scheme + '"'))
        elif route.request.url.endswith("/api/state"):
            route.fulfill(content_type="application/json", body=json.dumps(data))
        else:
            route.fulfill(content_type="text/html", body=panel_page.render_page(lang, "test").decode())
    page.route("**/*", respond)
    page.goto("http://panel.test/")
    page.wait_for_selector('[data-block="usage-compact-session"] > button')
    return ctx, page


def assert_no_overflow(page):
    assert page.evaluate('''() => {
      const width=document.documentElement.clientWidth;
      return document.documentElement.scrollWidth <= width && [...document.querySelectorAll("body *")].every(e=>{
        const s=getComputedStyle(e),r=e.getBoundingClientRect();
        return s.visibility==="hidden"||s.display==="none"||!r.width||!r.height||(r.left>=-1&&r.right<=width+1);
      });
    }''')


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_rich_topic_fits_one_screen_and_expands_with_nested_models(browser, lang, scheme):
    ctx, page = open_mock(browser, state(), lang, scheme)
    try:
        block = page.locator('[data-block="usage-compact-session"]')
        button = block.locator(":scope > button")
        assert button.get_attribute("aria-expanded") == "false"
        assert button.bounding_box()["height"] >= 44
        assert page.locator("#app").bounding_box()["y"] + page.locator("#app").bounding_box()["height"] <= 844
        assert page.evaluate("document.documentElement.scrollHeight") <= 900  # about one screen including safe-area footer
        for figure in (".cost", ".cache", ".cache-ring"):
            assert button.locator(figure).is_visible()
        boxes = button.locator(".compact-figure").all()
        assert len({round(x.bounding_box()["y"]) for x in boxes}) == 1  # one-line story at 390 px
        assert not block.locator('[data-block$="-model-unknown"]').count()
        assert_no_overflow(page)
        button.focus()
        page.keyboard.press("Enter")
        page.wait_for_timeout(700)  # the expand animation must finish before visibility is read
        assert button.get_attribute("aria-expanded") == "true"
        assert block.locator(".kv").first.is_visible()
        model = block.locator(".usage-model").first
        assert model.locator(":scope > button").get_attribute("aria-expanded") == "false"
        assert model.locator(".compact-mini").is_visible()
        model.locator(":scope > button").click()
        page.wait_for_timeout(700)  # the expand animation must finish before visibility is read
        assert model.locator(".kv").first.is_visible()
        assert_no_overflow(page)
        page.reload()
        page.wait_for_selector('[data-block="usage-compact-session"] > button')
        assert button.get_attribute("aria-expanded") == "true"
        assert model.locator(":scope > button").get_attribute("aria-expanded") == "true"
        button.click()
        page.wait_for_timeout(700)  # the expand animation must finish before visibility is read
        assert not block.locator(".kv").first.is_visible()
        assert not model.locator(".kv").first.is_visible()  # a remembered inner block cannot escape its closed parent
        assert_no_overflow(page)
        assert block.locator(".compact-panel").first.evaluate("n=>n.inert")
        page.reload()
        page.wait_for_selector('[data-block="usage-compact-session"] > button')
        assert button.get_attribute("aria-expanded") == "false"
        assert not model.locator(".kv").first.is_visible()
    finally:
        ctx.close()


def test_storage_failure_still_allows_expansion_and_reloads_collapsed(browser):
    ctx, page = open_mock(browser, state(), storage_failure=True)
    try:
        button = page.locator('[data-block="usage-compact-session"] > button')
        assert button.get_attribute("aria-expanded") == "false"
        button.click()
        assert button.get_attribute("aria-expanded") == "true"
        page.reload()
        page.wait_for_selector('[data-block="usage-compact-session"] > button')
        assert button.get_attribute("aria-expanded") == "false"
    finally:
        ctx.close()


def test_long_secondary_lists_fold_but_questions_actions_and_progress_stay_visible(browser):
    data = state()
    data["topic"]["board"]["link"] = [{"text": "מקור " + str(i), "url": "https://example.com/" + str(i)} for i in range(7)]
    data["cards"] = [dict(id="progress", kind="progress", title="התקדמות", detail="בדרך", session="compact-session", here=True,
                          age=3, steps=["שלב ראשון", "שלב שני"], at=1),
                     dict(id="question", kind="ask", title="שאלה חשובה", detail="מה להמשיך?", session="compact-session", here=True, age=2)]
    ctx, page = open_mock(browser, data)
    try:
        assert page.locator(".k-progress .ac-t").inner_text() == "התקדמות"
        assert page.locator(".k-progress .pbar").is_visible()
        assert page.get_by_text("שאלה חשובה", exact=True).is_visible()
        assert page.locator("#question-question textarea").is_visible()
        assert page.locator(".saybox textarea").is_visible()
        links = page.locator('[data-block$="-link"]')
        assert links.locator(":scope > button").get_attribute("aria-expanded") == "false"
        assert not page.get_by_text("מקור 6", exact=True).is_visible()
        links.locator(":scope > button").click()
        page.wait_for_timeout(700)  # the expand animation must finish before visibility is read
        assert page.get_by_text("מקור 6", exact=True).is_visible()
        assert_no_overflow(page)
    finally:
        ctx.close()


def test_home_agents_and_settings_secondary_details_share_the_pattern(browser):
    data = state()
    data["usage_totals"] = {"today": rich_usage()["today"], "week": rich_usage()["session"],
                            "top": [{"name": "Session " + str(i), "today": {"tokens": 100}} for i in range(7)]}
    data["settings"] = {"activity": "normal", "auto_compact": 200, "progress": True, "progress_details": False,
                        "auto_topic_names": True, "away": True, "voice_confirm": True, "delivery_end": True}
    data["mac"] = {"available": True, "status": "warn", "memory": {"available_bytes": 1073741824},
                   "cpu": {"load": [1, 2, 3], "cores": 4}, "disk": {"free_bytes": 1073741824}, "processes": {}}
    ctx, page = open_mock(browser, data)
    try:
        # Classic home: computer health remains in its summary while the diagnostics fold.
        page.locator(".tabs .tab").nth(1).click()
        mac = page.locator('[data-block="mac"]')
        assert mac.locator(":scope > button").get_attribute("aria-expanded") == "false"
        assert not mac.locator(".mac-value").first.is_visible()
        mac.locator(":scope > button").click()
        page.wait_for_timeout(700)  # the expand animation must finish before visibility is read
        assert mac.locator(".mac-value").first.is_visible()
        # Agents: totals precede independently expandable model comparisons and session ranking.
        page.locator(".tabs .tab").nth(2).click()
        usage = page.locator('[data-block="usage-all"]')
        assert usage.locator(":scope > button").get_attribute("aria-expanded") == "false"
        usage.locator(":scope > button").click()
        page.wait_for_timeout(700)  # the expand animation must finish before visibility is read
        assert page.locator('[data-block="usage-top"] > button').get_attribute("aria-expanded") == "false"
        assert not page.get_by_text("Session 6", exact=True).is_visible()
        # Settings: the main activity/compaction controls stay visible, long options expand on demand.
        page.locator(".tabs .tab").nth(3).click()
        options = page.locator('[data-block="settings-options"]')
        assert options.locator(":scope > button").get_attribute("aria-expanded") == "false"
        assert not options.locator(".sw").first.is_visible()
        assert page.locator(".seg.three").first.is_visible()
        options.locator(":scope > button").click()
        page.wait_for_timeout(700)  # the expand animation must finish before visibility is read
        assert options.locator(".sw").first.is_visible()
        assert_no_overflow(page)
    finally:
        ctx.close()
