"""Regression proofs for state, ordered progress, and the dashboard hotfix port."""
import json
import os

from conftest import GOOD_TOKEN, FakeBot
from ctb import dashboard, hooks, progress
from ctb.config import Config, state_dir, write_private
from ctb.telegram import TelegramError
from ctb.topics import TopicMap, session_state, session_state_path, set_state


def cfg(**kwargs):
    return Config(bot_token=GOOD_TOKEN, owner_chat_id=7, **kwargs)


def rig():
    bot = FakeBot()
    topics = TopicMap(state_dir() / 'topics.json', bot, 7)
    topics.ensure('s', 'Title')
    return bot, topics


def test_placeholder_replacement_and_failed_cleanup_are_retried():
    bot, topics = rig()
    set_state(cfg(), 's', 'busy', bot)
    first = session_state('s')['placeholder']
    set_state(cfg(), 's', 'busy', bot)
    assert len(bot.methods('sendMessage')) == 1
    outage = {'on': True}
    real_call = bot.call

    def flaky(method, params=None, timeout=15):
        if method == 'deleteMessage' and outage['on']:
            bot.calls.append((method, params or {}))
            raise TelegramError('temporary failure')
        return real_call(method, params, timeout)
    bot.call = flaky
    set_state(cfg(), 's', 'bottom', bot)
    current = session_state('s')['placeholder']
    assert current != first and set(session_state('s')['all']) == {first, current}
    outage['on'] = False
    set_state(cfg(), 's', 'busy', bot)
    assert session_state('s')['all'] == [current]
    set_state(cfg(), 's', 'done', bot)
    assert session_state('s')['all'] == [] and 'placeholder' not in session_state('s')
    assert bot.methods('editForumTopic')[-1]['name'] == '✅ Title'
    assert topics.all()['s']['name'] == 'Title'
    assert {p['message_id'] for p in bot.methods('deleteMessage')} <= {first, current}
    assert session_state_path('s').stat().st_mode & 0o777 == 0o600


def test_topic_rename_preserves_state_and_title_beats_fallback():
    bot, topics = rig()
    write_private(state_dir() / 'topic-names.json', json.dumps({'s': 'Fallback', 'new': 'Named'}))
    set_state(cfg(), 's', 'busy', bot)
    topics.ensure('s', 'Real app title')
    assert bot.methods('editForumTopic')[-1]['name'] == '⚙️ Real app title'
    topics.ensure('new', '')
    assert topics.all()['new']['name'] == 'Named'


def test_short_interim_translation_and_fast_flush(monkeypatch):
    from ctb import translate
    calls = []
    monkeypatch.setattr(translate, 'translate_text', lambda text, config: calls.append(text) or 'בודק כעת')
    c = cfg(translate=True)
    assert progress.interim_text('Checking now', c) == 'בודק כעת'
    assert progress.interim_text('Checking now', c, fast=True) == 'Checking now'
    assert calls == ['Checking now']


def test_ordered_text_boundary_cards_and_output_redaction(monkeypatch, tmp_path):
    bot, topics = rig()
    monkeypatch.setattr(hooks, 'spawn', lambda *a, **k: None)
    transcript = tmp_path / 't.jsonl'
    transcript.write_text(json.dumps({'type': 'assistant', 'uuid': 'a', 'message': {'role': 'assistant',
        'content': [{'type': 'text', 'text': 'A short update'}]}}) + '\n')
    c = cfg(target_language='en')
    data = {'transcript_path': str(transcript), 'tool_name': 'Read',
            'tool_input': {'file_path': '/safe/file'}, 'tool_response': 'token=secret123456789'}  # gitleaks:allow (dummy value for the redaction test)
    progress.append_progress('s', data, c)
    progress.append_boundary('s')
    progress.append_progress('s', data, c)
    progress.run_send_progress(c, 's', bot=bot, throttle=0)
    texts = [p['text'] for p in bot.methods('sendMessage')]
    assert texts.count('A short update') == 1
    assert any('last updated' in t for t in texts)
    assert all('secret123456789' not in t for t in texts)
    cards = [p for p in bot.methods('sendMessage') if 'last updated' in p['text']]
    assert len(cards) == 2  # boundary freezes the first card
    assert 'a:0' in hooks._seen('s')
    count = len(bot.calls)
    progress.run_send_progress(c, 's', bot=bot, throttle=0)
    assert len(bot.calls) == count
    progress.run_end_progress(c, 's', bot=bot)
    assert not any(p['message_id'] in {i for i, (m, p) in enumerate(bot.calls, 1)
                                     if m == 'sendMessage' and 'last updated' in p['text']}
                   for p in bot.methods('deleteMessage'))


def test_stuck_once_per_busy_period_and_daily_is_unpinned(tmp_path):
    bot, topics = rig()
    transcript = tmp_path / 's.jsonl'
    transcript.write_text('')
    os.utime(transcript, (100, 100))
    set_state(cfg(), 's', 'busy', bot)
    find = lambda session: str(transcript)
    dashboard.stuck_check(cfg(), topics.all(), bot, now=800, find=find)
    dashboard.stuck_check(cfg(), topics.all(), bot, now=900, find=find)
    warnings = [p for p in bot.methods('sendMessage') if '⚠️' in p['text']]
    assert len(warnings) == 1
    bot.calls.clear()
    assert dashboard.run(cfg(), daily=True, bot=bot, now=900, find=find) == 0
    sent = bot.methods('sendMessage')
    assert len(sent) == 1 and 'disable_notification' not in sent[0]
    assert not bot.methods('pinChatMessage')
    bot.calls.clear()
    dashboard.run(cfg(), print_only=True, bot=bot, now=900, find=find)
    assert bot.calls == []
