"""Real 390px he/en topic cards, pagination, timeline and every dismissal path."""
import json

import pytest

from ctb import panel_page, topic_cards
import panel_browser
from test_cockpit_overflow_browser import browser
from test_topic_cards import NOW, row


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("brief", [False, True])
def test_phone_topic_cards(browser, lang, brief):
    long = "LongSubject1234567890" * 20
    data = {"now": NOW, "sessions": [], "topic_cards": topic_cards.build([
        row(n, title=f"Topic {n}. {long}", title_he=f"נושא {n}. {long}",
            steps=[{"text": "Decided: go #123", "text_he": "הוחלט להתקדם #123", "at": NOW - 10},
                   {"text": long, "text_he": long, "at": NOW - 5}],
            wait="owner" if n == 1 else "team", status="done" if n == 11 else "open")
        for n in range(1, 12)], NOW)}
    if brief:
        data["brief"] = {"headline": {"moving": 0, "stuck": 0, "waiting": 0, "lines": 0}, "since": None,
                         "waiting": [], "lines": [], "noticed": [], "teams": {"working": 0, "unused": []}, "ask": []}
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, bypass_csp=True)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    def respond(route):
        if route.request.url.endswith("telegram-web-app.js"):
            route.fulfill(content_type="application/javascript", body=panel_browser.STUB % json.dumps("test"))
        elif route.request.url.endswith("/api/state"):
            route.fulfill(content_type="application/json", body=json.dumps(data))
        else:
            route.fulfill(content_type="text/html", body=panel_page.render_page(lang, "test").decode())
    page.route("**/*", respond)
    try:
        page.goto("http://panel.test/")
        page.wait_for_selector(".topic-card")
        labels = panel_page.STRINGS[lang]
        assert page.locator("html").get_attribute("dir") == ("rtl" if lang == "he" else "ltr")
        assert page.locator(".topic-card:visible").count() == 8
        assert page.locator(".topics-done > button").get_attribute("aria-expanded") == "false"
        first = page.locator(".topic-card:visible").first
        assert first.locator(".topic-title").inner_text() == ("נושא 1." if lang == "he" else "Topic 1.")
        assert first.locator(".topic-status").inner_text() == labels["topic_owner"]
        assert first.locator(".topic-prs").inner_text() == "#123"
        assert page.evaluate("document.documentElement.scrollWidth <= 390")
        page.get_by_role("button", name=labels["more"], exact=True).click()
        assert page.locator(".topic-card:visible").count() == 10
        first.click()
        sheet = page.get_by_role("dialog")
        assert sheet.is_visible()
        assert sheet.locator(".topic-timeline li").count() == 2
        assert sheet.locator(".topic-timeline li").first.inner_text().endswith(long)
        assert sheet.locator("time").count() == 2
        assert sheet.evaluate("e => e.scrollWidth <= e.clientWidth")
        assert page.evaluate("document.documentElement.scrollWidth <= 390")
        # A state refresh does not discard the open detail sheet.
        page.evaluate("document.getElementById('rf').click()")  # the page's own refresh (its functions are not global)
        page.wait_for_timeout(500)
        assert sheet.is_visible()
        sheet.get_by_role("button", name=labels["topicClose"]).click()
        assert not sheet.is_visible()
        first = page.locator(".topic-card:visible").first
        first.click(); page.keyboard.press("Escape")
        assert not page.get_by_role("dialog").is_visible()
        assert first.evaluate("e => document.activeElement === e")
        first.click(); page.evaluate("window.__backClick()")
        page.wait_for_function("!document.querySelector('.topic-sheet').open")
        page.locator(".topics-done > button").click()
        page.wait_for_timeout(700)
        assert page.locator(".topic-card:visible").count() == 11
        page.locator(".topics-done > button").click()
        assert not errors
    finally:
        ctx.close()
