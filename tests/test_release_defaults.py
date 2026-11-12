"""Release-default guards use isolated sample state only."""
import json

import pytest

from ctb import accounts, autopilot, config, panel_settings, progress, topic_names

pytestmark = pytest.mark.new_install


def test_new_defaults_are_conservative():
    cfg = config.from_dict({})
    assert not any((cfg.auto_account_switch, cfg.quota_auto_continue, cfg.session_auto_revive,
                    cfg.progress, cfg.progress_details, cfg.auto_topic_names))


@pytest.mark.parametrize('opt_out', [False, True])
def test_existing_config_migrates_once_and_preserves_explicit_values(tmp_path, monkeypatch, opt_out):
    monkeypatch.setenv('CTB_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('CTB_STATE_DIR', str(tmp_path / 'state'))
    path = config.config_path()
    config.write_private(path, json.dumps({'progress': False, 'auto_topic_names': False}))
    config.write_private(config.state_dir() / 'topics.json', '{}')
    if opt_out:
        config.write_private(config.state_dir() / 'account-auto-off', '')
    cfg = config.load()
    raw = json.loads(path.read_text())
    assert cfg.auto_account_switch is (not opt_out)
    assert cfg.quota_auto_continue and cfg.session_auto_revive and cfg.progress_details
    assert not cfg.progress and not cfg.auto_topic_names
    assert raw['release_defaults_v1'] is True
    panel_settings.write('quota_auto_continue', False)
    assert config.load().quota_auto_continue is False
    assert json.loads(path.read_text())['quota_auto_continue'] is False


@pytest.mark.parametrize('entries', [
    ('account-current',), ('account-limits.json',), ('activity-level', 'session-accounts.json'),
    ('old-directory/',), ('account-auto-off',),
])
def test_any_previous_state_identifies_existing_install(tmp_path, monkeypatch, entries):
    monkeypatch.setenv('CTB_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('CTB_STATE_DIR', str(tmp_path / 'state'))
    config.write_private(config.config_path(), '{}')
    for name in entries:
        if name.endswith('/'):
            (config.state_dir() / name).mkdir(parents=True)
        else:
            config.write_private(config.state_dir() / name, 'sample')
    cfg = config.load()
    assert cfg.quota_auto_continue and cfg.session_auto_revive and cfg.progress
    assert cfg.progress_details and cfg.auto_topic_names and cfg.topic_check_reminders
    assert cfg.auto_account_switch is ('account-auto-off' not in entries)


@pytest.mark.parametrize('state_exists', [False, True])
def test_empty_or_missing_state_is_new_install(tmp_path, monkeypatch, state_exists):
    monkeypatch.setenv('CTB_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('CTB_STATE_DIR', str(tmp_path / 'state'))
    config.write_private(config.config_path(), '{}')
    if state_exists:
        config.state_dir().mkdir()
    cfg = config.load()
    assert not any((cfg.auto_account_switch, cfg.quota_auto_continue, cfg.session_auto_revive,
                    cfg.progress, cfg.progress_details, cfg.auto_topic_names, cfg.topic_check_reminders))


def test_load_does_not_count_its_own_config_and_lock_as_prior_state(tmp_path, monkeypatch):
    monkeypatch.setenv('CTB_CONFIG_DIR', str(tmp_path / 'shared'))
    monkeypatch.setenv('CTB_STATE_DIR', str(tmp_path / 'shared'))
    config.write_private(config.config_path(), '{}')
    cfg = config.load()
    assert not cfg.auto_account_switch and not cfg.progress and not cfg.quota_auto_continue


def test_failed_migration_save_returns_settings_and_warns_once(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv('CTB_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('CTB_STATE_DIR', str(tmp_path / 'state'))
    config.write_private(config.config_path(), '{"progress": false}')
    config.write_private(config.state_dir() / 'account-current', 'sample')
    def fail_save(*args, **kwargs):
        raise OSError('sample disk full')
    monkeypatch.setattr(config, 'save_raw', fail_save)
    cfg = config.load()
    assert cfg.auto_account_switch and cfg.quota_auto_continue
    assert not cfg.progress
    assert json.loads(config.config_path().read_text()) == {'progress': False}
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == 'WARNING'
    assert 'sample disk full' in caplog.records[0].message


@pytest.mark.parametrize('via_panel', [False, True])
def test_auto_switch_changes_sync_legacy_marker(tmp_path, monkeypatch, via_panel):
    monkeypatch.setenv('CTB_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('CTB_STATE_DIR', str(tmp_path / 'state'))
    config.write_private(config.config_path(), '{"release_defaults_v1": true, "auto_account_switch": true}')
    marker = config.state_dir() / 'account-auto-off'
    for enabled in (False, True, False):
        if via_panel:
            panel_settings.write('auto_account_switch', enabled)
        else:
            assert accounts.toggle_auto() is enabled
        assert config.load().auto_account_switch is enabled
        assert marker.exists() is (not enabled)


def test_switch_settings_are_typed_and_persistent(tmp_path, monkeypatch):
    monkeypatch.setenv('CTB_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('CTB_STATE_DIR', str(tmp_path / 'state'))
    config.write_private(config.config_path(), '{"release_defaults_v1": true}')
    for key in ('auto_account_switch', 'quota_auto_continue', 'session_auto_revive', 'progress', 'progress_details', 'auto_topic_names'):
        assert panel_settings.allowed(key, True)
        assert not panel_settings.allowed(key, 'true')
        panel_settings.write(key, True)
        assert panel_settings.read()[key] is True
    assert accounts.auto_enabled()
    assert accounts.toggle_auto() is False
    assert not config.load().auto_account_switch


def test_unknown_quota_room_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv('CTB_STATE_DIR', str(tmp_path))
    monkeypatch.setattr(accounts, 'account_for', lambda _: 'sample')
    monkeypatch.setattr(accounts, 'usage_path', lambda _: tmp_path / 'usage.json')
    (tmp_path / 'usage.json').write_text('{"unrelated": 1}')
    assert autopilot.account_room('sample', now=1000) is None


def test_labels_only_progress_has_no_detail_or_output(tmp_path, monkeypatch):
    monkeypatch.setenv('CTB_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('CTB_STATE_DIR', str(tmp_path / 'state'))
    from ctb import hooks
    monkeypatch.setattr(hooks, 'spawn', lambda *a, **k: None)
    cfg = config.Config(bot_token='sample', owner_chat_id=1, progress=True)
    progress.append_progress('sample', {'tool_name': 'Read', 'tool_input': {'file_path': 'sensitive-sample'},
                                       'tool_response': 'private-sample'}, cfg, root=tmp_path / 'progress')
    data = (tmp_path / 'progress' / 'sample.jsonl').read_text()
    assert 'sensitive-sample' not in data and 'private-sample' not in data
    assert 'Read' in data


def test_topic_naming_is_off_without_opt_in(monkeypatch):
    monkeypatch.setattr(topic_names, 'plan', lambda *a, **k: pytest.fail('naming ran'))
    assert topic_names.run(config.Config()) == []


@pytest.mark.parametrize("enabled,room,expected", [(False, 10, 0), (False, None, 0), (True, None, 1), (True, 10, 1), (True, 95, 0)])
def test_quota_continuation_respects_setting_and_room(tmp_path, monkeypatch, enabled, room, expected):
    from ctb import autopilot
    monkeypatch.setenv('CTB_STATE_DIR', str(tmp_path))
    transcript = tmp_path / 'sample.txt'
    transcript.write_text('sample')
    monkeypatch.setattr(autopilot, 'away_active', lambda: False)
    monkeypatch.setattr(autopilot, 'load_memory', lambda: {})
    monkeypatch.setattr(autopilot, 'save_memory', lambda *a: None)
    monkeypatch.setattr(autopilot, 'TopicMap', lambda *a, **k: type('Topics', (), {'all_places': lambda self: {'sample': {'thread': 1}}})())
    monkeypatch.setattr(autopilot, 'pane_for_session', lambda _: 'sample')
    monkeypatch.setattr(autopilot, 'find_transcript', lambda _: transcript)
    monkeypatch.setattr(autopilot, 'last_turn', lambda _: ('sample', 'waiting for 5-hour quota to reset'))
    monkeypatch.setattr(autopilot, 'account_room', lambda *a, **k: room)
    monkeypatch.setattr(autopilot, 'at_prompt', lambda _: True)
    calls = []
    monkeypatch.setattr(autopilot, 'type_into', lambda *a: calls.append(a))
    cfg = config.Config(quota_auto_continue=enabled)
    autopilot._run_locked(cfg, None, False, transcript.stat().st_mtime + 1000, 1)
    assert len(calls) == expected
    if room is None and expected:
        assert '?%' in calls[0][1]


def test_handwritten_config_uses_safe_defaults_and_keeps_choices(tmp_path, monkeypatch):
    monkeypatch.setenv('CTB_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('CTB_STATE_DIR', str(tmp_path / 'state'))
    config.write_private(config.config_path(), json.dumps({'progress': True}))
    cfg = config.load()
    assert cfg.progress is True
    assert not any((cfg.auto_account_switch, cfg.quota_auto_continue, cfg.session_auto_revive,
                    cfg.progress_details, cfg.auto_topic_names, cfg.topic_check_reminders))
    assert json.loads(config.config_path().read_text())['release_defaults_v1'] is True


def test_pair_writes_safe_defaults(tmp_path, monkeypatch):
    from ctb import cli, pairing, telegram
    from conftest import GOOD_TOKEN
    monkeypatch.setenv('CTB_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('CTB_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('CTB_BOT_TOKEN', GOOD_TOKEN)
    monkeypatch.setattr(telegram, 'Bot', lambda token: type('Bot', (), {'call': lambda self, *args: {}})())
    monkeypatch.setattr(pairing, 'new_code', lambda: 'sample')
    monkeypatch.setattr(pairing, 'wait_for_start', lambda *args: {'from': {'id': 1}, 'chat': {'id': 1}})
    monkeypatch.setattr('builtins.input', lambda _: 'y')
    assert cli._cmd_pair() == 0
    raw = json.loads(config.config_path().read_text())
    assert raw['release_defaults_v1'] is True
    cfg = config.load()
    assert not any((cfg.auto_account_switch, cfg.quota_auto_continue, cfg.session_auto_revive,
                    cfg.progress, cfg.progress_details, cfg.auto_topic_names, cfg.topic_check_reminders))


def test_listener_automation_reads_current_switches(tmp_path, monkeypatch):
    from ctb import dashboard, inbox
    monkeypatch.setenv('CTB_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('CTB_STATE_DIR', str(tmp_path / 'state'))
    config.write_private(config.config_path(), json.dumps({'release_defaults_v1': True,
                         'quota_auto_continue': True, 'auto_topic_names': True}))
    seen = []
    monkeypatch.setattr(dashboard, 'run', lambda cfg: seen.append(('quota', cfg.quota_auto_continue)))
    monkeypatch.setattr(topic_names, 'run', lambda cfg, *args: seen.append(('names', cfg.auto_topic_names)))
    inbox._refresh_dashboard_live()
    inbox._run_topic_names_live(None, None)
    panel_settings.write('quota_auto_continue', False)
    panel_settings.write('auto_topic_names', False)
    inbox._refresh_dashboard_live()
    inbox._run_topic_names_live(None, None)
    assert seen == [('quota', True), ('names', True), ('quota', False), ('names', False)]


def test_heartbeat_does_not_revive_when_disabled(monkeypatch):
    from ctb import heartbeat, revive, buildstamp
    monkeypatch.setattr(heartbeat.Bot, 'from_config', lambda _: object())
    calls = []
    monkeypatch.setattr(revive, 'revive_in_background', lambda: calls.append('revive'))
    monkeypatch.setattr(buildstamp, 'record_running', lambda *a: None)
    monkeypatch.setattr(buildstamp, 'clear_running', lambda *a: None)
    monkeypatch.setattr(heartbeat, 'run_once', lambda *a, **k: None)
    monkeypatch.setattr('ctb.policy.on', lambda: False)
    def stop(_):
        raise StopIteration
    revive.revived.clear()
    with pytest.raises(StopIteration):
        heartbeat.run(config.Config(bot_token='sample', owner_chat_id=1), sleep_fn=stop)
    assert calls == []
    revive.revived.clear()


def test_setting_labels_exist_in_each_language():
    import re
    from ctb.panel_page import STRINGS, PAGE
    for key in ('auto_account_switch', 'quota_auto_continue', 'session_auto_revive', 'progress', 'progress_details', 'auto_topic_names'):
        assert '"' + key + '"' in PAGE
        for lang in ('en', 'he'):
            label, hint = (STRINGS[lang][prefix + key] for prefix in ('set_', 'hint_'))
            assert label and hint
            assert not re.search(r'[A-Za-z]', label + hint) if lang == 'he' else not re.search(r'[\u0590-\u05ff]', label + hint)


def test_new_setup_writes_explicit_disabled_choices(tmp_path, monkeypatch):
    from ctb import setup
    from conftest import GOOD_TOKEN
    monkeypatch.setenv('CTB_CONFIG_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('CTB_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(setup, '_token', lambda *a, **k: GOOD_TOKEN)
    monkeypatch.setattr(setup, 'check_bot', lambda bot: {'username': 'sample'})
    monkeypatch.setattr(setup, '_threaded_mode', lambda *a: False)
    monkeypatch.setattr(setup, 'install_step', lambda *a: None)
    setup.run(owner_id=1, language='en', interactive=False, install_hooks=False, service=False,
              start=False, test_message=False, run_doctor=False, bot=object(), say=lambda _: None)
    raw = json.loads(config.config_path().read_text())
    assert raw['release_defaults_v1'] is True
    for key in ('auto_account_switch', 'quota_auto_continue', 'session_auto_revive', 'progress', 'progress_details', 'auto_topic_names'):
        assert raw[key] is False
