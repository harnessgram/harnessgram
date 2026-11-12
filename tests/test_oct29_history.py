import json
import uuid

import pytest

from ctb import inbox as I
from ctb.config import Config
from ctb.topics import TopicMap


@pytest.fixture
def context(tmp_path, bot, monkeypatch):
    cfg = Config(owner_chat_id=7)
    topics = TopicMap(tmp_path / 'topics.json', bot, 7)
    box = I.Inbox(tmp_path / 'box')
    msg = {'chat': {'id': 7, 'type': 'private'}, 'from': {'id': 7},
           'message_thread_id': 42, 'message_id': 12, 'date': 2000000000}
    monkeypatch.setattr(I, 'projects', lambda *a, **k: ['/work/one', '/work/general'])
    return cfg, topics, box, msg


def callback(context, bot, menu, data, sender=7):
    cfg, topics, box, _ = context
    return I.handle_button({'id': 'q', 'from': {'id': sender}, 'message': menu, 'data': data},
                           cfg, bot, topics, box)


def test_folder_paging_search_and_new_prompt(context, bot, monkeypatch, tmp_path):
    cfg, topics, box, msg = context
    items = [{'sid': str(uuid.uuid4()), 'title': f'topic {i}', 'mtime': i,
              'path': str(tmp_path / str(i)), 'folder': '/work/one'} for i in range(10)]
    monkeypatch.setattr(I, 'history_conversations', lambda folder=None: items)
    assert I.handle({'message': dict(msg, text='/history')}, cfg, bot, topics, box, None, 0) == 'history'
    menu_id = bot.methods('editMessageText')[-1]['message_id']
    menu = dict(msg, message_id=menu_id)
    assert callback(context, bot, menu, 'h:f:0') == 'history'
    rows = bot.methods('editMessageText')[-1]['reply_markup']['inline_keyboard']
    assert len(rows) == 11 and rows[-1][0]['callback_data'] == 'more:dismiss'  # 8 items, paging, search, close
    assert all(len(button['callback_data'].encode()) <= 64 for row in rows for button in row)
    callback(context, bot, menu, 'h:p:1')
    rows = bot.methods('editMessageText')[-1]['reply_markup']['inline_keyboard']
    assert len(rows) == 5 and 'topic 8' in rows[0][0]['text']
    edits = len(bot.methods('editMessageText'))
    callback(context, bot, menu, 'h:search')
    assert len(bot.methods('editMessageText')) == edits
    assert bot.methods('sendMessage')[-1]['message_thread_id'] == 42
    assert 'חיפוש' in bot.methods('sendMessage')[-1]['text']
    assert I.handle({'message': dict(msg, text='topic 9')}, cfg, bot, topics, box, None, 0) == 'history'
    result = bot.methods('editMessageText')[-1]
    assert result['message_id'] != menu_id
    assert 'topic 9' in result['reply_markup']['inline_keyboard'][0][0]['text']


def test_auth_expiry_and_bound_menu(context, bot, monkeypatch):
    cfg, topics, box, msg = context
    I.history_start(cfg, bot, msg)
    menu = dict(msg, message_id=bot.methods('editMessageText')[-1]['message_id'])
    assert callback(context, bot, menu, 'h:search', sender=8) == 'ignored'
    assert not I.history_consume_search(cfg, bot, msg, 'work')
    callback(context, bot, dict(menu, message_thread_id=43), 'h:search')
    assert not I.history_consume_search(cfg, bot, msg, 'work')
    callback(context, bot, menu, 'h:search')
    monkeypatch.setattr(I.time, 'time', lambda: 9999999999)
    assert not I.history_consume_search(cfg, bot, msg, 'work')


def test_records_search_and_open(context, bot, monkeypatch, tmp_path):
    cfg, topics, box, msg = context
    root = tmp_path / 'claude' / 'projects'
    folder = tmp_path / 'work'
    folder.mkdir()
    directory = root / str(folder).replace('/', '-')
    directory.mkdir(parents=True)
    sid = str(uuid.uuid4())
    record = {'type': 'user', 'cwd': str(folder), 'message': {'content': 'find this phrase'}}
    (directory / (sid + '.jsonl')).write_text(json.dumps(record) + '\n' + ' ' * 2048)
    monkeypatch.setattr(I.status, 'projects_dir', lambda: root)
    items = I.history_conversations(str(folder))
    assert [x['sid'] for x in items] == [sid]
    assert I.history_search(items, 'THIS PHRASE') == items
    calls = []
    monkeypatch.setattr(I, '_tmux', lambda *args: calls.append(args) or type('Result', (), {'returncode': 0})())
    I.history_open(cfg, bot, msg, topics, items[0])
    assert topics.thread_of(sid)
    assert calls[0][0] == 'new-session' and '--resume ' + sid in calls[0][-1]
    assert not any(call[0] == 'kill-session' for call in calls)


def test_search_user_text_and_reject_secret(context, bot, tmp_path):
    cfg, topics, box, msg = context
    path = tmp_path / 'transcript.jsonl'
    path.write_text(json.dumps({'type': 'user', 'message': {'content': [
        {'type': 'text', 'text': 'A hidden phrase'}]}}))
    item = {'sid': str(uuid.uuid4()), 'title': 'different', 'path': str(path)}
    assert I.history_search([item], 'HIDDEN') == [item]
    I.history_start(cfg, bot, msg)
    menu = dict(msg, message_id=bot.methods('editMessageText')[-1]['message_id'])
    callback(context, bot, menu, 'h:search')
    secret = 'sk-ant-api03-' + 'x' * 100
    assert I.handle({'message': dict(msg, text=secret)}, cfg, bot, topics, box, None, 0) == 'history'
    assert all(secret not in str(params) for _, params in bot.calls)
