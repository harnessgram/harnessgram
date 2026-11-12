"""The Morning Brief on the page: the must-keep contract of the new home (every part present, one action per
waiting item, ages everywhere), the switch that leaves the classic home untouched, the ask box's empty-field
direction, and text-only drawing. Checked on the page source; the screenshots are made from the same page."""
import re

from ctb import panel, panel_page, panel_settings
from test_panel_page import func, js

MUST_KEEP = ("headline", "since", "waiting", "lines", "noticed", "teams", "ask")


def test_the_home_keeps_every_part_of_the_chosen_concept():
    render = func("renderBrief")
    for part in ("briefHead(b,d)", "sinceStrip(b)", "waitBox(b,d)", "linesBox(b)", "noticedBox(b)", "teamsRow(b)", "askBox(b)"):
        assert part in render, part
    code = js()
    for key in MUST_KEEP:  # each part is findable by its data key (the ask chips jump to them)
        assert re.search(r'"%s"' % key, code), key
    assert 'c.setAttribute("data-k",key)' in func("bCard") and 'setAttribute("data-k","teams")' in func("teamsRow")
    assert 'setAttribute("data-k","ask")' in func("askBox")


def test_the_brief_replaces_only_the_home_and_only_behind_its_switch():
    render = func("render")
    assert "if(d.brief)renderBrief(d);else renderAll(d);" in render
    assert 'if(d.brief)list.forEach(function(p){if(p[0]==="me")p[1]=S.tabBrief;});' in func("tabs")
    assert "brief_home" in panel_settings.SETTINGS and '"brief_home"]' in js()  # the settings switch


def test_every_waiting_item_has_one_action():
    row = func("waitRow")
    # a question opens its own answer card; otherwise a topic link; otherwise its details and source: exactly one
    assert row.count("r.appendChild(b)") == 1 and row.count("r.appendChild(bAct(") == 1 and row.count("r.appendChild(db)") == 1
    assert "if(t){" in row and "else if(w.url)" in row and "else{var dt=" in row and "S.src_ledger" in row
    assert "decision(t,true)" in row and "agentCard(t)" in row  # the existing answer cards, with their guards


def test_every_fact_shows_its_age_and_stale_facts_grey():
    for name in ("waitRow", "lineRow", "noticedBox"):
        assert "agoS(" in func(name), name
    assert "agoS(s.age)" in func("sinceStrip")
    assert 'x.stale?" stale":""' in func("lineRow") and 'w.stale?" stale":""' in func("waitRow")
    assert 'n.stale?" stale":""' in func("noticedBox") and "S.staleBadge" in func("lineRow")
    css = panel_page.PAGE[panel_page.PAGE.index("<style"):panel_page.PAGE.index("</style>")]
    assert ".bl.stale .bl-s" in css and ".bw.stale .bw-t" in css


def test_tones_come_from_the_server_not_from_words():
    row = func("lineRow")
    assert '"bl t-"+x.tone' in row and "S[\"why_\"+x.why]" in row
    assert "x.tone===\"bad\"&&x.url" in row  # the one action of a stuck line opens its topic


def test_the_ask_box_input_takes_the_page_direction_while_empty():
    box = func("askBox")
    assert "tune(inp,false)" in box and 'dir="auto"' not in box and ".dir=" not in box
    assert 'ta.dir=ta.value?"auto":(RTL?"rtl":"ltr")' in func("tune")


def test_agent_text_is_drawn_as_text_and_names_are_isolated():
    for name in ("waitRow", "lineRow", "noticedBox", "lineDetails"):
        code = func(name)
        assert "innerHTML" not in code and "insertAdjacentHTML" not in code
    assert 'k.dir="auto"' in func("waitRow") and 'n.dir="auto"' in func("lineRow")
    assert "openLink(w.url)" in func("waitRow") and "openLink(x.url)" in func("lineRow")


def test_the_marker_moves_only_when_the_page_is_left():
    mark = func("markBrief")
    assert '"/api/brief-seen"' in mark and "keepalive:true" in mark and "{at:at}" in mark
    assert 'if(document.hidden)markBrief();' in js() and 'addEventListener("pagehide",markBrief)' in js()
    assert panel.ACTIONS["/api/brief-seen"] == "_brief_seen"


def test_the_brief_strings_exist_in_both_languages():
    he, en = panel_page.STRINGS["he"], panel_page.STRINGS["en"]
    keys = ["tabBrief", "since", "sinceFirst", "waitNone", "lines", "noticed", "teams", "askPh", "askSoon", "staleBadge",
            "hlCalm", "set_brief_home", "hint_brief_home"] + ["q_" + k for k in ("waiting", "stuck", "working", "changed", "mac")]
    for key in keys:
        assert he[key].strip() and en[key].strip(), key
    for form in ("numsA", "numsC", "numsF"):
        assert len(he[form].split(",")) == len(en[form].split(",")) == 11
    assert he["brief_morning"] == "תדריך הבוקר"


def test_numbers_and_dates_go_through_intl():
    code = js()
    assert "new Intl.RelativeTimeFormat(LANG,{numeric:\"auto\",style:\"short\"})" in code
    assert "new Intl.ListFormat(LANG,{type:\"conjunction\"})" in code
    assert "new Intl.DateTimeFormat(LANG,{weekday:\"long\"})" in func("briefHead")
    assert "nf.format(n)" in func("word")  # above ten: digits by locale
    assert 'replace(/\\s*\\(\\d+\\)/,"")' in func("agoS")  # Hebrew ICU's "(1)" after a single unit is dropped
