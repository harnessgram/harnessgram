"""Phone-width he/en topic cards, progress, pagination and detail dismissal."""
import json

import pytest

from ctb import panel_page, topic_cards
import panel_browser
from test_cockpit_overflow_browser import browser
from test_topic_cards import NOW, row


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("brief", [False, True])
@pytest.mark.parametrize("width", [360, 390])
def test_phone_topic_cards(browser, lang, brief, width):
    long = "LongSubject1234567890" * 20
    rows = [
        row(n, title=f"Topic {n}. {long}", **({"title_he": f"נושא {n}. {long}"} if lang == "he" else {}),  # only a Hebrew page is translated
            steps=[dict({"text": "Decided: go #123", "at": NOW - 10}, **({"text_he": "הוחלט להתקדם #123"} if lang == "he" else {})),
                   dict({"text": long, "at": NOW - 5}, **({"text_he": long} if lang == "he" else {}))],
            wait="owner" if n == 1 else "team", status="done" if n == 11 else "open")
        for n in range(1, 12)]
    built = topic_cards.build(rows, NOW)
    data = {"now": NOW, "sessions": [], "topic_cards": topic_cards.slim(built, NOW)}
    if brief:
        data["brief"] = {"headline": {"moving": 0, "stuck": 0, "waiting": 0, "lines": 0}, "since": None,
                         "waiting": [], "lines": [], "noticed": [], "teams": {"working": 0, "unused": []}, "ask": []}
    ctx = browser.new_context(viewport={"width": width, "height": 844}, bypass_csp=True)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    def respond(route):
        if route.request.url.endswith("telegram-web-app.js"):
            route.fulfill(content_type="application/javascript", body=panel_browser.STUB % json.dumps("test"))
        elif route.request.url.endswith("/api/state"):
            route.fulfill(content_type="application/json", body=json.dumps(data))
        elif route.request.url.endswith("/api/topic-detail"):
            key = route.request.post_data_json["key"]
            notes = next(t["notes"] for t in built if t["key"] == key)
            route.fulfill(content_type="application/json", body=json.dumps({"ok": True, "notes": notes}))
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
        assert first.locator(".topic-stage").inner_text() == labels["topicStage_development"]
        assert first.get_by_role("progressbar").get_attribute("aria-valuenow") == "40"
        assert first.evaluate("e => e.scrollWidth <= e.clientWidth")
        assert first.locator(".topic-prs").inner_text() == "#123"
        assert page.evaluate("document.documentElement.scrollWidth <= " + str(width))
        page.get_by_role("button", name=labels["more"], exact=True).click()
        assert page.locator(".topic-card:visible").count() == 10
        first.click()
        sheet = page.get_by_role("dialog")
        assert sheet.is_visible()
        sheet.locator(".topic-timeline li").first.wait_for()  # read when the sheet opens
        assert sheet.locator(".topic-timeline li").count() == 2
        assert sheet.locator(".topic-timeline li").first.inner_text().endswith(long)
        assert sheet.locator("time").count() == 2
        assert sheet.evaluate("e => e.scrollWidth <= e.clientWidth")
        assert page.evaluate("document.documentElement.scrollWidth <= " + str(width))
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


@pytest.mark.parametrize('lang', ['he', 'en'])
def test_compact_project_topics_fit_360px_with_stage_and_wait_chip(browser, lang):
    from cockpit_v2_browser import open_v2, rich_state
    state = rich_state(lang)
    topic = state['topic_cards'][0]
    topic.update(state='owner', stage='development', percent=40, step=1,
                 title='VeryLongSubject1234567890' * 10)
    key = next(p['key'] for p in state['cockpit_v2']['projects'] if topic['key'] in p['topics'])
    ctx, page, _, errors, _ = open_v2(browser, lang, state=state, width=360)
    try:
        page.locator('.v2-project[data-project="%s"]' % key).click()
        card = page.locator('.v2-topics .topic-card:visible').first
        assert card.locator('.topic-stage').inner_text() == panel_page.STRINGS[lang]['topicStage_development']
        assert card.locator('.topic-status').inner_text() == panel_page.STRINGS[lang]['topic_owner']
        assert card.evaluate('e => e.scrollWidth <= e.clientWidth')
        assert page.evaluate('document.documentElement.scrollWidth <= 360')
        assert card.locator('.topic-title').bounding_box()['width'] > 0
        meter, fill = card.locator('.topic-meter'), card.locator('.topic-meter-fill')
        track, value = meter.bounding_box(), fill.bounding_box()
        assert abs(value['width'] / track['width'] - .4) < .02
        edge = 'right' if lang == 'he' else 'left'
        assert abs((value['x'] + value['width'] if edge == 'right' else value['x'])
                   - (track['x'] + track['width'] if edge == 'right' else track['x'])) < 1
        assert not errors
    finally:
        ctx.close()
