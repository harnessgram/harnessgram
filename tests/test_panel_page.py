"""The control panel page in three tabs: agent cards drawn safely, the /api/card call, native Mini App touches
behind feature checks, and the layout guards the redesign must keep."""
from pathlib import Path
import re
import subprocess

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
    for key in ("tabMe", "tabAgents", "settings", "moreTabs", "heroOne", "heroMany", "allClear", "cardsAsk", "cardsShow",
                "cardsOld", "cardSent", "cardAlready", "cardGone", "answerPh", "send", "openLink", "wrote"):
        assert he[key].strip() and en[key].strip(), key
    for kind in ("progress", "choice", "ask", "link", "numbers"):
        assert he["k_" + kind] and en["k_" + kind]
    assert he["tabMe"] == "בשבילי" and he["tabAgents"] == "סוכנים" and he["settings"] == "הגדרות"
    assert he["activityAgent"] == "סוכן" and en["activityAgent"] == "Agent"
    assert 'x.task||x.type||x.name||S.activityAgent' in func("activityFeed")


def test_old_home_offers_one_tap_return_to_v2_only_when_it_is_off():
    assert panel_page.STRINGS["he"]["backToV2"] == "חזרה לעיצוב הקוקפיט החדש"
    assert panel_page.STRINGS["en"]["backToV2"] == "Back to the new Cockpit design"
    card = func("v2ReturnCard")
    assert 'if(!d.settings||d.settings.ui_v2!=="off")return null' in card
    assert 'post("/api/setting",{name:"ui_v2",value:"on"})' in card
    assert '.then(function(){load();})' in card
    render = func("render")
    assert 'if(view==="me"){var v2Return=v2ReturnCard(d);if(v2Return)app.appendChild' in render


def test_session_fork_button_requires_arm_then_explicit_confirmation():
    from ctb.i18n import t

    for lang in ("he", "en"):
        assert panel_page.STRINGS[lang]["forkAction"] == t("fork_action", lang=lang)
        assert panel_page.STRINGS[lang]["forkConfirm"] == t("fork_confirm", lang=lang)
    assert panel.ACTIONS["/api/fork"] == "_fork"
    assert 'if(!skipFork&&s.forkable&&s.state!=="ended")host.appendChild(forkRow(s));' in func("details")
    assert 'if(s.forkable&&s.state!=="ended")b.appendChild(forkRow(s));' in func("sessionRow")
    row = func("forkRow")
    assert 'armed?{action:"start",session:s.id,armed:armed}:{action:"arm",session:s.id}' in row
    assert 'post("/api/fork",body)' in row and 'if(armed)box.appendChild(el("div",null,S.forkConfirm))' in row
    assert "S.cancelIt" in row and "delete forkArmed[s.id];draw()" in row
    assert "if(forkBusy[s.id])return" in row and "go.disabled=!!forkBusy[s.id]" in row
    assert row.count("e.preventDefault();e.stopPropagation()") == 2  # no summary toggle on action taps


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
    assert 'el("span",null,label)' in func("choiceBody") and 'shown(c,"options")[i]' in func("choiceBody")
    assert 'el("span","sl",shown(c,"steps")[i])' in func("progressBody")
    nums = func("numbersBody")
    assert 'el("b",null,shown(f,"value"))' in nums and 'el("span",null,shown(f,"label"))' in nums and ".slice(0,3)" in nums


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
    # The bottom buttons belong to the cockpit_native module alone (tests/test_cockpit_native.py checks its wrapper).
    native = (Path(panel_page.__file__).parent / "cockpit_native.js").read_text(encoding="utf-8").split("\n", 1)[1]
    assert "MainButton" in native and "MainButton" not in code.replace(native, "")
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


def test_the_policy_allows_only_the_official_youtube_player_origins():
    assert panel_page.csp("N", "https://web.telegram.org") == (
        "default-src 'none'; script-src https://telegram.org/js/telegram-web-app.js 'nonce-N' https://www.youtube.com/iframe_api https://www.youtube.com/s/player/; style-src 'nonce-N'; "
        "connect-src 'self'; media-src 'self'; img-src 'self' data:; frame-src 'self' https://www.youtube-nocookie.com; base-uri 'none'; form-action 'none'; "
        "frame-ancestors https://web.telegram.org")  # frame-src: cockpit apps' sandboxed frames, this server only
    page = panel.render_page("he", "N").decode()
    assert page.count('nonce="N"') == 2 and len(re.findall(r"<script\b", page)) == 2
    assert not re.search(r"\son[a-z]+=", page)  # no inline event handlers


def test_my_music_tile_uses_official_player_and_ducks_under_radio():
    page = panel_page.PAGE
    assert "המוזיקה שלי" in panel_page.STRINGS["he"].values()
    assert 's.src="https://www.youtube.com/iframe_api"' in page
    assert 'host:"https://www.youtube-nocookie.com"' in page
    assert 'setVolume(20)' in page and "myMusicDuck" in page
    assert "min-inline-size:200px;min-block-size:200px" in page


def test_my_music_radio_ducking_is_idempotent_and_restores_on_every_exit():
    duck = func("myMusicDuck")
    assert "on===myMusicDucked" in duck
    assert duck.index("myMusicPreviousVolume=myMusicPlayer.getVolume()") < duck.index("myMusicDucked=true")
    assert "myMusicPlayer.setVolume(myMusicPreviousVolume);myMusicDucked=false" in duck

    radio = func("radioPlayerDraw")
    assert 'if(typeof myMusicDuck==="function")myMusicDuck(event==="play"||event==="playing"||event==="waiting")' in radio
    assert 'event==="pause"||event==="ended"||event==="error"' in radio
    assert "radioState=\"error\";radioMessage=S.radioPlayerError;if(typeof myMusicDuck===\"function\")myMusicDuck(false)" in func("radioPlayerPlay")

    load = func("myMusicLoad")
    assert "events:{onReady:function()" in load
    assert "radioAudio&&!radioAudio.paused&&!radioAudio.ended&&!radioAudio.error" in load
    assert "myMusicDuck(true)" in load

    page = js()
    assert 'window.addEventListener("pagehide",function(){myMusicDuck(false);})' in page
    assert 'document.addEventListener("visibilitychange",function(){if(document.hidden)myMusicDuck(false);})' in page


def test_the_youtube_staff_radio_card_keeps_its_guards():
    """Must-keep elements of the YouTube staff radio card (panel_youtube.py): the card is drawn in the agents
    view, the sign-in opens only an https link through Telegram, Start always takes two taps (arm, then start
    with the token), the confirmation names the channel, and setup-pending shows no button."""
    assert "if(d.youtube){sec(S.ytTitle);app.appendChild(youtubeCard(d.youtube));}" in func("renderAgents")
    card = func("youtubeCard")
    assert 'ytPost({action:"connect"},cn)' in card and "openSafe(j.url)" in card and "/^https:\\/\\//i.test(j.url)" in card
    assert 'ytPost({action:"arm"},st)' in card and 'action:"start"' not in card  # the card itself never starts
    assert 'ytPost({action:mirror?"mirror-on":"start",armed:t},yes)' in func("ytConfirm") and "ytArmed.channel" in func("ytConfirm")
    assert 'ytPost({action:"stop"},sp)' in card and 'ytPost({action:"disconnect"},dc)' in card
    assert "if(!y.setup||!y.setup.client||!y.setup.base){c.appendChild(el(\"div\",\"safe\",S.ytSetup));return c;}" in card
    assert 'post("/api/youtube",body)' in func("ytPost")
    assert 'role","switch"' in func("ytSwitch") and 'cb.type="checkbox"' in func("ytProjects")


def test_poll_render_stabilizes_the_viewport_and_skips_unchanged_trees():
    render = func("render")
    page = panel_page.PAGE
    assert "fp===renderFingerprint" in render
    assert "updateRelativeTimes(previous)" in render
    assert "firstVisibleAnchor()" in render and "settleAndRestore(anchor,y,oldHeight,!changedView&&!newContent)" in render
    assert 'if(changedView)app.style.minHeight="";else app.style.minHeight=Math.ceil(oldHeight)+"px"' in render
    assert "panelEditing()||now-lastInteraction<800" in render
    assert "now-renderDeferredSince<10000" in render
    assert "restoreEditor(editor)" in render
    assert '["touchstart","touchmove","scroll","wheel"]' in page
    assert 'animateEntries=changedView||renderedView===null' in render
    assert 'data-key","session:"+s.id' in page


def test_every_generated_image_reserves_intrinsic_space():
    page = panel_page.PAGE
    assert 'img.width=4;img.height=3' in page
    assert 'img.width=160;img.height=100' in page


def test_poll_fingerprint_covers_the_whole_payload_except_clock_fields():
    page = panel_page.PAGE
    fingerprint = func("fingerprint")
    assert "stableCopy(d)" in fingerprint and "viewPayload" not in page
    volatile = func("volatileKey")
    for clock_key in ('k==="age"', 'k==="now"', '/_age$/', '/_ago$/', 'server_'):
        assert clock_key in volatile
    # No rendered domain field may become another allow-list omission.
    for key in ("tasks", "mac", "topic_cleanup", "deliveries", "quota", "bank",
                "radio", "room", "podcast", "youtube", "default_model", "models",
                "listen_link", "widget_devices"):
        assert key not in volatile


def test_scroll_stability_helpers_require_an_explicit_test_flag():
    page = panel_page.PAGE
    assert 'if(window.__CTB_TEST__===true)window.__ctbTest=Object.freeze({fingerprint:fingerprint,render:render});' in page
    assert page.count("window.__ctbTest=") == 1


def test_deferred_render_and_anchor_restore_have_hard_safety_guards():
    page = panel_page.PAGE
    render = func("render")
    settle = func("settleAndRestore")
    assert "render(last,false)" in render
    assert "if(!renderDeferred)" in render
    assert "now-renderDeferredSince<10000" in render
    assert "Math.abs(window.scrollY-token.expectedY)>4" in settle
    assert "pendingRestore!==token" in settle
    assert 'renderNewContent=true;render(last,true)' in (Path(panel_page.__file__).with_name("cockpit_v2_screens.js").read_text())


def test_no_comma_return_that_leaks_globals():
    # `return null,x=[]` returns the last value and assigns an undeclared global (a throw in strict mode).
    assert not re.search(r"return\s+null\s*,", panel_page.PAGE)


def test_settle_and_restore_runs_in_strict_mode():
    # The page is strict: a comma chain after a statement (`a=b,imgs=...`) assigns undeclared globals and
    # throws a ReferenceError on every full render ("error showing the board").
    page = panel_page.render_page("en", "test").decode()
    start = page.index("function settleAndRestore(")
    end = page.index("\nfunction ", start + 10)
    script = '"use strict";\n' + r'''
var pendingRestore=null;
var app={style:{},querySelectorAll:function(){return [];}};
global.window={scrollY:0,scrollTo:function(){},scrollBy:function(){}};
global.requestAnimationFrame=function(f){f();};
''' + page[start:end] + r'''
settleAndRestore(null,0,0,true);
if(pendingRestore===null)throw new Error("token not kept");
'''
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, timeout=20)
