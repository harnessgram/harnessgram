"""'What the system knows' screens: exact payloads, locked rules, undo and field direction.

The node half runs everywhere CI runs (fake DOM, the page's real functions). The browser half needs Playwright
and headless Chromium; it checks layout, the must-keep contract, back steps and the owner's taps at 390 px.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ctb import panel_page
from cockpit_v2_browser import knowledge_notes_sample, knowledge_sample, sample_state
from test_panel_page import func

NODE = shutil.which("node")
HERE = Path(__file__).parent
PRELUDE = (HERE / "studio_fake_dom.js").read_text() + (HERE / "cockpit_v2_fake_dom.js").read_text()
ROOT = Path(panel_page.__file__).parent


def run(body, lang="he", data=None, reply=None):
    script = (ROOT / "cockpit_v2.js").read_text().replace("__V2_STRINGS__", json.dumps(panel_page.V2_STRINGS, ensure_ascii=False))
    script = 'var requestProject="",requestOwnerOnly=false;\n' + "\n".join(func(n) for n in ("el", "icon", "svgAdd", "he", "shown", "compactStorageKey", "compactBlock", "requestAction", "requestRow", "requestRank", "requestDetail", "requestList", "requestGroups", "requestText", "requestBoard", "v2ModeRow", "settingsBox")) + script
    script += "\n" + (ROOT / "cockpit_v2_screens.js").read_text() + "\n" + (ROOT / "cockpit_v2_knowledge.js").read_text()
    script += "\n" + (ROOT / "cockpit_v2_addons.js").read_text()  # Settings' second entry
    script += "\nvar view='settings',rendered=0;function render(){rendered++;}vkData=%s;" % json.dumps(data or knowledge_sample(lang))
    view = data or knowledge_sample(lang)
    # Reads answer with the view, changes with their log entry; timers never keep node alive.
    post = ("var VIEW=%s,REPLY=%s;sandbox.setTimeout=()=>0;sandbox.post=(path,body)=>{posted.push([path,body]);"
            "return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve(JSON.parse(JSON.stringify("
            "body.action==='view'||body.action==='scan'?VIEW:REPLY)))});};"
            % (json.dumps(view), json.dumps(reply or {"ok": True, "entry": {"id": "f" * 12}})))
    src = PRELUDE + "\n" + post + "\nbootV2(%s,%s,%s,%s);\n(async()=>{root.textContent='';%s})().catch(e=>{console.error(e);process.exit(1);});" % (
        json.dumps(script), json.dumps(sample_state(lang)), json.dumps(panel_page.STRINGS[lang]), json.dumps(lang), body)
    result = subprocess.run([NODE, "-"], input=src, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


node = pytest.mark.skipif(not NODE, reason="node is not installed")


@node
@pytest.mark.parametrize("lang", ["he", "en"])
def test_home_shows_five_kinds_vault_and_history(lang):
    got = run("sandbox.vkNav.open=true;sandbox.vkPage(sandbox.last);"
              "emit({kinds:allEls().filter(e=>e.attrs['data-vk-kind']).map(e=>e.attrs['data-vk-kind']),"
              "vault:byClass('vk-vault-tile').length,history:byClass('vk-history-row')[0].textContent,"
              "status:byClass('vk-status').length,search:allEls().filter(e=>e.attrs['data-vk-search']==='home').length});", lang)
    k = panel_page.V2_STRINGS[lang]["knowledge"]
    assert got["kinds"] == ["rule", "memory", "doc", "tool", "skill"]
    assert got["vault"] == 1 and got["status"] == 1 and got["search"] == 1
    assert got["history"].startswith(k["changesWeek"].replace("#", "4"))


@node
def test_list_has_one_switch_per_open_item_and_no_switch_for_locked_rules():
    data = knowledge_sample()
    got = run("sandbox.vkNav.open=true;sandbox.vkNav.kind='rule';sandbox.vkPage(sandbox.last);"
              "emit({switches:allEls().filter(e=>e.attrs['data-vk-toggle']).map(e=>e.attrs['data-vk-toggle']),"
              "locked:byClass('vk-locked-row').map(e=>e.textContent)});", data=data)
    open_rules = [i["id"] for i in data["items"] if i["kind"] == "rule" and not i["locked"]]
    assert got["switches"] == open_rules
    assert got["locked"] == [panel_page.V2_STRINGS["he"]["knowledge"]["vaultLine"].replace("#", "6")]


@node
@pytest.mark.parametrize("on", [True, False])
def test_switch_posts_the_exact_toggle(on):
    data = knowledge_sample()
    item = next(i for i in data["items"] if i["kind"] == "rule" and not i["locked"] and i["on"] != on)
    got = run("sandbox.vkNav.open=true;sandbox.vkNav.kind='rule';sandbox.vkPage(sandbox.last);"
              "var sw=allEls().filter(e=>e.attrs['data-vk-toggle']===%s)[0];sw.click();var checked=sw.getAttribute('aria-checked');await flush();"
              "emit({posted,checked});" % json.dumps(item["id"]), data=data)
    assert got["posted"][0] == ["/api/knowledge", {"action": "toggle", "id": item["id"], "on": on}]
    assert got["checked"] == str(on).lower()
    assert got["posted"][1] == ["/api/knowledge", {"action": "view"}]  # the counts and history are reloaded


@node
def test_locked_item_sheet_has_no_switch_and_no_suggestion():
    data = knowledge_sample()
    locked = next(i for i in data["items"] if i["locked"])
    got = run("sandbox.vkNav.open=true;sandbox.vkNav.vault=true;sandbox.vkNav.item=%s;sandbox.vkPage(sandbox.last);"
              "var sh=byClass('vk-sheet')[0];emit({switch:sh.querySelectorAll('[role=switch]').length,suggest:sh.querySelectorAll('textarea').length,"
              "note:sh.querySelectorAll('.locked').length,vaultRows:byClass('vk-vault-row').length});" % json.dumps(locked["id"]), data=data)
    assert got == {"switch": 0, "suggest": 0, "note": 1, "vaultRows": 6}


@node
def test_suggestion_posts_text_and_needs_words():
    got = run("sandbox.vkNav.open=true;sandbox.vkNav.kind='rule';sandbox.vkNav.item='rule:1.one-question';sandbox.vkPage(sandbox.last);"
              "var sh=byClass('vk-sheet')[0],ta=sh.querySelector('textarea'),send=sh.querySelector('[data-vk-suggest]');"
              "var before=send.disabled;ta.value='  לשאול שתי שאלות כשהן קשורות ';ta.fire('input');send.click();await flush();"
              "emit({before,posted,next:sh.querySelectorAll('[data-vk-next]').length});")
    assert got["before"] is True and got["next"] == 1
    assert got["posted"][0] == ["/api/knowledge", {"action": "suggest", "id": "rule:1.one-question", "text": "לשאול שתי שאלות כשהן קשורות"}]


@node
def test_history_undo_and_redo_use_the_log_ids():
    got = run("sandbox.vkNav.open=true;sandbox.vkNav.history=true;sandbox.vkPage(sandbox.last);"
              "var u=allEls().filter(e=>e.attrs['data-vk-undo'])[0];var id=u.attrs['data-vk-undo'];u.click();await flush();"
              "root.textContent='';sandbox.vkPage(sandbox.last);var redo=allEls().filter(e=>e.attrs['data-vk-redo'])[0];"
              "redo.click();await flush();emit({id,posted,undone:byClass('vk-undone').length,suggestUndo:allEls().filter(e=>e.attrs['data-vk-log']==='a1b2c3d4e5f7')[0].querySelectorAll('[data-vk-undo]').length});")
    assert got["posted"][0] == ["/api/knowledge", {"action": "undo", "log": got["id"]}]
    assert ["/api/knowledge", {"action": "undo", "log": "f" * 12}] in got["posted"]
    assert got["undone"] == 1 and got["suggestUndo"] == 0  # a suggestion is not a switch: nothing to undo


@node
def test_settings_lists_the_knowledge_entry_first():
    got = run("var d=sandbox.last;d.settings=d.settings||{};sandbox.SWITCHES=[];sandbox.S.set_x='x';"
              "sandbox.v2Capture=f=>[];sandbox.hookHealthCard=()=>{};"
              "try{sandbox.v2Settings(d);}catch(e){}emit(root.children.slice(0,2).map(e=>e.className));")
    assert got[1] == "vk-entry"


# --- plain-words notes -------------------------------------------------------------------------------------------

LIST_ROWS = ("sandbox.vkNav.open=true;sandbox.vkNav.kind='rule';sandbox.vkPage(sandbox.last);"
             "var rows=byClass('vk-row').map(r=>{var tx=r.querySelectorAll('.vk-row-text')[0],b=tx.childNodes[0],s=tx.childNodes.filter(x=>x.tagName==='span'&&!x.classList.contains('vk-advice'))[0],"
             "c=r.querySelectorAll('.vk-advice')[0];return {id:r.attrs['data-vk-item'],title:b.textContent,tdir:b.dir||'',raw:b.classList.contains('vk-raw'),"
             "sum:s?s.textContent:'',chip:c?[c.attrs['data-vk-advice'],c.textContent]:null};});"
             "var heads=byClass('vk-group-head').map(h=>{var b=h.querySelectorAll('bdi')[0];return [b.textContent,b.dir||''];});")


@node
@pytest.mark.parametrize("lang", ["he", "en"])
def test_rows_show_the_note_in_the_page_language_with_an_advice_chip(lang):
    data = knowledge_notes_sample()
    got = run(LIST_ROWS + "emit({rows,heads});", lang, data=data)
    k = panel_page.V2_STRINGS[lang]["knowledge"]
    rows = {r["id"]: r for r in got["rows"]}
    first = next(i for i in data["items"] if i["id"] == "rule:0.r0")
    assert rows["rule:0.r0"]["title"] == first["note"]["title_" + lang] and rows["rule:0.r0"]["sum"] == first["note"]["summary_" + lang]
    assert rows["rule:0.r0"]["raw"] is False and rows["rule:0.r0"]["tdir"] == ""  # a note takes the page's direction
    assert rows["rule:0.r0"]["chip"] == ["keep", k["advice_keep"]]
    assert rows["rule:4.r4"]["chip"] == ["review", k["advice_review"]]
    assert rows["rule:6.r6"]["chip"] == ["optional", k["advice_optional"]]
    # No note: the raw English words, in their own left-to-right element, and no chip.
    assert rows["rule:3.r3"] == dict(rows["rule:3.r3"], title="Night work", tdir="ltr", raw=True, chip=None)
    heads = dict(got["heads"])
    assert ("שיחה איתך" if lang == "he" else "Talking with you") in heads
    assert heads["Computer"] == "ltr"  # a heading without a note keeps its raw words, isolated left to right


@node
def test_hebrew_rows_never_put_english_words_inside_a_hebrew_line():
    got = run(LIST_ROWS + "emit({rows,heads});", "he", data=knowledge_notes_sample())
    import re
    latin, hebrew = re.compile(r"[A-Za-z]"), re.compile(r"[֐-׿]")
    for r in got["rows"]:
        for text in (r["title"], r["sum"]):
            assert not (latin.search(text) and hebrew.search(text)), text
        if latin.search(r["title"]):
            assert r["raw"] and r["tdir"] == "ltr"


@node
@pytest.mark.parametrize("lang", ["he", "en"])
def test_the_sheet_shows_why_and_the_original_words(lang):
    data = knowledge_notes_sample()
    item = next(i for i in data["items"] if i["id"] == "rule:4.r4")
    got = run("sandbox.vkNav.open=true;sandbox.vkNav.kind='rule';sandbox.vkNav.item='rule:4.r4';sandbox.vkPage(sandbox.last);"
              "var sh=byClass('vk-sheet')[0],box=sh.querySelectorAll('[data-vk-why]')[0],orig=sh.querySelectorAll('.vk-orig')[0];"
              "emit({head:byClass('vk-sheet-head')[0].childNodes[1].textContent,label:sh.attrs['aria-label'],chip:box.querySelectorAll('.vk-advice')[0].textContent,"
              "why:box.querySelectorAll('p')[0].textContent,orig:orig.textContent,odir:orig.dir,"
              "olabel:sh.querySelectorAll('.vk-orig-label')[0].textContent,first:sh.querySelectorAll('.vk-text-card')[0].querySelectorAll('p')[0].textContent});", lang, data=data)
    k = panel_page.V2_STRINGS[lang]["knowledge"]
    n = item["note"]
    assert got["head"] == got["label"] == n["title_" + lang]
    assert got["chip"] == k["advice_review"] and got["why"] == n["why_" + lang]
    assert got["first"] == n["summary_" + lang] and got["olabel"] == k["original"]
    assert got["orig"] == item["text"] and got["odir"] == "ltr"


@node
def test_a_locked_rule_with_a_note_says_keep_and_an_item_without_a_note_falls_back():
    data = knowledge_notes_sample()
    locked = next(i for i in data["items"] if i["locked"])
    locked["note"]["advice"] = "optional"  # the bridge forces keep; the page never shows anything else either
    got = run("sandbox.vkNav.open=true;sandbox.vkNav.vault=true;sandbox.vkNav.item=%s;sandbox.vkPage(sandbox.last);"
              "var sh=byClass('vk-sheet')[0];var vault=byClass('vk-vault-row')[0].querySelectorAll('b')[0].textContent;"
              "emit({chip:sh.querySelectorAll('[data-vk-advice]')[0].attrs['data-vk-advice'],vault});" % json.dumps(locked["id"]), data=data)
    assert got == {"chip": "keep", "vault": locked["note"]["title_he"]}
    why = run("sandbox.vkNav.open=true;sandbox.vkNav.vault=true;sandbox.vkNav.item=%s;sandbox.vkPage(sandbox.last);"
              "emit(byClass('vk-advice-box')[0].querySelectorAll('p')[0].textContent);" % json.dumps(locked["id"]), data=data)
    assert why == panel_page.V2_STRINGS["he"]["knowledge"]["lockedWhy"]
    got = run("sandbox.vkNav.open=true;sandbox.vkNav.kind='rule';sandbox.vkNav.item='rule:3.r3';sandbox.vkPage(sandbox.last);"
              "var sh=byClass('vk-sheet')[0],h=byClass('vk-sheet-head')[0].childNodes[1],p=sh.querySelectorAll('.vk-text-card')[0].querySelectorAll('p')[0];"
              "emit({h:h.textContent,hdir:h.dir,p:p.dir,why:sh.querySelectorAll('[data-vk-why]').length,orig:sh.querySelectorAll('.vk-orig').length});", data=data)
    assert got == {"h": "Night work", "hdir": "ltr", "p": "ltr", "why": 0, "orig": 0}


@node
def test_search_finds_an_item_by_its_hebrew_note():
    got = run("sandbox.vkNav.open=true;sandbox.vkPage(sandbox.last);var ta=allEls().filter(e=>e.attrs['data-vk-search']==='home')[0];"
              "ta.value='שאלה אחת';ta.fire('input');emit(byClass('vk-row').map(r=>r.attrs['data-vk-item']));", data=knowledge_notes_sample())
    assert got == ["rule:1.one-question"]


@node
def test_history_puts_a_raw_title_on_its_own_line_on_a_hebrew_page():
    data = knowledge_notes_sample()
    got = run("sandbox.vkNav.open=true;sandbox.vkNav.history=true;sandbox.vkPage(sandbox.last);"
              "emit(byClass('vk-hist').map(r=>{var l=r.querySelectorAll('.vk-raw-line')[0];return [r.attrs['data-vk-log'],"
              "r.querySelectorAll('b')[0].textContent,l?[l.textContent,l.dir]:null];}));", data=data)
    rows = {r[0]: r for r in got}
    assert rows["a1b2c3d4e5f6"][2] == ["Night work", "ltr"]  # rule:3.r3 has no note
    assert rows["a1b2c3d4e5f8"][2] is None and "סיכום קצר בסוף" in rows["a1b2c3d4e5f8"][1]


# --- the browser half --------------------------------------------------------------------------------------------

def _browser():
    pytest.importorskip("playwright")
    return pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    sync_api = _browser()
    import panel_browser
    panel_browser.prepare_env()
    with sync_api.sync_playwright() as p:
        b = panel_browser.launch(p)
        if b is None:
            pytest.skip("headless Chromium cannot run in this environment")
        yield b
        b.close()


@pytest.fixture
def opened(browser):
    from cockpit_v2_browser import open_v2, rich_state
    made = []

    def make(lang="he", scheme="light"):
        ctx, page, calls, errors, state = open_v2(browser, lang, scheme, state=rich_state(lang))
        made.append(ctx)
        return page, calls, errors
    yield make
    for ctx in made:
        ctx.close()


def _screens():
    import cockpit_v2_screenshots as shots
    return {"k-home": shots.go_knowledge, "k-rules": shots.go_k_rules, "k-item": shots.go_k_item,
            "k-vault": shots.go_k_vault, "k-skill": shots.go_k_skill, "k-history": shots.go_k_history}


@pytest.mark.parametrize("screen", ["k-home", "k-rules", "k-item", "k-vault", "k-skill", "k-history"])
@pytest.mark.parametrize("lang,scheme", [("he", "light"), ("en", "dark")])
def test_screen_must_keep_contract(opened, screen, lang, scheme):
    from cockpit_v2_browser import assert_screen_contract
    page, calls, errors = opened(lang, scheme)
    _screens()[screen](page)
    if screen in ("k-item", "k-skill"):
        # The open sheet covers the tab bar; its own controls keep the 44 px targets.
        small = page.evaluate("Array.from(document.querySelectorAll('.vk-sheet button')).filter(b=>b.getBoundingClientRect().height<44).length")
        assert small == 0
    else:
        assert_screen_contract(page, screen)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert not errors
    assert all(path == "/api/knowledge" and body["action"] == "view" for path, body in calls)


def test_settings_contract_keeps_five_groups_and_adds_the_entry(opened):
    import cockpit_v2_screenshots as shots
    from cockpit_v2_browser import assert_screen_contract
    page, calls, errors = opened()
    shots.go_settings(page)
    assert_screen_contract(page, "settings")
    assert page.locator("#app > *").nth(1).get_attribute("data-v2-section") == "knowledge-entry"
    assert not calls  # nothing private is fetched until the owner opens it


def test_back_steps_close_sheet_list_and_home_in_order(opened):
    page, calls, errors = opened()
    _screens()["k-item"](page)
    page.evaluate("window.__backClick()")
    page.locator(".vk-sheet").wait_for(state="detached")
    assert page.locator(".vk-list").is_visible()
    page.locator(".vk-list [data-vk-back]").click()
    page.locator(".vk-tiles").wait_for()
    page.evaluate("window.__backClick()")
    page.locator('[data-v2-section="settings-notify"]').wait_for()
    assert not errors


def test_toggle_and_undo_from_the_phone(opened):
    page, calls, errors = opened()
    _screens()["k-rules"](page)
    sw = page.locator('.vk-row[data-vk-item="rule:3.r3"] [role="switch"]')
    assert sw.get_attribute("aria-checked") == "false"
    sw.click()
    page.wait_for_timeout(300)
    assert ("/api/knowledge", {"action": "toggle", "id": "rule:3.r3", "on": True}) in calls
    assert page.locator('.vk-row[data-vk-item="rule:3.r3"] [role="switch"]').get_attribute("aria-checked") == "true"
    page.evaluate("window.__backClick()")
    page.locator(".vk-history-row").click()
    first = page.locator("[data-vk-undo]").first
    log = first.get_attribute("data-vk-undo")
    first.click()
    page.locator(".vk-snack [data-vk-redo]").wait_for()
    assert ("/api/knowledge", {"action": "undo", "log": log}) in calls
    assert not errors


def test_empty_fields_take_the_page_direction(opened):
    page, calls, errors = opened()
    _screens()["k-item"](page)
    ta = page.locator(".vk-sheet textarea")
    assert ta.evaluate("e=>e.dir") == "rtl"
    ta.fill("check this first")
    assert ta.evaluate("e=>e.dir") == "auto"
    page.evaluate("window.__backClick()")
    search = page.locator(".vk-list .vk-search textarea")
    assert search.evaluate("e=>e.dir") == "rtl"
    search.fill("שאלה")
    assert search.evaluate("e=>e.dir") == "auto"
    assert page.locator(".vk-list .vk-row").count() == 1


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_notes_fit_a_390px_hebrew_phone(browser, scheme):
    import cockpit_v2_screenshots as shots
    from cockpit_v2_browser import open_v2, rich_state
    ctx, page, calls, errors, state = open_v2(browser, "he", scheme, state=rich_state("he"), knowledge=knowledge_notes_sample())
    try:
        shots.go_k_rules(page)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        got = page.evaluate("""() => {
          const box = e => e.getBoundingClientRect();
          const lines = e => Math.round(box(e).height / parseFloat(getComputedStyle(e).lineHeight));
          const rows = [...document.querySelectorAll('.vk-list .vk-row')];
          const list = box(document.querySelector('.vk-list'));
          const chips = [...document.querySelectorAll('.vk-list .vk-advice')];
          return {
            gutter: Math.min(list.left, window.innerWidth - list.right),
            names: Math.max(...rows.map(r => lines(r.querySelector('.vk-row-text b')))),
            chips: chips.length, chipInside: chips.every(c => { const b = box(c), r = box(c.closest('.vk-row')); return b.left >= r.left && b.right <= r.right; }),
            raw: [...document.querySelectorAll('.vk-list .vk-raw')].map(e => [e.textContent, getComputedStyle(e).direction]),
            pad: Math.min(...rows.map(r => parseFloat(getComputedStyle(r.querySelector('.vk-row-main')).paddingInlineStart))),
            // A raw heading stays beside its icon in the page direction (right to left), not at the far left.
            head: (() => { const h = document.querySelector('.vk-group[data-vk-group="Computer"] .vk-group-head');
                           return box(h.querySelector('svg')).left - box(h.querySelector('.vk-raw')).right; })(),
          };
        }""")
        assert got["gutter"] >= 16 and got["names"] <= 2 and got["pad"] >= 12
        assert 0 <= got["head"] <= 16, got["head"]
        assert got["chips"] >= 5 and got["chipInside"]
        assert ["Night work", "ltr"] in got["raw"] and ["Computer", "ltr"] in got["raw"]
        page.locator('.vk-row[data-vk-item="rule:4.r4"] .vk-row-main').click()
        page.locator(".vk-sheet [data-vk-why]").wait_for()
        assert page.locator(".vk-sheet .vk-orig").evaluate("e => getComputedStyle(e).direction") == "ltr"
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not errors
    finally:
        ctx.close()
