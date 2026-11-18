from types import SimpleNamespace

from ctb import board, decisions, inbox, mirror, owner_requests, panel, panel_pin, waiting
from ctb.config import Config


def test_count_includes_only_open_owner_items(monkeypatch):
    from ctb import sessions_menu
    monkeypatch.setattr(decisions, 'open_decisions', lambda: [dict(id=str(i), session='s', title=('Alpha' if i == 1 else 'Beta'), created=i) for i in (1, 2)])
    monkeypatch.setattr(sessions_menu, 'collect', lambda *a, **kw: [SimpleNamespace(session='s', state='idle')])
    monkeypatch.setattr(board, 'open_reminders', lambda: [()])
    monkeypatch.setattr(owner_requests, 'read', lambda *a, **kw: [
        {'status': 'open', 'wait': 'owner'}, {'status': 'open', 'wait': 'team'},
        {'status': 'done', 'wait': 'owner'}])
    assert waiting.waiting_count(Config()) == 4


def test_labels_and_taps_share_count(monkeypatch):
    cfg = Config(ui_language='he')
    count = [3]
    monkeypatch.setattr(waiting, 'waiting_count', lambda cfg: count[0])
    for lang, base in [('he', '🎛 הקוקפיט'), ('en', '🎛 Cockpit')]:
        cfg.ui_language = lang
        kb = mirror.live_keyboard('', lang=lang, cfg=cfg)
        label = kb['keyboard'][-1][-1]['text']
        assert label == '🎛 3' and inbox._panel_button(label)
        assert ' · 3' in panel_pin._markup(cfg, 'DemoBot', 1, 2)['inline_keyboard'][0][0]['text']
        assert not inbox._panel_button(base + ' · 3 extra')
        count[0] = 0
        assert mirror.live_keyboard('', lang=lang, cfg=cfg)['keyboard'][-1][-1]['text'] == base
        assert ' · ' not in panel_pin._markup(cfg, 'DemoBot', 1, 2)['inline_keyboard'][0][0]['text']
        count[0] = 3


def test_widget_uses_shared_count(monkeypatch):
    monkeypatch.setattr(waiting, 'waiting_count', lambda cfg: 17)
    monkeypatch.setattr(panel, 'radio_player_on', lambda cfg: False)
    assert panel.widget_view(Config(), {})['waiting_count'] == 17


def test_menu_throttles_changes_and_failures(monkeypatch):
    panel_pin._menu_state.clear()
    cfg = Config(bot_token='123:abc', owner_chat_id=7, ui_language='he', panel_base_url='https://example.com')
    calls = []
    bot = SimpleNamespace(call=lambda method, args, **kw: calls.append((method, args)))
    panel_pin._refresh_menu(cfg, bot, 0, 0)
    panel_pin._refresh_menu(cfg, bot, 1, 59)
    panel_pin._refresh_menu(cfg, bot, 1, 60)
    panel_pin._refresh_menu(cfg, bot, 1, 121)
    assert len(calls) == 2
    assert calls[-1][1]['chat_id'] == 7
    assert calls[-1][1]['menu_button']['text'] == '🎛 קוקפיט · 1'


def test_all_four_surfaces_use_shared_counter():
    import inspect
    assert 'waiting.waiting_count(cfg, now=now, requests=ledger_rows)' in inspect.getsource(panel.build_state)
    assert 'waiting.waiting_count(cfg)' in inspect.getsource(panel.widget_view)
    assert 'waiting.waiting_count(cfg)' in inspect.getsource(mirror.live_keyboard)
    assert 'waiting.waiting_count(cfg)' in inspect.getsource(panel_pin._markup)


def test_count_excludes_ended_and_replaced_but_keeps_old(monkeypatch):
    from ctb import panel_view, sessions_menu
    now = 1_000_000
    records = [dict(id=i, session=s, title=t, created=at) for i, s, t, at in [
        ('ended', 'gone', 'Question', now), ('replaced', 'live', 'Release 1', now-10),
        ('fresh', 'live', 'Release 2', now), ('old', 'live', 'Another', now-panel_view.OLD_AFTER-1)]]
    monkeypatch.setattr(waiting.time, 'time', lambda: now)
    monkeypatch.setattr(decisions, 'open_decisions', lambda: records)
    monkeypatch.setattr(sessions_menu, 'collect', lambda *a, **k: [SimpleNamespace(session='live', state='idle')])
    monkeypatch.setattr(board, 'open_reminders', lambda: [])
    monkeypatch.setattr(owner_requests, 'read', lambda *a, **kw: [])
    assert waiting.waiting_count(Config()) == 2


def test_classic_and_v2_count_use_server_value():
    import subprocess
    from pathlib import Path
    classic = Path('src/ctb/panel_page.py').read_text().split('function waitingCount(d)', 1)[1].split('\nfunction ', 1)[0]
    v2 = Path('src/ctb/cockpit_v2.js').read_text().split('function v2OpenCount(d)', 1)[1].split('\nfunction ', 1)[0]
    script = 'function waitingCount(d)' + classic + '\nfunction v2OpenCount(d)' + v2 + '\n' + '''
    const d={waiting_count:2, decisions:[], cards:[]};
    if(waitingCount(d)!==2 || v2OpenCount(d)!==2) process.exit(1);
    '''
    subprocess.run(['node', '-e', script], check=True)
    assert 'postMessage({cockpit:"badge",n:waitingCount(d)}' in Path('src/ctb/panel_page.py').read_text()


def test_menu_respects_flood_gate_and_transport_timeout(monkeypatch):
    from ctb import flood
    panel_pin._menu_state.clear()
    cfg = Config(bot_token='123:abc', owner_chat_id=7, ui_language='en', panel_base_url='https://example.com')
    calls = []
    bot = SimpleNamespace(call=lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(flood, 'allow_optional', lambda *a, **kw: False)
    monkeypatch.setattr(flood, 'remaining', lambda *a: 180)
    panel_pin._refresh_menu(cfg, bot, 1, 0)
    panel_pin._refresh_menu(cfg, bot, 1, 60)
    assert not calls
    monkeypatch.setattr(flood, 'allow_optional', lambda *a, **kw: True)
    panel_pin._refresh_menu(cfg, bot, 1, 181)
    assert len(calls) == 1 and calls[0][1]['timeout'] <= 5


def test_open_question_cards_count_unless_their_session_ended(monkeypatch):
    from ctb import cards, sessions_menu
    monkeypatch.setattr(decisions, 'open_decisions', lambda: [])
    monkeypatch.setattr(board, 'open_reminders', lambda: [])
    monkeypatch.setattr(owner_requests, 'read', lambda *a, **kw: [])
    monkeypatch.setattr(cards, 'open_cards', lambda: [
        dict(id='a', kind='choice', session='live'), dict(id='b', kind='ask', session='gone'),
        dict(id='c', kind='progress', session='live')])
    monkeypatch.setattr(sessions_menu, 'collect', lambda *a, **kw: [
        SimpleNamespace(session='live', state='idle'), SimpleNamespace(session='gone', state='ended')])
    assert waiting.waiting_count(Config()) == 1
