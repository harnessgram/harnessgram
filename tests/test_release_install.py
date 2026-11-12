import json
import shlex
from pathlib import Path

import pytest

from ctb import doctor, install as I, radio, radio_youtube


@pytest.mark.parametrize("sample", ["space name", "quote'\"name", "100%", "cost$5", r"back\slash"])
def test_systemd_values_roundtrip(sample, monkeypatch):
    executable = "/opt/" + sample + "/ctb"
    argument = "arg " + sample
    env = "/tmp/" + sample
    monkeypatch.setattr(I.launch, "argv", lambda: [executable, argument])
    monkeypatch.setattr(I, "state_dir", lambda: Path(env))
    monkeypatch.setattr(I, "_env", lambda: {"CTB_STATE_DIR": env})
    unit = I.unit_text()
    values = dict(line.split("=", 1) for line in unit.splitlines() if "=" in line)
    # Decode the systemd word syntax and its specifier/environment expansion separately.
    args = shlex.split(values["ExecStart"])
    assert args[0].replace("%%", "%") == executable
    assert args[1].replace("%%", "%").replace("$$", "$") == argument
    assert shlex.split(values["Environment"])[0].replace("%%", "%") == "CTB_STATE_DIR=" + env
    assert values["WorkingDirectory"].replace("%%", "%") == env
    assert "Restart=always" in unit and "KillMode=process" in unit


def test_install_statusline_preserves_settings_and_uninstall_only_own(tmp_path, monkeypatch, capsys):
    from ctb import hooks
    monkeypatch.setattr(hooks, "wait_idle", lambda _: True)
    monkeypatch.setattr(I.launch, "argv", lambda: ["/sample path/ctb"])
    path = tmp_path / "settings.json"
    original = {"theme": "dark", "statusLine": {"type": "command", "command": "other"}}
    path.write_text(json.dumps(original))
    assert I.install(service=False, settings_file=path) == 0
    first = json.loads(path.read_text())
    assert first["statusLine"] == original["statusLine"] and first["theme"] == "dark"
    assert "--wire-statusline" in capsys.readouterr().out
    assert I.remove_settings(first)[0] == original
    assert I.install(service=False, settings_file=path, wire_statusline=True) == 0
    wired = json.loads(path.read_text())
    assert I.statusline_wired(wired) and wired["theme"] == "dark"
    assert "statusLine" not in I.remove_settings(wired)[0]


def test_doctor_warns_when_statusline_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(doctor, "check_config", lambda: ([], None))
    for name in ("check_tmux", "check_claude", "check_listener", "check_hooks", "check_running_code"):
        monkeypatch.setattr(doctor, name, lambda: [])
    assert any(level == "warn" and "--wire-statusline" in msg for level, msg in doctor.collect(offline=True))


def test_uninstall_stops_both_radios_without_starting_youtube(tmp_path, monkeypatch):
    from ctb import config
    monkeypatch.setattr(I.sys, "platform", "darwin")
    monkeypatch.setattr(radio, "folder", lambda: tmp_path / "team")
    monkeypatch.setattr(radio, "_job_loaded", lambda: False)
    monkeypatch.setattr(radio, "plist_path", lambda: tmp_path / "team.plist")
    monkeypatch.setattr(radio, "wanted", lambda: True)
    monkeypatch.setattr(radio_youtube, "_state", lambda: {"broadcast_id": "sample"})
    monkeypatch.setattr(radio_youtube, "_job_on", lambda: False)
    monkeypatch.setattr(radio_youtube, "plist_path", lambda: tmp_path / "youtube.plist")
    monkeypatch.setattr(config, "load", lambda: object())
    calls = []
    monkeypatch.setattr(radio, "stop", lambda cfg: calls.append("team") or "radio_stopped")
    monkeypatch.setattr(radio_youtube, "stop", lambda cfg: calls.append("youtube") or "youtube_stopped")
    monkeypatch.setattr(radio_youtube, "start", lambda *a: pytest.fail("must not start"))
    assert I._uninstall_radios(True) and calls == []
    assert I._uninstall_radios(False) and calls == ["youtube", "team"]
    monkeypatch.setattr(radio_youtube, "stop", lambda cfg: "youtube_stopped_left")
    assert not I._uninstall_radios(False)


def test_uninstall_goes_on_when_a_radio_stop_fails(tmp_path, monkeypatch, capsys):
    import json
    from ctb import install as I
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(I.merge_settings({}, True)[0]))
    monkeypatch.setattr(I, "_uninstall_radios", lambda dry_run: False)
    monkeypatch.setattr(I, "SERVICES", [])
    assert I.uninstall(settings_file=settings) == 1  # the failure is reported ...
    assert I.merge_settings(json.loads(settings.read_text()), True)[0] != json.loads(settings.read_text())  # ... and the hooks are gone
