"""Guard: the live settings are never replaced by another owner's (sample) settings."""
import json

import pytest

from ctb import config as C


def test_refuses_to_replace_another_owners_settings():
    C.save_raw({"bot_token": "1:x", "owner_chat_id": 123456789, "translate": True})
    with pytest.raises(C.ConfigError):
        C.save_raw({"bot_token": "1:y", "owner_chat_id": 7})
    assert json.loads(C.config_path().read_text())["owner_chat_id"] == 123456789


def test_same_owner_writes_and_keeps_a_backup():
    C.save_raw({"owner_chat_id": 5, "translate": True})
    C.save_raw({"owner_chat_id": 5, "translate": False})
    assert json.loads(C.config_path().read_text())["translate"] is False
    assert json.loads(C.config_path().with_name("config.json.bak").read_text())["translate"] is True


def test_setup_may_pair_a_new_owner():
    C.save_raw({"owner_chat_id": 5})
    C.save_raw({"owner_chat_id": 6}, replace_owner=True)
    assert json.loads(C.config_path().read_text())["owner_chat_id"] == 6


def test_refuses_the_real_settings_from_a_test(monkeypatch):
    import os, pwd
    from pathlib import Path
    real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "claude-telegram-bridge" / "config.json"
    with pytest.raises(C.ConfigError):
        C.save_raw({"owner_chat_id": 7}, real)


def test_an_explicit_temp_path_is_fine_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("CTB_CONFIG_DIR")
    C.save_raw({"owner_chat_id": 7}, tmp_path / "c.json")


def test_a_corrupt_config_is_still_backed_up():
    C.config_path().parent.mkdir(parents=True, exist_ok=True)
    C.config_path().write_text("{broken")
    C.save_raw({"owner_chat_id": 5})
    assert C.config_path().with_name("config.json.bak").read_text() == "{broken"


def test_unattended_setup_never_replaces_another_owner():
    from ctb import setup as S
    C.save_raw({"bot_token": "1:x", "owner_chat_id": 123456789})
    raw = S._existing_raw(C.config_path())
    assert raw["owner_chat_id"] == 123456789
    with pytest.raises(C.ConfigError):
        C.save_raw({**raw, "owner_chat_id": 7},
                   replace_owner=not True or not S._existing_raw(C.config_path()).get("owner_chat_id"))
