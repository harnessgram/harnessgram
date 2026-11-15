"""The "request an app" sheet and answer card, run in node with the tiny fake DOM (no browser).

Layout, the real tile in Tools > More and screenshots are covered by test_cockpit_app_request_browser.py."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ctb import panel_page
from test_panel_page import func

NODE = shutil.which("node")
HERE = Path(__file__).parent
SRC = Path(panel_page.__file__).parent
PRELUDE = (HERE / "studio_fake_dom.js").read_text() + (HERE / "cockpit_v2_fake_dom.js").read_text()

EXTRA = r"""
doc.body = new El("body");
var realCreate = doc.createElement;
doc.createElement = function (t) {
  var e = realCreate(t);
  if (t === "textarea") e.value = "";
  if (t === "dialog") { e.open = false; e.showModal = function () { e.open = true; }; e.close = function () { e.open = false; }; }
  return e;
};
Object.defineProperty(El.prototype, "isConnected", {get: function () { var n = this; while (n.parentNode) n = n.parentNode; return n === doc.body || n === shell; }});
sandbox.document = doc;
sandbox.navigator = %(navigator)s;
sandbox.RTL = %(rtl)s;
var toasts = [], replies = [];
sandbox.toast = function (text, bad) { toasts.push([text, !!bad]); };
sandbox.haptic = function () {};
sandbox.bindBackLayer = function (key, node, on, apply) { return function (v) { apply(v); }; };
sandbox.post = function (path, body) {
  posted.push([path, body]);
  var r = replies.shift();
  if (r === "reject") return Promise.reject(new Error("offline"));
  return Promise.resolve({status: r.status, ok: r.status < 400, json: function () { return Promise.resolve(r.body); }});
};
"""


def run(body, lang="he", clipboard=False):
    tools = panel_page.V2_STRINGS[lang]["tools"]
    code = "\n".join(func(n) for n in ("el", "icon", "tune")) + "\nfunction v2Chev(){var c=el('span','v2-chev');return c;}\n" \
        + (SRC / "cockpit_app_request.js").read_text()
    navigator = "{clipboard:{readText:function(){return Promise.resolve(%s);}}}" % json.dumps("https://apps.apple.com/app/id123456789") if clipboard else "{}"
    src = PRELUDE + (EXTRA % {"navigator": navigator, "rtl": "true" if lang == "he" else "false"}) + (
        "\nsandbox.LANG=%s;sandbox.S={reopen:'reopen'};sandbox.V2={tools:%s};sandbox.ICONS={};docEl.dir=%s;\n"
        % (json.dumps(lang), json.dumps(tools, ensure_ascii=False), json.dumps("rtl" if lang == "he" else "ltr"))
        + "vm.runInNewContext(%s, sandbox);\n" % json.dumps(code)
        + "(async()=>{%s})().catch(e=>{console.error(e);process.exit(1);});" % body)
    result = subprocess.run([NODE, "-"], input=src, capture_output=True, text=True, timeout=20,
                            env=dict(os.environ, TZ="Asia/Jerusalem"))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")

OPEN = "var tile=sandbox.v2AppRequestTile();root.appendChild(tile);tile.click();var dlg=doc.body.childNodes[0];" \
       "var ta=dlg.querySelector('textarea'),send=dlg.querySelector('.ar-send');"


def shown(body, **kw):
    return run(OPEN + body, **kw)


def test_tile_and_sheet_are_built_with_the_right_roles():
    got = shown("emit({tile:tile.attrs['data-v2-app-request'],popup:tile.attrs['aria-haspopup'],text:tile.textContent,"
                "open:dlg.open,label:dlg.attrs['aria-labelledby'],dir:dlg.dir,title:dlg.querySelector('.topic-heading').childNodes[0].textContent,"
                "max:ta.maxLength,sendOff:send.disabled,live:dlg.querySelector('.ar-out').attrs['aria-live'],paste:!!dlg.querySelector('.ar-paste')});")
    assert got["tile"] == "1" and got["popup"] == "dialog" and got["open"] is True
    assert got["label"] == "ar-sheet-title" and got["dir"] == "rtl" and got["max"] == 500
    assert got["title"] == "בקשת אפליקציה" and got["text"].startswith("בקשת אפליקציה")
    assert got["sendOff"] is True and got["live"] == "polite" and got["paste"] is False


@pytest.mark.parametrize("lang,direction", [("he", "rtl"), ("en", "ltr")])
def test_empty_field_takes_the_page_direction_and_text_decides_after(lang, direction):
    got = shown("var empty=ta.dir;ta.value='https://apps.apple.com/app/id123456789';ta.fire('input');var typed=ta.dir;"
                "ta.value='';ta.fire('input');emit({empty,typed,again:ta.dir,ph:ta.placeholder});", lang=lang)
    assert got["empty"] == direction and got["typed"] == "auto" and got["again"] == direction and got["ph"]


def test_send_needs_text_and_posts_exactly_the_trimmed_text():
    got = shown("var off=send.disabled;ta.value='   ';ta.fire('input');var blank=send.disabled;"
                "ta.value='  https://apps.apple.com/app/id123456789 \\n';ta.fire('input');var on=!send.disabled;"
                "replies.push({status:200,body:{ok:true,result:{state:'review',app:'',reason:'',reason_he:null,until:''}}});"
                "send.click();await flush();emit({off,blank,on,posted,state:dlg.querySelector('.ar-result').attrs['data-ar-state']});")
    assert got["off"] and got["blank"] and got["on"]
    assert got["posted"] == [["/api/app-request", {"text": "https://apps.apple.com/app/id123456789"}]]
    assert got["state"] == "review"


def test_approved_card_shows_local_time_app_and_hebrew_reason():
    got = shown("ta.value='id123456789';ta.fire('input');"
                "replies.push({status:200,body:{ok:true,result:{state:'approved',app:'Maps',bundle:'com.m',reason:'Fine',"
                "reason_he:'מתאים לגיל',until:'2026-11-14T12:35:00Z'}}});send.click();await flush();"
                "var c=dlg.querySelector('.ar-result');emit({state:c.attrs['data-ar-state'],title:c.querySelector('.ar-title').textContent,"
                "until:c.querySelector('.ar-until').textContent,app:c.querySelector('.ar-app').querySelector('bdi').textContent,"
                "reason:c.querySelector('.ar-reason').querySelector('bdi').textContent,reasonDir:c.querySelector('.ar-reason').querySelector('bdi').dir,"
                "cleared:ta.value,busy:ta.disabled});")
    assert got["state"] == "approved" and got["title"] == "אושר"
    assert got["until"] == "פתוח להתקנה עד 14:35"  # 12:35Z in Asia/Jerusalem
    assert got["app"] == "Maps" and got["reason"] == "מתאים לגיל" and got["reasonDir"] == "auto"
    assert got["cleared"] == "" and got["busy"] is False


def test_english_reason_has_its_own_block_when_not_translated():
    got = shown("ta.value='id123456789';ta.fire('input');replies.push({status:200,body:{ok:true,result:{state:'denied',app:'Chatty',"
                "reason:'Messaging apps are never approved',reason_he:null,until:''}}});send.click();await flush();"
                "var c=dlg.querySelector('.ar-result'),line=c.querySelector('.ar-reason');"
                "emit({state:c.attrs['data-ar-state'],title:c.querySelector('.ar-title').textContent,block:line.tagName,"
                "text:line.querySelector('bdi').textContent,dir:line.querySelector('bdi').dir,until:!!c.querySelector('.ar-until')});")
    assert got == {"state": "denied", "title": "לא אושר", "block": "div", "text": "Messaging apps are never approved",
                   "dir": "auto", "until": False}


@pytest.mark.parametrize("state,title", [("review", "הוועדה בודקת, תקבל תשובה"), ("pending", "אושר, אבל הניהול מרחוק עדיין לא מחובר"),
                                         ("error", "לא הצלחתי לבדוק את הבקשה")])
def test_other_answers(state, title):
    got = shown("ta.value='id123456789';ta.fire('input');replies.push({status:200,body:{ok:true,result:{state:'%s',app:'',"
                "reason:'',reason_he:null,until:''}}});send.click();await flush();var c=dlg.querySelector('.ar-result');"
                "emit({title:c.querySelector('.ar-title').textContent,note:!!c.querySelector('.ar-note'),until:!!c.querySelector('.ar-until')});" % state)
    assert got["title"] == title and got["until"] is False
    assert got["note"] is (state in ("pending", "error"))


def test_approved_without_a_time_still_says_it_is_open():
    got = shown("ta.value='id123456789';ta.fire('input');replies.push({status:200,body:{ok:true,result:{state:'approved',app:'',"
                "reason:'',reason_he:null,until:''}}});send.click();await flush();emit(dlg.querySelector('.ar-until').textContent);")
    assert got == "פתוח להתקנה עכשיו"


def test_refused_text_and_rate_limit_keep_the_text_for_another_try():
    got = shown("ta.value='nonsense';ta.fire('input');replies.push({status:400,body:{error:'bad request'}});send.click();await flush();"
                "var bad=dlg.querySelector('.ar-result').attrs['data-ar-state'],kept=ta.value;"
                "replies.push({status:429,body:{error:'rate',retry:7}});send.click();await flush();"
                "var c=dlg.querySelector('.ar-result');emit({bad,kept,rate:c.attrs['data-ar-state'],text:c.querySelector('.ar-title').textContent,"
                "single:dlg.querySelector('.ar-out').children.length,value:ta.value,posts:posted.length});")
    assert got["bad"] == "bad" and got["kept"] == "nonsense" and got["rate"] == "rate"
    assert "7" in got["text"] and got["single"] == 1 and got["value"] == "nonsense" and got["posts"] == 2


def test_expired_login_and_network_failure():
    got = shown("ta.value='id123456789';ta.fire('input');replies.push({status:403,body:{error:'forbidden'}});send.click();await flush();"
                "var afterLogin={toasts:toasts.slice(),cards:dlg.querySelector('.ar-out').children.length,enabled:!send.disabled,busy:ta.disabled};"
                "replies.push('reject');send.click();await flush();"
                "emit({afterLogin,state:dlg.querySelector('.ar-result').attrs['data-ar-state'],kept:ta.value});")
    assert got["afterLogin"] == {"toasts": [["reopen", True]], "cards": 0, "enabled": True, "busy": False}
    assert got["state"] == "error" and got["kept"] == "id123456789"


def test_double_tap_sends_once_while_busy():
    got = shown("ta.value='id123456789';ta.fire('input');"
                "var gate;replies.push({status:200,body:{ok:true,result:{state:'review'}}});"
                "send.click();send.click();ta.fire('keydown',{key:'Enter',ctrlKey:true,preventDefault(){}});await flush();"
                "emit({posts:posted.length});")
    assert got["posts"] == 1


def test_close_button_and_one_sheet_at_a_time():
    got = shown("dlg.querySelector('.topic-close').click();var closed=!dlg.open;tile.click();tile.click();"
                "emit({closed,sheets:doc.body.childNodes.filter(c=>c.tagName==='dialog').length});")
    assert got == {"closed": True, "sheets": 1}


def test_paste_button_appears_only_with_clipboard_support_and_fills_the_field():
    got = shown("var paste=dlg.querySelector('.ar-paste');paste.click();await flush();"
                "emit({value:ta.value,enabled:!send.disabled});", clipboard=True)
    assert got == {"value": "https://apps.apple.com/app/id123456789", "enabled": True}


@pytest.mark.parametrize("lang", ["he", "en"])
def test_strings_cover_every_state_in_both_languages(lang):
    tools = panel_page.V2_STRINGS[lang]["tools"]
    for key in ("arTile", "arTileHint", "arTitle", "arHint", "arPlaceholder", "arPaste", "arSend", "arSending", "arClose",
                "arApproved", "arUntil", "arOpenNow", "arDenied", "arReview", "arPending", "arPendingHint", "arError",
                "arErrorHint", "arBad", "arRate", "arAppLabel", "arReasonLabel"):
        assert tools[key].strip()
    assert "{time}" in tools["arUntil"] and "{n}" in tools["arRate"]
    js = (SRC / "cockpit_app_request.js").read_text()
    for key in __import__("re").findall(r"V2\.tools\.(ar\w+)", js):
        assert key in tools, key


def test_hebrew_copy_never_mixes_english_into_a_line_without_an_isolate():
    import re
    for key, text in panel_page.V2_STRINGS["he"]["tools"].items():
        if not key.startswith("ar"):
            continue
        for part in text.split("\n"):
            outside = re.sub(r"\u2068[^\u2069]*\u2069|\{\w+\}", "", part)
            assert not re.search("[A-Za-z]", outside), (key, text)
