"""External ledger validation, refresh visibility and the existing translation path."""
import json

from ctb import owner_requests as requests, panel, panel_text
from ctb.config import Config

NOW = 1_800_000_000


def item(ident=1, **extra):
    return dict(id=ident, text="Review the new screen", status="open", wait="team",
                created=NOW - 100, updated=NOW - 50, notes=[], **extra)


def put(folder, project, rows):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (project + ".json")).write_text(json.dumps({"next": 100, "items": rows}))


def test_shape_order_notes_and_recent_done(tmp_path):
    team, owner, done, expired = (item(i) for i in range(1, 5))
    owner.update(wait="owner", notes=["First step", {"at": NOW - 10, "text": "Latest\nstep"}, None, {}])
    done.update(status="done", updated=NOW - requests.DONE_DAYS)
    expired.update(status="done", updated=NOW - requests.DONE_DAYS - 1)
    put(tmp_path, "alpha", [team, done, expired, owner])
    put(tmp_path, "beta", [item()])
    rows = requests.read(NOW, tmp_path)
    assert [(r["project"], r["id"]) for r in rows] == [("alpha", 2), ("alpha", 1), ("alpha", 3), ("beta", 1)]
    assert rows[0]["age"] == 100
    assert rows[0]["note"] == "Latest step"
    assert rows[0]["steps"] == [{"text": "First step"}, {"text": "Latest step"}]


def test_malformed_size_bound_symlinks_and_item_validation(tmp_path, monkeypatch):
    put(tmp_path, "valid", [None, {}, item(), item(), dict(item(2), updated=float("nan")),
                            dict(item(3), id=True), dict(item(4), text=[]), dict(item(5), status="other")])
    (tmp_path / "broken.json").write_text("{")
    (tmp_path / "shape.json").write_text("[]")
    (tmp_path / "encoding.json").write_bytes(b"\xff")
    (tmp_path / "large.json").write_text(" " * 2000)
    (tmp_path / "link.json").symlink_to(tmp_path / "valid.json")
    monkeypatch.setattr(requests, "FILE_BYTES", 1900)
    assert [(r["project"], r["id"]) for r in requests.read(NOW, tmp_path)] == [("valid", 1)]
    assert requests.read(NOW, tmp_path / "missing") == []


def test_hard_read_bound_even_when_size_changes(tmp_path, monkeypatch):
    put(tmp_path, "large", [dict(item(), text="a" * 200)])
    monkeypatch.setattr(requests, "FILE_BYTES", 32)
    original = requests.os.fstat
    class Small:
        st_size = 0
        st_mode = 0o100600
    monkeypatch.setattr(requests.os, "fstat", lambda fd: Small())
    assert requests.read(NOW, tmp_path) == []
    monkeypatch.setattr(requests.os, "fstat", original)


def test_state_reads_new_rows_on_next_poll_and_redacts_before_translation(monkeypatch, tmp_path):
    folder = tmp_path / "requests"
    put(folder, "demo", [item()])
    cfg = Config(brief={"ledger_dir": str(folder)})
    assert len(panel.build_state(cfg, now=NOW, light=True)["requests"]) == 1
    put(folder, "demo", [item(), dict(item(2), text="Request two")])
    assert len(panel.build_state(cfg, now=NOW, light=True)["requests"]) == 2
    monkeypatch.setattr(requests, "redact_channel", lambda text: text.replace("private-value", "[hidden]"))
    put(folder, "demo", [dict(item(), text="Review private-value", notes=["Step private-value"])])
    rows = requests.read(NOW, folder)
    assert "private-value" not in json.dumps(rows)


def test_ledger_location_reuses_existing_configuration():
    from pathlib import Path
    assert requests.ledger_dir(Config()) is None
    assert requests.ledger_dir(Config(radio={"ledger_dir": "~/requests"})) == Path.home() / "requests"
    assert requests.ledger_dir(Config(brief={"ledger_dir": "/brief"}, radio={"ledger_dir": "/radio"})) == Path("/brief")


def test_translation_cache_key_is_project_id_updated_and_retryable(monkeypatch):
    tr = panel_text.Translator(Config())
    calls = []
    monkeypatch.setattr(tr, "he", lambda text: calls.append(text) or "בדיקת המסך")
    assert tr.he_item("a", 1, NOW, "Review screen") == "בדיקת המסך"
    assert tr.he_item("a", 1, NOW, "Review screen") == "בדיקת המסך"
    assert len(calls) == 1
    tr.he_item("b", 1, NOW, "Review screen")
    tr.he_item("a", 1, NOW + 1, "Review screen")
    assert len(calls) == 3
    monkeypatch.setattr(tr, "he", lambda text: None)
    assert tr.he_item("a", 2, NOW, "New title") is None
    monkeypatch.setattr(tr, "he", lambda text: "כותרת חדשה")
    assert tr.he_item("a", 2, NOW, "New title") == "כותרת חדשה"


def test_translation_fallback_and_pending_do_not_hide_rows(monkeypatch):
    tr = panel_text.Translator(Config())
    rows = [dict(item(), project="demo", title="Review screen", note="", steps=[])]
    monkeypatch.setattr(tr, "he", lambda text: None if text else "")
    pending = []
    requests.translate(rows, tr, pending)
    assert rows[0]["title"] == "Review screen" and rows[0]["title_he"] is None
    assert pending == ["request"]
    monkeypatch.setattr(tr, "he", lambda text: "בדיקת המסך" if text else "")
    requests.translate(rows, tr, [])
    assert rows[0]["title_he"] == "בדיקת המסך"


def test_titles_are_queued_before_note_histories(monkeypatch):
    tr = panel_text.Translator(Config())
    seen = []
    monkeypatch.setattr(tr, "he", lambda text: seen.append(text) or None)
    rows = [dict(project="demo", id=n, updated=NOW, title=f"Title {n}", note="A note",
                 steps=[{"text": "A step"}]) for n in (1, 2)]
    requests.translate(rows, tr, [])
    assert seen[:2] == ["Title 1", "Title 2"]


def run_requests_js(body, lang="he", functions=()):
    """Exercise the production renderer offline; the lead runs the phone browser proof."""
    import shutil
    import subprocess
    import pytest
    from ctb import panel_page
    from test_panel_page import func

    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    script = r'''
const assert=require("node:assert/strict");
class Element {
 constructor(tag,cls,text){this.tag=tag;this.className=cls||"";this.children=[];this.textContent=text||"";this.attrs={};this.events={};}
 appendChild(x){this.children.push(x);x.parent=this;return x;}
 get firstChild(){return this.children[0];}
 setAttribute(k,v){this.attrs[k]=v;}
 addEventListener(k,f){this.events[k]=f;}
 click(){this.events.click();}
 focus(){this.focused=true;}
 remove(){this.parent.children=this.parent.children.filter(x=>x!==this);}
 replaceWith(x){const i=this.parent.children.indexOf(this);this.parent.children[i]=x;x.parent=this.parent;}
 querySelector(s){return all(this).find(n=>s==='button'?n.tag==='button':s[0]==='.'?has(n,s.slice(1)):n.attrs['aria-pressed']==='true');}
}
function el(t,c,s){return new Element(t,c,s);}
function icon(){return el('svg');} function haptic(){} function sec(){}
function bindBackLayer(key,node,on,apply){return apply;}
function adoptBackLayers(){} function retireMissingBackLayers(){}
function ago(){return 'age';} function shown(r,k){return r[k+'_he']||r[k];}
function all(n){return [n,...n.children.flatMap(all)];}
function has(n,c){return n.className.split(' ').includes(c);}
function pick(n,c){return all(n).filter(x=>has(x,c));}
function text(n){return all(n).map(x=>x.textContent).join(' ');}
function button(n){return n.children[0];}
var S=STRINGS,wa={initDataUnsafe:{user:{id:7}}},compactSerial=0,opened={},app=el('main'),last=null;
var nf={format:n=>'#'+n},requestProject='',requestOwnerOnly=false,requestLimits=Object.create(null);
const saved=new Map();var localStorage={getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v)};
function row(project,id,wait='team',status='open'){
 return {project,id,wait,status,age:20,title:'Original title',title_he:'כותרת בעברית',
 note:'Original note',note_he:'הערה בעברית',steps:[{text:'Original step',text_he:'שלב בעברית'}]};
}
'''.replace("STRINGS", json.dumps(panel_page.STRINGS[lang]))
    script += "\n".join(func(n) for n in ("compactStorageKey", "compactBlock", "fold", "requestText", "requestRow", "requestList", "renderRequests") + tuple(functions))
    result = subprocess.run([node, "-e", script + "\n" + body], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_compact_requests_counts_order_shortlist_limits_and_hidden_done():
    run_requests_js('''
const rows=[...Array.from({length:12},(_,i)=>row('large',i)),row('owner',1,'owner'),
 ...Array.from({length:8},(_,i)=>row('priority',i,i<4?'owner':'team')),row('closed',1,'owner','done')];
const host=renderRequests({requests:rows}),filters=pick(host,'requests-filter')[0];
assert.deepEqual(pick(filters,'requests-pill-name').map(x=>x.textContent),[S.requestsAll,'priority','owner','large']);
assert.deepEqual(pick(filters,'requests-count').map(x=>x.textContent),['#21','#8','#1','#12']);
assert.equal(button(filters).attrs['aria-pressed'],'true');
assert.equal(pick(pick(host,'requests-needs')[0],'request-row').length,3);
const groups=pick(host,'request-project');assert.equal(groups.length,4);
assert(groups.every(g=>button(g).attrs['aria-expanded']==='false'));
const priority=groups[0];assert.equal(pick(priority,'request-row').length,5);
assert.equal(pick(priority,'requests-more')[0].textContent,S.requestsMore.replace('{n}','#3'));
const done=all(host).find(n=>n.attrs['data-block']==='requests-done:closed');
assert.equal(button(done).attrs['aria-expanded'],'false');
assert.equal(pick(done.body,'request-row').length,1);  // actual disclosure body, never a visible sibling
assert(pick(host,'request-details').every(n=>n.hidden));
''')


def test_request_disclosure_translated_details_pagination_and_refresh_state():
    run_requests_js('''
const r=row('alpha',1,'owner'),n=requestRow(r),toggle=button(n),details=pick(n,'request-details')[0];
assert.equal(toggle.tag,'button');assert.equal(toggle.attrs['aria-expanded'],'false');assert(details.hidden);
assert.equal(toggle.attrs['aria-controls'],details.id);
assert(text(n).includes('כותרת בעברית'));assert(!text(n).includes('Original'));
toggle.click();assert.equal(toggle.attrs['aria-expanded'],'true');assert(!details.hidden);
assert(text(details).includes('הערה בעברית'));assert(text(details).includes('שלב בעברית'));
const refreshed=requestRow(r);assert.equal(button(refreshed).attrs['aria-expanded'],'true');
const preview=requestRow(r,true);assert.equal(button(preview).attrs['aria-expanded'],'false');
button(preview).click();assert.equal(button(preview).attrs['aria-expanded'],'true');  // independent duplicate
const data={requests:Array.from({length:9},(_,i)=>row('alpha',i))};
const host=renderRequests(data),group=pick(host,'request-project')[0];button(group).click();
assert.equal(button(group).attrs['aria-expanded'],'true');
pick(group,'requests-more')[0].click();assert.equal(pick(group,'request-row').length,9);
assert.equal(pick(group,'requests-more').length,0);
const again=pick(renderRequests(data),'request-project')[0];
assert.equal(button(again).attrs['aria-expanded'],'true');assert.equal(pick(again,'request-row').length,9);
requestProject='alpha';const filtered=renderRequests(data);
assert.equal(pick(filtered,'request-project').length,1);assert.equal(pick(filtered,'requests-needs').length,0);
assert.equal(pick(pick(filtered,'requests-filter')[0],'btn')[1].attrs['aria-pressed'],'true');
''')


def test_requests_show_all_owner_only_and_empty_done_only_data_survive():
    run_requests_js('''
last={requests:[row('alpha',1,'owner'),row('alpha',2),row('beta',1,'owner'),row('done',1,'owner','done')]};
const host=renderRequests(last);pick(host,'requests-needs')[0].children[0].children[1].click();
const focused=app.children[0];assert.equal(pick(focused,'requests-needs').length,0);
assert.equal(pick(focused,'request-row').length,2);
assert(pick(focused,'request-project').every(g=>button(g).attrs['aria-expanded']==='true'));
assert(!text(focused).includes(S.requestsTeam));assert.equal(all(focused).filter(n=>(n.attrs["data-block"]||"").startsWith("requests-done:")).length,0);
pick(focused,'requests-back')[0].click();assert.equal(pick(app.children[0],'requests-needs').length,1);
requestProject='gone';requestOwnerOnly=false;
const done=renderRequests({requests:[row('closed',1,'owner','done')]});
assert.equal(requestProject,'');assert(text(done).includes(S.requestsEmpty));
assert.equal(pick(pick(done,'requests-filter')[0],'btn').length,1);
assert.equal(pick(done,'request-row').length,1);
const empty=renderRequests({});assert(text(empty).includes(S.requestsEmpty));assert.equal(pick(empty,'request-project').length,0);
''')


def test_requests_copy_and_phone_css_use_logical_properties():
    import re
    from ctb import panel_page
    for lang in ("he", "en"):
        for key in ("requestsNeedsYou", "requestsShowAll", "requestsMore", "requestsOpenCount", "requestsOwnerCount", "requestsBackAll"):
            assert panel_page.STRINGS[lang][key]
    css = panel_page.PAGE.split("/* The rail scrolls", 1)[1].split(".rem-actions{", 1)[0]
    assert "flex-wrap:nowrap" in css and "scroll-snap-type:inline" in css
    assert "-webkit-line-clamp:2" in css and "min-block-size:44px" in css
    assert not re.search(r"(?:[;{])\s*(?:left|right|margin-left|margin-right|padding-left|padding-right|width|min-width|max-width):", css)
