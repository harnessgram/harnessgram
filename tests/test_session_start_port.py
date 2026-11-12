import json

from ctb import install, wake


def test_session_start_records_only_valid_tmux_panes(tmp_path, monkeypatch):
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TMUX_PANE", "%42")
    monkeypatch.setattr(wake, "_pane_is_ours", lambda pane, chain: True)  # the real check asks tmux
    assert wake.hook_start(json.dumps({"session_id": "abc"})) == 0
    assert json.loads((tmp_path / "tmux-panes.json").read_text()) == {"abc": "%42"}
    monkeypatch.setenv("TMUX_PANE", "other:window")
    wake.hook_start(json.dumps({"session_id": "other"}))
    assert json.loads((tmp_path / "tmux-panes.json").read_text()) == {"abc": "%42"}
    assert (tmp_path / "tmux-panes.json").stat().st_mode & 0o777 == 0o600


def test_session_start_hook_installed_and_removed():
    settings, _ = install.merge_settings({})
    assert "hook-start" in settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert install.remove_settings(settings)[0] == {}
