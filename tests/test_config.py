import os
import sys

import pytest

from conftest import GOOD_TOKEN

from ctb import config as C


def test_defaults_and_coercion():
    cfg = C.from_dict({"bot_token": "  " + GOOD_TOKEN + "\n", "owner_chat_id": "123", "unknown_key": 1})
    assert cfg.bot_token == GOOD_TOKEN  # whitespace stripped
    assert cfg.owner_chat_id == 123 and cfg.target_language == "he" and cfg.translate is False
    assert cfg.ready


def test_bad_values_are_rejected():
    with pytest.raises(C.ConfigError):
        C.from_dict({"owner_chat_id": "abc"})
    with pytest.raises(C.ConfigError):
        C.from_dict({"translate": "yes"})
    with pytest.raises(C.ConfigError):
        C.from_dict({"engines": "google"})


def test_original_and_stop_words_validation():
    assert C.from_dict({}).original == "edit" and "stop" in C.from_dict({}).stop_words
    assert C.from_dict({"original": "edit-off"}).original == "edit-off"
    assert C.from_dict({"original": "folded-after"}).original == "folded-after"
    assert C.from_dict({"original": "after", "stop_words": ["halt"]}).stop_words == ["halt"]
    with pytest.raises(C.ConfigError):
        C.from_dict({"original": "sideways"})
    with pytest.raises(C.ConfigError):
        C.from_dict({"stop_words": "stop"})


def test_repr_never_shows_the_token():
    cfg = C.Config(bot_token="123456:SECRETVALUE", owner_chat_id=1)
    assert "SECRETVALUE" not in repr(cfg) and "SECRETVALUE" not in str(cfg)


def test_save_is_mode_600_and_roundtrips():
    path = C.save_raw({"bot_token": GOOD_TOKEN, "owner_chat_id": 5, "translate": True})
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert C.loose_mode(path) is None
    cfg = C.load()
    assert cfg.translate is True and cfg.owner_chat_id == 5
    os.chmod(path, 0o644)
    assert C.loose_mode(path) == "644"


@pytest.mark.new_install
def test_missing_config_is_a_clear_error():
    with pytest.raises(C.ConfigError, match="no config"):
        C.load()


def test_token_from_command_is_lazy_and_cached():
    import sys
    cmd = [sys.executable, "-c", "print('123456:' + 'A' * 35)"]
    cfg = C.from_dict({"bot_token_command": cmd, "owner_chat_id": 1})
    assert cfg.ready and "AAAA" not in repr(cfg)
    assert cfg.token() == "123456:" + "A" * 35
    cfg.bot_token_command = ["/nonexistent"]  # cached: the command is not run again
    assert cfg.token().startswith("123456:")


def test_token_command_not_run_on_load_and_failure_hides_output():
    import sys
    cfg = C.from_dict({"bot_token_command": ["/nonexistent/cmd"], "owner_chat_id": 1})  # load must not run it
    with pytest.raises(C.ConfigError) as err:
        cfg.token()
    assert "nonexistent" not in str(err.value)
    bad = C.from_dict({"bot_token_command": [sys.executable, "-c", "print('leaked-secret')"], "owner_chat_id": 1})
    with pytest.raises(C.ConfigError) as err:
        bad.token()
    assert "leaked" not in str(err.value)
