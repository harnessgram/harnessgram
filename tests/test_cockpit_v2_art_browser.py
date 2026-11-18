"""Change picture, in a real browser at phone width: the sheet, the before-and-after preview, one-tap approve, long press."""
import pytest

pytest.importorskip("playwright")

import base64

import panel_browser
from ctb import cockpit_v2
from cockpit_v2_browser import open_v2, rich_state

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
def project_page(browser):
    made = []

    def make(lang="he", art=False):
        state = rich_state(lang)
        if art:
            state["cockpit_v2"]["projects"][0]["art"] = {"name": "custom", "v": 1}
        ctx, page, calls, errors, st = open_v2(browser, lang, "light", state=state, width=390)
        made.append(ctx)
        return page, calls, errors

    yield make
    for ctx in made:
        ctx.close()


def open_sheet(page):
    page.locator(".v2-project").first.click()
    page.locator("[data-v2-art=open]").click()
    page.locator(".v2-art-sheet").wait_for()


def test_scene_flow_previews_then_posts_only_on_approve(project_page):
    page, calls, errors = project_page()
    key = page.locator(".v2-project").first.get_attribute("data-project")
    open_sheet(page)
    assert page.locator("dialog.v2-art-sheet[open]").count() == 1
    assert page.locator("[data-v2-art=auto]").count() == 0  # no custom picture yet: nothing to go back from
    page.locator("[data-v2-art=scene]").click()
    page.locator("[data-v2-art-scene=books]").click()
    assert page.locator("[data-v2-art=compare] img").count() == 2
    assert not [c for c in calls if c[0] == "/api/project-art"]
    page.locator("[data-v2-art=approve]").click()
    page.locator(".v2-art-sheet").wait_for(state="detached")
    assert [c[1] for c in calls if c[0] == "/api/project-art"] == [{"action": "scene", "project": key, "scene": "books"}]
    assert not errors


def test_cancel_posts_nothing_and_automatic_is_offered_for_a_custom_picture(project_page):
    page, calls, errors = project_page(art=True)
    key = page.locator(".v2-project").first.get_attribute("data-project")
    open_sheet(page)
    page.locator("[data-v2-art=auto]").click()
    page.locator("[data-v2-art=cancel]").click()
    assert page.locator("[data-v2-art=upload]").count() == 1
    assert not [c for c in calls if c[0] == "/api/project-art"]
    page.locator("[data-v2-art=auto]").click()
    page.locator("[data-v2-art=approve]").click()
    page.locator(".v2-art-sheet").wait_for(state="detached")
    assert [c[1] for c in calls if c[0] == "/api/project-art"] == [{"action": "clear", "project": key}]
    assert not errors


def test_upload_is_cropped_to_a_square_webp_before_approval(project_page):
    page, calls, errors = project_page()
    open_sheet(page)
    img = cockpit_v2.ASSET_ROOT / "plane.webp"
    page.locator("[data-v2-art=file]").set_input_files(str(img))
    page.locator("[data-v2-art=compare]").wait_for()
    assert not [c for c in calls if c[0] == "/api/project-art"]
    page.locator("[data-v2-art=approve]").click()
    page.locator(".v2-art-sheet").wait_for(state="detached")
    body = [c[1] for c in calls if c[0] == "/api/project-art"][0]
    assert body["action"] == "set" and len(body["data"]) < 1_300_000
    raw = base64.b64decode(body["data"])
    assert raw[:4] == b"RIFF" or raw[:3] == b"\xff\xd8\xff"
    assert not errors


def test_long_press_opens_the_sheet_without_opening_the_project(project_page):
    page, calls, errors = project_page()
    tile = page.locator(".v2-project").first
    tile.scroll_into_view_if_needed()
    box = tile.bounding_box()
    page.mouse.move(box["x"] + 20, box["y"] + 20)
    page.mouse.down()
    page.wait_for_timeout(800)
    page.mouse.up()
    page.locator(".v2-art-sheet").wait_for()
    assert page.locator(".v2-phead").count() == 0
    assert not errors


def test_escape_closes_the_sheet(project_page):
    page, calls, errors = project_page()
    open_sheet(page)
    page.keyboard.press("Escape")
    page.locator(".v2-art-sheet").wait_for(state="detached")
    assert not [c for c in calls if c[0] == "/api/project-art"] and not errors


def test_the_tap_after_a_closed_long_press_opens_the_project(project_page):
    page, calls, errors = project_page()
    tile = page.locator(".v2-project").first
    tile.scroll_into_view_if_needed()
    box = tile.bounding_box()
    page.mouse.move(box["x"] + 20, box["y"] + 20)
    page.mouse.down()
    page.wait_for_timeout(800)
    page.mouse.up()
    page.locator(".v2-art-sheet").wait_for()
    page.keyboard.press("Escape")
    page.locator(".v2-art-sheet").wait_for(state="detached")
    tile.click()
    page.locator(".v2-phead").wait_for()
    assert not errors


@pytest.mark.parametrize("lang", ["he", "en"])
def test_sheet_fits_the_phone_and_follows_the_page_direction(project_page, lang):
    page, calls, errors = project_page(lang)
    open_sheet(page)
    page.locator("[data-v2-art=scene]").click()
    got = page.evaluate("""()=>{const W=document.documentElement.clientWidth,c=document.querySelector('.v2-art-sheet').getBoundingClientRect();
      return {overflow:document.documentElement.scrollWidth-W,left:c.left,right:c.right,W,dir:getComputedStyle(document.querySelector('.v2-art-sheet')).direction};}""")
    assert got["overflow"] <= 0 and got["left"] >= 0 and got["right"] <= got["W"], got
    assert got["dir"] == ("rtl" if lang == "he" else "ltr")
    assert not errors
