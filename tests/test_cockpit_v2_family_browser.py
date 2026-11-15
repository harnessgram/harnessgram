"""The family board on the Now screen and in its sheet in a real browser: fit at 390 px, the week as tabs or a grid, the
editor, the Shabbat rule and the print view.

Every request is intercepted by a small fake of the bridge's family routes: no bridge and no network."""
import copy

import pytest

pytest.importorskip("playwright")

import panel_browser
from cockpit_v2_browser import family_sample, open_v2, rich_state

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
    """The two routes the board uses, answering from an in-memory view the way family_board.view does."""

    def __init__(self, lang="he", shabbat=False):
        self.view = family_sample(lang, shabbat)
        self.posts = []

    def find(self, ident):
        for col in self.view["days"]:
            for r in col["chores"]:
                if r["id"] == ident:
                    return r
        for key in ("daily", "shop", "meals"):
            for r in self.view[key]:
                if r["id"] == ident:
                    return r

    def __call__(self, path, body):
        if path == "/api/family-view":
            return 200, {"ok": True, "view": self.view}
        self.posts.append(body)
        if body["action"] == "check":
            if self.view["shabbat"]:
                return 409, {"error": "shabbat"}
            self.find(body["id"])["done"] = body["done"]
        elif body["action"] == "add":
            row = {"id": "%012x" % (500 + len(self.posts)), "text": body["text"], "day": body.get("day"), "done": False}
            if body["list"] == "weekly":
                self.view["days"][body["day"]]["chores"].append(row)
            else:
                self.view[body["list"]].append(row)
        elif body["action"] == "edit":
            self.find(body["id"])["text"] = body["text"]
        elif body["action"] == "remove":
            row = self.find(body["id"])
            for col in self.view["days"]:
                if row in col["chores"]:
                    col["chores"].remove(row)
            for key in ("daily", "shop", "meals"):
                if row in self.view[key]:
                    self.view[key].remove(row)
        elif body["action"] == "clear":
            self.view["shop"] = [r for r in self.view["shop"] if not r["done"]]
        return 200, {"ok": True, "view": self.view, "removed": {"id": body.get("id"), "text": "x", "day": None, "list": "shop"}}


@pytest.fixture
def opened(browser):
    made = []

    def make(lang="he", scheme="light", width=390, shabbat=False, height=844):
        state = copy.deepcopy(rich_state(lang))
        bridge = FakeBridge(lang, shabbat)
        ctx, page, calls, errors, st = open_v2(browser, lang, scheme, state=state, width=width, family=bridge, height=height)
        made.append(ctx)
        page.locator(".v2-schedule .fb-block").wait_for()
        page.wait_for_function("()=>!document.querySelector('.fb-block [data-fb-state=loading]')")
        return page, bridge, errors

    yield make
    for ctx in made:
        ctx.close()


FIT = """()=>{const W=document.documentElement.clientWidth,card=document.querySelector('.v2-schedule'),c=card.getBoundingClientRect();
 const small=[...card.querySelectorAll('.fb-block button')].filter(b=>{const r=b.getBoundingClientRect();return r.height<43.5||r.width<43.5;}).map(b=>b.textContent);
 const out=[...card.querySelectorAll('.fb-row')].filter(r=>{const q=r.getBoundingClientRect();return q.left<c.left-0.5||q.right>c.right+0.5;}).length;
 const pad=parseFloat(getComputedStyle(card).paddingInlineStart);
 return {overflow:document.documentElement.scrollWidth-W,left:c.left,right:W-c.right,pad,small,out,rows:card.querySelectorAll('.fb-row').length,
  heads:[...card.querySelectorAll('.fb-sub')].map(h=>h.textContent)};}"""

SHEET_FIT = """()=>{const W=document.documentElement.clientWidth,d=document.querySelector('.fb-sheet'),r=d.getBoundingClientRect();
 const kids=[...d.querySelectorAll('button')].filter(b=>b.getBoundingClientRect().width>0);
 const small=kids.filter(b=>{const q=b.getBoundingClientRect();return q.height<43.5||q.width<43.5;}).map(b=>b.textContent||b.className);
 const wide=[...d.querySelectorAll('*')].filter(e=>e.getBoundingClientRect().width>0&&(e.getBoundingClientRect().right>r.right+0.5||e.getBoundingClientRect().left<r.left-0.5)).length;
 return {overflow:document.documentElement.scrollWidth-W,left:r.left,right:W-r.right,pad:parseFloat(getComputedStyle(d).paddingInlineStart),small,wide,scrollW:d.scrollWidth-d.clientWidth};}"""


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width", [360, 390])
def test_the_block_fits_inside_the_card(opened, lang, scheme, width):
    page, bridge, errors = opened(lang, scheme, width)
    got = page.evaluate(FIT)
    assert got["overflow"] <= 0 and got["left"] >= 15.5 and got["right"] >= 15.5
    assert 12 <= got["pad"] <= 16 and got["small"] == [] and got["out"] == 0 and got["rows"] == 1 + 5
    assert len(got["heads"]) == 2
    assert errors == []


def test_ticking_a_row_takes_one_tap_and_undoing_one_more(opened):
    page, bridge, errors = opened()
    first = page.locator(".fb-block .fb-row:not(.done) [data-fb-done]").first
    ident = first.get_attribute("data-fb-done")
    first.click()
    page.wait_for_function("(i)=>document.querySelector('[data-fb-done=\"'+i+'\"]').getAttribute('aria-pressed')==='true'", arg=ident)
    assert bridge.posts[-1] == {"action": "check", "id": ident, "done": True}
    page.locator("[data-fb-done=\"%s\"]" % ident).click()
    page.wait_for_function("(i)=>document.querySelector('[data-fb-done=\"'+i+'\"]').getAttribute('aria-pressed')==='false'", arg=ident)
    assert bridge.posts[-1]["done"] is False


def test_on_shabbat_the_card_shows_a_line_and_no_rows(opened):
    page, bridge, errors = opened(shabbat=True)
    assert page.locator(".fb-block .fb-row").count() == 0 and "שבת שלום" in page.locator(".fb-block .fb-note").inner_text()
    assert page.evaluate(FIT)["overflow"] <= 0


@pytest.mark.parametrize("lang,scheme", [("he", "light"), ("en", "dark")])
def test_the_sheet_fits_at_390_and_tabs_show_one_day_at_a_time(opened, lang, scheme):
    page, bridge, errors = opened(lang, scheme)
    page.locator("[data-fb=open]").click()
    page.locator(".fb-sheet .fb-day.on").wait_for()
    got = page.evaluate(SHEET_FIT)
    assert got["overflow"] <= 0 and got["small"] == [] and got["wide"] == 0 and got["scrollW"] <= 0
    assert min(got["left"], got["right"]) >= 11 and 12 <= got["pad"] <= 16
    visible = page.evaluate("()=>[...document.querySelectorAll('.fb-day')].filter(d=>d.getBoundingClientRect().height>0).length")
    assert visible == 1 and page.locator(".fb-daytab").count() == 7
    page.locator("[data-fb-daytab=\"6\"]").click()
    assert page.locator(".fb-day.on .fb-dayname").inner_text().startswith("מוצאי שבת" if lang == "he" else "After Shabbat")
    for tab in ("daily", "shop", "meals"):
        page.locator("[data-fb-tab=%s]" % tab).click()
        got = page.evaluate(SHEET_FIT)
        assert got["overflow"] <= 0 and got["small"] == [] and got["wide"] == 0, tab
    assert errors == []


def test_on_a_wide_screen_the_week_is_a_grid_without_day_tabs(opened):
    page, bridge, errors = opened(width=1024)
    page.locator("[data-fb=open]").click()
    page.locator(".fb-sheet .fb-day").first.wait_for()
    got = page.evaluate("""()=>({tabs:[...document.querySelectorAll('.fb-daytab')].filter(t=>t.getBoundingClientRect().height>0).length,
      days:[...document.querySelectorAll('.fb-day')].filter(d=>d.getBoundingClientRect().height>0).length,
      cols:new Set([...document.querySelectorAll('.fb-day')].map(d=>Math.round(d.getBoundingClientRect().left))).size,
      overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth})""")
    assert got["tabs"] == 0 and got["days"] == 7 and got["cols"] >= 3 and got["overflow"] <= 0


def test_add_rename_delete_and_undo_through_the_sheet(opened):
    page, bridge, errors = opened()
    page.locator("[data-fb=open]").click()
    page.locator("[data-fb-add=\"weekly:3\"]").click()
    field = page.locator("[data-fb=field]")
    assert field.evaluate("e=>getComputedStyle(e).direction") == "rtl"        # an empty field takes the page direction
    field.fill("להשקות עציצים")
    page.locator("[data-fb=save]").click()
    page.locator(".fb-day.on .fb-text", has_text="להשקות עציצים").wait_for()
    assert bridge.posts[-1] == {"action": "add", "list": "weekly", "text": "להשקות עציצים", "day": 3}
    page.locator("[data-fb-tab=shop]").click()
    page.locator("[data-fb-edit]").first.click()
    page.locator("[data-fb=field]").fill("ירקות טריים")
    page.locator("[data-fb=save]").click()
    page.locator(".fb-text", has_text="ירקות טריים").wait_for()
    page.locator("[data-fb-edit]").first.click()
    page.locator("[data-fb=delete]").click()
    page.locator(".fb-undo:not([hidden])").wait_for()
    assert bridge.posts[-1]["action"] == "remove"
    page.locator("[data-fb=undo]").click()
    page.wait_for_function("()=>document.querySelector('.fb-undo').hidden")
    assert bridge.posts[-1]["action"] == "add" and bridge.posts[-1]["list"] == "shop"
    assert errors == []


def test_clear_checked_removes_the_ticked_shopping_items(opened):
    page, bridge, errors = opened()
    page.locator("[data-fb=open]").click()
    page.locator("[data-fb-tab=shop]").click()
    before = page.locator(".fb-sheet .fb-row").count()
    page.locator("[data-fb=clear]").click()
    page.wait_for_function("(n)=>document.querySelectorAll('.fb-sheet .fb-row').length===n-1", arg=before)
    assert bridge.posts[-1] == {"action": "clear"} and page.locator("[data-fb=clear]").is_disabled()


def test_escape_closes_the_sheet(opened):
    page, bridge, errors = opened()
    page.locator("[data-fb=open]").click()
    page.locator(".fb-sheet").wait_for()
    page.keyboard.press("Escape")
    page.wait_for_function("()=>!document.querySelector('.fb-sheet').open")


@pytest.mark.parametrize("lang", ["he", "en"])
def test_the_print_view_is_one_hebrew_a4_page_with_everything_else_hidden(opened, lang):
    page, bridge, errors = opened(lang)
    page.evaluate("()=>{window.__prints=0;window.print=()=>{window.__prints++;};}")
    page.locator("[data-fb=open]").click()
    page.locator("[data-fb=print]").click()
    assert page.evaluate("()=>window.__prints") == 1
    page.emulate_media(media="print")
    got = page.evaluate("""()=>{const p=document.getElementById('fb-print'),vis=e=>getComputedStyle(e).display!=='none';
      return {page:vis(p),dir:getComputedStyle(p).direction,head:p.querySelector('h1').textContent,boxes:p.querySelectorAll('.fbp-box').length,
       imgs:p.querySelectorAll('img,svg,canvas').length,others:[...document.body.children].filter(c=>c!==p&&vis(c)).length,
       overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth,
       latin:/[A-Za-z]/.test(p.textContent.replace(/\\d+/g,''))}}""")
    assert got["page"] and got["dir"] == "rtl" and got["head"] == "הלוח המשפחתי שלנו" and got["boxes"] == 10
    assert got["imgs"] == 0 and got["others"] == 0 and got["latin"] is False
    page.emulate_media(media="screen")
    page.evaluate("()=>window.dispatchEvent(new Event('afterprint'))")
    assert page.evaluate("()=>!document.getElementById('fb-print')&&!document.body.classList.contains('fb-printing')")
    assert errors == []


def _pdf_pages(data):
    import re
    return len(re.findall(rb"/Type\s*/Page[^s]", data))


def test_the_standalone_print_page_is_two_a4_pages_with_the_default_lists(browser):
    from ctb import family_print
    ctx = browser.new_context(viewport={"width": 794, "height": 1123})
    page = ctx.new_page()
    page.set_content(family_print.render(family_sample("he"), "n").decode())
    pdf = page.pdf(format="A4", prefer_css_page_size=True)
    assert _pdf_pages(pdf) == 2
    assert page.evaluate("()=>getComputedStyle(document.querySelector('.low')).breakBefore") == "page"
    ctx.close()


def test_the_standalone_print_page_with_full_lists_still_prints_and_stays_on_the_page(browser):
    from ctb import family_print
    view = family_sample("he")
    long = "ניקיון יסודי " * 8
    for col in view["days"]:
        col["chores"] = [{"id": "%012x" % (i + 7 * col["day"]), "text": long, "day": col["day"], "done": False} for i in range(4)]
    view["shop"] = [{"id": "%012x" % (900 + i), "text": "פריט קניות %d" % i, "day": None, "done": False} for i in range(80)]
    ctx = browser.new_context(viewport={"width": 794, "height": 1123})
    page = ctx.new_page()
    page.set_content(family_print.render(view, "n").decode())
    assert page.evaluate("()=>document.documentElement.scrollWidth-document.documentElement.clientWidth") <= 0
    assert 2 <= _pdf_pages(page.pdf(format="A4", prefer_css_page_size=True)) <= 5
    ctx.close()


def test_the_standalone_print_page_fits_a_phone(browser):
    from ctb import family_print
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.set_content(family_print.render(family_sample("he"), "n").decode())
    got = page.evaluate("()=>({o:document.documentElement.scrollWidth-document.documentElement.clientWidth,"
                        "pad:parseFloat(getComputedStyle(document.body).paddingInlineStart),btn:document.getElementById('print').getBoundingClientRect().height})")
    assert got["o"] <= 0 and got["pad"] >= 16 and got["btn"] >= 44
    ctx.close()
