"""The Now card for old topics: exact payloads, the second-tap confirm naming the count, the undo.
Node runs the real v2 code on a tiny DOM; the browser test (playwright, guarded) checks layout and RTL."""
import json

import pytest

from ctb import panel_page
from test_cockpit_v2_node import NODE, run

pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")

TOPICS = [{"id": "a1b2c3d4", "name": "ספרים: טיוטה", "project": "ספרים", "age": 20 * 86400, "url": ""},
          {"id": "b2c3d4e5", "name": "Old thread", "project": "", "age": 30 * 86400, "url": ""}]


def with_cleanup(topics=TOPICS):
    def mutate(state):
        state["topic_cleanup"] = {"days": 14, "count": len(topics), "more": 0, "topics": topics, "undo": []}
    return mutate


def go(body, lang="he", topics=TOPICS):
    return run("sandbox.v2CleanupArmed=false;sandbox.v2CleanupResult=null;" + body, lang, with_cleanup(topics))


@pytest.mark.parametrize("lang", ["he", "en"])
def test_card_lists_name_project_and_last_activity_with_keep_and_close(lang):
    got = go("var f=byClass('v2-cleanup-fold')[0];emit({title:f.querySelector('.compact-title').textContent,"
             "rows:byClass('v2-cleanup-row').length,meta:byClass('v2-cleanup-meta')[0].textContent,"
             "keep:allEls().filter(e=>e.attrs['data-v2-cleanup']==='keep').length,"
             "close:allEls().filter(e=>e.attrs['data-v2-cleanup']==='close').length,"
             "all:byClass('v2-cleanup-all')[0].textContent});", lang)
    c = panel_page.V2_STRINGS[lang]["cleanup"]
    assert got["title"] == c["title"] + " (2)" and got["rows"] == 2 and got["keep"] == got["close"] == 2
    assert got["all"] == c["closeAll"] and c["last"] in got["meta"]
    if lang == "he":
        assert got["title"] == "נושאים ישנים (2)"


def test_no_card_without_stale_topics():
    assert go("emit(byClass('v2-cleanup-fold').length);", topics=[]) == 0


def test_keep_and_single_close_send_exact_payloads():
    got = go("var q=s=>allEls().filter(e=>e.attrs['data-v2-cleanup']===s);q('keep')[1].click();await flush();emit(posted);")
    assert got == [["/api/topic-cleanup", {"action": "keep", "ids": ["b2c3d4e5"]}]]
    got = go("var q=s=>allEls().filter(e=>e.attrs['data-v2-cleanup']===s);q('close')[0].click();await flush();emit(posted);")
    assert got == [["/api/topic-cleanup", {"action": "close", "ids": ["a1b2c3d4"]}]]


def test_first_tap_on_close_all_only_arms_and_posts_nothing():
    got = go("var before=renders;byClass('v2-cleanup-all')[0].click();await flush();"
             "emit({armed:sandbox.v2CleanupArmed,posted:posted.length,redrawn:renders-before});")
    assert got == {"armed": True, "posted": 0, "redrawn": 1}


def test_second_tap_posts_all_ids_with_the_confirmed_count():
    got = go("sandbox.v2CleanupArmed=true;var f=sandbox.v2TopicCleanup(sandbox.last);"
             "var all=f.querySelector('.v2-cleanup-all');var label=all.textContent;all.click();await flush();"
             "emit({label,posted,cancel:f.querySelectorAll('.v2-cleanup-cancel').length});", "en")
    assert got["posted"] == [["/api/topic-cleanup", {"action": "close", "ids": ["a1b2c3d4", "b2c3d4e5"], "confirm": 2}]]
    assert got["label"] == "Close 2 topics? Tap again to confirm" and got["cancel"] == 1


def test_keep_offers_an_undo_that_unkeeps():
    got = go("sandbox.v2CleanupResult={kind:'keep',ids:['a1b2c3d4']};var f=sandbox.v2TopicCleanup(sandbox.last);"
             "f.querySelector('[data-v2-cleanup=unkeep]').click();await flush();emit(posted);")
    assert got == [["/api/topic-cleanup", {"action": "unkeep", "ids": ["a1b2c3d4"]}]]


def test_spacing_of_rows_and_result_box_is_twelve_to_sixteen_pixels():
    import re
    from pathlib import Path
    css = (Path(panel_page.__file__).parent / "cockpit_v2.css").read_text()
    for sel in (".v2-cleanup-row{", ".v2-cleanup-result{"):
        rule = next(l for l in css.splitlines() if sel in l)
        pads = [int(x) for x in re.findall(r"padding(?:-block|-inline)?:(\d+)px", rule)]
        assert pads and all(12 <= n <= 16 for n in pads)


def test_a_finished_close_offers_reopen_as_undo():
    got = go("sandbox.v2CleanupResult={ids:['a1b2c3d4']};var f=sandbox.v2TopicCleanup(sandbox.last);"
             "f.querySelector('[data-v2-cleanup=reopen]').click();await flush();emit(posted);")
    assert got == [["/api/topic-cleanup", {"action": "reopen", "ids": ["a1b2c3d4"]}]]


def test_strings_exist_in_both_languages_and_css_is_logical():
    he, en = (panel_page.V2_STRINGS[k]["cleanup"] for k in ("he", "en"))
    assert set(he) == set(en)
    from pathlib import Path
    css = (Path(panel_page.__file__).parent / "cockpit_v2.css").read_text()
    mine = "\n".join(l for l in css.splitlines() if "v2-cleanup" in l)
    for physical in ("margin-left", "margin-right", "padding-left", "padding-right", "left:", "right:", "text-align:left", "text-align:right"):
        assert physical not in mine


@pytest.mark.parametrize("lang", ["he", "en"])
def test_browser_close_all_confirm_undo_layout_and_rtl(lang):
    pytest.importorskip("playwright.sync_api")
    import panel_browser
    from cockpit_v2_browser import open_v2, sample_state
    from playwright import sync_api
    state = sample_state(lang)
    state["topic_cleanup"] = {"days": 14, "count": 2, "more": 0, "topics": TOPICS, "undo": []}
    panel_browser.prepare_env()
    with sync_api.sync_playwright() as p:
        browser = panel_browser.launch(p)
        if browser is None:
            pytest.skip("headless Chromium cannot run in this environment")
        ctx, page, calls, errors, _ = open_v2(browser, lang, state=state)
        try:
            fold = page.locator('[data-v2-section="cleanup"]')
            fold.locator(".compact-toggle").click()
            c = panel_page.V2_STRINGS[lang]["cleanup"]
            assert page.locator(".v2-cleanup-row").count() == 2
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.locator(".v2-cleanup-all").click()
            assert page.locator(".v2-cleanup-all").inner_text() == c["confirmAll"].replace("{n}", "2")
            assert page.locator(".v2-cleanup-cancel").count() == 1
            assert not [x for x in calls if x[0] == "/api/topic-cleanup"]
            page.locator(".v2-cleanup-all").click()
            page.locator(".v2-cleanup-undo").wait_for()
            assert ("/api/topic-cleanup", {"action": "close", "ids": ["a1b2c3d4", "b2c3d4e5"], "confirm": 2}) in calls
            page.locator(".v2-cleanup-undo").click()
            page.wait_for_function("!document.querySelector('.v2-cleanup-undo')")
            assert ("/api/topic-cleanup", {"action": "reopen", "ids": ["a1b2c3d4", "b2c3d4e5"]}) in calls
            assert not errors
        finally:
            ctx.close()
