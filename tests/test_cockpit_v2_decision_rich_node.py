"""The Cockpit v2 decision view with its whole context, run in node without a browser: markdown-lite detail built
from text nodes, pictures behind the media token, https links, and a comment sent alone or with the answer."""
import json

import pytest

from ctb import panel_page
from test_cockpit_v2_node import NODE, run

pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")

STUBS = ("var minted=[],viewed=[],opened=[];"
         "sandbox.cardImageUrl=id=>{minted.push(id);return Promise.resolve('/card-media/'+id+'?auth=t');};"
         "sandbox.openCardImage=(url,label)=>{viewed.push([url,label]);};"
         "sandbox.openLink=u=>{opened.push(u);};sandbox.S.cardEnlarge='zoom';sandbox.S.cardImageError='err';")


def rich(extra):
    return "var d=Object.assign({},sandbox.last.decisions[0],{rich_decision:true}," + json.dumps(extra, ensure_ascii=False) + ");var row=sandbox.v2Decision(d);"


def test_default_off_renders_the_unchanged_decision_controls():
    got = run("var row=byClass('v2-decision').find(r=>r.querySelector('textarea')),ta=row.querySelector('textarea');"
              "emit({max:ta.maxLength,comment:!!row.querySelector('.v2-comment-only'),rich:!!row.querySelector('.v2-rich'),"
              "inside:(function(n){while(n&&n!==row){if(String(n.className).split(/\\s+/).includes('v2-detail'))return true;n=n.parentNode;}return false;})(ta)});", "en")
    assert got == {"max": 300, "comment": False, "rich": False, "inside": True}


@pytest.mark.parametrize("lang", ["he", "en"])
def test_detail_is_markdown_lite_built_from_text_only(lang):
    detail = "Two ways.\nSecond line.\n\n- **A**: today\n- B: <b>next</b> week\n\nClosing <script>x</script>"
    got = run(rich({"detail": detail}) +
              "var box=row.querySelector('.detail');emit({ps:box.querySelectorAll('p').length,"
              "lis:box.querySelectorAll('li').map(e=>e.textContent),bold:box.querySelectorAll('b').map(e=>e.textContent),"
              "first:box.querySelectorAll('p')[0].textContent,dir:box.querySelectorAll('ul')[0].dir,"
              "scripts:box.querySelectorAll('script').length,last:box.querySelectorAll('p')[1].textContent});", lang)
    assert got == {"ps": 2, "lis": ["A: today", "B: <b>next</b> week"], "bold": ["A"],
                   "first": "Two ways.\nSecond line.", "dir": "auto", "scripts": 0,
                   "last": "Closing <script>x</script>"}


def test_pictures_load_through_the_token_and_enlarge_on_tap():
    got = run(STUBS + rich({"images": ["a" * 32, "../etc/passwd", "b" * 32]}) +
              "await flush();var pics=row.querySelectorAll('.v2-decision-picture');"
              "pics[1].click();await flush();"
              "emit({n:pics.length,minted,viewed:viewed.map(v=>v[0]),src:pics[0].querySelector('img').src});")
    assert got["n"] == 2 and got["minted"][:2] == ["a" * 32, "b" * 32]
    assert got["viewed"] == ["/card-media/" + "b" * 32 + "?auth=t"]
    assert got["src"] == "/card-media/" + "a" * 32 + "?auth=t"


def test_only_https_links_are_drawn_and_open_on_tap():
    links = [{"label": "Preview", "url": "https://example.com/p?x=1"}, {"label": "bad", "url": "http://example.com"},
             {"label": "js", "url": "javascript:alert(1)"}, {"label": "", "url": "https://docs.example.org/a"},
             {"label": "login", "url": "https://user@example.com/"}]
    got = run(STUBS + rich({"links": links}) +
              "var bs=row.querySelectorAll('.v2-decision-link');bs[0].fire('click',{preventDefault(){},stopPropagation(){}});"
              "emit({n:bs.length,names:bs.map(b=>b.querySelector('bdi').textContent),opened});")
    assert got == {"n": 2, "names": ["Preview", "docs.example.org"], "opened": ["https://example.com/p?x=1"]}


@pytest.mark.parametrize("lang", ["he", "en"])
def test_a_comment_can_be_sent_alone_and_approves_nothing(lang):
    got = run(STUBS + rich({}) +
              "var ta=row.querySelector('textarea'),only=row.querySelector('[data-v2-reply]');"
              "var before={disabled:only.disabled,dir:ta.dir,max:ta.maxLength};"
              "ta.value='  Which backups?  ';ta.fire('input');var enabled=!only.disabled;only.click();await flush();await flush();"
              "emit({before,enabled,posted,cleared:ta.value,replies:row.querySelectorAll('.v2-reply').map(e=>e.textContent),"
              "answers:row.querySelectorAll('[data-v2-answer]').length,"
              "boxBeforeButtons:row.children.indexOf(row.querySelector('.v2-comment-box'))<row.children.indexOf(row.querySelector('.v2-answer-buttons'))});",
              lang)
    assert got["before"] == {"disabled": True, "dir": "rtl" if lang == "he" else "ltr", "max": 1000}
    assert got["enabled"] is True and got["cleared"] == "" and got["replies"] == ["Which backups?"]
    assert len(got["posted"]) == 1 and got["posted"][0][0] == "/api/decide"
    assert got["posted"][0][1]["choice"] == "reply" and got["posted"][0][1]["note"] == "Which backups?"
    assert got["answers"] == 2 and got["boxBeforeButtons"] is True


def test_yes_carries_the_comment_and_strings_exist_in_both_languages():
    got = run(STUBS + rich({}) +
              "var ta=row.querySelector('textarea');ta.value='ok, but tell me';ta.fire('input');"
              "row.querySelector('[data-v2-answer=yes]').click();await flush();emit(posted);")
    assert got == [["/api/decide", {"id": got[0][1]["id"], "choice": "yes", "note": "ok, but tell me"}]]
    for lang in ("he", "en"):
        now = panel_page.V2_STRINGS[lang]["now"]
        for key in ("picture", "commentPh", "commentOnly", "commentHint", "commentSent", "yourComments"):
            assert now[key]


def test_a_permission_request_offers_no_comment_alone_and_stale_disables_it():
    got = run(STUBS + rich({"request": "rm -rf build"}) +
              "var perm=row.querySelector('[data-v2-reply]').hidden;"
              "var d2=Object.assign({},sandbox.last.decisions[0],{rich_decision:true,stale:'ended'}),r2=sandbox.v2Decision(d2),ta=r2.querySelector('textarea');"
              "ta.value='x';ta.fire('input');emit({perm,stale:r2.querySelector('[data-v2-reply]').disabled});")
    assert got == {"perm": True, "stale": True}
