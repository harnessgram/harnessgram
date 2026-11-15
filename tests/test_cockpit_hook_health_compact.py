"""The session-health card: never on every tab when healthy; in Settings one line of counts, no session ids."""
import pytest

from ctb import panel_page
from test_cockpit_compact_browser import open_mock, state
from test_cockpit_overflow_browser import browser  # noqa: F401  (fixture)

IDS = ["d6a2f1e9aa", "5019d969aa", "a2311d51aa", "7b54be96aa", "12664b02aa", "dead0000aa"]


def health():
    skipped = [{"session": IDS[0], "reason": "current"}, {"session": IDS[1], "reason": "current"},
               {"session": IDS[2], "reason": "busy"}, {"session": IDS[3], "reason": "busy"},
               {"session": IDS[5], "reason": "dialog"}]
    return {"ok": True, "events": [], "restored": False,
            "reload": {"running": False, "at": 1, "reloaded": [IDS[4]], "skipped": skipped}}


@pytest.mark.parametrize("lang", ["he", "en"])
def test_healthy_card_only_in_settings_as_one_line(browser, lang):
    data = state()
    data["hook_health"] = health()
    ctx, page = open_mock(browser, data, lang)
    try:
        assert page.locator(".hook-health").count() == 0  # home tab: nothing, even with a finished reload
        page.locator(".tabs .tab").nth(3).click()
        page.wait_for_timeout(150)
        card = page.locator(".hook-health")
        assert card.count() == 1
        S = panel_page.STRINGS[lang]
        want = " · ".join([S["hooksNReloaded"].replace("#", "1"), S["hooksNCurrent"].replace("#", "2"),
                           S["hooksNBusy"].replace("#", "2"), S["hooksNOther"].replace("#", "1")])
        assert card.locator(".hook-sum").inner_text() == want
        text = card.inner_text()
        assert not any(i[:8] in text for i in IDS)
        assert card.locator("div").count() == 0  # no per-session rows
    finally:
        ctx.close()


def test_broken_connection_still_shows_everywhere(browser):
    data = state()
    data["hook_health"] = {"ok": False, "events": ["Stop"], "restored": False, "reload": None}
    ctx, page = open_mock(browser, data, "he")
    try:
        assert page.locator(".hook-health").count() == 1
        assert panel_page.STRINGS["he"]["hooksBroken"] in page.locator(".hook-health").inner_text()
    finally:
        ctx.close()
