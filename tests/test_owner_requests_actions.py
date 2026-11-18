"""Owner actions use only the installed CLI; Push uses the existing guarded idle-line loop."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from ctb import config, idle_lines, owner_request_actions as actions, owner_requests, panel, panel_page
from test_owner_requests import NOW, item, put, run_requests_js


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    tmp_path = tmp_path/'ledger'
    cfg = config.Config(brief={'ledger_dir': str(tmp_path), 'ledger_tool': ['/test/ledger-tool']})
    put(tmp_path, 'project', [item(), item(2)])
    calls = []
    def cli(argv, **kw):
        calls.append((argv, kw))
        # The external tool owns all mutation. It can preserve a concurrent unrelated edit.
        _, project, verb, ident, *rest = argv
        note = rest[-1]
        path = tmp_path / (project + '.json')
        data = json.loads(path.read_text())
        row = next(i for i in data['items'] if i['id'] == int(ident))
        if verb == 'done':
            assert rest == ['--if-open', note]
            if row['status'] != 'open':
                raise subprocess.CalledProcessError(3, argv)
            row['status'] = 'done'
        else:
            assert verb == 'note'
        row['notes'].append(note)
        row['updated'] = NOW
        path.write_text(json.dumps(data))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(actions.subprocess, 'run', cli)
    cfg.calls = calls
    return cfg, tmp_path


def current(ledger):
    return json.loads((ledger[1] / 'project.json').read_text())['items'][0]


@pytest.mark.parametrize('action,verb,note', [('done','done','done by owner'),
    ('drop','done','dropped by owner'), ('push','note','pushed by owner')])
def test_actions_are_single_cli_calls_without_panel_state(ledger, action, verb, note):
    cfg, folder = ledger
    assert actions.act(cfg, 'project', 1, action) is None
    args = ['/test/ledger-tool', 'project', verb, '1']
    assert cfg.calls[0][0] == args + (['--if-open'] if verb == 'done' else []) + [note]
    assert cfg.calls[0][1] == dict(check=True, timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    data = json.loads((folder/'project.json').read_text())
    assert not any(k.startswith('_cockpit') for k in data)
    assert not any(k.startswith('_cockpit') for k in data['items'][0])
    assert data['items'][1] == item(2)
    assert list(folder.iterdir()) == [folder/'project.json']
    assert not hasattr(actions, 'undo') and not hasattr(actions, '_LOCK')


@pytest.mark.parametrize('action', ['done','drop'])
def test_duplicate_completion_is_rejected_without_a_second_note(ledger, action):
    cfg, _ = ledger
    actions.act(cfg, 'project', 1, action)
    before = current(ledger)
    with pytest.raises(actions.AlreadyClosed, match='already closed'):
        actions.act(cfg, 'project', 1, action)
    assert len(cfg.calls) == 2 and current(ledger) == before


def test_drop_action_disappears_from_topics_but_done_remains_visible(ledger):
    from ctb import topic_cards
    cfg, folder = ledger
    actions.act(cfg, 'project', 1, 'drop')
    actions.act(cfg, 'project', 2, 'done')
    rows = owner_requests.read(NOW, folder, history=True)
    assert next(r for r in rows if r['id'] == 1)['note'] == 'dropped by owner'
    cards = topic_cards.build(rows, NOW)
    assert [c['ids'] for c in cards] == [[2]]
    assert cards[0]['state'] == 'done'


@pytest.mark.parametrize('action', ['done','drop'])
def test_completion_racing_locate_is_checked_by_the_cli(ledger, monkeypatch, action):
    cfg, folder = ledger
    cli = actions.subprocess.run
    def raced(argv, **kw):
        # A different writer closes the item after panel validation, before the CLI reads it.
        put(folder, 'project', [dict(item(), status='done', notes=['closed by lead']), item(2)])
        return cli(argv, **kw)
    monkeypatch.setattr(actions.subprocess, 'run', raced)
    with pytest.raises(actions.AlreadyClosed):
        actions.act(cfg, 'project', 1, action)
    assert current(ledger)['notes'] == ['closed by lead']


def test_push_is_immediately_urgent_without_action_history(ledger):
    cfg, folder = ledger
    put(folder, 'project', [dict(item(), wait='owner')])
    actions.act(cfg, 'project', 1, 'push')
    assert len(cfg.calls) == 1
    assert owner_requests.read(NOW, folder)[0]['urgent']
    assert len(idle_lines.read_items(cfg, NOW)) == 1


def test_no_lead_means_urgent_only_and_park_note_supersedes_push(ledger):
    cfg, folder = ledger
    actions.act(cfg, 'project', 1, 'push')
    assert idle_lines.check(cfg, NOW) == 0
    assert owner_requests.read(NOW, folder)[0]['urgent']
    actions.command(cfg, 'project', 1, 'note', 'parked: waiting on another team')
    assert idle_lines.read_items(cfg, NOW) == []


def test_push_schedules_existing_idle_tick_without_delay(ledger, monkeypatch):
    cfg, _ = ledger
    seen = []
    class Thread:
        def __init__(self, target, args, daemon):
            seen.append((target, args, daemon))
        def start(self):
            self.started = True
    monkeypatch.setattr(actions.threading, 'Thread', Thread)
    worker = actions.schedule(cfg)
    assert seen == [(idle_lines.tick, (cfg,), True)] and worker.started
    assert not hasattr(panel.PanelServer, 'request_nudge')


@pytest.mark.parametrize('project,ident', [('../project',1),('/project',1),('missing',1),
    ('project',True), ('project',0), ('project',3)])
def test_invalid_target_never_runs_tool(ledger, project, ident):
    cfg, _ = ledger
    with pytest.raises(ValueError):
        actions.act(cfg, project, ident, 'done')
    assert not cfg.calls


def test_symlink_target_never_runs_tool(ledger):
    cfg, folder = ledger
    (folder/'linked.json').symlink_to(folder/'project.json')
    with pytest.raises(OSError):
        actions.act(cfg, 'linked', 1, 'done')
    assert not cfg.calls


def test_tool_config_fallback_and_failure_have_no_panel_writer(ledger, monkeypatch):
    cfg, folder = ledger
    cfg.brief.pop('ledger_tool')
    cfg.radio = {'ledger_tool': ['/custom/tool','--private-ledger']}
    monkeypatch.setattr(actions.subprocess, 'run', lambda argv, **kw: cfg.calls.append(argv))
    actions.act(cfg, 'project', 1, 'done')
    assert cfg.calls[-1][:2] == ['/custom/tool','--private-ledger']
    cfg.radio = {}
    with pytest.raises(ValueError, match='invalid ledger tool'):
        actions.act(cfg, 'project', 1, 'done')
    cfg.brief['ledger_tool'] = ['/test/ledger-tool']
    before = (folder/'project.json').read_bytes()
    def failed(*a, **kw):
        raise subprocess.TimeoutExpired('tool',5)
    monkeypatch.setattr(actions.subprocess, 'run', failed)
    with pytest.raises(OSError):
        actions.act(cfg, 'project', 1, 'done')
    assert (folder/'project.json').read_bytes() == before


@pytest.mark.parametrize('action,code', [('done',1),('done',2),('push',3)])
def test_other_cli_exit_codes_are_unavailable_without_mutation(ledger, monkeypatch, action, code):
    cfg, folder = ledger
    before = (folder/'project.json').read_bytes()
    def failed(argv, **kw):
        raise subprocess.CalledProcessError(code, argv)
    monkeypatch.setattr(actions.subprocess, 'run', failed)
    with pytest.raises(OSError, match='ledger tool unavailable'):
        actions.act(cfg, 'project', 1, action)
    assert (folder/'project.json').read_bytes() == before


def test_older_fold_uses_last_touch_seven_day_boundary(ledger):
    cfg, folder = ledger
    put(folder, 'project', [dict(item(), wait='owner', updated=NOW-7*86400),
                          dict(item(2), wait='owner', updated=NOW-7*86400+1),
                          dict(item(3), wait='team', updated=NOW-20*86400)])
    rows = owner_requests.read(NOW, folder)
    assert {r['id']:r['older'] for r in rows} == {1:True, 2:False, 3:False}
    assert all(r['status'] == 'open' for r in rows)


def test_long_waiting_items_fold_collapsed_with_the_same_actions():
    run_requests_js('''
const old={...row('old',1,'owner'),long:true,older:true,idle:9*86400},fresh=row('new',2,'owner');
const host=renderRequests({requests:[old,fresh]});
const block=pick(host,'requests-fold').find(f=>f.attrs['data-requests']==='long');
assert.equal(button(block).attrs['aria-expanded'],'false');
assert.equal(pick(block,"request-row").length,1);
assert.equal(pick(block,'request-actions').length,1);
assert.equal(pick(block,'request-actions')[0].children.length,2);  // touch and drop replace the old actions
assert.equal(pick(host,'requests-old-review').length,1);
assert.equal(pick(pick(host,"request-groups")[0],"request-row").length,1);
''')


def test_requests_follow_cards_in_both_home_layouts():
    from test_panel_page import func
    classic, brief = func('renderAll'), func('renderBrief')
    assert classic.index('cardSections') < classic.index('renderRequests')
    assert brief.index('waitBox') < brief.index('renderRequests')
    for lang in ('he','en'):
        for key in ('requestDone','requestDrop','requestPush','requestConfirm','requestClosed','hint_cold_compact'):
            assert panel_page.STRINGS[lang][key]


def handler(cfg, req, init):
    """Actual routing and authentication without a loopback socket (sandbox forbids bind)."""
    from conftest import GOOD_TOKEN
    h = object.__new__(panel.PanelHandler)
    h.path = '/api/request'
    h.headers = {panel.INIT_HEADER: init}
    h._body = json.dumps(req).encode()
    h._off = lambda: False
    h._body_ok = lambda _: True
    results = []
    h._json = lambda code, body: results.append((code, body))
    h.server = SimpleNamespace(cfg=cfg, bot_token=GOOD_TOKEN, owner_id=7)
    h.do_POST()
    return results[-1]


@pytest.mark.parametrize('action', ['done','drop','push'])
def test_authenticated_route_uses_only_supported_actions(ledger, action, monkeypatch):
    from test_panel import signed
    cfg, _ = ledger
    scheduled = []
    monkeypatch.setattr(actions, 'schedule', lambda *a: scheduled.append(a))
    req = dict(project='project', id=1, action=action)
    for init in ('', signed(user_id=8), signed(auth_date=1)):
        assert handler(cfg, req, init)[0] == 403
    assert not cfg.calls
    status, response = handler(cfg, req, signed())
    assert status == 200 and len(cfg.calls) == 1
    assert bool(scheduled) == (action == 'push')
    assert response == {'ok': True}
    assert handler(cfg, dict(project='project', id=1, action='undo'), signed())[0] == 409
    assert handler(cfg, dict(project='project', id=1, action='undo', token='old'), signed())[0] == 400
    assert len(cfg.calls) == 1


@pytest.mark.parametrize('action', ['done','drop'])
def test_route_reports_already_closed_from_cli(ledger, action):
    from test_panel import signed
    cfg, _ = ledger
    req = dict(project='project', id=1, action=action)
    assert handler(cfg, req, signed())[0] == 200
    assert handler(cfg, req, signed()) == (409, {'error':'already closed'})


@pytest.mark.parametrize('lang', ['he', 'en'])
@pytest.mark.parametrize('action,index', [('done',0),('drop',1),('push',2)])
def test_row_requires_second_tap_within_four_seconds(lang, action, index):
    run_requests_js(r'''
var calls=[],timers=new Map(),serial=0,now=1000;
Date.now=()=>now;
var setTimeout=(fn,ms)=>{assert.equal(ms,4000);timers.set(++serial,fn);return serial;};
var clearTimeout=id=>timers.delete(id),requestAction=(r,a)=>calls.push(a);
const r=row('project',1),node=requestRow(r),buttons=pick(node,'request-actions')[0].children;
const b=buttons[INDEX],original=b.textContent;
b.click();assert.equal(b.textContent,S.requestConfirm);assert.deepEqual(calls,[]);
now+=4000;b.click();assert.deepEqual(calls,[]);  // expired even if the browser delayed its timer
assert.equal(b.textContent,S.requestConfirm);
[...timers.values()][0]();assert.equal(b.textContent,original);
b.click();buttons[(INDEX+1)%3].click();assert.equal(b.textContent,original);
b.click();now+=3999;b.click();assert.deepEqual(calls,[ACTION]);
assert.equal(b.textContent,original);assert.equal(timers.size,0);
'''.replace('INDEX',str(index)).replace('ACTION',json.dumps(action)), lang=lang)


def test_browser_has_no_replay_and_refreshes_after_closed_or_success():
    run_requests_js(r'''
var calls=[],toasts=[],loads=0;
var toast=(...a)=>toasts.push(a),load=()=>loads++;
const r=row('project',1),buttons=[el('button')];
var post=(path,payload)=>{calls.push(payload);return Promise.resolve({ok:true});};
(async()=>{
 await requestAction(r,'push',buttons);
 assert.deepEqual(calls,[{project:'project',id:1,action:'push'}]);
 assert.equal(saved.size,0);assert.equal(loads,1);
 assert.equal(toasts.at(-1)[0],S.requestSaved);
 await requestAction(r,'push',buttons);assert.equal(calls.length,1); // busy until refreshed
 buttons[0].disabled=false;
 post=()=>{calls.push('closed');return Promise.resolve({ok:false,status:409,json:()=>Promise.resolve({error:'already closed'})});};
 await requestAction(r,'done',buttons);
 assert.equal(loads,2);assert.equal(toasts.at(-1)[0],S.requestClosed);assert(!buttons[0].disabled);
 post=()=>{calls.push('lost');return Promise.reject(new TypeError('response lost'));};
 await requestAction(r,'done',buttons);
 assert.equal(calls.filter(c=>c==='lost').length,1);
 assert.equal(saved.size,0);assert.equal(loads,2);assert(!buttons[0].disabled);
})().catch(e=>{console.error(e);process.exitCode=1;});
''', functions=('requestAction',))


@pytest.mark.parametrize('gate,expected', [({},1), ({'quiet':0},0),
    ({'boxes':('', 'owner draft')},0), ({'command':'zsh'},0), ({'foreground':(99,)},0)])
def test_pushed_item_enters_existing_lead_gate_without_panel_queue(ledger, monkeypatch, gate, expected):
    from test_idle_lines import _fake_pane, entry
    cfg, _ = ledger
    cfg.idle_lines = {'leads': {'project':'lead-project-1234'}}
    actions.act(cfg, 'project', 1, 'push')
    typed = _fake_pane(monkeypatch, **gate)
    def send(session, line, before_enter):
        return idle_lines.deliver(session, line, now=NOW, sleep=lambda _:None, before_enter=before_enter)
    assert idle_lines.check(cfg, NOW, entries=[entry('lead-project-1234','Project')], send=send) == expected
    assert len(typed) == expected
    if typed:
        assert typed[0][2] == idle_lines.nudge_line('project')


def test_touch_busy_is_a_retryable_response(ledger, monkeypatch):
    from test_panel import signed
    from ctb import request_sweep
    cfg, _ = ledger
    def busy(*args):
        raise TimeoutError('request ledger busy')
    monkeypatch.setattr(request_sweep, 'touch', busy)
    assert handler(cfg, dict(project='project', id=1, action='touch'), signed()) == (503, {'error': 'unavailable'})
