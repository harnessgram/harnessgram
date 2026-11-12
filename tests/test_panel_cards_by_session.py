"""Agent cards in the Cockpit are grouped by session: one head per session (robot, topic name and tag once, and a
button into the session's own Telegram topic), its cards inside without those parts, newest first. A card title
that repeats its topic's "<project>:" prefix shows without it; the stored card keeps its text."""
import json
import re
import subprocess
import threading

import pytest

from conftest import GOOD_TOKEN
from ctb import cards, panel, panel_page, panel_view, sessions_menu
from ctb.config import Config, state_dir, write_private
from test_panel import OWNER, call, signed
from test_panel_page import func, js

CHAT = -1001234567
LINK_A = "https://t.me/c/1234567/10"
LINK_B = "https://t.me/c/1234567/11"

DOM = r"""
class Element {
 constructor(tag){this.tagName=tag;this.children=[];this.style={};this.listeners={};this._text="";this.className="";this.parentNode=null;}
 appendChild(c){c.parentNode=this;this.children.push(c);return c;}
 set textContent(t){this._text=String(t);this.children=[];}
 get textContent(){return this._text+this.children.map(c=>c.textContent).join("");}
 setAttribute(k,v){this[k]=v;}
 addEventListener(k,f){this.listeners[k]=f;}
 get classList(){var n=this;return {add:function(c){n.className+=" "+c;},contains:function(c){return n.className.split(" ").includes(c);}};}
}
var document={createElement:t=>new Element(t)};
var S=STRINGS,opened={},cardDone={},target="",seenAt=0,nf={format:String},app=new Element("div");
var ASKS={choice:1,ask:1},KIND_ICON={},BODIES={},calls=[],location={href:""};
var last={now:0,sessions:SESSIONS,cards:[]},wa=WA;
function shown(x,f){var h=x[f+"_he"];return typeof h==="string"&&h?h:x[f];}
function he(x,f){return x[f+"_he"];}
function icon(n){var s=new Element("svg");s.className="icon-"+n;return s;}
function robot(sid,state,small){var r=new Element("div");r.className="bot";r.sid=sid;return r;}
function enter(n){return n;}
function ago(a){return "";}
function isNew(){return false;}
function source(){}
function haptic(){}
function markSeen(){}
function all(n){return [n].concat(n.children.flatMap(all));}
function pick(n,cls){return all(n).filter(x=>(x.className||"").split(" ").includes(cls));}
function texts(n,cls){return pick(n,cls).map(x=>x.textContent);}
"""

HELPERS = ("el", "sec", "fold", "openLink", "sidTag", "sessionOf", "agentCard", "bare", "prefixOf", "unprefixed",
           "topicLinkOf", "tgBtn", "sesKey", "bySession", "sessionGroup", "cardGroups", "allCards", "oldCards", "cardSections")


def run(script, sessions=(), wa="{openTelegramLink:function(u){calls.push(['tg',u]);},openLink:function(u){calls.push(['web',u]);}}",
        lang="he"):
    marks = re.search(r"var BIDI_MARKS=[^\n]*", js()).group(0)
    program = (DOM.replace("STRINGS", json.dumps(panel_page.STRINGS[lang])).replace("SESSIONS", json.dumps(list(sessions)))
               .replace("WA", wa) + "\n" + marks + "\n" + "\n".join(func(n) for n in HELPERS) + "\n" + script)
    out = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=10, check=True)
    return json.loads(out.stdout)


def card(ident, session, title, age, kind="progress", topic="Alpha: build", **extra):
    return dict(id=ident, session=session, kind=kind, title=title, detail="", age=age, topic=topic,
                topic_url=extra.pop("topic_url", LINK_A if session == "aaaa1111" else LINK_B), **extra)


SAMPLE = [card("c1", "aaaa1111", "Alpha: first", 7200), card("c2", "bbbb2222", "Beta: other", 600, topic="Beta: run"),
          card("c3", "aaaa1111", "Alpha: second", 60), card("c4", "aaaa1111", "Which one?", 30, kind="choice",
                                                              options=["a", "b"])]
STATE = "var d={cards:%s};" % json.dumps(SAMPLE)


def test_cards_of_one_session_share_one_head_and_drop_its_repeated_parts():
    out = run(STATE + """
cardSections(d,false);
var gs=pick(app,"sgrp");
console.log(JSON.stringify(gs.map(g=>({heads:pick(g,"sg-h").length,bots:pick(g,"bot").map(b=>b.sid),
 topics:texts(g,"sg-t"),sids:texts(g,"sid"),cardTopics:texts(g,"tp").length,titles:texts(g,"ac-t"),
 inG:pick(g,"ac").every(c=>c.className.split(" ").includes("in-g"))}))));
""")
    asks, *shows = out
    # a session that asks shows once, in the asking section, its updates with it (newest first)
    assert asks["titles"] == ["Which one?", "second", "first"]
    assert [g["titles"] for g in shows] == [["other"]]
    for g in out:
        assert g["heads"] == 1 and len(g["bots"]) == 1 and len(g["sids"]) == 1 and g["cardTopics"] == 0 and g["inG"]
    assert asks["topics"] == ["Alpha: build"] and asks["sids"] == ["#aaaa"]


def test_a_topic_view_shows_its_cards_without_a_second_head():
    out = run(STATE + """
d.cards.forEach(function(c){c.here=c.session==="aaaa1111";});cardSections(d,true);
console.log(JSON.stringify({heads:pick(app,"sg-h").length,cards:pick(app,"ac").length}));
""")
    assert out == {"heads": 0, "cards": 3}


@pytest.mark.parametrize(("title", "topic", "shown"), [
    ("Alpha: next step", "Alpha: build", "next step"),
    ("‏ Alpha :  next step ", "⁧Alpha⁩: build", "next step"),  # trimmed and without direction marks
    ("Beta: next step", "Alpha: build", "Beta: next step"),  # another project's prefix stays
    ("Alpha next step", "Alpha: build", "Alpha next step"),
    ("Alpha:", "Alpha: build", "Alpha:"),  # nothing left after the prefix: the title stays whole
    ("Alpha: next step", "", "Alpha: next step"),
])
def test_the_title_drops_only_its_topics_own_prefix(title, topic, shown):
    out = run("console.log(JSON.stringify(unprefixed(%s,{topic:%s})));" % (json.dumps(title), json.dumps(topic)))
    assert out == shown


def test_the_prefix_is_matched_against_the_translated_topic_too_and_the_card_keeps_its_text():
    c = card("c9", "aaaa1111", "Alpha: next", 5, topic="Beta: x", topic_he="Alpha: y", title_he="Alpha: next")
    out = run("var c=%s,b=agentCard(c,false);console.log(JSON.stringify([texts(b,'ac-t'),c.title]));" % json.dumps(c))
    assert out == [["next"], "Alpha: next"]


def test_the_head_button_opens_the_sessions_topic_through_telegram():
    out = run(STATE + """
cardSections(d,false);var b=pick(app,"sg-go")[0],stop=0;
b.listeners.click({preventDefault:function(){},stopPropagation:function(){stop++;}});
console.log(JSON.stringify({label:b.textContent,calls:calls,stop:stop,n:pick(app,"sg-go").length}));
""")
    assert out["label"] == panel_page.STRINGS["he"]["openTg"] and out["calls"] == [["tg", LINK_A]]
    assert out["stop"] == 1 and out["n"] == 2  # one button per session: the asking one and the other


def test_without_the_telegram_app_the_button_falls_back_to_a_plain_link():
    out = run(STATE + """
cardSections(d,false);pick(app,"sg-go")[0].listeners.click({preventDefault:function(){},stopPropagation:function(){}});
console.log(JSON.stringify(location.href));
""", wa="undefined")
    assert out == LINK_A


def test_only_a_t_me_link_makes_a_button_and_the_sessions_own_link_is_the_fallback():
    bad = [card("c1", "aaaa1111", "x", 5, topic_url="javascript:alert(1)"),
           card("c2", "bbbb2222", "y", 6, topic_url="https://example.invalid/x"),
           card("c3", "cccc3333", "z", 7, topic_url="")]
    out = run("var d={cards:%s};cardSections(d,false);console.log(JSON.stringify(pick(app,'sgrp').map(g=>pick(g,'sg-go').length)));"
              % json.dumps(bad), sessions=[{"id": "cccc3333", "state": "idle", "url": LINK_B}])
    assert out == [0, 0, 1]


def test_strings_and_css_follow_the_house_rules():
    he, en = panel_page.STRINGS["he"], panel_page.STRINGS["en"]
    assert he["openTg"] == "פתיחה בטלגרם" and en["openTg"] == "Open in Telegram"
    assert not re.search(r"[A-Za-z]", he["openTg"])  # no English inside the Hebrew label
    page = panel_page.PAGE
    css = "\n".join(line for line in page[page.index("<style"):page.index("</style>")].splitlines()
                    if line.startswith((".sgrp", ".sg-")))
    assert css and not re.search(r"\b(left|right|width|min-width|max-width)\s*:", css)
    assert 'icon("send")' in func("tgBtn") and 'icon("plus")' not in func("tgBtn")
    assert "openLink(u)" in func("tgBtn") and 'wa.openTelegramLink(u)' in func("openLink")


def test_an_answered_card_is_redrawn_in_its_own_form():
    assert "agentCard(c,/ in-g\\b/.test(box.className))" in func("sendCard")


# --- the server gives each card its session's topic link ------------------------------------------------

@pytest.fixture
def server():
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he")
    srv = panel.PanelServer(("127.0.0.1", 0), cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_the_state_carries_each_cards_topic_link_and_no_chat_id(server, monkeypatch):
    write_private(state_dir() / "topics.json", json.dumps(
        {"s1-session": {"thread": 10, "name": "T0", "chat": CHAT, "home": False}}))
    monkeypatch.setattr(panel, "collect", lambda topics, now=None: [
        sessions_menu.Entry("s1-session", "s1-s", 10, 30.0, "idle", False, CHAT)])
    cards.publish("s1-session", CHAT, 10, "progress", "Alpha: step", "", steps=["a", "b"], at=0)
    body = json.loads(call(server, init=signed())[2])
    (c,) = body["cards"]
    assert c["topic_url"] == panel_view.topic_url(CHAT, 10) == LINK_A
    assert str(CHAT) not in json.dumps(c) and c["title"] == "Alpha: step"  # stored text untouched


def test_no_two_page_functions_share_a_name():
    """A later `function x(` silently replaces an earlier one in the browser (a second `groups` once hid
    every card); the tests that lift single functions cannot see that, so the whole script is checked."""
    names = re.findall(r"(?m)^function (\w+)\(", js())
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, dupes


def test_a_session_that_asks_shows_its_updates_in_the_same_group():
    src = func("cardSections")
    assert "asks.concat(withAsk)" in src and "!asking[" in src


def test_two_sessions_with_the_same_short_id_stay_apart():
    out = run("""
var list=[{id:"1",session:"12345678",topic_url:"https://t.me/c/1/2",age:1},{id:"2",session:"12345678",topic_url:"https://t.me/c/1/3",age:2}];
console.log(JSON.stringify(bySession(list).map(g=>g.map(c=>c.id))));
""")
    assert out == [["1"], ["2"]]


def test_older_questions_are_grouped_by_session_too():
    assert "cardGroups(cards,body,false)" in panel_page.PAGE
