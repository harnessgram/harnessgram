"""Automatic topic derivation and offline interaction proof, without external services."""
import json

import pytest

from ctb import owner_requests, panel, panel_page, topic_cards
from ctb.config import Config
from test_owner_requests import NOW, item, put, run_requests_js
from test_panel_page import func


def row(ident=1, **extra):
    value = dict(project="demo", id=ident, title="First sentence. More detail.", status="open", wait="team",
                 created=NOW - 1000, updated=NOW - 20, note="", steps=[])
    value.update(extra)
    return value


@pytest.mark.parametrize('url,state,expected', [
    ('pull/12', {}, ('development', 40, 1)),
    ('pull/12', {'state': 'open'}, ('development', 40, 1)),
    ('pull/12', {'state': 'open', 'draft': True}, ('development', 40, 1)),
    ('pull/12', {'state': 'open', 'head_pushed_at': '2026-11-16T09:00:00Z',
                 'comments': [{'body': 'review: ship\nnotes', 'created_at': '2026-11-16T10:00:00Z'}]}, ('review', 70, 2)),
    ('pull/12', {'state': 'open', 'comments': [{'body': 'notes\nreview: ship'}]}, ('development', 40, 1)),
    ('pull/12', {'state': 'open', 'ship': True}, ('review', 70, 2)),
    ('pull/12', {'state': 'open', 'head_committed_at': '2026-11-16T09:30:00Z',
                 'head_pushed_at': '2026-11-16T11:00:00Z',
                 'comments': [{'body': 'review: ship', 'created_at': '2026-11-16T10:00:00Z'}]}, ('development', 40, 1)),
    ('pull/12', {'state': 'closed', 'merged_at': '2026-11-16'}, ('active', 100, 4)),
    ('pull/12', {'state': 'closed', 'ship': True}, ('development', 40, 1)),
    ('issues/13', {'state': 'closed', 'state_reason': 'completed'}, ('active', 100, 4)),
    ('issues/13', {'state': 'closed', 'state_reason': 'not_planned'}, ('development', 40, 1)),
])
def test_stage_from_explicit_link_states(url, state, expected):
    url = 'https://github.com/demo/project/' + url
    card = topic_cards.build([row(links=[url])], NOW, {url: state})[0]
    assert (card['stage'], card['percent'], card['step']) == expected


def test_notes_and_completion_stages_and_dropped_members():
    cards = topic_cards.build([row(1), row(2, note='Next: implement'),
                               row(3, status='done'), row(4, status='done', note='dropped by owner')], NOW)
    assert {c['ids'][0]: (c['stage'], c['percent'], c['step']) for c in cards} == {
        1: ('planning', 10, 0), 2: ('development', 40, 1), 3: ('active', 100, 4)}
    # Dropping a member must not remove the remaining work in the same topic.
    cards = topic_cards.build([row(1, topic='t', status='done', note='dropped by owner'), row(2, topic='t')], NOW)
    assert cards[0]['ids'] == [2]
    assert len(topic_cards.build([row(note='Dropped but still open')], NOW)) == 1


@pytest.mark.parametrize('note', ['dropped by owner', 'Dropped By Owner', 'DROPPED BY OWNER'])
def test_dropped_marker_is_case_insensitive_and_only_hides_completed_items(note):
    assert topic_cards.build([row(status='done', note=note)], NOW) == []
    assert len(topic_cards.build([row(note=note)], NOW)) == 1


def test_group_stage_uses_least_advanced_active_member_and_full_url():
    one, two = 'https://github.com/demo/one/pull/12', 'https://github.com/demo/two/pull/12'
    rows = [row(1, topic='t', status='done'), row(2, topic='t', links=[one, two])]
    states = {one: {'state': 'closed', 'merged_at': '2026-11-16'}, two: {'state': 'open', 'ship': True}}
    assert topic_cards.build(rows, NOW, states)[0]['stage'] == 'review'
    assert topic_cards.build(rows + [row(3, topic='t')], NOW, states)[0]['stage'] == 'planning'


def test_panel_preserves_links_and_uses_stale_cache_without_any_fetch(tmp_path, monkeypatch):
    from ctb import request_sweep
    url = 'https://github.com/demo/project/pull/12'
    put(tmp_path, 'demo', [item(links=[url])])
    cfg = Config(brief={'ledger_dir': str(tmp_path)})
    request_sweep.sweep(cfg, NOW - 900, lambda _: {'state': 'open', 'ship': True})
    def forbidden(*args, **kwargs):
        pytest.fail('the panel must never fetch or refresh GitHub state')
    monkeypatch.setattr(request_sweep, 'github_state', forbidden)
    monkeypatch.setattr(request_sweep, '_refresh_state', forbidden)
    result = panel.build_state(cfg, NOW, light=True)
    assert owner_requests.read(NOW, tmp_path, history=True)[0]['links'] == [url]
    assert 'links' not in result['requests'][0]
    assert result['topic_cards'][0]['stage'] == 'review'


@pytest.mark.parametrize("extra,state", [
    ({"status": "done", "note": "Decided: ship", "wait": "owner"}, "done"),
    ({"wait": "owner"}, "owner"),
    ({"updated": NOW - 86400}, "working"),
    ({"updated": NOW - 86401}, "stuck"),
    ({"updated": NOW + 10}, "working"),
    ({"note": "Decided: ship"}, "decided"),
    ({"note": "Owner picked option A", "wait": "owner"}, "owner"),
    ({"note": "owner approved the plan", "updated": NOW - 999999}, "decided"),
    ({"note": "Not Decided: still checking"}, "working"),
    ({"note": "Owner approvedly"}, "working"),
])
def test_status(extra, state):
    assert topic_cards.status(row(**extra), NOW) == state


def test_order_and_latest_decision_survives_later_work_note():
    rows = [row(1, status="done"), row(2, note="Decided: go"), row(3),
            row(4, updated=NOW - 86401), row(5, wait="owner", updated=NOW - 50),
            row(6, wait="owner", steps=[{"text": "Owner approved plan #123"}, {"text": "Check #123 and #456"}])]
    cards = topic_cards.build(rows, NOW)
    assert [c["ids"][0] for c in cards] == [6, 5, 4, 3, 2, 1]
    card = cards[0]
    assert card["state"] == "owner" and card["title"] == "First sentence."
    assert card["decision"] == "Owner approved plan #123"
    assert card["next"] == "Check #123 and #456"
    assert card["prs"] == [{"number": 123}, {"number": 456}]
    assert [n["text"] for n in card["notes"]] == [card["next"], card["decision"]]


def test_grouping_is_opt_in_project_scoped_and_open_members_win():
    rows = [row(1, topic="release", status="done", updated=NOW, steps=[{"text": "Decided: old item", "at": NOW - 80}]),
            row(2, topic="release", created=NOW - 500, updated=NOW - 40, wait="owner", steps=[{"text": "Need approval"}]),
            row(3, topic="release", created=NOW - 400, updated=NOW - 86401),
            row(4, topic="release", project="another"), row(5), row(6)]
    cards = topic_cards.build(rows, NOW)
    assert len(cards) == 4
    group = cards[0]
    assert group["ids"] == [1, 2, 3] and group["state"] == "owner"
    assert group["next"] == "Need approval" and group["decision"] == "Decided: old item"
    rows[1]["status"] = "done"
    assert next(c for c in topic_cards.build(rows, NOW) if c["ids"] == [1, 2, 3])["state"] == "stuck"
    rows[2]["status"] = "done"
    assert next(c for c in topic_cards.build(rows, NOW) if c["ids"] == [1, 2, 3])["state"] == "done"


def test_read_preserves_topic_tags_history_and_dated_notes_only_in_history_mode(tmp_path):
    notes = [f"Step {n}" for n in range(60)] + [{"at": NOW - 20, "text": "Decided: ship"}]  # dated note inside the window
    put(tmp_path, "demo", [dict(item(1), topic="release", notes=notes),
                           dict(item(2), status="done", updated=NOW - 30 * 86400),
                           dict(item(3), topic="../invalid"), dict(item(4), topic=["bad"])])
    checklist = owner_requests.read(NOW, tmp_path)
    assert len(checklist) == 3 and len(checklist[0]["steps"]) == 50
    rows = owner_requests.read(NOW, tmp_path, history=True)
    tagged = next(r for r in rows if r["id"] == 1)
    assert len(rows) == 4 and len(tagged["steps"]) == owner_requests.MAX_NOTES  # the Cockpit never shows more than the last MAX_NOTES notes
    assert tagged["topic"] == "release" and tagged["steps"][-1]["at"] == NOW - 20
    assert all("topic" not in r for r in rows if r["id"] in (3, 4))


def test_state_translation_redaction_and_refresh_reuse_the_ledger(tmp_path, monkeypatch):
    folder = tmp_path / "ledger"
    put(folder, "demo", [dict(item(), text="Original title. Extra.",
                             notes=[{"at": NOW - 5, "text": "Decided: safe #123"}, "Next step"]),
                           dict(item(2), status="done", updated=NOW - 20 * 86400)])
    cfg = Config(brief={"ledger_dir": str(folder)})
    translations = {"Original title. Extra.": "כותרת בעברית. המשך.", "Decided: safe #123": "הוחלט להתקדם", "Next step": "השלב הבא"}
    class Translator:
        def he_item(self, project, ident, updated, text):
            return translations.get(text, "")
        def he(self, text):
            return translations.get(text, "")
    source = (folder / "demo.json").read_bytes()
    reads = []
    original = owner_requests._read
    # The pre-existing idle-work detector has its own full-note reader; isolate
    # it to prove Requests and Topics themselves share one source read.
    monkeypatch.setattr(panel.idle_lines, "view", lambda *args: {"count": 0, "items": []})
    def read(path):
        reads.append(path)
        return original(path)
    monkeypatch.setattr(owner_requests, "_read", read)
    result = panel.build_state(cfg, NOW, light=True, tr=Translator())
    assert reads == [folder / "demo.json"]
    assert (folder / "demo.json").read_bytes() == source
    assert len(result["requests"]) == 1 and len(result["topic_cards"]) == 1
    card = result["topic_cards"][0]
    assert card["title_he"] == "כותרת בעברית."
    assert card["decision_he"] == "הוחלט להתקדם" and card["next_he"] == "השלב הבא"
    assert "notes" not in card  # the timeline is read when the sheet opens
    rows = owner_requests.read(NOW, folder, history=True)
    assert topic_cards.timeline(rows, card["key"], NOW, Translator())[0]["text_he"] == "השלב הבא"
    # Source and cached translations receive the final response credential filter.
    translations["Next step"] = "token " + "ghp_" + "A" * 36
    assert "ghp_" not in json.dumps(panel.build_state(cfg, NOW, light=True, tr=Translator())["topic_cards"])
    put(folder, "demo", [dict(item(), status="done")])
    assert panel.build_state(cfg, NOW, light=True)["topic_cards"][0]["state"] == "done"


def test_state_count_uses_the_same_open_ledger_rows(tmp_path, monkeypatch):
    from ctb import board, cards, decisions
    put(tmp_path, "demo", [dict(item(1), wait="owner"), dict(item(2), wait="team"),
                           dict(item(3), wait="owner", status="done")])
    monkeypatch.setattr(decisions, "open_decisions", lambda: [])
    monkeypatch.setattr(board, "open_reminders", lambda: [])
    monkeypatch.setattr(cards, "open_cards", lambda: [])
    result = panel.build_state(Config(brief={"ledger_dir": str(tmp_path)}), NOW, light=True)
    assert result["waiting_count"] == 1


def test_active_translation_titles_precede_archived_titles(tmp_path):
    put(tmp_path, "a-archive", [dict(item(), status="done", updated=NOW - 20 * 86400)])
    put(tmp_path, "z-active", [dict(item(), text="Active request")])
    queued = []
    class Translator:
        def he_item(self, project, ident, updated, text):
            queued.append(project)
            return ""
        def he(self, text):
            return ""
    panel.build_state(Config(brief={"ledger_dir": str(tmp_path)}), NOW, light=True, tr=Translator())
    assert queued == ["z-active"]


@pytest.mark.parametrize("lang", ["he", "en"])
def test_offline_cards_limit_fold_more_and_detail_timeline(lang):
    run_requests_js('''
var topicLimit=8,topicViewer=null,LANG='LANGUAGE';
const card={title:'Title',title_he:'כותרת',project:'demo',state:'working',stage:'review',percent:70,step:2,decision:'Decided: go',next:'Build',
 prs:[{number:123}],key:['demo','item',1]};
post=(path,body)=>{posted.push([path,body]);return Promise.resolve({ok:true,json:()=>Promise.resolve({notes:[{text:'Newest'},{text:'Oldest'}]})});};
const host=renderTopics({topic_cards:[...Array.from({length:10},()=>card),{...card,state:'done'}]});
assert.equal(pick(host,'topic-card').length,9);
assert.equal(pick(host,'topic-title')[0].textContent,LANG==='he'?'כותרת':'Title');
const completed=pick(host,'topics-done')[0];
const labelOf=c=>pick(topicCard(c),'topic-next')[0].children[0].textContent;
// Hebrew labels follow the translated text even for a legacy blank-original card.
const plain={...card,next:'',next_he:'בניית',next_explicit:false},explicit={...card,next:'',next_he:'הבא: בניית',next_explicit:true};
if(LANG==='he'){assert.equal(labelOf(plain),S.topicLatest);assert.equal(labelOf(explicit),S.topicNext);}
if(LANG==='he')assert(text(topicCard(plain)).includes('בניית'));assert(!text(topicCard({...card,title:'Original',title_he:'כותרת'})).includes(LANG==='he'?'Original':'כותרת'));   // each page shows its own language, never the other
const original={...card,next:'Build',next_explicit:false};delete original.next_he;assert.equal(labelOf(original),S.topicLatest);assert.equal(button(completed).attrs['aria-expanded'],'false');
assert.equal(pick(host,'topic-stage')[0].textContent,LANG==='he'?'סקירה':'Review');
const meter=pick(host,'topic-meter')[0];assert.equal(meter.attrs.role,'progressbar');
assert.equal(meter.attrs['aria-valuenow'],'70');assert.equal(pick(host,'topic-meter-fill')[0].style.inlineSize,'70%');
assert.equal(pick(host,'topics-more').length,1);pick(host,'topics-more')[0].click();
assert.equal(pick(host,'topic-card').length,11);assert.equal(pick(host,'topics-more').length,0);
assert(text(host).includes('#123'));assert(!text(host).includes('Merged'));
Element.prototype.showModal=function(){this.open=true;};Element.prototype.close=function(){this.open=false;};
const body=el('body');body.style={overflow:''};var document={body,documentElement:{dir:'rtl'}};
const opener=pick(host,'topic-card')[0];opener.isConnected=true;
(async()=>{
opener.click();const dialog=body.children[0];assert.equal(dialog.open,true);assert.equal(body.style.overflow,'hidden');
assert.deepEqual(posted,[['/api/topic-detail',{key:card.key}]]);
await tick();await tick();   // the timeline is read when the sheet opens
assert.deepEqual(pick(dialog,'topic-timeline')[0].children.map(n=>text(n).trim()),['Newest','Oldest']);
assert.equal(pick(dialog,'topic-close')[0].textContent,S.topicClose);pick(dialog,'topic-close')[0].click();
assert(!dialog.open);assert(opener.focused);assert.equal(body.style.overflow,'');
opener.click();const again=body.children[0];again.events.cancel({preventDefault(){}});assert(!again.open);
})().catch(e=>{console.error(e);process.exit(1);});
'''.replace("LANGUAGE", lang), lang=lang,
        functions=("topicText", "topicField", "topicPRs", "topicCard", "openTopicDetails", "renderTopics"))


def test_topics_use_existing_home_and_dismissal_boundaries():
    assert func("renderAll").index("decisions(dl,true)") < func("renderAll").index("renderTopics(d)")
    assert func("renderBrief").index("waitBox(b,d)") < func("renderBrief").index("renderTopics(d)") < func("renderBrief").index("linesBox(b)")
    assert "bindBackLayer(\"topic-details\"" in func("openTopicDetails")
    assert panel_page.STRINGS["he"].keys() == panel_page.STRINGS["en"].keys()


def test_latest_update_comes_only_from_open_items():
    rows = [row(1, topic="t", status="done", updated=NOW, steps=[{"text": "Finished", "at": NOW}]),
            row(2, topic="t", wait="owner", note="Owner approved plan",
                steps=[{"text": "Owner approved plan", "at": NOW - 10}])]
    card = topic_cards.build(rows, NOW)[0]
    assert card["state"] == "owner"
    assert card["decision"] == "Owner approved plan"
    assert card["next"] == "Owner approved plan" and not card["next_explicit"]
    rows[1]["steps"].append({"text": "Next: ask owner"})
    card = topic_cards.build(rows, NOW)[0]
    assert card["next"] == "Next: ask owner" and card["next_explicit"]
    rows[1]["status"] = "done"
    assert topic_cards.build(rows, NOW)[0]["next"] == ""


def test_topic_cards_never_expose_older_notes_or_expired_completions(tmp_path):
    notes = ["Private old note #777"] + [f"Update {i}" for i in range(50)]
    put(tmp_path, "demo", [dict(item(1), notes=notes),
                           dict(item(2), status="done", updated=NOW - 5 * topic_cards.DAY),  # finished days ago: still reachable
                           dict(item(3), status="done", updated=NOW - owner_requests.DONE_DAYS - 1),
                           dict(item(4), status="done", updated=NOW - owner_requests.DONE_DAYS)])
    result = panel.build_state(Config(brief={"ledger_dir": str(tmp_path)}), NOW, light=True)
    assert {c["key"][2] for c in result["topic_cards"]} == {1, 2, 4}
    card = next(c for c in result["topic_cards"] if c["key"][2] == 1)
    assert "notes" not in card
    rows = owner_requests.read(NOW, tmp_path, history=True)
    timeline = topic_cards.timeline(rows, card["key"], NOW)
    assert len(timeline) == owner_requests.MAX_NOTES and "Private old note" not in json.dumps(timeline)
    assert "Private old note" not in json.dumps(result["topic_cards"])
    assert card["prs"] == []


@pytest.mark.parametrize("lang", ["he", "en"])
def test_topic_update_label_matches_explicit_prefix(lang):
    run_requests_js('''
var LANG='LANGUAGE';
const base={title:'Title',project:'demo',state:'owner',decision:'Approved',next:'Waiting',prs:[]};
assert(text(topicCard(base)).includes(S.topicLatest));
assert(text(topicCard({...base,next:'Next: act',next_explicit:true})).includes(S.topicNext));
assert(text(topicCard({...base,next:''})).includes(S.topicNoNext));
'''.replace("LANGUAGE", lang), lang=lang,
        functions=("topicText", "topicField", "topicPRs", "topicCard"))


def test_history_keeps_the_same_note_window_even_with_blank_notes(tmp_path):
    # Blank notes among the last MAX_NOTES must not let an older note in.
    import json
    from ctb import owner_requests as R
    notes = ["very old private note"] + [""] * (R.MAX_NOTES - 1) + ["latest"]
    (tmp_path / "proj.json").write_text(json.dumps({"next": 2, "items": [
        {"id": 1, "text": "t", "status": "open", "wait": "team", "created": 1.0, "updated": 2.0, "notes": notes}]}))
    rows = R.read(10.0, tmp_path, history=True)
    texts = [s["text"] for s in rows[0]["steps"]]
    assert "very old private note" not in texts and texts[-1] == "latest"


def test_note_step_does_not_overwrite_numeric_progress_step():
    card = topic_cards.build([row(steps=[{'text': 'Next: implement'}])], NOW)[0]
    assert card['step'] == 1
    assert card['stage'] == 'development'
    assert card['next'] == 'Next: implement'


@pytest.mark.parametrize("lang", ["he", "en"])
def test_translated_slim_state_keeps_each_pages_text_and_progress(tmp_path, lang):
    put(tmp_path, "demo", [dict(item(), text="Original title. Extra.",
                               notes=["Decided: ship", "Next: build"])])
    translations = {"Original title. Extra.": "כותרת מקורית. המשך.",
                    "Decided: ship": "הוחלט להתקדם", "Next: build": "הבא: לבנות"}

    class Translator:
        def he_item(self, project, ident, updated, text):
            return translations.get(text, "")

        def he(self, text):
            return translations.get(text, "")

    result = panel.build_state(Config(brief={"ledger_dir": str(tmp_path)}), NOW,
                               light=True, tr=Translator())
    card = result["topic_cards"][0]
    assert card["title"] == "Original title."
    assert card["decision"] == "Decided: ship" and card["next"] == "Next: build"
    assert (card["stage"], card["percent"], card["step"]) == ("development", 40, 1)
    assert "notes" not in card and "steps" not in result["requests"][0]
    assert result["requests"][0]["title"] == "Original title. Extra."
    run_requests_js("""
var LANG=LANGUAGE,card=CARD;
assert.equal(topicText(card,'title'),EXPECTED_TITLE);
assert.equal(topicText(card,'decision'),EXPECTED_DECISION);
assert.equal(topicText(card,'next'),EXPECTED_NEXT);
assert.equal(pick(topicCard(card),'topic-meter')[0].attrs['aria-valuenow'],'40');
""".replace("LANGUAGE", json.dumps(lang)).replace("CARD", json.dumps(card))
        .replace("EXPECTED_TITLE", json.dumps(card["title_he"] if lang == "he" else card["title"]))
        .replace("EXPECTED_DECISION", json.dumps(card["decision_he"] if lang == "he" else card["decision"]))
        .replace("EXPECTED_NEXT", json.dumps(card["next_he"] if lang == "he" else card["next"])),
        lang=lang, functions=("topicText", "topicField", "topicPRs", "topicCard"))


def test_a_finished_card_keeps_its_decision_but_not_its_next_step():
    """Re-review P2 (#368): a finished card's card and sheet must still say what was decided."""
    base = {"key": ["p", "1"], "project": "p", "next_explicit": False, "prs": [], "stage": None, "percent": None,
            "step": None, "updated": NOW, "title": "Ship the widget", "title_he": "",
            "decision": "Decision: merged in batch 472", "decision_he": "", "next": "Watch the canary", "next_he": ""}
    done, = topic_cards.slim([dict(base, state="done")], NOW)
    assert done["decision"].startswith("Decision") and "next" not in done
    live, = topic_cards.slim([dict(base, state="working")], NOW)
    assert "decision" in live and live["next"].startswith("Watch")
