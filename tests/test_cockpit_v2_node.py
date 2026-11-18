"""Execute the new screen and the existing request controls without a browser.

These check semantic structure and exact action payloads; browser tests remain
required for layout, overflow, keyboard focus, history and screenshots.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ctb import panel_page
from cockpit_v2_browser import needs_state, sample_state
from test_panel_page import func

NODE = shutil.which("node")
HERE = Path(__file__).parent
PRELUDE = (HERE / "studio_fake_dom.js").read_text() + (HERE / "cockpit_v2_fake_dom.js").read_text()


def run(body, lang="he", mutate=None):
    script = (Path(panel_page.__file__).parent / "cockpit_v2.js").read_text().replace(
        "__V2_STRINGS__", json.dumps(panel_page.V2_STRINGS, ensure_ascii=False))
    script = 'var requestProject="",requestOwnerOnly=false;\n' + "\n".join(func(n) for n in ("el", "icon", "svgAdd", "he", "shown", "compactStorageKey", "compactBlock", "requestAction", "requestRow", "requestRank", "requestDetail", "requestList", "requestGroups", "requestText", "requestBoard", "v2ModeRow", "settingsBox")) + script
    state = sample_state(lang)
    if mutate:
        mutate(state)
    src = PRELUDE + "\nbootV2(%s,%s,%s,%s);\n(async()=>{%s})().catch(e=>{console.error(e);process.exit(1);});" % (
        json.dumps(script), json.dumps(state), json.dumps(panel_page.STRINGS[lang]), json.dumps(lang), body)
    result = subprocess.run([NODE, "-"], input=src, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")


@pytest.mark.parametrize("lang,label,why", [
    ("he", "עבודה", "עבודה פעילה ליד המחשב"),
    ("en", "Work", "Active work at the Mac"),
])
def test_home_context_chip_renders_only_when_state_field_is_present(lang, label, why):
    absent = run("emit({chips:byClass('v2-context').length});", lang)
    present = run("emit({chips:byClass('v2-context').length,label:byClass('v2-context-chip')[0].textContent,"
                  "why:byClass('v2-context-why')[0].textContent});", lang,
                  lambda state: state.update(home_context={"mode": "work", "why": "active_work", "signals": {}}))
    assert absent == {"chips": 0}
    assert present == {"chips": 1, "label": label, "why": why}


def test_v2_request_row_places_status_line_after_the_full_width_title():
    got = run("var row=byClass('v2-requests')[0].querySelector('.request-row'),body=row.querySelector('.body');"
              "emit({children:body.children.map(x=>x.className),chips:body.children[2].querySelectorAll('.chip').length});",
              mutate=lambda state: state.update(needs_state()))
    assert got == {"children": ["nm", "request-disclosure", "mt request-status-line"], "chips": 1}


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("server_count", [True, False])
def test_needs_count_equals_all_expandable_rows_from_the_four_sources(lang, server_count):
    def mutate(state):
        state.update(needs_state(lang))
        if not server_count:
            state.pop("waiting_count")
        # None of these belong in the shared waiting count.
        state["decisions"].append(dict(state["decisions"][0], id="ended", stale="ended"))
        state["cards"].append(dict(state["cards"][-1], id="ended-card", ended=True))
        state["requests"].append(dict(state["requests"][-1], id=99, status="done"))

    got = run("var zone=byClass('v2-needs')[0],groups=zone.querySelectorAll('[data-v2-needs-group]');"
              "var groupsInfo=groups.map(g=>{var fold=g.querySelector('.v2-needs-more'),b=fold.querySelector('.compact-toggle');"
              "var initial=b.getAttribute('aria-expanded');b.click();var expanded=b.getAttribute('aria-expanded');b.click();"
              "return {key:g.getAttribute('data-v2-needs-group'),n:g.querySelectorAll('[data-v2-decision]').length+"
              "g.querySelectorAll('[data-v2-reminder]').length+g.querySelectorAll('[data-v2-request]').length,"
              "label:fold.querySelector('.compact-title').textContent,initial,expanded,collapsed:b.getAttribute('aria-expanded')};});"
              "emit({groups:groupsInfo,count:Number(zone.querySelector('.v2-count').textContent),"
              "open:sandbox.v2OpenCount(sandbox.last),empty:zone.querySelectorAll('.v2-empty').length,"
              "reminders:allEls().filter(e=>e.attrs['data-v2-reminder']).length,"
              "requests:allEls().filter(e=>e.attrs['data-v2-request']).map(e=>e.attrs['data-v2-request'])});", lang, mutate)
    sizes = dict(zip(("decisions", "asks", "reminders", "owner-requests"), (6, 7, 8, 9)))
    assert got["groups"] == [dict(key=key, n=n, label=panel_page.V2_STRINGS[lang]["now"]["showAll"].replace("{n}", str(n)),
                                 initial="false", expanded="true", collapsed="false") for key, n in sizes.items()]
    assert got["count"] == got["open"] == sum(g["n"] for g in got["groups"]) == 30
    assert got["empty"] == 0 and got["reminders"] == 8
    assert len(got["requests"]) == len(set(got["requests"])) == 15


@pytest.mark.parametrize("source", ["reminders", "requests", None])
def test_needs_empty_text_only_when_shared_count_is_zero(source):
    def mutate(state):
        state.update(needs_state())
        for key in ("decisions", "cards", "reminders", "requests"):
            if key != source:
                state[key] = []
        state["waiting_count"] = 8 if source == "reminders" else 9 if source == "requests" else 0

    got = run("var zone=byClass('v2-needs')[0];emit({count:Number(zone.querySelector('.v2-count').textContent),"
              "empty:zone.querySelectorAll('.v2-empty').map(e=>e.textContent)});", mutate=mutate)
    assert got == {"count": 8 if source == "reminders" else 9 if source == "requests" else 0,
                   "empty": [] if source else [panel_page.V2_STRINGS["he"]["now"]["empty"]]}


def test_answered_items_leave_the_needs_rows_and_shared_count_together():
    got = run("sandbox.closed['0000000000000001']=1;sandbox.cardDone['0000000000000014']={c:sandbox.last.cards[4]};"
              "root.childNodes.length=0;sandbox.v2Now(sandbox.last);var zone=byClass('v2-needs')[0];"
              "emit({count:Number(zone.querySelector('.v2-count').textContent),"
              "rows:zone.querySelectorAll('[data-v2-decision]').length+zone.querySelectorAll('[data-v2-reminder]').length+"
              "zone.querySelectorAll('[data-v2-request]').length});", mutate=lambda state: state.update(needs_state()))
    assert got == {"count": 28, "rows": 28}


@pytest.mark.parametrize("lang", ["he", "en"])
def test_screen_semantics_keep_decisions_and_use_touch_drop_for_dormant_requests(lang):
    got = run("emit({zones:byClass('v2-needs').length,ids:allEls().filter(e=>e.attrs['data-v2-decision']).map(e=>e.attrs['data-v2-decision']),"
              "answers:allEls().filter(e=>e.attrs['data-v2-answer']).length,requests:byClass('request-row').filter(e=>e.querySelector('.request-actions')).map(e=>e.querySelector('.request-actions').children.length),"
              "projects:byClass('v2-project').length,first:root.children[1].attrs['data-v2-section']});", lang)
    assert got == {"zones": 1, "ids": [str(n) * 16 for n in (1, 2, 3)], "answers": 6,
                   "requests": [3, 3, 3, 2], "projects": 4, "first": "decisions"}


@pytest.mark.parametrize("lang,label", [("he", "למעבר להודעה"), ("en", "Go to the message")])
def test_waiting_items_render_exact_message_links(lang, label):
    def mutate(state):
        state["decisions"][0]["link"] = "https://t.me/c/123/7/41"
        state["cards"].append(dict(state["cards"][0], id="a" * 16, kind="ask", title="Question",
                                   link="https://t.me/c/123/7/42", ended=False))
    got = run("emit({labels:byClass('v2-message-link').map(e=>e.textContent),"
              "targets:byClass('v2-message-link').map(e=>e.listeners.click?true:false)});", lang, mutate)
    assert got == {"labels": [label] * 4, "targets": [True] * 4}


def test_request_filters_exclude_projects_with_only_finished_items():
    def mutate(state):
        base = state["requests"][0]
        state["requests"] = [dict(base, project="active", id=1, status="open"),
                             dict(base, project="active", id=2, status="done"),
                             dict(base, project="finished-only", id=3, status="done")]

    got = run("var board=sandbox.requestBoard(sandbox.last);"
              "emit(board.querySelectorAll('.requests-pill-name').map(e=>e.textContent));", mutate=mutate)
    assert got == [panel_page.STRINGS["he"]["requestsAll"], "active"]


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("choice", ["yes", "no"])
def test_decision_uses_original_id_choice_and_note(lang, choice):
    got = run("var row=byClass('v2-decision')[2];row.querySelector('textarea').value='הערה';"
              "row.querySelector('[data-v2-answer=%s]').click();emit(posted);" % choice, lang)
    assert got == [["/api/decide", {"id": "3" * 16, "choice": choice, "note": "הערה"}]]


@pytest.mark.parametrize("action,index", [("done", 0), ("drop", 1), ("push", 2)])
def test_actual_request_action_requires_two_taps(action, index):
    got = run("var b=byClass('request-row').find(e=>e.querySelector('.request-actions')&&!e.parentNode.parentNode.attrs['data-requests']).querySelector('.request-actions').children[%d];"
              "b.click();var before=posted.length;b.click();await flush();emit({before,posted,loads});" % index)
    assert got == {"before": 0, "posted": [["/api/request", {"project": "לימוד", "id": 2, "action": action}]], "loads": 1}


def test_new_disclosures_have_reversible_controls_and_note_direction():
    got = run("var row=byClass('v2-decision')[0],more=row.querySelector('.compact'),button=row.querySelector('.v2-picture').querySelector('button');"
              "var initial=more.classList.contains('open');button.click();var opened=more.classList.contains('open');button.click();"
              "emit({initial,opened,closed:!more.classList.contains('open'),dir:row.querySelector('textarea').dir});")
    assert got == {"initial": False, "opened": True, "closed": True, "dir": "rtl"}


@pytest.mark.parametrize("lang", ["he", "en"])
def test_full_decision_detail_precedes_answers_outside_disclosure(lang):
    detail = ("תנאי חשוב לפעולה.\n" if lang == "he" else "An important condition.\n") * 40
    detail += "Requires owner review before publication."
    got = run("var d=Object.assign({},sandbox.last.decisions[0],{detail:" + json.dumps(detail) + "});"
              "var row=sandbox.v2Decision(d),detail=row.querySelector('.detail'),answers=row.querySelector('.v2-answer-buttons');"
              "row.querySelector('.v2-picture').querySelector('button').click();"
              "row.querySelector('.v2-picture').querySelector('button').click();"
              "emit({text:detail.textContent,direct:detail.parentNode===row,"
              "before:row.children.indexOf(detail)<row.children.indexOf(answers),"
              "folded:!row.querySelector('.compact').classList.contains('open')});", lang)
    assert got == {"text": detail, "direct": True, "before": True, "folded": True}


def test_unknown_progress_is_labeled_and_not_rendered_as_zero():
    got = run("var p=sandbox.last.cockpit_v2.projects[0];p.percent=null;var b=sandbox.v2Project(p);"
              "emit({label:b.querySelector('.v2-progress').getAttribute('aria-label'),text:b.querySelector('.v2-progress').textContent});")
    assert got == {"label": panel_page.V2_STRINGS["he"]["now"]["progressUnknown"], "text": "–"}


@pytest.mark.parametrize("lang", ["he", "en"])
@pytest.mark.parametrize("current", ["off", "preview", "on", None])
@pytest.mark.parametrize("choice", ["off", "preview", "on"])
def test_classic_settings_always_offer_localized_mode_switch_using_existing_api(lang, current, choice):
    got = run("sandbox.SWITCHES=[];sandbox.last.settings=" + json.dumps({"ui_v2": current} if current else {}) + ";"
              "var box=sandbox.settingsBox(sandbox.last),buttons=box.querySelectorAll('[data-v2-mode]');"
              "var options=buttons.map(b=>({value:b.getAttribute('data-v2-mode'),label:b.textContent,pressed:b.getAttribute('aria-pressed')}));"
              "box.querySelector('[data-v2-mode=%s]').click();await flush();emit({options,posted,loads});" % choice, lang)
    assert got == {
        "options": [{"value": mode, "label": panel_page.V2_STRINGS[lang]["settings"][mode],
                     "pressed": str(mode == (current or "off")).lower()} for mode in ("off", "preview", "on")],
        "posted": [["/api/setting", {"name": "ui_v2", "value": choice}]], "loads": 1,
    }


@pytest.mark.parametrize("previous", ["off", "preview", "on"])
def test_rollback_poll_with_new_decision_keeps_settings_and_classic_notice_behavior(previous):
    # Run the actual poll handler: a rollback and a new decision can share a response.
    scenario = r'''
const assert = require('assert');
let wa={initData:'fixture'}, loading=false, reloadPending=false, targetPending=false,
    targetClosed=false, priorWaiting=['old'], target='', view='settings', followTimer=null,
    followups=0, last={settings:{ui_v2:PREVIOUS},ui_v2_enabled:PREVIOUS!=='off'};
let document={getElementById(){return {className:''};}}, window={};window.parent=window;
let ASKS={ask:1,choice:1}, S={}, rendered=false, error=false;
function status(value){if(value==='off')error=true;}
function makeDigest(){} function radioPlayerRefresh(){}
function render(d){last=d;rendered=true;}
function fetch(){return Promise.resolve({ok:true,json:()=>Promise.resolve({
    settings:{ui_v2:'off'},ui_v2_enabled:false,decisions:[{id:'old'},{id:'new'}],cards:[]
})});}
'''.replace('PREVIOUS', json.dumps(previous))
    scenario += func('v2Enabled') + func('load') + r'''
load();
setImmediate(()=>{
    assert.equal(rendered,true);assert.equal(error,false);
    assert.equal(view,PREVIOUS==='off'?'all':'settings');
    assert.equal(targetPending,PREVIOUS==='off');
    assert.deepEqual(priorWaiting,['old','new']);
});
'''.replace('PREVIOUS', json.dumps(previous))
    result = subprocess.run([NODE, '-'], input=scenario, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("lang", ["he", "en"])
def test_recommendation_only_detail_is_shown_once(lang):
    # The fixture's detail is exactly one "Recommendation: ..." line.
    got = run("var row=sandbox.v2Decision(sandbox.last.decisions[0]);"
              "emit({detail:row.querySelectorAll('.detail').length,rec:row.querySelector('.v2-recommendation').textContent});", lang)
    assert got["detail"] == 0 and got["rec"].endswith("Continue with the prepared plan." if lang == "en" else "להמשיך לפי התכנון שהוכן.")


def test_detail_with_more_than_the_recommendation_stays_whole():
    detail = "Recommendation: go.\nCondition: only after review."
    got = run("var d=Object.assign({},sandbox.last.decisions[0],{detail:%s});var row=sandbox.v2Decision(d);"
              "emit(row.querySelector('.detail').textContent);" % json.dumps(detail), "en")
    assert got == detail


def with_stale(state):
    """One ended and one replaced decision beside the three live/old ones."""
    for ident, why in (("a" * 16, "ended"), ("b" * 16, "replaced")):
        state["decisions"].append(dict(id=ident, topic="תכנון: task", title="שאלה ישנה " + why, detail="", yes="כן", no="לא",
                                       age=9000, old=False, stale=why, url=""))


@pytest.mark.parametrize("lang", ["he", "en"])
def test_stale_decisions_leave_the_needs_list_and_count_and_fill_a_dismiss_fold(lang):
    got = run("var zone=byClass('v2-needs')[0],fold=byClass('v2-stale-fold')[0];"
              "emit({needs:zone.querySelectorAll('[data-v2-decision]').map(e=>e.attrs['data-v2-decision']),"
              "count:zone.querySelector('.v2-count').textContent,open:sandbox.v2OpenCount(sandbox.last),"
              "rows:fold.querySelectorAll('.v2-stale-row').length,"
              "buttons:fold.querySelectorAll('[data-v2-dismiss]').map(e=>e.attrs['data-v2-dismiss']),"
              "title:fold.querySelector('.compact-title').textContent,"
              "inZone:zone.querySelectorAll('.v2-stale-fold').length,"
              "dis:fold.querySelectorAll('[data-v2-answer]').length});", lang, with_stale)
    assert got["needs"] == [str(n) * 16 for n in (1, 2, 3)]
    assert got["count"] == "5" and got["open"] == 5
    assert got["rows"] == 2 and got["inZone"] == 0 and got["dis"] == 0
    assert got["buttons"] == ["a" * 16, "b" * 16, "all"]
    assert got["title"] == {"he": "כבר לא רלוונטיות (2)", "en": "No longer relevant (2)"}[lang]


def test_no_fold_without_stale_decisions_and_old_ones_stay_answerable():
    got = run("emit({fold:byClass('v2-stale-fold').length,open:sandbox.v2OpenCount(sandbox.last)});")
    assert got == {"fold": 0, "open": 5}


def test_remove_dismisses_that_one_id_through_the_classic_endpoint_then_refreshes():
    got = run("var fold=byClass('v2-stale-fold')[0];fold.querySelector('[data-v2-dismiss=%s]').click();await flush();"
              "emit({posted,closed:Object.keys(sandbox.closed),renders});" % ("b" * 16),
              mutate=with_stale)
    assert got["posted"] == [["/api/dismiss", {"ids": ["b" * 16]}]]
    assert got["renders"] == 1


def test_clear_all_dismisses_every_stale_id_once():
    got = run("byClass('v2-stale-fold')[0].querySelector('[data-v2-dismiss=all]').click();await flush();emit({posted,renders});",
              mutate=with_stale)
    assert got == {"posted": [["/api/dismiss", {"ids": ["a" * 16, "b" * 16]}]], "renders": 1}


def test_closed_stale_decisions_disappear_from_the_fold():
    got = run("sandbox.closed['a'.repeat(16)]=1;sandbox.closed['b'.repeat(16)]=1;"
              "root.childNodes.length=0;sandbox.v2Now(sandbox.last);emit({fold:byClass('v2-stale-fold').length});", mutate=with_stale)
    assert got == {"fold": 0}


def test_old_but_live_decision_keeps_answers_and_gets_a_remove_that_dismisses_its_id():
    got = run("var rows=byClass('v2-decision'),old=rows[2],fresh=rows[0];"
              "var before={fresh:fresh.querySelectorAll('.v2-old-remove').length,old:old.querySelectorAll('.v2-old-remove').length,"
              "answers:old.querySelectorAll('[data-v2-answer]').length};"
              "old.querySelector('.v2-old-remove').click();await flush();emit({before,posted,renders});")
    assert got == {"before": {"fresh": 0, "old": 1, "answers": 2},
                   "posted": [["/api/dismiss", {"ids": ["3" * 16]}]], "renders": 1}


def test_raw_slug_names_read_as_words_and_configured_names_stay():
    got = run("emit(['sample-809-audit-exception-renewal','app_store__watch','תכנון','Studio','example.com','-'].map("
              "n=>sandbox.v2Name({name:n})).concat([sandbox.v2Name({name:'app-x',name_he:'אפליקציה'})]));")
    assert got == ["Sample 809 audit exception renewal", "App store watch", "תכנון", "Studio", "example.com", "-", "אפליקציה"]


def test_quiet_projects_fold_and_active_ones_come_first():
    got = run("var P=[{key:'a',status:'on_track',stale:true,percent:null,waiting:0,age:90000},"
              "{key:'b',status:'on_track',stale:false,percent:40,waiting:0,age:600},"
              "{key:'c',status:'waiting',stale:true,percent:null,waiting:1,age:90000},"
              "{key:'d',status:'on_track',stale:true,percent:70,waiting:0,age:200000},"
              "{key:'e',status:'blocked',stale:false,percent:null,waiting:0,age:60}];"
              "emit({order:sandbox.v2ProjectOrder(P).map(p=>p.key),quiet:P.filter(sandbox.v2Quiet).map(p=>p.key)});")
    assert got == {"order": ["c", "e", "b", "a", "d"], "quiet": ["a"]}


def test_a_quiet_brief_line_waiting_on_the_owner_stays_on_now():
    """From source data: a work-lines item waiting on the owner, three days old, no progress, no session."""
    from types import SimpleNamespace
    from ctb import brief, cockpit_v2, work_lines
    from ctb.config import Config

    def mutate(state):
        rows = [("gamma", "Pick a caption style", "open", "owner", 3 * 24 * 60),
                ("delta", "Old note", "open", "", 3 * 24 * 60)]
        state["brief"] = brief.build(Config(), state, [], state["now"], provider=work_lines.SampleProvider(rows),
                                     block_rows=[], card_rows=[], events=[], seen=0.0)
        cfg = SimpleNamespace(projects=[], brief={})
        for d in state["decisions"]:
            for key in ("project_key", "project_name", "project_asset"):
                d.pop(key, None)
        state["cockpit_v2"] = cockpit_v2.presentation(cfg, state, [], state["now"])
    got = run("var grid=allEls().filter(e=>e.attrs['data-v2-section']==='projects')[0],"
              "fold=allEls().filter(e=>e.attrs['data-v2-section']==='quiet-projects')[0],"
              "keys=n=>{var r=[];if(n)walk(n,e=>{if(e.attrs['data-project'])r.push(e.attrs['data-project']);});return r;};"
              "emit({main:keys(grid),quiet:keys(fold),p:sandbox.last.cockpit_v2.projects.filter(p=>p.key==='gamma')[0]});",
              mutate=mutate)
    assert got["p"]["waiting"] == 1 and got["p"]["status"] == "waiting" and got["p"]["stale"] and got["p"]["percent"] is None
    assert "gamma" in got["main"] and "gamma" not in got["quiet"]
    assert "delta" in got["quiet"] and "delta" not in got["main"]
