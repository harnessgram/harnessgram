"""A decision with its whole context in a real browser: no overflow at phone, tablet and desktop widths, pictures
enlarge on tap without answering, and the comment field follows the page direction until it has text."""
import time

import pytest

pytest.importorskip("playwright")

import panel_browser
from cockpit_v2_browser import open_v2, sample_state
from test_cards_images import png


def assert_no_overflow(page):
    """No page-wide horizontal scroll, and nothing inside a decision reaches past the viewport."""
    assert page.evaluate('''() => {
      const width=document.documentElement.clientWidth;
      return document.documentElement.scrollWidth <= width && [...document.querySelectorAll(".v2-decision, .v2-decision *")].every(e=>{
        const s=getComputedStyle(e),r=e.getBoundingClientRect();
        return s.visibility==="hidden"||s.display==="none"||!r.width||!r.height||(r.left>=-1&&r.right<=width+1);
      });
    }''')

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


def with_rich_decision(lang):
    state = sample_state(lang)
    he = lang == "he"
    d = state["decisions"][0]
    d["rich_decision"] = True
    d["detail"] = (("שתי דרכים לשחרר את הגרסה.\n\n- **היום**: עם הבדיקות הקיימות\n- **בשבוע הבא**: אחרי סקירה מלאה\n\n"
                    if he else "Two ways to ship.\n\n- **Today**: with the current checks\n- **Next week**: after a full review\n\n")
                   + "averyveryverylongwordwithoutanyspacesthatmustwrapinsteadofoverflowing" * 3)
    d["images"] = ["a" * 32, "b" * 32, "c" * 32]
    d["links"] = [{"label": "תצוגה מקדימה" if he else "Preview", "url": "https://example.com/" + "p" * 120},
                  {"label": "", "url": "https://docs.example.org/notes"}]
    d["replies"] = [{"text": "מה עם הגיבוי?" if he else "What about the backup?", "age": 60}]
    return state


def open_rich(browser, lang, width):
    ctx, page, calls, errors, state = open_v2(browser, lang, "light", state=with_rich_decision(lang), width=width)
    posted = []

    def route(r):
        path = r.request.url.split("127.0.0.1", 1)[-1].split("?", 1)[0]
        if path == "/api/card-media-token":
            r.fulfill(json={"token": "image-only", "expires": int(time.time()) + 600})
        elif path.startswith("/card-media/"):
            r.fulfill(content_type="image/png", body=png(800, 600))
        elif path == "/api/decide":
            posted.append(r.request.post_data_json)
            r.fulfill(json={"ok": True, "reply": True, "key": "k", "delivery": "queued"})
        else:
            r.fallback()
    page.route("**/*", route)
    page.reload()
    page.locator(".v2-decision .v2-decision-pictures").first.wait_for()
    return ctx, page, errors, posted


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("width", [390, 768, 1280])
def test_the_whole_decision_fits_every_width(browser, lang, width):
    ctx, page, errors, posted = open_rich(browser, lang, width)
    try:
        card = page.locator(".v2-decision").first
        assert card.locator(".v2-rich li").count() == 2 and card.locator(".v2-rich b").count() == 2
        assert card.locator(".v2-decision-picture").count() == 3 and card.locator(".v2-decision-link").count() == 2
        assert card.locator(".v2-reply").count() == 1
        page.wait_for_function("[...document.querySelectorAll('.v2-decision-picture')].every(e=>!e.disabled)")
        assert_no_overflow(page)
        if width == 390:  # the 16px phone gutter
            box = card.bounding_box()
            assert box["x"] >= 15 and box["x"] + box["width"] <= 390 - 15
        for b in card.locator(".v2-decision-link, .v2-comment-only, [data-v2-answer]").all():
            assert b.bounding_box()["height"] >= 44
        assert not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
def test_comment_direction_enlarge_and_reply(browser, lang):
    ctx, page, errors, posted = open_rich(browser, lang, 390)
    try:
        card = page.locator(".v2-decision").first
        ta = card.locator("textarea.v2-comment")
        assert ta.get_attribute("dir") == ("rtl" if lang == "he" else "ltr")
        assert card.locator(".v2-comment-only").is_disabled()
        ta.fill("Please check the logs first")
        assert ta.get_attribute("dir") == "auto"
        page.wait_for_function("[...document.querySelectorAll('.v2-decision-picture')].every(e=>!e.disabled)")
        card.locator(".v2-decision-picture").nth(1).click()
        assert page.locator(".card-image-viewer").is_visible()
        page.keyboard.press("Escape")
        page.wait_for_function("!document.querySelector('.card-image-viewer').open")
        assert posted == []  # enlarging never answers
        card.locator(".v2-comment-only").click()
        page.wait_for_function("document.querySelectorAll('.v2-decision .v2-reply').length===2")
        assert posted == [{"id": posted[0]["id"], "choice": "reply", "note": "Please check the logs first"}]
        assert ta.input_value() == "" and ta.get_attribute("dir") == ("rtl" if lang == "he" else "ltr")
        assert card.locator("[data-v2-answer]").count() == 2  # still open, still answerable
        assert_no_overflow(page)
        assert not errors
    finally:
        ctx.close()
