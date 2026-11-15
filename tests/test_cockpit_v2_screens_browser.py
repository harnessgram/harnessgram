"""Milestone 2 screens at 390 px: must-keep contract, navigation, back and unchanged actions."""
import pytest

pytest.importorskip("playwright")

import panel_browser
from cockpit_v2_browser import assert_screen_contract, open_v2, rich_state
from cockpit_v2_screenshots import go_project, go_projects, go_radio, go_settings, go_studio

sync_api = pytest.importorskip("playwright.sync_api")
SCREENS = {"projects": go_projects, "project": go_project, "tools-radio": go_radio, "tools-studio": go_studio, "settings": go_settings}


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

    def make(lang="he", scheme="light", state=None):
        ctx, page, calls, errors, st = open_v2(browser, lang, scheme, state=state or rich_state(lang))
        made.append(ctx)
        return page, calls, errors, st
    yield make
    for ctx in made:
        ctx.close()


@pytest.mark.parametrize("screen", sorted(SCREENS))
@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_screen_must_keep_contract(opened, screen, lang, scheme):
    page, calls, errors, state = opened(lang, scheme)
    SCREENS[screen](page)
    assert_screen_contract(page, screen)
    assert page.locator("html").get_attribute("dir") == ("rtl" if lang == "he" else "ltr")
    assert not errors


def test_one_card_per_project_with_bridge_status_and_waiting_count(opened):
    page, calls, errors, state = opened()
    go_projects(page)
    projects = state["cockpit_v2"]["projects"]
    cards = page.locator(".v2-pcard")
    assert cards.count() == len(projects)
    for p in projects:
        card = page.locator('.v2-pcard[data-project="%s"]' % p["key"])
        assert card.get_attribute("data-status") == p["status"]
        assert (card.locator(".v2-wait-chip").count() == 1) == bool(p["waiting"])
    # Team and System status are collapsed, reversible disclosures.
    toggle = page.locator('[data-v2-section="team"] > .compact-toggle')
    assert toggle.get_attribute("aria-expanded") == "false"
    toggle.click()
    assert toggle.get_attribute("aria-expanded") == "true"
    assert page.locator('[data-v2-section="team"] .srow').count() == len(state["sessions"])
    page.go_back()
    assert toggle.get_attribute("aria-expanded") == "false"


@pytest.mark.parametrize("how", ["arrow", "browser", "tab"])
def test_project_page_closes_with_arrow_back_or_tab_and_keeps_list(opened, how):
    page, calls, errors, state = opened()
    go_project(page)
    assert page.locator(".v2-plist").count() == 0
    if how == "arrow":
        page.locator("[data-v2-back]").click()
    elif how == "browser":
        page.go_back()
    else:
        page.locator('#tabs [data-v2-tab="agents"]').click()
    page.locator(".v2-plist").wait_for()
    assert page.locator(".v2-ppage").count() == 0
    assert not errors


def test_project_page_answers_its_own_decision_with_the_original_id(opened):
    page, calls, errors, state = opened()
    go_project(page)
    key = page.locator(".v2-ppage").get_attribute("data-project")
    mine = [d["id"] for d in state["decisions"] if d["project_key"] == key]
    shown = page.locator('[data-v2-section="project-needs"] [data-v2-decision]').evaluate_all("es=>es.map(e=>e.dataset.v2Decision)")
    assert shown == mine and mine
    # The detail is only "Recommendation: ...": it is shown once, before the answer buttons.
    row = page.locator('[data-v2-decision="%s"]' % mine[0])
    assert row.locator(".v2-recommendation").is_visible() and row.locator(".detail").count() == 0
    rec = "להמשיך לפי התכנון שהוכן."
    assert row.inner_text().count(rec) == 1
    row.locator('[data-v2-answer="yes"]').click()
    row.wait_for(state="detached")
    assert ("/api/decide", {"id": mine[0], "choice": "yes", "note": ""}) in calls


def test_topic_rows_open_the_existing_sheet_with_a_way_out(opened):
    page, calls, errors, state = opened()
    go_project(page)
    page.locator('[data-v2-section="project-topics"] .topic-card').first.click()
    sheet = page.locator("dialog.topic-sheet")
    assert sheet.is_visible()
    page.go_back()
    sheet.wait_for(state="hidden")
    assert page.locator(".v2-ppage").count() == 1


def test_now_tile_opens_that_project(opened):
    page, calls, errors, state = opened()
    tile = page.locator(".v2-project").first
    key = tile.get_attribute("data-project")
    tile.click()
    assert page.locator(".v2-ppage").get_attribute("data-project") == key
    assert page.locator('#tabs [data-v2-tab="agents"]').get_attribute("aria-selected") == "true"


def test_radio_tool_moves_the_one_player_and_keeps_classic_actions(opened):
    page, calls, errors, state = opened()
    handle = page.evaluate_handle("document.getElementById('radio-player')")
    go_radio(page)
    assert page.evaluate("(h)=>h===document.getElementById('radio-player')&&!!h.closest('.v2-radio-hero')", handle)
    assert page.locator("#radio-player").is_visible()
    # The announcement composer is the classic one, inside its tile.
    tile = page.locator('[data-v2-section="radio-actions"] .v2-tile').nth(1)
    tile.locator(".compact-toggle").click()
    tile.locator("textarea").fill("בדיקה")
    tile.locator(".btn.pri").click()
    page.wait_for_timeout(100)
    assert ("/api/radio-say", {"text": "בדיקה"}) in calls
    # The voice digest choice uses the same setting API.
    page.locator('[data-v2-section="voice-digest"] .opt').first.click()
    page.wait_for_timeout(100)
    assert ("/api/setting", {"name": "voice_digest", "value": "off"}) in calls
    # The station's start/stop is the classic call.
    page.locator('[data-v2-section="station"] .btns .btn').click()
    page.wait_for_timeout(100)
    assert ("/api/radio", {"on": False}) in calls
    # Leaving the tool returns the same player to its classic place on Now.
    page.locator('#tabs [data-v2-tab="me"]').click()
    assert page.evaluate("(h)=>h===document.getElementById('radio-player')&&!h.closest('#app')", handle)
    assert not errors


def test_tool_choice_is_remembered_and_studio_is_the_existing_frame(opened):
    page, calls, errors, state = opened()
    go_studio(page)
    assert page.locator('[data-v2-tool="studio"]').get_attribute("aria-pressed") == "true"
    page.locator('#tabs [data-v2-tab="me"]').click()
    page.locator('#tabs [data-v2-tab="tools"]').click()
    assert page.locator('[data-v2-tool="studio"]').get_attribute("aria-pressed") == "true"
    assert page.locator("#apps iframe.appframe").first.is_visible()
    page.locator('[data-v2-tool="more"]').click()
    page.locator('[data-v2-section="more-tools"] .v2-app').wait_for()
    assert page.locator("#apps").is_hidden()
    assert not errors


def test_settings_groups_hold_every_classic_control_with_the_same_api(opened):
    page, calls, errors, state = opened()
    go_settings(page)
    for group in ("notify", "models", "radio"):
        page.locator('[data-v2-section="settings-%s"] > .compact-toggle' % group).click()
    row = page.locator('[data-v2-section="settings-auto"] .sw').first
    was = row.get_attribute("aria-checked") == "true"
    row.click()
    page.wait_for_timeout(100)
    assert any(c[0] == "/api/setting" and c[1]["value"] is (not was) for c in calls)
    assert page.locator('[data-v2-section="settings-models"] .seg .opt').count() >= 2
    assert page.locator('[data-v2-section="settings-radio"] .card.yt').count() == 1
    assert page.locator('[data-v2-section="settings-radio"] .sw').count() == 3
    # Off restores the classic panel.
    page.locator('[data-v2-mode="off"]').click()
    page.wait_for_function("!document.documentElement.classList.contains('cockpit-v2')")
    assert page.locator(".v2-group").count() == 0
    assert not errors


def test_classic_tabs_are_unchanged_when_the_switch_is_off(opened):
    state = rich_state("he")
    state["settings"]["ui_v2"] = "off"
    state["ui_v2_enabled"] = False
    page, calls, errors, st = opened(state=state)
    page.locator("#tabs .tab").nth(1).click()
    page.wait_for_timeout(200)
    assert page.locator(".v2-plist, .v2-group, .v2-radio-hero").count() == 0
    assert not page.evaluate("document.documentElement.classList.contains('cockpit-v2')")


@pytest.mark.parametrize("lang", ["he", "en"])
def test_header_title_never_sits_under_the_exit_button(opened, lang):
    page, calls, errors, state = opened(lang)
    a, b = page.locator("#v2-exit").bounding_box(), page.locator("#t").bounding_box()
    apart = a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"] or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"]
    assert apart, (a, b)
