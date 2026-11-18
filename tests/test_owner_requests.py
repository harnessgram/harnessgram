"""External ledger validation, refresh visibility and the existing translation path."""
import json

import pytest

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


def test_display_preview_removes_internal_scaffolding_and_isolates_short_latin_names():
    raw = ("Owner 18.11 ~14:15 (voice): לתקן Foo Bar #524 מתוך "
           "~/.agents/research/notes.md וגם `~/.codex/state/session.json` עכשיו")
    assert requests.display_preview(raw) == "לתקן \u2068Foo Bar\u2069 מתוך וגם עכשיו"
    assert requests.display_preview("Owner 18.11 14:15 (typed): לבדוק This English fragment is too long היום") == "לבדוק היום"
    assert requests.display_preview("Owner 18.11 ~14:15 (screenshot iPhone): לפתוח /Users/me/private/file.txt") == "לפתוח"


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
 constructor(tag,cls,text){this.tag=tag;this.className=cls||"";this.children=[];this.textContent=text||"";this.attrs={};this.events={};this.style={};}
 appendChild(x){this.children.push(x);x.parent=this;return x;}
 get firstChild(){return this.children[0];}
 setAttribute(k,v){this.attrs[k]=v;} getAttribute(k){return this.attrs[k]===undefined?null:this.attrs[k];} removeAttribute(k){delete this.attrs[k];}
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
var nf={format:n=>'#'+n},V2=V2COPY,requestProject="",requestOwnerOnly=false;
var posted=[];function post(path,body){posted.push([path,body]);return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve(
 {title:'Original title, in full',title_he:'כותרת מלאה בעברית',note:'Original note',note_he:'הערה בעברית',steps:[{text:'Original step',text_he:'שלב בעברית'}]})});}
const tick=()=>new Promise(r=>setImmediate(r));
const saved=new Map();var localStorage={getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v)};
function row(project,id,wait='team',status='open'){
 return {project,id,wait,status,age:20,updated:5,idle:0,long:false,title:'Original title',title_he:'כותרת בעברית'};
}
'''.replace("STRINGS", json.dumps(panel_page.STRINGS[lang])).replace("V2COPY", json.dumps(panel_page.V2_STRINGS[lang], ensure_ascii=False))
    script += "\n".join(func(n) for n in ("compactStorageKey", "compactBlock", "fold", "requestText", "requestRank", "requestDetail", "requestRow", "requestList", "requestGroups", "requestBoard", "renderRequests") + tuple(functions))
    result = subprocess.run([node, "-"], input=script + "\n" + body,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_requests_group_by_project_and_split_by_who_is_waiting():
    run_requests_js('''
const rows=[...Array.from({length:12},(_,i)=>row('large',i)),row('owner',1,'owner'),
 ...Array.from({length:8},(_,i)=>row('priority',i,i<4?'owner':'team')),
 {...row('slow',1,'owner'),long:true,idle:9*86400},{...row('slow',2),long:true,idle:9*86400},row('closed',1,'owner','done')];
const host=renderRequests({requests:rows});
const heads=pick(host,'requests-head');assert.equal(pick(heads[0],'requests-count')[0].textContent,'#5');
const blocks=host.children.filter(n=>has(n,'request-groups')||has(n,'requests-fold'));
const mine=blocks[0],folds=blocks.slice(1);
const groups=pick(mine,'request-project');
assert.deepEqual(groups.map(g=>pick(g,'compact-title')[0]?.textContent??g.children[0].children[0].textContent),['priority','owner']);
assert.deepEqual(groups.map(g=>pick(g,'compact-figure')[0].textContent),['#4','#1']);
assert(groups.every(g=>button(g).attrs['aria-expanded']==='true'));
assert.deepEqual(folds.map(f=>f.attrs['data-requests']),['agents','long','done']);
assert(folds.every(f=>button(f).attrs['aria-expanded']==='false'));
const agents=folds[0],large=pick(agents,'request-project').find(g=>pick(g,'compact-title')[0].textContent==='large');
assert.equal(pick(large,'request-row').length,5);
assert.equal(pick(large,'requests-more')[0].textContent,V2.requests.showAll.replace('{n}','#12'));
pick(large,'requests-more')[0].click();assert.equal(pick(large,'request-row').length,12);
assert.equal(pick(folds[1],'request-row').length,2);   // long waiters of any kind, never deleted
assert.equal(pick(folds[2],'request-row').length,1);
assert(pick(host,'request-details').every(n=>n.hidden));
assert(pick(host,'request-row').every(n=>pick(n,'request-age').length===1));
''')


def test_five_per_group_are_urgent_first_then_newest():
    run_requests_js('''
const rows=Array.from({length:8},(_,i)=>({...row('p',i,'owner'),age:100+i*10,urgent:i===6}));
const group=pick(renderRequests({requests:rows}),'request-project')[0];
const ids=pick(group,'nm').map(n=>n.textContent);assert.equal(ids.length,5);
assert.deepEqual(rows.slice().sort(requestRank).slice(0,5).map(r=>r.id),[6,0,1,2,3]);
''')


def test_a_row_keeps_disclosure_and_visible_actions_and_loads_text_when_opened():
    run_requests_js('''
(async()=>{
const r=row('alpha',1,'owner'),n=requestRow(r,true),toggle=button(n),details=pick(n,'request-details')[0];
assert.equal(toggle.tag,'button');assert.equal(toggle.attrs['aria-expanded'],'false');assert(details.hidden);
assert.equal(toggle.attrs['aria-controls'],details.id);
assert.equal(pick(n,'request-disclosure')[0].textContent,'עוד');
assert(text(toggle).includes('כותרת בעברית'));assert.equal(posted.length,0);
assert.equal(pick(n,'request-actions')[0].parent,n);
toggle.click();await tick();await tick();
assert.equal(toggle.attrs['aria-expanded'],'true');assert(!details.hidden);
assert.equal(pick(n,'request-disclosure')[0].textContent,'פחות');
assert.deepEqual(posted,[['/api/request-detail',{project:'alpha',id:1}]]);
assert(text(details).includes('כותרת מלאה בעברית'));assert(text(details).includes('הערה בעברית'));assert(text(details).includes('שלב בעברית'));
const again=requestRow(r,true);assert.equal(button(again).attrs['aria-expanded'],'true');await tick();await tick();
assert.equal(posted.length,1);   // the same item at the same update time is not fetched twice
})().catch(e=>{console.error(e);process.exit(1);});
''')


def test_classic_request_row_places_status_line_after_the_full_width_title():
    run_requests_js('''
const n=requestRow(row('alpha',1,'owner'),true),body=pick(n,'body')[0];
assert.equal(body.children[0].className,'nm');
assert.equal(body.children[2].className,'mt request-status-line');
assert.equal(pick(body.children[2],'chip').length,1);
''')


def test_empty_and_done_only_data_survive():
    run_requests_js('''
const done=renderRequests({requests:[row('closed',1,'owner','done')]});
assert(text(done).includes(S.requestsEmpty));assert.equal(pick(done,'request-row').length,1);
const empty=renderRequests({});assert(text(empty).includes(S.requestsEmpty));assert.equal(pick(empty,'request-project').length,0);
const quiet=renderRequests({requests:[row('a',1)]});assert(text(quiet).includes(V2.requests.nothing));
''')


def test_requests_copy_and_phone_css_use_logical_properties():
    import re
    from ctb import panel_page
    for lang in ("he", "en"):
        assert set(panel_page.V2_STRINGS[lang]["requests"]) == {"needsYou", "agents", "longWait", "nothing", "showAll", "loading", "loadFailed"}
        for key in ("requestsNeedsYou", "requestsShowAll", "requestsMore", "requestsOpenCount", "requestsOwnerCount", "requestsBackAll"):
            assert panel_page.STRINGS[lang][key]
    css = panel_page.PAGE.split("/* The rail scrolls", 1)[1].split(".rem-actions{", 1)[0]
    assert "flex-wrap:nowrap" in css and "scroll-snap-type:inline" in css
    assert "-webkit-line-clamp:2" in css and "min-block-size:44px" in css
    assert not re.search(r"(?:[;{])\s*(?:left|right|margin-left|margin-right|padding-left|padding-right|width|min-width|max-width):", css)


@pytest.mark.parametrize("lang, expected", [("he", "לכל #2 הבקשות"), ("en", "Show all #2")])
def test_show_all_displays_owner_count_in_each_language(lang, expected):
    run_requests_js("""
const host=renderRequests({requests:[row('alpha',1,'owner'),row('beta',2,'owner'),row('alpha',3)]});
assert.equal(pick(host,'requests-needs')[0].children[0].children[1].textContent, EXPECTED);
""".replace("EXPECTED", json.dumps(expected)), lang)


def test_project_and_owner_filters_keep_grouping_and_expanded_limits():
    run_requests_js('''
const data={requests:[...Array.from({length:9},(_,i)=>row('alpha',i,'owner')),row('beta',1)]};
const host=renderRequests(data),group=pick(host,'request-project')[0];
pick(group,'requests-more')[0].click();assert.equal(pick(group,'request-row').length,9);
const refreshed=renderRequests(data);
assert.equal(pick(pick(refreshed,'request-project')[0],'request-row').length,9);
const rail=pick(refreshed,'requests-filter')[0];pick(rail,'btn')[1].click();
assert.equal(requestProject,'alpha');
const filtered=app.children[app.children.length-1];
assert.equal(pick(filtered,'request-project').length,1);
assert.equal(pick(pick(filtered,'requests-filter')[0],'btn')[1].attrs['aria-pressed'],'true');
pick(filtered,'link-btn')[0].click();assert.equal(requestOwnerOnly,true);
const owned=app.children[app.children.length-1];assert.equal(pick(owned,'requests-back').length,1);
pick(owned,'requests-back')[0].click();assert.equal(requestOwnerOnly,false);
''')


def test_v2_board_leaves_owner_requests_in_needs_and_folds_configured_old_items():
    run_requests_js('''
const requests=[row('p',1,'owner'),{...row('p',2,'owner'),long:true},row('p',3),
 {...row('p',4,'owner'),older:true,long:false},row('p',5,'team','done')];
const host=requestBoard({requests},{omitOwner:true});
assert.deepEqual(pick(host,'request-row').map(r=>r.attrs['data-request']).sort(),['p:3','p:4','p:5']);
const waiting=pick(host,'requests-fold').find(f=>f.attrs['data-requests']==='long');
assert.equal(button(waiting).attrs['aria-expanded'],'false');
assert.equal(pick(waiting,'request-actions')[0].children.length,2);
assert.equal(pick(waiting,'requests-old-review').length,1);
''')


def test_detail_routes_share_main_read_only_route_policy():
    assert {"/api/request-detail", "/api/topic-detail", "/api/packs", "/api/widget-state"} <= set(panel.READ_ONLY_ROUTES)
    assert "/api/voice-copilot/start" not in panel.READ_ONLY_ROUTES
    assert "/api/voice-copilot/start" not in panel.VOICE_CONTINUATION_ROUTES


def test_display_preview_handles_real_ledger_titles():
    from ctb.owner_requests import display_preview
    he = display_preview("אונר 18.11 ~14:15: להגיש את Harnessgram לאפסטור. התכנית: ~/.agents/research/x/report.md")
    assert he.startswith("להגיש") and "~/" not in he and "⁨Harnessgram⁩" in he
    assert display_preview("Owner 17.11 ~21:30 (voice): Cockpit radio should play music he likes") == \
        "Cockpit radio should play music he likes"
    assert display_preview("Team 18.11 ~22:00: bridge spend #524 is high") == "bridge spend is high"
