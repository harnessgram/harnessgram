"""Per-account Claude windows from source through the team card and topic keyboard."""
import json
import time

import pytest

from ctb import accounts, dashboard, mirror, statusline, usage
from ctb.config import Config, state_dir


def record(five=13, week=2):
    now = time.time()
    def window(value, delay):
        return None if value is None else {"utilization": value, "resets_at": now + delay}
    return accounts.usage_record_from_oauth({"five_hour": window(five, 3600),
                                              "seven_day": window(week, 86400 * 3)}, now)


def save(name, data):
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / f"claude-usage-{name}.json").write_text(json.dumps(data))


def test_sources_distinguish_missing_and_zero_windows():
    both = record()
    assert both['five_hour'] == 13 and both['weekly'] == 2
    assert both['five_hour_resets'] > time.time() and both['weekly_resets'] > time.time()
    assert usage.has_five_hour(both)
    assert not usage.has_five_hour(record(None, 2))
    assert accounts.usage_record_from_oauth({}, time.time()) is None
    assert usage.has_five_hour(statusline.usage_record({'five_hour': {'used_percentage': 0}}))
    assert not usage.has_five_hour(statusline.usage_record({'seven_day': {'used_percentage': 2}}))


def test_team_card_shows_each_account_and_respects_missing(monkeypatch):
    monkeypatch.setattr(accounts, 'names', lambda: ['main', 'other', 'missing'])
    monkeypatch.setattr(accounts, 'multiple', lambda: True)
    monkeypatch.setattr(accounts, 'label', lambda n: n)
    monkeypatch.setattr(accounts, 'sign_in_unfinished', lambda n: False)
    save('main', record())
    save('other', record(None, 72))
    card = '\n'.join(dashboard.quotas(cfg=Config(), lang='he', now=time.time()))
    block = card.split('5 שעות')[1].split('</pre>')[0]
    assert 'Claude · main' in block and '13%' in block and '↻' in block
    assert 'other' not in block and 'missing' not in block
    assert 'main: 5ש 13% · שבוע 2%' in card
    assert 'other: שבוע 72%' in card and 'missing: אין נתונים' in card


def test_team_card_week_shows_every_account_not_only_the_bridge_one(monkeypatch):
    monkeypatch.setattr(accounts, 'names', lambda: ['main', 'other'])
    monkeypatch.setattr(accounts, 'multiple', lambda: True)
    monkeypatch.setattr(accounts, 'label', lambda n: n)
    monkeypatch.setattr(accounts, 'sign_in_unfinished', lambda n: False)
    save('main', record(None, 100))
    save('other', record(18, 64))
    card = '\n'.join(dashboard.quotas(cfg=Config(), lang='he', now=time.time()))
    week = card.split('שבוע')[1].split('</pre>')[0]
    assert 'Claude · main' in week and '100%' in week
    assert 'Claude · other' in week and '64%' in week
    assert week.count('Claude') == 2


@pytest.mark.parametrize('five,week,expected', [(13, 2, '⏱️ 13%'), (13, 72, '⏱️ 13%'),
                                                (13, 90, '📅 90%'), (95, 92, '📅 92%'),
                                                (None, 2, '📅 2%')])
def test_keyboard_shows_five_hour_unless_week_nearly_used(monkeypatch, five, week, expected):
    monkeypatch.setattr(accounts, 'account_for', lambda _: 'other')
    monkeypatch.setattr(accounts, 'multiple', lambda: False)
    save('main', record(99, 99))
    save('other', record(five, week))
    rows = mirror.live_keyboard('', session='sample', lang='he')['keyboard']
    cells = [cell['text'] for row in rows for cell in row]
    assert expected in cells
    assert not any('99%' in cell for cell in cells)


def test_missing_account_does_not_take_another_accounts_five_hour(monkeypatch):
    monkeypatch.setattr(accounts, 'account_for', lambda _: 'other')
    monkeypatch.setattr(accounts, 'multiple', lambda: False)
    save('main', record())
    (state_dir() / 'claude-usage.json').write_text(json.dumps(record()))
    rows = mirror.live_keyboard('', session='sample', lang='he')['keyboard']
    assert not any('13%' in cell['text'] for row in rows for cell in row)


@pytest.mark.parametrize('account', ['main', 'other'])
def test_quota_rows_fallback_keeps_week_but_not_another_accounts_five_hour(account):
    shared = record(95, 72)
    save('pro', shared)
    (state_dir() / 'claude-usage.json').write_text(json.dumps(shared))
    rows = usage.quota_rows(account=account, cfg=Config(), lang='en')
    assert not any(window == '5h' for window, _, _, _ in rows)
    assert any(window == 'week' and pct == 72 for window, _, pct, _ in rows)
    save(account, record(13, 2))
    rows = usage.quota_rows(account=account, cfg=Config(), lang='en')
    assert any(window == '5h' and pct == 13 for window, _, pct, _ in rows)


@pytest.mark.parametrize('before,after,old_cell,new_cell', [
    (record(13, 2), record(24, 2), '⏱️ 13%', '⏱️ 24%'),
    (record(12, 85), record(12, 92), '⏱️ 12%', '📅 92%'),
    ({'five_hour': 0, 'five_hour_resets': None, 'weekly': None},
     {'five_hour': 0, 'weekly': None}, '', '⏱️ 0%'),
])
def test_quota_cell_changes_refresh_key(monkeypatch, before, after, old_cell, new_cell):
    monkeypatch.setattr(accounts, 'account_for', lambda _: 'other')
    monkeypatch.setattr(accounts, 'multiple', lambda: False)
    now = time.time()
    monkeypatch.setattr(mirror.time, 'time', lambda: now)
    keys = []
    for data, expected in [(before, old_cell), (after, new_cell)]:
        save('other', dict(data, at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))))
        cells = [cell['text'] for row in mirror.live_keyboard('', session='sample')['keyboard'] for cell in row]
        quotas = [cell for cell in cells if cell.startswith(('📅', '⏱️'))]
        assert quotas == ([expected] if expected else [])
        keys.append(mirror.card_data_key('sample'))
    assert keys[0] != keys[1]
    assert not mirror._only_context_moved(keys[0], keys[1])


@pytest.mark.parametrize('lang,label', [('he', '5ש'), ('en', '5h')])
@pytest.mark.parametrize('five,dot', [(0, '🟢'), (13, '🟢'), (72, '🟡'), (95, '🔴')])
def test_account_line_shows_five_hour_without_weekly(monkeypatch, lang, label, five, dot):
    monkeypatch.setattr(accounts, 'names', lambda: ['main'])
    monkeypatch.setattr(accounts, 'multiple', lambda: True)
    monkeypatch.setattr(accounts, 'label', lambda name: name)
    monkeypatch.setattr(accounts, 'sign_in_unfinished', lambda name: False)
    save('main', record(five, None))
    assert accounts.usage_lines(lang=lang)[-1] == f'{dot} main: {label} {five}%'


def test_five_hour_hit_moves_and_spent_account_is_skipped(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(usage, 'limit_of', lambda _: {'id': 'five-hour-hit'})
    monkeypatch.setattr(mirror, '_essential_place', lambda *a: SimpleNamespace(thread=1))
    monkeypatch.setattr(accounts, 'limited_account', lambda _: 'main')
    monkeypatch.setattr(accounts, 'note_limit', lambda *a: None)
    monkeypatch.setattr(mirror.identity, 'check', lambda *a, **k: None)
    monkeypatch.setattr(accounts, 'fallback_plan', lambda *a: {'auto': True, 'fresh': ['other'], 'here': 'main'})
    monkeypatch.setattr(mirror, 'send_essential', lambda *a: None)
    # Credit mode (#423): prose alone never marks an account exhausted; the transcript's API limit does.
    monkeypatch.setattr(mirror.usage, 'limit_of', lambda _: {'id': 'L1', 'kind': 'five_hour'})
    spawned = []
    assert mirror.limit_offer(Config(), None, 'sample', "You've hit your 5-hour limit", spawn_fn=spawned.append)
    assert '--target=other' in spawned[0]
    save('main', record(100, 2))
    assert accounts._spent_one('main')
    save('main', record(None, 2))
    assert not accounts._spent_one('main')


def test_claude_five_hour_ignores_the_shared_fallback_file():
    (state_dir() / 'claude-usage.json').parent.mkdir(parents=True, exist_ok=True)
    (state_dir() / 'claude-usage.json').write_text(json.dumps(record(95, 72)))
    assert usage.claude_five_hour(account='work') is None
    save('work', record(13, 2))
    assert usage.claude_five_hour(account='work') == 13.0


@pytest.mark.parametrize('bad', [1e309, float('nan'), -5, True, '40'])
def test_corrupt_five_hour_is_not_a_window(bad):
    assert not usage.has_five_hour({'five_hour': bad, 'five_hour_resets': 'x'})


def test_corrupt_usage_does_not_crash_teams(monkeypatch):
    monkeypatch.setattr(accounts, 'names', lambda: ['main', 'work'])
    rec = record(13, 2)
    rec['weekly'] = 1e309
    save('work', rec)
    save('main', record(13, 2))
    text = '\n'.join(accounts.usage_lines(lang='en'))
    assert 'inf' not in text


def test_zero_width_joiner_glues_only_emoji():
    assert len(mirror.graphemes('1234567a‍b')) == 9
    assert len(mirror.graphemes('\U0001F468‍\U0001F469‍\U0001F467')) == 1
