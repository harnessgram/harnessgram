"""Owner's progress-card regressions on a phone-sized page in both directions."""
import pytest

from ctb import panel_page
from test_cockpit_compact_browser import assert_no_overflow, open_mock, state
from test_cockpit_overflow_browser import browser


@pytest.mark.parametrize("lang", ["he", "en"])
def test_progress_copy_is_translated_timed_and_expandable_at_phone_width(browser, lang):
    data = state()
    original = "Review fixes and back gestures once the browser checks finish successfully. " * 8
    translated = "בודקים את התיקונים ואת מחוות החזרה לאחר שבדיקות הדפדפן יסתיימו בהצלחה. " * 8
    card = dict(id="progress", kind="progress", title="Review", title_he="בדיקה" if lang == "he" else None,
                detail="", session="compact-session", here=True, age=416*60, steps=["Review fixes", "Back gestures"],
                at=1, percent=50, live=original, live_age=416*60, task="Check the UI", old=False, finished=False)
    if lang == "he":
        card.update(steps_he=["בדיקת תיקונים", "מחוות חזרה"], live_he=translated, task_he="בדיקת התצוגה")
    data["sessions"][0].update(now="Review fixes", now_he="בדיקת תיקונים" if lang == "he" else None)
    data["cards"] = [card]
    ctx, page = open_mock(browser, data, lang)
    try:
        strings = panel_page.STRINGS[lang]
        assert ("בדיקת תיקונים" if lang == "he" else "Review fixes") in page.locator(".act").all_text_contents()
        progress = page.locator(".k-progress")
        assert progress.locator(".sl").all_text_contents() == card.get("steps_he", card["steps"])
        statuses = progress.locator(".ac-status").all_text_contents()
        assert any(("שעות" if lang == "he" else "hours") in text for text in statuses)
        assert not any("416" in text for text in statuses)
        toggle = progress.locator(".card-copy-toggle").first
        preview = toggle.locator(".card-copy-preview")
        assert preview.text_content() == card.get("live_he", original)
        assert toggle.get_attribute("aria-expanded") == "false"
        assert toggle.get_attribute("title") == strings["cardTextMore"]
        assert preview.evaluate("e=>getComputedStyle(e).webkitLineClamp") == "3"
        # Layout height, not the bounding box: the card's entry animation briefly scales the box (0.985).
        height = lambda: preview.evaluate("e=>parseFloat(getComputedStyle(e).height)")
        collapsed = height()
        assert preview.evaluate("e=>e.scrollHeight>e.clientHeight")
        toggle.click()
        assert toggle.get_attribute("aria-expanded") == "true"
        assert height() > collapsed
        assert_no_overflow(page)
        toggle.click()
        assert toggle.get_attribute("aria-expanded") == "false"
        assert abs(height() - collapsed) <= 2
        toggle.focus()
        page.keyboard.press("Enter")
        assert toggle.get_attribute("aria-expanded") == "true"
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
def test_finished_and_ended_progress_moves_to_the_existing_collapsed_group(browser, lang):
    data = state()
    base = dict(kind="progress", detail="", session="compact-session", here=True, steps=["Check"], at=1, percent=100)
    data["cards"] = [dict(base, id="recent", title="Recently finished", finished=True, age=1799, old=False),
                     dict(base, id="finished", title="Finished half an hour ago", finished=True, age=1800, old=True),
                     dict(base, id="ended", title="Ended session", ended=True, age=3600),
                     dict(base, id="active", title="Unfinished old work", at=0, percent=20, age=300000, old=True, finished=False)]
    ctx, page = open_mock(browser, data, lang)
    try:
        group = page.locator('[data-block="cards-old"]')
        assert group.locator(":scope > button").get_attribute("aria-expanded") == "false"
        assert page.get_by_text("Recently finished", exact=True).is_visible()
        assert page.get_by_text("Unfinished old work", exact=True).is_visible()
        assert not page.get_by_text("Finished half an hour ago", exact=True).is_visible()
        assert not page.get_by_text("Ended session", exact=True).is_visible()
        # The next state refresh moves the same card, without manual dismissal or a new id.
        data["cards"][0].update(age=1800, old=True)
        page.locator("#rf").click()  # the real refresh path: /api/state is served from the mutated `data`
        page.get_by_text("Recently finished", exact=True).wait_for(state="hidden")
        group.locator(":scope > button").click()
        page.wait_for_timeout(700)
        for title in ("Recently finished", "Finished half an hour ago", "Ended session"):
            assert group.get_by_text(title, exact=True).is_visible()
        assert_no_overflow(page)
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
def test_single_hour_age_has_no_icu_disambiguator(browser, lang):
    data = state()
    data["cards"] = [dict(id="p", kind="progress", title="Review", detail="", session="compact-session", here=True,
                          age=75 * 60, steps=["A", "B"], at=1, percent=50, live="Working", live_age=75 * 60,
                          old=False, finished=False)]
    ctx, page = open_mock(browser, data, lang)
    try:
        assert "(" not in page.locator(".k-progress").first.inner_text()  # Hebrew ICU would print "לפני שעה (1)"
        assert any(("לפני שעה" if lang == "he" else "1 hour ago") in t
                   for t in page.locator(".k-progress .ac-status").all_text_contents())
    finally:
        ctx.close()
