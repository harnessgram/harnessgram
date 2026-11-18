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


def open_fold(page, block):
    """Open a collapsed block and wait out its short reveal."""
    block.locator(":scope > .compact-toggle").click()
    page.wait_for_timeout(700)


@pytest.mark.parametrize("brief_home", [False, True])
def test_hebrew_requests_group_expand_done_poll_and_no_overflow(browser, server, monkeypatch, brief_home):
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
        mine = page.locator('[data-block="requests-group:owner:alpha"]')
        assert mine.locator(":scope > .compact-toggle").get_attribute("aria-expanded") == "true"
        assert mine.locator(".request-row:visible .nm").all_text_contents() == ["לבדוק את המסך"]
        # The row is one compact line; the full text, note and steps wait behind a tap and the buttons stay in view.
        first = mine.locator(".request-row").first
        assert not first.locator(".request-details").is_visible()
        assert first.locator(".request-actions").is_visible()
        first.locator(".request-toggle").click()
        first.locator(".request-note").wait_for()
        assert first.locator(".request-note").inner_text() == "הבדיקה הושלמה"
        assert first.locator(".request-steps ol").inner_text().splitlines() == ["בדיקה ראשונה", "הבדיקה הושלמה"]
        assert first.locator(".request-actions button:visible").count() == 3
        # Items waiting on agents and finished ones stay folded until asked for.
        agents = page.locator('[data-block="requests-agents"]')
        assert agents.locator(":scope > .compact-toggle").get_attribute("aria-expanded") == "false"
        open_fold(page, agents)
        assert agents.locator('[data-block="requests-group:agents:alpha"] .nm').all_text_contents() == ["להכין את הגרסה"]
        long_row = agents.locator('[data-block="requests-group:agents:beta"] .request-row').first
        assert long_row.locator(".nm").evaluate("e => e.scrollWidth <= e.clientWidth + 1 || getComputedStyle(e).textOverflow === 'ellipsis'")
        long_row.locator(".request-toggle").click()
        long_row.locator(".request-note").wait_for()
        done = page.locator('[data-block="requests-done"]')
        assert not page.locator(".requests").get_by_text("בקשה שבוצעה", exact=True).is_visible()
        open_fold(page, done)
        assert done.get_by_text("בקשה שבוצעה", exact=True).is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        # A normal state refresh sees the external file.
        put_requests("beta", [request(1, "בקשה ראשונה"), request(2, "בקשה שהגיעה עכשיו")])
        page.clock.run_for(61000)  # one idle poll (20 s), plus the brief's slower refresh when it is the home
        agents.get_by_text("בקשה שהגיעה עכשיו", exact=True).wait_for()
    finally:
        ctx.close()


@pytest.mark.parametrize("lang", ["he", "en"])
def test_requests_phone_groups_paging_long_wait_and_remembered_folds(browser, server, monkeypatch, lang):
    monkeypatch.setattr(panel.brief, "on", lambda: False)
    long_title = "בקשה עם כותרת ארוכה " * 20
    note = "הערה מלאה לבקשה " * 30
    put_requests("alpha", [request(n, long_title + str(n), wait="owner" if n <= 4 else "team",
                                   notes=["שלב ראשון", note]) for n in range(1, 9)])
    put_requests("beta", [request(n, "בקשת צוות " + str(n)) for n in range(1, 13)])
    put_requests("gamma", [request(1, "מחכה לך", wait="owner")])
    put_requests("oldie", [request(1, "ממתינה הרבה זמן", wait="owner", updated=time.time() - 9 * DAY)])
    put_requests("completed", [request(1, "בקשה סגורה", status="done")])
    for n in range(8):
        put_requests("project-" + str(n), [request(1, "משימה")])
    server.lang = lang  # the page is served in the server's UI language; the browser locale alone does not switch it
    ctx, page = panel_browser.open_panel(browser, server, signed(), lang=lang, width=390)
    copy = panel_page.V2_STRINGS[lang]["requests"]
    try:
        page.clock.install()
        page.reload()
        page.locator(".tabs .tab").filter(has_text=panel_page.STRINGS[lang]["tabMe"]).first.click()
        page.locator(".requests").wait_for()
        assert page.locator("html").get_attribute("dir") == ("rtl" if lang == "he" else "ltr")
        # Needs you: alpha (4) and gamma (1); the nine-day-old item is not here.
        assert page.locator(".requests > .requests-needs > .requests-head .requests-count").inner_text() == "5"
        owner = page.locator('[data-block^="requests-group:owner:"]')
        assert owner.evaluate_all("es => es.map(e => e.getAttribute('data-block'))") == ["requests-group:owner:alpha", "requests-group:owner:gamma"]
        assert owner.first.locator(".request-row:visible").count() == 4
        assert page.locator(".requests-fold").evaluate_all("es => es.map(e => e.dataset.requests)") == ["agents", "long", "done"]
        assert page.locator('[data-block="requests-long"] .request-row').count() == 1
        assert page.evaluate("document.documentElement.scrollWidth <= 390")
        # Agents are on it: collapsed by default, five rows per project, then Show all.
        agents = page.locator('[data-block="requests-agents"]')
        assert agents.locator(":scope > .compact-toggle").get_attribute("aria-expanded") == "false"
        open_fold(page, agents)
        beta = agents.locator('[data-block="requests-group:agents:beta"]')
        assert beta.locator(".compact-figure").first.inner_text() == "12"
        assert beta.locator(".request-row:visible").count() == 5
        more = beta.get_by_role("button", name=copy["showAll"].replace("{n}", "12"), exact=True)
        assert more.is_visible()
        row = beta.locator(".request-row").first
        toggle = row.locator(".request-toggle")
        assert toggle.get_attribute("aria-expanded") == "false"
        assert toggle.bounding_box()["height"] >= 44
        style = row.locator(".nm").evaluate("e => [getComputedStyle(e).whiteSpace, getComputedStyle(e).webkitLineClamp]")
        assert style == ["pre-line", "3"]  # a long title wraps to three lines, then clips
        tops = row.locator(".mt > *").evaluate_all("es => new Set(es.map(e => Math.round(e.getBoundingClientRect().top + e.getBoundingClientRect().height / 2))).size")
        assert tops == 1  # the chip and the age share one metadata line
        more.click()
        assert beta.locator(".request-row:visible").count() == 12
        assert not beta.get_by_role("button", name=copy["showAll"].replace("{n}", "12"), exact=True).count()
        # Opening a row fetches its text and shows the three buttons.
        alpha_row = owner.first.locator(".request-row").first
        alpha_row.locator(".request-toggle").click()
        alpha_row.locator(".request-note").wait_for()
        assert alpha_row.locator(".request-note").inner_text() == note.strip()
        assert alpha_row.locator(".request-steps li").count() == 2
        assert alpha_row.locator(".request-full").inner_text().startswith(long_title.strip())
        toggle = alpha_row.locator(".request-toggle")
        toggle.focus()
        page.keyboard.press("Enter")
        assert not alpha_row.locator(".request-note").is_visible()
        page.keyboard.press("Space")
        assert alpha_row.locator(".request-note").is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= 390")
        # State polls and a reload keep the folds, paging and the open row.
        page.clock.run_for(21000)
        assert agents.locator(":scope > .compact-toggle").get_attribute("aria-expanded") == "true"
        assert beta.locator(".request-row:visible").count() == 12
        assert alpha_row.locator(".request-note").is_visible()
        page.reload()
        page.locator(".requests").wait_for()
        assert page.locator('[data-block="requests-agents"] > .compact-toggle').get_attribute("aria-expanded") == "true"
        assert page.locator('[data-block="requests-long"] > .compact-toggle').get_attribute("aria-expanded") == "false"
        assert page.evaluate("document.documentElement.scrollWidth <= 390")
        # The narrowest common phone: the compact line still fits, open or not.
        page.set_viewport_size({"width": 320, "height": 700})
        page.locator('[data-block="requests-long"] > .compact-toggle').click()
        page.wait_for_timeout(700)
        page.locator('[data-block="requests-long"] .request-toggle').first.click()
        assert page.evaluate("document.documentElement.scrollWidth <= 320")
        assert page.locator(".requests").evaluate("e => e.scrollWidth <= e.clientWidth")
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
