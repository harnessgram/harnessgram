"""Audit every Cockpit view at phone widths with long, realistic state."""
import json

import pytest
from ctb import panel_page
import panel_browser

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


@pytest.mark.parametrize("width", [360, 390])
@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("edition", ["staff", "mirror"])
def test_phone_views_fit(browser, width, lang, scheme, edition):
    long = "שלום-ChannelName1234567890" * 7
    state = {
        "now": 1700000000,
        "topic": {"name": long, "board": {"link": [{"text": long, "url": "https://example.com/" + long}]}},
        "tasks": [{"text": long, "topic": long, "age": 50}],
        "quota": [{"label": long, "weekly": 45}],
        "bank": [{"name": long, "n": 2, "expires": "2026-12-01", "days": 12}],
        "settings": {"activity": "normal", "auto_compact": 200, "radio_every_reply": True, "radio_board": True, "radio_room": True},
        "youtube": {"edition": edition, "setup": {"client": True, "base": True}, "connected": True,
                    "channel": long, "live": True, "since": 1700000000, "hours_left": 12,
                    "mirror_owner_start": True, "enabled": True},
        "apps": [{"name": "studio", "title": long, "title_he": long, "icon": "film"}],
    }
    # Exercise the shared disclosures with both rich usage and unbroken diagnostic text.
    counts = {"input_tokens": 12000, "output_tokens": 8000, "cache_creation_input_tokens": 40000,
              "cache_read_input_tokens": 140000, "tokens": 200000, "estimate_usd": 1.25}
    usage = dict(counts, models={"claude-opus-4-6": counts,
                               "unknown": {"input_tokens": 0, "output_tokens": 0, "estimate_usd": 0}})
    state["sessions"] = [{"id": "sample", "here": True, "name": long, "state": "working", "age": 50,
                          "context": 123456, "model": "claude-opus-4-6", "account": long,
                          "folder": "/project/" + long, "branch": long, "summary": long,
                          "usage": {"today": usage, "session": usage}}]
    state["usage_totals"] = {"today": usage, "week": usage, "top": []}
    # The Requests list: every section (needs you, agents, long wait, done) with unbroken long titles and project names.
    state["requests"] = [
        dict(project=long[:60], id=n, title=long[:160], title_he=long[:160], status="done" if n == 5 else "open",
             wait="owner" if n < 3 else "team", age=3600, updated=1700000000 - (900000 if n == 4 else 60),
             idle=900000 if n == 4 else 60, long=n == 4)
        for n in range(1, 6)]
    ctx = browser.new_context(viewport={"width": width, "height": 844}, color_scheme=scheme,
                              reduced_motion="reduce", bypass_csp=True)
    page = ctx.new_page()
    def respond(route):
        url = route.request.url
        if url.endswith("telegram-web-app.js"):
            route.fulfill(content_type="application/javascript", body=(panel_browser.STUB % json.dumps("test")).replace('colorScheme: "light"', 'colorScheme: "' + scheme + '"'))
        elif url.endswith("/api/state"):
            route.fulfill(content_type="application/json", body=json.dumps(state))
        elif url.endswith("/api/app-token"):
            panel_browser.app_token_reply(route)
        elif "/app/" in url:
            route.fulfill(content_type="text/html", body="<!doctype html><title>App</title>")
        else:
            route.fulfill(content_type="text/html", body=panel_page.render_page(lang, "test").decode())
    page.route("**/*", respond)
    page.goto("http://panel.test/")
    page.wait_for_selector(".tabs .tab")
    assert page.locator("html").get_attribute("data-theme") == scheme
    for index, view in enumerate(("here", "me", "agents", "settings", "app:studio")):
        page.locator(".tabs .tab").nth(index).click()  # the bar's order; the page's functions are not global
        if view == "app:studio":
            page.wait_for_selector("iframe.appframe")
        else:
            page.wait_for_function("i => document.querySelectorAll('.tabs .tab')[i].getAttribute('aria-selected') === 'true' && document.getElementById('app').children.length > 0", arg=index)
        if view == "me":
            page.wait_for_selector(".requests .request-row")
        if view == "agents":
            assert page.locator(".yt .btns .btn").count() >= 2
            assert page.locator(".yt .ch").first.inner_text()
        if view == "me":
            assert page.locator(".requests .request-row").count() == 5
        if view == "app:studio":
            assert page.locator("iframe.appframe").is_visible()
        for expanded in (False, True):
            if expanded:
                page.evaluate("""() => {
                  document.querySelectorAll('.srow').forEach(n=>n.open=true);
                  document.querySelectorAll('.compact-toggle[aria-expanded="false"]').forEach(n=>n.click());
                  document.querySelectorAll('.request-toggle').forEach(n=>n.click());
                }""")
            problems = page.evaluate("""() => {
              const w = document.documentElement.clientWidth;
              // A designed scroll rail (the Requests project filter) keeps its off-screen items reachable by
              // swiping inside the rail; that is not page overflow while the rail itself fits the screen and no
              // item is wider than the rail. Everything else must sit inside the viewport.
              const inRail = e => {
                const r = e.getBoundingClientRect();
                const rail = e.closest('.requests-filter');
                if (!rail || rail === e) return false;
                const s = getComputedStyle(rail), ar = rail.getBoundingClientRect();
                return (s.overflowX === 'auto' || s.overflowX === 'scroll')
                  && ar.left >= -1 && ar.right <= w + 1 && r.width <= rail.clientWidth + 1;
              };
              const bad = [...document.querySelectorAll('body *')].filter(e => {
                const s = getComputedStyle(e), r = e.getBoundingClientRect();
                return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0
                  && (r.left < -1 || r.right > w + 1) && !inRail(e);
              }).map(e => ({tag:e.tagName, class:e.className.baseVal ?? e.className,
                            text:e.textContent.slice(0, 50), x:e.getBoundingClientRect().x,
                            width:e.getBoundingClientRect().width}));
              return {scroll:document.documentElement.scrollWidth, width:w, bad};
            }""")
            assert problems["scroll"] <= problems["width"], (view, problems)
            assert not problems["bad"], (view, problems)
    ctx.close()
