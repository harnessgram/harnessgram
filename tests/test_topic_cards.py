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
    assert card["notes"][0]["text_he"] == "השלב הבא"
    # Source and cached translations receive the final response credential filter.
    translations["Next step"] = "token " + "ghp_" + "A" * 36
    assert "ghp_" not in json.dumps(panel.build_state(cfg, NOW, light=True, tr=Translator())["topic_cards"])
    put(folder, "demo", [dict(item(), status="done")])
    assert panel.build_state(cfg, NOW, light=True)["topic_cards"][0]["state"] == "done"


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
const card={title:'Title',title_he:'כותרת',project:'demo',state:'working',decision:'Decided: go',next:'Build',
 prs:[{number:123}],notes:[{text:'Newest'},{text:'Oldest'}]};
const host=renderTopics({topic_cards:[...Array.from({length:10},()=>card),{...card,state:'done'}]});
assert.equal(pick(host,'topic-card').length,9);
assert.equal(pick(host,'topic-title')[0].textContent,LANG==='he'?'כותרת':'Title');
const completed=pick(host,'topics-done')[0];assert.equal(button(completed).attrs['aria-expanded'],'false');
assert.equal(pick(host,'topics-more').length,1);pick(host,'topics-more')[0].click();
assert.equal(pick(host,'topic-card').length,11);assert.equal(pick(host,'topics-more').length,0);
assert(text(host).includes('#123'));assert(!text(host).includes('Merged'));
Element.prototype.showModal=function(){this.open=true;};Element.prototype.close=function(){this.open=false;};
const body=el('body');body.style={overflow:''};var document={body,documentElement:{dir:'rtl'}};
const opener=pick(host,'topic-card')[0];opener.isConnected=true;
opener.click();const dialog=body.children[0];assert.equal(dialog.open,true);assert.equal(body.style.overflow,'hidden');
assert.deepEqual(pick(dialog,'topic-timeline')[0].children.map(n=>text(n).trim()),['Newest','Oldest']);
assert.equal(pick(dialog,'topic-close')[0].textContent,S.topicClose);pick(dialog,'topic-close')[0].click();
assert(!dialog.open);assert(opener.focused);assert.equal(body.style.overflow,'');
opener.click();const again=body.children[0];again.events.cancel({preventDefault(){}});assert(!again.open);
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
                           dict(item(2), status="done", updated=NOW - owner_requests.DONE_DAYS),
                           dict(item(3), status="done", updated=NOW - owner_requests.DONE_DAYS - 1)])
    result = panel.build_state(Config(brief={"ledger_dir": str(tmp_path)}), NOW, light=True)
    assert {c["ids"][0] for c in result["topic_cards"]} == {1, 2}
    card = next(c for c in result["topic_cards"] if c["ids"] == [1])
    assert len(card["notes"]) == 50
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
