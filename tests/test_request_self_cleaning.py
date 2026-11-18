import fcntl
import json
import threading
import time
from types import SimpleNamespace

import pytest

from ctb import owner_request_actions, owner_requests, request_sweep, waiting
from test_cockpit_v2_node import run

NOW = 2_000_000_000


def item(ident, updated, **extra):
    return dict(id=ident, text='Example', status='open', wait='owner', created=updated,
                updated=updated, notes=[], **extra)


def setup_ledger(tmp_path, items):
    path = tmp_path / 'example.json'
    path.write_text(json.dumps({'next': 9, 'items': items}, ensure_ascii=False, indent=1))
    return path, SimpleNamespace(brief={'ledger_dir': str(tmp_path)}, radio={})


def test_merged_pr_closes_and_preserves_ledger_format(tmp_path):
    url = 'https://github.com/example/project/pull/7'
    path, cfg = setup_ledger(tmp_path, [item(1, NOW - 10, links=[url])])
    request_sweep.sweep(cfg, NOW, fetch=lambda m: {'merged_at': '2026-01-01'})
    raw = path.read_text()
    data = json.loads(raw)
    assert raw == json.dumps(data, ensure_ascii=False, indent=1)
    assert data['next'] == 9
    assert data['items'][0]['status'] == 'done'
    assert data['items'][0]['notes'] == ['closed automatically: PR #7 merged']
    assert data['items'][0]['updated'] == NOW


def test_touch_obeys_ledger_sibling_lock_and_preserves_concurrent_change(tmp_path):
    path, cfg = setup_ledger(tmp_path, [item(1, NOW - 900000)])
    started = threading.Event()
    def worker():
        started.set()
        owner_request_actions.act(cfg, 'example', 1, 'touch')
    with open(path.with_suffix('.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        thread = threading.Thread(target=worker)
        thread.start()
        assert started.wait(1)
        time.sleep(.05)
        assert thread.is_alive()
        data = json.loads(path.read_text())
        data['items'][0]['notes'].append('parallel update')
        path.write_text(json.dumps(data))
    thread.join(2)
    assert not thread.is_alive()
    result = json.loads(path.read_text())
    assert result['items'][0]['notes'] == ['parallel update']
    assert not owner_requests.read(time.time(), tmp_path)[0]['older']


def test_dormant_waiting_excluded_and_setting_applies(tmp_path, monkeypatch):
    _, cfg = setup_ledger(tmp_path, [item(1, NOW - 8 * 86400), item(2, NOW - 3600)])
    monkeypatch.setattr(waiting.decisions, 'open_decisions', lambda: [])
    monkeypatch.setattr(waiting.board, 'open_reminders', lambda: [])
    from ctb import cards
    monkeypatch.setattr(cards, 'open_cards', lambda: [])
    assert waiting.waiting_count(cfg, now=NOW) == 1
    cfg.brief['request_dormant_days'] = 0.01
    assert waiting.waiting_count(cfg, now=NOW) == 0


@pytest.mark.parametrize('lang', ['he', 'en'])
def test_ui_folds_dormant_and_overflow_and_touch_is_one_tap(lang):
    def mutate(state):
        base = state['requests'][0]
        state['requests'] = [dict(base, id=i, updated=i, older=False) for i in range(1, 9)]
        state['requests'].append(dict(base, id=9, updated=0, older=True, wait='owner'))
    got = run("var main=allEls().find(e=>e.attrs['data-v2-section']==='requests');"
              "var active=main.querySelector('[data-requests=agents]');"
              "var dormant=main.querySelector('[data-v2-section=dormant]');"
              "var more=active.querySelector('.requests-more');"
              "var ids=active.querySelectorAll('[data-v2-request]').map(e=>e.attrs['data-v2-request']);"
              "var folded=active.querySelector('.compact-toggle').getAttribute('aria-expanded')==='false'&&"
              "dormant.querySelector('.compact-toggle').getAttribute('aria-expanded')==='false';"
              "more.click();"
              "var overflow=active.querySelectorAll('[data-v2-request]').slice(5).map(e=>e.attrs['data-v2-request']);"
              "dormant.querySelector('.request-actions').children[0].click();await flush();"
              "emit({ids,folded,overflow,posted});", lang, mutate)
    assert [int(x.split(':')[-1]) for x in got['ids']] == [8, 7, 6, 5, 4]
    assert got['folded']
    assert [int(x.split(':')[-1]) for x in got['overflow']] == [3, 2, 1]
    assert got['posted'][0][1]['action'] == 'touch'
    assert got['posted'][0][1]['id'] == 9


def test_only_merged_pr_or_closed_issue_are_completed(tmp_path):
    path, cfg = setup_ledger(tmp_path, [
        item(1, NOW, links=['https://github.com/example/project/pull/1']),
        item(2, NOW, links=['https://github.com/example/project/issues/2']),
        item(3, NOW, links=['https://github.com/example/project/issues/3']),
    ])
    def fetch(match):
        if match.group(4) == '1':
            return {'state': 'closed', 'merged_at': None}
        if match.group(4) == '2':
            return {'state': 'closed', 'state_reason': 'completed'}
        raise TimeoutError()
    request_sweep.sweep(cfg, NOW, fetch=fetch)
    items = json.loads(path.read_text())['items']
    assert [i['status'] for i in items] == ['open', 'done', 'open']
    assert items[1]['notes'] == ['closed automatically: issue #2 closed']


def test_link_removed_during_lookup_is_not_closed(tmp_path):
    url = 'https://github.com/example/project/pull/7'
    path, cfg = setup_ledger(tmp_path, [item(1, NOW, links=[url])])
    def fetch(_):
        data = json.loads(path.read_text())
        data['items'][0]['links'] = []
        path.write_text(json.dumps(data))
        return {'merged_at': '2026-01-01'}
    request_sweep.sweep(cfg, NOW, fetch=fetch)
    assert json.loads(path.read_text())['items'][0]['status'] == 'open'


def test_ten_minute_limit_survives_process_restart(tmp_path, monkeypatch):
    _, cfg = setup_ledger(tmp_path, [])
    calls = []
    finished = threading.Event()
    def fake_sweep(_cfg):
        calls.append(True)
        finished.set()
    monkeypatch.setattr(request_sweep, 'sweep', fake_sweep)
    monkeypatch.setattr(request_sweep, '_last', None)
    request_sweep.start(cfg, NOW)
    assert finished.wait(2)
    with request_sweep._guard:
        pass
    monkeypatch.setattr(request_sweep, '_last', None)
    request_sweep.start(cfg, NOW + 10)
    with request_sweep._guard:
        pass
    assert len(calls) == 1
    monkeypatch.setattr(request_sweep, '_last', None)
    request_sweep.start(cfg, NOW + 600)
    with request_sweep._guard:
        pass
    assert len(calls) == 2


@pytest.mark.parametrize('source', ['text', 'note', 'dated_note'])
def test_sweep_ignores_mentions(tmp_path, source):
    url = 'https://github.com/example/demo/issues/42'
    entry = item(1, NOW)
    if source == 'text':
        entry['text'] = f'Please review ({url}).'
    else:
        entry['notes'] = [f'See {url}.' if source == 'note' else {'at': NOW, 'text': f'See {url}.'}]
    path, cfg = setup_ledger(tmp_path, [entry])
    calls = []
    request_sweep.sweep(cfg, NOW, fetch=lambda m: calls.append(m))
    assert not calls
    assert json.loads(path.read_text())['items'][0]['status'] == 'open'


def test_sweep_requires_all_explicit_links_completed(tmp_path):
    urls = ['https://github.com/example/demo/pull/1', 'https://github.com/example/demo/issues/2']
    path, cfg = setup_ledger(tmp_path, [item(1, NOW, links=urls)])
    def fetch(match):
        return {'merged_at': 'now'} if match.group(3) == 'pull' else {'state': 'closed', 'state_reason': 'not_planned'}
    request_sweep.sweep(cfg, NOW, fetch=fetch)
    assert json.loads(path.read_text())['items'][0]['status'] == 'open'
    # Every sweep reads each link afresh (review #432b), so the next sweep sees both completed.
    request_sweep.sweep(cfg, NOW + 600, fetch=lambda m: {'merged_at': 'now'} if m.group(3) == 'pull' else {'state': 'closed', 'state_reason': 'completed'})
    assert json.loads(path.read_text())['items'][0]['status'] == 'done'


def test_added_link_during_lookup_prevents_closure(tmp_path):
    path, cfg = setup_ledger(tmp_path, [item(1, NOW, links=['https://github.com/example/demo/pull/1'])])
    def fetch(_):
        data = json.loads(path.read_text())
        data['items'][0]['links'].append('https://github.com/example/demo/pull/2')
        path.write_text(json.dumps(data))
        return {'merged_at': 'now'}
    request_sweep.sweep(cfg, NOW, fetch=fetch)
    assert json.loads(path.read_text())['items'][0]['status'] == 'open'


def test_invalid_completion_link_blocks_sweep(tmp_path):
    entry = item(1, NOW, links=['https://github.com/example/demo/pull/1', 'https://example.com/other'])
    path, cfg = setup_ledger(tmp_path, [entry])
    calls = []
    request_sweep.sweep(cfg, NOW, fetch=lambda m: calls.append(m))
    assert not calls
    assert json.loads(path.read_text())['items'][0]['status'] == 'open'


def test_busy_ledger_bounds_touch_and_sweep(tmp_path):
    url = 'https://github.com/example/demo/pull/1'
    path, cfg = setup_ledger(tmp_path, [item(1, NOW, links=[url])])
    with open(path.with_suffix('.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(TimeoutError, match='busy'):
            owner_request_actions.act(cfg, 'example', 1, 'touch')
        assert 1.8 <= time.monotonic() - started < 3.5
        started = time.monotonic()
        request_sweep.sweep(cfg, NOW, fetch=lambda m: {'merged_at': 'now'})
        assert 1.8 <= time.monotonic() - started < 3.5
    assert json.loads(path.read_text())['items'][0]['status'] == 'open'
    request_sweep.sweep(cfg, NOW, fetch=lambda m: {'merged_at': 'now'})
    assert json.loads(path.read_text())['items'][0]['status'] == 'done'


@pytest.mark.parametrize('state', [
    {'state': 'closed'},
    {'state': 'closed', 'state_reason': 'not_planned'},
    {'state': 'closed', 'state_reason': 'completed', 'pull_request': {}},
])
def test_issue_requires_explicit_completed_reason_and_is_not_pr(tmp_path, state):
    path, cfg = setup_ledger(tmp_path, [item(1, NOW, links=['https://github.com/example/demo/issues/2'])])
    request_sweep.sweep(cfg, NOW, fetch=lambda _: state)
    assert json.loads(path.read_text())['items'][0]['status'] == 'open'
