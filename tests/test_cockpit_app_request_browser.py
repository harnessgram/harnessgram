"""Request an app (Tools > More) at 390 px in a real browser: the tile, the sheet, the answer cards, direction and fit.

All requests are intercepted: no bridge, no approval command and no network is touched."""
import copy
import json

import pytest

pytest.importorskip("playwright")

import panel_browser
from cockpit_v2_browser import open_v2, rich_state

sync_api = pytest.importorskip("playwright.sync_api")

UNTIL = "2026-11-14T14:35:00+02:00"
LINK = "https://apps.apple.com/il/app/some-app/id123456789"


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

    def make(lang="he", scheme="light", configured=True, answers=None):
        state = copy.deepcopy(rich_state(lang))
        if configured:
            state["app_request"] = True
        ctx, page, calls, errors, st = open_v2(browser, lang, scheme, state=state)
        made.append(ctx)
        queue = list(answers or [])

        def route(request):
            calls.append(("/api/app-request", request.request.post_data_json))
            status, body = queue.pop(0) if queue else (200, {"ok": True, "result": {"state": "review"}})
            request.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        page.route("**/api/app-request", route)
        return page, calls, errors

    yield make
    for ctx in made:
        ctx.close()


def go_more(page):
    page.locator('#tabs [data-v2-tab="tools"]').click()
    page.locator('[data-v2-tool="more"]').click()
    page.locator('[data-v2-section="more-tools"]').wait_for()


def approved(until=UNTIL, reason_he="אפליקציית חינוך המתאימה לגיל 4+"):
    return 200, {"ok": True, "result": {"state": "approved", "app": "Duolingo", "bundle": "com.duolingo", "reason": "Education app rated 4+",
                                        "reason_he": reason_he, "until": until}}


def open_sheet(page):
    go_more(page)
    page.locator("[data-v2-app-request]").click()
    page.locator("dialog.ar-sheet[open]").wait_for()


def test_tile_is_hidden_when_the_feature_is_not_configured(opened):
    page, calls, errors = opened(configured=False)
    go_more(page)
    assert page.locator("[data-v2-app-request]").count() == 0
    assert not errors


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_tile_opens_the_sheet_and_everything_fits_at_390(opened, lang, scheme):
    page, calls, errors = opened(lang, scheme)
    go_more(page)
    tile = page.locator("[data-v2-app-request]")
    assert tile.count() == 1 and tile.bounding_box()["height"] >= 44
    assert page.locator('[data-v2-section="more-tools"] > *').first.get_attribute("data-v2-app-request") == "1"
    tile.click()
    sheet = page.locator("dialog.ar-sheet[open]")
    sheet.wait_for()
    box = sheet.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 390 and box["y"] >= 0 and box["y"] + box["height"] <= 844
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    for button in sheet.locator("button").all():
        assert button.bounding_box()["height"] >= 44, button.inner_text()
    if scheme == "dark":
        assert sheet.evaluate("e => getComputedStyle(e).backgroundColor").startswith("rgb(")  # opaque, not see-through
    assert not errors


def test_empty_hebrew_field_takes_the_page_direction_and_text_decides_after(opened):
    page, calls, errors = opened("he")
    open_sheet(page)
    field = page.locator("dialog.ar-sheet textarea")
    assert field.evaluate("t => getComputedStyle(t).direction") == "rtl" and field.get_attribute("dir") == "rtl"
    assert field.get_attribute("placeholder")
    field.fill(LINK)
    assert field.get_attribute("dir") == "auto"
    field.fill("")
    assert field.get_attribute("dir") == "rtl"


def test_send_needs_text_and_shows_the_approved_card_with_local_time(opened):
    page, calls, errors = opened("he", answers=[approved()])
    open_sheet(page)
    send = page.locator(".ar-send")
    assert send.is_disabled()
    page.locator("dialog.ar-sheet textarea").fill("  " + LINK + "  ")
    assert send.is_enabled()
    send.click()
    card = page.locator('.ar-result[data-ar-state="approved"]')
    card.wait_for()
    assert calls[-1] == ("/api/app-request", {"text": LINK})
    expected = page.evaluate("new Intl.DateTimeFormat('he', {timeStyle: 'short'}).format(new Date('%s'))" % UNTIL)
    assert card.locator(".ar-until").inner_text() == "פתוח להתקנה עד " + expected
    assert "Duolingo" in card.locator(".ar-app").inner_text()
    assert "אפליקציית חינוך" in card.locator(".ar-reason").inner_text()
    assert page.locator("dialog.ar-sheet textarea").input_value() == ""
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert not errors


@pytest.mark.parametrize("answer,state", [
    ((200, {"ok": True, "result": {"state": "denied", "app": "Chat", "reason": "Messaging apps are never approved", "reason_he": None}}), "denied"),
    ((200, {"ok": True, "result": {"state": "review", "app": "Game", "reason": "", "reason_he": None}}), "review"),
    ((200, {"ok": True, "result": {"state": "pending", "app": "Maps", "reason": "", "reason_he": None}}), "pending"),
    ((200, {"ok": True, "result": {"state": "error", "error": "timeout"}}), "error"),
    ((400, {"error": "bad request"}), "bad"),
    ((429, {"error": "rate", "retry": 8}), "rate"),
])
def test_every_answer_has_its_own_card(opened, answer, state):
    page, calls, errors = opened("he", answers=[answer])
    open_sheet(page)
    page.locator("dialog.ar-sheet textarea").fill(LINK)
    page.locator(".ar-send").click()
    card = page.locator('.ar-result[data-ar-state="%s"]' % state)
    card.wait_for()
    assert card.locator(".ar-title").inner_text().strip()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    if state == "denied":
        # an untranslated English reason sits on its own line and reads left to right
        reason = card.locator(".ar-reason bdi")
        assert reason.inner_text() == "Messaging apps are never approved" and reason.evaluate("e => getComputedStyle(e).direction") == "ltr"
    if state == "rate":
        assert "8" in card.locator(".ar-title").inner_text()
    assert not errors


def test_close_returns_to_the_tools_screen(opened):
    page, calls, errors = opened("en")
    open_sheet(page)
    page.locator(".ar-sheet .topic-close").click()
    assert page.locator("dialog.ar-sheet[open]").count() == 0
    assert page.locator("[data-v2-app-request]").is_visible()
    assert not errors
