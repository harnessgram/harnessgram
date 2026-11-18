import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from ctb import credit_usage


def entry(stamp, ident, tokens, model='claude-sonnet-4'):
    return {'type': 'assistant', 'timestamp': stamp,
            'message': {'id': ident, 'model': model, 'usage': {'input_tokens': tokens}}}


def write(path, *rows):
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


def test_credit_intervals_daily_total_and_duplicates(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    write(first, entry('2026-11-15T21:00:00Z', 'old', 9_000_000),
          entry('2026-11-16T08:00:00Z', 'one', 1_000_000),
          entry('2026-11-16T08:01:00Z', 'one', 2_000_000))
    write(second, entry('2026-11-16T08:01:00Z', 'one', 2_000_000),
          entry('2026-11-16T09:00:00Z', 'two', 1_000_000))
    start = datetime(2026, 11, 16, 7, tzinfo=timezone.utc).timestamp()
    now = datetime(2026, 11, 16, 10, tzinfo=timezone.utc).timestamp()
    cost = credit_usage.estimate([([first, second], start, None)], now, zone=ZoneInfo('Asia/Jerusalem'))
    assert cost == 9
    assert not credit_usage.may_start(cost)  # the safety margin stops renewal before $10
    assert not credit_usage.may_start(10)
    assert not credit_usage.may_start(None)


def test_unknown_or_incomplete_transcripts_fail_closed(tmp_path):
    path = tmp_path / 'log'
    now = datetime(2026, 11, 16, 10, tzinfo=timezone.utc).timestamp()
    write(path, entry('2026-11-16T08:00:00Z', 'one', 10, 'unknown'))
    assert credit_usage.estimate([([path], 0, None)], now, zone=timezone.utc) is None
    path.write_text('{"unfinished"')
    assert credit_usage.estimate([([path], 0, None)], now, zone=timezone.utc) is None
    assert credit_usage.estimate([([tmp_path / 'missing'], 0, None)], now, zone=timezone.utc) is None


def test_closed_activation_excludes_later_subscription_usage(tmp_path):
    path = tmp_path / 'log'
    write(path, entry('2026-11-16T08:00:00Z', 'one', 1_000_000),
          entry('2026-11-16T09:00:00Z', 'two', 1_000_000))
    start = datetime(2026, 11, 16, 7, tzinfo=timezone.utc).timestamp()
    end = datetime(2026, 11, 16, 9, tzinfo=timezone.utc).timestamp()
    now = datetime(2026, 11, 16, 10, tzinfo=timezone.utc).timestamp()
    assert credit_usage.estimate([([path], start, end)], now, zone=timezone.utc) == 3


def test_overlapping_intervals_do_not_charge_anonymous_rows_twice(tmp_path):
    path = tmp_path / 'log'
    write(path, entry('2026-11-16T08:00:00Z', None, 1_000_000))
    now = datetime(2026, 11, 16, 10, tzinfo=timezone.utc).timestamp()
    assert credit_usage.estimate([([path], 0, None), ([path], 0, None)], now, zone=timezone.utc) == 3


def test_empty_cache_details_do_not_erase_reported_cache_tokens(tmp_path):
    path = tmp_path / 'log'
    row = entry('2026-11-16T08:00:00Z', 'one', 0)
    row['message']['usage'].update(cache_creation_input_tokens=1_000_000, cache_creation={})
    write(path, row)
    now = datetime(2026, 11, 16, 10, tzinfo=timezone.utc).timestamp()
    assert credit_usage.estimate([([path], 0, None)], now, zone=timezone.utc) == 3.75


def test_different_models_share_one_daily_cap_and_use_cache_prices(tmp_path):
    path = tmp_path / 'log'
    cached = entry('2026-11-16T08:00:00Z', 'cached', 0)
    cached['message']['usage'] = {
        'cache_creation_input_tokens': 1_000_000,
        'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 1_000_000}}
    write(path, cached, entry('2026-11-16T08:01:00Z', 'output', 1_000_000, 'claude-opus-4-6'))
    now = datetime(2026, 11, 16, 10, tzinfo=timezone.utc).timestamp()
    cost = credit_usage.estimate([([path], 0, None)], now, zone=timezone.utc)
    assert cost == 11
    assert not credit_usage.may_start(cost)


def test_local_midnight_is_not_utc_midnight(tmp_path):
    path = tmp_path / 'log'
    write(path, entry('2026-11-15T21:59:59Z', 'yesterday', 4_000_000),
          entry('2026-11-15T22:00:00Z', 'today', 1_000_000))
    now = datetime(2026, 11, 15, 22, 1, tzinfo=timezone.utc).timestamp()
    assert credit_usage.estimate([([path], 0, None)], now, zone=ZoneInfo('Asia/Jerusalem')) == 3


@pytest.mark.parametrize("cost,allowed", [(0, True), (8.49, True), (8.50, False), (9, False), (9.99, False),
                                        (10, False), (None, False), (float("nan"), False),
                                        (float("inf"), False), (-1, False), (True, False)])
def test_measured_spend_gate_leaves_safety_margin(cost, allowed):
    assert credit_usage.MARGIN_USD == 1.50
    assert credit_usage.spend_gate_available()
    assert credit_usage.may_start(cost) is allowed
