"""Choice cards with a picture per option inside the Cockpit v2 project page, and the legible project header."""
import json
import time

import pytest

pytest.importorskip("playwright")

import panel_browser
from cockpit_v2_browser import open_v2, rich_state
from cockpit_v2_screenshots import go_project
from test_cards_images import png
from test_cockpit_compact_browser import assert_no_overflow

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


def with_choice(lang, count=3):
    state = rich_state(lang)
    base = next(c for c in state["cards"] if c["kind"] == "progress" and c["session"] == "sample00")
    labels = (["כיוון כחול", "כיוון חם", "כיוון בהיר"] if lang == "he" else ["Blue direction", "Warm direction", "Light direction"])[:count]
    state["cards"].append(dict(id="c" * 16, kind="choice", title="איזה כיוון עיצובי?" if lang == "he" else "Which design direction?",
                               detail="", age=30, session=base["session"], topic=base["topic"], here=True, options=labels,
                               images=[chr(97 + i) * 32 for i in range(count)], ended=False,
                               project_key=base["project_key"], project_asset=base["project_asset"]))
    return state


def open_choice(browser, lang, scheme, width=390, count=3):
    ctx, page, calls, errors, state = open_v2(browser, lang, scheme, state=with_choice(lang, count), width=width)
    mints, answers, media = [], [], []

    def route(r):
        url = r.request.url
        path = url.split("127.0.0.1", 1)[-1].split("?", 1)[0]
        if path == "/api/card-media-token":
            mints.append(r.request.post_data_json)
            r.fulfill(json={"token": "image-only", "expires": int(time.time()) + 600})
        elif path.startswith("/card-media/"):
            media.append(url)  # the page left behind by the reload may still ask once with its old token
            r.fulfill(content_type="image/png", body=png(800, 600))
        elif path == "/api/card":
            answers.append(r.request.post_data_json)
            r.fulfill(json={"ok": True, "delivery": "queued", "key": "fixture"})
        else:
            r.fallback()
    page.route("**/*", route)
    page.reload()
    page.locator(".v2-needs").first.wait_for()
    go_project(page)
    page.locator(".v2-ppage .choice-tile").first.wait_for()
    page.media = media
    return ctx, page, errors, mints, answers


def wait_true(page, expression, timeout=5.0):
    end = time.monotonic() + timeout
    while not page.evaluate(expression):
        if time.monotonic() > end:
            raise AssertionError("timed out waiting for " + expression)
        page.wait_for_timeout(50)


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_project_page_shows_each_option_as_an_image_tile(browser, lang, scheme):
    ctx, page, errors, mints, answers = open_choice(browser, lang, scheme)
    try:
        tiles = page.locator(".v2-ppage .choice-tile")
        assert tiles.count() == 3
        assert page.locator("html").get_attribute("dir") == ("rtl" if lang == "he" else "ltr")
        wait_true(page, "[...document.querySelectorAll('.v2-ppage .choice-preview')].every(e=>!e.disabled)")
        assert {m["id"] for m in mints} == {"a" * 32, "b" * 32, "c" * 32}
        wait_true(page, "[...document.querySelectorAll('.v2-ppage .choice-preview img')].every(i=>i.complete&&i.naturalWidth>0)")
        assert all("/card-media/" in u for u in page.media) and any(u.endswith("?auth=image-only") for u in page.media)
        for i, tile in enumerate(tiles.all()):
            assert tile.locator("img").get_attribute("alt") == tile.locator(".choice-label").inner_text()
            assert tile.locator("img").get_attribute("loading") == "lazy"
            assert tile.locator(".copt").bounding_box()["height"] >= 44
            assert tile.locator(".choice-preview").bounding_box()["height"] >= 80
        # Two columns on a 390 px phone: tiles 0 and 1 share a row, in reading order for the page direction.
        a, b = tiles.nth(0).bounding_box(), tiles.nth(1).bounding_box()
        assert abs(a["y"] - b["y"]) < 2 and (a["x"] > b["x"] if lang == "he" else a["x"] < b["x"])
        assert tiles.nth(2).bounding_box()["y"] > a["y"]
        assert_no_overflow(page)
        # A tap on the picture enlarges it and never answers.
        tiles.nth(0).locator(".choice-preview").click()
        assert page.locator(".card-image-viewer").is_visible()
        assert answers == []
        page.keyboard.press("Escape")
        wait_true(page, "!document.querySelector('.card-image-viewer').open")
        tiles.nth(2).locator(".copt").click()
        wait_true(page, "!!document.querySelector('.v2-ppage .done-c')")
        assert answers == [{"id": "c" * 16, "pick": 2}]
        assert not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("width,columns", [(320, 1), (359, 1), (360, 2), (412, 2)])
def test_tiles_use_one_column_under_360_px(browser, width, columns):
    ctx, page, errors, mints, answers = open_choice(browser, "he", "light", width=width, count=2)
    try:
        tiles = page.locator(".v2-ppage .choice-tile")
        one_row = abs(tiles.nth(0).bounding_box()["y"] - tiles.nth(1).bounding_box()["y"]) < 2
        assert one_row == (columns == 2)
        assert_no_overflow(page)
        assert not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_project_header_text_is_legible_over_the_picture(browser, lang, scheme):
    ctx, page, calls, errors, state = open_v2(browser, lang, scheme, state=rich_state(lang))
    try:
        go_project(page)
        head = page.locator(".v2-ppage .v2-phead")
        info = head.evaluate('''h => {
          const c=e=>getComputedStyle(e), t=h.querySelector("h2");
          return {ink:c(t).color, shadow:c(t).textShadow, scrim:getComputedStyle(h.querySelector(".v2-phead-text"),"::before").backgroundImage,
                  over:t.getBoundingClientRect().width>0, width:h.getBoundingClientRect().width};
        }''')
        dark = scheme == "dark"
        assert info["ink"] == ("rgb(245, 248, 255)" if dark else "rgb(11, 23, 48)")
        assert info["shadow"] != "none"
        assert "gradient" in info["scrim"]
        assert head.locator("h2").is_visible() and head.locator("p").count() <= 1
        assert_no_overflow(page)
        assert not errors
    finally:
        ctx.close()


WHITE = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='4' height='4'%3E%3Crect width='4' height='4' fill='white'/%3E%3C/svg%3E"
BLACK = WHITE.replace("fill='white'", "fill='black'")


@pytest.mark.parametrize("width", [390, 600, 860])
@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme,picture", [("dark", WHITE), ("light", BLACK)])
def test_header_text_box_end_keeps_contrast_on_the_worst_picture_at_any_width(browser, width, lang, scheme, picture):
    """Sampled from real pixels: the strip at the text box's end edge, where a fixed-width scrim would have faded out."""
    ctx, page, calls, errors, state = open_v2(browser, lang, scheme, state=rich_state(lang), width=width)
    try:
        go_project(page)
        page.evaluate("src => { const i=document.querySelector('.v2-ppage .v2-phead-bg'); i.removeAttribute('srcset'); i.src=src; }", picture)
        page.wait_for_timeout(200)
        box = page.locator(".v2-ppage .v2-phead-text").bounding_box()
        rtl = lang == "he"
        x = box["x"] + 1 if rtl else box["x"] + box["width"] - 3  # the end edge: the left in RTL, the right in LTR
        shot = page.screenshot(clip={"x": x, "y": box["y"], "width": 2, "height": box["height"]})
        import base64
        ratio = page.evaluate('''async b64 => {
          const img=new Image(); img.src="data:image/png;base64,"+b64; await img.decode();
          const c=document.createElement("canvas"); c.width=img.width; c.height=img.height;
          const g=c.getContext("2d"); g.drawImage(img,0,0); const d=g.getImageData(0,0,c.width,c.height).data;
          const lum=[]; const lin=v=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4);};
          for(let i=0;i<d.length;i+=4)lum.push(0.2126*lin(d[i])+0.7152*lin(d[i+1])+0.0722*lin(d[i+2]));
          lum.sort((a,b)=>a-b); const bg=lum[lum.length>>1];
          const ink=getComputedStyle(document.querySelector(".v2-ppage .v2-phead h2")).color.match(/\\d+/g).map(Number);
          const t=0.2126*lin(ink[0])+0.7152*lin(ink[1])+0.0722*lin(ink[2]);
          return (Math.max(bg,t)+0.05)/(Math.min(bg,t)+0.05);
        }''', base64.b64encode(shot).decode())
        assert ratio >= 4.5, ratio
        assert not errors
    finally:
        ctx.close()
