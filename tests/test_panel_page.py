"""The control panel page in three tabs: agent cards drawn safely, the /api/card call, native Mini App touches
behind feature checks, and the layout guards the redesign must keep."""
import re

from ctb import panel, panel_page


def js() -> str:
    page = panel_page.PAGE
    return page[page.index('<script nonce="__NONCE__">'):page.index("</script>\n</body>")]


def func(name: str) -> str:
    """The source of one top-level page function, up to the next one."""
    code = js()
    start = code.index("function %s(" % name)
    nxt = code.find("\nfunction ", start + 1)
    return code[start:nxt if nxt > 0 else len(code)]


def test_both_languages_have_the_new_tab_and_card_strings():
    he, en = panel_page.STRINGS["he"], panel_page.STRINGS["en"]
    assert he.keys() == en.keys()
    for key in ("tabMe", "tabAgents", "settings", "heroOne", "heroMany", "allClear", "cardsAsk", "cardsShow",
                "cardsOld", "cardSent", "cardAlready", "cardGone", "answerPh", "send", "openLink", "wrote"):
        assert he[key].strip() and en[key].strip(), key
    for kind in ("progress", "choice", "ask", "link", "numbers"):
        assert he["k_" + kind] and en["k_" + kind]
    assert he["tabMe"] == "בשבילי" and he["tabAgents"] == "סוכנים" and he["settings"] == "הגדרות"


def test_the_page_has_three_tabs_and_keeps_the_topic_view():
    code = js()
    tabs = func("tabs")
    assert '["me",S.tabMe],["agents",S.tabAgents],["settings",S.settings]' in tabs
    assert 'd.topic?[["here",S.here]]' in tabs  # opened from a topic: its own tab comes first
    assert 'setAttribute("role","tab")' in tabs and "aria-selected" in tabs
    # the last tab is remembered for the session, and storage that throws never breaks the page
    assert "try{view=sessionStorage.getItem(TABKEY)||null;}catch(e){}" in code
    assert "try{sessionStorage.setItem(TABKEY,v);}catch(e){}" in func("switchTo")
    render = func("render")
    for name in ("renderHere(d)", "renderAgents(d)", "renderSettings(d)", "renderAll(d)"):
        assert name in render


def test_every_agent_card_kind_is_drawn_with_text_only():
    page = panel_page.PAGE
    assert "innerHTML" not in page and "outerHTML" not in page and "insertAdjacentHTML" not in page
    assert "document.write" not in page and "eval(" not in page and "new Function" not in page
    assert "BODIES={progress:progressBody,choice:choiceBody,ask:askBody,link:linkBody,numbers:numbersBody}" in js()
    card = func("agentCard")
    assert 'shown(c,"title")' in card and 'shown(c,"detail")' in card and 'shown(c,"topic")' in card
    # option labels, step names, figures and link labels are agent text: always set as text, never markup
    assert 'el("span",null,o)' in func("choiceBody")
    assert 'el("span","sl",s)' in func("progressBody")
    nums = func("numbersBody")
    assert 'el("b",null,f.value)' in nums and 'el("span",null,f.label)' in nums and ".slice(0,3)" in nums


def test_a_card_link_opens_only_https_and_only_through_telegram():
    link = func("linkBody")
    assert "if(!/^https:\\/\\//i.test(u))return;" in link
    assert "openSafe(u)" in link and "openLink(u)" not in link.replace("openSafe(u)", "") and "href" not in link
    safe = func("openSafe")
    assert '/^https:\\/\\//i.test(u)' in safe and "wa.openLink(u)" in safe
    assert "location" not in safe  # no fallback that navigates the panel itself


def test_the_card_answer_call_matches_the_server_route():
    assert panel.ACTIONS["/api/card"] == "_card" and panel.ROUTES["/api/card"] >= 1200
    assert '{id:c.id,pick:i}' in func("choiceBody")
    assert '{id:c.id,text:t}' in func("askBody") and ',300,S.answerPh,' in func("askBody")
    assert "ta.maxLength=max" in func("composer")
    send = func("sendCard")
    assert 'post("/api/card",body)' in send
    assert "r.status===409" in send and "S.cardAlready" in send
    assert "r.status===404" in send and "S.cardGone" in send
    assert "r.status===403?S.reopen:r.status===503?S.busy:S.failed" in send
    assert 'haptic("n","success")' in send and 'haptic("n","error")' in func("sendCard")
    assert "cardBusy[c.id]" in send  # one answer in flight per card


def test_ended_cards_fold_away_and_answered_ones_stay_done():
    sections = func("cardSections")
    assert 'fold("cards-old",S.cardsOld' in sections and "c.ended" in sections
    card = func("agentCard")
    assert "cardDoneBox(done)" in card
    assert "!(c.ended&&ASKS[c.kind])" in card  # an ended question shows no answer buttons


def test_native_touches_are_feature_checked():
    code = js()
    assert "function ver(v){try{return !!(wa&&wa.isVersionAtLeast&&wa.isVersionAtLeast(v));}catch(e){return false;}}" in code
    assert 'if(!ver("6.2"))return;' in func("syncClose") and "enableClosingConfirmation" in func("syncClose")
    assert 'ver("6.1")&&wa.BackButton' in code and 'ver("7.0")&&wa.SettingsButton' in code
    assert 'ver("7.10")&&wa.setBottomBarColor' in code and 'ver("6.9")' in func("theme")
    assert "MainButton" not in code
    css = panel_page.PAGE[panel_page.PAGE.index("<style"):panel_page.PAGE.index("</style>")]
    assert "--tg-content-safe-area-inset-top" in css and "--tg-safe-area-inset-bottom" in css


def test_drafts_ask_before_the_panel_closes():
    assert "syncClose()" in func("decision") and "composer(" in func("askBody") and "syncClose()" in func("composer") and "syncClose()" in func("radioCard")


def test_the_banked_resets_line_sits_inside_its_own_meter():
    quota = func("quotaCard")
    assert 'bm=el("div","meter bk")' in quota and "bm.appendChild(bk)" in quota and "a.appendChild(bk)" not in quota


def test_the_tap_note_and_motion_guards_stay():
    page = panel_page.PAGE
    assert "S.tapNote" in func("decision")
    assert "prefers-reduced-motion:reduce" in page and "reduce=window.matchMedia" in page
    assert '"+ "+S.' not in page  # no plus-shaped marks in the copy


def test_the_policy_is_unchanged():
    assert panel_page.csp("N", "https://web.telegram.org") == (
        "default-src 'none'; script-src https://telegram.org/js/telegram-web-app.js 'nonce-N'; style-src 'nonce-N'; "
        "connect-src 'self'; img-src 'self' data:; frame-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors https://web.telegram.org")  # frame-src: cockpit apps' sandboxed frames, this server only
    page = panel.render_page("he", "N").decode()
    assert page.count('nonce="N"') == 2 and len(re.findall(r"<script\b", page)) == 2
    assert not re.search(r"\son[a-z]+=", page)  # no inline event handlers


def test_the_youtube_staff_radio_card_keeps_its_guards():
    """Must-keep elements of the YouTube staff radio card (panel_youtube.py): the card is drawn in the agents
    view, the sign-in opens only an https link through Telegram, Start always takes two taps (arm, then start
    with the token), the confirmation names the channel, and setup-pending shows no button."""
    assert "if(d.youtube){sec(S.ytTitle);app.appendChild(youtubeCard(d.youtube));}" in func("renderAgents")
    card = func("youtubeCard")
    assert 'ytPost({action:"connect"},cn)' in card and "openSafe(j.url)" in card and "/^https:\\/\\//i.test(j.url)" in card
    assert 'ytPost({action:"arm"},st)' in card and 'action:"start"' not in card  # the card itself never starts
    assert 'ytPost({action:"start",armed:t},yes)' in func("ytConfirm") and "ytArmed.channel" in func("ytConfirm")
    assert 'ytPost({action:"stop"},sp)' in card and 'ytPost({action:"disconnect"},dc)' in card
    assert "if(!y.setup||!y.setup.client||!y.setup.base){c.appendChild(el(\"div\",\"safe\",S.ytSetup));return c;}" in card
    assert 'post("/api/youtube",body)' in func("ytPost")
    assert 'role","switch"' in func("ytSwitch") and 'cb.type="checkbox"' in func("ytProjects")
