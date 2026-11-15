import re
"""Real owner-authenticated phone taps through HTTP into the board, including other topics."""
import json
import threading
import time
from pathlib import Path

import pytest

from ctb import board, owner_requests, panel, panel_page
from ctb.config import Config, ProjectConfig, state_dir, write_private
from conftest import FakeBot, GOOD_TOKEN
from test_panel import call, signed
import panel_browser

sync_api = pytest.importorskip("playwright.sync_api")
CHAT, AWAY = -1001234567890, -1009876543210
DAY = 86400


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
def server(monkeypatch):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=7, chat_id=CHAT, ui_language="he",
                 brief={"ledger_dir": str(Path.home() / "requests")},
                 projects=[ProjectConfig("another", AWAY)])
    srv = panel.PanelServer(("127.0.0.1", 0), cfg, bot=FakeBot())
    # Use the actual translator interface, with deterministic completed translations.
    names = {"Review the screen": "לבדוק את המסך", "Prepare the release": "להכין את הגרסה",
             "Complete request": "בקשה שבוצעה", "First step": "בדיקה ראשונה", "Latest step": "הבדיקה הושלמה"}
    monkeypatch.setattr(srv.translator, "he", lambda text: names.get(text, ""))
    monkeypatch.setattr(panel, "collect", lambda topics, now=None: [])
    monkeypatch.setattr(panel.mac_resources, "snapshot", lambda: {"available": False})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def put_requests(project, rows):
    folder = Path.home() / "requests"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (project + ".json")).write_text(json.dumps({"next": 20, "items": rows}))


def request(ident, text, **extra):
    now = time.time()
    row = dict(id=ident, text=text, wait="team", status="open", created=now - DAY, updated=now, notes=[])
    row.update(extra)
    return row


def me(page):
    he = panel_page.STRINGS["he"]  # with the Morning Brief on, the "For me" tab is labelled Brief
    page.locator(".tabs .tab").filter(has_text=re.compile(he["tabMe"] + "|" + he["tabBrief"])).first.click()


@pytest.mark.parametrize("brief_home", [False, True])
def test_hebrew_requests_filter_steps_done_poll_and_no_overflow(browser, server, monkeypatch, brief_home):
    monkeypatch.setattr(panel.brief, "on", lambda: brief_home)
    put_requests("alpha", [request(1, "Prepare the release"),
                           request(2, "Review the screen", wait="owner", notes=["First step", {"text": "Latest step"}]),
                           request(3, "Complete request", status="done")])
    put_requests("beta", [request(1, "בקשה " + "מילהארוכה" * 40, notes=["פרטים " * 100])])
    ctx, page = panel_browser.open_panel(browser, server, signed(), lang="he", width=390)
    try:
        page.clock.install()
        page.reload()
        me(page)
        page.wait_for_selector(".request-row")
        assert page.locator("html").get_attribute("dir") == "rtl"
        assert page.locator(".requests-needs .request-row").count() == 1
        group = page.locator('[data-block="requests-project:alpha"]')
        toggle = group.locator(":scope > .compact-toggle")
        assert toggle.get_attribute("aria-expanded") == "false"
        toggle.click()
        page.wait_for_timeout(700)
        titles = group.locator(".request-row:visible .nm").all_text_contents()
        assert titles == ["לבדוק את המסך", "להכין את הגרסה"]
        first = group.locator(".request-row").first
        assert first.get_by_text("נדרש ממך").is_visible()
        assert not first.locator(".request-note").is_visible()
        first.locator(".request-toggle").click()
        assert first.locator(".request-note").inner_text() == "הבדיקה הושלמה"
        assert first.locator(".request-steps ol").inner_text().splitlines() == ["בדיקה ראשונה", "הבדיקה הושלמה"]
        done = group.locator('[data-block="requests-done:alpha"]')
        assert done.locator(":scope > button").get_attribute("aria-expanded") == "false"
        assert not group.get_by_text("בקשה שבוצעה", exact=True).is_visible()
        done.locator(":scope > .compact-toggle").click()
        page.wait_for_timeout(700)
        assert group.get_by_text("בקשה שבוצעה", exact=True).is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.get_by_role("group", name="לפי פרויקט").get_by_role("button", name=re.compile(r"^beta")).click()
        assert page.locator(".request-project").count() == 1
        assert not page.locator(".requests").get_by_text("לבדוק את המסך", exact=True).count()  # the brief may list it in its own section
        # A normal state refresh sees the external file, and retains this viewer's filter.
        put_requests("beta", [request(1, "בקשה ראשונה"), request(2, "בקשה שהגיעה עכשיו")])
        page.clock.run_for(61000)  # one idle poll (20 s), plus the brief's slower refresh when it is the home
        page.locator(".requests").get_by_text("בקשה שהגיעה עכשיו", exact=True).wait_for()  # the brief may list it too
        assert page.get_by_role("group", name="לפי פרויקט").get_by_role("button", name=re.compile(r"^beta")).get_attribute("aria-pressed") == "true"
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
def test_requests_phone_rail_shortlist_paging_expansion_and_remembered_groups(browser, server, monkeypatch, lang):
    monkeypatch.setattr(panel.brief, "on", lambda: False)
    long_title = "בקשה עם כותרת ארוכה " * 20
    note = "הערה מלאה לבקשה " * 30
    put_requests("alpha", [request(n, long_title + str(n), wait="owner" if n <= 4 else "team",
                                   notes=["שלב ראשון", note]) for n in range(1, 9)])
    put_requests("beta", [request(n, "בקשת צוות " + str(n)) for n in range(1, 13)])
    put_requests("gamma", [request(1, "מחכה לך", wait="owner")])
    put_requests("completed", [request(1, "בקשה סגורה", status="done")])
    for n in range(8):
        put_requests("project-" + str(n), [request(1, "משימה")])
    server.lang = lang  # the page is served in the server's UI language; the browser locale alone does not switch it
    ctx, page = panel_browser.open_panel(browser, server, signed(), lang=lang, width=390)
    strings = panel_page.STRINGS[lang]
    try:
        page.clock.install()
        page.reload()
        page.locator(".tabs .tab").filter(has_text=strings["tabMe"]).first.click()
        rail = page.get_by_role("group", name=strings["requestsProject"])
        assert page.locator("html").get_attribute("dir") == ("rtl" if lang == "he" else "ltr")
        assert rail.evaluate("e => e.scrollWidth > e.clientWidth")
        assert rail.locator("button").evaluate_all("es => new Set(es.map(e=>Math.round(e.getBoundingClientRect().top))).size === 1")
        assert rail.locator(".requests-pill-name").all_text_contents()[:4] == [strings["requestsAll"], "alpha", "gamma", "beta"]
        assert "completed" not in rail.inner_text()
        assert rail.locator("button").first.get_attribute("aria-pressed") == "true"
        assert page.locator(".requests-needs .request-row").count() == 3
        assert page.locator(".requests .request-row:visible").count() == 3  # project copies stay hidden
        assert page.evaluate("document.documentElement.scrollWidth <= 390")
        assert rail.evaluate("e => getComputedStyle(e).scrollSnapType.includes('inline')")
        rail.evaluate("e => e.scrollLeft = document.documentElement.dir === 'rtl' ? -e.scrollWidth : e.scrollWidth")
        assert rail.evaluate("e => Math.abs(e.scrollLeft) > 0")
        group = page.locator('[data-block="requests-project:alpha"]')
        group.locator(":scope > button").click()
        page.wait_for_timeout(700)
        assert group.locator(".request-row:visible").count() == 5
        more = group.get_by_role("button", name=strings["requestsMore"].replace("{n}", "3"), exact=True)
        assert more.is_visible()
        first = group.locator(".request-row").first
        row_button = first.locator(".request-toggle")
        assert row_button.get_attribute("aria-expanded") == "false"
        assert row_button.bounding_box()["height"] >= 44
        assert first.locator(".nm").evaluate("e => getComputedStyle(e).webkitLineClamp") == "2"
        assert not first.locator(".request-note").is_visible()
        row_button.click()
        assert row_button.get_attribute("aria-expanded") == "true"
        assert first.locator(".request-note").is_visible()
        assert first.locator(".request-note").inner_text() == note.strip()
        assert first.locator(".request-steps li").count() == 2
        assert first.locator(".nm").evaluate("e => e.scrollHeight <= e.clientHeight + 1")
        row_button.focus()
        page.keyboard.press("Enter")
        assert not first.locator(".request-note").is_visible()
        page.keyboard.press("Space")
        assert first.locator(".request-note").is_visible()
        more.click()
        assert group.locator(".request-row:visible").count() == 8
        assert not group.locator(".requests-more").count()
        # State polls retain disclosure, row expansion, filter and pagination.
        page.clock.run_for(21000)
        assert group.locator(":scope > button").get_attribute("aria-expanded") == "true"
        assert group.locator(".request-row:visible").count() == 8
        assert group.locator(".request-note:visible").count() == 1
        assert page.locator('[data-block="requests-project:beta"] > button').get_attribute("aria-expanded") == "false"
        page.reload()
        page.locator(".requests").wait_for()
        assert group.locator(":scope > button").get_attribute("aria-expanded") == "true"
        assert page.locator('[data-block="requests-project:beta"] > button').get_attribute("aria-expanded") == "false"
        page.locator(".requests-needs").get_by_role("button", name=strings["requestsShowAll"].replace("{n}", "5")).click()
        page.wait_for_timeout(700)
        assert page.locator(".requests-needs").count() == 0
        assert page.locator(".request-project").count() == 2
        assert page.locator(".requests .request-row:visible").count() == 5
        assert not page.locator(".requests").get_by_text(strings["requestsTeam"], exact=True).count()
        page.get_by_role("button", name=strings["requestsBackAll"], exact=True).click()
        assert page.locator(".requests-needs .request-row").count() == 3
        rail.get_by_role("button", name=re.compile(r"^beta")).click()
        assert page.locator(".request-project").count() == 1
        assert page.locator(".request-project .request-row:visible").count() == 5
        assert page.evaluate("document.documentElement.scrollWidth <= 390")
    finally:
        ctx.close()


def seed_reminders():
    now = time.time()
    write_private(state_dir() / "topics.json", json.dumps({
        "home-session": {"chat": CHAT, "thread": 10, "name": "נושא הבית"},
        "other-session": {"chat": AWAY, "thread": 20, "name": "נושא אחר"}}))
    rows = [dict(k="remind", i=1, s="תזכורת מנושא אחר", at=now - DAY),
            dict(k="remind", i=2, s="ישנה בלי תאריך", at=now - 15 * DAY),
            dict(k="remind", i=3, s="תזכורת שעבר מועדה", at=now, d="2026-01-01")]
    write_private(state_dir() / board.STATE_FILE, json.dumps({
        f"{CHAT}:10": dict(board._blank(), items=[dict(k="remind", i=1, s="תזכורת הבית", at=now)]),
        f"{AWAY}:20": dict(board._blank(), items=rows)}))


@pytest.mark.parametrize("brief_home", [False, True])
def test_other_topics_clear_and_older_restores_with_real_phone_taps(browser, server, monkeypatch, brief_home):
    monkeypatch.setattr(panel.brief, "on", lambda: brief_home)
    seed_reminders()
    ctx, page = panel_browser.open_panel(browser, server, signed(extra={"start_param": panel.topic_param(CHAT, 10)}), lang="he")
    try:
        me(page)
        row = page.locator(".row").filter(has=page.get_by_text("תזכורת מנושא אחר", exact=True))
        with page.expect_response(lambda r: r.url.endswith("/api/remind")) as response:
            row.get_by_role("button", name="סמן שבוצע", exact=True).click()
        assert response.value.json() == {"ok": True, "cleared": 1}
        assert [i["i"] for i in board.items_of(AWAY, 20)] == [2, 3]
        page.get_by_text("תזכורת מנושא אחר", exact=True).wait_for(state="detached")
        older = page.locator(".oldg")
        assert older.locator(":scope > .compact-toggle").get_attribute("aria-expanded") == "false"
        older.locator(":scope > .compact-toggle").click(); page.wait_for_timeout(700)
        old = older.locator(".row").filter(has=page.get_by_text("ישנה בלי תאריך", exact=True))
        assert old.get_by_role("button").count() == 2
        with page.expect_response(lambda r: r.url.endswith("/api/remind")) as response:
            old.get_by_role("button", name="להחזיר לרשימה הפעילה", exact=True).click()
        assert response.value.json() == {"ok": True, "restored": 1}
        page.wait_for_function("() => ![...document.querySelectorAll('.oldg .nm')].some(e=>e.textContent==='ישנה בלי תאריך')")
        restored = page.locator(".row").filter(has=page.get_by_text("ישנה בלי תאריך", exact=True))
        assert restored.is_visible()
        with page.expect_response(lambda r: r.url.endswith("/api/remind")) as response:
            restored.get_by_role("button", name="סמן שבוצע", exact=True).click()
        assert response.value.json()["cleared"] == 1
        assert [i["i"] for i in board.items_of(AWAY, 20)] == [3]
        # Every remaining row, including an old dated reminder, has a clear button.
        page.get_by_text("ישנה בלי תאריך", exact=True).wait_for(state="detached")
        assert older.get_by_role("button", name="סמן שבוצע", exact=True).count() == 1
        assert page.evaluate("document.documentElement.scrollWidth <= 390")
    finally:
        ctx.close()


def test_topic_view_has_clear_button_even_without_due(browser, server):
    seed_reminders()
    ctx, page = panel_browser.open_panel(browser, server, signed(extra={"start_param": panel.topic_param(CHAT, 10)}), lang="he")
    try:
        page.locator(".tabs .tab").first.click()
        row = page.locator(".row").filter(has=page.get_by_text("תזכורת הבית", exact=True))
        with page.expect_response(lambda r: r.url.endswith("/api/remind")) as response:
            row.get_by_role("button", name="סמן שבוצע", exact=True).click()
        assert response.value.json() == {"ok": True, "cleared": 1}
        assert not board.items_of(CHAT, 10)
    finally:
        ctx.close()


def test_requests_and_reminder_mutations_require_owner_auth(server):
    put_requests("alpha", [request(1, "Private request")])
    status, _, body = call(server, init=signed(user_id=8))
    assert status == 403 and b"Private request" not in body
    assert call(server, path="/api/remind", init=signed(user_id=8), body=b'{"refs":["12345678:1"],"action":"restore"}')[0] == 403
