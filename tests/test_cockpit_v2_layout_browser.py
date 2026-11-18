"""Spacing and responsiveness guard for every top-level v2 screen.

At 360, 390, 430, 768 and 1024 px, in Hebrew and English, light and dark, with the crowded fixture
(raw slug names, quiet projects, no art mapping): no horizontal overflow, a gutter of at least 16 px,
no text outside its card, 44 px tap targets and the project grid's column count.
"""
import pytest

pytest.importorskip("playwright")

import panel_browser
from cockpit_v2_browser import crowded_state, needs_state, open_v2
from cockpit_v2_screenshots import go_addons, go_knowledge, go_project, go_projects, go_radio, go_settings

sync_api = pytest.importorskip("playwright.sync_api")

WIDTHS = (360, 390, 430, 768, 1024)
GUTTER = 16
# Deliberately edge to edge: the sky header, page heads, the vault hero, sideways chips and the tab bar.
BLEED = ".top,.v2-page-head,.vk-head,.vk-vault-hero,.vk-chips,#tabs,.tabs"
CARDS = (".v2-card,.v2-project,.v2-pcard,.v2-block,.v2-group,.v2-decision,.v2-needs,.v2-less,.v2-older,.v2-stale-row,"
         ".v2-requests .request-row,.v2-reminders,.v2-changes,.vk-tile,.vk-entry,.va-entry,.vk-group,.vk-row,.vk-search,.v2-app,.v2-yt,"
         ".v2-station,.v2-radio-hero,.v2-seg,.v2-phead,.vk-vault-tile,.vk-history-row,.vk-status,.v2-schedule,.sc-row")

MEASURE = """([bleed,cards,gutter])=>{
 const W=document.documentElement.clientWidth,out={overflow:document.documentElement.scrollWidth-W,gutter:[],clipped:[],small:[],pills:[]};
 const shown=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'
  &&!e.closest('.compact:not(.open)>.compact-reveal,[hidden],dialog:not([open])');};
 const name=e=>(e.className&&e.className.baseVal===undefined?e.className:e.tagName)+':'+(e.textContent||'').trim().slice(0,24);
 // Cards keep the gutter on both sides (inside the page column on wide screens).
 for(const c of document.querySelectorAll('#app '+cards.split(',').join(',#app '))){
  if(!shown(c)||c.closest(bleed))continue;const r=c.getBoundingClientRect();
  if(r.left<gutter-0.5||r.right>W-gutter+0.5)out.gutter.push([name(c),Math.round(r.left),Math.round(W-r.right)]);}
 // Text never spills out of the card that holds it.
 const walker=document.createTreeWalker(document.getElementById('app'),NodeFilter.SHOW_TEXT);let t;
 while((t=walker.nextNode())){const host=t.parentElement;if(!t.textContent.trim()||!host||!shown(host)||host.closest('.vk-chips'))continue;
  const card=host.closest(cards);if(!card||!shown(card))continue;const range=document.createRange();range.selectNodeContents(t);
  const a=card.getBoundingClientRect();for(const r of range.getClientRects()){if(!r.width)continue;
   // Clipped by an ancestor (ellipsis, line clamp) is fine: only what is painted counts.
   let vis={left:r.left,right:r.right,top:r.top,bottom:r.bottom};
   for(let p=host;p&&p!==card.parentElement;p=p.parentElement){const s=getComputedStyle(p);if(s.overflowX!=='visible'||s.overflowY!=='visible'){
    const q=p.getBoundingClientRect();vis={left:Math.max(vis.left,q.left),right:Math.min(vis.right,q.right),top:Math.max(vis.top,q.top),bottom:Math.min(vis.bottom,q.bottom)};}}
   if(vis.right<=vis.left||vis.bottom<=vis.top)continue;
   if(vis.left<a.left-1||vis.right>a.right+1||vis.top<a.top-1||vis.bottom>a.bottom+1){out.clipped.push(name(host));break;}}}
 // Tap targets: 44 px both ways.
 for(const b of document.querySelectorAll('#app button,#tabs button')){if(!shown(b)||b.closest('.srow,.topic-card'))continue;
  const r=b.getBoundingClientRect();if(r.height<44-0.5||r.width<44-0.5)out.small.push([name(b),Math.round(r.width),Math.round(r.height)]);}
 // Pills and chips stay on one line (a status word never breaks mid-word), and a card's chip stays inside its column.
 for(const c of document.querySelectorAll('#app .v2-yt-state,#app .v2-wait-chip,#app .v2-status-chip')){if(!shown(c))continue;
  const r=c.getBoundingClientRect(),lh=parseFloat(getComputedStyle(c).lineHeight)||parseFloat(getComputedStyle(c).fontSize)*1.3;
  if(r.height>lh*1.6+8)out.pills.push([name(c),Math.round(r.height)]);
  const col=c.closest('.v2-pcard-body');if(col){const q=col.getBoundingClientRect();if(r.left<q.left-1||r.right>q.right+1)out.pills.push([name(c),'outside its column']);}}
 return out;}"""

GRID = """()=>{const tiles=[...document.querySelectorAll('[data-v2-section="projects"]>.v2-project')],first=tiles[0].getBoundingClientRect().top;
 const row=tiles.filter(t=>Math.abs(t.getBoundingClientRect().top-first)<1);
 return {columns:row.length,widths:row.map(t=>t.getBoundingClientRect().width),heights:row.map(t=>t.getBoundingClientRect().height),
  names:tiles.map(t=>{const n=t.querySelector('.v2-project-name'),s=getComputedStyle(n);return [n.textContent,n.getBoundingClientRect().height/parseFloat(s.lineHeight)];}),
  overlap:tiles.filter(t=>{const a=t.querySelector('.v2-project-art img').getBoundingClientRect(),b=t.querySelector('.v2-progress').getBoundingClientRect();
   return a.left<b.right&&b.left<a.right&&a.top<b.bottom&&b.top<a.bottom;}).length,
  main:tiles.map(t=>t.dataset.project),quiet:[...document.querySelectorAll('[data-v2-section="quiet-projects"] .v2-project')].map(t=>t.dataset.project)};}"""


@pytest.fixture(scope="module")
def browser():
    panel_browser.prepare_env()
    with sync_api.sync_playwright() as p:
        b = panel_browser.launch(p)
        if b is None:
            pytest.skip("headless Chromium cannot run in this environment")
        yield b
        b.close()


def check(page, screen):
    got = page.evaluate(MEASURE, [BLEED, CARDS, GUTTER])
    assert got["overflow"] <= 0, (screen, "horizontal overflow", got["overflow"])
    assert not got["gutter"], (screen, "gutter under %dpx" % GUTTER, got["gutter"][:6])
    assert not got["clipped"], (screen, "text outside its card", got["clipped"][:6])
    assert not got["small"], (screen, "tap target under 44px", got["small"][:6])
    assert not got["pills"], (screen, "pill wraps or leaves its column", got["pills"][:6])


@pytest.mark.parametrize("width", [360, 390, 768])
@pytest.mark.parametrize("lang", ["he", "en"])
def test_long_requests_clamp_expand_and_keep_actions_below_on_phones(browser, width, lang):
    state = needs_state(lang)
    base = state["requests"][0]
    # Cover the ordinary preview/overflow lists as well as owner requests and history.
    state["requests"] += [dict(base, id=i) for i in range(30, 36)]
    state["requests"].append(dict(base, id=90, status="done"))
    title = ("בקשה ארוכה מאוד עם פרטים רבים לבדיקה לפני שממשיכים בעבודה. " * 30
             + "מזההשלאניתןלפצל" * 20)
    for row in state["requests"]:
        row.update(title=title, project="פרויקט עם שם ארוך מאוד לבדיקה " * 8)
    ctx, page, calls, errors, _ = open_v2(browser, lang, state=state, width=width)
    try:
        page.evaluate("document.fonts.ready")
        for fold in ('.v2-needs [data-v2-needs-group="owner-requests"] .v2-needs-more',
                     '[data-block="requests-agents"]', '[data-v2-section="dormant"]', '[data-v2-section="older"]'):
            page.locator(fold + ' > .compact-toggle').click()
        for selector in ('.v2-needs [data-v2-request]', '.v2-needs-more [data-v2-request]',
                         '[data-v2-section="requests"] [data-v2-request]',
                         '[data-block="requests-agents"] [data-v2-request]',
                         '[data-v2-section="dormant"] [data-v2-request]',
                         '[data-v2-section="older"] [data-v2-request]'):
            row = page.locator(selector).first
            toggle, text = row.locator('.request-toggle'), row.locator('.nm')
            assert toggle.get_attribute('aria-expanded') == 'false'
            assert row.locator('.request-disclosure').inner_text() == ('עוד' if lang == 'he' else 'More')
            size = text.evaluate("""e => ({height:e.getBoundingClientRect().height,
                line:parseFloat(getComputedStyle(e).lineHeight),clamp:getComputedStyle(e).webkitLineClamp,
                clipped:e.scrollHeight>e.clientHeight})""")
            assert size['clamp'] == '3' and size['clipped'], (selector, size)
            assert abs(size['height'] - 3 * size['line']) < 1, (selector, size)
            # Project, chip and age share a single metadata line, even with a long project name.
            assert row.locator('.mt').evaluate("""e => {
                const r=e.getBoundingClientRect();return [...e.children].every(c=>{
                    const b=c.getBoundingClientRect();return b.top>=r.top-1&&b.bottom<=r.bottom+1;
                })&&e.scrollWidth<=e.clientWidth;
            }""")
            if width < 600:
                bounds = toggle.bounding_box()
                for action in row.locator('.request-actions button').all():
                    assert action.bounding_box()['y'] >= bounds['y'] + bounds['height'], selector
                assert abs(bounds['width'] - row.evaluate('e=>e.clientWidth-24')) < 1
            assert row.evaluate('e=>e.scrollWidth<=e.clientWidth')
            check(page, selector)
            toggle.click()
            assert toggle.get_attribute('aria-expanded') == 'true'
            assert row.locator('.request-disclosure').inner_text() == ('פחות' if lang == 'he' else 'Less')
            assert text.bounding_box()['height'] > size['height']
            assert text.evaluate('e=>e.scrollHeight<=e.clientHeight+1')
            check(page, selector + ' expanded')
            toggle.click()
            assert toggle.get_attribute('aria-expanded') == 'false'
            assert abs(text.bounding_box()['height'] - size['height']) < 1
        assert not calls and not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_needs_four_groups_expand_without_duplicates_or_overflow_at_360(browser, lang, scheme):
    state = needs_state(lang)
    # Exercise wrapping for Hebrew, English and an unbroken source identifier.
    long = ("תזכורת ארוכה לבדיקה " if lang == "he" else "A long reminder to review ") * 8 + "x" * 100
    for row in state["reminders"]:
        row["text"] = long
    for row in state["requests"]:
        row["title"] = long
    ctx, page, calls, errors, state = open_v2(browser, lang, scheme, state=state, width=360)
    try:
        zone = page.locator(".v2-needs")
        sizes = {"decisions": 6, "asks": 7, "reminders": 8, "owner-requests": 9}
        selector = "[data-v2-decision],[data-v2-reminder],[data-v2-request]"
        assert zone.locator(".v2-count").inner_text().strip() == "30"
        assert zone.locator(".v2-empty").count() == 0
        check(page, "needs preview")
        for key, n in sizes.items():
            group = zone.locator(f'[data-v2-needs-group="{key}"]')
            assert group.locator(selector).filter(visible=True).count() == 5
            toggle = group.locator(".v2-needs-more > .compact-toggle")
            assert toggle.get_attribute("aria-expanded") == "false"
            toggle.click()
            # Wait for the disclosure's layout transition (bounded; a fixed 250 ms flaked under load).
            for _ in range(40):
                if group.locator(selector).filter(visible=True).count() == n:
                    break
                page.wait_for_timeout(50)
            assert group.locator(selector).filter(visible=True).count() == n
        assert zone.locator(selector).filter(visible=True).count() == state["waiting_count"]
        check(page, "needs expanded")
        assert page.locator("[data-v2-reminder]").count() == 8
        assert page.locator("[data-v2-request]").count() == 14  # nine owner rows, three agents, one dormant, one finished
        zone.locator('.v2-needs-more > .compact-toggle').first.click()
        assert zone.locator('[data-v2-needs-group="decisions"] .v2-needs-more > button').get_attribute("aria-expanded") == "false"
        assert not calls and not errors
    finally:
        ctx.close()


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_every_screen_keeps_gutters_targets_and_text_inside_cards(browser, width, lang, scheme):
    state = crowded_state(lang)
    ctx, page, calls, errors, state = open_v2(browser, lang, scheme, state=state, width=width)
    try:
        page.evaluate("document.fonts.ready")
        check(page, "now")
        grid = page.evaluate(GRID)
        expected = 2 if width < 430 else 3 if width < 700 else None
        if expected:
            assert grid["columns"] == expected, (width, grid["columns"])
        else:
            assert grid["columns"] >= 4, (width, grid["columns"])
        assert min(grid["widths"]) >= (120 if expected == 3 else 150), (width, grid["widths"])
        # One height per row, names never past two lines, the ring never on the picture.
        assert max(grid["heights"]) - min(grid["heights"]) < 1, grid["heights"]
        assert all(lines < 2.2 for _, lines in grid["names"]), grid["names"]
        assert grid["overlap"] == 0
        # Raw slugs read as words; the data key is unchanged.
        assert not any("-" in n or "_" in n for n, _ in grid["names"]), grid["names"]
        # Quiet projects (a day without updates, no progress, nothing waiting) fold below the active ones.
        quiet = sorted(p["key"] for p in state["cockpit_v2"]["projects"]
                       if p["stale"] and p["percent"] is None and not p["waiting"])
        assert quiet and sorted(grid["quiet"]) == quiet and not set(quiet) & set(grid["main"])
        for screen, go in (("projects", go_projects), ("project", go_project), ("tools-radio", go_radio),
                           ("settings", go_settings), ("k-home", go_knowledge), ("addons", go_addons)):
            go(page)
            page.wait_for_timeout(50)
            check(page, screen)
        assert not errors, errors
    finally:
        ctx.close()


def test_quiet_fold_opens_and_its_tiles_open_their_project(browser):
    ctx, page, calls, errors, state = open_v2(browser, "he", "light", state=crowded_state("he"))
    try:
        fold = page.locator('[data-v2-section="quiet-projects"]')
        toggle = fold.locator("> .compact-toggle")
        assert toggle.get_attribute("aria-expanded") == "false"
        toggle.click()
        assert toggle.get_attribute("aria-expanded") == "true"
        tile = fold.locator(".v2-project").first
        key = tile.get_attribute("data-project")
        tile.click()
        assert page.locator(".v2-ppage").get_attribute("data-project") == key
        assert not errors, errors
    finally:
        ctx.close()


def test_deep_link_to_a_question_past_the_first_five_opens_its_fold(browser):
    # Review #434: a question beyond the five-row preview stayed hidden (inert) when opened by link.
    state = needs_state("he")
    ident = state["decisions"][5]["id"]
    ctx, page, calls, errors, state = open_v2(browser, "he", "light", state=state, width=390, start="q" + ident)
    try:
        group = page.locator('.v2-needs [data-v2-needs-group="decisions"]')
        assert group.locator(".v2-needs-more > .compact-toggle").get_attribute("aria-expanded") == "true"
        target = group.locator(f'[data-v2-decision="{ident}"]')
        assert target.is_visible()
        assert target.evaluate("e => !e.closest('[inert],[aria-hidden=true]')")
        assert not errors
    finally:
        ctx.close()
