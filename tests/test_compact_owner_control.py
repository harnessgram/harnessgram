"""Cold compaction uses API activity and obeys the owner's cancellable choice."""
import json
import time
from types import SimpleNamespace

import pytest

from ctb import compact, config, inbox, panel_settings, usage, wake
from ctb.idle_lines import _foreground_claude as real_foreground_claude
from test_compact_typing_gate import SID, auto, pane  # noqa: F401

pytestmark = pytest.mark.real_type_command


def transcript(tmp_path, stamps):
    path = tmp_path / 'conversation.jsonl'
    path.write_text('\n'.join(json.dumps(row) for row in stamps))
    return str(path)


def test_cold_setting_default_validation_and_settings_round_trip():
    assert config.from_dict({}).cold_compact == 'on'
    assert panel_settings.read()['cold_compact'] == 'on'
    panel_settings.write('cold_compact', 'off')
    assert config.load().cold_compact == 'off'
    assert panel_settings.read()['cold_compact'] == 'off'
    for bad in (True, 1, None, 'yes', []):
        with pytest.raises((ValueError, config.ConfigError)):
            config.from_dict({'cold_compact': bad})
        assert not panel_settings.allowed('cold_compact', bad)


@pytest.mark.parametrize('entry', [compact, wake])
@pytest.mark.parametrize('idle,ctx,on,expected', [
    (3599, 200000, 'on', False), (3600, 150000, 'on', True),
    (7200, 149999, 'on', False), (7200, 200000, 'off', False),
    (7200, 200000, 'on', True), (-10, 200000, 'on', False)])
def test_idle_is_since_last_assistant_not_owner_or_waiter(tmp_path, monkeypatch, entry, idle, ctx, on, expected):
    now = time.time()
    path = transcript(tmp_path, [
        {'type': 'assistant', 'timestamp': now - 9000},
        {'type': 'assistant', 'timestamp': now - idle},
        {'type': 'user', 'timestamp': now},  # just-arrived owner message does not warm the API cache
        {'type': 'assistant', 'timestamp': now, 'isSidechain': True}])
    compact.set_auto_compact_limit(400)
    panel_settings.write('cold_compact', on)
    monkeypatch.setattr(usage, 'context_tokens', lambda p: (ctx, 'model'))
    monkeypatch.setattr(entry, 'usage_of', lambda p: (ctx, ''), raising=False)
    # wake imports status.usage_of at call time
    from ctb import status
    monkeypatch.setattr(status, 'usage_of', lambda p: (ctx, ''))
    calls = []
    def start(*a, **k):
        calls.append(a)
        compact.cold_transition(a[0], k["cold_attempt"], "started")
        config.write_private(compact.marker('s'), '')
    kw = {'auto_compact_fn': start} if entry is compact else {'spawn_fn': start}
    # idle_since deliberately disagrees: only the transcript is authoritative.
    result = entry.cold_compact('s', path, idle_since=now - (1 if idle > 3600 else 20000), clock=lambda: now, **kw)
    assert result is expected and bool(calls) is expected


def test_timestamp_iso_malformed_unknown_and_latest_value(tmp_path):
    path = transcript(tmp_path, [{'type': 'assistant', 'timestamp': '2026-01-01T12:00:00Z'},
                                 {'type': 'assistant', 'timestamp': '2026-01-01T11:00:00Z'},
                                 {'type': 'assistant', 'timestamp': 'bad'}])
    assert compact.last_api_activity(path) == 1767268800
    assert compact.last_api_activity('/missing') is None
    compact.set_auto_compact_limit(400)
    assert not compact.cold_eligible('s', '/missing', time.time())


def test_cancellation_and_start_are_mutually_exclusive():
    first = compact.cold_offer('s')
    assert not compact.cold_transition('other', first, 'cancelled')
    assert compact.cold_transition('s', first, 'cancelled')
    assert not compact.cold_transition('s', first, 'started')
    second = compact.cold_offer('s')
    assert not compact.cold_transition('s', first, 'cancelled')
    assert compact.cold_transition('s', second, 'started')
    assert not compact.cold_transition('s', second, 'cancelled')


def test_cold_notice_button_cancels_before_terminal_submission(pane, auto):
    def cancel(_):
        notice = auto.bot.methods('sendMessage')[-1]
        data = notice['reply_markup']['inline_keyboard'][0][0]['callback_data']
        assert 'cache' in notice['text'] and 'minute' in notice['text']
        assert compact.cold_transition(SID, data[5:], 'cancelled')
    assert not inbox.auto_compact(SID, 200000, cold=True, cfg=auto.cfg, bot=auto.bot,
                                  topics=auto.topics, sleep_fn=cancel)
    assert not pane.sent and not auto.started


@pytest.mark.parametrize('started', [False, True])
def test_notice_callback_is_owner_and_session_bound(auto, started):
    token = compact.cold_offer(SID)
    if started:
        compact.cold_transition(SID, token, 'started')
    topics = SimpleNamespace(session_at=lambda *a: SID)
    q = {'id': 'q', 'data': 'cold:' + token, 'from': {'id': 8},
         'message': {'message_id': 3, 'chat': {'id': 7, 'type': 'private'}}}
    assert inbox.handle_button(q, auto.cfg, auto.bot, topics, None) == 'ignored'
    q['from']['id'] = 7
    assert inbox.handle_button(q, auto.cfg, auto.bot, topics, None) == ('cold-started' if started else 'cold-cancelled')
    text = auto.bot.methods('answerCallbackQuery')[-1]['text']
    assert ('cannot cancel' if started else 'Cancelled') in text


def test_cold_compaction_resolves_the_new_pane_after_the_cancellation_wait(pane, auto, monkeypatch):
    target = ['%1']
    monkeypatch.setattr(inbox, '_pane_from_process', lambda s: target[0] if s == SID else None)
    def move(_):
        target[0] = '%2'  # the old pane now belongs to another session
    assert inbox.auto_compact(SID, 200000, cold=True, cfg=auto.cfg, bot=auto.bot,
                              topics=auto.topics, sleep_fn=move)
    keys = [c for c in pane.calls if c[0] == 'send-keys']
    assert keys and all(c[2] == '%2' for c in keys)
    assert auto.started[0][4] == '%2'


@pytest.mark.parametrize('cold', [False, True])
@pytest.mark.parametrize('change', ['session', 'foreground'])
@pytest.mark.parametrize('moment', ['before_typing', 'before_enter'])
def test_compaction_rechecks_exact_session_and_foreground_at_both_typing_boundaries(
        pane, auto, monkeypatch, cold, change, moment):
    from ctb import idle_lines
    process = {'pane': '%1', 'foreground': [(42, 'start')]}
    monkeypatch.setattr(inbox, 'session_pid', lambda s: 42 if s == SID else None)
    monkeypatch.setattr(inbox, '_pane_from_process', lambda s: process['pane'] if s == SID else None)
    monkeypatch.setattr(inbox, '_pane_claude_targets', lambda p: process['foreground'])
    # Use the real foreground check rather than the fixture's always-true guard.
    monkeypatch.setattr(idle_lines, '_foreground_claude', real_foreground_claude)
    def change_process():
        if change == 'session':
            process['pane'] = '%2'
        else:
            process['foreground'] = [(99, 'another process')]
    original = inbox._tmux
    def tmux(*args):
        result = original(*args)
        if moment == 'before_enter' and args[0] == 'send-keys' and '-l' in args:
            change_process()
        return result
    monkeypatch.setattr(inbox, '_tmux', tmux)
    original_box = inbox.input_box
    reads = [0]
    def box(p):
        result = original_box(p)
        reads[0] += 1
        # The first read is the auto precheck; the next is inside the typing gate.
        if moment == 'before_typing' and reads[0] == 2:
            change_process()
        return result
    monkeypatch.setattr(inbox, 'input_box', box)
    assert not inbox.auto_compact(SID, 200000, cold=cold, cfg=auto.cfg, bot=auto.bot,
                                  topics=auto.topics, sleep_fn=lambda _: None)
    assert not pane.sent and not auto.started
    if moment == 'before_typing':
        assert not [c for c in pane.calls if c[0] == 'send-keys']
    assert not [c for c in pane.calls if c[0] == 'send-keys' and 'Enter' in c]


@pytest.mark.parametrize('entry', [compact, wake])
def test_timeout_cancels_exact_attempt_and_late_worker_never_announces_or_types(pane, auto, monkeypatch, entry):
    from ctb import status
    monkeypatch.setattr(compact, 'cold_eligible', lambda *a: True)
    monkeypatch.setattr(compact, 'limit_for', lambda s: 400)
    monkeypatch.setattr(usage, 'context_tokens', lambda p: (200000, 'model'))
    monkeypatch.setattr(status, 'usage_of', lambda p: (200000, 'model'))
    now = [time.time()]
    launched = []
    def launch(s, ctx, **kw):
        launched.append(kw['cold_attempt'])
    def advance(_):
        now[0] += 31
    kw = {'auto_compact_fn':launch} if entry is compact else {'spawn_fn':launch}
    assert not entry.cold_compact(SID, 'ignored', 0, clock=lambda:now[0], sleep=advance, **kw)
    token, = launched
    assert compact.cold_status(SID, token) == 'cancelled'
    assert not inbox.auto_compact(SID, 200000, cold=True, cold_attempt=token,
        cfg=auto.cfg, bot=auto.bot, topics=auto.topics, sleep_fn=lambda _: None)
    assert not auto.bot.methods('sendMessage') and not pane.sent and not auto.started


def test_timeout_after_notice_before_submission_prevents_all_typing(pane, auto):
    token = compact.cold_offer(SID)
    def timeout(_):
        assert not compact.cold_timeout(SID, token)
    assert not inbox.auto_compact(SID, 200000, cold=True, cold_attempt=token,
        cfg=auto.cfg, bot=auto.bot, topics=auto.topics, sleep_fn=timeout)
    assert len(auto.bot.methods('sendMessage')) == 1
    assert not [c for c in pane.calls if c[0] == 'send-keys']
    assert not auto.started


def test_late_attempt_cannot_replace_newer_offer_or_cancel_it(pane, auto):
    old = compact.cold_offer(SID)
    new = compact.cold_offer(SID)
    assert not compact.cold_timeout(SID, old)
    assert compact.cold_status(SID, new) == 'waiting'
    assert not inbox.auto_compact(SID, 200000, cold=True, cold_attempt=old,
        cfg=auto.cfg, bot=auto.bot, topics=auto.topics, sleep_fn=lambda _: None)
    assert compact.cold_status(SID, new) == 'waiting'
    assert not auto.bot.methods('sendMessage') and not pane.sent


def test_timeout_cannot_fall_back_after_exact_worker_claimed_submission(pane, auto):
    token = compact.cold_offer(SID)
    assert inbox.auto_compact(SID, 200000, cold=True, cold_attempt=token,
        cfg=auto.cfg, bot=auto.bot, topics=auto.topics, sleep_fn=lambda _: None)
    assert compact.cold_timeout(SID, token)
    assert compact.cold_status(SID, token) == 'started'
    assert pane.sent == ['/compact']


def test_detached_worker_receives_the_exact_cold_attempt(monkeypatch):
    seen = []
    monkeypatch.setattr(compact.subprocess, 'Popen', lambda argv, **kw: seen.append(argv))
    assert compact.spawn_auto_compact(SID, 200000, cold=True, cold_attempt='exact-attempt')
    assert '--cold-attempt=exact-attempt' in seen[0]
    from ctb import cli
    args = cli._parser().parse_args(['auto-compact', SID, '200000', 'cold', '--cold-attempt=exact-attempt'])
    assert args.cold_attempt == 'exact-attempt'


def test_failed_enter_cancels_attempt_instead_of_leaving_waiter_compacting(pane, auto, monkeypatch):
    token = compact.cold_offer(SID)
    original = inbox._tmux
    def fail_enter(*args):
        if args[0] == 'send-keys' and 'Enter' in args:
            return SimpleNamespace(returncode=1)
        return original(*args)
    monkeypatch.setattr(inbox, '_tmux', fail_enter)
    assert not inbox.auto_compact(SID, 200000, cold=True, cold_attempt=token,
        cfg=auto.cfg, bot=auto.bot, topics=auto.topics, sleep_fn=lambda _:None)
    assert compact.cold_status(SID, token) == 'cancelled'
    assert not compact.cold_timeout(SID, token)
    assert not pane.sent and not auto.started


def test_unrelated_fresh_compaction_marker_cannot_satisfy_a_cold_attempt():
    token = compact.cold_offer(SID)
    compact.marker(SID).write_text('an unrelated worker')
    now = [time.time()]
    def advance(_):
        now[0] += 31
    assert not compact.wait_cold(SID, token, now[0], 30, lambda:now[0], advance, 1)
    assert compact.cold_status(SID, token) == 'cancelled'
