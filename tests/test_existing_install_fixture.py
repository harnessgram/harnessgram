"""Checks for the shared existing-install test setup."""
import json

from ctb import config


def test_existing_install_fixture_defaults_and_explicit_choices():
    names = ("auto_topic_names", "topic_check_reminders", "auto_account_switch",
             "quota_auto_continue", "session_auto_revive", "progress", "progress_details")
    assert all(getattr(config.Config(), name) for name in names)
    assert not any(getattr(config.Config(**{name: False for name in names}), name) for name in names)


def test_existing_install_fixture_migrates_without_overriding_choices():
    config.write_private(config.config_path(), '{"progress": false}')
    cfg = config.load()
    assert not cfg.progress and cfg.auto_account_switch and cfg.topic_check_reminders
    assert json.loads(config.config_path().read_text())["release_defaults_v1"] is True
