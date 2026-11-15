"""Must-keep contract at 390 px, real browser interactions and both locales."""
import pytest

import panel_browser
from cockpit_v2_browser import assert_contract, open_v2, sample_state

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


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_must_keep_phone_contract(browser, lang, scheme):
    ctx, page, calls, errors, state = open_v2(browser, lang, scheme)
    try:
        assert_contract(page, state)
        assert page.locator('.v2-mode-row').count() == 0
        assert not errors
        page.locator("#radio-player .v2-radio-art").wait_for()
        metadata = page.evaluate("navigator.mediaSession.metadata && {title:navigator.mediaSession.metadata.title,artist:navigator.mediaSession.metadata.artist,artwork:navigator.mediaSession.metadata.artwork}")
        assert metadata["title"] == "רדיו הצוות" and metadata["artist"] == "Harnessgram"
        assert metadata["artwork"][0]["src"].endswith("/assets/cockpit/v1/artwork.webp")
        page.locator("#v2-exit").click()
        assert page.evaluate("window.__closed === true")
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("choice", ["yes", "no"])
def test_answer_old_or_new_decision_preserves_exact_target(browser, lang, choice):
    ctx, page, calls, errors, state = open_v2(browser, lang)
    try:
        old_id = "3" * 16
        row = page.locator('[data-v2-decision="%s"]' % old_id)  # an old but open decision
        row.locator('.v2-picture button').click()
        field = row.locator('textarea')
        assert field.is_visible()
        assert field.get_attribute('dir') == ('rtl' if lang == 'he' else 'ltr')
        field.fill("הערת בדיקה")
        row.locator('[data-v2-answer="%s"]' % choice).click()
        row.wait_for(state="detached")
        assert ("/api/decide", {"id": old_id, "choice": choice, "note": "הערת בדיקה"}) in calls
        assert not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
def test_full_decision_detail_is_visible_before_answers_even_when_more_is_closed(browser, lang):
    state = sample_state(lang)
    detail = ("תנאי חשוב לפני האישור.\n" if lang == "he" else "Important approval condition.\n") * 40
    detail += "Final condition: publication needs separate review."
    state["decisions"][0]["detail"] = detail
    ctx, page, calls, errors, _ = open_v2(browser, lang, state=state)
    try:
        row = page.locator('[data-v2-decision="%s"]' % state["decisions"][0]["id"])
        for tap in range(3):  # initial, expanded, collapsed
            if tap:
                row.locator('.v2-picture button').click()
            text = row.locator('.detail')
            assert text.is_visible()
            assert text.text_content() == detail
            bounds = text.bounding_box()
            assert bounds['y'] + bounds['height'] <= row.locator('.v2-answer-buttons').bounding_box()['y']
            assert text.evaluate('(el) => el.scrollHeight <= el.clientHeight')
        assert not calls and not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("action,index", [("done", 0), ("drop", 1), ("push", 2)])
def test_request_actions_require_second_tap_and_keep_project_identity(browser, action, index):
    ctx, page, calls, errors, state = open_v2(browser)
    try:
        row = page.locator('[data-v2-section="requests"] [data-v2-request]').first
        b = row.locator('.request-actions button').nth(index)
        b.click()
        assert not calls
        b.click()
        page.wait_for_function("() => document.querySelectorAll('[data-v2-section=\"requests\"] [data-v2-request]').length === 2")
        assert calls == [("/api/request", {"project": "תכנון", "id": 1, "action": action})]
        assert not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
def test_navigation_settings_escape_and_no_forced_tab_switch(browser, lang):
    ctx, page, calls, errors, state = open_v2(browser, lang)
    try:
        page.locator('[data-v2-tab="settings"]').click()
        page.locator('.v2-mode-row').wait_for()
        assert page.locator('#v2-exit').is_visible()
        # A state poll bringing a new decision must not move an owner out of Settings.
        page.clock.install()
        extra = dict(state["decisions"][0], id="4" * 16)
        state["decisions"].append(extra)
        page.clock.run_for(21000)
        assert page.locator('[data-v2-tab="settings"]').get_attribute('aria-selected') == 'true'
        # A decision may also arrive between the last poll and the rollback.
        state["decisions"].append(dict(extra, id="5" * 16))
        page.locator('[data-v2-mode="off"]').click()
        page.wait_for_function("() => !document.documentElement.classList.contains('cockpit-v2')")
        assert page.locator('.v2-needs').count() == 0
        assert page.locator('[data-v2-mode="off"]').get_attribute('aria-pressed') == 'true'
        assert page.locator('[data-v2-mode]').count() == 3
        page.locator('[data-v2-mode="preview"]').click()
        page.wait_for_function("() => document.documentElement.classList.contains('cockpit-v2')")
        assert calls[-2:] == [("/api/setting", {"name": "ui_v2", "value": value})
                              for value in ("off", "preview")]
        page.locator('[data-v2-tab="me"]').click()
        page.locator('[data-v2-section="older"] > button').click()
        assert page.locator('[data-v2-section="older"]').get_attribute('class').endswith(' open')
        page.locator('[data-v2-section="older"] > button').click()
        assert not page.locator('[data-v2-section="older"]').get_attribute('class').endswith(' open')
        assert not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("mode", ["off", "preview"])
def test_tabs_stay_at_viewport_bottom_with_safe_area_while_scrolling(browser, lang, mode):
    ctx, page, _, errors, _ = open_v2(browser, lang)
    try:
        if mode == "off":
            page.locator('[data-v2-tab="settings"]').click()
            page.locator('[data-v2-mode="off"]').click()
            page.wait_for_function("() => !document.documentElement.classList.contains('cockpit-v2')")
            page.locator('#tabs [role="tab"]').first.click()
        # Telegram supplies this separately from the browser's safe-area env().
        page.evaluate("document.documentElement.style.setProperty('--tg-safe-area-inset-bottom', '28px')")
        for height in (844, 640):
            page.set_viewport_size({"width": 390, "height": height})
            for fraction in (0, 0.5, 1):
                geometry = page.evaluate("""fraction => {
                    window.scrollTo(0, (document.documentElement.scrollHeight - innerHeight) * fraction);
                    const nav = document.querySelector('#tabs .tabs'), rect = nav.getBoundingClientRect();
                    return {position: getComputedStyle(nav).position, bottom: rect.bottom,
                            height: rect.height, viewport: innerHeight,
                            safePadding: parseFloat(getComputedStyle(nav).paddingBottom),
                            contentPadding: parseFloat(getComputedStyle(document.body).paddingBottom)};
                }""", fraction)
                assert geometry['position'] == 'fixed'
                assert geometry['bottom'] == pytest.approx(geometry['viewport'], abs=1)
                assert geometry['safePadding'] >= 28
                assert geometry['contentPadding'] >= geometry['height']
        assert not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
def test_long_content_and_empty_state_do_not_overflow(browser, lang):
    state = sample_state(lang)
    long = "משפטארוךVeryLongWord123" * 45
    for d in state['decisions']:
        d.update(title=long, detail=long, topic=long, yes=long, no=long)
    for r in state['requests']:
        r['title'] = long
    ctx, page, _, errors, _ = open_v2(browser, lang, state=state)
    try:
        assert page.evaluate('document.documentElement.scrollWidth <= 390')
        for button in page.locator('.v2-answer-buttons button').all():
            assert button.bounding_box()['height'] >= 44
        assert not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
def test_stale_decisions_fold_away_and_can_be_removed_at_390(browser, lang):
    state = sample_state(lang)
    for ident, why in (("a" * 16, "ended"), ("b" * 16, "replaced")):
        state["decisions"].append(dict(id=ident, topic="Plan: task", title="Old " + why, detail="", yes="", no="", age=9000,
                                       old=False, stale=why, url=""))
    ctx, page, calls, errors, state = open_v2(browser, lang, state=state)
    try:
        assert page.locator(".v2-needs .v2-count").inner_text().strip() in ("3", "\u200f3")
        assert page.locator(".v2-needs [data-v2-decision]").count() == 3
        fold = page.locator(".v2-stale-fold")
        fold.locator(".compact-toggle").click()
        assert fold.locator(".v2-stale-row").count() == 2
        assert not page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
        fold.locator('[data-v2-dismiss="%s"]' % ("a" * 16)).click()
        page.wait_for_function("document.querySelectorAll('.v2-stale-row').length === 1")
        fold.locator("[data-v2-dismiss=all]").click()
        page.wait_for_function("document.querySelectorAll('.v2-stale-fold').length === 0")
        assert [c for c in calls if c[0] == "/api/dismiss"] == [("/api/dismiss", {"ids": ["a" * 16]}), ("/api/dismiss", {"ids": ["b" * 16]})]
        assert not errors
    finally:
        ctx.close()
