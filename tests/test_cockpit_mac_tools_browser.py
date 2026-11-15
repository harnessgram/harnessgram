"""Mac maintenance (Tools > More) at 390 px in a real browser: the tile, the sheet, the two-tap confirmations, direction and fit.

Every request is intercepted by a small fake of the server's job manager: no bridge, no command and no network is touched."""
import copy
import json
import time

import pytest

pytest.importorskip("playwright")

import panel_browser
from cockpit_v2_browser import open_v2, rich_state

sync_api = pytest.importorskip("playwright.sync_api")

GB = 1024 ** 3
NOW = int(time.time())
MAC = {"available": True, "status": "warn", "disk": {"free_bytes": 35 * GB, "total_bytes": 460 * GB, "status": "warn"},
       "memory": {"pressure_free_percent": 42, "status": "ok", "available_bytes": 3 * GB},
       "cpu": {"load": [2.34, 2.0, 1.5], "cores": 10, "status": "ok"},
       "processes": {"cpu": [], "memory": [{"kind": "browser", "memory_bytes": 3 * GB, "cpu_percent": 4},
                                           {"kind": "build", "memory_bytes": 2 * GB, "cpu_percent": 3},
                                           {"kind": "agent", "memory_bytes": GB, "cpu_percent": 1}]}}
CATS = [{"name": "User app cache", "bytes": 43 * GB}, {"name": "Xcode DerivedData", "bytes": 2800000000},
        {"name": "Clang module cache", "bytes": 276000000}]


class FakeServer:
    """Answers /api/mac-tools-status and /api/mac-tools the way mac_tools.Manager does, but instantly and in memory."""

    def __init__(self, last=None, dry=None):
        self.job, self.last, self.dry, self.posts, self.n = None, last, dry, [], 0

    def status(self):
        dry = dict(self.dry) if self.dry else None
        return {"ok": True, "now": NOW, "actions": {"dry_run": True, "clean": True, "maintenance": True}, "fresh_seconds": 600,
                "job": self.job, "dry": dry, "last": self.last, "mac": MAC}

    def finish(self):
        """The running job ends: the next poll sees its report."""
        job, self.job = self.job, None
        kind = job["kind"]
        report = {"id": job["id"], "kind": kind, "state": "ok", "started_at": NOW - 60, "finished_at": NOW, "duration": 60,
                  "free_before": 35 * GB, "free_after": 38 * GB if kind != "dry-run" else 35 * GB}
        if kind == "dry-run":
            report.update(bytes=48 * GB, items=380, categories=CATS)
            self.dry = {"id": job["id"], "bytes": 48 * GB, "items": 380, "categories": CATS, "finished_at": NOW, "age": 0, "fresh": True}
        elif kind == "clean":
            report.update(bytes=3 * GB)
            self.dry = None
        self.last = report

    def start(self, body):
        self.posts.append(body)
        if self.job:
            return 409, {"error": "busy", "kind": self.job["kind"]}
        self.n += 1
        ident = "%012x" % self.n
        self.job = {"id": ident, "kind": body["action"], "elapsed": 5}
        return 200, {"ok": True, "id": ident, "kind": body["action"]}


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

    def make(lang="he", scheme="light", configured=True, server=None, extra=None):
        state = copy.deepcopy(rich_state(lang))
        state.update(extra or {})
        if configured:
            state["mac_tools"] = {"dry_run": True, "clean": True, "maintenance": True}
        ctx, page, calls, errors, st = open_v2(browser, lang, scheme, state=state)
        made.append(ctx)
        fake = server or FakeServer()

        def status(request):
            request.fulfill(json=fake.status())

        def start(request):
            code, body = fake.start(request.request.post_data_json)
            request.fulfill(status=code, json=body)
        page.route("**/api/mac-tools-status", status)
        page.route("**/api/mac-tools", start)
        return page, fake, errors

    yield make
    for ctx in made:
        ctx.close()


def go_more(page):
    page.locator('#tabs [data-v2-tab="tools"]').click()
    page.locator('[data-v2-tool="more"]').click()
    page.locator('[data-v2-section="more-tools"]').wait_for()


def open_sheet(page):
    go_more(page)
    page.locator("[data-v2-mac-tools]").click()
    page.locator("dialog.mt-sheet[open]").wait_for()
    page.locator('[data-mt="status"]').wait_for()


def fits(page):
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_tile_is_hidden_when_the_feature_is_not_configured(opened):
    page, fake, errors = opened(configured=False)
    go_more(page)
    assert page.locator("[data-v2-mac-tools]").count() == 0
    assert not errors


def test_tile_comes_right_after_the_app_request_tile(opened):
    page, fake, errors = opened(extra={"app_request": True})
    go_more(page)
    tiles = page.locator('[data-v2-section="more-tools"] > *')
    assert tiles.nth(0).get_attribute("data-v2-app-request") == "1" and tiles.nth(1).get_attribute("data-v2-mac-tools") == "1"
    assert not errors


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_sheet_fits_at_390_with_big_targets(opened, lang, scheme):
    page, fake, errors = opened(lang, scheme, server=FakeServer(dry={"id": "a" * 12, "bytes": 48 * GB, "items": 380, "categories": CATS,
                                                                    "finished_at": NOW, "age": 5, "fresh": True},
                                                                last={"id": "b" * 12, "kind": "maintenance", "state": "ok", "started_at": NOW - 300,
                                                                      "finished_at": NOW - 200, "duration": 100, "free_before": 30 * GB,
                                                                      "free_after": 35 * GB}))
    go_more(page)
    tile = page.locator("[data-v2-mac-tools]")
    tile_box = tile.bounding_box()
    assert tile_box["height"] >= 44
    tile.click()
    sheet = page.locator("dialog.mt-sheet[open]")
    sheet.wait_for()
    page.locator('[data-mt="dry"]').wait_for()
    box = sheet.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 390 and box["y"] >= 0 and box["y"] + box["height"] <= 844
    assert fits(page)
    for button in sheet.locator("button").all():
        assert button.bounding_box()["height"] >= 44, button.inner_text()
    if scheme == "dark":
        assert sheet.evaluate("e => getComputedStyle(e).backgroundColor").startswith("rgb(")
    assert sheet.evaluate("e => e.scrollWidth <= e.clientWidth")
    assert not errors


def test_hebrew_sheet_is_rtl_and_the_english_category_names_read_left_to_right(opened):
    page, fake, errors = opened("he", server=FakeServer(dry={"id": "a" * 12, "bytes": 48 * GB, "items": 380, "categories": CATS,
                                                              "finished_at": NOW, "age": 5, "fresh": True}))
    open_sheet(page)
    page.locator('[data-mt="dry"]').wait_for()
    assert page.locator("dialog.mt-sheet").evaluate("e => getComputedStyle(e).direction") == "rtl"
    name = page.locator('[data-mt="dry"] bdi').first
    assert name.inner_text() == "User app cache" and name.evaluate("e => getComputedStyle(e).direction") == "ltr"
    assert not errors


def test_check_then_two_taps_then_the_report(opened):
    page, fake, errors = opened("he")
    open_sheet(page)
    clean = page.locator('[data-mt="clean"]')
    assert clean.is_disabled()
    page.locator('[data-mt="check"]').click()
    page.locator('[data-mt="running"]').wait_for()
    assert page.locator('[data-mt="check"]').is_disabled() and page.locator('[data-mt="maintenance"]').is_disabled()
    assert fits(page)
    fake.finish()
    page.locator('[data-mt="dry"]').wait_for(timeout=8000)
    assert "48" in page.locator('[data-mt="dry"]').inner_text()
    assert clean.is_enabled()
    clean.click()
    assert page.locator('[data-mt="clean"]').inner_text() == "לנקות 48 גיגה־בייט?"
    assert fake.posts == [{"action": "dry-run"}]            # the first tap sent nothing
    page.locator('[data-mt="clean"]').click()
    page.locator('[data-mt="running"]').wait_for()
    assert fake.posts[-1] == {"action": "clean", "confirm": "000000000001"}
    fake.finish()
    page.locator('[data-mt="last"]').get_by_text("התפנו 3 גיגה־בייט").wait_for(timeout=8000)
    assert page.locator('[data-mt="clean"]').is_disabled()   # the preview was spent
    assert fits(page) and not errors


def test_weekly_routine_needs_a_second_tap(opened):
    page, fake, errors = opened("en")
    open_sheet(page)
    page.locator('[data-mt="maintenance"]').click()
    assert page.locator('[data-mt="maintenance"]').inner_text() == "Run the weekly maintenance?" and fake.posts == []
    page.locator('[data-mt="cancel"]').click()
    assert fake.posts == [] and page.locator('[data-mt="maintenance"]').inner_text() == "Run weekly maintenance now"
    page.locator('[data-mt="maintenance"]').click()
    page.locator('[data-mt="maintenance"]').click()
    page.locator('[data-mt="running"]').wait_for()
    assert fake.posts == [{"action": "maintenance", "confirm": True}]
    assert not errors


def test_close_returns_to_the_tools_screen(opened):
    page, fake, errors = opened("en")
    open_sheet(page)
    page.locator(".mt-sheet .topic-close").click()
    assert page.locator("dialog.mt-sheet[open]").count() == 0
    assert page.locator("[data-v2-mac-tools]").is_visible()
    assert not errors
