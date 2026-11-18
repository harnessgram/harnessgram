"""The Cockpit's podcast card in a real browser at phone width, in both languages: the picker flow (feed, episode,
queue), the link field, the transport buttons, no overflow at 390 px and the spacing rule (gutter >= 16 px, card
padding 12-16 px). The panel is a throwaway loopback server; the feed fetch is replaced. Skipped as a whole when
Playwright or a Chromium build is missing."""
import threading

import pytest

from conftest import GOOD_TOKEN
from ctb import panel, radio, radio_podcast as P
from ctb.config import Config
from test_panel import OWNER, signed
from test_radio_podcast import FEED, rss, shared_checker  # noqa: F401 (the autouse checker stand-in)
import panel_browser

sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

TITLES = {"en": [("Alpha episode with a rather long title that must wrap inside the card on a phone", "ok"), ("Beta", "ok")],
          "he": [("פרק ראשון עם כותרת ארוכה מאוד שצריכה לעטוף בתוך הכרטיס בטלפון", "בסדר"), ("פרק שני", "בסדר")]}


@pytest.fixture(autouse=True)
def no_real_launchctl(monkeypatch):
    monkeypatch.setattr(radio, "job_pid", lambda: None)


@pytest.fixture
def browser():
    panel_browser.prepare_env()
    with sync_api.sync_playwright() as p:
        b = panel_browser.launch(p)
        if b is None:
            pytest.skip("no headless browser is installed")
        yield b
        b.close()


@pytest.fixture(params=["en", "he"])
def lang(request):
    return request.param


@pytest.fixture
def server(lang, monkeypatch):
    data = rss(TITLES[lang])
    monkeypatch.setattr(P, "fetch", lambda url, *a, **k: ("application/rss+xml", data))
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he", ui_language=lang,
                 radio={"every_minutes": 20}, radio_podcasts=[{"name": "Science Hour", "url": FEED}])
    s = panel.PanelServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()
    s.server_close()


def open_card(browser, server, lang):
    ctx, page = panel_browser.open_panel(browser, server, signed(), lang=lang, width=390)
    page.locator("[role=tab]").nth(1).click()  # the agents tab holds the radio area
    page.wait_for_selector(".card.pod")
    return ctx, page


def test_the_card_fits_a_phone_and_follows_the_spacing_rule(browser, server, lang):
    ctx, page = open_card(browser, server, lang)
    try:
        page.click(".card.pod .compact > .compact-toggle")
        page.wait_for_selector(".card.pod .seg button")
        box = page.eval_on_selector(".card.pod", "e => { const r = e.getBoundingClientRect(); return [r.left, r.right]; }")
        assert box[0] >= 16 and 390 - box[1] >= 16, box          # gutter
        pad = page.eval_on_selector(".card.pod", "e => parseFloat(getComputedStyle(e).paddingInlineStart)")
        assert 12 <= pad <= 16, pad
        assert page.evaluate("document.documentElement.scrollWidth") <= 390
        assert page.evaluate("document.body.scrollWidth") <= 390
        assert page.evaluate("document.dir || document.documentElement.dir") == ("rtl" if lang == "he" else "ltr") or lang == "en"
        heights = page.eval_on_selector_all(".card.pod button", "bs => bs.map(b => b.getBoundingClientRect().height)")
        assert heights and min(heights) >= 44, heights
    finally:
        ctx.close()


def test_pick_a_feed_then_an_episode_and_see_it_queued(browser, server, lang):
    ctx, page = open_card(browser, server, lang)
    try:
        page.click(".card.pod .compact > .compact-toggle")
        page.click(".card.pod .seg button")                       # the feed
        page.wait_for_selector(".card.pod .podeps button")
        eps = page.locator(".card.pod .podeps button")
        assert eps.count() == 2
        assert page.evaluate("document.documentElement.scrollWidth") <= 390  # a long title wraps
        eps.nth(0).click()                                        # newest first
        page.wait_for_selector(".card.pod .podrow")
        assert page.locator(".card.pod .podrow").count() == 1
        assert [i["title"] for i in P.view(server.cfg)["queue"]] == [TITLES[lang][1][0]]
    finally:
        ctx.close()


def test_the_episode_list_has_a_way_back_to_the_feeds(browser, server, lang):
    ctx, page = open_card(browser, server, lang)
    try:
        page.click(".card.pod .compact > .compact-toggle")
        page.click(".card.pod .seg button")
        page.wait_for_selector(".card.pod .podeps button")
        page.locator(".card.pod .compact-inner > button").first.click()
        page.wait_for_selector(".card.pod .seg button")
        assert page.locator(".card.pod .podeps").count() == 0
    finally:
        ctx.close()


def test_a_pasted_link_is_queued_and_a_refusal_is_shown(browser, server, lang):
    ctx, page = open_card(browser, server, lang)
    try:
        page.click(".card.pod .compact > .compact-toggle")
        field = page.locator(".card.pod .podlink input")
        field.fill("http://insecure.example.org/feed")
        page.locator(".card.pod .podlink button").click()
        page.wait_for_selector("#toast .bad")
        assert P.view(server.cfg)["queue"] == []
        field = page.locator(".card.pod .podlink input")
        field.fill("https://pods.example.org/feed")
        page.locator(".card.pod .podlink button").click()
        page.wait_for_selector(".card.pod .podrow")
        assert len(P.view(server.cfg)["queue"]) == 1
    finally:
        ctx.close()


def test_the_transport_buttons_act_on_the_queue(browser, server, lang):
    ctx, page = open_card(browser, server, lang)
    try:
        buttons = page.locator(".card.pod .btns button")
        assert buttons.count() == 3 and all(buttons.nth(i).is_disabled() for i in range(3))  # nothing to control yet
        P.add_link(server.cfg, "https://pods.example.org/feed", lambda url: ("application/rss+xml", rss(TITLES[lang])))
        page.reload()
        page.wait_for_selector(".tabs")
        page.locator("[role=tab]").nth(1).click()
        page.wait_for_selector(".card.pod .podrow")
        page.locator(".card.pod .btns button").first.click()      # pause
        page.wait_for_function("true")
        for _ in range(50):
            if P.view(server.cfg)["paused"]:
                break
            page.wait_for_timeout(100)
        assert P.view(server.cfg)["paused"] is True
        page.locator(".card.pod .podrow button").click()          # remove
        for _ in range(50):
            if not P.view(server.cfg)["queue"]:
                break
            page.wait_for_timeout(100)
        assert P.view(server.cfg)["queue"] == []
    finally:
        ctx.close()
