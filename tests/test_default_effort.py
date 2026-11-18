"""Default Claude effort is read and updated only through temporary profile fixtures."""
import json
import os
import stat

import pytest

from ctb import accounts, panel_page, panel_settings


def profiles(tmp_path, monkeypatch, values):
    home = tmp_path / ".claude"
    root = tmp_path / ".claude-profiles"
    home.mkdir()
    root.mkdir()
    for name, value in values.items():
        folder = home if name == "main" else root / name
        folder.mkdir(exist_ok=True)
        (folder / "settings.json").write_text(
            json.dumps({"theme": "dark", "effortLevel": value, "hooks": {"Stop": []}}, indent=4) + "\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("CTB_CLAUDE_HOME", str(home))
    monkeypatch.setenv("CTB_PROFILES_DIR", str(root))
    return home, root


def test_reads_current_and_mixed_profile_values(tmp_path, monkeypatch):
    home, root = profiles(tmp_path, monkeypatch, {"main": "high", "work": "high"})
    assert accounts.effort_level() == "high"
    data = json.loads((root / "work" / "settings.json").read_text())
    data["effortLevel"] = "low"
    (root / "work" / "settings.json").write_text(json.dumps(data))
    assert accounts.effort_level() == accounts.MIXED_EFFORT


def test_reads_default_models_effort_before_legacy_top_level(tmp_path, monkeypatch):
    home, _ = profiles(tmp_path, monkeypatch, {"main": "low"})
    path = home / "settings.json"
    data = json.loads(path.read_text())
    data["modelSettings"] = {"claude-opus-5-5": {"effortLevel": "high"}}
    path.write_text(json.dumps(data))
    assert accounts.effort_level() == "high"


def test_writes_every_profile_and_preserves_keys_order_mode_and_backup(tmp_path, monkeypatch):
    home, root = profiles(tmp_path, monkeypatch, {"main": "low", "work": "high"})
    paths = [home / "settings.json", root / "work" / "settings.json"]
    originals = {}
    for i, path in enumerate(paths):
        os.chmod(path, 0o640 + i)
        originals[path] = path.read_bytes()
    accounts.set_effort_level("xhigh")
    for i, path in enumerate(paths):
        data = json.loads(path.read_text())
        assert list(data) == ["theme", "effortLevel", "hooks", "modelSettings"]
        assert data == {"theme": "dark", "effortLevel": "xhigh", "hooks": {"Stop": []},
                        "modelSettings": {"claude-opus-5-5": {"effortLevel": "xhigh"}}}
        assert stat.S_IMODE(path.stat().st_mode) == 0o640 + i
        backup = path.with_name("settings.json.bak-effort")
        assert backup.read_bytes() == originals[path]
    first_backups = {path: path.with_name("settings.json.bak-effort").read_bytes() for path in paths}
    accounts.set_effort_level("medium")
    for path in paths:
        assert path.with_name("settings.json.bak-effort").read_bytes() == first_backups[path]


def test_rejects_unknown_effort_without_touching_profiles(tmp_path, monkeypatch):
    home, _ = profiles(tmp_path, monkeypatch, {"main": "high"})
    before = (home / "settings.json").read_bytes()
    with pytest.raises(ValueError):
        accounts.set_effort_level("unlimited")
    assert (home / "settings.json").read_bytes() == before
    assert not (home / "settings.json.bak-effort").exists()
    assert not panel_settings.allowed("default_effort", "unlimited")


def test_panel_contains_localized_default_effort_row():
    assert "set_default_effort" in panel_page.PAGE
    assert "רמת חשיבה לסשנים חדשים" == panel_page.STRINGS["he"]["set_default_effort"]
    assert "Thinking level for new sessions" == panel_page.STRINGS["en"]["set_default_effort"]
    assert "שונה בין החשבונות" == panel_page.STRINGS["he"]["effort_mixed"]
    assert accounts.EFFORT_LEVELS == ("low", "medium", "high", "xhigh")
    assert 'var EFFORT=["low","medium","high","xhigh"]' in panel_page.PAGE


def test_updates_model_settings_and_does_not_replace_nested_effort(tmp_path, monkeypatch):
    home, _ = profiles(tmp_path, monkeypatch, {"main": "low"})
    path = home / "settings.json"
    path.write_text(json.dumps({"env": {"effortLevel": "low"}, "effortLevel": "high",
                                "modelSettings": {"claude-opus-5-5": {"effortLevel": "low"},
                                                  "other": {"temperature": 1}}}, indent=2) + "\n")
    accounts.set_effort_level("xhigh")
    data = json.loads(path.read_text())
    assert data["env"]["effortLevel"] == "low"
    assert data["effortLevel"] == "xhigh"
    assert all(value["effortLevel"] == "xhigh" for value in data["modelSettings"].values())


def test_creates_effort_entries_for_default_and_profiles_full_model(tmp_path, monkeypatch):
    home, _ = profiles(tmp_path, monkeypatch, {"main": "low"})
    path = home / "settings.json"
    path.write_text(json.dumps({"model": "claude-sonnet-5-5", "theme": "dark"}))
    accounts.set_effort_level("high")
    data = json.loads(path.read_text())
    assert data["effortLevel"] == "high"
    assert data["modelSettings"] == {
        "claude-opus-5-5": {"effortLevel": "high"},
        "claude-sonnet-5-5": {"effortLevel": "high"},
    }


def test_missing_settings_is_created_private(tmp_path, monkeypatch):
    home, _ = profiles(tmp_path, monkeypatch, {"main": "high"})
    (home / "settings.json").unlink()
    assert accounts.set_effort_level("medium") == [{"profile": "main", "ok": True}]
    assert json.loads((home / "settings.json").read_text()) == {
        "effortLevel": "medium", "modelSettings": {"claude-opus-5-5": {"effortLevel": "medium"}}}
    assert stat.S_IMODE((home / "settings.json").stat().st_mode) == 0o600


def test_settings_symlink_must_resolve_to_settings_json_inside_config_roots(tmp_path, monkeypatch):
    home, root = profiles(tmp_path, monkeypatch, {"main": "low", "work": "high"})
    shared_dir = home / "shared"
    shared_dir.mkdir()
    shared = shared_dir / "settings.json"
    shared.write_text('{"effortLevel":"low"}')
    (home / "settings.json").unlink()
    (home / "settings.json").symlink_to(shared)
    outside = tmp_path / "outside.json"
    outside.write_text('{"effortLevel":"low"}')
    work = root / "work" / "settings.json"
    work.unlink()
    work.symlink_to(outside)
    results = accounts.set_effort_level("high")
    assert (home / "settings.json").is_symlink()
    assert json.loads(shared.read_text())["effortLevel"] == "high"
    assert json.loads(outside.read_text())["effortLevel"] == "low"
    assert results == [{"profile": "main", "ok": True},
                       {"profile": "work", "ok": False, "error": "outside profiles"}]


def test_settings_symlink_cannot_target_another_file_in_config_root(tmp_path, monkeypatch):
    home, _ = profiles(tmp_path, monkeypatch, {"main": "low"})
    credentials = home / ".credentials.json"
    credentials.write_text('{"token":"secret"}')
    (home / "settings.json").unlink()
    (home / "settings.json").symlink_to(credentials)
    assert accounts.set_effort_level("high") == [
        {"profile": "main", "ok": False, "error": "not settings.json"}]
    assert json.loads(credentials.read_text()) == {"token": "secret"}


def test_names_ignores_symlinked_profile_directories(tmp_path, monkeypatch):
    home, root = profiles(tmp_path, monkeypatch, {"main": "high"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "linked").symlink_to(elsewhere, target_is_directory=True)
    assert accounts.names() == ["main"]


def test_retries_when_settings_change_between_read_and_rename(tmp_path, monkeypatch):
    home, _ = profiles(tmp_path, monkeypatch, {"main": "low"})
    path = home / "settings.json"
    real_read = type(path).read_bytes
    reads = 0

    def racing_read(candidate):
        nonlocal reads
        if candidate == path:
            reads += 1
            if reads == 2:
                candidate.write_text('{"theme":"from-cli","effortLevel":"medium"}')
        return real_read(candidate)

    monkeypatch.setattr(type(path), "read_bytes", racing_read)
    accounts.set_effort_level("high")
    assert json.loads(path.read_text()) == {
        "theme": "from-cli", "effortLevel": "high",
        "modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}}
    assert reads >= 4


def test_panel_reports_total_profile_failure_as_failure_not_partial_success():
    assert 'j.profiles.every(function(p){return !p.ok;})' in panel_page.PAGE
    assert 'toast(failed?S.failed:j.partial?S.partlySaved:S.saved' in panel_page.PAGE
