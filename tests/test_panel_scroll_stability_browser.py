"""Poll the rendered Cockpit through its normal /api/state entry point."""
import copy

import pytest

from cockpit_v2_browser import open_v2, rich_state
from ctb import panel_page
from test_cockpit_back_browser import open_cockpit
from test_cockpit_compact_browser import state
from test_cockpit_overflow_browser import browser  # noqa: F401
from cockpit_v2_screenshots import go_project, go_projects, go_radio

expect = pytest.importorskip("playwright.sync_api").expect


def poll(page):
    with page.expect_response("**/api/state"):
        # Trigger the same load as the poll timer, without scrolling to the refresh button.
        page.locator("#rf").evaluate("button => button.click()")
    expect(page.locator("#rf")).not_to_have_class("icon-btn spin")


def frames(page):
    page.evaluate("new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))")


def test_every_payload_field_invalidates_the_fingerprint_except_clocks(browser):
    data = state()
    lists = ("tasks", "deliveries", "quota", "bank", "models", "widget_devices")
    objects = ("mac", "topic_cleanup", "radio", "room", "podcast", "youtube", "default_model", "listen_link")
    clocks = ("age", "updated_ago", "now", "server_time")
    data.update({key: [{"sample": 1}] for key in lists})
    data.update({key: {"sample": 1} for key in objects})
    data.update({key: 1 for key in clocks if key != "now"})
    ctx, page = open_cockpit(browser, data)
    try:
        child = page.locator("#app > *").first
        for key in lists + objects + clocks:
            previous = child.element_handle()
            if key in lists:
                data[key][0]["sample"] += 1
            elif key in objects:
                data[key]["sample"] += 1
            else:
                data[key] += 1
            poll(page)
            assert previous.evaluate("node => node.isConnected") == (key in clocks), key
    finally:
        ctx.close()


def test_identical_and_changed_polls_keep_the_visible_anchor(browser):
    data = state()
    seed = data["sessions"][0]
    data["sessions"] = [
        dict(copy.deepcopy(seed), id=f"scroll-{index:02d}", name=f"Long running session {index:02d}")
        for index in range(36)
    ]
    ctx, page = open_cockpit(browser, data)
    try:
        page.get_by_role("tab", name=panel_page.STRINGS["he"]["tabAgents"], exact=True).click()
        page.wait_for_function(
            "document.getAnimations().every(a => a.effect.getComputedTiming().iterations === Infinity"
            " || a.playState === 'finished')")
        anchor = page.locator('[data-key="session:scroll-18"]')
        anchor.evaluate("node => window.scrollBy(0, node.getBoundingClientRect().top + 10)")
        frames(page)
        before = anchor.bounding_box()["y"]
        previous = page.locator("#app > *").first.element_handle()

        poll(page)
        assert previous.evaluate("node => node.isConnected")
        assert abs(anchor.bounding_box()["y"] - before) <= 2

        changed = "A much longer current activity that wraps across several lines. " * 8
        data["sessions"][0]["now"] = changed
        poll(page)
        expect(page.locator('[data-key="session:scroll-00"] summary')).to_contain_text(changed.strip())
        frames(page)
        assert abs(anchor.bounding_box()["y"] - before) <= 2
    finally:
        ctx.close()


# The same repeatable card layout exercises the real poll renderer even on short screens
# and around app frames. This fixture also controls intrinsic image size and network timing.
CARDS = """
function drawScrollFixture(app, cards) {
    if (!cards) return;
    const rows = cards.map(({id, title, extra, images}) => {
        const card = document.createElement("article");
        card.dataset.key = "poll-card:" + id;
        card.style.minHeight = "120px";
        card.textContent = title;
        if (extra) {
            const detail = document.createElement("p");
            detail.style.height = "140px";
            detail.textContent = extra;
            card.append(detail);
        }
        for (const src of images || []) {
            const image = document.createElement("img");
            image.src = src;
            image.style.display = "block";
            card.append(image);
        }
        return card;
    });
    app.prepend(...rows);
}
"""

SCREENS = (
    ("classic-here", (), ".s-head"),
    ("classic-me", ('[role=tab] >> nth=1',), "#app .hero"),
    ("classic-agents", ('[role=tab] >> nth=2',), "#app .srow"),
    ("classic-settings", ('[role=tab] >> nth=3',), "#app .row"),
    ("v2-now", (), ".v2-needs"),
    ("v2-projects", go_projects, ".v2-plist"),
    ("v2-project", go_project, ".v2-ppage"),
    ("v2-tools-radio", go_radio, ".v2-radio-hero"),
    ("v2-tools-studio", ('[data-v2-tab=tools]', '[data-v2-tool=studio]'), "iframe.appframe"),
    ("v2-tools-tower", ('[data-v2-tab=tools]', '[data-v2-tool=tower]'), ".vcp-page"),
    ("v2-tools-more", ('[data-v2-tab=tools]', '[data-v2-tool=more]'), '[data-v2-section=more-tools]'),
    ("v2-settings", ('[data-v2-tab=settings]',), '[data-v2-section=settings-notify]'),
    ("v2-knowledge", ('[data-v2-tab=settings]', '[data-v2-section=knowledge-entry]'), ".vk-tiles"),
    ("v2-addons", ('[data-v2-tab=settings]', '[data-v2-section=addons-entry]'), ".va-group"),
    ("v2-packs", ('[data-v2-tab=settings]', '[data-v2-section=packs-entry]'), '[data-v2-section=packs]'),
)


@pytest.mark.parametrize("screen,navigate,ready", SCREENS, ids=[row[0] for row in SCREENS])
def test_three_polls_hold_the_first_visible_card_on_phone(browser, monkeypatch, screen, navigate, ready):
    render_page = panel_page.render_page

    def fixture_page(lang, nonce):
        html = render_page(lang, nonce).decode()
        # Measure Cockpit's own restoration independently of Chromium's native scroll anchoring.
        html = html.replace("</style>", "html{overflow-anchor:none}</style>", 1)
        html = html.replace("function render(d,force){", CARDS + "\nfunction render(d,force){", 1)
        return html.replace("showApp(d);document.getElementById",
                            "drawScrollFixture(app,d.scroll_fixture);showApp(d);document.getElementById", 1)

    monkeypatch.setattr(panel_page, "render_page", fixture_page)
    data = rich_state() if screen.startswith("v2-") else state()
    if screen == "classic-settings":
        # The classic Settings tab draws only what the payload carries.
        rich = rich_state()
        data["settings"] = rich["settings"]
        data["default_model"] = rich["default_model"]
    if screen == "v2-tools-tower":
        data["settings"]["voice_copilot"] = True
    data["scroll_fixture"] = [{"id": i, "title": f"Fixture card {i}"} for i in range(32)]
    ctx, page, _, errors, data = open_v2(browser, width=390, height=844, state=data)
    held = {}
    try:
        if callable(navigate):
            navigate(page)
        else:
            for selector in navigate:
                page.locator(selector).click()
        page.locator(ready).first.wait_for()
        page.route("**/poll-image-*.svg", lambda route: held.update({route.request.url.rsplit("/", 1)[-1]: route}))

        anchor = page.locator('[data-key="poll-card:16"]')
        anchor.evaluate("node => window.scrollBy(0, node.getBoundingClientRect().top + 15)")
        frames(page)
        before = anchor.bounding_box()["y"]

        def assert_anchor(stage):
            first = page.locator("[data-key^='poll-card:']").evaluate_all(
                "nodes => nodes.find(node => node.getBoundingClientRect().bottom > 0)?.dataset.key"
            )
            assert first == "poll-card:16", stage
            assert abs(anchor.bounding_box()["y"] - before) <= 2, stage

        assert_anchor("before polling")
        poll(page)
        assert_anchor("identical state")

        data["scroll_fixture"][28]["extra"] = "Changed below the viewport"
        poll(page)
        expect(page.locator('[data-key="poll-card:28"]')).to_contain_text("Changed below")
        frames(page)
        assert_anchor("changed below")

        image = "poll-image-late.svg"
        data["scroll_fixture"][4].update(
            extra="Changed above the viewport", images=["/" + image, "/poll-image-still-pending.svg"],
        )
        with page.expect_request("**/" + image):
            poll(page)
        above = page.locator('[data-key="poll-card:4"]')
        expect(above).to_contain_text("Changed above")
        frames(page)
        assert_anchor("changed above, before its image")
        assert image in held
        # A second image remains pending. The first image's height must be compensated on its own.
        height = above.bounding_box()["height"]
        page.wait_for_timeout(450)
        held[image].fulfill(content_type="image/svg+xml",
                            body='<svg xmlns="http://www.w3.org/2000/svg" width="180" height="160"/>')
        page.wait_for_function("src => document.querySelector('img[src=\"/' + src + '\"]')?.naturalHeight === 160",
                               arg=image)
        frames(page)
        assert above.bounding_box()["height"] >= height + 150
        assert_anchor("late image loaded")
        assert not errors, errors
    finally:
        ctx.close()
