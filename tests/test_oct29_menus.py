from ctb import inbox as I
from ctb.config import Config
from ctb.topics import TopicMap


def test_skills_discovery_and_pick(tmp_path, monkeypatch, bot):
    monkeypatch.setattr(I.Path, 'home', lambda: tmp_path)
    for path in ('skills/my-skill/SKILL.md', 'plugins/cache/vendor/plugin/1/skills/tool/SKILL.md'):
        file = tmp_path / '.claude' / path
        file.parent.mkdir(parents=True)
        file.write_text('')
    assert 'my-skill' in I.skill_names()
    assert 'plugin:tool' in I.skill_names()
    cfg = Config(owner_chat_id=7)
    m = {'chat': {'id': 7, 'type': 'private'}, 'from': {'id': 7}, 'message_thread_id': 10}
    I.skills_menu(cfg, bot, m, 'my-ski')
    assert I._skill_pending(10)['name'] == 'my-skill'
    topics = TopicMap(tmp_path / 'topics', bot, 7)
    thread = topics.ensure('s', 'test')
    I._skill_pick(cfg, bot, dict(m, message_thread_id=thread), 'my-skill')
    calls = []
    monkeypatch.setattr(I, 'cli_command', lambda cfg, bot, m, session, text: calls.append(text) or 'cli')
    msg = dict(m, message_thread_id=thread, message_id=3, date=2000000000, text='-')
    assert I.handle({'message': msg}, cfg, bot, topics, I.Inbox(tmp_path / 'box'), None, 0) == 'cli'
    assert calls == ['/my-skill']


def test_skills_callback_auth_and_model_buttons(tmp_path, bot, monkeypatch):
    cfg = Config(owner_chat_id=7)
    topics = TopicMap(tmp_path / 'topics', bot, 7)
    thread = topics.ensure('s', 'test')
    box = I.Inbox(tmp_path / 'box')
    m = {'chat': {'id': 7, 'type': 'private'}, 'message_thread_id': thread, 'message_id': 2}
    q = {'id': 'q', 'from': {'id': 8}, 'message': m, 'data': 'sk:search'}
    assert I.handle_button(q, cfg, bot, topics, box) == 'ignored'
    assert I._skill_pending(thread) is None
    assert I.cli_command(cfg, bot, m, 's', '/model') == 'model'
    assert bot.methods('sendMessage')[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data'] == 'model:fable'
    called = []
    monkeypatch.setattr(I, 'cli_command', lambda cfg, bot, m, session, text, **kw: called.append(text) or 'cli')
    q.update({'from': {'id': 7}, 'data': 'model:sonnet'})
    assert I.handle_button(q, cfg, bot, topics, box) == 'cli'
    assert called == ['/model claude-sonnet-5-5']
