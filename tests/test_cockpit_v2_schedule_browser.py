"""My schedule on the Now screen at 390 px in a real browser: fit, names, tap targets, direction, the add sheet.

Every request is intercepted by a small fake of the bridge's schedule routes: no bridge, no calendar and no network."""
import copy

import pytest

pytest.importorskip("playwright")

import panel_browser
from cockpit_v2_browser import open_v2, rich_state, schedule_sample

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


class FakeBridge:
    """The two routes the card uses, answering from an in-memory list the way schedule.view does."""

    def __init__(self, lang="he"):
        self.view = schedule_sample(lang)
        self.posts = []

    def __call__(self, path, body):
        if path == "/api/schedule-view":
            return 200, {"ok": True, "view": self.view}
        self.posts.append(body)
        if body["action"] == "done":
            for r in self.view["days"]["today"]:
                if r["id"] == body["id"]:
                    r["done"] = body["done"]
            return 200, {"ok": True, "view": self.view}
        if body["action"] == "add":
            if body["text"] == "full":
                return 409, {"error": "full"}
            row = {"id": "f" * 12, "title": body["text"], "time": None, "done": False, "kind": "own", "late": False, "allday": False}
            self.view["days"]["today"].append(row)
            return 200, {"ok": True, "view": self.view, "item": {"id": row["id"], "title": body["text"], "date": None, "time": None, "done": False}}
        if body["action"] == "google-disconnect":
            self.view["google"] = {"configured": True, "connected": False}
            return 200, {"ok": True, "view": self.view}
        return 400, {"error": "bad request"}


@pytest.fixture
def opened(browser):
    made = []

    def make(lang="he", scheme="light", width=390, bridge=None):
        state = copy.deepcopy(rich_state(lang))
        bridge = bridge or FakeBridge(lang)
        ctx, page, calls, errors, st = open_v2(browser, lang, scheme, state=state, width=width, schedule=bridge)
        made.append(ctx)
        page.locator(".v2-schedule .sc-row").first.wait_for()
        return page, bridge, errors

    yield make
    for ctx in made:
        ctx.close()


FIT = """()=>{const W=document.documentElement.clientWidth,card=document.querySelector('.v2-schedule'),c=card.getBoundingClientRect();
 const pad=parseFloat(getComputedStyle(card).paddingInlineStart);
 const small=[...card.querySelectorAll('button')].filter(b=>{const r=b.getBoundingClientRect();return r.height<43.5||r.width<43.5;}).map(b=>b.textContent);
 const titles=[...card.querySelectorAll('.sc-title')].map(t=>Math.round(t.getBoundingClientRect().height/parseFloat(getComputedStyle(t).lineHeight)));
 const out=[...card.querySelectorAll('.sc-row')].filter(r=>{const q=r.getBoundingClientRect();return q.left<c.left-0.5||q.right>c.right+0.5;}).length;
 return {overflow:document.documentElement.scrollWidth-W,left:c.left,right:W-c.right,pad,small,titles,out,rows:card.querySelectorAll('.sc-row').length};}"""


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [360, 390])
def test_the_card_fits_with_the_gutter_padding_and_two_line_names(opened, lang, scheme, width):
    page, bridge, errors = opened(lang, scheme, width)
    got = page.evaluate(FIT)
    assert got["overflow"] <= 0 and got["left"] >= 16 - 0.5 and got["right"] >= 16 - 0.5
    assert 12 <= got["pad"] <= 16 and got["small"] == [] and got["out"] == 0 and got["rows"] == 5
    assert max(got["titles"]) <= 2          # the long name is cut at two lines
    assert errors == []


def test_the_card_sits_under_the_decisions_and_the_tabs_switch(opened):
    page, bridge, errors = opened()
    order = page.evaluate("()=>[...document.querySelectorAll('#app>section')].map(s=>s.dataset.v2Section)")
    assert order[:2] == ["decisions", "schedule"]
    page.locator("[data-sc-tab=tomorrow]").click()
    assert page.locator(".sc-empty").inner_text().startswith("לוח ההמראות למחר ריק")
    page.locator("[data-sc-tab=today]").click()
    assert page.locator(".sc-row").count() == 5


def test_done_and_undo_take_one_tap_each(opened):
    page, bridge, errors = opened()
    page.locator("[data-sc-done]").first.click()
    page.locator(".sc-row.done").nth(1).wait_for()
    assert bridge.posts[-1] == {"action": "done", "id": "%012x" % 3, "done": True}
    page.locator(".sc-row.done [data-sc-done]").first.click()
    page.wait_for_function("()=>document.querySelectorAll('.sc-row.done').length===1")
    assert bridge.posts[-1]["done"] is False


def test_calendar_rows_are_read_only(opened):
    page, bridge, errors = opened()
    cal = page.locator(".sc-row.cal")
    assert cal.count() == 2 and cal.locator("button").count() == 0


@pytest.mark.parametrize("lang,scheme", [("he", "light"), ("en", "dark")])
def test_the_add_sheet_takes_one_line_and_closes_on_save(opened, lang, scheme):
    page, bridge, errors = opened(lang, scheme)
    page.locator("[data-sc=add]").click()
    field = page.locator("[data-sc=field]")
    field.wait_for()
    assert page.evaluate("()=>document.querySelector('.sc-sheet').open") is True
    # an empty field takes the page's direction, so the Hebrew placeholder sits on the right
    assert field.evaluate("e=>getComputedStyle(e).direction") == ("rtl" if lang == "he" else "ltr")
    box = page.evaluate("()=>{const r=document.querySelector('.sc-sheet').getBoundingClientRect(),W=document.documentElement.clientWidth;return [r.left,W-r.right];}")
    assert min(box) >= 11
    assert field.get_attribute("placeholder").startswith("למשל" if lang == "he" else "For example")
    field.fill("סידור מחר")
    assert field.evaluate("e=>getComputedStyle(e).direction") == "rtl"  # typed text decides
    page.locator("[data-sc=save]").click()
    page.wait_for_function("()=>!document.querySelector('.sc-sheet')||!document.querySelector('.sc-sheet').open")
    assert bridge.posts[-1] == {"action": "add", "text": "סידור מחר"}
    assert page.locator(".sc-row").count() == 6


def test_a_refusal_stays_in_the_sheet(opened):
    page, bridge, errors = opened()
    page.locator("[data-sc=add]").click()
    page.locator("[data-sc=field]").fill("full")
    page.locator("[data-sc=save]").click()
    page.locator(".sc-out:not(:empty)").wait_for()
    assert "מלא" in page.locator(".sc-out").inner_text() and page.evaluate("()=>document.querySelector('.sc-sheet').open")
    page.keyboard.press("Escape")
    page.wait_for_function("()=>!document.querySelector('.sc-sheet').open")


GOOGLE_FIT = """()=>{const card=document.querySelector('.v2-schedule').getBoundingClientRect(),row=document.querySelector('[data-sc-google]');
 const r=row.getBoundingClientRect(),b=row.querySelector('button').getBoundingClientRect(),cs=getComputedStyle(row);
 return {inside:r.left>=card.left-0.5&&r.right<=card.right+0.5,button:b.left>=r.left-0.5&&b.right<=r.right+0.5,
  h:b.height,w:b.width,pad:[parseFloat(cs.paddingInlineStart),parseFloat(cs.paddingBlockStart)],
  dir:getComputedStyle(row.querySelector('.sc-g-name')).direction};}"""


@pytest.mark.parametrize("connected", [False, True])
@pytest.mark.parametrize("lang,scheme", [("he", "light"), ("he", "dark"), ("en", "dark")])
@pytest.mark.parametrize("width", [360, 390, 768, 1280])
def test_the_google_row_fits_on_phone_tablet_and_desktop(opened, connected, lang, scheme, width):
    bridge = FakeBridge(lang)
    bridge.view["google"] = {"configured": True, "connected": connected}
    page, bridge, errors = opened(lang, scheme, width, bridge)
    page.locator("[data-sc-google]").wait_for()
    got, fit = page.evaluate(GOOGLE_FIT), page.evaluate(FIT)
    assert fit["overflow"] <= 0 and fit["small"] == [] and got["inside"] and got["button"]
    assert got["h"] >= 43.5 and got["w"] >= 43.5 and all(12 <= p <= 16 for p in got["pad"])
    assert got["dir"] == ("rtl" if lang == "he" else "ltr") and errors == []


def test_disconnect_takes_one_tap_and_offers_connect_again(opened):
    bridge = FakeBridge()
    bridge.view["google"] = {"configured": True, "connected": True}
    page, bridge, errors = opened(bridge=bridge)
    assert page.locator(".sc-g-state").inner_text() == "מחובר"
    page.locator("[data-sc=google-disconnect]").click()
    page.locator("[data-sc=google-connect]").wait_for()
    assert bridge.posts[-1] == {"action": "google-disconnect"} and errors == []


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("width", [360, 390])
def test_hour_timeline_and_actions_fit_phone_width_without_overflow(opened, lang, width):
    bridge = FakeBridge(lang)
    bridge.view["timeline"] = {"today": {"available": True, "blocks": [
        {"id": "topic1", "title": "נושא ארוך בעברית Mixed project title " * 4, "summary": "תכנון לשעה הקרובה " * 8,
         "start": 480, "end": 540, "current": True, "fixed": False},
        {"id": "topic2", "title": "Topic two", "start": 540, "end": 600, "fixed": False},
        {"id": "anchor", "title": "Appointment", "start": 630, "end": 690, "fixed": True}]}}
    page, bridge, errors = opened(lang=lang, width=width, bridge=bridge)
    page.locator("[data-sc-hour=topic1]").wait_for()
    got = page.evaluate("""()=>({overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth,
      small:[...document.querySelectorAll('.sc-hour-actions button')].filter(b=>b.getBoundingClientRect().height<44||b.getBoundingClientRect().width<44).length,
      anchors:document.querySelectorAll('[data-sc-hour=anchor] button').length})""")
    assert got == {"overflow": 0, "small": 0, "anchors": 0} and errors == []
