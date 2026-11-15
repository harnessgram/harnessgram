"""The "My schedule" card's Google Calendar row, run in node with the tiny fake DOM: Connect opens the one-time link
in the browser, coming back reloads the card, Connected shows a one-tap Disconnect, both languages."""
import pytest

from test_cockpit_v2_schedule_node import NODE, card

pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")

ROW = "function g(){return card.querySelector('[data-sc-google]');}"


def test_no_row_when_the_section_is_not_set():
    got = card("view([],[],{google:{configured:false,connected:false}})", ROW + "emit({row:!!g()});")
    assert got == {"row": False}


def test_connect_asks_for_a_link_opens_it_and_reloads_on_return():
    got = card("view([],[],{google:{configured:true,connected:false}})", ROW +
               "var opened=[];sandbox.openSafe=function(u){opened.push(u);};"
               "var b=g().querySelector('button');var label=b.textContent,state=g().attrs['data-sc-google'];"
               "reply('/api/schedule',200,{ok:true,url:'https://cockpit.example.test/oauth/google/start?t=abc'});"
               "b.click();await flush();await flush();"
               "lastReply['/api/schedule-view']={status:200,body:{ok:true,view:view([],[],{google:{configured:true,connected:true}})}};"
               "var before=posted.length;docHandlers.visibilitychange();await flush();await flush();"
               "emit({label:label,state:state,sent:posted[1],opened:opened,toasts:toasts,reload:posted.slice(before)});")
    assert got["label"] == "התחבר ל־⁨Google⁩" and got["state"] == "connect"
    assert got["sent"] == ["/api/schedule", {"action": "google-connect"}]
    assert got["opened"] == ["https://cockpit.example.test/oauth/google/start?t=abc"]
    assert got["toasts"] == [["ההתחברות נפתחה בדפדפן. אחרי האישור חזרו לכאן.", False]]
    assert got["reload"] == [["/api/schedule-view", {}]]


def test_a_link_that_is_not_https_is_never_opened():
    got = card("view([],[],{google:{configured:true,connected:false}})", ROW +
               "var opened=[];sandbox.openSafe=function(u){opened.push(u);};"
               "reply('/api/schedule',200,{ok:true,url:'javascript:alert(1)'});"
               "g().querySelector('button').click();await flush();await flush();emit({opened:opened,toasts:toasts});")
    assert got["opened"] == [] and got["toasts"][-1][1] is True


def test_connected_shows_the_state_and_a_one_tap_disconnect():
    got = card("view([],[],{google:{configured:true,connected:true}})", ROW +
               "var row=g(),b=row.querySelector('button');var shown={state:row.querySelector('.sc-g-state').textContent,"
               "name:row.querySelector('.sc-g-name').textContent,button:b.textContent,kind:b.attrs['data-sc']};"
               "reply('/api/schedule',200,{ok:true,view:view([],[],{google:{configured:true,connected:false}})});"
               "b.click();await flush();await flush();"
               "emit({shown:shown,sent:posted[1],toasts:toasts,renders:renders2});")
    assert got["shown"] == {"state": "מחובר", "name": "יומן ⁨Google⁩", "button": "נתק", "kind": "google-disconnect"}
    assert got["sent"] == ["/api/schedule", {"action": "google-disconnect"}]
    assert got["toasts"] == [["היומן נותק", False]] and got["renders"] >= 2


def test_a_failed_disconnect_says_so_and_the_button_works_again():
    got = card("view([],[],{google:{configured:true,connected:true}})", ROW +
               "var b=g().querySelector('button');reply('/api/schedule',503,{error:'unavailable'});"
               "b.click();await flush();await flush();emit({toasts:toasts,disabled:b.disabled});")
    assert got == {"toasts": [["לא הצלחתי, נסו שוב", True]], "disabled": False}


def test_english():
    got = card("view([],[],{google:{configured:true,connected:true}})", ROW +
               "emit({state:g().querySelector('.sc-g-state').textContent,button:g().querySelector('button').textContent});",
               lang="en")
    assert got == {"state": "Connected", "button": "Disconnect"}
