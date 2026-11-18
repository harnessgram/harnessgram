"""Shared link snapshots: only fakes and throwaway ledgers, never live GitHub."""
import json
from types import SimpleNamespace

import pytest

from ctb import owner_requests, request_sweep as sweep
from ctb.config import Config
from test_owner_requests import NOW, item, put

PR = 'https://github.com/demo/project/pull/12'
ISSUE = 'https://github.com/demo/project/issues/13'
HEAD_TIME = '2026-11-16T09:00:00Z'
SHA = 'abc1234' + 'd' * 33


def review(verdict, at='2026-11-16T10:00:00Z'):
    return {'body': f'review: {verdict}\nDetails', 'created_at': at}


def test_shared_links_refreshed_once_per_sweep_across_cfg_instances(tmp_path):
    put(tmp_path, 'demo', [item(1, links=[PR]), item(2, links=['https://github.com/Demo/Project/pull/12/'])])
    calls = []
    def fetch(match):
        calls.append(match.group(0))
        return {'state': 'open', 'draft': True, 'head_pushed_at': HEAD_TIME,
                'comments': [review('ship')]}
    cfg = Config(brief={'ledger_dir': str(tmp_path)})
    sweep.sweep(cfg, NOW, fetch)
    assert calls == [PR]
    sweep.sweep(Config(brief={'ledger_dir': str(tmp_path)}), NOW + 1, fetch)
    assert calls == [PR, PR]
    assert sweep.cached_states(cfg)[PR] == {'state': 'open', 'draft': True, 'ship': True}
    assert 'Details' not in (tmp_path / '.request-link-cache').read_text()
    sweep.sweep(cfg, NOW + 600, fetch)
    assert calls == [PR, PR, PR]


def test_cache_read_at_ten_seconds_is_due_at_each_six_hundred_second_sweep(tmp_path):
    put(tmp_path, 'demo', [item(links=[PR])])
    cfg = Config(brief={'ledger_dir': str(tmp_path)})
    calls = []
    def fetch(match):
        calls.append(match.group(0))
        return {'state': 'open', 'ship': len(calls) == 2}
    for count, elapsed in enumerate((10, 600, 1200), 1):
        sweep.sweep(cfg, NOW + elapsed, fetch)
        assert calls == [PR] * count
        assert sweep.cached_states(cfg)[PR]['ship'] is (count == 2)


def test_failed_reads_retry_each_sweep_and_keep_last_known_state(tmp_path):
    put(tmp_path, 'demo', [item(links=[PR])])
    cfg = Config(brief={'ledger_dir': str(tmp_path)})
    sweep.sweep(cfg, NOW, lambda _: {'state': 'open', 'ship': True})
    calls = []
    def broken(_):
        calls.append(True)
        raise OSError('offline')
    sweep.sweep(cfg, NOW + 600, broken)
    sweep.sweep(cfg, NOW + 601, broken)
    assert len(calls) == 2
    assert sweep.cached_states(cfg)[PR]['ship'] is True


def test_slow_sweep_refreshes_all_five_links_at_zero_and_six_hundred(tmp_path, monkeypatch):
    links = [PR.replace('/12', f'/{n}') for n in range(12, 17)]
    put(tmp_path, 'demo', [item(links=links)])
    cfg = Config(brief={'ledger_dir': str(tmp_path)})
    clock = [0]
    monkeypatch.setattr(sweep.time, 'time', lambda: NOW + clock[0])
    calls = []
    def fetch(match):
        calls.append(match.group(0))
        # The previous snapshot remains visible throughout this slow sweep.
        snapshot = sweep.cached_states(cfg)
        if len(calls) <= 5:
            assert snapshot == {}
        else:
            assert all(state['ship'] is False for state in snapshot.values())
        clock[0] += 20
        return {'state': 'open', 'ship': len(calls) > 5}
    sweep.sweep(cfg, fetch=fetch)
    clock[0] = 600
    sweep.sweep(cfg, fetch=fetch)
    assert calls == links * 2
    assert all(state['ship'] is True for state in sweep.cached_states(cfg).values())


def test_no_links_or_unsupported_links_never_fetch(tmp_path):
    put(tmp_path, 'demo', [item(1), item(2, links=[]), item(3, links=['https://example.com/pull/1'])])
    def forbidden(_):
        pytest.fail('a linkless/invalid item must not call GitHub')
    cfg = Config(brief={'ledger_dir': str(tmp_path)})
    sweep.sweep(cfg, NOW, forbidden)
    assert sweep.cached_states(cfg) == {}
    assert not (tmp_path / '.request-link-cache').exists()


def test_all_links_are_refreshed_and_all_must_complete_before_closing(tmp_path):
    put(tmp_path, 'demo', [item(links=[PR, ISSUE])])
    cfg = Config(brief={'ledger_dir': str(tmp_path)})
    calls = []
    states = {PR: {'state': 'open'}, ISSUE: {'state': 'closed', 'state_reason': 'completed'}}
    def fetch(match):
        calls.append(match.group(0))
        return states[match.group(0)]
    sweep.sweep(cfg, NOW, fetch)
    assert calls == [PR, ISSUE]
    assert owner_requests.read(NOW, tmp_path)[0]['status'] == 'open'
    states[PR] = {'state': 'closed', 'merged_at': '2026-11-16T10:00:00Z'}
    sweep.sweep(cfg, NOW + 600, fetch)
    assert owner_requests.read(NOW + 600, tmp_path)[0]['status'] == 'done'


@pytest.mark.parametrize('value', ['not json', '[]', '{"bad": {}}'])
def test_bad_cache_is_an_empty_offline_snapshot(tmp_path, value):
    (tmp_path / '.request-link-cache').write_text(value)
    assert sweep.cached_states(Config(brief={'ledger_dir': str(tmp_path)})) == {}


@pytest.mark.parametrize('comments,expected', [
    ([review('ship')], False),  # No proven push time in the REST PR response.
    ([review(f'ship {SHA[:7]}')], True),
    ([review(f'ship\nHead: {SHA}')], True),
    ([review(f'ship {SHA}'), review('fix-first', '2026-11-16T11:00:00Z')], False),
])
def test_github_reader_reduces_paginated_comments_without_commit_date_lookup(monkeypatch, comments, expected):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs['timeout'] == 5 and kwargs['check'] is True
        if '--paginate' in argv:
            return SimpleNamespace(stdout=json.dumps(comments))
        return SimpleNamespace(stdout=json.dumps({'state': 'open', 'head': {'sha': SHA}}))
    monkeypatch.setattr(sweep.subprocess, 'run', run)
    match = next(iter(sweep.item_urls({'links': [PR]}).values()))
    state = sweep.github_state(match)
    assert state['ship'] is expected and 'comments' not in state
    assert len(calls) == 2 and calls[0] == ['gh', 'api', 'repos/demo/project/pulls/12']
    assert 'repos/demo/project/issues/12/comments?per_page=100' in calls[1]
    assert '--slurp' in calls[1]


@pytest.mark.parametrize('comments,head,expected', [
    ([review('ship'), review('fix-first', '2026-11-16T11:00:00Z')], HEAD_TIME, False),
    ([review('fix-first', '2026-11-16T11:00:00Z'), review('ship')], HEAD_TIME, False),
    ([review('ship', '2026-11-16T11:00:00Z'), review('fix-first')], HEAD_TIME, True),
    ([review('ship'), review('ongoing', '2026-11-16T11:00:00Z')], HEAD_TIME, True),
    ([review('ship'), review('fix-first')], HEAD_TIME, False),
    ([review('ship')], '2026-11-16T10:00:01Z', False),
    ([review('ship')], '2026-11-16T10:00:00Z', False),
    ([review('ship')], '2026-11-16T12:00:00+02:00', False),
    ([review('ship')], None, False),
    ([review('ship')], 'bad date', False),
    ([review('ship')], '2026-11-16T09:00:00', False),
    ([review('ship', 'bad date')], HEAD_TIME, False),
    ([{'body': 'review: ship'}], HEAD_TIME, False),
    ([review('shipment')], HEAD_TIME, False),
    ([{'body': 'Notes\nreview: ship', 'created_at': HEAD_TIME}], HEAD_TIME, False),
])
def test_only_latest_dated_review_verdict_for_current_head_counts(comments, head, expected):
    assert sweep.shipped({'comments': comments, 'head_pushed_at': head}) is expected


@pytest.mark.parametrize('pushed,verdict,expected', [
    ('2026-11-16T11:00:00Z', 'ship', False),
    (None, 'ship', False),
    ('2026-11-16T09:59:59Z', 'ship', True),
    (None, f'ship {SHA}', True),
    (None, f'ship {SHA[:7]}', True),
    (None, f'ship {SHA[:6]}', False),
    (None, f'ship 0{SHA}', False),
    (None, f'ship {SHA}0', False),
    (None, 'ship deadbee', False),
    (None, f'ship\nHead: `{SHA}`', True),
    (None, f'fix-first {SHA}', False),
])
def test_verdict_binds_to_named_head_or_actual_push_not_commit_time(pushed, verdict, expected):
    # Locally committed at 09:30, reviewed at 10:00, only pushed at 11:00.
    state = {'head': {'sha': SHA}, 'head_committed_at': '2026-11-16T09:30:00Z',
             'head_pushed_at': pushed, 'comments': [review(verdict)]}
    assert sweep.shipped(state) is expected


@pytest.mark.parametrize('kind,state', [
    ('pull', {'state': 'closed'}),
    ('issues', {'state': 'closed', 'state_reason': 'not_planned'}),
    ('issues', {'state': 'closed', 'state_reason': 'completed', 'pull_request': {}}),
])
def test_closed_is_not_necessarily_completed(kind, state):
    assert not sweep.completed(state, kind)


def test_failed_read_never_closes_from_a_stale_cached_completion(tmp_path):
    # Review #432c: an issue cached as closed, reopened and linked to an open item, then a failed read.
    cfg = Config(brief={'ledger_dir': str(tmp_path)})
    put(tmp_path, 'other', [item(links=[ISSUE])])
    sweep.sweep(cfg, NOW, lambda _: {'state': 'closed', 'state_reason': 'completed'})
    put(tmp_path, 'demo', [item(links=[ISSUE])])
    def broken(_):
        raise OSError('offline')
    sweep.sweep(cfg, NOW + 600, broken)
    assert owner_requests.read(NOW + 600, tmp_path)[0]['status'] == 'open'
    sweep.sweep(cfg, NOW + 1200, lambda _: {'state': 'closed', 'state_reason': 'completed'})
    assert owner_requests.read(NOW + 1200, tmp_path)[0]['status'] == 'done'
